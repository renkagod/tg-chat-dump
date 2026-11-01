import datetime
import inspect
import json
import sqlite3

import pytest
from telethon.tl import types
from telethon.tl.alltlobjects import tlobjects
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    FactCheck,
    GeoPoint,
    InlineButtonTypeCallback,
    InlineButtonTypeDisabled,
    InlineButtonTypeUrl,
    KeyboardInlineButton,
    KeyboardInlineButtonRow,
    Message,
    MessageActionTopicEdit,
    MessageEntityMentionName,
    MessageEntityTextUrl,
    MessageFwdHeader,
    MessageMediaContact,
    MessageMediaDice,
    MessageMediaDocument,
    MessageMediaGeo,
    MessageMediaVenue,
    MessageMediaWebPage,
    MessageReplyHeader,
    MessageReplyStoryHeader,
    MessageService,
    PageBlockBlockquoteBlocks,
    PageBlockButtonRow,
    PageBlockDetails,
    PageBlockDivider,
    PageBlockHeading2,
    PageBlockList,
    PageBlockOrderedList,
    PageBlockParagraph,
    PageBlockPhoto,
    PageBlockPreformatted,
    PageBlockTable,
    PageBlockUnsupported,
    PageButton,
    PageCaption,
    PageListItemBlocks,
    PageListItemText,
    PageListOrderedItemText,
    PageTableCell,
    PageTableRow,
    PeerChannel,
    PeerUser,
    ReplyInlineMarkup,
    RichMessage,
    TextBold,
    TextConcat,
    TextCustomEmoji,
    TextEmpty,
    TextFixed,
    TextItalic,
    TextPlain,
    TextUrl,
    TextWithEntities,
    WebPage,
)

from tgdump.export import export, text_line
from tgdump.rich import Renderer, rich_text
from tgdump.store import COLUMN_NAMES, FORMAT, SCHEMA, open_db, outdated, plan_full, refetch, save, to_row

DATE = datetime.datetime(2026, 5, 13, 6, 38, 53, tzinfo=datetime.UTC)
PEER = PeerChannel(1)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO tasks(key, lo, cursor) VALUES('k', 0, 0)")
    yield conn
    conn.close()


def msg(id, text="", **kw):
    return Message(id=id, peer_id=PEER, date=DATE, message=text, **kw)


def field(row, name):
    return row[COLUMN_NAMES.index(name)]


def extra(row):
    return json.loads(field(row, "extra") or "{}")


def p(*texts):
    return PageBlockParagraph(text=TextConcat(texts=[TextPlain(t) if isinstance(t, str) else t for t in texts]))


# An invented release post, built like the announcements bots send as rich messages.
RELEASE = RichMessage(
    blocks=[
        p(TextCustomEmoji(document_id=1, alt="🤖"), " ", TextBold(TextPlain("Demo Bot v2.0"))),
        PageBlockBlockquoteBlocks(
            blocks=[
                p("• ", TextBold(TextPlain("Payments.")), " Fixed double charges."),
                p("• ", TextItalic(TextPlain("Trials")), " end on time."),
            ],
            caption=TextEmpty(),
        ),
        PageBlockList(
            items=[
                PageListItemText(text=TextPlain("first")),
                PageListItemText(text=TextPlain("done"), checkbox=True, checked=True),
                PageListItemBlocks(blocks=[p("two"), p("lines")]),
            ]
        ),
        PageBlockOrderedList(items=[PageListOrderedItemText(text=TextPlain(x)) for x in "ab"]),
        PageBlockDivider(),
        PageBlockHeading2(text=TextPlain("Changes")),
        PageBlockTable(
            title=TextEmpty(),
            rows=[
                PageTableRow(cells=[PageTableCell(text=TextPlain(x), header=True) for x in ("OS", "Get")]),
                PageTableRow(cells=[PageTableCell(text=t) for t in (TextPlain("Linux"), TextFixed(TextPlain("x64")))]),
            ],
        ),
        PageBlockDetails(
            blocks=[PageBlockPreformatted(text=TextPlain("print(1)"), language="python")], title=TextPlain("Log")
        ),
        PageBlockPhoto(photo_id=5, caption=PageCaption(text=TextPlain("screenshot"), credit=TextEmpty())),
        PageBlockButtonRow(buttons=[PageButton(text=TextPlain("Stop"), type=InlineButtonTypeDisabled())]),
        p(TextUrl(text=TextPlain("GitHub"), url="https://example.com/demo", webpage_id=0), " · ",
          TextUrl(text=TextPlain("example.com/x"), url="https://example.com/x", webpage_id=0)),
    ],
    photos=[],
    documents=[],
)  # fmt: skip


