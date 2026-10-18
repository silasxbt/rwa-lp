"""投研：代币合约审计（代理、权限、危险函数、持币集中度）+ 标的基本面（Yahoo）+ 综合结论。"""
import os, json, time, requests
from .chain import rpc, call, batch, calls, cached, addr, words, sel, k, _fetch

ESCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"    # EIP-1967
ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
BEACON_SLOT = "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50"
UA = {"User-Agent": "Mozilla/5.0"}

# 写函数按名字归类：(关键词, 类别, 说明, 严重度 None=需留意 False=危险)
RULES = [
    (("mint",), "增发", "可以增发代币", None),
    (("burnfrom", "forceburn", "seize", "forcetransfer", "confiscate", "wipe"), "罚没", "可以动用户余额", False),
    (("pause", "unpause", "setpausemanager"), "暂停", "可以暂停转账", None),
    (("blacklist", "blocklist", "block", "freeze", "sanction", "setcompliance"), "拉黑", "可以拉黑地址（含 LP 池子）", None),
    (("upgrade", "setimplementation", "setbeacon"), "升级", "可以替换合约逻辑", None),
    (("fee", "tax"), "交易税", "可以设置交易税", False),
    (("multiplier", "split", "rebase"), "换算倍数", "可以修改每枚代币对应的股数（拆股/合股）", None),
    (("setname", "setsymbol"), "改名", "可以修改代币名称", True),
    (("rescue", "sweep", "withdraw", "recover"), "转走资产", "可以把合约里的资产转走", None),
    (("grantrole", "transferownership", "setowner", "setadmin"), "权限转移", "权限可以增减或转给别人", True),
]
FALLBACK_SIGS = {   # 没有验证源码时，按字节码里的函数选择器判断
    "mint(address,uint256)": "mint", "burn(address,uint256)": "burnfrom", "burnFrom(address,uint256)": "burnfrom",
    "pause()": "pause", "upgradeTo(address)": "upgrade", "upgradeToAndCall(address,bytes)": "upgrade",
    "blacklist(address)": "blacklist", "addBlackList(address)": "blacklist", "setFee(uint256)": "fee",
    "setTaxFee(uint256)": "tax", "transferOwnership(address)": "transferownership", "grantRole(bytes32,address)": "grantrole",
    "rescueTokens(address,uint256)": "rescue",
}

def escan(address, action="getsourcecode"):
    if not ESCAN_KEY: return None
    def f():
        r = requests.get("https://api.etherscan.io/v2/api", params=dict(chainid=56, module="contract", action=action, address=address,
                                                                        apikey=ESCAN_KEY), timeout=20).json()
        return r["result"][0] if r.get("status") == "1" else None
    try: return cached(("escan", action, address), 86400, f)
    except Exception: return None

def _selectors(code):
    b, out, j = bytes.fromhex(code[2:]), set(), 0
    while j < len(b):
        op = b[j]
        if op == 0x63: out.add(b[j + 1:j + 5].hex())
        j += 1 + (op - 0x5f if 0x60 <= op <= 0x7f else 0)
    return out

def _who_many(addrs):
    """批量判断地址类型：EOA / Safe 多签（门槛/人数）/ 其他合约。"""
    addrs = [a for a in dict.fromkeys(addrs) if a]
    codes = batch([("eth_getCode", [a, "latest"]) for a in addrs]) if addrs else []
    out, cons = {}, []
    for a, c in zip(addrs, codes):
        if int(a, 16) == 0: out[a] = {"address": a, "kind": "无", "text": "无（已放弃）"}
        elif c in ("0x", None): out[a] = {"address": a, "kind": "EOA", "text": "单一私钥钱包"}
        else: cons.append(a)
    if cons:
        r = calls([x for a in cons for x in ((a, "getThreshold()", ""), (a, "getOwners()", ""))])
        for n, a in enumerate(cons):
            th, ow = r[2 * n], r[2 * n + 1]
            if th and ow and len(ow) > 130: out[a] = {"address": a, "kind": "多签", "text": f"{int(th, 16)}/{words(ow)[1]} 多签"}
            else: out[a] = {"address": a, "kind": "合约", "text": "合约（非 Safe 多签，需看代码）"}
    return out

