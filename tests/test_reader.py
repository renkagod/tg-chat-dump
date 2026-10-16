import datetime
import json
import os
import re
import sqlite3

import pytest

from tgdump import reader
from tgdump.reader import DATA_DIR, js_literal, write_reader
from tgdump.store import COLUMN_NAMES, SCHEMA

XSS = "</script><img src=x onerror=alert(1)>"
LS, PS = chr(0x2028), chr(0x2029)  # line and paragraph separators, valid in JSON but not in older JS
OLD = 1_000_000_000  # mtime of files that must not be rewritten


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("CREATE INDEX messages_topic ON messages(topic_id, id)")
    yield conn
    conn.close()


@pytest.fixture
def small_parts(monkeypatch):
    monkeypatch.setattr(reader, "PART_ROWS", 3)


def add(db, id, topic=None, text="", **kw):
    row = dict.fromkeys(COLUMN_NAMES)
    row.update(
        id=id, date=f"2026-05-13T06:{id % 60:02d}:00+00:00", topic_id=topic, sender_id=7, sender="Ann", text=text
    )
    row.update(kw)
    db.execute(f"INSERT INTO messages VALUES({','.join('?' * len(COLUMN_NAMES))})", [row[c] for c in COLUMN_NAMES])


def files(out):
    return sorted(p.name for p in (out / DATA_DIR).iterdir())


def part(out, key, n):
    """Rows and senders of one data file, decoded from its TGR.a/TGR.s/TGR.e calls."""
    rows, senders, ended = [], None, False
    for line in (out / DATA_DIR / f"{key}.{n}.js").read_text(encoding="utf-8").splitlines():
        call, k, i, payload = re.fullmatch(r"TGR\.(\w)\((\"[^\"]*\"),(\d+)(?:,(.*))?\);", line).groups()
        assert (json.loads(k), int(i)) == (key, n)
        if call == "a":
            rows += json.loads(payload)
        elif call == "s":
            senders = json.loads(payload)
        else:
            ended = True
    assert ended
    return rows, senders


def data(out, key):
    """All rows of a list, and the senders of its first part."""
    n = len([f for f in files(out) if f.startswith(key + ".")])
    parts = [part(out, key, i) for i in range(n)]
    return [r for rows, _ in parts for r in rows], parts[0][1]


def meta(out):
    page = (out / "index.html").read_text(encoding="utf-8")
    return json.loads(re.search(r"<script>const META = (.*?);</script>", page)[1])


def age(out):
    """Mark every data file as old, so a rewrite shows in its mtime."""
    for p in (out / DATA_DIR).iterdir():
        os.utime(p, (OLD, OLD))


def untouched(out):
    return {p.name for p in (out / DATA_DIR).iterdir() if p.stat().st_mtime == OLD}


def test_forum_gets_data_files_per_topic(db, tmp_path):
    add(db, 1, 1, "hello")
    add(db, 10, 10, action="MessageActionTopicCreate", text=None)
    add(db, 11, 10, "in topic", reply_to=10)
    add(db, 12, 10, "answer", reply_to=11)
    index = write_reader(db, tmp_path, "Chat", {10: "Topic ten"})
    assert index == tmp_path / "index.html" and index.exists()
    assert files(tmp_path) == ["1.0.js", "10.0.js"]
    m = meta(tmp_path)
    assert m["forum"] and not m["threads"]
    assert {(t[0], t[2], t[3], len(t[5])) for t in m["topics"]} == {("1", "General", 1, 1), ("10", "Topic ten", 3, 1)}
    rows, senders = data(tmp_path, "10")
    assert [r[0] for r in rows] == [10, 11, 12]
    assert rows[0][7] == "TopicCreate"
    assert len(rows[1]) == 4  # reply to the topic itself is not a reply; trailing nulls are dropped
    assert rows[2][4] == 11
    assert senders == [[7, "Ann"]]


def test_only_changed_topics_are_rewritten(db, tmp_path):
    add(db, 1, 1, "old general")
    add(db, 10, 10, "old ten")
    write_reader(db, tmp_path, "Chat", {})
    db.execute("UPDATE messages SET text = 'new ' || id")
    add(db, 20, 20, "brand new topic")
    write_reader(db, tmp_path, "Chat", {}, topics={10})
    assert data(tmp_path, "1")[0][0][3] == "old general"
    assert data(tmp_path, "10")[0][0][3] == "new 10"
    assert data(tmp_path, "20")[0][0][3] == "brand new topic"  # missing files are always written
    assert len(meta(tmp_path)["topics"]) == 3
    write_reader(db, tmp_path, "Chat", {})
    assert data(tmp_path, "1")[0][0][3] == "new 1"