def test_rich_message_becomes_readable_text():
    text = rich_text(RELEASE)
    assert text.startswith("🤖 Demo Bot v2.0\n\n")
    assert "> • Payments. Fixed double charges.\n> • Trials end on time." in text
    assert "• first\n• ☑ done\n• two\n  lines" in text
    assert "1. a\n2. b" in text
    assert "OS | Get\nLinux | x64" in text
    assert "▸ Log\nprint(1)" in text
    assert "[photo] screenshot" in text
    assert "[Stop]" in text
    assert text.endswith("GitHub (https://example.com/demo) · example.com/x")  # a shown address is not repeated


def test_rich_message_as_markdown():
    md = rich_text(RELEASE, md=True)
    assert md.startswith("🤖 **Demo Bot v2.0**")
    assert "> • **Payments.** Fixed double charges.\n> • __Trials__ end on time." in md
    assert "- first\n- [x] done" in md
    assert "## Changes" in md
    assert "| OS | Get |\n| --- | --- |\n| Linux | `x64` |" in md
    assert "```python\nprint(1)\n```" in md
    assert "[GitHub](https://example.com/demo)" in md


def test_markdown_keeps_spaces_outside_the_marks():
    md = Renderer(md=True).text(TextConcat(texts=[TextPlain("a"), TextBold(TextPlain(" b ")), TextPlain("c")]))
    assert md == "a **b** c"


def sample(cls):
    """An instance of a PageBlock/RichText type with every argument filled in by its name."""
    text = TextPlain("x")
    by_name = {
        "text": text, "title": text, "author": text, "old_text": text, "credit": text,
        "caption": PageCaption(text=text, credit=TextEmpty()), "cover": p("x"),
        "url": "https://example.com", "email": "a@example.com", "phone": "100", "source": "x", "language": "",
        "name": "x", "alt": "🙂", "html": "", "geo": GeoPoint(long=1.5, lat=2.5, access_hash=0),
        "channel": types.ChatEmpty(id=5), "date": DATE, "published_date": DATE, "type": InlineButtonTypeUrl("https://e.x"),
        "texts": [text], "blocks": [p("x")], "items": [PageListItemText(text=text)],
        "rows": [PageTableRow(cells=[PageTableCell(text=text)])], "articles": [], "buttons": [],
    }  # fmt: skip
    if cls is TextPlain:
        by_name["text"] = "x"
    if cls.__name__ in ("PageBlockCollage", "PageBlockSlideshow"):
        by_name["items"] = [p("x")]
    kw = {}
    for arg, param in inspect.signature(cls.__init__).parameters.items():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if arg in by_name:
            kw[arg] = by_name[arg]
        elif param.default is inspect.Parameter.empty and arg != "self":
            kw[arg] = 1  # ids, sizes, zoom
    return cls(**kw)


def schema_types(base):
    return [c for c in tlobjects.values() if c.SUBCLASS_OF_ID == base.SUBCLASS_OF_ID]


@pytest.mark.parametrize("cls", schema_types(PageBlockParagraph) + schema_types(TextPlain), ids=lambda c: c.__name__)
def test_every_block_and_text_type_of_the_schema_is_rendered(cls):
    obj = sample(cls)
    r = Renderer()
    out = r.block(obj) if cls.__name__.startswith(("PageBlock", "InputPageBlock")) else r.text(obj)
    if cls is PageBlockUnsupported:
        assert out == "<PageBlockUnsupported>"
    else:
        assert not out.startswith("<"), out
    Renderer(md=True).block(obj) if cls.__name__.startswith(("PageBlock", "InputPageBlock")) else None


