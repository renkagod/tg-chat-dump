"""Database -> per-topic folders with messages.jsonl and messages.txt."""

import json
import logging

from .config import out_dir
from .reader import write_reader
from .scope import NO_SCOPE
from .store import BASE_COLUMNS, JSON_COLUMNS, open_db
from .util import slug

log = logging.getLogger("dump")
to_json = json.JSONEncoder(ensure_ascii=False).encode  # json.dumps without its setup on every call


def size_text(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fields_text(d, skip=("_",)):
    """A details dict as `key=value` pairs, nested ones flattened; lists of plain values joined by commas."""
    out = []
    for k, v in d.items():
        if k in skip:
            continue
        if isinstance(v, dict):
            v = v.get("_", "") + (f"({fields_text(v)})" if len(v) > ("_" in v) else "")
        elif isinstance(v, list):
            v = ", ".join(fields_text(x) if isinstance(x, dict) else str(x) for x in v)
        out.append(f"{k}={v}")
    return ", ".join(out)


def media_text(extra):
    """What the media shows, after its type: a file's name and size, a link preview's title, a place..."""
    if f := extra.get("file"):
        parts = [f.get("name") or f.get("kind") or f.get("mime")]
        if f.get("emoji"):
            parts.append(f["emoji"])
        if f.get("performer") or f.get("title"):
            parts.append(" – ".join(x for x in (f.get("performer"), f.get("title")) if x))
        if f.get("duration"):
            parts.append(f"{f['duration']:.0f} s")
        if f.get("size"):
            parts.append(size_text(f["size"]))
        return ", ".join(str(p) for p in parts if p)
    if w := extra.get("webpage"):
        return " · ".join(w[k] for k in ("site", "title", "description", "url") if w.get(k))
    if g := extra.get("geo"):
        return f"{g.get('lat')},{g.get('long')}" + (" (live)" if g.get("live") else "")
    if v := extra.get("venue"):
        return " · ".join(str(x) for x in (v.get("title"), v.get("address"), f"{v.get('lat')},{v.get('long')}") if x)
    if c := extra.get("contact"):
        name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x)
        return ", ".join(x for x in (name, c.get("phone")) if x)
    if d := extra.get("dice"):
        return f"{d['emoticon']} {d['value']}"
    if g := extra.get("game"):
        return g.get("title", "")
    if i := extra.get("invoice"):
        return f"{i.get('title', '')}, {i.get('amount')} {i.get('currency', '')}".strip(", ")
    if s := extra.get("story"):
        return f"story {s.get('id')} of {s.get('peer')}" + (f": {s['caption']}" if s.get("caption") else "")
    if p := extra.get("paid_media"):
        return f"{p.get('stars')} stars, {', '.join(p.get('items', []))}"
    if p := extra.get("photo"):
        return fields_text(p)
    if i := extra.get("media_info"):
        return fields_text(i)
    return ""


def text_line(m):
    """One entry of the human-readable messages.txt: a line, or several when the text has them."""
    extra = m.get("extra") or {}
    who = str(m["sender"] or m["sender_id"] or "?")
    if extra.get("author"):
        who += f" (signed {extra['author']})"
    if extra.get("rank"):
        who += f" [{extra['rank']}]"
    if extra.get("via_bot"):
        who += f" via {extra['via_bot']}"
    head = f"[{m['date'][:19].replace('T', ' ')}] #{m['id']} {who}"
    reply = extra.get("reply") or {}
    if story := reply.get("story"):
        head += f" (reply to story {story['id']} of {story['peer']})"
    elif m["reply_to"] and (reply.get("peer") or m["reply_to"] not in (m["topic_id"], m.get("thread"))):
        where = f" in {reply.get('chat') or reply['peer']}" if reply.get("peer") else ""
        source = f" from {reply['from']}" if reply.get("from") else ""
        head += f" (reply to #{m['reply_to']}{where}{source})"
    if reply.get("quote"):
        head += f" (quoting «{reply['quote']}»)"
    if m["fwd_from"]:
        head += f" (forwarded from {m['fwd_from']})"
    body = m.get("text_md") or m["text"] or ""
    if poll := extra.get("poll"):
        answers = " | ".join(f"{a['text']} ({a['voters'] or 0})" for a in poll["answers"])
        body = f"<poll> {poll['question']} | {answers} | {poll['total_voters'] or 0} votes"
    elif todo := extra.get("todo"):
        items = " | ".join(("[x] " if i["done"] else "[ ] ") + i["text"] for i in todo["items"])
        body = f"<checklist> {todo['title']} | {items}"
    elif m["media"]:
        about = media_text(extra)
        body = f"<{m['media']}{': ' + about if about else ''}> {body}".rstrip()
    if m["action"]:
        about = fields_text(extra.get("action") or {})
        body = f"<{m['action']}{': ' + about if about else ''}> {body}".rstrip()
    meta = []
    if m.get("reactions"):
        meta.append(" ".join(f"{k} {v}" for k, v in m["reactions"].items()))
    if m.get("views"):
        meta.append(f"{m['views']:,} views")
    if m.get("replies"):
        meta.append(f"{m['replies']} replies")
    if extra.get("paid_stars"):
        meta.append(f"paid {extra['paid_stars']} stars")
    under = []  # indented lines under the message
    if rows := extra.get("buttons"):
        buttons = (b["text"] + (f" <{b['url']}>" if "url" in b else "") for r in rows for b in r)
        under.append("buttons: " + " | ".join(buttons))
    if (links := extra.get("links")) and not m.get("text_md"):
        under.append("links: " + " | ".join(f"{x['text']} <{x['url']}>" for x in links))
    if fc := extra.get("factcheck"):
        under.append(f"fact check{' ' + fc['country'] if fc.get('country') else ''}: {fc.get('text', '')}")
    for r in extra.get("restricted", []):
        under.append(f"restricted on {r.get('platform')}: {r.get('text') or r.get('reason')}")
    line = f"{head}: {body}" + (f"  [{' · '.join(meta)}]" if meta else "")
    return line + "".join(f"\n  [{u}]" for u in under) + "\n"


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
    # counting the rows up to max_id would read the whole table, seconds on a cold disk cache;
    # all rows are counted from the small topic index instead, and only the new ones from the table
    total = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    newer = db.execute("SELECT COUNT(*) FROM messages WHERE id > ?", (max_id,)).fetchone()[0]
    if not renamed or total - newer != rows:
        return None  # e.g. older messages were added by a resumed range, so the order needs a rewrite
    return max_id


def export(
    db, chat_id, chat_title, out_root=None, scope=NO_SCOPE, folder=None, threads=False, full=False, reader=False
):
    """Write <out>/<chat>/<topic id>_<title>/messages.jsonl and messages.txt.

    Later runs append only the new messages, unless older ones changed; full=True rewrites everything.
    reader=True also writes index.html, the offline reader.
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
    if reader:
        changed = None if since is None or threads else set(topic_ids)
        write_reader(db, out, chat_title, titles, topics=changed, threads=threads, since=since)
    return out


def export_db(db_path, chat_id, chat_title, **kw):
    """Export from a database file; safe to run in a worker thread."""
    db = open_db(db_path)
    try:
        return export(db, chat_id, chat_title, **kw)
    finally:
        db.close()
