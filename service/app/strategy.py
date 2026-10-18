"""策略工具：参考区间、各口径年化（含 LVR 与调仓损失折合年化）、分层 LP、期权保护、做空对比。

几个公式（对数价格做布朗运动，日方差 σ²）：
  - LVR（集中流动性相对“同样持仓但随时再平衡”的损失，即无常损失的期望速度）：
      LVR / 仓位价值 = σ²/4 · √P / (2√P − √a − P/√b)   （全区间时退化成 σ²/8）
  - 出界期望时间：从区间内对数距离下沿 A、上沿 B 处出发，E[T] = A·B / σ²
    出界后按 shift 平移，新区间里价格离两边分别是 (1−shift)·w 和 shift·w（w = ln(b/a)），稳态下每次间隔 shift(1−shift)·w² / σ²
  - 调仓损失折合年化 = 每年调仓次数 × 单次成本占本金
"""
import math, time, bisect
from .analytics import pool_info, hourly, rolling, simulate, estimate, liq, amounts, GAS_USD, HEDGE_PRESETS, _to_usd
from .events import events_for, daily_variance, exit_probability, _phi
from .position import ticker_of, fair_price, PREMIUM_FLOOR

def lvr_k(p, a, b):
    """LVR/价值 = σ²·k/4 里的 k（集中度放大倍数，全区间为 1/2）。"""
    if not a < p < b: return 0.0
    sp = math.sqrt(p)
    return sp / (2 * sp - math.sqrt(a) - p / math.sqrt(b))

def var_avg(i, days=30):
    """未来 days 天的平均日方差（含事件跳空、周末折减）。"""
    tk = ticker_of(i) or ""
    v = daily_variance(i["sigma_day"] or 0.02, events_for(tk, days), days)
    return sum(x for _, x in v) / days

def rebal_cost(i, value_q, p):
    """一次出界调仓的成本（计价币）：出界时已全是单一资产，重新入场约换一半；swap 费 + 价差底线 + 价格冲击 + gas。"""
    sw = value_q / 2
    impact = sw * sw / (i["Lh"] * math.sqrt(p)) if i["Lh"] else 0
    return sw * (i["fee"] / 1e6 + PREMIUM_FLOOR) + impact + GAS_USD / i["qusd"]

def rebalances_per_year(w, var_day, shift=0.5):
    if var_day <= 0 or w <= 0: return 0.0
    return 365 * var_day / (shift * (1 - shift) * w * w)

# ---------- 选区间 ----------
GRID = [1, 1.5, 2, 3, 4, 5, 6, 8, 10, 12, 15]

