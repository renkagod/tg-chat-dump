"""Interactive console mode: pick accounts and a chat, then watch the dump."""

import asyncio
import collections
import contextlib
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
from prompt_toolkit.formatted_text import ANSI
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from telethon import errors, functions, utils
from telethon.tl.types import Channel, Chat, User

from . import style
from .accounts import login, make_client, new_session_name, open_accounts
from .config import DATA, OPTIONS, ensure_api_keys, load_settings, out_dir, save_options, save_settings, saved_options
from .fetch import Takeouts, dump_chat, saved_by, saved_dumps
from .scope import KINDS, Scope, parse_date
from .style import fit, keys, kind, link, paint, visible_len
from .util import fmt_duration

log = logging.getLogger("dump")
FLOOD_RE = re.compile(r"Sleeping (?:early )?for (\d+)s")
LINK_RE = re.compile(
    r"@(?P<at>\w{4,})|(?:https?://)?(?:www\.)?(?:t|telegram)\.me/(?:s/)?"
    r"(?:c/(?P<c>\d+)|(?P<invite>\+[\w-]+|joinchat/[\w-]+)|(?P<name>\w+))(?:/\d+)*/?(?:\?\S*)?",
    re.IGNORECASE,
)
SETTINGS = ("/", "/s", "/settings")
QUIT = ("q", "/q", "quit", "exit")
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
SHOW = 15  # chats listed per search
PEEK = 100  # newest messages fetched to count what a repeat dump will add
MENU_STYLE = {
    "completion-menu.completion": "bg:#2b2d31 #dcdfe4",
    "completion-menu.meta.completion": "bg:#2b2d31 #dcdfe4",
    "completion-menu.completion.current": "bg:#3e4451 ansibrightblue bold",
    "completion-menu.meta.completion.current": "bg:#3e4451",
}
dumped = False  # whether a dump ran, for the goodbye line


async def ask(prompt=""):
    """input() without blocking the event loop, so the Telegram connections stay alive."""
    return (await asyncio.to_thread(input, prompt)).strip()


def account_name(me):
    name = utils.get_display_name(me) or str(me.id)
    return f"{name} {paint('@' + me.username, 'dim')}" if me.username else name


def ago(timestamp):
    seconds = time.time() - timestamp
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


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


# --- accounts and settings --------------------------------------------------


async def add_account(accounts):
    client = make_client(new_session_name())
    print("Log in to a Telegram account (Ctrl+C to cancel).")
    try:
        me = await login(client)
    except (Exception, KeyboardInterrupt) as e:  # noqa: BLE001 - any failure just cancels the login
        print(paint(f"Login cancelled: {e or type(e).__name__}", "red"))
        await client.disconnect()
        client.session.delete()
        return
    if any(m.id == me.id for _, m in accounts):
        print(account_name(me) + paint(" is already added.", "yellow"))
        await client.log_out()  # drop the duplicate session
        return
    accounts.append((client, me))
    print(paint("Added ", "green") + account_name(me))


async def remove_account(accounts, n):
    if not 1 <= n <= len(accounts):
        print(paint("No such account.", "red"))
        return
    client, me = accounts[n - 1]
    if (await ask(f"Log out {account_name(me)} and delete its session? {keys('[y/N]')} ")).lower() == "y":
        await client.log_out()  # ends the session on Telegram's side and deletes the file
        accounts.pop(n - 1)
        print(paint("Logged out.", "green"))


async def change_out_dir():
    path = await ask(f"New output folder (Enter keeps {out_dir()}): ")
    if not path:
        return
    folder = Path(path.strip('"')).expanduser().resolve()
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(paint(f"Cannot use this folder: {e}", "red"))
        return
    save_settings({"TG_OUT": str(folder)})
    print(paint("Saved.", "green") + f" Dumps now go to {link(folder)}")


async def options_menu():
    while True:
        on = saved_options()
        print("\n" + paint("Extras (saved for next time):", "bold"))
        for i, (name, about) in enumerate(OPTIONS.items(), 1):
            box = paint(f"[x] {name:<9}", "green") if name in on else paint("[ ]", "dim") + f" {name:<9}"
            print(f"  {paint(f'{i})', 'blue')} {box} {paint(about, 'dim')}")
        choice = await ask(keys("Number to toggle, Enter to go back > "))
        if not choice:
            return
        if choice.isdigit() and 1 <= int(choice) <= len(OPTIONS):
            save_options(on ^ {list(OPTIONS)[int(choice) - 1]})


