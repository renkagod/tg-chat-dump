"""What the app shows about a chat besides its messages: the chat itself, forum topics, pins, the people in it."""

import asyncio
import collections
import logging

from telethon import errors, functions, types, utils
from telethon.tl.types import Channel, Chat, User

from .details import peer_id
from .store import person, save_users
from .util import fmt_duration, iso

log = logging.getLogger("dump")
PER_REQUEST = 100  # users or custom emoji asked about in one request
BIO_SECONDS = 0.6  # what one bio takes, to say how long the rest will


def chat_kind(e):
    if isinstance(e, User):
        return "bot" if e.bot else "user"
    if isinstance(e, Channel):
        return "forum" if e.forum else "group" if e.megagroup else "channel"
    return "group"


def usernames_of(entity):
    """Every username of a chat or user, the one shown first."""
    names = [entity.username] if getattr(entity, "username", None) else []
    names += [u.username for u in getattr(entity, "usernames", None) or () if u.active and u.username not in names]
    return names


async def chat_info(client, entity):
    """What the app shows about the chat itself as {key: value}, and the pts of a channel's change log.

    One request; when Telegram refuses it, what the chat's entry in the chat list says is still returned.
    """
    names = usernames_of(entity)
    info = {
        "id": entity.id,
        "kind": chat_kind(entity),
        "title": utils.get_display_name(entity),
        "username": names[0] if names else None,
        "usernames": names if len(names) > 1 else None,
        "link": f"https://t.me/{names[0]}" if names else None,
        "verified": getattr(entity, "verified", None),
        "scam": getattr(entity, "scam", None),
        "fake": getattr(entity, "fake", None),
    }
    rights = getattr(entity, "default_banned_rights", None)
    if rights:
        info["members_cannot"] = sorted(k for k, v in rights.to_dict().items() if v is True)
    pts = full = None
    try:
        if isinstance(entity, Channel):
            res = await client(functions.channels.GetFullChannelRequest(entity))
            full, pts = res.full_chat, res.full_chat.pts
        elif isinstance(entity, Chat):
            res = await client(functions.messages.GetFullChatRequest(entity.id))
            full = res.full_chat
        else:
            res = await client(functions.users.GetFullUserRequest(entity))
            full = res.full_user
    except errors.RPCError as e:
        log.warning(f"Cannot read the chat's details: {e}")
    if isinstance(entity, User):
        info["phone"] = entity.phone
    elif isinstance(entity, Chat):
        info["created"] = iso(entity.date)
        info["members"] = entity.participants_count
    else:
        # a channel tells its members when they joined, and everyone else when it was created
        info["created" if entity.left else "joined"] = iso(entity.date)
        info.update(
            protected=entity.noforwards,
            join_request=entity.join_request,
            signatures=entity.signatures,
            members=getattr(entity, "participants_count", None),
        )
    if full is None:
        return info, pts
    invite = getattr(full, "exported_invite", None)
    linked = getattr(full, "linked_chat_id", None)
    location = getattr(full, "location", None)
    info.update(
        about=getattr(full, "about", None),
        members=getattr(full, "participants_count", None) or info.get("members"),
        admins=getattr(full, "admins_count", None),
        invite=getattr(invite, "link", None),
        linked_chat=linked,
        linked_title=next((utils.get_display_name(c) for c in getattr(res, "chats", ()) if c.id == linked), None),
        slow_mode=getattr(full, "slowmode_seconds", None),
        auto_delete=getattr(full, "ttl_period", None),
        pinned_message=getattr(full, "pinned_msg_id", None),
        hidden_members=getattr(full, "participants_hidden", None),
        hidden_history=getattr(full, "hidden_prehistory", None),
        location=getattr(location, "address", None),
    )
    return info, pts


async def add_photo(client, entity, info, known):
    """Put the chat's small picture into `info`, unless the saved one (photo id `known`) is still it."""
    photo_id = getattr(getattr(entity, "photo", None), "photo_id", None)
    info["photo_id"] = photo_id
    if photo_id == known:
        return
    info["photo"] = None  # the key alone drops the saved picture
    if photo_id:
        try:
            info["photo"] = await client.download_profile_photo(entity, file=bytes, download_big=False)
        except (errors.RPCError, OSError, ValueError) as e:
            log.warning(f"Cannot download the chat's picture: {e}")
            info["photo_id"] = None  # asked for again by the next run


TOPIC_UPSERT = (
    "INSERT INTO topics(id, title, date, creator_id, icon_emoji_id, color, closed, pinned, hidden) "
    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title=excluded.title, date=excluded.date, "
    "creator_id=excluded.creator_id, icon=IIF(icon_emoji_id IS excluded.icon_emoji_id, icon, NULL), "
    "icon_emoji_id=excluded.icon_emoji_id, color=excluded.color, closed=excluded.closed, pinned=excluded.pinned, "
    "hidden=excluded.hidden"
)