def audit(token):
    """代币合约审计：代理结构、升级权限、写函数权限、源码验证、持币集中度。"""
    token = token.lower()
    def f():
        slots = batch([("eth_getStorageAt", [token, s, "latest"]) for s in (IMPL_SLOT, ADMIN_SLOT, BEACON_SLOT)])
        impl, admin, beacon = (addr(x) if x and int(x, 16) else None for x in slots)
        upgrader = None
        if beacon:
            impl = addr(call(beacon, "implementation()"))
            try: upgrader = addr(call(beacon, "owner()"))
            except Exception: pass
        elif admin:
            upgrader = admin
            try: upgrader = addr(call(admin, "owner()"))    # ProxyAdmin 合约 → 它的 owner
            except Exception: pass
        proxy = "Beacon 代理" if beacon else "EIP-1967 代理" if impl else "不可升级"
        logic = impl or token
        from concurrent.futures import ThreadPoolExecutor
        ex = ThreadPoolExecutor(3)
        info = ex.submit(_fetch, f"tokens/{token}/info", {})
        ft, fl = ex.submit(escan, token), ex.submit(escan, logic) if logic != token else None
        src_t, src_l = ft.result(), fl.result() if fl else None
        ex.shutdown(wait=False)
        src = src_l or src_t
        verified = bool(src and src.get("ABI", "").startswith("["))
        fns, cats = [], {}
        if verified:
            for fn in json.loads(src["ABI"]):
                if fn.get("type") != "function" or fn.get("stateMutability") in ("view", "pure"): continue
                name = fn["name"]; ins = [x["type"] for x in fn.get("inputs", [])]
                low = name.lower()
                if low in ("approve", "transfer", "transferfrom", "initialize", "renouncerole", "renounceownership", "permit",
                           "increaseallowance", "decreaseallowance"): continue
                if low == "burn" and ins and ins[0] == "address": low = "burnfrom"     # burn(address,…) 能烧别人的币
                fns.append(f"{name}({','.join(ins)})")
                for keys, cat, text, sev in RULES:
                    if any(x in low for x in keys): cats.setdefault(cat, {"cat": cat, "text": text, "ok": sev, "fns": []})["fns"].append(name); break
        else:
            have = _selectors(rpc("eth_getCode", [logic, "latest"]))
            for sig, key in FALLBACK_SIGS.items():
                if sel(sig)[2:] in have:
                    fns.append(sig)
                    for keys, cat, text, sev in RULES:
                        if key in keys: cats.setdefault(cat, {"cat": cat, "text": text, "ok": sev, "fns": []})["fns"].append(sig.split("(")[0])
        rids = [("Admin", "0" * 64), ("Issuer", k("ISSUER_ROLE"))]
        head = calls([(token, "getRoleMemberCount(bytes32)", rid) for _, rid in rids] + [(token, "owner()", "")])
        reqs = [(role, (token, "getRoleMember(bytes32,uint256)", rid + format(j, "064x")))
                for (role, rid), c in zip(rids, head) if c for j in range(min(int(c, 16), 5))]
        mem = calls([r for _, r in reqs]) if reqs else []
        members = [(role, addr(m)) for (role, _), m in zip(reqs, mem) if m]
        owner_a = addr(head[2]) if head[2] and len(head[2]) >= 66 else None
        who = _who_many([m for _, m in members] + [upgrader, owner_a])
        roles = {}
        for role, m in members: roles.setdefault(role, []).append(who[m])
        owner = who.get(owner_a)
        holders = None
        try:
            h = info.result()["attributes"]
            holders = {"count": (h.get("holders") or {}).get("count"), "dist": (h.get("holders") or {}).get("distribution_percentage"),
                       "gt_score": h.get("gt_score"), "gt_verified": h.get("gt_verified"), "website": (h.get("websites") or [None])[0],
                       "name": h.get("name"), "cg_id": h.get("coingecko_coin_id")}
        except Exception: pass
        supply = int(call(token, "totalSupply()"), 16) / 1e18
        return dict(token=token, proxy=proxy, implementation=impl, upgrader=who.get(upgrader),
                    verified=verified, contract_name=(src or {}).get("ContractName"), compiler=(src or {}).get("CompilerVersion"),
                    source_checked=bool(ESCAN_KEY), functions=fns, powers=list(cats.values()), roles=roles, owner=owner,
                    holders=holders, supply=supply)
    return cached(("audit", token), 1800, f)

