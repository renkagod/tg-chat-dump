"""Downloading: ranges or topics split over workers and accounts."""

import asyncio
import contextlib
import datetime
import functools
import logging
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from telethon import errors, functions, utils
from telethon.tl.functions import InvokeWithTakeoutRequest
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import Channel, InputUserSelf, PeerChannel

from .accounts import resolve
from .changes import Changes, apply_changes, channel_changes
from .chatinfo import add_photo, chat_info, fetch_bios, fetch_topics, pinned_ids, refresh_users
from .config import DATA
from .export import export_db, write_about
from .files import download_files, file_limits
from .scope import NO_SCOPE, Scope, has_link
from .store import (
    BATCH_SIZE,
    COLUMN_NAMES,
    clear,
    get_chat,
    get_state,
    open_db,
    outdated,
    person,
    plan_full,
    plan_topics,
    range_work_left,
    recheck_from,
    recheck_topics,
    refetch,
    save,
    save_users,
    set_chat,
    set_state,
    sync_pins,
    to_row,
)
from .util import all_or_none, fmt_duration, iso, parse_chat
from .who import fetch_who

log = logging.getLogger("dump")
EXTRAS_SHOWN = ("people", "who", "files")  # the progress of the extras, as stats shows it
RECHECK_DAYS = 2  # how far before the last run messages are downloaded again when Telegram's change log is no help


@dataclass
class Job:
    """What every worker needs to know about the chat being dumped."""

    db: sqlite3.Connection
    forum: bool
    options: frozenset
    iter_kwargs: dict
    when: str  # the time of this run, noted on the people seen in it
    gone_at: str | None = None  # the time to mark saved messages with when Telegram no longer lists them
    firsts: dict = field(default_factory=dict)  # id(client) -> the oldest message id its account sees
    marked: int = 0  # messages marked as deleted so far


async def run_task(client, entity, job, progress, key):
    topic, lo, cursor = job.db.execute("SELECT topic, lo, cursor FROM tasks WHERE key=?", (key,)).fetchone()
    kw = dict(job.iter_kwargs)
    if topic is not None:
        kw["reply_to"] = topic
    gone = (job.firsts.get(id(client), 0), job.gone_at) if job.gone_at else None
    rows, people = [], {}
    async for m in client.iter_messages(entity, offset_id=cursor, min_id=lo, wait_time=0, **kw):
        rows.append(to_row(m, job.forum, job.options))
        if m.sender is not None and m.sender_id:
            people[m.sender_id] = person(m.sender, m.sender_id)
        if len(rows) >= BATCH_SIZE:
            save_users(job.db, people.values(), job.when)
            job.marked += save(job.db, key, rows, gone=gone)
            progress["n"] += len(rows)
            rows, people = [], {}
    save_users(job.db, people.values(), job.when)
    job.marked += save(job.db, key, rows, done=True, gone=gone)
    progress["n"] += len(rows)
    progress["left"] -= 1


async def worker(queue, *args):
    while not queue.empty():
        await run_task(*args, queue.get_nowait())


async def report(progress, start):
    while True:
        await asyncio.sleep(10)
        rate = progress["n"] / (time.monotonic() - start) * 60
        log.info(f"{progress['n']} messages, ~{rate:.0f}/min, tasks left: {progress['left']}")


async def enter_takeout(stack, client):
    """Telegram's export mode for one account; falls back to the normal client if it is not allowed yet."""
    if not isinstance(client.session.takeout_id, int):
        client.session.takeout_id = None  # some session files hold b"" here, which Telethon takes for an open takeout
    if client.session.takeout_id:  # left open by a killed run; Telegram may have closed it since
        try:
            await client(InvokeWithTakeoutRequest(client.session.takeout_id, GetUsersRequest([InputUserSelf()])))
        except errors.TakeoutInvalidError:
            client.session.takeout_id = None
    # a takeout that is still open is reused, otherwise a new one is requested
    scopes = {} if client.session.takeout_id else dict(users=True, chats=True, megagroups=True, channels=True)
    try:
        return await stack.enter_async_context(client.takeout(finalize=True, **scopes))
    except errors.TakeoutInitDelayError as e:
        return e.seconds  # Telegram waits for the account owner to allow the export


