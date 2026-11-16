"""A schedule for --update-all, kept by the system itself: Task Scheduler on Windows, cron elsewhere."""

import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from xml.sax.saxutils import escape

NAME = "tg-chat-dump update"  # the task in Task Scheduler
MARK = "# tg-chat-dump update"  # the end of our line in the crontab
TASK = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>tg-chat-dump: fetch the new messages of every chat dumped before</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>{repeat}
      <StartBoundary>2026-01-01T{hour:02d}:{minute:02d}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{user}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
    <ExecutionTimeLimit>PT12H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
    </Exec>
  </Actions>
</Task>
"""
REPEAT = """
      <Repetition>
        <Interval>PT{hours}H</Interval>
        <Duration>P1D</Duration>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>"""


@dataclass(frozen=True)
class When:
    """Every day at hour:minute, or every `every` hours on that minute."""

    hour: int = 0
    minute: int = 0
    every: int | None = None

    def __str__(self):
        if self.every:
            return "every hour" if self.every == 1 else f"every {self.every} hours"
        return f"every day at {self.hour:02d}:{self.minute:02d}"


def parse_when(text):
    """04:30 for every day at that time, hourly, or 6h for every six hours."""
    text = (text or "").strip().lower()
    if text == "hourly":
        return When(every=1)
    if m := re.fullmatch(r"(\d{1,2})h", text):
        if 1 <= int(m[1]) <= 12:
            return When(every=int(m[1]))
    elif (m := re.fullmatch(r"(\d{1,2}):(\d{2})", text)) and int(m[1]) < 24 and int(m[2]) < 60:
        return When(int(m[1]), int(m[2]))
    raise ValueError(f"cannot read the time {text!r}; use 04:30 for every day at that time, hourly, or 6h, up to 12h")


def command():
    """What the system runs: this very installation, by its own interpreter, with nothing on the screen."""
    exe = sys.executable
    if sys.platform == "win32" and (silent := Path(exe).with_name("pythonw.exe")).is_file():
        exe = str(silent)  # python.exe would flash a console window on every run
    return [exe, "-m", "tgdump", "--update-all"]


def cron_line(when, run):
    hours = f"*/{when.every}" if when.every and when.every > 1 else "*" if when.every else str(when.hour)
    home = f"TG_HOME={quote(os.environ['TG_HOME'])} " if os.getenv("TG_HOME") else ""
    return f"{when.minute} {hours} * * * {home}{' '.join(quote(part) for part in run)} {MARK}"


def quote(text):
    return text if re.fullmatch(r"[\w./:@%+=-]+", text) else "'" + text.replace("'", "'\\''") + "'"


def cron_when(line):
    minute, hours = line.split()[:2]
    if hours == "*":
        return When(minute=int(minute), every=1)
    if hours.startswith("*/"):
        return When(minute=int(minute), every=int(hours[2:]))
    return When(int(hours), int(minute))


def task_xml(when, run):
    exe, *args = run
    arguments = subprocess.list2cmdline(args)
    if os.getenv("TG_HOME"):  # a task has no environment of its own to keep it in
        inner = f'set "TG_HOME={os.environ["TG_HOME"]}" && {subprocess.list2cmdline(run)}'
        exe, arguments = os.environ.get("COMSPEC", "cmd.exe"), f'/c "{inner}"'
    repeat = REPEAT.format(hours=when.every) if when.every else ""
    # the task belongs to the one who sets it, and runs while they are logged in: no password is asked or kept
    name = "\\".join(filter(None, (os.getenv("USERDOMAIN"), os.getenv("USERNAME"))))
    user = f"\n      <UserId>{escape(name)}</UserId>" if os.getenv("USERNAME") else ""
    return TASK.format(
        repeat=repeat,
        user=user,
        hour=when.hour,
        minute=when.minute,
        command=escape(exe),
        arguments=escape(arguments),
    )


def task_when(xml):
    start = re.search(r"<StartBoundary>[^T<]*T(\d\d):(\d\d)", xml)
    every = re.search(r"<Interval>PT(\d+)H</Interval>", xml)
    if not start:
        return None
    return When(int(start[1]), int(start[2]), int(every[1]) if every else None)


def call(args, text=None, run=subprocess.run):
    return run(args, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)


def current(run=subprocess.run):
    """The schedule that is set, or None."""
    if sys.platform == "win32":
        done = call(["schtasks", "/Query", "/TN", NAME, "/XML"], run=run)
        return task_when(done.stdout) if done.returncode == 0 else None
    done = call(["crontab", "-l"], run=run)
    lines = [ln for ln in done.stdout.splitlines() if ln.rstrip().endswith(MARK)] if done.returncode == 0 else []
    return cron_when(lines[0]) if lines else None


def remove(run=subprocess.run):
    """Take the schedule away; True when there was one."""
    if sys.platform == "win32":
        return call(["schtasks", "/Delete", "/TN", NAME, "/F"], run=run).returncode == 0
    done = call(["crontab", "-l"], run=run)
    lines = done.stdout.splitlines() if done.returncode == 0 else []
    kept = [ln for ln in lines if not ln.rstrip().endswith(MARK)]
    if len(kept) == len(lines):
        return False
    failed = call(["crontab", "-"], "\n".join(kept) + "\n" if kept else "", run=run)
    if failed.returncode:
        raise OSError(failed.stderr.strip() or "crontab refused the new table")
    return True


def schedule(when, run=subprocess.run):
    """Have the system run --update-all at these times, in place of any schedule set before."""
    if sys.platform == "win32":
        # from a file: the command line of schtasks can say neither "catch up on a missed run" nor "no window"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "task.xml"
            path.write_text(task_xml(when, command()), encoding="utf-16")
            done = call(["schtasks", "/Create", "/TN", NAME, "/XML", str(path), "/F"], run=run)
        if done.returncode:
            why = done.stderr.strip() or done.stdout.strip() or "no reason given"
            raise OSError(f"Task Scheduler refused the task: {why}")
        return
    listed = call(["crontab", "-l"], run=run)
    lines = listed.stdout.splitlines() if listed.returncode == 0 else []  # no crontab yet is an error to crontab -l
    kept = [ln for ln in lines if not ln.rstrip().endswith(MARK)]
    done = call(["crontab", "-"], "\n".join([*kept, cron_line(when, command())]) + "\n", run=run)
    if done.returncode:
        raise OSError(done.stderr.strip() or "crontab refused the new table")


def apply(text, run=subprocess.run, write=print):
    """What --schedule does: set the schedule, show it (no value), or take it away (off)."""
    try:
        if text in ("off", "none", "no"):
            write("The schedule is removed." if remove(run) else "There was no schedule.")
        elif text in (None, "", "show"):
            now = current(run)
            write(f"All dumped chats are updated {now}." if now else "No schedule is set.")
        else:
            when = parse_when(text)
            schedule(when, run)
            write(f"All dumped chats will be updated {when}: {subprocess.list2cmdline(command())}")
            write("Turn it off with --schedule off.")
    except FileNotFoundError as e:
        tool = "Task Scheduler (schtasks)" if sys.platform == "win32" else "cron (crontab)"
        raise OSError(f"{tool} is not found on this system") from e