def audit_items(a):
    """把审计结果整理成风险条目：ok True 安全 / None 需留意 / False 危险。"""
    out = []
    add = lambda ok, text, cat="合约": out.append({"cat": cat, "ok": ok, "text": text})
    if not a["source_checked"]: add(None, "未配置 BscScan key，只按字节码选择器粗查")
    else: add(a["verified"], f"源码已验证：{a['contract_name']}（{a['compiler']}）" if a["verified"] else "逻辑合约源码未验证，无法确认权限")
    up = a["upgrader"]
    if a["proxy"] == "不可升级": add(True, "合约不可升级")
    else: add(None if up and up["kind"] == "多签" else False if up and up["kind"] == "EOA" else None,
              f"{a['proxy']}，升级权限：{up['text'] if up else '未知'}")
    for p in a["powers"]: add(p["ok"], f"{p['text']}（{', '.join(dict.fromkeys(p['fns']))}）", "权限")
    for role, ms in a["roles"].items():
        if ms: add(None if any(m["kind"] == "EOA" for m in ms) else True, f"{role} {len(ms)} 个：" + "、".join(m["text"] for m in ms), "权限")
    h = a.get("holders")
    if h and h.get("dist"):
        top = float(h["dist"].get("top_10") or 0)
        add(None if top > 50 else True, f"持币 {h['count']:,} 个地址，前 10 名占 {top:.1f}%" + ("（含交易所和发行方托管，集中度高是代币化股票的常态）" if top > 50 else ""), "持币")
    if h and h.get("gt_score") is not None:
        add(h["gt_score"] >= 50, f"GeckoTerminal 评分 {h['gt_score']:.0f}/100" + ("，已认证" if h.get("gt_verified") else ""), "持币")
    return out

# ---------- 基本面（Yahoo） ----------
_ys = {"s": None, "crumb": None, "at": 0}

def _yahoo(path, **params):
    if not _ys["crumb"] or time.time() - _ys["at"] > 6 * 3600:
        s = requests.Session()
        try: s.get("https://fc.yahoo.com", headers=UA, timeout=10)
        except requests.RequestException: pass
        _ys.update(s=s, crumb=s.get("https://query1.finance.yahoo.com/v1/test/getcrumb", headers=UA, timeout=10).text, at=time.time())
    r = _ys["s"].get("https://query2.finance.yahoo.com" + path, params=dict(params, crumb=_ys["crumb"]), headers=UA, timeout=15)
    if r.status_code == 401: _ys["crumb"] = None
    r.raise_for_status()
    return r.json()

raw = lambda d, k: (d.get(k) or {}).get("raw") if isinstance(d.get(k), dict) else d.get(k)