def extras_text():
    extras = ", ".join(o for o in OPTIONS if o in saved_options())
    return paint(extras, "green") if extras else paint("none", "dim")


def print_status(accounts):
    """What the next dump will use, in two lines; the settings are one / away, as the search prompt says."""
    print(f"{paint('Accounts:', 'dim')} {', '.join(account_name(me) for _, me in accounts)}")
    print(f"{paint('Extras:', 'dim')} {extras_text()}   {paint('Output:', 'dim')} {link(out_dir())}")


async def setup_menu(accounts):
    while True:
        print("\n" + paint("Settings", "bold") + paint(" (saved for next time)", "dim"))
        print(paint("Accounts:", "dim"))
        for i, (_, me) in enumerate(accounts, 1):
            print(f"  {paint(f'{i})', 'blue')} {account_name(me)}")
        if not accounts:
            print(paint("  (none yet)", "dim"))
        print(f"{paint('Output folder:', 'dim')} {out_dir()}")
        print(f"{paint('Extras:', 'dim')} {extras_text()}")
        choice = await ask(keys("[a] add account  [d N] remove  [f] output folder  [x] extras  [Enter] back > "))
        if not choice:
            if accounts:
                return
            print(paint("Add an account first.", "yellow"))
        elif choice.lower() == "a":
            await add_account(accounts)
        elif m := re.fullmatch(r"d\s*(\d+)", choice, re.IGNORECASE):
            await remove_account(accounts, int(m.group(1)))
        elif choice.lower() == "f":
            await change_out_dir()
        elif choice.lower() == "x":
            await options_menu()
        else:
            print(paint("Type a, d and a number, f, x, or press Enter.", "yellow"))


async def first_account(accounts):
    """The first run has no accounts: log one in right away. False when the user gives up."""
    print(paint("No accounts yet. Log in to the Telegram account whose chats you want to dump.", "bold"))
    while not accounts:
        await add_account(accounts)
        if not accounts and (await ask(keys("[Enter] try again  [q] quit > "))).lower() == "q":
            return False
    return True


# --- chats ------------------------------------------------------------------


async def load_chats(accounts):
    print(paint("Loading your chats…", "dim"), end="", flush=True)
    chats = {}
    dialogs = await asyncio.gather(*(client.get_dialogs() for client, _ in accounts))
    for (_, me), found in zip(accounts, dialogs, strict=True):
        for d in found:
            e = d.entity
            if isinstance(e, Chat) and e.migrated_to:
                continue  # the old group behind a supergroup
            item = chats.setdefault(utils.get_peer_id(e), make_item(e))
            item.accounts.add(me.id)
    print(f" {paint(len(chats), 'bold')} {paint('found.', 'dim')}")
    return chats


async def search_public(client, q, chats):
    try:
        res = await client(functions.contacts.SearchRequest(q=q, limit=20))
    except errors.FloodWaitError as e:
        print(paint(f"Public search is rate limited for {fmt_duration(e.seconds)}.", "yellow"))
        return []
    found = []
    for e in [*res.chats, *res.users]:
        pid = utils.get_peer_id(e)
        found.append(chats.get(pid) or chats.setdefault(pid, make_item(e, public=True)))
    return found


async def from_link(q, client, chats):
    """The chat a t.me link or an @username names. None when q is neither, False when it cannot be opened."""
    m = LINK_RE.fullmatch(q)
    if not m:
        return None
    if m.group("c"):  # t.me/c/<id>/<message>: a private chat, only reachable through your own chats
        item = chats.get(int(f"-100{m.group('c')}"))
        if not item:
            print(paint("This private chat is not in your chats; join it first.", "yellow"))
        return item or False
    name = m.group("at") or m.group("name")
    if name:
        for c in chats.values():
            if c.username and c.username.lower() == name.lower():
                return c
    try:
        e = await client.get_entity(name or q)
    except (ValueError, TypeError, errors.RPCError) as e:
        print(paint(f"Cannot open {q}: {e}", "yellow"))
        return False
    pid = utils.get_peer_id(e)
    return chats.get(pid) or chats.setdefault(pid, make_item(e, public=True))