class Takeouts:
    """Takeouts kept open across dumps: each account enters export mode once, and all leave it together on exit.

    Opening and closing a takeout is a slow request, so a session that dumps several chats saves seconds per chat.
    """

    def __init__(self):
        self.stacks, self.clients = {}, {}
        self.delayed = {}  # client -> seconds until Telegram allows the export without a confirmation
        self.declined = set()  # clients told to go on without export mode

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        stacks, self.stacks, self.clients = list(self.stacks.values()), {}, {}
        for r in await asyncio.gather(*(s.__aexit__(*exc) for s in stacks), return_exceptions=True):
            if isinstance(r, Exception):  # a takeout left open is reused or replaced next time
                log.warning(f"Cannot close the takeout: {r}")

    async def enter(self, pairs):
        """The (client, chat) pairs with every client switched to its takeout; new takeouts open in parallel."""
        new = [c for c, _ in pairs if c not in self.clients]
        for c in new:
            self.stacks.setdefault(c, contextlib.AsyncExitStack())
        new = [c for c in new if c not in self.declined]
        opened = await asyncio.gather(*(enter_takeout(self.stacks[c], c) for c in new))
        for c, t in zip(new, opened, strict=True):
            if isinstance(t, int):  # without takeout it is asked again on the next dump
                self.delayed[c] = t
                log.warning(
                    "Telegram asks to confirm the data export: open Telegram, allow the request from 'Telegram' "
                    f"in the chat list (or wait {fmt_duration(t)}). Continuing without takeout for now."
                )
            else:
                self.clients[c] = t
                self.delayed.pop(c, None)
        return [(self.clients.get(c, c), e) for c, e in pairs]

    def waiting(self, clients):
        """The clients whose export Telegram waits to have allowed."""
        return [c for c in clients if c in self.delayed and c not in self.clients and c not in self.declined]

    def decline(self, clients):
        """Go on without export mode for these clients until the session ends."""
        self.declined.update(clients)


async def busiest(pairs):
    """The (client, chat) pair whose account sees the most messages in the chat; pairs that fail are skipped."""
    best, most = pairs[0], -1
    for c, e in pairs:
        with contextlib.suppress(ValueError, TypeError, errors.RPCError):
            n = (await c.get_messages(e, limit=0)).total
            if n > most:
                best, most = (c, e), n
    return best


async def saved_copy(pairs, db_path):
    """The pair whose account saved the existing copy of a private chat, so a repeat run does not start over."""
    if len(pairs) < 2 or not db_path.exists():
        return None
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        if not db.execute("SELECT 1 FROM messages LIMIT 1").fetchone():
            return None
        for c, e in pairs:
            with contextlib.suppress(ValueError, TypeError, errors.RPCError):
                if await saved_by(c, e, db):
                    return c, e
    return None


def full_dump_done(db_path):
    """Whether the database holds a finished whole-chat dump, so filters can be applied to it."""
    if not db_path.exists():
        return False
    with contextlib.suppress(sqlite3.Error), contextlib.closing(sqlite3.connect(db_path)) as db:
        ranges, left = db.execute("SELECT COUNT(*), SUM(done = 0) FROM tasks WHERE topic IS NULL").fetchone()
        return ranges > 0 and not left
    return False


def has_details(db_path):
    """Whether every saved message says what its media is; rows saved before 1.3.0 do not, until --refetch."""
    db = open_db(db_path)
    try:
        return not outdated(db)
    finally:
        db.close()


