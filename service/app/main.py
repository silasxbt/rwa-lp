import os, hmac, pathlib
from fastapi import FastAPI, HTTPException, Header, Query, Body
from fastapi.responses import HTMLResponse, Response
from .chain import cached, rpc
from . import analytics, monitor, telegram, position

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
    return {"pool": {k: v for k, v in i.items() if k != "L"}, "estimate": analytics.estimate(i, min(lo, hi), max(lo, hi), capital)}

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

@app.get("/api/depth")
def depth(address: str):
    _pool(address)
    return cached(("depth", address.lower()), 60, lambda: analytics.depth(address))

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
