# bStocks 代币化股票审计记录（BSC）

审计日期：2026-10-01 ~ 10-02。基于链上当前状态和 Sourcify 验证源码；历史事件（增发、暂停、权限变更）未扫描（公共 RPC 不支持归档日志）。

## 结论

没有发现外部攻击者可直接利用的代码漏洞。全损风险集中在**发行方权限与私钥安全**，且 QQQB、AAPLB、GOOGLB、NVDAB、BABAB、PDDB、MSTRB **共用同一套治理合约**，在它们之间分散只能分散股价风险，分散不了发行方风险。

## 合约结构

| 组件 | 地址 | 说明 |
|---|---|---|
| 代币（以 QQQB 为例） | `0x205812cdbed920aff76c6580abd681a46d11efc7` | BeaconProxy |
| AAPLB | `0x431a3bee82e2ca41e49895cbece5bb0f76a89b7a` | 同一 Beacon |
| Beacon | `0x156d6dce9a4f6139a3406f1f021f1a4880de93a3` | 所有 bStocks 代币共用 |
| 逻辑合约 | `0xcfed6c4679297ea4889f8183bc057b4a86c64e46` | `SecuritiesToken.sol`，Sourcify exact match |
| PauseManager | `0x9fc74be63f3589485b2423984a7a0557e0cf700a` | 可单币暂停或全部暂停 |
| Compliance | `0x53dba7aabde774787a1f57236b235567da8e14f4` | 按代币黑名单 + 全局制裁名单 |

合约注释声明为 ADGM（阿布扎比金融监管）认定的证券凭证。代码基于 OpenZeppelin：无转账税、无 `forceTransfer`/`seize`。`_update` 每次转账检查暂停状态，并对 from / to / msg.sender 做合规检查。

## 权限持有人

| 角色 | 地址 | 类型 | 观察 |
|---|---|---|---|
| Beacon owner（可升级全部逻辑） | `0x4333daf4481f281f3d3d2b8735ce80bc00028d0c` | EOA | nonce 0，余额 0；非多签、无时间锁 |
| DEFAULT_ADMIN（唯一） | `0x45e35fe982f3869221b222abea372fa97aa7679d` | EOA | 217 笔交易；暂停/黑名单配置、改名、开关增发 |
| ISSUER | `0xa42436335a41fa6c82cde224ae2b82c025027e5b` | EOA | 16 万+ 笔交易，自动化热钱包，**最可能被盗的私钥** |
| ISSUER | `0x5948602680fe4aea08d98302337a2715b3aadad1` | 可升级代理合约 | 1 笔交易 |
| ISSUER | `0xc94e485510c458860f89d23dcb8dd34bd10e6b8b` | 可升级代理合约 | 1 笔交易 |

`mintEnabled = true`，`burnEnabled = true`。

## 风险清单（按严重度）

| # | 风险 | 对 LP 的影响 |
|---|---|---|
| 1 | Issuer 热钱包泄露 → 无限增发 | 增发后砸池，池中稳定币被换空 |
| 2 | Beacon owner 单签可升级 | 逻辑可被换成任意代码 |
| 3 | 暂停（单币或全部） | 撤 LP 时股票侧转不出，整笔交易失败 |
| 4 | 黑名单 / 制裁名单 | 池子或钱包被封则无法 swap / 撤出 |
| 5 | uiMultiplier（1e-9x ~ 1e9x） | 只影响 UI 显示，不影响 `balanceOf` 与池子 |
| 6 | 发行方信用 / 赎回渠道 | 脱锚时 LP 被动接满股票 |

## 池子核验

| 池子 | DEX | 官方 factory | 协议抽成 |
|---|---|---|---|
| QQQB/USDC 0.3% `0xfc4e77248b76fefc27c4cac7151a2ee5b5cc590e` | Uniswap V3 BSC | ✅ | 1/6 |
| AAPLB/USDT 0.25% `0xe9b9998b2ec5430d2246c7f1f8d9f298c97d7365` | PancakeSwap V3 | ✅ | 32%（另有 CAKE 挖矿 pid 591，奖励可忽略） |
| AAPLB/USDT 0.05% `0x36c0fc3159eb8662a2e1b84a4df518d916bce0e1` | Uniswap V3 BSC | ✅ | 25% |

可用 `tools/pool_check.py` 复核。

## 未完成

- 权限地址的完整交易历史、历史增发 / 暂停 / 黑名单事件：需要带归档能力的 RPC（publicnode / drpc / Ankr 付费或个人 token），Etherscan 免费 key 不覆盖 BSC。
- 发行方链下赎回机制。