def seen_by(c, n_accounts):
    """How many accounts can dump the chat: green when all of them, yellow when only some."""
    if c.public:
        return paint("public", "cyan")
    if n_accounts < 2:
        return ""
    return paint(f"{len(c.accounts)}/{n_accounts} accounts", "green" if len(c.accounts) == n_accounts else "yellow")


def print_chats(items, n_accounts):
    for i, c in enumerate(items[:SHOW], 1):
        user = "  " + paint(f"@{c.username}", "dim") if c.username else ""
        num, title = paint(f"{i:>2})", "blue"), paint(c.title, "bold")
        print(f"  {num} {title}{user}  {kind(c.kind)}  {seen_by(c, n_accounts)}".rstrip())
    if len(items) > SHOW:
        print(paint(f"      …and {len(items) - SHOW} more, type more of the name", "dim"))


def dumped_items(chats):
    """Chats dumped before, latest first, as (item, last run time, saved messages)."""
    by_id = {}
    for c in chats.values():
        by_id.setdefault(c.entity.id, c)
    return [(by_id[i], when, rows) for i, when, rows in saved_dumps() if i in by_id]


def parse_picks(choice, n):
    """Numbers picked from a list of n: "3", "2 5 7", "4-6". Digits run together are one number, never several.

    None when the answer is not numbers at all, [] when some number is not on the list.
    """
    if not re.fullmatch(r"[\d\s,-]+", choice.strip()):
        return None
    picks = []
    for part in filter(None, re.split(r"[\s,]+", choice.strip())):
        if m := re.fullmatch(r"(\d+)-(\d+)", part):
            a, b = sorted((int(m.group(1)), int(m.group(2))))
            picks += range(a, b + 1)
        elif part.isdigit():
            picks.append(int(part))
        else:
            return []
    if not picks or not all(1 <= p <= n for p in picks):
        return []
    return list(dict.fromkeys(picks))


def print_dumps(dumps):
    print(paint("Your dumps:", "bold"))
    for i, (c, when, rows) in enumerate(dumps[:SHOW], 1):
        num, title = paint(f"{i:>2})", "blue"), paint(c.title, "bold")
        print(f"  {num} {title}  {kind(c.kind)}  {paint(f'{rows:,} saved · {ago(when)}', 'dim')}")
    if len(dumps) > SHOW:
        print(paint(f"      …and {len(dumps) - SHOW} more, find them by name", "dim"))


class ChatCompleter(Completer):
    """Lists matching chats under the cursor while the name is being typed; Tab fills one in."""

    def __init__(self, chats, n_accounts):
        self.chats = chats
        self.n_accounts = n_accounts

    def get_completions(self, document, complete_event):
        q = document.text_before_cursor.strip()
        if not q or q.startswith("/") or "t.me/" in q.lower():
            return
        for c in self.chats.values():
            if c.matches(q):
                user = paint(f"@{c.username}", "dim") + "  " if c.username else ""
                yield Completion(
                    c.title,
                    start_position=-len(document.text_before_cursor),
                    display_meta=ANSI(f"{user}{kind(c.kind)}  {seen_by(c, self.n_accounts)}".rstrip()),
                )


def chat_prompt(chats, n_accounts):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None  # piped input gets a plain prompt
    bindings = KeyBindings()

    @bindings.add("tab")
    def _(event):
        # matches can start mid-name, so fill in the first suggestion instead of a common prefix
        buf = event.current_buffer
        if buf.complete_state:
            buf.complete_next()
        else:
            buf.start_completion(select_first=True)

    return PromptSession(
        completer=ChatCompleter(chats, n_accounts),
        complete_while_typing=True,
        key_bindings=bindings,
        style=Style.from_dict(MENU_STYLE) if style.enabled else None,
    )


async def ask_chat(session):
    hint = (
        f"{paint('[Enter]', 'blue')} {paint('your dumps', 'dim')}   "
        f"{paint('[/]', 'blue')} {paint('settings: accounts, extras, output folder', 'dim')}   "
        f"{paint('[q]', 'blue')} {paint('quit', 'dim')}"
    )
    about = paint(" (name, @username, id or t.me link):", "dim")
    prompt = f"\n{hint}\n{paint('Search chat', 'bold')}{about} "
    if session is None:
        return await ask(prompt)
    return (await session.prompt_async(ANSI(prompt))).strip()


