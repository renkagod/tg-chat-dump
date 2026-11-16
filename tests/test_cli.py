"""The command line: what is named on it, and several chats in one run."""

import asyncio
import sys

import pytest

from tgdump import cli, config


@pytest.fixture
def args(tmp_path, monkeypatch):
    """parse_args() for a command line, with no settings saved."""
    monkeypatch.setattr(config, "ROOT", tmp_path)
    for key in ("TG_CHAT", "TG_FILE_TYPES", "TG_FILE_SIZE"):
        monkeypatch.setenv(key, "")
    monkeypatch.setenv("TG_OPTIONS", "none")

    def parse(*argv):
        monkeypatch.setattr(sys, "argv", ["tg-chat-dump", *argv])
        return cli.parse_args()

    return parse


def test_chats_are_named_in_a_row_or_with_the_option(args, monkeypatch):
    assert args("@one", "https://t.me/two", "-1003").chats == ["@one", "https://t.me/two", "-1003"]
    assert args("@one", "--chat", "@two").chats == ["@one", "@two"] and args("--chat", "@two").named == ["@two"]
    monkeypatch.setenv("TG_CHAT", "@saved")
    a = args("--search", "pizza")
    assert a.chats == ["@saved"] and a.named == []  # the saved chat is dumped by default, and does not narrow a search
    assert args("@one").chats == ["@one"]


def test_command_line_refuses_what_makes_no_sense(args, capsys):
    for argv in ([], ["@one", "@two", "--topics", "5"], ["@one", "--with", "everything"], ["@one", "--since", "soon"]):
        with pytest.raises(SystemExit):
            args(*argv)
    assert "--topics names the topics of one chat" in capsys.readouterr().err
    a = args("@one", "--topics", "37, 12,37", "--since", "2026-03")
    assert a.topics == [12, 37] and a.scope.topics == (12, 37)
    assert args("@one", "--topics", "1").scope.topics == ()  # topics alone are a part of the whole dump


def test_several_chats_are_dumped_in_one_run_and_one_failure_does_not_stop_it(args, monkeypatch, caplog):
    dumped, closed = [], []

    class Account:
        async def disconnect(self):
            closed.append(self)

    async def open_accounts():
        return [Account()], []

    async def dump_chat(clients, chat, takeouts=None, **kw):
        if chat == "@gone":
            raise ValueError("None of the accounts can access this chat")
        dumped.append((chat, takeouts))

    monkeypatch.setattr(cli, "ensure_api_keys", lambda: None)
    monkeypatch.setattr(cli, "open_accounts", open_accounts)
    monkeypatch.setattr(cli, "dump_chat", dump_chat)
    monkeypatch.setattr(sys, "argv", ["tg-chat-dump", "@one", "@gone", "-1003"])
    with caplog.at_level("INFO", logger="dump"), pytest.raises(SystemExit) as stopped:
        asyncio.run(cli.main())
    assert stopped.value.code == 1 and [c for c, _ in dumped] == ["@one", -1003] and len(closed) == 1
    assert dumped[0][1] is dumped[1][1] is not None  # export mode is shared by the chats of a run
    assert "Cannot dump @gone" in caplog.text and "Dumped 2 of 3 chats" in caplog.text
    # one chat that fails says why, as before
    monkeypatch.setattr(sys, "argv", ["tg-chat-dump", "@gone"])
    with pytest.raises(ValueError, match="None of the accounts"):
        asyncio.run(cli.main())


def test_several_chats_are_picked_from_the_search_at_once(monkeypatch, capsys):
    from telethon.tl.types import Channel, ChatPhotoEmpty

    from tgdump import interactive
    from tgdump.interactive import make_item

    names = ("Forum Club", "Forum News", "Forum Talks")
    items = [make_item(Channel(i, n, ChatPhotoEmpty(), None, megagroup=True)) for i, n in enumerate(names, 5)]
    chats = {c.peer_id: c for c in items}
    answers = iter(["forum", "1 3", "forum", "9", "forum", "2"])

    async def ask(prompt=""):
        return next(answers)

    monkeypatch.setattr(interactive, "ask", ask)
    pick = lambda: asyncio.run(interactive.pick_chat([(None, None)], chats, None))  # noqa: E731
    assert pick() == ("update", [items[0], items[2]])
    assert pick() == items[1]  # 9 is not on the list: said so, asked again
    assert "Pick numbers from 1 to 3." in capsys.readouterr().out


def test_a_second_run_stops_while_another_uses_the_accounts(tmp_path, monkeypatch, caplog):
    import subprocess

    from tgdump import accounts

    monkeypatch.setattr(accounts, "DATA", tmp_path)
    monkeypatch.setattr(accounts, "HELD", None)
    accounts.hold_accounts()
    held = accounts.HELD
    accounts.hold_accounts()  # asked again by the same run: it already holds them
    assert accounts.HELD is held
    second = (
        "import pathlib, sys\nfrom tgdump import accounts\n"
        "accounts.DATA = pathlib.Path(sys.argv[1])\naccounts.hold_accounts()\nprint('started')"
    )

    def another_run():
        return subprocess.run([sys.executable, "-c", second, str(tmp_path)], capture_output=True, text=True)

    busy = another_run()
    assert busy.returncode != 0 and "Another tg-chat-dump run is using the accounts" in busy.stderr
    held.close()  # the first run ends
    assert another_run().stdout.strip() == "started"

    # the command line says so in a line, in the log too, and exits with 1
    async def main():
        raise accounts.Busy("Another run is using the accounts")

    monkeypatch.setattr(cli, "main", main)
    monkeypatch.setattr(cli, "setup_logging", lambda console=True: None)
    monkeypatch.setattr(sys, "argv", ["tg-chat-dump", "@one"])
    with caplog.at_level("INFO", logger="dump"), pytest.raises(SystemExit) as stopped:
        cli.run()
    assert stopped.value.code == 1 and "Another run is using the accounts" in caplog.text
