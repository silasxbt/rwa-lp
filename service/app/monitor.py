"""每分钟由 Cloud Scheduler 触发：bStocks 治理状态变化 → 广播；订阅池子出/近区间 → 通知订阅者。"""
from .chain import rpc, call, batch, calls, cached, k, pad, addr, words
from .analytics import BSTOCKS_BEACON, BEACON_SLOT, STABLES, quote_of
import time
from . import store, telegram

PM = "0x9fc74be63f3589485b2423984a7a0557e0cf700a"
COMPLIANCE = "0x53dba7aabde774787a1f57236b235567da8e14f4"
# 各 bStocks 股票的主力稳定币池
KNOWN_POOLS = {
    "QQQB": "0xfc4e77248b76fefc27c4cac7151a2ee5b5cc590e", "AAPLB": "0xe9b9998b2ec5430d2246c7f1f8d9f298c97d7365",
    "GOOGLB": "0x89001d846f7ca36ee089f73eefc25657e1798144", "NVDAB": "0x8fb4243b553ac29ba088acf00b9b7da24bd6690c",
    "BABAB": "0xfd95cb1391999006eb91797a7c62acfe88b20292", "PDDB": "0xd0c2a4dc7c581db2660286660b703cf6883f29ba",
    "MSTRB": "0x692081209619735f25700557078ab084d3e5d007",
    "MSFTB": "0x5018b018ceb7645c927c5cf246786f89ebcbe7ea", "METAB": "0xc2151a561e928d16576d75ea88544543ac63d80b",
    "BNCB": "0x320e8096e5e7a46e43446141a5b15d801fc596f5",   # 主池是 BNCB/BNB
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
            q, q1 = quote_of(t0, t1)
            out[p] = (t0, True) if (q1 if q else t1 in STABLES) else (t1, False)
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

MINT_SINGLE = 0.05   # 单笔增发超过总供应量 5% 告警
MINT_DAILY = 0.20    # 当天累计增发超过总供应量 20% 告警（每天只报一次）

def mint_roles(token):
    """有权铸币的地址：Issuer + Admin（每小时刷新）。正常申购由它们发起。"""
    def f():
        out = set()
        for role in (k("ISSUER_ROLE"), "0" * 64):
            n = int(call(token, "getRoleMemberCount(bytes32)", role), 16)
            for j in range(n): out.add(addr(call(token, "getRoleMember(bytes32,uint256)", role + format(j, "064x"))))
        return out
    return cached(("mint_roles", token), 3600, f)

def check_mint(sym, token, log, pools, supply):
    """判断一笔增发是否异常。正常申购（Issuer 发起、不砸池子、量级正常）不报警。返回告警文字列表。"""
    out, blk = [], int(log["blockNumber"], 16)
    amt = int(log["data"], 16) / 1e18
    tx = rpc("eth_getTransactionByHash", [log["transactionHash"]]) or {}
    roles = mint_roles(token)
    sender, target = (tx.get("from") or "").lower(), (tx.get("to") or "").lower()
    if sender not in roles and target not in roles:
        out.append(f"{sym} 出现非 Issuer 发起的增发 {amt:,.1f} 枚（发起地址 {sender[:10]}…），可能是私钥被盗或合约被改，区块 {blk}")
    if supply and amt >= supply * MINT_SINGLE:
        out.append(f"{sym} 单笔增发 {amt:,.1f} 枚，占总供应量 {amt / supply * 100:.1f}%，远超正常申购，区块 {blk}")
    rc = rpc("eth_getTransactionReceipt", [log["transactionHash"]]) or {}
    dumped = sum(int(l["data"], 16) / 1e18 for l in rc.get("logs", [])
                 if l.get("address", "").lower() == token and len(l.get("topics", [])) == 3 and l["topics"][0] == TRANSFER
                 and "0x" + l["topics"][2][-40:] in pools)
    if dumped > 0:
        out.append(f"{sym} 增发后在同一笔交易里直接卖进 LP 池子 {dumped:,.1f} 枚，区块 {blk}")
    return out, amt

def scan_logs(frm, to, pools, toks):
    alerts = []
    q = lambda a, b, addr_, topics=None: rpc("eth_getLogs", [{"address": addr_, "fromBlock": hex(a), "toBlock": hex(b),
                                                             **({"topics": topics} if topics else {})}])
    pools = {p.lower() for p in pools}
    supply, minted = {}, {}
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
                if s not in supply: supply[s] = int(call(t, "totalSupply()"), 16) / 1e18
                try: a, amt = check_mint(s, t, l, pools, supply[s])
                except Exception: a, amt = [], int(l["data"], 16) / 1e18
                alerts += a; minted[s] = minted.get(s, 0) + amt
        b = e + 1
    # 当天累计增发：超过总供应量 20% 时报一次
    if minted:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        st = store.get_state("mint_day") or {}
        if st.get("day") != day: st = {"day": day, "sum": {}, "alerted": []}
        for sym, amt in minted.items():
            st["sum"][sym] = st["sum"].get(sym, 0) + amt
            if sym not in st["alerted"] and supply.get(sym) and st["sum"][sym] >= supply[sym] * MINT_DAILY:
                st["alerted"].append(sym)
                alerts.append(f"{sym} 今天累计增发 {st['sum'][sym]:,.0f} 枚，占总供应量 {st['sum'][sym] / supply[sym] * 100:.0f}%，明显高于平时")
        store.set_state("mint_day", st)
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
                fp = lambda v: f"{v:.6g} BNB" if w.get("quote") == "BNB" else f"${v:,.2f}"
                telegram.send(w["chat_id"], f"{icon} {w['name']} {fp(p)} {z}\n区间 {fp(w['lo'])} – {fp(w['hi'])}\n{ZONE_TIP[z]}")
                sent += 1
    report["zone_alerts"] = sent
    report["rating_alerts"] = rating_alerts(ws)
    if int(time.time() // 60) % 5 == 0:   # 池子异动每 5 分钟查一次（行情数据本身 15 分钟刷新）
        try: report["pool_alerts"] = pool_alerts(ws)
        except Exception as e: report["pool_alerts"] = f"error: {e}"
    return report

# ---------- 池子异动追踪 ----------
TVL_DROP = 0.30          # TVL 比过去 6 小时最高值低 30%：有人大额撤流动性
TVL_DROP_SEVERE = 0.50   # 低 50% 广播给所有人
DEVIATION_OPEN = 0.03    # 美股盘中链上价偏离真实股价 3%：套利通道可能出问题
DEVIATION_CLOSED = 0.06  # 休市时放宽到 6%
VOL_SPIKE = 8            # 1 小时成交是 24 小时均值的 8 倍，且超过 2 万美元
MOVE_1H = 0.04           # 1 小时涨跌 4%
POOL_COOLDOWN = 3 * 3600

def pool_status(pools, save=True):
    """每个池子的当前异动指标（网页的「池子追踪」也用这个）。"""
    from .chain import gt_multi
    from .analytics import pool_info
    from .position import fair_price
    try: snap = store.get_state("pool_snap") or {}
    except Exception: snap = {}
    data = gt_multi(pools)
    now = time.time(); out = []
    for p in pools:
        d = data.get(p)
        if not d: continue
        a = d["attributes"]
        tvl = float(a.get("reserve_in_usd") or 0)
        v1, v24 = float(a["volume_usd"].get("h1") or 0), float(a["volume_usd"].get("h24") or 0)
        ch1 = float((a.get("price_change_percentage") or {}).get("h1") or 0) / 100
        hist = [x for x in snap.get(p, []) if now - x["t"] < 6 * 3600]   # Firestore 不支持嵌套数组，存成 {t, v}
        peak = max([x["v"] for x in hist] + [tvl])
        r = dict(pool=p, name=a.get("name"), tvl=tvl, tvl_peak6h=peak, tvl_drop=1 - tvl / peak if peak else 0,
                 vol1h=v1, vol24=v24, spike=v1 / (v24 / 24) if v24 else 0, move1h=ch1, premium=None, state=None)
        try:
            i = pool_info(p); f = fair_price(i)
            if f: r.update(premium=i["price"] / f["fair"] - 1, state=f["state"], name=i["name"])
        except Exception: pass
        r["flags"] = []
        if r["tvl_drop"] >= TVL_DROP: r["flags"].append(("tvl", f"TVL 从 6 小时内高点 ${peak:,.0f} 降到 ${tvl:,.0f}（−{r['tvl_drop']*100:.0f}%），有人大额撤流动性，你的份额和成交深度都会变"))
        if r["premium"] is not None:
            lim = DEVIATION_OPEN if r["state"] == "盘中" else DEVIATION_CLOSED
            if abs(r["premium"]) >= lim: r["flags"].append(("dev", f"链上价相对真实股价{'溢价' if r['premium'] > 0 else '折价'} {abs(r['premium'])*100:.1f}%（{r['state']}），套利没跟上，回归时会扫过你的区间"))
        if r["spike"] >= VOL_SPIKE and v1 >= 20_000: r["flags"].append(("vol", f"1 小时成交 ${v1:,.0f}，是 24 小时均值的 {r['spike']:.0f} 倍"))
        if abs(ch1) >= MOVE_1H: r["flags"].append(("move", f"1 小时{'上涨' if ch1 > 0 else '下跌'} {abs(ch1)*100:.1f}%"))
        snap[p] = (hist + [{"t": now, "v": tvl}])[-80:]
        out.append(r)
    if save: store.set_state("pool_snap", snap)
    return out

def pool_alerts(ws):
    pools = sorted(set(KNOWN_POOLS.values()) | {w["pool"] for w in ws})
    rows = pool_status(pools)
    cool = store.get_state("pool_cool") or {}
    log = store.get_state("pool_log") or {"items": []}
    now, sent = time.time(), 0
    watchers = {}
    for w in ws: watchers.setdefault(w["pool"], set()).add(w["chat_id"])
    for r in rows:
        for kind, text in r["flags"]:
            key = f"{r['pool']}|{kind}"
            if now - cool.get(key, 0) < POOL_COOLDOWN: continue
            cool[key] = now
            log["items"] = ([{"t": now, "pool": r["pool"], "name": r["name"], "kind": kind, "text": text}] + log["items"])[:50]
            to = set(watchers.get(r["pool"], ()))
            if kind == "tvl" and r["tvl_drop"] >= TVL_DROP_SEVERE: to |= set(store.chats())
            for c in to: telegram.send(c, f"📡 {r['name']} 池子异动\n{text}"); sent += 1
    store.set_state("pool_cool", {k: v for k, v in cool.items() if now - v < 86400})
    store.set_state("pool_log", log)
    return sent

RATING_ICON = {"安全": "🟢", "警惕": "🟠", "危险": "🔴"}
RATING_COOLDOWN = 1800  # 同一订阅降级/升级提醒的最短间隔（秒）；升到「危险」不受限制

def rating_alerts(ws):
    """按订阅区间计算评级（链上价 + 真实股价 + 日波动），评级变化时推送。"""
    from .analytics import pool_info
    from .position import rating, fair_price
    infos, sent, now = {}, 0, time.time()
    try:   # 所有订阅的池子合成一次请求，监控的额度消耗不随订阅数量增长
        from .chain import gt_multi
        gt_multi(sorted({w["pool"] for w in ws}))
    except Exception: pass
    for w in ws:
        try:
            if w["pool"] not in infos:
                i = pool_info(w["pool"]); infos[w["pool"]] = (i, fair_price(i))
            i, fair = infos[w["pool"]]
            r = rating(i, w["lo"], w["hi"], fair)
        except Exception:
            continue
        prev = w.get("rating")
        if prev == r["level"]: continue
        if prev is None:
            store.update_watch(w["id"], rating=r["level"], rating_at=now); continue
        if r["level"] != "危险" and now - w.get("rating_at", 0) < RATING_COOLDOWN: continue
        store.update_watch(w["id"], rating=r["level"], rating_at=now)
        from .position import fmt_px
        fp = f"\n真实股价折算 {fmt_px(i, fair['fair'])}（{fair['ticker']} ${fair['last']:,.2f} {fair['state']}）" if fair else ""
        telegram.send(w["chat_id"],
            f"{RATING_ICON[r['level']]} {w['name']} 评级 {prev} → {r['level']}\n"
            f"链上 {fmt_px(i, i['price'])}，区间 {fmt_px(i, w['lo'])} – {fmt_px(i, w['hi'])}，位置 {r['pos']*100:.0f}%，离边界 {r['edge']*100:.1f}%{fp}\n"
            + "\n".join("· " + t for t in r["reasons"]))
        sent += 1
    return sent


def token_governance(token, pool):
    """单个 bStocks 代币 + 池子的治理安全项，供页面体检使用。ok: True 安全 / False 危险 / None 需留意。"""
    def f():
        r = calls([(PM, "isTokenPaused(address)", pad(token)),
                   (COMPLIANCE, "blockedAddresses(address,address)", pad(token) + pad(pool)),
                   (COMPLIANCE, "sanctionedAddresses(address)", pad(pool)),
                   (token, "mintEnabled()", ""),
                   (token, "getRoleMemberCount(bytes32)", k("ISSUER_ROLE")),
                   (token, "getRoleMemberCount(bytes32)", "0" * 64),
                   (token, "getRoleMember(bytes32,uint256)", "0" * 128),
                   (token, "compliance()", ""), (token, "pauseManager()", ""),
                   (BSTOCKS_BEACON, "owner()", "")])
        v = lambda j: int(r[j], 16) if r[j] else None
        admin = addr(r[6]) if r[6] else None
        owner = addr(r[9]) if r[9] else None
        codes = batch([("eth_getCode", [a, "latest"]) for a in (admin, owner) if a])
        admin_eoa = codes[0] in ("0x", None) if admin else None
        owner_eoa = codes[-1] in ("0x", None) if owner else None
        beacon = addr(rpc("eth_getStorageAt", [token, BEACON_SLOT, "latest"]))
        items = []
        add = lambda ok, text: items.append({"cat": "发行方", "ok": ok, "text": text})
        add(not v(0), "转账正常，未被暂停" if not v(0) else "代币已被暂停，转账和撤池都会失败")
        bl = v(1) or v(2)
        add(not bl, "池子未被拉黑或制裁" if not bl else "池子已被拉黑或列入制裁名单")
        add(None if v(3) else True, f"增发已开启，{v(4)} 个 Issuer 可铸币" if v(3) else "增发已关闭")
        add(None if admin_eoa else True, f"Admin {v(5)} 个，" + ("是单一私钥钱包" if admin_eoa else "是合约（可能为多签）"))
        add(None if owner_eoa else True, "升级权限在单签钱包，无时间锁" if owner_eoa else "升级权限在合约钱包")
        same = addr(r[7]) == COMPLIANCE and addr(r[8]) == PM and beacon == BSTOCKS_BEACON
        add(same, "权限合约与审计时一致" if same else "合规、暂停或升级合约已被替换，与审计时不一致")
        return items
    return cached(("tgov", token, pool), 60, f)
