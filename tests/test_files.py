"""The "files" extra: which files are downloaded, where they land, and how the export and the reader show them."""

import asyncio
import json
import re
import sqlite3

import pytest
from telethon import types
from test_changes import DATE, NOW, Telegram, day, msg, run
from test_people import club, topic

from tgdump.fetch import copy_scope
from tgdump.files import Limits, download_files, file_limits, file_name
from tgdump.scope import Scope
from tgdump.store import open_db, plan_full, save, to_row

FILES = frozenset({"files"})
OUT = "out/7_Club"


def photo(id, w=800, h=600, size=5000, **kw):
    sizes = [types.PhotoStrippedSize("i", b""), types.PhotoSize("m", w // 4, h // 4, size // 9)]
    sizes.append(types.PhotoSize("x", w, h, size))
    return types.MessageMediaPhoto(photo=types.Photo(id, 1, b"", DATE, sizes, dc_id=4), **kw)


def doc(id, *attributes, mime="application/pdf", size=3000, **flags):
    return types.MessageMediaDocument(
        document=types.Document(id, 1, b"", DATE, mime, size, 2, list(attributes)), **flags
    )


def named(name):
    return types.DocumentAttributeFilename(name)


def club_with_files():
    """A group with a photo, a document, a voice message, a sticker and a plain message."""
    voice = doc(503, types.DocumentAttributeAudio(duration=4, voice=True), mime="audio/ogg", size=700, voice=True)
    sticker = types.DocumentAttributeSticker(alt="🙂", stickerset=types.InputStickerSetEmpty())
    tg = Telegram(
        [
            day(1, 0, media=photo(501)),
            day(2, 0, "the report", media=doc(502, named("report.pdf"))),
            day(3, 1, media=voice),
            day(4, 1, media=doc(504, sticker, mime="image/webp", size=90)),
            day(5, 2, "no file here"),
        ]
    )
    tg.content = {501: b"p" * 5000, 502: b"d" * 3000, 503: b"v" * 700, 504: b"s" * 90}
    return tg


def saved(home, sql="SELECT id, path, size, w, h FROM files ORDER BY id", name="7"):
    db = sqlite3.connect(home / f"{name}.sqlite")
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def records(folder):
    lines = (folder / "messages.jsonl").read_text(encoding="utf-8").splitlines()
    return {m["id"]: m for m in map(json.loads, lines)}


def on_disk(folder):
    return sorted(p.name for p in (folder / "files").iterdir())


# --- what is downloaded and where it lands


def test_files_land_in_the_chat_folder_with_their_paths_in_the_export(home):
    tg = club_with_files()
    stats = run(tg, options=FILES | {"html"})
    out = home / OUT
    assert on_disk(out) == ["1.jpg", "2_report.pdf", "3.ogg"]  # stickers only when asked for
    assert (out / "files" / "2_report.pdf").read_bytes() == b"d" * 3000
    assert saved(home) == [
        (1, "files/1.jpg", 5000, 800, 600),
        (2, "files/2_report.pdf", 3000, None, None),
        (3, "files/3.ogg", 700, None, None),
    ]
    assert stats["files"] == (3, 3, 8700)
    # each file was asked of the data centre that keeps it, the photo in its largest size
    assert sorted(tg.downloads) == [(501, 4), (502, 2), (503, 2)]
    got = records(out)
    assert got[1]["extra"]["file"] == {"path": "files/1.jpg", "size": 5000, "w": 800, "h": 600}
    assert got[2]["extra"]["file"] == {
        "path": "files/2_report.pdf",
        "size": 3000,
        "mime": "application/pdf",
        "name": "report.pdf",
    }
    assert "file" not in got[4]["extra"] or "path" not in got[4]["extra"]["file"]
    text = (out / "messages.txt").read_text(encoding="utf-8")
    assert "<MessageMediaDocument: report.pdf, 2.9 KB, files/2_report.pdf> the report" in text
    assert "<MessageMediaPhoto: 4.9 KB, files/1.jpg>" in text
    data = (out / ".reader" / "all.0.js").read_text(encoding="utf-8")
    assert '"path":"files/1.jpg"' in data and '"w":800' in data


def test_a_file_is_downloaded_once_and_again_when_it_is_removed(home):
    tg = club_with_files()
    run(tg, options=FILES)
    tg.downloads.clear()
    tg.looked_up = 0
    stats = run(tg, options=FILES)
    assert tg.downloads == [] and tg.looked_up == 0 and "files" not in stats
    (home / OUT / "files" / "1.jpg").unlink()
    run(tg, options=FILES)
    assert tg.downloads == [(501, 4)] and on_disk(home / OUT) == ["1.jpg", "2_report.pdf", "3.ogg"]


def test_without_the_extra_nothing_is_downloaded(home):
    tg = club_with_files()
    run(tg)
    assert tg.downloads == [] and not (home / OUT / "files").exists()


def test_types_and_size_limit_what_is_downloaded(home):
    tg = club_with_files()
    tg.messages[6] = day(6, 2, media=doc(506, named("film.mkv"), mime="video/x-matroska", size=30_000_000))
    run(tg, options=FILES, files=file_limits("docs,stickers", "20"))
    # the large one is known to be large from what is saved, so Telegram is not asked about it
    assert on_disk(home / OUT) == ["2_report.pdf", "4.webp"] and tg.looked_up == 2
    tg.content[506] = b"f" * 10
    run(tg, options=FILES, files=file_limits("all", "0"))
    assert on_disk(home / OUT) == ["1.jpg", "2_report.pdf", "3.ogg", "4.webp", "6_film.mkv"]


def test_a_photo_over_the_limit_waits_for_a_larger_one(home):
    tg = club_with_files()
    tg.messages[1] = day(1, 0, media=photo(501, size=3_000_000))
    tg.content[501] = b"p" * 3_000_000
    run(tg, options=FILES, files=file_limits("photos", "1"))
    # the size of a photo is not saved with the message, so it took a look to know
    assert tg.downloads == [] and saved(home) == [(1, None, 3_000_000, None, None)]
    tg.looked_up = 0
    run(tg, options=FILES, files=file_limits("photos", "1"))
    assert tg.looked_up == 0  # not looked at again while the limit stays
    run(tg, options=FILES, files=file_limits("photos", "5"))
    assert saved(home) == [(1, "files/1.jpg", 3_000_000, 800, 600)]


def test_a_file_gone_from_telegram_is_noted_and_left_alone(home):
    tg = club_with_files()
    tg.messages[1] = day(1, 0, media=types.MessageMediaPhoto(photo=None, ttl_seconds=10))  # it has expired
    del tg.content[502]  # Telegram refuses to hand this one out
    run(tg, options=FILES)
    assert on_disk(home / OUT) == ["3.ogg"]
    assert saved(home) == [(1, None, None, None, None), (3, "files/3.ogg", 700, None, None)]
    tg.looked_up = 0
    tg.content[502] = b"d" * 3000
    run(tg, options=FILES)
    assert tg.looked_up == 1 and on_disk(home / OUT) == ["2_report.pdf", "3.ogg"]  # a failed download is tried again
    run(tg, options=FILES, again=True)  # --refetch also looks at the gone ones again
    assert tg.looked_up == 2


def test_files_of_a_forum_go_to_the_folders_of_their_topics(home):
    def in_topic(id, to, **kw):
        return msg(id, reply_to=types.MessageReplyHeader(forum_topic=True, reply_to_msg_id=to), **kw)

    plans = msg(10, action=types.MessageActionTopicCreate(title="Plans", icon_color=1))
    tg = Telegram([plans, in_topic(11, 10, media=photo(501)), msg(12, media=photo(502))], entity=club(forum=True))
    tg.topics = [topic(10, "Plans"), topic(1, "General")]
    tg.content = {501: b"a", 502: b"b"}
    run(tg, options=FILES | {"html"})
    out = home / OUT
    assert on_disk(out / "10_Plans") == ["11.jpg"] and on_disk(out / "1_General") == ["12.jpg"]
    assert records(out / "10_Plans")[11]["extra"]["file"]["path"] == "files/11.jpg"
    page = (out / "index.html").read_text(encoding="utf-8")
    meta = json.loads(re.search(r"const META = (.*);</script>", page)[1])
    assert {t[1]: t[7] for t in meta["topics"]} == {10: "10_Plans", 1: "1_General"}


def test_a_filtered_dump_gets_its_own_files(home):
    tg = club_with_files()
    run(tg)
    part = home / "out" / "7_Club_photos"
    run(tg, scope=Scope(kind="photos"), options=FILES)
    assert on_disk(part) == ["1.jpg"] and not (home / OUT / "files").exists()
    assert records(part)[1]["extra"]["file"]["path"] == "files/1.jpg"
    run(tg, scope=Scope(kind="photos"), options=FILES)  # the copy is made anew, the file stays noted
    assert tg.downloads == [(501, 4)] and records(part)[1]["extra"]["file"]["path"] == "files/1.jpg"


def test_a_filtered_copy_notes_what_changed_in_the_whole_chat(tmp_path):
    whole, part = tmp_path / "1.sqlite", tmp_path / "1_x.sqlite"
    db = open_db(whole)
    plan_full(db, 1, 2)
    save(db, "range:0", [to_row(m, forum=False) for m in (day(2, 0, "second"), day(1, 0, "frist"))], done=True)
    assert copy_scope(whole, part, Scope(kind=None, since=DATE.date())) == 2
    save(db, "range:0", [to_row(day(1, 0, "first", edit_date=DATE), forum=False)])
    db.close()
    copy_scope(whole, part, Scope(kind=None, since=DATE.date()))
    copy = sqlite3.connect(part)
    assert copy.execute("SELECT id FROM changed").fetchall() == [(1,)]
    assert copy.execute("SELECT text FROM messages WHERE id = 1").fetchone() == ("first",)
    copy.close()


def test_what_one_account_cannot_see_is_asked_of_the_other(tmp_path):
    late, early = club_with_files(), club_with_files()
    del late.messages[1]  # this account joined after the photo was sent
    del late.messages[2], early.messages[2]  # and this message is gone for everyone
    db = open_db(tmp_path / "7.sqlite")
    plan_full(db, 1, 5)
    save(db, "range:0", [to_row(m, forum=False) for m in club_with_files().messages.values()], done=True)
    limits, stats = Limits(frozenset({"photo", None, "voice"}), 0), {}
    pairs = [(late, late.entity), (early, early.entity)]
    assert asyncio.run(download_files(pairs, db, tmp_path / "out", limits, NOW, stats)) == 2
    assert on_disk(tmp_path / "out") == ["1.jpg", "3.ogg"] and early.downloads[-1] == (501, 4)
    assert db.execute("SELECT id, path FROM files WHERE path IS NULL").fetchall() == [(2, None)]
    assert stats["files"] == (3, 3, 5700)
    db.close()


# --- limits and names


def test_file_limits_come_from_the_command_line_then_the_settings(monkeypatch):
    monkeypatch.delenv("TG_FILE_TYPES", raising=False)
    monkeypatch.delenv("TG_FILE_SIZE", raising=False)
    usual = file_limits()
    assert usual.size == 20 * 1024 * 1024 and "sticker" not in usual.kinds and "photo" in usual.kinds
    assert usual.wants(None, 1000) and not usual.wants(None, 21 * 1024 * 1024) and usual.wants("photo", None)
    monkeypatch.setenv("TG_FILE_TYPES", "voice, Round")
    monkeypatch.setenv("TG_FILE_SIZE", "1.5")
    assert file_limits() == Limits(frozenset({"voice", "round"}), 1572864)
    assert file_limits("all", "0") == Limits(
        frozenset({"photo", "video", "gif", "voice", "round", "audio", None, "sticker"}), 0
    )
    assert str(file_limits("photos,docs", 0)) == "photos, docs, of any size"
    assert str(file_limits("music", 2)) == "music, up to 2.0 MB"
    with pytest.raises(ValueError, match="unknown file type"):
        file_limits("photos,films")
    for size in ("big", "-1"):
        with pytest.raises(ValueError, match="megabytes"):
            file_limits(None, size)


@pytest.mark.parametrize(
    ("media", "name"),
    [
        (photo(1), "9.jpg"),
        (doc(1, named("notes.txt"), mime="text/plain"), "9_notes.txt"),
        (doc(1, named("../../etc/pass?wd.txt")), "9_pass_wd.txt"),
        (doc(1, named('a<b>:"c"|d*.zip')), "9_a_b_c_d_.zip"),
        (doc(1, named("x" * 200 + ".tar.gz")), "9_" + "x" * 60 + ".gz"),
        (doc(1, named(" . ")), "9.pdf"),
        (doc(1, mime="audio/ogg"), "9.ogg"),
        (doc(1, mime="application/x-unknown-thing"), "9"),
        (doc(1, named("Отчёт за май.docx")), "9_Отчёт за май.docx"),
    ],
)
def test_file_names_start_with_the_message_id_and_are_safe(media, name):
    assert file_name(msg(9, media=media)) == name


# --- the interactive mode


def test_file_limits_are_changed_in_the_settings(tmp_path, monkeypatch):
    from tgdump import config, interactive

    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setenv("TG_FILE_TYPES", "")
    monkeypatch.setenv("TG_FILE_SIZE", "")
    answers = iter(["Photos, docs", "1,5", "films", "", "", ""])

    async def ask(prompt=""):
        return next(answers)

    monkeypatch.setattr(interactive, "ask", ask)
    asyncio.run(interactive.change_file_limits())
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "TG_FILE_TYPES=photos,docs\nTG_FILE_SIZE=1.5\n"
    assert interactive.files_text() == "photos, docs, up to 1.5 MB"
    asyncio.run(interactive.change_file_limits())  # an unknown type is not saved
    asyncio.run(interactive.change_file_limits())  # and Enter twice keeps both
    assert interactive.files_text() == "photos, docs, up to 1.5 MB"


def test_progress_line_counts_the_files():
    from types import SimpleNamespace

    from tgdump.interactive import ProgressLine

    watch = SimpleNamespace(flood_until=0, warnings=[])
    line = ProgressLine({"phase": "files", "files": (1200, 3400, 310 * 1024 * 1024)}, watch).render()
    assert "downloading files… 1,200 of 3,400, 310.0 MB" in line
    assert "looking for files" in ProgressLine({"phase": "files"}, watch).render()


def test_a_stopped_download_leaves_no_half_of_a_file(home):
    tg = club_with_files()

    def stopped(file, dc_id=None, file_size=None):
        async def chunks():
            yield b"half"
            raise KeyboardInterrupt

        return chunks()

    tg.iter_download = stopped
    with pytest.raises(KeyboardInterrupt):
        run(tg, options=FILES)
    assert on_disk(home / OUT) == [] and saved(home) == []
    assert 1 in records(home / OUT)  # the messages were exported before the files
