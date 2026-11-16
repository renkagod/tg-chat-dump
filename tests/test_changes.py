"""Edits and deletions: the database migration, the history of edits, deletion marks, Telegram's change log."""

import asyncio
import datetime
import json
import sqlite3
from types import SimpleNamespace

import pytest
from telethon import errors, functions, types
from telethon.tl.types import Channel, ChatPhotoEmpty, Message, PeerChannel, User

from tgdump import changes, fetch
from tgdump.changes import Changes, apply_changes, channel_changes
from tgdump.export import export
from tgdump.reader import write_reader
from tgdump.store import (
    FORMAT,
    SCHEMA,
    get_state,
    mark_deleted,
    open_db,
    outdated,
    plan_full,
    recheck_from,
    save,
    set_pinned,
    set_state,
    to_row,
)

DATE = datetime.datetime(2026, 5, 13, 6, 38, 53, tzinfo=datetime.UTC)
LATER = DATE + datetime.timedelta(minutes=5)
PEER = PeerChannel(1)
NOW = "2026-06-01T00:00:00+00:00"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO tasks(key, lo, hi, cursor) VALUES('k', 0, 100, 101)")
    yield conn
    conn.close()


def msg(id, text="", date=DATE, **kw):
    return Message(id=id, peer_id=PEER, date=date, message=text, **kw)


def rows(*messages, forum=False):
    return [to_row(m, forum) for m in messages]


def reset(db, key="k"):
    db.execute("UPDATE tasks SET cursor=hi + 1, done=0 WHERE key=?", (key,))


def deleted(db):
    return [i for (i,) in db.execute("SELECT id FROM messages WHERE deleted_at IS NOT NULL ORDER BY id")]


# --- migration


def old_database(path, version):
    """A database as 1.3.0 left it: no deleted_at column, no edits, one finished export."""
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, date TEXT, edit_date TEXT, topic_id INTEGER, sender_id INTEGER,"
        " sender TEXT, text TEXT, reply_to INTEGER, reply_top INTEGER, fwd_from TEXT, media TEXT, action TEXT,"
        " grouped_id INTEGER, text_md TEXT, views INTEGER, forwards INTEGER, replies INTEGER, reactions TEXT,"
        " extra TEXT);"
        "CREATE TABLE topics(id INTEGER PRIMARY KEY, title TEXT);"
        "CREATE TABLE tasks(key TEXT PRIMARY KEY, topic INTEGER, lo INTEGER, hi INTEGER, cursor INTEGER,"
        " done INTEGER DEFAULT 0);"
        "CREATE TABLE exports(folder TEXT PRIMARY KEY, max_id INTEGER, rows INTEGER, titles TEXT);"
        "INSERT INTO messages(id, date, text) VALUES(1, '2026-05-13T06:38:53+00:00', 'kept');"
        "INSERT INTO tasks VALUES('range:0', NULL, 0, 1, 1, 1);"
        "INSERT INTO exports VALUES('out/5_Chat', 1, 1, '[]');"
    )
    db.execute(f"PRAGMA user_version={version}")
    db.commit()
    db.close()


