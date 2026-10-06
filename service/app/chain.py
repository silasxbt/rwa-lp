"""BSC RPC / GeckoTerminal 访问与缓存。"""
import os, time, threading, requests
from Crypto.Hash import keccak

RPC = os.environ.get("BSC_RPC", "https://bsc-rpc.publicnode.com")
GT = "https://api.geckoterminal.com/api/v2/networks/bsc/pools/"
_s = requests.Session()
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

def gt(path, ttl=300, **params):
    def fetch():
        for n in range(6):
            try:
                r = _s.get(GT + path, params=params, timeout=20).json()
                if "data" in r: return r["data"]
                time.sleep(10)  # 限流 30 次/分钟
            except (requests.RequestException, ValueError):
                time.sleep(1 + n)  # 网络抖动 / TLS 中断，短暂重试
        raise RuntimeError("GeckoTerminal 暂时不可用，请稍后再试")
    return cached(("gt", path, tuple(sorted(params.items()))), ttl, fetch)

def symbol(token):
    h = call(token, "symbol()")[2:]
    n = int(h[64:128], 16)
    return bytes.fromhex(h[128:128 + 2 * n]).decode(errors="replace")
