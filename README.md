# rwa-lp

BSC 上 bStocks 代币化美股（QQQB、AAPLB 等）的 V3 集中流动性做市：风控监控、池子体检、区间回测。

所有脚本都带 PEP 723 依赖声明，装好 [uv](https://docs.astral.sh/uv/) 直接运行，无需 API key（用 publicnode 公共 RPC + GeckoTerminal）。

## 目录

| 路径 | 用途 |
|---|---|
| `monitor/bstocks_monitor.py` | 7×24 风控监控：合约升级、暂停、黑名单/制裁、权限变更、大额增发、价格出区间 → macOS 通知 |
| `tools/pool_check.py` | 池子体检：官方 factory 校验、协议抽成、TVL/成交量、你的份额与预估手续费 |
| `tools/range_backtest.py` | 用小时 K 线滚动回测不同区间宽度的手续费、无常损失、在区间时间 |
| `docs/audit-bstocks.md` | bStocks 合约与权限审计记录 |

## 用法

```bash
# 监控（把你的 LP 钱包地址加在后面）；区间在脚本 ASSETS 里改
uv run monitor/bstocks_monitor.py 0x你的钱包

# 池子体检（--range 为美元价）
uv run tools/pool_check.py 0xe9b9998b2ec5430d2246c7f1f8d9f298c97d7365 --range 310 350

# 区间回测（相对开仓价的百分比）
uv run tools/range_backtest.py 0xe9b9998b2ec5430d2246c7f1f8d9f298c97d7365 --ranges=-3.6:4.8,-4:6,-5:7
```

## 仓位配置

监控用的仓位区间放在 `monitor/positions.json`（已 gitignore），格式见 `monitor/positions.example.json`。

## 纪律

- 所有 bStocks 代币共用同一发行方治理（见审计），仓位总额按发行方信用风险设上限。
- 美股休市、周末不调仓；出区间后等开盘稳定 1 小时再重开。
- 手续费每周复投一次；到仓位上限后只领取不加仓，避免待领取手续费长期留在池中。
- 监控只告警不撤池，收到告警需手动撤出；不依赖 Binance Wallet 前端，确认能直接通过 NonfungiblePositionManager 撤池。

## 在线服务 `service/`

FastAPI 服务，部署在 GCP Cloud Run（asia-east1），网址 https://rwalp.silasxbt.com 。

- 网页：池子体检、手续费估算、区间回测（含出区间平移调仓的 swap 费 / 价格冲击 / gas 成本）、bStocks 治理状态
- 回测可选 Delta 对冲（永续合约 / 券商融券），按真实股价小时线计对冲盈亏，给出持仓成本、交易成本和不同本金规模下是否划算
- 区间评估：近 24h 年化、三种调仓情形的成本与回本时间、对照真实股价（Yahoo Finance）的区间位置评级
- API：`/api/pool`、`/api/position`、`/api/backtest`、`/api/depth`、`/api/bstocks`，文档见 `/api/docs`
- Telegram 机器人：`/watch` 订阅区间提醒，订阅者自动接收治理告警
- Cloud Scheduler 每分钟调用 `/cron/tick`（`X-Cron-Key` 校验），状态存 Firestore
- 密钥在 Secret Manager：`rwalp-cron-key`、`rwalp-tg-webhook-secret`、`rwalp-tg-token`

```bash
cd service
uv run --python 3.12 --with-requirements requirements.txt uvicorn app.main:app --port 8080   # 本地运行（cron/机器人需要 GCP 凭据）
gcloud run deploy rwalp --source . --region asia-east1 --project <你的GCP项目> # 部署
```