def test_lists_are_split_into_parts_with_their_own_senders(db, tmp_path, small_parts):
    for i in range(1, 8):
        add(db, i, text=f"m{i}", sender="Bob" if i == 7 else "Ann", grouped_id=2**60 + 1 if i in (3, 4) else None)
    write_reader(db, tmp_path, "Plain", {})
    assert files(tmp_path) == ["all.0.js", "all.1.js", "all.2.js"]
    assert [len(part(tmp_path, "all", n)[0]) for n in range(3)] == [3, 3, 1]
    assert part(tmp_path, "all", 1)[1] == [[7, "Ann"]]
    assert part(tmp_path, "all", 2) == ([[7, 1778652420, 0, "m7"]], [[7, "Bob"]])
    rows = data(tmp_path, "all")[0]
    assert rows[2][8] == rows[3][8] == 1  # one album across a part boundary, kept exact for JS
    assert len(meta(tmp_path)["topics"][0][5]) == 3


def test_repeat_run_with_since_rewrites_only_the_tail(db, tmp_path, small_parts):
    for i in range(1, 8):
        add(db, i, text=f"m{i}")
    write_reader(db, tmp_path, "Plain", {})
    before = {n: (tmp_path / DATA_DIR / f"all.{n}.js").read_bytes() for n in range(3)}
    age(tmp_path)
    db.execute("UPDATE messages SET text = 'changed' WHERE id = 1")  # not rewritten: older than since
    for i in range(8, 12):
        add(db, i, text=f"m{i}")
    write_reader(db, tmp_path, "Plain", {}, topics={None}, since=7)
    assert files(tmp_path) == ["all.0.js", "all.1.js", "all.2.js", "all.3.js"]
    assert untouched(tmp_path) == {"all.0.js", "all.1.js"}
    assert all((tmp_path / DATA_DIR / f"all.{n}.js").read_bytes() == before[n] for n in (0, 1))
    assert [r[0] for r in data(tmp_path, "all")[0]] == list(range(1, 12))
    assert len(meta(tmp_path)["topics"][0][5]) == 4

    db.execute("UPDATE messages SET text = 'm1' WHERE id = 1")
    full = tmp_path / "full"
    write_reader(db, full, "Plain", {})
    for n in range(4):  # parts written in pieces are the same as parts written at once
        assert (full / DATA_DIR / f"all.{n}.js").read_bytes() == (tmp_path / DATA_DIR / f"all.{n}.js").read_bytes()


def test_since_on_a_full_last_part_only_adds_parts(db, tmp_path, small_parts):
    for i in range(1, 7):
        add(db, i, text=f"m{i}")
    write_reader(db, tmp_path, "Plain", {})
    age(tmp_path)
    add(db, 7, text="m7")
    write_reader(db, tmp_path, "Plain", {}, topics={None}, since=6)
    assert untouched(tmp_path) == {"all.0.js", "all.1.js"}
    assert part(tmp_path, "all", 2)[0] == [[7, 1778652420, 0, "m7"]]


def test_since_in_a_forum_touches_only_the_changed_topics(db, tmp_path, small_parts):
    for i in range(1, 8):
        add(db, i, 1 if i % 2 else 10, text=f"m{i}")
    write_reader(db, tmp_path, "Forum", {})  # topic 1: 1 3 5 | 7, topic 10: 2 4 6
    age(tmp_path)
    add(db, 8, 10, "new")
    add(db, 9, 10, "newer")
    write_reader(db, tmp_path, "Forum", {}, topics={10}, since=7)
    assert untouched(tmp_path) == {"1.0.js", "1.1.js", "10.0.js"}
    assert [r[0] for r in part(tmp_path, "10", 1)[0]] == [8, 9]


def test_missing_parts_are_written_even_with_since(db, tmp_path, small_parts):
    for i in range(1, 8):
        add(db, i, text=f"m{i}")
    write_reader(db, tmp_path, "Plain", {})
    (tmp_path / DATA_DIR / "all.0.js").unlink()
    age(tmp_path)
    add(db, 8, text="m8")
    write_reader(db, tmp_path, "Plain", {}, topics={None}, since=7)
    assert untouched(tmp_path) == set()
    assert [r[0] for r in data(tmp_path, "all")[0]] == list(range(1, 9))


def test_full_rewrite_removes_stale_parts_and_the_old_layout(db, tmp_path, small_parts):
    for i in range(1, 8):
        add(db, i, text=f"m{i}")
    write_reader(db, tmp_path, "Plain", {})
    (tmp_path / DATA_DIR / "all.js").write_text("old layout")
    (tmp_path / DATA_DIR / "all.5.js.tmp").write_text("cut short")
    db.execute("DELETE FROM messages WHERE id > 3")
    write_reader(db, tmp_path, "Plain", {})
    assert files(tmp_path) == ["all.0.js"]
    assert len(meta(tmp_path)["topics"][0][5]) == 1


