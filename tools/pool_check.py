# /// script
# dependencies = ["pycryptodome", "requests"]
# ///
"""V3 池子体检：官方 factory 校验、费率与协议抽成、流动性、成交量、你的份额与预估手续费。
用法: uv run tools/pool_check.py <池子地址> [<池子地址> ...] [--range 310 350] [--capital 1000]
区间为非稳定币一侧的美元价；不给 --range 时默认用当前价 -4%/+6%。"""
import argparse, math, time, statistics as st, requests
from Crypto.Hash import keccak

R = "https://bsc-rpc.publicnode.com"
GT = "https://api.geckoterminal.com/api/v2/networks/bsc/pools/"
FACTORIES = {"0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865": "PancakeSwap V3",
             "0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7": "Uniswap V3 (BSC)"}
STABLES = {"0x55d398326f99059ff775485246999027b3197955", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"}
BSTOCKS_BEACON = "156d6dce9a4f6139a3406f1f021f1a4880de93a3"
BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
k = lambda s: keccak.new(digest_bits=256, data=s.encode()).hexdigest()
pad = lambda a: a[2:].lower().zfill(64)

def rpc(m, p):
    r = requests.post(R, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=20).json()
    if "error" in r: raise RuntimeError(r["error"])
    return r["result"]
def call(to, sig, args=""):
    return rpc("eth_call", [{"to": to, "data": "0x" + k(sig)[:8] + args}, "latest"])
addr = lambda h: "0x" + h[-40:]
words = lambda h: [int(h[2 + i:66 + i], 16) for i in range(0, len(h) - 2, 64)]

def symbol(t):
    h = call(t, "symbol()")[2:]
    n = int(h[64:128], 16)
    return bytes.fromhex(h[128:128 + 2 * n]).decode()

def gt(url, **kw):
    for _ in range(8):
        r = requests.get(url, timeout=20, **kw).json()
        if "data" in r: time.sleep(3); return r["data"]
        time.sleep(15)  # GeckoTerminal 限流
    raise RuntimeError("GeckoTerminal 请求失败")

def protocol_cut(dex, fp):
    if dex.startswith("Pancake"):  # 两个 16 位，单位 1/10000
        return ((fp & 0xFFFF) + (fp >> 16)) / 2 / 1e4
    f0, f1 = fp % 16, fp >> 4      # Uniswap: 协议拿 1/n，0 表示关闭
    return ((1 / f0 if f0 else 0) + (1 / f1 if f1 else 0)) / 2

def check(pool, rng, capital):
    pool = pool.lower()
    f = addr(call(pool, "factory()"))
    t0, t1 = addr(call(pool, "token0()")), addr(call(pool, "token1()"))
    fee = int(call(pool, "fee()"), 16)
    dex = FACTORIES.get(f, "⚠️ 未知 factory")
    same = addr(call(f, "getPool(address,address,uint24)", pad(t0) + pad(t1) + hex(fee)[2:].zfill(64))) == pool
    d0, d1 = int(call(t0, "decimals()"), 16), int(call(t1, "decimals()"), 16)
    s0 = words(call(pool, "slot0()"))
    P = (s0[0] / 2**96) ** 2  # token1 raw / token0 raw
    L = int(call(pool, "liquidity()"), 16)
    cut = protocol_cut(dex, s0[5])
    stock, stable_is_1 = (t0, True) if t1 in STABLES else (t1, False)
    price = P * 10 ** (d0 - d1) if stable_is_1 else 1 / (P * 10 ** (d0 - d1))
    lo, hi = rng or (price * 0.96, price * 1.06)

    # 你的流动性（raw 单位），按 token1 计价
    if stable_is_1:
        pa, pb, cap1 = lo * 10 ** (d1 - d0), hi * 10 ** (d1 - d0), capital * 10 ** d1
    else:
        pa, pb, cap1 = 10 ** (d1 - d0) / hi, 10 ** (d1 - d0) / lo, capital / price * 10 ** d1
    pc = min(max(P, pa), pb)
    uL = cap1 / (2 * math.sqrt(pc) - math.sqrt(pa) - pc / math.sqrt(pb)) if pa < P < pb else 0
    share = uL / (L + uL) if uL else 0

    a = gt(GT + pool)["attributes"]
    o = sorted(gt(GT + pool + "/ohlcv/day", params={"limit": 31, "token": "base"})["attributes"]["ohlcv_list"])[:-1]
    v7, v30 = st.mean(x[5] for x in o[-7:]), st.mean(x[5] for x in o[-30:])
    tvl = float(a["reserve_in_usd"])
    lp_fee = fee / 1e6 * (1 - cut)
    beacon = rpc("eth_getStorageAt", [stock, BEACON_SLOT, "latest"])[-40:]

    print(f"\n===== {a['name']}  {pool}")
    print(f"DEX         {dex}  factory={f}  getPool 回查一致={same}")
    print(f"交易对      {symbol(t0)} {t0} / {symbol(t1)} {t1}")
    print(f"费率        {fee/1e4:.2f}%  协议抽成 {cut*100:.0f}%  → LP 实得 {lp_fee*100:.4f}%")
    print(f"发行方      {'bStocks（共用 Beacon/Admin/PauseManager）' if beacon == BSTOCKS_BEACON else '非 bStocks，需单独审计'}")
    print(f"价格        ${price:.2f}  创建于 {a['pool_created_at'][:10]}")
    print(f"TVL         ${tvl:,.0f}   当前价位活跃流动性 {L:.3e}")
    print(f"日均成交    7日 ${v7:,.0f}  30日 ${v30:,.0f}  (30日 成交/TVL={v30/tvl:.2f})")
    print(f"池子平均APR {v30*lp_fee/tvl*365*100:.0f}%（30日口径，已扣协议抽成）")
    print(f"你的仓位    ${capital:,.0f} @ ${lo:.2f}–${hi:.2f}  份额 {share*100:.3f}%  "
          f"手续费/天 7日口径 ${v7*lp_fee*share:.2f}  30日口径 ${v30*lp_fee*share:.2f}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pools", nargs="+")
    ap.add_argument("--range", nargs=2, type=float, metavar=("LO", "HI"))
    ap.add_argument("--capital", type=float, default=1000)
    args = ap.parse_args()
    for p in args.pools: check(p, args.range, args.capital)