def test_old_database_is_migrated_in_place(tmp_path):
    old_database(tmp_path / "a.sqlite", 2)
    db = open_db(tmp_path / "a.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == FORMAT
    assert db.execute("SELECT text, deleted_at FROM messages").fetchall() == [("kept", None)]
    assert not outdated(db)  # 1.3.0 already kept the details
    # the export is written again once, in the new format, and still lists the chat as dumped
    assert db.execute("SELECT max_id, rows FROM exports").fetchall() == [(-1, 1)]
    save(db, "range:0", rows(msg(1, "kept, then edited", edit_date=LATER)))
    assert db.execute("SELECT text FROM edits").fetchall() == [("kept",)]
    db.close()
    db = open_db(tmp_path / "a.sqlite")  # a second open changes nothing
    assert db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 1


def test_database_from_before_details_still_asks_for_a_refetch(tmp_path):
    old_database(tmp_path / "b.sqlite", 0)
    db = open_db(tmp_path / "b.sqlite")
    assert db.execute("PRAGMA user_version").fetchone()[0] == FORMAT and outdated(db)


# --- history of edits


def test_edited_text_keeps_the_earlier_one(db):
    save(db, "k", rows(msg(5, "helo")))
    save(db, "k", rows(msg(5, "hello", edit_date=LATER)))
    save(db, "k", rows(msg(5, "hello", edit_date=LATER)))  # the same state again adds nothing
    third = LATER + datetime.timedelta(hours=1)
    save(db, "k", rows(msg(5, "hello there", edit_date=third)))
    assert db.execute("SELECT id, date, text, replaced FROM edits ORDER BY date").fetchall() == [
        (5, DATE.isoformat(), "helo", LATER.isoformat()),
        (5, LATER.isoformat(), "hello", third.isoformat()),
    ]


def test_changes_that_are_not_edits_leave_no_history(db):
    save(db, "k", rows(msg(5, "")))
    save(db, "k", rows(msg(5, "filled in by a newer version")))  # no edit date: the text was just read better
    save(db, "k", rows(msg(5, "filled in by a newer version", edit_date=LATER)))  # e.g. a bot changed the buttons
    assert db.execute("SELECT COUNT(*) FROM edits").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM changed").fetchone()[0] == 1  # still exported again


# --- deletion marks


def test_messages_missing_from_a_span_read_again_are_marked_deleted(db):
    save(db, "k", rows(*(msg(i) for i in (9, 7, 5, 3))), done=True)
    assert deleted(db) == []
    reset(db)
    gone = (1, NOW)
    assert save(db, "k", rows(msg(9), msg(5)), gone=gone) == 1  # 7 is between them and did not come
    assert deleted(db) == [7]
    assert save(db, "k", [], done=True, gone=gone) == 1  # nothing below 5 came either
    assert deleted(db) == [3, 7]
    assert db.execute("SELECT deleted_at FROM messages WHERE id=7").fetchone() == (NOW,)


def test_deletion_marks_stop_at_the_oldest_visible_message(db):
    save(db, "k", rows(*(msg(i) for i in (9, 7, 5, 3))), done=True)
    reset(db)
    save(db, "k", rows(msg(9)), done=True, gone=(6, NOW))  # this account sees nothing below 6
    assert deleted(db) == [7]


def test_a_message_that_comes_back_loses_the_mark(db):
    save(db, "k", rows(msg(9), msg(7)), done=True)
    assert mark_deleted(db, [7, 8], NOW) == 1  # 8 was never saved
    assert mark_deleted(db, [7], "2026-07-01T00:00:00+00:00") == 0  # the first date stays
    reset(db)
    save(db, "k", rows(msg(9), msg(7)), done=True, gone=(1, NOW))
    assert deleted(db) == []


def test_a_search_does_not_mark_anything(db):
    save(db, "k", rows(msg(9), msg(7)), done=True)
    reset(db)
    save(db, "k", rows(msg(9)), done=True)  # no gone=: only some messages are listed
    assert deleted(db) == []


def test_topic_download_judges_only_its_own_topic(db):
    def topic(to):
        return types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=to)

    head = types.MessageService(id=10, peer_id=PEER, date=DATE, action=types.MessageActionTopicCreate("T", 0))
    saved = [head, msg(11, reply_to=topic(10)), msg(12, reply_to=topic(20)), msg(13, reply_to=topic(10))]
    save(db, "k", rows(*saved, forum=True), done=True)
    db.execute("INSERT INTO tasks(key, topic, lo, cursor) VALUES('topic:10', 10, 0, 0)")
    # the topic is read whole and only 13 is still there; its first message is not among the replies
    save(db, "topic:10", rows(saved[3], forum=True), done=True, gone=(0, NOW))
    assert deleted(db) == [11]