def suggest(pool, capital=1000, horizon=7, shift=0.5):
    """网格扫描上下沿（相对当前价 %），同时看历史滚动回测和前瞻模型（含事件），给出保守 / 平衡 / 进取三个参考区间。"""
    i = pool_info(pool)
    p, q = i["price"], i["qusd"]
    cap_q = capital / q
    o = hourly(i)
    W = horizon * 24
    if len(o) < W + 48: raise ValueError("历史数据不足")
    tk = ticker_of(i) or ""
    evs = events_for(tk, horizon)
    sig = i["sigma_day"] or 0.02
    vday = var_avg(i, max(horizon, 7))
    fair = fair_price(i)
    rows = []
    for d in GRID:
        for u in GRID:
            if not 0.5 <= u / d <= 2: continue          # 太偏的不看
            lo, hi = p * (1 - d / 100), p * (1 + u / 100)
            ep = exit_probability(p, lo, hi, sig, evs, horizons=(horizon,))
            if not ep.get("in_range"): continue
            pe = ep["horizons"][f"{horizon}d"]["p"]; days_in = ep["expected_days_30"]   # 这里是 horizon 天内的预期在区间天数
            e = estimate(i, lo, hi, capital)
            fee = e["windows"]["7d"]["fee_day"] * days_in                               # 美元
            lvr = vday * lvr_k(p, lo, hi) / 4 * capital * days_in
            rb = pe * rebal_cost(i, cap_q, p) * q
            model = fee - lvr - rb
            h = _to_usd(rolling(o, i["Lh"], i["lp_fee"], -d, u, cap_q, W), q)
            if not h: continue
            score = min(model, h["net"])   # 前瞻模型和历史回测取较差的，避免被早期热度成交量带偏
            rows.append(dict(down=d, up=u, lo=lo, hi=hi, p_exit=pe, days_in=days_in, fee=fee, lvr=lvr, rebal=rb, model_net=model,
                             hist_net=h["net"], hist_fees=h["fees"], hist_in=h["in_range"], hist_worst=h["worst"],
                             score=score, apr=score / horizon * 365 / capital, fee_apr=fee / horizon * 365 / capital,
                             model_apr=model / horizon * 365 / capital, hist_apr=h["net"] / horizon * 365 / capital))
    if not rows: raise ValueError("没有可用的区间")
    best = lambda xs, key: max(xs, key=key) if xs else None
    cons = best([r for r in rows if r["p_exit"] <= 0.25 and r["hist_in"] >= 0.85], lambda r: r["score"])
    bal = best([r for r in rows if 0.25 < r["p_exit"] <= 0.5 and r is not cons], lambda r: r["score"]) or \
          best([r for r in rows if r["p_exit"] <= 0.5 and r is not cons], lambda r: r["score"])
    agg = best([r for r in rows if r["hist_in"] >= 0.5 and r["model_net"] > 0], lambda r: r["fee_apr"])
    picks = [dict(r, profile=n, why=w) for n, r, w in (
        ("保守", cons, f"{horizon} 天出界概率 ≤25%、历史在区间 ≥85% 里综合收益最高"),
        ("平衡", bal, f"{horizon} 天出界概率 25–50% 里综合收益最高"),
        ("进取", agg, "历史在区间 ≥50% 且模型净收益为正里手续费年化最高，需要盯盘")) if r]
    vh = sum(v for _, v in daily_variance(sig, evs, horizon))
    bands = [dict(k=kk, down=(1 - math.exp(-kk * math.sqrt(vh))) * 100, up=(math.exp(kk * math.sqrt(vh)) - 1) * 100,
                  lo=p * math.exp(-kk * math.sqrt(vh)), hi=p * math.exp(kk * math.sqrt(vh)), p_inside=2 * _phi(kk) - 1)
             for kk in (1.0, 1.5, 2.0)]
    skew = None
    if fair:
        prem = p / fair["fair"] - 1
        if abs(prem) > 0.005:
            skew = f"链上价相对真实股价{'溢价' if prem > 0 else '折价'} {abs(prem)*100:.2f}%，套利大概率把价格往{'下' if prem > 0 else '上'}拉，区间可以往{'下' if prem > 0 else '上'}多留一点"
    return dict(pool=i["pool"], name=i["name"], price=p, quote_sym=i["quote_sym"], quote_kind=i["quote_kind"], capital=capital,
                horizon=horizon, sigma_day=sig, vol_h=math.sqrt(vh), events=[e["title"] for e in evs if e["jump"]], picks=picks,
                rows=sorted(rows, key=lambda r: -r["score"])[:15], bands=bands, skew=skew, hours=len(o))

