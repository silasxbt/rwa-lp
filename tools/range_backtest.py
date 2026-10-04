# /// script
# dependencies = ["pycryptodome", "requests"]
# ///
"""V3 区间回测：用 GeckoTerminal 小时 K 线，滚动模拟 N 天持仓的手续费、无常损失、在区间时间。
用法: uv run tools/range_backtest.py <池子地址> [--ranges=-4:6,-3.6:4.8,-5:7] [--capital 1000] [--days 7]
区间为相对开仓价的百分比。竞争流动性取池子当前价位的活跃流动性（偏保守）。"""
import argparse, math, time, statistics as st, requests
from Crypto.Hash import keccak

R = "https://bsc-rpc.publicnode.com"
GT = "https://api.geckoterminal.com/api/v2/networks/bsc/pools/"
k = lambda s: keccak.new(digest_bits=256, data=s.encode()).hexdigest()

def call(to, sig):
    return requests.post(R, json={"jsonrpc": "2.0", "id": 1, "method": "eth_call",
                                  "params": [{"to": to, "data": "0x" + k(sig)[:8]}, "latest"]}, timeout=20).json()["result"]

def gt(url, **kw):
    for _ in range(8):
        r = requests.get(url, timeout=20, **kw).json()
        if "data" in r: return r["data"]
        time.sleep(15)
    raise RuntimeError("GeckoTerminal 请求失败")

def lp_fee(pool):
    fee = int(call(pool, "fee()"), 16) / 1e6
    fp = int(call(pool, "slot0()")[2 + 64 * 5:2 + 64 * 6], 16)
    if call(pool, "factory()")[-40:] == "0bfbcf9fa4f9c56b0f40a671ad40e0805a091865":  # Pancake
        cut = ((fp & 0xFFFF) + (fp >> 16)) / 2 / 1e4
    else:
        cut = sum(1 / n for n in (fp % 16, fp >> 4) if n) / 2
    return fee * (1 - cut)

def amounts(L, p, a, b):  # 股票数量, 美元数量
    sp, sa, sb = math.sqrt(min(max(p, a), b)), math.sqrt(a), math.sqrt(b)
    return L * (1 / sp - 1 / sb), L * (sp - sa)

def backtest(o, lc, fee, lo, hi, capital, W):
    res = []
    for i in range(0, len(o) - W, 24):
        p0 = o[i][4]; a, b = p0 * (1 + lo / 100), p0 * (1 + hi / 100)
        L = capital / (2 * math.sqrt(p0) - math.sqrt(a) - p0 / math.sqrt(b))
        x0, y0 = amounts(L, p0, a, b)
        fees = sum(h[5] * fee * L / (lc + L) for h in o[i + 1:i + 1 + W] if a <= h[4] <= b)
        inr = sum(a <= h[4] <= b for h in o[i + 1:i + 1 + W]) / W
        p1 = o[i + W][4]; x1, y1 = amounts(L, p1, a, b)
        res.append((fees, x1 * p1 + y1 - (x0 * p1 + y0), inr))
    return res

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("pool")
    ap.add_argument("--ranges", default="-2:2,-3:3,-3.6:4.8,-4:6,-5:7,-7:7")
    ap.add_argument("--capital", type=float, default=1000)
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    pool = args.pool.lower()
    fee = lp_fee(pool)
    lc = int(call(pool, "liquidity()"), 16) / 1e18  # 双 18 位小数时的人类单位
    o = sorted(gt(GT + pool + "/ohlcv/hour", params={"limit": 1000, "token": "base"})["attributes"]["ohlcv_list"])
    W = args.days * 24
    print(f"样本 {len(o)} 小时，持仓窗口 {args.days} 天，LP 实得费率 {fee*100:.4f}%，本金 ${args.capital:,.0f}")
    print(f"{'区间':>12} {'手续费':>8} {'无常损失':>8} {'净收益':>8} {'在区间':>6} {'最差窗口':>8}")
    for r in args.ranges.split(","):
        lo, hi = map(float, r.split(":"))
        res = backtest(o, lc, fee, lo, hi, args.capital, W)
        f, il, inr = (st.mean(x[j] for x in res) for j in range(3))
        worst = min(x[0] + x[1] for x in res)
        print(f"{lo:+5.1f}/{hi:+4.1f}% {f:8.2f} {il:8.2f} {f+il:8.2f} {inr*100:5.0f}% {worst:8.2f}")