def test_recheck_resets_the_ranges_from_an_id_on(tmp_path):
    db = open_db(tmp_path / "c.sqlite")
    plan_full(db, 1, 12_000)
    db.execute("UPDATE tasks SET done=1, cursor=lo")
    assert recheck_from(db, 20_000) is None
    assert recheck_from(db, 7_000) == 5_000  # the range that holds 7000 starts above 5000
    left = db.execute("SELECT key FROM tasks WHERE done=0 AND cursor=hi + 1 ORDER BY lo").fetchall()
    assert left == [("range:5000",), ("range:10000",)]


def test_pins_are_put_and_taken_off(db):
    save(db, "k", rows(msg(5, "a"), msg(6, "b", pinned=True)))
    set_pinned(db, {5: True, 6: False, 99: True})
    assert db.execute("SELECT id, extra FROM messages ORDER BY id").fetchall() == [(5, '{"pinned": true}'), (6, None)]


# --- Telegram's change log


def difference(pts, updates=(), final=True):
    return types.updates.ChannelDifference(pts, new_messages=[], other_updates=list(updates), chats=[], users=[],
                                           final=final)  # fmt: skip


def too_long(pts):
    return types.updates.ChannelDifferenceTooLong(SimpleNamespace(pts=pts), messages=[], chats=[], users=[])


class LogClient:
    """Answers updates.getChannelDifference with the given pages, one per request."""

    def __init__(self, *pages):
        self.pages, self.asked = list(pages), []

    async def __call__(self, request):
        assert isinstance(request, functions.updates.GetChannelDifferenceRequest)
        assert request.limit == changes.LOG_LIMIT  # a small limit makes Telegram answer "too long" to any gap
        self.asked.append(request.pts)
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return page


def test_change_log_is_read_page_by_page():
    edit = types.UpdateEditChannelMessage(msg(5, "new"), pts=11, pts_count=1)
    gone = types.UpdateDeleteChannelMessages(channel_id=1, messages=[7, 8], pts=13, pts_count=2)
    pin = types.UpdatePinnedChannelMessages(channel_id=1, messages=[9], pts=14, pts_count=1, pinned=True)
    unpin = types.UpdatePinnedChannelMessages(channel_id=1, messages=[4], pts=15, pts_count=1)
    client = LogClient(difference(13, [edit, gone], final=False), difference(15, [pin, unpin]))
    found = asyncio.run(channel_changes(client, PEER, 10))
    assert client.asked == [10, 13]
    assert (found.edited, found.deleted, found.pinned) == ({5}, {7, 8}, {9: True, 4: False})
    assert found.complete and found.pts == 15


def test_nothing_changed():
    found = asyncio.run(channel_changes(LogClient(types.updates.ChannelDifferenceEmpty(10, final=True)), PEER, 10))
    assert found.complete and found.pts == 10 and not found.edited


@pytest.mark.parametrize("answer", [too_long(500), errors.PersistentTimestampInvalidError(None)])
def test_log_that_does_not_reach_back_is_not_complete(answer):
    found = asyncio.run(channel_changes(LogClient(answer), PEER, 10))
    assert not found.complete
    assert found.pts == (500 if not isinstance(answer, Exception) else None)


def test_log_that_stands_still_does_not_loop():
    found = asyncio.run(channel_changes(LogClient(difference(10, final=False)), PEER, 10))
    assert not found.complete


