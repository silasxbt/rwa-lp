"""池子体检、手续费估算、区间回测（含出区间平移调仓）。"""
import math, statistics as st, datetime as dt
from zoneinfo import ZoneInfo
from .chain import call, rpc, gt, cached, symbol, addr, pad, words

FACTORIES = {"0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865": "PancakeSwap V3",
             "0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7": "Uniswap V3 (BSC)"}
STABLES = {"0x55d398326f99059ff775485246999027b3197955": "USDT",
           "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC"}
BSTOCKS_BEACON = "0x156d6dce9a4f6139a3406f1f021f1a4880de93a3"
BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
GAS_USD = 0.1  # BSC 上撤池+swap+开仓三笔交易合计
NY = ZoneInfo("America/New_York")

def protocol_cut(dex, fp):
    if dex.startswith("Pancake"):  # 两个 16 位，单位 1/10000
        return ((fp & 0xFFFF) + (fp >> 16)) / 2 / 1e4
    return sum(1 / n for n in (fp % 16, fp >> 4) if n) / 2  # Uniswap: 协议拿 1/n

def _static(pool):
    f = addr(call(pool, "factory()"))
    t0, t1 = addr(call(pool, "token0()")), addr(call(pool, "token1()"))
    fee = int(call(pool, "fee()"), 16)
    canon = addr(call(f, "getPool(address,address,uint24)", pad(t0) + pad(t1) + hex(fee)[2:].zfill(64))) == pool
    d0, d1 = int(call(t0, "decimals()"), 16), int(call(t1, "decimals()"), 16)
    stable_is_1 = t1 in STABLES
    stock = t0 if stable_is_1 else t1
    beacon = addr(rpc("eth_getStorageAt", [stock, BEACON_SLOT, "latest"]))
    return dict(pool=pool, factory=f, dex=FACTORIES.get(f), canonical=canon, token0=t0, token1=t1,
                symbol0=symbol(t0), symbol1=symbol(t1), d0=d0, d1=d1, fee=fee, stock=stock,
                stable=t1 if stable_is_1 else t0, stable_is_1=stable_is_1, bstocks=beacon == BSTOCKS_BEACON)

def pool_info(pool):
    pool = pool.lower()
    s = cached(("static", pool), 86400, lambda: _static(pool))
    if s["stable"] not in STABLES: raise ValueError("只支持股票/USDT 或 股票/USDC 的 V3 池子")
    slot0 = words(call(pool, "slot0()"))
    P = (slot0[0] / 2**96) ** 2
    Lraw = int(call(pool, "liquidity()"), 16)
    dex = s["dex"] or "未知"
    cut = protocol_cut(dex, slot0[5]) if s["dex"] else 0
    d = 10 ** (s["d0"] - s["d1"])
    price = P * d if s["stable_is_1"] else 1 / (P * d)
    a = gt(pool)["attributes"]
    o = sorted(gt(pool + "/ohlcv/day", limit=31, token=s["stock"])["attributes"]["ohlcv_list"])[:-1]
    vols = [x[5] for x in o]
    v7 = st.mean(vols[-7:]) if vols else 0
    v30 = st.mean(vols[-30:]) if vols else 0
    closes = [x[4] for x in o]
    rets = [math.log(closes[j] / closes[j - 1]) for j in range(1, len(closes)) if closes[j - 1] > 0]
    sigma_day = st.pstdev(rets) if len(rets) > 5 else 0.02
    v24 = float(a["volume_usd"]["h24"] or 0)
    tvl = float(a["reserve_in_usd"] or 0)
    created = a["pool_created_at"][:10]
    age = (dt.date.today() - dt.date.fromisoformat(created)).days
    info = dict(s, name=a["name"], price=price, L=Lraw, Lh=Lraw / 10 ** ((s["d0"] + s["d1"]) / 2),
                protocol_cut=cut, lp_fee=s["fee"] / 1e6 * (1 - cut), tvl=tvl, vol24=v24, vol7=v7, vol30=v30, sigma_day=sigma_day,
                created=created, age_days=age)
    info["flags"] = quality_flags(info)
    return info

