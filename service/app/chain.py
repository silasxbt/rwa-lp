"""BSC RPC / GeckoTerminal 访问与缓存。"""
import os, time, threading, requests
from Crypto.Hash import keccak

RPC = os.environ.get("BSC_RPC", "https://bsc-rpc.publicnode.com")
CG_KEY = os.environ.get("COINGECKO_API_KEY", "")
CG_BUDGET = int(os.environ.get("COINGECKO_MONTHLY_BUDGET", "9000"))   # Demo 档有月度额度，留出余量
# 两个数据源是同一套 GeckoTerminal 数据：有 key 时优先 CoinGecko（限额高），失败、限流或额度用完时退回公共接口
CG = "https://api.coingecko.com/api/v3/onchain/networks/bsc/"
GTP = "https://api.geckoterminal.com/api/v2/networks/bsc/"
GT = (CG if CG_KEY else GTP) + "pools/"
_s = requests.Session()
USAGE = {"cg_calls": 0, "gt_calls": 0, "cg_errors": 0, "fallbacks": 0, "cg_pause_until": 0.0, "last_error": None,
         "month": None, "month_calls": None, "unflushed": 0, "flushed_at": 0.0}
_cache, _lock = {}, threading.Lock()

def k(s: str) -> str:
    return keccak.new(digest_bits=256, data=s.encode()).hexdigest()

def sel(sig: str) -> str:
    return "0x" + k(sig)[:8]

pad = lambda a: a[2:].lower().zfill(64)
addr = lambda h: "0x" + h[-40:]
words = lambda h: [int(h[2 + i:66 + i], 16) for i in range(0, len(h) - 2, 64)]

def rpc(method, params):
    r = _s.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20).json()
    if "error" in r: raise RuntimeError(f"{method}: {r['error']}")
    return r["result"]

def call(to, sig, args=""):
    return rpc("eth_call", [{"to": to, "data": sel(sig) + args}, "latest"])

def batch(reqs):
    """reqs: [(method, params)] → 结果列表（出错为 None），每 50 个一批。"""
    out = []
    for i in range(0, len(reqs), 50):
        body = [{"jsonrpc": "2.0", "id": j, "method": m, "params": p} for j, (m, p) in enumerate(reqs[i:i + 50])]
        res = sorted(_s.post(RPC, json=body, timeout=30).json(), key=lambda r: r["id"])
        out += [r.get("result") for r in res]
    return out

def calls(items):
    """items: [(to, sig, args)] → eth_call 结果列表。"""
    return batch([("eth_call", [{"to": t, "data": sel(s) + a}, "latest"]) for t, s, a in items])

def cached(key, ttl, fn):
    with _lock:
        hit = _cache.get(key)
        if hit and time.time() - hit[0] < ttl: return hit[1]
    val = fn()
    with _lock: _cache[key] = (time.time(), val)
    return val

def _usage_doc():
    from google.cloud import firestore
    global _fs
    if "_fs" not in globals(): _fs = firestore.Client()
    return _fs.collection("state").document("coingecko_usage_" + time.strftime("%Y-%m", time.gmtime())), firestore

def _flush_usage(force=False):
    """把本实例的调用次数累加到 Firestore（公开站和私有站共用一个 key，所以按月合计）。本地没有凭据时静默跳过。"""
    now = time.time(); month = time.strftime("%Y-%m", time.gmtime())
    if USAGE["month"] != month: USAGE.update(month=month, month_calls=None)
    if not force and USAGE["unflushed"] < 20 and now - USAGE["flushed_at"] < 300 and USAGE["month_calls"] is not None: return
    try:
        ref, fs = _usage_doc(); n = USAGE["unflushed"]
        if n: ref.set({"calls": fs.Increment(n), "updated": now}, merge=True)
        USAGE["unflushed"] = 0; USAGE["flushed_at"] = now
        d = ref.get(); USAGE["month_calls"] = int((d.to_dict() or {}).get("calls", 0)) if d.exists else 0
    except Exception:
        USAGE["flushed_at"] = now
        if USAGE["month_calls"] is None: USAGE["month_calls"] = USAGE["cg_calls"]

