"""Settings in .env and the optional extras."""

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

REPO = Path(__file__).resolve().parent.parent
# A git checkout keeps settings, sessions and dumps next to dump.py; an installed package
# (pipx install tg-chat-dump) keeps settings and sessions in ~/.tg-chat-dump and dumps in ~/tg-chat-dump.
if os.getenv("TG_HOME"):
    ROOT = Path(os.environ["TG_HOME"]).expanduser()
    OUT = ROOT / "out"
elif (REPO / "dump.py").exists():
    ROOT = REPO
    OUT = ROOT / "out"
else:
    ROOT = Path.home() / ".tg-chat-dump"
    OUT = Path.home() / "tg-chat-dump"
DATA = ROOT / "data"  # OUT is the default; TG_OUT in .env overrides it
KEYS = ("TG_API_ID", "TG_API_HASH", "TG_CHAT", "TG_PROXY", "TG_OUT", "TG_OPTIONS")

# Optional extras, switched on with TG_OPTIONS in .env, --with, or the interactive menu.
OPTIONS = {
    "meta": "reactions, views, forwards and reply counts",
    "polls": "poll and checklist contents with results",
    "markdown": "formatting and hidden links, as Markdown in text_md",
    "comments": "for channels: also dump the comments under posts",
    "takeout": "Telegram's export mode, about 10x faster (allow it once in the Telegram app)",
}


def load_settings():
    load_dotenv(ROOT / ".env")
    return {k: os.getenv(k, "") for k in KEYS}


def save_settings(updates):
    """Write keys to .env, keeping the other lines as they are; an empty value removes the key."""
    path = ROOT / ".env"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    rest = [ln for ln in lines if ln.split("=", 1)[0].strip() not in updates]
    rest += [f"{k}={v}" for k, v in updates.items() if v]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(rest) + "\n", encoding="utf-8")
    for k, v in updates.items():
        os.environ[k] = v


def ensure_api_keys():
    s = load_settings()
    missing = {k: input(f"{k} (from my.telegram.org): ").strip() for k in ("TG_API_ID", "TG_API_HASH") if not s[k]}
    if missing:
        save_settings(missing)


def out_dir():
    """Where chat folders are written: TG_OUT from .env, or the default OUT."""
    out = load_settings()["TG_OUT"]
    return Path(out).expanduser() if out else OUT


def parse_options(value):
    opts = {o.strip().lower() for o in (value or "").split(",") if o.strip()}
    unknown = opts - OPTIONS.keys()
    if unknown:
        raise ValueError(f"unknown option(s): {', '.join(sorted(unknown))}; choose from {', '.join(OPTIONS)}")
    return frozenset(opts)


def saved_options():
    return parse_options(load_settings()["TG_OPTIONS"])


def save_options(options):
    save_settings({"TG_OPTIONS": ",".join(o for o in OPTIONS if o in options)})


def parse_proxy(url):
    """socks5://user:pass@host:port -> Telethon proxy dict."""
    if not url:
        return None
    u = urlparse(url)
    return {
        "proxy_type": u.scheme,
        "addr": u.hostname,
        "port": u.port,
        "username": u.username,
        "password": u.password,
        "rdns": True,
    }