def test_changes_are_applied_to_saved_messages_only(db):
    save(db, "k", rows(msg(5, "old"), msg(6, "stays"), msg(7, "goes"), msg(8, "edited, then deleted"), msg(9, "new")))

    class Client:
        asked = []

        async def get_messages(self, entity, ids):
            self.asked.append(ids)
            return [msg(5, "new", edit_date=LATER) if i == 5 else None for i in ids]

    found = Changes(edited={5, 8, 9, 50}, deleted={7, 70}, pinned={6: True})
    marked = asyncio.run(apply_changes(Client(), PEER, db, found, False, frozenset(), NOW, upto=8))
    # 50 is not saved: it comes with the download, if it is in what is dumped; 9 was downloaded by this very run
    assert Client.asked == [[5, 8]]
    assert marked == 2 and deleted(db) == [7, 8]  # 8 is gone by now, Telegram has no such message any more
    assert db.execute("SELECT text FROM messages WHERE id=5").fetchone() == ("new",)
    assert db.execute("SELECT text FROM edits").fetchall() == [("old",)]
    assert json.loads(db.execute("SELECT extra FROM messages WHERE id=6").fetchone()[0]) == {"pinned": True}


# --- export


def test_export_shows_edits_and_deletions(db, tmp_path):
    save(db, "k", rows(msg(5, "helo"), msg(6, "bye")))
    save(db, "k", rows(msg(5, "hello", edit_date=LATER)))
    mark_deleted(db, [6], NOW)
    out = export(db, 5, "Chat", out_root=tmp_path)
    txt = (out / "messages.txt").read_text(encoding="utf-8").splitlines()
    assert txt == [
        "[2026-05-13 06:38:53] #5 ? (edited 2026-05-13 06:43:53): hello",
        "  [until 2026-05-13 06:43:53: helo]",
        "[2026-05-13 06:38:53] #6 ? (deleted): bye",
    ]
    first, second = map(json.loads, (out / "messages.jsonl").read_text(encoding="utf-8").splitlines())
    assert first["edits"] == [{"date": DATE.isoformat(), "text": "helo", "replaced": LATER.isoformat()}]
    assert second["deleted_at"] == NOW and "deleted_at" not in first


def test_repeat_export_rewrites_only_the_topics_that_changed(db, tmp_path):
    def topic(to):
        return types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=to)

    save(db, "k", rows(msg(11, "a", reply_to=topic(10)), msg(21, "b", reply_to=topic(20)), forum=True))
    out = export(db, 5, "Forum", out_root=tmp_path)
    kept = out / "20" / "messages.txt"
    kept.write_text(kept.read_text(encoding="utf-8") + "a line of my own\n", encoding="utf-8")
    save(db, "k", rows(msg(11, "A", edit_date=LATER, reply_to=topic(10)), forum=True))
    save(db, "k", rows(msg(22, "c", reply_to=topic(20)), forum=True))
    export(db, 5, "Forum", out_root=tmp_path)
    assert "(edited 2026-05-13 06:43:53): A" in (out / "10" / "messages.txt").read_text(encoding="utf-8")
    assert kept.read_text(encoding="utf-8").splitlines()[1:] == ["a line of my own", "[2026-05-13 06:38:53] #22 ?: c"]
    assert db.execute("SELECT COUNT(*) FROM changed").fetchone()[0] == 0


def test_reader_gets_deletions_and_earlier_texts(db, tmp_path):
    save(db, "k", rows(msg(5, "helo"), msg(6, "bye")))
    save(db, "k", rows(msg(5, "hello", edit_date=LATER)))
    mark_deleted(db, [6], NOW)
    write_reader(db, tmp_path, "Chat", {})
    data = (tmp_path / ".reader" / "all.0.js").read_text(encoding="utf-8")
    assert f'{{"edits":[[{int(LATER.timestamp())},"helo"]]}}' in data
    assert '{"deleted":1780272000}' in data


def test_reader_rewrites_the_part_of_an_edited_message(db, tmp_path, monkeypatch):
    monkeypatch.setattr("tgdump.reader.PART_ROWS", 2)
    save(db, "k", rows(*(msg(i, f"m{i}") for i in range(1, 6))))
    write_reader(db, tmp_path, "Chat", {})
    parts = [tmp_path / ".reader" / f"all.{n}.js" for n in range(3)]
    before = [p.read_text(encoding="utf-8") for p in parts]
    save(db, "k", rows(msg(3, "m3, edited", edit_date=LATER)))
    write_reader(db, tmp_path, "Chat", {}, topics={None}, since=5, changed={None: 3})
    after = [p.read_text(encoding="utf-8") for p in parts]
    assert after[0] == before[0] and "m3, edited" in after[1]


