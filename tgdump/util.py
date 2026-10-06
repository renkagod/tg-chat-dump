import re


def iso(d):
    return d.isoformat() if d else None


def slug(s):
    return re.sub(r"[^\w\-]+", "_", s).strip("_")[:60]


def plain(text):
    """Poll and checklist texts are TextWithEntities in newer layers, plain strings in older ones."""
    return getattr(text, "text", text)


def parse_chat(s):
    """A chat or user reference: numeric id, @username or t.me link."""
    s = str(s).strip()
    return int(s) if s.lstrip("-").isdigit() else s


def fmt_duration(seconds):
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m {s:02d}s" if m else f"{s}s"
