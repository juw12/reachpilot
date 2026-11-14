"""Measure "Try it" clicks without touching the project sites.

Every live-site link the bot posts (https://<user>.github.io/<name>/...) is swapped for a tracked
go-link, https://<user>.github.io/go/<name>/. That tiny page (committed to the <user>.github.io repo)
pings GoatCounter and forwards the visitor to the real site in well under a second. Link-preview
crawlers don't run JavaScript, so they are not counted.

GoatCounter (free, open source, privacy-friendly) stores the counts; the bot reads them back daily
through its API with a token kept in secrets.json.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

CREATE_NO_WINDOW = 0x08000000
RESERVED_NAMES = {"go", "p", "assets", "static", "demo"}
DEMO_REUSE_DAYS = 60  # a hosted clip is replaced only this often, so the Pages repo does not grow with every post
DEMO_LIVE_WAIT_SECONDS = 150  # GitHub Pages usually serves a pushed file within a minute or two

GO_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<meta name="robots" content="noindex">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="{title}">
<meta property="og:url" content="{target}">
<link rel="canonical" href="{target}">
<noscript><meta http-equiv="refresh" content="0;url={target}"></noscript>
<script>
(function () {{
  var target = "{target_js}";
  var done = false;
  function go() {{ if (!done) {{ done = true; location.replace(target); }} }}
  var ping = new Image();
  ping.onload = ping.onerror = go;
  ping.src = "https://{code}.goatcounter.com/count?p=" + encodeURIComponent("/go/{name}")
    + "&t=" + encodeURIComponent("{name}")
    + "&r=" + encodeURIComponent(document.referrer || "")
    + "&rnd=" + Math.random();
  setTimeout(go, 700);
}})();
</script>
<style>body{{font:16px system-ui,sans-serif;margin:3rem;color:#334155}}</style>
</head>
<body><p>Opening <a href="{target}">{title}</a>&hellip;</p></body>
</html>
"""


def go_url(user: str, name: str) -> str:
    return f"https://{user}.github.io/go/{name}/"


def find_site_links(text: str, user: str) -> dict[str, str]:
    """name -> original URL for every https://<user>.github.io/<name>/... link in the text."""
    pattern = re.compile(rf"https://{re.escape(user)}\.github\.io/([A-Za-z0-9_.-]+)/[^\s)\]]*", re.IGNORECASE)
    links = {}
    for match in pattern.finditer(text):
        name = match.group(1)
        if name.lower() not in RESERVED_NAMES:
            links.setdefault(name, match.group(0))
    return links


def rewrite_links(text: str, user: str, available: set[str]) -> str:
    for name, url in find_site_links(text, user).items():
        if name in available:
            text = text.replace(url, go_url(user, name))
    return text


def render_page(name: str, target: str, code: str) -> str:
    title = name.replace("-", " ").title()
    return GO_PAGE.format(
        title=html.escape(title),
        target=html.escape(target, quote=True),
        target_js=target.replace("\\", "\\\\").replace('"', '\\"'),
        code=code,
        name=name,
    )


