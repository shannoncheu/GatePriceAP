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
- 任意 Gate USDT 永续合约都可用 `/price` 查询，不需要先加入提醒、持仓或观察列表
- 可选接入 OpenAI API，直接用自然语言查价格、查盈亏和获得简短持仓风险提示
- 没有持仓时也能按 Gate 15 分钟 K 线、EMA、RSI、MACD 和成交量做简短行情分析

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
/whoami
```

`/position` 参数依次为：合约、方向、实际持仓数量、开仓价、杠杆。方向支持 `long`（多单）和 `short`（空单）。

`/watch` 一次输入 1 到 20 个币种，会覆盖旧的观察列表；发送 `/watchlist` 查看这些合约的最新价格。

配置 AI 后，也可以直接发送普通文字，例如：

```text
BTC 现在多少钱？
查看我的持仓盈亏
看看我的持仓，简单说说风险
分析一下 BTC 15 分钟走势，现在适合关注多单吗？
我的观察列表里有什么？
```

AI 只负责理解自然语言和整理简短说明。实时价格、K 线指标、盈亏和收益率仍由 Gate 行情与程序计算；机器人不会自动下单，技术指标也不代表对未来价格的保证。

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
OPENAI_API_KEY=你的OpenAI API Key
OPENAI_MODEL=gpt-5.6-luna
OPENAI_BASE_URL=
AI_ALLOWED_TELEGRAM_USER_IDS=你的Telegram数字用户ID
LOG_LEVEL=WARNING
```

`OPENAI_API_KEY` 为空时，原来的命令功能仍可正常使用。部署后先向机器人发送 `/whoami` 获取自己的数字 ID，再填入允许名单；多人使用时用英文逗号分隔。允许名单可防止其他人消耗你的 OpenAI API 额度。

使用 Sub2API 等 OpenAI 兼容中转服务时，`OPENAI_API_KEY` 填中转平台生成的 Key，`OPENAI_BASE_URL` 填平台提供的完整接口地址（一般以 `/v1` 结尾），`OPENAI_MODEL` 填该平台后台实际可用的模型名称。使用 OpenAI 官方 API 时，让 `OPENAI_BASE_URL` 保持为空。

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

`.env` 包含 Telegram Token 和 OpenAI API Key，`alerts.db` 包含提醒及持仓数据。两者均不应上传到 GitHub。启用自然语言功能后，用户发送的文字以及程序整理的持仓、提醒和观察列表数据会发给 OpenAI 生成回答；不会发送 Telegram Token 或 OpenAI API Key。行情使用 Gate 的公开 `futures.tickers` WebSocket 频道，价格字段为 `last`（最新成交价）。

Gate WebSocket 文档：<https://www.gate.com/docs/developers/futures/ws/en/>
