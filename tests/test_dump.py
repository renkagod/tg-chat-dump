import datetime
import json
import sqlite3

import pytest
from telethon.tl.types import (
    Message,
    MessageActionTopicCreate,
    MessageEntityBold,
    MessageEntityTextUrl,
    MessageFwdHeader,
    MessageMediaPhoto,
    MessageMediaPoll,
    MessageMediaToDo,
    MessageReactions,
    MessageReplies,
    MessageReplyHeader,
    MessageService,
    PeerChannel,
    Poll,
    PollAnswer,
    PollAnswerVoters,
    PollResults,
    ReactionCount,
    ReactionCustomEmoji,
    ReactionEmoji,
    TextWithEntities,
    TodoCompletion,
    TodoItem,
    TodoList,
)

from tgdump import config
from tgdump.export import export
from tgdump.scope import Scope, parse_date
from tgdump.store import COLUMN_NAMES, SCHEMA, open_db, plan_full, plan_topics, save, to_row
from tgdump.util import slug

DATE = datetime.datetime(2026, 5, 13, 6, 38, 53, tzinfo=datetime.UTC)
PEER = PeerChannel(1)
ALL = frozenset(config.OPTIONS)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    yield conn
    conn.close()


def msg(id, text="", **kw):
    return Message(id=id, peer_id=PEER, date=DATE, message=text, **kw)


def topic_reply(to, top=None):
    return MessageReplyHeader(forum_topic=True, reply_to_msg_id=to, reply_to_top_id=top)


def field(row, name):
    return row[COLUMN_NAMES.index(name)]


def twe(text):
    return TextWithEntities(text=text, entities=[])


# --- topics and basic fields


def test_topic_creation_message_belongs_to_its_own_topic():
    m = MessageService(id=10, peer_id=PEER, date=DATE, action=MessageActionTopicCreate(title="t", icon_color=0))
    row = to_row(m, forum=True)
    assert field(row, "topic_id") == 10
    assert field(row, "action") == "MessageActionTopicCreate"


def test_topic_is_taken_from_reply_header():
    assert field(to_row(msg(11, reply_to=topic_reply(10)), forum=True), "topic_id") == 10
    assert field(to_row(msg(12, reply_to=topic_reply(11, top=10)), forum=True), "topic_id") == 10


def test_message_without_topic_goes_to_general():
    assert field(to_row(msg(13), forum=True), "topic_id") == 1


def test_non_forum_chat_has_no_topic():
    assert field(to_row(msg(14, reply_to=MessageReplyHeader(reply_to_msg_id=5)), forum=False), "topic_id") is None


def test_media_and_forward_are_recorded():
    m = msg(15, "pic", media=MessageMediaPhoto(), fwd_from=MessageFwdHeader(date=DATE, from_name="Someone"))
    row = to_row(m, forum=False)
    assert field(row, "fwd_from") == "Someone"
    assert field(row, "media") == "MessageMediaPhoto"


# --- optional extras


def test_extras_are_empty_unless_switched_on():
    m = msg(
        16,
        "hi",
        views=10,
        entities=[MessageEntityBold(0, 2)],
        reactions=MessageReactions(results=[ReactionCount(reaction=ReactionEmoji("👍"), count=3)]),
    )
    row = to_row(m, forum=False)
    assert all(field(row, c) is None for c in ("text_md", "views", "reactions", "extra"))


def test_meta_option_records_reactions_views_and_replies():
    reactions = MessageReactions(
        results=[
            ReactionCount(reaction=ReactionEmoji("👍"), count=3),
            ReactionCount(reaction=ReactionCustomEmoji(document_id=42), count=1),
        ]
    )
    m = msg(17, "post", views=1200, forwards=5, reactions=reactions, replies=MessageReplies(replies=7, replies_pts=0))
    row = to_row(m, forum=False, options={"meta"})
    assert (field(row, "views"), field(row, "forwards"), field(row, "replies")) == (1200, 5, 7)
    assert json.loads(field(row, "reactions")) == {"👍": 3, "custom:42": 1}


