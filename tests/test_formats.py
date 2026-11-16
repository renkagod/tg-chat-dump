"""Other formats: messages.csv, and Telegram Desktop's result.json written from a dump and read into one."""

import csv
import datetime
import functools
import json
import sqlite3

import pytest
from telethon import types
from telethon.tl.types import PeerUser, User
from test_changes import DATE, Telegram, day, msg, run
from test_files import club_with_files, doc, named, on_disk, photo, records
from test_people import club, topic

from tgdump import fetch, imports
from tgdump.imports import import_result
from tgdump.tdesktop import NOT_INCLUDED, peer_number, read_text

OUT = "out/7_Club"
ANN = User(id=11, first_name="Ann", username="ann")


def by(m, user=ANN):
    """Give a message its sender, as Telegram hands it over."""
    m.from_id, m._sender_id, m._sender = PeerUser(user.id), user.id, user
    return m


def table(folder):
    with open(folder / "messages.csv", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def result(folder):
    return json.loads((folder / "result.json").read_text(encoding="utf-8"))


@pytest.fixture
def read(home, monkeypatch):
    """Imports write to the same place as the runs of the `home` fixture."""
    monkeypatch.setattr(imports, "DATA", home)
    return functools.partial(import_result, reader=True)


# --- csv


def test_csv_has_a_row_per_message_and_grows_with_the_chat(home):
    tg = Telegram(
        [by(day(1, 0, 'said "hi", twice\nand left')), day(2, 0, "the notes", media=doc(5, named("notes.pdf")))]
    )
    run(tg, options=frozenset({"csv", "meta"}))
    rows = table(home / OUT)
    assert [r["id"] for r in rows] == ["1", "2"] and rows[0]["text"] == 'said "hi", twice\nand left'
    assert rows[0]["sender"] == "Ann" and rows[0]["sender_id"] == "11" and rows[1]["file"] == "notes.pdf"
    assert rows[0]["date"] == "2026-05-13T06:38:53+00:00" and rows[0]["media"] == ""
    tg.messages[3] = day(3, 1, "later")
    run(tg, options=frozenset({"csv"}))
    assert [r["id"] for r in table(home / OUT)] == ["1", "2", "3"]
    raw = (home / OUT / "messages.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbfid,date,") and raw.count(b"\xef\xbb\xbf") == 1


def test_csv_turned_on_later_covers_the_saved_messages(home):
    tg = Telegram([day(1, 0), day(2, 0)])
    run(tg)
    assert not (home / OUT / "messages.csv").exists()
    tg.messages[3] = day(3, 1)
    run(tg, options=frozenset({"csv"}))
    assert [r["id"] for r in table(home / OUT)] == ["1", "2", "3"]


# --- Telegram Desktop's format, written


def test_result_json_is_written_in_telegram_desktops_format(home):
    tg = club_with_files()
    tg.messages[1] = by(tg.messages[1])
    tg.messages[5] = by(day(5, 2, "edited since", edit_date=DATE))
    tg.messages[6] = msg(6, action=types.MessageActionChatEditTitle(title="Club"))
    tg.messages[7] = day(7, 2, "an answer", reply_to=types.MessageReplyHeader(reply_to_msg_id=5))
    run(tg, options=frozenset({"tdesktop", "files", "meta"}))
    got = result(home / OUT)
    assert (got["name"], got["type"], got["id"]) == ("Club", "private_supergroup", 7)
    by_id = {m["id"]: m for m in got["messages"]}
    assert list(by_id) == [1, 2, 3, 4, 5, 6, 7]
    assert by_id[1]["from"] == "Ann" and by_id[1]["from_id"] == "user11" and by_id[1]["type"] == "message"
    assert by_id[1]["photo"] == "files/1.jpg" and (by_id[1]["width"], by_id[1]["height"]) == (800, 600)
    assert by_id[1]["date_unixtime"] == str(int(DATE.timestamp())) and len(by_id[1]["date"]) == 19
    assert by_id[2]["file"] == "files/2_report.pdf" and by_id[2]["file_name"] == "report.pdf"
    assert by_id[2]["text"] == "the report" and by_id[2]["text_entities"] == [{"type": "plain", "text": "the report"}]
    assert by_id[3]["media_type"] == "voice_message" and by_id[3]["duration_seconds"] == 4
    assert (
        by_id[4]["file"] == NOT_INCLUDED and by_id[4]["media_type"] == "sticker" and by_id[4]["sticker_emoji"] == "🙂"
    )
    assert "edited" in by_id[5] and by_id[5]["edited_unixtime"] == str(int(DATE.timestamp()))
    assert by_id[6] == {**by_id[6], "type": "service", "action": "edit_group_title", "title": "Club"}
    assert by_id[7]["reply_to_message_id"] == 5


def test_formatting_is_written_as_entities(home):
    bold = types.MessageEntityBold(offset=4, length=4)
    link = types.MessageEntityTextUrl(offset=13, length=4, url="https://example.com/that")
    marks = day(2, 0, "2 ** 3 ** 4 is a lot")  # typed by hand, not formatting
    tg = Telegram([day(1, 0, "see this and that", entities=[bold, link]), marks])
    run(tg, options=frozenset({"tdesktop", "markdown"}))
    first, second = result(home / OUT)["messages"]
    that = {"type": "text_link", "text": "that", "href": "https://example.com/that"}
    assert first["text"] == ["see ", {"type": "bold", "text": "this"}, " and ", that]
    assert [e["type"] for e in first["text_entities"]] == ["plain", "bold", "plain", "text_link"]
    assert second["text"] == "2 ** 3 ** 4 is a lot" and [e["type"] for e in second["text_entities"]] == ["plain"]


# --- Telegram Desktop's format, read


def export_file(folder, chat):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "result.json").write_text(json.dumps(chat, ensure_ascii=False), encoding="utf-8")
    return folder / "result.json"


