"""Build the public ReachPilot demo (GitHub Pages) from dashboard.html plus made-up data.

Usage:  python docs/build_demo.py [path/to/dashboard.html]

Writes docs/index.html and copies the app icons and fonts next to it. Every name, number and job
below is invented for the demo; nothing is read from your state, logs or private modules.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
sys.path.insert(0, str(ROOT))

import hub  # noqa: E402  (the real card, to-do and help builders, fed with the made-up projects below)
import patents  # noqa: E402  (the real scorer)

TODAY = date(2026, 11, 7)


def day(offset: int) -> str:
    return (TODAY + timedelta(days=offset)).isoformat()


def job(score, company, title, location, source, status="New", notes=""):
    return {
        "score": score,
        "company": company,
        "title": title,
        "location": location,
        "status": status,
        "link": "#",
        "notes": notes,
        "my_notes": "",
        "source": source,
        "applied_on": "",
    }


TODAY_JOBS = [
    job(96, "Northwind Robotics", "Mechanical Design Engineer", "Singapore", "greenhouse"),
    job(93, "Contoso Audio", "Product Design Engineer", "Taipei, Taiwan", "lever"),
    job(91, "Fabrikam Motion", "R&D Mechanical Engineer", "Penang, Malaysia", "workday"),
    job(88, "Globex Hardware", "NPI Mechanical Engineer", "Shenzhen, China", "greenhouse",
        status="Stopped", notes="NOT submitted: the site asked for an e-mailed verification code. Finish it in the apply session."),
    job(86, "Initech Labs", "Enclosure Design Engineer", "Singapore", "lever"),
    job(84, "Umbrella Medical", "Mechanical Engineer, Devices", "Penang, Malaysia", "workday"),
    job(82, "Wayne Instruments", "Design for Manufacturing Engineer", "Taipei, Taiwan", "greenhouse"),
]
NEEDS_YOU = [entry for entry in TODAY_JOBS if entry["status"] == "Stopped"]

EMAIL_RECENT = [
    {"at": f"{TODAY} 09:12", "company": "Acme Precision", "title": "Mechanical Engineer", "region": "Singapore",
     "source": "jobstreet", "url": "#", "to_domain": "example.com"},
    {"at": f"{TODAY} 08:40", "company": "Blue Harbor Systems", "title": "Product Engineer", "region": "Penang",
     "source": "linkedin", "url": "#", "to_domain": "example.org"},
    {"at": f"{day(-1)} 17:05", "company": "Summit Moulding", "title": "Tooling Engineer", "region": "Johor",
     "source": "jobstreet", "url": "#", "to_domain": "example.net"},
    {"at": f"{day(-1)} 11:22", "company": "Orbit Electronics", "title": "NPI Engineer", "region": "Bangkok",
     "source": "jobstreet", "url": "#", "to_domain": "example.com"},
    {"at": f"{day(-2)} 15:48", "company": "Cedar Medtech", "title": "R&D Engineer", "region": "Singapore",
     "source": "linkedin", "url": "#", "to_domain": "example.org"},
]
EMAIL_LEADS = [
    {"title": "Senior Mechanical Engineer", "company": "Lumen Optics", "region": "Singapore", "url": "#", "fit": 27, "salary": ""},
    {"title": "Manufacturing Engineer", "company": "Delta Motion", "region": "Kuala Lumpur", "url": "#", "fit": 24, "salary": ""},
    {"title": "Product Development Engineer", "company": "Quartz Devices", "region": "Hsinchu", "url": "#", "fit": 23, "salary": ""},
]
EMAIL_SERIES = [{"x": day(-13 + i), "y": y} for i, y in enumerate([0, 2, 5, 9, 14, 11, 8, 12, 16, 10, 13, 9, 12, 7])]


def task(name, last, nxt):
    return {"name": name, "installed": True, "state": "Ready", "last_run": last, "next_run": nxt, "last_result": "0", "ok": True}


SCHEDULE = [
    {"slot": f"{day(3)} 08:00", "weekday": "Tue", "images": [], "kind": "prepared", "repo": None, "angle": "launch",
     "title": "pocket-cad", "hook": "A tiny parametric CAD sketcher that runs in the browser.",
     "body": "Built a tiny parametric sketcher that runs in the browser: constraints, dimensions and a STEP export.\n\nTry it: <demo link>",
     "comment": ""},
    {"slot": f"{day(4)} 08:00", "weekday": "Wed", "images": [], "kind": "prepared", "repo": None, "angle": "deep dive",
     "title": "lab-notes", "hook": "One feature, explained: searchable test logs.",
     "body": "One feature from lab-notes: every test log becomes searchable the moment it lands in the folder.",
     "comment": ""},
    {"slot": f"{day(5)} 08:00", "weekday": "Thu", "images": [], "kind": "prepared", "repo": None, "angle": "behind the build",
     "title": "weather-kit", "hook": "How the sensor enclosure went from three parts to one.",
     "body": "Behind the build: how the weather-kit enclosure went from three printed parts to one.",
     "comment": ""},
]


def daily_row(module, tab, label, by_day, limit=None, limit_label="", note=""):
    """Same shape as hub._daily_row, but anchored on the demo's fixed TODAY."""
    last14 = [day(offset) for offset in range(-13, 1)]
    window = [day(offset) for offset in range(-6, 1)]
    return {
        "module": module,
        "tab": tab,
        "label": label,
        "today": by_day.get(day(0), 0),
        "avg7": round(sum(by_day.get(d, 0) for d in window) / 7, 1),
        "avg_days": 7,
        "best": max(by_day.values()) if by_day else 0,
        "total": sum(by_day.values()),
        "limit": limit,
        "limit_label": limit_label or (f"{limit} a day" if limit else "no limit"),
        "series": [{"x": d, "y": by_day.get(d, 0)} for d in last14],
        "note": note,
    }


