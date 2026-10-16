"""Command-line mode, and the entry point that picks it or the interactive mode."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from telethon import errors, utils

from . import interactive
from .accounts import find_account, login, make_client, open_accounts, resolve
from .config import DATA, OPTIONS, ensure_api_keys, load_settings, parse_options, save_settings
from .export import export_db
from .fetch import Takeouts, dump_chat, saved_dumps
from .scope import KINDS, Scope, parse_date
from .util import parse_chat

log = logging.getLogger("dump")


def setup_logging(console=True):
    """Log to data/dump.log and, unless the interactive mode draws its own progress, to the console."""
    DATA.mkdir(parents=True, exist_ok=True)
    handlers = [logging.FileHandler(DATA / "dump.log", encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )


def parse_args():
    s = load_settings()
    p = argparse.ArgumentParser(
        prog="dump.py" if sys.argv[0].endswith("dump.py") else "tg-chat-dump",
        description="Dump a Telegram chat, forum topics included, into SQLite and per-topic folders.",
        epilog="Run without arguments for the interactive mode.",
    )
    p.add_argument("target", nargs="?", metavar="CHAT", help="chat id, @username or t.me link (same as --chat)")
    p.add_argument("--chat", default=s["TG_CHAT"], help="chat id, @username or t.me link")
    p.add_argument("--update-all", action="store_true", help="fetch the new messages of every chat dumped before")
    p.add_argument("--topics", help="comma-separated topic ids; without it the whole chat is dumped")
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
    p.add_argument("--out", metavar="DIR", help="output folder; it is saved to .env and used from now on")
    p.add_argument("--export-only", action="store_true", help="rebuild the output folders from the database")
    p.add_argument("--login", metavar="NAME", help="log in one more account to data/NAME.session and exit")
    a = p.parse_args()
    a.chat = a.target or a.chat
    if not a.chat and not a.login and not a.update_all:
        p.error("name a chat (tg-chat-dump @somegroup), pass --update-all, or set TG_CHAT in .env")
    try:
        a.options = parse_options(a.options)
        a.scope = Scope(parse_date(a.since), parse_date(a.until, end=True), a.from_user, a.kind)
    except ValueError as e:
        p.error(str(e))
    return a


async def update_all(clients, workers, options, takeouts):
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
            await dump_chat(clients, target, workers=workers, options=options, takeouts=takeouts)
        except (ValueError, errors.RPCError) as e:
            log.warning(f"Cannot update {chat_id}: {e}")
            failed.append(chat_id)
    log.info(f"Updated {len(dumps) - len(failed)} of {len(dumps)} chats")
    if failed:
        raise SystemExit(1)


async def main():
    a = parse_args()
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
                await update_all(using, a.workers, a.options, takeouts)
            return
        chat = parse_chat(a.chat)
        if a.export_only:
            entity = await resolve(using[0], chat)
            db_path = DATA / f"{entity.id}{a.scope.db_suffix()}.sqlite"
            title = utils.get_display_name(entity)
            export_db(db_path, entity.id, title, scope=a.scope, full=True, reader="html" in a.options)
            return
        topics = [int(t) for t in a.topics.split(",")] if a.topics else None
        async with Takeouts() as takeouts:  # also covers a channel's comments, dumped right after it
            await dump_chat(
                using, chat, topics=topics, workers=a.workers, options=a.options, scope=a.scope, takeouts=takeouts
            )
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