def test_markdown_option_keeps_formatting_and_hidden_links():
    entities = [MessageEntityBold(0, 4), MessageEntityTextUrl(5, 4, url="https://example.com")]
    row = to_row(msg(18, "Bold link", entities=entities), forum=False, options={"markdown"})
    assert field(row, "text") == "Bold link"
    assert field(row, "text_md") == "**Bold** [link](https://example.com)"


def test_polls_option_records_question_answers_and_votes():
    poll = Poll(
        id=1,
        question=twe("Best OS?"),
        answers=[PollAnswer(text=twe("Linux"), option=b"0"), PollAnswer(text=twe("Windows"), option=b"1")],
        hash=0,
    )
    results = PollResults(
        results=[PollAnswerVoters(option=b"0", voters=7), PollAnswerVoters(option=b"1", voters=2)], total_voters=9
    )
    row = to_row(msg(19, media=MessageMediaPoll(poll=poll, results=results)), forum=False, options={"polls"})
    extra = json.loads(field(row, "extra"))["poll"]
    assert extra["question"] == "Best OS?"
    assert extra["answers"] == [{"text": "Linux", "voters": 7}, {"text": "Windows", "voters": 2}]
    assert extra["total_voters"] == 9


def test_polls_option_records_checklists():
    todo = TodoList(title=twe("Release"), list=[TodoItem(id=1, title=twe("tests")), TodoItem(id=2, title=twe("tag"))])
    media = MessageMediaToDo(todo=todo, completions=[TodoCompletion(id=1, completed_by=PEER, date=DATE)])
    extra = json.loads(field(to_row(msg(20, media=media), forum=False, options={"polls"}), "extra"))
    assert extra["todo"] == {
        "title": "Release",
        "items": [{"text": "tests", "done": True}, {"text": "tag", "done": False}],
    }


def test_open_db_adds_new_columns_to_old_databases(tmp_path):
    path = tmp_path / "old.sqlite"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE messages(id INTEGER PRIMARY KEY, date TEXT, text TEXT)")
    old.close()
    cols = {c[1] for c in open_db(path).execute("PRAGMA table_info(messages)")}
    assert set(COLUMN_NAMES) <= cols


def test_parse_options_rejects_unknown_names():
    assert config.parse_options("meta, Polls") == {"meta", "polls"}
    with pytest.raises(ValueError, match="unknown option"):
        config.parse_options("meta,videos")


# --- filters


def test_scope_has_its_own_database_and_folder_name():
    scope = Scope(parse_date("2026-05-01"), parse_date("2026-05-31"), "@alice", "links")
    assert scope.tag() == "since-2026-05-01_until-2026-05-31_from-alice_links"
    assert scope.db_suffix().startswith("_") and len(scope.db_suffix()) == 9
    assert Scope().db_suffix() == "" and not Scope()


def test_scope_validates_input():
    with pytest.raises(ValueError, match="unknown type"):
        Scope(kind="stickers")
    with pytest.raises(ValueError, match="after the end date"):
        Scope(parse_date("2026-06-01"), parse_date("2026-05-01"))


# --- planning


def test_plan_full_skips_ids_below_first_message(db):
    plan_full(db, first_id=10_001, top_id=22_000)
    tasks = db.execute("SELECT lo, hi, cursor FROM tasks ORDER BY lo").fetchall()
    assert tasks[0] == (10_000, 15_000, 15_001)
    assert tasks[-1][1] == 22_000


def test_plan_full_only_adds_new_ranges(db):
    plan_full(db, 1, 12_000)
    db.execute("UPDATE tasks SET done=1")
    plan_full(db, 1, 13_500)
    pending = db.execute("SELECT lo, hi FROM tasks WHERE done=0").fetchall()
    assert pending == [(12_000, 13_500)]


def test_plan_topics_resumes_from_newest_stored_message(db):
    plan_topics(db, [10])
    db.execute("UPDATE tasks SET done=1")
    save(db, "topic:10", [to_row(msg(42, reply_to=topic_reply(10)), forum=True)])
    plan_topics(db, [10])
    assert db.execute("SELECT lo, cursor, done FROM tasks WHERE key='topic:10'").fetchone() == (42, 0, 0)


