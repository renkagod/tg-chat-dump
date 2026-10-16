"""Settings in .env and the optional extras."""

import os
import re
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
    "html": "index.html in the chat folder to read the dump in a browser, offline",
}
DEFAULT_OPTIONS = "takeout,html"  # when TG_OPTIONS is not set at all; "none" turns everything off


def load_settings():
    load_dotenv(ROOT / ".env")
    settings = {k: os.getenv(k, "") for k in KEYS}
    settings["TG_OPTIONS"] = os.getenv("TG_OPTIONS", DEFAULT_OPTIONS)
    return settings


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


API_KEYS = {
    "TG_API_ID": (re.compile(r"\d{3,12}"), "api_id is a number, like 1234567"),
    "TG_API_HASH": (re.compile(r"[0-9a-fA-F]{32}"), "api_hash is 32 letters and digits, 0-9 and a-f"),
}


def ensure_api_keys():
    """Ask for api_id and api_hash until they look right; a broken value in .env is asked again too."""
    s = load_settings()
    bad = [k for k, (pattern, _) in API_KEYS.items() if not pattern.fullmatch(s[k])]
    if not bad:
        return
    print("Telegram needs an app of your own: open https://my.telegram.org, log in,")
    print("choose API development tools, create an app (any name) and copy its api_id and api_hash.")
    found = {}
    for k in bad:
        pattern, hint = API_KEYS[k]
        while not pattern.fullmatch(value := input(f"{k.removeprefix('TG_').lower()}: ").strip()):
            print(f"  {hint}, try again")
        found[k] = value
    save_settings(found)


def out_dir():
    """Where chat folders are written: TG_OUT from .env, or the default OUT."""
    out = load_settings()["TG_OUT"]
    return Path(out).expanduser() if out else OUT


def parse_options(value):
    opts = {o.strip().lower() for o in (value or "").split(",") if o.strip()} - {"none"}
    unknown = opts - OPTIONS.keys()
    if unknown:
        raise ValueError(f"unknown option(s): {', '.join(sorted(unknown))}; choose from {', '.join(OPTIONS)}")
    return frozenset(opts)


def saved_options():
    return parse_options(load_settings()["TG_OPTIONS"])


def save_options(options):
    # an empty value would drop the line from .env and bring the default back
    save_settings({"TG_OPTIONS": ",".join(o for o in OPTIONS if o in options) or "none"})


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