def said(id, text="hi", **more):
    when = int(DATE.timestamp()) + id
    return {
        "id": id,
        "type": "message",
        "date": "2026-05-13T09:38:53",
        "date_unixtime": str(when),
        "from": "Ann",
        "from_id": "user11",
        "text": text,
        **more,
    }


def rows(home, sql, name="5"):
    db = sqlite3.connect(home / f"{name}.sqlite")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_an_export_is_read_into_a_dump(home, read, tmp_path):
    source = tmp_path / "ChatExport"
    (source / "photos").mkdir(parents=True)
    (source / "photos" / "photo_1.jpg").write_bytes(b"jpeg")
    entities = [
        {"type": "plain", "text": "see "},
        {"type": "bold", "text": "this"},
        {"type": "plain", "text": " and "},
        {"type": "text_link", "text": "that", "href": "https://example.com/that"},
    ]
    poll = {
        "question": "Lunch?",
        "closed": False,
        "total_voters": 3,
        "answers": [{"text": "Pizza", "voters": 2, "chosen": True}, {"text": "Soup", "voters": 1, "chosen": False}],
    }
    chat = {
        "name": "Old friends",
        "type": "private_group",
        "id": 5,
        "messages": [
            {
                "id": 1,
                "type": "service",
                "date": "2026-05-13T09:38:53",
                "date_unixtime": "1778654333",
                "actor": "Ann",
                "actor_id": "user11",
                "action": "create_group",
                "title": "Old friends",
                "members": ["Ann", "Bob"],
                "text": "",
            },
            said(
                2,
                "see this and that",
                text_entities=entities,
                edited="2026-05-13T09:40:00",
                edited_unixtime="1778654400",
            ),
            said(3, "", photo="photos/photo_1.jpg", width=640, height=480, reply_to_message_id=2),
            said(
                4,
                "",
                file="(File not included. Change data exporting settings to download.)",
                media_type="voice_message",
                mime_type="audio/ogg",
                duration_seconds=7,
            ),
            said(5, "", poll=poll, forwarded_from="Carol", forwarded_from_id="user33"),
            said(
                6,
                "from a channel",
                from_id="channel77",
                reactions=[{"type": "emoji", "count": 2, "emoji": "👍"}, {"type": "paid", "count": 1}],
            ),
            said(7, "", location_information={"latitude": 50.4, "longitude": 30.5}),
        ],
    }
    [(out, name, total, added)] = read(export_file(source, chat))
    assert (name, total, added) == ("Old friends", 7, 7) and out == home / "out" / "5_Old_friends"
    got = records(out)
    assert got[1]["action"] == "MessageActionChatCreate" and got[1]["extra"]["action"]["names"] == ["Ann", "Bob"]
    assert (
        got[2]["text"] == "see this and that"
        and got[2]["text_md"] == "see **this** and [that](https://example.com/that)"
    )
    assert got[2]["extra"]["links"] == [{"text": "that", "url": "https://example.com/that"}]
    assert got[2]["date"] == "2026-05-13T06:38:55+00:00" and got[2]["edit_date"] == "2026-05-13T06:40:00+00:00"
    assert (got[2]["sender"], got[2]["sender_id"]) == ("Ann", 11)
    assert got[3]["media"] == "MessageMediaPhoto" and got[3]["reply_to"] == 2
    assert got[3]["extra"]["file"] == {"path": "files/3_photo_1.jpg", "size": 4, "w": 640, "h": 480}
    assert (out / "files" / "3_photo_1.jpg").read_bytes() == b"jpeg"
    assert got[4]["extra"]["file"] == {"mime": "audio/ogg", "kind": "voice", "duration": 7}
    assert got[5]["media"] == "MessageMediaPoll" and got[5]["extra"]["poll"]["answers"][0] == {
        "text": "Pizza",
        "voters": 2,
    }
    assert got[5]["fwd_from"] == "Carol" and got[5]["extra"]["fwd"] == {"from_id": 33}
    assert got[6]["sender_id"] == -1000000000077 and got[6]["reactions"] == {"👍": 2, "paid": 1}
    assert got[7]["media"] == "MessageMediaGeo" and got[7]["extra"]["geo"] == {"lat": 50.4, "long": 30.5}
    assert (out / "index.html").exists() and json.loads((out / "chat.json").read_text(encoding="utf-8"))[
        "kind"
    ] == "group"
    assert rows(home, "SELECT id, name FROM users ORDER BY id") == [(-1000000000077, "Ann"), (11, "Ann")]
    # it is not a chat the tool downloads until it is asked to, so updating everything leaves it alone
    assert fetch.saved_dumps() == []