EMAIL_BY_DAY = {point["x"]: point["y"] for point in EMAIL_SERIES}
JOBS_FOUND_BY_DAY = {day(-13 + i): n for i, n in enumerate([9, 14, 6, 0, 0, 11, 8, 13, 10, 7, 0, 0, 15, 12])}
JOBS_APPLIED_BY_DAY = {day(-6): 1, day(-2): 1, day(-1): 2}
POSTS_BY_DAY = {day(-11): 1, day(-10): 1, day(-9): 1, day(-4): 1, day(-3): 1}
DAILY = [
    daily_row("E-mail applications", "email", "Applications e-mailed", EMAIL_BY_DAY, 40, "",
              "Only ads that list a recruiter e-mail and pass the fit and salary rules."),
    daily_row("Job search", "jobs", "Matching jobs found", JOBS_FOUND_BY_DAY, None, "",
              "New roles on the company job boards that fit your CV."),
    daily_row("Job search", "jobs", "Applications submitted", JOBS_APPLIED_BY_DAY, 20, "goal 20 a day",
              "Needs you: the apply session fills the form, you press Submit."),
    daily_row("LinkedIn posts", "linkedin", "Posts published", POSTS_BY_DAY, None, "3 a week (Tue/Wed/Thu)",
              "Posting days are fixed, so most days show 0."),
]

# ------------------------------------------------------------------ patent screen

def patent_project(name, description, readme, created, private=False, licence="mit", fork=False):
    """One made-up repository in the shape GitHub returns, with its made-up README."""
    return ({
        "name": name, "full_name": f"demo/{name}", "html_url": "#", "description": description,
        "private": private, "fork": fork, "archived": False, "created_at": created, "pushed_at": day(-9),
        "license": {"key": licence} if licence else None,
    }, readme)


