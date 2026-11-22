"""What differs between Windows and a Mac, kept in one place.

ReachPilot was written on Windows. The other files ask this one for the things the two systems do differently:

  NO_WINDOW            the subprocess flag that keeps a console window from flashing up (0 where there is none)
  hidden_python()      the Python that runs without a window (pythonw.exe on Windows)
  day_label()          "Sat 7 Nov · 12:30", which strftime cannot spell the same way on both
  open_path()          open a file or a folder in its own program
  open_console()       a terminal window for the steps that need you (a sign-in, an approval)
  notify()             a notification on a Mac (the Windows toast stays in autopilot.py)
  browser_channel()    the installed browser Playwright drives for screenshots and recordings
  font_path()          the font file to draw with
  install_task() ...   the scheduled tasks on a Mac, as launchd agents. On Windows they stay Task Scheduler tasks,
                       made by each module's own install command.

    python machine.py run <task> <minutes> <command...>

is what a launchd agent runs: the command with a time limit, plus a note of when it ran and how it ended
(data/tasks/<task>.json, git-ignored), which is where the dashboard reads a task's last run from.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # 0x08000000 on Windows; any other value is refused elsewhere

AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LABEL_PREFIX = "io.github.reachpilot."
TASK_NOTES = CODE_DIR / "data" / "tasks"
TIMED_OUT = 124  # exit code of a run that was stopped at its time limit (the same number `timeout` uses)

MAC_FONT_DIRS = (Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts"))
MAC_FONTS = {  # the Windows font the drawing code asks for -> the nearest font every Mac has
    "segoeui.ttf": "Arial.ttf",
    "segoeuib.ttf": "Arial Bold.ttf",
    "seguisb.ttf": "Arial Bold.ttf",
    "seguibl.ttf": "Arial Black.ttf",
    "arialbd.ttf": "Arial Bold.ttf",
}


# ------------------------------------------------------------------ small differences

def console_python() -> Path:
    """The Python with a console: python.exe next to pythonw.exe on Windows, the running one elsewhere."""
    candidate = Path(sys.executable).with_name("python.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def hidden_python() -> Path:
    """The Python that opens no window: pythonw.exe on Windows, the running one elsewhere."""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def day_label(moment: datetime) -> str:
    """'Sat 7 Nov · 12:30'. The day without its leading zero is %#d on Windows and %-d elsewhere, so it is built here."""
    return f"{moment:%a} {moment.day} {moment:%b} · {moment:%H:%M}"


def open_path(path) -> None:
    """Open a file or a folder the way a double click does."""
    if IS_WINDOWS:
        os.startfile(path)  # noqa: S606 - callers pass their own files only
    else:
        subprocess.Popen(["open" if IS_MAC else "xdg-open", str(path)])


