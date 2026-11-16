"""Reading an export made by Telegram Desktop into a dump: no account is asked for anything."""

import json
import logging
import shutil
from pathlib import Path

from .config import DATA, out_dir
from .export import chat_folder, export
from .store import COLUMN_NAMES, get_chat, open_db, set_chat, set_state
from .tdesktop import chat_kind, read_message, topics_of
from .util import topic_folder

log = logging.getLogger("dump")


def dump_folder(db, chat_id, title, out_root=None):
    """The chat's folder in the output folder: the one a run has written already, or a new one."""
    root = Path(out_root or out_dir()).resolve()
    for (folder,) in db.execute("SELECT folder FROM exports"):
        if Path(folder).is_dir() and Path(folder).resolve().parent == root:
            return Path(folder)
    return chat_folder(chat_id, title, out_root=out_root)


def copy_files(db, files, source, out):
    """Copy the files an export came with into the folders of their topics; returns how many are new there.

    files: {message id: (path inside the export, width, height)}.
    """
    titles = dict(db.execute("SELECT id, title FROM topics"))
    known = {i for (i,) in db.execute("SELECT id FROM files WHERE path IS NOT NULL")}
    copied = 0
    for mid, (path, w, h) in files.items():
        origin = source / path
        if mid in known or not origin.is_file():
            continue
        # where the message is in the dump, which for one saved before is not always where the export had it
        (topic,) = db.execute("SELECT topic_id FROM messages WHERE id=?", (mid,)).fetchone()
        folder = (out if topic is None else topic_folder(out, topic, titles)) / "files"
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{mid}_{origin.name}"
        shutil.copy2(origin, folder / name)
        row = (mid, f"files/{name}", origin.stat().st_size, w, h, None)
        db.execute("INSERT OR REPLACE INTO files VALUES(?, ?, ?, ?, ?, ?)", row)
        copied += 1
    db.commit()
    return copied


def import_chat(chat, source, out_root=None, **how):
    """Read one chat of an export into its database and write its folders. Returns (folder, messages added)."""
    messages = [m for m in chat.get("messages", []) if isinstance(m.get("id"), int)]
    title = chat.get("name") or ("Saved Messages" if chat.get("type") == "saved_messages" else "Deleted Account")
    DATA.mkdir(parents=True, exist_ok=True)
    db = open_db(DATA / f"{chat['id']}.sqlite")
    try:
        had = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        known = dict(db.execute("SELECT id, title FROM topics"))
        placed = dict(db.execute("SELECT id, topic_id FROM messages")) if known else {}
        topics, heads = topics_of(messages, known, placed)
        rows, files = [], {}
        for m in messages:
            row, path = read_message(m, topics.get(m["id"]) if topics else None)
            rows.append(row)
            if path:
                files[m["id"]] = (path, m.get("width"), m.get("height"))
        # only what is not saved yet: a message the tool has downloaded itself says more than the export does
        marks = ",".join("?" * len(COLUMN_NAMES))
        db.executemany(f"INSERT OR IGNORE INTO messages({','.join(COLUMN_NAMES)}) VALUES({marks})", rows)
        db.executemany("INSERT OR IGNORE INTO topics(id, title) VALUES(?, ?)", heads.items())
        if topics:
            db.execute("INSERT OR IGNORE INTO topics(id, title) VALUES(1, 'General')")
        db.execute(
            "INSERT OR IGNORE INTO users(id, name) SELECT sender_id, sender FROM "
            "(SELECT sender_id, sender, MAX(id) FROM messages WHERE sender_id IS NOT NULL GROUP BY sender_id)"
        )
        if not get_chat(db):
            kind = "forum" if topics else chat_kind(chat.get("type"))
            set_chat(db, {"id": chat["id"], "kind": kind, "title": title})
        set_state(db, "imported", 1)
        db.commit()
        added = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] - had
        title = get_chat(db).get("title") or title
        out = dump_folder(db, chat["id"], title, out_root)
        if copied := copy_files(db, files, source, out):
            log.info(f"Files copied from the export: {copied}")
        # older messages may have come in between the saved ones, so every folder is written whole
        db.execute("UPDATE exports SET max_id=-1")
        export(db, chat["id"], title, folder=out, full=True, **how)
        return out, added
    finally:
        db.close()


def import_result(path, out_root=None, **how):
    """Read Telegram Desktop's result.json, of one chat or of a whole account, into the databases.

    `path` is the file or the folder it is in; `how` goes to export.export (reader, formats).
    Returns [(folder, title, messages in the export, messages added)].
    """
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / "result.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if "messages" in data:
        chats = [data]
    else:
        chats = [c for key in ("chats", "left_chats") for c in (data.get(key) or {}).get("list", [])]
    done = []
    for chat in chats:
        if not isinstance(chat.get("id"), int) or not chat.get("messages"):
            continue
        out, added = import_chat(chat, path.parent, out_root, **how)
        name = chat.get("name") or str(chat["id"])
        log.info(f"Imported {name}: {len(chat['messages'])} messages in the export, {added} new, in {out}")
        done.append((out, name, len(chat["messages"]), added))
    return done