# --- export


def test_export_writes_one_folder_per_topic(db, tmp_path):
    db.execute("INSERT INTO tasks(key, lo, cursor) VALUES('k', 0, 0)")
    rows = [
        to_row(msg(11, "hello", reply_to=topic_reply(10)), forum=True),
        to_row(msg(12, "answer", reply_to=topic_reply(11, top=10)), forum=True),
        to_row(msg(13, "general"), forum=True),
    ]
    save(db, "k", rows)
    db.execute("INSERT INTO topics VALUES(10, 'Tips & tricks!')")

    out = export(db, 123, "My chat", out_root=tmp_path)

    assert out == tmp_path / "123_My_chat"
    assert sorted(p.name for p in out.iterdir()) == ["10_Tips_tricks", "1_General"]
    lines = (out / "10_Tips_tricks" / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["text"] for line in lines] == ["hello", "answer"]
    assert "views" not in json.loads(lines[0])  # optional fields that were not collected are left out
    txt = (out / "10_Tips_tricks" / "messages.txt").read_text(encoding="utf-8")
    assert "(reply to #11)" in txt
    assert "(reply to #10)" not in txt  # replying to the topic root is not a real reply


def test_export_shows_extras_in_text(db, tmp_path):
    db.execute("INSERT INTO tasks(key, lo, cursor) VALUES('k', 0, 0)")
    m = msg(
        30,
        "Bold",
        entities=[MessageEntityBold(0, 4)],
        views=1500,
        reactions=MessageReactions(results=[ReactionCount(reaction=ReactionEmoji("🔥"), count=4)]),
    )
    save(db, "k", [to_row(m, forum=False, options=ALL)])
    out = export(db, 5, "Chan", out_root=tmp_path)
    line = (out / "messages.txt").read_text(encoding="utf-8")
    assert "**Bold**" in line and "🔥 4" in line and "1,500 views" in line
    record = json.loads((out / "messages.jsonl").read_text(encoding="utf-8"))
    assert record["reactions"] == {"🔥": 4}


