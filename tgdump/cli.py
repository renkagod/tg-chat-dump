"""Command-line mode, and the entry point that picks it or the interactive mode."""

import argparse
import asyncio
import contextlib
import logging
import sys
from pathlib import Path

from telethon import errors, utils

from . import interactive
from .accounts import find_account, login, make_client, open_accounts, resolve
from .config import DATA, OPTIONS, ensure_api_keys, load_settings, parse_options, save_settings
from .export import export_db
from .fetch import RECHECK_DAYS, Takeouts, dump_chat, saved_dumps
from .files import DEFAULT_SIZE, DEFAULT_TYPES, TYPES, file_limits
from .imports import import_result
from .schedule import apply as set_schedule
from .scope import KINDS, Scope, parse_date
from .search import LIMIT, report
from .util import parse_chat

log = logging.getLogger("dump")
LINE = "%(asctime)s %(levelname)s %(name)s: %(message)s"
LOG_LIMIT = 10_000_000  # bytes; past this dump.log is started anew, and the old one stays as dump.log.1


def setup_logging(console=True):
    """Log to data/dump.log and, unless the interactive mode draws its own progress, to the console."""
    DATA.mkdir(parents=True, exist_ok=True)
    file = logging.FileHandler(fresh_log(DATA / "dump.log"), encoding="utf-8")
    file.setFormatter(logging.Formatter(LINE, "%Y-%m-%d %H:%M:%S"))  # scheduled runs fill it day after day
    handlers = [file]
    if console and sys.stderr is not None:  # a scheduled run has no console at all
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format=LINE, datefmt="%H:%M:%S", handlers=handlers)


def fresh_log(path, limit=LOG_LIMIT):
    """The log to write to; one that has grown past the limit is put aside first."""
    with contextlib.suppress(OSError):  # none yet, or another run has it open: then it grows a little more
        if path.stat().st_size > limit:
            path.replace(path.with_name(path.name + ".1"))
    return path


def parse_args():
    s = load_settings()
    p = argparse.ArgumentParser(
        prog="dump.py" if sys.argv[0].endswith("dump.py") else "tg-chat-dump",
        description="Dump a Telegram chat, forum topics included, into SQLite and per-topic folders.",
        epilog="Run without arguments for the interactive mode.",
    )
    p.add_argument(
        "target",
        nargs="*",
        metavar="CHAT",
        help="chat id, @username or t.me link; several chats are dumped one after another",
    )
    p.add_argument("--chat", help="one more way to name a chat: id, @username or t.me link")
    p.add_argument("--update-all", action="store_true", help="fetch the new messages of every chat dumped before")
    p.add_argument(
        "--schedule",
        nargs="?",
        const="show",
        metavar="WHEN",
        help="have the system run --update-all on its own: 04:30 for every day at that time, hourly, or 6h for "
        "every six hours; off removes the schedule, and without a value it is shown",
    )
    p.add_argument(
        "--topics",
        help="only these forum topics, comma-separated ids (1 is General); with filters, the filtered messages "
        "of these topics go to a folder of their own",
    )
    p.add_argument("--workers", type=int, default=3, help="parallel workers per account")
    p.add_argument(
        "--account",
        metavar="NAME",
        help="dump with this account only: session name (as in --login), @username or user id; "
        "private chats default to the account that saved them, or the one with the most messages",
    )
    p.add_argument(
        "--with",
        dest="options",
        default=s["TG_OPTIONS"],
        metavar="OPTIONS",
        help="comma-separated extras: " + "; ".join(f"{k}: {v}" for k, v in OPTIONS.items()),
    )
    p.add_argument("--since", metavar="DATE", help="only messages from this day on: YYYY-MM-DD, YYYY-MM, YYYY, 7d, 2w")
    p.add_argument("--until", metavar="DATE", help="only messages up to this day, inclusive; a month means its end")
    p.add_argument("--from", dest="from_user", metavar="USER", help="only messages from this user (@username or id)")
    p.add_argument("--type", dest="kind", choices=KINDS, help="only messages of this type")
    p.add_argument(
        "--file-types",
        metavar="TYPES",
        help=f"what the files extra downloads, comma-separated: {', '.join(TYPES)}, or all "
        f"(default {s['TG_FILE_TYPES'] or DEFAULT_TYPES})",
    )
    p.add_argument(
        "--file-size",
        metavar="MB",
        help="the largest file the files extra downloads, in megabytes; 0 for no limit "
        f"(default {s['TG_FILE_SIZE'] or DEFAULT_SIZE})",
    )
    p.add_argument("--out", metavar="DIR", help="output folder; it is saved to .env and used from now on")
    p.add_argument("--export-only", action="store_true", help="rebuild the output folders from the database")
    p.add_argument(
        "--search",
        metavar="TEXT",
        help="look for these words in every chat dumped before, without the network; a chat named with it and "
        "--since, --until, --from, --type narrow the search",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=LIMIT,
        metavar="N",
        help=f"how many matches --search shows per chat (default {LIMIT})",
    )
    p.add_argument(
        "--import",
        dest="import_path",
        metavar="PATH",
        help="read an export made by Telegram Desktop (its result.json, or the folder it is in) into a dump: "
        "no account is needed, and messages that are saved already stay as they are",
    )
    p.add_argument(
        "--refetch",
        action="store_true",
        help="download the saved messages again, to fill in what older versions did not keep (rich messages, "
        "file names, link previews, buttons...); rows are updated in place, nothing saved is dropped",
    )
    p.add_argument(
        "--recheck",
        type=int,
        default=RECHECK_DAYS,
        metavar="DAYS",
        help="when Telegram cannot list the edits and deletions since the last run, download again what was "
        f"written from this many days before it (default {RECHECK_DAYS}); 0 does not look for them at all",
    )
    p.add_argument("--login", metavar="NAME", help="log in one more account to data/NAME.session and exit")
    a = p.parse_args()
    a.named = [*a.target, *([a.chat] if a.chat else [])]  # the chats named on the command line
    a.chats = a.named or [c for c in [s["TG_CHAT"]] if c]
    asked = a.login or a.update_all or a.import_path or a.search is not None or a.schedule is not None
    if not a.chats and not asked:
        p.error("name a chat (tg-chat-dump @somegroup), pass --update-all, or set TG_CHAT in .env")
    if a.topics and len(a.chats) > 1:
        p.error("--topics names the topics of one chat")
    try:
        a.options = parse_options(a.options)
        a.files = file_limits(a.file_types, a.file_size)
        a.topics = sorted({int(t) for t in a.topics.split(",") if t.strip()}) if a.topics else None
        a.scope = Scope(parse_date(a.since), parse_date(a.until, end=True), a.from_user, a.kind).within(a.topics)
    except ValueError as e:
        p.error(str(e))
    return a


