"""Searching the saved dumps: every chat in data/, read as it is, without the network."""

import contextlib
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import DATA
from .scope import NO_SCOPE, has_link

LIMIT = 20  # matches shown per chat unless asked otherwise
AROUND = 60  # characters of the text shown on each side of the first match


@dataclass
class Found:
    """What a search found in one chat."""

    chat_id: int
    title: str
    folder: Path | None  # the chat's folder, where it still is
    total: int = 0
    hits: list = field(default_factory=list)  # the newest ones: (id, date, topic id, topic title, sender, text, gone)
    problem: str | None = None  # why the chat could not be searched


def words(query):
    """The words to look for, in any case; a "quoted phrase" is looked for as it is."""
    found = re.findall(r'"([^"]+)"|(\S+)', query or "")
    return [(phrase or word).casefold() for phrase, word in found]


def snippet(text, wanted, around=AROUND):
    """A line of the text around the first of the words."""
    flat = " ".join(text.split())
    at = min((i for w in wanted if (i := flat.casefold().find(w)) >= 0), default=0)
    start, end = max(at - around, 0), min(at + around, len(flat))
    return ("…" if start else "") + flat[start:end] + ("…" if end < len(flat) else "")


def chat_title(db, folder, chat_id):
    with contextlib.suppress(sqlite3.Error):
        if row := db.execute("SELECT value FROM chat WHERE key = 'title'").fetchone():
            return row[0]
    if folder is None:
        return str(chat_id)
    if folder.name == "comments":  # a channel's discussion group, saved inside the channel's folder
        return f"{folder.parent.name.partition('_')[2].replace('_', ' ')}, comments"
    return folder.name.partition("_")[2].replace("_", " ") or str(chat_id)


def is_chat(db, folder, chat_id, ref):
    """Whether the saved chat is the one named: by id, @username, t.me link or a part of its title."""
    ref = str(ref).strip()
    if ref.lstrip("-").isdigit():
        return str(chat_id) in (ref.lstrip("-"), ref.lstrip("-").removeprefix("100"))
    name = ref.rstrip("/").rsplit("/", 1)[-1].lstrip("@").casefold()
    username = None
    with contextlib.suppress(sqlite3.Error):
        if row := db.execute("SELECT value FROM chat WHERE key = 'username'").fetchone():
            username = row[0]
    return name == (username or "").casefold() or name in chat_title(db, folder, chat_id).casefold()


def senders(db, who):
    """Ids of the people a --from names: an id, an @username, or a part of the name."""
    who = who.strip()
    if who.lstrip("-").isdigit():
        return [int(who)]
    name = who.lstrip("@").casefold()
    found = []
    for i, shown, username in db.execute("SELECT id, name, username FROM users"):
        if (username or "").casefold() == name or (not who.startswith("@") and name in (shown or "").casefold()):
            found.append(i)
    return found


def search_db(db, wanted, scope=NO_SCOPE, limit=LIMIT):
    """(how many messages have all the words, the newest `limit` of them)."""
    db.create_function("has_link", 3, has_link, deterministic=True)
    db.create_function("hit", 1, lambda text: text is not None and all(w in text.casefold() for w in wanted))
    where, params = scope.sql()
    if scope.from_user:
        ids = senders(db, scope.from_user)
        if not ids:
            return 0, []
        where += f" AND sender_id IN ({','.join('?' * len(ids))})"
        params += ids
    columns = {c[1] for c in db.execute("PRAGMA table_info(messages)")}
    gone = "deleted_at IS NOT NULL" if "deleted_at" in columns else "0"
    titles = dict(db.execute("SELECT id, title FROM topics"))
    sql = f"SELECT id, date, topic_id, sender, text, {gone} FROM messages WHERE {where} AND hit(text) ORDER BY id DESC"
    total, hits = 0, []
    for mid, date, topic, sender, text, deleted in db.execute(sql, params):
        total += 1
        if len(hits) < limit:
            hits.append((mid, date, topic, titles.get(topic), sender, text, deleted))
    return total, hits


def search(query, scope=NO_SCOPE, chat=None, limit=LIMIT):
    """Look for the words in every saved chat, the one dumped last first; `chat` narrows it to the chats named.

    Yields a Found per chat that has matches or could not be read.
    """
    wanted = words(query)
    if not wanted:
        raise ValueError("nothing to search for")
    paths = [p for p in DATA.glob("*.sqlite") if p.stem.isdigit()]  # a filtered dump holds a part of a whole one
    for p in sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True):
        chat_id = int(p.stem)
        with contextlib.closing(sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)) as db:
            try:
                folders = [Path(f) for (f,) in db.execute("SELECT folder FROM exports")]
                folder = next((f for f in folders if f.is_dir()), None)
                if chat and not is_chat(db, folder, chat_id, chat):
                    continue
                found = Found(chat_id, chat_title(db, folder, chat_id), folder)
                found.total, found.hits = search_db(db, wanted, scope, limit)
            except sqlite3.Error as e:  # saved by a version whose database lacks what the search looks at
                found = Found(chat_id, str(chat_id), None, problem=str(e))
        if found.total or found.problem:
            yield found


def anchor(mid, topic):
    """Where the message is in the chat's reader."""
    return f"#msg/{mid}" if topic is None else f"#topic/{topic}/msg/{mid}"


def say(line):
    """Print a line on any console: what its encoding has no letters for, an emoji on Windows, is replaced."""
    encoding = sys.stdout.encoding or "utf-8"
    print(line.encode(encoding, "replace").decode(encoding))


def report(query, scope=NO_SCOPE, chat=None, limit=LIMIT, write=say):
    """Print what a search finds; returns the number of matching messages."""
    wanted, total, chats = words(query), 0, 0
    for found in search(query, scope, chat, limit):
        if found.problem:
            write(f"\n{found.title}: cannot be searched ({found.problem}); a run of the chat brings it up to date")
            continue
        total, chats = total + found.total, chats + 1
        reader = found.folder / "index.html" if found.folder else None
        where = f"  {reader.as_uri()}" if reader and reader.is_file() else ""
        write(f"\n{found.title}: {found.total:,} message{'s' if found.total != 1 else ''}{where}")
        for mid, date, topic, topic_title, sender, text, gone in found.hits:
            who = " · ".join(filter(None, (topic_title, sender)))
            head = ("(deleted) " if gone else "") + (f"{who}: " if who else "")
            write(f"  {date[:16].replace('T', ' ')}  {head}{snippet(text, wanted)}  {anchor(mid, topic)}")
        if found.total > len(found.hits):
            write(f"  … and {found.total - len(found.hits):,} older; --limit {found.total} shows them all")
    if not total:
        write("Nothing found.")
    elif chats > 1:
        write(f"\n{total:,} messages in {chats} chats")
    return total