def _cg_ok():
    if not CG_KEY or time.time() < USAGE["cg_pause_until"]: return False
    _flush_usage()
    return (USAGE["month_calls"] or 0) + USAGE["unflushed"] < CG_BUDGET

def _fetch(path, params):
    """path 相对 networks/bsc/。返回 JSON 的 data；CoinGecko 出错时暂停它一会儿并退回 GeckoTerminal 公共接口。"""
    if _cg_ok():
        try:
            r = _s.get(CG + path, params=params, headers={"x-cg-demo-api-key": CG_KEY}, timeout=20)
            USAGE["cg_calls"] += 1; USAGE["unflushed"] += 1
            j = r.json()
            if "data" in j: return j["data"]
            USAGE["cg_errors"] += 1; USAGE["last_error"] = f"CoinGecko {r.status_code}: {str(j)[:120]}"
            # 429 = 每分钟限流，歇 1 分钟；401/403 或额度类错误，歇 1 小时
            USAGE["cg_pause_until"] = time.time() + (60 if r.status_code == 429 else 3600 if r.status_code in (401, 403) else 120)
        except (requests.RequestException, ValueError) as e:
            USAGE["cg_errors"] += 1; USAGE["last_error"] = f"CoinGecko: {str(e)[:120]}"; USAGE["cg_pause_until"] = time.time() + 60
        USAGE["fallbacks"] += 1
    for n in range(6):
        try:
            USAGE["gt_calls"] += 1
            j = _s.get(GTP + path, params=params, timeout=20).json()
            if "data" in j: return j["data"]
            time.sleep(10)  # 公共接口限流
        except (requests.RequestException, ValueError):
            time.sleep(1 + n)  # 网络抖动 / TLS 中断，短暂重试
    raise RuntimeError("行情数据暂时不可用，请稍后再试")

def gt(path, ttl=300, **params):
    """池子接口（path 相对 pools/），带缓存。"""
    return cached(("gt", path, tuple(sorted(params.items()))), ttl, lambda: _fetch("pools/" + path, params))

def gt_multi(pools, ttl=900):
    """一次请求取多个池子的概况，并写进 gt(pool) 的缓存，省掉逐个请求。最多 30 个。"""
    pools = [p.lower() for p in pools][:30]
    with _lock:
        need = [p for p in pools if not (("gt", p, ()) in _cache and time.time() - _cache[("gt", p, ())][0] < ttl)]
    if need:
        for d in _fetch("pools/multi/" + ",".join(need), {}):
            with _lock: _cache[("gt", d["attributes"]["address"].lower(), ())] = (time.time(), d)
    return {p: _cache[("gt", p, ())][1] for p in pools if ("gt", p, ()) in _cache}

def token_pools(token, ttl=1800):
    """某个代币的全部池子（按成交和流动性排序的前 20 个）。"""
    return cached(("tp", token.lower()), ttl, lambda: _fetch(f"tokens/{token.lower()}/pools", {}))

def data_source():
    _flush_usage(force=USAGE["month_calls"] is None or time.time() - USAGE["flushed_at"] > 60)
    paused = time.time() < USAGE["cg_pause_until"]
    over = CG_KEY and (USAGE["month_calls"] or 0) >= CG_BUDGET
    return {"source": "CoinGecko" if CG_KEY and not paused and not over else "GeckoTerminal 公共接口", "key_configured": bool(CG_KEY),
            "month": USAGE["month"], "month_calls": USAGE["month_calls"], "budget": CG_BUDGET, "over_budget": bool(over),
            "paused_seconds": max(0, int(USAGE["cg_pause_until"] - time.time())), "last_error": USAGE["last_error"],
            "instance": {k: USAGE[k] for k in ("cg_calls", "gt_calls", "cg_errors", "fallbacks")}}

def symbol(token):
    h = call(token, "symbol()")[2:]
    n = int(h[64:128], 16)
    return bytes.fromhex(h[128:128 + 2 * n]).decode(errors="replace")