# --- a whole run


class Found(list):
    total = 0


class Session:
    """The people an account has met: it can ask Telegram only about them."""

    def __init__(self):
        self.met = set()

    def get_input_entity(self, peer):
        if peer.user_id not in self.met:
            raise ValueError("never met")
        return types.InputPeerUser(peer.user_id, access_hash=1)


class Telegram:
    """A supergroup as Telegram keeps it: its messages, its change log and the pts the log stands at."""

    def __init__(self, messages, entity=None):
        self.messages = {m.id: m for m in messages}
        self.pts, self.log, self.too_long, self.asked = 100, [], False, []
        self.entity = entity or Channel(7, "Club", ChatPhotoEmpty(), DATE, megagroup=True, access_hash=1)
        self.session, self.flood_sleep_threshold = Session(), 60
        self.full = {}  # what the chat's own page says
        self.people, self.bios = {}, {}  # user id -> the user as shown now, and their bio
        self.topics, self.emoji = [], {}  # the forum's topics; custom emoji id -> the emoji it stands for
        self.users_asked = 0  # how many requests about people were made
        self.users_limit = None  # how many of them Telegram answers before it says to wait
        self.content = {}  # photo or document id -> the bytes of the file
        self.downloads = []  # (photo or document id, the data centre asked), in the order asked for
        self.looked_up = 0  # how many messages were asked for by id
        self.unlisted = set()  # ids that the list of a topic leaves out

    def listed(self, topic):
        """The messages a forum topic lists: all of the chat for no topic, and never the topic's first one."""
        found = []
        for m in self.messages.values():
            r = m.reply_to
            own = (r.reply_to_top_id or r.reply_to_msg_id) if r is not None and r.forum_topic else 1
            made = isinstance(m.action, types.MessageActionTopicCreate)
            if topic is None or (own == topic and not made and m.id not in self.unlisted):
                found.append(m)
        return found

    async def get_entity(self, peer):
        return self.entity

    async def get_me(self):
        return User(id=1, first_name="Me")

    async def get_messages(
        self, entity, limit=None, ids=None, reverse=False, offset_date=None, filter=None, reply_to=None
    ):
        if ids is not None:
            self.looked_up += len(ids)
            return [self.messages.get(i) for i in ids]
        found = sorted(self.listed(reply_to), key=lambda m: m.id, reverse=not reverse)
        if offset_date:
            found = [m for m in found if m.date < offset_date]
        out = Found(found[:limit])
        out.total = len(self.listed(reply_to))
        return out

    def iter_messages(self, entity, offset_id=0, min_id=0, wait_time=None, filter=None, reply_to=None):
        async def walk():
            for m in sorted(self.listed(reply_to), key=lambda m: -m.id):
                if filter is types.InputMessagesFilterPinned and not m.pinned:
                    continue
                if m.id > min_id and (not offset_id or m.id < offset_id):
                    yield m

        return walk()

    def iter_download(self, file, dc_id=None, file_size=None):
        async def chunks():
            self.downloads.append((file.id, dc_id))
            if file.id not in self.content:
                raise errors.FileReferenceExpiredError(None)
            yield self.content[file.id]

        return chunks()

    async def __call__(self, request):
        if isinstance(request, functions.channels.GetFullChannelRequest):
            return SimpleNamespace(full_chat=SimpleNamespace(pts=self.pts, **self.full), chats=[])
        if isinstance(request, functions.users.GetFullUserRequest):
            uid = getattr(request.id, "user_id", None) or request.id.id
            if isinstance(request.id, types.PeerUser) and uid not in self.session.met:
                raise ValueError("never met")
            return SimpleNamespace(full_user=SimpleNamespace(about=self.bios.get(uid)), chats=[])
        if isinstance(request, functions.users.GetUsersRequest):
            self.users_asked += 1
            if self.users_limit is not None and self.users_asked > self.users_limit:
                raise errors.FloodWaitError(None, 61)
            return [self.people[u.user_id] for u in request.id if u.user_id in self.people]
        if isinstance(request, functions.messages.GetForumTopicsRequest):
            return SimpleNamespace(topics=self.topics, messages=[])
        if isinstance(request, functions.messages.GetCustomEmojiDocumentsRequest):
            sticker = types.InputStickerSetEmpty()
            return [
                SimpleNamespace(id=i, attributes=[types.DocumentAttributeCustomEmoji(self.emoji[i], sticker)])
                for i in request.document_id
                if i in self.emoji
            ]
        assert isinstance(request, functions.updates.GetChannelDifferenceRequest)
        self.asked.append(request.pts)
        return too_long(self.pts) if self.too_long else difference(self.pts, self.log)