def test_export_groups_comments_by_channel_post(db, tmp_path):
    db.execute("INSERT INTO tasks(key, lo, cursor) VALUES('k', 0, 0)")
    root = msg(100, "post copy", fwd_from=MessageFwdHeader(date=DATE, channel_post=7))
    rows = [
        to_row(root, forum=False),
        to_row(msg(101, "first!", reply_to=MessageReplyHeader(reply_to_msg_id=100)), forum=False),
        to_row(msg(102, "agree", reply_to=MessageReplyHeader(reply_to_msg_id=101, reply_to_top_id=100)), forum=False),
    ]
    save(db, "k", rows)
    out = export(db, 9, "Group", folder=tmp_path / "comments", threads=True)
    txt = (out / "messages.txt").read_text(encoding="utf-8")
    assert "=== post #7 ===" in txt
    assert "(reply to #101)" in txt and "(reply to #100)" not in txt
    threads = [json.loads(line)["thread"] for line in (out / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
    assert threads == [100, 100, 100]


def test_scoped_export_goes_to_its_own_folder(db, tmp_path):
    out = export(db, 5, "Chan", out_root=tmp_path, scope=Scope(kind="links"))
    assert out == tmp_path / "5_Chan_links"


def test_slug_keeps_unicode_words():
    assert slug("Заметки и идеи / FAQ") == "Заметки_и_идеи_FAQ"


def test_chat_search_suggests_while_typing_and_tab_fills_in():
    from prompt_toolkit.document import Document

    from tgdump.interactive import ChatCompleter, ChatItem

    chats = {
        -1001: ChatItem(-1001, None, "Заметки и идеи", "forum", "notes_club", {1}),
        -1002: ChatItem(-1002, None, "Кулинарный клуб", "group", None, {1, 2}),
    }
    completer = ChatCompleter(chats, n_accounts=2)

    def suggest(text):
        return [c.text for c in completer.get_completions(Document(text), None)]

    assert suggest("зам") == ["Заметки и идеи"]
    assert suggest("@notes") == ["Заметки и идеи"]
    assert suggest("клуб") == ["Кулинарный клуб"]
    assert suggest("") == []


def test_takeout_reuses_an_open_one_and_replaces_a_blank_or_closed_one():
    import asyncio
    import contextlib
    from types import SimpleNamespace

    from telethon import errors

    from tgdump.fetch import enter_takeout

    class Client:
        def __init__(self, takeout_id, closed=False):
            self.session = SimpleNamespace(takeout_id=takeout_id)
            self.closed = closed
            self.scopes = None

        async def __call__(self, request):
            if self.closed:
                raise errors.TakeoutInvalidError(request)

        def takeout(self, finalize, **scopes):
            self.scopes = scopes
            return contextlib.nullcontext(self)

    async def enter(client):
        async with contextlib.AsyncExitStack() as stack:
            await enter_takeout(stack, client)

    blank, open_one, closed = Client(b""), Client(42), Client(43, closed=True)
    for client in (blank, open_one, closed):
        asyncio.run(enter(client))
    assert blank.session.takeout_id is None and blank.scopes  # a new takeout is requested
    assert open_one.scopes == {}  # the one left open is reused
    assert closed.session.takeout_id is None and closed.scopes  # Telegram closed it, so a new one is requested


def test_colors_keep_the_progress_line_width_and_turn_off_when_disabled(monkeypatch):
    from tgdump import style

    monkeypatch.setattr(style, "enabled", True)
    line = style.paint("[████", "blue") + style.paint("░░]", "dim") + " 62%  " + style.paint("112,880/min", "green")
    plain = style.CODE_RE.sub("", style.fit(line, 12))
    assert plain == "[████░░] 62%"
    assert style.CODE_RE.sub("", style.fit(line, 40)) == "[████░░] 62%  112,880/min".ljust(40)
    assert style.keys("[o] open folder  [Enter] quit > ").count("\x1b[94m") == 3

    monkeypatch.setattr(style, "enabled", False)
    assert style.paint("done", "green", "bold") == "done"
    assert style.keys("[y/N] ") == "[y/N] "
    assert style.fit("abc", 5) == "abc  "


def test_progress_line_shrinks_the_bar_to_keep_the_rate_limit_visible():
    import time
    from types import SimpleNamespace

    from tgdump.interactive import ProgressLine
    from tgdump.style import visible_len

    stats = {"phase": "downloading", "work_total": 100, "work_left": 40, "stored": 94_410, "fetched": 94_410}
    watch = SimpleNamespace(flood_until=time.monotonic() + 12, warnings=[])
    wide, narrow = ProgressLine(stats, watch).render(200), ProgressLine(stats, watch).render(60)
    assert "+94,410" not in wide  # a fresh dump shows the count once
    assert "rate limit 12s" in narrow and "ETA" not in narrow
    assert visible_len(narrow) == 60 and visible_len(wide) - visible_len(narrow) == 6  # only the bar got shorter


def test_takeout_is_on_until_extras_are_set(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.delenv("TG_OPTIONS", raising=False)
    assert config.saved_options() == {"takeout"}

    config.save_options(set())  # everything switched off in the menu
    assert "TG_OPTIONS=none" in (tmp_path / ".env").read_text(encoding="utf-8")
    assert config.saved_options() == set()

    config.save_options({"meta"})
    assert config.saved_options() == {"meta"}


def test_repeat_export_appends_new_messages_and_rewrites_when_older_ones_arrive(db, tmp_path):
    db.execute("INSERT INTO tasks(key, lo, cursor) VALUES('k', 0, 0)")
    save(db, "k", [to_row(msg(20, "first"), forum=False)])
    out = export(db, 7, "Chat", out_root=tmp_path)
    txt = out / "messages.txt"
    txt.write_text(txt.read_text(encoding="utf-8") + "marker\n", encoding="utf-8")

    save(db, "k", [to_row(msg(21, "newer"), forum=False)])
    export(db, 7, "Chat", out_root=tmp_path)
    lines = txt.read_text(encoding="utf-8").splitlines()
    assert lines[1] == "marker" and lines[2].endswith("#21 ?: newer")  # appended, not rewritten

    save(db, "k", [to_row(msg(5, "older"), forum=False)])  # e.g. a resumed range below the last export
    export(db, 7, "Chat", out_root=tmp_path)
    ids = [json.loads(line)["id"] for line in (out / "messages.jsonl").read_text(encoding="utf-8").splitlines()]
    assert ids == [5, 20, 21] and "marker" not in txt.read_text(encoding="utf-8")


def test_new_message_count_is_exact_for_a_short_gap_and_estimated_for_a_long_one(db):
    import asyncio
    from types import SimpleNamespace

    from tgdump import interactive

    class Found(list):
        total = 9_999

    class Client:
        def __init__(self, newest_ids):
            self.newest_ids = newest_ids

        async def get_messages(self, target, limit, min_id):
            return Found(SimpleNamespace(id=i) for i in self.newest_ids[:limit])

    db.execute("INSERT INTO tasks(key, lo, cursor) VALUES('k', 0, 0)")
    save(db, "k", [to_row(msg(i), forum=False) for i in range(1, 1001, 2)])  # every other id saved, up to 999
    short = asyncio.run(interactive.count_new(Client([1005, 1003]), "@chat", db))
    long = asyncio.run(interactive.count_new(Client(list(range(1999, 999, -1))), "@chat", db))
    assert short == (2, 9_999, True)
    assert long[1:] == (9_999, False) and 499 <= long[0] <= 501  # 1000 new ids, about half of ids are messages


def test_private_chat_is_dumped_by_the_account_that_sees_most_of_it():
    import asyncio
    from types import SimpleNamespace

    from tgdump.fetch import busiest

    class Client:
        def __init__(self, total):
            self.total = total

        async def get_messages(self, target, limit):
            if self.total is None:
                raise ValueError("unknown user")
            return SimpleNamespace(total=self.total)

    # message ids in a private chat are per account, so their ranges cannot be shared
    side, main, stranger = Client(1), Client(189_002), Client(None)
    assert asyncio.run(busiest([(side, "u"), (stranger, "u"), (main, "u")]))[0] is main
    assert asyncio.run(busiest([(stranger, "u"), (side, "u")]))[0] is side


def test_copy_saved_by_another_account_is_recognized(db):
    import asyncio
    from types import SimpleNamespace

    from tgdump.fetch import saved_by

    class Client:
        def __init__(self, dates):
            self.dates = dates  # id -> date as this account sees it

        async def get_messages(self, entity, ids):
            return [SimpleNamespace(date=self.dates[i]) if i in self.dates else None for i in ids]

    later = DATE + datetime.timedelta(days=1)
    assert asyncio.run(saved_by(Client({}), "u", db))  # nothing saved yet
    db.execute("INSERT INTO tasks(key, lo, cursor) VALUES('k', 0, 0)")
    save(db, "k", [to_row(msg(i), forum=False) for i in (10, 11)])
    assert asyncio.run(saved_by(Client({10: DATE}), "u", db))  # 11 was deleted, 10 still matches
    assert not asyncio.run(saved_by(Client({10: later, 11: later}), "u", db))  # same ids, other messages
    assert not asyncio.run(saved_by(Client({}), "u", db))


def test_account_is_found_by_session_name_username_or_id():
    import asyncio
    from types import SimpleNamespace

    from tgdump.accounts import find_account

    class Client:
        def __init__(self, file, id, username):
            self.session = SimpleNamespace(filename=f"data/{file}.session")
            self.me = SimpleNamespace(id=id, username=username)

        async def get_me(self):
            return self.me

    main, side = Client("session", 101, "Main"), Client("acc2", 202, None)
    clients = [main, side]
    assert asyncio.run(find_account(clients, "acc2")) is side
    assert asyncio.run(find_account(clients, "@main")) is main
    assert asyncio.run(find_account(clients, 202)) is side
    with pytest.raises(ValueError):
        asyncio.run(find_account(clients, "nobody"))