PATENT_PROJECTS = [
    patent_project(
        "hinge-lab", "Friction hinge with a printed torque limiter for laptop-style lids.",
        """# hinge-lab

A friction hinge whose torque stays flat over 20,000 cycles. A printed wave spring preloads a split bearing,
and a fixture with a torque sensor logs every cycle.

Unfiled patentable material: do not publish this repository before a filing decision.

## How it works

The solver fits the spring compliance to the logged torque curve, and an interpolation table sets the preload.

## Results

Measured torque drift is within about 4 percent after 20,000 cycles, compared with 18 percent for the stock hinge.
""", day(-120), private=True, licence=""),
    patent_project(
        "pocket-cad", "A tiny parametric CAD sketcher in the browser.",
        """# pocket-cad

Sketch, constrain and export STL without installing anything.

## How it works

A constraint solver written from scratch turns each sketch into a signed-distance field. Marching squares traces
the outlines, and a small parser reads dimension expressions.

## Results

Benchmarks: a sketch with 200 constraints re-solves in 12 ms, 3x faster than the Newton solver it replaced.
""", day(-340)),
    patent_project(
        "gear-calc", "Spur gear sizing calculator: module, centre distance and backlash on one screen.",
        """# gear-calc

Spur gear sizing in one screen, no spreadsheet.

## How it works

It runs a worst-case and a root-sum-square stack over tooth thickness and centre distance. A Monte Carlo run of
10,000 pairs then counts how often a pair binds, and an interpolation algorithm reads the form factor from a table.
A heuristic picks the nearest standard module.

## Checked

Predicted backlash was compared with 30 printed pairs: every measured value was within about 0.02 mm.
""", day(-290)),
    patent_project(
        "weather-kit", "A $9 weather station that logs to a spreadsheet.",
        """# weather-kit

One board, one pressure and humidity sensor, and firmware that sleeps between readings. The enclosure prints
without supports on any FDM printer.

## How it works

Every reading carries a checksum. Readings taken during an outage are stored and replayed when the network returns.

## Results

Measured battery life: 11 months on two AA cells.
""", day(-700)),
    patent_project(
        "lab-notes", "Every test log, searchable the moment it lands.",
        """# lab-notes

A small CLI and dashboard that index test logs as they are written, so a failed run can be found by serial number.
""", day(-200), licence=""),
    patent_project(
        "tile-garden", "A cozy tile-laying game for the browser.",
        """# tile-garden

Lay tiles, grow a garden. Twelve levels, works offline.
""", day(-60)),
    patent_project(
        "seal-rig", "Leak-test rig that finds the leaking gasket segment from the pressure decay curve.",
        """# seal-rig

A bench rig that pressurises an enclosure and fits the decay curve per gasket segment.

## How it works

Each segment is masked in turn by a valve manifold. The solver fits a two-term decay model to every curve and
ranks the segments by leak rate.

## Results

Measured on 40 enclosures: the rig named the leaking segment in 38, against a dye test as reference.
""", day(-150), private=True, licence=""),
    patent_project("awesome-mechanisms", "A curated list of mechanism references.", "", day(-400), fork=True),
]
PATENT_NOTES = {
    "seal-rig": {"rating": "candidate", "reviewed": day(-40), "decision": "file",
                 "filed": {"date": day(-30), "number": "63/000,000"},
                 "verdict": "A measuring method with a physical rig and numbers behind it."},
    "gear-calc": {"rating": "maybe", "reviewed": day(-3),
                  "verdict": "The stack maths is textbook. Only the binding count might be new; look for earlier work first."},
    "weather-kit": {"decision": "publish", "reviewed": day(-3),
                    "verdict": "Public for almost two years, so it is prior art already."},
    "lab-notes": {"owner": "employer?", "rating": "low", "reviewed": day(-3),
                  "verdict": "A search index over log files: nothing a patent would cover."},
}


