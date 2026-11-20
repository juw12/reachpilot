"""ReachPilot hub: brings the private modules into the dashboard.

Modules live in modules/ (git-ignored, personal data) and keep running on their own schedules:
  jobs         company job boards -> scored tracker, briefs, autofill apply sessions
  email_apply  JobStreet / LinkedIn ads with a recruiter e-mail -> CV + cover letter via the Gmail relay
  job_digest   daily digest + Gmail relay credentials used by email_apply
  promote      creator / UGC outreach e-mails for the GitHub projects (modules/promote/promote.py)
  podcast      "Bits & Tickers" episodes (a daily and a weekly series), published to an RSS feed (modules/podcast/podcast.py)

Other platforms (Bluesky, Mastodon, Dev.to, Telegram, Discord) are part of the public code
(channels.py); channels_section() reads their state from state.db.
Launch tests (launch_section) are read from a project's public summary.json, plus private steps from
launch_private.json (git-ignored) when that file exists. Where launch_auto.py is installed its rows join them:
steps this PC does by itself (watch for an answer, one follow-up, approved invitations, daily proposal checks).
The patent screen (patents_section) reads the last daily scan written by patents.py into data/patents/
(git-ignored); it shows only where that folder exists. The patent desk (patent_desk.py) follows the screen's
candidates from there: readiness, the filing package, and the dates before and after a filing.

The inbox card (inbox_section) shows what a mailbox holds that only I can deal with. It reads
inbox_private.json (git-ignored), written by whatever went through that mailbox; it shows only where that file exists.
What I answer to a mail on the card or in To do (inbox_action) is kept in inbox_answers.json (git-ignored): the card,
To do and whatever went through the mailbox all read that one file.

The "This PC" card (upkeep_section) shows the housekeeping jobs of this PC that have nothing to do with promotion but
that I want to see in the same place: a clean-up, a backup, a sync. It reads upkeep_private.json (git-ignored), which
names the scheduled tasks and where each one leaves its status; it shows only where that file exists.

This file only reads their files, builds the "what I need from you" list, and runs a short whitelist of actions.
Every reader is defensive: a missing or half-written file shows as "not set up" instead of breaking the dashboard.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

import machine

ROOT = Path(__file__).resolve().parent
MODULES = ROOT / "modules"
JOBS = MODULES / "jobs"
EMAIL = MODULES / "email_apply"
DIGEST = MODULES / "job_digest"
PROMOTE = MODULES / "promote"
PODCAST = MODULES / "podcast"
TASK_FOLDER = "\\ReachPilot\\"
TASKS = {
    "linkedin": "LinkedIn posts",
    "jobs": "Job search",
    "email": "Email applications",
    "bigtech": "Big Tech watch",
    "promote": "Promotion",
    "updates": "Updates",
}

def podcast_tasks(folder: Path) -> dict[str, str]:
    """One scheduled task per podcast series that has run days, named as podcast.py's task_name() names it.
    A series with no days is paused and has no task. Unreadable config: the three original series."""
    try:
        series = json.loads((folder / "podcast_config.json").read_text(encoding="utf-8")).get("series") or {}
    except (ValueError, OSError, AttributeError):
        series = {}
    active = [key for key, spec in series.items() if isinstance(spec, dict) and spec.get("days")]
    return {f"podcast_{key}": f"Podcast {key.capitalize()}" for key in active or ("brief", "breakdown", "drive")}


if (PODCAST / "podcast.py").exists():  # only where the private podcast module is present
    TASKS.update(podcast_tasks(PODCAST))
if (ROOT / "data" / "patents").exists():  # only where `python patents.py install` has been run
    TASKS["patents"] = "Patent screen"
if (ROOT / "data" / "launch_auto").exists():  # only where `python launch_auto.py install` has been run
    TASKS["launch"] = "Launch automations"
if (ROOT / "data" / "shorts").exists():  # only where `python shorts.py install` has been run
    TASKS["shorts"] = "Shorts"
if (ROOT / "data" / "replies").exists():  # only where `python replies.py install` has been run
    TASKS["replies"] = "Replies"
PYTHONW = machine.hidden_python()  # pythonw.exe on Windows; the running Python on a Mac
PYTHON = machine.console_python()
NO_WINDOW = machine.NO_WINDOW

_cache: dict[str, tuple[float, object]] = {}


def cached(key: str, seconds: float, build):
    """Small time-based cache so the 60-second dashboard refresh stays cheap."""
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < seconds:
        return hit[1]
    value = build()
    _cache[key] = (now, value)
    return value


def file_age_hours(path: Path) -> float | None:
    return (time.time() - path.stat().st_mtime) / 3600 if path.exists() else None


def stamp(path: Path) -> str | None:
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M") if path.exists() else None


# ------------------------------------------------------------------ scheduled tasks

def _task_row(name: str, folder: str = TASK_FOLDER) -> dict:
    """Last/next run and last result of one scheduled task (schtasks; launchd on a Mac)."""
    if machine.IS_MAC:
        return machine.task_info(name)
    try:
        output = subprocess.run(
            ["schtasks", "/query", "/tn", folder + name, "/fo", "CSV", "/v"],
            capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
        ).stdout
        rows = list(csv.DictReader(output.splitlines()))
        unknown = False
    except Exception:
        # No answer (a busy PC makes schtasks miss the 10 seconds) is not the same as "not installed".
        rows, unknown = [], True
    if not rows:
        return {"name": name, "installed": False, "unknown": unknown}
    row = rows[0]
    last_result = row.get("Last Result", "").strip()
    return {
        "name": name,
        "installed": True,
        "state": row.get("Status", "").strip(),
        # 267011 = never run: schtasks then reports a placeholder date (30 Nov 1999), so show nothing
        "last_run": "" if last_result == "267011" else _pretty_task_time(row.get("Last Run Time", "")),
        "next_run": _pretty_task_time(row.get("Next Run Time", "")),
        "last_result": last_result,
        "ok": last_result in ("0", "267009", "267011", "267014"),  # success, running, never run, terminated by us
    }


def task_status() -> dict:
    """Last/next run and last result of every ReachPilot task (schtasks; launchd on a Mac; cached 2 minutes)."""
    return cached("tasks", 120, lambda: {key: _task_row(name) for key, name in TASKS.items()})


def _pretty_task_time(raw: str) -> str:
    """Turn schtasks' locale time (e.g. '11/7/2026 12:30:00 PM') into 'Sat 7 Nov · 12:30'."""
    from datetime import datetime as _dt
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return machine.day_label(_dt.strptime(raw, fmt))
        except ValueError:
            continue
    return raw


# ------------------------------------------------------------------ jobs module

AUTO_APPLY_KINDS = {"APPLIED": "applied", "needs you": "needs_you", "later": "later", "already": "already", "ready": "ready"}
AUTO_APPLY_LINE = re.compile(r"^  (left for you|trying|APPLIED|needs you|later|ready|already)\s+(?:\d+|dig)\s+(.*)$")
AUTO_APPLY_HEADER = re.compile(r"^Auto-apply (CRASHED )?(\d{4}-\d\d-\d\d \d\d:\d\d)( \(dry run\))?")


def auto_apply_view(folder: Path) -> dict | None:
    """What the scheduled auto-apply (modules/jobs/auto_apply.py) did, read from the two files it writes:
    logs/auto_apply.log (one block per run) and digest_apply_state.json (its queue from the Big tech mail).
    Real runs only, newest first. None where it has never run, so the Jobs tab has no Auto-apply card there.
    digest.done lists every job of the mail that is settled (applied, already applied, not applied and why),
    whoever settled it: a scheduled run or an application made outside one. The log only knows the runs."""
    log = folder / "logs" / "auto_apply.log"
    try:
        with log.open("rb") as handle:
            handle.seek(max(0, log.stat().st_size - 200_000))
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
        fresh = time.time() - log.stat().st_mtime < 1800
    except OSError:
        return None

    def new_run(when: str, dry: bool = False) -> dict:
        return {"when": when, "dry": dry, "crashed": "", "submitted": None, "results": [], "left": [], "notes": []}

    runs: list[dict] = []
    trying, in_crash = "", False
    for line in lines:
        header = AUTO_APPLY_HEADER.match(line)
        if header and not header.group(1):
            runs.append(new_run(header.group(2), bool(header.group(3))))
            trying, in_crash = "", False
            continue
        if header:  # the crash marker closes the run it interrupted; a finished run before it is left alone
            if not runs or runs[-1]["submitted"] is not None or runs[-1]["crashed"]:
                runs.append(new_run(header.group(2)))
            runs[-1]["crashed"] = header.group(2)
            in_crash = True
            continue
        if not runs or in_crash:  # in_crash: the traceback under the marker
            continue
        run = runs[-1]
        footer = re.match(r"Submitted (\d+) application", line)
        if footer:
            run["submitted"] = int(footer.group(1))
            continue
        entry = AUTO_APPLY_LINE.match(line)
        if not entry:
            if line.startswith("  (") or "stay queued" in line:
                run["notes"].append(line.strip().strip("()")[:240])
            continue
        kind, rest = entry.group(1), entry.group(2).strip()
        if kind == "trying":
            trying = rest
        elif kind == "left for you":
            job, _, reason = rest.partition(": ")
            run["left"].append((job.strip(), reason.strip()))
        else:  # the line repeats the label of the "trying" line above it, then says how it ended
            if trying and rest.startswith(trying):
                job, detail = trying, rest[len(trying):]
            else:
                job, _, detail = rest.partition(": ")
            run["results"].append({"when": run["when"], "kind": AUTO_APPLY_KINDS[kind], "job": job.strip(),
                                   "detail": detail.strip(" :")[:300]})

    runs = [run for run in runs if not run["dry"]][::-1]
    if not runs:
        return None
    for position, run in enumerate(runs):
        if run["crashed"]:
            run["state"] = "crashed"
        elif run["submitted"] is not None:
            run["state"] = "finished"
        else:  # no closing line: still going if the log moved in the last half hour, otherwise it was cut off
            run["state"] = "running" if position == 0 and fresh else "stopped"
        run["counts"] = {name: sum(1 for row in run["results"] if row["kind"] == name) for name in AUTO_APPLY_KINDS.values()}

    reasons: dict[str, list[str]] = {}
    for job, reason in runs[0]["left"]:
        names = reasons.setdefault(re.split(r"[:;]", reason)[0].strip() or "no reason given", [])
        if job not in names:
            names.append(job)
    left = sorted(({"reason": reason, "jobs": names} for reason, names in reasons.items()), key=lambda item: -len(item["jobs"]))

    digest = {"counts": {}, "queued": [], "needs_you": [], "done": []}
    try:
        state = json.loads((folder / "digest_apply_state.json").read_text(encoding="utf-8"))
        entries = [entry for entry in state.values() if isinstance(entry, dict)]
    except (ValueError, OSError, AttributeError):
        entries = []
    waiting: dict[str, list[str]] = {}
    for entry in entries:
        status = str(entry.get("status") or "queued")
        digest["counts"][status] = digest["counts"].get(status, 0) + 1
        if status == "queued":
            waiting.setdefault(str(entry.get("company") or "?"), []).append(str(entry.get("title") or ""))
        elif status == "needs_you":
            digest["needs_you"].append({"company": str(entry.get("company") or ""), "title": str(entry.get("title") or ""),
                                        "detail": str(entry.get("detail") or "")[:300], "url": entry.get("url") or ""})
        elif status in ("applied", "already", "skipped"):
            digest["done"].append({"company": str(entry.get("company") or ""), "title": str(entry.get("title") or ""),
                                   "status": status, "detail": str(entry.get("detail") or "")[:300],
                                   "date": str(entry.get("date") or ""), "url": entry.get("url") or ""})
    settled_order = {"applied": 0, "already": 1, "skipped": 2}
    digest["done"].sort(key=lambda job: (settled_order[job["status"]], job["company"], job["title"]))
    digest["done"].sort(key=lambda job: job["date"], reverse=True)  # newest day first; within a day, applied first
    digest["done"] = digest["done"][:60]
    digest["queued"] = sorted(({"company": company, "titles": titles} for company, titles in waiting.items()),
                              key=lambda item: (-len(item["titles"]), item["company"]))
    return {
        "runs": [{"when": run["when"], "state": run["state"], "crashed": run["crashed"], "submitted": run["submitted"],
                  "counts": run["counts"], "left": len({job for job, _ in run["left"]}), "notes": run["notes"]} for run in runs[:6]],
        "results": [row for run in runs[:3] for row in run["results"]][:40],
        "left": left,
        "digest": digest,
    }


def jobs_section() -> dict:
    tracker = JOBS / "jobs_tracker.xlsx"
    if not tracker.exists():
        return {"installed": False}

    def build():
        from openpyxl import load_workbook

        sheet = load_workbook(tracker)["Jobs"]
        headers = [cell.value for cell in sheet[1]]
        rows = []
        for cells in sheet.iter_rows(min_row=2):
            row = dict(zip(headers, (cell.value for cell in cells)))
            link = cells[headers.index("Link")]
            row["Link"] = link.hyperlink.target if link.hyperlink else None
            rows.append(row)

        today = date.today()
        week_ago = today - timedelta(days=7)

        def as_date(value):
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            try:
                return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                return None

        def brief(row):
            return {
                "score": row.get("Score"), "company": row.get("Company"), "title": row.get("Title"),
                "location": row.get("Location"), "status": row.get("Status"), "link": row.get("Link"),
                "notes": row.get("Notes") or "", "my_notes": row.get("My notes") or "",
                "source": str(row.get("Key", "")).split(":", 1)[0],
                "applied_on": str(as_date(row.get("Applied on")) or ""),
            }

        open_rows = [row for row in rows if row.get("Still open") == "Yes"]
        queue = sorted(
            (row for row in open_rows if row.get("Status") in ("New", "Shortlist")),
            key=lambda row: (row.get("Status") != "Shortlist", -(row.get("Score") or 0)),
        )
        applied = [row for row in rows if row.get("Status") in ("Applied", "Interview", "Offer")]
        follow_ups = []
        for row in rows:
            if row.get("Status") != "Applied":
                continue
            applied_on = as_date(row.get("Applied on"))
            if applied_on and applied_on + timedelta(days=7) <= today:
                follow_ups.append(brief(row))
        needs_you = [brief(row) for row in open_rows
                     if re.search(r"not submitted|needs you", str(row.get("My notes") or ""), re.I)
                     and row.get("Status") in ("New", "Shortlist")]
        status_counts: dict[str, int] = {}
        for row in rows:
            status_counts[row.get("Status") or "New"] = status_counts.get(row.get("Status") or "New", 0) + 1

        daily_target = 20
        try:
            daily_target = json.loads((JOBS / "config.json").read_text(encoding="utf-8")).get("daily_target", 20)
        except Exception:
            pass
        todays = queue[:daily_target]
        workday_companies = sorted({row.get("Company") for row in todays if str(row.get("Key", "")).startswith("workday:")})
        blocked_companies = sorted({row.get("Company") for row in todays if str(row.get("Key", "")).startswith("smartrecruiters:")})

        last_log = JOBS / "logs" / "last_run.log"
        summary, last_error = "", ""
        if last_log.exists():
            log_text = last_log.read_text(encoding="utf-8", errors="replace")
            for line in log_text.splitlines():
                if "openings scanned" in line:
                    summary = line.strip()
            if "No network route" in log_text:
                last_error = "The last search couldn't reach the internet (proxy / network down), so no new jobs were added."
            elif "Traceback" in log_text:
                last_error = "The last search crashed; see logs/last_run.log."
        fill_me = []
        answers = JOBS / "my_answers.txt"
        if answers.exists():
            fill_me = [line.split("|")[0].strip() for line in answers.read_text(encoding="utf-8").splitlines()
                       if "FILL ME" in line and "|" in line and not line.startswith("#")]
        try:  # the auto-apply card is extra: a log it cannot read must not take the Jobs tab down
            auto_apply = auto_apply_view(JOBS)
        except Exception:
            auto_apply = None
        found_by_day = count_by_day(as_date(row.get("First seen")) for row in rows)
        applied_by_day = count_by_day(as_date(row.get("Applied on")) for row in applied)
        return {
            "installed": True,
            "found_by_day": found_by_day,
            "applied_by_day": applied_by_day,
            "daily_target": daily_target,
            "tracked": len(rows),
            "open": len(open_rows),
            "queue_total": len(queue),
            "today": [brief(row) for row in todays],
            "applied_total": len(applied),
            "applied_week": sum(1 for row in applied if (as_date(row.get("Applied on")) or date.min) >= week_ago),
            "applied": sorted((brief(row) for row in applied), key=lambda item: item["applied_on"], reverse=True)[:30],
            "follow_ups": follow_ups,
            "needs_you": needs_you,
            "status_counts": status_counts,
            "workday_companies": workday_companies,
            "blocked_companies": blocked_companies,
            "last_run": stamp(last_log),
            "last_summary": summary,
            "last_error": last_error,
            "fill_me": fill_me,
            "tracker": str(tracker),
            "auto_apply": auto_apply,
        }

    return cached(f"jobs:{tracker.stat().st_mtime}", 60, build)