CREATED = {
    "type": "service",
    "date_unixtime": "1778654333",
    "actor": "Ann",
    "actor_id": "user11",
    "action": "topic_created",
}


def test_topics_of_a_forum_are_found_by_the_replies(home, read, tmp_path):
    chat = {
        "name": "Forum",
        "type": "private_supergroup",
        "id": 5,
        "messages": [
            {"id": 10, "title": "Plans", **CREATED},
            {"id": 20, "title": "Talks", **CREATED},
            said(11, "first in plans", reply_to_message_id=10),
            said(12, "an answer to it", reply_to_message_id=11),
            {**CREATED, "id": 13, "action": "topic_edit", "new_title": "Big plans", "reply_to_message_id": 10},
            said(21, "in talks", reply_to_message_id=20),
            {**CREATED, "id": 22, "action": "pin_message", "message_id": 21},
            said(30, "in general"),
            said(31, "an answer to a message that is gone", reply_to_message_id=5),
        ],
    }
    [(out, *_)] = read(export_file(tmp_path / "x", chat))
    found = dict(rows(home, "SELECT id, topic_id FROM messages"))
    assert found == {10: 10, 11: 10, 12: 10, 13: 10, 20: 20, 21: 20, 22: 20, 30: 1, 31: 1}
    assert sorted(p.name for p in out.iterdir() if p.is_dir() and p.name[0].isdigit()) == [
        "10_Big_plans",
        "1_General",
        "20_Talks",
    ]
    assert json.loads((out / "chat.json").read_text(encoding="utf-8"))["kind"] == "forum"


