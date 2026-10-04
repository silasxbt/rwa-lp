"""Firestore 存储：订阅的聊天、区间订阅、治理基线、扫块游标。"""
from google.cloud import firestore

_db = None
def db():
    global _db
    if _db is None: _db = firestore.Client()
    return _db

def add_chat(chat_id):
    db().collection("chats").document(str(chat_id)).set({"chat_id": chat_id}, merge=True)

def remove_chat(chat_id):
    db().collection("chats").document(str(chat_id)).delete()

def chats():
    return [d.to_dict()["chat_id"] for d in db().collection("chats").stream()]

def add_watch(chat_id, pool, name, lo, hi, zone):
    ref = db().collection("watches").document()
    ref.set({"chat_id": chat_id, "pool": pool, "name": name, "lo": lo, "hi": hi, "zone": zone,
             "created": firestore.SERVER_TIMESTAMP})
    return ref.id

def watches(chat_id=None):
    q = db().collection("watches")
    if chat_id is not None: q = q.where("chat_id", "==", chat_id)
    return [dict(d.to_dict(), id=d.id) for d in q.stream()]

def update_watch(wid, **fields):
    db().collection("watches").document(wid).update(fields)

def remove_watch(chat_id, wid):
    ref = db().collection("watches").document(wid)
    d = ref.get()
    if d.exists and d.to_dict()["chat_id"] == chat_id:
        ref.delete(); return True
    return False

def get_state(name):
    d = db().collection("state").document(name).get()
    return d.to_dict() if d.exists else None

def set_state(name, value):
    db().collection("state").document(name).set(value)