def test_unknown_types_leave_a_visible_trace():
    class PageBlockHologram:
        text = TextPlain("beam")

    class TextGlitter:
        pass

    r = Renderer()
    assert r.block(PageBlockHologram()) == "<PageBlockHologram> beam"
    assert r.text(TextGlitter()) == "<TextGlitter>"


def test_rich_message_row_has_text_and_markdown():
    m = msg(1, rich_message=RELEASE)
    row = to_row(m, forum=False)
    assert field(row, "text").startswith("🤖 Demo Bot v2.0")
    assert field(row, "text_md") is None
    assert extra(row)["rich"] is True
    assert field(to_row(m, forum=False, options={"markdown"}), "text_md").startswith("🤖 **Demo Bot v2.0**")


# --- what else a person sees


def doc_media(**kw):
    doc = types.Document(
        id=1, access_hash=0, file_reference=b"", date=DATE, mime_type="audio/mpeg", size=3_500_000, dc_id=1,
        attributes=[DocumentAttributeFilename("song.mp3"),
                    DocumentAttributeAudio(duration=200, title="Song", performer="Band")],
    )  # fmt: skip
    return MessageMediaDocument(document=doc, **kw)


def test_media_details_are_kept():
    page = WebPage(id=1, url="https://example.com/a", display_url="example.com/a", hash=0, site_name="Example",
                   title="A page", description="What it is about", attributes=[])  # fmt: skip
    cases = {
        "file": (doc_media(), {"name": "song.mp3", "mime": "audio/mpeg", "size": 3_500_000, "duration": 200,
                               "title": "Song", "performer": "Band", "kind": "audio"}),
        "webpage": (MessageMediaWebPage(webpage=page), {"url": "https://example.com/a", "site": "Example",
                                                         "title": "A page", "description": "What it is about"}),
        "geo": (MessageMediaGeo(geo=GeoPoint(long=37.6, lat=55.7, access_hash=0)), {"lat": 55.7, "long": 37.6}),
        "venue": (MessageMediaVenue(geo=GeoPoint(long=1.0, lat=2.0, access_hash=0), title="Cafe", address="Main st 1",
                                    provider="foursquare", venue_id="v1", venue_type=""),
                  {"lat": 2.0, "long": 1.0, "title": "Cafe", "address": "Main st 1", "provider": "foursquare",
                   "venue_id": "v1"}),
        "contact": (MessageMediaContact(phone_number="+10000000000", first_name="Ann", last_name="", vcard="",
                                        user_id=0),
                    {"phone": "+10000000000", "first_name": "Ann"}),
        "dice": (MessageMediaDice(value=6, emoticon="🎲"), {"emoticon": "🎲", "value": 6}),
    }  # fmt: skip
    for key, (media, expected) in cases.items():
        assert extra(to_row(msg(2, media=media), forum=False))[key] == expected, key


def test_sender_reply_forward_and_buttons_are_kept():
    markup = ReplyInlineMarkup(
        rows=[
            KeyboardInlineButtonRow(
                buttons=[
                    KeyboardInlineButton(text="Download", type=InlineButtonTypeUrl(url="https://example.com/get")),
                    KeyboardInlineButton(text="Vote", type=InlineButtonTypeCallback(data=b"\x01")),
                ]
            )
        ]
    )
    m = msg(
        3,
        "see link",
        from_id=PeerUser(42),
        from_rank="moderator",
        post_author="Ann",
        reply_to=MessageReplyHeader(reply_to_msg_id=7, reply_to_peer_id=PeerChannel(99), quote_text="the quoted bit"),
        fwd_from=MessageFwdHeader(date=DATE, from_id=PeerChannel(55), post_author="Bob"),
        reply_markup=markup,
        entities=[MessageEntityTextUrl(0, 3, url="https://example.com/hidden"), MessageEntityMentionName(4, 4, 42)],
        factcheck=FactCheck(hash=0, country="NL", text=TextWithEntities(text="Not quite.", entities=[])),
    )
    row = to_row(m, forum=False)
    ex = extra(row)
    assert ex["rank"] == "moderator"
    assert ex["reply"] == {"quote": "the quoted bit", "peer": -1000000000099}
    assert field(row, "fwd_from") == "-1000000000055"  # without the source chat at hand its id stands in for the name
    assert ex["fwd"] == {"from_id": -1000000000055, "date": DATE.isoformat(), "author": "Bob"}
    assert ex["buttons"] == [[{"text": "Download", "url": "https://example.com/get"}, {"text": "Vote"}]]
    assert ex["links"] == [
        {"text": "see", "url": "https://example.com/hidden"},
        {"text": "link", "url": "tg://user?id=42"},
    ]
    assert ex["factcheck"] == {"text": "Not quite.", "country": "NL"}
    line = text_line(dict(zip(COLUMN_NAMES, row, strict=True)) | {"extra": ex, "thread": None})
    assert "[moderator]" in line and "(reply to #7 in -1000000000099)" in line and "«the quoted bit»" in line
    assert "[buttons: Download <https://example.com/get> | Vote]" in line
    assert "[links: see <https://example.com/hidden> | link <tg://user?id=42>]" in line