def test_an_import_adds_only_what_is_not_saved(home, read, tmp_path):
    tg = Telegram([by(day(2, 0, "as Telegram has it now")), day(3, 0)], entity=club())
    run(tg)
    chat = {
        "name": "Club",
        "type": "private_supergroup",
        "id": 7,
        "messages": [said(1, "deleted long ago"), said(2, "as it was then")],
    }
    [(out, _, total, added)] = read(export_file(tmp_path / "x", chat))
    assert (total, added) == (2, 1) and out == home / OUT
    got = records(out)
    assert (
        list(got) == [1, 2, 3] and got[1]["text"] == "deleted long ago" and got[2]["text"] == "as Telegram has it now"
    )
    assert [i for i, *_ in fetch.saved_dumps()] == [7]  # still a chat the tool keeps up to date


def test_a_part_of_a_forum_is_placed_by_the_messages_most_replied_to(home, read, tmp_path):
    chat = {
        "name": "Forum",
        "type": "private_supergroup",
        "id": 5,
        "messages": [
            *[said(100 + i, reply_to_message_id=40) for i in range(6)],  # the topic was made before the export begins
            said(110, "an answer in that topic", reply_to_message_id=103),
            said(120, "an answer to something older", reply_to_message_id=77),
            {"id": 500, "title": "New", **CREATED},
            said(501, reply_to_message_id=500),
        ],
    }
    [(out, *_)] = read(export_file(tmp_path / "x", chat))
    found = dict(rows(home, "SELECT id, topic_id FROM messages"))
    assert found == {**dict.fromkeys(range(100, 106), 40), 110: 40, 120: 1, 500: 500, 501: 500}
    assert sorted(p.name for p in out.iterdir() if p.is_dir() and p.name[0].isdigit()) == ["1_General", "40", "500_New"]


def test_an_export_merged_into_a_forum_follows_the_saved_messages(home, read, tmp_path):
    head = msg(10, action=types.MessageActionTopicCreate(title="Plans", icon_color=1))
    kept = msg(11, "still there", reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=10))
    tg = Telegram([head, kept], entity=club(forum=True))
    tg.topics = [topic(10, "Plans"), topic(1, "General")]
    run(tg)
    source = tmp_path / "x"
    (source / "files").mkdir(parents=True)
    (source / "files" / "plan.pdf").write_bytes(b"pdf")
    chat = {
        "name": "Club, as it was called then",
        "type": "private_supergroup",
        "id": 7,
        "messages": [
            said(9, "deleted, was in general"),
            said(12, "deleted, answered a saved one", reply_to_message_id=11, file="files/plan.pdf"),
            said(13, "deleted, was in the topic", reply_to_message_id=10),
        ],
    }
    [(out, _, _, added)] = read(export_file(source, chat))
    assert added == 3 and out == home / OUT  # the folder the chat has, whatever the export calls it
    assert dict(rows(home, "SELECT id, topic_id FROM messages", "7")) == {9: 1, 10: 10, 11: 10, 12: 10, 13: 10}
    assert on_disk(out / "10_Plans") == ["12_plan.pdf"]
    assert records(out / "10_Plans")[12]["extra"]["file"]["path"] == "files/12_plan.pdf"