async def update_all(clients, workers, options, takeouts, again=False, recheck=RECHECK_DAYS, files=None):
    """Fetch the new messages of every whole-chat dump in data/; a chat that fails does not stop the others."""
    dumps = saved_dumps()
    if not dumps:
        raise SystemExit("No chats dumped yet")
    known = {}  # the dialogs give every account the chats' access hashes
    for found in await asyncio.gather(*(c.get_dialogs() for c in clients)):
        for d in found:
            known.setdefault(d.entity.id, d.entity)
    failed = []
    for chat_id, _, _ in dumps:
        entity = known.get(chat_id)
        target = utils.get_peer(entity) if entity else chat_id
        try:
            await dump_chat(
                clients,
                target,
                workers=workers,
                options=options,
                takeouts=takeouts,
                again=again,
                recheck=recheck,
                files=files,
            )
        except (ValueError, errors.RPCError) as e:
            log.warning(f"Cannot update {chat_id}: {e}")
            failed.append(chat_id)
    log.info(f"Updated {len(dumps) - len(failed)} of {len(dumps)} chats")
    if failed:
        raise SystemExit(1)


async def main():
    a = parse_args()
    if a.import_path:  # nothing is asked of Telegram, so no keys and no account
        if a.out:
            save_settings({"TG_OUT": str(Path(a.out).expanduser().resolve())})
        try:
            found = import_result(a.import_path, reader="html" in a.options, formats=a.options)
        except (OSError, ValueError, KeyError) as e:
            raise SystemExit(f"Cannot read the export: {e}") from None
        if not found:
            raise SystemExit("The export has no chats with messages")
        return
    if a.schedule is not None:
        try:
            set_schedule(a.schedule)
        except (ValueError, OSError) as e:
            raise SystemExit(f"--schedule: {e}") from None
        return
    if a.search is not None:  # the saved dumps are read as they are
        try:
            report(a.search, a.scope, a.named, a.limit)
        except ValueError as e:
            raise SystemExit(f"--search: {e}") from None
        return
    ensure_api_keys()
    if a.out:
        save_settings({"TG_OUT": str(Path(a.out).expanduser().resolve())})

    if a.login:
        c = make_client(a.login)
        try:
            me = await login(c)
            log.info(f"Account {utils.get_display_name(me)} saved to data/{a.login}.session")
        finally:
            await c.disconnect()
        return

    clients, _ = await open_accounts()
    try:
        if not clients:  # first run: log in the main account interactively
            clients = [make_client("session")]
            await login(clients[0])
        using = clients
        if a.account:
            try:
                using = [await find_account(clients, a.account)]
            except ValueError as e:
                raise SystemExit(f"--account: {e}") from None
        if a.update_all:
            async with Takeouts() as takeouts:
                await update_all(using, a.workers, a.options, takeouts, a.refetch, a.recheck, a.files)
            return
        if a.export_only:
            for chat in a.chats:
                entity = await resolve(using[0], parse_chat(chat))
                db_path = DATA / f"{entity.id}{a.scope.db_suffix()}.sqlite"
                title = utils.get_display_name(entity)
                how = dict(scope=a.scope, full=True, reader="html" in a.options, formats=a.options)
                export_db(db_path, entity.id, title, **how)
            return
        failed = []
        # export mode is entered once for all the chats; it also covers a channel's comments, dumped right after it
        async with Takeouts() as takeouts:
            for chat in a.chats:
                try:
                    await dump_chat(
                        using,
                        parse_chat(chat),
                        topics=a.topics,
                        workers=a.workers,
                        options=a.options,
                        scope=a.scope,
                        takeouts=takeouts,
                        again=a.refetch,
                        recheck=a.recheck,
                        files=a.files,
                    )
                except (ValueError, errors.RPCError) as e:
                    if len(a.chats) == 1:
                        raise
                    log.warning(f"Cannot dump {chat}: {e}")  # the other chats are still dumped
                    failed.append(chat)
        if len(a.chats) > 1:
            log.info(f"Dumped {len(a.chats) - len(failed)} of {len(a.chats)} chats")
        if failed:
            raise SystemExit(1)
    finally:
        for c in clients:
            await c.disconnect()


def run():
    interactive_mode = len(sys.argv) == 1
    setup_logging(console=not interactive_mode)
    try:
        asyncio.run(interactive.main() if interactive_mode else main())
    except (KeyboardInterrupt, EOFError):
        log.info("Stopped, progress is saved")
        if interactive_mode:
            print("\nStopped, progress is saved." if interactive.dumped else "\nBye.")
    except Exception:
        log.exception("Crashed")
        raise