async def pick_chat(accounts, chats, session):
    """A ChatItem, SETTINGS, QUIT, or ("update", items) to update those dumps."""
    client, q = accounts[0][0], None
    while True:
        if q is None:
            q = await ask_chat(session)
        if q.lower() in SETTINGS:
            return SETTINGS
        if q.lower() in QUIT:
            return QUIT
        if not q and (dumps := dumped_items(chats)):
            print_dumps(dumps)
            hint = f"Number or several (2 5 7, 4-6), [u] update all {len(dumps)}, a name, or [q] quit > "
            choice = await ask(keys(hint))
            if choice.lower() in QUIT:
                return QUIT
            if choice.lower() == "u":
                return "update", [c for c, _, _ in dumps]
            picks = parse_picks(choice, min(len(dumps), SHOW))
            if picks and len(picks) == 1:
                return dumps[picks[0] - 1][0]
            if picks:
                return "update", [dumps[i - 1][0] for i in picks]
            if picks == []:  # numbers, but not all of them on the list
                print(paint(f"Pick numbers from 1 to {min(len(dumps), SHOW)}.", "yellow"))
                continue
            q = choice or None
            continue
        exact = [c for c in chats.values() if q and c.title.lower() == q.lower()]
        if len(exact) == 1:  # picked from the suggestions
            return exact[0]
        items = [c for c in chats.values() if not q or c.matches(q)]
        if not items and q:
            found = await from_link(q, client, chats)
            if found:
                return found
            if found is False:
                q = None
                continue
            print(paint("Not in your chats, searching public ones…", "dim"))
            items = await search_public(client, q, chats)
        if not items:
            print(paint("Nothing found.", "yellow"))
            q = None
            continue
        print_chats(items, len(accounts))
        hint = "Number, [p] search public chats, or type to search again > " if q else "Number or Enter > "
        choice = await ask(keys(hint))
        if choice.lower() == "p" and q:
            items = await search_public(client, q, chats)
            if not items:
                print(paint("No public chats found.", "yellow"))
                q = None
                continue
            print_chats(items, len(accounts))
            choice = await ask(keys("Number or Enter > "))
        if choice.isdigit() and 1 <= int(choice) <= min(len(items), SHOW):
            return items[int(choice) - 1]
        q = None if choice.isdigit() or choice.lower() == "p" else choice or None


async def count_new(client, target, db):
    """Messages newer than the last saved one, and Telegram's total for the chat.

    Telegram's total drops when messages are deleted while the database keeps them, so the
    difference of the two undercounts. Instead the newest messages are fetched: up to PEEK
    the count is exact, beyond that it is estimated from how densely the saved ids lie.
    """
    top, first = db.execute("SELECT MAX(id), MIN(id) FROM messages").fetchone()
    newest = await client.get_messages(target, limit=PEEK, min_id=top or 0)
    if not top:  # nothing saved yet
        return newest.total, newest.total, False
    if len(newest) < PEEK:
        return len(newest), newest.total, True
    window = min(5000, top - first + 1)
    near = db.execute("SELECT COUNT(*) FROM messages WHERE id > ?", (top - window,)).fetchone()[0]
    return round((newest[0].id - top) * near / window), newest.total, False


async def pick_account(item, accounts):
    """Private chats and basic groups are dumped by one account: each account has its own copy of them."""
    counts, saved = [], []
    db_path = DATA / f"{item.entity.id}.sqlite"
    with contextlib.closing(sqlite3.connect(db_path)) if db_path.exists() else contextlib.nullcontext() as db:
        for c, _ in accounts:
            try:
                counts.append((await c.get_messages(item.target, limit=0)).total)
                saved.append(bool(db) and await saved_by(c, item.target, db))
            except (ValueError, TypeError, errors.RPCError):
                counts.append(0)
                saved.append(False)
    has_copy = any(saved) and not all(saved)  # all True means nothing is saved yet
    order = sorted(range(len(accounts)), key=lambda i: (-(has_copy and saved[i]), -counts[i]))
    print(paint("Every account has its own copy of this chat:", "dim"))
    for n, i in enumerate(order, 1):
        count = f"{counts[i]:,} message{'s' * (counts[i] != 1)}"
        mark = paint("  saved by this one, only new messages are fetched", "green") if has_copy and saved[i] else ""
        print(f"  {paint(f'{n:>2})', 'blue')} {account_name(accounts[i][1])}  {count}{mark}")
    while True:
        choice = await ask(keys("Account number (Enter = 1) > "))
        if not choice:
            return accounts[order[0]]
        if choice.isdigit() and 1 <= int(choice) <= len(order):
            return accounts[order[int(choice) - 1]]
        print(paint("  No such account, try again", "yellow"))