def run(telegram, **kw):
    stats = {}
    asyncio.run(fetch.dump_chat([telegram], 7, stats=stats, **kw))
    return stats


def day(id, n, text=None, **kw):
    return msg(id, text or f"m{id}", date=DATE + datetime.timedelta(days=n), **kw)


def state(home, key):
    db = sqlite3.connect(home / "7.sqlite")
    try:
        return get_state(db, key), deleted(db), db.execute("SELECT id, text FROM edits ORDER BY id").fetchall()
    finally:
        db.close()


def test_repeat_run_reads_the_change_log(home):
    tg = Telegram([day(1, 0), day(2, 1), day(3, 9)])
    assert run(tg)["new"] == 3
    assert state(home, "pts") == (100, [], []) and tg.asked == []  # the first run only notes where the log stands
    # since then: a very old message was edited, another deleted, one new message came
    tg.messages[1] = day(1, 0, "m1, edited", edit_date=LATER)
    del tg.messages[2]
    tg.messages[4] = day(4, 10)
    tg.log = [
        types.UpdateEditChannelMessage(tg.messages[1], pts=101, pts_count=1),
        types.UpdateDeleteChannelMessages(channel_id=7, messages=[2], pts=102, pts_count=1),
    ]
    tg.pts = 103
    stats = run(tg)
    assert tg.asked == [100]
    assert (stats["new"], stats["edited"], stats["deleted"], stats["stored"]) == (1, 1, 1, 4)
    assert state(home, "pts") == (103, [2], [(1, "m1")])
    txt = (home / "out" / "7_Club" / "messages.txt").read_text(encoding="utf-8")
    assert "#1 ? (edited 2026-05-13 06:43:53): m1, edited" in txt and "#2 ? (deleted): m2" in txt


def ago(id, days, text=None, **kw):
    """A message written that many days before the test runs: the check counts back from the last run."""
    when = datetime.datetime.now(datetime.UTC).replace(microsecond=0) - datetime.timedelta(days=days)
    return msg(id, text or f"m{id}", date=when, **kw)


def test_without_a_log_the_last_days_are_downloaded_again(home):
    tg = Telegram([ago(1, 30), ago(2002, 5), ago(2003, 1), ago(2004, 1)])  # the ids span three download ranges
    run(tg)
    tg.too_long = True
    tg.messages[1] = ago(1, 30, "an edit too old to be looked for", edit_date=LATER)
    tg.messages[2003] = ago(2003, 1, "m2003, edited", edit_date=LATER)
    del tg.messages[2004]
    tg.pts = 900
    stats = run(tg)
    assert (stats["new"], stats["edited"], stats["deleted"], stats["stored"]) == (0, 1, 1, 4)
    assert state(home, "pts") == (900, [2004], [(2003, "m2003")])
    db = sqlite3.connect(home / "7.sqlite")
    assert db.execute("SELECT text FROM messages WHERE id=1").fetchone() == ("m1",)
    db.close()
    tg.too_long = False
    run(tg)
    assert tg.asked == [100, 900]  # the log is used again from where it stands now