def copy_scope(whole_path, db_path, scope, sender_id=None):
    """Fill a filtered database from the whole-chat one, without the network. Returns the message count."""
    db = open_db(db_path)
    try:
        db.create_function("has_link", 3, has_link, deterministic=True)
        where, params = scope.sql(sender_id)
        cols = ",".join(COLUMN_NAMES)
        db.execute("ATTACH DATABASE ? AS whole", (str(whole_path),))
        # the rows that are here already are updated in place, so the ones that changed are noted for the
        # export; a downloaded file is one of them, and its message is still here to carry it
        db.execute(f"DELETE FROM messages WHERE id NOT IN (SELECT id FROM whole.messages WHERE {where})", params)
        sets = ", ".join(f"{c}=excluded.{c}" for c in COLUMN_NAMES[1:])
        db.execute(
            f"INSERT INTO messages({cols}) SELECT {cols} FROM whole.messages WHERE {where} "
            f"ON CONFLICT(id) DO UPDATE SET {sets}",
            params,
        )
        for table in ("topics", "chat"):
            db.execute(f"DELETE FROM {table}")
            db.execute(f"INSERT INTO {table} SELECT * FROM whole.{table}")
        db.execute("DELETE FROM users")
        db.execute("INSERT INTO users SELECT * FROM whole.users WHERE id IN (SELECT sender_id FROM messages)")
        db.execute("DELETE FROM edits")
        db.execute("INSERT INTO edits SELECT * FROM whole.edits WHERE id IN (SELECT id FROM messages)")
        # who reacted and who voted: what the whole dump knows is added to what was asked here
        for table in ("reacted", "voted", "asked"):
            db.execute(f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM messages)")
            db.execute(
                f"INSERT OR IGNORE INTO {table} SELECT * FROM whole.{table} WHERE id IN (SELECT id FROM messages)"
            )
        db.execute(
            "INSERT OR IGNORE INTO users SELECT * FROM whole.users WHERE id IN "
            "(SELECT user_id FROM reacted UNION SELECT user_id FROM voted)"
        )
        db.commit()
        db.execute("DETACH DATABASE whole")
        return db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        db.close()


def saved_dumps():
    """Whole-chat dumps in data/ as (chat id, last run time, saved messages), the latest first."""
    found = []
    for p in DATA.glob("*.sqlite"):
        if not p.stem.isdigit():
            continue  # filtered dumps have a suffix
        exports, imported = [], False
        with contextlib.suppress(sqlite3.Error), contextlib.closing(sqlite3.connect(p)) as db:
            exports = db.execute("SELECT folder, rows FROM exports").fetchall()
            # read from an export and never downloaded: there may be no chat left to update
            imported = db.execute(
                "SELECT 1 FROM state WHERE key = 'imported' AND NOT EXISTS (SELECT 1 FROM tasks)"
            ).fetchone()
        if imported:
            continue
        if all(Path(f).name == "comments" for f, _ in exports):
            continue  # a channel's discussion group, updated with the channel; or never exported, before 1.1.4
        found.append((int(p.stem), p.stat().st_mtime, max((r for _, r in exports), default=0)))
    return sorted(found, key=lambda d: -d[1])


async def saved_by(client, entity, db, look=5):
    """Whether the stored messages came from this account: private chats number messages per account.

    The `look` newest stored ids are looked up; one of them with the same date is enough (others may be deleted).
    """
    stored = db.execute("SELECT id, date FROM messages ORDER BY id DESC LIMIT ?", (look,)).fetchall()
    if not stored:
        return True
    found = await client.get_messages(entity, ids=[i for i, _ in stored])
    return any(m is not None and iso(m.date) == date for m, (_, date) in zip(found, stored, strict=True))


async def oldest_id(client, entity):
    """The id of the oldest message this account sees in the chat, 0 when it sees none."""
    oldest = await client.get_messages(entity, limit=1, reverse=True)
    return oldest[0].id if oldest else 0


