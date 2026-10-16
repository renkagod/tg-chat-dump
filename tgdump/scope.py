"""Filters for a partial dump: dates, author, message type."""

import datetime
import hashlib
import re
from dataclasses import dataclass

from telethon import types

from .util import parse_chat, slug

# Message types for --type; each maps to a server-side search filter.
KINDS = {
    "photos": types.InputMessagesFilterPhotos,
    "videos": types.InputMessagesFilterVideo,
    "media": types.InputMessagesFilterPhotoVideo,
    "docs": types.InputMessagesFilterDocument,
    "links": types.InputMessagesFilterUrl,
    "gifs": types.InputMessagesFilterGif,
    "voice": types.InputMessagesFilterVoice,
    "music": types.InputMessagesFilterMusic,
    "round": types.InputMessagesFilterRoundVideo,
    "pinned": types.InputMessagesFilterPinned,
    "geo": types.InputMessagesFilterGeo,
    "contacts": types.InputMessagesFilterContacts,
}


RELATIVE_RE = re.compile(r"(\d+)\s*([dwm])")
DAYS = {"d": 1, "w": 7, "m": 30}


def parse_date(s, end=False, today=None):
    """YYYY-MM-DD, YYYY-MM, YYYY, or days back: 7d, 2w, 3m. A month or year starts on its first day,
    or, with end=True (an inclusive end date), ends on its last day."""
    s = (s or "").strip().lower()
    if not s:
        return None
    if m := RELATIVE_RE.fullmatch(s):
        return (today or datetime.date.today()) - datetime.timedelta(int(m.group(1)) * DAYS[m.group(2)])
    try:
        if re.fullmatch(r"\d{4}", s):
            return datetime.date(int(s), 12, 31) if end else datetime.date(int(s), 1, 1)
        if re.fullmatch(r"\d{4}-\d{1,2}", s):
            year, month = map(int, s.split("-"))
            first = datetime.date(year, month, 1)
            if not end:
                return first
            return (first + datetime.timedelta(32)).replace(day=1) - datetime.timedelta(1)
        return datetime.date.fromisoformat(s)
    except ValueError:
        raise ValueError(f"cannot read the date {s!r}; use YYYY-MM-DD, YYYY-MM, YYYY or 7d, 2w, 3m") from None


@dataclass(frozen=True)
class Scope:
    """A filtered dump gets its own database and output folder, so it never mixes with the full one."""

    since: datetime.date | None = None
    until: datetime.date | None = None  # inclusive
    from_user: str | None = None
    kind: str | None = None

    def __post_init__(self):
        if self.kind and self.kind not in KINDS:
            raise ValueError(f"unknown type {self.kind!r}; choose from {', '.join(KINDS)}")
        if self.since and self.until and self.since > self.until:
            raise ValueError("the start date is after the end date")

    def __bool__(self):
        return any((self.since, self.until, self.from_user, self.kind))

    def tag(self):
        parts = []
        if self.since:
            parts.append(f"since-{self.since}")
        if self.until:
            parts.append(f"until-{self.until}")
        if self.from_user:
            parts.append(f"from-{slug(self.from_user.lstrip('@'))}")
        if self.kind:
            parts.append(self.kind)
        return "_".join(parts)

    def db_suffix(self):
        return "_" + hashlib.sha1(self.tag().encode()).hexdigest()[:8] if self else ""

    def iter_kwargs(self):
        kw = {}
        if self.from_user:
            kw["from_user"] = parse_chat(self.from_user)
        if self.kind:
            kw["filter"] = KINDS[self.kind]
        return kw

    def sql(self, sender_id=None):
        """The dates and author as a WHERE clause over the messages table; `kind` has no local equivalent."""
        where, params = ["1"], []
        if self.since:
            where.append("date >= ?")
            params.append(self.day_start(self.since).isoformat())
        if self.until:
            where.append("date < ?")
            params.append(self.day_start(self.until + datetime.timedelta(1)).isoformat())
        if sender_id is not None:
            where.append("sender_id = ?")
            params.append(sender_id)
        return " AND ".join(where), params

    @staticmethod
    def day_start(d):
        return datetime.datetime.combine(d, datetime.time.min, tzinfo=datetime.UTC)


NO_SCOPE = Scope()  # the default: the whole chat
