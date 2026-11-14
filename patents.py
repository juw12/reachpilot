"""Patent screen: which of my GitHub projects are worth showing to a patent attorney, and until when.

A weekly first pass over every repository I own (public and private). For each one it reads the description
and the README and answers three separate questions:

    score    does the text describe a technical mechanism (not just an app), with evidence quoted from the README
    window   is a filing still possible: private = not disclosed; public = 12 months from the day the repo was created
    route    what protects it in practice: an attorney look, a defensive publication, a licence, or nothing

It is a keyword screen, not a legal opinion. It cannot judge novelty or inventive step, it does not search
prior art, and it does not know what you showed people outside GitHub. Its job is to make sure nothing
technical slips past a deadline unread. Results stay on this PC (data/patents/, git-ignored) because they
name private repositories.

    python patents.py                     scan once (what the scheduled task does)
    python patents.py install             create / refresh the weekly task \\ReachPilot\\Patent screen
    python patents.py decide NAME WHAT    record your decision: file | publish | licence | skip | clear
    python patents.py show                print the ranked result of the last scan
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = CODE_DIR / "data" / "patents"  # git-ignored
SCAN_FILE = DATA_DIR / "scan.json"
OVERRIDES_FILE = DATA_DIR / "overrides.json"  # your own notes and decisions, never overwritten by a scan
LOG_FILE = CODE_DIR / "patents.log"  # git-ignored (*.log)
TASK_PATH = "\\ReachPilot\\"
TASK_NAME = "Patent screen"
RUBRIC = "2026-11c"  # change when the word lists or the points change, so every repo is scored again

GRACE_MONTHS = 12  # own-disclosure grace period in the US (35 U.S.C. 102(b)(1)), Japan, South Korea and Taiwan
SOON_DAYS = 90
URGENT_DAYS = 30
BAND_LOOK = 60
BAND_MAYBE = 40
BANDS = {"look": "worth an attorney look", "maybe": "maybe", "unlikely": "unlikely"}
RATINGS = {"candidate": "look", "maybe": "maybe", "low": "unlikely"}  # a reviewed rating outranks the keyword band
DECISIONS = {
    "file": "file",
    "publish": "publish defensively",
    "licence": "licence only",
    "skip": "skip",
}
PERMISSIVE = {"mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "unlicense", "cc0-1.0", "0bsd", "mit-0"}
README_LIMIT = 20000  # characters of README text that are scored
HEAD_LIMIT = 1200  # the part that says what the project is

# ------------------------------------------------------------------ rubric (transparent on purpose)

# Subject class: highest class wins. (key, points, label, terms looked for in the description and the README's opening)
SUBJECTS = (
    ("physical", 30, "Physical part or engineering method", (
        "topology-optimised", "topology-optimized", "topology optimisation", "topology optimization", "finite element",
        "bearing", "tolerance stack", "stack-up", "step assembly", "cad", "stl", "fdm", "3d printer", "5-axis",
        "pcb", "firmware", "sensor", "actuator", "fixture", "enclosure", "von mises", "torque", "2d drawings",
    )),
    ("systems", 25, "Core systems technique", (
        "microkernel", "kernel", "compiler", "type checker", "type system", "typed", "programming language",
        "scripting language", "interpreter", "bytecode", "sandbox", "protocol", "end-to-end encrypted", "encryption",
        "crdt", "capability-based", "device drivers", "file system", "operating system", "consensus",
    )),
    ("algorithm", 20, "Algorithm, model or data method", (
        "embedding", "neural", "trained", "machine learning", "classifier", "optimiser", "optimizer", "monte carlo",
        "simulation", "simulations", "solver", "ranks", "ranked", "detects", "scans", "scanner", "fingerprint", "ocr",
        "predicts", "estimate", "fairest", "analyzer", "analyser", "merge gate", "bisected",
    )),
    ("tooling", 12, "Automation or developer tooling", (
        "automation", "automations", "cli", "dashboard", "workflow", "proxy", "hooks", "agents", "agent", "scheduled",
        "tracker", "calculator", "tray", "mcp", "skills", "tools", "tool",
    )),
)
CONTENT = (3, "App, content, game or list")
CONTENT_TERMS = (
    "game", "games", "pwa", "curated list", "awesome", "podcast", "cozy", "invitations", "life sim",
    "videos", "portfolio", "prototypes", "config files", "community health", "lucky cat", "pokedex",
)

# Mechanism words: the "how", not the "what". "a|b" are spellings of one idea and count once.
# 3 points per distinct idea, at most 30.
MECHANISM_POINTS, MECHANISM_CAP = 3, 30
MECHANISM_TERMS = (
    "algorithm", "solver", "eigenvalues", "p-norm", "multigrid", "signed-distance", "marching", "voxel",
    "finite element", "simp", "embedding|embeddings", "cosine", "trained", "loss function", "checksum",
    "pins|pinning", "syscall", "ipc", "capability|capabilities", "bisected|bisects", "compare-and-swap",
    "merge-tree", "parser", "type checker", "unit mismatch", "units of measure", "fuel limit|fuel metering",
    "metering", "monte carlo", "root sum square|root-sum-square", "cpk", "worst case|worst-case", "outlines",
    "ocr", "classifier", "heuristic", "zero-width", "look-alike|homoglyph", "quarantine", "provenance", "crdt",
    "replayed", "travel time", "affinity", "encrypted", "key exchange", "fuzz|fuzzed", "microkernel", "sandbox",
    "protocol", "overhang", "compliance", "isolation", "interpolation", "fsrs", "pitch contour",
)

# Technical-effect words: a measured result. 4 points per distinct signal, at most 20.
EFFECT_POINTS, EFFECT_CAP = 4, 20
EFFECT_TERMS = (
    "measured", "benchmark|benchmarks", "compared with", "accuracy", "recall", "false positives", "error rate",
    "faster", "reduces", "within about", "pass/fail",
)
EFFECT_NUMBER = re.compile(
    r"\d(?:[\d.,]*\d)?\s?(?:µm|mpa|n·mm|arcmin|ms|dof|x faster|% (?:faster|fewer|less|lower|smaller))", re.I)

# Novelty words: the author says it is new. 5 points each, at most 10.
NOVELTY_POINTS, NOVELTY_CAP = 5, 10
NOVELTY_TERMS = ("novel", "no competitor", "the only", "first to", "from scratch", "new method", "whitepaper")

# Your own note that the repo holds patent material is the strongest signal a README can carry.
MARKED_POINTS = 40
MARKED = re.compile(
    r"unfiled patent\w*|patentable|destroy novelty|patent novelty|patent strategy|patent pending|"
    r"provisional (?:patent )?application", re.I)
# Text that says the repo must stay private. A public repo that says so is reported as exposed.
CONFIDENTIAL = re.compile(
    r"do not (?:make this repository public|publish)|confidential|unfiled patent|destroy novelty|patent novelty", re.I)


def _term_pattern(term: str) -> re.Pattern:
    spellings = "|".join(re.escape(spelling) for spelling in term.split("|"))
    return re.compile(r"(?<![a-z0-9])(?:" + spellings + r")(?![a-z0-9])")


_PATTERNS: dict[str, re.Pattern] = {}


def _has(term: str, text: str) -> re.Match | None:
    """First match of a term in lower-case text. "a|b" matches either spelling."""
    pattern = _PATTERNS.get(term)
    if pattern is None:
        pattern = _PATTERNS[term] = _term_pattern(term)
    return pattern.search(text)


def plain_text(markdown: str) -> str:
    """README markdown as plain prose: no HTML, badges, images, code blocks or tables rules."""
    text = re.sub(r"<!--.*?-->", " ", markdown or "", flags=re.S)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[*_`#>|]+", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def _evidence(text: str, position: int, limit: int = 170) -> str:
    """The sentence (or line) around a match, shortened to fit a table cell."""
    start = max(text.rfind("\n", 0, position), text.rfind(". ", 0, position)) + 1
    ends = [end for end in (text.find("\n", position), text.find(". ", position)) if end != -1]
    end = min(ends) if ends else len(text)
    sentence = " ".join(text[start:end].split()).strip(" .:-")
    if len(sentence) <= limit:
        return sentence
    offset = max(0, min(position - start - limit // 3, len(sentence) - limit))
    cut = sentence[offset:offset + limit].strip()
    return ("\u2026" if offset else "") + cut + ("\u2026" if offset + limit < len(sentence) else "")


def _distinct_hits(terms, lowered: str, original: str) -> list[tuple[str, str]]:
    hits = []
    for term in terms:
        match = _has(term, lowered)
        if match:
            hits.append((match.group(0), _evidence(original, match.start())))
    return hits


def score_text(description: str, readme: str) -> dict:
    """Score one project from its own words. Every signal carries the words found and one quoted sentence."""
    description = " ".join((description or "").split())
    body = plain_text(readme)[:README_LIMIT]
    full = (description + "\n" + body).strip()
    lowered = full.lower()
    head = lowered[:len(description) + 1 + HEAD_LIMIT]
    lowered_description = description.lower()

    subject_key, subject_points, subject_label, subject_hits = "content", CONTENT[0], CONTENT[1], []
    content_hits = [term for term in CONTENT_TERMS if _has(term, lowered_description)]
    for key, points, label, terms in SUBJECTS:
        hits = _distinct_hits(terms, head, full)
        in_description = any(_has(term, lowered_description) for term, _ in hits)
        strong = len(hits) >= 2 and (in_description or len(hits) >= 3)
        if content_hits and key in ("algorithm", "tooling"):
            continue  # an app that mentions a tool word is still an app
        if strong or (in_description and key != "tooling") or (key == "tooling" and hits):
            subject_key, subject_points, subject_label, subject_hits = key, points, label, hits
            break

    signals = [{
        "key": "subject", "label": subject_label, "points": subject_points,
        "words": [term for term, _ in subject_hits][:6] or content_hits[:6],
        "evidence": subject_hits[0][1] if subject_hits else description[:170],
    }]
    for key, label, terms, per, cap in (
        ("mechanism", "Describes how it works", MECHANISM_TERMS, MECHANISM_POINTS, MECHANISM_CAP),
        ("effect", "States a measured result", EFFECT_TERMS, EFFECT_POINTS, EFFECT_CAP),
        ("novelty", "Says it is new", NOVELTY_TERMS, NOVELTY_POINTS, NOVELTY_CAP),
    ):
        hits = _distinct_hits(terms, lowered, full)
        if key == "effect":
            number = EFFECT_NUMBER.search(full)
            if number:
                hits.append((number.group(0).strip(), _evidence(full, number.start())))
        if hits:
            signals.append({
                "key": key, "label": label, "points": min(cap, per * len(hits)),
                "words": [term for term, _ in hits][:8], "evidence": hits[0][1],
            })

    total = sum(signal["points"] for signal in signals)
    if subject_key == "content":
        total = min(total, BAND_MAYBE - 1)  # an app or a list is never a candidate on keywords alone
    elif subject_key == "tooling":
        total = min(total, BAND_LOOK - 1)
    marked = MARKED.search(full)
    if marked:  # added after the caps: your own note outranks what kind of project it looks like
        signals.append({
            "key": "marked", "label": "You wrote that it holds patent material", "points": MARKED_POINTS,
            "words": [marked.group(0).lower()], "evidence": _evidence(full, marked.start()),
        })
        total += MARKED_POINTS
    total = min(total, 100)
    band = "look" if total >= BAND_LOOK else "maybe" if total >= BAND_MAYBE else "unlikely"
    return {
        "score": total, "band": band, "subject": subject_key, "signals": signals,
        "confidential": bool(CONFIDENTIAL.search(full)),
    }


# ------------------------------------------------------------------ filing window

def _parse_day(value) -> date | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def add_months(day: date, months: int) -> date:
    year = day.year + (day.month - 1 + months) // 12
    month = (day.month - 1 + months) % 12 + 1
    for last in (day.day, 30, 29, 28):  # 31 Jan + 1 month, 29 Feb + 12 months
        try:
            return date(year, month, last)
        except ValueError:
            continue
    return date(year, month, 28)


def window(private: bool, created, today: date | None = None) -> dict:
    """Can a patent still be filed, judged only from this repository's visibility and age.

    A public repo counts as disclosed on the day it was created: GitHub does not say when a repo turned
    public, and the earliest possible day gives the safest deadline.
    """
    today = today or date.today()
    if private:
        return {"state": "open", "deadline": None, "days_left": None,
                "text": "Not public: nothing disclosed by this repo, every country still open."}
    disclosed = _parse_day(created)
    if disclosed is None:
        return {"state": "unknown", "deadline": None, "days_left": None,
                "text": "Public, creation date unknown: assume disclosed."}
    deadline = add_months(disclosed, GRACE_MONTHS)
    days_left = (deadline - today).days
    if days_left < 0:
        return {"state": "closed", "deadline": deadline.isoformat(), "days_left": days_left,
                "text": f"Public for over {GRACE_MONTHS} months: closed everywhere. It now stops others patenting it."}
    return {"state": "grace", "deadline": deadline.isoformat(), "days_left": days_left,
            "text": f"Public since {disclosed.isoformat()}: US, Japan, Korea and Taiwan until {deadline.isoformat()} "
                    f"({days_left} days). Europe and China: already closed."}


def licence_kind(key: str | None) -> tuple[str, str]:
    key = (key or "").lower()
    if not key:
        return "none", "No licence: all rights reserved, copying needs your permission."
    if key in PERMISSIVE:
        return "permissive", f"{key.upper()}: anyone may copy, change and sell it. A patent would not undo that for this code."
    return "custom", "Custom or copyleft licence: read its terms."


def route(band: str, private: bool, fork: bool, archived: bool, window_state: str, licence: str) -> dict:
    """What realistically protects this project. One line, most useful action first."""
    if fork:
        return {"key": "none", "text": "A fork of someone else's work: not yours to protect."}
    if band == "unlikely":
        if private:
            return {"key": "private", "text": "No patent route seen. Keep it private, or pick a licence before publishing."}
        if licence == "permissive":
            return {"key": "licence", "text": "No patent route seen. To make copying cost something: change the licence for new versions, or protect the name as a trademark."}
        return {"key": "licence", "text": "No patent route seen. Copyright already covers the code; the name can be a trademark."}
    if archived:
        return {"key": "none", "text": "Archived: decide only if you revive it."}
    if private:
        return {"key": "attorney", "text": "Keep it private and show it to a patent attorney before anything is published."}
    if window_state == "grace":
        return {"key": "attorney", "text": "Already public. If it matters commercially, see an attorney before the 12-month date; otherwise it stands as a defensive publication."}
    return {"key": "defensive", "text": "Too late to patent. It is prior art now: nobody else can patent it either."}


# ------------------------------------------------------------------ scan

def _load(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def load_overrides() -> dict:
    value = _load(OVERRIDES_FILE, {})
    return value if isinstance(value, dict) else {}


def list_repos(github_user: str) -> list[dict]:
    """Every repo I own, private ones included when gh is signed in; public ones only otherwise."""
    import github_source as gh

    repos, page = [], 1
    while page <= 10:
        batch = gh.gh_api(f"user/repos?per_page=100&affiliation=owner&page={page}")
        if not isinstance(batch, list) or not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    if not repos:
        batch = gh.gh_api(f"users/{github_user}/repos?per_page=100&type=owner")
        repos = batch if isinstance(batch, list) else []
    return [repo for repo in repos if isinstance(repo, dict) and repo.get("name")]


def read_readme(full_name: str) -> str:
    import github_source as gh

    raw = gh.gh_api(f"repos/{full_name}/readme", raw=True)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw if isinstance(raw, str) else ""


def build_entry(repo: dict, readme: str) -> dict:
    licence = ((repo.get("license") or {}).get("key") or "") if isinstance(repo.get("license"), dict) else ""
    scored = score_text(repo.get("description") or "", readme)
    fork, archived = bool(repo.get("fork")), bool(repo.get("archived"))
    if fork:
        scored.update({"score": 0, "band": "unlikely", "signals": [], "confidential": False})
    return {
        "name": repo["name"], "full_name": repo.get("full_name") or repo["name"],
        "url": repo.get("html_url") or "", "private": bool(repo.get("private")), "fork": fork, "archived": archived,
        "description": " ".join((repo.get("description") or "").split())[:240],
        "created": repo.get("created_at") or "", "pushed": repo.get("pushed_at") or "",
        "licence": licence, "has_readme": bool(readme.strip()), "rubric": RUBRIC, **scored,
    }


def changes_since(previous: list[dict], current: list[dict]) -> list[dict]:
    """What is new since the last scan: new projects, a score band that moved, a repo that turned public."""
    before = {entry.get("name"): entry for entry in previous if isinstance(entry, dict)}
    found = []
    for entry in current:
        old = before.get(entry["name"])
        if old is None:
            if before and entry["band"] != "unlikely":
                found.append({"repo": entry["name"], "kind": "new", "text": f"New project, {BANDS[entry['band']]}."})
            continue
        if old.get("private") and not entry["private"]:
            found.append({"repo": entry["name"], "kind": "public", "text": "Was private, is public now: the 12-month clock runs."})
        if old.get("band") != entry["band"] and old.get("rubric") == entry["rubric"]:
            found.append({"repo": entry["name"], "kind": "band",
                          "text": f"Was {BANDS.get(old.get('band'), 'unknown')}, now {BANDS[entry['band']]}."})
    return found


def scan(github_user: str | None = None) -> dict:
    """Read every repo, score it, keep the result. A repo that did not change since the last scan is not read again."""
    if github_user is None:
        github_user = _load(CODE_DIR / "config.json", {}).get("github_user", "")
    previous = _load(SCAN_FILE, {})
    previous_repos = previous.get("repos") if isinstance(previous, dict) and isinstance(previous.get("repos"), list) else []
    known = {entry.get("name"): entry for entry in previous_repos if isinstance(entry, dict)}
    repos = list_repos(github_user)
    if not repos:
        return {"ok": False, "message": "GitHub gave no repositories (offline, or gh not signed in). Kept the last scan."}
    entries = []
    for repo in repos:
        old = known.get(repo["name"])
        unchanged = (old and old.get("rubric") == RUBRIC and old.get("pushed") == (repo.get("pushed_at") or "")
                     and (old.get("description") or "") == " ".join((repo.get("description") or "").split())[:240])
        if repo.get("fork"):
            readme = ""
        elif unchanged:
            entries.append({**old, "private": bool(repo.get("private")), "archived": bool(repo.get("archived")),
                            "licence": ((repo.get("license") or {}).get("key") or "")})
            continue
        else:
            readme = read_readme(repo.get("full_name") or f"{github_user}/{repo['name']}")
        entries.append(build_entry(repo, readme))
    entries.sort(key=lambda entry: (-entry["score"], entry["name"].lower()))
    result = {
        "version": 1, "rubric": RUBRIC, "owner": github_user,
        "scanned_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "includes_private": any(entry["private"] for entry in entries),
        "repos": entries, "changes": changes_since(previous_repos, entries),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SCAN_FILE.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    counts = {band: sum(1 for entry in entries if entry["band"] == band and not entry["fork"]) for band in BANDS}
    return {"ok": True, "message": f"{len(entries)} repos read: {counts['look']} worth an attorney look, "
                                   f"{counts['maybe']} maybe, {len(result['changes'])} change(s)."}


# ------------------------------------------------------------------ view (what the dashboard shows)

def view(scan_data: dict, overrides: dict | None = None, today: date | None = None) -> list[dict]:
    """Scan entries with today's deadline, the route and my own notes merged in. Tolerates a damaged file.

    Sorted for reading: candidates first, then by keyword score.
    """
    overrides = overrides if isinstance(overrides, dict) else {}
    rows = []
    for entry in (scan_data or {}).get("repos") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        note = overrides.get(entry["name"]) if isinstance(overrides.get(entry["name"]), dict) else {}
        screen_band = entry.get("band") if entry.get("band") in BANDS else "unlikely"
        rating = note.get("rating") if note.get("rating") in RATINGS else ""
        band = RATINGS.get(rating, screen_band)  # a person read it: that reading wins over the keywords
        private, fork, archived = bool(entry.get("private")), bool(entry.get("fork")), bool(entry.get("archived"))
        when = window(private, entry.get("created"), today)
        licence, licence_text = licence_kind(entry.get("licence"))
        decision = note.get("decision") if note.get("decision") in DECISIONS else ""
        employer = note.get("owner") == "employer?"
        way = route(band, private, fork, archived, when["state"], licence)
        if employer and not fork:
            way = {"key": "employer", "text": "Made for or at work? Read your employment agreement first: "
                                              "work done for an employer usually belongs to the employer."}
        reviewed = str(note.get("reviewed") or "")
        signals = [signal for signal in entry.get("signals") or [] if isinstance(signal, dict)]
        rows.append({
            "name": entry["name"], "url": entry.get("url") or "", "description": entry.get("description") or "",
            "private": private, "fork": fork, "archived": archived,
            "score": int(entry.get("score") or 0), "band": band, "band_label": BANDS[band],
            "screen_band": screen_band, "rating": rating,
            "signals": [{"label": str(signal.get("label") or ""), "points": int(signal.get("points") or 0),
                         "words": [str(word) for word in signal.get("words") or []][:8],
                         "evidence": str(signal.get("evidence") or "")[:200]} for signal in signals],
            "window": when, "route": way,
            "licence": entry.get("licence") or "", "licence_kind": licence, "licence_text": licence_text,
            "confidential": bool(entry.get("confidential")),
            "exposed": bool(entry.get("confidential")) and not private,
            "employer": employer,
            "verdict": str(note.get("verdict") or "")[:400], "decision": decision,
            "decision_label": DECISIONS.get(decision, ""), "reviewed": reviewed,
            "changed_since_review": bool(reviewed) and str(entry.get("pushed") or "")[:10] > reviewed[:10],
        })
    order = {"look": 0, "maybe": 1, "unlikely": 2}
    rows.sort(key=lambda row: (row["fork"], order[row["band"]], -row["score"], row["name"].lower()))
    return rows


# ------------------------------------------------------------------ command line

def decide(name: str, decision: str) -> str:
    if decision != "clear" and decision not in DECISIONS:
        return f"Unknown decision '{decision}'. Use: {', '.join(DECISIONS)} or clear."
    names = {entry.get("name") for entry in (_load(SCAN_FILE, {}).get("repos") or []) if isinstance(entry, dict)}
    if name not in names:
        return f"No project called '{name}' in the last scan."
    overrides = load_overrides()
    note = overrides.get(name) if isinstance(overrides.get(name), dict) else {}
    if decision == "clear":
        note.pop("decision", None)
    else:
        note["decision"] = decision
    note["decided"] = date.today().isoformat()
    overrides[name] = note
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OVERRIDES_FILE.write_text(json.dumps(overrides, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return f"{name}: decision cleared." if decision == "clear" else f"{name}: {DECISIONS[decision]}."


def log(result: str) -> None:
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-199:] if LOG_FILE.exists() else []
    lines.append(f"{datetime.now():%Y-%m-%d %H:%M}  {result}")
    LOG_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def install_task() -> None:
    """Every Monday 09:00, hidden (pythonw), catching up if the PC was off."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    script = f"""
