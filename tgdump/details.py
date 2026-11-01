"""What a person sees in a message besides its text: media, buttons, replies, forwards, signatures, service actions.

Everything here goes into the JSON `extra` column, one key per kind of detail, empty ones left out.
"""

import datetime

from telethon import utils
from telethon.helpers import add_surrogate, del_surrogate
from telethon.tl import types
from telethon.tl.tlobject import TLObject

from .rich import Renderer
from .util import iso, plain

# fields that are only good for downloading or caching, never shown
HIDDEN = {
    "access_hash", "file_reference", "thumbs", "video_thumbs", "sizes", "video_sizes", "stripped_thumb", "dc_id",
    "waveform", "hash", "thumb", "data", "credentials", "payload", "charge",
}  # fmt: skip
FILES = {"Photo", "PhotoEmpty", "Document", "DocumentEmpty", "WebDocument", "WebDocumentNoProxy"}


def empty(v):
    return v is None or v is False or (isinstance(v, str | list | dict) and not v)


def simple(v):
    """A TL value as plain JSON: peers become ids, texts their text, files their id; empty fields are left out."""
    if isinstance(v, TLObject):
        n = type(v).__name__
        if n.startswith("Peer") or n.startswith("InputPeer"):
            try:
                return utils.get_peer_id(v)
            except (TypeError, ValueError):
                pass
        if n == "TextWithEntities":
            return v.text
        if n in FILES:
            return {"_": n, "id": getattr(v, "id", None)}
        out = {"_": n}
        for k, x in v.__dict__.items():
            if k.startswith("_") or k in HIDDEN:
                continue
            x = simple(x)
            if not empty(x):
                out[k] = x
        return out
    if isinstance(v, list | tuple):
        return [y for y in (simple(x) for x in v) if not empty(y)]
    if isinstance(v, datetime.datetime):
        return iso(v)
    if isinstance(v, bytes):
        return None  # callback data, hashes: nothing a person reads
    return v


def fields(obj, drop=("_",)):
    """simple() of an object without its type name."""
    d = simple(obj)
    return {k: v for k, v in d.items() if k not in drop} if isinstance(d, dict) else d


def compact(d):
    return {k: v for k, v in d.items() if not empty(v)}


def display_name(entity):
    name = utils.get_display_name(entity) if entity else ""
    return name or None


def peer_id(peer):
    try:
        return utils.get_peer_id(peer) if peer else None
    except (TypeError, ValueError):
        return None


# --- media


def file_info(media):
    doc = media.document
    if not isinstance(doc, types.Document):
        return {}
    info = {"mime": doc.mime_type, "size": doc.size}
    kind = "voice" if media.voice else "round" if media.round else "video" if media.video else None
    for a in doc.attributes:
        if isinstance(a, types.DocumentAttributeFilename):
            info["name"] = a.file_name
        elif isinstance(a, types.DocumentAttributeAudio):
            info.update(duration=a.duration, title=a.title, performer=a.performer)
            kind = kind or ("voice" if a.voice else "audio")
        elif isinstance(a, types.DocumentAttributeVideo):
            info.update(duration=a.duration, w=a.w, h=a.h)
            kind = kind or ("round" if a.round_message else "video")
        elif isinstance(a, types.DocumentAttributeImageSize):
            info.update(w=a.w, h=a.h)
        elif isinstance(a, types.DocumentAttributeSticker | types.DocumentAttributeCustomEmoji):
            info["emoji"] = a.alt
            kind = "sticker"
        elif isinstance(a, types.DocumentAttributeAnimated):
            kind = kind if kind == "sticker" else "gif"
    info.update(kind=kind, spoiler=media.spoiler, ttl=media.ttl_seconds)
    return compact(info)


def webpage_info(page):
    if isinstance(page, types.WebPage):
        return compact(
            {
                "url": page.url,
                "site": page.site_name,
                "title": page.title,
                "description": page.description,
                "author": page.author,
                "type": page.type,
            }
        )
    return compact({"url": getattr(page, "url", None)})


