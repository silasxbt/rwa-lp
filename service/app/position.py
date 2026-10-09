"""仓位实时评估：近 24h 年化、调仓成本与调仓后收益、真实股价对照的区间位置评级。"""
import math, time
import requests
from .chain import call, rpc, cached, words
from .analytics import pool_info, depth, liq, amounts

CHAINLINK_BNB = "0x0567F2323251f0Aab15c8dFb1967E4e8A7D42aeE"  # BNB/USD，8 位小数
GAS_UNITS = 250_000 + 150_000 + 450_000  # 撤池+领取、swap、开新仓
TICKERS = {"QQQB": "QQQ", "AAPLB": "AAPL", "GOOGLB": "GOOGL", "NVDAB": "NVDA", "BABAB": "BABA",
           "PDDB": "PDD", "MSTRB": "MSTR", "TSLAB": "TSLA"}

def gas_usd():
    def f():
        gp = int(rpc("eth_gasPrice", []), 16)
        bnb = int(call(CHAINLINK_BNB, "latestAnswer()"), 16) / 1e8
        return {"gwei": gp / 1e9, "bnb": bnb, "usd": GAS_UNITS * gp / 1e18 * bnb}
    return cached("gas", 120, f)

def competitor_L(i, price):
    """目标价位上其他 LP 的活跃流动性（人类单位），取自链上 tick 分布，取不到时用当前值。"""
    try:
        d = cached(("depth", i["pool"]), 60, lambda: depth(i["pool"]))
        for b in d["bins"]:
            if b["p0"] <= price < b["p1"]: return max(b["L"], 1e-9)
    except Exception:
        pass
    return i["Lh"]

def fee_rate(i, lo, hi, value, price, vol, comp=None):
    """comp：竞争流动性。默认取该价位的链上值；调仓后估算用当前价位的值（假设其他 LP 会跟着价格移动）。"""
    if not lo < price < hi or value <= 0: return 0, 0
    uL = liq(value, price, lo, hi)
    share = uL / ((comp if comp is not None else competitor_L(i, price)) + uL)
    return share, vol * i["lp_fee"] * share

PREMIUM_FLOOR = 0.002  # 价差成本下限：拿不到真实股价、或休市时，按 0.2% 计

def rebalance(i, value, x, y, price, lo, hi, gas, premium=None, comp_now=None):
    """把当前持仓（x 股 + y 美元，按 price 计价）换成新区间 [lo,hi] 需要的配比，返回成本和调仓后收益。
    成本 = swap 手续费 + 价格冲击 + 价差（链上价偏离真实股价的那部分，只在买贵 / 卖便宜的方向计） + gas。
    调仓后收益用保守口径：竞争流动性取当前价位的值（其他 LP 会跟着价格移动），成交量取 7 日均值。"""
    need_x, _ = amounts(liq(value, price, lo, hi), price, lo, hi)
    buy = need_x > x
    swap = abs(need_x - x) * price
    lc = competitor_L(i, price)
    fee = swap * i["fee"] / 1e6
    impact = swap * swap / (lc * math.sqrt(price)) if lc else 0
    if premium is None: spread_pct = PREMIUM_FLOOR
    else: spread_pct = max(premium if buy else -premium, 0) + PREMIUM_FLOOR / 2
    spread = swap * spread_pct
    cost = fee + impact + spread + gas
    share, day = fee_rate(i, lo, hi, value - cost, price, i["vol7"], comp_now)
    return dict(lo=lo, hi=hi, at=price, value=value, swap=swap, side="买入股票" if buy else "卖出股票",
                swap_fee=fee, impact=impact, spread=spread, spread_pct=spread_pct, gas=gas, cost=cost,
                cost_pct=cost / value if value else 0, share=share, fee_day=day,
                apr24=day * 365 / value if value else 0, payback_days=cost / day if day else None)

