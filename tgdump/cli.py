"""Command-line mode, and the entry point that picks it or the interactive mode."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from telethon import utils

from . import interactive
from .accounts import make_client, open_accounts, resolve
from .config import DATA, OPTIONS, ensure_api_keys, load_settings, parse_options, save_settings
from .export import export_db
from .fetch import dump_chat
from .scope import KINDS, Scope, parse_date
from .util import parse_chat

log = logging.getLogger("dump")


def setup_logging(console=True):
    """Log to data/dump.log and, unless the interactive mode draws its own progress, to the console."""
    DATA.mkdir(exist_ok=True)
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
        prog="dump.py",
        description="Dump a Telegram chat, forum topics included, into SQLite and per-topic folders.",
        epilog="Run without arguments for the interactive mode.",
    )
    p.add_argument("--chat", default=s["TG_CHAT"], help="chat id, @username or t.me link")
    p.add_argument("--topics", help="comma-separated topic ids; without it the whole chat is dumped")
    p.add_argument("--workers", type=int, default=3, help="parallel workers per account")
    p.add_argument(
        "--with",
        dest="options",
        default=s["TG_OPTIONS"],
        metavar="OPTIONS",
        help="comma-separated extras: " + "; ".join(f"{k}: {v}" for k, v in OPTIONS.items()),
    )
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only messages from this day on")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="only messages up to this day, inclusive")
    p.add_argument("--from", dest="from_user", metavar="USER", help="only messages from this user (@username or id)")
    p.add_argument("--type", dest="kind", choices=KINDS, help="only messages of this type")
    p.add_argument("--out", metavar="DIR", help="output folder; it is saved to .env and used from now on")
    p.add_argument("--export-only", action="store_true", help="rebuild the output folders from the database")
    p.add_argument("--login", metavar="NAME", help="log in one more account to data/NAME.session and exit")
    a = p.parse_args()
    if not a.chat and not a.login:
        p.error("pass --chat or set TG_CHAT in .env")
    try:
        a.options = parse_options(a.options)
        a.scope = Scope(parse_date(a.since), parse_date(a.until), a.from_user, a.kind)
    except ValueError as e:
        p.error(str(e))
    return a


async def main():
    a = parse_args()
    ensure_api_keys()
    if a.out:
        save_settings({"TG_OUT": str(Path(a.out).expanduser().resolve())})

    if a.login:
        async with make_client(a.login) as c:  # start() asks for phone and code
            log.info(f"Account {utils.get_display_name(await c.get_me())} saved to data/{a.login}.session")
        return

    clients, _ = await open_accounts()
    try:
        if not clients:  # first run: log in the main account interactively
            c = make_client("session")
            await c.start()
            clients = [c]
        chat = parse_chat(a.chat)
        if a.export_only:
            entity = await resolve(clients[0], chat)
            db_path = DATA / f"{entity.id}{a.scope.db_suffix()}.sqlite"
            export_db(db_path, entity.id, utils.get_display_name(entity), scope=a.scope)
            return
        topics = [int(t) for t in a.topics.split(",")] if a.topics else None
        await dump_chat(clients, chat, topics=topics, workers=a.workers, options=a.options, scope=a.scope)
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
            print("\nStopped, progress is saved.")
    except Exception:
        log.exception("Crashed")
        raise