# ---------- 各口径年化 ----------
def apr(pool, lo, hi, capital=1000, shift=0.5, risk=0.05):
    """同一个仓位的多种年化口径，从毛手续费一路扣到净年化。"""
    i = pool_info(pool)
    p = i["price"]
    e = estimate(i, lo, hi, capital)
    sig = i["sigma_day"] or 0.02
    tk = ticker_of(i) or ""
    evs = events_for(tk, 30)
    ep = exit_probability(p, lo, hi, sig, evs) if lo < p < hi else {"in_range": False}
    inr = ep["expected_days_30"] / 30 if ep.get("in_range") else 0
    vday = var_avg(i, 30)
    k = lvr_k(p, lo, hi)
    lvr_apr = vday * k / 4 * 365
    w = math.log(hi / lo)
    n = rebalances_per_year(w, vday, shift)
    c = rebal_cost(i, capital / i["qusd"], p) * i["qusd"] / capital
    rb_apr = n * c
    lvr_days_apr = lvr_apr * inr
    rows = {}
    for key, wv in e["windows"].items():
        g = wv["apr"]
        tw = g * inr
        net = tw - lvr_days_apr - rb_apr
        rows[key] = dict(gross=g, pool=wv["pool_apr"], time_weighted=tw, net=net, apy=(1 + net / 52) ** 52 - 1 if net > -52 else -1,
                         risk_adj=(1 - risk) * (1 + net) - 1)
    base = rows["7d"]
    steps = [("毛手续费年化（7 日成交量）", base["gross"], "当前份额 × 7 日日均成交 × LP 实得费率"),
             ("× 预期在区间时间", base["time_weighted"] - base["gross"], f"事件钟估算未来 30 天约 {inr*30:.0f} 天在区间内（{inr*100:.0f}%）"),
             ("− 无常损失（LVR）", -lvr_days_apr, f"σ²/4 × 集中度 {k:.1f} × 365，在区间时才发生"),
             ("− 调仓损失", -rb_apr, f"每年约 {n:.0f} 次 × 单次 {c*100:.2f}%（swap 费 + 价差 + 冲击 + gas）"),
             ("= 净年化", base["net"], "U 本位，未计股价涨跌方向"),
             ("周复投后", base["apy"], "每周把手续费加回仓位"),
             (f"扣发行方风险（{risk*100:.0f}%/年）", base["risk_adj"], "(1 − p)(1 + 净年化) − 1")]
    be_vol = math.sqrt(4 * base["gross"] / 365 / k) if k and base["gross"] > 0 else None
    return dict(pool=i["pool"], name=i["name"], lo=lo, hi=hi, capital=capital, in_range=lo < p < hi, in_ratio=inr,
                sigma_day=sig, sigma_eff=math.sqrt(vday), lvr_k=k, lvr_apr=lvr_apr, rebalances_year=n, rebalance_cost=c,
                rebalance_apr=rb_apr, days_between=365 / n if n else None, windows=rows, steps=steps,
                breakeven_sigma=be_vol, payback_days=c / (base["gross"] / 365) if base["gross"] > 0 else None)

# ---------- 分层 LP ----------
DEFAULT_TIERS = [(0.5, -2, 2), (0.3, -5, 5), (0.2, -12, 12)]

def ladder(pool, capital=1000, tiers=DEFAULT_TIERS, shift=0.5, market_hours=True):
    """把本金分成几层不同宽度的区间，和单一区间对比历史表现与当前状态。"""
    i = pool_info(pool)
    q, p = i["qusd"], i["price"]
    o = hourly(i)
    if len(o) < 24 * 7: raise ValueError("历史数据不足")
    sig = i["sigma_day"] or 0.02
    evs = events_for(ticker_of(i) or "", 7)
    run = lambda lo, hi, cap: _to_usd(simulate(o, i["Lh"], i["fee"] / 1e6, i["lp_fee"], lo, hi, cap / q, True, shift, market_hours,
                                               gas_cost=GAS_USD / q), q)
    out, tot = [], dict(fees=0, cost=0, pnl=0, fee_now=0)
    for wgt, d, u in tiers:
        cap = capital * wgt
        r = run(d, u, cap)
        lo, hi = p * (1 + d / 100), p * (1 + u / 100)
        e = estimate(i, lo, hi, cap)
        ep = exit_probability(p, lo, hi, sig, evs, horizons=(7,))
        pnl = r["pnl_apr"] * cap
        out.append(dict(weight=wgt, down=d, up=u, lo=lo, hi=hi, capital=cap, rebalances=r["rebalances"], in_range=r["in_range"],
                        fee_apr=r["fee_apr"], cost_apr=r["cost_apr"], pnl_apr=r["pnl_apr"], vs_hodl_apr=r["vs_hodl_apr"],
                        mdd=r["max_drawdown"], fee_day_now=e["windows"]["7d"]["fee_day"], p_exit7=ep["horizons"]["7d"]["p"]))
        tot["fees"] += r["fee_apr"] * cap; tot["cost"] += r["cost_apr"] * cap; tot["pnl"] += pnl; tot["fee_now"] += e["windows"]["7d"]["fee_day"]
    combo = dict(fee_apr=tot["fees"] / capital, cost_apr=tot["cost"] / capital, pnl_apr=tot["pnl"] / capital,
                 fee_day_now=tot["fee_now"], apr_now=tot["fee_now"] * 365 / capital,
                 in_range=sum(t["in_range"] * t["weight"] for t in out))
    singles = []
    for d, u in sorted({(t[1], t[2]) for t in tiers}):
        r = run(d, u, capital)
        singles.append(dict(down=d, up=u, fee_apr=r["fee_apr"], cost_apr=r["cost_apr"], pnl_apr=r["pnl_apr"], in_range=r["in_range"],
                            rebalances=r["rebalances"], mdd=r["max_drawdown"]))
    return dict(pool=i["pool"], name=i["name"], capital=capital, tiers=out, combo=combo, singles=singles,
                start=o[0][0], end=o[-1][0], shift=shift)