def fair_price(i):
    sym = i["symbol0"] if i["stable_is_1"] else i["symbol1"]
    tk = TICKERS.get(sym.upper()) or (sym[:-1].upper() if sym.upper().endswith("B") else None)
    if not tk: return None
    def f():
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{tk}", params={"interval": "1m", "range": "1d", "includePrePost": "true"},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=10).json()["chart"]["result"][0]
        m = r["meta"]; closes = [c for c in (r.get("indicators", {}).get("quote", [{}])[0].get("close") or []) if c]
        reg = m.get("currentTradingPeriod", {}).get("regular", {})
        now = time.time()
        state = "盘中" if reg.get("start", 0) <= now < reg.get("end", 0) else "休市"
        last = closes[-1] if closes else m["regularMarketPrice"]
        return {"ticker": tk, "price": m["regularMarketPrice"], "last": last, "prev_close": m.get("chartPreviousClose"),
                "time": m.get("regularMarketTime"), "state": state}
    try:
        q = cached(("yahoo", tk), 60, f)
    except Exception:
        return None
    mult = 1.0
    if i["bstocks"]:
        try: mult = int(call(i["stock"], "uiMultiplier()"), 16) / 1e18
        except Exception: pass
    return dict(q, multiplier=mult, fair=q["last"] * mult)

def place(lo, hi, p):
    """价格在区间内的位置：0=下沿，1=上沿；离最近边界的距离（对数收益）。"""
    pos = (math.log(p) - math.log(lo)) / (math.log(hi) - math.log(lo))
    edge = min(math.log(p / lo), math.log(hi / p))
    return pos, edge

def rating(i, lo, hi, fair):
    sig = i["sigma_day"] or 0.02
    p = i["price"]
    pos, edge = place(lo, hi, p)
    reasons, level = [], 0  # 0 安全 1 警惕 2 危险
    def bump(l, why): nonlocal level; level = max(level, l); reasons.append(why)
    if edge < 0: bump(2, "链上价格已在区间外，停止收手续费")
    elif edge < 0.5 * sig: bump(2, f"离边界仅 {edge*100:.1f}%，不到半天的典型波动（日波动 {sig*100:.1f}%）")
    elif edge < sig: bump(1, f"离边界 {edge*100:.1f}%，小于一天的典型波动（{sig*100:.1f}%）")
    fp = None
    if fair:
        fp = fair["fair"]
        prem = p / fp - 1
        fpos, fedge = place(lo, hi, fp)
        if fedge < 0: bump(2, f"真实股价折算 ${fp:,.2f} 已在区间外，{('开盘后' if fair['state'] == '休市' else '')}套利会把链上价拉出区间")
        elif fedge < 0.5 * sig: bump(1, f"真实股价折算离边界只有 {fedge*100:.1f}%")
        if abs(prem) > 0.01: bump(1, f"链上价相对真实股价{'溢价' if prem > 0 else '折价'} {abs(prem)*100:.1f}%，会被套利拉回")
        if fair["state"] == "休市": reasons.append("美股休市，链上价没有锚，开盘可能跳空")
    if not reasons: reasons.append(f"离最近边界 {edge*100:.1f}%，约 {edge/sig:.1f} 个日波动")
    return dict(level=["安全", "警惕", "危险"][level], code=level, pos=pos, edge=edge, sigma_day=sig,
                fair_pos=place(lo, hi, fp)[0] if fp else None, reasons=reasons)

def evaluate(pool, lo, hi, capital, shift=0.5):
    i = pool_info(pool)
    p = i["price"]
    gas = gas_usd()
    share, day = fee_rate(i, lo, hi, capital, p, i["vol24"])
    uL = liq(capital, p, lo, hi)
    x, y = amounts(uL, p, lo, hi)
    step = (hi / lo) ** shift
    w = math.sqrt(hi / lo)
    # 三种情形：价格打到上沿后上移、打到下沿后下移、现在按当前价重新居中
    xu, yu = amounts(uL, hi, lo, hi); xd, yd = amounts(uL, lo, lo, hi)
    fair = fair_price(i)
    prem = (p / fair["fair"] - 1) if fair else None
    comp = competitor_L(i, p)
    scen = {
        "up": rebalance(i, xu * hi + yu, xu, yu, hi, lo * step, hi * step, gas["usd"], prem, comp),
        "down": rebalance(i, xd * lo + yd, xd, yd, lo, lo / step, hi / step, gas["usd"], prem, comp),
        "recenter": rebalance(i, capital, x, y, p, p / w, p * w, gas["usd"], prem, comp),
    }
    return dict(pool=i["pool"], name=i["name"], price=p, lo=lo, hi=hi, capital=capital, shift=shift,
                vol24=i["vol24"], share24=share, fee_day24=day, apr24=day * 365 / capital,
                holdings={"stock": x, "stock_usd": x * p, "stable": y}, gas=gas,
                rebalance=scen, fair=fair, rating=rating(i, lo, hi, fair))
