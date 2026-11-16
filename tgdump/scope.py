"""Filters for a partial dump: dates, author, message type."""

import dataclasses
import datetime
import hashlib
import json
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


# The same types as the saved copy tells them: `media` is the kind of media, extra.file.kind the kind of file
# (a file without one is a plain document), extra.pinned the pin mark that every run brings up to date.
FILE_KIND = "json_extract(extra, '$.file.kind')"
SAVED_KINDS = {
    "photos": "media = 'MessageMediaPhoto'",
    "videos": f"{FILE_KIND} = 'video'",
    "media": f"(media = 'MessageMediaPhoto' OR {FILE_KIND} = 'video')",
    "docs": f"(json_extract(extra, '$.file.mime') IS NOT NULL AND {FILE_KIND} IS NULL)",
    "links": "has_link(text, media, extra)",
    "gifs": f"{FILE_KIND} = 'gif'",
    "voice": f"{FILE_KIND} = 'voice'",
    "music": f"{FILE_KIND} = 'audio'",
    "round": f"{FILE_KIND} = 'round'",
    "pinned": "json_extract(extra, '$.pinned')",
    "geo": "media IN ('MessageMediaGeo', 'MessageMediaGeoLive', 'MessageMediaVenue')",
    "contacts": "media = 'MessageMediaContact'",
}
# Telegram turns a bare name into a link when it ends in a real top-level domain: every country's, and the
# generic ones people meet. So readme.md and main.py are links to it, and notes.txt is not.
TLDS = (
    "ac ad ae af ag ai al am ao aq ar as at au aw ax az ba bb bd be bf bg bh bi bj bm bn bo br bs bt bw by bz ca cc "
    "cd cf cg ch ci ck cl cm cn co cr cu cv cw cx cy cz de dj dk dm do dz ec ee eg er es et eu fi fj fk fm fo fr ga "
    "gd ge gf gg gh gi gl gm gn gp gq gr gs gt gu gw gy hk hm hn hr ht hu id ie il im in io iq ir is it je jm jo jp "
    "ke kg kh ki km kn kp kr kw ky kz la lb lc li lk lr ls lt lu lv ly ma mc md me mg mh mk ml mm mn mo mp mq mr ms "
    "mt mu mv mw mx my mz na nc ne nf ng ni nl no np nr nu nz om pa pe pf pg ph pk pl pm pn pr ps pt pw py qa re ro "
    "rs ru rw sa sb sc sd se sg sh si sk sl sm sn so sr ss st su sv sx sy sz tc td tf tg th tj tk tl tm tn to tr tt "
    "tv tw tz ua ug uk us uy uz va vc ve vg vi vn vu wf ws ye yt za zm zw "
    "com org net edu gov mil int info biz name pro xyz top site online club shop store app dev tech cloud host link "
    "live life world today news blog wiki space website fun icu vip work tools team money rip gay art design agency "
    "digital email group media network systems solutions services software games game chat social video stream "
    "zone one plus page run codes center company finance expert guru ninja rocks cool best win bet mobi asia tel "
    "travel jobs cat aero coop museum cash pay bot bio sbs cfd lol wtf fyi moe pet pub bar cafe city land farm "
    "academy school studio support help market trade exchange capital fund bank credit loan gold tips click "
    "download press review reviews express global international center pics photo photos pictures audio movie "
    "film show tube radio fm stream gg io "
    "рф рус укр бел дети москва онлайн сайт орг ком"
).split()
# what the app shows as a link in plain text: an address with a scheme, a bare domain name, an IPv4 address,
# an e-mail address
LINK_RE = re.compile(
    r"\b(?:https?|ftp|tg|ton|tonsite)://\S|\bwww\.\S"
    rf"|(?<![\w@.-])(?:[^\W_][\w-]*\.)+(?:{'|'.join(sorted(set(TLDS)))})(?![\w-])"
    r"|(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.]*\.\d)"
    r"|[\w.+-]+@[\w-]+\.[\w.-]*[^\W\d_]",
    re.IGNORECASE,
)


def has_link(text, media, extra):
    """Whether a saved message has a link: an address in its text, a link under a word, or a link preview."""
    if media == "MessageMediaWebPage" or (text and LINK_RE.search(text)):
        return True
    if not extra or '"links"' not in extra:
        return False
    # mentions of people without a username are kept among the hidden links, but are not links
    return any(not x["url"].startswith("tg://") for x in json.loads(extra).get("links", ()))


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


LISTED_TOPICS = 5  # how many topic ids a folder name spells out


@dataclass(frozen=True)
class Scope:
    """A filtered dump gets its own database and output folder, so it never mixes with the full one."""

    since: datetime.date | None = None
    until: datetime.date | None = None  # inclusive
    from_user: str | None = None
    kind: str | None = None
    topics: tuple = ()  # the forum topics the filters are applied in; see within()

    def __post_init__(self):
        if self.kind and self.kind not in KINDS:
            raise ValueError(f"unknown type {self.kind!r}; choose from {', '.join(KINDS)}")
        if self.since and self.until and self.since > self.until:
            raise ValueError("the start date is after the end date")

    def __bool__(self):
        return any((self.since, self.until, self.from_user, self.kind))

    def within(self, topics):
        """These filters inside some forum topics. Without filters the topics are a part of the whole dump."""
        return dataclasses.replace(self, topics=tuple(sorted(set(topics)))) if self and topics else self

    def tag(self):
        parts = []
        if self.topics:
            ids = "-".join(map(str, self.topics))
            if len(self.topics) > LISTED_TOPICS:  # a folder name has to stay short, and still be the only one
                first = "-".join(map(str, self.topics[:LISTED_TOPICS]))
                ids = (
                    f"{first}-and-{len(self.topics) - LISTED_TOPICS}-more-{hashlib.sha1(ids.encode()).hexdigest()[:6]}"
                )
            parts.append(f"topics-{ids}")
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
        """The filters as a WHERE clause over the messages table; `links` needs has_link as an SQL function."""
        where, params = ["1"], []
        if self.kind:
            where.append(SAVED_KINDS[self.kind])
        if self.since:
            where.append("date >= ?")
            params.append(self.day_start(self.since).isoformat())
        if self.until:
            where.append("date < ?")
            params.append(self.day_start(self.until + datetime.timedelta(1)).isoformat())
        if sender_id is not None:
            where.append("sender_id = ?")
            params.append(sender_id)
        if self.topics:
            where.append(f"topic_id IN ({','.join('?' * len(self.topics))})")
            params += self.topics
        return " AND ".join(where), params

    @staticmethod
    def day_start(d):
        return datetime.datetime.combine(d, datetime.time.min, tzinfo=datetime.UTC)


NO_SCOPE = Scope()  # the default: the whole chat