def test_threads_mode_ignores_since(db, tmp_path, small_parts):
    for i in range(1, 5):
        add(db, i, text=f"post {i}")
    write_reader(db, tmp_path, "Discussion", {}, threads=True)
    age(tmp_path)
    add(db, 5, text="comment on the first post", reply_to=1)
    write_reader(db, tmp_path, "Discussion", {}, topics={None}, threads=True, since=4)
    assert untouched(tmp_path) == set()
    assert [r[0] for r in data(tmp_path, "all")[0]] == [1, 5, 2, 3, 4]


def test_untrusted_text_cannot_break_out_of_scripts(db, tmp_path):
    add(db, 1, None, XSS + " & " + LS, sender=XSS, fwd_from="<b>x</b>")
    write_reader(db, tmp_path, XSS, {})
    raw = (tmp_path / DATA_DIR / "all.0.js").read_text(encoding="utf-8")
    assert not re.search(f"[<>&{LS}{PS}]", raw)
    rows, senders = data(tmp_path, "all")
    assert rows[0][3] == XSS + " & " + LS and senders == [[7, XSS]] and rows[0][5] == "<b>x</b>"
    page = (tmp_path / "index.html").read_text(encoding="utf-8")
    assert page.count("</script") == 2  # only the page's own two script tags
    assert "<title>&lt;/script&gt;&lt;img src=x onerror=alert(1)&gt;</title>" in page
    assert meta(tmp_path)["title"] == XSS
    assert js_literal("</script>" + PS) == '"' + "\\u003c/script\\u003e\\u2029" + '"'


def test_plain_chat_is_one_list(db, tmp_path):
    add(db, 1, text="first", media="MessageMediaPhoto", grouped_id=555, edit_date="2026-05-13T07:00:00+00:00")
    add(db, 2, text="", media="MessageMediaPhoto", grouped_id=555)
    add(db, 3, text="plain", text_md="**plain**", reply_to=1, views=10, replies=2, reactions='{"👍": 3}')
    write_reader(db, tmp_path, "Plain", {})
    assert files(tmp_path) == ["all.0.js"]
    m = meta(tmp_path)
    assert not m["forum"] and m["topics"][0][:4] == ["all", None, "Plain", 3]
    rows, _ = data(tmp_path, "all")
    assert rows[0][1] == datetime.datetime(2026, 5, 13, 6, 1, tzinfo=datetime.UTC).timestamp()
    assert rows[0][6] == "Photo" and rows[0][8] == rows[1][8] == 555 and rows[0][9] == 1
    assert rows[1][3] is None
    assert rows[2][3] == "**plain**" and rows[2][4] == 1
    assert rows[2][10:13] == [{"👍": 3}, 10, 2]


def test_polls_and_checklists_are_kept_without_channel_post(db, tmp_path):
    poll = {"question": "Q?", "answers": [{"text": "a", "voters": 2}], "total_voters": 2, "closed": True}
    add(db, 1, text=None, media="MessageMediaPoll", extra=json.dumps({"poll": poll, "channel_post": 5}))
    add(db, 2, text=None, extra=json.dumps({"todo": {"title": "T", "items": [{"text": "x", "done": True}]}}))
    add(db, 3, text="fwd", extra=json.dumps({"channel_post": 9}))
    write_reader(db, tmp_path, "Polls", {})
    rows, _ = data(tmp_path, "all")
    assert rows[0][13] == {"poll": poll}
    assert rows[1][13]["todo"]["title"] == "T"
    assert len(rows[2]) == 4


def test_threads_mode_orders_by_thread(db, tmp_path):
    add(db, 1, text="post one", extra=json.dumps({"channel_post": 100}))
    add(db, 2, text="post two", extra=json.dumps({"channel_post": 101}))
    add(db, 3, text="comment on one", reply_to=1, reply_top=None)
    add(db, 4, text="reply in one", reply_to=3, reply_top=1)
    add(db, 5, text="comment on two", reply_to=2)
    write_reader(db, tmp_path, "Discussion", {}, threads=True)
    assert files(tmp_path) == ["all.0.js"]
    assert meta(tmp_path)["threads"]
    rows, _ = data(tmp_path, "all")
    assert [r[0] for r in rows] == [1, 3, 4, 2, 5]
    heads = {r[0]: r[14] for r in rows if len(r) > 14}
    assert heads == {1: [1, 100], 2: [1, 101]}
    reply = {r[0]: r[4] if len(r) > 4 else None for r in rows}
    assert reply == {1: None, 3: None, 4: 3, 2: None, 5: None}  # replies to the thread post are the thread


def test_removed_topics_lose_their_data_files(db, tmp_path):
    add(db, 1, 1, "a")
    add(db, 10, 10, "b")
    write_reader(db, tmp_path, "Chat", {})
    db.execute("DELETE FROM messages WHERE topic_id = 10")
    write_reader(db, tmp_path, "Chat", {}, topics=set())
    assert files(tmp_path) == ["1.0.js"]


def test_empty_chat_still_gets_a_page(db, tmp_path):
    assert write_reader(db, tmp_path, "Empty", {}).exists()
    assert meta(tmp_path)["topics"] == []