def _git_exe() -> str:
    """Task Scheduler runs without Git on PATH, so look in the usual install folders too."""
    import os
    import shutil

    candidates = [
        shutil.which("git"),
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
        os.path.expandvars(r"%ProgramFiles%\Git\cmd\git.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Git\cmd\git.exe"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return candidate
    return "git"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_git_exe(), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=120,
        creationflags=CREATE_NO_WINDOW,
    )


def _pages_checkout(user: str, checkout: Path, log) -> bool:
    """Make sure the local copy of the <user>.github.io repo exists and is up to date."""
    if not checkout.exists():
        result = subprocess.run(
            ["gh", "repo", "clone", f"{user}/{user}.github.io", str(checkout), "--", "--depth", "1"],
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            log.warning("Could not clone %s.github.io: %s", user, result.stderr[-300:])
            return False
    _git(["pull", "--ff-only", "--quiet"], checkout)
    return True


def demo_url(user: str, name: str) -> str:
    return f"https://{user}.github.io/demo/{name}.mp4"


def _is_live(url: str) -> bool:
    try:
        return requests.head(url, timeout=30, allow_redirects=True).status_code == 200
    except requests.RequestException:
        return False


def host_demo_video(user: str, name: str, video: Path, checkout: Path, log) -> str:
    """Give the demo clip a public URL (demo/<name>.mp4 on the Pages site) for platforms that fetch media by URL.

    Returns the URL once it answers, or "" so the caller posts the image instead.
    """
    if not _pages_checkout(user, checkout, log):
        return ""
    url = demo_url(user, name)
    hosted = checkout / "demo" / f"{name}.mp4"
    recent = hosted.exists() and time.time() - hosted.stat().st_mtime < DEMO_REUSE_DAYS * 86400
    if recent and _is_live(url):
        return url
    if not recent:
        hosted.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(video, hosted)
        _git(["add", "demo"], checkout)
        _git(["commit", "-m", f"Demo video: {name}"], checkout)
    push = _git(["push", "--quiet"], checkout)
    if push.returncode != 0:
        log.warning("Pushing the demo video for %s failed: %s", name, push.stderr[-300:])
        return ""
    for _ in range(DEMO_LIVE_WAIT_SECONDS // 10):
        if _is_live(url):
            log.info("Demo video for %s is live at %s", name, url)
            return url
        time.sleep(10)
    log.warning("Demo video for %s was pushed but %s is not live yet", name, url)
    return ""


def sync_go_pages(user: str, targets: dict[str, str], code: str, checkout: Path, log) -> set[str]:
    """Write/update go/<name>/index.html in the <user>.github.io repo; one commit, only if changed."""
    if not _pages_checkout(user, checkout, log):
        return set()

    changed = []
    for name, target in targets.items():
        page = checkout / "go" / name / "index.html"
        content = render_page(name, target, code)
        if page.exists() and page.read_text(encoding="utf-8") == content:
            continue
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(content, encoding="utf-8", newline="\n")
        changed.append(name)

    if changed:
        _git(["add", "go"], checkout)
        commit = _git(["commit", "-m", f"Tracked go-links: {', '.join(sorted(changed))}"], checkout)
        push = _git(["push", "--quiet"], checkout)
        if push.returncode != 0:
            log.warning("Pushing go-links failed: %s", (push.stderr or commit.stderr)[-300:])
            return set()
        log.info("Published %d go-link page(s): %s", len(changed), ", ".join(sorted(changed)))

    existing = {path.parent.name for path in (checkout / "go").glob("*/index.html")}
    return existing


def check_goatcounter(code: str, token: str) -> str:
    response = requests.get(
        f"https://{code}.goatcounter.com/api/v0/me",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    user = response.json().get("user") or response.json()
    return user.get("email") or user.get("name") or "ok"


def fetch_go_clicks(code: str, token: str, day: str) -> dict[str, int]:
    """Clicks per go-link name for one day (YYYY-MM-DD)."""
    start = datetime.strptime(day, "%Y-%m-%d")
    end = start + timedelta(days=1) - timedelta(seconds=1)
    response = requests.get(
        f"https://{code}.goatcounter.com/api/v0/stats/hits",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        params={
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 100,
        },
        timeout=30,
    )
    response.raise_for_status()
    clicks: dict[str, int] = {}
    for hit in response.json().get("hits", []):
        path = hit.get("path") or ""
        if not path.startswith("/go/"):
            continue
        name = path.strip("/").split("/")[1] if path.count("/") >= 2 else ""
        if not name:
            continue
        count = hit.get("count")
        if count is None:
            count = sum(day_stat.get("daily", 0) for day_stat in hit.get("stats", []))
        clicks[name] = clicks.get(name, 0) + int(count or 0)
    return clicks


def load_goatcounter(config: dict, secrets_path: Path) -> tuple[str, str] | None:
    code = config.get("goatcounter_code", "").strip()
    if not code or not secrets_path.exists():
        return None
    token = json.loads(secrets_path.read_text(encoding="utf-8")).get("goatcounter_token", "")
    return (code, token) if token else None
