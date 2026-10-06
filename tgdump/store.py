"""SQLite storage: the message table, Telegram message -> row, and download planning."""

import json
import sqlite3

from telethon import types, utils
from telethon.extensions import markdown
from telethon.tl.types import MessageActionTopicCreate, MessageReplyHeader

from .util import iso, plain

CHUNK = 5000  # message ids per task in full-chat mode
BATCH_SIZE = 100  # how often progress is committed

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
    ("extra", "TEXT"),  # JSON: poll, todo, channel_post
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
"""


def open_db(path):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    have = {c[1] for c in db.execute("PRAGMA table_info(messages)")}
    for name, typ in COLUMNS:  # databases from older versions lack the extra columns
        if name not in have:
            db.execute(f"ALTER TABLE messages ADD COLUMN {name} {typ}")
    db.execute("CREATE INDEX IF NOT EXISTS messages_topic ON messages(topic_id, id)")
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


def media_details(media):
    if isinstance(media, types.MessageMediaPoll):
        poll, results = media.poll, media.results
        votes = {v.option: v.voters for v in results.results} if results and results.results else {}
        return {
            "poll": {
                "question": plain(poll.question),
                "answers": [{"text": plain(a.text), "voters": votes.get(a.option)} for a in poll.answers],
                "total_voters": results.total_voters if results else None,
                "closed": bool(poll.closed),
                "quiz": bool(poll.quiz),
                "multiple_choice": bool(poll.multiple_choice),
            }
        }
    if isinstance(media, types.MessageMediaToDo):
        done = {c.id for c in media.completions or []}
        return {
            "todo": {
                "title": plain(media.todo.title),
                "items": [{"text": plain(i.title), "done": i.id in done} for i in media.todo.list],
            }
        }
    return {}


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
    fwd = None
    if fwd_header:
        fwd = fwd_header.from_name or (str(utils.get_peer_id(fwd_header.from_id)) if fwd_header.from_id else None)
    sender = utils.get_display_name(m.sender) if m.sender else getattr(m, "post_author", None)
    media = getattr(m, "media", None)
    text = getattr(m, "message", None)

    text_md = views = forwards = replies = reactions = None
    extra = {}
    if fwd_header and fwd_header.channel_post:
        extra["channel_post"] = fwd_header.channel_post  # links a discussion-group thread to its channel post
    if "markdown" in options and text and getattr(m, "entities", None):
        text_md = markdown.unparse(text, m.entities)
    if "meta" in options:
        views, forwards = getattr(m, "views", None), getattr(m, "forwards", None)
        rp = getattr(m, "replies", None)
        replies = rp.replies if rp else None
        reactions = reactions_of(m)
    if "polls" in options:
        extra.update(media_details(media))

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


def save(db, key, rows, done=False):
    if rows:
        cols = ",".join(COLUMN_NAMES)
        db.executemany(f"INSERT OR REPLACE INTO messages({cols}) VALUES({','.join('?' * len(COLUMNS))})", rows)
        db.execute("UPDATE tasks SET cursor=? WHERE key=?", (min(r[0] for r in rows), key))
    if done:
        db.execute("UPDATE tasks SET done=1 WHERE key=?", (key,))
    db.commit()


def plan_full(db, first_id, top_id):
    """Split [first_id, top_id] into ranges; existing ranges are kept, only new ones are added on top."""
    # Nothing exists below the first visible message, so those ranges are skipped.
    db.execute("UPDATE tasks SET done=1 WHERE topic IS NULL AND hi < ?", (first_id,))
    start = db.execute("SELECT COALESCE(MAX(hi), 0) FROM tasks WHERE topic IS NULL").fetchone()[0]
    start = max(start, first_id - 1)
    for lo in range(start, top_id, CHUNK):
        hi = min(lo + CHUNK, top_id)
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
