"""--type applied to the saved copy of a chat, without asking Telegram."""

import sqlite3

import pytest
from telethon import types
from telethon.tl.types import Message, PeerChannel
from test_changes import DATE, Telegram, day, run
from test_dump import whole_dump

from tgdump.fetch import copy_scope
from tgdump.scope import KINDS, SAVED_KINDS, Scope, has_link
from tgdump.store import set_state

PEER = PeerChannel(1)


def post(id, media=None, text="", **kw):
    return Message(id=id, peer_id=PEER, date=DATE, message=text, media=media, **kw)


def file(*attributes, mime="application/octet-stream", **flags):
    doc = types.Document(1, 1, b"", DATE, mime, 10, 1, list(attributes))
    return types.MessageMediaDocument(document=doc, **flags)


def video(**kw):
    return types.DocumentAttributeVideo(duration=3, w=320, h=240, **kw)


GEO = types.GeoPoint(long=30.5, lat=50.4, access_hash=1)
MESSAGES = [
    post(1, types.MessageMediaPhoto(photo=types.PhotoEmpty(1))),
    post(2, file(video(), mime="video/mp4", video=True)),
    post(3, file(video(), types.DocumentAttributeAnimated(), mime="video/mp4", video=True)),
    post(4, file(types.DocumentAttributeAudio(duration=2, voice=True), mime="audio/ogg", voice=True)),
    post(5, file(types.DocumentAttributeAudio(duration=200, title="Song", performer="Band"), mime="audio/mpeg")),
    post(6, file(video(round_message=True), mime="video/mp4", round=True)),
    post(7, file(types.DocumentAttributeFilename("report.pdf"), mime="application/pdf")),
    post(8, file(types.DocumentAttributeSticker(alt="🙂", stickerset=types.InputStickerSetEmpty()), mime="image/webp")),
    post(9, types.MessageMediaGeo(geo=GEO)),
    post(10, types.MessageMediaVenue(GEO, "Cafe", "Long street 1", "maps", "v1", "")),
    post(11, types.MessageMediaContact("100200", "Ann", "", "", 0)),
    post(12, text="the rules are at https://example.com/rules"),
    post(13, text="here", entities=[types.MessageEntityTextUrl(0, 4, "https://example.org/")]),
    post(14, types.MessageMediaWebPage(webpage=types.WebPageEmpty(1)), text="a preview"),
    post(15, text="Bob", entities=[types.MessageEntityMentionName(0, 3, 5)]),
    post(16, text="no link in v1.2 of notes.txt, said Bob"),
    post(17, text="read this first", pinned=True),
    post(18, text="docs are on example.net now"),
]
FOUND = {
    "photos": [1],
    "videos": [2],
    "media": [1, 2],
    "docs": [7],
    "links": [12, 13, 14, 18],
    "gifs": [3],
    "voice": [4],
    "music": [5],
    "round": [6],
    "pinned": [17],
    "geo": [9, 10],
    "contacts": [11],
}


def test_every_type_has_its_saved_counterpart():
    assert SAVED_KINDS.keys() == KINDS.keys() == FOUND.keys()


@pytest.mark.parametrize("kind", FOUND)
def test_type_is_found_in_the_saved_copy(tmp_path, kind):
    whole, part = tmp_path / "1.sqlite", tmp_path / "1_x.sqlite"
    whole_dump(whole, MESSAGES)
    assert copy_scope(whole, part, Scope(kind=kind)) == len(FOUND[kind])
    db = sqlite3.connect(part)
    assert [i for (i,) in db.execute("SELECT id FROM messages ORDER BY id")] == FOUND[kind]
    db.close()


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ("http://example.com", True),
        ("see www.example.com/page", True),
        ("t.me/somechannel", True),
        ("write to example.org.", True),
        ("сайт.рф", True),
        ("the server is 203.0.113.7:8080", True),
        ("allow 198.51.100.0/24", True),
        ("ann@example.com", True),
        ("see readme.md", True),  # .md is a country's domain, so the app makes it a link
        ("tg://resolve?domain=somechannel", True),
        ("ss://c2VjcmV0QGhvc3Q6NDQz is a key, not a link", False),
        ("version 2.5.1 is out", False),
        ("notes.txt and e.g. this", False),
        ("ok.then we go", False),
        ("", False),
        (None, False),
    ],
)
def test_addresses_in_text_count_as_links(text, found):
    assert bool(has_link(text, None, None)) is found


def test_type_filter_uses_the_saved_chat_once_it_is_whole(home):
    photo = day(2, 1, media=types.MessageMediaPhoto(photo=types.PhotoEmpty(1)))
    tg = Telegram([day(1, 0), photo, day(3, 2)])
    run(tg)
    stats = run(tg, scope=Scope(kind="photos"))
    assert stats["local"] and stats["stored"] == 1
    txt = (home / "out" / "7_Club_photos" / "messages.txt").read_text(encoding="utf-8")
    assert txt.count("\n") == 1 and "#2 " in txt


def test_type_filter_asks_telegram_while_the_saved_rows_lack_details(home):
    tg = Telegram([day(1, 0), day(2, 1)])
    run(tg)
    db = sqlite3.connect(home / "7.sqlite")
    set_state(db, "lacks_details", 1)  # as a dump made before 1.3.0 is marked
    db.commit()
    db.close()
    stats = run(tg, scope=Scope(kind="photos"))
    assert not stats.get("local")
