# /// script
# dependencies = ["pycryptodome", "requests"]
# ///
"""bStocks LP 风控监控（QQQB + AAPLB）：升级/暂停/黑名单/增发/权限变更/出区间即告警（macOS 通知）。
用法: uv run monitor/bstocks_monitor.py [你的钱包地址 ...]
区间：复制 monitor/positions.example.json 为 monitor/positions.json 后修改 lo/hi（美元价）。"""
import sys, json, time, pathlib, subprocess, requests
from Crypto.Hash import keccak
R = "https://bsc-rpc.publicnode.com"
# 所有 bStocks 代币共用同一套 Beacon / PauseManager / Compliance
B = "0x156d6dce9a4f6139a3406f1f021f1a4880de93a3"
PM, C = "0x9fc74be63f3589485b2423984a7a0557e0cf700a", "0x53dba7aabde774787a1f57236b235567da8e14f4"
# 你的仓位区间放在 monitor/positions.json（已 gitignore，不会提交）；没有时用示例文件
_here = pathlib.Path(__file__).parent
_cfg = _here / "positions.json"
ASSETS = json.loads((_cfg if _cfg.exists() else _here / "positions.example.json").read_text())
WALLETS = [a.lower() for a in sys.argv[1:]]
WATCH = [a["pool"] for a in ASSETS] + WALLETS
NEAR = 0.01  # 距离区间边界 1% 以内提前预警
k = lambda s: keccak.new(digest_bits=256, data=s.encode()).hexdigest()
pad = lambda a: a[2:].lower().zfill(64)
BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"

def rpc(m, p):
    r = requests.post(R, json={"jsonrpc": "2.0", "id": 1, "method": m, "params": p}, timeout=20).json()
    if "error" in r: raise RuntimeError(r["error"])
    return r["result"]
def call(to, sig, args=""):
    return rpc("eth_call", [{"to": to, "data": "0x" + k(sig)[:8] + args}, "latest"])

def alert(msg):
    print(time.strftime("%F %T"), "🚨", msg, flush=True)
    subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "bStocks LP 告警" sound name "Sosumi"'])

def price(pool):  # token0 = 股票, token1 = 稳定币, 均为 18 位
    return (int(call(pool, "slot0()")[2:66], 16) / 2**96) ** 2

def zone(a, p):
    if p < a["lo"]: return "跌破下沿(全部变成股票)"
    if p > a["hi"]: return "涨破上沿(全部变成稳定币)"
    if p < a["lo"] * (1 + NEAR): return "接近下沿"
    if p > a["hi"] * (1 - NEAR): return "接近上沿"
    return "区间内"

def state():
    s = {"beaconImpl": call(B, "implementation()")[-40:]}
    for a in ASSETS:
        n, t = a["name"], a["token"]
        s[f"{n} beacon"] = rpc("eth_getStorageAt", [t, BEACON_SLOT, "latest"])[-40:]
        s[f"{n} paused"] = int(call(PM, "isTokenPaused(address)", pad(t)), 16)
        s[f"{n} admin数"] = int(call(t, "getRoleMemberCount(bytes32)", "0" * 64), 16)
        s[f"{n} issuer数"] = int(call(t, "getRoleMemberCount(bytes32)", k("ISSUER_ROLE")), 16)
        s[f"{n} compliance"] = call(t, "compliance()")[-40:]
        s[f"{n} pauseManager"] = call(t, "pauseManager()")[-40:]
        for w in WATCH:
            s[f"{n} 黑名单 {w[:10]}"] = int(call(C, "blockedAddresses(address,address)", pad(t) + pad(w)), 16)
    for w in WATCH:
        s[f"制裁名单 {w[:10]}"] = int(call(C, "sanctionedAddresses(address)", pad(w)), 16)
    return s

TR, ZERO = "0x" + k("Transfer(address,address,uint256)"), "0x" + "0" * 64
base = state(); print("基线:", base, flush=True)
zones = {}
for a in ASSETS:
    p = price(a["pool"]); zones[a["name"]] = zone(a, p)
    print(f"{a['name']} 价格 ${p:.2f} 区间 ${a['lo']}–${a['hi']} 状态: {zones[a['name']]}", flush=True)
last = int(rpc("eth_blockNumber", []), 16) - 30
beat = time.time()
while True:
    try:
        time.sleep(15)
        cur = state()
        for key, v in cur.items():
            if v != base.get(key): alert(f"{key}: {base.get(key)} -> {v}")
        base = cur
        line = []
        for a in ASSETS:
            p = price(a["pool"]); z = zone(a, p); line.append(f"{a['name']} ${p:.2f} {z}")
            if z != zones[a["name"]]: alert(f"{a['name']} ${p:.2f} {z}"); zones[a["name"]] = z
        if time.time() - beat > 600: print(time.strftime("%F %T"), "♥", " | ".join(line), flush=True); beat = time.time()
        head = int(rpc("eth_blockNumber", []), 16) - 30  # 留余量避免节点不同步
        while last < head:
            to = min(last + 49, head)
            q = lambda addr, topics=None: rpc("eth_getLogs", [{"address": addr, "fromBlock": hex(last + 1), "toBlock": hex(to), **({"topics": topics} if topics else {})}])
            for l in q(B): alert(f"Beacon 事件(可能升级) 区块 {int(l['blockNumber'],16)}")
            for l in q(PM): alert(f"PauseManager 事件 区块 {int(l['blockNumber'],16)}")
            for l in q(C):
                if any(pad(x) in l["data"] + "".join(l["topics"]) for x in WATCH): alert(f"Compliance 涉及池子/你的地址! 区块 {int(l['blockNumber'],16)}")
            for a in ASSETS:
                for l in q(a["token"], [TR, ZERO]):
                    amt = int(l["data"], 16) / 1e18
                    if amt >= a["mint"]: alert(f"大额增发 {amt:.1f} {a['name']} 区块 {int(l['blockNumber'],16)}")
            last = to
    except Exception as e:
        print(time.strftime("%F %T"), "rpc错误", str(e)[:120], flush=True)