def geo_info(geo, **more):
    if not isinstance(geo, types.GeoPoint):
        return compact(more)
    return compact({"lat": geo.lat, "long": geo.long, **more})


def poll_info(media):
    poll, results = media.poll, media.results
    votes = {v.option: v.voters for v in results.results} if results and results.results else {}
    return {
        "question": plain(poll.question),
        "answers": [{"text": plain(a.text), "voters": votes.get(a.option)} for a in poll.answers],
        "total_voters": results.total_voters if results else None,
        "closed": bool(poll.closed),
        "quiz": bool(poll.quiz),
        "multiple_choice": bool(poll.multiple_choice),
    }


def todo_info(media):
    done = {c.id for c in media.completions or []}
    return {
        "title": plain(media.todo.title),
        "items": [{"text": plain(i.title), "done": i.id in done} for i in media.todo.list],
    }


def media_details(media):
    """{key: details} for the message's media; types without special handling keep their plain fields."""
    if media is None:
        return {}
    if isinstance(media, types.MessageMediaPoll):
        return {"poll": poll_info(media)}
    if isinstance(media, types.MessageMediaToDo):
        return {"todo": todo_info(media)}
    if isinstance(media, types.MessageMediaDocument):
        return {"file": d} if (d := file_info(media)) else {}
    if isinstance(media, types.MessageMediaPhoto):
        return {"photo": d} if (d := compact({"spoiler": media.spoiler, "ttl": media.ttl_seconds})) else {}
    if isinstance(media, types.MessageMediaWebPage):
        return {"webpage": webpage_info(media.webpage)}
    if isinstance(media, types.MessageMediaVenue):
        info = geo_info(
            media.geo, title=media.title, address=media.address, provider=media.provider, venue_id=media.venue_id
        )
        return {"venue": info}
    if isinstance(media, types.MessageMediaGeoLive):
        return {"geo": geo_info(media.geo, live=True, period=media.period)}
    if isinstance(media, types.MessageMediaGeo):
        return {"geo": geo_info(media.geo)}
    if isinstance(media, types.MessageMediaContact):
        info = compact(
            {
                "phone": media.phone_number,
                "first_name": media.first_name,
                "last_name": media.last_name,
                "user_id": media.user_id or None,
                "vcard": media.vcard,
            }
        )
        return {"contact": info}
    if isinstance(media, types.MessageMediaDice):
        return {"dice": {"emoticon": media.emoticon, "value": media.value}}
    if isinstance(media, types.MessageMediaGame):
        g = media.game
        return {"game": compact({"title": g.title, "description": g.description, "short_name": g.short_name})}
    if isinstance(media, types.MessageMediaInvoice):
        info = {
            "title": media.title,
            "description": media.description,
            "currency": media.currency,
            "amount": media.total_amount,
        }
        return {"invoice": compact(info)}
    if isinstance(media, types.MessageMediaStory):
        story = media.story
        caption = getattr(story, "caption", None)
        return {"story": compact({"peer": peer_id(media.peer), "id": media.id, "caption": caption})}
    if isinstance(media, types.MessageMediaPaidMedia):
        items = [type(getattr(x, "media", x)).__name__.removeprefix("MessageMedia") for x in media.extended_media]
        return {"paid_media": {"stars": media.stars_amount, "items": items}}
    info = fields(media)  # giveaways, streams and whatever comes in later layers
    return {"media_info": info} if info else {}


# --- the message around the text


def button_list(markup):
    """Rows of buttons as [[{text, url}]]; what a button does when pressed is kept only if a person can see it."""
    rows = []
    for row in getattr(markup, "rows", None) or ():
        out = []
        for b in getattr(row, "buttons", None) or ():
            text = b.text if isinstance(b.text, str) else Renderer().text(b.text)
            btn = {"text": text}
            for src in (b, getattr(b, "type", None)):
                for key, attr in (("url", "url"), ("copy", "copy_text"), ("query", "query"), ("user_id", "user_id")):
                    value = getattr(src, attr, None)
                    if value and key not in btn and not isinstance(value, bytes):
                        btn[key] = value
            out.append(btn)
        if out:
            rows.append(out)
    return rows


