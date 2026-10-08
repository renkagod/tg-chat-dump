"""Downloading: ranges or topics split over workers and accounts."""

import asyncio
import contextlib
import datetime
import logging
import sqlite3
import time
from dataclasses import dataclass

from telethon import errors, functions, utils
from telethon.tl.functions import InvokeWithTakeoutRequest
from telethon.tl.functions.users import GetUsersRequest
from telethon.tl.types import Channel, InputUserSelf, PeerChannel

from .accounts import resolve
from .config import DATA
from .export import export_db
from .scope import NO_SCOPE, Scope
from .store import BATCH_SIZE, clear, open_db, plan_full, plan_topics, range_work_left, save, to_row
from .util import fmt_duration, iso

log = logging.getLogger("dump")


@dataclass
class Job:
    """What every worker needs to know about the chat being dumped."""

    db: sqlite3.Connection
    forum: bool
    options: frozenset
    iter_kwargs: dict


async def run_task(client, entity, job, progress, key):
    topic, lo, cursor = job.db.execute("SELECT topic, lo, cursor FROM tasks WHERE key=?", (key,)).fetchone()
    kw = dict(job.iter_kwargs)
    if topic is not None:
        kw["reply_to"] = topic
    rows = []
    async for m in client.iter_messages(entity, offset_id=cursor, min_id=lo, wait_time=0, **kw):
        rows.append(to_row(m, job.forum, job.options))
        if len(rows) >= BATCH_SIZE:
            save(job.db, key, rows)
            progress["n"] += len(rows)
            rows = []
    save(job.db, key, rows, done=True)
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


async def fetch_topics(client, entity, db):
    offset_date, offset_id, offset_topic = None, 0, 0
    while True:
        res = await client(
            functions.messages.GetForumTopicsRequest(
                peer=entity, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100
            )
        )
        db.executemany(
            "INSERT OR REPLACE INTO topics VALUES(?, ?)", [(t.id, getattr(t, "title", None)) for t in res.topics]
        )
        if len(res.topics) < 100:
            break
        last = res.topics[-1]
        msgs = {m.id: m for m in res.messages}
        offset_topic, offset_id = last.id, getattr(last, "top_message", 0)
        offset_date = msgs[offset_id].date if offset_id in msgs else None
    db.commit()


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
        log.warning(
            "Telegram asks to confirm the data export: open Telegram, allow the request from 'Telegram' "
            f"in the chat list (or wait {fmt_duration(e.seconds)}). Continuing without takeout for now."
        )
        return client


class Takeouts:
    """Takeouts kept open across dumps: each account enters export mode once, and all leave it together on exit.

    Opening and closing a takeout is a slow request, so a session that dumps several chats saves seconds per chat.
    """

    def __init__(self):
        self.stacks, self.clients = {}, {}

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
        opened = await asyncio.gather(*(enter_takeout(self.stacks[c], c) for c in new))
        for c, t in zip(new, opened, strict=True):
            if t is not c:  # without takeout it is asked again on the next dump
                self.clients[c] = t
        return [(self.clients.get(c, c), e) for c, e in pairs]


async def busiest(pairs):
    """The (client, chat) pair whose account sees the most messages in the chat; pairs that fail are skipped."""
    best, most = pairs[0], -1
    for c, e in pairs:
        with contextlib.suppress(ValueError, TypeError, errors.RPCError):
            n = (await c.get_messages(e, limit=0)).total
            if n > most:
                best, most = (c, e), n
    return best


