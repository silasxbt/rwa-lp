"""每分钟由 Cloud Scheduler 触发：bStocks 治理状态变化 → 广播；订阅池子出/近区间 → 通知订阅者。"""
from .chain import rpc, batch, calls, cached, k, pad, addr, words
from .analytics import BSTOCKS_BEACON, BEACON_SLOT, STABLES
from . import store, telegram

PM = "0x9fc74be63f3589485b2423984a7a0557e0cf700a"
COMPLIANCE = "0x53dba7aabde774787a1f57236b235567da8e14f4"
# 各 bStocks 股票的主力稳定币池
KNOWN_POOLS = {
    "QQQB": "0xfc4e77248b76fefc27c4cac7151a2ee5b5cc590e", "AAPLB": "0xe9b9998b2ec5430d2246c7f1f8d9f298c97d7365",
    "GOOGLB": "0x89001d846f7ca36ee089f73eefc25657e1798144", "NVDAB": "0x8fb4243b553ac29ba088acf00b9b7da24bd6690c",
    "BABAB": "0xfd95cb1391999006eb91797a7c62acfe88b20292", "PDDB": "0xd0c2a4dc7c581db2660286660b703cf6883f29ba",
    "MSTRB": "0x692081209619735f25700557078ab084d3e5d007",
}
NEAR = 0.01
MAX_BLOCKS = 600  # 每次最多补扫的区块数
TRANSFER = "0x" + k("Transfer(address,address,uint256)")
ZERO = "0x" + "0" * 64

def pool_tokens(pools):
    """pool → (股票地址, 股票是否 token0)"""
    def f():
        res = calls([(p, "token0()", "") for p in pools] + [(p, "token1()", "") for p in pools])
        out = {}
        for i, p in enumerate(pools):
            t0, t1 = addr(res[i]), addr(res[i + len(pools)])
            out[p] = (t0, True) if t1 in STABLES else (t1, False)
        return out
    return cached(("tokens", tuple(pools)), 3600, f)

def bstocks_tokens():
    """KNOWN_POOLS 对应的 bStocks 代币 {symbol: token}"""
    tk = pool_tokens(list(KNOWN_POOLS.values()))
    return {s: tk[p][0] for s, p in KNOWN_POOLS.items()}

def governance(extra_pools=()):
    toks = bstocks_tokens()
    pools = sorted(set(KNOWN_POOLS.values()) | set(extra_pools))
    items, keys = [], []
    for s, t in toks.items():
        for key, req in [("暂停", (PM, "isTokenPaused(address)", pad(t))),
                         ("Admin数", (t, "getRoleMemberCount(bytes32)", "0" * 64)),
                         ("Issuer数", (t, "getRoleMemberCount(bytes32)", k("ISSUER_ROLE"))),
                         ("compliance", (t, "compliance()", "")),
                         ("pauseManager", (t, "pauseManager()", "")),
                         ("增发开关", (t, "mintEnabled()", ""))]:
            keys.append(f"{s} {key}"); items.append(req)
        for p in pools:
            keys.append(f"{s} 黑名单 {p}"); items.append((COMPLIANCE, "blockedAddresses(address,address)", pad(t) + pad(p)))
    for p in pools:
        keys.append(f"制裁名单 {p}"); items.append((COMPLIANCE, "sanctionedAddresses(address)", pad(p)))
    keys.append("Beacon逻辑合约"); items.append((BSTOCKS_BEACON, "implementation()", ""))
    vals = calls(items)
    beacons = batch([("eth_getStorageAt", [t, BEACON_SLOT, "latest"]) for t in toks.values()])
    out = {}
    for key, v in zip(keys, vals):
        if v is None: continue
        out[key] = addr(v) if key.endswith(("compliance", "pauseManager", "逻辑合约")) else int(v, 16)
    for (s, _), b in zip(toks.items(), beacons):
        if b: out[f"{s} Beacon"] = addr(b)
    return out

def prices(pools):
    tk = pool_tokens(pools)
    res = calls([(p, "slot0()", "") for p in pools])
    out = {}
    for p, r in zip(pools, res):
        if not r: continue
        P = (words(r)[0] / 2**96) ** 2  # 双 18 位小数
        out[p] = P if tk[p][1] else 1 / P
    return out

