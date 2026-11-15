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
# The database layout, stored as its user_version; open_db brings older databases up to it.
# 2: rich messages, media details, buttons, reply quotes, forward sources and action fields in the rows
#    (rows saved before that lack them until --refetch).
# 3: deleted_at, the edits, changed and state tables, edits and deletions in the exported text.
FORMAT = 3

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
    ("deleted_at", "TEXT"),  # when a later run found the message gone from Telegram
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
-- earlier texts of edited messages: `date` is when that text was written, `replaced` the edit that replaced it
CREATE TABLE IF NOT EXISTS edits(
  id INTEGER, date TEXT, text TEXT, text_md TEXT, replaced TEXT, PRIMARY KEY(id, date));
-- messages changed in place since the last export, so their part of it is written again
CREATE TABLE IF NOT EXISTS changed(id INTEGER PRIMARY KEY);
-- where the chat stands: its pts (Telegram's change counter) and the time of the last check for edits
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, value);
-- the triggers check for an existing row themselves: inside an upsert, OR IGNORE of a trigger's statement is not used
CREATE TRIGGER IF NOT EXISTS keep_edit AFTER UPDATE OF text, edit_date ON messages
WHEN new.edit_date IS NOT NULL AND new.edit_date IS NOT old.edit_date AND new.text IS NOT old.text
BEGIN
  INSERT INTO edits SELECT old.id, COALESCE(old.edit_date, old.date), old.text, old.text_md, new.edit_date
  WHERE NOT EXISTS (SELECT 1 FROM edits WHERE id = old.id AND date = COALESCE(old.edit_date, old.date));
END;
CREATE TRIGGER IF NOT EXISTS note_change AFTER UPDATE ON messages
WHEN {" OR ".join(f"new.{n} IS NOT old.{n}" for n, _ in COLUMNS[1:])}
BEGIN
  INSERT INTO changed SELECT old.id WHERE NOT EXISTS (SELECT 1 FROM changed WHERE id = old.id);
END;
"""


def open_db(path):
    db = sqlite3.connect(path)
    # a commit every BATCH_SIZE messages: with WAL it is a cheap append instead of a full journal sync
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    have = {c[1] for c in db.execute("PRAGMA table_info(messages)")}
    for name, typ in COLUMNS:  # databases from older versions lack the newer columns, which the triggers name
        if have and name not in have:
            db.execute(f"ALTER TABLE messages ADD COLUMN {name} {typ}")
    db.executescript(SCHEMA)
    db.execute("CREATE INDEX IF NOT EXISTS messages_topic ON messages(topic_id, id)")
    migrate(db)
    db.commit()
    return db


def migrate(db):
    """Bring a database written by an older version up to FORMAT; open_db has added the tables and columns."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= FORMAT:
        return
    if db.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
        if version < 2:
            set_state(db, "lacks_details", 1)  # what those rows miss cannot be added here, only by --refetch
        if version < 3:
            # the text files show edits and deletions now, so every export is written again once;
            # the rows stay, they also list the chats dumped before
            db.execute("UPDATE exports SET max_id=-1")
    db.execute(f"PRAGMA user_version={FORMAT}")


def get_state(db, key, default=None):
    row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_state(db, key, value):
    """Remember a value for the next run; None forgets it."""
    if value is None:
        db.execute("DELETE FROM state WHERE key=?", (key,))
    else:
        db.execute("INSERT OR REPLACE INTO state VALUES(?, ?)", (key, value))


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
        None,  # deleted_at: Telegram has the message, whatever an earlier run found
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