# ---------- 期权保护 ----------
def bs_put(S, K, T, v, r=0.04):
    if T <= 0 or v <= 0: return max(K - S, 0)
    d1 = (math.log(S / K) + (r + v * v / 2) * T) / (v * math.sqrt(T)); d2 = d1 - v * math.sqrt(T)
    return K * math.exp(-r * T) * _phi(-d2) - S * _phi(-d1)

def chain(tk, days):
    """到期日 ≥ days 天的最近一组期权链（看跌）。"""
    from .research import _yahoo
    from .chain import cached
    def f():
        base = _yahoo(f"/v7/finance/options/{tk}")["optionChain"]["result"][0]
        target = time.time() + days * 86400
        exp = next((t for t in base["expirationDates"] if t >= target), base["expirationDates"][-1])
        r = _yahoo(f"/v7/finance/options/{tk}", date=exp)["optionChain"]["result"][0]
        puts = [dict(strike=x["strike"], bid=x.get("bid") or 0, ask=x.get("ask") or 0, last=x.get("lastPrice") or 0,
                     iv=x.get("impliedVolatility") or 0, oi=x.get("openInterest") or 0) for x in r["options"][0]["puts"]]
        return dict(expiry=exp, spot=r["quote"]["regularMarketPrice"], puts=puts)
    return cached(("chain", tk, days), 900, f)

def options(pool, lo, hi, capital=1000, days=30):
    """保护性看跌期权 / 看跌价差 覆盖跌破下沿后的损失；对比卖出同行权价看跌期权；手续费隐含波动 vs 期权隐含波动。"""
    i = pool_info(pool)
    p, q = i["price"], i["qusd"]
    fair = fair_price(i)
    if not fair: raise ValueError("没有对应的美股标的或取不到真实股价")
    tk, mult = fair["ticker"], fair["multiplier"]
    L = liq(capital / q, p, lo, hi)
    x_now, _ = amounts(L, p, lo, hi)
    x_max, _ = amounts(L, lo, lo, hi)              # 跌到下沿时全部是股票
    S = fair["last"]
    K_lo = S * lo / p                               # 下沿折算成真实股价
    shares = x_max * mult
    try:
        ch = chain(tk, days); src = "Yahoo 期权链"
    except Exception:
        ch = None; src = "Black-Scholes 估算（期权链不可用）"
    T = ((ch["expiry"] - time.time()) / 86400 if ch else days) / 365
    rv = (i["sigma_day"] or 0.02) * math.sqrt(365)
    def put(K):
        if ch and ch["puts"]:
            c = min(ch["puts"], key=lambda x: abs(x["strike"] - K))
            px_ = c["ask"] or c["last"] or bs_put(S, c["strike"], T, c["iv"] or rv)
            return dict(c, price=px_, mid=(c["bid"] + c["ask"]) / 2 if c["bid"] and c["ask"] else px_)
        v = bs_put(S, K, T, rv)
        return dict(strike=K, price=v, mid=v, iv=rv, bid=None, ask=None, oi=None)
    k1 = put(K_lo)
    k2 = put(K_lo * math.exp(-2 * rv * math.sqrt(T)))
    atm = put(S)
    cost1 = k1["price"] * shares
    cost_spread = max(k1["price"] - k2["mid"], 0) * shares
    contracts = shares / 100
    ann = 1 / T if T > 0 else 0
    e = estimate(i, lo, hi, capital)
    fee_period = e["windows"]["7d"]["fee_day"] * T * 365
    k = lvr_k(p, lo, hi)
    g7 = e["windows"]["7d"]["apr"]
    fee_iv = math.sqrt(4 * g7 / 365 / k) * math.sqrt(365) if k and g7 > 0 else None    # 手续费刚好覆盖 LVR 时的年化波动
    sell_put = k1["mid"] * shares
    return dict(pool=i["pool"], name=i["name"], ticker=tk, source=src, spot=S, multiplier=mult, lo=lo, hi=hi, K_lo=K_lo,
                expiry=time.strftime("%Y-%m-%d", time.gmtime(ch["expiry"])) if ch else None, days=T * 365,
                shares_now=x_now * mult, shares_max=shares, contracts=contracts, min_capital_1=capital / contracts if contracts else None,
                put=k1, put2=k2, cost_put=cost1, cost_put_apr=cost1 / capital * ann, cost_spread=cost_spread,
                cost_spread_apr=cost_spread / capital * ann, fee_period=fee_period, fee_apr=g7,
                atm_iv=atm.get("iv"), realized_vol=rv, fee_implied_vol=fee_iv, sell_put=sell_put, sell_put_apr=sell_put / capital * ann,
                bnb_note=i["quote_kind"] == "bnb")