async def id_bounds(client, entity, scope):
    """First and last message id to scan, narrowed to the scope's dates. Returns (first, top, total)."""
    latest, oldest = await asyncio.gather(
        client.get_messages(entity, limit=1), client.get_messages(entity, limit=1, reverse=True)
    )
    if not latest:
        raise ValueError("The chat has no messages visible to this account")
    first = oldest[0].id
    top = latest[0].id
    if scope.since:
        before = await client.get_messages(entity, limit=1, offset_date=Scope.day_start(scope.since))
        if before:
            first = max(first, before[0].id + 1)
    if scope.until:
        upto = await client.get_messages(
            entity, limit=1, offset_date=Scope.day_start(scope.until + datetime.timedelta(1))
        )
        top = upto[0].id if upto else 0
    total = latest.total
    if scope.iter_kwargs():
        total = (await client.get_messages(entity, limit=0, **scope.iter_kwargs())).total
    return first, top, total


async def dump_chat(
    clients,
    chat,
    topics=None,
    workers=3,
    stats=None,
    options=frozenset(),
    scope=NO_SCOPE,
    folder=None,
    threads=False,
    takeouts=None,
    again=False,
    recheck=RECHECK_DAYS,
    files=None,
    who=True,
    seen=None,
):
    """Download a chat with every client that can see it, then export it.

    again=True downloads the saved messages again too (--refetch), to fill in what older versions did not keep.
    A repeat run also looks for messages edited or deleted since the last one: in Telegram's change log where
    the chat has one that reaches back far enough, otherwise by downloading again what was written from
    `recheck` days before the last run on. recheck=0 leaves the saved messages alone.

    `files` (a files.Limits) says which files the "files" extra downloads; without it the saved settings do.
    who=False leaves the asking of the "who" extra to the caller: the reactions are counted, nobody is asked
    who put them.
    `takeouts` (a Takeouts) keeps export mode open for later dumps; without it, it is closed when this one ends.
    `seen` ({client: the chat as its dialogs show it, or None}) spares asking Telegram for the chat again.
    `stats` (a dict) is updated in place for progress display: phase, label, total
    (approximate message count from Telegram), stored, fetched, accounts, and
    work_total/work_left (what is left to scan). Returns the export folder.
    Telegram rate limits are per account, so every account adds throughput.
    """
    stats = stats if stats is not None else {}
    stats.update(phase="connecting", total=0, stored=0, fetched=0, accounts=0, work_total=0, work_left=0)
    for done in EXTRAS_SHOWN:  # what the extras of a chat dumped before have shown
        stats.pop(done, None)
    stats.setdefault("label", "")
    scope = scope.within(topics)
    pairs = []

    async def find(c):
        if seen and c in seen:  # its dialogs were read: a chat not among them is looked up, not listed again
            return seen[c] or await resolve(c, chat, listed=True)
        return await resolve(c, chat)

    found = await asyncio.gather(*(find(c) for c in clients), return_exceptions=True)
    for c, e in zip(clients, found, strict=True):
        if isinstance(e, ValueError | TypeError):
            log.warning(f"{utils.get_display_name(await c.get_me())} cannot see this chat, skipping")
        elif isinstance(e, BaseException):
            raise e
        else:
            pairs.append((c, e))
    if not pairs:
        raise ValueError("None of the accounts can access this chat")
    db_path = DATA / f"{pairs[0][1].id}{scope.db_suffix()}.sqlite"
    if not isinstance(pairs[0][1], Channel):
        # private chats and basic groups number messages per account, so one account does the whole chat
        pairs = [await saved_copy(pairs, db_path) or await busiest(pairs)]
    stats["accounts"] = len(pairs)

    client, entity = pairs[0]
    title = utils.get_display_name(entity)
    forum = isinstance(entity, Channel) and bool(entity.forum)
    whole = DATA / f"{entity.id}.sqlite"
    now = iso(datetime.datetime.now(datetime.UTC).replace(microsecond=0))
    # a filter is applied to the saved copy of the whole chat where there is one; a type only when that copy
    # tells the types apart. Filters inside topics always work this way: the topics are saved first
    local = scope and folder is None and (topics or full_dump_done(whole))
    if local and (topics or not scope.kind or has_details(whole)):
        try:
            sender = await client.get_peer_id(parse_chat(scope.from_user)) if scope.from_user else None
        except (ValueError, TypeError, errors.RPCError):
            sender = None  # an author that cannot be looked up is left to Telegram's search
        if topics and scope.from_user and not sender:
            raise ValueError(f"Cannot find the user {scope.from_user}")
        if sender or not scope.from_user:
            # the whole chat, or these topics of it, is brought up to date, then the filters are applied to it
            # locally
            await dump_chat(
                clients,
                chat,
                topics=topics,
                workers=workers,
                stats=stats,
                # the files, and who reacted, are wanted for the filtered messages only: see below
                options=options - {"comments", "files"},
                who=False,
                takeouts=takeouts,
                again=again,
                recheck=recheck,
            )
            if topics and scope.kind and not has_details(whole):
                raise ValueError(
                    "--type inside topics needs what versions before 1.3.0 did not save; "
                    "run --refetch on the whole chat first"
                )
            stats.update(phase="exporting", label="")
            found = await asyncio.to_thread(copy_scope, whole, db_path, scope, sender)
            log.info(f"Filters: {scope.tag()}, {found} messages taken from the saved dump of the chat")
            stats.update(stored=found, fetched=0, local=True)
            export = functools.partial(
                export_db, db_path, entity.id, title, scope=scope, reader="html" in options, formats=options
            )
            out = await asyncio.to_thread(export, full=again)
            # the bios were asked for with the whole chat
            await extras(pairs, entity, db_path, out, export, now, stats, options - {"bio"}, files, workers=workers)
            stats["phase"] = "done"
            return out

    async with contextlib.AsyncExitStack() as stack:
        plain = [c for c, _ in pairs]  # for what Telegram does not serve in export mode
        if "takeout" in options:
            own = takeouts or await stack.enter_async_context(Takeouts())
            pairs = await own.enter(pairs)
            stats["no_takeout"] = sum(c not in own.clients.values() for c, _ in pairs)
        db = open_db(db_path)
        stack.callback(db.close)
        log.info(f"Chat: {title} (id {entity.id}), forum: {'yes' if forum else 'no'}, accounts: {len(pairs)}")
        if scope:
            log.info(f"Filters: {scope.tag()}")

        stats["phase"] = "planning"
        imported = get_state(db, "imported")
        # much of what came from an old export may be gone from Telegram, so more of it is looked up
        if not isinstance(entity, Channel) and not await saved_by(client, entity, db, 100 if imported else 5):
            if imported:  # it may hold messages that are nowhere else by now
                raise ValueError(
                    "The saved copy was read from an export that does not match this account's copy of the chat; "
                    "it is left as it is"
                )
            log.warning("The saved copy came from another account, downloading the chat again")
            clear(db)
        if again:
            refetch(db, topics)
            log.info("Downloading the saved messages again")
        elif outdated(db):
            log.info("Messages saved by an older version lack details it did not keep; --refetch fills them in")
        if forum:
            await fetch_topics(client, entity, db)

        # edits and deletions since the last run; a filtered dump has its own database and is left as it is
        check = recheck > 0 and not scope
        newest = db.execute("SELECT date, id FROM messages ORDER BY id DESC LIMIT 1").fetchone()
        last = get_state(db, "checked") or (newest and newest[0])
        had, had_edits = db.execute("SELECT (SELECT COUNT(*) FROM messages), (SELECT COUNT(*) FROM edits)").fetchone()
        seen, window = Changes(), None
        info, pts_now = await chat_info(client, entity)
        if check and isinstance(entity, Channel):
            pts = get_state(db, "pts")
            if pts and not again:
                stats["phase"] = "checking"
                seen = await channel_changes(client, entity, int(pts))
                if seen.complete:
                    log.info(f"Telegram's change log: {len(seen.edited)} edited, {len(seen.deleted)} deleted")
                else:
                    log.info("Telegram's change log does not reach back to the last run")
                stats["phase"] = "planning"
        if check and last and not again and not seen.complete:
            # no log to go by: what was written shortly before the last run is downloaded again
            start = datetime.datetime.fromisoformat(last) - datetime.timedelta(days=recheck)
            window = db.execute(
                "SELECT MIN(id) FROM messages WHERE id > COALESCE("
                "(SELECT id FROM messages WHERE date < ? ORDER BY id DESC LIMIT 1), 0)",
                (iso(start),),
            ).fetchone()[0]  # the first saved message of those days; both lookups walk the ids from the newest
            if window:
                log.info(f"Checking the messages from {start:%Y-%m-%d %H:%M} on for edits and deletions")

        if topics:
            if not forum:
                raise ValueError("The chat has no topics: it is not a forum")
            plan_topics(db, topics)
            if window:
                recheck_topics(db, topics, window)
            keys = [f"topic:{t}" for t in topics]
            total = 0
            for t in topics:
                total += (await client.get_messages(entity, limit=0, reply_to=t)).total
            marks = ",".join("?" * len(topics))
            stored = db.execute(f"SELECT COUNT(*) FROM messages WHERE topic_id IN ({marks})", topics).fetchone()[0]
            if window:  # the messages fetched again are counted as they come back
                again_sql = f"SELECT COUNT(*) FROM messages WHERE topic_id IN ({marks}) AND id >= ?"
                stored -= db.execute(again_sql, [*topics, window]).fetchone()[0]
            firsts = {}
        else:
            (first, top, total), *others = await asyncio.gather(
                id_bounds(client, entity, scope), *(oldest_id(c, e) for c, e in pairs[1:])
            )
            firsts = {id(c): f for (c, _), f in zip(pairs, [first, *others], strict=True)}
            log.info(f"Messages: ~{total}, ids {first}..{top}")
            if top >= first:
                plan_full(db, first, top, parts=workers * len(pairs))
            keys = [k for (k,) in db.execute("SELECT key FROM tasks WHERE topic IS NULL")]
            stored = had
            if window and (redo := recheck_from(db, window)) is not None:
                stored -= db.execute("SELECT COUNT(*) FROM messages WHERE id > ?", (redo,)).fetchone()[0]

        if again:
            stored = 0  # every saved message is fetched again, so none of them counts as already there
        pending = [k for k in keys if not db.execute("SELECT done FROM tasks WHERE key=?", (k,)).fetchone()[0]]
        queue = asyncio.Queue()
        for k in pending:
            queue.put_nowait(k)
        progress = {"n": 0, "left": len(pending)}

        def work_left():
            if topics:  # topic threads have no id span, count messages instead
                return max(total - stored - progress["n"], 0)
            return range_work_left(db)

        def sync():
            stats.update(fetched=progress["n"], stored=stored + progress["n"], work_left=work_left())

        stats.update(phase="downloading", total=total, stored=stored, work_total=work_left(), work_left=work_left())

        async def mirror():
            while True:
                sync()
                await asyncio.sleep(0.5)

        # a search by author or type lists only some of the messages, so it cannot tell which ones are gone
        gone_at = now if (check or again) and not scope.iter_kwargs() else None
        job = Job(db, forum, options, scope.iter_kwargs(), now, gone_at, firsts)
        helpers = [asyncio.create_task(report(progress, time.monotonic())), asyncio.create_task(mirror())]
        try:
            await all_or_none(worker(queue, c, e, job, progress) for _ in range(workers) for c, e in pairs)
        finally:
            for h in helpers:
                h.cancel()
            sync()
        if check:
            stats["phase"] = "checking"
            # with the export-mode client where there is one: the plain one is rate limited after a few requests
            job.marked += await apply_changes(*pairs[0], db, seen, forum, options, now, newest[1] if newest else 0)
            if not again or not topics:  # --refetch of some topics says nothing about the rest of the chat
                if seen.pts or pts_now:
                    set_state(db, "pts", seen.pts or pts_now)
                set_state(db, "checked", now)
            db.commit()
        if not scope:
            # the chat's own list of pins: a message unpinned long ago is found only there
            pins = None if topics else await pinned_ids(pairs[0][0], entity)
            if pins is not None:
                sync_pins(db, pins, first)
            stats["phase"] = "people"
            renamed = await refresh_users(plain, db, now)
            if renamed:
                log.info(f"People saved by earlier runs asked about again: {renamed}")
        created = db.execute("SELECT date FROM messages WHERE id = 1 AND action LIKE '%Create'").fetchone()
        if created:  # a channel tells its members only when they joined; its first message tells the rest
            info.setdefault("created", created[0])
        await add_photo(client, entity, info, get_chat(db).get("photo_id"))
        set_chat(db, {**info, "updated": now})
        saved, edits = db.execute("SELECT (SELECT COUNT(*) FROM messages), (SELECT COUNT(*) FROM edits)").fetchone()
        stats.update(stored=saved, new=saved - had, edited=edits - had_edits, deleted=job.marked)
        log.info(f"Fetched this run: {progress['n']}, new: {saved - had}, total in database: {saved}")
        if edits - had_edits or job.marked:
            log.info(f"Edited since the last run: {edits - had_edits}, gone from Telegram: {job.marked}")

    stats["phase"] = "exporting"
    export = functools.partial(
        export_db,
        db_path,
        entity.id,
        title,
        scope=scope,
        folder=folder,
        threads=threads,
        reader="html" in options,
        formats=options,
    )
    out = await asyncio.to_thread(export)
    mine = list(zip(plain, (e for _, e in pairs), strict=True))  # the accounts themselves, not in export mode
    wanted = options if who else options - {"who"}
    await extras(mine, entity, db_path, out, export, now, stats, wanted, files, topics, threads, workers)

    if "comments" in options and isinstance(entity, Channel) and entity.broadcast:
        await dump_comments(
            clients, client, entity, out, workers, stats, options, scope, takeouts, again, recheck, files
        )
    stats["phase"] = "done"
    return out


