"""Telegram Desktop's export format, result.json: a dump written in it, and its messages read (see imports.py)."""

import collections
import datetime
import json
import re
from pathlib import Path

from .store import COLUMN_NAMES, get_chat
from .util import topic_folder

NOT_INCLUDED = "(File not included. Change data exporting settings to download.)"
# our kind of chat -> Telegram Desktop's, for a chat with a username and without one
CHAT_TYPES = {
    "user": ("personal_chat", "personal_chat"),
    "bot": ("bot_chat", "bot_chat"),
    "group": ("public_supergroup", "private_supergroup"),
    "forum": ("public_supergroup", "private_supergroup"),
    "channel": ("public_channel", "private_channel"),
}
KINDS = {"personal_chat": "user", "bot_chat": "bot", "saved_messages": "user"}  # the rest by the words in the type
# extra.file.kind <-> media_type
MEDIA_TYPES = {
    "sticker": "sticker",
    "video": "video_file",
    "voice": "voice_message",
    "round": "video_message",
    "gif": "animation",
    "audio": "audio_file",
}
FILE_KINDS = {v: k for k, v in MEDIA_TYPES.items()}
# the service messages both sides name; any other keeps its name in the other side's spelling
ACTIONS = {
    "ChatCreate": "create_group",
    "ChannelCreate": "create_channel",
    "ChatEditTitle": "edit_group_title",
    "ChatEditPhoto": "edit_group_photo",
    "ChatDeletePhoto": "delete_group_photo",
    "ChatAddUser": "invite_members",
    "ChatDeleteUser": "remove_members",
    "ChatJoinedByLink": "join_group_by_link",
    "ChatJoinedByRequest": "join_group_by_request",
    "ChatMigrateTo": "migrate_to_supergroup",
    "ChannelMigrateFrom": "migrate_from_group",
    "PinMessage": "pin_message",
    "HistoryClear": "clear_history",
    "GameScore": "score_in_game",
    "PhoneCall": "phone_call",
    "ScreenshotTaken": "take_screenshot",
    "ContactSignUp": "joined_telegram",
    "GroupCall": "group_call",
    "GroupCallScheduled": "group_call_scheduled",
    "InviteToGroupCall": "invite_to_group_call",
    "SetMessagesTTL": "set_messages_ttl",
    "TopicCreate": "topic_created",
    "TopicEdit": "topic_edit",
    "BoostApply": "boost_apply",
}
OUR_ACTIONS = {v: k for k, v in ACTIONS.items()}
CHANNEL_BASE = 10**12  # a channel's id among senders is -(10**12 + its own id)
# text entity -> the Markdown marks the reader and text_md use
MARKS = {"bold": "**", "italic": "__", "strikethrough": "~~", "spoiler": "||", "code": "`"}
LIKELY_HEAD = 6  # replies to a message outside the export, from which on it is taken for a topic's first one
MARKED = re.compile(
    r"```(?P<pre>.+?)```|`(?P<code>[^`\n]+)`|\[(?P<link>[^\]\n]+)\]\((?P<href>[^)\s]+)\)"
    r"|\*\*(?P<bold>.+?)\*\*|__(?P<italic>.+?)__|~~(?P<strikethrough>.+?)~~|\|\|(?P<spoiler>.+?)\|\|",
    re.S,
)


# --- writing


def peer_tag(sender_id):
    """A sender's id as Telegram Desktop writes it: user123, channel456, chat789."""
    if sender_id is None:
        return None
    if sender_id > 0:
        return f"user{sender_id}"
    if sender_id < -CHANNEL_BASE:
        return f"channel{-sender_id - CHANNEL_BASE}"
    return f"chat{-sender_id}"


def stamps(key, iso_date):
    """A date as the two fields Telegram Desktop writes: local time, and Unix time as text."""
    when = datetime.datetime.fromisoformat(iso_date)
    return {key: when.astimezone().strftime("%Y-%m-%dT%H:%M:%S"), f"{key}_unixtime": str(int(when.timestamp()))}


def snake(name):
    return re.sub(r"(?<=[a-z0-9])([A-Z])", r"_\1", name).lower()


def camel(name):
    return "".join(part.capitalize() for part in name.split("_"))


