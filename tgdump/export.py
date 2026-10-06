"""Database -> per-topic folders with messages.jsonl and messages.txt."""

import json
import logging

from .config import out_dir
from .scope import NO_SCOPE
from .store import BASE_COLUMNS, JSON_COLUMNS, open_db
from .util import slug

log = logging.getLogger("dump")


def text_line(m):
    """One line of the human-readable messages.txt."""
    head = f"[{m['date'][:19].replace('T', ' ')}] #{m['id']} {m['sender'] or m['sender_id'] or '?'}"
    if m["reply_to"] and m["reply_to"] not in (m["topic_id"], m.get("thread")):
        head += f" (reply to #{m['reply_to']})"
    if m["fwd_from"]:
        head += f" (forwarded from {m['fwd_from']})"
    body = m.get("text_md") or m["text"] or ""
    extra = m.get("extra") or {}
    if poll := extra.get("poll"):
        answers = " | ".join(f"{a['text']} ({a['voters'] or 0})" for a in poll["answers"])
        body = f"<poll> {poll['question']} | {answers} | {poll['total_voters'] or 0} votes"
    elif todo := extra.get("todo"):
        items = " | ".join(("[x] " if i["done"] else "[ ] ") + i["text"] for i in todo["items"])
        body = f"<checklist> {todo['title']} | {items}"
    elif m["media"]:
        body = f"<{m['media']}> {body}".rstrip()
    if m["action"]:
        body = f"<{m['action']}> {body}".rstrip()
    meta = []
    if m.get("reactions"):
        meta.append(" ".join(f"{k} {v}" for k, v in m["reactions"].items()))
    if m.get("views"):
        meta.append(f"{m['views']:,} views")
    if m.get("replies"):
        meta.append(f"{m['replies']} replies")
    return f"{head}: {body}" + (f"  [{' · '.join(meta)}]" if meta else "") + "\n"


def message_dict(cols, row):
    """A database row as an export record: JSON columns decoded, empty optional fields left out."""
    m = dict(zip(cols, row, strict=True))
    for c in JSON_COLUMNS:
        if m.get(c):
            m[c] = json.loads(m[c])
    return {k: v for k, v in m.items() if v is not None or k in BASE_COLUMNS}


def chat_folder(chat_id, chat_title, scope=NO_SCOPE, out_root=None):
    name = f"{chat_id}_{slug(chat_title)}".rstrip("_")
    if scope:
        name += "_" + scope.tag()
    return (out_root or out_dir()) / name


def export(db, chat_id, chat_title, out_root=None, scope=NO_SCOPE, folder=None, threads=False):
    """Write <out>/<chat>/<topic id>_<title>/messages.jsonl and messages.txt.

    With threads=True (a channel's discussion group) everything goes to one folder,
    ordered by thread, and each thread is headed by the channel post it belongs to.
    """
    out = folder or chat_folder(chat_id, chat_title, scope, out_root)
    titles = dict(db.execute("SELECT id, title FROM topics"))
    cols = [c[1] for c in db.execute("PRAGMA table_info(messages)")]
    if threads:
        groups = [(None, "", (), "COALESCE(reply_top, reply_to, id), id")]
    else:
        topic_ids = [t for (t,) in db.execute("SELECT DISTINCT topic_id FROM messages")]
        groups = [(t, "WHERE topic_id IS ?", (t,), "id") for t in topic_ids]
    for t, where, params, order in groups:
        target = out if t is None else out / f"{t}_{slug(titles.get(t) or ('General' if t == 1 else ''))}".rstrip("_")
        target.mkdir(parents=True, exist_ok=True)
        with (
            open(target / "messages.jsonl", "w", encoding="utf-8") as fj,
            open(target / "messages.txt", "w", encoding="utf-8") as ft,
        ):
            current = None
            for r in db.execute(f"SELECT * FROM messages {where} ORDER BY {order}", params):
                m = message_dict(cols, r)
                if threads:
                    m["thread"] = m["reply_top"] or m["reply_to"] or m["id"]
                    if m["thread"] != current:
                        current = m["thread"]
                        post = (m.get("extra") or {}).get("channel_post") if m["thread"] == m["id"] else None
                        ft.write(f"\n=== {f'post #{post}' if post else f'thread #{current}'} ===\n")
                fj.write(json.dumps(m, ensure_ascii=False) + "\n")
                ft.write(text_line(m))
    log.info(f"Exported to {out}")
    return out


def export_db(db_path, chat_id, chat_title, **kw):
    """Export from a database file; safe to run in a worker thread."""
    db = open_db(db_path)
    try:
        return export(db, chat_id, chat_title, **kw)
    finally:
        db.close()