# ---------- 做空对比 ----------
def compare(pool, lo, hi, capital=1000, days=7, funding=None):
    """到期价格情景下各策略的盈亏：持币、纯 LP、LP + 永续空（静态 delta）、LP + 看跌期权、纯做空、拿稳定币。
    按事件钟的总方差给情景加权，得到期望收益和 5% 最差情形。"""
    i = pool_info(pool)
    p, q = i["price"], i["qusd"]
    cap_q = capital / q
    L = liq(cap_q, p, lo, hi)
    x0, y0 = amounts(L, p, lo, hi)
    e = estimate(i, lo, hi, capital)
    fee_day = e["windows"]["7d"]["fee_day"] / q if lo < p < hi else 0
    sig = i["sigma_day"] or 0.02
    vh = sum(v for _, v in daily_variance(sig, events_for(ticker_of(i) or "", days), days))
    sd = math.sqrt(vh)
    perp = HEDGE_PRESETS["perp"]
    fr = perp["rate"] if funding is None else funding
    put = None
    try:
        op = options(pool, lo, hi, capital, max(days, 7))
        put = dict(strike_ratio=op["K_lo"] / op["spot"], cost=op["cost_put"] / q * min(1, days / max(op["days"], 1)), shares_tok=op["shares_max"] / op["multiplier"])
    except Exception:
        pass
    def pnl(m):
        P1 = p * math.exp(m)
        x1, y1 = amounts(L, P1, lo, hi)
        lp_val = x1 * P1 + y1
        if lo < P1 < hi: f_in = 1.0
        else:
            edge = math.log(hi / p) if P1 >= hi else math.log(p / lo)
            f_in = min(1.0, edge / abs(m)) if m else 1.0
        fees = fee_day * days * f_in
        lp = lp_val + fees - cap_q
        hedge = -x0 * (P1 - p) - x0 * p * (fr * days / 365 + 2 * perp["fee"])
        r = {"持币不动": x0 * P1 + y0 - cap_q, "纯 LP": lp, "LP + 永续空": lp + hedge}
        if put: r["LP + 看跌期权"] = lp + max(p * put["strike_ratio"] - P1, 0) * put["shares_tok"] - put["cost"]
        r["纯做空 1×"] = -cap_q * (P1 / p - 1) - cap_q * (fr * days / 365 + 2 * perp["fee"])
        r["拿稳定币"] = 0.0
        return {k: v * q for k, v in r.items()}
    grid = [x / 100 for x in range(-30, 31)]
    dens = [math.exp(-0.5 * (m / sd) ** 2) for m in grid]
    tw = sum(dens)
    allp = [pnl(m) for m in grid]
    names = list(allp[0].keys())
    stats = {}
    for nm in names:
        vals = [(r[nm], w / tw) for r, w in zip(allp, dens)]
        ev = sum(v * w for v, w in vals)
        acc, var5 = 0, None
        for v, w in sorted(vals):
            acc += w
            if acc >= 0.05: var5 = v; break
        stats[nm] = dict(ev=ev, var5=var5, ev_apr=ev / capital * 365 / days)
    show = [-0.15, -0.10, -0.06, -0.03, 0, 0.03, 0.06, 0.10, 0.15]
    rows = [dict(move=math.exp(m) - 1, price=p * math.exp(m), prob=None, **{"pnl": pnl(m)}) for m in show]
    return dict(pool=i["pool"], name=i["name"], capital=capital, days=days, lo=lo, hi=hi, price=p, sd=sd, funding=fr,
                delta=x0 * p * q / capital, names=names, rows=rows, stats=stats, put_used=bool(put), fee_day=fee_day * q)