async def fetch_topics(client, entity, db):
    """Save the forum's topics as they are now: title, icon, colour, who made them, closed, pinned."""
    offset_date, offset_id, offset_topic, pins = None, 0, 0, 0
    while True:
        res = await client(
            functions.messages.GetForumTopicsRequest(
                peer=entity, offset_date=offset_date, offset_id=offset_id, offset_topic=offset_topic, limit=100
            )
        )
        rows = []
        for t in res.topics:
            if not isinstance(t, types.ForumTopic):
                continue  # a deleted topic: what is saved of it stays as it was
            pins += bool(t.pinned)
            rows.append(
                (
                    t.id,
                    t.title,
                    iso(t.date),
                    peer_id(t.from_id),
                    t.icon_emoji_id,
                    t.icon_color,
                    int(bool(t.closed)) or None,
                    pins if t.pinned else None,
                    int(bool(t.hidden)) or None,
                )
            )
        db.executemany(TOPIC_UPSERT, rows)
        if len(res.topics) < 100:
            break
        last = res.topics[-1]
        msgs = {m.id: m for m in res.messages}
        offset_topic, offset_id = last.id, getattr(last, "top_message", 0)
        offset_date = msgs[offset_id].date if offset_id in msgs else None
    await topic_icons(client, db)
    db.commit()


async def topic_icons(client, db):
    """A topic's icon is a custom emoji; look up which emoji the new ones stand for."""
    asked = [i for (i,) in db.execute("SELECT DISTINCT icon_emoji_id FROM topics WHERE icon_emoji_id AND icon IS NULL")]
    for n in range(0, len(asked), PER_REQUEST):
        part = asked[n : n + PER_REQUEST]
        try:
            docs = await client(functions.messages.GetCustomEmojiDocumentsRequest(document_id=part))
        except errors.RPCError as e:
            log.warning(f"Cannot read the topic icons: {e}")
            return
        found = dict.fromkeys(part, "")  # an emoji Telegram does not tell is not asked about again
        for d in docs:
            for a in getattr(d, "attributes", ()):
                if isinstance(a, types.DocumentAttributeCustomEmoji):
                    found[d.id] = a.alt
        db.executemany("UPDATE topics SET icon=? WHERE icon_emoji_id=?", [(alt, i) for i, alt in found.items()])


async def pinned_ids(client, entity):
    """Ids of the messages pinned in the chat now, or None when Telegram does not list them."""
    try:
        return [m.id async for m in client.iter_messages(entity, filter=types.InputMessagesFilterPinned)]
    except errors.RPCError as e:
        log.warning(f"Cannot read the pinned messages: {e}")
        return None


async def refresh_users(clients, db, when):
    """Ask Telegram how the people saved by earlier runs look now: names and usernames change.

    Those who wrote in this run are up to date already; of the rest, the ones not asked about for the longest
    go first. Telegram answers about a few hundred people a minute per account and no run waits for more:
    the others have their turn in the next runs. An account can ask only about people it has met, so each
    account is asked about the ones left by the accounts before it. Returns how many were refreshed.
    """
    stale = "SELECT id FROM users WHERE id > 0 AND updated IS NOT ? ORDER BY updated"
    left = [i for (i,) in db.execute(stale, (when,))]
    done = 0
    for client in clients:
        known = []
        for uid in left:
            try:
                known.append(utils.get_input_user(client.session.get_input_entity(types.PeerUser(uid))))
            except (ValueError, TypeError):
                continue
        asked = set()
        patience, client.flood_sleep_threshold = client.flood_sleep_threshold, 0  # a "wait" ends the asking here
        try:
            for n in range(0, len(known), PER_REQUEST):
                users = await client(functions.users.GetUsersRequest(known[n : n + PER_REQUEST]))
                people = [person(u, u.id) for u in users if isinstance(u, User)]
                save_users(db, people, when)
                db.commit()
                asked.update(p[0] for p in people)
        except errors.RPCError:
            pass  # asked too much for now, or one of them cannot be asked about
        finally:
            client.flood_sleep_threshold = patience
        done += len(asked)
        left = [uid for uid in left if uid not in asked]
    return done


async def fetch_bios(clients, db, when, stats):
    """The "bio" extra: ask for the bio of everyone who wrote and has not been asked about, a request per person.

    Every account takes its share. What is done is saved as it goes, so a stopped run goes on next time.
    """
    ids = collections.deque(
        i for (i,) in db.execute("SELECT id FROM users WHERE id > 0 AND bio_at IS NULL AND deleted IS NOT 1")
    )
    total, done = len(ids), 0
    if not total:
        return 0
    log.info(f"Bios: {total} people to ask about, about {fmt_duration(total * BIO_SECONDS / len(clients))}")
    stats.update(phase="people", people=(0, total))

    async def run(order):
        nonlocal done
        while ids:
            uid, bio = ids.popleft(), None
            for client in order:  # the others may know someone this account has not met
                try:
                    full = await client(functions.users.GetFullUserRequest(types.PeerUser(uid)))
                except (ValueError, TypeError, errors.RPCError):
                    continue
                bio = full.full_user.about or None
                break
            db.execute("UPDATE users SET bio=?, bio_at=? WHERE id=?", (bio, when, uid))
            done += 1
            if done % 20 == 0:
                db.commit()
            stats["people"] = (done, total)

    try:
        await asyncio.gather(*(run(clients[n:] + clients[:n]) for n in range(len(clients))))
    finally:
        db.commit()
    return done