def test_private_chat_has_no_log_and_checks_the_last_days(home):
    friend = User(id=7, first_name="Friend", access_hash=1)
    tg = Telegram([ago(1, 40), ago(2, 30), ago(2002, 1), ago(2003, 1)], entity=friend)
    run(tg)
    del tg.messages[2], tg.messages[2002]
    assert run(tg)["deleted"] == 1  # the one from a month ago is not looked at
    assert tg.asked == [] and state(home, "pts")[:2] == (None, [2002])


def test_recheck_zero_leaves_saved_messages_alone(home):
    tg = Telegram([day(1, 0), day(2, 9)])
    run(tg, recheck=0)
    del tg.messages[2]
    assert run(tg, recheck=0)["deleted"] == 0
    assert tg.asked == [] and state(home, "pts")[:2] == (None, [])


def test_set_state_forgets_none():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    set_state(db, "pts", 5)
    assert get_state(db, "pts") == 5
    set_state(db, "pts", None)
    assert get_state(db, "pts", "nothing") == "nothing"


def test_a_worker_that_fails_stops_the_others(home):
    slow, broken, left = Telegram([day(1, 0), day(2000, 0)]), Telegram([day(1, 0), day(2000, 0)]), []

    def stuck(entity, offset_id=0, min_id=0, **kw):
        async def walk():
            try:
                await asyncio.sleep(10)
                yield day(1, 0)
            finally:
                left.append("stopped")

        return walk()

    def dropped(entity, **kw):
        async def walk():
            raise ConnectionError("dropped")
            yield

        return walk()

    slow.iter_messages, broken.iter_messages = stuck, dropped

    async def go():
        with pytest.raises(ConnectionError):
            await fetch.dump_chat([slow, broken], 7, workers=1)
        return left

    assert asyncio.run(go()) == ["stopped"]  # before the database was closed under it


def test_a_chat_from_the_dialogs_is_not_asked_for_again(home):
    tg, stranger = Telegram([day(1, 0)]), Telegram([day(1, 0)])

    async def asked(*args):
        raise AssertionError("asked Telegram again")

    async def unknown(peer):
        raise ValueError("never met")

    tg.get_entity, stranger.get_entity, stranger.get_dialogs = asked, unknown, asked
    asyncio.run(fetch.dump_chat([tg], 7, seen={tg: tg.entity}))
    assert (home / "7.sqlite").exists()
    # an account whose dialogs do not have the chat looks it up once, without reading its dialogs again
    with pytest.raises(ValueError, match="None of the accounts"):
        asyncio.run(fetch.dump_chat([stranger], 7, seen={stranger: None}))


def test_the_extras_run_together_and_the_chat_is_exported_once_more(home, monkeypatch):
    started, exports = set(), []

    def extra(name, found):
        async def asked(*args):
            started.add(name)
            for _ in range(100):  # each waits for the others to have begun: one after another, none would end
                if started == {"bio", "who", "files"}:
                    return found
                await asyncio.sleep(0)
            raise AssertionError(f"{name} ran alone")

        return asked

    monkeypatch.setattr(fetch, "fetch_bios", extra("bio", 3))
    monkeypatch.setattr(fetch, "fetch_who", extra("who", 0))
    monkeypatch.setattr(fetch, "download_files", extra("files", 2))
    real = fetch.export_db
    monkeypatch.setattr(fetch, "export_db", lambda *a, **kw: exports.append(1) or real(*a, **kw))
    run(Telegram([day(1, 0)]), options=frozenset({"bio", "who", "files"}))
    assert len(exports) == 2  # the dump's own export, and one for the files the extras found
