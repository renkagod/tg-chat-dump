"""SQLite storage: the message table, Telegram message -> row, and download planning."""

import json
import sqlite3

from telethon import types, utils
from telethon.extensions import markdown
from telethon.tl.types import MessageActionTopicCreate, MessageReplyHeader

from .details import forward_info, message_details
from .rich import rich_text
from .util import iso

CHUNK = 5000  # message ids per task in full-chat mode
MIN_CHUNK = 500  # smaller tasks are not worth a worker
BATCH_SIZE = 100  # how often progress is committed
# What to_row keeps of a message, stored as the database's user_version. 2: rich messages, media details,
# buttons, reply quotes, forward sources, action fields. Older rows are filled in by --refetch.
FORMAT = 2

COLUMNS = [
    ("id", "INTEGER PRIMARY KEY"),
    ("date", "TEXT"),
    ("edit_date", "TEXT"),
    ("topic_id", "INTEGER"),
    ("sender_id", "INTEGER"),
    ("sender", "TEXT"),
    ("text", "TEXT"),
    ("reply_to", "INTEGER"),
    ("reply_top", "INTEGER"),
    ("fwd_from", "TEXT"),
    ("media", "TEXT"),
    ("action", "TEXT"),
    ("grouped_id", "INTEGER"),
    # optional extras, NULL unless their option is on
    ("text_md", "TEXT"),
    ("views", "INTEGER"),
    ("forwards", "INTEGER"),
    ("replies", "INTEGER"),
    ("reactions", "TEXT"),  # JSON {emoji: count}
    ("extra", "TEXT"),  # JSON: media details, buttons, reply quote, forward source, action fields (details.py)
]
COLUMN_NAMES = [name for name, _ in COLUMNS]
BASE_COLUMNS = set(COLUMN_NAMES[:13])  # always present in exports, even when empty
JSON_COLUMNS = {"reactions", "extra"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS messages({", ".join(f"{n} {t}" for n, t in COLUMNS)});
CREATE TABLE IF NOT EXISTS topics(id INTEGER PRIMARY KEY, title TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  key TEXT PRIMARY KEY, topic INTEGER, lo INTEGER, hi INTEGER,
  cursor INTEGER, done INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS exports(folder TEXT PRIMARY KEY, max_id INTEGER, rows INTEGER, titles TEXT);
"""


def open_db(path):
    db = sqlite3.connect(path)
    # a commit every BATCH_SIZE messages: with WAL it is a cheap append instead of a full journal sync
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.executescript(SCHEMA)
    have = {c[1] for c in db.execute("PRAGMA table_info(messages)")}
    for name, typ in COLUMNS:  # databases from older versions lack the extra columns
        if name not in have:
            db.execute(f"ALTER TABLE messages ADD COLUMN {name} {typ}")
    db.execute("CREATE INDEX IF NOT EXISTS messages_topic ON messages(topic_id, id)")
    if not db.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
        db.execute(f"PRAGMA user_version={FORMAT}")  # nothing saved yet, so nothing is missing
    db.commit()
    return db


def reactions_of(m):
    rs = getattr(m, "reactions", None)
    if not rs or not rs.results:
        return None
    out = {}
    for rc in rs.results:
        r = rc.reaction
        if isinstance(r, types.ReactionEmoji):
            key = r.emoticon
        elif isinstance(r, types.ReactionCustomEmoji):
            key = f"custom:{r.document_id}"
        else:
            key = type(r).__name__.removeprefix("Reaction").lower()  # e.g. "paid"
        out[key] = rc.count
    return out


def to_row(m, forum, options=frozenset()):
    r = m.reply_to if isinstance(m.reply_to, MessageReplyHeader) else None
    action = getattr(m, "action", None)
    if not forum:
        topic = None
    elif isinstance(action, MessageActionTopicCreate):
        topic = m.id
    elif r and r.forum_topic:
        topic = r.reply_to_top_id or r.reply_to_msg_id
    else:
        topic = 1  # General

    fwd_header = getattr(m, "fwd_from", None)
    fwd = forward_info(m)[0] if fwd_header else None  # the name the app shows; the source's id is in extra
    sender = utils.get_display_name(m.sender) if m.sender else getattr(m, "post_author", None)
    media = getattr(m, "media", None)
    text = getattr(m, "message", None)
    rich = getattr(m, "rich_message", None)

    text_md = views = forwards = replies = reactions = None
    extra = message_details(m)
    if fwd_header and fwd_header.channel_post:
        extra["channel_post"] = fwd_header.channel_post  # links a discussion-group thread to its channel post
    if rich is not None and not text:
        # a rich message keeps its content in blocks, its plain text field is empty
        text = rich_text(rich)
        extra["rich"] = True
        if "markdown" in options:
            text_md = rich_text(rich, md=True)
    elif "markdown" in options and text and getattr(m, "entities", None):
        text_md = markdown.unparse(text, m.entities)
    if "meta" in options:
        views, forwards = getattr(m, "views", None), getattr(m, "forwards", None)
        rp = getattr(m, "replies", None)
        replies = rp.replies if rp else None
        reactions = reactions_of(m)

    return (
        m.id,
        iso(m.date),
        iso(getattr(m, "edit_date", None)),
        topic,
        m.sender_id,
        sender,
        text,
        r.reply_to_msg_id if r else None,
        r.reply_to_top_id if r else None,
        fwd,
        type(media).__name__ if media else None,
        type(action).__name__ if action else None,
        getattr(m, "grouped_id", None),
        text_md,
        views,
        forwards,
        replies,
        json.dumps(reactions, ensure_ascii=False) if reactions else None,
        json.dumps(extra, ensure_ascii=False) if extra else None,
    )


def _on_conflict(c):
    """How a message saved again (a resumed range, --refetch) updates its row without losing what was there.

    Counts and Markdown come only with their options, so a run without them keeps the earlier values;
    Markdown only while the text stays the same.
    """
    if c in ("views", "forwards", "replies", "reactions", "extra"):
        return f"{c}=COALESCE(excluded.{c}, {c})"
    if c == "text_md":
        return "text_md=IIF(excluded.text_md IS NULL AND excluded.text IS text, text_md, excluded.text_md)"
    return f"{c}=excluded.{c}"


UPSERT = (
    f"INSERT INTO messages({','.join(COLUMN_NAMES)}) VALUES({','.join('?' * len(COLUMNS))}) "
    f"ON CONFLICT(id) DO UPDATE SET {', '.join(_on_conflict(c) for c in COLUMN_NAMES[1:])}"
)


def save(db, key, rows, done=False):
    if rows:
        db.executemany(UPSERT, rows)
        db.execute("UPDATE tasks SET cursor=? WHERE key=?", (min(r[0] for r in rows), key))
    if done:
        db.execute("UPDATE tasks SET done=1 WHERE key=?", (key,))
    db.commit()


def outdated(db):
    """Whether the saved messages were written by a version that kept less of them, see FORMAT."""
    return db.execute("PRAGMA user_version").fetchone()[0] < FORMAT


def refetch(db, topics=None):
    """Download the saved messages again, to fill in what an older version did not keep.

    The download ranges (or the given topics) are reset; existing rows are updated in place, and messages
    deleted from Telegram since stay. The next export is written from scratch. An interrupted refetch goes on
    with the next run, like any unfinished download.
    """
    if topics:
        db.executemany("UPDATE tasks SET lo=0, cursor=0, done=0 WHERE key=?", [(f"topic:{t}",) for t in topics])
    else:
        db.execute("UPDATE tasks SET cursor=hi + 1, done=0 WHERE topic IS NULL")
        db.execute(f"PRAGMA user_version={FORMAT}")
    db.execute("DELETE FROM exports")
    db.commit()


def clear(db):
    """Forget everything downloaded, so the chat is fetched again from scratch."""
    db.executescript("DELETE FROM messages; DELETE FROM tasks; DELETE FROM topics; DELETE FROM exports;")
    db.execute(f"PRAGMA user_version={FORMAT}")


def plan_full(db, first_id, top_id, parts=1):
    """Split [first_id, top_id] into ranges; existing ranges are kept, only new ones are added on top.

    A short new span is cut into about `parts` ranges, so every worker gets one.
    """
    # Nothing exists below the first visible message, so those ranges are skipped.
    db.execute("UPDATE tasks SET done=1 WHERE topic IS NULL AND hi < ?", (first_id,))
    start = db.execute("SELECT COALESCE(MAX(hi), 0) FROM tasks WHERE topic IS NULL").fetchone()[0]
    start = max(start, first_id - 1)
    chunk = min(CHUNK, max(MIN_CHUNK, -(-(top_id - start) // parts)))
    for lo in range(start, top_id, chunk):
        hi = min(lo + chunk, top_id)
        db.execute(
            "INSERT INTO tasks(key, topic, lo, hi, cursor) VALUES(?, NULL, ?, ?, ?)", (f"range:{lo}", lo, hi, hi + 1)
        )
    db.commit()


def plan_topics(db, topics):
    for t in topics:
        newest = db.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE topic_id=?", (t,)).fetchone()[0]
        key = f"topic:{t}"
        row = db.execute("SELECT done FROM tasks WHERE key=?", (key,)).fetchone()
        if row is None:
            db.execute("INSERT INTO tasks(key, topic, lo, cursor) VALUES(?, ?, ?, 0)", (key, t, newest))
        elif row[0]:  # previous pass finished: fetch only what is newer than the stored messages
            db.execute("UPDATE tasks SET lo=?, cursor=0, done=0 WHERE key=?", (newest, key))
    db.commit()


def range_work_left(db):
    """Message-id span still to scan in full-chat mode; measures progress exactly, also on repeat runs."""
    return db.execute(
        "SELECT COALESCE(SUM(MAX(cursor - 1 - lo, 0)), 0) FROM tasks WHERE done = 0 AND topic IS NULL"
    ).fetchone()[0]
