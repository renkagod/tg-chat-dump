"""What a run saves besides the messages: who wrote, the chat itself, forum topics and pins."""

import json
import re
import sqlite3

from telethon import types
from telethon.tl.types import Channel, ChatBannedRights, ChatPhoto, ChatPhotoEmpty, PeerUser, User
from test_changes import DATE, Telegram, day, msg, old_database, rows, run

from tgdump import reader
from tgdump.store import PIN_MARK, SCHEMA, get_chat, open_db, save, sync_pins
from tgdump.util import iso

ALICE = User(id=11, first_name="Alice", username="alice")
HELPER = User(id=12, first_name="Helper", username="helper_bot", bot=True)
BIO = frozenset({"bio"})
HTML = frozenset({"html"})
OUT = "out/7_Club"


def said(id, user, text="hi", days=0, **kw):
    """A message with its sender, as Telegram hands it over."""
    m = day(id, days, text, from_id=PeerUser(user.id), **kw)
    m._sender = user
    return m


def query(home, sql):
    db = sqlite3.connect(home / "7.sqlite")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def people(home):
    return query(home, "SELECT id, name, username, bot FROM users ORDER BY id")


def age(home):
    """Make the saved people look as if the run that saw them was long ago."""
    db = sqlite3.connect(home / "7.sqlite")
    db.execute("UPDATE users SET updated='2026-01-01T00:00:00+00:00'")
    db.commit()
    db.close()


def page(home):
    return (home / OUT / "index.html").read_text(encoding="utf-8")


# --- who wrote


