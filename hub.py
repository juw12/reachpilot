"""ReachPilot hub: brings the private modules into the dashboard.

Modules live in modules/ (git-ignored, personal data) and keep running on their own schedules:
  jobs         company job boards -> scored tracker, briefs, autofill apply sessions
  email_apply  JobStreet / LinkedIn ads with a recruiter e-mail -> CV + cover letter via the Gmail relay
  job_digest   daily digest + Gmail relay credentials used by email_apply
  promote      creator / UGC outreach e-mails for the GitHub projects (modules/promote/promote.py)

Other platforms (Bluesky, Mastodon, Dev.to, Telegram, Discord, HN / Reddit drafts) are part of the public code
(channels.py); channels_section() reads their state from state.db.

This file only reads their files, builds the "what I need from you" list, and runs a short whitelist of actions.
Every reader is defensive: a missing or half-written file shows as "not set up" instead of breaking the dashboard.
"""
from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODULES = ROOT / "modules"
JOBS = MODULES / "jobs"
EMAIL = MODULES / "email_apply"
DIGEST = MODULES / "job_digest"
PROMOTE = MODULES / "promote"
TASK_FOLDER = "\\ReachPilot\\"
TASKS = {
    "linkedin": "LinkedIn posts",
    "jobs": "Job search",
    "email": "Email applications",
    "bigtech": "Big Tech watch",
    "promote": "Promotion",
    "updates": "Updates",
}
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
PYTHON = Path(sys.executable).with_name("python.exe")
NO_WINDOW = 0x08000000

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

def task_status() -> dict:
    """Last/next run and last result of every ReachPilot task (schtasks, cached 2 minutes)."""
    def build():
        result = {}
        for key, name in TASKS.items():
            try:
                output = subprocess.run(
                    ["schtasks", "/query", "/tn", TASK_FOLDER + name, "/fo", "CSV", "/v"],
                    capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW,
                ).stdout
                rows = list(csv.DictReader(output.splitlines()))
            except Exception:
                rows = []
            if not rows:
                result[key] = {"name": name, "installed": False}
                continue
            row = rows[0]
            last_result = row.get("Last Result", "").strip()
            result[key] = {
                "name": name,
                "installed": True,
                "state": row.get("Status", "").strip(),
                "last_run": _pretty_task_time(row.get("Last Run Time", "")),
                "next_run": _pretty_task_time(row.get("Next Run Time", "")),
                "last_result": last_result,
                "ok": last_result in ("0", "267009", "267011", "267014"),  # success, running, never run, terminated by us
            }
        return result

    return cached("tasks", 120, build)


def _pretty_task_time(raw: str) -> str:
    """Turn schtasks' locale time (e.g. '11/7/2026 12:30:00 PM') into 'Sat 7 Nov · 12:30'."""
    from datetime import datetime as _dt
    raw = (raw or "").strip()
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            return _dt.strptime(raw, fmt).strftime("%a %#d %b · %H:%M")
        except ValueError:
            continue
    return raw


# ------------------------------------------------------------------ jobs module

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
PROMOTE_VERBS = {"approve", "approveall", "send", "skip", "retry", "mark", "mode", "pause", "resume", "plan", "inbox", "discover", "tick"}
PROMOTE_BACKGROUND = {"discover", "inbox", "tick"}  # slow (network): run without blocking the dashboard


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
    """name = 'promote:<verb>[:<argument>]', e.g. promote:send:12 or promote:mark:yt:UC123|interested."""
    _, _, rest = name.partition(":")
    verb, _, argument = rest.partition(":")
    if verb == "setkey":
        _console([str(PYTHON), str(PROMOTE_SCRIPT), "set-youtube-key"], PROMOTE)
        return "A window opened: paste your YouTube API key there."
    if verb == "setupchecker":
        _console([str(PYTHON), str(PROMOTE_SCRIPT), "setup-checker"], PROMOTE)
        return "A window opened with the 3-minute reply-checker setup."
    if verb == "openproducts":
        os.startfile(PROMOTE / "products.json")
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
                "tick": "Promotion run started in the background."}[verb]
    output = subprocess.run(command, cwd=PROMOTE, capture_output=True, text=True, encoding="utf-8", timeout=180, creationflags=NO_WINDOW)
    if output.returncode != 0:
        raise RuntimeError((output.stderr or "promote action failed").strip().splitlines()[-1])
    return (output.stdout.strip().splitlines() or ["Done."])[-1]


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
        "manual": [],
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
            for slot, channel, repo, title, url in connection.execute(
                "select slot, channel, repo, title, url from channel_posts where status='manual' order by created_at"
            ):
                section["manual"].append({"slot": slot, "channel": channels.LABELS[channel], "repo": repo, "title": title, "url": url})
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
    by_slot: dict[str, list[dict]] = {}
    for draft in section.get("manual", []):
        by_slot.setdefault(draft["slot"], []).append(draft)
    for slot, drafts in by_slot.items():
        repo = drafts[0]["repo"] or "this project"
        items.append({
            "priority": 2, "module": "Platforms",
            "title": f"Share {repo} on " + " and ".join(draft["channel"] for draft in drafts),
            "detail": "These sites don't allow bots, so the title and link are prefilled: open, check, press submit. "
                      "On Reddit, pick a subreddit that welcomes self-made projects. Mark done afterwards.",
            "action": f"channels_done:{slot}", "action_label": "Mark done",
            "links": [{"label": f"{draft['channel']}: {draft['title']}", "url": draft["url"]} for draft in drafts],
        })
    for failure in section.get("failed", []):
        items.append({
            "priority": 2, "module": "Platforms",
            "title": f"{failure['channel']} post failed ({failure['slot']})",
            "detail": f"Tried 3 times. Last error: {failure['error']}",
            "action": "channels_setup", "action_label": "Check connection", "links": [],
        })
    return items