HINGE_DOSSIER = {
    "title": "Friction hinge with a printed wave-spring preload and method of setting the preload",
    "field": "Friction hinges for lids and displays.",
    "problem": "A friction hinge loses torque as its bearing wears, so a lid that held its angle when new drops "
               "after a few thousand cycles. Re-tightening by hand is not possible in a sealed product.",
    "mechanism": [
        "A split bearing clamps the hinge shaft.",
        "A printed wave spring presses the two halves of the bearing together.",
        "A fixture with a torque sensor logs the torque of every cycle.",
        "A solver fits the spring compliance to the logged torque curve.",
        "An interpolation table turns the fitted compliance into the preload that keeps the torque flat.",
    ],
    "variants": ["A stack of two wave springs.", "A moulded spring in place of a printed one."],
    "effects": [{"result": "Torque drift within about 4 percent after 20,000 cycles, against 18 percent for the stock hinge.",
                 "source": "results/cycle_log.csv, made by python fixture.py --cycles 20000", "kind": "measured"}],
    "example": {"steps": ["Mount the hinge in the fixture.", "Run 500 cycles and fit the compliance.",
                          "Read the preload from the table and fit the matching spring."],
                "output": "Preload 1.8 N for a 0.25 Nm hinge."},
    "figures": [{"title": "Section through the hinge", "file": "figure1.svg"}],
    "claims": [
        "A friction hinge comprising a shaft, a split bearing around the shaft, and a wave spring that preloads the split bearing.",
        "The hinge of claim 1, wherein the wave spring is printed in one piece with a bearing half.",
        "The hinge of claim 1, wherein the preload is chosen from a table of fitted spring compliance.",
        "The hinge of claim 3, wherein the compliance is fitted to a torque curve logged over at least 500 cycles.",
    ],
    "prior_art": [
        {"reference": "Clip-type friction hinge (made-up reference A)", "url": "",
         "difference": "Preload comes from the clip itself and cannot be set after moulding."},
        {"reference": "Disc-spring torque hinge (made-up reference B)", "url": "",
         "difference": "Uses steel disc springs and a nut; no fitted compliance, no printed spring."},
        {"reference": "Adjustable laptop hinge (made-up reference C)", "url": "",
         "difference": "Adjusted by hand with a screw; nothing is measured."},
    ],
    "abstract": "A friction hinge whose split bearing is preloaded by a printed wave spring chosen from a fitted compliance table.",
    "inventors": [],
}
POCKET_DOSSIER = {
    "title": "Sketch constraint solving through a signed-distance field",
    "problem": "A browser sketcher that re-solves every constraint with a Newton solver stalls on large sketches, "
               "so dragging a point lags behind the pointer.",
    "mechanism": [
        "Each sketch entity is turned into a signed-distance field.",
        "Constraints are evaluated on the field instead of on the entity equations.",
        "Marching squares traces the outlines.",
        "A parser reads the dimension expressions.",
    ],
    "effects": [{"result": "A sketch with 200 constraints re-solves in 12 ms.", "source": "bench/solve.js"}],
    "example": {"steps": ["Open the 200-constraint sample.", "Drag one corner."], "output": "12 ms per solve."},
    "figures": [],
    "claims": [],
    "prior_art": [{"reference": "Newton-based sketch solver (made-up reference D)", "url": "",
                   "difference": "Solves the entity equations directly."}],
}
SEAL_DOSSIER = {
    "title": "Method of locating a leaking gasket segment from pressure decay curves",
    "problem": "A pressure decay test says that an enclosure leaks but not where. Finding the segment takes a dye "
               "test, which is slow and marks the part.",
    "mechanism": [
        "The enclosure is pressurised through one port.",
        "A valve manifold masks one gasket segment at a time.",
        "A two-term decay model is fitted to the pressure curve of every masking state.",
        "The segments are ranked by the leak rate their masking removes.",
    ],
    "effects": [{"result": "The leaking segment was named correctly in 38 of 40 enclosures, against a dye test.",
                 "source": "results/segments.csv, made by python rig.py --batch", "kind": "measured"}],
    "example": {"steps": ["Clamp the enclosure.", "Run the eight masking states."], "output": "Segment 5, 0.8 Pa/s."},
    "figures": [{"title": "Manifold and segments", "file": "figure1.svg"}],
    "claims": [
        "A method of locating a leak, comprising masking gasket segments in turn and fitting a decay model to each pressure curve.",
        "The method of claim 1, wherein the decay model has two exponential terms.",
        "The method of claim 1, wherein the segments are ranked by fitted leak rate.",
        "The method of claim 2, wherein the masking is done by a valve manifold.",
    ],
    "prior_art": [
        {"reference": "Pressure decay leak tester (made-up reference E)", "url": "", "difference": "Gives one leak rate for the whole part."},
        {"reference": "Tracer gas sniffer (made-up reference F)", "url": "", "difference": "Needs a tracer gas and a moving probe."},
        {"reference": "Dye penetrant test (made-up reference G)", "url": "", "difference": "Manual and marks the part."},
    ],
}
PATENT_DESK = {
    "dossiers": {"hinge-lab": HINGE_DOSSIER, "pocket-cad": POCKET_DOSSIER, "seal-rig": SEAL_DOSSIER},
    "facts": {
        "hinge-lab": {"ci": {"state": "success"}, "package": {"built": f"{day(-1)} 09:00", "pdf": True, "draft": False}},
        "pocket-cad": {"ci": {"state": "success"}, "package": {"built": f"{day(-2)} 09:00", "pdf": True, "draft": True}},
        "gear-calc": {"ci": {"state": "none"}},
        "seal-rig": {"ci": {"state": "success"}, "package": {"built": f"{day(-31)} 09:00", "pdf": True, "draft": False}},
    },
    "history": [
        {"date": day(-30), "name": "seal-rig", "text": "ready to file -> filed."},
        {"date": day(-6), "name": "pocket-cad", "text": "Readiness 35 -> 60 of 100."},
        {"date": day(-1), "name": "hinge-lab", "text": "strengthening -> ready to file."},
    ],
    "figure_exists": lambda name, file: name in ("hinge-lab", "seal-rig"),
    "refreshed_at": f"{TODAY} 09:00",
}