def zone(lo, hi, p):
    if p < lo: return "跌破下沿"
    if p > hi: return "涨破上沿"
    if p < lo * (1 + NEAR): return "接近下沿"
    if p > hi * (1 - NEAR): return "接近上沿"
    return "区间内"

ZONE_TIP = {"跌破下沿": "仓位已全部变成股票，停止收手续费", "涨破上沿": "仓位已全部变成稳定币，停止收手续费",
            "接近下沿": "距离下沿不到 1%", "接近上沿": "距离上沿不到 1%", "区间内": "已回到区间内"}

def scan_logs(frm, to, pools, toks):
    alerts = []
    q = lambda a, b, addr_, topics=None: rpc("eth_getLogs", [{"address": addr_, "fromBlock": hex(a), "toBlock": hex(b),
                                                             **({"topics": topics} if topics else {})}])
    bal = {}
    b = frm
    while b <= to:
        e = min(b + 49, to)
        for l in q(b, e, BSTOCKS_BEACON): alerts.append(f"Beacon 合约事件（可能是升级）区块 {int(l['blockNumber'], 16)}")
        for l in q(b, e, PM): alerts.append(f"PauseManager 事件（暂停/恢复）区块 {int(l['blockNumber'], 16)}")
        for l in q(b, e, COMPLIANCE):
            blob = l["data"] + "".join(l["topics"])
            hit = [p for p in pools if pad(p) in blob]
            if hit: alerts.append(f"Compliance 名单变动涉及池子 {', '.join(hit)} 区块 {int(l['blockNumber'], 16)}")
        for s, t in toks.items():
            for l in q(b, e, t, [TRANSFER, ZERO]):
                amt = int(l["data"], 16) / 1e18
                if s not in bal:
                    bal[s] = int(rpc("eth_call", [{"to": t, "data": "0x" + k("balanceOf(address)")[:8] + pad(KNOWN_POOLS[s])}, "latest"]), 16) / 1e18
                if amt >= max(bal[s] * 0.5, 1):
                    alerts.append(f"{s} 大额增发 {amt:,.1f} 枚（主池只有 {bal[s]:,.1f} 枚）区块 {int(l['blockNumber'], 16)}")
        b = e + 1
    return alerts

def tick():
    ws = store.watches()
    wpools = sorted({w["pool"] for w in ws})
    toks = bstocks_tokens()
    report = {"watches": len(ws)}

    gov = governance(wpools)
    base = store.get_state("governance")
    gov_alerts = []
    if base:
        for key, v in gov.items():
            if key in base and base[key] != v: gov_alerts.append(f"{key}: {base[key]} → {v}")
    store.set_state("governance", gov)

    head = int(rpc("eth_blockNumber", []), 16) - 5
    cur = store.get_state("cursor") or {"block": head - 60}
    frm = max(cur["block"] + 1, head - MAX_BLOCKS)
    if frm <= head:
        gov_alerts += scan_logs(frm, head, sorted(set(KNOWN_POOLS.values()) | set(wpools)), toks)
        store.set_state("cursor", {"block": head})
    report["blocks"] = [frm, head]

    if gov_alerts:
        msg = "🚨 bStocks 治理告警\n" + "\n".join(gov_alerts) + "\n\n考虑立即撤出相关 LP。"
        for c in store.chats(): telegram.send(c, msg)
    report["gov_alerts"] = gov_alerts

    px = prices(wpools) if wpools else {}
    sent = 0
    for w in ws:
        p = px.get(w["pool"])
        if p is None: continue
        z = zone(w["lo"], w["hi"], p)
        if z != w.get("zone"):
            store.update_watch(w["id"], zone=z)
            if not (z == "区间内" and w.get("zone") in (None, "区间内")):
                icon = "✅" if z == "区间内" else "⚠️"
                telegram.send(w["chat_id"], f"{icon} {w['name']} ${p:,.2f} {z}\n区间 ${w['lo']:,.2f} – ${w['hi']:,.2f}\n{ZONE_TIP[z]}")
                sent += 1
    report["zone_alerts"] = sent
    return report