async def describe(item, client):
    db_path = DATA / f"{item.entity.id}.sqlite"
    if not db_path.exists():
        with contextlib.suppress(Exception):  # the count is only informative
            print(f"~{(await client.get_messages(item.target, limit=0)).total:,} messages")
        return
    with contextlib.closing(sqlite3.connect(db_path)) as db:
        have = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        unfinished = db.execute("SELECT COUNT(*) FROM tasks WHERE done=0").fetchone()[0]
        try:
            if not isinstance(item.entity, Channel) and not await saved_by(client, item.target, db):
                total = (await client.get_messages(item.target, limit=0)).total
                print(paint(f"~{total:,} messages", "green", "bold"))
                print(paint(f"The {have:,} saved ones came from another account, the chat is fetched again", "dim"))
                return
            new, total, exact = await count_new(client, item.target, db)
        except Exception:  # noqa: BLE001
            print(f"{have:,} already saved, only new messages will be fetched")
            return
    if unfinished:  # an interrupted dump also has older ranges left
        print(
            paint(f"~{max(total - have, new):,} to fetch", "green", "bold")
            + paint(", the stopped dump resumes", "green")
        )
    else:
        count = f"{new:,}" if exact else f"~{new:,}"
        print(paint(f"{count} new", "green", "bold") + paint(", only they will be fetched", "green"))
    print(paint(f"~{total:,} messages in the chat, {have:,} already saved", "dim"))


async def ask_filters():
    """Filters for this dump only; an empty answer means no filter."""
    print(paint("Filters for this dump", "bold") + paint(" (Enter skips any of them):", "dim"))
    print(paint("  dates: 2026-03-14, a month 2026-03, a year 2026, or back from today 7d, 2w, 3m", "dim"))
    while True:
        try:
            since = parse_date(await ask("  from date: "))
            until = parse_date(await ask("  to date, inclusive: "), end=True)
            from_user = await ask("  only from user (@username or id): ") or None
            kind = await ask(f"  only type ({', '.join(KINDS)}): ") or None
            return Scope(since, until, from_user, kind)
        except ValueError as e:
            print(paint(f"  {e}, try again", "yellow"))


async def confirm_takeout(takeouts, accounts):
    """Open export mode before the dump; when Telegram wants it allowed first, wait for that instead of going slow."""
    clients = [c for c, _ in accounts if c not in takeouts.clients and c not in takeouts.declined]
    if not clients:
        return
    print(paint("Starting Telegram's export mode…", "dim"))
    await takeouts.enter([(c, None) for c in clients])
    while waiting := takeouts.waiting(clients):
        names = ", ".join(account_name(me) for c, me in accounts if c in waiting)
        wait = fmt_duration(max(takeouts.delayed[c] for c in waiting))
        print(paint(f"Telegram wants the data export allowed first ({names}).", "yellow"))
        print("  Open Telegram with this account: the chat 'Telegram' has a data export request, tap Allow.")
        print(paint(f"  Otherwise it opens by itself in {wait}; until then a dump is about 10x slower.", "dim"))
        choice = await ask(keys("[Enter] allowed, check again  [s] go on slower > "))
        if choice.lower() == "s":
            takeouts.decline(waiting)
            return
        await takeouts.enter([(c, None) for c in waiting])
    if all(c in takeouts.clients for c in clients):
        print(paint("Export mode is on.", "green"))


# --- progress ---------------------------------------------------------------