def patent_demo() -> dict:
    """The Patent screen card exactly as the app builds it, from the made-up projects above."""
    entries = [patents.build_entry(repo, readme) for repo, readme in PATENT_PROJECTS]
    last_week = [dict(entry, band="unlikely") if entry["name"] == "gear-calc" else entry for entry in entries]
    scan = {
        "scanned_at": f"{day(-5)} 09:00", "includes_private": True, "repos": entries,
        "changes": patents.changes_since(last_week, entries),
    }
    return hub.patents_summary(scan, PATENT_NOTES, TODAY, PATENT_DESK)


PATENTS = patent_demo()


DEMO = {
    "overview": {
        "now": f"{TODAY} 09:45", "mode": "auto", "paused": False, "token_days_left": 41, "click_tracking": True,
        "posts_this_week": 2, "max_per_week": 3, "posted_total": 18, "failed_total": 0, "prepared_left": 3,
        "next_post": SCHEDULE[0], "last_tick": f"{TODAY} 09:30", "impressions": 4210, "engagements": 186,
        "posts_with_linkedin_stats": 12, "slots": ["Tue 08:00", "Wed 08:00", "Thu 08:00"], "awaiting": 0, "inbox": "C:\\ReachPilot\\linkedin_inbox",
    },
    "hub": {
        "jobs": {
            "installed": True, "tracked": 214, "open": 214, "queue_total": 214, "today": TODAY_JOBS,
            "applied_total": 9, "applied_week": 3, "applied": [{"applied_on": day(-1), "company": "Tailspin Devices", "title": "Mechanical Engineer", "status": "Applied", "link": "#"}, {"applied_on": day(-1), "company": "Adatum Robotics", "title": "Product Design Engineer", "status": "Applied", "link": "#"}, {"applied_on": day(-2), "company": "Litware Audio", "title": "Enclosure Engineer", "status": "Applied", "link": "#"}, {"applied_on": day(-6), "company": "Proseware Medical", "title": "Mechanical Design Engineer", "status": "Applied", "link": "#"}], "follow_ups": [], "needs_you": NEEDS_YOU,
            "status_counts": {"New": 204, "Applied": 9, "Stopped": 1},
            "workday_companies": ["Fabrikam Motion", "Umbrella Medical"], "blocked_companies": [],
            "last_run": f"{TODAY} 07:31", "last_summary": "12 new roles from 37 company boards",
            "last_error": "", "fill_me": [], "tracker": "",
        },
        "email": {
            "installed": True, "sent_total": 128, "sent_today": EMAIL_BY_DAY[day(0)], "sent_week": sum(EMAIL_BY_DAY.get(day(o), 0) for o in range(-6, 1)), "series": EMAIL_SERIES,
            "regions": [["Singapore", 44], ["Penang", 21], ["Kuala Lumpur", 18], ["Bangkok", 15], ["Johor", 12], ["Hsinchu", 9]],
            "recent": EMAIL_RECENT, "leads_total": 3, "leads": EMAIL_LEADS, "relay_ok": True,
            "followups_enabled": True, "followup_after_days": 7, "max_per_day": 40, "report_to": "",
            "last_run": f"{TODAY} 09:12", "last_run_age_hours": 0.6, "bigtech_last_run": f"{TODAY} 08:00", "log_tail": [],
        },
        "promote": {
            "installed": False,
            "planned": [
                "Find creators / communities that fit each project (UGC, newsletters, small YouTubers)",
                "Write a short, personal pitch per contact from the project's real README facts",
                "Send from your Gmail relay with daily caps and one polite follow-up",
                "Track replies and which projects get picked up, next to LinkedIn reach",
            ],
            "folder": "",
        },
        "daily": DAILY,
        "tasks": {
            "linkedin": task("LinkedIn posts", "Sat 7 Nov · 09:30", "Sat 7 Nov · 10:00"),
            "jobs": task("Job search", "Sat 7 Nov · 07:30", "Sat 7 Nov · 12:30"),
            "email": task("Email applications", "Sat 7 Nov · 09:00", "Sat 7 Nov · 10:00"),
            "bigtech": task("Big Tech watch", "Sat 7 Nov · 08:00", "Sat 7 Nov · 12:00"),
            "patents": task("Patent screen", "Sat 7 Nov · 09:00", "Sun 8 Nov · 09:00"),
        },
        "patents": PATENTS,
        "todo": [
            {"priority": 1, "module": "Jobs", "title": "1 application stopped and needs you",
             "detail": "The site asked for an e-mailed verification code. ReachPilot never answers human checks; open the apply session and finish it.",
             "action": "jobs_apply", "action_label": "Finish stopped application",
             "links": [{"label": "Globex Hardware - NPI Mechanical Engineer", "url": "#"}]},
            {"priority": 2, "module": "Jobs", "title": "7 jobs ready to apply today",
             "detail": "Forms are pre-filled with your CV, cover letter and answers. You glance and press Submit.",
             "action": "jobs_apply", "action_label": "Start apply session", "links": []},
            {"priority": 3, "module": "Jobs", "title": "Workday sites need your own sign-in",
             "detail": "Fabrikam Motion and Umbrella Medical use Workday accounts, so these are applied by hand.",
             "action": None, "action_label": "", "links": []},
            {"priority": 4, "module": "Email", "title": "3 good leads have no recruiter email",
             "detail": "They can only be applied to on the company site.",
             "action": "email_open_leads", "action_label": "Open leads", "links": []},
        ],
        "help": [
            {"module": "LinkedIn", "runs": "Every 30 minutes; posts Tue/Wed/Thu 08:00.",
             "auto": "Picks a project, writes the post from real README facts, adds screenshots, posts through the official API, learns from clicks and stars.",
             "you": "Renew the LinkedIn login every 60 days (you get a reminder here and a Windows notification)."},
            {"module": "Jobs", "runs": "Searches company job boards at 07:30 and 12:30.",
             "auto": "Finds roles that fit your CV, scores them, writes cover letters, keeps the tracker. The apply session fills each form for you.",
             "you": "Open the apply session, glance at each form, press Submit and type an e-mailed code if a site asks. Sites with a human check never get submitted by the bot."},
            {"module": "Email", "runs": "E-mail applications every hour; Big Tech watch every 4 hours.",
             "auto": "Reads job ads, and when an ad lists a recruiter e-mail it sends your CV and a tailored letter from your sending Gmail; replies go to your personal inbox. One follow-up after 7 days if no reply.",
             "you": "Read replies and answer the ones you like."},
            {"module": "Promote", "runs": "Planned.",
             "auto": "Will find creators and communities that fit each project and send a short personal pitch.",
             "you": "Approve the first batch of pitches."},
        ],
    },
    "schedule": SCHEDULE,
    "history": [
        {"slot": f"{day(-2)} 08:00", "title": "weather-kit", "hook": "A $9 weather station that logs to a spreadsheet.",
         "status": "posted", "impressions": 512, "engagements": 23, "visitors": 14, "stars": 3, "try_clicks": 6, "url": ""},
        {"slot": f"{day(-3)} 08:00", "title": "gear-calc", "hook": "Spur gear sizing in one screen, no spreadsheet.",
         "status": "posted", "impressions": 388, "engagements": 15, "visitors": 9, "stars": 1, "try_clicks": 4, "url": ""},
        {"slot": f"{day(-8)} 08:00", "title": "lab-notes", "hook": "Every test log, searchable the moment it lands.",
         "status": "posted", "impressions": 701, "engagements": 34, "visitors": 22, "stars": 5, "try_clicks": 0, "url": ""},
        {"slot": f"{day(-9)} 08:00", "title": "pocket-cad", "hook": "A tiny parametric CAD sketcher in the browser.",
         "status": "posted", "impressions": 934, "engagements": 47, "visitors": 31, "stars": 8, "try_clicks": 19, "url": ""},
        {"slot": f"{day(-10)} 08:00", "title": "weather-kit", "hook": "What broke in month one of an outdoor sensor.",
         "status": "skipped", "impressions": None, "engagements": None, "visitors": None, "stars": None, "try_clicks": None, "url": ""},
    ],
    "github": {
        "latest_day": day(-1),
        "repos": [
            {"repo": "pocket-cad", "stars": 24, "li_uniques": 31, "li_views": 58, "clicks_30d": 19, "last_posted": day(-6), "posts": 3},
            {"repo": "lab-notes", "stars": 11, "li_uniques": 14, "li_views": 22, "clicks_30d": 8, "last_posted": day(-13), "posts": 2},
            {"repo": "weather-kit", "stars": 7, "li_uniques": 9, "li_views": 15, "clicks_30d": 5, "last_posted": day(-20), "posts": 2},
            {"repo": "gear-calc", "stars": 5, "li_uniques": 4, "li_views": 6, "clicks_30d": 2, "last_posted": None, "posts": 0},
        ],
        "stars_series": [{"x": day(-6 + i), "y": y} for i, y in enumerate([40, 41, 41, 43, 44, 46, 47])],
        "visitors_series": [{"x": day(-6 + i), "y": y} for i, y in enumerate([3, 8, 5, 4, 11, 6, 9])],
        "clicks_series": [{"x": day(-6 + i), "y": y} for i, y in enumerate([1, 5, 3, 2, 7, 4, 6])],
        "clicks_30d_total": 34,
    },
    "learning": {"records": 0, "repo": [], "angle": []},
    "log": [
        f"{TODAY} 09:30:02,114 INFO Tick: next slot Tue 08:00 (pocket-cad, launch angle)",
        f"{TODAY} 09:00:01,870 INFO GitHub: 4 public repos, no new releases",
        f"{day(-1)} 08:00:04,532 INFO Posted lab-notes (deep dive) through the official API",
    ],
}

