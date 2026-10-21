import os, hmac, pathlib
from fastapi import FastAPI, HTTPException, Header, Query, Body
from fastapi.responses import HTMLResponse, Response
from .chain import cached, rpc
from . import analytics, monitor, telegram, position, research, strategy

app = FastAPI(title="rwalp", docs_url="/api/docs")
CRON_KEY = os.environ.get("CRON_KEY", "")
TG_SECRET = os.environ.get("TG_WEBHOOK_SECRET", "")
INDEX = (pathlib.Path(__file__).parent / "static" / "index.html").read_text()

def _pool(address):
    if not (address.startswith("0x") and len(address) == 42): raise HTTPException(400, "池子地址格式错误")
    try:
        return analytics.pool_info(address)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"读取池子失败：{e}")

@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX.replace("{{BOT}}", os.environ.get("TG_BOT_USERNAME", ""))

GUIDE = (pathlib.Path(__file__).parent / "static" / "guide.html").read_text()

@app.get("/guide", response_class=HTMLResponse)
def guide():
    return GUIDE.replace("{{BOT}}", os.environ.get("TG_BOT_USERNAME", ""))

FAVICON = (pathlib.Path(__file__).parent / "static" / "favicon.svg").read_bytes()

@app.get("/favicon.svg")
@app.get("/favicon.ico")
def favicon():
    return Response(FAVICON, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

@app.get("/health")
def healthz():
    return {"ok": True}

@app.get("/api/pool")
def pool(address: str, lo: float | None = None, hi: float | None = None, capital: float = Query(1000, gt=0, le=1e8)):
    i = _pool(address)
    lo = lo or i["price"] * 0.96
    hi = hi or i["price"] * 1.06
    gov = None
    if i["bstocks"]:
        try: gov = monitor.token_governance(i["stock"], i["pool"])
        except Exception: gov = None
    return {"pool": {k: v for k, v in i.items() if k != "L"}, "estimate": analytics.estimate(i, min(lo, hi), max(lo, hi), capital),
            "governance": gov}

@app.get("/api/backtest")
def backtest(address: str, ranges: str = "-3:3,-4:6,-5:7", capital: float = Query(1000, gt=0, le=1e8),
             days: int = Query(7, ge=1, le=30), shift: float = Query(0.5, gt=0, le=2), market_hours: bool = True,
             hedge: str = Query("none", pattern="^(none|perp|broker)$"), threshold: float = Query(0.05, ge=0, le=1),
             hedge_rate: float | None = Query(None, ge=-1, le=1), hedge_fee: float | None = Query(None, ge=0, le=0.05),
             hedge_fixed: float | None = Query(None, ge=0, le=100), leverage: float | None = Query(None, ge=1, le=20)):
    _pool(address)
    try:
        rs = [tuple(sorted(map(float, r.split(":")))) for r in ranges.split(",")][:6]
        assert all(lo < 0 < hi for lo, hi in rs)
    except Exception:
        raise HTTPException(400, "区间格式：-4:6,-3:3（相对当前价的百分比，下沿为负、上沿为正）")
    hg = None if hedge == "none" else analytics.hedge_cfg(hedge, threshold, rate=hedge_rate, fee=hedge_fee, fixed=hedge_fixed, leverage=leverage)
    try:
        return analytics.backtest(address, rs, capital, days, shift, market_hours, hg)
    except ValueError as e:
        raise HTTPException(400, str(e))

@app.get("/api/hedge-presets")
def hedge_presets():
    return analytics.HEDGE_PRESETS

@app.get("/api/position")
def pos(address: str, lo: float = Query(..., gt=0), hi: float = Query(..., gt=0), capital: float = Query(1000, gt=0, le=1e8),
        shift: float = Query(0.5, gt=0, le=2)):
    _pool(address)
    lo, hi = min(lo, hi), max(lo, hi)
    if hi / lo < 1.002: raise HTTPException(400, "区间太窄")
    return position.evaluate(address, lo, hi, capital, shift)

@app.get("/api/board")
def board(pools: str | None = None):
    ps = [p for p in (pools.split(",") if pools else monitor.KNOWN_POOLS.values()) if p.startswith("0x") and len(p) == 42]
    if not ps: raise HTTPException(400, "没有池子地址")
    return cached(("board", tuple(sorted(x.lower() for x in ps))), 300, lambda: analytics.board(ps))

@app.get("/api/siblings")
def siblings(address: str):
    _pool(address)
    try: return cached(("sib", address.lower()), 900, lambda: analytics.siblings(address))
    except Exception as e: raise HTTPException(502, f"读取同标的池子失败：{e}")

@app.get("/api/source")
def source():
    from .chain import data_source
    return data_source()

@app.get("/api/depth")
def depth(address: str):
    _pool(address)
    return cached(("depth", address.lower()), 60, lambda: analytics.depth(address))

def _guard(fn, what):
    try: return fn()
    except HTTPException: raise
    except ValueError as e: raise HTTPException(400, str(e))
    except Exception as e: raise HTTPException(502, f"{what}失败：{e}")

def _range(lo, hi):
    lo, hi = min(lo, hi), max(lo, hi)
    if hi / lo < 1.002: raise HTTPException(400, "区间太窄")
    return lo, hi

@app.get("/api/research")
def api_research(address: str):
    _pool(address)
    return _guard(lambda: cached(("research", address.lower()), 600, lambda: research.research(address)), "投研")

@app.get("/api/suggest")
def api_suggest(address: str, capital: float = Query(1000, gt=0, le=1e8), horizon: int = Query(7, ge=1, le=30),
                shift: float = Query(0.5, gt=0, lt=1)):
    _pool(address)
    return _guard(lambda: cached(("suggest", address.lower(), capital, horizon, shift), 600,
                                 lambda: strategy.suggest(address, capital, horizon, shift)), "区间测算")

@app.get("/api/apr")
def api_apr(address: str, lo: float = Query(..., gt=0), hi: float = Query(..., gt=0), capital: float = Query(1000, gt=0, le=1e8),
            shift: float = Query(0.5, gt=0, lt=1), risk: float = Query(0.05, ge=0, le=1)):
    _pool(address); lo, hi = _range(lo, hi)
    return _guard(lambda: strategy.apr(address, lo, hi, capital, shift, risk), "年化测算")

@app.get("/api/ladder")
def api_ladder(address: str, capital: float = Query(1000, gt=0, le=1e8), tiers: str = "50:-2:2,30:-5:5,20:-12:12",
               shift: float = Query(0.5, gt=0, le=2), market_hours: bool = True):
    _pool(address)
    try:
        ts = [tuple(map(float, t.split(":"))) for t in tiers.split(",")][:5]
        tot = sum(t[0] for t in ts)
        ts = [(w / tot, min(a, b), max(a, b)) for w, a, b in ts]
        assert tot > 0 and all(lo < 0 < hi for _, lo, hi in ts)
    except Exception:
        raise HTTPException(400, "分层格式：权重:下沿%:上沿%，逗号分隔，如 50:-2:2,30:-5:5,20:-12:12")
    return _guard(lambda: cached(("ladder", address.lower(), capital, tuple(ts), shift, market_hours), 600,
                                 lambda: strategy.ladder(address, capital, ts, shift, market_hours)), "分层回测")

@app.get("/api/options")
def api_options(address: str, lo: float = Query(..., gt=0), hi: float = Query(..., gt=0), capital: float = Query(1000, gt=0, le=1e8),
                days: int = Query(30, ge=1, le=120)):
    _pool(address); lo, hi = _range(lo, hi)
    return _guard(lambda: strategy.options(address, lo, hi, capital, days), "期权测算")

@app.get("/api/compare")
def api_compare(address: str, lo: float = Query(..., gt=0), hi: float = Query(..., gt=0), capital: float = Query(1000, gt=0, le=1e8),
                days: int = Query(7, ge=1, le=60), funding: float | None = Query(None, ge=-1, le=1)):
    _pool(address); lo, hi = _range(lo, hi)
    return _guard(lambda: strategy.compare(address, lo, hi, capital, days, funding), "策略对比")

@app.get("/api/tracking")
def api_tracking(pools: str | None = None):
    ps = [p.lower() for p in (pools.split(",") if pools else monitor.KNOWN_POOLS.values()) if p.startswith("0x") and len(p) == 42][:20]
    rows = _guard(lambda: cached(("track", tuple(sorted(ps))), 120, lambda: monitor.pool_status(ps, save=False)), "池子追踪")
    try: log = (monitor.store.get_state("pool_log") or {}).get("items", [])
    except Exception: log = []
    return {"pools": [dict(r, flags=[t for _, t in r["flags"]]) for r in rows], "log": log[:20],
            "rules": {"tvl_drop": monitor.TVL_DROP, "dev_open": monitor.DEVIATION_OPEN, "dev_closed": monitor.DEVIATION_CLOSED,
                      "vol_spike": monitor.VOL_SPIKE, "move_1h": monitor.MOVE_1H}}

@app.get("/api/head")
def head():
    return cached("head", 3, lambda: {"block": int(rpc("eth_blockNumber", []), 16)})

@app.get("/api/bstocks")
def bstocks():
    return cached("gov", 60, lambda: {"tokens": monitor.bstocks_tokens(), "state": monitor.governance()})

@app.post("/tg/{secret}")
def tg(secret: str, update: dict = Body(...)):
    if not TG_SECRET or not hmac.compare_digest(secret, TG_SECRET): raise HTTPException(404)
    telegram.handle(update)
    return {"ok": True}

@app.post("/cron/tick")
def tick(x_cron_key: str = Header("")):
    if not CRON_KEY or not hmac.compare_digest(x_cron_key, CRON_KEY): raise HTTPException(404)
    return monitor.tick()
