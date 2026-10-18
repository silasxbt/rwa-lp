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

- 支持股票/USDT、股票/USDC、股票/BNB 三类池子；BNB 池价格按 BNB 显示，金额按 U
- 网页：池子体检、手续费估算、区间回测（含出区间平移调仓的 swap 费 / 价格冲击 / gas 成本）、bStocks 治理状态
- 回测可选 Delta 对冲（永续合约 / 券商融券），按真实股价小时线计对冲盈亏，给出持仓成本、交易成本和不同本金规模下是否划算
- 标的排行：一次请求比较全部自选池子的「手续费收益 ÷ 波动²」、成交趋势和下一个大事件；分析页列出同一只股票的其他官方 V3 池子
- 行情数据：配了 `COINGECKO_API_KEY` 时优先走 CoinGecko onchain 接口，限流、报错或超过月度预算（`COINGECKO_MONTHLY_BUDGET`，默认 9000 次）时自动退回 GeckoTerminal 公共接口；用量按月记在 Firestore，`/api/source` 可查
- 事件钟：财报、FOMC、CPI、非农、休市日折算成额外波动，用首次穿越近似给出 1/3/7/14/30 天出界概率，评级按 7 天出界概率定（`app/events.py`，日历需手动维护）
- 发行方风险调整后年化：按你填的发行方年化出事概率扣减
- 工具台（`app/research.py`、`app/strategy.py`）：
  - 投研审计：Yahoo 基本面（估值、增长、52 周位置、均线、分析师目标、ETF 持仓）+ 合约审计（代理结构、升级权限归属、按 BscScan 验证 ABI 归类写函数、Admin/Issuer 是单签/多签/合约、持币集中度、GeckoTerminal 评分）+ 代币化规模和链上溢价；配 `ETHERSCAN_API_KEY` 读源码，否则按字节码选择器粗查
  - 选区间：网格扫描上下沿，前瞻模型（含事件的出界概率、LVR、调仓成本）和历史滚动回测取较差者排序，给出保守 / 平衡 / 进取三个参考区间和 1/1.5/2σ 波动带
  - 年化测算：毛手续费 → × 预期在区间时间 → − LVR 无常损失 → − 调仓损失折合年化 → 净年化 → 周复投 → 扣发行方风险，五个成交口径并列
  - 高阶策略：分层 LP 回测与单层对照；保护性看跌 / 看跌价差成本（Yahoo 期权链）、手续费隐含波动 vs 期权隐含波动；持币、纯 LP、LP + 永续空、LP + 看跌、纯做空的情景盈亏与期望
  - 池子追踪：TVL 骤降、链上价偏离真实股价、异常放量、1 小时急涨急跌，每 5 分钟检查，推送给订阅者（Telegram `/alerts` 查最近记录）
- 事件钟的个股财报日期优先用 Yahoo 的日期
- 区间评估：近 24h 年化、三种调仓情形的成本与回本时间、对照真实股价（Yahoo Finance）的区间位置评级
- API：`/api/pool`、`/api/position`、`/api/backtest`、`/api/depth`、`/api/board`、`/api/siblings`、`/api/source`、`/api/bstocks`，文档见 `/api/docs`
- Telegram 机器人：`/watch` 订阅区间提醒，订阅者自动接收治理告警
- Cloud Scheduler 每分钟调用 `/cron/tick`（`X-Cron-Key` 校验），状态存 Firestore
- 密钥在 Secret Manager：`rwalp-cron-key`、`rwalp-tg-webhook-secret`、`rwalp-tg-token`、`coingecko-api-key`（可选，设了 `COINGECKO_API_KEY` 就走 CoinGecko onchain 接口，否则走 GeckoTerminal 公共接口）

```bash
cd service
uv run --python 3.12 --with-requirements requirements.txt uvicorn app.main:app --port 8080   # 本地运行（cron/机器人需要 GCP 凭据）
gcloud run deploy rwalp --source . --region asia-east1 --project <你的GCP项目> # 部署
```
