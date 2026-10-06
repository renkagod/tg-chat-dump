"""Interactive console mode: pick accounts and a chat, then watch the dump."""

import asyncio
import collections
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from telethon import errors, functions, utils
from telethon.tl.types import Channel, Chat, User

from .accounts import make_client, new_session_name, open_accounts
from .config import DATA, OPTIONS, ensure_api_keys, load_settings, out_dir, save_options, save_settings, saved_options
from .fetch import dump_chat
from .scope import KINDS, Scope, parse_date
from .util import fmt_duration

log = logging.getLogger("dump")
FLOOD_RE = re.compile(r"Sleeping (?:early )?for (\d+)s")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SHOW = 15  # chats listed per search


async def ask(prompt=""):
    """input() without blocking the event loop, so the Telegram connections stay alive."""
    return (await asyncio.to_thread(input, prompt)).strip()


def account_name(me):
    name = utils.get_display_name(me) or str(me.id)
    return f"{name} @{me.username}" if me.username else name


@dataclass
class ChatItem:
    peer_id: int
    entity: object
    title: str
    kind: str
    username: str | None
    accounts: set = field(default_factory=set)
    public: bool = False

    @property
    def target(self):
        """What each account resolves on its own: access hashes differ between accounts."""
        if self.public and self.username:
            return "@" + self.username
        return utils.get_peer(self.entity)

    def matches(self, q):
        q = q.lower().lstrip("@")
        return q in self.title.lower() or (self.username and q in self.username.lower()) or q == str(self.peer_id)


def chat_kind(e):
    if isinstance(e, User):
        return "bot" if e.bot else "user"
    if isinstance(e, Channel):
        return "forum" if e.forum else "group" if e.megagroup else "channel"
    return "group"


def make_item(e, public=False):
    title = "Saved Messages" if isinstance(e, User) and e.is_self else utils.get_display_name(e)
    pid = utils.get_peer_id(e)
    return ChatItem(pid, e, title or str(pid), chat_kind(e), getattr(e, "username", None), public=public)


