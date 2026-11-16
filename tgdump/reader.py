"""Offline HTML reader next to an export: index.html plus the topics' data, loaded when a topic is opened."""

import base64
import html
import json
import os
import re
from pathlib import Path

from .store import get_chat, saved_files
from .util import topic_folder

DATA_DIR = ".reader"
PART_ROWS = 10_000  # rows per data file, counted from the start of a list, so appending keeps earlier parts as is
CHUNK_ROWS = 2000  # rows per TGR.a(...) statement; smaller statements are parsed faster than one huge array
ALBUM_MASK = (1 << 53) - 1  # album ids are compared in JS, where integers are exact up to 2**53

_encode = json.JSONEncoder(ensure_ascii=False, separators=(",", ":"), check_circular=False).encode
# safe inside <script> and in any JS parser: no tag, entity or line-separator characters stay raw
_ESCAPES = (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029"))

# a row is [id, unix date, sender index in its part, text, reply to, forwarded from, media, action, album,
#           edited, reactions, views, replies, details, thread head]; trailing nulls are dropped
_SELECT = (
    "SELECT id, CAST(strftime('%s', date) AS INTEGER), sender_id, sender, COALESCE(NULLIF(text_md, ''), text),"
    " reply_to, reply_top, topic_id, fwd_from, media, action, grouped_id, edit_date IS NOT NULL,"
    " reactions, views, replies, extra, NULLIF(text_md, '') IS NOT NULL,"
    " CAST(strftime('%s', deleted_at) AS INTEGER) FROM messages"
)
# earlier texts of edited messages as [unix time of the edit that replaced it, text], the oldest first
_EDITS = (
    "SELECT e.id, CAST(strftime('%s', e.replaced) AS INTEGER), COALESCE(NULLIF(e.text_md, ''), e.text)"
    " FROM edits e JOIN messages m ON m.id = e.id"
)
# details the page does not show; "links" is left out too when the Markdown text already has them
_UNSHOWN = {"channel_post", "rich", "boosts", "effect"}
# what the page tells about the chat itself, from the chat table
_CHAT_SHOWN = (
    "kind", "about", "members", "admins", "username", "link", "invite", "created", "joined", "linked_chat",
    "linked_title", "slow_mode", "auto_delete", "members_cannot", "protected", "join_request", "signatures",
    "hidden_members", "hidden_history", "verified", "scam", "fake", "location", "phone", "updated",
)  # fmt: skip
_TOPICS = (
    "SELECT t.id, t.icon, t.color, t.closed, t.pinned, t.hidden, CAST(strftime('%s', t.date) AS INTEGER), u.name"
    " FROM topics t LEFT JOIN users u ON u.id = t.creator_id"
)


def chat_meta(db):
    """The chat table as the page takes it: the facts it shows, the picture as a data: address."""
    info = get_chat(db)
    chat = {k: info[k] for k in _CHAT_SHOWN if k in info}
    if info.get("photo"):
        chat["photo"] = "data:image/jpeg;base64," + base64.b64encode(info["photo"]).decode()
    return chat


def topic_meta(db):
    """{topic id: what the list shows of it besides the title}: icon, colour, closed, pinned, made by and when."""
    found = {}
    for tid, icon, color, closed, pinned, hidden, date, by in db.execute(_TOPICS):
        about = {"i": icon, "c": color, "x": closed, "p": pinned, "h": hidden, "at": date, "by": by}
        found[tid] = {k: v for k, v in about.items() if v}
    return found


def js_literal(obj):
    """JSON that can be embedded in a page or script file without breaking out of it."""
    s = _encode(obj)
    for a, b in _ESCAPES:
        s = s.replace(a, b)
    return s


def _replace(path, write):
    """Write a file under a temporary name and move it into place, so an open page never sees half of it."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", errors="replace", newline="\n") as f:
        write(f)
    os.replace(tmp, path)


def part_path(data, key, n):
    return data / f"{key}.{n}.js"


class _Part:
    """One data file being written: rows go out in chunks, its own sender table goes last."""

    def __init__(self, data, key, n):
        self.path, self.k, self.n = part_path(data, key, n), js_literal(key), n
        self.tmp = self.path.with_name(self.path.name + ".tmp")
        self.f = open(self.tmp, "w", encoding="utf-8", errors="replace", newline="\n")
        self.senders, self.chunk, self.rows = {}, [], 0

    def flush(self):
        if self.chunk:
            self.f.write(f"TGR.a({self.k},{self.n},{js_literal(self.chunk)});\n")
            self.chunk = []

    def close(self):
        self.flush()
        senders = js_literal([[sid, name] for sid, name in self.senders])
        self.f.write(f"TGR.s({self.k},{self.n},{senders});\nTGR.e({self.k},{self.n});\n")
        self.f.close()
        os.replace(self.tmp, self.path)


def _write_list(db, data, key, topic, threads, first=0, files=None):
    """Stream one list into its parts, starting with part `first` (earlier parts are kept).

    files: {message id: its downloaded file}, shown in the message.
    """
    if threads:
        sql, params = _SELECT + " ORDER BY COALESCE(reply_top, reply_to, id), id", ()
    elif first:
        # the part boundary is found in the (topic_id, id) index, without reading the rows before it
        start = db.execute(
            "SELECT id FROM messages WHERE topic_id IS ? ORDER BY id LIMIT 1 OFFSET ?", (topic, first * PART_ROWS)
        ).fetchone()
        if start is None:
            return
        sql, params = _SELECT + " WHERE topic_id IS ? AND id >= ? ORDER BY id", (topic, start[0])
    else:
        sql, params = _SELECT + " WHERE topic_id IS ? ORDER BY id", (topic,)
    edits = {}
    where, args = ("", ()) if threads else (" WHERE m.topic_id IS ?", (topic,))
    for mid, when, text in db.execute(_EDITS + where + " ORDER BY e.id, e.date", args):
        edits.setdefault(mid, []).append([when or 0, text or ""])
    part, n, current = None, first, None
    try:
        for r in db.execute(sql, params):
            mid, date, sid, sname, text, reply, top, tid, fwd, media, action, grouped, edited, *meta = r
            reactions, views, replies, extra, md, deleted = meta
            if part is None or part.rows == PART_ROWS:
                if part:
                    part.close()
                    n += 1
                part = _Part(data, key, n)
            s = part.senders.setdefault((sid, sname), len(part.senders))
            ex = json.loads(extra) if extra else None
            thread = head = None
            if threads:
                thread = top or reply or mid
                if thread != current:
                    current = thread
                    post = ex.get("channel_post") if ex and thread == mid else None
                    head = [1, post] if post else [0, thread]
            if ex:
                ex = {k: v for k, v in ex.items() if k not in _UNSHOWN and not (md and k == "links")} or None
            if files and mid in files:
                ex = ex or {}
                ex["file"] = {**files[mid], **ex.get("file", {})}
            if deleted or mid in edits:
                ex = ex or {}
                if deleted:
                    ex["deleted"] = deleted
                if mid in edits:
                    ex["edits"] = edits[mid]
            row = [
                mid,
                date or 0,
                s,
                text or None,
                None if reply in (tid, thread) else reply,
                fwd,
                media and media.removeprefix("MessageMedia"),
                action and action.removeprefix("MessageAction"),
                None if grouped is None else grouped & ALBUM_MASK,
                edited or None,
                json.loads(reactions) if reactions else None,
                views or None,
                replies or None,
                ex,
                head,
            ]
            while row[-1] is None:
                row.pop()
            part.chunk.append(row)
            part.rows += 1
            if len(part.chunk) >= CHUNK_ROWS:
                part.flush()
        if part:
            part.close()
    finally:
        if part and not part.f.closed:
            part.f.close()  # an error midway: the .tmp is left behind and removed by the next run


def write_reader(db, out, chat_title, titles, topics=None, threads=False, since=None, changed=None):
    """Write out/index.html and out/.reader/<topic or "all">.<part>.js.

    titles: {topic_id: title}; topics: ids of the topics whose messages changed, None rewrites every data file
    (index.html is always rewritten, missing parts are always written). since: the last message id of the
    previous export; with it only the parts of changed topics that hold newer messages are rewritten.
    changed: {topic_id: its oldest message edited or deleted since then}, rewritten from that message's part on.
    threads=True puts everything into one list ordered by thread, like export() does for a channel's
    discussion group; such a list is always rewritten whole. Returns the path of index.html.
    """
    out = Path(out)
    data = out / DATA_DIR
    data.mkdir(parents=True, exist_ok=True)
    # counts and newest ids come from the (topic_id, id) index; only the newest message of each topic is read
    if threads:
        groups = [(None, *db.execute("SELECT COUNT(*), MAX(id) FROM messages").fetchone())]
    else:
        groups = db.execute("SELECT topic_id, COUNT(*), MAX(id) FROM messages GROUP BY topic_id").fetchall()
    groups = [g for g in groups if g[1]]
    forum = not threads and any(t is not None for t, *_ in groups)
    about, files = topic_meta(db) if forum else {}, saved_files(db)
    entries, keep = [], set()
    for t, count, newest in groups:
        key = "all" if t is None else str(t)
        parts = -(-count // PART_ROWS)
        paths = [part_path(data, key, n) for n in range(parts)]
        first = parts  # nothing to write
        if topics is None or (t in topics if forum else bool(topics)):
            first = 0
            if since is not None and not threads:
                upto = min(since, changed[t] - 1) if changed and t in changed else since
                before = db.execute("SELECT COUNT(*) FROM messages WHERE topic_id IS ? AND id <= ?", (t, upto))
                first = before.fetchone()[0] // PART_ROWS
        first = min([first] + [n for n, p in enumerate(paths) if not p.exists()])
        if threads and first < parts:
            first = 0  # ordered by thread, not id: a new message can land in any part
        if first < parts:
            _write_list(db, data, key, t, threads, first, files)
        if not forum:
            title = chat_title
        elif t is None:
            title = "Without a topic"
        else:
            title = titles.get(t) or ("General" if t == 1 else f"Topic {t}")
        last = db.execute("SELECT CAST(strftime('%s', date) AS INTEGER) FROM messages WHERE id = ?", (newest,))
        stamps = [p.stat().st_mtime_ns // 1_000_000 for p in paths]
        # the folder the topic is exported to: the files of its messages are in it
        folder = "" if t is None or threads else topic_folder(out, t, titles).name
        entries.append([key, t, title, count, last.fetchone()[0] or 0, stamps, about.get(t) or None, folder])
        keep.update(p.name for p in paths)
    for f in data.iterdir():
        if f.name not in keep:
            f.unlink()  # parts past the end, topics that no longer exist, older layouts, writes cut short
    meta = {"title": chat_title, "forum": forum, "threads": threads, "topics": entries, "chat": chat_meta(db)}
    page_parts = {"TITLE": html.escape(chat_title or "Chat"), "CSS": _CSS, "JS": _JS, "META": js_literal(meta)}
    page = re.sub(r"__(TITLE|CSS|JS|META)__", lambda m: page_parts[m[1]], _HTML)
    index = out / "index.html"
    _replace(index, lambda f: f.write(page))
    return index


_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'self' 'unsafe-inline' file:; \
style-src 'unsafe-inline'; img-src data: 'self' file:; media-src 'self' file:; base-uri 'none'; \
form-action 'none'">
<meta name="color-scheme" content="light dark">
<meta name="referrer" content="no-referrer">
<link rel="icon" href="data:,">
<title>__TITLE__</title>
<style>__CSS__</style>
</head>
<body>
<div id="app">
<aside id="side">
<div class="side-head">
<div class="who">
<button id="chatava" class="cava" type="button" aria-label="About this chat" tabindex="-1"></button>
<div class="whot"><h1><button id="chat" class="plain" type="button" title="About this chat"></button></h1>
<p id="chatsub" class="sub"></p></div>
</div>
<label class="search filter" id="tfilterbox" hidden><svg viewBox="0 0 24 24" aria-hidden="true">\
<circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/></svg>\
<input id="tfilter" type="search" placeholder="Filter topics" autocomplete="off" spellcheck="false"></label>
</div>
<nav id="topics" aria-label="Topics"></nav>
</aside>
<main id="main">
<div id="scroller" tabindex="-1"><div id="list" role="log" aria-live="off"></div></div>
<header id="bar">
<button id="back" class="icon" type="button" aria-label="All topics"><svg viewBox="0 0 24 24" aria-hidden="true">\
<path d="m15 5-7 7 7 7"/></svg></button>
<div class="ttl"><h2 id="title"></h2><p id="sub" class="sub"></p></div>
<div class="tools" id="tools">
<label class="search"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/>\
<path d="m20 20-4.2-4.2"/></svg><input id="q" type="search" placeholder="Search" autocomplete="off" \
spellcheck="false" aria-label="Search in this topic"><span id="count" aria-live="polite"></span></label>
<button id="older" class="icon" type="button" aria-label="Older match" title="Older match (Enter)" disabled>\
<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 15 6-6 6 6"/></svg></button>
<button id="newer" class="icon" type="button" aria-label="Newer match" title="Newer match (Shift+Enter)" disabled>\
<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button>
<button id="pinbtn" class="icon" type="button" aria-label="Pinned messages" title="Pinned messages" \
aria-expanded="false" aria-controls="pins" hidden><svg viewBox="0 0 24 24" aria-hidden="true">\
<path d="M9 4h6l-.8 5.8 3.3 3.4V15h-11v-1.8l3.3-3.4z"/><path d="M12 15v5.5"/></svg><span id="pincount"></span></button>
<button id="jumpbtn" class="icon" type="button" aria-label="Go to a date or message" title="Go to a date or message" \
aria-expanded="false" aria-controls="jump"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="5" width="16" \
height="15" rx="2.5"/><path d="M4 10h16M8.5 3v4M15.5 3v4"/></svg></button>
<button id="infobtn" class="icon" type="button" aria-label="About this chat" title="About this chat" hidden>\
<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/>\
<path d="M12 7.6v.2"/></svg></button>
</div>
</header>
<div id="pill" class="material" aria-hidden="true"></div>
<div id="scrub" aria-hidden="true"><div id="thumb"></div><div id="bubble" class="material"></div></div>
<button id="down" class="material" type="button" aria-label="Newest messages"><svg viewBox="0 0 24 24" \
aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg></button>
<div id="state"></div>
<div id="jump" class="pop material" role="dialog" aria-label="Go to">
<label class="field">Date<input id="jdate" type="date"></label>
<label class="field">Message ID<span class="inrow"><input id="jmsg" type="text" inputmode="numeric" \
placeholder="e.g. 4512" autocomplete="off"><button id="jgo" class="btn" type="button">Go</button></span></label>
<div class="inrow"><button id="joldest" class="btn ghost" type="button">Oldest</button>\
<button id="jnewest" class="btn ghost" type="button">Newest</button></div>
<p id="jerr" class="err" aria-live="polite"></p>
</div>
<div id="pins" class="pop material" role="dialog" aria-label="Pinned messages">
<h3>Pinned messages</h3>
<div id="pinlist"></div>
</div>
</main>
</div>
<dialog id="about" aria-label="About this chat">
<button id="abclose" class="icon abx" type="button" aria-label="Close"><svg viewBox="0 0 24 24" aria-hidden="true">\
<path d="m7 7 10 10M17 7 7 17"/></svg></button>
<div id="abbody" class="abb"></div>
</dialog>
<div id="toast" class="material" role="status"></div>
<script>const META = __META__;</script>
<script>__JS__</script>
</body>
</html>
"""

_CSS = """
:root {
  --bg: #fff; --side: #f5f5f7; --text: #1d1d1f; --text2: #6e6e73; --text3: #8e8e93;
  --sep: rgba(60, 60, 67, .13); --fill: rgba(120, 120, 128, .12); --fill2: rgba(120, 120, 128, .07);
  --accent: #0071e3; --on-accent: #fff; --accent-fill: rgba(0, 113, 227, .13);
  --mark: rgba(255, 204, 0, .5); --mat: rgba(255, 255, 255, .72); --mat-solid: #fff;
  --shadow: 0 10px 30px rgba(0, 0, 0, .12), 0 1px 3px rgba(0, 0, 0, .08); --thumb: rgba(0, 0, 0, .32);
  --c0: #d70015; --c1: #c93400; --c2: #8944ab; --c3: #248a3d; --c4: #0071a4; --c5: #0040dd; --c6: #d30f45;
  --a0: #ff3b30; --a1: #ff9500; --a2: #af52de; --a3: #34c759; --a4: #30b0c7; --a5: #007aff; --a6: #ff2d55;
  --ease: cubic-bezier(.32, .72, 0, 1); --bar-h: 56px;
  color-scheme: light dark;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #161618; --side: #1f1f21; --text: #f5f5f7; --text2: #a1a1a6; --text3: #7c7c80;
    --sep: rgba(84, 84, 88, .55); --fill: rgba(120, 120, 128, .24); --fill2: rgba(120, 120, 128, .13);
    --accent: #0a84ff; --accent-fill: rgba(10, 132, 255, .22); --mark: rgba(255, 214, 10, .42);
    --mat: rgba(30, 30, 32, .72); --mat-solid: #232325; --thumb: rgba(255, 255, 255, .35);
    --shadow: 0 10px 30px rgba(0, 0, 0, .5), 0 0 0 1px rgba(255, 255, 255, .06);
    --c0: #ff6961; --c1: #ffb340; --c2: #da8fff; --c3: #30db5b; --c4: #70d7ff; --c5: #409cff; --c6: #ff6482;
    --a0: #e5453b; --a1: #e08a0b; --a2: #a95ad0; --a3: #2ea94d; --a4: #2f9fb4; --a5: #2a7fe8; --a6: #e33d5e;
  }
}
@media (prefers-reduced-transparency: reduce) { :root { --mat: var(--mat-solid); } }
@media (prefers-contrast: more) {
  :root { --sep: rgba(60, 60, 67, .4); --text2: #48484a; --text3: #5c5c60; --mat: var(--mat-solid); }
}
* { box-sizing: border-box; }
html, body { height: 100%; margin: 0; overflow: hidden; }
body {
  background: var(--bg); color: var(--text);
  font: 15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI Variable Text", "Segoe UI", system-ui, Roboto,
    "Helvetica Neue", Arial, sans-serif, "Apple Color Emoji", "Segoe UI Emoji";
  -webkit-font-smoothing: antialiased; -webkit-text-size-adjust: 100%;
}
button, input { font: inherit; color: inherit; }
button { cursor: pointer; -webkit-tap-highlight-color: transparent; }
svg { width: 20px; height: 20px; fill: none; stroke: currentColor; stroke-width: 1.8;
  stroke-linecap: round; stroke-linejoin: round; flex: none; }
[hidden] { display: none !important; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
input:focus-visible, #scroller:focus-visible { outline: 0; }
.sub { margin: 0; font-size: 12.5px; color: var(--text2); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; font-variant-numeric: tabular-nums; }
.material { background: var(--mat); -webkit-backdrop-filter: saturate(180%) blur(20px);
  backdrop-filter: saturate(180%) blur(20px); }

#app { display: grid; grid-template-columns: minmax(260px, 320px) 1fr; height: 100vh; height: 100dvh; }
#app.single { grid-template-columns: 1fr; }
#app.single #side, #app.single #back { display: none; }

#side { background: var(--side); border-right: 1px solid var(--sep); display: flex; flex-direction: column;
  min-height: 0; }
.side-head { padding: 22px 16px 10px; padding-top: max(22px, env(safe-area-inset-top)); }
.side-head h1 { margin: 0 0 2px; font-size: 22px; line-height: 1.15; font-weight: 700; letter-spacing: -.022em;
  overflow-wrap: anywhere; }
.side-head .filter { margin-top: 12px; width: 100%; }
.side-head .sub { white-space: normal; }
.who { display: flex; align-items: center; gap: 12px; }
.whot { min-width: 0; }
.cava { flex: none; width: 46px; height: 46px; padding: 0; border: 0; border-radius: 50%; display: grid;
  place-items: center; color: #fff; font-size: 18px; font-weight: 600; background: var(--a5) center / cover;
  user-select: none; }
button.cava:active { transform: scale(.95); }
.plain { padding: 0; border: 0; background: none; font: inherit; color: inherit; letter-spacing: inherit;
  text-align: left; overflow-wrap: anywhere; }
.plain:hover { color: var(--accent); }
#topics { flex: 1; overflow-y: auto; padding: 4px 8px 16px; padding-bottom: max(16px, env(safe-area-inset-bottom)); }
.topic { display: grid; grid-template-columns: 40px 1fr auto; grid-template-rows: auto auto; column-gap: 12px;
  align-items: center; width: 100%; padding: 8px 10px; border: 0; border-radius: 10px; background: none;
  text-align: left; transition: transform .1s ease-out; }
.topic:hover { background: var(--fill2); }
.topic:active { transform: scale(.985); background: var(--fill); }
.topic[aria-current="true"] { background: var(--accent); color: var(--on-accent); }
.topic[aria-current="true"] .tc, .topic[aria-current="true"] .td { color: rgba(255, 255, 255, .82); }
.tav { grid-row: 1 / 3; width: 40px; height: 40px; border-radius: 50%; display: grid; place-items: center;
  color: #fff; font-weight: 600; font-size: 16px; }
.tn { font-weight: 600; font-size: 14.5px; letter-spacing: -.005em; white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; }
.tav.em { background: var(--fill); font-size: 21px; font-weight: 400; }
.topic[aria-current="true"] .tav.em { background: rgba(255, 255, 255, .22); }
.td { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text3);
  font-variant-numeric: tabular-nums; white-space: nowrap; }
.td svg { width: 13px; height: 13px; }
.tc { grid-column: 2 / 4; font-size: 13px; color: var(--text2); font-variant-numeric: tabular-nums; }
.nomatch { padding: 16px 10px; color: var(--text2); font-size: 14px; }

#main { position: relative; min-width: 0; min-height: 0; overflow: hidden; background: var(--bg); }
#bar { position: absolute; z-index: 5; top: 0; left: 0; right: 0; display: flex; flex-wrap: wrap; align-items: center;
  gap: 8px 12px; padding: 10px 16px; padding-top: max(10px, env(safe-area-inset-top));
  background: var(--mat); -webkit-backdrop-filter: saturate(180%) blur(20px);
  backdrop-filter: saturate(180%) blur(20px); box-shadow: 0 1px 0 var(--sep); }
#back { display: none; margin-left: -8px; }
.ttl { flex: 1; min-width: 0; }
.ttl h2 { margin: 0; font-size: 17px; line-height: 1.25; font-weight: 600; letter-spacing: -.012em;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tools { display: flex; align-items: center; gap: 2px; }
.search { display: flex; align-items: center; gap: 6px; height: 34px; width: 280px; padding: 0 10px 0 8px;
  border-radius: 10px; background: var(--fill); color: var(--text3); transition: box-shadow .15s ease-out; }
.search:focus-within { box-shadow: 0 0 0 3px var(--accent-fill); }
.search svg { width: 17px; height: 17px; }
.search input { flex: 1; min-width: 0; height: 100%; border: 0; outline: 0; background: none; color: var(--text);
  font-size: 14px; -webkit-appearance: none; appearance: none; }
.search input::-webkit-search-cancel-button { display: none; }
.search input::placeholder { color: var(--text3); }
#count { font-size: 12px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.icon { width: 34px; height: 34px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 9px;
  background: none; color: var(--accent); transition: transform .1s ease-out, opacity .15s; }
.icon:hover { background: var(--fill2); }
.icon:active { transform: scale(.92); background: var(--fill); }
.icon:disabled { opacity: .3; pointer-events: none; }
.icon[aria-expanded="true"] { background: var(--accent-fill); }
#pinbtn { width: auto; min-width: 34px; padding: 0 7px; grid-auto-flow: column; gap: 3px; font-size: 12.5px;
  font-weight: 600; font-variant-numeric: tabular-nums; }
#pincount:empty { display: none; }

#scroller { position: absolute; inset: 0; overflow-y: auto; overflow-x: hidden; overflow-anchor: none;
  overscroll-behavior: contain; scrollbar-width: none; outline: 0; }
#scroller::-webkit-scrollbar { display: none; }
#list { position: relative; display: flex; flex-direction: column; min-height: 100%; max-width: 860px;
  margin: 0 auto; padding: calc(var(--bar-h) + 6px) 0 28px; padding-bottom: max(28px, env(safe-area-inset-bottom)); }
#list::before { content: ""; flex: 1 0 auto; }
.row { padding: 0 24px 0 20px; }
.sep { display: flex; justify-content: center; margin: 18px 0 6px; pointer-events: none; }
.sep span, .svc > span { font-size: 12.5px; font-weight: 600; color: var(--text2); background: var(--fill2);
  padding: 3px 11px; border-radius: 999px; }
.thr { display: flex; align-items: center; gap: 12px; margin: 30px 0 4px; color: var(--text3); font-size: 11.5px;
  font-weight: 600; letter-spacing: .05em; text-transform: uppercase; }
.thr::before, .thr::after { content: ""; flex: 1; height: 1px; background: var(--sep); }
.msg { position: relative; display: grid; grid-template-columns: 36px minmax(0, 1fr); column-gap: 12px;
  margin: 0 -8px; padding: 2px 8px; border-radius: 12px; }
.msg.head { margin-top: 10px; padding-top: 5px; }
.msg:hover { background: var(--fill2); }
.ava { width: 36px; height: 36px; margin-top: 1px; border-radius: 50%; display: grid; place-items: center;
  color: #fff; font-size: 14px; font-weight: 600; letter-spacing: .01em; user-select: none; }
.body { grid-column: 2; min-width: 0; }
.meta { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
.name { font-size: 14px; font-weight: 600; color: var(--sc); white-space: nowrap; overflow: hidden;
  text-overflow: ellipsis; min-width: 0; }
.time, .gtime { font-size: 12px; color: var(--text3); text-decoration: none; white-space: nowrap;
  font-variant-numeric: tabular-nums; }
.time:hover, .gtime:hover { text-decoration: underline; }
.gtime { position: absolute; left: 0; top: 5px; width: 44px; text-align: center; font-size: 11px; opacity: 0; }
.msg:hover .gtime, .gtime:focus { opacity: 1; }
@media (hover: none) { .gtime { opacity: .8; } }
.edited { font-size: 12px; color: var(--text3); }
.pinm { display: inline-flex; align-self: center; color: var(--text3); }
.pinm svg { width: 13px; height: 13px; }
.edited.del { color: var(--c0); }
.msg.gone .body > :not(.meta):not(.foot):not(.hist), .svc.gone > span { opacity: .55; }
.hist { max-width: 520px; margin: 3px 0 2px; }
.hist summary { width: fit-content; cursor: pointer; font-size: 12px; color: var(--text3); }
.hist summary:hover { color: var(--text2); }
.was { margin: 4px 0 0; padding: 3px 10px 4px 9px; border-left: 3px solid var(--text3); border-radius: 4px 8px 8px 4px;
  background: var(--fill2); font-size: 13.5px; color: var(--text2); }
.wasd { display: block; font-size: 11.5px; color: var(--text3); font-variant-numeric: tabular-nums; }
.text { white-space: pre-wrap; overflow-wrap: anywhere; }
.text a, .poll a { color: var(--accent); text-decoration: none; }
.text a:hover { text-decoration: underline; }
mark { background: var(--mark); color: inherit; border-radius: 3px; padding: 0 1px; margin: 0 -1px; }
.fwd { display: flex; align-items: center; gap: 5px; margin: 1px 0 2px; font-size: 13px; color: var(--text2); }
.fwd svg { width: 14px; height: 14px; }
.fwd b { font-weight: 600; overflow-wrap: anywhere; }
.quote { display: block; width: 100%; max-width: 520px; margin: 3px 0 4px; padding: 3px 10px 3px 9px; border: 0;
  border-left: 3px solid var(--qc, var(--text3)); border-radius: 4px 8px 8px 4px; background: var(--fill2);
  text-align: left; font-size: 13px; line-height: 1.35; transition: background .15s; }
.quote:hover { background: var(--fill); }
.quote:active { transform: scale(.99); }
.quote span { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.qn { font-weight: 600; color: var(--qc, var(--text2)); }
.qt { color: var(--text2); }
.chip { display: inline-flex; align-items: center; gap: 6px; margin: 3px 0; padding: 4px 11px 4px 8px;
  border-radius: 8px; background: var(--fill); color: var(--text2); font-size: 13px; font-weight: 500; }
.chip svg { width: 16px; height: 16px; }
.poll, .todo { max-width: 440px; margin: 5px 0 3px; padding: 12px 14px; border: 1px solid var(--sep);
  border-radius: 14px; }
.pq { font-weight: 600; overflow-wrap: anywhere; }
.pk { margin: 1px 0 6px; font-size: 12px; color: var(--text3); }
.pa { margin: 9px 0 0; font-size: 14px; }
.pl { display: flex; gap: 10px; align-items: baseline; }
.pp { min-width: 34px; font-weight: 600; font-size: 13px; font-variant-numeric: tabular-nums; }
.pt { overflow-wrap: anywhere; }
.bar { height: 5px; margin: 4px 0 0 44px; border-radius: 3px; background: var(--fill); overflow: hidden; }
.bar i { display: block; height: 100%; border-radius: 3px; background: var(--text3); }
.pa.top .bar i { background: var(--accent); }
.pv { margin-top: 10px; font-size: 12px; color: var(--text3); font-variant-numeric: tabular-nums; }
.todo ul { list-style: none; margin: 4px 0 0; padding: 0; }
.todo li { display: flex; gap: 10px; align-items: flex-start; margin: 7px 0 0; font-size: 14px; }
.ck { flex: none; width: 18px; height: 18px; margin-top: 1px; border: 1.5px solid var(--text3); border-radius: 50%;
  display: grid; place-items: center; }
.ck svg { width: 12px; height: 12px; stroke-width: 2.6; color: #fff; }
.todo li.done .ck { background: var(--accent); border-color: var(--accent); }
.todo li.done > span:last-child { color: var(--text2); }
.foot { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin: 4px 0 2px; }
.rx { padding: 2px 9px; border-radius: 999px; background: var(--fill); font-size: 13px;
  font-variant-numeric: tabular-nums; }
.stats { font-size: 12px; color: var(--text3); font-variant-numeric: tabular-nums; }
.alb { border-radius: 0; margin-top: 0; }
.alb.alb-s { border-radius: 12px 12px 0 0; }
.alb.alb-e { border-radius: 0 0 12px 12px; padding-bottom: 5px; }
.alb, .alb:hover { background: var(--fill2); }
.alb:not(.alb-s)::before { content: ""; position: absolute; left: 56px; right: 8px; top: 0; height: 1px;
  background: var(--sep); }
.rank { font-size: 12px; color: var(--text3); overflow-wrap: anywhere; }
a.chip { text-decoration: none; }
.shot { display: block; width: fit-content; max-width: min(100%, 440px); margin: 5px 0 3px; border-radius: 10px;
  overflow: hidden; background: var(--fill); }
.shot img, video.shot { display: block; max-width: 100%; height: auto; max-height: 460px; }
video.shot { width: min(100%, 440px); }
.shot.stk { background: none; border-radius: 0; }
.shot.rnd { border-radius: 50%; object-fit: cover; }
.shot.veil { cursor: pointer; }
a.shot.veil img, video.shot.veil { filter: blur(28px) saturate(1.4); }
.aud { display: block; width: min(100%, 360px); height: 38px; margin: 4px 0 3px; }
.cd { color: var(--text3); font-weight: 400; overflow-wrap: anywhere; }
.wp { display: block; max-width: 520px; margin: 4px 0 2px; padding: 4px 10px 5px 9px; color: inherit;
  border-left: 3px solid var(--accent); border-radius: 4px 8px 8px 4px; background: var(--fill2);
  font-size: 13.5px; line-height: 1.4; text-decoration: none; }
.ws { font-weight: 600; color: var(--accent); overflow-wrap: anywhere; }
.wt { font-weight: 600; overflow-wrap: anywhere; }
.wd { white-space: pre-wrap; overflow-wrap: anywhere; color: var(--text2); display: -webkit-box;
  -webkit-line-clamp: 8; -webkit-box-orient: vertical; overflow: hidden; }
.kbd { display: grid; gap: 4px; max-width: 520px; margin: 5px 0 2px; }
.kbr { display: flex; gap: 4px; }
.kb { flex: 1 1 0; min-width: 0; padding: 6px 10px; border-radius: 8px; background: var(--fill); color: var(--text2);
  font-size: 13px; font-weight: 500; text-align: center; text-decoration: none; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
a.kb { color: var(--accent); }
.note { max-width: 520px; margin: 4px 0 2px; padding: 6px 10px; border-radius: 8px; background: var(--fill2);
  font-size: 13px; color: var(--text2); white-space: pre-wrap; overflow-wrap: anywhere; }
.note b { color: var(--text); font-weight: 600; }
.note a { color: var(--accent); text-decoration: none; }
.svc small { display: block; font-size: 12px; font-weight: 400; color: var(--text3); }
.svc { display: flex; justify-content: center; margin: 10px 0; position: relative; }
.svc > span { max-width: min(560px, 86%); text-align: center; font-weight: 500; line-height: 1.4;
  overflow-wrap: anywhere; border-radius: 13px; padding: 4px 12px; }
.svc b { font-weight: 600; }
.svc .ql { color: inherit; background: none; border: 0; padding: 0; font-weight: 600; text-decoration: underline;
  text-decoration-color: var(--text3); text-underline-offset: 2px; }
.flash::after { content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
  background: var(--accent-fill); animation: flash 1.8s ease-out forwards; }
@keyframes flash { 0%, 35% { opacity: 1; } 100% { opacity: 0; } }

#pill { position: absolute; z-index: 4; top: calc(var(--bar-h) + 10px); left: 0; right: 0; width: fit-content;
  margin: 0 auto; padding: 4px 12px; border-radius: 999px; font-size: 12.5px; font-weight: 600; color: var(--text2);
  box-shadow: 0 1px 4px rgba(0, 0, 0, .08); opacity: 0; transition: opacity .3s ease-out; pointer-events: none; }
#pill.show { opacity: 1; }
#pill.dup { opacity: 0; }
#scrub { position: absolute; z-index: 4; right: 0; top: calc(var(--bar-h) + 8px); bottom: 8px; width: 22px;
  touch-action: none; opacity: 0; transition: opacity .3s ease-out; }
#scrub.show, #scrub:hover, #scrub.drag { opacity: 1; }
#scrub.off { display: none; }
@media (hover: none) { #scrub:not(.show):not(.drag) { pointer-events: none; } }
#thumb { position: absolute; right: 5px; top: 0; width: 5px; height: 44px; border-radius: 4px;
  background: var(--thumb); transition: width .15s ease-out, right .15s ease-out; }
#scrub:hover #thumb, #scrub.drag #thumb { width: 8px; right: 4px; }
#bubble { position: absolute; right: 26px; top: 0; padding: 5px 11px; border-radius: 9px; font-size: 13px;
  font-weight: 600; white-space: nowrap; box-shadow: var(--shadow); opacity: 0; transform: translateY(-50%);
  pointer-events: none; transition: opacity .15s; font-variant-numeric: tabular-nums; }
#scrub.drag #bubble { opacity: 1; }
#down { position: absolute; z-index: 4; right: 24px; bottom: 24px; bottom: max(24px, env(safe-area-inset-bottom));
  width: 42px; height: 42px; display: grid; place-items: center; padding: 0; border: 0; border-radius: 50%;
  color: var(--text2); box-shadow: var(--shadow); opacity: 0; transform: translateY(10px) scale(.85);
  pointer-events: none; transition: opacity .2s ease-out, transform .45s var(--ease); }
#down.show { opacity: 1; transform: none; pointer-events: auto; }
#down:active { transform: scale(.92); }
#down.ret svg { transform: rotate(180deg); }
#state { position: absolute; inset: var(--bar-h) 0 0; display: grid; place-items: center; align-content: center;
  gap: 14px; padding: 24px; color: var(--text2); text-align: center; pointer-events: none; }
#state:empty { display: none; }
#state h3 { margin: 0; font-size: 20px; font-weight: 600; letter-spacing: -.017em; color: var(--text); }
#state p { margin: 0; font-size: 14px; font-variant-numeric: tabular-nums; }
.spin { width: 24px; height: 24px; border-radius: 50%; border: 2.5px solid var(--fill); border-top-color: var(--accent);
  animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes pulse { from { opacity: .35; } to { opacity: 1; } }
.pop { position: absolute; z-index: 6; top: calc(var(--bar-h) + 8px); right: 12px; width: 280px; padding: 14px;
  border-radius: 16px; box-shadow: var(--shadow); transform-origin: calc(100% - 26px) 0;
  transition: opacity .18s ease-out, transform .35s var(--ease), visibility 0s .35s;
  opacity: 0; transform: scale(.94); visibility: hidden; }
.pop.open { opacity: 1; transform: none; visibility: visible;
  transition: opacity .18s ease-out, transform .35s var(--ease); }
#pins { width: 340px; max-height: min(62vh, 460px); padding: 8px; overflow-y: auto; }
#pins h3 { margin: 6px 8px; font-size: 12px; font-weight: 600; color: var(--text2); }
.pi { display: block; width: 100%; padding: 7px 8px; border: 0; border-radius: 10px; background: none;
  text-align: left; font-size: 13px; line-height: 1.35; }
.pi:hover { background: var(--fill2); }
.pi:active { background: var(--fill); }
.pi span { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.pin { font-weight: 600; }
.pin .pid { display: inline; margin-left: 6px; font-weight: 400; color: var(--text3);
  font-variant-numeric: tabular-nums; }
.pis { color: var(--text2); }
#about { width: min(440px, calc(100vw - 24px)); max-height: min(82vh, 720px); padding: 0; border: 0;
  border-radius: 20px; background: var(--mat-solid); color: var(--text); box-shadow: var(--shadow); overflow: hidden; }
#about[open] { display: flex; flex-direction: column; animation: sheet .35s var(--ease); }
#about::backdrop { background: rgba(0, 0, 0, .32); }
@keyframes sheet { from { opacity: 0; transform: translateY(10px) scale(.96); } }
.abx { position: absolute; top: 10px; right: 10px; color: var(--text2); }
.abb { overflow-y: auto; padding: 28px 22px 20px; }
.abh { display: grid; justify-items: center; gap: 3px; margin-bottom: 16px; text-align: center; }
.abh .cava { width: 76px; height: 76px; margin-bottom: 8px; font-size: 30px; }
.abh h2 { margin: 0; font-size: 20px; line-height: 1.2; font-weight: 700; letter-spacing: -.02em;
  overflow-wrap: anywhere; }
.abh .sub { white-space: normal; }
.abt { margin: 0 0 16px; font-size: 14px; white-space: pre-wrap; overflow-wrap: anywhere; }
.abt a, .abl a { color: var(--accent); text-decoration: none; }
.abt a:hover, .abl a:hover { text-decoration: underline; }
.abl { margin: 0; border-radius: 12px; background: var(--fill2); }
.abl > div { display: flex; justify-content: space-between; gap: 16px; padding: 9px 12px; font-size: 14px; }
.abl > div + div { box-shadow: 0 -1px 0 var(--sep); }
.abl dt { flex: none; color: var(--text2); }
.abl dd { min-width: 0; margin: 0; text-align: right; overflow-wrap: anywhere; font-variant-numeric: tabular-nums; }
.abf { margin: 12px 2px 0; font-size: 12px; color: var(--text3); text-align: center;
  font-variant-numeric: tabular-nums; }
.field { display: block; margin-bottom: 12px; font-size: 12px; font-weight: 600; color: var(--text2); }
.field input { display: block; width: 100%; margin-top: 5px; height: 34px; padding: 0 10px; border: 0;
  border-radius: 9px; background: var(--fill); color: var(--text); font-size: 14px; font-weight: 400; outline: 0; }
.field input:focus { box-shadow: 0 0 0 3px var(--accent-fill); }
.inrow { display: flex; gap: 8px; align-items: center; }
.inrow input { flex: 1; min-width: 0; }
.field .inrow .btn { margin-top: 5px; }
.btn { flex: none; height: 34px; padding: 0 14px; border: 0; border-radius: 9px; background: var(--accent);
  color: var(--on-accent); font-size: 14px; font-weight: 600; transition: transform .1s ease-out; }
.btn.ghost { flex: 1; background: var(--fill); color: var(--accent); }
.btn:active { transform: scale(.96); }
.err { margin: 10px 0 0; font-size: 13px; color: var(--c0); }
.err:empty { display: none; }
#toast { position: fixed; z-index: 9; left: 0; right: 0; bottom: 28px; width: fit-content; max-width: 90%;
  margin: 0 auto; padding: 9px 16px; border-radius: 12px; font-size: 14px; font-weight: 500;
  box-shadow: var(--shadow); opacity: 0; transform: translateY(8px); pointer-events: none;
  transition: opacity .2s ease-out, transform .45s var(--ease); }
#toast.show { opacity: 1; transform: none; }

@media (max-width: 720px) {
  #app { grid-template-columns: 1fr; }
  #side, #main { grid-area: 1 / 1; }
  #app:not(.single) #main { transform: translateX(100%); visibility: hidden;
    transition: transform .5s var(--ease), visibility 0s .5s; box-shadow: -10px 0 30px rgba(0, 0, 0, .1); }
  #app:not(.single) #side { transition: transform .5s var(--ease); }
  #app.open #main { transform: none; visibility: visible; transition: transform .5s var(--ease); }
  #app.open #side { transform: translateX(-28%); }
  #back { display: grid; }
  .tools { order: 3; flex-basis: 100%; }
  .tools .search { flex: 1; width: auto; min-width: 0; }
  .row { padding: 0 14px 0 12px; }
  .msg { grid-template-columns: 32px minmax(0, 1fr); column-gap: 10px; }
  .ava { width: 32px; height: 32px; font-size: 13px; }
  .alb:not(.alb-s)::before { left: 50px; }
  .gtime { width: 40px; }
  #down { right: 16px; }
  .pop, #pins { right: 8px; left: 8px; width: auto; transform-origin: calc(100% - 22px) 0; }
}
@media (prefers-reduced-motion: reduce) {
  .topic, .icon, .btn, .quote, #down, #toast, .pop, #thumb { transform: none !important; }
  #app:not(.single) #main, #app:not(.single) #side, #app.open #main, #app.open #side { transform: none !important; }
  #app:not(.single) #main { opacity: 0; transition: opacity .2s ease-out, visibility 0s .2s; }
  #app.open #main { opacity: 1; transition: opacity .2s ease-out; }
  .pop, .pop.open, #toast, #down { transition-property: opacity, visibility; }
  #about[open] { animation: none; }
  button.cava { transform: none !important; }
  .spin { border-color: var(--accent); animation: pulse 1s ease-in-out infinite alternate; }
}
"""

_JS = r"""
"use strict";
(function () {
const $ = (id) => document.getElementById(id);
const app = $("app"), sc = $("scroller"), list = $("list"), bar = $("bar"), q = $("q"), count = $("count");
const scrub = $("scrub"), thumb = $("thumb"), bubble = $("bubble"), pill = $("pill"), down = $("down");
const state = $("state"), pop = $("jump"), jumpbtn = $("jumpbtn"), toastEl = $("toast");
const olderBtn = $("older"), newerBtn = $("newer"), jdate = $("jdate"), jmsg = $("jmsg"), jerr = $("jerr");
const BATCH = 40, KEEP = 150, MAXROWS = 380;
// row fields, written by reader.py
const ID = 0, DATE = 1, SND = 2, TEXT = 3, REPLY = 4, FWD = 5, MEDIA = 6, ACT = 7, GRP = 8, EDIT = 9,
  REACT = 10, VIEWS = 11, REPLIES = 12, EXTRA = 13, HEAD = 14;
const reduced = matchMedia("(prefers-reduced-motion: reduce)");
const TOPICS = META.topics.map(([k, id, title, count, last, v, info, dir]) =>
  ({ k, id, title, count, last, v, info: info || {}, dir: dir || "" }));
const CHAT = META.chat || {};
const pinbtn = $("pinbtn"), pins = $("pins"), about = $("about");
const byKey = new Map(TOPICS.map((t) => [t.k, t]));
const byId = new Map(TOPICS.map((t) => [t.id, t]));
const single = !META.forum;
const S = { k: null, d: null, lo: 0, hi: 0, q: "", re: null, hits: [], cur: -1, ret: null, atEnd: true,
  anchor: null };
let barH = 56;

// --- formatting

const fTime = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
const fDay = new Intl.DateTimeFormat("en-GB", { weekday: "long", day: "numeric", month: "long" });
const fDayY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "long", year: "numeric" });
const fShort = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" });
const fShortY = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
const fWeek = new Intl.DateTimeFormat("en-GB", { weekday: "short" });
const fFull = new Intl.DateTimeFormat("en-GB", { dateStyle: "full", timeStyle: "medium" });
const num = new Intl.NumberFormat("en-US");
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const plural = (n, w, many) => `${num.format(n)} ${n === 1 ? w : many || w + "s"}`;

function dayKey(t) {
  const d = new Date(t * 1000);
  return d.getFullYear() * 10000 + d.getMonth() * 100 + d.getDate();
}
function dayLabel(t) {
  const now = Date.now() / 1000, k = dayKey(t);
  if (k === dayKey(now)) return "Today";
  if (k === dayKey(now - 86400)) return "Yesterday";
  return new Date(t * 1000).getFullYear() === new Date().getFullYear()
    ? fDay.format(t * 1000) : fDayY.format(t * 1000);
}
function shortDate(t) {
  if (!t) return "";
  const now = Date.now() / 1000;
  if (dayKey(t) === dayKey(now)) return fTime.format(t * 1000);
  if (now - t < 6 * 86400) return fWeek.format(t * 1000);
  return new Date(t * 1000).getFullYear() === new Date().getFullYear()
    ? fShort.format(t * 1000) : fShortY.format(t * 1000);
}

function h(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
const ICONS = {
  photo: '<rect x="3.5" y="5" width="17" height="14" rx="2.5"/><circle cx="9" cy="10" r="1.6"/>' +
    '<path d="m20.5 16-4.5-4.5L7 19"/>',
  file: '<path d="M14 3.5H7.5a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2h9a2 2 0 0 0 2-2V8z"/><path d="M14 3.5V8h4.5"/>',
  link: '<path d="M10 13.5a4.5 4.5 0 0 0 6.8.5l2.7-2.7a4.5 4.5 0 0 0-6.4-6.4l-1.5 1.5"/>' +
    '<path d="M14 10.5a4.5 4.5 0 0 0-6.8-.5l-2.7 2.7a4.5 4.5 0 0 0 6.4 6.4l1.5-1.5"/>',
  pin: '<path d="M12 20.5s-6.5-5.8-6.5-10.8a6.5 6.5 0 0 1 13 0c0 5-6.5 10.8-6.5 10.8z"/>' +
    '<circle cx="12" cy="9.7" r="2.3"/>',
  person: '<circle cx="12" cy="8.5" r="3.8"/><path d="M4.5 20a7.5 7.5 0 0 1 15 0"/>',
  star: '<path d="m12 3.8 2.5 5.1 5.6.8-4 4 1 5.6-5.1-2.7-5.1 2.7 1-5.6-4-4 5.6-.8z"/>',
  dice: '<rect x="4" y="4" width="16" height="16" rx="3.5"/><circle cx="9" cy="9" r=".9"/>' +
    '<circle cx="15" cy="15" r=".9"/><circle cx="15" cy="9" r=".9"/><circle cx="9" cy="15" r=".9"/>',
  fwd: '<path d="m14 5 6 6-6 6"/><path d="M20 11H11a7 7 0 0 0-7 7v1"/>',
  check: '<path d="m6.5 12.5 3.5 3.5 7.5-8"/>',
  tack: '<path d="M9 4h6l-.8 5.8 3.3 3.4V15h-11v-1.8l3.3-3.4z"/><path d="M12 15v5.5"/>',
  lock: '<rect x="5.5" y="10.5" width="13" height="9.5" rx="2.2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
};
function icon(name) {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  s.setAttribute("viewBox", "0 0 24 24");
  s.setAttribute("aria-hidden", "true");
  s.innerHTML = ICONS[name];  // constant markup only
  return s;
}
const MEDIA_LABELS = {
  Photo: ["Photo", "photo"], Document: ["File", "file"], WebPage: ["Link preview", "link"],
  Geo: ["Location", "pin"], GeoLive: ["Live location", "pin"], Venue: ["Venue", "pin"], Contact: ["Contact", "person"],
  Dice: ["Dice", "dice"], Game: ["Game", "star"], Invoice: ["Invoice", "file"], Story: ["Story", "photo"],
  Giveaway: ["Giveaway", "star"], GiveawayResults: ["Giveaway results", "star"], PaidMedia: ["Paid media", "star"],
  Poll: ["Poll", "file"], ToDo: ["Checklist", "file"], Unsupported: ["Unsupported media", "file"],
};
const mediaLabel = (m) => (MEDIA_LABELS[m] || [m.replace(/([a-z])([A-Z])/g, "$1 $2"), "file"]);
const ACTIONS = {
  ChatCreate: "created the group", ChannelCreate: "created the channel", ChatEditTitle: "changed the group name",
  ChatEditPhoto: "changed the group photo", ChatDeletePhoto: "removed the group photo",
  ChatAddUser: "added a member", ChatDeleteUser: "left the group", ChatJoinedByLink: "joined via an invite link",
  ChatJoinedByRequest: "was accepted into the group", ChatMigrateTo: "upgraded the group to a supergroup",
  ChannelMigrateFrom: "upgraded from a basic group", PinMessage: "pinned", HistoryClear: "cleared the history",
  GroupCall: "started a voice chat", GroupCallScheduled: "scheduled a voice chat",
  InviteToGroupCall: "invited members to a voice chat", PhoneCall: "made a call",
  ScreenshotTaken: "took a screenshot", ContactSignUp: "joined Telegram", TopicCreate: "created the topic",
  TopicEdit: "edited the topic", SetMessagesTTL: "changed the auto-delete timer", GameScore: "scored in a game",
  PaymentSent: "sent a payment", GiftPremium: "sent a gift", GiftCode: "sent a gift code", StarGift: "sent a gift",
  SetChatTheme: "changed the chat theme", SetChatWallPaper: "changed the wallpaper", BoostApply: "boosted the group",
  RequestedPeer: "shared a chat", WebViewDataSent: "sent data to a bot", BotAllowed: "allowed the bot to message",
};
const actionText = (a) => ACTIONS[a] || a.replace(/([a-z])([A-Z])/g, "$1 $2").toLowerCase();
const safeUrl = (u) => (typeof u === "string" && /^https?:\/\//i.test(u) ? u : null);
function out(tag, cls, url) {  // a link that opens in a new tab when url is safe, a plain element otherwise
  const e = h(url ? "a" : tag, cls);
  if (url) { e.href = url; e.target = "_blank"; e.rel = "noopener noreferrer"; }
  return e;
}
function sizeText(n) {
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < 3) { n /= 1024; i++; }
  return (i ? n.toFixed(1) : n) + " " + u[i];
}
const dur = (s) => { s = Math.round(s); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const join = (xs, sep) => xs.filter((x) => x != null && x !== "").join(sep || " · ");
function fieldsText(o) {  // plain fields of a details object, for types the page has no layout for
  const v = (x) => (x && typeof x === "object"
    ? (Array.isArray(x) ? x.map(v).join(", ") : (x._ || "") + (Object.keys(x).some((k) => k !== "_")
      ? " (" + fieldsText(x) + ")" : "")) : String(x));
  return Object.entries(o).filter(([k]) => k !== "_").map(([k, x]) => `${k.replace(/_/g, " ")}: ${v(x)}`).join(", ");
}
function osm(g) {
  const lat = Number(g.lat), lon = Number(g.long);
  if (g.lat == null || !isFinite(lat) || !isFinite(lon)) return null;
  return `https://www.openstreetmap.org/?mlat=${lat}&mlon=${lon}#map=16/${lat}/${lon}`;
}
function mediaInfo(ex) {
  const f = ex.file;
  if (f) {
    return join([f.name || f.emoji, join([f.performer, f.title], " – "), f.duration ? dur(f.duration) : "",
      f.size ? sizeText(f.size) : ""]);
  }
  if (ex.geo) return `${ex.geo.lat}, ${ex.geo.long}`;
  if (ex.venue) return join([ex.venue.title, ex.venue.address]);
  if (ex.contact) return join([join([ex.contact.first_name, ex.contact.last_name], " "), ex.contact.phone]);
  if (ex.dice) return `${ex.dice.emoticon} ${ex.dice.value}`;
  if (ex.game) return ex.game.title || "";
  if (ex.invoice) {
    const i = ex.invoice;
    return join([i.title, i.amount != null ? `${i.amount} ${i.currency || ""}` : ""]);
  }
  if (ex.story) return ex.story.caption || "";
  if (ex.paid_media) return `${ex.paid_media.stars} ⭐`;
  if (ex.photo) return fieldsText(ex.photo);
  if (ex.media_info) return fieldsText(ex.media_info);
  return "";
}

function fileUrl(path) {  // a downloaded file, from the folder of the topic that is open
  const dir = (byKey.get(S.k) || {}).dir || "";
  return [...dir.split("/"), ...path.split("/")].filter(Boolean).map(encodeURIComponent).join("/");
}
const IMAGE_RE = /\.(jpe?g|png|webp|gif|bmp|avif)$/i, VIDEO_RE = /\.(mp4|webm|mov|m4v)$/i;
const AUDIO_RE = /\.(mp3|ogg|oga|opus|m4a|wav|flac|aac)$/i;
const PAGE_RE = /\.(x?html?|shtml|svgz?|xml|xslt?|mht|mhtml)$/i;  // files a browser runs scripts from
function fitted(e, f, max) {  // the size is set before the file loads, so the rows around it do not jump
  if (!f.w || !f.h) return;
  const k = Math.min(1, max / f.w, max / f.h);
  e.style.width = Math.round(f.w * k) + "px";
  e.style.aspectRatio = `${f.w} / ${f.h}`;
}
function attachment(f, media, hidden) {  // [what shows the file, whether it stands in for the chip]
  const url = fileUrl(f.path), kind = f.kind;
  const veil = (e) => {
    if (!hidden) return e;
    e.classList.add("veil");
    e.title = "Hidden by the sender, click to show";
    e.addEventListener("click", (ev) => {
      if (!e.classList.contains("veil")) return;
      ev.preventDefault();
      e.classList.remove("veil");
      e.removeAttribute("title");
    });
    return e;
  };
  if (media === "Photo" || (IMAGE_RE.test(f.path) && kind !== "video" && kind !== "gif")) {
    const a = h("a", "shot" + (kind === "sticker" ? " stk" : "")), img = h("img");
    a.href = url;
    a.target = "_blank";
    img.src = url;
    img.loading = "lazy";
    img.decoding = "async";
    img.alt = kind === "sticker" ? f.emoji || "Sticker" : f.name || "Photo";
    fitted(img, f, kind === "sticker" ? 160 : 440);
    a.append(img);
    return [veil(a), true];
  }
  if (kind === "video" || kind === "gif" || kind === "round" || kind === "sticker" || VIDEO_RE.test(f.path)) {
    if (!VIDEO_RE.test(f.path)) return [null, false];  // an animated sticker in Telegram's own format
    const v = h("video", "shot" + (kind === "round" ? " rnd" : kind === "sticker" ? " stk" : ""));
    v.src = url;
    v.preload = "metadata";
    v.playsInline = true;
    if (kind === "gif" || kind === "sticker") { v.loop = true; v.muted = true; }
    if (kind === "sticker") v.autoplay = true;
    else v.controls = true;
    fitted(v, f, kind === "sticker" ? 160 : kind === "round" ? 240 : 440);
    return [veil(v), true];
  }
  if (kind === "voice" || kind === "audio" || AUDIO_RE.test(f.path)) {
    const a = h("audio", "aud");
    a.src = url;
    a.preload = "none";
    a.controls = true;
    return [a, false];
  }
  return [null, false];
}

function hash32(s) {
  let x = 0;
  for (let i = 0; i < s.length; i++) x = (x * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(x);
}
const sender = (d, r) => d.senders[r[SND]] || [null, null];
const nameOf = (s) => s[1] || (s[0] != null ? String(s[0]) : "Unknown");
const colorOf = (s) => (s[0] != null ? Math.abs(s[0]) : hash32(s[1] || "")) % 7;
function initials(s) {
  const w = s.split(/\s+/).map((x) => (x.match(/[\p{L}\p{N}]/u) || [""])[0]).filter(Boolean);
  return w.length ? (w[0] + (w[1] || "")).toUpperCase() : "#";
}
function snippet(d, r) {
  const ex = r[EXTRA];
  if (r[ACT]) return actionText(r[ACT]);
  if (ex && ex.poll) return "Poll: " + ex.poll.question;
  if (ex && ex.todo) return "Checklist: " + ex.todo.title;
  const t = r[TEXT] ? r[TEXT].slice(0, 200).replace(/\s+/g, " ").trim() : "";
  if (t) return t;
  const info = ex ? (ex.webpage && ex.webpage.title) || mediaInfo(ex) : "";
  return r[MEDIA] ? join([mediaLabel(r[MEDIA])[0], info], ": ") : "Message";
}

// --- text with links and search marks

const URL_RE = /\bhttps?:\/\/[^\s<>"'`«»]+/gi;
function marked(parent, s) {
  if (!S.re || !s) { parent.append(s); return; }
  S.re.lastIndex = 0;
  let last = 0, m;
  while ((m = S.re.exec(s))) {
    if (!m[0]) { S.re.lastIndex++; continue; }
    if (m.index > last) parent.append(s.slice(last, m.index));
    parent.append(h("mark", null, m[0]));
    last = m.index + m[0].length;
  }
  parent.append(s.slice(last));
}
function rich(parent, s) {
  URL_RE.lastIndex = 0;
  let last = 0, m;
  while ((m = URL_RE.exec(s))) {
    let url = m[0];
    while (/[.,;:!?'")\]}*_~]$/.test(url)) {
      const c = url.slice(-1);
      if (c === ")" && (url.match(/\(/g) || []).length >= (url.match(/\)/g) || []).length) break;
      url = url.slice(0, -1);
    }
    if (m.index > last) marked(parent, s.slice(last, m.index));
    const a = h("a");
    a.href = url;  // starts with http(s)://, matched above
    a.target = "_blank";
    a.rel = "noopener noreferrer";
    marked(a, url);
    parent.append(a);
    last = m.index + url.length;
    URL_RE.lastIndex = last;
  }
  marked(parent, s.slice(last));
}

// --- rows

function isHead(d, i) {
  if (i === 0) return true;
  const r = d.rows[i], p = d.rows[i - 1];
  if (r[HEAD] || p[ACT] || r[SND] !== p[SND] || r[DATE] - p[DATE] > 600) return true;
  if (r[GRP] != null && r[GRP] === p[GRP]) return false;
  return dayKey(r[DATE]) !== dayKey(p[DATE]);
}
function link(id) { return single ? `#msg/${id}` : `#topic/${S.k}/msg/${id}`; }

function quote(d, id, cls, rp) {
  rp = rp || {};
  if (rp.peer) {  // a message in another chat: nothing to jump to
    const b = h("div", cls || "quote");
    if (cls) { b.textContent = `message #${id} in ${rp.chat || "another chat"}`; return b; }
    b.append(h("span", "qn", join([rp.from, rp.chat || "Another chat"])));
    b.append(h("span", "qt", rp.quote || `Message #${id}`));
    return b;
  }
  const b = h("button", cls || "quote");
  b.type = "button";
  b.dataset.to = id;
  const j = d.idx.get(id);
  if (j == null) {
    if (cls) b.textContent = `message #${id}`;
    else {
      b.append(h("span", "qn", rp.from || "Reply"), h("span", "qt", rp.quote || `Message #${id} is not in this topic`));
    }
    return b;
  }
  const t = d.rows[j], s = sender(d, t);
  if (cls) { b.textContent = "“" + snippet(d, t).slice(0, 80) + "”"; return b; }
  b.style.setProperty("--qc", `var(--c${colorOf(s)})`);
  b.append(h("span", "qn", nameOf(s)), h("span", "qt", rp.quote || snippet(d, t)));
  return b;
}
function webCard(w) {
  const c = out("div", "wp", safeUrl(w.url));
  if (w.site) { const e = h("div", "ws"); marked(e, w.site); c.append(e); }
  if (w.title) { const e = h("div", "wt"); marked(e, w.title); c.append(e); }
  if (w.description) { const e = h("div", "wd"); marked(e, w.description); c.append(e); }
  if (!c.firstChild) c.append(h("div", "ws", w.url || "Link preview"));
  return c;
}
function keyboard(rows) {
  const k = h("div", "kbd");
  for (const row of rows) {
    const line = h("div", "kbr");
    for (const b of row) {
      const e = out("span", "kb", safeUrl(b.url));
      marked(e, b.text || "");
      e.title = b.url || b.copy || b.query || b.text || "";
      line.append(e);
    }
    k.append(line);
  }
  return k;
}
function note(label, text, links) {
  const n = h("div", "note");
  n.append(h("b", null, label), " ");
  if (links) {
    links.forEach((x, i) => {
      if (i) n.append(", ");
      const a = out("span", null, safeUrl(x.url));
      marked(a, x.text);
      a.title = x.url;
      n.append(a);
    });
  } else marked(n, text || "");
  return n;
}
const ACTION_SHOWN = new Set(["names", "title", "users", "icon_color", "icon_emoji_id"]);
function pollCard(p) {
  const c = h("div", "poll");
  const qq = h("div", "pq");
  marked(qq, p.question || "");
  const kinds = [p.quiz ? "Quiz" : "Poll"];
  if (p.multiple_choice) kinds.push("multiple choice");
  if (p.closed) kinds.push("closed");
  c.append(qq, h("div", "pk", kinds.join(" · ")));
  const total = p.total_voters || 0, answers = p.answers || [];
  const top = Math.max(0, ...answers.map((a) => a.voters || 0));
  const known = answers.some((a) => a.voters != null);
  for (const a of answers) {
    const v = a.voters || 0, pct = total ? Math.round((v * 100) / total) : 0;
    const row = h("div", "pa" + (known && v && v === top ? " top" : ""));
    const l = h("div", "pl"), t = h("span", "pt");
    marked(t, a.text || "");
    l.append(h("span", "pp", known ? pct + "%" : "•"), t);
    row.append(l);
    if (known) {
      const bar = h("div", "bar"), fill = h("i");
      fill.style.width = pct + "%";
      bar.append(fill);
      row.append(bar);
    }
    c.append(row);
  }
  c.append(h("div", "pv", total ? plural(total, "vote") : "No votes"));
  return c;
}
function todoCard(t) {
  const c = h("div", "todo"), items = t.items || [];
  const title = h("div", "pq");
  marked(title, t.title || "");
  const done = items.filter((x) => x.done).length;
  c.append(title, h("div", "pk", `Checklist · ${done} of ${items.length} done`));
  const ul = h("ul");
  for (const it of items) {
    const li = h("li", it.done ? "done" : ""), ck = h("span", "ck"), tx = h("span");
    if (it.done) ck.append(icon("check"));
    marked(tx, it.text || "");
    li.append(ck, tx);
    ul.append(li);
  }
  c.append(ul);
  return c;
}
function goneTag(when) {
  const g = h("span", "edited del", "deleted");
  g.title = `No longer in Telegram, found missing on ${fDayY.format(when * 1000)}`;
  return g;
}
function earlier(edits) {  // what an edited message said before
  const d = h("details", "hist");
  d.append(h("summary", null, edits.length === 1 ? "Earlier text" : `${edits.length} earlier texts`));
  for (const [when, text] of edits) {
    const v = h("div", "was"), t = h("div", "text");
    v.append(h("span", "wasd", when ? `until ${fShortY.format(when * 1000)}, ${fTime.format(when * 1000)}` : ""));
    rich(t, text);
    v.append(t);
    d.append(v);
  }
  if (S.re && edits.some((e) => { S.re.lastIndex = 0; return S.re.test(e[1]); })) d.open = true;
  return d;
}
function service(d, r, i) {
  const w = h("div", "svc"), span = h("span"), s = sender(d, r);
  w.title = fFull.format(r[DATE] * 1000);
  if (r[EXTRA] && r[EXTRA].deleted) {
    w.classList.add("gone");
    w.title += ` · deleted, found missing on ${fDayY.format(r[EXTRA].deleted * 1000)}`;
  }
  const a = (r[EXTRA] && r[EXTRA].action) || {};
  span.append(h("b", null, nameOf(s)), " " + actionText(r[ACT]));
  if (r[ACT] === "TopicCreate" && byId.has(r[ID])) span.append(` “${byId.get(r[ID]).title}”`);
  else if (a.title) span.append(` “${a.title}”`);
  if (a.names) span.append(" " + join(a.names, ", "));
  if (r[REPLY]) span.append(" ", quote(d, r[REPLY], "ql", r[EXTRA] && r[EXTRA].reply));
  if (r[TEXT]) span.append(": " + r[TEXT]);
  const more = fieldsText(Object.fromEntries(Object.entries(a).filter(([k]) => !ACTION_SHOWN.has(k))));
  if (more) span.append(h("small", null, more));
  w.append(span);
  return w;
}

function renderRow(i) {
  const d = S.d, r = d.rows[i], p = i ? d.rows[i - 1] : null;
  const row = h("div", "row");
  row.dataset.i = i;
  if (r[HEAD]) row.append(h("div", "thr", r[HEAD][0] ? `Channel post #${r[HEAD][1]}` : `Thread #${r[HEAD][1]}`));
  if (!p || dayKey(p[DATE]) !== dayKey(r[DATE])) {
    const sep = h("div", "sep");
    sep.append(h("span", null, dayLabel(r[DATE])));
    row.append(sep);
  }
  if (r[ACT]) { row.append(service(d, r, i)); return row; }
  const s = sender(d, r), ci = colorOf(s), head = isHead(d, i);
  let cls = "msg" + (head ? " head" : "");
  const g = r[GRP];
  if (g != null) {
    const n = d.rows[i + 1];
    const before = p && !p[ACT] && p[GRP] === g, after = n && !n[ACT] && n[GRP] === g;
    if (before || after) cls += " alb" + (before ? (after ? " alb-m" : " alb-e") : " alb-s");
  }
  const m = h("div", cls);
  m.style.setProperty("--sc", `var(--c${ci})`);
  const when = fTime.format(r[DATE] * 1000), full = fFull.format(r[DATE] * 1000);
  const body = h("div", "body"), ex = r[EXTRA] || {}, media = r[MEDIA];
  if (ex.deleted) m.classList.add("gone");
  if (head) {
    const a = h("div", "ava", initials(nameOf(s)));
    a.style.background = `var(--a${ci})`;
    a.setAttribute("aria-hidden", "true");
    m.append(a);
    const meta = h("div", "meta"), t = h("a", "time", when);
    t.href = link(r[ID]);
    t.title = full + ` · #${r[ID]}`;
    meta.append(h("span", "name", nameOf(s)));
    for (const x of [ex.author, ex.rank, ex.via_bot && "via " + ex.via_bot]) if (x) meta.append(h("span", "rank", x));
    meta.append(t);
    if (ex.pinned) {
      const p = h("span", "pinm");
      p.title = "Pinned";
      p.append(icon("tack"));
      meta.append(p);
    }
    if (r[EDIT]) meta.append(h("span", "edited", "edited"));
    if (ex.deleted) meta.append(goneTag(ex.deleted));
    body.append(meta);
  } else {
    const t = h("a", "gtime", when);
    t.href = link(r[ID]);
    t.title = full + ` · #${r[ID]}`;
    m.append(t);
  }
  if (r[FWD]) {
    const f = h("div", "fwd"), b = h("b");
    marked(b, r[FWD]);
    f.append(icon("fwd"), "Forwarded from ", b);
    if (ex.fwd && ex.fwd.author) f.append(` (${ex.fwd.author})`);
    body.append(f);
  }
  if (r[REPLY]) body.append(quote(d, r[REPLY], null, ex.reply));
  else if (ex.reply && ex.reply.story) body.append(h("div", "chip", "Reply to a story"));
  const file = ex.file && ex.file.path ? ex.file : null;
  const chip = () => {
    const [label, ic] = mediaLabel(media), g = ex.geo || ex.venue;
    // a page sent to the chat is not opened from here: next to the dump, it could read it
    const open = file && !PAGE_RE.test(file.path);
    const c = out("div", "chip", g ? osm(g) : open ? fileUrl(file.path) : null), info = mediaInfo(ex);
    if (file && !open) c.title = `Saved as ${file.path} in the topic's folder`;
    c.append(icon(ic), label);
    if (info) c.append(h("span", "cd", info));
    return c;
  };
  if (ex.poll) body.append(pollCard(ex.poll));
  else if (ex.todo) body.append(todoCard(ex.todo));
  else if (media && media !== "WebPage") {
    const [shown, alone] = file
      ? attachment(file, media, file.spoiler || (ex.photo && ex.photo.spoiler)) : [null, false];
    if (!alone) body.append(chip());
    if (shown) body.append(shown);
  }
  if (r[TEXT]) {
    const t = h("div", "text");
    rich(t, r[TEXT]);
    body.append(t);
  }
  if (ex.edits) body.append(earlier(ex.edits));
  if (media === "WebPage") body.append(ex.webpage ? webCard(ex.webpage) : chip());
  if (ex.buttons) body.append(keyboard(ex.buttons));
  if (ex.links) body.append(note("Links:", null, ex.links));
  if (ex.factcheck) {
    const fc = ex.factcheck;
    body.append(note(`Fact check${fc.country ? " · " + fc.country : ""}:`, fc.text));
  }
  for (const x of ex.restricted || []) body.append(note(`Restricted on ${x.platform}:`, x.text || x.reason));
  const foot = h("div", "foot");
  if (r[REACT]) {
    for (const [k, v] of Object.entries(r[REACT]).sort((a, b) => b[1] - a[1])) {
      const e = k.startsWith("custom:") ? "✦" : k === "paid" ? "⭐" : k;
      foot.append(h("span", "rx", `${e} ${num.format(v)}`));
    }
  }
  const stats = [];
  if (r[VIEWS]) stats.push(`${compact.format(r[VIEWS])} views`);
  if (r[REPLIES]) stats.push(plural(r[REPLIES], "reply", "replies"));
  if (ex.pinned && !head) stats.push("pinned");
  if (r[EDIT] && !head) stats.push("edited");
  if (stats.length) {
    const st = h("span", "stats", stats.join(" · "));
    if (r[VIEWS]) st.title = `${num.format(r[VIEWS])} views`;
    foot.append(st);
  }
  if (ex.deleted && !head) foot.append(goneTag(ex.deleted));
  if (foot.firstChild) body.append(foot);
  m.append(body);
  row.append(m);
  return row;
}

// --- windowed list: only a few hundred rows around the viewport are in the DOM

function firstVisible() {
  const kids = list.children, top = sc.scrollTop + barH;
  let a = 0, b = kids.length - 1;
  if (b < 0) return null;
  while (a < b) {
    const mid = (a + b) >> 1, e = kids[mid];
    if (e.offsetTop + e.offsetHeight <= top) a = mid + 1;
    else b = mid;
  }
  return kids[a];
}
function keep(fn) {  // run a DOM change without moving what is on screen
  const a = firstVisible();
  if (!a) { fn(); return; }
  const i = +a.dataset.i, off = a.offsetTop - sc.scrollTop;
  fn();
  const e = list.children[i - S.lo];
  if (e) sc.scrollTop = e.offsetTop - off;
}
function rows(from, to) {
  const f = document.createDocumentFragment();
  for (let i = from; i < to; i++) f.append(renderRow(i));
  return f;
}
function fill() {
  const n = S.d.rows.length;
  for (let guard = 0; guard < 30; guard++) {
    const st = sc.scrollTop, ch = sc.clientHeight, margin = ch * 1.5 + 400;
    if (st < margin && S.lo > 0) {
      const from = Math.max(0, S.lo - BATCH), f = rows(from, S.lo);
      keep(() => { list.prepend(f); S.lo = from; });
    } else if (sc.scrollHeight - st - ch < margin && S.hi < n) {
      const to = Math.min(n, S.hi + BATCH);
      list.append(rows(S.hi, to));
      S.hi = to;
    } else break;
  }
  if (S.hi - S.lo <= MAXROWS) return;
  const a = firstVisible(), pos = +a.dataset.i - S.lo;
  if (pos > KEEP) {
    const cut = pos - KEEP;
    keep(() => { for (let j = 0; j < cut; j++) list.firstElementChild.remove(); S.lo += cut; });
  }
  const extra = S.hi - (+a.dataset.i) - KEEP;
  for (let j = 0; j < extra; j++) list.lastElementChild.remove();
  if (extra > 0) S.hi -= extra;
}
function place(i, how) {
  const e = list.children[i - S.lo];
  if (how === "end" || !e) sc.scrollTop = sc.scrollHeight;
  else if (how === "top") sc.scrollTop = e.offsetTop - barH;
  else sc.scrollTop = e.offsetTop - barH - Math.max(0, (sc.clientHeight - barH - e.offsetHeight) / 2);
}
function jumpTo(i, how) {
  const n = S.d.rows.length;
  if (!n) return;
  i = Math.max(0, Math.min(n - 1, i));
  S.lo = Math.max(0, i - BATCH);
  S.hi = Math.min(n, i + BATCH + 1);
  list.replaceChildren(rows(S.lo, S.hi));
  place(i, how);
  fill();
  place(i, how);
  fill();
  update();
}
function refresh() {  // re-render the rows in place, e.g. for new search marks
  if (!S.d || S.hi <= S.lo) return;
  keep(() => list.replaceChildren(rows(S.lo, S.hi)));
}
function rowEl(i) { return i >= S.lo && i < S.hi ? list.children[i - S.lo] : null; }
function flash(i) {
  const r = rowEl(i), m = r && r.querySelector(".msg, .svc");
  if (!m) return;
  m.classList.remove("flash");
  void m.offsetWidth;
  m.classList.add("flash");
  setTimeout(() => m.classList.remove("flash"), 1900);
}
function show(i, opts) {  // bring message i into view, centered, and point it out
  if (S.ret == null && opts && opts.ret != null) S.ret = opts.ret;
  const e = rowEl(i);
  if (e) {
    const top = e.offsetTop - sc.scrollTop;
    if (top < barH + 8 || top + e.offsetHeight > sc.clientHeight - 8) place(i, "center");
    fill();
    update();
  } else jumpTo(i, "center");
  flash(i);
}

// --- scroll position: date pill, scrubber, newest button

let pillTimer = 0, scrubTimer = 0, dragging = false;
function update() {
  if (!S.d) return;
  const n = S.d.rows.length, a = firstVisible();
  const st = sc.scrollTop, ch = sc.clientHeight, sh = sc.scrollHeight;
  S.atEnd = S.hi === n && sh - st - ch < 8;
  down.classList.toggle("show", !S.atEnd || S.ret != null);
  down.classList.toggle("ret", S.ret != null);
  down.setAttribute("aria-label", S.ret != null ? "Back to where you were" : "Newest messages");
  if (!a) return;
  const i = +a.dataset.i;
  S.anchor = [i, a.offsetTop - st];
  const fits = S.lo === 0 && S.hi === n && sh <= ch + 2;
  scrub.classList.toggle("off", fits || n < 2);
  if (!dragging) {
    const frac = Math.min(1, Math.max(0, (st + barH - a.offsetTop) / Math.max(1, a.offsetHeight)));
    const visible = Math.max(1, (S.hi - S.lo) * ch / Math.max(1, sh));
    placeThumb(S.atEnd ? 1 : Math.min(1, (i + frac) / Math.max(1, n - visible)), visible / n);
  }
  pill.textContent = dayLabel(S.d.rows[i][DATE]);
  const sep = a.querySelector(":scope > .sep");  // the day separator itself is on screen
  pill.classList.toggle("dup", !!sep && sep.offsetTop + 4 >= st + barH);
}
function placeThumb(f, size) {
  const track = scrub.clientHeight, th = Math.max(36, Math.min(track / 3, track * (size || 0)));
  thumb.style.height = th + "px";
  thumb.style.transform = `translateY(${f * (track - th)}px)`;
  return th;
}
function nudge() {
  pill.classList.add("show");
  scrub.classList.add("show");
  clearTimeout(pillTimer);
  clearTimeout(scrubTimer);
  pillTimer = setTimeout(() => pill.classList.remove("show"), 1200);
  scrubTimer = setTimeout(() => scrub.classList.remove("show"), 1500);
}
let raf = 0, lastTop = 0;
sc.addEventListener("scroll", () => {
  if (raf) return;
  raf = requestAnimationFrame(() => {
    raf = 0;
    if (!S.d) return;
    fill();
    update();
    if (Math.abs(sc.scrollTop - lastTop) > 2) nudge();
    lastTop = sc.scrollTop;
  });
}, { passive: true });
new ResizeObserver(() => {
  barH = bar.offsetHeight;
  document.documentElement.style.setProperty("--bar-h", barH + "px");
}).observe(bar);
new ResizeObserver(() => {
  if (!S.d) return;
  const e = S.anchor && rowEl(S.anchor[0]);  // rows reflow on resize: keep the same message at the top
  if (S.atEnd) sc.scrollTop = sc.scrollHeight;
  else if (e) sc.scrollTop = e.offsetTop - S.anchor[1];
  fill();
  update();
}).observe(sc);

let grab = 0, thumbH = 36, jumpRaf = 0, target = 0;
scrub.addEventListener("pointerdown", (e) => {
  if (!S.d || e.button > 0) return;
  e.preventDefault();
  scrub.setPointerCapture(e.pointerId);
  dragging = true;
  scrub.classList.add("drag");
  thumbH = thumb.offsetHeight;
  const tr = thumb.getBoundingClientRect();
  grab = e.target === thumb ? e.clientY - tr.top : thumbH / 2;  // keep the grab point under the pointer
  drag(e);
});
scrub.addEventListener("pointermove", (e) => { if (dragging) drag(e); });
const endDrag = () => {
  if (!dragging) return;
  dragging = false;
  scrub.classList.remove("drag");
  nudge();
};
scrub.addEventListener("pointerup", endDrag);
scrub.addEventListener("pointercancel", endDrag);
function drag(e) {
  const r = scrub.getBoundingClientRect(), n = S.d.rows.length;
  const f = Math.min(1, Math.max(0, (e.clientY - r.top - grab) / Math.max(1, r.height - thumbH)));
  placeThumb(f, thumbH / r.height);
  const i = Math.round(f * (n - 1));
  bubble.textContent = fDayY.format(S.d.rows[i][DATE] * 1000);
  bubble.style.top = (f * (r.height - thumbH) + thumbH / 2) + "px";
  target = f;
  if (!jumpRaf) jumpRaf = requestAnimationFrame(() => {
    jumpRaf = 0;
    if (target >= 1) jumpTo(n - 1, "end");
    else jumpTo(Math.round(target * (n - 1)), "top");
  });
}
down.addEventListener("click", () => {
  if (!S.d) return;
  if (S.ret != null) { const r = S.ret; S.ret = null; show(r); return; }
  if (S.hi === S.d.rows.length && sc.scrollHeight - sc.scrollTop < sc.clientHeight * 3 && !reduced.matches) {
    sc.scrollTo({ top: sc.scrollHeight, behavior: "smooth" });
  } else jumpTo(S.d.rows.length - 1, "end");
});

// --- clicks in the list: reply quotes and message links

let toastTimer = 0;
function toast(text) {
  toastEl.textContent = text;
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 1800);
}
list.addEventListener("click", (e) => {
  const qb = e.target.closest("[data-to]");
  if (qb) {
    const j = S.d.idx.get(+qb.dataset.to);
    if (j == null) { toast("That message is not in this topic"); return; }
    const from = qb.closest(".row");
    show(j, { ret: from ? +from.dataset.i : null });
    return;
  }
  const a = e.target.closest("a.time, a.gtime");
  if (a) {
    e.preventDefault();
    history.replaceState(null, "", a.getAttribute("href"));
    const url = location.href;
    const done = () => toast("Link to the message copied");
    if (navigator.clipboard && window.isSecureContext !== false) {
      navigator.clipboard.writeText(url).then(done, () => toast("Link is in the address bar"));
    } else toast("Link is in the address bar");
  }
});

// --- search

let searchTimer = 0, searchJob = 0;
function showCount() {
  const n = S.hits.length;
  if (!S.re) count.textContent = "";
  else if (!n) count.textContent = "No results";
  else count.textContent = `${num.format(n - S.cur)} of ${num.format(n)}`;
  olderBtn.disabled = !n || S.cur <= 0;
  newerBtn.disabled = !n || S.cur >= n - 1;
}
function extraText(ex) {
  const parts = [];
  if (ex.poll) parts.push(ex.poll.question, ...ex.poll.answers.map((a) => a.text));
  if (ex.todo) parts.push(ex.todo.title, ...ex.todo.items.map((a) => a.text));
  if (ex.webpage) parts.push(ex.webpage.site, ex.webpage.title, ex.webpage.description);
  if (ex.buttons) parts.push(...ex.buttons.flat().map((b) => b.text));
  if (ex.reply) parts.push(ex.reply.quote);
  if (ex.factcheck) parts.push(ex.factcheck.text);
  if (ex.edits) parts.push(...ex.edits.map((e) => e[1]));
  parts.push(mediaInfo(ex), ex.author, ex.rank);
  return join(parts, "\n");
}
function runSearch() {
  const text = q.value.trim(), job = ++searchJob;
  S.q = text;
  if (!S.d || !text) {
    const had = S.re;
    S.re = null; S.hits = []; S.cur = -1;
    showCount();
    if (had) refresh();
    return;
  }
  const esc = text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const test = new RegExp(esc, "i"), re = new RegExp(esc, "gi");
  const rowsAll = S.d.rows, hits = [];
  let i = 0;
  count.textContent = "Searching…";
  const step = () => {
    if (job !== searchJob) return;
    const end = Math.min(rowsAll.length, i + 20000);
    for (; i < end; i++) {
      const r = rowsAll[i];
      if ((r[TEXT] && test.test(r[TEXT])) || (r[FWD] && test.test(r[FWD])) ||
        (r[EXTRA] && test.test(extraText(r[EXTRA])))) hits.push(i);
    }
    if (i < rowsAll.length) { setTimeout(step, 0); return; }
    S.re = re; S.hits = hits;
    const a = firstVisible(), at = S.atEnd ? rowsAll.length : a ? +a.dataset.i + 3 : rowsAll.length;
    let k = hits.length - 1;
    while (k > 0 && hits[k] > at) k--;
    S.cur = hits.length ? k : -1;
    showCount();
    refresh();
    if (hits.length) show(hits[S.cur]);
  };
  step();
}
function step(dir) {
  if (!S.hits.length) return;
  S.cur = Math.max(0, Math.min(S.hits.length - 1, S.cur + dir));
  showCount();
  show(S.hits[S.cur]);
}
q.addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 220); });
q.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    if (q.value.trim() !== S.q) { clearTimeout(searchTimer); runSearch(); return; }
    step(e.shiftKey ? 1 : -1);
  }
});
olderBtn.addEventListener("click", () => step(-1));
newerBtn.addEventListener("click", () => step(1));
function clearSearch() {
  q.value = "";
  clearTimeout(searchTimer);
  runSearch();
}

// --- go to date or message

function togglePop(open) {
  if (open) togglePins(false);
  pop.classList.toggle("open", open);
  jumpbtn.setAttribute("aria-expanded", String(open));
  jerr.textContent = "";
  if (open) setTimeout(() => jdate.focus({ preventScroll: true }), 30);
}
jumpbtn.addEventListener("click", () => togglePop(!pop.classList.contains("open")));
document.addEventListener("pointerdown", (e) => {
  if (pop.classList.contains("open") && !pop.contains(e.target) && !jumpbtn.contains(e.target)) togglePop(false);
  if (pins.classList.contains("open") && !pins.contains(e.target) && !pinbtn.contains(e.target)) togglePins(false);
});

// --- pinned messages of the open topic

function togglePins(open) {
  if (open) {
    togglePop(false);
    const d = S.d, items = [];
    for (let n = d.pins.length - 1; n >= 0; n--) {  // the newest first, as the app lists them
      const i = d.pins[n], r = d.rows[i], b = h("button", "pi"), who = h("span", "pin", nameOf(sender(d, r)));
      b.type = "button";
      b.dataset.i = i;
      who.append(h("span", "pid", shortDate(r[DATE])));
      b.append(who, h("span", "pis", snippet(d, r)));
      items.push(b);
    }
    $("pinlist").replaceChildren(...items);
    pins.scrollTop = 0;
  }
  pins.classList.toggle("open", open);
  pinbtn.setAttribute("aria-expanded", String(open));
}
pinbtn.addEventListener("click", () => { if (S.d) togglePins(!pins.classList.contains("open")); });
$("pinlist").addEventListener("click", (e) => {
  const b = e.target.closest(".pi");
  if (!b) return;
  togglePins(false);
  history.replaceState(null, "", link(S.d.rows[+b.dataset.i][ID]));
  show(+b.dataset.i);
});
const isoDay = (t) => {
  const d = new Date(t * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
jdate.addEventListener("change", () => {
  if (!S.d || !jdate.value) return;
  const [y, mo, da] = jdate.value.split("-").map(Number), t = new Date(y, mo - 1, da).getTime() / 1000;
  const r = S.d.rows;
  let i;
  if (META.threads) {
    i = -1;
    for (let j = 0; j < r.length; j++) if (r[j][DATE] >= t && (i < 0 || r[j][DATE] < r[i][DATE])) i = j;
  } else {
    let a = 0, b = r.length;
    while (a < b) { const m = (a + b) >> 1; if (r[m][DATE] < t) a = m + 1; else b = m; }
    i = a < r.length ? a : -1;
  }
  if (i < 0) { jumpTo(r.length - 1, "end"); jerr.textContent = "No messages after that date"; return; }
  jumpTo(i, "top");
  flash(i);
});
function goMessage() {
  if (!S.d) return;
  const id = parseInt(jmsg.value.replace(/[^\d]/g, ""), 10), j = S.d.idx.get(id);
  if (j == null) { jerr.textContent = isNaN(id) ? "Enter a message ID" : `No message #${id} in this topic`; return; }
  togglePop(false);
  history.replaceState(null, "", link(id));
  show(j);
}
$("jgo").addEventListener("click", goMessage);
jmsg.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); goMessage(); } });
$("joldest").addEventListener("click", () => { if (S.d) { togglePop(false); jumpTo(0, "top"); } });
$("jnewest").addEventListener("click", () => { if (S.d) { togglePop(false); jumpTo(S.d.rows.length - 1, "end"); } });

// --- keyboard

document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (about.open) return;  // the dialog closes itself on Escape
  if (e.key === "Escape") {
    if (pop.classList.contains("open")) { togglePop(false); jumpbtn.focus(); return; }
    if (pins.classList.contains("open")) { togglePins(false); pinbtn.focus(); return; }
    if (document.activeElement === q || q.value) { clearSearch(); q.blur(); sc.focus({ preventScroll: true }); }
    return;
  }
  if (!S.k) return;
  if ((e.key === "/" && !typing) || ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f")) {
    e.preventDefault();
    q.focus();
    q.select();
  }
});

// --- data files: .reader/<key>.<n>.js calls TGR.a(k, n, rows), TGR.s(k, n, senders), TGR.e(k, n)

const cache = new Map(), pending = new Map();
const part = (k, n) => { const ps = pending.get(k); return ps && ps[n]; };
window.TGR = {
  a(k, n, rs) { const p = part(k, n); if (p) for (const r of rs) p.rows.push(r); },
  s(k, n, s) { const p = part(k, n); if (p) p.senders = s; },
  e(k, n) { const p = part(k, n); if (p) p.done = true; },
};
function merge(parts) {  // one list; sender indexes are per part, so map them to one table
  const d = { rows: [], senders: [], idx: new Map(), pins: [] }, seen = new Map();
  for (const p of parts) {
    const map = p.senders.map((s) => {
      const key = JSON.stringify(s);
      if (!seen.has(key)) { seen.set(key, d.senders.length); d.senders.push(s); }
      return seen.get(key);
    });
    for (const r of p.rows) {
      r[SND] = map[r[SND]];
      if (r[EXTRA] && r[EXTRA].pinned && !r[EXTRA].deleted) d.pins.push(d.rows.length);
      d.idx.set(r[ID], d.rows.length);
      d.rows.push(r);
    }
  }
  return d;
}
function load(k, progress) {
  if (cache.has(k)) {
    const d = cache.get(k);
    cache.delete(k);
    cache.set(k, d);
    return Promise.resolve(d);
  }
  return new Promise((resolve, reject) => {
    const stamps = byKey.get(k).v, parts = stamps.map(() => ({ rows: [], senders: [], done: false }));
    const scripts = [];
    let left = parts.length, failed = false;
    const finish = (err) => {
      pending.delete(k);
      for (const s of scripts) s.remove();
      if (err) { reject(err); return; }
      const d = merge(parts);
      cache.set(k, d);
      while (cache.size > 3) cache.delete(cache.keys().next().value);
      resolve(d);
    };
    if (!left) { finish(); return; }
    pending.set(k, parts);
    stamps.forEach((v, n) => {
      const s = document.createElement("script");
      s.async = false;  // fetched in parallel, run in order
      s.src = `.reader/${encodeURIComponent(k)}.${n}.js?v=${v}`;
      s.onload = () => {
        if (failed) return;
        if (!parts[n].done) { failed = true; finish(new Error("incomplete")); return; }
        if (progress) progress(parts.length - left + 1, parts.length);
        if (--left === 0) finish();
      };
      s.onerror = () => { if (!failed) { failed = true; finish(new Error("missing")); } };
      scripts.push(s);
      document.head.append(s);
    });
  });
}

// --- about the chat: META.chat, as the last run found it

const KINDS = { forum: "Forum", group: "Group", channel: "Channel", user: "Private chat", bot: "Bot" };
function membersText() {
  if (!CHAT.members) return "";
  return CHAT.kind === "channel" ? plural(CHAT.members, "subscriber") : plural(CHAT.members, "member");
}
function avatar(el) {  // the chat's picture, or its initials on a colour
  if (CHAT.photo) el.style.backgroundImage = `url("${CHAT.photo}")`;
  else {
    el.textContent = initials(META.title || "");
    el.style.backgroundColor = `var(--a${hash32(META.title || "") % 7})`;
  }
}
function span(n) {  // seconds as the largest unit that fits: 30 seconds, 1 hour, 7 days
  for (const [size, word] of [[86400, "day"], [3600, "hour"], [60, "minute"]]) {
    if (n >= size && n % size === 0) return plural(n / size, word);
  }
  return plural(n, "second");
}
function buildAbout() {
  const body = $("abbody"), head = h("div", "abh"), av = h("span", "cava"), dl = h("dl", "abl");
  avatar(av);
  const marks = [KINDS[CHAT.kind], membersText(), CHAT.verified && "verified", CHAT.scam && "marked as scam",
    CHAT.fake && "marked as fake"];
  head.append(av, h("h2", null, META.title), h("p", "sub", join(marks)));
  body.replaceChildren(head);
  if (CHAT.about) {
    const t = h("div", "abt");
    rich(t, CHAT.about);
    body.append(t);
  }
  const row = (label, value, url) => {
    if (value == null || value === "") return;
    const r = h("div"), dd = h("dd"), v = out("span", null, safeUrl(url));
    v.textContent = value;
    dd.append(v);
    r.append(h("dt", null, label), dd);
    dl.append(r);
  };
  const day = (s) => (s ? fDayY.format(new Date(s)) : null);
  row("Username", CHAT.username && "@" + CHAT.username, CHAT.link);
  row("Invite link", CHAT.invite, CHAT.invite);
  row("Phone", CHAT.phone && "+" + CHAT.phone);
  row("Created", day(CHAT.created));
  row("Joined", day(CHAT.joined));
  row("Admins", CHAT.admins && num.format(CHAT.admins));
  row(CHAT.kind === "channel" ? "Discussion group" : "Linked channel", CHAT.linked_title || CHAT.linked_chat);
  row("Slow mode", CHAT.slow_mode && span(CHAT.slow_mode) + " between messages");
  row("Auto-delete", CHAT.auto_delete && "after " + span(CHAT.auto_delete));
  row("Location", CHAT.location);
  row("Settings", join([CHAT.protected && "saving and forwarding are off", CHAT.join_request && "joining by request",
    CHAT.signatures && "posts are signed", CHAT.hidden_members && "the member list is hidden",
    CHAT.hidden_history && "new members do not see earlier messages"], ", "));
  row("Members cannot", (CHAT.members_cannot || []).map((x) => x.replace(/_/g, " ")).join(", "));
  if (dl.firstChild) body.append(dl);
  const total = TOPICS.reduce((s, t) => s + t.count, 0);
  body.append(h("p", "abf", join([`${plural(total, "message")} saved`,
    CHAT.updated && `as of ${fDayY.format(new Date(CHAT.updated))}, ${fTime.format(new Date(CHAT.updated))}`])));
}
function openAbout() {
  buildAbout();
  about.showModal();
  $("abbody").scrollTop = 0;
}
for (const id of ["chat", "chatava", "infobtn"]) $(id).addEventListener("click", openAbout);
$("abclose").addEventListener("click", () => about.close());
about.addEventListener("click", (e) => { if (e.target === about) about.close(); });  // a click on the backdrop

// --- topics and navigation

function setState(title, text, spin) {
  state.replaceChildren();
  if (spin) state.append(h("div", "spin"));
  if (title) state.append(h("h3", null, title));
  if (text) state.append(h("p", null, text));
}
function chrome(t) {
  $("title").textContent = t ? t.title : META.title;
  const parts = [];
  if (t) parts.push(plural(t.count, "message"));
  if (t && META.threads) parts.push("grouped by thread");
  if (t && t.info.at) {
    parts.push(`created ${fShortY.format(t.info.at * 1000)}` + (t.info.by ? ` by ${t.info.by}` : ""));
  }
  if (t && t.info.x) parts.push("closed");
  if (!t) parts.push(plural(TOPICS.length, "topic"));
  if (!t || single) parts.push(membersText());
  $("sub").textContent = join(parts);
  $("tools").hidden = !t;
  document.title = t && !single ? `${t.title} – ${META.title}` : META.title;
}
async function openTopic(k, msgId) {
  const t = byKey.get(k);
  if (!t) return;
  const same = S.k === k && S.d;
  if (!same) {
    S.k = k; S.d = null; S.lo = S.hi = 0; S.ret = null; S.anchor = null;
    q.value = ""; S.q = ""; S.re = null; S.hits = []; S.cur = -1; showCount();
    list.replaceChildren();
    scrub.classList.add("off");
    down.classList.remove("show");
    togglePop(false);
    togglePins(false);
    pinbtn.hidden = true;
  }
  app.classList.add("open");
  for (const b of document.querySelectorAll(".topic")) b.setAttribute("aria-current", String(b.dataset.k === k));
  chrome(t);
  if (!same) {
    const loading = `Loading ${plural(t.count, "message")}…`;
    setState(null, t.count > 20000 ? loading : "", true);
    const progress = (i, n) => {
      if (S.k === k && n > 2) setState(null, `${loading} ${Math.round((i * 100) / n)}%`, true);
    };
    let d;
    try { d = await load(k, progress); } catch (err) {
      if (S.k === k) setState("Cannot open this topic", `Data files .reader/${k}.*.js are missing or incomplete.`);
      return;
    }
    if (S.k !== k) return;
    S.d = d;
    setState();
    pinbtn.hidden = !d.pins.length;
    $("pincount").textContent = d.pins.length > 1 ? num.format(d.pins.length) : "";
    if (!d.rows.length) { setState("No messages"); return; }
    if (!META.threads) {
      jdate.min = isoDay(d.rows[0][DATE]);
      jdate.max = isoDay(d.rows[d.rows.length - 1][DATE]);
    }
  }
  const j = msgId != null ? S.d.idx.get(msgId) : undefined;
  if (j != null) { if (same) show(j); else { jumpTo(j, "center"); flash(j); } }
  else if (!same) jumpTo(S.d.rows.length - 1, "end");
  if (msgId != null && j == null) toast(`No message #${msgId} in this topic`);
  if (window.innerWidth > 720 || single) sc.focus({ preventScroll: true });
}
function closeTopic() {
  app.classList.remove("open");
  for (const b of document.querySelectorAll(".topic")) b.setAttribute("aria-current", "false");
}
function route() {
  const m = location.hash.match(/^#(?:topic\/([\w-]+))?\/?(?:msg\/(\d+))?$/);
  const k = single ? (TOPICS[0] && TOPICS[0].k) : m && m[1];
  const id = m && m[2] ? +m[2] : null;
  if (k && byKey.has(k)) openTopic(k, id);
  else if (!single) {
    closeTopic();
    if (!S.k) {
      chrome(null);
      setState(TOPICS.length ? "Choose a topic" : "No messages", TOPICS.length ? `${META.title}` : "");
    }
  }
}
window.addEventListener("hashchange", route);
$("back").addEventListener("click", () => {
  history.replaceState(null, "", location.pathname + location.search);
  closeTopic();
});

function buildTopics() {
  const nav = $("topics"), filter = $("tfilter");
  // pinned topics first, in the order they are pinned in, then the ones written in last
  const sorted = TOPICS.slice().sort((a, b) => (a.info.p || 1e9) - (b.info.p || 1e9) || b.last - a.last);
  const total = TOPICS.reduce((s, t) => s + t.count, 0);
  $("chat").textContent = META.title;
  $("chatsub").textContent = join([plural(TOPICS.length, "topic"), plural(total, "message"), membersText()]);
  avatar($("chatava"));
  const items = sorted.map((t) => {
    const b = h("button", "topic"), info = t.info;
    b.type = "button";
    b.dataset.k = t.k;
    b.setAttribute("aria-current", "false");
    const av = h("span", "tav", info.i || initials(t.title).slice(0, 1) || "#");
    if (info.i) av.classList.add("em");
    else if (info.c) {  // Telegram's own icon colours are light, so the letter on them is dark
      av.style.background = "#" + info.c.toString(16).padStart(6, "0");
      av.style.color = "rgba(0, 0, 0, .62)";
    } else av.style.background = `var(--a${(t.id == null ? 0 : Math.abs(t.id)) % 7})`;
    av.setAttribute("aria-hidden", "true");
    const count = h("span", "tc", plural(t.count, "message")), td = h("span", "td");
    if (info.p) td.append(icon("tack"));
    if (info.x) td.append(icon("lock"));
    td.append(shortDate(t.last));
    td.title = join([info.p && "Pinned topic", info.x && "Closed topic"], ", ");
    b.append(av, h("span", "tn", t.title), td, count);
    b.addEventListener("click", () => {
      if (location.hash === `#topic/${t.k}`) openTopic(t.k);
      else location.hash = `#topic/${t.k}`;
    });
    return b;
  });
  nav.replaceChildren(...items);
  const none = h("div", "nomatch", "No topics match");
  if (TOPICS.length > 8) {
    $("tfilterbox").hidden = false;
    filter.addEventListener("input", () => {
      const v = filter.value.trim().toLowerCase();
      let shown = 0;
      for (let i = 0; i < items.length; i++) {
        const hit = !v || sorted[i].title.toLowerCase().includes(v);
        items[i].hidden = !hit;
        shown += hit;
      }
      if (!shown) nav.append(none); else none.remove();
    });
  }
}

if (single) {
  app.classList.add("single");
  $("infobtn").hidden = !Object.keys(CHAT).length;
} else buildTopics();
chrome(single ? TOPICS[0] || null : null);
if (single && !TOPICS.length) setState("No messages");
route();
})();
"""
