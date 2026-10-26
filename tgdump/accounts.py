"""Telegram sessions in data/*.session."""

import asyncio
import contextlib
import getpass
import io
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

import qrcode
from telethon import TelegramClient, errors, functions, utils
from telethon.tl.types import PeerChannel, PeerUser

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

    Accounts connect in parallel, each with its own timeout, so one that cannot connect does not hold up the others.
    """

    async def connect(name):
        c = make_client(name)
        try:
            await asyncio.wait_for(c.connect(), timeout)
            if not await asyncio.wait_for(c.is_user_authorized(), timeout):
                problem = f"{name}: not logged in"
            elif until := await asyncio.wait_for(frozen_until(c), timeout):
                # a frozen account is read-only: it cannot even look up a public chat by its username
                me = await c.get_me()
                problem = (
                    f"{name} ({utils.get_display_name(me)}): frozen by Telegram, skipped; "
                    f"appeal via @SpamBot before {until:%Y-%m-%d} or the account is deleted"
                )
            else:
                return c, None
        except TimeoutError:
            problem = f"{name}: no answer from Telegram in {timeout}s"
        except OSError as e:
            problem = f"{name}: {e or type(e).__name__}"
        log.warning(problem)
        await c.disconnect()
        return None, problem

    results = await asyncio.gather(*(connect(n) for n in session_names()))
    return [c for c, _ in results if c], [p for _, p in results if p]


async def frozen_until(client):
    """When a frozen account is deleted unless appealed, or None when the account is not frozen."""
    try:
        config = await client(functions.help.GetAppConfigRequest(hash=0))
    except errors.RPCError:
        return None
    values = {v.key: v.value for v in getattr(config.config, "value", [])}
    until = values.get("freeze_until_date")
    return datetime.fromtimestamp(until.value, UTC) if until else None


async def resolve(client, chat):
    # a bare positive id is a channel or group, otherwise a user
    targets = [PeerChannel(chat), PeerUser(chat)] if isinstance(chat, int) and chat > 0 else [chat]
    for fresh in (False, True):
        if fresh:  # a fresh session does not know the chat's access_hash yet
            await client.get_dialogs()
        for target in targets:
            with contextlib.suppress(ValueError):
                return await client.get_entity(target)
    raise ValueError(f"Cannot find {chat}")


async def find_account(clients, ref):
    """The client named by its session file (as in --login), @username or user id."""
    ref = str(ref).lstrip("@").lower()
    for c in clients:
        me = await c.get_me()
        if ref in {Path(c.session.filename).stem.lower(), str(me.id), (me.username or "").lower()}:
            return c
    raise ValueError(f"no logged-in account matches {ref!r}")


def qr_text(url):
    """The QR code as text, light modules as blocks so it reads on a dark terminal."""
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    out = io.StringIO()
    qr.print_ascii(out=out, invert=True)
    return out.getvalue()


async def qr_login(client):
    """Log in by scanning a QR code with a phone where the account is already logged in."""
    qr = await client.qr_login()
    print("In Telegram on your phone: Settings > Devices > Link Desktop Device, then scan this code.")
    drawn = 0
    while True:
        text = qr_text(qr.url)
        if drawn and sys.stdout.isatty():  # draw the new code over the old one
            print(f"\x1b[{drawn}A", end="")
        print(text, end="", flush=True)
        drawn = text.count("\n")
        try:
            return await qr.wait()
        except TimeoutError:
            await qr.recreate()  # a code lives about 30 seconds
        except errors.SessionPasswordNeededError:
            break
    while True:  # two-step verification
        try:
            return await client.sign_in(password=getpass.getpass("Two-step verification password: "))
        except errors.PasswordHashInvalidError:
            print("Wrong password, try again")


async def login(client):
    """Log a new session in: a QR code by default, or the phone number and code. Returns the account."""
    await client.connect()
    if not await client.is_user_authorized():
        how = input("Log in with [Enter] a QR code from the Telegram app or [p] a phone number > ").strip().lower()
        if how == "p":
            await client.start()  # asks for the phone, the code and the two-step password
        else:
            await qr_login(client)
    return await client.get_me()