def search_page(url, kind, issues=(), told=day(-1), google="", listed=True):
    return {"url": url, "kind": kind, "title": "", "issues": list(issues), "in_sitemap": listed, "told": told if listed else "",
            "google": google, "crawled": day(-2) if google == "in Google" else "", "status": 200, "on_site": True}


SEARCH = {
    "installed": True, "ran": f"{TODAY} 06:40", "host": "demo.github.io", "sitemap": "https://demo.github.io/sitemap.xml",
    "google_connected": True, "door": "api.indexnow.org", "notes": ["told 2 new or changed page(s) through api.indexnow.org"],
    "counts": {"projects": 4, "pages": 7, "trouble": 2, "in_sitemap": 7, "told": 7, "in_google": 3, "asked_google": 5},
    "projects": [
        {"name": "lab-notes", "trouble": 1, "pages": [
            search_page("https://demo.github.io/lab-notes/", "site", google="in Google"),
            search_page("https://demo.github.io/watch/lab-notes/", "watch", ["its title is only 9 characters (\"Lab notes\")"])]},
        {"name": "torque-log", "trouble": 1, "pages": [
            search_page("https://demo.github.io/torque-log/", "site", ["has no description, so a search result shows whatever text comes first"])]},
        {"name": "pocket-cad", "trouble": 0, "pages": [
            search_page("https://demo.github.io/pocket-cad/", "site", google="in Google"),
            search_page("https://demo.github.io/p/pocket-cad/", "landing", google="not in Google yet")]},
        {"name": "fixture-kit", "trouble": 0, "pages": [
            search_page("https://demo.github.io/fixture-kit/", "site", google="in Google"),
            search_page("https://demo.github.io/p/fixture-kit/", "landing", google="not in Google yet", told=day(0))]},
    ],
}
DEMO["hub"]["findable"] = SEARCH
import findable  # noqa: E402  (the real To do rule)

