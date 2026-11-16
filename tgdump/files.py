"""The "files" extra: what is attached to the saved messages, downloaded into the folders of their topics."""

import asyncio
import collections
import contextlib
import logging
import mimetypes
import os
import re
from dataclasses import dataclass

from telethon import errors, types

from .config import load_settings
from .details import file_info
from .util import all_or_none, size_text, topic_folder

log = logging.getLogger("dump")
# --file-types -> what a saved message calls its file in extra.file.kind; a photo has no such entry,
# a plain document has no kind
TYPES = {
    "photos": "photo",
    "videos": "video",
    "gifs": "gif",
    "voice": "voice",
    "round": "round",
    "music": "audio",
    "docs": None,
    "stickers": "sticker",
}
DEFAULT_TYPES = "photos,videos,gifs,voice,round,music,docs"  # stickers repeat a lot, so only when asked for
DEFAULT_SIZE = 20  # MB
PER_REQUEST = 100  # messages asked for at once: a file is downloaded from the message as Telegram has it now
MEDIA = ("MessageMediaPhoto", "MessageMediaDocument")
# what a file without a name of its own ends in; mimetypes answers differently from system to system
EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "application/pdf": ".pdf",
    "application/x-tgsticker": ".tgs",
}
UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')  # what a file name cannot hold on one system or another


@dataclass(frozen=True)
class Limits:
    """Which files are downloaded: their kinds as in TYPES' values, and the largest size in bytes (0: any)."""

    kinds: frozenset
    size: int

    def wants(self, kind, size):
        return kind in self.kinds and not (self.size and size and size > self.size)

    def __str__(self):
        names = ", ".join(name for name, kind in TYPES.items() if kind in self.kinds)
        return f"{names}, {f'up to {size_text(self.size)}' if self.size else 'of any size'}"


def file_limits(types=None, size=None):
    """Limits from --file-types and --file-size; what is not given comes from .env, then from the defaults."""
    s = load_settings()
    names = {t.strip().lower() for t in (types or s["TG_FILE_TYPES"] or DEFAULT_TYPES).split(",") if t.strip()}
    if "all" in names:
        names = set(TYPES)
    if unknown := names - TYPES.keys():
        raise ValueError(f"unknown file type(s): {', '.join(sorted(unknown))}; choose from {', '.join(TYPES)}, all")
    size = s["TG_FILE_SIZE"] or DEFAULT_SIZE if size in (None, "") else size
    try:
        megabytes = float(size)
    except ValueError:
        megabytes = -1
    if megabytes < 0:
        raise ValueError(f"cannot read the file size {size!r}; it is a number of megabytes, 0 for no limit")
    return Limits(frozenset(TYPES[n] for n in names), int(megabytes * 1024 * 1024))


def wanted(db, out, limits, titles, topics=None, threads=False):
    """Saved messages whose file is within the limits and not in its folder yet, the newest first.

    Returns [(message id, topic id, size or None)]. A file that could not be downloaded is not asked for
    again, and one that was over the size limit only when the limit has grown past it.
    """
    sql = (
        "SELECT m.id, m.topic_id, m.media = 'MessageMediaPhoto', json_extract(m.extra, '$.file.kind'), "
        "json_extract(m.extra, '$.file.mime'), json_extract(m.extra, '$.file.size'), "
        "f.id IS NOT NULL, f.path, f.size FROM messages m LEFT JOIN files f ON f.id = m.id "
        f"WHERE m.media IN {MEDIA} AND m.deleted_at IS NULL"
    )
    if topics:
        sql += f" AND m.topic_id IN ({','.join(str(int(t)) for t in topics)})"
    found, folders = [], {}
    for mid, topic, photo, kind, mime, size, tried, path, seen in db.execute(sql + " ORDER BY m.id DESC"):
        if photo:
            kind = "photo"
        elif mime is None:
            kind = ...  # saved before 1.3.0, which did not keep what the file is: the message itself will tell
        size = size or seen
        if kind is not ... and not limits.wants(kind, size):
            continue
        if tried and not path and not (seen and limits.wants(kind, seen)):
            continue
        if path:
            if topic not in folders:
                folders[topic] = folder_of(out, topic, titles, threads)
            if (folders[topic] / path).exists():
                continue
        found.append((mid, topic, size))
    return found


def folder_of(out, topic, titles, threads=False):
    """Where a message's topic is exported to; a chat without topics has one folder for everything."""
    return out if topic is None or threads else topic_folder(out, topic, titles)


def attached(m):
    """(kind, size, width, height) of the file a message carries, or None when there is none to download."""
    media = m.media
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        if not any(isinstance(s, types.PhotoSize | types.PhotoSizeProgressive) for s in media.photo.sizes):
            return None
        kind = "photo"
    elif isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        kind = file_info(media).get("kind")
    else:
        return None  # a photo that has expired, or no file at all
    return kind, m.file.size, m.file.width, m.file.height


def location(media):
    """What to ask Telegram for to get the file, and the data centre that keeps it."""
    if isinstance(media, types.MessageMediaPhoto):
        photo = media.photo
        sizes = [s for s in photo.sizes if isinstance(s, types.PhotoSize | types.PhotoSizeProgressive)]
        best = max(sizes, key=lambda s: s.w * s.h)  # the photo as it was sent; the rest are previews of it
        return types.InputPhotoFileLocation(photo.id, photo.access_hash, photo.file_reference, best.type), photo.dc_id
    doc = media.document
    return types.InputDocumentFileLocation(doc.id, doc.access_hash, doc.file_reference, thumb_size=""), doc.dc_id