class ProgressLine:
    def __init__(self, stats, watch):
        self.stats, self.watch = stats, watch
        self.samples = collections.deque()
        self.frame = 0

    def render(self, width=120):
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
            return f"{paint(spin, 'blue')} {label}{paint(text, 'dim')}"

        work_total, work_left = s.get("work_total", 0), s.get("work_left", 0)
        self.samples.append((now, work_total - work_left, s.get("fetched", 0)))
        while len(self.samples) > 2 and now - self.samples[0][0] > 120:
            self.samples.popleft()
        rate = msg_rate = 0.0  # id span/s for the ETA, messages/s to show
        if len(self.samples) > 1 and self.samples[-1][0] - self.samples[0][0] > 3:
            (t0, w0, m0), (t1, w1, m1) = self.samples[0], self.samples[-1]
            rate, msg_rate = (w1 - w0) / (t1 - t0), (m1 - m0) / (t1 - t0)

        share = 1 - work_left / work_total if work_total else 1
        eta = fmt_duration(work_left / rate) if rate > 0 and work_left else "…" if work_left else "0s"
        stored, fetched = s.get("stored", 0), s.get("fetched", 0)
        tail = f" {paint(f'{share:4.0%}', 'bold')}  {stored:,} saved"
        if fetched != stored:  # on a repeat run, how many of them are new
            tail += "  " + paint(f"+{fetched:,}", "dim")
        tail += "  " + paint(f"{msg_rate * 60:,.0f}/min", "green", "bold")
        if s.get("no_takeout"):  # why it is slow
            tail += "  " + paint("no takeout", "yellow")
        if self.watch.flood_until > now:  # the ETA means little while Telegram makes us wait
            tail += "  " + paint(f"{spin} rate limit {int(self.watch.flood_until - now) + 1}s", "yellow")
        else:
            tail += f"  {paint('ETA', 'dim')} {eta}"
        # the bar gives up width first, so the numbers stay visible in a narrow window
        size = max(8, min(20, width - visible_len(label + tail) - 2))
        done = round(share * size)
        bar = paint("[" + "█" * done, "blue") + paint("░" * (size - done) + "]", "dim")
        return label + bar + tail

    def draw(self):
        width = shutil.get_terminal_size().columns - 1
        while self.watch.warnings:
            print("\r" + " " * width + "\r" + paint("! " + self.watch.warnings.popleft(), "yellow"))
        print("\r" + fit(self.render(width), width), end="", flush=True)

    def clear(self):
        print("\r" + " " * (shutil.get_terminal_size().columns - 1) + "\r", end="")


async def run_dump(item, clients, workers, options, scope, takeouts):
    global dumped
    dumped = True
    stats, watch = {}, LogWatch()
    logging.getLogger().addHandler(watch)
    line = ProgressLine(stats, watch)
    started = time.monotonic()
    task = asyncio.create_task(
        dump_chat(clients, item.target, workers=workers, stats=stats, options=options, scope=scope, takeouts=takeouts)
    )
    try:
        while not task.done():
            line.draw()
            await asyncio.wait({task}, timeout=0.2)
        line.draw()
        if task.exception():
            print()  # keep the last progress line above the error
        out = task.result()
        line.clear()  # the line below says it all
        took = fmt_duration(time.monotonic() - started)
        if stats.get("local"):
            what = f"{stats.get('stored', 0):,} messages, filtered from the saved dump"
        else:
            what = f"+{stats.get('fetched', 0):,} new, {stats.get('stored', 0):,} saved in total"
        print(f"{paint('✓ Done in', 'green')} {paint(took, 'green', 'bold')}{paint(':', 'green')} {what}")
        reader = out / "index.html"
        print(f"{link(reader) if reader.exists() else link(out)}")
        return out
    finally:
        logging.getLogger().removeHandler(watch)
        task.cancel()


async def safe_dump(item, clients, options, scope, takeouts):
    """run_dump that reports a failure and returns None, so the session goes on with the next chat."""
    try:
        return await run_dump(item, clients, 3, options, scope, takeouts)
    except ValueError as e:
        print("\n" + paint(f"Cannot dump: {e}", "red"))
    except Exception as e:  # noqa: BLE001 - a network error or a bug must not end the session
        log.exception("Dump failed")
        print("\n" + paint(f"The dump stopped: {e or type(e).__name__}", "red"))
        print(paint(f"  Details are in {DATA / 'dump.log'}; the next run picks up where this one stopped.", "dim"))
    return None


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