class LogWatch(logging.Handler):
    """Turns Telegram flood waits into a countdown and keeps warnings to print above the progress line."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.flood_until = 0.0
        self.warnings = collections.deque()

    def emit(self, record):
        msg = record.getMessage()
        if m := FLOOD_RE.search(msg):
            self.flood_until = max(self.flood_until, time.monotonic() + int(m.group(1)))
        elif record.levelno >= logging.WARNING and record.name == "dump":
            self.warnings.append(msg)


# --- accounts ---------------------------------------------------------------


async def add_account(accounts):
    client = make_client(new_session_name())
    print("Log in with a phone number (Ctrl+C to cancel).")
    try:
        await client.start()  # asks for the phone, the code and the two-step password
        me = await client.get_me()
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - any failure just cancels the login
        print(f"Login cancelled: {e or type(e).__name__}")
        await client.disconnect()
        client.session.delete()
        return
    if any(m.id == me.id for _, m in accounts):
        print(f"{account_name(me)} is already added.")
        await client.log_out()  # drop the duplicate session
        return
    accounts.append((client, me))
    print(f"Added {account_name(me)}.")


async def remove_account(accounts, n):
    if not 1 <= n <= len(accounts):
        print("No such account.")
        return
    client, me = accounts[n - 1]
    if (await ask(f"Log out {account_name(me)} and delete its session? [y/N] ")).lower() == "y":
        await client.log_out()  # ends the session on Telegram's side and deletes the file
        accounts.pop(n - 1)
        print("Logged out.")


async def change_out_dir():
    path = await ask(f"New output folder (Enter keeps {out_dir()}): ")
    if not path:
        return
    folder = Path(path.strip('"')).expanduser().resolve()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"Cannot use this folder: {e}")
        return
    save_settings({"TG_OUT": str(folder)})
    print(f"Saved. Dumps now go to {folder}")


async def options_menu():
    while True:
        on = saved_options()
        print("\nExtras (saved for next time):")
        for i, (name, about) in enumerate(OPTIONS.items(), 1):
            print(f"  {i}) [{'x' if name in on else ' '}] {name:<9} {about}")
        choice = await ask("Number to toggle, Enter to go back > ")
        if not choice:
            return
        if choice.isdigit() and 1 <= int(choice) <= len(OPTIONS):
            save_options(on ^ {list(OPTIONS)[int(choice) - 1]})


async def setup_menu(accounts):
    while True:
        print("\nAccounts:")
        for i, (_, me) in enumerate(accounts, 1):
            print(f"  {i}) {account_name(me)}")
        if not accounts:
            print("  (none yet)")
        print(f"Output folder: {out_dir()}")
        print(f"Extras: {', '.join(o for o in OPTIONS if o in saved_options()) or 'none'}")
        choice = await ask("[a] add account  [d N] remove  [f] output folder  [x] extras  [Enter] continue > ")
        if not choice:
            if accounts:
                return
            print("Add an account first.")
        elif choice.lower() == "a":
            await add_account(accounts)
        elif m := re.fullmatch(r"d\s*(\d+)", choice, re.IGNORECASE):
            await remove_account(accounts, int(m.group(1)))
        elif choice.lower() == "f":
            await change_out_dir()
        elif choice.lower() == "x":
            await options_menu()
        else:
            print("Type a, d and a number, f, x, or press Enter.")


# --- chats ------------------------------------------------------------------


async def load_chats(accounts):
    print("Loading your chats…", end="", flush=True)
    chats = {}
    for client, me in accounts:
        async for d in client.iter_dialogs():
            e = d.entity
            if isinstance(e, Chat) and e.migrated_to:
                continue  # the old group behind a supergroup
            item = chats.setdefault(utils.get_peer_id(e), make_item(e))
            item.accounts.add(me.id)
    print(f" {len(chats)} found.")
    return chats


async def search_public(client, q, chats):
    try:
        res = await client(functions.contacts.SearchRequest(q=q, limit=20))
    except errors.FloodWaitError as e:
        print(f"Public search is rate limited for {fmt_duration(e.seconds)}.")
        return []
    found = []
    for e in [*res.chats, *res.users]:
        pid = utils.get_peer_id(e)
        found.append(chats.get(pid) or chats.setdefault(pid, make_item(e, public=True)))
    return found


def print_chats(items, n_accounts):
    for i, c in enumerate(items[:SHOW], 1):
        user = f"  @{c.username}" if c.username else ""
        where = "public" if c.public else f"{len(c.accounts)}/{n_accounts} accounts" if n_accounts > 1 else ""
        print(f"  {i:>2}) {c.title}{user}  [{c.kind}]  {where}".rstrip())
    if len(items) > SHOW:
        print(f"      …and {len(items) - SHOW} more, type more of the name")


class ChatCompleter(Completer):
    """Lists matching chats under the cursor while the name is being typed; Tab fills one in."""

    def __init__(self, chats, n_accounts):
        self.chats = chats
        self.n_accounts = n_accounts

    def get_completions(self, document, complete_event):
        q = document.text_before_cursor.strip()
        if not q:
            return
        for c in self.chats.values():
            if c.matches(q):
                user = f"@{c.username}  " if c.username else ""
                where = f"  {len(c.accounts)}/{self.n_accounts} accounts" if self.n_accounts > 1 else ""
                yield Completion(
                    c.title,
                    start_position=-len(document.text_before_cursor),
                    display_meta=f"{user}[{c.kind}]{where}",
                )


def chat_prompt(chats, n_accounts):
    keys = KeyBindings()

    @keys.add("tab")
    def _(event):
        # matches can start mid-name, so fill in the first suggestion instead of a common prefix
        buf = event.current_buffer
        if buf.complete_state:
            buf.complete_next()
        else:
            buf.start_completion(select_first=True)

    return PromptSession(completer=ChatCompleter(chats, n_accounts), complete_while_typing=True, key_bindings=keys)


async def ask_chat(session):
    prompt = "\nSearch chat (name, @username or id; Tab completes, Enter lists all): "
    if session is None:
        return await ask(prompt)
    return (await session.prompt_async(prompt)).strip()


async def pick_chat(accounts, chats):
    tty = sys.stdin.isatty() and sys.stdout.isatty()  # piped input gets a plain prompt
    session = chat_prompt(chats, len(accounts)) if tty else None
    while True:
        q = await ask_chat(session)
        exact = [c for c in chats.values() if q and c.title.lower() == q.lower()]
        if len(exact) == 1:  # picked from the suggestions
            return exact[0]
        items = [c for c in chats.values() if not q or c.matches(q)]
        if not items and q:
            print("Not in your chats, searching public ones…")
            items = await search_public(accounts[0][0], q, chats)
        if not items:
            print("Nothing found.")
            continue
        print_chats(items, len(accounts))
        hint = "Number, [p] search public chats, or Enter to search again > " if q else "Number or Enter > "
        choice = await ask(hint)
        if choice.lower() == "p" and q:
            items = await search_public(accounts[0][0], q, chats)
            if not items:
                print("No public chats found.")
                continue
            print_chats(items, len(accounts))
            choice = await ask("Number or Enter > ")
        if choice.isdigit() and 1 <= int(choice) <= min(len(items), SHOW):
            return items[int(choice) - 1]


async def describe(item, clients):
    print(f"\n{item.title}  [{item.kind}]  id {item.peer_id}")
    total = None
    try:
        total = (await clients[0].get_messages(item.target, limit=0)).total
    except Exception:  # noqa: BLE001 - the count is only informative
        pass
    have = None
    db_path = DATA / f"{item.entity.id}.sqlite"
    if db_path.exists():
        with sqlite3.connect(db_path) as db:
            have = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    if have is None:
        if total is not None:
            print(f"~{total:,} messages")
        return
    if total is None:
        print(f"{have:,} already saved, only new messages will be fetched")
        return
    print(f"~{max(total - have, 0):,} new, only they will be fetched")
    print(f"~{total:,} messages in total, {have:,} already saved")


async def ask_filters():
    """Filters for this dump only; an empty answer means no filter."""
    print("Filters for this dump (Enter skips any of them):")
    while True:
        try:
            since = parse_date(await ask("  from date, YYYY-MM-DD: "))
            until = parse_date(await ask("  to date (inclusive), YYYY-MM-DD: "))
            from_user = await ask("  only from user (@username or id): ") or None
            kind = await ask(f"  only type ({', '.join(KINDS)}): ") or None
            return Scope(since, until, from_user, kind)
        except ValueError as e:
            print(f"  {e}, try again")


# --- progress ---------------------------------------------------------------


class ProgressLine:
    def __init__(self, stats, watch):
        self.stats, self.watch = stats, watch
        self.samples = collections.deque()
        self.frame = 0

    def render(self):
        s, now = self.stats, time.monotonic()
        self.frame += 1
        spin = SPINNER[self.frame % len(SPINNER)]
        phase = s.get("phase", "connecting")
        label = f"{s['label']}: " if s.get("label") else ""
        if phase != "downloading":
            text = {
                "connecting": "connecting accounts…",
                "planning": "reading chat info…",
                "exporting": "writing topic folders…",
                "done": "done",
            }.get(phase, phase)
            return f"{spin} {label}{text}"

        work_total, work_left = s.get("work_total", 0), s.get("work_left", 0)
        self.samples.append((now, work_total - work_left, s.get("fetched", 0)))
        while len(self.samples) > 2 and now - self.samples[0][0] > 120:
            self.samples.popleft()
        rate = msg_rate = 0.0  # id span/s for the ETA, messages/s to show
        if len(self.samples) > 1 and self.samples[-1][0] - self.samples[0][0] > 3:
            (t0, w0, m0), (t1, w1, m1) = self.samples[0], self.samples[-1]
            rate, msg_rate = (w1 - w0) / (t1 - t0), (m1 - m0) / (t1 - t0)

        share = 1 - work_left / work_total if work_total else 1
        bar = "█" * round(share * 20) + "░" * (20 - round(share * 20))
        eta = fmt_duration(work_left / rate) if rate > 0 and work_left else "…" if work_left else "0s"
        line = (
            f"{label}[{bar}] {share:4.0%}  {s.get('stored', 0):,} saved  +{s.get('fetched', 0):,}"
            f"  {msg_rate * 60:,.0f}/min  ETA {eta}"
        )
        if self.watch.flood_until > now:
            line += f"  {spin} rate limit {int(self.watch.flood_until - now) + 1}s"
        return line

    def draw(self):
        width = shutil.get_terminal_size().columns - 1
        while self.watch.warnings:
            print("\r" + " " * width + "\r! " + self.watch.warnings.popleft())
        print("\r" + self.render()[:width].ljust(width), end="", flush=True)


async def run_dump(item, clients, workers, options, scope):
    stats, watch = {}, LogWatch()
    logging.getLogger().addHandler(watch)
    line = ProgressLine(stats, watch)
    started = time.monotonic()
    task = asyncio.create_task(
        dump_chat(clients, item.target, workers=workers, stats=stats, options=options, scope=scope)
    )
    try:
        while not task.done():
            line.draw()
            await asyncio.wait({task}, timeout=0.2)
        line.draw()
        print()
        out = task.result()
        print(
            f"Done in {fmt_duration(time.monotonic() - started)}: +{stats.get('fetched', 0):,} new,"
            f" {stats.get('stored', 0):,} saved in total\n{out}"
        )
        return out
    finally:
        logging.getLogger().removeHandler(watch)
        task.cancel()


def open_folder(path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


# --- main -------------------------------------------------------------------


async def main():
    print("tg-chat-dump, interactive mode. Ctrl+C quits at any time; a stopped dump resumes next time.\n")
    ensure_api_keys()
    proxy = load_settings()["TG_PROXY"]
    print(f"Connecting{' via ' + proxy if proxy else ''}…")
    clients, problems = await open_accounts()
    for p in problems:
        print(f"! {p}")
    if problems:
        print("  (check TG_PROXY in .env if Telegram is blocked in your network)")
    accounts = [(c, await c.get_me()) for c in clients]
    try:
        await setup_menu(accounts)
        chats = await load_chats(accounts)
        while True:
            item = await pick_chat(accounts, chats)
            clients = [c for c, me in accounts if item.public or me.id in item.accounts]
            await describe(item, clients)
            n, options = len(clients), saved_options()
            extras = f", extras: {', '.join(o for o in OPTIONS if o in options)}" if options else ""
            answer = (await ask(f"Dump it with {n} account{'s' * (n > 1)}{extras}? [Y/n, f = filters] ")).lower()
            if answer == "n":
                continue
            scope = await ask_filters() if answer == "f" else Scope()
            try:
                out = await run_dump(item, clients, 3, options, scope)
            except ValueError as e:
                print(f"\nCannot dump: {e}")
                continue
            choice = (await ask("[o] open folder  [n] dump another chat  [Enter] quit > ")).lower()
            if choice == "o":
                open_folder(out)
                choice = (await ask("[n] dump another chat  [Enter] quit > ")).lower()
            if choice != "n":
                return
    finally:
        for c, _ in accounts:
            await c.disconnect()
