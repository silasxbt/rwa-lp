"""事件钟与出界概率：把影响价格的事件（财报、议息、CPI、非农）折算成额外波动，估算区间在未来 N 天内被打穿的概率。

模型（对数价格做布朗运动）：
  - 普通交易日方差 = σ_d²（σ_d 是该池子的日波动）；周末两天合计按 0.6 个交易日计（链上照常交易但没有锚）
  - 事件日方差 = σ_d² + j²，j 是该事件的典型跳空幅度（财报按标的设，宏观事件按指数敏感度缩放）
  - 窗口 T 天的总方差 V = Σ 每天方差
  - 出界概率用首次穿越近似：P(触碰下沿) ≈ 2·Φ(−a/√V)，P(触碰上沿) ≈ 2·Φ(−b/√V)，合计 = 1 − (1 − P下)(1 − P上)
    a、b 是当前价到下沿、上沿的对数距离；无漂移、上下独立，是偏保守的上界近似
  - 预期在区间天数：按每天的累计出界概率求和
"""
import math, datetime as dt
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
WEEKEND_FACTOR = 0.3   # 周末每天的方差按普通交易日的 30% 计

# 事件日历：date 为美东日期；confirmed=False 表示按往年规律推算
MACRO = [
    {"date": "2026-10-14", "kind": "CPI", "title": "9 月 CPI", "time": "08:30", "confirmed": True},
    {"date": "2026-10-28", "kind": "FOMC", "title": "FOMC 议息决议", "time": "14:00", "confirmed": True},
    {"date": "2026-11-06", "kind": "NFP", "title": "10 月非农", "time": "08:30", "confirmed": False},
    {"date": "2026-11-10", "kind": "CPI", "title": "10 月 CPI", "time": "08:30", "confirmed": True},
    {"date": "2026-12-04", "kind": "NFP", "title": "11 月非农", "time": "08:30", "confirmed": False},
    {"date": "2026-12-09", "kind": "FOMC", "title": "FOMC 议息 + 点阵图", "time": "14:00", "confirmed": True},
    {"date": "2026-12-10", "kind": "CPI", "title": "11 月 CPI", "time": "08:30", "confirmed": True},
]
# 美股休市日（周末之外）：链上照常交易但没有锚，方差按周末处理
CLOSED = {"2026-11-26": "感恩节休市", "2026-12-25": "圣诞节休市"}
# 宏观事件对指数（QQQ 口径）的典型单日额外波动；个股按 beta 缩放
MACRO_JUMP = {"FOMC": 0.012, "CPI": 0.010, "NFP": 0.007}

# 财报：time = "after"（盘后，跳空落在次日）/ "before"（盘前，当日）；jump 为历史典型单日波动
EARNINGS = {
    "AAPL": {"date": "2026-10-29", "time": "after", "confirmed": False, "jump": 0.045},
    "MSFT": {"date": "2026-10-28", "time": "after", "confirmed": False, "jump": 0.050},
    "GOOGL": {"date": "2026-10-27", "time": "after", "confirmed": False, "jump": 0.060},
    "META": {"date": "2026-10-28", "time": "after", "confirmed": False, "jump": 0.080},
    "NVDA": {"date": "2026-11-18", "time": "after", "confirmed": False, "jump": 0.070},
    "BABA": {"date": "2026-11-13", "time": "before", "confirmed": False, "jump": 0.060},
    "PDD": {"date": "2026-11-18", "time": "before", "confirmed": False, "jump": 0.120},
    "MSTR": {"date": "2026-10-29", "time": "after", "confirmed": False, "jump": 0.080},
    "NFLX": {"date": "2026-10-20", "time": "after", "confirmed": True, "jump": 0.095},
}
# 指数成分的财报对指数的影响：QQQ 前几大权重股财报日按小跳空计
INDEX_EARNINGS = {"QQQ": [("AAPL", 0.010), ("MSFT", 0.009), ("GOOGL", 0.007), ("META", 0.007), ("NVDA", 0.014)]}
BETA = {"QQQ": 1.0, "AAPL": 1.1, "MSFT": 1.1, "GOOGL": 1.15, "META": 1.3, "NVDA": 1.6, "BABA": 0.8, "PDD": 0.9,
        "MSTR": 1.8, "NFLX": 1.2, "BNC": 0.6}

def _impact_day(date, when):
    """财报盘后发布，价格冲击落在下一个交易日；周五盘后落到周一。"""
    d = dt.date.fromisoformat(date)
    if when == "after":
        d += dt.timedelta(days=1)
        while d.weekday() >= 5: d += dt.timedelta(days=1)
    return d