def quality_flags(i):
    f = []
    ok = lambda good, text: f.append({"ok": good, "text": text})
    ok(bool(i["dex"]), f"官方 factory：{i['dex']}" if i["dex"] else "factory 不是 PancakeSwap/Uniswap 官方，可能是仿盘")
    ok(i["canonical"], "factory.getPool 回查一致" if i["canonical"] else "factory.getPool 回查不一致")
    ok(i["tvl"] >= 200_000, f"TVL ${i['tvl']:,.0f}" + ("" if i["tvl"] >= 200_000 else "，偏浅，大额进出滑点大、份额易被稀释"))
    ok(i["age_days"] >= 30, f"上线 {i['age_days']} 天" + ("" if i["age_days"] >= 30 else "，数据太短，成交量可能是新币热度"))
    r = i["vol30"] / i["tvl"] if i["tvl"] else 0
    ok(r >= 0.5, f"30日 成交/TVL = {r:.2f}" + ("" if r >= 0.5 else "，手续费收入偏低"))
    drop = i["vol7"] / i["vol30"] - 1 if i["vol30"] else 0
    ok(drop > -0.4, f"7日成交量较30日均值 {drop*100:+.0f}%")
    ok(i["protocol_cut"] <= 0.34, f"协议抽成 {i['protocol_cut']*100:.0f}%，LP 实得费率 {i['lp_fee']*100:.4f}%")
    if i["bstocks"]:
        f.append({"ok": None, "text": "bStocks 发行：发行方可升级/暂停/拉黑/增发，所有 bStocks 代币共用同一套权限（见治理状态）"})
    else:
        f.append({"ok": None, "text": "非 bStocks 代币，需单独审计发行方权限"})
    return f

def liq(value, p, a, b):
    pc = min(max(p, a), b)
    return value / (2 * math.sqrt(pc) - math.sqrt(a) - pc / math.sqrt(b))

def amounts(L, p, a, b):  # 股票数量, 美元数量
    sp, sa, sb = math.sqrt(min(max(p, a), b)), math.sqrt(a), math.sqrt(b)
    return L * (1 / sp - 1 / sb), L * (sp - sa)

def estimate(i, lo, hi, capital):
    p = i["price"]
    if not lo < p < hi: return dict(lo=lo, hi=hi, in_range=False, share=0, fee_day7=0, fee_day30=0, apr7=0, apr30=0)
    uL = liq(capital, p, lo, hi)
    share = uL / (i["Lh"] + uL)
    d7, d30 = i["vol7"] * i["lp_fee"] * share, i["vol30"] * i["lp_fee"] * share
    return dict(lo=lo, hi=hi, in_range=True, share=share, fee_day7=d7, fee_day30=d30,
                apr7=d7 * 365 / capital, apr30=d30 * 365 / capital)

def hourly(i):
    o = gt(i["pool"] + "/ohlcv/hour", ttl=900, limit=1000, token=i["stock"])["attributes"]["ohlcv_list"]
    return sorted(o)

def us_open(ts):
    t = dt.datetime.fromtimestamp(ts, NY)
    return t.weekday() < 5 and dt.time(9, 30) <= t.time() < dt.time(16, 0)

def rolling(o, lc, fee, lo, hi, capital, W):
    res = []
    for s in range(0, len(o) - W, 24):
        p0 = o[s][4]; a, b = p0 * (1 + lo / 100), p0 * (1 + hi / 100)
        L = liq(capital, p0, a, b); x0, y0 = amounts(L, p0, a, b)
        win = o[s + 1:s + 1 + W]
        fees = sum(h[5] * fee * L / (lc + L) for h in win if a <= h[4] <= b)
        inr = sum(a <= h[4] <= b for h in win) / W
        p1 = o[s + W][4]; x1, y1 = amounts(L, p1, a, b)
        res.append((fees, x1 * p1 + y1 - (x0 * p1 + y0), inr))
    if not res: return None
    f, il, inr = (st.mean(x[j] for x in res) for j in range(3))
    return dict(fees=f, il=il, net=f + il, in_range=inr, worst=min(x[0] + x[1] for x in res), windows=len(res))

