"""The "who" extra: who reacted to a message and who voted in a poll, how it is asked for and where it shows."""

import asyncio
import json
import sqlite3

from telethon import errors, functions, types
from test_changes import DATE, LATER, NOW, Telegram, day, run

from tgdump import who
from tgdump.scope import Scope, parse_date
from tgdump.store import open_db
from tgdump.who import fetch_who, waiting

WHO = frozenset({"who"})
OUT = "out/7_Club"
ANN, BOB = types.User(11, first_name="Ann", username="ann"), types.User(12, first_name="Bob")
NEWS = types.Channel(30, "News", types.ChatPhotoEmpty(), DATE, broadcast=True)


def emoji(e):
    return types.ReactionEmoji(e)


def reactions(*counts):
    return types.MessageReactions(results=[types.ReactionCount(r, n) for r, n in counts], can_see_list=True)


def reacted(user, reaction, date=DATE):
    peer = types.PeerUser(user.id) if isinstance(user, types.User) else types.PeerChannel(user.id)
    return types.MessagePeerReaction(peer, date, reaction)


def poll(*answers, voters=(), public=True, multiple=False):
    said = types.TextWithEntities
    made = types.Poll(
        1,
        said("Lunch?", []),
        [types.PollAnswer(said(a, []), bytes([65 + n])) for n, a in enumerate(answers)],
        hash=0,
        public_voters=public,
        multiple_choice=multiple,
    )
    results = [types.PollAnswerVoters(bytes([65 + n]), voters=v) for n, v in enumerate(voters)]
    return types.MessageMediaPoll(made, types.PollResults(results=results, total_voters=sum(voters)))


class Club(Telegram):
    """A group that also says who reacted and who voted, a page at a time."""

    def __init__(self, messages, **kw):
        super().__init__(messages, **kw)
        self.reacted, self.voted, self.refuses = {}, {}, {}  # message id -> the list, or the error it answers with
        self.lists, self.page, self.wait = [], 100, None  # (message id, offset) asked; people a page; a flood wait

    def pages(self, request, found, answer):
        if self.wait:
            seconds, self.wait = self.wait, None
            raise errors.FloodWaitError(None, seconds)
        self.lists.append((request.id, request.offset))
        if request.id in self.refuses:
            raise self.refuses[request.id](None)
        start = int(request.offset or 0)
        part, more = found[start : start + self.page], start + self.page < len(found)
        people = {p.user_id if isinstance(p, types.PeerUser) else p.channel_id for p in map(self.peer_of, part)}
        users = [u for u in (ANN, BOB) if u.id in people]
        chats = [NEWS] if NEWS.id in people else []
        return answer(len(found), part, chats, users, str(start + self.page) if more else None)

    @staticmethod
    def peer_of(one):
        return getattr(one, "peer_id", None) or one.peer

    async def __call__(self, request):
        if isinstance(request, functions.messages.GetMessageReactionsListRequest):
            assert request.limit == who.PAGE
            return self.pages(request, self.reacted.get(request.id, []), types.messages.MessageReactionsList)
        if isinstance(request, functions.messages.GetPollVotesRequest):
            return self.pages(request, self.voted.get(request.id, []), types.messages.VotesList)
        return await super().__call__(request)


def table(home, sql, name="7"):
    db = sqlite3.connect(home / f"{name}.sqlite")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def club():
    """Two messages with reactions, one without, and a poll two people voted in."""
    custom = types.ReactionCustomEmoji(777)
    tg = Club(
        [
            day(1, 0, "hello", reactions=reactions((emoji("👍"), 2), (custom, 1))),
            day(2, 0, "nothing under it"),
            day(3, 1, "news", reactions=reactions((emoji("🔥"), 1))),
            day(4, 1, "", media=poll("Pizza", "Sushi", voters=(1, 1))),
        ]
    )
    tg.reacted = {
        1: [reacted(ANN, emoji("👍")), reacted(BOB, emoji("👍"), LATER), reacted(ANN, custom, LATER)],
        3: [reacted(NEWS, emoji("🔥"))],
    }
    tg.voted = {
        4: [
            types.MessagePeerVote(types.PeerUser(12), b"B", DATE),
            types.MessagePeerVote(types.PeerUser(11), b"A", LATER),
        ]
    }
    return tg