async def extras(
    pairs, entity, db_path, out, export, when, stats, options, limits=None, topics=None, threads=False, workers=3
):
    """The extras that ask Telegram person by person or message by message: "bio", "who" and "files".

    They come after the export, so the messages are all in place if they are stopped, and run together:
    each asks for a thing of its own, and Telegram limits each kind of request apart. What a stopped run
    was told is saved. The export is run once more when there are answers or files to show; the bios go
    to users.jsonl only, also when the run is stopped.
    """
    jobs = {}
    db = open_db(db_path)
    try:
        if "bio" in options:
            jobs["bio"] = fetch_bios([c for c, _ in pairs], db, when, stats)
        if "who" in options and isinstance(entity, Channel) and entity.broadcast:
            log.info("A channel does not say who reacted to its posts")
        elif "who" in options:
            jobs["who"] = fetch_who(pairs, db, when, stats, topics)
        if "files" in options:
            limits = limits or file_limits()
            jobs["files"] = download_files(pairs, db, out, limits, when, stats, topics, threads, workers)
        got = dict(zip(jobs, await all_or_none(jobs.values()), strict=True))
    finally:
        if "bio" in jobs:
            write_about(db, out)
        db.close()
    if got.get("who") or got.get("files"):
        stats["phase"] = "exporting"
        await asyncio.to_thread(export)


async def dump_comments(
    clients,
    client,
    channel,
    out,
    workers,
    stats,
    options,
    scope,
    takeouts,
    again=False,
    recheck=RECHECK_DAYS,
    files=None,
):
    """Comments under channel posts live in the linked discussion group; dump it into <channel>/comments."""
    full = await client(functions.channels.GetFullChannelRequest(channel))
    linked = full.full_chat.linked_chat_id
    if not linked:
        log.info("This channel has no comments (no discussion group)")
        return
    stats["label"] = "comments"
    try:
        await dump_chat(
            clients,
            PeerChannel(linked),
            workers=workers,
            stats=stats,
            options=options - {"comments"},
            scope=scope,
            folder=out / "comments",
            threads=True,
            takeouts=takeouts,
            again=again,
            recheck=recheck,
            files=files,
        )
    except (ValueError, errors.RPCError) as e:
        log.warning(f"Cannot dump the comments: {e}")
    finally:
        stats["label"] = ""
