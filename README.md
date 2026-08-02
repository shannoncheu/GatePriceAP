# Gate 合约价格提醒 Telegram 机器人

监控 Gate 的 **USDT 永续合约最新成交价**。当价格达到你设置的条件时，机器人会立刻发送 Telegram 消息。程序不下单、不读取 Gate 账户，也不需要 Gate API Key。

## 功能

- 每位 Telegram 用户最多 3 条有效提醒
- 支持 `BTC`、`BTC_USDT`、`BTC/USDT` 等输入
- `up`：价格大于或等于目标价时触发；`down`：价格小于或等于目标价时触发
- 触发后自动删除，防止重复通知
- 使用 SQLite 保存提醒；重启后未触发的提醒仍在

## 安装和运行

需要 Python 3.10 或更高版本。

```powershell
cd gate-price-alert-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

打开 `.env`，填入从 Telegram [@BotFather](https://t.me/BotFather) 创建机器人后拿到的 Token：

```ini
TELEGRAM_BOT_TOKEN=123456:ABC...
```

启动：

```powershell
python main.py
```

首次使用时，在 Telegram 打开你的机器人并发送 `/start`。

## 命令

```text
/set BTC 120000 up
/set ETH_USDT 3000 down
/list
/delete 1
/price BTC
```

`/price` 只显示当前已被提醒订阅的合约价格；先通过 `/set` 订阅即可。

## 部署建议

本机退出或断网后将无法监控。要全天运行，请部署到一台持续在线的 VPS 或云服务，并保留同目录下的 `alerts.db` 文件。不要把 `.env` 或真实 Telegram Token 上传到 GitHub。

## 行情来源

机器人订阅 Gate 的公开 `futures.tickers` WebSocket 频道，读取 `last`（最新成交价）字段。Gate 的官方 WebSocket 文档：<https://www.gate.com/docs/developers/futures/ws/en/>。
