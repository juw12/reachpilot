"""Keep ReachPilot on the newest version from GitHub, even when the app window is closed.

Runs from Task Scheduler (\\ReachPilot\\Updates, every 30 minutes and at sign-in) and from the open app.
It only fast-forwards: on main, no local edits, nothing unpushed. It never merges, rebases or touches local work.
A running app notices the new code by itself and restarts; scheduled tasks pick it up on their next run.

    python selfupdate.py            check once (what the scheduled task does)
    python selfupdate.py install    create / refresh the scheduled task
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CODE_DIR = Path(__file__).parent
LOG_FILE = CODE_DIR / "updates.log"  # git-ignored (*.log)
TASK_PATH = "\\ReachPilot\\"
TASK_NAME = "Updates"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
LOCAL_TIMEOUT = 180  # seconds; plain local git calls have taken over 60 s when the PC is at 100 % CPU
FETCH_TIMEOUT = 120
FALLBACK_PROXIES = ("http://localhost:3128",)  # local proxy used on the office network when the direct route fails
TASK_TIME_LIMIT_MINUTES = 20


def find_git() -> str:
    """Task Scheduler runs with a short PATH, so look in the usual Git for Windows folders too."""
    candidates = [
        shutil.which("git"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
        os.path.expandvars(r"%ProgramFiles%\Git\cmd\git.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Git\cmd\git.exe"),
    ]
    return next((path for path in candidates if path and Path(path).exists()), "git")


GIT = find_git()


def git(*args: str, cwd: Path = CODE_DIR, timeout: int = LOCAL_TIMEOUT) -> subprocess.CompletedProcess:
    return subprocess.run([GIT, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)


def fetch_main(folder: Path) -> bool:
    """Fetch origin/main directly, then through each fallback proxy. True when one route worked."""
    routes = [()] + [("-c", f"http.proxy={proxy}") for proxy in FALLBACK_PROXIES]
    for route in routes:
        try:
            if git(*route, "fetch", "-q", "origin", "main", cwd=folder, timeout=FETCH_TIMEOUT).returncode == 0:
                return True
        except subprocess.TimeoutExpired:
            continue
    return False


def pull_latest_version(folder: Path = CODE_DIR) -> str:
    """Fast-forward to the newest version on GitHub when that is safe. Returns what happened."""
    try:
        if git("rev-parse", "--abbrev-ref", "HEAD", cwd=folder).stdout.strip() != "main":
            return "skipped: not on main"
        if git("--no-optional-locks", "status", "--porcelain", "--untracked-files=no", cwd=folder).stdout.strip():
            return "skipped: local edits in progress"
        if not fetch_main(folder):
            return "skipped: GitHub not reachable"
        behind = int(git("rev-list", "--count", "HEAD..origin/main", cwd=folder).stdout.strip() or 0)
        ahead = int(git("rev-list", "--count", "origin/main..HEAD", cwd=folder).stdout.strip() or 0)
        if behind == 0:
            return "up to date"
        if ahead:
            return "skipped: local commits not pushed yet"
        if git("merge", "--ff-only", "-q", "origin/main", cwd=folder).returncode != 0:
            return "skipped: could not fast-forward"
        return f"updated: {behind} new change(s)"
    except Exception as error:
        return f"skipped: {error}"


def log(result: str) -> None:
    """Keep a short history (last 200 lines) so the Log tab and a human can see what happened."""
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-199:] if LOG_FILE.exists() else []
    lines.append(f"{datetime.now():%Y-%m-%d %H:%M}  {result}")
    LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_task() -> None:
    """Every 30 minutes plus at sign-in, hidden (pythonw), catching up if the PC was asleep."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    script = f"""
$action = New-ScheduledTaskAction -Execute '{runner}' -Argument '"{Path(__file__).resolve()}"' -WorkingDirectory '{CODE_DIR}'
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes {TASK_TIME_LIMIT_MINUTES}) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' -Action $action -Trigger @($repeat, $logon) -Settings $settings `
    -Description 'Keeps ReachPilot on the newest version from GitHub (fast-forward only, never touches local edits).' -Force | Out-Null
Get-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' | Select-Object TaskName, State
"""
    print(subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True).stdout.strip())


if __name__ == "__main__":
    if sys.argv[1:] == ["install"]:
        install_task()
    else:
        log(pull_latest_version())