def seen_accounts(item, accounts):
    return [(c, me) for c, me in accounts if item.public or me.id in item.accounts]


async def update_all(items, accounts, takeouts):
    """Fetch the new messages of every chat dumped before, one after another."""
    options = saved_options()
    if "takeout" in options:
        await confirm_takeout(takeouts, accounts)
    failed = 0
    for n, item in enumerate(items, 1):
        print(f"\n{paint(f'{n}/{len(items)}', 'dim')} {paint(item.title, 'bold')}  {kind(item.kind)}")
        clients = [c for c, _ in seen_accounts(item, accounts)]
        if not clients or not await safe_dump(item, clients, options, Scope(), takeouts):
            failed += 1
    tail = paint(f", {failed} failed", "yellow") if failed else ""
    print(f"\n{paint('✓ Updated', 'green')} {paint(len(items) - failed, 'bold')} of {len(items)} chats{tail}")


async def after_dump(out):
    """What to do once a dump is done; True to dump another chat."""
    reader = out / "index.html"
    menu = "[o] open in the browser  [f] open the folder" if reader.exists() else "[o] open the folder"
    while True:
        choice = (await ask(keys(f"{menu}  [n] dump another chat  [Enter] quit > "))).lower()
        if choice == "o":
            open_path(reader if reader.exists() else out)
        elif choice == "f" and reader.exists():
            open_path(out)
        else:
            return choice == "n"


# --- main -------------------------------------------------------------------


async def main():
    style.enable()
    about = ", interactive mode. Ctrl+C quits at any time; a stopped dump resumes next time."
    print(paint("tg-chat-dump", "blue", "bold") + paint(about, "dim") + "\n")
    ensure_api_keys()
    proxy = load_settings()["TG_PROXY"]
    print(paint(f"Connecting{' via ' + proxy if proxy else ''}…", "dim"))
    clients, problems = await open_accounts()
    for p in problems:
        print(paint(f"! {p}", "yellow"))
    if any("frozen by Telegram" not in p for p in problems):
        print(paint("  (check TG_PROXY in .env if Telegram is blocked in your network)", "dim"))
    accounts = list(zip(clients, await asyncio.gather(*(c.get_me() for c in clients)), strict=True))
    try:
        if not accounts and not await first_account(accounts):
            return
        print_status(accounts)
        chats = await load_chats(accounts)
        session = chat_prompt(chats, len(accounts))
        # export mode stays open between chats, every account leaves it once on quit
        async with Takeouts() as takeouts:
            while True:
                item = await pick_chat(accounts, chats, session)
                if item == QUIT:
                    return
                if item == SETTINGS:
                    before = {me.id for _, me in accounts}
                    await setup_menu(accounts)
                    if {me.id for _, me in accounts} != before:  # other accounts see other chats
                        chats = await load_chats(accounts)
                        session = chat_prompt(chats, len(accounts))
                    print_status(accounts)
                    continue
                if isinstance(item, tuple):
                    await update_all(item[1], accounts, takeouts)
                    continue
                seen = seen_accounts(item, accounts)
                print(f"\n{paint(item.title, 'bold')}  {kind(item.kind)}  {paint(f'id {item.peer_id}', 'dim')}")
                if not isinstance(item.entity, Channel) and len(seen) > 1:
                    seen = [await pick_account(item, seen)]
                clients = [c for c, _ in seen]
                await describe(item, clients[0])
                n, options = len(clients), saved_options()
                extras = f", extras: {paint(', '.join(o for o in OPTIONS if o in options), 'green')}" if options else ""
                who = account_name(seen[0][1]) if n == 1 else paint(f"{n} accounts", "bold")
                answer = (await ask(f"Dump it with {who}{extras}? {keys('[Y/n, f = filters]')} ")).lower()
                if answer == "n":
                    continue
                scope = await ask_filters() if answer == "f" else Scope()
                if "takeout" in options:
                    await confirm_takeout(takeouts, seen)
                out = await safe_dump(item, clients, options, scope, takeouts)
                if out and not await after_dump(out):
                    return
    finally:
        for c, _ in accounts:
            await c.disconnect()