def fwd_name(header, entity=None):
    """The name a forward shows: the hidden sender's name, the source chat or user, or at least its id."""
    if header.from_name:
        return header.from_name
    return display_name(entity) or (str(peer_id(header.from_id)) if header.from_id else None)


def forward_info(m):
    f = m.fwd_from
    fwd = getattr(m, "_forward", None)
    source = (getattr(fwd, "chat", None) or getattr(fwd, "sender", None)) if fwd else None
    info = compact(
        {
            "from_id": peer_id(f.from_id),
            "date": iso(f.date),
            "author": f.post_author,
            "saved_from": peer_id(f.saved_from_peer) or peer_id(f.saved_from_id) or f.saved_from_name,
            "psa": f.psa_type,
            "imported": f.imported,
        }
    )
    return fwd_name(f, source), info


def reply_info(m):
    r = m.reply_to
    if isinstance(r, types.MessageReplyStoryHeader):
        return {"story": {"peer": peer_id(r.peer), "id": r.story_id}}
    if not isinstance(r, types.MessageReplyHeader):
        return {}
    info = {"quote": r.quote_text}
    if r.reply_to_peer_id:
        info["peer"] = peer_id(r.reply_to_peer_id)
        info["chat"] = display_name(getattr(m, "_reply_to_chat", None))
    if r.reply_from:
        info["from"] = fwd_name(r.reply_from, getattr(m, "_reply_to_sender", None))
    if r.reply_media:
        info["media"] = type(r.reply_media).__name__
    info["todo_item"] = r.todo_item_id
    return compact(info)


def hidden_links(text, entities):
    """Links whose address is not in the text itself: [text](url) links and mentions of users without a username."""
    if not text or not entities:
        return []
    wide = add_surrogate(text)
    out = []
    for e in entities:
        if isinstance(e, types.MessageEntityTextUrl):
            url = e.url
        elif isinstance(e, types.MessageEntityMentionName):
            url = f"tg://user?id={e.user_id}"
        else:
            continue
        shown = del_surrogate(wide[e.offset : e.offset + e.length])
        if shown.strip() != url:
            out.append({"text": shown, "url": url})
    return out


def action_details(m):
    info = fields(m.action)
    names = [display_name(e) for e in getattr(m, "_action_entities", None) or ()]
    if any(names):
        info["names"] = names
    return info


def message_details(m):
    """Everything besides text, sender, reply and forward ids that the Telegram app shows for the message."""
    d = {}
    if getattr(m, "post_author", None) and getattr(m, "sender", None):
        d["author"] = m.post_author  # a channel post signed by its author; without a sender it is the sender name
    d["rank"] = getattr(m, "from_rank", None)
    d["boosts"] = getattr(m, "from_boosts_applied", None)
    if getattr(m, "via_bot_id", None):
        bot = getattr(m, "_via_bot", None)
        d["via_bot"] = f"@{bot.username}" if getattr(bot, "username", None) else m.via_bot_id
    if reply := reply_info(m):
        d["reply"] = reply
    if getattr(m, "fwd_from", None):
        d["fwd"] = forward_info(m)[1]
    d.update(media_details(getattr(m, "media", None)))
    if getattr(m, "action", None):
        d["action"] = action_details(m)
    d["buttons"] = button_list(getattr(m, "reply_markup", None))
    d["links"] = hidden_links(getattr(m, "message", None), getattr(m, "entities", None))
    if fc := getattr(m, "factcheck", None):
        d["factcheck"] = compact({"text": plain(fc.text), "country": fc.country})
    reasons = getattr(m, "restriction_reason", None) or []
    d["restricted"] = [compact({"platform": r.platform, "reason": r.reason, "text": r.text}) for r in reasons]
    d["effect"] = getattr(m, "effect", None)
    d["paid_stars"] = getattr(m, "paid_message_stars", None)
    if sp := getattr(m, "suggested_post", None):
        d["suggested_post"] = fields(sp)
    d["pinned"] = getattr(m, "pinned", None)
    return compact(d)