def events_for(ticker, days=30, today=None):
    """该标的未来 days 天内的价格事件，按日期排序。"""
    today = today or dt.datetime.now(NY).date()
    end = today + dt.timedelta(days=days)
    beta = BETA.get(ticker, 1.0)
    out = []
    for m in MACRO:
        d = dt.date.fromisoformat(m["date"])
        if today <= d <= end:
            out.append(dict(m, ticker=ticker, impact=d.isoformat(), jump=MACRO_JUMP[m["kind"]] * beta, scope="宏观"))
    e = EARNINGS.get(ticker)
    if e:
        d = _impact_day(e["date"], e["time"])
        if today <= d <= end:
            out.append({"date": e["date"], "impact": d.isoformat(), "kind": "EARN", "title": f"{ticker} 财报（{'盘后' if e['time'] == 'after' else '盘前'}）",
                        "confirmed": e["confirmed"], "jump": e["jump"], "ticker": ticker, "scope": "个股"})
    for comp, j in INDEX_EARNINGS.get(ticker, []):
        ce = EARNINGS.get(comp)
        if not ce: continue
        d = _impact_day(ce["date"], ce["time"])
        if today <= d <= end:
            out.append({"date": ce["date"], "impact": d.isoformat(), "kind": "EARN", "title": f"{comp} 财报（成分股）",
                        "confirmed": ce["confirmed"], "jump": j, "ticker": ticker, "scope": "成分股"})
    for c, title in CLOSED.items():
        d = dt.date.fromisoformat(c)
        if today <= d <= end:
            out.append({"date": c, "impact": c, "kind": "CLOSED", "title": title, "confirmed": True, "jump": 0.0, "ticker": ticker, "scope": "休市"})
    for x in out: x["days_away"] = (dt.date.fromisoformat(x["impact"]) - today).days
    return sorted(out, key=lambda x: (x["impact"], -x["jump"]))

def _phi(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def daily_variance(sigma, evs, days=30, today=None):
    """未来每天的方差列表（含周末折减和事件跳空）。"""
    today = today or dt.datetime.now(NY).date()
    by = {}
    for e in evs: by[e["impact"]] = by.get(e["impact"], 0) + e["jump"] ** 2
    out = []
    for k in range(1, days + 1):
        d = today + dt.timedelta(days=k)
        closed = d.weekday() >= 5 or d.isoformat() in CLOSED
        v = sigma ** 2 * (WEEKEND_FACTOR if closed else 1.0) + by.get(d.isoformat(), 0)
        out.append((d.isoformat(), v))
    return out

def exit_probability(price, lo, hi, sigma, evs, horizons=(1, 3, 7, 14, 30), today=None):
    """区间出界概率（含事件跳空）与不含事件的对照，以及预期在区间内天数。"""
    if not (lo < price < hi) or sigma <= 0:
        return {"in_range": False}
    a, b = math.log(price / lo), math.log(hi / price)
    var = daily_variance(sigma, evs, max(horizons), today)
    base = daily_variance(sigma, [], max(horizons), today)
    def p(V):
        if V <= 0: return 0.0
        pd, pu = min(1, 2 * _phi(-a / math.sqrt(V))), min(1, 2 * _phi(-b / math.sqrt(V)))
        return 1 - (1 - pd) * (1 - pu), pd, pu
    cum = cumb = 0.0; rows = {}; stay = 0.0; prev = 0.0
    for k, ((d, v), (_, vb)) in enumerate(zip(var, base), 1):
        cum += v; cumb += vb
        pt, pd, pu = p(cum)
        stay += 1 - pt                                 # 第 k 天还在区间里的概率
        if k in horizons:
            rows[f"{k}d"] = {"p": pt, "down": pd, "up": pu, "p_no_events": p(cumb)[0], "vol": math.sqrt(cum)}
    return {"in_range": True, "dist_down": a, "dist_up": b, "horizons": rows,
            "expected_days_30": stay, "half_life_days": next((int(k[:-1]) for k, r in rows.items() if r["p"] >= 0.5), None)}

def clock(ticker, price, lo, hi, sigma, days=30, today=None):
    """事件钟：未来事件 + 每个事件前后的出界概率变化。"""
    evs = events_for(ticker, days, today)
    ep = exit_probability(price, lo, hi, sigma, evs, today=today)
    for e in evs:   # 单个事件当天如果发生 1 个标准差的跳空，是否会打穿区间
        if ep.get("in_range"):
            e["breach_1sd"] = e["jump"] >= min(ep["dist_down"], ep["dist_up"])
            e["breach_side"] = "下沿" if ep["dist_down"] <= ep["dist_up"] else "上沿"
    today = today or dt.datetime.now(NY).date()
    hit = {}
    for e in evs: hit.setdefault(e["impact"], []).append(e["title"])
    cal = [{"date": d, "sd": math.sqrt(v), "closed": dt.date.fromisoformat(d).weekday() >= 5 or d in CLOSED, "events": hit.get(d, [])}
           for d, v in daily_variance(sigma, evs, days, today)]
    return {"ticker": ticker, "today": today.isoformat(), "events": evs, "exit": ep, "calendar": cal}