async def fetch(client, m, path):
    """Write the message's file to `path`.

    The data centre is named at once: left to find out, the client asks its own one first, a request wasted
    on every file kept elsewhere.
    """
    where, dc = location(m.media)
    with open(path, "wb") as f:
        async for chunk in client.iter_download(where, dc_id=dc, file_size=m.file.size):
            f.write(chunk)


def file_name(m):
    """The message id, then the file's own name where it has one: 123_report.pdf, 124.jpg."""
    if name := m.file.name:
        name = UNSAFE.sub("_", os.path.basename(name.replace("\\", "/"))).strip(" .")
        stem, ext = os.path.splitext(name)
        if stem:
            return f"{m.id}_{stem[:60].rstrip(' .')}{ext[:12]}"
    mime = m.file.mime_type or ""
    return f"{m.id}{EXTENSIONS.get(mime) or mimetypes.guess_extension(mime) or ''}"


async def download_files(pairs, db, out, limits, when, stats, topics=None, threads=False, workers=3):
    """Download the files of the saved messages into <topic folder>/files. Returns how many were downloaded.

    Every (client, chat) pair takes its share, `workers` files at a time. A message is asked for again first,
    a hundred at a time: Telegram hands a file out only for a short while after showing the message. What one
    account cannot see is left to the others. Each file is noted as it lands, so a stopped run goes on with
    the rest next time; the messages are marked as changed, for the export to add the paths.
    """
    titles = dict(db.execute("SELECT id, title FROM topics"))
    todo = wanted(db, out, limits, titles, topics, threads)
    if not todo:
        return 0
    known = sum(size or 0 for _, _, size in todo)
    log.info(f"Files: {len(todo)} to download ({limits}){f', {size_text(known)} or more' if known else ''}")
    total, done, loaded, weight = len(todo), 0, 0, 0
    stats.update(phase="files", files=(0, total, 0))
    batches = collections.deque(todo[n : n + PER_REQUEST] for n in range(0, total, PER_REQUEST))
    missed = [[] for _ in pairs]  # what each account could not see
    made = set()

    def note(mid, path=None, size=None, w=None, h=None):
        db.execute("INSERT OR REPLACE INTO files VALUES(?, ?, ?, ?, ?, ?)", (mid, path, size, w, h, when))
        if path:
            db.execute("INSERT OR IGNORE INTO changed VALUES(?)", (mid,))
        db.commit()

    async def save(client, m, topic):
        nonlocal loaded, weight
        found = attached(m)
        if not found:
            note(m.id)
            return
        kind, size, w, h = found
        if not limits.wants(kind, size):
            if kind in limits.kinds:  # too large; with a larger limit the file is wanted again
                note(m.id, size=size)
            return
        folder = folder_of(out, topic, titles, threads) / "files"
        if folder not in made:
            folder.mkdir(parents=True, exist_ok=True)
            made.add(folder)
        name = file_name(m)
        part = folder / (name + ".part")  # a file under its own name is always whole
        try:
            await fetch(client, m, part)
        except BaseException as e:
            with contextlib.suppress(OSError):
                part.unlink()  # half a file is of no use
            if not isinstance(e, errors.RPCError | OSError | ValueError):
                raise  # the run was stopped
            log.warning(f"Cannot download the file of message {m.id}: {e}")
            return  # asked for again by the next run
        os.replace(part, folder / name)
        size = (folder / name).stat().st_size
        note(m.id, f"files/{name}", size, w, h)
        loaded += 1
        weight += size

    async def some(k, part):
        """One request's worth of messages with account k; the ones it cannot see are set aside."""
        client, entity = pairs[k]
        gate = asyncio.Semaphore(workers)

        async def one(m, topic):
            nonlocal done
            async with gate:
                await save(client, m, topic)
            done += 1
            stats["files"] = (done, total, weight)

        got = await client.get_messages(entity, ids=[mid for mid, _, _ in part])
        jobs = []
        for item, m in zip(part, got, strict=True):
            if m is None:
                missed[k].append(item)
            else:
                jobs.append(one(m, item[1]))
        await all_or_none(jobs)

    async def account(k):
        while batches:
            part = batches.popleft()
            try:
                await some(k, part)
            except errors.RPCError as e:
                log.warning(f"Files: an account stopped downloading: {e}")
                batches.append(part)  # for the other accounts, or the next run
                return

    async def report():
        while True:
            await asyncio.sleep(30)
            log.info(f"Files: {done} of {total} looked at, {loaded} downloaded, {size_text(weight)}")

    progress = asyncio.create_task(report())
    try:
        await all_or_none(account(k) for k in range(len(pairs)))
        # what an account could not see is passed on to the next one, until every account has been asked:
        # one of them may have joined the chat earlier
        for _ in pairs[1:]:
            passed = [list(items) for items in missed]
            for items in missed:
                items.clear()
            for k, items in enumerate(passed, 1):
                for n in range(0, len(items), PER_REQUEST):
                    with contextlib.suppress(errors.RPCError):
                        await some(k % len(pairs), items[n : n + PER_REQUEST])
        for items in missed:
            for mid, _, _ in items:
                note(mid)  # gone from Telegram
                done += 1
    finally:
        progress.cancel()
        db.commit()
    stats["files"] = (done, total, weight)
    log.info(f"Files: {loaded} downloaded, {size_text(weight)}")
    return loaded