# ------------------------------------------------------------------ what I need from you

def todo_items(linkedin: dict | None, jobs: dict, email: dict, tasks: dict) -> list[dict]:
    items: list[dict] = []

    def add(priority, module, title, detail, action=None, action_label=None, links=None):
        items.append({"priority": priority, "module": module, "title": title, "detail": detail,
                      "action": action, "action_label": action_label, "links": links or []})

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
            add(2, "LinkedIn", f"{linkedin['failed_total']} LinkedIn post(s) failed", "See the Log tab for the reason.")
        if linkedin.get("mode") == "review" and linkedin.get("awaiting"):
            add(1, "LinkedIn", "A LinkedIn draft is waiting for your OK", "Mode is 'review', so it won't post until approved.",
                "linkedin_approve", "Approve draft")

    if jobs.get("installed"):
        if jobs.get("last_error"):
            add(1, "Jobs", "Today's job search failed", jobs["last_error"] + " It retries at the next slot, or run it now.",
                "jobs_search_now", "Search now")
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

    for key, task in tasks.items():
        if task.get("installed") and not task.get("ok") and task.get("last_result") not in ("", None):
            add(2, "System", f"'{task['name']}' last run reported an error (code {task['last_result']})",
                "Check the module's log; it will retry on its next run.")
        if not task.get("installed"):
            add(2, "System", f"Scheduled task '{task['name']}' is missing", "That automation won't run on its own until it's re-installed.")

    return sorted(items, key=lambda item: item["priority"])


# ------------------------------------------------------------------ help text

def help_sections(tasks: dict) -> list[dict]:
    def when(key, fallback):
        task = tasks.get(key, {})
        return f"Next run: {task['next_run']}" if task.get("next_run") and task.get("installed") else fallback

    return [
        {"module": "LinkedIn", "runs": "Every 30 minutes; posts Tue/Wed/Thu 08:00. " + when("linkedin", ""),
         "auto": "Picks a project, writes the post from real README facts, adds screenshots, posts through the official API, learns from clicks and stars.",
         "you": "Renew the LinkedIn login every 60 days (you get a reminder here and a Windows notification)."},
        {"module": "Platforms", "runs": "With every LinkedIn slot (same task, same days).",
         "auto": "Cuts each post to fit Bluesky (300 characters) and Mastodon (500), sends the full post to your Telegram channel and Discord "
                 "server, publishes a Dev.to article for launches and releases (at most one per project every 60 days), and writes "
                 "a Show HN and Reddit draft once per new project. Each platform retries on its own up to 3 times.",
         "you": "Connect the platforms once (Connect platforms). For Hacker News and Reddit, press submit on the prefilled page; those sites ban bots."},
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


# ------------------------------------------------------------------ actions (whitelist only)

def _console(command: list[str], cwd: Path) -> None:
    """Open a visible console window for things that need you (sign-in, apply session)."""
    subprocess.Popen(command, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE)


def _start_task(key: str) -> None:
    subprocess.run(["schtasks", "/run", "/tn", TASK_FOLDER + TASKS[key]], capture_output=True, creationflags=NO_WINDOW)
    _cache.pop("tasks", None)


def run_action(name: str) -> str:
    import autopilot as ap

    if name == "jobs_apply":
        _console(["cmd", "/k", str(JOBS / "autofill.bat")], JOBS)
        return "Apply session opened in a new window."
    if name == "jobs_search_now":
        _start_task("jobs")
        return "Job search started; new jobs appear in a few minutes."
    if name == "jobs_open_tracker":
        os.startfile(JOBS / "jobs_tracker.xlsx")
        return "Tracker opened."
    if name == "open_answers":
        os.startfile(JOBS / "my_answers.txt")
        return "Answers file opened."
    if name == "email_run_now":
        _start_task("email")
        return "E-mail applications started in the background."
    if name == "email_open_leads":
        os.startfile(EMAIL / "leads_to_apply.html")
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
    if name.startswith("channels_done:"):
        slot = name.partition(":")[2]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", slot):
            raise ValueError(f"Unknown action: {name}")
        db = ap.connect_db()
        try:
            db.execute("UPDATE channel_posts SET status='done', posted_at=? WHERE slot=? AND status='manual'",
                       (datetime.now().strftime("%Y-%m-%d %H:%M"), slot))
            db.commit()
        finally:
            db.close()
        return "Marked as shared."
    if name == "open_modules":
        os.startfile(MODULES)
        return "Modules folder opened."
    if name.startswith("promote:"):
        return promote_action(name)
    raise ValueError(f"Unknown action: {name}")


# ------------------------------------------------------------------ everything for the dashboard

def build(linkedin_overview: dict | None) -> dict:
    tasks = task_status()
    sections = {}
    for key, reader in (("jobs", jobs_section), ("email", email_section), ("promote", promote_section),
                        ("channels", channels_section)):
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
            todo_items(linkedin_overview, sections["jobs"], sections["email"], tasks) + sections["promote"].get("todo", [])
            + channels_todo(sections["channels"]),
            key=lambda item: item["priority"],
        ),
        "help": help_sections(tasks),
    }