DEMO["hub"]["todo"] = sorted(DEMO["hub"]["todo"] + hub.patents_todo(PATENTS) + findable.todo(SEARCH), key=lambda item: item["priority"])
DEMO["hub"]["help"] += [entry for entry in hub.help_sections({}, None, None, PATENTS) if entry["module"] == "Patents"]

ACTION_REPLIES = {
    "patents_run_now": "Demo: on your PC this reads every repository you own again and updates this card in about a minute.",
    "findable_run_now": "Demo: on your PC this reads every project page again, updates the sitemap and tells the search engines what changed.",
    "jobs_apply": "Demo: on your PC this opens the apply session, which fills each form and waits for you to press Submit.",
    "email_open_leads": "Demo: on your PC this opens the list of good jobs that have no recruiter e-mail.",
}

SHIM = """<script>
/* ReachPilot public demo: serve made-up data instead of the local API. */
(() => {
  const DEMO = %s;
  const REPLIES = %s;
  const realFetch = window.fetch.bind(window);
  const json = (body) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  window.fetch = async (url, options = {}) => {
    const path = String(url);
    if (path.endsWith("/api/data")) return json(DEMO);
    if (path.endsWith("/api/action")) {
      let name = "";
      try { name = JSON.parse(options.body || "{}").name || ""; } catch (error) { name = ""; }
      const fallback = name.startsWith("patents:decide:")
        ? "Demo: on your PC this records your decision for that project, and its to-do item goes away."
        : name.startsWith("patents:open:")
        ? "Demo: on your PC this opens that project's dossier folder."
        : name.startsWith("patents:")
        ? "Demo: on your PC this is saved in your patent notes and the desk card updates."
        : "Demo: on your PC this button runs that task right away.";
      return json({ message: REPLIES[name] || fallback });
    }
    return realFetch(url, options);
  };
})();
</script>
"""