def test_run_saves_who_wrote(home):
    run(Telegram([said(1, ALICE), said(2, HELPER), said(3, ALICE), day(4, 0)]))
    assert people(home) == [(11, "Alice", "alice", 0), (12, "Helper", "helper_bot", 1)]
    lines = (home / OUT / "users.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["username"] for ln in lines] == ["alice", "helper_bot"]


def test_people_saved_before_are_asked_about_again(home):
    tg = Telegram([said(1, ALICE)])
    run(tg)
    assert tg.users_asked == 0  # everyone was just seen
    age(home)
    tg.session.met = {11}
    tg.people[11] = User(id=11, first_name="Alice", last_name="Moss", username="amoss")
    run(tg)
    assert people(home) == [(11, "Alice Moss", "amoss", 0)]
    assert query(home, "SELECT sender FROM messages") == [("Alice",)]  # the message keeps the name it came with


def test_deleted_account_keeps_its_last_name(home):
    tg = Telegram([said(1, ALICE)])
    run(tg)
    age(home)
    tg.session.met = {11}
    tg.people[11] = User(id=11, deleted=True)
    run(tg)
    assert query(home, "SELECT name, username, deleted FROM users") == [("Alice", None, 1)]


def test_no_run_waits_for_telegram_to_tell_about_people(home):
    crowd = [User(id=1000 + i, first_name=f"Guest {i}") for i in range(250)]
    tg = Telegram([said(i + 1, u) for i, u in enumerate(crowd)])
    run(tg)
    age(home)
    tg.session.met = {u.id for u in crowd}
    tg.people = {u.id: User(id=u.id, first_name="Renamed") for u in crowd}
    tg.users_limit = 1  # the second request is told to wait a minute
    run(tg)
    assert query(home, "SELECT COUNT(*) FROM users WHERE name = 'Renamed'") == [(100,)]
    assert tg.users_asked == 2 and tg.flood_sleep_threshold == 60  # stopped at once, patience as it was
    tg.users_asked = 0
    run(tg)  # the next run goes on with those not asked about yet
    assert query(home, "SELECT COUNT(*) FROM users WHERE name = 'Renamed'") == [(200,)]


def test_an_account_asks_only_about_people_it_has_met(home):
    tg = Telegram([said(1, ALICE), said(2, HELPER)])
    run(tg)
    age(home)
    tg.session.met = {12}
    tg.people = {11: User(id=11, first_name="Never asked"), 12: User(id=12, first_name="Helper 2", bot=True)}
    run(tg)
    assert [p[1] for p in people(home)] == ["Alice", "Helper 2"]


def test_database_from_1_4_lists_the_people_of_its_messages(tmp_path):
    old_database(tmp_path / "a.sqlite", 3)
    raw = sqlite3.connect(tmp_path / "a.sqlite")
    raw.executescript(
        "INSERT INTO messages(id, date, sender_id, sender) VALUES(2, '2026-05-13T07:00:00+00:00', 11, 'Alice');"
        "INSERT INTO messages(id, date, sender_id, sender) VALUES(3, '2026-05-14T07:00:00+00:00', 11, 'Alice M.');"
        "INSERT INTO messages(id, date, sender_id, sender) VALUES(4, '2026-05-14T08:00:00+00:00', -1007, 'Club');"
    )
    raw.close()
    db = open_db(tmp_path / "a.sqlite")
    # under the name on their newest message; Telegram has not been asked yet
    assert db.execute("SELECT id, name, updated FROM users ORDER BY id").fetchall() == [
        (-1007, "Club", None),
        (11, "Alice M.", None),
    ]
    assert db.execute("SELECT max_id FROM exports").fetchall() == [(-1,)]  # the reader is written again, with pins
    db.close()


# --- the bio extra


def bios(home):
    return query(home, "SELECT id, bio, bio_at IS NOT NULL FROM users ORDER BY id")


def test_bio_is_asked_once_per_person_and_only_with_the_extra(home):
    tg = Telegram([said(1, ALICE), said(2, HELPER)])
    tg.session.met = {11, 12}
    tg.bios = {11: "Reads a lot"}
    run(tg)
    assert bios(home) == [(11, None, 0), (12, None, 0)]
    stats = run(tg, options=BIO)
    assert bios(home) == [(11, "Reads a lot", 1), (12, None, 1)] and stats["people"] == (2, 2)
    tg.bios[11] = "Writes a lot"
    run(tg, options=BIO)
    assert bios(home)[0] == (11, "Reads a lot", 1)  # not asked again
    run(tg, options=BIO, again=True)
    assert bios(home)[0] == (11, "Writes a lot", 1)  # --refetch asks again


def test_bio_of_someone_no_account_has_met_is_left_empty(home):
    tg = Telegram([said(1, ALICE)])
    run(tg, options=BIO)
    assert bios(home) == [(11, None, 1)]


# --- the chat itself


def club(**kw):
    return Channel(7, "Club", kw.pop("photo", ChatPhotoEmpty()), DATE, megagroup=True, access_hash=1, **kw)


def test_chat_details_are_saved_and_shown(home):
    rights = ChatBannedRights(until_date=None, send_polls=True, pin_messages=True)
    tg = Telegram([day(1, 0)], entity=club(username="club", default_banned_rights=rights, noforwards=True))
    tg.full = {"about": "Be kind. https://example.com/rules", "participants_count": 1234, "slowmode_seconds": 30}
    run(tg, options=HTML)
    db = sqlite3.connect(home / "7.sqlite")
    info = get_chat(db)
    db.close()
    updated = info.pop("updated")
    assert info == {
        "id": 7,
        "kind": "group",
        "title": "Club",
        "username": "club",
        "link": "https://t.me/club",
        "members_cannot": ["pin_messages", "send_polls"],
        "joined": iso(DATE),
        "protected": 1,
        "members": 1234,
        "about": "Be kind. https://example.com/rules",
        "slow_mode": 30,
    }
    saved = json.loads((home / OUT / "chat.json").read_text(encoding="utf-8"))
    assert saved == {**info, "updated": updated}
    assert '"about":"Be kind. https://example.com/rules"' in page(home) and '"members":1234' in page(home)


def test_details_that_are_gone_do_not_linger(home):
    tg = Telegram([day(1, 0)])
    tg.full = {"about": "Old rules", "slowmode_seconds": 30}
    run(tg)
    tg.full = {}
    run(tg)
    assert query(home, "SELECT key FROM chat WHERE key IN ('about', 'slow_mode')") == []


def test_first_message_tells_when_the_chat_was_created(home):
    created = msg(1, action=types.MessageActionChannelCreate("Club"))
    run(Telegram([created, day(2, 3)]))
    assert query(home, "SELECT value FROM chat WHERE key = 'created'") == [(iso(DATE),)]


def test_chat_picture_is_downloaded_when_it_changes(home):
    asked = []

    async def download(entity, file=None, download_big=True):
        asked.append(entity.photo.photo_id)
        return b"\xff\xd8 a small jpeg"

    tg = Telegram([day(1, 0)], entity=club(photo=ChatPhoto(photo_id=5, dc_id=2)))
    tg.download_profile_photo = download
    run(tg, options=HTML)
    run(tg, options=HTML)
    assert asked == [5]  # the second run finds the same picture saved
    assert query(home, "SELECT value FROM chat WHERE key = 'photo'") == [(b"\xff\xd8 a small jpeg",)]
    assert "data:image/jpeg;base64,/9ggYSBzbWFsbCBqcGVn" in page(home)
    assert "photo" not in json.loads((home / OUT / "chat.json").read_text(encoding="utf-8")).keys() - {"photo_id"}
    tg.entity = club(photo=ChatPhoto(photo_id=6, dc_id=2))
    run(tg, options=HTML)
    assert asked == [5, 6]
    tg.entity = club()
    run(tg, options=HTML)
    assert query(home, "SELECT key FROM chat WHERE key LIKE 'photo%'") == []  # the chat has no picture any more


# --- forum topics


def topic(id, title, **kw):
    return types.ForumTopic(
        id=id,
        date=DATE,
        peer=types.PeerChannel(7),
        title=title,
        icon_color=0x6FB9F0,
        top_message=id,
        read_inbox_max_id=0,
        read_outbox_max_id=0,
        unread_count=0,
        unread_mentions_count=0,
        unread_reactions_count=0,
        unread_poll_votes_count=0,
        from_id=PeerUser(11),
        notify_settings=types.PeerNotifySettings(),
        **kw,
    )


def test_topics_keep_their_icon_colour_and_state(home):
    plans = msg(10, action=types.MessageActionTopicCreate(title="Plans", icon_color=0x6FB9F0))
    tg = Telegram([said(1, ALICE), plans], entity=club(forum=True))
    tg.topics = [
        topic(10, "Plans", icon_emoji_id=555, pinned=True, closed=True),
        topic(1, "General"),
        topic(30, "Talks"),
    ]
    tg.emoji = {555: "🚀"}
    run(tg, options=HTML)
    cols = "id, title, date, creator_id, icon, icon_emoji_id, color, closed, pinned, hidden"
    assert query(home, f"SELECT {cols} FROM topics ORDER BY id") == [
        (1, "General", iso(DATE), 11, None, None, 0x6FB9F0, None, None, None),
        (10, "Plans", iso(DATE), 11, "🚀", 555, 0x6FB9F0, 1, 1, None),
        (30, "Talks", iso(DATE), 11, None, None, 0x6FB9F0, None, None, None),
    ]
    meta = json.loads(re.search(r"const META = (.*);</script>", page(home))[1])
    by_id = {t[1]: t[6] for t in meta["topics"]}
    assert by_id[10] == {"i": "🚀", "c": 0x6FB9F0, "x": 1, "p": 1, "at": int(DATE.timestamp()), "by": "Alice"}
    saved = json.loads((home / OUT / "chat.json").read_text(encoding="utf-8"))["topics"]
    assert [t["id"] for t in saved] == [1, 10, 30] and saved[1]["icon"] == "🚀" and "closed" not in saved[0]
    # a topic deleted in Telegram keeps what was saved of it; a changed icon is looked up again
    tg.topics = [topic(10, "Plans", icon_emoji_id=777), topic(1, "General"), types.ForumTopicDeleted(id=30)]
    tg.emoji = {777: "🌙"}
    run(tg, options=HTML)
    assert query(home, "SELECT id, title, icon, closed FROM topics WHERE id IN (10, 30) ORDER BY id") == [
        (10, "Plans", "🌙", None),
        (30, "Talks", None, None),
    ]


# --- pins


def pinned(home):
    return [i for (i,) in query(home, f"SELECT id FROM messages WHERE extra LIKE '{PIN_MARK}' ORDER BY id")]


def test_pin_marks_follow_the_list_of_pinned_messages(home):
    tg = Telegram([day(1, 0, pinned=True), day(2, 1, pinned=True), day(3, 2)])
    run(tg, options=HTML)
    assert pinned(home) == [1, 2]
    # unpinned and pinned long after they were saved, with no trace in the change log
    tg.messages[2] = day(2, 1)
    tg.messages[3] = day(3, 2, pinned=True)
    run(tg, options=HTML)
    assert pinned(home) == [1, 3]
    data = (home / OUT / ".reader" / "all.0.js").read_text(encoding="utf-8")
    assert data.count('"pinned":true') == 2


def test_pins_older_than_the_account_sees_are_left_alone():
    db = sqlite3.connect(":memory:")
    db.executescript(SCHEMA)
    db.execute("INSERT INTO tasks(key, lo, hi, cursor) VALUES('k', 0, 100, 101)")
    save(db, "k", rows(msg(5, pinned=True), msg(50, pinned=True), msg(60), msg(70, pinned=True)))
    sync_pins(db, [60, 70], first=10)  # Telegram lists the pins from message 10 on
    marked = [i for (i,) in db.execute("SELECT id FROM messages WHERE extra LIKE ? ORDER BY id", (PIN_MARK,))]
    assert marked == [5, 60, 70]
    sync_pins(db, [70], first=10)  # the list kept from last time tells what to take off
    marked = [i for (i,) in db.execute("SELECT id FROM messages WHERE extra LIKE ? ORDER BY id", (PIN_MARK,))]
    assert marked == [5, 70]
    db.close()


# --- the page


def test_page_script_leaves_the_browser_objects_alone():
    # a function of the page's script called like one of these would hide it from the rest of the script
    taken = re.findall(r"function (history|location|document|window|navigator|name|status|event)\(", reader._JS)
    assert taken == []