def save(db, key, rows, done=False, gone=None):
    """Store a task's next messages, newest first, and move its cursor down to them.

    gone=(first id, timestamp) also marks as deleted the saved messages that Telegram left out of the span
    these rows cover: it lists a span whole, so a message missing there is gone. Ids below the first one are
    not judged, the account may just not see that far back. Returns how many messages were marked.
    """
    topic, lo, cursor = db.execute("SELECT topic, lo, cursor FROM tasks WHERE key=?", (key,)).fetchone()
    ids = [r[0] for r in rows]
    if rows:
        db.executemany(UPSERT, rows)
        db.execute("UPDATE tasks SET cursor=? WHERE key=?", (min(ids), key))
    marked = 0
    if gone and (rows or done):
        first, when = gone
        where, params = "id >= ? AND deleted_at IS NULL", [when, max(lo + 1 if done else min(ids), first)]
        if cursor:  # 0 is a topic read from its newest message down
            where += " AND id < ?"
            params.append(cursor)
        if topic is not None:  # a topic's first message is not among its replies
            where += " AND topic_id = ? AND id != ?"
            params += [topic, topic]
        sql = f"UPDATE messages SET deleted_at=? WHERE {where} AND id NOT IN ({','.join('?' * len(ids))})"
        marked = db.execute(sql, params + ids).rowcount
    if done:
        db.execute("UPDATE tasks SET done=1 WHERE key=?", (key,))
    db.commit()
    return marked


def _chunks(ids, size=500):
    ids = list(ids)
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def mark_deleted(db, ids, when):
    """Mark the saved messages among `ids` as gone from Telegram. Returns how many were marked."""
    marked = 0
    for part in _chunks(ids):
        sql = f"UPDATE messages SET deleted_at=? WHERE deleted_at IS NULL AND id IN ({','.join('?' * len(part))})"
        marked += db.execute(sql, [when, *part]).rowcount
    db.commit()
    return marked


def stored_ids(db, ids):
    """Those of `ids` that are saved."""
    found = []
    for part in _chunks(ids):
        found += [r[0] for r in db.execute(f"SELECT id FROM messages WHERE id IN ({','.join('?' * len(part))})", part)]
    return found


def set_pinned(db, pins):
    """Apply {message id: pinned or not} to the details of the saved messages."""
    for mid, pinned in pins.items():
        row = db.execute("SELECT extra FROM messages WHERE id=?", (mid,)).fetchone()
        extra = json.loads(row[0]) if row and row[0] else {}
        if not row or bool(extra.get("pinned")) == pinned:
            continue
        if pinned:
            extra["pinned"] = True
        else:
            del extra["pinned"]
        packed = json.dumps(extra, ensure_ascii=False) if extra else None
        db.execute("UPDATE messages SET extra=? WHERE id=?", (packed, mid))
    db.commit()


def recheck_from(db, first_id):
    """Have the ranges that reach `first_id` or newer downloaded again, to find edits and deletions there.

    Returns the id above which messages are fetched again, or None when no range reaches that far.
    """
    lo = db.execute("SELECT MIN(lo) FROM tasks WHERE topic IS NULL AND hi >= ?", (first_id,)).fetchone()[0]
    if lo is not None:
        db.execute("UPDATE tasks SET cursor=hi + 1, done=0 WHERE topic IS NULL AND hi >= ?", (first_id,))
        db.commit()
    return lo


def recheck_topics(db, topics, first_id):
    """The same for topic downloads: each of these topics is read again from its newest message down to `first_id`."""
    rows = [(first_id - 1, f"topic:{t}") for t in topics]
    db.executemany("UPDATE tasks SET lo=MIN(lo, ?), cursor=0, done=0 WHERE key=?", rows)
    db.commit()


def outdated(db):
    """Whether messages were saved by a version before 1.3.0, which kept less of them; --refetch fills that in."""
    return bool(get_state(db, "lacks_details"))


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
        set_state(db, "lacks_details", None)
    db.execute("UPDATE exports SET max_id=-1")  # written again; the rows stay, they list the chats dumped before
    db.commit()


def clear(db):
    """Forget everything downloaded, so the chat is fetched again from scratch."""
    db.executescript(
        "DELETE FROM messages; DELETE FROM tasks; DELETE FROM topics; DELETE FROM edits; DELETE FROM changed;"
        "DELETE FROM state; UPDATE exports SET max_id=-1;"
    )


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