BANNER = (  # the first thing on every page of the demo, in the page's own amber note colours (design.md)
    '<div role="note" style="margin:0 0 24px;padding:8px 14px;border:1px solid #e3cf9c;border-radius:6px;'
    'background:#f6e8c9;color:#5c3a04;font-size:13.5px;line-height:1.45">'
    "Demo with made-up data. ReachPilot itself runs locally on Windows: "
    '<a href="https://github.com/juw12/reachpilot" style="color:#5c3a04;font-weight:600;display:inline-block;padding:13px 4px;margin:-13px 0">source on GitHub</a>'
    "</div>"
)
FONTS = ("instrument-serif-normal.woff2", "geist-normal.woff2", "geist-mono-normal.woff2", "LICENSE.md")

ICONS = {"icon.svg": "icon.svg", "icon-192.png": "icon-192.png", "favicon.ico": "app.ico"}

MANIFEST = {
    "name": "ReachPilot (demo)",
    "short_name": "ReachPilot",
    "start_url": "./",
    "display": "standalone",
    "background_color": "#f4f1ea",
    "theme_color": "#f4f1ea",
    "icons": [
        {"src": "icon.svg", "sizes": "any", "type": "image/svg+xml"},
        {"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
    ],
}


def build(source: Path) -> Path:
    page = source.read_text(encoding="utf-8")
    page = page.replace("__RP_TOKEN__", "demo")
    for absolute in ('href="/icon.svg"', 'href="/favicon.ico"', 'href="/icon-192.png"',
                     'href="/manifest.webmanifest"', 'src="/icon.svg"'):
        page = page.replace(absolute, absolute.replace('="/', '="'))
    page = page.replace('url("/fonts/', 'url("fonts/')
    assert "<b>This PC</b>" in page, "the sidebar status line moved: update the demo label"
    page = page.replace("<b>This PC</b>", "<b>Demo data</b>", 1)
    page = page.replace(
        "<title>ReachPilot</title>",
        "<title>ReachPilot demo: a posting schedule made from your GitHub projects</title>\n"
        '<meta name="description" content="ReachPilot writes each post from a repo\'s real facts, posts it to LinkedIn and '
        'other platforms, and shows reach, to-dos and whether search engines can find each project. A live dashboard with made-up data.">',
        1,
    )
    shim = SHIM % (json.dumps(DEMO, ensure_ascii=False), json.dumps(ACTION_REPLIES))
    page = page.replace("<head>", "<head>\n" + shim, 1)
    assert page.count("<main>") == 1, "the demo banner goes first inside <main>"
    page = page.replace("<main>", "<main>\n  " + BANNER, 1)

    out = DOCS / "index.html"
    out.write_text(page, encoding="utf-8")
    for target, name in ICONS.items():
        shutil.copyfile(ROOT / "icons" / name, DOCS / target)
    (DOCS / "fonts").mkdir(exist_ok=True)
    for name in FONTS:
        shutil.copyfile(ROOT / "fonts" / name, DOCS / "fonts" / name)
    (DOCS / "manifest.webmanifest").write_text(json.dumps(MANIFEST, indent=2), encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    return out


if __name__ == "__main__":
    source_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dashboard.html"
    print(build(source_path))
