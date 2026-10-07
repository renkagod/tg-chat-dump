"""Terminal colors for the interactive mode. Off when the output is piped, in command-line mode and with NO_COLOR."""

import os
import re
import sys
from pathlib import Path

STYLES = {
    "bold": "1",
    "dim": "90",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "magenta": "95",
    "cyan": "96",
    "blue": "94",  # the bright variants follow the terminal's theme and stay readable on dark backgrounds
}
KIND_COLORS = {"forum": "magenta", "channel": "cyan", "group": "blue", "user": "dim", "bot": "dim"}
CODE_RE = re.compile(r"(\x1b\[[\d;]*m|\x1b\]8;;[^\x1b]*\x1b\\)")
KEY_RE = re.compile(r"\[[^\]]+\]|>(?=\s*$)")
RESET = "\x1b[0m"

enabled = False


def enable():
    global enabled
    enabled = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and (sys.platform != "win32" or win_vt())


def win_vt():
    """Turn on escape codes in the old Windows console; Windows Terminal has them on already."""
    import ctypes

    kernel = ctypes.windll.kernel32
    handle, mode = kernel.GetStdHandle(-11), ctypes.c_uint32()
    return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode)) and kernel.SetConsoleMode(handle, mode.value | 4))


def paint(text, *styles):
    if not enabled or not styles:
        return str(text)
    return f"\x1b[{';'.join(STYLES[s] for s in styles)}m{text}{RESET}"


def keys(text):
    """Highlights the [x] choices and the closing > of a prompt."""
    return KEY_RE.sub(lambda m: paint(m.group(), "blue"), text)


def kind(name):
    return paint(f"[{name}]", KIND_COLORS.get(name, "dim"))


def link(path):
    """A path that Ctrl+click opens, in terminals that support links."""
    if not enabled:
        return str(path)
    return f"\x1b]8;;{Path(path).resolve().as_uri()}\x1b\\{paint(path, 'blue')}\x1b]8;;\x1b\\"


def visible_len(text):
    return len(CODE_RE.sub("", text))


def fit(text, width):
    """Cuts or pads a line to `width` visible characters without breaking its color codes."""
    out, seen = [], 0
    for part in CODE_RE.split(text):
        if CODE_RE.fullmatch(part):
            out.append(part)
        else:
            part = part[: width - seen]
            seen += len(part)
            out.append(part)
    return "".join(out) + (RESET if enabled else "") + " " * (width - seen)
