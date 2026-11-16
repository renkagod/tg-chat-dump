import asyncio
import re


async def all_or_none(coros):
    """Run the workers together and return their results; when one fails, the others are stopped before
    the error goes on.

    asyncio.gather leaves them running, and they would go on writing to a database that is closed by then.
    """
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def iso(d):
    return d.isoformat() if d else None


def slug(s):
    return re.sub(r"[^\w\-]+", "_", s).strip("_")[:60]


def topic_folder(out, topic, titles):
    """A forum topic's folder; its name follows the topic title."""
    return out / f"{topic}_{slug(titles.get(topic) or ('General' if topic == 1 else ''))}".rstrip("_")


def plain(text):
    """Poll and checklist texts are TextWithEntities in newer layers, plain strings in older ones."""
    return getattr(text, "text", text)


def parse_chat(s):
    """A chat or user reference: numeric id, @username or t.me link."""
    s = str(s).strip()
    return int(s) if s.lstrip("-").isdigit() else s


def size_text(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_duration(seconds):
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"
