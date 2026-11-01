"""Rich messages: Telegram's block layout (paragraphs, quotes, lists, tables) as readable text or Markdown.

Such a message has an empty `message`; its content is a tree of PageBlock* and RichText* objects in
`rich_message.blocks`, the same types Instant View pages are made of. Types are told apart by name, so the
module works with any Telethon layer; one it does not know shows up as <TypeName> instead of vanishing.
The Markdown follows Telethon's own: **bold**, __italic__, ~~strike~~, `code`, ```pre```, [text](url).
"""

from .util import iso

HEADINGS = {"PageBlockTitle": 1, "PageBlockHeader": 2, "PageBlockSubheader": 3}
HEADINGS.update({f"PageBlockHeading{n}": n for n in range(1, 7)})
PLAIN_BLOCKS = {"PageBlockParagraph", "PageBlockSubtitle", "PageBlockKicker", "PageBlockFooter"}
MD_MARKS = {"TextBold": "**", "TextItalic": "__", "TextStrike": "~~", "TextFixed": "`"}
# formatting that has no Markdown in Telethon: the text stays, the look is lost
PLAIN_MARKS = {
    "TextUnderline", "TextSpoiler", "TextMarked", "TextSubscript", "TextSuperscript", "TextMention",
    "TextHashtag", "TextCashtag", "TextBotCommand", "TextAutoUrl", "TextAutoEmail", "TextAutoPhone",
    "TextBankCard", "TextAnchor", "TextDate",
}  # fmt: skip
MEDIA_BLOCKS = {"PageBlockPhoto": "photo", "PageBlockVideo": "video", "PageBlockAudio": "audio"}


def name(obj):
    return type(obj).__name__