def media_fields(m, extra, folder):
    """The media of a message as Telegram Desktop's fields; `folder` is where the topic's files are."""
    f = extra.get("file") or {}
    path = f"{folder}{f['path']}" if f.get("path") else NOT_INCLUDED
    if m["media"] == "MessageMediaPhoto":
        hidden = (extra.get("photo") or {}).get("spoiler") or None
        size = {"photo_file_size": f.get("size"), "width": f.get("w"), "height": f.get("h")}
        return {"photo": path, **size, "media_spoiler": hidden}
    if m["media"] == "MessageMediaDocument":
        return {
            "file": path,
            "file_name": f.get("name"),
            "file_size": f.get("size"),
            "media_type": MEDIA_TYPES.get(f.get("kind")),
            "sticker_emoji": f.get("emoji") if f.get("kind") == "sticker" else None,
            "performer": f.get("performer"),
            "title": f.get("title"),
            "mime_type": f.get("mime"),
            "duration_seconds": f.get("duration"),
            "width": f.get("w"),
            "height": f.get("h"),
            "media_spoiler": f.get("spoiler") or None,
        }
    if poll := extra.get("poll"):
        answers = [{"text": a["text"], "voters": a.get("voters") or 0, "chosen": False} for a in poll["answers"]]
        return {
            "poll": {
                "question": poll["question"],
                "closed": bool(poll.get("closed")),
                "total_voters": poll.get("total_voters") or 0,
                "answers": answers,
            }
        }
    if place := extra.get("venue") or extra.get("geo"):
        return {
            "location_information": {"latitude": place.get("lat"), "longitude": place.get("long")},
            "place_name": place.get("title"),
            "address": place.get("address"),
        }
    if c := extra.get("contact"):
        info = {
            "first_name": c.get("first_name", ""),
            "last_name": c.get("last_name", ""),
            "phone_number": c.get("phone"),
        }
        return {"contact_information": info}
    return {}


def reaction_list(reactions, reacted=()):
    """Reactions as Telegram Desktop lists them; `recent` names who put each, when the dump knows it."""
    out, who = [], {}
    for r in reacted:
        one = {"from": r["name"], "from_id": peer_tag(r["user_id"])}
        if r["date"]:  # Telegram may leave it out
            one["date"] = stamps("date", r["date"])["date"]
        who.setdefault(r["reaction"], []).append(one)
    for key, count in (reactions or {}).items():
        if key.startswith("custom:"):
            out.append({"type": "custom_emoji", "count": count, "document_id": key.removeprefix("custom:")})
        elif key == "paid":
            out.append({"type": "paid", "count": count})
        else:
            out.append({"type": "emoji", "count": count, "emoji": key})
        if key in who:
            out[-1]["recent"] = who[key]
    return out


def piece(found):
    """(entity type, its text, a link's address) of one match of MARKED."""
    groups = {k: v for k, v in found.groupdict().items() if v is not None}
    href = groups.pop("href", None)
    ((kind, text),) = groups.items()
    if kind not in ("pre", "code"):  # of formatting inside formatting only the outer one is kept
        text = MARKED.sub(lambda inner: piece(inner)[1], text)
    return kind, text, href


def text_entities(text, marked):
    """A message's text as Telegram Desktop's list of entities, from its Markdown when it has one."""
    plain = [{"type": "plain", "text": text}] if text else []
    if not marked:
        return plain
    out, at = [], 0
    for found in MARKED.finditer(marked):
        if found.start() > at:
            out.append({"type": "plain", "text": marked[at : found.start()]})
        kind, inner, href = piece(found)
        if kind != "link":
            out.append({"type": kind, "text": inner} | ({"language": ""} if kind == "pre" else {}))
        elif person := re.fullmatch(r"tg://user\?id=(\d+)", href):
            out.append({"type": "mention_name", "text": inner, "user_id": int(person[1])})
        else:
            out.append({"type": "text_link", "text": inner, "href": href})
        at = found.end()
    if at < len(marked):
        out.append({"type": "plain", "text": marked[at:]})
    # marks typed by hand read as formatting too: then the pieces do not add up to the text, and it stays plain
    return out if "".join(e["text"] for e in out) == text else plain


def desktop_message(m, folder=""):
    """An export record (see export.message_dict) as a message of result.json."""
    extra = m.get("extra") or {}
    service = bool(m["action"])
    out = {"id": m["id"], "type": "service" if service else "message", **stamps("date", m["date"])}
    if m["edit_date"]:
        out.update(stamps("edited", m["edit_date"]))
    who = "actor" if service else "from"
    out[who], out[f"{who}_id"] = m["sender"], peer_tag(m["sender_id"])
    if service:
        name = m["action"].removeprefix("MessageAction")
        details = extra.get("action") or {}
        out["action"] = ACTIONS.get(name) or snake(name)
        out["new_title" if name == "TopicEdit" else "title"] = details.get("title")
        out["members"] = details.get("names")
        if name == "PinMessage":
            out["message_id"] = m["reply_to"]
    # Telegram Desktop leaves it out of service messages; here it is what tells their topic
    if m["reply_to"] and "message_id" not in out:
        out["reply_to_message_id"] = m["reply_to"]
    out["forwarded_from"] = m["fwd_from"]
    out["forwarded_from_id"] = peer_tag((extra.get("fwd") or {}).get("from_id"))
    out["author"], out["via_bot"] = extra.get("author"), extra.get("via_bot")
    if m["media"]:
        out.update(media_fields(m, extra, folder))
    text = m["text"] or ""
    out["text_entities"] = text_entities(text, m.get("text_md"))
    # as Telegram Desktop has it: a string, or with formatting a list of strings and entities
    mixed = [e["text"] if e["type"] == "plain" else e for e in out["text_entities"]]
    out["text"] = text if all(isinstance(e, str) for e in mixed) else mixed
    if m.get("reactions"):
        out["reactions"] = reaction_list(m["reactions"], m.get("reacted", ()))
    return {k: v for k, v in out.items() if v is not None}


