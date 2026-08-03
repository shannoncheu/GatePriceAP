"""Gate USDT perpetual-futures price alert bot for Telegram."""

import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

import websockets
from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.request import HTTPXRequest

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "alerts.db"))
MAX_ALERTS_PER_USER = 3
MAX_POSITIONS_PER_USER = 3
GATE_WS_URL = "wss://fx-ws.gateio.ws/v4/ws/usdt"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
# Successful Telegram polling requests are routine and otherwise make the
# terminal noisy every few seconds. Errors still remain visible.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Alert:
    id: int
    chat_id: int
    contract: str
    direction: str
    target: Decimal


@dataclass(frozen=True)
class Position:
    id: int
    chat_id: int
    contract: str
    side: str
    quantity: Decimal
    entry_price: Decimal
    leverage: Decimal


class AlertStore:
    def __init__(self, path: Path) -> None:
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                contract TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('up', 'down')),
                target TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                contract TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('long', 'short')),
                quantity TEXT NOT NULL,
                entry_price TEXT NOT NULL,
                leverage TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        self.connection.commit()

    def add(self, chat_id: int, contract: str, direction: str, target: Decimal) -> int:
        count = self.connection.execute(
            "SELECT COUNT(*) FROM alerts WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        if count >= MAX_ALERTS_PER_USER:
            raise ValueError(f"每位用户最多只能保留 {MAX_ALERTS_PER_USER} 条监控。")
        cursor = self.connection.execute(
            "INSERT INTO alerts(chat_id, contract, direction, target, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, contract, direction, str(target), int(time.time())),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_for_chat(self, chat_id: int) -> list[Alert]:
        rows = self.connection.execute(
            "SELECT id, chat_id, contract, direction, target FROM alerts WHERE chat_id = ? ORDER BY id",
            (chat_id,),
        ).fetchall()
        return [self._row_to_alert(row) for row in rows]

    def all(self) -> list[Alert]:
        rows = self.connection.execute(
            "SELECT id, chat_id, contract, direction, target FROM alerts ORDER BY id"
        ).fetchall()
        return [self._row_to_alert(row) for row in rows]

    def delete(self, chat_id: int, alert_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM alerts WHERE id = ? AND chat_id = ?", (alert_id, chat_id)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def delete_many(self, alert_ids: list[int]) -> None:
        if not alert_ids:
            return
        self.connection.executemany("DELETE FROM alerts WHERE id = ?", [(item,) for item in alert_ids])
        self.connection.commit()

    def add_position(
        self, chat_id: int, contract: str, side: str, quantity: Decimal, entry_price: Decimal, leverage: Decimal
    ) -> int:
        count = self.connection.execute(
            "SELECT COUNT(*) FROM positions WHERE chat_id = ?", (chat_id,)
        ).fetchone()[0]
        if count >= MAX_POSITIONS_PER_USER:
            raise ValueError(f"每位用户最多只能保留 {MAX_POSITIONS_PER_USER} 个持仓。")
        cursor = self.connection.execute(
            """INSERT INTO positions(chat_id, contract, side, quantity, entry_price, leverage, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, contract, side, str(quantity), str(entry_price), str(leverage), int(time.time())),
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def list_positions_for_chat(self, chat_id: int) -> list[Position]:
        rows = self.connection.execute(
            """SELECT id, chat_id, contract, side, quantity, entry_price, leverage
               FROM positions WHERE chat_id = ? ORDER BY id""",
            (chat_id,),
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def all_positions(self) -> list[Position]:
        rows = self.connection.execute(
            "SELECT id, chat_id, contract, side, quantity, entry_price, leverage FROM positions ORDER BY id"
        ).fetchall()
        return [self._row_to_position(row) for row in rows]

    def delete_position(self, chat_id: int, position_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM positions WHERE id = ? AND chat_id = ?", (position_id, chat_id)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    @staticmethod
    def _row_to_alert(row: sqlite3.Row) -> Alert:
        return Alert(row["id"], row["chat_id"], row["contract"], row["direction"], Decimal(row["target"]))

    @staticmethod
    def _row_to_position(row: sqlite3.Row) -> Position:
        return Position(
            row["id"], row["chat_id"], row["contract"], row["side"],
            Decimal(row["quantity"]), Decimal(row["entry_price"]), Decimal(row["leverage"]),
        )


def normalize_contract(value: str) -> str:
    contract = value.strip().upper().replace("-", "_").replace("/", "_")
    if "_" not in contract:
        contract += "_USDT"
    if not contract.endswith("_USDT") or not contract.replace("_", "").isalnum():
        raise ValueError("合约格式不正确，例如 BTC、BTC_USDT 或 BTC/USDT。")
    return contract


def parse_direction(value: str) -> str:
    aliases = {"up": "up", ">=": "up", "上涨": "up", "down": "down", "<=": "down", "下跌": "down"}
    direction = aliases.get(value.lower())
    if not direction:
        raise ValueError("方向只能是 up（上涨触价）或 down（下跌触价）。")
    return direction


def parse_target(value: str) -> Decimal:
    try:
        target = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("价格必须是有效的正数。") from exc
    if target <= 0:
        raise ValueError("价格必须大于 0。")
    return target


def parse_position_side(value: str) -> str:
    aliases = {"long": "long", "多": "long", "多单": "long", "short": "short", "空": "short", "空单": "short"}
    side = aliases.get(value.lower())
    if not side:
        raise ValueError("方向只能是 long（做多）或 short（做空）。")
    return side


def parse_leverage(value: str) -> Decimal:
    leverage = parse_target(value)
    if leverage > 125:
        raise ValueError("杠杆必须在 1 到 125 之间。")
    return leverage


HELP_TEXT = """<b>Gate 合约价格提醒</b>

设置提醒：<code>/set BTC 120000 up</code>
设置下跌提醒：<code>/set ETH_USDT 3000 down</code>
查询当前价格：<code>/price BTC</code>
查看提醒列表：<code>/list</code>
删除提醒：<code>/delete 1</code>

记录持仓：<code>/position BTC long 0.1 100000 10</code>
查看持仓盈亏：<code>/pnl</code>
查看持仓列表：<code>/positions</code>
删除持仓：<code>/closeposition 1</code>

<code>up</code>：价格 ≥ 目标价时触发。
<code>down</code>：价格 ≤ 目标价时触发。
持仓参数依次为：合约、方向（long/short）、数量、开仓价、杠杆。数量按币的实际数量输入。
每位用户最多可保留 3 条提醒和 3 个持仓；提醒触发后自动关闭。"""


class GateMonitor:
    def __init__(self, app: Application, store: AlertStore) -> None:
        self.app = app
        self.store = store
        self.reload_requested = asyncio.Event()
        self.task: asyncio.Task | None = None
        self.last_prices: dict[str, Decimal] = {}

    def request_reload(self) -> None:
        self.reload_requested.set()

    async def run(self) -> None:
        while True:
            contracts = sorted(
                {alert.contract for alert in self.store.all()} | {position.contract for position in self.store.all_positions()}
            )
            if not contracts:
                self.reload_requested.clear()
                await self.reload_requested.wait()
                continue
            try:
                await self._watch(contracts)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Gate WebSocket disconnected; reconnecting shortly")
                await asyncio.sleep(3)

    async def _watch(self, contracts: list[str]) -> None:
        self.reload_requested.clear()
        async with websockets.connect(GATE_WS_URL, ping_interval=20, ping_timeout=20) as ws:
            request = {
                "time": int(time.time()),
                "channel": "futures.tickers",
                "event": "subscribe",
                "payload": contracts,
            }
            await ws.send(json.dumps(request))
            logger.info("Subscribed to %s", ", ".join(contracts))
            while not self.reload_requested.is_set():
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                except TimeoutError:
                    continue
                await self._handle_message(json.loads(raw))

    async def _handle_message(self, message: dict) -> None:
        if message.get("channel") != "futures.tickers" or message.get("event") != "update":
            return
        triggered: list[tuple[Alert, Decimal]] = []
        for ticker in message.get("result", []):
            contract = ticker.get("contract")
            try:
                price = Decimal(ticker["last"])
            except (KeyError, InvalidOperation):
                continue
            self.last_prices[contract] = price
            for alert in self.store.all():
                if alert.contract != contract:
                    continue
                if (alert.direction == "up" and price >= alert.target) or (alert.direction == "down" and price <= alert.target):
                    triggered.append((alert, price))
        if not triggered:
            return
        self.store.delete_many([alert.id for alert, _ in triggered])
        self.request_reload()
        for alert, price in triggered:
            direction = "上涨至" if alert.direction == "up" else "下跌至"
            try:
                await self.app.bot.send_message(
                    chat_id=alert.chat_id,
                    text=(f"<b>价格提醒已触发</b>\n\n{alert.contract} {direction}目标价。\n"
                          f"目标价：<code>{alert.target}</code> USDT\n当前价：<code>{price}</code> USDT\n\n"
                          "该提醒已自动关闭。"),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:
                logger.exception("Could not notify chat %s for alert %s", alert.chat_id, alert.id)


def get_store(context: ContextTypes.DEFAULT_TYPE) -> AlertStore:
    return context.application.bot_data["store"]


def get_monitor(context: ContextTypes.DEFAULT_TYPE) -> GateMonitor:
    return context.application.bot_data["monitor"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_html(HELP_TEXT)


async def set_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 3:
        await update.effective_message.reply_text("用法：/set BTC 120000 up\n方向：up 或 down")
        return
    try:
        contract = normalize_contract(context.args[0])
        target = parse_target(context.args[1])
        direction = parse_direction(context.args[2])
        alert_id = get_store(context).add(update.effective_chat.id, contract, direction, target)
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    get_monitor(context).request_reload()
    relation = "≥" if direction == "up" else "≤"
    await update.effective_message.reply_html(
        f"提醒已设置 #{alert_id}\n<code>{contract}</code> 价格 {relation} <code>{target}</code> USDT"
    )


async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts = get_store(context).list_for_chat(update.effective_chat.id)
    if not alerts:
        await update.effective_message.reply_text("暂无有效提醒。")
        return
    lines = ["<b>当前提醒</b>"]
    for alert in alerts:
        relation = "≥" if alert.direction == "up" else "≤"
        lines.append(f"#{alert.id}  <code>{alert.contract}</code>  {relation} <code>{alert.target}</code> USDT")
    await update.effective_message.reply_html("\n".join(lines))


async def delete_alert(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/delete 提醒编号，例如 /delete 1")
        return
    deleted = get_store(context).delete(update.effective_chat.id, int(context.args[0]))
    if deleted:
        get_monitor(context).request_reload()
        await update.effective_message.reply_text("提醒已删除。")
    else:
        await update.effective_message.reply_text("未找到该提醒编号。")


async def price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.effective_message.reply_text("用法：/price BTC 或 /price BTC_USDT")
        return
    try:
        contract = normalize_contract(context.args[0])
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    value = get_monitor(context).last_prices.get(contract)
    if value is None:
        await update.effective_message.reply_text("该合约尚未订阅或没有行情。先用 /set 或 /position 添加；请确认合约名称正确。")
    else:
        await update.effective_message.reply_html(f"<code>{contract}</code> 最新成交价：<code>{value}</code> USDT")


def format_usdt(value: Decimal) -> str:
    return f"{value:,.2f}"


def calculate_pnl(position: Position, current_price: Decimal) -> tuple[Decimal, Decimal, Decimal]:
    multiplier = Decimal("1") if position.side == "long" else Decimal("-1")
    pnl = (current_price - position.entry_price) * position.quantity * multiplier
    initial_margin = position.entry_price * position.quantity / position.leverage
    roe = pnl / initial_margin * Decimal("100")
    return pnl, initial_margin, roe


async def set_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 5:
        await update.effective_message.reply_text(
            "用法：/position BTC long 0.1 100000 10\n参数：合约 方向 数量 开仓价 杠杆"
        )
        return
    try:
        contract = normalize_contract(context.args[0])
        side = parse_position_side(context.args[1])
        quantity = parse_target(context.args[2])
        entry_price = parse_target(context.args[3])
        leverage = parse_leverage(context.args[4])
        position_id = get_store(context).add_position(
            update.effective_chat.id, contract, side, quantity, entry_price, leverage
        )
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    get_monitor(context).request_reload()
    side_text = "多单" if side == "long" else "空单"
    await update.effective_message.reply_html(
        f"持仓已记录 #{position_id}\n<code>{contract}</code> {side_text}\n"
        f"数量：<code>{quantity}</code>\n开仓价：<code>{entry_price}</code> USDT\n"
        f"杠杆：<code>{leverage}x</code>\n使用 /pnl 查询当前未实现盈亏。"
    )


async def pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    positions = get_store(context).list_positions_for_chat(update.effective_chat.id)
    if not positions:
        await update.effective_message.reply_text("暂无记录的持仓。用 /position 添加持仓。")
        return
    prices = get_monitor(context).last_prices
    lines = ["<b>当前未实现盈亏</b>"]
    unavailable: list[str] = []
    total_pnl = Decimal("0")
    total_margin = Decimal("0")
    for position in positions:
        current_price = prices.get(position.contract)
        if current_price is None:
            unavailable.append(position.contract)
            continue
        position_pnl, margin, roe = calculate_pnl(position, current_price)
        total_pnl += position_pnl
        total_margin += margin
        side_text = "多" if position.side == "long" else "空"
        lines.append(
            f"\n#{position.id} <code>{position.contract}</code> {side_text}\n"
            f"现价：<code>{current_price}</code>  开仓：<code>{position.entry_price}</code>\n"
            f"未实现盈亏：<code>{format_usdt(position_pnl)}</code> USDT\n"
            f"收益率：<code>{roe:.2f}%</code>  保证金：<code>{format_usdt(margin)}</code> USDT"
        )
    if total_margin:
        total_roe = total_pnl / total_margin * Decimal("100")
        lines.append(
            f"\n<b>合计</b>\n未实现盈亏：<code>{format_usdt(total_pnl)}</code> USDT\n"
            f"收益率：<code>{total_roe:.2f}%</code>"
        )
    if unavailable:
        lines.append(f"\n正在获取 {', '.join(sorted(set(unavailable)))} 的行情，请稍后再次发送 /pnl。")
    await update.effective_message.reply_html("\n".join(lines))


async def list_positions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    positions = get_store(context).list_positions_for_chat(update.effective_chat.id)
    if not positions:
        await update.effective_message.reply_text("暂无记录的持仓。")
        return
    lines = ["<b>当前持仓</b>"]
    for position in positions:
        side_text = "多单" if position.side == "long" else "空单"
        lines.append(
            f"#{position.id} <code>{position.contract}</code> {side_text}  数量 <code>{position.quantity}</code>\n"
            f"开仓 <code>{position.entry_price}</code>  杠杆 <code>{position.leverage}x</code>"
        )
    await update.effective_message.reply_html("\n".join(lines))


async def close_position(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1 or not context.args[0].isdigit():
        await update.effective_message.reply_text("用法：/closeposition 持仓编号，例如 /closeposition 1")
        return
    deleted = get_store(context).delete_position(update.effective_chat.id, int(context.args[0]))
    if deleted:
        get_monitor(context).request_reload()
        await update.effective_message.reply_text("持仓记录已删除。")
    else:
        await update.effective_message.reply_text("未找到该持仓编号。")


async def post_init(app: Application) -> None:
    store = AlertStore(DATABASE_PATH)
    monitor = GateMonitor(app, store)
    app.bot_data["store"] = store
    app.bot_data["monitor"] = monitor
    monitor.task = asyncio.create_task(monitor.run(), name="gate-price-monitor")
    monitor.request_reload()


async def post_shutdown(app: Application) -> None:
    monitor: GateMonitor | None = app.bot_data.get("monitor")
    if monitor and monitor.task:
        monitor.task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor.task
    store: AlertStore | None = app.bot_data.get("store")
    if store:
        store.connection.close()


def main() -> None:
    if not TOKEN:
        raise RuntimeError("未设置 TELEGRAM_BOT_TOKEN。请复制 .env.example 为 .env 并填入 Token。")
    # Telegram can occasionally respond slowly from some VPS networks.  Keep the
    # bot alive and retry rather than aborting during the initial getMe request.
    request = HTTPXRequest(connect_timeout=20, read_timeout=30, write_timeout=30, pool_timeout=20)
    get_updates_request = HTTPXRequest(connect_timeout=20, read_timeout=35, write_timeout=30, pool_timeout=20)
    app = (
        Application.builder()
        .token(TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("set", set_alert))
    app.add_handler(CommandHandler("list", list_alerts))
    app.add_handler(CommandHandler("delete", delete_alert))
    app.add_handler(CommandHandler("price", price))
    app.add_handler(CommandHandler("position", set_position))
    app.add_handler(CommandHandler("pnl", pnl))
    app.add_handler(CommandHandler("positions", list_positions))
    app.add_handler(CommandHandler("closeposition", close_position))
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
