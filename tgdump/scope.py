"""Filters for a partial dump: dates, author, message type."""

import datetime
import hashlib
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


def parse_date(s):
    return datetime.date.fromisoformat(s) if s else None


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

    @staticmethod
    def day_start(d):
        return datetime.datetime.combine(d, datetime.time.min, tzinfo=datetime.UTC)


NO_SCOPE = Scope()  # the default: the whole chat
