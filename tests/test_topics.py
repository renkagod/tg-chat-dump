"""--topics: single forum topics, General among them, and filters inside them."""

import datetime
import sqlite3

import pytest
from telethon import types
from telethon.tl.types import User
from test_changes import DATE, Telegram, day, msg, run
from test_files import FILES, on_disk, photo, records
from test_people import club, topic

from tgdump.scope import Scope, parse_date

OUT = "out/7_Club"


def inside(id, top, days=0, text=None, **kw):
    """A message in the forum topic `top`."""
    head = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=top)
    return day(id, days, text, reply_to=head, **kw)


def forum(*messages):
    plans = msg(10, action=types.MessageActionTopicCreate(title="Plans", icon_color=1))
    talks = msg(20, action=types.MessageActionTopicCreate(title="Talks", icon_color=1))
    tg = Telegram([plans, talks, *messages], entity=club(forum=True))
    tg.topics = [topic(10, "Plans"), topic(20, "Talks"), topic(1, "General")]
    return tg


def saved(home, sql="SELECT id FROM messages ORDER BY id", name="7"):
    db = sqlite3.connect(home / f"{name}.sqlite")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def folders(out):
    return sorted(p.name for p in out.iterdir() if p.is_dir() and p.name[0].isdigit())


# --- General


def test_general_is_dumped_like_any_other_topic(home):
    tg = forum(
        inside(11, 10),
        day(12, 0, "in general"),
        day(13, 0, "an answer", reply_to=types.MessageReplyHeader(reply_to_msg_id=12)),
    )
    run(tg, topics=[1])
    assert saved(home) == [(12,), (13,)] and folders(home / OUT) == ["1_General"]
    assert [m["topic_id"] for m in records(home / OUT / "1_General").values()] == [1, 1]
    tg.messages[14] = day(14, 1, "later, in general")
    tg.messages[21] = inside(21, 20)
    run(tg, topics=[1, 20])
    assert saved(home) == [(12,), (13,), (14,), (21,)] and folders(home / OUT) == ["1_General", "20_Talks"]


def test_what_general_does_not_list_is_not_taken_for_deleted(home):
    joined = msg(13, action=types.MessageActionChatAddUser(users=[5]))
    tg = forum(day(11, 0), day(12, 0), joined)
    run(tg)
    tg.unlisted = {13}  # Telegram shows it in General, and leaves it out of the topic's own list
    del tg.messages[12]
    assert run(tg, topics=[1], again=True)["deleted"] == 1
    assert saved(home, "SELECT id FROM messages WHERE deleted_at IS NOT NULL") == [(12,)]


def test_topics_of_a_chat_that_is_not_a_forum_are_refused(home):
    with pytest.raises(ValueError, match="not a forum"):
        run(Telegram([day(1, 0)]), topics=[1])


# --- filters inside topics


def test_filters_inside_topics_get_a_folder_of_their_own(home):
    tg = forum(
        inside(11, 10, 0, "old"),
        inside(12, 10, 5, "new, with a photo", media=photo(501)),
        inside(13, 10, 6, "new"),
        inside(21, 20, 6, "new, in another topic", media=photo(502)),
        day(30, 6, "new, in general"),
    )
    tg.content = {501: b"jpeg", 502: b"jpeg"}
    since = Scope(since=(DATE + datetime.timedelta(days=4)).date())
    run(tg, topics=[10, 1], scope=since)
    out = home / f"out/7_Club_topics-1-10_since-{since.since}"
    assert folders(out) == ["10_Plans", "1_General"]
    assert list(records(out / "10_Plans")) == [12, 13] and list(records(out / "1_General")) == [30]
    # the topics themselves are saved whole, as a part of the dump of the chat; the other topic is not touched
    assert saved(home) == [(11,), (12,), (13,), (30,)] and folders(home / OUT) == ["10_Plans", "1_General"]
    run(tg, topics=[10], scope=Scope(kind="photos"), options=FILES)
    out = home / "out/7_Club_topics-10_photos"
    assert list(records(out / "10_Plans")) == [12] and on_disk(out / "10_Plans") == ["12.jpg"]
    assert folders(out) == ["10_Plans"] and tg.downloads == [(501, 4)]


def test_an_author_filter_inside_topics_needs_the_author_found(home):
    ann = User(id=11, first_name="Ann", username="ann")
    tg = forum(inside(11, 10, from_id=types.PeerUser(11)), inside(12, 10, from_id=types.PeerUser(12)))
    tg.messages[11]._sender = ann

    async def get_peer_id(peer):
        if peer != "@ann":
            raise ValueError("no such user")
        return 11

    tg.get_peer_id = get_peer_id
    run(tg, topics=[10], scope=Scope(from_user="@ann"))
    assert list(records(home / "out/7_Club_topics-10_from-ann" / "10_Plans")) == [11]
    with pytest.raises(ValueError, match="Cannot find the user @nobody"):
        run(tg, topics=[10], scope=Scope(from_user="@nobody"))


def test_scope_inside_topics_names_them():
    march = Scope(parse_date("2026-03"), parse_date("2026-03", end=True))
    assert march.within([37, 12, 37]).tag() == "topics-12-37_since-2026-03-01_until-2026-03-31"
    assert march.within([12]).db_suffix() != march.db_suffix() != march.within([12, 37]).db_suffix()
    assert march.within(None) == march and march.within([]) == march
    assert Scope().within([12]) == Scope() and not Scope().within([12])  # topics alone are a part of the whole dump
    many = Scope(kind="links").within(range(1, 40))
    assert many.tag().startswith("topics-1-2-3-4-5-and-34-more-") and many.tag().endswith("_links")
    assert many.tag() != Scope(kind="links").within(range(1, 41)).tag().replace("35-more", "34-more")
    where, params = march.within([12, 37]).sql(sender_id=5)
    assert where.endswith("sender_id = ? AND topic_id IN (?,?)") and params[-3:] == [5, 12, 37]
