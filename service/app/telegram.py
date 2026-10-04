"""Telegram 机器人：发消息 + 处理命令。未配置 TG_TOKEN 时静默跳过。"""
import os, re, requests
from . import store

TOKEN = os.environ.get("TG_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}"
POOL_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
MAX_WATCHES = 10

HELP = """rwalp 机器人：bStocks 代币化美股 LP 风控

/watch <池子地址> <下沿> <上沿>  订阅区间提醒（美元价）
/list  查看我的订阅
/unwatch <编号>  取消订阅
/check <池子地址> [下沿 上沿]  池子体检 + 手续费估算
/stop  停止接收所有提醒

订阅后会收到：出区间/近区间提醒、区间评级变化（安全/警惕/危险）、bStocks 治理告警（升级、暂停、黑名单、大额增发）。
网页版：https://rwalp.silasxbt.com"""

def send(chat_id, text):
    if not TOKEN: return
    try:
        requests.post(f"{API}/sendMessage", json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=10)
    except requests.RequestException:
        pass

def handle(update):
    from .analytics import pool_info, estimate  # 避免冷启动时加载
    from .monitor import zone
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg: return
    chat, args = msg["chat"]["id"], msg["text"].split()
    cmd = args[0].split("@")[0].lower()

    if cmd in ("/start", "/help"):
        store.add_chat(chat); send(chat, HELP)
    elif cmd == "/stop":
        for w in store.watches(chat): store.remove_watch(chat, w["id"])
        store.remove_chat(chat); send(chat, "已取消全部订阅。发送 /start 重新开启。")
    elif cmd == "/list":
        ws = store.watches(chat)
        send(chat, "\n".join(f"{w['id'][:6]}  {w['name']}  ${w['lo']:,.2f} – ${w['hi']:,.2f}  {w.get('zone', '')}" for w in ws)
             or "还没有订阅。用 /watch <池子地址> <下沿> <上沿> 添加。")
    elif cmd == "/unwatch" and len(args) == 2:
        hit = [w for w in store.watches(chat) if w["id"].startswith(args[1])]
        ok = len(hit) == 1 and store.remove_watch(chat, hit[0]["id"])
        send(chat, "已取消。" if ok else "没找到这个编号，先用 /list 查看。")
    elif cmd == "/watch" and len(args) == 4 and POOL_RE.match(args[1]):
        if len(store.watches(chat)) >= MAX_WATCHES: return send(chat, f"最多订阅 {MAX_WATCHES} 个。")
        try:
            lo, hi = sorted(map(float, args[2:4]))
            i = pool_info(args[1])
        except Exception as e:
            return send(chat, f"添加失败：{e}")
        store.add_chat(chat)
        z = zone(lo, hi, i["price"])
        wid = store.add_watch(chat, i["pool"], i["name"], lo, hi, z)
        warn = "" if i["dex"] and i["canonical"] else "\n⚠️ 这不是官方 factory 创建的池子，注意仿盘风险。"
        send(chat, f"已订阅 {wid[:6]}：{i['name']}\n当前 ${i['price']:,.2f}，区间 ${lo:,.2f} – ${hi:,.2f}（{z}）{warn}")
    elif cmd == "/check" and len(args) in (2, 4) and POOL_RE.match(args[1]):
        try:
            i = pool_info(args[1])
            lo, hi = sorted(map(float, args[2:4])) if len(args) == 4 else (i["price"] * 0.96, i["price"] * 1.06)
            e = estimate(i, lo, hi, 1000)
        except Exception as ex:
            return send(chat, f"查询失败：{ex}")
        flags = "\n".join(("✅ " if f["ok"] else "❌ " if f["ok"] is False else "ℹ️ ") + f["text"] for f in i["flags"])
        send(chat, f"{i['name']}  ${i['price']:,.2f}\n{flags}\n\n$1000 @ ${lo:,.2f} – ${hi:,.2f}\n"
                   f"份额 {e['share']*100:.3f}%  手续费/天 ${e['fee_day7']:.2f}（7日）/ ${e['fee_day30']:.2f}（30日）\n"
                   f"年化 {e['apr7']*100:.0f}% / {e['apr30']*100:.0f}%（未扣无常损失）")
    else:
        send(chat, HELP)
