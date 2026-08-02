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

    @staticmethod
    def _row_to_alert(row: sqlite3.Row) -> Alert:
        return Alert(row["id"], row["chat_id"], row["contract"], row["direction"], Decimal(row["target"]))


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


HELP_TEXT = """<b>Gate 合约价格提醒机器人</b>

设置提醒：<code>/set BTC 120000 up</code>
下跌提醒：<code>/set ETH_USDT 3000 down</code>
查看当前价格：<code>/price BTC</code>
查看提醒：<code>/list</code>
删除提醒：<code>/delete 1</code>

参数：币种/合约、目标价格、方向。方向 <code>up</code> 代表价格 ≥ 目标价，<code>down</code> 代表价格 ≤ 目标价。
每位用户最多 3 条有效提醒，触发后自动删除。价格来源为 Gate USDT 永续合约的最新成交价。"""


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
            contracts = sorted({alert.contract for alert in self.store.all()})
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
                    text=(f"🔔 <b>价格触发</b>\n\n{alert.contract} {direction}目标价。\n"
                          f"目标：<code>{alert.target}</code> USDT\n当前：<code>{price}</code> USDT\n\n"
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
        f"✅ 已添加提醒 #{alert_id}\n<code>{contract}</code> 价格 {relation} <code>{target}</code> USDT"
    )


async def list_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    alerts = get_store(context).list_for_chat(update.effective_chat.id)
    if not alerts:
        await update.effective_message.reply_text("你目前没有有效提醒。")
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
        await update.effective_message.reply_text("已删除该提醒。")
    else:
        await update.effective_message.reply_text("找不到这个提醒编号。")


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
        await update.effective_message.reply_text("该合约尚未订阅或没有行情。先用 /set 添加监控；请确认合约名称正确。")
    else:
        await update.effective_message.reply_html(f"<code>{contract}</code> 最新成交价：<code>{value}</code> USDT")


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
    app.run_polling(drop_pending_updates=True, bootstrap_retries=-1)


if __name__ == "__main__":
    main()