# ------------------------------------------------------------------ e-mail applications module

def email_section() -> dict:
    log_csv = EMAIL / "applications_log.csv"
    if not log_csv.exists():
        return {"installed": False}

    def build():
        with open(log_csv, encoding="utf-8-sig", newline="") as handle:
            sends = [row for row in csv.DictReader(handle) if row.get("at")]
        today = date.today()
        by_day: dict[str, int] = {}
        regions: dict[str, int] = {}
        for row in sends:
            day = row["at"][:10]
            by_day[day] = by_day.get(day, 0) + 1
            region = row.get("region") or "Other"
            regions[region] = regions.get(region, 0) + 1
        series = []
        for offset in range(13, -1, -1):
            day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
            series.append({"x": day, "y": by_day.get(day, 0)})
        week_start = (today - timedelta(days=6)).strftime("%Y-%m-%d")

        leads, leads_total, leads_by_day = [], 0, {}
        state_path = EMAIL / "state.json"
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                leads_by_day = count_by_day(str(item.get("found") or "")[:10] for item in state.get("site_queue", {}).values())
                queued = [item for item in state.get("site_queue", {}).values() if item.get("status") == "queued"]
                leads_total = len(queued)
                leads = sorted(queued, key=lambda item: -(item.get("fit") or 0))[:15]
            except (ValueError, OSError):
                pass

        config = {}
        try:
            config = json.loads((EMAIL / "auto_apply_config.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
        recent = [
            {"at": row["at"].replace("T", " ")[:16], "company": row.get("company"), "title": row.get("title"),
             "region": row.get("region"), "source": row.get("source"), "url": row.get("url"),
             "to_domain": (row.get("to") or "").split("@")[-1]}
            for row in reversed(sends[-40:])
        ]
        return {
            "installed": True,
            "sent_by_day": by_day,
            "leads_by_day": leads_by_day,
            "sent_total": len(sends),
            "sent_today": by_day.get(today.strftime("%Y-%m-%d"), 0),
            "sent_week": sum(count for day, count in by_day.items() if day >= week_start),
            "series": series,
            "regions": sorted(regions.items(), key=lambda pair: -pair[1])[:8],
            "recent": recent,
            "leads_total": leads_total,
            "leads": [{"title": item.get("title"), "company": item.get("company"), "region": item.get("region"),
                       "url": item.get("url"), "fit": item.get("fit"), "salary": item.get("salary")} for item in leads],
            "relay_ok": (EMAIL / "relay_v2_ok.flag").exists(),
            "followups_enabled": bool(config.get("followup", {}).get("enabled")),
            "followup_after_days": config.get("followup", {}).get("after_days"),
            "max_per_day": config.get("max_emails_per_day"),
            "report_to": config.get("report_to"),
            "last_run": stamp(EMAIL / "auto_apply.log"),
            "last_run_age_hours": file_age_hours(EMAIL / "auto_apply.log"),
            "bigtech_last_run": stamp(EMAIL / "bigtech_watch.log"),
            "log_tail": tail(EMAIL / "auto_apply.log", 25),
        }

    return cached(f"email:{log_csv.stat().st_mtime}:{int(time.time() // 120)}", 60, build)


# ------------------------------------------------------------------ the inbox card (Email page)

INBOX_PRIVATE = ROOT / "inbox_private.json"
INBOX_ANSWERS = ROOT / "inbox_answers.json"
INBOX_GROUPS, INBOX_ITEMS = 12, 40
INBOX_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
INBOX_NOTE = 300
# What a mail asks decides what I can answer. "Not me" settles nothing: the mail stays open until I press "Secured".
INBOX_ASKS = {"confirm": ("mine", "not_mine"), "do": ("done", "skip"), "know": ("seen",)}
INBOX_NEXT = {"not_mine": ("secured",)}
INBOX_LABELS = {"mine": "It was me", "not_mine": "Not me", "secured": "Secured", "done": "Done", "skip": "Won't do",
                "seen": "Seen"}
_inbox_lock = threading.Lock()
INBOX_ADDRESS = re.compile(r"[^\s@,;]+@[^\s@,;]+\.[^\s@,;]+")
INBOX_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
INBOX_MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?")


def _inbox_text(value, limit: int) -> str:
    """One line of plain text, cut to `limit`. Mail is written by strangers: anything that is not text becomes ''."""
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _inbox_when(value, shape: re.Pattern) -> str:
    """A date the page can print ('2026-11-19', or with ' 10:05'); '' for anything else."""
    text = _inbox_text(value, 16)
    return text if shape.fullmatch(text) else ""


def _inbox_count(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _inbox_answers() -> dict:
    """What I answered, from inbox_answers.json (git-ignored):
    {"answers": {"<key>": {"choice": "mine", "note": "...", "about": "<date>|<title>", "at": "2026-11-19 10:31"}}}"""
    try:
        answers = json.loads(INBOX_ANSWERS.read_text(encoding="utf-8-sig")).get("answers")
    except (OSError, ValueError, AttributeError):
        return {}
    return {key: answer for key, answer in answers.items() if isinstance(answer, dict)} if isinstance(answers, dict) else {}


def _inbox_save(answers: dict) -> None:
    """Whatever reads the answers never sees a half-written file."""
    temporary = INBOX_ANSWERS.with_suffix(".tmp")
    temporary.write_text(json.dumps({"answers": answers}, indent=1, ensure_ascii=False), encoding="utf-8")
    temporary.replace(INBOX_ANSWERS)


def _inbox_about(item: dict) -> str:
    """An answer belongs to one mail. A later mail under the same key has another date or title, so it is open again."""
    return f"{item['date']}|{item['title']}"


def inbox_section() -> dict:
    """What a mailbox holds that only I can deal with, from inbox_private.json (git-ignored):

    {"account": "me@gmail.com", "checked_at": "2026-11-19 10:05", "read": 168, "still_unread": 0, "note": "...",
     "groups": [{"title": "Money", "priority": 1, "note": "...",
                 "items": [{"title": "...", "detail": "...", "from": "...", "date": "2026-11-17",
                            "unread": true, "search": "subject:(...)", "key": "shop-payout", "ask": "do"}]}]}

    Whatever went through the mailbox writes that file; this only reads it. Every text is cut to a length and shown
    as text. A link is never taken from the file: it is built here, from the account and the item's own search words,
    and always opens Gmail's search. Not set up where the file is missing or is not this shape.

    A mail with a "key" can be answered (inbox_action). "ask" says what it wants: "confirm" (was this you?), "do"
    (something only I can do), anything else is only to know. Each mail comes back with my answer and note, "open"
    (still waits for me) and the answers that can be given now, as action names.
    """
    try:
        data = json.loads(INBOX_PRIVATE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"installed": False}
    if not isinstance(data, dict) or not isinstance(data.get("groups"), list):
        return {"installed": False}
    account = _inbox_text(data.get("account"), 120)
    opens = "https://mail.google.com/mail/?authuser=" + urllib.parse.quote(account, safe="@") + "#search/" \
        if INBOX_ADDRESS.fullmatch(account) else None

    answers = _inbox_answers()
    groups = []
    for group in data["groups"][:INBOX_GROUPS]:
        if not isinstance(group, dict) or not isinstance(group.get("items"), list):
            continue
        items = []
        for item in group["items"][:INBOX_ITEMS]:
            title = _inbox_text(item.get("title"), 160) if isinstance(item, dict) else ""
            if not title:
                continue
            search = _inbox_text(item.get("search"), 200)
            key = _inbox_text(item.get("key"), 40)
            key = key if INBOX_KEY.fullmatch(key) else ""
            ask = item.get("ask") if item.get("ask") in ("confirm", "do") else "know"
            mail = {
                "title": title,
                "detail": _inbox_text(item.get("detail"), 500),
                "sender": _inbox_text(item.get("from"), 80),
                "date": _inbox_when(item.get("date"), INBOX_DAY),
                "unread": item.get("unread") is True,
                "url": opens + urllib.parse.quote(search, safe="") if opens and search else None,
                "key": key,
                "ask": ask,
            }
            mine = answers.get(key) or {}
            mine = mine if key and mine.get("about") == _inbox_about(mail) else {}
            choice = mine.get("choice") if mine.get("choice") in INBOX_LABELS else None
            can_answer = () if not key else INBOX_NEXT.get(choice, ()) if choice else INBOX_ASKS[ask]
            mail.update({
                "answer": {"choice": choice, "label": INBOX_LABELS[choice],
                           "at": _inbox_when(mine.get("at"), INBOX_MOMENT)} if choice else None,
                "note": _inbox_text(mine.get("note"), INBOX_NOTE),
                "open": bool(key) and (choice is None or choice in INBOX_NEXT),
                "choices": [{"name": f"inbox:answer:{key}:{name}", "label": INBOX_LABELS[name]} for name in can_answer],
            })
            items.append(mail)
        if items:
            groups.append({
                "title": _inbox_text(group.get("title"), 80) or "Other",
                "priority": group.get("priority") if group.get("priority") in (1, 2, 3) else 2,
                "note": _inbox_text(group.get("note"), 300),
                "items": items,
                "seen_all": f"inbox:seen:{len(groups)}",
            })
    return {
        "installed": True,
        "account": account,
        "checked_at": _inbox_when(data.get("checked_at"), INBOX_MOMENT),
        "read": _inbox_count(data.get("read")),
        "still_unread": _inbox_count(data.get("still_unread")),
        "note": _inbox_text(data.get("note"), 400),
        "groups": groups,
        "kept_unread": sum(item["unread"] for group in groups for item in group["items"]),
        "open": sum(item["open"] for group in groups for item in group["items"]),
        "answered": sum(bool(item["key"]) and not item["open"] for group in groups for item in group["items"]),
    }


def inbox_todo(section: dict | None) -> list[dict]:
    """One line in To do per group of the inbox card that still holds a mail for me, with those mails as links and
    their answers as buttons. An urgent group: every mail I have not answered. Another group: only the unanswered mails
    that ask for something. A mail I said was not me is urgent wherever it sits."""
    if not section or not section.get("installed"):
        return []
    todo = []
    for group in section["groups"]:
        urgent = group["priority"] == 1
        waiting = [item for item in group["items"]
                   if (item["open"] and (urgent or item["ask"] != "know")) or (urgent and not item["key"])]
        if not waiting:
            continue
        alarm = any(item["answer"] and item["answer"]["choice"] == "not_mine" for item in waiting)
        todo.append({
            "priority": 1 if urgent or alarm else 2, "module": "Email", "title": f"Inbox: {group['title']}",
            "detail": group["note"] or f"{len(waiting)} mail(s) in {section['account'] or 'the inbox'} that only you can deal with.",
            "action": None, "action_label": None,
            "links": [{"label": item["title"], "url": item["url"], "note": item["detail"], "actions": item["choices"]}
                      for item in waiting],
        })
    return todo


def inbox_action(name: str) -> str:
    """name = 'inbox:answer:<key>:<choice>', 'inbox:reopen:<key>', 'inbox:note:<key>|<text>' (the text keeps its
    own colons) or 'inbox:seen:<group number>' (every mail of that group that is only to know). Only an answer that
    the card offers for that mail right now is taken. It writes inbox_answers.json and nothing else: no mail is
    opened, answered, moved or deleted by an answer."""
    _, _, rest = name.partition(":")
    verb, _, argument = rest.partition(":")
    key, _, value = argument.partition("|" if verb == "note" else ":")
    with _inbox_lock:
        groups = inbox_section().get("groups", [])
        answers = _inbox_answers()
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        if verb == "seen":
            if not argument.isdigit() or int(argument) >= len(groups):
                raise ValueError(f"Unknown action: {name}")
            seen = [item for item in groups[int(argument)]["items"]
                    if item["open"] and item["ask"] == "know" and not item["answer"]]
            if not seen:
                raise ValueError(f"Unknown action: {name}")
            for item in seen:
                answers[item["key"]] = {"choice": "seen", **({"note": item["note"]} if item["note"] else {}),
                                        "about": _inbox_about(item), "at": now}
            _inbox_save(answers)
            return f"Saved: {len(seen)} mail(s) seen."
        mail = next((item for group in groups for item in group["items"] if key and item["key"] == key), None)
        if mail is None:
            raise ValueError(f"Unknown action: {name}")
        mine = {field: text for field, text in (("choice", mail["answer"] and mail["answer"]["choice"]), ("note", mail["note"]))
                if text}
        if verb == "answer" and f"inbox:answer:{key}:{value}" in [choice["name"] for choice in mail["choices"]]:
            mine["choice"] = value
            message = f"Saved: {INBOX_LABELS[value]}."
            if value in INBOX_NEXT:
                message += " Change that account's password now, then press Secured."
        elif verb == "reopen" and not value and mail["answer"]:
            del mine["choice"]
            message = "Open again."
        elif verb == "note":
            mine["note"] = _inbox_text(value, INBOX_NOTE)
            message = "Note saved." if mine["note"] else "Note removed."
            if not mine["note"]:
                del mine["note"]
        else:
            raise ValueError(f"Unknown action: {name}")
        if mine:
            answers[key] = {**mine, "about": _inbox_about(mail), "at": now}
        else:
            answers.pop(key, None)
        _inbox_save(answers)
    return message


def tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    with open(path, "rb") as handle:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - 20000))
        text = handle.read().decode("utf-8", errors="replace")
    return text.splitlines()[-lines:][::-1]


# ------------------------------------------------------------------ promote module

PROMOTE_SCRIPT = PROMOTE / "promote.py"
PROMOTE_VERBS = {"approve", "approveall", "send", "sendall", "skip", "retry", "retryall", "mark", "mode", "pause", "resume", "plan", "inbox",
                 "discover", "tick", "listings", "listingdone", "covered", "credited"}
PROMOTE_SEND_ALL_SECONDS = 420  # 'Send all now' sends one e-mail after another; the module starts no new one after 240 s
PROMOTE_BACKGROUND = {"discover", "inbox", "tick", "listings"}  # slow (network): run without blocking the dashboard


def promote_section() -> dict:
    """The module runs in its own process (own imports, own SQLite), so a problem there never breaks the hub."""
    if not PROMOTE_SCRIPT.exists():
        return {"installed": False, "folder": str(PROMOTE)}

    def build():
        output = subprocess.run(
            [str(PYTHON), str(PROMOTE_SCRIPT), "summary"], cwd=PROMOTE,
            capture_output=True, text=True, encoding="utf-8", timeout=60, creationflags=NO_WINDOW,
        )
        if output.returncode != 0:
            return {"installed": False, "error": (output.stderr or "summary failed").strip()[-300:], "folder": str(PROMOTE)}
        return json.loads(output.stdout)

    db_file = PROMOTE / "state.db"
    version = int(db_file.stat().st_mtime) if db_file.exists() else 0
    return cached(f"promote:{version}", 30, build)


def promote_action(name: str) -> str:
    """name = 'promote:<verb>[:<argument>]', e.g. promote:send:12, promote:mark:yt:UC123|interested
    or promote:covered:yt:UC123|https://youtu.be/... (the argument keeps its own colons)."""
    _, _, rest = name.partition(":")
    verb, _, argument = rest.partition(":")
    if verb == "setkey":
        _console([str(PYTHON), str(PROMOTE_SCRIPT), "set-youtube-key"], PROMOTE)
        return "A window opened: paste your YouTube API key there."
    if verb == "setupchecker":
        _console([str(PYTHON), str(PROMOTE_SCRIPT), "setup-checker"], PROMOTE)
        return "A window opened with the 3-minute reply-checker setup."
    if verb == "openproducts":
        machine.open_path(PROMOTE / "products.json")
        return "products.json opened."
    if verb not in PROMOTE_VERBS:
        raise ValueError(f"Unknown action: {name}")
    command = [str(PYTHON), str(PROMOTE_SCRIPT), "action", verb] + ([argument] if argument else [])
    for key in [key for key in _cache if key.startswith("promote:")]:
        _cache.pop(key, None)
    if verb in PROMOTE_BACKGROUND:
        subprocess.Popen([str(PYTHONW)] + command[1:], cwd=PROMOTE, creationflags=NO_WINDOW)
        return {"discover": "Searching YouTube for creators in the background; results appear in a few minutes.",
                "inbox": "Checking for replies in the background.",
                "tick": "Promotion run started in the background.",
                "listings": "Checking the directory listings in the background; refresh in a minute."}[verb]
    output = subprocess.run(command, cwd=PROMOTE, capture_output=True, text=True, encoding="utf-8",
                            timeout=PROMOTE_SEND_ALL_SECONDS if verb == "sendall" else 180, creationflags=NO_WINDOW)
    if output.returncode != 0:
        raise RuntimeError((output.stderr or "promote action failed").strip().splitlines()[-1])
    return (output.stdout.strip().splitlines() or ["Done."])[-1]


# ------------------------------------------------------------------ podcast module

def _podcast_series_next_run(series: dict, now: datetime) -> str:
    """Next time a series runs from its weekday + time schedule, e.g. 'Wed 12 Nov · 07:30'."""
    weekdays = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
    wanted = {weekdays[str(day).lower()] for day in series.get("days") or [] if str(day).lower() in weekdays}
    try:
        hour, minute = (int(part) for part in str(series.get("time") or "0:0").split(":")[:2])
    except ValueError:
        return ""
    day = now.date()
    for _ in range(8):  # 8 days: on the series' own weekday after its time, the next run is a week later
        if day.weekday() in wanted:
            candidate = datetime(day.year, day.month, day.day, hour, minute)
            if candidate > now:
                return machine.day_label(candidate)
        day += timedelta(days=1)
    return ""


def podcast_section() -> dict:
    """Reads the files the podcast module already keeps, with the same rules as its load_episodes() and
    pending_episodes(), so a missing or half-written file shows as 'not set up' instead of breaking the hub."""
    script = PODCAST / "podcast.py"
    if not script.exists():
        return {"installed": False}

    def build():
        config = {}
        try:
            config = json.loads((PODCAST / "podcast_config.json").read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
        now = datetime.now()
        published, held = [], []
        for record in sorted((PODCAST / "episodes").glob("*/episode.json")):
            try:
                episode = json.loads(record.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            if not isinstance(episode, dict) or not episode.get("file"):
                continue
            if not (PODCAST / "site" / "audio" / episode["file"]).exists():
                continue  # audio not on the site yet: not on the feed and not in the review queue
            item = {
                "slug": episode.get("slug") or record.parent.name,
                "number": episode.get("number"),
                "title": episode.get("title"),
                "series_name": episode.get("series_name"),
                "published": episode.get("published"),
                "seconds": episode.get("seconds"),
            }
            if episode.get("status", "published") == "published":
                published.append(item)
            elif episode.get("status") == "pending":
                held.append(item)
        published.sort(key=lambda item: item["published"] or "", reverse=True)
        held.sort(key=lambda item: item["published"] or "")
        last_run = {}
        last_run_file = PODCAST / "last_run.json"
        if last_run_file.exists():
            try:
                loaded = json.loads(last_run_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    last_run = {key: loaded.get(key) for key in
                                ("ok", "title", "seconds", "published", "held", "awaiting_review", "finished")
                                if loaded.get(key) is not None}
            except (ValueError, OSError):
                pass
        publish = config.get("publish") or {}
        base_url = str(publish.get("base_url") or "").rstrip("/")
        return {
            "installed": True,
            "show_title": config.get("show_title"),
            "tagline": config.get("tagline"),
            "latest": published[0] if published else None,
            "held": held,
            "episodes_total": len(published),
            "published_by_day": count_by_day(item["published"] for item in published),
            "review": bool(publish.get("review")),
            "publish_enabled": bool(publish.get("enabled")),
            "site_url": base_url or None,
            "feed_url": base_url + "/feed.xml" if base_url else None,
            "series": [
                {"key": key, "name": spec.get("name"), "days": spec.get("days") or [], "time": spec.get("time"),
                 "next_run": _podcast_series_next_run(spec, now)}
                for key, spec in (config.get("series") or {}).items() if isinstance(spec, dict)
            ],
            "last_run": last_run,
        }

    mtimes = []
    for path in (PODCAST / "podcast_config.json", PODCAST / "last_run.json", PODCAST / "episodes"):
        try:
            mtimes.append(path.stat().st_mtime)
        except OSError:
            pass
    return cached(f"podcast:{int(max(mtimes)) if mtimes else 0}", 60, build)


PODCAST_VERBS = {"approve", "skip", "review", "open"}


def podcast_action(name: str) -> str:
    """name = 'podcast:<verb>[:<argument>]', e.g. podcast:approve:all, podcast:skip:2026-11-12-ep002, podcast:review:off."""
    _, _, rest = name.partition(":")
    verb, _, argument = rest.partition(":")
    if verb == "open":
        if (PODCAST / "site").exists():
            machine.open_path(PODCAST / "site")
            return "Site folder opened."
        raise ValueError("The site folder isn't there yet (no episodes built).")
    if verb in PROMOTION_VERBS:
        return podcast_promotion_action(verb, argument)
    if verb not in PODCAST_VERBS:
        raise ValueError(f"Unknown action: {name}")
    for key in [key for key in _cache if key.startswith("podcast:")]:
        _cache.pop(key, None)
    command = [str(PYTHON), str(PODCAST / "podcast.py"), verb]
    if verb in ("approve", "skip"):
        if verb == "skip" and argument == "all":
            raise ValueError("Skip works on one episode at a time; name the slug or leave it out.")
        if argument:
            command.append("--all" if argument == "all" else argument)
        # no argument: the module picks the oldest held episode
    elif argument not in ("on", "off"):
        raise ValueError("Review takes 'on' or 'off'.")
    else:
        command.append(argument)
    output = subprocess.run(command, cwd=PODCAST, capture_output=True, text=True, encoding="utf-8", timeout=300,
                            creationflags=NO_WINDOW)
    if output.returncode != 0:
        raise RuntimeError((output.stderr or "podcast action failed").strip().splitlines()[-1])
    return (output.stdout.strip().splitlines() or ["Done."])[-1]


# ------------------------------------------------------------------ podcast promotion (listings, downloads, announcements)

_promotion_refresh: subprocess.Popen | None = None  # the background 'promotion.py refresh', so it never runs twice


def _load_promotion():
    """The podcast module's promotion.py, loaded from its file (it is not on the import path). None if missing."""
    script = PODCAST / "promotion.py"
    if not script.exists():
        return None
    import importlib.util

    spec = importlib.util.spec_from_file_location("podcast_promotion", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _start_promotion_refresh(force: bool = False) -> bool:
    """Ask OP3 (and Apple) in the background; the next dashboard refresh shows the answer. False if one is running."""
    global _promotion_refresh
    if _promotion_refresh is not None and _promotion_refresh.poll() is None:
        return False
    command = [str(PYTHONW), str(PODCAST / "promotion.py"), "refresh"] + (["--force"] if force else [])
    _promotion_refresh = subprocess.Popen(command, cwd=PODCAST, creationflags=NO_WINDOW)
    return True


def podcast_promotion_section() -> dict:
    """What the Podcast tab shows: directories, downloads, ranked episodes, series compared, where each episode was
    announced and follower counts. promotion.view() only reads files; when the OP3 answer is old it starts a
    background refresh, so opening the dashboard never waits on the network."""
    def build():
        promotion = _load_promotion()
        if promotion is None:
            return {"installed": False}
        data = promotion.view()
        refreshing = _promotion_refresh is not None and _promotion_refresh.poll() is None
        if data.get("stale") and not refreshing:
            refreshing = _start_promotion_refresh()
        data["refreshing"] = refreshing
        return data

    return cached("podcast_promotion", 60, build)


def apple_late_todo(promotion: dict | None) -> list[dict]:
    """Apple Podcasts reads the feed by itself. An episode it still does not show two days after it went out
    needs a push: Podcasts Connect, the show, Refresh feed."""
    apple = (promotion or {}).get("apple") or {}
    late = [entry for entry in apple.get("late") or [] if isinstance(entry, dict)]
    if not (promotion or {}).get("installed") or not late:
        return []
    return [{
        "priority": 2,
        "module": "Podcast",
        "title": f"Apple Podcasts has not picked up {len(late)} episode{'' if len(late) == 1 else 's'}",
        "detail": "Out for more than two days and still not on Apple: "
                  + "; ".join(f"{str(entry.get('published') or '')[:10]} {entry.get('title') or entry.get('slug') or ''}".strip()
                              for entry in late[:5])
                  + ". Open Podcasts Connect, choose the show and press Refresh feed. "
                    "Check that the episode is on the feed first (Feed link).",
        "action": "podcast:promo-refresh",
        "action_label": "Ask Apple again",
        "links": [{"label": "Podcasts Connect", "url": "https://podcastsconnect.apple.com/"},
                  {"label": "Feed", "url": str(promotion.get("feed_url") or "")}],
        "failed": True,
    }]


def podcast_promotion_todo(promotion: dict | None) -> list[dict]:
    """One reminder while directories are still to do (it stays until each is listed or marked as submitted),
    and one when Apple is behind on an episode."""
    if not promotion or not promotion.get("installed"):
        return []
    late = apple_late_todo(promotion)
    if not promotion.get("todo"):
        return late
    rows = [row for row in promotion.get("directories") or [] if row.get("status") == "todo"]
    return late + [{
        "priority": 3,
        "module": "Podcast",
        "title": f"List the podcast on {len(rows)} more director{'y' if len(rows) == 1 else 'ies'}",
        "detail": "Paste the feed address on each site, then press Mark as submitted in the Podcast tab. "
                  "Feed: " + str(promotion.get("feed_url") or ""),
        "action": None,
        "action_label": None,
        "links": [{"label": row.get("name"), "url": row.get("url"), "note": row.get("why")} for row in rows],
    }]


PROMOTION_VERBS = {"listed", "unlisted", "promo-refresh"}


def podcast_promotion_action(verb: str, argument: str) -> str:
    promotion = _load_promotion()
    if promotion is None:
        raise ValueError("The podcast module has no promotion.py yet; update it first.")
    if verb == "promo-refresh":
        _cache.pop("podcast_promotion", None)
        if _start_promotion_refresh(force=True):
            return "Checking OP3 and Apple now; the numbers update in under a minute."
        return "A check is already running; the numbers update in under a minute."
    message = promotion.set_mark(argument, verb == "listed")  # raises ValueError for an unknown directory
    _cache.pop("podcast_promotion", None)
    return message


# ------------------------------------------------------------------ per-day numbers for every automation

def count_by_day(days) -> dict[str, int]:
    """{'YYYY-MM-DD': n} from dates or ISO strings (anything that doesn't look like a date is ignored)."""
    counts: dict[str, int] = {}
    for day in days:
        key = day.strftime("%Y-%m-%d") if isinstance(day, (date, datetime)) else str(day or "")[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", key):
            counts[key] = counts.get(key, 0) + 1
    return counts


def _sqlite_by_day(db_file: Path, sql: str) -> dict[str, int]:
    """Read-only query returning (day, count) rows; {} if the database is missing or busy."""
    import sqlite3

    if not db_file.exists():
        return {}
    try:
        connection = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=3)
        try:
            return {str(day)[:10]: count for day, count in connection.execute(sql) if day}
        finally:
            connection.close()
    except sqlite3.Error:
        return {}


def _bigtech_by_day() -> dict[str, int]:
    found_csv = EMAIL / "bigtech_jobs.csv"
    if not found_csv.exists():
        return {}

    def build():
        with open(found_csv, encoding="utf-8-sig", newline="") as handle:
            return count_by_day(row.get("found") for row in csv.DictReader(handle))

    return cached(f"bigtech:{found_csv.stat().st_mtime}", 300, build)


def _daily_row(module: str, tab: str, label: str, by_day: dict[str, int], limit=None, limit_label: str = "",
               note: str = "") -> dict:
    today = date.today()
    last14 = [(today - timedelta(days=offset)).strftime("%Y-%m-%d") for offset in range(13, -1, -1)]
    series = [{"x": day, "y": by_day.get(day, 0)} for day in last14]
    first_day = min(by_day) if by_day else None
    # average over the last 7 days, or over the days since it started if that is shorter (a new module isn't penalised)
    window_start = max(today - timedelta(days=6), datetime.strptime(first_day, "%Y-%m-%d").date()) if first_day else today
    window_days = (today - window_start).days + 1
    window_total = sum(count for day, count in by_day.items() if day >= window_start.strftime("%Y-%m-%d"))
    return {
        "module": module,
        "tab": tab,
        "label": label,
        "today": by_day.get(today.strftime("%Y-%m-%d"), 0),
        "avg7": round(window_total / window_days, 1) if window_days else 0,
        "avg_days": window_days,
        "best": max(by_day.values()) if by_day else 0,
        "total": sum(by_day.values()),
        "limit": limit,
        "limit_label": limit_label or (f"{limit} a day" if limit else "no limit"),
        "series": series,
        "note": note,
    }


def daily_stats(sections: dict) -> list[dict]:
    """Today / 7-day average / best day / limit for every automation, from the records each one already keeps."""
    rows: list[dict] = []
    promote = sections.get("promote", {})
    if PROMOTE_SCRIPT.exists():
        cap = promote.get("cap")
        rows.append(_daily_row(
            "Creator outreach", "promote", "E-mails to creators",
            _sqlite_by_day(PROMOTE / "state.db", "select substr(sent_at,1,10), count(*) from messages where status='sent' group by 1"),
            cap, f"{cap} a day (warm-up, rises to 20)" if cap else "",
            "Limited by the Gmail warm-up now; later by how many creators publish an e-mail.",
        ))
        rows.append(_daily_row(
            "Creator outreach", "promote", "Creators found with a usable e-mail",
            _sqlite_by_day(PROMOTE / "state.db",
                           "select substr(found_at,1,10), count(*) from creators where email is not null and email_source is not null and stage not in ('skipped','no_email','bounced','suppressed') group by 1"),
            None, "set by searches a day", "YouTube + websites + podcasts, after the quality checks.",
        ))
    email = sections.get("email", {})
    if email.get("installed"):
        rows.append(_daily_row(
            "E-mail applications", "email", "Applications e-mailed", email.get("sent_by_day", {}),
            email.get("max_per_day"), "", "Only ads that list a recruiter e-mail and pass the fit and salary rules.",
        ))
        rows.append(_daily_row(
            "E-mail applications", "email", "Site-only leads added", email.get("leads_by_day", {}),
            None, "", "Good fits without an e-mail: apply on the site.",
        ))
    bigtech = _bigtech_by_day()
    if bigtech:
        rows.append(_daily_row("Big Tech watch", "email", "Big Tech roles found", bigtech, None, "",
                               "New matching roles at the watched companies."))
    jobs = sections.get("jobs", {})
    if jobs.get("installed"):
        rows.append(_daily_row("Job search", "jobs", "Matching jobs found", jobs.get("found_by_day", {}), None, "",
                               "New roles on the company job boards that fit your CV."))
        target = jobs.get("daily_target")
        rows.append(_daily_row("Job search", "jobs", "Applications submitted", jobs.get("applied_by_day", {}),
                               target, f"goal {target} a day" if target else "",
                               "Needs you: the apply session fills the form, you press Submit."))
    if sections.get("podcast", {}).get("installed"):
        rows.append(_daily_row(
            "Podcast", "overview", "Episodes published", sections["podcast"].get("published_by_day", {}),
            None, "3 a week", "One episode per series, on its scheduled day."))
    rows.append(_daily_row(
        "LinkedIn posts", "linkedin", "Posts published",
        _sqlite_by_day(ROOT / "state.db", "select substr(slot,1,10), count(*) from posts where status='posted' group by 1"),
        None, "3 a week (Tue/Wed/Thu)", "Posting days are fixed, so most days show 0.",
    ))
    if sections.get("channels", {}).get("connected"):
        rows.append(_daily_row(
            "Other platforms", "linkedin", "Posts on other platforms",
            _sqlite_by_day(ROOT / "state.db",
                           "select substr(posted_at,1,10), count(*) from channel_posts where status='posted' group by 1"),
            None, "same days as LinkedIn", "One per connected platform for each LinkedIn slot.",
        ))
    rows.append(_daily_row(
        "Audience", "linkedin", "Followers (Mastodon + Bluesky + Dev.to + GitHub)",
        _sqlite_by_day(ROOT / "state.db", "select day, sum(followers) from audience group by 1"),
        None, "", "Counted once a day. LinkedIn does not share follower counts with apps.",
    ))
    rows.append(_daily_row(
        "Audience", "linkedin", "GitHub visitors from social (last 14 days)",
        _sqlite_by_day(ROOT / "state.db", "select day, sum(uniques) from github_referrers "
                       "where platform in ('LinkedIn','Mastodon','Bluesky','Dev.to') group by 1"),
        None, "", "Unique visitors GitHub says came from LinkedIn, Mastodon, Bluesky and Dev.to.",
    ))
    rows.append(_daily_row(
        "Audience", "linkedin", "Growth actions (profiles, pins, follow-backs, GitHub links)",
        _sqlite_by_day(ROOT / "state.db", "select substr(at,1,10), count(*) from growth_log group by 1"),
        None, "", "Every change growth.py makes on your accounts; details in the growth_log table and the log file.",
    ))
    return rows


# ------------------------------------------------------------------ other platforms (channels.py)

def channels_section() -> dict:
    import channels
    import sqlite3

    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    secrets_file = ROOT / "secrets.json"
    secrets = json.loads(secrets_file.read_text(encoding="utf-8")) if secrets_file.exists() else {}
    creds = channels.credentials(secrets)
    automatic = [name for name in channels.AUTO if name in channels.enabled(config)]
    section = {
        "connected": [channels.LABELS[name] for name in automatic if creds.get(name)],
        "not_connected": [channels.LABELS[name] for name in automatic if not creds.get(name)],
        "failed": [],
        "recent": [],
    }
    db_file = ROOT / "state.db"
    if not db_file.exists():
        return section
    try:
        connection = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=3)
        try:
            exists = connection.execute("select 1 from sqlite_master where name='channel_posts'").fetchone()
            if not exists:
                return section
            for slot, channel, error in connection.execute(
                f"select slot, channel, error from channel_posts where status='failed' and attempts>={channels.MAX_ATTEMPTS} "
                "and created_at >= date('now', '-14 day')"
            ):
                section["failed"].append({"slot": slot, "channel": channels.LABELS[channel], "error": (error or "")[:200]})
            for slot, channel, status, url in connection.execute(
                "select slot, channel, status, url from channel_posts where status in ('posted','ready','failed') "
                "order by created_at desc limit 20"
            ):
                section["recent"].append({"slot": slot, "channel": channels.LABELS[channel], "status": status, "url": url})
        finally:
            connection.close()
    except sqlite3.Error:
        pass
    return section


def channels_todo(section: dict) -> list[dict]:
    items = []
    if section.get("not_connected"):
        items.append({
            "priority": 3, "module": "Platforms",
            "title": "Connect " + ", ".join(section["not_connected"]),
            "detail": "Free, official APIs. Each post then also goes out there, cut to fit. About 2 minutes per platform.",
            "action": "channels_setup", "action_label": "Connect platforms", "links": [],
        })
    for failure in section.get("failed", []):
        items.append({
            "priority": 2, "module": "Platforms",
            "title": f"{failure['channel']} post failed ({failure['slot']})",
            "detail": f"Tried 3 times. Last error: {failure['error']}",
            "action": "channels_setup", "action_label": "Check connection", "links": [], "failed": True,
        })
    return items


# ------------------------------------------------------------------ launch tests (a project's public summary.json)

LAUNCH_TESTS = [
    # One row per project that publishes a launch-test summary (juw12/lelet: scripts/launch-verdict.mjs writes it).
    {"id": "lelet", "name": "Lelet",
     "summary_url": "https://raw.githubusercontent.com/juw12/lelet/launch-status/summary.json"},
]
LAUNCH_PRIVATE = ROOT / "launch_private.json"  # git-ignored: steps that must not sit in a public repo
LAUNCH_CACHE = ROOT / "data" / "launch_cache"  # git-ignored: the last good answer, shown when offline
LAUNCH_REFRESH_SECONDS = 600
LAUNCH_RETRY_SECONDS = 120  # after a failed read, try again sooner
LAUNCH_COUNTS = ("testers", "regulars", "letters")
LAUNCH_STATES = {"done": "done", "last-tap": "one tap left", "blocked": "blocked", "yours": "only you", "auto": "runs itself"}
LAUNCH_BUTTON = re.compile(r"^(launch_run_now|launch:[a-z_]{2,30}:[a-z0-9][a-z0-9-]{0,39})$")
LAUNCH_LINKS = (("app", "App"), ("repo", "Repo"), ("status", "Status page"),
                ("launchIssue", "Launch issue"), ("verdictIssue", "Verdict"))


def _launch_fetch(url: str) -> dict | None:
    """The summary as a dict, or None when offline or the answer is not a JSON object."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "reachpilot"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _launch_summary(test: dict) -> dict:
    """A fresh summary when the network answers; otherwise the last good one from disk, marked stale."""
    cache_file = LAUNCH_CACHE / f"{test['id']}.json"
    summary = _launch_fetch(test["summary_url"])
    if summary is not None:
        try:
            LAUNCH_CACHE.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(summary), encoding="utf-8")
        except OSError:
            pass
        return {"summary": summary, "stale": False, "read_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    try:
        summary = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        summary = None
    if not isinstance(summary, dict):
        return {"summary": None, "stale": True, "read_at": None}
    return {"summary": summary, "stale": True, "read_at": stamp(cache_file)}


def _launch_cached(test: dict) -> dict:
    key = "launch:" + test["id"]
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < (LAUNCH_RETRY_SECONDS if hit[1]["stale"] else LAUNCH_REFRESH_SECONDS):
        return hit[1]
    value = _launch_summary(test)
    _cache[key] = (time.time(), value)
    return value


def _launch_private() -> dict:
    """Private steps per test id from launch_private.json; {} when the file is missing or unreadable."""
    try:
        data = json.loads(LAUNCH_PRIVATE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _launch_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _launch_int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _launch_url(value) -> str | None:
    """Only web links: the summary comes from the network and ends up in an href."""
    return value if isinstance(value, str) and value.startswith(("https://", "http://")) else None


def _launch_days(moment, now: datetime | None) -> int | None:
    """Days from now until an ISO time, rounded up (0 or less once it has passed)."""
    try:
        target = datetime.fromisoformat(str(moment))
    except ValueError:
        return None
    current = now or datetime.now(target.tzinfo)
    try:
        seconds = (target - current).total_seconds()
    except TypeError:  # one side has a time zone and the other does not
        return None
    return int(-(-seconds // 86400))


def _launch_steps(items, private: bool, trusted: bool = False) -> list[dict]:
    """Steps for the page. Only trusted rows (from launch_auto.py on this PC) may say "runs itself" or carry
    buttons: a public summary comes from the network and must never be able to put an action on the page."""
    steps = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        status = item.get("status") if item.get("status") in LAUNCH_STATES else "yours"
        if status == "auto" and not trusted:
            status = "yours"
        buttons = [
            {"name": entry["name"], "label": str(entry.get("label") or "Do it")[:60]}
            for entry in (item.get("actions") if trusted and isinstance(item.get("actions"), list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("name"), str) and LAUNCH_BUTTON.match(entry["name"])
        ]
        links = [
            {"label": str(entry.get("label") or entry["url"])[:80], "url": entry["url"], "note": ""}
            for entry in (item.get("links") if isinstance(item.get("links"), list) else [])
            if isinstance(entry, dict) and _launch_url(entry.get("url"))
        ]
        steps.append({
            "id": str(item.get("id") or item["title"])[:60],
            "title": str(item["title"])[:160],
            "status": status,
            "status_label": LAUNCH_STATES[status],
            "due": str(item["due"])[:10] if item.get("due") else None,
            "detail": str(item.get("detail") or "")[:600],
            "links": links,
            "private": private,
            "automation": trusted,
            "actions": buttons,
            "replaces": str(item["replaces"])[:60] if trusted and item.get("replaces") else None,
        })
    return steps


def _launch_auto_rows(test_id: str) -> list[dict]:
    """What launch_auto.py is doing for this test, read from disk. A problem there must not break the card."""
    try:
        import launch_auto

        return launch_auto.rows(test_id)
    except Exception:
        return []


def _launch_view(test: dict, fetched: dict, private_steps=None, now: datetime | None = None, auto_rows=None) -> dict:
    """One launch test for the dashboard. Every field is checked: a half-written summary must not break the page."""
    summary = _launch_dict(fetched.get("summary"))
    totals, need, stop = (_launch_dict(summary.get(key)) for key in ("totals", "need", "stop"))
    labels = _launch_dict(summary.get("labels"))
    links = _launch_dict(summary.get("links"))
    days_to_start = _launch_days(summary.get("start"), now)
    days_left = _launch_days(summary.get("end"), now)
    if days_to_start is not None and days_to_start > 0:
        when = f"starts in {days_to_start} day{'' if days_to_start == 1 else 's'}"
    elif days_left is not None and days_left > 0:
        when = f"{days_left} day{'' if days_left == 1 else 's'} left"
    else:
        when = "ended" if days_left is not None else ""
    public = _launch_steps(summary.get("needsMe"), False)
    automated = _launch_steps(auto_rows, True, trusted=True)
    replaced = {step["replaces"]: step for step in reversed(automated) if step["replaces"]}
    for step in public:  # an automation that takes a public step over keeps that step's links, after its own
        taker = replaced.get(step["id"])
        if taker is not None:
            own = {entry["url"] for entry in taker["links"]}
            taker["links"] = taker["links"] + [entry for entry in step["links"] if entry["url"] not in own]
    steps = [step for step in public if step["id"] not in replaced] + _launch_steps(private_steps, True) + automated

    def text(key: str) -> str:
        return str(summary.get(key) or "")[:300]

    return {
        "id": test["id"],
        "name": str(summary.get("name") or test["name"])[:60],
        "available": bool(summary),
        "stale": bool(fetched.get("stale")),
        "read_at": fetched.get("read_at"),
        "phase": text("phase"),
        "outcome": text("outcome"),
        "headline": text("headline"),
        "next_step": text("nextStep"),
        "updated_text": text("updatedText"),
        "start_text": text("startText"),
        "end_text": text("endText"),
        "decision_day": text("decisionDay"),
        "when": when,
        "days_to_start": days_to_start,
        "days_left": days_left,
        "counts": [{
            "key": key,
            "label": str(labels.get(key) or key.capitalize())[:80],
            "have": _launch_int(totals.get(key)),
            "need": _launch_int(need.get(key)),
            "stop": _launch_int(stop.get(key)),
        } for key in LAUNCH_COUNTS],
        "links": [{"label": label, "url": links[key]} for key, label in LAUNCH_LINKS if _launch_url(links.get(key))],
        "needs_me": steps,
        "open": sum(1 for step in steps if step["status"] not in ("done", "auto")),
        "automatic": sum(1 for step in steps if step["status"] == "auto"),
    }


def launch_section() -> dict:
    private = _launch_private()
    tests = [
        _launch_view(test, _launch_cached(test), _launch_dict(private.get(test["id"])).get("needsMe"),
                     auto_rows=_launch_auto_rows(test["id"]))
        for test in LAUNCH_TESTS
    ]
    return {"installed": bool(tests), "tests": tests}


def launch_todo(section: dict | None, today: date | None = None) -> list[dict]:
    """Open launch steps as to-do items: urgent from 3 days before their date, quiet before that."""
    today = today or date.today()
    items = []
    for test in (section or {}).get("tests") or []:
        if test.get("phase") == "final" and test.get("headline"):
            items.append({
                "priority": 1, "module": "Launch",
                "title": f"{test['name']} launch test is over: {test['headline']}",
                "detail": test.get("next_step") or "",
                "action": None, "action_label": None,
                "links": [{**entry, "note": ""} for entry in test.get("links") or []],
            })
        for step in test.get("needs_me") or []:
            if step["status"] in ("done", "auto"):  # "auto" runs without me: nothing to do
                continue
            try:
                days = (date.fromisoformat(step["due"]) - today).days if step.get("due") else None
            except ValueError:
                days = None
            if days is not None and days <= 3:
                priority = 1
            else:
                priority = 2 if step["status"] == "last-tap" else 3
            lead = step["status_label"].capitalize() + (f", by {step['due']}" if step.get("due") else "") + "."
            button = (step.get("actions") or [None])[0]
            items.append({
                "priority": priority, "module": "Launch",
                "title": f"{test['name']}: {step['title']}",
                "detail": f"{lead} {step['detail']}".strip(),
                "action": button["name"] if button else None,
                "action_label": button["label"] if button else None,
                "links": step["links"],
            })
    return items


# ------------------------------------------------------------------ patent screen (patents.py)

PATENTS_DISCLAIMER = (
    "A keyword screen, not legal advice. It does not search prior art and cannot judge novelty. "
    "A public repo is counted as disclosed on the day it was created. Talk to a patent attorney before "
    "filing, and before publishing anything you want to protect."
)


def patents_section(today: date | None = None) -> dict:
    """The last scan with today's deadlines and my own notes. Shown only where the screen has been installed."""
    import patents

    if not patents.DATA_DIR.exists():
        return {"installed": False}
    import patent_desk

    desk = {**patent_desk.load_inputs(), "figure_exists": patent_desk.figure_on_disk}
    return patents_summary(patents._load(patents.SCAN_FILE, None), patents.load_overrides(), today, desk)


def patents_summary(scan, overrides, today: date | None = None, desk: dict | None = None) -> dict:
    """What the Patent screen and Patent desk cards show, from a scan, my notes and the dossiers.

    Reads no files: the public demo feeds it made-up projects. desk carries dossiers, facts, history,
    figure_exists and refreshed_at; without it there is no desk card.
    """
    import patents

    empty = {
        "desk": None,
        "installed": True, "has_scan": False, "scanned_at": "", "includes_private": False, "signed_in": True, "total": 0,
        "counts": {"look": 0, "maybe": 0, "unlikely": 0, "forks": 0, "undecided": 0, "exposed": 0},
        "next_deadline": None, "candidates": [], "others": [], "changes": [], "disclaimer": PATENTS_DISCLAIMER,
    }
    if not isinstance(scan, dict) or not isinstance(scan.get("repos"), list):
        return empty
    rows = patents.view(scan, overrides, today)
    own = [row for row in rows if not row["fork"]]
    candidates = [row for row in own if row["band"] != "unlikely"]
    waiting = [row for row in candidates if not row["decision"]]
    dated = sorted(
        (row for row in waiting if row["window"]["state"] == "grace"),
        key=lambda row: row["window"]["days_left"],
    )
    names = {row["name"] for row in rows}
    changes = [
        {"repo": str(change.get("repo") or ""), "kind": str(change.get("kind") or ""), "text": str(change.get("text") or "")[:200]}
        for change in scan.get("changes") or []
        if isinstance(change, dict) and change.get("repo") in names
    ]
    followed = None
    if isinstance(desk, dict):
        import patent_desk

        followed = patent_desk.build(rows, overrides, desk.get("dossiers"), desk.get("facts"), today,
                                     desk.get("history"), desk.get("figure_exists"))
        followed["refreshed_at"] = str(desk.get("refreshed_at") or "")
    return {
        **empty,
        "desk": followed,
        "has_scan": True,
        "scanned_at": str(scan.get("scanned_at") or ""),
        "includes_private": bool(scan.get("includes_private")),
        "signed_in": scan.get("signed_in") is not False,  # False: private projects are carried over from an earlier scan
        "total": len(own),
        "counts": {
            "look": sum(1 for row in own if row["band"] == "look"),
            "maybe": sum(1 for row in own if row["band"] == "maybe"),
            "unlikely": sum(1 for row in own if row["band"] == "unlikely"),
            "forks": len(rows) - len(own),
            "undecided": len(waiting),
            "exposed": sum(1 for row in own if row["exposed"]),
        },
        "next_deadline": {
            "name": dated[0]["name"], "date": dated[0]["window"]["deadline"], "days_left": dated[0]["window"]["days_left"],
        } if dated else None,
        "candidates": candidates,
        "others": [{
            "name": row["name"], "url": row["url"], "private": row["private"], "score": row["score"],
            "screen_band": row["screen_band"], "rating": row["rating"], "licence": row["licence"],
            "route": row["route"]["text"], "verdict": row["verdict"], "exposed": row["exposed"],
        } for row in own if row["band"] == "unlikely"],
        "changes": changes,
    }


def patents_todo(section: dict | None) -> list[dict]:
    """Deadlines that are close, a private note sitting in a public repo, and candidates nobody has decided on."""
    import patents

    if not section or not section.get("installed") or not section.get("has_scan"):
        return []
    items = []

    def add(priority, title, detail, row):
        items.append({
            "priority": priority, "module": "Patents", "title": title, "detail": detail,
            "action": None, "action_label": None,
            "links": [{"label": "Open the repository", "url": row["url"], "note": ""}] if row.get("url") else [],
        })

    for row in list(section.get("candidates") or []) + list(section.get("others") or []):
        if row.get("exposed"):
            add(1, f"{row['name']} says it must stay private, but the repo is public",
                "Its README says so. Make the repo private again or take the note out. What was public already counts as disclosed.", row)
    quiet = []
    for row in section.get("candidates") or []:
        if row["decision"]:
            continue
        when = row["window"]
        days = when.get("days_left")
        if when["state"] == "grace" and days is not None and days <= patents.SOON_DAYS:
            urgent = days <= patents.URGENT_DAYS and row["band"] == "look"
            add(1 if urgent else 2,
                f"{row['name']}: {days} day{'' if days == 1 else 's'} left to file where a grace period applies",
                f"Public since it was created, so the 12-month date is {when['deadline']} (US, Japan, Korea, Taiwan). "
                f"{row['route']['text']} Then record your decision on the Patent screen card.", row)
        elif row["band"] == "look":
            quiet.append(row["name"])
    if quiet:
        shown = ", ".join(quiet[:5]) + (f" and {len(quiet) - 5} more" if len(quiet) > 5 else "")
        items.append({
            "priority": 3, "module": "Patents",
            "title": f"{len(quiet)} project{'' if len(quiet) == 1 else 's'} worth an attorney look, no decision yet",
            "detail": f"{shown}. Read the evidence on the Patents page, then choose file, publish, licence only or skip.",
            "action": None, "action_label": None, "links": [],
        })
    return items + patent_desk_todo(section.get("desk"))


def patent_desk_todo(desk: dict | None) -> list[dict]:
    """What the patent desk needs from me: a package to file, a date after a filing, an ownership question, gaps to fill."""
    import patents

    if not isinstance(desk, dict):
        return []
    items = []

    def add(priority, title, detail, name=None):
        items.append({
            "priority": priority, "module": "Patents", "title": title, "detail": detail,
            "action": f"patents:open:{name}" if name else None,
            "action_label": "Open the folder" if name else None, "links": [],
        })

    def days_of(item):
        return (item.get("deadline") or {}).get("days_left")

    gaps = []
    for item in desk.get("projects") or []:
        days = days_of(item)
        if item["stage"] == "ready":
            close = days is not None and days <= patents.URGENT_DAYS
            dated = f" The 12-month date of the public repository is {item['deadline']['date']}." if item.get("deadline") else ""
            add(1 if close else 2, f"{item['name']}: the provisional package is ready to file",
                f"All {item['total']} checks pass. {item['next_step']}{dated} ReachPilot files nothing.", item["name"])
        elif item["stage"] == "filed" and days is not None and days <= patents.SOON_DAYS:
            left = f"{days} day{'' if days == 1 else 's'} left" if days >= 0 else "the 12 months have ended"
            add(1 if days <= patents.URGENT_DAYS else 2,
                f"{item['name']}: {left} for the non-provisional or PCT application", item["next_step"])
        elif item["stage"] == "ownership":
            add(2, f"{item['name']}: who owns it?",
                f"{item['next_step']} Then press Ownership cleared on the Patent desk card.")
        elif item["stage"] in ("strengthen", "disclosure"):
            gaps.append(item)
    if gaps:
        shown = " ".join(f"{item['name']} ({item['score']}/100): {item['next_step']}" for item in gaps[:4])
        more = f" And {len(gaps) - 4} more." if len(gaps) > 4 else ""
        add(3, f"{len(gaps)} patent candidate{'' if len(gaps) == 1 else 's'} not ready to file yet",
            f"{shown}{more} The dossier folders are under Patent desk on the Patents page.")
    return items


def patents_action(name: str) -> str:
    import patent_desk
    import patents

    parts = name.split(":")
    if len(parts) < 3 or not patent_desk.NAME.fullmatch(parts[2]):
        raise ValueError(f"Unknown action: {name}")
    verb, project, rest = parts[1], parts[2], parts[3:]
    if verb == "decide" and len(rest) == 1:
        return patents.decide(project, rest[0])
    if verb == "ownership" and len(rest) == 1:
        return patent_desk.set_ownership(project, rest[0])
    if verb == "filed" and len(rest) == 2:
        return patent_desk.record_filing(project, rest[0], rest[1])
    if verb == "unfiled" and not rest:
        return patent_desk.clear_filing(project)
    if verb == "open" and not rest:
        return patent_desk.open_folder(project)
    raise ValueError(f"Unknown action: {name}")


# ------------------------------------------------------------------ what I need from you

def todo_items(linkedin: dict | None, jobs: dict, email: dict, tasks: dict, podcast: dict | None = None) -> list[dict]:
    items: list[dict] = []

    def add(priority, module, title, detail, action=None, action_label=None, links=None, failed=False):
        # failed: something broke (not merely waiting for you). Overview never cuts such an item off.
        items.append({"priority": priority, "module": module, "title": title, "detail": detail,
                      "action": action, "action_label": action_label, "links": links or [], "failed": failed})

    if linkedin:
        days = linkedin.get("token_days_left")
        if days is None:
            add(1, "LinkedIn", "Connect LinkedIn", "Posting is off until you sign in once.", "linkedin_auth", "Sign in to LinkedIn")
        elif days < 10:
            add(1, "LinkedIn", f"LinkedIn login expires in {days} days",
                "LinkedIn only allows 60-day logins for this kind of app. Takes 30 seconds.", "linkedin_auth", "Renew login")
        if linkedin.get("paused"):
            add(2, "LinkedIn", "LinkedIn posting is paused", "No posts go out while the PAUSE switch is on.",
                "linkedin_toggle_pause", "Resume posting")
        if linkedin.get("failed_total"):
            posts = linkedin.get("failed_posts") or []
            add(2, "LinkedIn", f"{linkedin['failed_total']} LinkedIn post(s) failed",
                "The reason is next to each one; the Log tab has the full line." if posts else "See the Log tab for the reason.",
                links=[{"label": f"{post['slot']} · {post['repo'] or 'post'}", "url": "", "note": post["error"] or "no reason recorded"}
                       for post in posts], failed=True)
        if linkedin.get("mode") == "review" and linkedin.get("awaiting"):
            add(1, "LinkedIn", "A LinkedIn draft is waiting for your OK", "Mode is 'review', so it won't post until approved.",
                "linkedin_approve", "Approve draft")

    if jobs.get("installed"):
        if jobs.get("last_error"):
            add(1, "Jobs", "Today's job search failed", jobs["last_error"] + " It retries at the next slot, or run it now.",
                "jobs_search_now", "Search now", failed=True)
        if jobs["needs_you"]:
            add(1, "Jobs", f"{len(jobs['needs_you'])} application(s) stopped and need you",
                "Usually an e-mail verification code or a question only you can answer. Open the apply session to finish them.",
                "jobs_apply", "Finish stopped applications",
                [{"label": f"{job['company']} - {job['title']}", "url": job["link"], "note": job["my_notes"]} for job in jobs["needs_you"]])
        if jobs["today"]:
            add(2, "Jobs", f"{len(jobs['today'])} jobs ready to apply today",
                "The form fills itself (CV, cover letter, answers). You check it and press Submit, about 30 seconds each.",
                "jobs_apply", "Start apply session")
        if jobs["workday_companies"]:
            add(3, "Jobs", "One-time sign-in on Workday sites",
                "These companies use Workday: the first time, click 'Sign in with Google' in the apply session. After that it remembers you: "
                + ", ".join(jobs["workday_companies"]))
        if jobs["blocked_companies"]:
            add(3, "Jobs", "Some sites block automation",
                "SmartRecruiters jobs (" + ", ".join(jobs["blocked_companies"]) + ") open in your normal Chrome with the cover letter ready to paste.")
        if jobs["follow_ups"]:
            add(2, "Jobs", f"{len(jobs['follow_ups'])} application(s) are a week old",
                "Worth a short follow-up or a status check on the company site.", None, None,
                [{"label": f"{job['company']} - {job['title']} (applied {job['applied_on']})", "url": job["link"]} for job in jobs["follow_ups"][:10]])
        if jobs["fill_me"]:
            add(1, "Jobs", "Some answers are still empty", "Fill: " + ", ".join(jobs["fill_me"]), "open_answers", "Open my answers")

    if email.get("installed"):
        if not email["relay_ok"]:
            add(1, "Email", "Gmail sending is not confirmed", "Run one test send so real applications can go out.", "email_test", "Send test e-mail")
        if email.get("last_run_age_hours") is not None and email["last_run_age_hours"] > 6:
            add(2, "Email", "E-mail applications haven't run for a while",
                f"Last activity {email['last_run']}. Your PC may have been off; it catches up automatically, or run it now.",
                "email_run_now", "Run now")
        if email["leads_total"]:
            add(3, "Email", f"{email['leads_total']} good jobs have no recruiter email",
                "They need applying on the company site. The leads page lists them best-fit first.", "email_open_leads", "Open leads page")

    if podcast and podcast.get("installed"):
        if podcast.get("held"):
            add(1, "Podcast", f"{len(podcast['held'])} podcast episode(s) are waiting for your OK",
                "Review mode is on, so the episode was built but not published: approve it and it goes to the feed, or skip it.",
                "podcast:approve:all", "Approve & publish")
        if (podcast.get("last_run") or {}).get("ok") is False:
            add(2, "Podcast", "The last podcast run failed",
                "The episode didn't build. It retries on the next slot, or look at the module's log.",
                "open_modules", "Open modules", failed=True)

    for key, task in tasks.items():
        if task.get("installed") and not task.get("ok") and task.get("last_result") not in ("", None):
            add(2, "System", f"'{task['name']}' last run reported an error (code {task['last_result']})",
                "Check the module's log; it will retry on its next run.", failed=True)
        if not task.get("installed") and not task.get("unknown"):
            add(2, "System", f"Scheduled task '{task['name']}' is missing", "That automation won't run on its own until it's re-installed.")
    unchecked = sum(1 for task in tasks.values() if task.get("unknown"))
    if unchecked:
        add(3, "System", f"Could not check {unchecked} scheduled task{'s' if unchecked != 1 else ''}",
            "This PC was too busy to answer within 10 seconds. That says nothing about whether they are installed; "
            "the next refresh asks again.")

    return sorted(items, key=lambda item: item["priority"])


# ------------------------------------------------------------------ help text

def help_sections(tasks: dict, podcast: dict | None = None, launch: dict | None = None,
                  patents: dict | None = None, gmail: dict | None = None) -> list[dict]:
    def when(key, fallback):
        task = tasks.get(key, {})
        return f"Next run: {task['next_run']}" if task.get("next_run") and task.get("installed") else fallback

    entries = [
        {"module": "LinkedIn", "runs": "Every 30 minutes; posts Tue/Wed/Thu 08:00. " + when("linkedin", ""),
         "auto": "Picks a project, writes the post from real README facts, records the live site being used and cuts it into a short vertical video (a hook in the first 3 seconds, captions, a 'Star it on GitHub' end card; five formats in rotation: hook, POV, question, 3 things, speedrun), adds screenshots, posts through the official API, learns from clicks and stars which project, angle and video format to use next.",
         "you": "Renew the LinkedIn login every 60 days (you get a reminder here and a Windows notification)."},
        {"module": "Platforms", "runs": "With every LinkedIn slot (same task, same days).",
         "auto": "Cuts each post to fit Bluesky (300 characters) and Mastodon (500), sends the full post to your Telegram channel and Discord "
                 "server, posts to X, Threads and Substack Notes for free through Buffer (one link, the project image), "
                 "and publishes a Dev.to article for launches and releases (at most one per project every 60 days). "
                 "Each platform retries on its own up to 3 times. Once a day it also keeps your Mastodon and Bluesky "
                 "bios and pinned intro pointing at GitHub, follows back new followers (25 a day each), lists your "
                 "accounts on your GitHub profile, and counts followers and GitHub visitors per platform.",
         "you": "Connect the platforms once (Connect platforms). Nothing else."},
        {"module": "Jobs", "runs": "Searches company job boards at 07:30 and 12:30. " + when("jobs", ""),
         "auto": "Finds roles that fit your CV (37 companies, SEA/TW/CN), scores them, writes cover letters, keeps the tracker. The apply session fills each form for you.",
         "you": "Open the apply session, glance at each form, press Submit and type an e-mailed code if a site asks. Sites with a human check never get submitted by the bot."},
        {"module": "Email", "runs": "E-mail applications every hour; Big Tech watch every 4 hours. " + when("email", ""),
         "auto": "Reads JobStreet and LinkedIn ads, and when an ad lists a recruiter e-mail it sends your CV and a tailored letter from your sending Gmail; replies go to your personal inbox. One follow-up after 7 days if no reply, plus a daily report.",
         "you": "Reply to recruiters who answer. Jobs without an e-mail land on the leads page for you to apply on the site."},
        {"module": "Promote", "runs": "Every 30 minutes; finds creators once a day, sends 09:00-20:00, checks replies every 2 hours. " + when("promote", ""),
         "auto": "Finds YouTube creators whose niche fits each GitHub project (only e-mails they published themselves), writes a short personal e-mail that mentions one of their videos, rotates through every project, warms up from 5 to 20 e-mails a day, sends one follow-up after 5 days, stops when someone replies, and learns which wording and creator types answer.",
         "you": "Review mode (default): press Send or Approve all in the Promote tab. Answer replies from your personal inbox and mark them Interested / Not a fit. Add TikTok / Instagram creators by hand in creators_import.csv."},
    ]
    if podcast and podcast.get("installed"):
        schedule = "; ".join(
            f"{s.get('name')} " + " & ".join(str(day)[:3] for day in s.get("days") or [])
            + (f" {s.get('time')}" if s.get("time") else "")
            for s in podcast.get("series") or [] if s.get("name")
        )
        if schedule:
            entries.append({
                "module": "Podcast",
                "runs": f"{podcast.get('show_title') or 'The podcast'}, one episode per series: {schedule}. " + when("podcast_brief", ""),
                "auto": "Picks the day's stories from the RSS feeds, writes the script, records it with AI voices (disclosed in the feed), builds the site and pushes the episode to the RSS feed, then announces it on the connected channels.",
                "you": "With review on (default) each episode waits in the podcast tile: listen to it, then Approve & publish or skip it. Turn review off in the tile and episodes publish on their own. "
                       "The Podcast tab tracks promotion: which directories list the show (submit the feed once on each), downloads per episode and per week from OP3, "
                       "series compared, and where each episode was announced. The Bits & Tickers Studio shows only the production side.",
            })
    if launch and launch.get("tests"):
        entries.append({
            "module": "Launch",
            "runs": "Read every 10 minutes from each project's public launch summary ("
                    + ", ".join(test["name"] for test in launch["tests"]) + "). The project's own workflow updates that file.",
            "auto": "Shows the launch test on the Launch tests page: days to the start or the end, each count against what is needed and the stop line, "
                    "the verdict, and the steps that still need you. Offline it shows the last good answer and says so. "
                    "Steps that must stay private are read from launch_private.json on this PC (git-ignored). "
                    "Where launch_auto.py is installed, steps marked 'runs itself' are done by this PC every hour: it looks for the answer "
                    "to a mail that was sent and sends one follow-up when the date passes without one, mails approved invitations at the "
                    "start time (each person once, never more than the target), and checks the waiting proposal every day. "
                    "If reminders are switched on, a step that is due today and needs you is mailed to you once. "
                    + when("launch", ""),
            "you": "Do the steps marked 'one tap left', 'blocked' or 'only you'. They also sit in the To do list, urgent from 3 days before their date. "
                   "Nothing is mailed to other people before you approved the preview that is mailed to you first.",
        })
    if patents and patents.get("installed"):
        entries.append({
            "module": "Patents",
            "runs": "Every day 09:00, or when you press Scan now. " + when("patents", ""),
            "auto": "Reads the description and README of every repository you own, public and private. Scores how much technical "
                    "mechanism the text describes and quotes the sentence it found, works out the filing date (12 months from "
                    "the day a public repo was created; open while a repo is private) and names the realistic route: attorney, "
                    "defensive publication, licence or nothing. Reports new projects and repos that turned public. "
                    "Then the patent desk follows every candidate: it creates a dossier folder, scores the dossier against "
                    "eight checks (problem, mechanism, a result with its source, example, tests, figure, claims, prior art), "
                    "reads the test runs on the repository's default branch (a Pages build or a release does not count as a "
                    "test), and once the dossier has content builds a draft US provisional package: the "
                    "specification as PDF, the cover sheet fields, a filing checklist and notes for the attorney. After a "
                    "filing it counts the 12 months to the non-provisional or PCT application. "
                    "Results stay on this PC (data/patents, git-ignored).",
            "you": "Read the candidates on the Patents page and record a decision on each: file, publish, licence only or skip. "
                   "On the Patent desk card: clear the ownership question, fill the gaps the checklist names (you or an agent "
                   "write the dossier; the desk checks it and says what is missing), then file the package yourself or through "
                   "an attorney and press Record filing. ReachPilot files nothing: a filing needs your identity check, your "
                   "name as inventor and your payment. "
                   "It is a keyword screen and a draft, not legal advice: it does not search patent databases and cannot judge "
                   "novelty. See a patent attorney before filing, and before publishing anything you want to protect.",
        })
    if gmail and gmail.get("installed"):
        entries.append({
            "module": "Gmail answers",
            "runs": "Titip (its own program on this PC) looks at new Gmail every 10 minutes, in Chrome. ReachPilot shows "
                    "what it did and passes your buttons on.",
            "auto": "Reads new mail in the Primary tab. A short answer that promises nothing goes out by itself: only to the "
                    "sender, at most 8 a day, one per conversation in 12 hours. Mail about money, a job, a contract, a code "
                    "or personal papers is kept for you with a suggested answer. Newsletters, receipts and machines are left "
                    "alone. A mail it could not judge is read again, three times. It never forwards, deletes, follows a link "
                    "or opens an attachment. These limits are in Titip's code, not in a prompt.",
            "you": "Answer what waits on the Reach page or in To do: Send, Write my own or Don't answer. "
                   "Keep this PC unlocked and Chrome signed in to Gmail. On the phone the card shows counts, senders and "
                   "subjects only; the words of a mail are not copied to the phone view.",
        })
    return entries


# ------------------------------------------------------------------ actions (whitelist only)

def _console(command: list[str], cwd: Path) -> None:
    """Open a visible console window for things that need you (sign-in, apply session)."""
    machine.open_console(command, cwd)


def _start_task(key: str) -> None:
    if machine.IS_MAC:
        machine.start_task(TASKS[key])
    else:
        subprocess.run(["schtasks", "/run", "/tn", TASK_FOLDER + TASKS[key]], capture_output=True, creationflags=NO_WINDOW)
    _cache.pop("tasks", None)


# Buttons pressed on the phone (phone_action.py). Only what needs nothing on this PC's screen: a button that
# opens a window, a file or a sign-in is left out, and so is every name not listed here.
PHONE_PLAIN = {"jobs_search_now", "email_run_now", "linkedin_run_now", "linkedin_toggle_pause", "patents_run_now",
               "launch_run_now", "shorts_run_now", "replies_run_now"}
PHONE_VERBS = {
    "promote": PROMOTE_VERBS,
    "podcast": (PODCAST_VERBS - {"open"}) | PROMOTION_VERBS,
    "patents": {"decide", "ownership", "filed", "unfiled"},
    "launch": {"followup", "replied", "no_reply", "reply_read", "approve_invites", "shared", "wa_done"},
    "shorts": {"pause", "resume"},
    "replies": {"send", "say", "skip", "mode"},
    "gmail": {"check", "mode"},  # an answer to a mail is only sent from the PC: the phone copy carries no mail text
    "inbox": None,  # every answer on the inbox card only writes inbox_answers.json
    "upkeep": {"run", "keep"},  # Open report opens a file on the PC
}
PHONE_PC_ONLY = "This button only works on your PC: it opens a window or a file there."
# What the window asks when one platform is connected again (channels_setup:<platform>).
CONNECT_AGAIN = {
    "bluesky": "type your Bluesky password there once (and the code Bluesky e-mails you, if it asks). Only a new app "
               "password is saved, and the window says at once whether messages can be read.",
    "mastodon": "press Enter at the token question, then Authorize in the browser.",
}
CAIRN_APP = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "Programs" / "Cairn" / "app"


def phone_allowed(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if name in PHONE_PLAIN:
        return True
    family, _, rest = name.partition(":")
    if family not in PHONE_VERBS or not rest:
        return False
    verbs = PHONE_VERBS[family]
    return verbs is None or rest.partition(":")[0] in verbs


def run_action(name: str) -> str:
    import autopilot as ap

    if name == "phone_link":
        if not (CAIRN_APP / "cairn").is_dir():
            raise ValueError("The phone view needs Cairn on this PC: https://github.com/juw12/cairn")
        # cmd /k keeps the window open after the code is drawn
        _console(["cmd", "/k", str(PYTHON), "-m", "cairn", "dashboards", "link"], CAIRN_APP)
        return "A window opened with a QR code: scan it with your phone. It carries this PC's GitHub sign-in, so show it to nobody else."

    if name == "jobs_apply":
        _console(["cmd", "/k", str(JOBS / "autofill.bat")], JOBS)
        return "Apply session opened in a new window."
    if name == "jobs_search_now":
        _start_task("jobs")
        return "Job search started; new jobs appear in a few minutes."
    if name == "jobs_open_tracker":
        machine.open_path(JOBS / "jobs_tracker.xlsx")
        return "Tracker opened."
    if name == "open_answers":
        machine.open_path(JOBS / "my_answers.txt")
        return "Answers file opened."
    if name == "email_run_now":
        _start_task("email")
        return "E-mail applications started in the background."
    if name == "email_open_leads":
        machine.open_path(EMAIL / "leads_to_apply.html")
        return "Leads page opened."
    if name == "email_preview":
        _console([str(PYTHON), "auto_apply.py", "--dry-run"], EMAIL)
        return "Dry run started; it writes outbox_preview.html and sends nothing."
    if name == "email_test":
        _console([str(PYTHON), "auto_apply.py", "--test"], EMAIL)
        return "Test send started in a new window."
    if name == "linkedin_auth":
        _console([str(PYTHON), "autopilot.py", "auth"], ROOT)
        return "LinkedIn sign-in opened."
    if name == "linkedin_approve":
        _console([str(PYTHON), "autopilot.py", "approve"], ROOT)
        return "Approval window opened."
    if name == "linkedin_run_now":
        _start_task("linkedin")
        return "LinkedIn check started."
    if name == "linkedin_toggle_pause":
        if ap.PAUSE_FLAG.exists():
            ap.PAUSE_FLAG.unlink()
            return "LinkedIn posting resumed."
        ap.PAUSE_FLAG.write_text("paused from ReachPilot dashboard")
        return "LinkedIn posting paused."
    if name == "channels_setup":
        _console([str(PYTHON), "autopilot.py", "channels"], ROOT)
        return "A window opened to connect Bluesky, Mastodon, Dev.to, Telegram and Discord."
    if name.startswith("channels_setup:"):
        platform = name.partition(":")[2]
        if platform not in ap.channels.AUTO:
            raise ValueError(f"Unknown action: {name}")
        # cmd /k keeps the window open, so what the sign-in answered can still be read
        _console(["cmd", "/k", str(PYTHON), "autopilot.py", "channels", platform], ROOT)
        return (f"A window opened on this PC to connect {ap.channels.LABELS[platform]} again: "
                + CONNECT_AGAIN.get(platform, "answer what it asks there."))
    if name == "open_modules":
        machine.open_path(MODULES)
        return "Modules folder opened."
    if name.startswith("promote:"):
        return promote_action(name)
    if name.startswith("podcast:"):
        return podcast_action(name)
    if name == "patents_run_now":
        runner = PYTHONW if PYTHONW.exists() else Path(sys.executable)
        subprocess.Popen([str(runner), str(ROOT / "patents.py")], cwd=ROOT, creationflags=NO_WINDOW)
        _cache.pop("tasks", None)
        return "Patent screen started; the card updates in about a minute."
    if name.startswith("patents:"):
        return patents_action(name)
    if name == "launch_run_now":
        runner = PYTHONW if PYTHONW.exists() else Path(sys.executable)
        subprocess.Popen([str(runner), str(ROOT / "launch_auto.py"), "--force"], cwd=ROOT, creationflags=NO_WINDOW)
        _cache.pop("tasks", None)
        return "Launch automations started; the card updates in a few minutes."
    if name.startswith("launch:"):
        import launch_auto

        return launch_auto.action(name)
    if name == "shorts_run_now":
        runner = PYTHONW if PYTHONW.exists() else Path(sys.executable)
        subprocess.Popen([str(runner), str(ROOT / "shorts.py")], cwd=ROOT, creationflags=NO_WINDOW)
        _cache.pop("tasks", None)
        return "Shorts run started; the card updates in a minute or two."
    if name.startswith("shorts:"):
        import shorts

        return shorts.action(name)
    if name == "replies_run_now":
        runner = PYTHONW if PYTHONW.exists() else Path(sys.executable)
        subprocess.Popen([str(runner), str(ROOT / "replies.py")], cwd=ROOT, creationflags=NO_WINDOW)
        _cache.pop("tasks", None)
        return "Reading comments and messages; the card updates in a minute."
    if name.startswith("replies:"):
        import replies

        return replies.action(name)
    if name.startswith("gmail:"):
        import gmail_answers

        _cache.pop("gmail", None)
        return gmail_answers.action(name)
    if name.startswith("inbox:"):
        return inbox_action(name)
    if name.startswith("upkeep:"):
        return upkeep_action(name)
    raise ValueError(f"Unknown action: {name}")


# ------------------------------------------------------------------ readers (a project's own numbers, one CSV row a day)

READERS = [
    # juw12/morrow writes one row a day on its social-data branch. ReachPilot only reads it: no post, no write.
    {"id": "morrow", "name": "Morrow Brief",
     "url": "https://raw.githubusercontent.com/juw12/morrow/social-data/metrics.csv",
     "git": "https://github.com/juw12/morrow.git", "branch": "social-data", "file": "metrics.csv",
     "source": "https://github.com/juw12/morrow/tree/social-data",
     "target": 250, "target_date": "2026-12-31"},
]
READERS_CACHE = ROOT / "data" / "readers_cache"  # git-ignored: the last good file, shown when the network is out
READERS_REFRESH_SECONDS = 900
READERS_RETRY_SECONDS = 120  # after a failed read, try again sooner


def parse_metrics(text: str) -> list[dict]:
    """The rows of a metrics.csv, oldest first. A line that is not a date with numbers is skipped, not fatal."""
    if not text:
        return []
    rows: list[dict] = []
    try:
        reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
        for line in reader:
            day = date.fromisoformat(str(line.get("date") or "").strip())
            rows = [row for row in rows if row["date"] != day.isoformat()]  # a day written twice: the last line wins
            rows.append({
                "date": day.isoformat(),
                "subscribers": int(str(line.get("subscribers") or 0).strip()),
                "views": int(float(str(line.get("median_views") or 0).strip())),
                "posts": int(float(str(line.get("posts_counted") or 0).strip())),
                "per_100": int(float(str(line.get("views_per_100_subscribers") or 0).strip())),
            })
    except (ValueError, TypeError, csv.Error):  # a half-written file: keep what was readable before the bad line
        pass
    rows.sort(key=lambda row: row["date"])
    return rows


def readers_day(iso: str, with_year: bool = False) -> str:
    """'2026-11-17' as the card writes it: 'Tue 17 Nov', or 'Tue 17 Nov 2026' when the year matters."""
    try:
        return date.fromisoformat(iso).strftime("%a %d %b %Y" if with_year else "%a %d %b")
    except ValueError:
        return iso


def readers_view(rows: list[dict], target: int, target_date: str, today: date | None = None) -> dict:
    """Where a project stands against its own target, the pace the remaining days need, and where its own pace leads."""
    today = today or date.today()
    days_left = max((date.fromisoformat(target_date) - today).days, 0)
    view = {"available": False, "target": target, "target_date": target_date, "days_left": days_left, "forecast": "",
            "age_days": None, "note": ""}
    if not rows:
        return view
    first, latest = rows[0], rows[-1]
    subscribers = latest["subscribers"]
    views = latest["views"]
    reached = subscribers >= target
    needed = max(target - subscribers, 0)
    per_day = 0 if reached or not days_left else math.ceil(needed / days_left)
    gained = subscribers - first["subscribers"]
    span = (date.fromisoformat(latest["date"]) - date.fromisoformat(first["date"])).days
    age_days = max((today - date.fromisoformat(latest["date"])).days, 0)  # a future-dated row is not 'late'
    pace = round(gained / span, 1) if span > 0 else None  # subscribers a day, from the rows themselves
    finish = (date.fromisoformat(latest["date"]) + timedelta(days=math.ceil(needed / pace))
              if pace and pace > 0 and needed else None)
    if reached:
        forecast = ""
    elif pace is None:
        forecast = ""  # one row: no pace to speak of yet
    elif gained <= 0:
        forecast = f"At this pace: no gain since {readers_day(first['date'])}, so {target} never arrives."
    else:
        late = (finish - date.fromisoformat(target_date)).days
        forecast = (f"At +{pace:g} a day since {readers_day(first['date'])}: {target} around "
                    f"{readers_day(finish.isoformat(), with_year=True)}"
                    + (f", {late} days late." if late > 0 else ", before the target."))
    view.update({
        "available": True, "date": latest["date"], "subscribers": subscribers, "views": views,
        "per_100": latest["per_100"] or (round(100 * views / subscribers) if subscribers else 0),
        "posts": latest["posts"], "needed": needed, "reached": reached, "per_day": per_day,
        "per_week": per_day * 7 if per_day else 0,
        "progress": 100 if reached else max(0, min(100, round(100 * subscribers / target))) if target else 0,
        "gained": gained, "pace": pace, "finish": finish.isoformat() if finish else None, "forecast": forecast,
        "age_days": age_days,
        "note": f"nothing new since {readers_day(latest['date'])}" if age_days >= 2 else "",
        "since": first["date"], "days_counted": len(rows),
        "headline": f"{subscribers} subscriber{'s' if subscribers != 1 else ''}, "
                    f"{views} typical view{'s' if views != 1 else ''} a post",
        "series": [{"x": row["date"], "y": row["subscribers"]} for row in rows],
    })
    return view


def _readers_fetch(url: str) -> str | None:
    """The CSV text, or None when the network is out or the answer is not a CSV with numbers in it."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": "reachpilot"})
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            return response.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _readers_git(source: dict) -> str | None:
    """The same file through git, for a network where raw.githubusercontent is blocked (this PC's OA proxy is).

    A small bare copy sits in the cache folder, so only a newer commit costs a fetch, and nothing in the
    project's working copy is touched.
    """
    folder = READERS_CACHE / "git" / source["id"]
    try:
        folder.parent.mkdir(parents=True, exist_ok=True)
        if (folder / "HEAD").exists():
            done = subprocess.run(["git", "--git-dir", str(folder), "fetch", "--quiet", "--depth", "1", "origin",
                                   source["branch"]], capture_output=True, timeout=60)
        else:
            done = subprocess.run(["git", "clone", "--quiet", "--bare", "--single-branch", "--branch", source["branch"],
                                   source["git"], str(folder)], capture_output=True, timeout=90)
        if done.returncode != 0:
            return None
        shown = subprocess.run(["git", "--git-dir", str(folder), "show", f"{source['branch']}:{source['file']}"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return shown.stdout if shown.returncode == 0 else None


def _readers_file(source: dict) -> dict:
    """A fresh read when the network answers (plain link first, git after it), else the last good copy on disk."""
    cache_file = READERS_CACHE / f"{source['id']}.csv"
    for route, read in (("link", lambda: _readers_fetch(source["url"])), ("git", lambda: _readers_git(source))):
        text = read()
        if text and parse_metrics(text):
            try:
                READERS_CACHE.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(text, encoding="utf-8")
            except OSError:
                pass
            return {"text": text, "stale": False, "route": route, "read_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    try:
        text = cache_file.read_text(encoding="utf-8")
    except OSError:
        return {"text": "", "stale": True, "route": "cache", "read_at": None}
    return {"text": text, "stale": True, "route": "cache", "read_at": stamp(cache_file)}


def _readers_cached(source: dict) -> dict:
    """One read per 15 minutes; after a failed read, try again in 2, so the card recovers when the network returns."""
    key = "readers:" + source["id"]
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < (READERS_RETRY_SECONDS if hit[1]["stale"] else READERS_REFRESH_SECONDS):
        return hit[1]
    value = _readers_file(source)
    _cache[key] = (time.time(), value)
    return value


def readers_section(today: date | None = None) -> dict:
    products = []
    for source in READERS:
        got = _readers_cached(source)
        view = readers_view(parse_metrics(got["text"]), source["target"], source["target_date"], today)
        view.update({"id": source["id"], "name": source["name"], "link": source.get("source", ""),
                     "source_url": source["url"], "stale": got["stale"], "read_at": got["read_at"],
                     "route": got.get("route", "link")})
        products.append(view)
    return {"installed": any(product["available"] for product in products), "products": products}


# ------------------------------------------------------------------ shorts (finished short videos, scheduled for X)

def shorts_section() -> dict:
    """What shorts.py is doing for each configured channel, read from its files on this PC. No network."""
    import shorts

    rows = shorts.card()
    return {"installed": bool(rows), "channels": rows}


def shorts_todo(section: dict | None) -> list[dict]:
    """A short video that was refused twice is not tried again by itself, so it is listed until it is dealt with."""
    items = []
    for channel in (section or {}).get("channels") or []:
        refused = channel.get("failed") or []
        if refused:
            items.append({
                "priority": 2, "module": "Shorts",
                "title": f"{len(refused)} short video(s) of {channel.get('name', 'a channel')} were refused twice",
                "detail": "They are not tried again by themselves. The reason is next to each one.",
                "action": None, "action_label": None, "failed": True,
                "links": [{"label": video.get("title") or video.get("episode", ""), "url": "",
                           "note": (video.get("error") or "no reason recorded")[:200]} for video in refused],
            })
    return items


# ------------------------------------------------------------------ replies (comments and messages on the platforms)

def replies_section() -> dict:
    """What people wrote on the connected platforms and what was answered (replies.py), read from this PC's files."""
    import replies

    return replies.card()


def replies_todo(section: dict | None) -> list[dict]:
    """Every comment or message that waits for an answer, with its buttons, and a sign-in that cannot read yet."""
    section = section or {}
    if not section.get("installed"):
        return []
    items = []
    waiting = section.get("waiting") or []
    if waiting:
        links = []
        for item in waiting:
            if item["by_hand"]:
                buttons = [{"name": f"replies:skip:{item['key']}", "label": "Done"}]
            else:
                buttons = ([{"name": f"replies:send:{item['key']}", "label": "Send"}] if item["draft"] else []) + [
                    {"name": f"replies:say:{item['key']}|", "label": "Write my own",
                     "ask": f"Your answer to @{item['author']} on {item['platform']} (up to {item['limit']} characters)",
                     "most": item["limit"]},
                    {"name": f"replies:skip:{item['key']}", "label": "Skip"}]
            note = item["text"][:200]
            if item["error"]:
                note += f" · Not sent: {item['error']}"
            elif item["by_hand"]:
                note += f" · Answer it on {item['platform']}"
            elif item["draft"]:
                note += f" · Suggested answer: {item['draft']}"
            links.append({"label": f"{item['platform']} {'message' if item['kind'] == 'dm' else 'comment'} from @{item['author']}",
                          "url": item["url"], "note": note, "actions": buttons})
        alone = section.get("mode") == "auto"
        items.append({
            "priority": 2, "module": "Replies",
            "title": f"{len(waiting)} comment(s) or message(s) wait for an answer",
            "detail": ("Simple public comments are answered by themselves. These need you: a message, a real question, "
                       "or an answer that was refused." if alone
                       else "Nothing is sent until you choose. Send uses the suggested answer; Skip leaves it unanswered."),
            "action": None, "action_label": None, "links": links,
            "failed": any(item["error"] for item in waiting),
        })
    for platform in section.get("platforms") or []:
        if not platform.get("sign_in"):
            continue
        # one item and one button per platform: the window it opens asks about that platform and nothing else
        key = platform.get("key", "")
        how = CONNECT_AGAIN.get(key)
        items.append({
            "priority": 3, "module": "Replies",
            "title": f"Connect {platform['platform']} again so {'replies' if platform['comments'] else 'messages'} can be read",
            "detail": (platform["comments"] or platform["messages"])
                      + (f" The button opens a window on this PC: {how}" if how else ""),
            "action": f"channels_setup:{key}" if key else "channels_setup",
            "action_label": f"Connect {platform['platform']}", "links": [],
        })
    broken = [platform for platform in section.get("platforms") or [] if platform.get("error")]
    if broken:
        items.append({
            "priority": 3, "module": "Replies",
            "title": f"Comments on {', '.join(platform['platform'] for platform in broken)} could not be read",
            "detail": "The next run tries again. " + " ".join(f"{platform['platform']}: {platform['error']}" for platform in broken),
            "action": "replies_run_now", "action_label": "Check now", "links": [], "failed": True,
        })
    return items


# ------------------------------------------------------------------ Gmail answers (Titip's Autopilot on this PC)

def gmail_section() -> dict:
    """What Titip's Autopilot did with new Gmail and what waits for an answer (gmail_answers.py). Asks this PC only."""
    import gmail_answers

    return cached("gmail", 15, gmail_answers.card)


def gmail_todo(section: dict | None) -> list[dict]:
    """Mail that waits for an answer, mail that could not be judged, and an Autopilot that is closed or stuck."""
    section = section or {}
    if not section.get("installed"):
        return []
    box = ", ".join(section.get("mailboxes") or []) or "Gmail"
    items = []

    def mail_links(rows: list[dict], buttons) -> list[dict]:
        links = []
        for row in rows:
            note = " · ".join(part for part in (
                row.get("summary"), f"Kept because: {row['reason']}" if row.get("reason") else "",
                f"Not sent: {row['error']}" if row.get("error") else "",
                f"Suggested answer: {row['draft'][:300]}" if row.get("draft") else "") if part)
            links.append({"label": f"{row.get('sender') or row.get('address') or 'Unknown sender'}: {row.get('subject') or '(no subject)'}",
                          "url": row.get("url") or "", "note": note, "actions": buttons(row), "mail": True})
        return links

    if not section.get("readable", True):
        return [{"priority": 3, "module": "Gmail", "title": "Gmail answers: Titip's state could not be read",
                 "detail": str(section.get("error") or ""), "action": None, "action_label": None, "links": [], "failed": True}]
    if not section.get("running") and section.get("on"):
        can_start = bool(section.get("can_start"))
        items.append({
            "priority": 2, "module": "Gmail",
            "title": "Gmail answers have stopped: Titip is closed",
            "detail": f"No mail in {box} is read or answered until Titip runs again."
                      + ("" if can_start else " Start Titip from its own folder (Titip.pyw)."),
            "action": "gmail:start" if can_start else None, "action_label": "Start Titip" if can_start else None,
            "links": [], "failed": True,
        })
    elif section.get("problem"):
        items.append({
            "priority": 2, "module": "Gmail",
            "title": "Gmail could not be read or answered at the last look",
            "detail": f"{section['problem']} ({box}, {section.get('problem_at') or 'just now'}). The next look tries again.",
            "action": "gmail:check", "action_label": "Check now", "links": [], "failed": True,
        })
    elif section.get("late"):
        items.append({
            "priority": 2, "module": "Gmail",
            "title": "Gmail has not been looked at for over 30 minutes",
            "detail": f"Titip is on but its last look at {box} was {section.get('last_check') or 'never'}. "
                      "It needs this PC unlocked and Chrome signed in to Gmail.",
            "action": "gmail:check", "action_label": "Check now", "links": [], "failed": True,
        })
    waiting = section.get("waiting") or []
    if waiting:
        def buttons(row: dict) -> list[dict]:
            if row.get("sending"):
                return []
            return ([{"name": f"gmail:send:{row['id']}", "label": "Send"}] if row.get("draft") else []) + [
                {"name": f"gmail:say:{row['id']}|", "label": "Write my own",
                 "ask": f"Your answer to {row.get('sender') or row.get('address')} (up to 1500 characters)", "most": 1500},
                {"name": f"gmail:skip:{row['id']}", "label": "Don't answer"}]

        items.append({
            "priority": 2, "module": "Gmail",
            "title": f"{len(waiting)} mail{'' if len(waiting) == 1 else 's'} in {box} wait{'s' if len(waiting) == 1 else ''} for your answer",
            "detail": "Kept for you because they are about money, a job, a contract, a code or personal papers, or the answer "
                      "would promise something. Send uses the suggested answer; Don't answer leaves the mail in Gmail.",
            "action": None, "action_label": None, "links": mail_links(waiting, buttons),
            "failed": any(row.get("error") for row in waiting),
        })
    failed = section.get("failed") or []
    if failed:
        items.append({
            "priority": 3, "module": "Gmail",
            "title": f"{len(failed)} mail{'' if len(failed) == 1 else 's'} in {box} could not be judged",
            "detail": "The model did not answer or Gmail's page did not show the sender, so nothing was decided and nothing "
                      "was sent. They are still unread in Gmail: open the ones that matter, then clear the list. "
                      "From Titip 0.8.1 on such a mail is read again by itself, three times, before it lands here.",
            "action": "gmail:clear", "action_label": "Clear these",
            "links": mail_links(failed, lambda row: [{"name": f"gmail:skip:{row['id']}", "label": "Clear"}]),
            "failed": True,
        })
    return items


# ------------------------------------------------------------------ this PC (housekeeping jobs: clean-up, backup, sync)

UPKEEP_PRIVATE = ROOT / "upkeep_private.json"  # git-ignored: which scheduled jobs of this PC the card shows
UPKEEP_JOBS, UPKEEP_ROWS = 8, 60
# A task in Task Scheduler's top folder, by its name: no backslash, so never a path into another folder.
UPKEEP_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()-]{0,59}")
UPKEEP_FOLDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
UPKEEP_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
UPKEEP_MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?")
UPKEEP_NO_STATUS = {"ran": "", "ok": None, "summary": "", "attention": [], "coming": [], "free": []}


def _upkeep_jobs() -> list[dict]:
    """The jobs named in upkeep_private.json; [] where the file is missing or is not this shape."""
    try:
        data = json.loads(UPKEEP_PRIVATE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    jobs = []
    for job in (data.get("jobs") if isinstance(data, dict) else None) or []:
        if not isinstance(job, dict) or not _inbox_text(job.get("name"), 60):
            continue
        task = _inbox_text(job.get("task"), 60)
        keep = job.get("keep")
        jobs.append({
            "name": _inbox_text(job.get("name"), 60),
            "about": _inbox_text(job.get("about"), 200),
            "task": task if UPKEEP_TASK.fullmatch(task) else "",
            "status": job.get("status") if isinstance(job.get("status"), str) else "",
            "report": job.get("report") if isinstance(job.get("report"), str) else "",
            "keep": keep if isinstance(keep, list) and keep and all(isinstance(part, str) and part for part in keep) else [],
        })
    return jobs[:UPKEEP_JOBS]


def _upkeep_status(path: str) -> dict:
    """What a job wrote about its last run (its status file), every value checked and cut:

    {"ran": "2026-11-20 13:04", "ok": true, "summary": "Freed 1.2 GB.", "attention": ["..."],
     "free_bytes": {"C:": 52000000000},
     "coming": [{"name": "session_abc", "bytes": 1200000, "idle_days": 31, "after": "2026-11-27"}]}

    A job without such a file shows its scheduled task only."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig")) if path else None
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return dict(UPKEEP_NO_STATUS)
    coming = []
    for row in data.get("coming") if isinstance(data.get("coming"), list) else []:
        name = row.get("name") if isinstance(row, dict) else None
        if isinstance(name, str) and UPKEEP_FOLDER.fullmatch(name):
            coming.append({"name": name, "bytes": _inbox_count(row.get("bytes")) or 0, "idle_days": _inbox_count(row.get("idle_days")),
                           "after": _inbox_when(row.get("after"), UPKEEP_DAY)})
    free = data.get("free_bytes") if isinstance(data.get("free_bytes"), dict) else {}
    attention = data.get("attention") if isinstance(data.get("attention"), list) else []
    return {
        "ran": _inbox_when(data.get("ran"), UPKEEP_MOMENT),
        "ok": data.get("ok") if isinstance(data.get("ok"), bool) else None,
        "summary": _inbox_text(data.get("summary"), 300),
        "attention": [line for line in (_inbox_text(item, 300) for item in attention[:UPKEEP_ROWS]) if line],
        "coming": sorted(coming, key=lambda row: (row["after"], row["name"]))[:UPKEEP_ROWS],
        "free": [{"drive": _inbox_text(drive, 8), "bytes": size} for drive, size in sorted(free.items())[:6]
                 if _inbox_count(size) is not None and _inbox_text(drive, 8)],
    }


def upkeep_section() -> dict:
    """The housekeeping jobs of this PC, from upkeep_private.json (git-ignored, next to the code):

    {"jobs": [{"name": "Laptop clean-up", "about": "Every Monday: removes old work folders",
               "task": "Laptop clean-up", "status": "C:\\...\\status.json", "report": "C:\\...\\last-report.md",
               "keep": ["python", "C:\\...\\laptop_cleanup.py", "--keep"]}]}

    "task" is the name of a task in Task Scheduler's top folder (a launchd agent on a Mac): the card shows when it
    last ran, how that ended and when it runs next, and Run now starts it. "status" is a file the job writes after
    each run (see _upkeep_status); "report" is a file Open report opens on this PC. "keep" is a command that takes
    one folder name: it is offered per folder the job is about to remove, and only for the names in the status file.
    Not set up where the file is missing. The paths stay on this PC: the card carries names, sizes and dates."""
    jobs = _upkeep_jobs()
    if not jobs:
        return {"installed": False}
    tasks = cached("upkeep_tasks", 120, lambda: {job["task"]: _task_row(job["task"], "\\") for job in jobs if job["task"]})
    rows = []
    for index, job in enumerate(jobs):
        rows.append({"index": index, "name": job["name"], "about": job["about"], "task": tasks.get(job["task"]),
                     "has_report": bool(job["report"]), "can_keep": bool(job["keep"]), **_upkeep_status(job["status"])})
    return {"installed": True, "jobs": rows}


def upkeep_todo(section: dict | None) -> list[dict]:
    """A job whose last run failed, what a job says needs a look, and the folders a job is about to remove."""
    items = []
    for job in (section or {}).get("jobs") or []:
        task = job.get("task") or {}
        name = job["name"]
        if (task.get("installed") and not task.get("ok")) or job.get("ok") is False:
            items.append({
                "priority": 2, "module": "This PC", "title": f"{name} failed on its last run",
                "detail": job.get("summary") or f"Task Scheduler result {task.get('last_result') or 'unknown'}.",
                "action": f"upkeep:run:{job['index']}" if task.get("installed") else None,
                "action_label": "Run again" if task.get("installed") else None, "failed": True,
                "links": [{"label": line, "url": "", "note": ""} for line in job.get("attention") or []],
            })
        elif job.get("attention"):
            items.append({
                "priority": 2, "module": "This PC", "title": f"{name}: {len(job['attention'])} thing(s) only you can do",
                "detail": "The job goes on working; these stay on the list until they are done.",
                "action": None, "action_label": None,
                "links": [{"label": line, "url": "", "note": ""} for line in job["attention"]],
            })
        coming = job.get("coming") or []
        if coming:
            first = min((row["after"] for row in coming if row["after"]), default="the next run")
            items.append({
                "priority": 3, "module": "This PC",
                "title": f"{name}: {len(coming)} old folder(s) will be removed from {first}",
                "detail": "Nothing in them changed for weeks. Press Keep on the This PC card (Overview) for one you still need.",
                "action": None, "action_label": None,
                "links": [{"label": row["name"], "url": "",
                           "note": " · ".join(part for part in (
                               f"{row['bytes'] / 1048576:,.0f} MB",
                               f"idle {row['idle_days']} days" if row["idle_days"] is not None else "",
                               f"from {row['after']}" if row["after"] else "") if part)} for row in coming],
            })
    return items


def upkeep_action(name: str) -> str:
    """upkeep:run:<job> starts the job's task, upkeep:open:<job> opens its report on this PC,
    upkeep:keep:<job>:<folder> hands one folder name from the job's own list to its keep command."""
    _, verb, rest = (name.split(":", 2) + ["", ""])[:3]
    number, _, argument = rest.partition(":")
    jobs = _upkeep_jobs()
    if not number.isdigit() or int(number) >= len(jobs):
        raise ValueError(f"Unknown action: {name}")
    job = jobs[int(number)]
    if verb == "run" and job["task"] and not argument:
        if machine.IS_MAC:
            machine.start_task(job["task"])
        else:
            subprocess.run(["schtasks", "/run", "/tn", "\\" + job["task"]], capture_output=True, creationflags=NO_WINDOW)
        _cache.pop("upkeep_tasks", None)
        return f"{job['name']} started; the card updates when the run is done."
    if verb == "open" and not argument:
        if not job["report"] or not Path(job["report"]).is_file():
            raise ValueError(f"{job['name']} has not written a report yet.")
        machine.open_path(Path(job["report"]))
        return "Report opened."
    if verb == "keep" and job["keep"]:
        if argument not in {row["name"] for row in _upkeep_status(job["status"])["coming"]}:
            raise ValueError("That folder is not on the list of folders to be removed.")
        command = [str(PYTHON) if job["keep"][0] == "python" else job["keep"][0], *job["keep"][1:], argument]
        done = subprocess.run(command, capture_output=True, text=True, timeout=120, creationflags=NO_WINDOW)
        if done.returncode != 0:
            raise ValueError(((done.stderr or done.stdout or "").strip().splitlines() or ["It could not be kept."])[-1][:300])
        return f"{argument} is kept: {job['name']} will leave it alone."
    raise ValueError(f"Unknown action: {name}")


# ------------------------------------------------------------------ everything for the dashboard

def build(linkedin_overview: dict | None) -> dict:
    tasks = task_status()
    sections = {}
    for key, reader in (("jobs", jobs_section), ("email", email_section), ("promote", promote_section),
                        ("channels", channels_section), ("podcast", podcast_section),
                        ("podcast_promotion", podcast_promotion_section), ("launch", launch_section),
                        ("patents", patents_section),
                        ("readers", readers_section), ("shorts", shorts_section), ("replies", replies_section),
                        ("gmail", gmail_section), ("inbox", inbox_section), ("upkeep", upkeep_section)):
        try:
            sections[key] = reader()
        except Exception as error:  # one broken module must not take the dashboard down
            sections[key] = {"installed": False, "error": f"{type(error).__name__}: {error}"}
    try:
        daily = daily_stats(sections)
    except Exception as error:  # stats are nice-to-have; never take the dashboard down
        daily = [{"module": "Stats", "tab": "overview", "label": f"unavailable ({type(error).__name__})", "today": 0,
                  "avg7": 0, "avg_days": 0, "best": 0, "total": 0, "limit": None, "limit_label": "", "series": [], "note": str(error)[:200]}]
    return {
        **sections,
        "daily": daily,
        "tasks": tasks,
        "todo": sorted(
            todo_items(linkedin_overview, sections["jobs"], sections["email"], tasks, sections.get("podcast"))
            + sections["promote"].get("todo", [])
            + channels_todo(sections["channels"])
            + podcast_promotion_todo(sections.get("podcast_promotion"))
            + launch_todo(sections.get("launch"))
            + patents_todo(sections.get("patents"))
            + shorts_todo(sections.get("shorts"))
            + replies_todo(sections.get("replies"))
            + gmail_todo(sections.get("gmail"))
            + inbox_todo(sections.get("inbox"))
            + upkeep_todo(sections.get("upkeep")),
            key=lambda item: item["priority"],
        ),
        "help": help_sections(tasks, sections.get("podcast"), sections.get("launch"), sections.get("patents"),
                              sections.get("gmail")),
    }