async def saved_by(client, entity, db):
    """Whether the stored messages came from this account: private chats number messages per account.

    The newest stored ids are looked up; one of them with the same date is enough (others may be deleted).
    """
    stored = db.execute("SELECT id, date FROM messages ORDER BY id DESC LIMIT 5").fetchall()
    if not stored:
        return True
    found = await client.get_messages(entity, ids=[i for i, _ in stored])
    return any(m is not None and iso(m.date) == date for m, (_, date) in zip(found, stored, strict=True))


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
):
    """Download a chat with every client that can see it, then export it.

    `takeouts` (a Takeouts) keeps export mode open for later dumps; without it, it is closed when this one ends.
    `stats` (a dict) is updated in place for progress display: phase, label, total
    (approximate message count from Telegram), stored, fetched, accounts, and
    work_total/work_left (what is left to scan). Returns the export folder.
    Telegram rate limits are per account, so every account adds throughput.
    """
    stats = stats if stats is not None else {}
    stats.update(phase="connecting", total=0, stored=0, fetched=0, accounts=0, work_total=0, work_left=0)
    stats.setdefault("label", "")
    if topics and scope:
        raise ValueError("Filters work only with whole-chat dumps, not with --topics")
    pairs = []
    found = await asyncio.gather(*(resolve(c, chat) for c in clients), return_exceptions=True)
    for c, e in zip(clients, found, strict=True):
        if isinstance(e, ValueError | TypeError):
            log.warning(f"{utils.get_display_name(await c.get_me())} cannot see this chat, skipping")
        elif isinstance(e, BaseException):
            raise e
        else:
            pairs.append((c, e))
    if not pairs:
        raise ValueError("None of the accounts can access this chat")
    if not isinstance(pairs[0][1], Channel):
        # private chats and basic groups number messages per account, so one account does the whole chat
        pairs = [await busiest(pairs)]
    stats["accounts"] = len(pairs)

    client, entity = pairs[0]
    title = utils.get_display_name(entity)
    forum = isinstance(entity, Channel) and bool(entity.forum)
    db_path = DATA / f"{entity.id}{scope.db_suffix()}.sqlite"
    async with contextlib.AsyncExitStack() as stack:
        if "takeout" in options:
            own = takeouts or await stack.enter_async_context(Takeouts())
            pairs = await own.enter(pairs)
        db = open_db(db_path)
        stack.callback(db.close)
        log.info(f"Chat: {title} (id {entity.id}), forum: {'yes' if forum else 'no'}, accounts: {len(pairs)}")
        if scope:
            log.info(f"Filters: {scope.tag()}")

        stats["phase"] = "planning"
        if not isinstance(entity, Channel) and not await saved_by(client, entity, db):
            log.warning("The saved copy came from another account, downloading the chat again")
            clear(db)
        if forum:
            await fetch_topics(client, entity, db)
        if topics:
            topics = [t for t in topics if t != 1]
            if not topics:
                raise ValueError("General (id 1) cannot be dumped on its own; dump the whole chat instead")
            plan_topics(db, topics)
            keys = [f"topic:{t}" for t in topics]
            total = 0
            for t in topics:
                total += (await client.get_messages(entity, limit=0, reply_to=t)).total
            marks = ",".join("?" * len(topics))
            stored = db.execute(f"SELECT COUNT(*) FROM messages WHERE topic_id IN ({marks})", topics).fetchone()[0]
        else:
            first, top, total = await id_bounds(client, entity, scope)
            log.info(f"Messages: ~{total}, ids {first}..{top}")
            if top >= first:
                plan_full(db, first, top, parts=workers * len(pairs))
            keys = [k for (k,) in db.execute("SELECT key FROM tasks WHERE topic IS NULL")]
            stored = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

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

        job = Job(db, forum, options, scope.iter_kwargs())
        helpers = [asyncio.create_task(report(progress, time.monotonic())), asyncio.create_task(mirror())]
        try:
            await asyncio.gather(*(worker(queue, c, e, job, progress) for _ in range(workers) for c, e in pairs))
        finally:
            for h in helpers:
                h.cancel()
            sync()
        log.info(f"Fetched this run: {progress['n']}, total in database: {stored + progress['n']}")

    stats["phase"] = "exporting"
    out = await asyncio.to_thread(export_db, db_path, entity.id, title, scope=scope, folder=folder, threads=threads)

    if "comments" in options and isinstance(entity, Channel) and entity.broadcast:
        await dump_comments(clients, client, entity, out, workers, stats, options, scope, takeouts)
    stats["phase"] = "done"
    return out


async def dump_comments(clients, client, channel, out, workers, stats, options, scope, takeouts):
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
        )
    except (ValueError, errors.RPCError) as e:
        log.warning(f"Cannot dump the comments: {e}")
    finally:
        stats["label"] = ""
