"""Thank the creators who covered a project, in the next post about it and on its watch page.

The outreach e-mails (the private Promote module, modules/promote) promise: "If you do cover it, I'll credit you on the
project page and share it with my LinkedIn network." This is the half that keeps that promise. A cover is recorded in
the dashboard ("They covered it" on the Promote tab); the module is asked for the list in its own process, so when it
is not installed, or fails, posts simply go out without a credit.

Nothing here contacts anyone: no direct messages, no mentions, only a thank-you line with the link to their video.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROMOTE_SCRIPT = Path(__file__).resolve().parent / "modules" / "promote" / "promote.py"
CREATE_NO_WINDOW = 0x08000000
NAME_CHARS = 60


def _promote(*arguments: str) -> str:
    if not PROMOTE_SCRIPT.exists():
        return ""
    try:
        result = subprocess.run(
            [sys.executable, str(PROMOTE_SCRIPT), *arguments], cwd=PROMOTE_SCRIPT.parent,
            capture_output=True, text=True, encoding="utf-8", timeout=60, creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout if result.returncode == 0 else ""


def coverage(repo: str | None = None) -> list[dict]:
    """Every recorded cover: [{"id", "creator", "url", "repo", "credited"}], for one repo when repo is given."""
    try:
        entries = json.loads(_promote("credits") or "[]")
    except ValueError:
        return []
    clean = []
    for entry in entries:
        name = " ".join(str(entry.get("creator") or "").split())[:NAME_CHARS]
        url = str(entry.get("url") or "")
        if name and url.startswith("https://") and (repo is None or entry.get("repo") == repo):
            clean.append({**entry, "creator": name, "url": url})
    return clean


def pending(repo: str) -> list[dict]:
    """Covers of this repo that no post has thanked yet."""
    return [entry for entry in coverage(repo) if not entry.get("credited")]


def credit(body: str, comment: str, entries: list[dict], project: str) -> tuple[str, str]:
    """The post with its thank-you: the names before the hashtags, the links where the post keeps its links."""
    if not entries:
        return body, comment
    names = [entry["creator"] for entry in entries]
    listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    thanks = f"Thanks to {listed} for covering {project}."
    links = "\n".join(f"{entry['creator']}: {entry['url']}" for entry in entries)
    if comment:
        comment = f"{comment}\n{links}"
    else:
        thanks = f"{thanks}\n{links}"
    text, separator, tags = body.rpartition("\n\n")
    if separator and tags.startswith("#"):
        return f"{text}\n\n{thanks}\n\n{tags}", comment
    return f"{body}\n\n{thanks}", comment


def mark(entries: list[dict]) -> None:
    """Remember that these covers were thanked, so the next post does not repeat it."""
    for entry in entries:
        _promote("action", "credited", str(entry["id"]))
