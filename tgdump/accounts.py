"""Telegram sessions in data/*.session."""

import asyncio
import logging

from telethon import TelegramClient
from telethon.tl.types import PeerChannel

from .config import DATA, load_settings, parse_proxy

log = logging.getLogger("dump")


def make_client(name):
    s = load_settings()
    DATA.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(DATA / name),
        int(s["TG_API_ID"]),
        s["TG_API_HASH"],
        proxy=parse_proxy(s["TG_PROXY"]),
        flood_sleep_threshold=24 * 3600,
    )


def session_names():
    return [p.stem for p in sorted(DATA.glob("*.session"))]


def new_session_name():
    taken = set(session_names())
    return next(f"acc{i}" for i in range(1, 1000) if f"acc{i}" not in taken)


async def open_accounts(timeout=30):
    """Connect every data/*.session; returns (logged-in clients, problems).

    Each account gets its own timeout, so one that cannot connect does not hold up the others.
    """
    clients, problems = [], []
    for name in session_names():
        c = make_client(name)
        try:
            await asyncio.wait_for(c.connect(), timeout)
            if await asyncio.wait_for(c.is_user_authorized(), timeout):
                clients.append(c)
                continue
            problems.append(f"{name}: not logged in")
        except TimeoutError:
            problems.append(f"{name}: no answer from Telegram in {timeout}s")
        except OSError as e:
            problems.append(f"{name}: {e or type(e).__name__}")
        log.warning(problems[-1])
        await c.disconnect()
    return clients, problems


async def resolve(client, chat):
    target = PeerChannel(chat) if isinstance(chat, int) and chat > 0 else chat
    try:
        return await client.get_entity(target)
    except ValueError:  # a fresh session does not know the chat's access_hash yet
        await client.get_dialogs()
        return await client.get_entity(target)