def simulate(o, lc, fee_tier, lp_fee, lo, hi, capital, rebalance, shift=0.5, market_hours=True):
    """整段样本模拟。rebalance=True 时出区间按区间对数宽度的 shift 比例朝出界方向平移，直到价格回到区间内。"""
    p0 = o[0][4]; a, b = p0 * (1 + lo / 100), p0 * (1 + hi / 100)
    L = liq(capital, p0, a, b); x0, y0 = amounts(L, p0, a, b)
    fees = swap_fee = impact = gas = 0.0; n = inr = 0
    for h in o[1:]:
        p = h[4]
        if a <= p <= b:
            fees += h[5] * lp_fee * L / (lc + L); inr += 1
        elif rebalance and (not market_hours or us_open(h[0])):
            x, y = amounts(L, p, a, b); val = x * p + y
            step = (b / a) ** shift
            while p > b: a, b = a * step, b * step
            while p < a: a, b = a / step, b / step
            xt, _ = amounts(liq(val, p, a, b), p, a, b)
            sw = abs(xt - x) * p
            c_fee, c_imp = sw * fee_tier, sw * sw / (lc * math.sqrt(p))
            swap_fee += c_fee; impact += c_imp; gas += GAS_USD; n += 1
            L = liq(val - c_fee - c_imp - GAS_USD, p, a, b)
    pe = o[-1][4]; x, y = amounts(L, pe, a, b)
    hours = len(o) - 1; ann = 8760 / hours / capital
    lp_val, hodl = x * pe + y, x0 * pe + y0
    cost = swap_fee + impact + gas
    return dict(rebalances=n, in_range=inr / hours, fees=fees, swap_fee=swap_fee, impact=impact, gas=gas,
                cost=cost, cost_per_rebalance=cost / n if n else 0, fee_apr=fees * ann, cost_apr=cost * ann,
                vs_hodl_apr=(lp_val + fees - hodl) * ann, pnl_apr=(lp_val + fees - capital) * ann,
                hodl_apr=(hodl - capital) * ann, days=hours / 24)

def backtest(pool, ranges, capital=1000, days=7, shift=0.5, market_hours=True):
    i = pool_info(pool)
    o = hourly(i)
    if len(o) < days * 24 + 48: raise ValueError("历史数据不足")
    out = []
    for lo, hi in ranges:
        out.append(dict(lo=lo, hi=hi,
                        rolling=rolling(o, i["Lh"], i["lp_fee"], lo, hi, capital, days * 24),
                        static=simulate(o, i["Lh"], i["fee"] / 1e6, i["lp_fee"], lo, hi, capital, False),
                        rebalance=simulate(o, i["Lh"], i["fee"] / 1e6, i["lp_fee"], lo, hi, capital, True, shift, market_hours)))
    return dict(pool=i["pool"], name=i["name"], hours=len(o), start=o[0][0], end=o[-1][0], lp_fee=i["lp_fee"],
                fee_tier=i["fee"] / 1e6, capital=capital, days=days, shift=shift, market_hours=market_hours, results=out)

def depth(pool, span=0.12, buckets=60):
    """当前价 ±span 内的活跃流动性分布（人类单位），用于画深度图。"""
    from .chain import batch, sel
    i = pool_info(pool)
    pool = i["pool"]
    ts = int(call(pool, "tickSpacing()"), 16)
    t = words(call(pool, "slot0()"))[1]
    t = t - 2**256 if t >= 2**255 else t
    t = t - 2**24 if t >= 2**23 else t
    base = (t // ts) * ts
    n = int(math.log(1 + span) / math.log(1.0001)) // ts + 1
    ids = [base + j * ts for j in range(-n, n + 1)]
    res = batch([("eth_call", [{"to": pool, "data": sel("ticks(int24)") + format(x & (2**256 - 1), "064x")}, "latest"]) for x in ids])
    net = {}
    for x, r in zip(ids, res):
        v = int(r[66:130], 16) if r else 0
        net[x] = v - 2**256 if v >= 2**255 else v
    act, cur = {base: i["L"]}, i["L"]
    for x in ids:
        if x > base: cur += net[x]; act[x] = cur
    cur = i["L"]
    for x in reversed(ids):
        if x <= base: act[x] = cur; cur -= net[x]
    scale = 10 ** ((i["d0"] + i["d1"]) / 2)
    dd = 10 ** (i["d0"] - i["d1"])
    lo, hi = i["price"] / (1 + span), i["price"] * (1 + span)
    edges = [lo * (hi / lo) ** (j / buckets) for j in range(buckets + 1)]
    out = []
    for j in range(buckets):
        mid = math.sqrt(edges[j] * edges[j + 1])
        raw = mid / dd if i["stable_is_1"] else 1 / (mid * dd)
        x = math.floor(math.log(raw) / math.log(1.0001) / ts) * ts
        out.append({"p0": edges[j], "p1": edges[j + 1], "L": act.get(x, 0) / scale})
    return {"price": i["price"], "Lh": i["Lh"], "lo": lo, "hi": hi, "bins": out, "tick_spacing": ts}
