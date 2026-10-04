import os, hmac, pathlib
from fastapi import FastAPI, HTTPException, Header, Query, Body
from fastapi.responses import HTMLResponse
from .chain import cached
from . import analytics, monitor, telegram

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
             days: int = Query(7, ge=1, le=30), shift: float = Query(0.5, gt=0, le=2), market_hours: bool = True):
    _pool(address)
    try:
        rs = [tuple(sorted(map(float, r.split(":")))) for r in ranges.split(",")][:6]
        assert all(lo < 0 < hi for lo, hi in rs)
    except Exception:
        raise HTTPException(400, "区间格式：-4:6,-3:3（相对当前价的百分比，下沿为负、上沿为正）")
    try:
        return analytics.backtest(address, rs, capital, days, shift, market_hours)
    except ValueError as e:
        raise HTTPException(400, str(e))

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