def test_story_replies_and_action_fields_are_kept():
    m = msg(4, reply_to=MessageReplyStoryHeader(peer=PeerUser(5), story_id=9))
    assert extra(to_row(m, forum=False))["reply"] == {"story": {"peer": 5, "id": 9}}
    s = MessageService(id=5, peer_id=PEER, date=DATE, action=MessageActionTopicEdit(title="Renamed", closed=True))
    row = to_row(s, forum=False)
    assert extra(row)["action"] == {"title": "Renamed", "closed": True}
    line = text_line(dict(zip(COLUMN_NAMES, row, strict=True)) | {"extra": extra(row)})
    assert "<MessageActionTopicEdit: title=Renamed, closed=True>" in line


def test_export_names_the_file_and_the_link_preview(db, tmp_path):
    page = WebPage(id=1, url="https://example.com", display_url="example.com", hash=0, site_name="Example",
                   title="Title", attributes=[])  # fmt: skip
    save(db, "k", [to_row(msg(1, "listen", media=doc_media()), False)])
    save(db, "k", [to_row(msg(2, media=MessageMediaWebPage(webpage=page)), False)])
    txt = (export(db, 5, "Chan", out_root=tmp_path) / "messages.txt").read_text(encoding="utf-8")
    assert "<MessageMediaDocument: song.mp3, Band – Song, 200 s, 3.3 MB> listen" in txt
    assert "<MessageMediaWebPage: Example · Title · https://example.com>" in txt


# --- saving again


def test_saving_again_updates_rows_but_keeps_what_this_run_did_not_collect(db):
    first = msg(1, "same", views=10, entities=[types.MessageEntityBold(0, 4)])
    save(db, "k", [to_row(first, False, {"meta", "markdown"})])
    save(db, "k", [to_row(msg(1, "same", edit_date=DATE), False)])  # a run without the extras
    assert db.execute("SELECT text_md, views, edit_date FROM messages").fetchone() == ("**same**", 10, DATE.isoformat())
    save(db, "k", [to_row(msg(1, "edited"), False)])
    assert db.execute("SELECT text, text_md, views FROM messages").fetchone() == ("edited", None, 10)


def test_refetch_resets_the_ranges_and_the_export(tmp_path):
    db = open_db(tmp_path / "old.sqlite")
    plan_full(db, 1, 12_000)
    db.execute("UPDATE tasks SET done=1, cursor=lo")
    save(db, "range:0", [to_row(msg(1, "x"), False)])
    db.execute("INSERT INTO exports VALUES('out', 1, 1, '[]')")
    db.execute("PRAGMA user_version=1")  # written by an older version
    assert outdated(db)
    refetch(db)
    assert db.execute("SELECT COUNT(*) FROM tasks WHERE done=0 AND cursor=hi + 1").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM exports").fetchone()[0] == 0
    assert db.execute("PRAGMA user_version").fetchone()[0] == FORMAT and not outdated(db)
    assert db.execute("SELECT text FROM messages").fetchone() == ("x",)  # nothing saved is dropped


def test_new_database_is_up_to_date(tmp_path):
    assert not outdated(open_db(tmp_path / "new.sqlite"))