$action = New-ScheduledTaskAction -Execute '{runner}' -Argument '"{Path(__file__).resolve()}"' -WorkingDirectory '{CODE_DIR}'
$weekly = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 09:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' -Action $action -Trigger $weekly -Settings $settings `
    -Description 'Weekly patent screen of my GitHub projects: score, filing deadline, route. A screen, not legal advice.' -Force | Out-Null
Get-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' | Select-Object TaskName, State
"""
    print(subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True).stdout.strip())


def show() -> None:
    rows = [row for row in view(_load(SCAN_FILE, {}), load_overrides()) if not row["fork"]]
    for row in rows:
        when = row["window"]
        deadline = "private" if row["private"] else (when["deadline"] or when["state"])
        print(f"{row['score']:>3}  {row['band_label']:<22} {row['name']:<28} {deadline:<11} {row['route']['key']}")


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments == ["install"]:
        install_task()
    elif arguments == ["show"]:
        show()
    elif len(arguments) == 3 and arguments[0] == "decide":
        print(decide(arguments[1], arguments[2]))
    elif not arguments:
        try:
            outcome = scan()["message"]
        except Exception as error:  # a scheduled run must leave a line in the log, not a silent crash
            outcome = f"failed: {type(error).__name__}: {error}"
        log(outcome)
        if sys.stdout:
            print(outcome)
    else:
        print(__doc__)