def write_result(db, out, title, records, titles, threads=False):
    """Write <out>/result.json, the whole chat in Telegram Desktop's format.

    records: the export records in the order of their ids; titles: {topic id: title}, to find the files.
    """
    chat = get_chat(db)
    kinds = CHAT_TYPES.get(chat.get("kind"), CHAT_TYPES["group"])
    head = {"name": title, "type": kinds[0] if chat.get("username") else kinds[1], "id": chat.get("id")}
    folders = {}
    with open(out / "result.json", "w", encoding="utf-8") as f:
        f.write(json.dumps(head, ensure_ascii=False, indent=1)[:-2] + ',\n "messages": [')
        first = True
        for m in records:
            topic = m["topic_id"]
            if topic not in folders:
                folders[topic] = "" if topic is None or threads else topic_folder(out, topic, titles).name + "/"
            text = json.dumps(desktop_message(m, folders[topic]), ensure_ascii=False, indent=1)
            f.write(("\n" if first else ",\n") + "\n".join("  " + line for line in text.splitlines()))
            first = False
        f.write("\n ]\n}\n")


# --- reading


def unix_iso(msg, key):
    """One of a message's two dates as ISO 8601 in UTC; the local time is used only without the Unix one."""
    if msg.get(f"{key}_unixtime"):
        when = datetime.datetime.fromtimestamp(int(msg[f"{key}_unixtime"]), datetime.UTC)
    elif msg.get(key):
        when = datetime.datetime.fromisoformat(msg[key]).astimezone(datetime.UTC)
    else:
        return None
    return when.isoformat()


def peer_number(tag):
    """user123 -> 123, channel456 -> the id a channel has among senders, chat789 -> -789."""
    if isinstance(tag, int):  # exports made before 2021 have the bare number
        return tag
    m = re.fullmatch(r"(user|channel|chat)(\d+)", tag or "")
    if not m:
        return None
    n = int(m[2])
    return n if m[1] == "user" else -CHANNEL_BASE - n if m[1] == "channel" else -n


def read_text(msg):
    """(plain text, Markdown when the text has formatting, the links hidden under words)."""
    parts = msg.get("text_entities")
    if parts is None:  # older exports: a string, or a list of strings and entities
        raw = msg.get("text") or ""
        parts = [raw] if isinstance(raw, str) else raw
    plain, marked, links, formatted = [], [], [], False
    for p in parts:
        if isinstance(p, str):
            p = {"type": "plain", "text": p}
        text, kind = p.get("text") or "", p.get("type")
        plain.append(text)
        url = p.get("href") if kind == "text_link" else None
        if kind == "mention_name" and p.get("user_id"):
            url = f"tg://user?id={p['user_id']}"
        if url and text.strip() != url:
            links.append({"text": text, "url": url})
            marked.append(f"[{text}]({url})")
        elif kind == "pre":
            marked.append(f"```{p.get('language') or ''}\n{text}\n```")
        elif kind in MARKS and text.strip():
            marked.append(f"{MARKS[kind]}{text}{MARKS[kind]}")
        else:
            marked.append(text)
            continue
        formatted = True
    return "".join(plain), "".join(marked) if formatted else None, links