def fundamentals(ticker):
    def f():
        m = "price,summaryDetail,defaultKeyStatistics,financialData,calendarEvents,recommendationTrend,topHoldings,assetProfile"
        r = _yahoo(f"/v10/finance/quoteSummary/{ticker}", modules=m)["quoteSummary"]["result"][0]
        p, sd, ks, fd = r.get("price", {}), r.get("summaryDetail", {}), r.get("defaultKeyStatistics", {}), r.get("financialData", {})
        etf = p.get("quoteType") == "ETF"
        last = raw(p, "regularMarketPrice")
        lo52, hi52 = raw(sd, "fiftyTwoWeekLow"), raw(sd, "fiftyTwoWeekHigh")
        earn = [x.get("raw") for x in ((r.get("calendarEvents") or {}).get("earnings") or {}).get("earningsDate", [])]
        rec = ((r.get("recommendationTrend") or {}).get("trend") or [{}])[0]
        th = r.get("topHoldings") or {}
        out = dict(ticker=ticker, name=p.get("longName") or p.get("shortName"), etf=etf, price=last, currency=p.get("currency"),
                   market_cap=raw(p, "marketCap") or raw(sd, "totalAssets"), pe=raw(sd, "trailingPE"), fpe=raw(sd, "forwardPE") or raw(ks, "forwardPE"),
                   ps=raw(sd, "priceToSalesTrailing12Months"), pb=raw(ks, "priceToBook"), beta=raw(sd, "beta") or raw(ks, "beta3Year"),
                   div_yield=raw(sd, "dividendYield") or raw(sd, "yield"), lo52=lo52, hi52=hi52,
                   pos52=(last - lo52) / (hi52 - lo52) if last and lo52 and hi52 and hi52 > lo52 else None,
                   ma50=raw(sd, "fiftyDayAverage"), ma200=raw(sd, "twoHundredDayAverage"),
                   rev_growth=raw(fd, "revenueGrowth"), eps_growth=raw(fd, "earningsGrowth"), gross_margin=raw(fd, "grossMargins"),
                   op_margin=raw(fd, "operatingMargins"), fcf=raw(fd, "freeCashflow"), debt_equity=raw(fd, "debtToEquity"),
                   target=raw(fd, "targetMeanPrice"), target_hi=raw(fd, "targetHighPrice"), target_lo=raw(fd, "targetLowPrice"),
                   analysts=raw(fd, "numberOfAnalystOpinions"), rec_key=fd.get("recommendationKey"),
                   rec={k: rec.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell")},
                   earnings=[time.strftime("%Y-%m-%d", time.gmtime(t)) for t in earn if t],
                   earnings_estimate=((r.get("calendarEvents") or {}).get("earnings") or {}).get("isEarningsDateEstimate"),
                   expense=raw(ks, "annualReportExpenseRatio") or raw(sd, "expenseRatio"),
                   ytd=raw(ks, "ytdReturn"), holdings=[{"symbol": h.get("symbol"), "name": h.get("holdingName"), "w": raw(h, "holdingPercent")}
                                                       for h in (th.get("holdings") or [])[:8]],
                   sector=(r.get("assetProfile") or {}).get("sector"), industry=(r.get("assetProfile") or {}).get("industry"))
        if out["target"] and last: out["upside"] = out["target"] / last - 1
        return out
    return cached(("fund", ticker), 3600, f)

def earnings_date(ticker):
    """Yahoo 给的下次财报 (日期 YYYY-MM-DD, 是否公司已确认)，取不到返回 None。事件钟用它覆盖按往年推算的日期。失败结果也缓存，避免每分钟重试。"""
    def f():
        try:
            fd = fundamentals(ticker)
            if fd["etf"] or not fd["earnings"]: return None
            return fd["earnings"][0], not fd.get("earnings_estimate")
        except Exception:
            return None
    return cached(("earn", ticker), 6 * 3600, f)

def fund_items(f):
    out = []
    add = lambda ok, text: out.append({"cat": "基本面", "ok": ok, "text": text})
    if f.get("pos52") is not None:
        add(None if f["pos52"] > 0.9 or f["pos52"] < 0.1 else True,
            f"52 周区间 ${f['lo52']:,.2f} – ${f['hi52']:,.2f}，现价在 {f['pos52']*100:.0f}% 位置" + ("，接近高点，回撤风险大" if f["pos52"] > 0.9 else "，接近低点" if f["pos52"] < 0.1 else ""))
    if f.get("ma50") and f.get("ma200") and f.get("price"):
        up = f["price"] > f["ma50"] > f["ma200"]; dn = f["price"] < f["ma50"] < f["ma200"]
        add(None if dn else True, f"趋势：现价 {'>' if f['price'] > f['ma50'] else '<'} 50 日均线 ${f['ma50']:,.2f}，50 日 {'>' if f['ma50'] > f['ma200'] else '<'} 200 日 ${f['ma200']:,.2f}"
            + ("（多头排列）" if up else "（空头排列，单边下跌对 LP 最伤）" if dn else ""))
    if f.get("pe"): add(None if f["pe"] > 45 else True, f"市盈率 {f['pe']:.1f}" + (f"，预期 {f['fpe']:.1f}" if f.get("fpe") else "") + ("，估值偏高" if f["pe"] > 45 else ""))
    if f.get("rev_growth") is not None: add(f["rev_growth"] > 0, f"营收同比 {f['rev_growth']*100:+.1f}%" + (f"，盈利同比 {f['eps_growth']*100:+.1f}%" if f.get("eps_growth") is not None else ""))
    if f.get("upside") is not None: add(None if f["upside"] < 0 else True, f"{f['analysts'] or 0} 位分析师目标价均值 ${f['target']:,.2f}（{f['upside']*100:+.1f}%），区间 ${f['target_lo']:,.0f} – ${f['target_hi']:,.0f}")
    if f.get("beta"): add(None if f["beta"] > 1.5 else True, f"Beta {f['beta']:.2f}" + ("，波动大，区间要放宽" if f["beta"] > 1.5 else ""))
    if f.get("earnings"): add(None, f"下次财报 {f['earnings'][0]}（Yahoo）")
    if f.get("etf") and f.get("expense"): add(True, f"ETF 费率 {f['expense']*100:.2f}%")
    return out

def research(pool):
    """综合投研：标的基本面 + 代币合约审计 + 发行方治理 + 链上代币化规模，给出打分和结论。"""
    from .analytics import pool_info
    from .position import ticker_of, fair_price
    from .monitor import token_governance
    from concurrent.futures import ThreadPoolExecutor
    i = pool_info(pool)
    tk = ticker_of(i)
    with ThreadPoolExecutor(4) as ex:
        fa = ex.submit(audit, i["stock"])
        ff = ex.submit(lambda: fundamentals(tk) if tk else None)
        fr = ex.submit(fair_price, i)
        fg = ex.submit(lambda: token_governance(i["stock"], i["pool"]) if i["bstocks"] else [])
        a, fair, gov = fa.result(), fr.result(), fg.result()
        try: f = ff.result()
        except Exception: f = None
    # 审计里已经列了 Admin 和升级权限，治理项里同样的两条去掉
    gov = [g for g in gov if not g["text"].startswith(("Admin", "升级权限"))]
    items = (fund_items(f) if f else []) + audit_items(a) + gov
    chain = None
    if fair:
        aum = a["supply"] * fair["fair_usd"]
        chain = dict(supply=a["supply"], aum=aum, pool_tvl=i["tvl"], pool_share=i["tvl"] / aum if aum else None,
                     premium=i["price"] / fair["fair"] - 1, multiplier=fair["multiplier"], fair_usd=fair["fair_usd"])
        items.append({"cat": "链上", "ok": True if chain["pool_share"] and chain["pool_share"] < 0.2 else None,
                      "text": f"代币化规模 ${aum:,.0f}（{a['supply']:,.0f} 枚），本池 TVL 占 {chain['pool_share']*100:.1f}%"})
        pr = chain["premium"]
        items.append({"cat": "链上", "ok": abs(pr) < 0.01 or None, "text": f"链上价相对真实股价{'溢价' if pr >= 0 else '折价'} {abs(pr)*100:.2f}%（{fair['state']}）"})
    bad = sum(x["ok"] is False for x in items); warn = sum(x["ok"] is None for x in items)
    score = max(0, 100 - 20 * bad - 3 * warn)
    verdict = "可以做" if bad == 0 else "小仓位试，按发行方风险设仓位上限" if bad == 1 else "不建议"
    return dict(pool=i["pool"], name=i["name"], ticker=tk, stock=i["stock"], bstocks=i["bstocks"], fundamentals=f, audit=a,
                chain=chain, items=items, score=score, verdict=verdict, bad=bad, warn=warn)