def _applescript_text(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def open_console(command: list[str], cwd: Path) -> None:
    """Open a visible terminal window that runs command in cwd, for the steps that need you."""
    if IS_WINDOWS:
        subprocess.Popen(command, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return
    if command[:2] == ["cmd", "/k"]:  # "keep the window open" is how a terminal behaves anyway
        command = command[2:]
    line = f"cd {shlex.quote(str(cwd))} && {shlex.join(str(part) for part in command)}"
    if IS_MAC:
        script = f'tell application "Terminal"\nactivate\ndo script {_applescript_text(line)}\nend tell'
        subprocess.Popen(["osascript", "-e", script])
    else:
        terminal = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal") or "xterm"
        subprocess.Popen([terminal, "-e", "sh", "-c", f"{line}; exec sh"])


def notify(title: str, message: str) -> None:
    """A notification on a Mac; best effort, never blocks. Does nothing on other systems."""
    if not IS_MAC:
        return
    script = f"display notification {_applescript_text(message[:240])} with title {_applescript_text(title)}"
    try:
        subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def _mac_app(name: str) -> Path | None:
    for folder in (Path("/Applications"), Path.home() / "Applications"):
        if (folder / f"{name}.app").exists():
            return folder / f"{name}.app"
    return None


def browser_channel() -> str | None:
    """The installed browser Playwright drives, so no browser has to be downloaded.

    Windows always has Edge. A Mac uses Edge when it is installed, else Chrome. None means neither is there and
    Playwright uses its own Chromium (`python -m playwright install chromium` downloads it once).
    """
    if IS_WINDOWS:
        return "msedge"
    if IS_MAC:
        if _mac_app("Microsoft Edge"):
            return "msedge"
        return "chrome" if _mac_app("Google Chrome") else None
    if shutil.which("microsoft-edge"):
        return "msedge"
    return "chrome" if shutil.which("google-chrome") else None


def app_window_browsers() -> list[str]:
    """Browsers that can show a page as an app window of its own (--app=URL), the preferred one first."""
    if IS_WINDOWS:
        return [
            shutil.which("msedge") or "",
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    if IS_MAC:
        found = []
        for name in ("Microsoft Edge", "Google Chrome"):
            app = _mac_app(name)
            if app:
                found.append(str(app / "Contents" / "MacOS" / name))
        return found
    return [shutil.which(name) or "" for name in ("microsoft-edge", "google-chrome", "chromium")]


def font_path(name: str) -> Path:
    """Where the font file is. The drawing code names Windows fonts; a Mac gets the nearest one it has."""
    if IS_MAC:
        for folder in MAC_FONT_DIRS:
            candidate = folder / MAC_FONTS.get(name, name)
            if candidate.exists():
                return candidate
    return Path("C:/Windows/Fonts") / name


# ------------------------------------------------------------------ scheduled tasks on a Mac (launchd)

def task_slug(name: str) -> str:
    """'LinkedIn posts' -> 'linkedin-posts': the task's name in a file name and in its launchd label."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def task_label(name: str) -> str:
    return LABEL_PREFIX + task_slug(name)


def task_plist_path(name: str) -> Path:
    return AGENTS_DIR / f"{task_label(name)}.plist"


TOOL_FOLDERS = ("/opt/homebrew/bin", "/usr/local/bin")  # where gh and git are on a Mac when PATH does not say


def task_path() -> str:
    """PATH for a task: the installing terminal's, plus the folder that holds gh when that PATH does not find it.

    An AI app's tool shell can have the same short PATH as launchd. A task installed from it found no gh, GitHub
    then gave it no list of repos, and the Search task wrote a sitemap without any project's own site.
    """
    path = os.environ.get("PATH") or "/usr/bin:/bin:/usr/sbin:/sbin"
    if shutil.which("gh", path=path):
        return path
    for folder in TOOL_FOLDERS:
        if (Path(folder) / "gh").is_file():
            return f"{folder}{os.pathsep}{path}"
    return path


def task_plist(name: str, command: list[str], cwd: Path, every_minutes: int = 0, daily_at: str = "",
               at_login: bool = False, limit_minutes: int = 30) -> dict:
    """The launchd agent for one task: command every every_minutes, or once a day at daily_at ("09:00").

    launchd never starts a second copy while one runs, and a run that was missed while the Mac slept happens once
    on waking, which is what the Windows tasks are set to do. PATH is copied from the terminal that installs the
    task, because launchd starts with a short one that does not find gh, git or a Homebrew program.
    """
    agent = {
        "Label": task_label(name),
        "ProgramArguments": [sys.executable, str(CODE_DIR / "machine.py"), "run", task_slug(name), str(limit_minutes),
                             *[str(part) for part in command]],
        "WorkingDirectory": str(cwd),
        "EnvironmentVariables": {"PATH": task_path(), "PYTHONUTF8": "1"},
        "ProcessType": "Standard",  # "Background" is throttled; a throttled run did not finish in time on Windows either
        "RunAtLoad": bool(at_login),
    }
    if daily_at:
        hour, minute = (int(part) for part in daily_at.split(":"))
        agent["StartCalendarInterval"] = {"Hour": hour, "Minute": minute}
    else:
        agent["StartInterval"] = int(every_minutes) * 60
    return agent


def _launchctl(*arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *arguments], capture_output=True, text=True, timeout=30)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install_task(name: str, command: list[str], cwd: Path, every_minutes: int = 0, daily_at: str = "",
                 at_login: bool = False, limit_minutes: int = 30) -> str:
    """Create or refresh the task's launchd agent and load it. Returns the line to print."""
    if not IS_MAC:
        raise SystemExit("Scheduled tasks are set up for Windows (Task Scheduler) and macOS (launchd). "
                         f"On this system, run it from cron: {shlex.join(str(part) for part in command)}")
    agent = task_plist(name, command, cwd, every_minutes, daily_at, at_login, limit_minutes)
    path = task_plist_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(agent))
    _launchctl("bootout", f"{_domain()}/{agent['Label']}")  # an older copy, when there is one
    loaded = _launchctl("bootstrap", _domain(), str(path))
    if loaded.returncode != 0:  # launchd may still be letting go of the older copy
        time.sleep(2)
        loaded = _launchctl("bootstrap", _domain(), str(path))
    if loaded.returncode != 0:
        return f"{name}: written to {path}, but launchd did not load it: {(loaded.stderr or loaded.stdout).strip()}"
    when = f"every day at {daily_at}" if daily_at else f"every {every_minutes} minutes"
    return f"{name}: scheduled {when}{' and at sign-in' if at_login else ''} ({path})"


def remove_task(name: str) -> str:
    """Unload the task's launchd agent and delete it. Returns the line to print."""
    path = task_plist_path(name)
    _launchctl("bootout", f"{_domain()}/{task_label(name)}")
    existed = path.exists()
    path.unlink(missing_ok=True)
    return f"{name}: removed" if existed else f"{name}: was not installed"


def start_task(name: str) -> None:
    """Run the task now, as its Run now button on the dashboard asks."""
    _launchctl("kickstart", f"{_domain()}/{task_label(name)}")


def task_note(name: str) -> dict:
    """What the last run left behind: {"started", "ended", "exit"}; {} before the first run."""
    try:
        return json.loads((TASK_NOTES / f"{task_slug(name)}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _next_run(agent: dict, started: datetime | None, now: datetime) -> datetime | None:
    calendar = agent.get("StartCalendarInterval")
    if calendar:
        at = now.replace(hour=int(calendar.get("Hour", 0)), minute=int(calendar.get("Minute", 0)), second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)
    interval = int(agent.get("StartInterval") or 0)
    if interval and started:
        upcoming = started + timedelta(seconds=interval)
        return upcoming if upcoming > now else now
    return None


def task_info(name: str, now: datetime | None = None) -> dict:
    """One task as the dashboard shows it: the same fields hub.task_status reads from Task Scheduler on Windows."""
    path = task_plist_path(name)
    if not path.exists():
        return {"name": name, "installed": False, "unknown": False}
    try:
        agent = plistlib.loads(path.read_bytes())
    except Exception:
        return {"name": name, "installed": False, "unknown": True}
    now = now or datetime.now()
    note = task_note(name)
    started = None
    try:
        started = datetime.fromisoformat(note["started"]) if note.get("started") else None
    except ValueError:
        pass
    running = bool(started) and not note.get("ended")
    upcoming = _next_run(agent, started, now)
    last_result = "" if running or "exit" not in note else str(note["exit"])
    return {
        "name": name,
        "installed": True,
        "state": "Running" if running else "Ready",
        "last_run": day_label(started) if started else "",
        "next_run": day_label(upcoming) if upcoming else "",
        "last_result": last_result,
        "ok": last_result in ("", "0"),  # never run, running, or ended well
    }


def run_task(slug: str, limit_minutes: int, command: list[str]) -> int:
    """Run command with a time limit and note when it ran and how it ended. Returns its exit code."""
    TASK_NOTES.mkdir(parents=True, exist_ok=True)
    note_file = TASK_NOTES / f"{slug}.json"
    note = {"started": datetime.now().isoformat(timespec="seconds")}
    note_file.write_text(json.dumps(note), encoding="utf-8")
    try:
        code = subprocess.run(command, timeout=limit_minutes * 60 if limit_minutes > 0 else None).returncode
    except subprocess.TimeoutExpired:
        code = TIMED_OUT
    except OSError as error:
        print(f"{slug}: could not start {command[:1]}: {error}", file=sys.stderr)
        code = 127
    note.update(ended=datetime.now().isoformat(timespec="seconds"), exit=code)
    note_file.write_text(json.dumps(note), encoding="utf-8")
    return code


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "run" and sys.argv[3].isdigit():
        sys.exit(run_task(sys.argv[2], int(sys.argv[3]), sys.argv[4:]))
    print(__doc__)
    sys.exit(2)