def test_who_reacted_and_who_voted_are_saved_and_shown(home):
    tg = club()
    stats = run(tg, options=WHO | {"html", "tdesktop"})
    assert stats["who"] == (3, 3)
    assert sorted(i for i, _ in tg.lists) == [1, 3, 4]  # the message without reactions is not asked about
    assert table(home, "SELECT id, user_id, reaction FROM reacted ORDER BY id, date, reaction") == [
        (1, 11, "👍"),
        (1, 11, "custom:777"),
        (1, 12, "👍"),
        (3, -1000000000030, "🔥"),  # a channel reacting as itself
    ]
    assert table(home, "SELECT id, user_id, option FROM voted ORDER BY user_id") == [(4, 11, 0), (4, 12, 1)]
    assert dict(table(home, "SELECT id, name FROM users")) == {11: "Ann", 12: "Bob", -1000000000030: "News"}
    lines = [json.loads(ln) for ln in (home / OUT / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
    first, _, third, fourth = lines
    assert first["reactions"] == {"👍": 2, "custom:777": 1}  # the counts are saved with "who", without "meta"
    assert "views" not in first and "reacted" not in lines[1]
    assert [(r["name"], r["reaction"]) for r in first["reacted"]] == [
        ("Ann", "custom:777"),
        ("Ann", "👍"),
        ("Bob", "👍"),
    ]
    assert first["reacted"][0] == {
        "user_id": 11,
        "name": "Ann",
        "reaction": "custom:777",
        "date": "2026-05-13T06:43:53+00:00",
    }
    assert third["reacted"][0]["name"] == "News"
    assert [(v["name"], v["option"]) for v in fourth["voted"]] == [("Ann", 0), ("Bob", 1)]
    text = (home / OUT / "messages.txt").read_text(encoding="utf-8")
    assert "  [reacted: custom:777 Ann · 👍 Ann, Bob]\n" in text
    assert "  [voted: Pizza: Ann · Sushi: Bob]\n" in text
    part = (home / OUT / ".reader" / "all.0.js").read_text(encoding="utf-8")
    assert '"reacted":{"👍":["Ann","Bob"],"custom:777":["Ann"]}' in part
    assert '"voted":{"1":["Bob"],"0":["Ann"]}' in part  # under each answer, in the order they voted

    result = json.loads((home / OUT / "result.json").read_text(encoding="utf-8"))
    thumbs = result["messages"][0]["reactions"][0]
    assert (thumbs["emoji"], thumbs["count"]) == ("👍", 2)
    assert [(r["from"], r["from_id"]) for r in thumbs["recent"]] == [("Ann", "user11"), ("Bob", "user12")]


def test_a_message_is_asked_about_again_only_when_its_counts_change(home):
    tg = club()
    run(tg, options=WHO)
    tg.lists.clear()
    run(tg, options=WHO)
    assert tg.lists == []
    tg.messages[3].reactions = reactions((emoji("🔥"), 2))
    tg.messages[3].edit_date = LATER  # the change log names the message, so it is saved again
    tg.log = [types.UpdateEditChannelMessage(tg.messages[3], pts=101, pts_count=1)]
    tg.pts = 101
    tg.reacted[3] = [reacted(NEWS, emoji("🔥")), reacted(BOB, emoji("🔥"), LATER)]
    run(tg, options=WHO)
    assert [i for i, _ in tg.lists] == [3]
    assert table(home, "SELECT user_id FROM reacted WHERE id = 3 ORDER BY date") == [(-1000000000030,), (12,)]
    line = json.loads((home / OUT / "messages.jsonl").read_text(encoding="utf-8").splitlines()[2])
    assert [r["name"] for r in line["reacted"]] == ["News", "Bob"]  # the export follows


def test_long_lists_come_in_pages_and_a_wait_is_sat_out(home, monkeypatch):
    slept, real = [], asyncio.sleep

    async def sleep(seconds):
        slept.append(seconds)
        await real(0)

    monkeypatch.setattr(who.asyncio, "sleep", sleep)
    tg = club()
    tg.page, tg.wait = 2, 300
    run(tg, options=WHO)
    assert 301 in slept
    assert [offset for i, offset in tg.lists if i == 1] == [None, "2"]
    assert len(table(home, "SELECT * FROM reacted WHERE id = 1")) == 3


def test_what_telegram_refuses_is_not_asked_again_and_keeps_what_was_saved(home, caplog):
    tg = club()
    run(tg, options=WHO)
    tg.messages[1].reactions = reactions((emoji("👍"), 3))
    tg.messages[4].media = poll("Pizza", "Sushi", voters=(2, 1))
    tg.log = [types.UpdateEditChannelMessage(tg.messages[i], pts=100 + i, pts_count=1) for i in (1, 4)]
    tg.pts = 104
    tg.refuses = {1: errors.MsgIdInvalidError, 4: errors.PollVoteRequiredError}
    with caplog.at_level("INFO"):
        run(tg, options=WHO)
    assert "only to those who voted in them: 1" in caplog.text and "did not answer about: 1" in caplog.text
    assert len(table(home, "SELECT * FROM reacted WHERE id = 1")) == 3
    assert len(table(home, "SELECT * FROM voted")) == 2
    tg.lists.clear()
    run(tg, options=WHO)
    assert tg.lists == []


def test_anonymous_polls_and_channels_are_not_asked(home, caplog):
    tg = Club([day(1, 0, "", media=poll("Yes", "No", voters=(3, 1), public=False))])
    run(tg, options=WHO)
    assert tg.lists == [] and table(home, "SELECT what, seen FROM asked") == [("votes", "4")]
    channel = types.Channel(7, "News", types.ChatPhotoEmpty(), DATE, broadcast=True, access_hash=1)
    posts = Club([day(1, 0, "post", reactions=reactions((emoji("👍"), 9)))], entity=channel)
    with caplog.at_level("INFO"):
        run(posts, options=WHO)
    assert posts.lists == [] and "does not say who reacted" in caplog.text


def test_filters_and_topics_ask_about_their_own_messages_only(home):
    tg = club()
    run(tg, options=frozenset({"meta"}))  # the whole chat is here, saved with its counts
    run(tg, options=WHO, scope=Scope(parse_date("2026-05-14")))
    assert sorted(i for i, _ in tg.lists) == [3, 4]  # the first day's message is outside the filter
    assert table(home, "SELECT COUNT(*) FROM asked") == [(0,)]  # the whole dump was not asked about
    part = next(home.glob("7_*.sqlite")).stem
    assert table(home, "SELECT id, what FROM asked ORDER BY id", part) == [(3, "reactions"), (4, "votes")]
    folder = next((home / "out").glob("7_Club_since-*"))
    assert "  [reacted: 🔥 News]\n" in (folder / "messages.txt").read_text(encoding="utf-8")
    tg.lists.clear()
    run(tg, options=WHO, scope=Scope(parse_date("2026-05-14")))
    assert tg.lists == []  # the filtered dump remembers what it asked


def test_several_answers_of_one_voter(tmp_path):
    tg = Club([day(1, 0, "", media=poll("Mon", "Tue", "Wed", voters=(1, 1, 0), multiple=True))])
    tg.voted = {1: [types.MessagePeerVoteMultiple(types.PeerUser(11), [b"A", b"B"], DATE)]}
    db = open_db(tmp_path / "7.sqlite")
    extra = json.dumps({"poll": {"total_voters": 1}})
    db.execute("INSERT INTO messages(id, date, media, extra) VALUES(1, ?, 'MessageMediaPoll', ?)", (NOW, extra))
    assert waiting(db) == [(1, "votes", "1")] and waiting(db, [5]) == []
    stats = {}
    assert asyncio.run(fetch_who([(tg, tg.entity)], db, NOW, stats)) == 1
    assert db.execute("SELECT user_id, option FROM voted ORDER BY option").fetchall() == [(11, 0), (11, 1)]
    assert waiting(db) == [] and db.execute("SELECT id FROM changed").fetchall() == [(1,)]
    db.close()


def test_a_worker_that_fails_stops_the_others():
    from tgdump.util import all_or_none

    left = []

    async def fails():
        raise ConnectionError("dropped")

    async def slow():
        try:
            await asyncio.sleep(10)
        finally:
            left.append("stopped")

    async def both():
        await all_or_none([slow(), fails()])

    try:
        asyncio.run(both())
    except ConnectionError:
        pass
    assert left == ["stopped"]  # before the error went on, so nothing writes to a closed database afterwards


def test_a_reaction_without_a_date_is_still_written_for_telegram_desktop():
    from tgdump.tdesktop import reaction_list

    found = reaction_list({"👍": 1}, [{"user_id": 11, "name": "Ann", "reaction": "👍", "date": None}])
    assert found[0]["recent"] == [{"from": "Ann", "from_id": "user11"}]


def test_what_is_said_of_messages_is_read_only_for_those_written(tmp_path):
    from tgdump.export import earlier_texts, who_said
    from tgdump.store import saved_files

    db = open_db(tmp_path / "7.sqlite")
    for mid, topic in ((1, 5), (2, 5), (3, 9)):
        db.execute("INSERT INTO messages(id, date, topic_id) VALUES(?, ?, ?)", (mid, NOW, topic))
        db.execute("INSERT INTO edits(id, date, text) VALUES(?, ?, 'before')", (mid, NOW))
        db.execute("INSERT INTO files(id, path, size) VALUES(?, ?, 10)", (mid, f"files/{mid}.jpg"))
        db.execute("INSERT INTO reacted VALUES(?, 11, '👍', ?)", (mid, NOW))
        db.execute("INSERT INTO voted VALUES(?, 11, 0, ?)", (mid, NOW))
    mine, args = " AND m.topic_id IS ? AND m.id > ?", (5, 1)  # what an appending export of topic 5 writes
    assert list(earlier_texts(db, mine, args)) == [2] and list(saved_files(db, mine, args)) == [2]
    assert [list(found) for found in who_said(db, mine, args)] == [[2], [2]]
    assert list(earlier_texts(db)) == list(saved_files(db)) == [1, 2, 3]  # with no condition, all of them
    assert [list(found) for found in who_said(db)] == [[1, 2, 3], [1, 2, 3]]
    db.close()
