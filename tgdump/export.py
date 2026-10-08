"""Database -> per-topic folders with messages.jsonl and messages.txt."""

import json
import logging

from .config import out_dir
from .scope import NO_SCOPE
from .store import BASE_COLUMNS, JSON_COLUMNS, open_db
from .util import slug

log = logging.getLogger("dump")
to_json = json.JSONEncoder(ensure_ascii=False).encode  # json.dumps without its setup on every call


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


def topic_folder(out, topic, titles):
    """A forum topic's folder; its name follows the topic title."""
    return out / f"{topic}_{slug(titles.get(topic) or ('General' if topic == 1 else ''))}".rstrip("_")


def follow_renames(out, old, new):
    """Rename the folders of renamed topics; False when one cannot be moved, e.g. a file in it is open."""
    for t in new:
        was, now = topic_folder(out, t, old), topic_folder(out, t, new)
        if was == now or not was.exists():
            continue  # a missing one was already renamed; deleted topics keep their folder
        if now.exists():
            return False  # both names exist, so only a rewrite puts the topic back together
        try:
            was.rename(now)
        except OSError as e:
            log.warning(f"Cannot rename {was.name} to {now.name}, rewriting the export: {e}")
            return False
    return True


def appendable_since(db, out, titles):
    """The last exported message id when only newer messages were saved since, so they can be appended.

    New topics just get a new folder, and the folders of renamed ones are renamed to match.
    """
    row = db.execute("SELECT max_id, rows, titles FROM exports WHERE folder=?", (str(out),)).fetchone()
    if not row or not out.is_dir():
        return None  # never exported here
    max_id, rows, old = row
    renamed = follow_renames(out, dict(json.loads(old)), titles)  # also before a rewrite, so no stale folder stays
    if not renamed or db.execute("SELECT COUNT(*) FROM messages WHERE id <= ?", (max_id,)).fetchone()[0] != rows:
        return None  # e.g. older messages were added by a resumed range, so the order needs a rewrite
    return max_id


def export(db, chat_id, chat_title, out_root=None, scope=NO_SCOPE, folder=None, threads=False, full=False):
    """Write <out>/<chat>/<topic id>_<title>/messages.jsonl and messages.txt.

    Later runs append only the new messages, unless older ones changed; full=True rewrites everything.
    With threads=True (a channel's discussion group) everything goes to one folder,
    ordered by thread, and each thread is headed by the channel post it belongs to.
    """
    out = folder or chat_folder(chat_id, chat_title, scope, out_root)
    titles = dict(db.execute("SELECT id, title FROM topics"))
    fingerprint = json.dumps(sorted(titles.items()), ensure_ascii=False)
    cols = [c[1] for c in db.execute("PRAGMA table_info(messages)")]
    since = None if full or threads else appendable_since(db, out, titles)
    if threads:
        groups = [(None, "", (), "COALESCE(reply_top, reply_to, id), id")]
    else:
        topic_ids = [t for (t,) in db.execute("SELECT DISTINCT topic_id FROM messages WHERE id > ?", (since or 0,))]
        groups = [(t, "WHERE topic_id IS ? AND id > ?", (t, since or 0), "id") for t in topic_ids]
    mode = "w" if since is None else "a"
    for t, where, params, order in groups:
        target = out if t is None else topic_folder(out, t, titles)
        target.mkdir(parents=True, exist_ok=True)
        with (
            open(target / "messages.jsonl", mode, encoding="utf-8") as fj,
            open(target / "messages.txt", mode, encoding="utf-8") as ft,
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
                fj.write(to_json(m) + "\n")
                ft.write(text_line(m))
    max_id, rows = db.execute("SELECT MAX(id), COUNT(*) FROM messages").fetchone()
    db.execute("INSERT OR REPLACE INTO exports VALUES(?, ?, ?, ?)", (str(out), max_id or 0, rows, fingerprint))
    db.commit()
    log.info(f"{'Exported' if since is None else 'Appended the new messages'} to {out}")
    return out


def export_db(db_path, chat_id, chat_title, **kw):
    """Export from a database file; safe to run in a worker thread."""
    db = open_db(db_path)
    try:
        return export(db, chat_id, chat_title, **kw)
    finally:
        db.close()
