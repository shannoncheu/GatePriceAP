# Gate 合约价格提醒 Bot

一个 Telegram 价格提醒机器人，监控 Gate USDT 永续合约的最新成交价。达到设定条件后，向设置提醒的聊天发送通知。

程序只读取公开行情，不下单，不需要 Gate API Key，也不会访问 Gate 账户。

## 功能

- 监控 Gate USDT 永续合约最新成交价
- 支持 `BTC`、`BTC_USDT` 和 `BTC/USDT` 格式
- `up`：当前价大于或等于目标价时通知
- `down`：当前价小于或等于目标价时通知
- 每个 Telegram 用户最多 3 条有效提醒
- 提醒触发后自动关闭
- 提醒存储在本地 SQLite 数据库；服务重启后仍会保留
- 手动记录合约持仓，按最新价计算未实现盈亏、保证金和收益率
- 可设置最多 20 个币种的观察列表并随时查询最新价格
- 单币查询和触价提醒会尝试显示币种 logo；没有对应图标时自动使用文字消息

## 命令

```text
/set BTC 120000 up
/set ETH_USDT 3000 down
/price BTC
/list
/delete 1
/watch BTC ETH SOL
/watchlist
/clearwatch
/position BTC long 0.1 100000 10
/pnl
/positions
/closeposition 1
```

`/position` 参数依次为：合约、方向、实际持仓数量、开仓价、杠杆。方向支持 `long`（多单）和 `short`（空单）。

`/watch` 一次输入 1 到 20 个币种，会覆盖旧的观察列表；发送 `/watchlist` 查看这些合约的最新价格。

## 配置

需要 Python 3.10 或更高版本。

```bash
git clone https://github.com/shannoncheu/GatePriceAP.git
cd GatePriceAP
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env`：

```ini
TELEGRAM_BOT_TOKEN=从BotFather获取的Token
LOG_LEVEL=WARNING
```

启动：

```bash
python main.py
```

## 后台运行（systemd）

服务文件 `/etc/systemd/system/gate-price-alert-bot.service`：

```ini
[Unit]
Description=Gate Price Alert Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/GatePriceAP
EnvironmentFile=/root/GatePriceAP/.env
ExecStart=/root/GatePriceAP/.venv/bin/python /root/GatePriceAP/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

启用服务：

```bash
systemctl daemon-reload
systemctl enable --now gate-price-alert-bot
```

常用命令：

```bash
systemctl status gate-price-alert-bot
systemctl restart gate-price-alert-bot
journalctl -u gate-price-alert-bot -f
```

## 数据与安全

`.env` 包含 Telegram Token，`alerts.db` 包含提醒数据。两者均不应上传到 GitHub。行情使用 Gate 的公开 `futures.tickers` WebSocket 频道，价格字段为 `last`（最新成交价）。

Gate WebSocket 文档：<https://www.gate.com/docs/developers/futures/ws/en/>