def read_media(msg):
    """(our media type, the details for extra, the file's path inside the export or None)."""
    path = msg.get("photo") or msg.get("file")
    path = path if path and not path.startswith("(") else None
    if "photo" in msg:
        return "MessageMediaPhoto", {"photo": {"spoiler": True}} if msg.get("media_spoiler") else {}, path
    if "file" in msg:
        info = {
            "name": msg.get("file_name") or (Path(path).name if path else None),
            "mime": msg.get("mime_type") or "application/octet-stream",
            "size": msg.get("file_size"),
            "kind": FILE_KINDS.get(msg.get("media_type")),
            "duration": msg.get("duration_seconds"),
            "w": msg.get("width"),
            "h": msg.get("height"),
            "emoji": msg.get("sticker_emoji"),
            "performer": msg.get("performer"),
            "title": msg.get("title"),
            "spoiler": msg.get("media_spoiler") or None,
        }
        return "MessageMediaDocument", {"file": {k: v for k, v in info.items() if v is not None}}, path
    if poll := msg.get("poll"):
        answers = [{"text": a.get("text"), "voters": a.get("voters")} for a in poll.get("answers", [])]
        info = {"question": poll.get("question"), "answers": answers, "total_voters": poll.get("total_voters")}
        return "MessageMediaPoll", {"poll": {**info, "closed": bool(poll.get("closed"))}}, None
    if place := msg.get("location_information"):
        geo = {"lat": place.get("latitude"), "long": place.get("longitude")}
        if msg.get("place_name"):
            return (
                "MessageMediaVenue",
                {"venue": {**geo, "title": msg["place_name"], "address": msg.get("address")}},
                None,
            )
        return "MessageMediaGeo", {"geo": geo}, None
    if c := msg.get("contact_information"):
        info = {"first_name": c.get("first_name"), "last_name": c.get("last_name"), "phone": c.get("phone_number")}
        return "MessageMediaContact", {"contact": {k: v for k, v in info.items() if v}}, None
    return None, {}, None


def read_reactions(found):
    out = {}
    for r in found or []:
        key = r.get("emoji") or (f"custom:{r['document_id']}" if r.get("document_id") else r.get("type"))
        if key and r.get("count"):
            out[key] = r["count"]
    return out


def read_message(msg, topic):
    """A message of result.json as a database row and the path of its file inside the export."""
    service = msg.get("type") == "service"
    text, text_md, links = read_text(msg)
    # what a service message shows, a group's new photo, is not a file of the message
    media, extra, path = (None, {}, None) if service else read_media(msg)
    action = None
    if service:
        name = msg.get("action") or "unknown"
        action = "MessageAction" + (OUR_ACTIONS.get(name) or camel(name))
        details = {"title": msg.get("title") or msg.get("new_title"), "names": msg.get("members")}
        if details := {k: v for k, v in details.items() if v}:
            extra["action"] = details
    if links:
        extra["links"] = links
    if (origin := peer_number(msg.get("forwarded_from_id"))) is not None:
        extra["fwd"] = {"from_id": origin}
    for key in ("author", "via_bot"):
        if msg.get(key):
            extra[key] = msg[key]
    reactions = read_reactions(msg.get("reactions"))
    who = "actor" if service else "from"
    row = {
        "id": msg["id"],
        "date": unix_iso(msg, "date"),
        "edit_date": unix_iso(msg, "edited"),
        "topic_id": topic,
        "sender_id": peer_number(msg.get(f"{who}_id")),
        "sender": msg.get(who),
        "text": text,
        "reply_to": msg.get("reply_to_message_id") or (msg.get("message_id") if service else None),
        "fwd_from": msg.get("forwarded_from"),
        "media": media,
        "action": action,
        "text_md": text_md,
        "reactions": json.dumps(reactions, ensure_ascii=False) if reactions else None,
        "extra": json.dumps(extra, ensure_ascii=False) if extra else None,
    }
    return tuple(row.get(c) for c in COLUMN_NAMES), path


def answered(msg):
    """The message this one replies to; for a service message, the one it is about (the pinned one)."""
    return msg.get("reply_to_message_id") or (msg.get("message_id") if msg.get("type") == "service" else None)


def topics_of(messages, known=None, placed=None):
    """({message id: topic id}, {topic id: title}) for a forum, two empty ones for any other chat.

    The export does not name a message's topic. But a message in a topic replies to the topic's first message
    or to another message of the topic, so the chain of replies leads to it; a chain that ends anywhere else
    is in General. `known` are the topics the dump has already and `placed` the topics of its messages, which
    the chains may lead to as well. An export of a part of the chat has the first messages of few topics:
    there, a message that is not in it and has many replies is taken for one.
    """
    heads, placed = dict(known or {}), placed or {}
    for m in messages:
        if m.get("type") == "service" and m.get("action") == "topic_created":
            heads[m["id"]] = m.get("title")
    if not heads:
        return {}, {}
    here = {m["id"] for m in messages} | placed.keys() | heads.keys()
    outside = collections.Counter(r for m in messages if (r := answered(m)) and r not in here)
    heads.update({r: None for r, replies in outside.items() if replies >= LIKELY_HEAD})
    found = {}
    for m in sorted(messages, key=lambda m: m["id"]):
        mid, reply = m["id"], answered(m)
        found[mid] = mid if mid in heads else reply if reply in heads else found.get(reply) or placed.get(reply) or 1
        if m.get("action") == "topic_edit" and m.get("new_title") and found[mid] in heads:
            heads[found[mid]] = m["new_title"]  # renamed since it was made
    return found, heads


def chat_kind(kind):
    return KINDS.get(kind) or ("channel" if "channel" in (kind or "") else "group")
