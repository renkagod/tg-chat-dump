"""--search: looking for words in every saved dump."""

import os

import pytest
from telethon import types
from telethon.tl.types import Channel, ChatPhotoEmpty, PeerUser, User
from test_changes import DATE, Telegram, day, msg, run
from test_people import club, topic

from tgdump import fetch, search
from tgdump.scope import Scope, parse_date
from tgdump.search import report, snippet, words

ANN = User(id=11, first_name="Ann", username="ann")
BOB = User(id=12, first_name="Bob Stone")


def by(user, id, days, text, **kw):
    m = day(id, days, text, from_id=PeerUser(user.id), **kw)
    m._sender = user
    return m


@pytest.fixture
def dumps(home, monkeypatch):
    """Two saved chats: a forum and a channel, the channel dumped last."""
    monkeypatch.setattr(search, "DATA", home)
    plans = msg(10, action=types.MessageActionTopicCreate(title="Plans", icon_color=1))
    head = types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=10)
    tg = Telegram(
        [
            plans,
            by(ANN, 11, 0, "Пицца в семь, у вокзала", reply_to=head),
            by(BOB, 12, 1, "pizza is fine, but who brings the PIZZA cutter?", reply_to=head),
            by(ANN, 13, 2, "no pizza for me"),
            by(BOB, 14, 3, "the cutter is here: https://example.com/cutter"),
        ],
        entity=club(forum=True, username="club"),
    )
    tg.topics = [topic(10, "Plans"), topic(1, "General")]
    run(tg, options=frozenset({"html"}))
    news = Channel(8, "Pizza news", ChatPhotoEmpty(), DATE, broadcast=True, access_hash=1)
    asyncio_run(
        fetch.dump_chat(
            [Telegram([day(1, 0, "weather"), day(2, 0, "a new pizza place opened"), day(3, 0, "rain")], entity=news)], 8
        )
    )
    os.utime(home / "7.sqlite", (1, 1))
    return home


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)


def found(*args, **kw):
    lines = []
    total = report(*args, **kw, write=lines.append)
    return total, lines


def test_words_are_found_in_every_dump_in_any_case(dumps):
    total, lines = found("PIZZA")
    assert total == 3
    text = "\n".join(lines)
    assert text.index("Pizza news: 1 message") < text.index("Club: 2 messages")  # the chat dumped last comes first
    assert (dumps / "out/7_Club/index.html").as_uri() in text
    assert "Ann: no pizza for me  #topic/1/msg/13" in text and "Plans · Bob Stone: pizza is fine" in text
    assert "a new pizza place opened  #msg/2" in text and lines[-1].strip() == "3 messages in 2 chats"
    assert found("пицца")[0] == 1 and found("ПИЦЦА вокзала")[0] == 1  # Cyrillic too, and every word has to be there


def test_a_phrase_in_quotes_is_looked_for_whole(dumps):
    assert words('fine "Pizza Cutter" ') == ["fine", "pizza cutter"]
    assert found('"pizza cutter"')[0] == 1 and found('"cutter pizza"')[0] == 0
    assert found("cutter pizza")[0] == 1


def test_search_is_narrowed_by_chat_author_date_and_type(dumps):
    assert found("pizza", chat="@club")[0] == 2 and found("pizza", chat="https://t.me/club")[0] == 2
    assert (
        found("pizza", chat="news")[0] == 1
        and found("pizza", chat="-1008")[0] == 1
        and found("pizza", chat="8")[0] == 1
    )
    assert found("pizza", chat="nowhere") == (0, ["Nothing found."])
    assert found("pizza", Scope(from_user="@ann"))[0] == 1 and found("pizza", Scope(from_user="stone"))[0] == 1
    assert found("pizza", Scope(from_user="12"))[0] == 1 and found("pizza", Scope(from_user="@stone"))[0] == 0
    day_two = (DATE.date().replace(day=DATE.day + 2)).isoformat()
    assert found("pizza", Scope(since=parse_date(day_two)))[0] == 1
    assert found("cutter", Scope(kind="links"))[0] == 1 and found("pizza", Scope(kind="links"))[0] == 0


def test_only_the_newest_matches_are_shown(dumps):
    total, lines = found("pizza", chat="club", limit=1)
    assert total == 2 and sum("#topic/" in line for line in lines) == 1
    assert "no pizza for me" in lines[1] and "1 older; --limit 2 shows them all" in lines[2]


def test_a_deleted_message_is_found_and_marked(dumps):
    tg = Telegram(
        [day(1, 0, "weather"), day(3, 0, "rain")],
        entity=Channel(8, "Pizza news", ChatPhotoEmpty(), DATE, broadcast=True, access_hash=1),
    )
    asyncio_run(fetch.dump_chat([tg], 8, again=True))
    assert "  (deleted) a new pizza place opened" in "\n".join(found("pizza", chat="news")[1])


def test_snippet_shows_the_text_around_the_match():
    text = "start " + "x" * 200 + " the\nneedle  is here " + "y" * 200 + " end"
    cut = snippet(text, ["needle"], around=20)
    assert cut.startswith("…") and cut.endswith("…") and "the needle is here" in cut and len(cut) <= 42
    assert snippet("short text", ["text"]) == "short text" and snippet("no match here", ["zzz"]) == "no match here"


def test_the_interactive_prompt_searches_after_a_question_mark(dumps, monkeypatch, capsys):
    from tgdump import interactive

    answers = iter(["?cutter pizza", "?", "q"])

    async def ask(prompt=""):
        return next(answers)

    monkeypatch.setattr(interactive, "ask", ask)
    assert asyncio_run(interactive.pick_chat([(None, None)], {}, None)) == interactive.QUIT
    shown = capsys.readouterr().out
    assert "Club: 1 message" in shown and "who brings the PIZZA cutter?" in shown and "?pizza" in shown


def test_nothing_to_search_for_is_refused(dumps):
    with pytest.raises(ValueError, match="nothing to search"):
        found("  ")
