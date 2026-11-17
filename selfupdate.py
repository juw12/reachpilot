"""Keep ReachPilot on the newest version from GitHub, even when the app window is closed.

Runs from Task Scheduler (\\ReachPilot\\Updates, every 30 minutes and at sign-in) and from the open app.
It only fast-forwards: on main, no local edits, nothing unpushed. It never merges, rebases or touches local work.
A running app notices the new code by itself and restarts; scheduled tasks pick it up on their next run.
When requirements.txt changes, the packages a new feature needs are installed too (missing ones only).

    python selfupdate.py            check once (what the scheduled task does)
    python selfupdate.py install    create / refresh the scheduled task
"""

from __future__ import annotations

import hashlib
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
REQUIREMENTS_FILE = "requirements.txt"
PACKAGES_STAMP = "packages.log"  # git-ignored (*.log): which requirements.txt was installed last, and how it went
PIP_TIMEOUT = 420  # seconds per route; two routes stay inside the task's time limit


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


def packages_due(requirements: str, stamp: str, today: str) -> bool:
    """True when this requirements.txt was not installed yet, or the last try failed on an earlier day."""
    digest = hashlib.sha256(requirements.encode("utf-8")).hexdigest()
    recorded = stamp.split()
    if recorded[:1] != [digest]:
        return True
    return recorded[1:2] == ["failed"] and recorded[2:3] != [today]


def run_pip(*args: str, cwd: Path = CODE_DIR) -> bool:
    """pip for the Python that runs ReachPilot (python.exe next to pythonw.exe). True when it succeeded."""
    console_python = Path(sys.executable).with_name("python.exe")
    python = console_python if console_python.exists() else Path(sys.executable)
    command = [str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-input", *args]
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=PIP_TIMEOUT, creationflags=NO_WINDOW).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def install_new_packages(folder: Path = CODE_DIR, today: str = "") -> str:
    """Install what requirements.txt asks for when it changed. Missing packages only, nothing is upgraded.

    Returns "" when there was nothing to do. A failure never blocks the app: features that need a missing
    package fall back by themselves (the film goes out silent), and the install is tried again the next day.
    """
    today = today or f"{datetime.now():%Y-%m-%d}"
    requirements_file, stamp_file = folder / REQUIREMENTS_FILE, folder / PACKAGES_STAMP
    try:
        if not requirements_file.exists():
            return ""
        requirements = requirements_file.read_text(encoding="utf-8")
        stamp = stamp_file.read_text(encoding="utf-8") if stamp_file.exists() else ""
        if not packages_due(requirements, stamp, today):
            return ""
        routes = [()] + [("--proxy", proxy) for proxy in FALLBACK_PROXIES]
        installed = any(run_pip(*route, "-r", REQUIREMENTS_FILE, cwd=folder) for route in routes)
        digest = hashlib.sha256(requirements.encode("utf-8")).hexdigest()
        stamp_file.write_text(f"{digest} {'ok' if installed else 'failed'} {today}\n", encoding="utf-8")
        return "packages: installed what the new version needs" if installed else "packages: install failed, trying again tomorrow"
    except Exception as error:
        return f"packages: skipped: {error}"


def check_once(folder: Path = CODE_DIR) -> None:
    """One full check: newest code, then the packages it needs. Used by the scheduled task and the open app."""
    log(pull_latest_version(folder))
    packages = install_new_packages(folder)
    if packages:
        log(packages)


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
        check_once()
