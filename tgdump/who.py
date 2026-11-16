"""The "who" extra: who reacted to each message and who voted in its poll. Telegram tells it message by message."""

import asyncio
import collections
import logging

from telethon import errors, functions, utils

from .store import person, reaction_key, save_users
from .util import all_or_none, fmt_duration, iso

log = logging.getLogger("dump")
PAGE = 100  # people per request, the most Telegram gives
SAVE_EVERY = 50  # messages between commits
WHO_SECONDS = 0.1  # what one message takes on one account, to say how long the rest will


def waiting(db, topics=None):
    """[(message id, "reactions" or "votes", its counts now)], the newest first: the messages with reactions and
    the polls with votes that were not asked about while they had these counts. topics: only in these."""
    inside = f" AND m.topic_id IN ({','.join(str(int(t)) for t in topics)})" if topics else ""
    voters = "json_extract(m.extra, '$.poll.total_voters')"
    return db.execute(
        "SELECT m.id, 'reactions', m.reactions FROM messages m "
        "LEFT JOIN asked a ON a.id = m.id AND a.what = 'reactions' "
        f"WHERE m.reactions IS NOT NULL AND m.deleted_at IS NULL AND a.seen IS NOT m.reactions{inside} "
        f"UNION ALL SELECT m.id, 'votes', CAST({voters} AS TEXT) FROM messages m "
        "LEFT JOIN asked a ON a.id = m.id AND a.what = 'votes' "
        f"WHERE m.media = 'MessageMediaPoll' AND m.deleted_at IS NULL AND {voters} > 0 "
        f"AND a.seen IS NOT CAST({voters} AS TEXT){inside} ORDER BY 1 DESC"
    ).fetchall()


def people_of(answer):
    return [person(e, utils.get_peer_id(e)) for e in (*answer.users, *answer.chats)]


async def who_reacted(client, peer, mid):
    """([(who, reaction, when)], the people and channels among them) for one message, all pages of the list."""
    found, people, offset = [], [], None
    while True:
        r = await client(functions.messages.GetMessageReactionsListRequest(peer, mid, limit=PAGE, offset=offset))
        found += [(utils.get_peer_id(x.peer_id), reaction_key(x.reaction), iso(x.date)) for x in r.reactions]
        people += people_of(r)
        offset = r.next_offset
        if not offset or not r.reactions or len(found) >= r.count:
            return found, people


async def who_voted(client, peer, mid):
    """The same for a poll: [(who, the answer's place in the poll, when)]. A poll with anonymous votes has none."""
    got = await client.get_messages(peer, ids=[mid])  # the answers' own keys are not saved with the poll
    poll = getattr(getattr(got[0] if got else None, "media", None), "poll", None)
    if not poll or not poll.public_voters:
        return [], []
    place = {a.option: n for n, a in enumerate(poll.answers)}
    found, people, offset, voters = [], [], None, set()
    while True:
        r = await client(functions.messages.GetPollVotesRequest(peer, mid, limit=PAGE, offset=offset))
        for v in r.votes:
            who = utils.get_peer_id(v.peer)
            voters.add(who)
            chosen = getattr(v, "options", None) or [getattr(v, "option", None)]  # several in a multiple choice
            found += [(who, place[o], iso(v.date)) for o in chosen if o in place]
        people += people_of(r)
        offset = r.next_offset
        if not offset or not r.votes or len(voters) >= r.count:
            return found, people


def note(db, mid, what, seen, found, people, when):
    """Save the answer about one message in place of the earlier one, and the counts it was asked with.

    found is None when Telegram refused to answer: what an earlier run saved then stays.
    """
    table = "reacted" if what == "reactions" else "voted"
    if found is not None:
        had = db.execute(f"SELECT 1 FROM {table} WHERE id = ? LIMIT 1", (mid,)).fetchone()
        db.execute(f"DELETE FROM {table} WHERE id = ?", (mid,))
        db.executemany(f"INSERT OR IGNORE INTO {table} VALUES(?, ?, ?, ?)", [(mid, *f) for f in found])
        save_users(db, list({p[0]: p for p in people}.values()), when)
        if found or had:
            db.execute("INSERT OR IGNORE INTO changed VALUES(?)", (mid,))  # the export shows it
    db.execute("INSERT OR REPLACE INTO asked VALUES(?, ?, ?)", (mid, what, seen))


async def fetch_who(pairs, db, when, stats, topics=None):
    """Ask who reacted to every message that has reactions and who voted in every poll that has votes.

    pairs: [(client, the chat as this account knows it)]; the accounts take messages from one queue. An answer
    is saved as it comes, so a stopped run goes on next time, and a message is asked about again only when
    its counts have changed. Returns how many messages were asked about.
    """
    queue = collections.deque(waiting(db, topics))
    total = len(queue)
    if not total:
        return 0
    took = fmt_duration(total * WHO_SECONDS / len(pairs))
    log.info(f"Who reacted and who voted: {total} messages to ask about, about {took}")
    stats.update(phase="who", who=(0, total))
    done, refused = 0, collections.Counter()

    async def run(client, peer):
        nonlocal done
        while queue:
            mid, what, seen = queue.popleft()
            try:
                found, people = await (who_reacted if what == "reactions" else who_voted)(client, peer, mid)
            except errors.FloodWaitError as e:  # a wait too long for the client to sit out by itself
                queue.appendleft((mid, what, seen))
                log.info(f"Telegram asks to wait {fmt_duration(e.seconds)} before it says more")
                await asyncio.sleep(e.seconds + 1)
                continue
            except errors.RPCError as e:
                # the message is gone, its reactions belong to a channel's post, or the poll shows its
                # voters only to those who voted in it
                found, people = None, []
                refused[what, type(e).__name__] += 1
            note(db, mid, what, seen, found, people, when)
            done += 1
            stats["who"] = (done, total)
            if done % SAVE_EVERY == 0:
                db.commit()

    try:
        await all_or_none(run(client, peer) for client, peer in pairs)
    finally:
        db.commit()
    closed = sum(n for (what, error), n in refused.items() if error == "PollVoteRequiredError")
    if closed:
        log.info(f"Polls that show their voters only to those who voted in them: {closed}")
    if other := sum(refused.values()) - closed:
        log.info(f"Messages Telegram did not answer about: {other}")
    return done
