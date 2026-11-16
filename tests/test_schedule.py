"""--schedule: the system runs --update-all on its own, by Task Scheduler on Windows and cron elsewhere."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from tgdump import schedule
from tgdump.schedule import MARK, NAME, When, parse_when


def done(code=0, out="", err=""):
    return SimpleNamespace(returncode=code, stdout=out, stderr=err)


class Cron:
    """crontab as cron keeps it: -l prints the table or fails when there is none, - replaces it."""

    def __init__(self, table=None):
        self.table = table

    def __call__(self, args, input=None, **kw):
        assert args[0] == "crontab"
        if args[1] == "-l":
            return done(out=self.table) if self.table is not None else done(1, err="no crontab for ann")
        self.table = input
        return done()


class Tasks:
    """Task Scheduler behind schtasks: one task by our name, kept as the XML it was made from."""

    def __init__(self):
        self.xml, self.calls = None, []

    def __call__(self, args, input=None, **kw):
        assert args[0] == "schtasks" and args[args.index("/TN") + 1] == NAME
        self.calls.append(args[1])
        if args[1] == "/Create":
            raw = Path(args[args.index("/XML") + 1]).read_bytes()
            assert raw.startswith(b"\xff\xfe") and "/F" in args  # UTF-16 as its header says, and replacing the old one
            self.xml = raw.decode("utf-16")
        elif self.xml is None:
            return done(1, err="ERROR: The system cannot find the file specified.")
        elif args[1] == "/Delete":
            self.xml = None
        else:
            return done(out=self.xml)
        return done()


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(sys, "executable", "/home/ann/.local/pipx/venvs/tg-chat-dump/bin/python")
    monkeypatch.delenv("TG_HOME", raising=False)


@pytest.fixture
def windows(monkeypatch, tmp_path):
    for name in ("python.exe", "pythonw.exe"):
        (tmp_path / name).write_bytes(b"")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", str(tmp_path / "python.exe"))
    monkeypatch.delenv("TG_HOME", raising=False)
    monkeypatch.setenv("USERDOMAIN", "DESK")
    monkeypatch.setenv("USERNAME", "ann")
    return tmp_path


def said(text, run):
    lines = []
    schedule.apply(text, run, lines.append)
    return " ".join(lines)


def test_times_are_read():
    assert parse_when("04:30") == When(4, 30) and parse_when(" 4:05 ") == When(4, 5)
    assert parse_when("hourly") == When(every=1) and parse_when("6H") == When(every=6)
    assert (str(When(4, 5)), str(When(every=1)), str(When(every=6))) == (
        "every day at 04:05",
        "every hour",
        "every 6 hours",
    )
    for wrong in ("", "25:00", "04:60", "0h", "13h", "noon", "4"):
        with pytest.raises(ValueError, match="cannot read the time"):
            parse_when(wrong)


def test_cron_gets_a_line_and_keeps_the_others(linux):
    cron = Cron("MAILTO=ann\n15 3 * * * backup --all\n")
    assert said(None, cron) == "No schedule is set."
    assert "every day at 04:30" in said("04:30", cron)
    ours = f"30 4 * * * /home/ann/.local/pipx/venvs/tg-chat-dump/bin/python -m tgdump --update-all {MARK}"
    assert cron.table == f"MAILTO=ann\n15 3 * * * backup --all\n{ours}\n"
    assert said("show", cron) == "All dumped chats are updated every day at 04:30."
    said("6h", cron)  # a new time replaces the old one
    assert cron.table.count(MARK) == 1 and "\n0 */6 * * * /home" in cron.table
    assert said("show", cron) == "All dumped chats are updated every 6 hours."
    said("hourly", cron)
    assert "\n0 * * * * /home" in cron.table and schedule.current(cron) == When(every=1)
    assert said("off", cron) == "The schedule is removed." and cron.table == "MAILTO=ann\n15 3 * * * backup --all\n"
    assert said("off", cron) == "There was no schedule."


def test_cron_with_no_table_yet_and_a_home_of_its_own(linux, monkeypatch):
    monkeypatch.setenv("TG_HOME", "/data/my dumps")
    cron = Cron()
    said("23:05", cron)
    assert cron.table.startswith("5 23 * * * TG_HOME='/data/my dumps' /home/ann/") and cron.table.endswith(f"{MARK}\n")
    assert schedule.current(cron) == When(23, 5)
    said("off", cron)
    assert cron.table == ""


def test_windows_gets_a_task_that_catches_up_and_shows_no_window(windows):
    tasks = Tasks()
    assert said(None, tasks) == "No schedule is set."
    assert "every day at 04:30" in said("04:30", tasks)
    assert "<StartBoundary>2026-01-01T04:30:00</StartBoundary>" in tasks.xml and "<Repetition>" not in tasks.xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in tasks.xml
    assert "<UserId>DESK\\ann</UserId>" in tasks.xml and "<LogonType>InteractiveToken</LogonType>" in tasks.xml
    ElementTree.fromstring(tasks.xml.split("?>", 1)[1])  # it is XML at all
    assert f"<Command>{windows / 'pythonw.exe'}</Command>" in tasks.xml
    assert "<Arguments>-m tgdump --update-all</Arguments>" in tasks.xml
    assert said("show", tasks) == "All dumped chats are updated every day at 04:30."
    said("6h", tasks)
    assert "<Interval>PT6H</Interval>" in tasks.xml and schedule.current(tasks) == When(every=6)
    assert said("off", tasks) == "The schedule is removed." and said("off", tasks) == "There was no schedule."
    assert tasks.calls.count("/Create") == 2


def test_windows_task_carries_a_home_of_its_own(windows, monkeypatch):
    monkeypatch.setenv("TG_HOME", r"D:\My dumps & more")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    tasks = Tasks()
    said("hourly", tasks)
    assert r"<Command>C:\Windows\System32\cmd.exe</Command>" in tasks.xml
    assert 'set "TG_HOME=D:\\My dumps &amp; more" &amp;&amp; ' in tasks.xml and "--update-all" in tasks.xml


def test_what_goes_wrong_is_said(linux, windows):
    def refuses(args, **kw):
        return done(1, err="ERROR: Access is denied.")

    with pytest.raises(OSError, match="Access is denied"):
        schedule.schedule(When(4, 30), refuses)

    def missing(args, **kw):
        raise FileNotFoundError(args[0])

    with pytest.raises(OSError, match="Task Scheduler"):
        said("04:30", missing)
    with pytest.raises(ValueError, match="cannot read the time"):
        said("sometimes", missing)
    assert schedule.command()[1:] == ["-m", "tgdump", "--update-all"]
    assert subprocess.list2cmdline(schedule.command()).endswith("pythonw.exe -m tgdump --update-all")