class Renderer:
    def __init__(self, md=False, documents=()):
        self.md = md
        self.documents = {d.id: d for d in documents or () if hasattr(d, "id")}

    # --- inline text

    def text(self, t):
        if t is None:
            return ""
        if isinstance(t, str):
            return t
        n = name(t)
        if n == "TextEmpty":
            return ""
        if n == "TextPlain":
            return t.text
        if n == "TextConcat":
            return "".join(self.text(x) for x in t.texts)
        if n in MD_MARKS:
            inner = self.text(t.text)
            core = inner.strip()
            if not self.md or not core:
                return inner
            # spaces stay outside the marks: "** bold**" is not bold in Markdown
            lead, tail = inner[: len(inner) - len(inner.lstrip())], inner[len(inner.rstrip()) :]
            return f"{lead}{MD_MARKS[n]}{core}{MD_MARKS[n]}{tail}"
        if n in PLAIN_MARKS:
            return self.text(t.text)
        if n == "TextUrl":
            return self.link(self.text(t.text), t.url)
        if n == "TextEmail":
            return self.link(self.text(t.text), f"mailto:{t.email}", t.email)
        if n == "TextPhone":
            label = self.text(t.text)
            digits = "".join(c for c in label if c.isdigit())
            return self.link(label, f"tel:{t.phone}", None if digits == t.phone.lstrip("+") else t.phone)
        if n == "TextMentionName":
            return self.link(self.text(t.text), f"tg://user?id={t.user_id}", shown=None)
        if n == "TextCustomEmoji":
            return t.alt or ""
        if n == "TextImage":
            return "[image]"
        if n == "TextMath":
            return f"`{t.source}`" if self.md else t.source
        if n == "TextDiff":
            old = self.text(t.old_text)
            return (f"~~{old}~~" if self.md and old else "") + self.text(t.text)
        if n == "TextButton":
            return self.button(t)
        return self.unknown(t)

    def link(self, label, url, shown=""):
        """A link: [label](url) in Markdown, `label (url)` in plain text unless the label already says it.

        shown is what the plain text adds in brackets ("" means the url itself, None means nothing).
        """
        if not url:
            return label
        if self.md:
            return f"[{label}]({url})"
        shown = url if shown == "" else shown
        if not shown or not label.strip() or same_url(label, shown):
            return label or shown or ""
        return f"{label} ({shown})"

    def button(self, b):
        label = self.text(b.text)
        url = getattr(getattr(b, "type", None), "url", None)
        if self.md:
            return f"[{label}]({url})" if url else f"[{label}]"
        return f"[{label}]" + (f" ({url})" if url else "")

    def unknown(self, obj):
        """A type this module does not know: its name, and whatever text it carries."""
        inner = getattr(obj, "text", None)
        inner = self.text(inner) if inner is not None else ""
        return f"<{name(obj)}>" + (f" {inner}" if inner else "")

    # --- blocks

    def blocks(self, blocks, sep="\n\n"):
        parts = (self.block(b) for b in blocks or ())
        return sep.join(p for p in parts if p)

    def caption(self, c):
        if c is None:
            return ""
        if name(c) != "PageCaption":
            return self.text(c)
        text, credit = self.text(c.text).strip(), self.text(c.credit).strip()
        return " — ".join(x for x in (text, credit) if x)

    def with_caption(self, head, c):
        cap = self.caption(c)
        return f"{head} {cap}".strip() if cap else head

    def block(self, b):
        n = name(b)
        if n in PLAIN_BLOCKS:
            return self.text(b.text)
        if n in HEADINGS:
            text = self.text(b.text)
            return f"{'#' * HEADINGS[n]} {text}" if self.md and text.strip() else text
        if n == "PageBlockDivider":
            return "---" if self.md else "———"
        if n == "PageBlockAnchor":
            return ""
        if n == "PageBlockPreformatted":
            text = self.text(b.text)
            return f"```{b.language or ''}\n{text}\n```" if self.md else text
        if n == "PageBlockMath":
            return f"```\n{b.source}\n```" if self.md else b.source
        if n in ("PageBlockBlockquote", "PageBlockPullquote"):
            return quote(self.with_caption(self.text(b.text), b.caption))
        if n == "PageBlockBlockquoteBlocks":
            body = self.blocks(b.blocks, "\n")
            cap = self.caption(b.caption)
            return quote(body + (f"\n— {cap}" if cap else ""))
        if n == "PageBlockThinking":
            return quote(f"Thinking: {self.text(b.text)}")
        if n == "PageBlockDetails":
            title = self.text(b.title)
            return f"▸ {title}" + (f"\n{self.blocks(b.blocks, chr(10))}" if b.blocks else "")
        if n == "PageBlockList":
            return "\n".join(self.item(i, "- " if self.md else "• ") for i in b.items)
        if n == "PageBlockOrderedList":
            return self.ordered(b)
        if n == "PageBlockTable":
            return self.table(b)
        if n in MEDIA_BLOCKS:
            return self.with_caption(self.media(MEDIA_BLOCKS[n], getattr(b, "url", None)), b.caption)
        if n == "PageBlockDocument":
            return self.with_caption(f"[file{self.file_name(b.document_id)}]", b.caption)
        if n in ("PageBlockCollage", "PageBlockSlideshow"):
            return self.with_caption(" ".join(self.block(i) for i in b.items), b.caption)
        if n == "PageBlockCover":
            return self.block(b.cover)
        if n == "PageBlockEmbed":
            return self.with_caption(self.link("[embed]", b.url) if b.url else "[embed]", b.caption)
        if n == "PageBlockEmbedPost":
            head = f"[post by {b.author}, {iso(b.date)}]" if b.author else "[post]"
            body = self.blocks(b.blocks, "\n")
            return self.with_caption(self.link(head, b.url), b.caption) + (f"\n{quote(body)}" if body else "")
        if n in ("PageBlockMap", "InputPageBlockMap"):
            geo = b.geo
            spot = f"{geo.lat},{geo.long}" if hasattr(geo, "lat") else ""
            return self.with_caption(f"[map {spot}]".replace(" ]", "]"), b.caption)
        if n == "PageBlockChannel":
            ch = b.channel
            title = getattr(ch, "title", None) or str(getattr(ch, "id", ""))
            user = getattr(ch, "username", None)
            return f"[channel {title}" + (f" @{user}" if user else "") + "]"
        if n == "PageBlockAuthorDate":
            author = self.text(b.author)
            return " · ".join(x for x in (author, iso(b.published_date) if b.published_date else "") if x)
        if n == "PageBlockRelatedArticles":
            lines = [self.text(b.title)] if b.title else []
            for a in b.articles:
                label = a.title or a.url
                lines.append(("- " if self.md else "• ") + self.link(label, a.url))
            return "\n".join(lines)
        if n == "PageBlockButtonRow":
            return " ".join(self.button(x) for x in b.buttons)
        return self.unknown(b)

    def media(self, kind, url=None):
        label = f"[{kind}]"
        return self.link(label, url) if url else label

    def file_name(self, document_id):
        doc = self.documents.get(document_id)
        for a in getattr(doc, "attributes", None) or ():
            if name(a) == "DocumentAttributeFilename":
                return f" {a.file_name}"
        return ""

    def item(self, i, marker):
        if getattr(i, "checkbox", False):
            marker += ("[x] " if i.checked else "[ ] ") if self.md else ("☑ " if i.checked else "☐ ")
        if hasattr(i, "blocks"):
            body = self.blocks(i.blocks, "\n")
        elif hasattr(i, "text"):
            body = self.text(i.text)
        else:
            body = self.unknown(i)
        return indent(body, marker)

    def ordered(self, b):
        start = b.start or 1
        count = len(b.items)
        out = []
        for k, i in enumerate(b.items):
            num = getattr(i, "num", None) or str(start + count - 1 - k if b.reversed else start + k)
            out.append(self.item(i, f"{num}. "))
        return "\n".join(out)

    def table(self, b):
        rows = [[self.text(c.text).replace("\n", " ").strip() for c in r.cells] for r in b.rows]
        rows = [r for r in rows if r]
        lines = [self.text(b.title)] if b.title and self.text(b.title) else []
        if not rows:
            return "\n".join(lines)
        if not self.md:
            return "\n".join(lines + [" | ".join(r) for r in rows])
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        cells = (c.replace("|", "\\|") for r in rows for c in r)
        rows = [[next(cells) for _ in range(width)] for _ in rows]
        table = [f"| {' | '.join(rows[0])} |", "|" + " --- |" * width] + [f"| {' | '.join(r)} |" for r in rows[1:]]
        return "\n".join(lines + table)


def same_url(label, url):
    strip = lambda s: s.strip().removeprefix("https://").removeprefix("http://").removeprefix("mailto:").rstrip("/")  # noqa: E731
    return strip(label) == strip(url)


def quote(text):
    return "\n".join(f"> {line}" if line else ">" for line in text.split("\n"))


def indent(text, marker):
    """A list item: the marker on its first line, the rest lined up under the text."""
    first, *rest = text.split("\n")
    pad = " " * len(marker)
    return "\n".join([marker + first] + [pad + line if line else "" for line in rest])


def rich_text(rich, md=False):
    """The whole rich message as one text: blocks separated by blank lines."""
    r = Renderer(md, getattr(rich, "documents", None))
    return r.blocks(rich.blocks).strip("\n")