def test_an_imported_private_chat_is_never_dropped_for_another_copy(home, read, tmp_path):
    friend = User(id=7, first_name="Friend", access_hash=1)
    chat = {"name": "Friend", "type": "personal_chat", "id": 7, "messages": [said(i, f"m{i}") for i in range(1, 11)]}
    [(out, *_)] = read(export_file(tmp_path / "x", chat))
    seconds = datetime.timedelta(seconds=1)
    # the six newest were deleted since the export was made: five looked up would all be missing
    run(Telegram([msg(i, f"m{i}", date=DATE + i * seconds) for i in range(1, 5)], entity=friend))
    assert sorted(records(out)) == list(range(1, 11))
    # the other side's account numbers the same chat its own way
    other = Telegram([msg(i, "theirs", date=DATE + (i + 50) * seconds) for i in range(1, 5)], entity=friend)
    with pytest.raises(ValueError, match="read from an export"):
        run(other)
    assert rows(home, "SELECT COUNT(*) FROM messages", "7") == [(10,)]


def test_an_export_of_the_whole_account_is_read_chat_by_chat(home, read, tmp_path):
    account = {
        "about": "Here is the data you requested.",
        "chats": {
            "list": [
                {"name": "Ann", "type": "personal_chat", "id": 11, "messages": [said(1)]},
                {"name": "Empty", "type": "private_group", "id": 12, "messages": []},
            ]
        },
        "left_chats": {"list": [{"name": "News", "type": "public_channel", "id": 13, "messages": [said(5), said(6)]}]},
    }
    done = read(export_file(tmp_path / "x", account).parent)  # the folder is enough
    assert [(name, total) for _, name, total, _ in done] == [("Ann", 1), ("News", 2)]
    assert json.loads((home / "out" / "13_News" / "chat.json").read_text(encoding="utf-8"))["kind"] == "channel"
    assert json.loads((home / "out" / "11_Ann" / "chat.json").read_text(encoding="utf-8"))["kind"] == "user"


def test_text_of_older_exports_is_a_list_of_strings_and_entities():
    old = {"text": ["run ", {"type": "code", "text": "uv sync"}, " first"]}
    assert read_text(old) == ("run uv sync first", "run `uv sync` first", [])
    assert read_text({"text": "plain"}) == ("plain", None, [])
    assert read_text({}) == ("", None, [])
    assert [peer_number(tag) for tag in (11, "user11", "chat5", "channel77", None)] == [
        11,
        11,
        -5,
        -(10**12) - 77,
        None,
    ]


def test_what_is_written_can_be_read_back(home, read, tmp_path, monkeypatch):
    plans = by(msg(10, action=types.MessageActionTopicCreate(title="Plans", icon_color=1)))
    inside = by(
        msg(
            11,
            "in the topic",
            reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=10),
            media=photo(501),
        )
    )
    tg = Telegram([plans, inside, by(msg(12, "in general"))], entity=club(forum=True))
    tg.topics, tg.content = [topic(10, "Plans"), topic(1, "General")], {501: b"jpeg"}
    run(tg, options=frozenset({"tdesktop", "files"}))
    first = home / OUT
    before = {t: records(first / t) for t in ("10_Plans", "1_General")}
    monkeypatch.setattr(imports, "DATA", tmp_path / "again")
    monkeypatch.setenv("TG_OUT", str(tmp_path / "again" / "out"))
    [(out, _, total, added)] = import_result(first, reader=True)
    assert (total, added) == (3, 3) and on_disk(out / "10_Plans") == ["11_11.jpg"]
    for name, saved in before.items():
        for mid, m in records(out / name).items():
            for key in ("date", "topic_id", "sender_id", "sender", "text", "reply_to", "media", "action"):
                assert m[key] == saved[mid][key], (mid, key)


def test_an_import_writes_to_the_current_output_folder(tmp_path):
    from tgdump.imports import dump_folder
    from tgdump.store import open_db

    old, new = tmp_path / "old", tmp_path / "new"
    (old / "7_Club").mkdir(parents=True)
    db = open_db(tmp_path / "7.sqlite")
    db.execute("INSERT INTO exports VALUES(?, 0, 0, NULL)", (str(old / "7_Club"),))
    assert dump_folder(db, 7, "Club", old) == old / "7_Club"
    assert dump_folder(db, 7, "Club", new) == new / "7_Club"  # the folder written before is elsewhere
    db.close()
