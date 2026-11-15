"""Edits and deletions since the last run, from the change log Telegram keeps for a channel or supergroup."""

import logging
from dataclasses import dataclass, field

from telethon import errors, functions, types

from .store import UPSERT, mark_deleted, set_pinned, stored_ids, to_row

log = logging.getLogger("dump")

# Telegram pages through a gap only when asked for a large limit: with a small one it answers any gap past
# the limit with "too long" at once. A page holds about 100 changes whatever the limit.
LOG_LIMIT = 100_000
IDS_PER_REQUEST = 100


@dataclass
class Changes:
    """What the log lists from a saved pts on: ids of edited and deleted messages, and pins put or taken off."""

    edited: set = field(default_factory=set)
    deleted: set = field(default_factory=set)
    pinned: dict = field(default_factory=dict)  # message id -> pinned or not
    pts: int | None = None  # where the log stands now, to start from next time
    complete: bool = False  # False: the log does not reach back to the saved pts, or was not read at all


async def channel_pts(client, channel):
    """Where the channel's change log stands now, or None when Telegram does not say."""
    try:
        full = await client(functions.channels.GetFullChannelRequest(channel))
    except errors.RPCError as e:
        log.warning(f"Cannot read the chat's state: {e}")
        return None
    return full.full_chat.pts


async def channel_changes(client, channel, pts):
    """Read the channel's change log from `pts` to its end.

    The log keeps a limited number of changes (new messages count too); past that Telegram answers "too
    long" and the result is not complete. It works the same for a public channel the account has not joined.
    """
    found = Changes()
    while True:
        request = functions.updates.GetChannelDifferenceRequest(
            channel=channel, filter=types.ChannelMessagesFilterEmpty(), pts=pts, limit=LOG_LIMIT, force=True
        )
        try:
            diff = await client(request)
        except errors.RPCError as e:
            log.warning(f"Cannot read the chat's change log: {e}")
            return found
        if isinstance(diff, types.updates.ChannelDifferenceTooLong):
            found.pts = getattr(diff.dialog, "pts", None)
            return found
        for u in getattr(diff, "other_updates", ()):
            if isinstance(u, types.UpdateEditChannelMessage):
                found.edited.add(u.message.id)
            elif isinstance(u, types.UpdateDeleteChannelMessages):
                found.deleted.update(u.messages)
            elif isinstance(u, types.UpdatePinnedChannelMessages):
                found.pinned.update(dict.fromkeys(u.messages, bool(u.pinned)))
        if diff.final:
            found.pts, found.complete = diff.pts, True
            return found
        if diff.pts <= pts:  # no way forward; better to stop than to ask for the same page forever
            return found
        pts = diff.pts


async def apply_changes(client, entity, db, found, forum, options, when, upto):
    """Bring the saved messages in line with the log. Returns how many were marked as deleted.

    Edited messages are fetched again and saved over their rows, which keeps the earlier text in `edits`.
    Only messages saved by earlier runs are touched, those with ids up to `upto`: what this run downloaded
    is as Telegram has it now.
    """
    marked = mark_deleted(db, found.deleted, when)
    ids = sorted(i for i in stored_ids(db, found.edited - found.deleted) if i <= upto)
    for i in range(0, len(ids), IDS_PER_REQUEST):
        part = ids[i : i + IDS_PER_REQUEST]
        got = await client.get_messages(entity, ids=part)
        db.executemany(UPSERT, [to_row(m, forum, options) for m in got if m is not None])
        marked += mark_deleted(db, [i for i, m in zip(part, got, strict=True) if m is None], when)
    set_pinned(db, found.pinned)
    return marked
