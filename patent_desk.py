"""Patent desk: what each candidate still needs before it can be filed, the filing package, and the dates after.

The patent screen (patents.py) says which projects may hold patent material. The desk takes those projects
(rated "worth an attorney look" or "maybe", and not decided as publish, licence only or skip) and follows each
one every day:

    dossier     data/patents/dossiers/NAME/dossier.json: the invention written down (problem, mechanism,
                a result with its source, example, figures, claims, closest prior art). A person or an agent
                fills it.
    readiness   eight checks over the dossier and the repository's test runs on GitHub, 100 points in total
    package     once the dossier has content: a draft US provisional specification (Markdown, HTML and PDF),
                the cover sheet fields (form PTO/SB/16), a filing checklist and notes for the attorney;
                for a ready package also the form itself with the dossier's values typed in, to check and sign
    dates       before filing: the 12-month date of a public repository; after filing: the 12 months to file
                the non-provisional or PCT application, which cannot be extended

What it never does: file anything. A filing needs your identity check at the patent office, your name as
inventor and your payment. It does not write the invention for you either: it checks what is written, and
says what is missing. Drafts are not legal advice and need an attorney's read. Everything stays on this PC
(data/patents/, git-ignored), because a draft pushed to a public repository is a disclosure.

    python patent_desk.py                           refresh once (the daily task runs it after the screen)
    python patent_desk.py show                      print stage, readiness and next step per project
    python patent_desk.py ownership NAME cleared    you checked your employment agreement: it is yours (or: unsure)
    python patent_desk.py filed NAME DATE NUMBER    record a filing, e.g. filed hinge-lab 2026-12-01 63/123,456
    python patent_desk.py unfiled NAME              remove a recorded filing
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

import patents

FOLLOW_UP_MONTHS = 12  # 35 U.S.C. 119(e): non-provisional or PCT within 12 months of the provisional; no extension
HISTORY_LIMIT = 60
NAME = re.compile(r"[A-Za-z0-9._-]{1,100}")
NUMBER = re.compile(r"[0-9/, ]{6,14}")
OFF_DESK = {"publish", "licence", "skip"}
STAGES = {
    "filed": "filed",
    "closed": "window closed",
    "ownership": "waiting on ownership",
    "disclosure": "needs an invention disclosure",
    "strengthen": "strengthening",
    "ready": "ready to file",
}
STAGE_ORDER = ("ready", "filed", "strengthen", "disclosure", "ownership", "closed")
DEPENDENT = re.compile(r"\b(?:of|to|in) (?:any (?:one )?of )?claims? \d", re.I)
KINDS = ("measured", "simulated", "computed")  # how a result was obtained; the specification says which
# A workflow run counts as a test run when a push or a schedule started it on the default branch and the
# workflow does not only ship something. A Pages build that went through says nothing about the tests.
TEST_EVENTS = {"push", "schedule"}
SHIPS = re.compile(r"pages|deploy|publish|release", re.I)
TESTS = re.compile(r"tests?\b|\bci\b|check|verify|lint", re.I)
NOTE = (
    "Drafts and a checklist, not legal advice. ReachPilot files nothing: a filing needs your identity check at "
    "the patent office, your name as inventor and your payment. Have an attorney read a draft before it is filed. "
    "The readiness number counts what is written down; it cannot judge novelty."
)
# USPTO fee schedule, provisional application filing fee (37 CFR 1.16(d)), read 2026-11. Check it before paying.
FEES = "325 USD (large entity), 130 USD (small entity), 65 USD (micro entity)"
EDGE = (
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
)
# Form PTO/SB/16, the provisional cover sheet, as the USPTO publishes it. The desk keeps one blank copy next to
# the dossiers (data/patents/forms/, git-ignored) and types a project's values into a copy of it. The names are
# the form's own field names. Address, entity status, fee, payment, government interest, signature and date are
# never typed in: they are yours.
SB16_URL = "https://www.uspto.gov/sites/default/files/documents/sb0016.pdf"
SB16_FILE = "sb16_prefilled.pdf"
SB16_ROWS = 5  # inventor rows on the form; a sixth inventor goes on the form's continuation sheet, by hand
SB16_ON = "/On"
SB16_FIELDS = {
    "given": "Given Name first and middle if anyRow{row}",
    "family": "Family Name or SurnameRow{row}",
    "residence": "Residence City and either State or Foreign CountryRow{row}",
    "title": "TITLE OF THE INVENTION 500 characters maxRow1",
    "specification": "Specification eg description of the invention",
    "pages": "Number of Pages",
    "drawings": "Drawings",
    "sheets": "Number of Sheets",
    "docket": "DOCKET NUMBER",
}


def dossiers_dir() -> Path:
    return patents.DATA_DIR / "dossiers"


def desk_file() -> Path:
    return patents.DATA_DIR / "desk.json"


def folder(name: str) -> Path:
    if not NAME.fullmatch(name) or set(name) == {"."}:
        raise ValueError(f"Not a project name: {name!r}")
    return dossiers_dir() / name


# ------------------------------------------------------------------ dossier

def _text(value) -> str:
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) else ""


def _items(value) -> list:
    return [item for item in value if item] if isinstance(value, list) else []


def load_dossier(name: str) -> dict | None:
    value = patents._load(folder(name) / "dossier.json", None)
    return value if isinstance(value, dict) else None


def is_empty(dossier: dict | None) -> bool:
    if not isinstance(dossier, dict):
        return True
    return not (_text(dossier.get("problem")) or _items(dossier.get("mechanism")) or _items(dossier.get("claims")))


def skeleton(row: dict) -> dict:
    """An empty dossier with the sentences the screen quoted, as a starting point for whoever writes it."""
    return {
        "title": "",
        "field": "",
        "problem": "",
        "mechanism": [],
        "variants": [],
        "effects": [],
        "example": {"steps": [], "output": ""},
        "tests_evidence": {"command": "", "date": "", "passed": 0, "failed": 0},
        "figures": [],
        "claims": [],
        "prior_art": [],
        "abstract": "",
        "inventors": [],
        "hints": [signal["evidence"] for signal in row.get("signals") or [] if signal.get("evidence")][:6],
    }


BRIEF = """# How to fill this dossier

`dossier.json` in this folder is the invention written down. The patent desk reads it every day, scores it
against eight checks, and builds the filing package in `provisional/` from it. It is a draft for a patent
attorney to read, not legal advice.

Rules

- Keep this folder private. Do not copy any of it into a repository, an issue, a post or a chat with a
  cloud model you do not control. A draft that becomes public is a disclosure.
- Write only what the repository really does. No invented numbers: every result names the file or the
  command that reproduces it, and says whether it was measured, simulated or computed.
- Describe the mechanism, not the product. What does it do, step by step, that a skilled person would not
  already do?

Fields

- `title`: a plain technical title, 500 characters at most (the cover sheet limit).
- `field`: one sentence, the technical field.
- `problem`: what fails today and for whom. At least 120 characters.
- `mechanism`: a list of steps, at least 4, concrete enough that someone could rebuild it.
- `variants`: other ways to build the same idea (other data, thresholds, orderings). A provisional
  application only protects what it describes, so list them.
- `effects`: a list of `{"result": "...", "source": "path or command", "kind": "measured"}`. At least one.
  `kind` is `measured` (a bench, a benchmark run, real data), `simulated` (a model of the thing) or
  `computed` (worked out from a formula or a solver). The specification says which; a simulation is never
  printed as a measurement.
- `example`: `{"steps": ["..."], "output": "..."}`: one worked run.
- `tests_evidence`: only if the repository has no test workflow on GitHub:
  `{"command": "python -m pytest", "date": "2026-11-01", "passed": 42, "failed": 0}` from a real local run.
  It counts only while GitHub reports no test runs; it never outweighs a failed run, and a sentence in
  place of the four fields does not count.
- `figures`: a list of `{"title": "...", "file": "fig1.svg"}`; the files sit in this folder.
- `claims`: a list of strings. One independent claim and at least three dependent ones
  ("The method of claim 1, wherein ..."). A provisional application does not need claims, but drafting
  them shows what is actually new.
- `prior_art`: a list of `{"reference": "...", "url": "...", "difference": "..."}`. The three closest,
  with what differs. This goes to the attorney, not into the specification.
- `abstract`: 150 words at most.
- `inventors`: a list of `{"given": "", "family": "", "residence": "city, country"}`. Only people who
  contributed to the idea in a claim.
"""


def ensure_skeleton(row: dict) -> bool:
    """Create the dossier folder for a project that has none. Never overwrites a dossier. True when created."""
    target = folder(row["name"])
    if (target / "dossier.json").exists():
        return False
    target.mkdir(parents=True, exist_ok=True)
    (target / "dossier.json").write_text(json.dumps(skeleton(row), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    (target / "BRIEF.md").write_text(BRIEF, encoding="utf-8")
    return True


# ------------------------------------------------------------------ readiness

def split_claims(claims) -> tuple[list[str], list[str]]:
    texts = [_text(claim) for claim in _items(claims)]
    texts = [text for text in texts if text]
    dependent = [text for text in texts if DEPENDENT.search(text)]
    return [text for text in texts if text not in dependent], dependent


def effect_kind(effect) -> str:
    """measured, simulated or computed; empty when the dossier does not say."""
    kind = _text(effect.get("kind")).lower() if isinstance(effect, dict) else ""
    return kind if kind in KINDS else ""


def local_run(value, today: date | None = None) -> dict | None:
    """A recorded local test run that can count: a command, a date that is not in the future, at least one test
    passed and none failed. A sentence, a missing field or a failure is None."""
    if not isinstance(value, dict):
        return None
    command = _text(value.get("command"))
    day = _day(value.get("date")) if isinstance(value.get("date"), str) else None
    passed, failed = value.get("passed"), value.get("failed")
    if not command or day is None or day > (today or date.today()):
        return None
    if type(passed) is not int or type(failed) is not int or passed < 1 or failed != 0:
        return None
    return {"command": command, "date": day.isoformat(), "passed": passed, "failed": failed}


def tests_pass(ci: dict, evidence, today: date | None = None) -> tuple[bool, str]:
    """Whether the tests count as passing, and what was found. Green test runs on GitHub count. A local run
    counts only when GitHub answered that the repository has no test runs: it never outweighs a failed run,
    and it does not count while the state is unknown."""
    state = ci.get("state") if ci.get("state") in ("success", "failure", "none") else "unknown"
    names = ", ".join(_text(name) for name in _items(ci.get("workflows")) if _text(name))
    counted = f" ({names})" if names else ""
    run = local_run(evidence, today)
    written = bool(_text(evidence)) or (isinstance(evidence, dict) and any(evidence.values()))
    if state == "success":
        return True, f"last CI run passed{counted}"
    if state == "failure":
        return False, f"last CI run failed{counted}" + ("; a local run does not count while CI fails" if written else "")
    if state == "unknown":
        return False, "CI state not read yet" + ("; a local run counts once GitHub has answered" if written else "")
    if run:
        return True, (f"no test runs on GitHub; local run on {run['date']}: {run['passed']} passed, "
                      f"{run['failed']} failed ({run['command'][:80]})")
    if isinstance(evidence, dict) and written:
        return False, "no test runs on GitHub; the local run under tests_evidence is incomplete or has failures"
    if written:
        return False, "no test runs on GitHub; tests_evidence is a sentence, not a run (command, date, passed, failed)"
    return False, "no test runs on GitHub"


def readiness(dossier: dict | None, ci: dict | None = None, figure_exists=None, today: date | None = None) -> list[dict]:
    """The eight checks. Each one: key, label, points, ok, what was found, and what to do when it fails."""
    dossier = dossier if isinstance(dossier, dict) else {}
    ci = ci if isinstance(ci, dict) else {}
    figure_exists = figure_exists or (lambda file: False)
    problem = _text(dossier.get("problem"))
    steps = [_text(step) for step in _items(dossier.get("mechanism")) if _text(step)]
    effects = [effect for effect in _items(dossier.get("effects"))
               if isinstance(effect, dict) and _text(effect.get("result")) and _text(effect.get("source"))]
    unsourced = len(_items(dossier.get("effects"))) - len(effects)
    example = dossier.get("example")
    has_example = bool(_text(example)) or (isinstance(example, dict) and bool(
        _items(example.get("steps")) or _text(example.get("command"))))
    labelled = [effect_kind(effect) for effect in effects]
    kinds = [f"{labelled.count(kind)} {kind}" for kind in KINDS if kind in labelled]
    if kinds and "" in labelled:
        kinds.append(f"{labelled.count('')} not labelled")
    tests_ok, tests_found = tests_pass(ci, dossier.get("tests_evidence"), today)
    figures = [figure for figure in _items(dossier.get("figures")) if isinstance(figure, dict) and _text(figure.get("file"))]
    present = [figure for figure in figures if figure_exists(_text(figure["file"]))]
    independent, dependent = split_claims(dossier.get("claims"))
    prior = [entry for entry in _items(dossier.get("prior_art"))
             if isinstance(entry, dict) and _text(entry.get("reference")) and _text(entry.get("difference"))]

    def check(key, label, points, ok, found, fix):
        return {"key": key, "label": label, "points": points, "ok": bool(ok), "found": found, "fix": fix}

    return [
        check("problem", "Problem stated", 10, len(problem) >= 120, f"{len(problem)} characters",
              "Write the problem in the dossier: what fails today and for whom, in at least 120 characters."),
        check("mechanism", "Mechanism in steps", 15, len(steps) >= 4, f"{len(steps)} step(s)",
              "List the mechanism as at least 4 steps, concrete enough that someone could rebuild it."),
        check("effect", "Result with a source", 15, len(effects) >= 1,
              f"{len(effects)} with a source" + (f", {unsourced} without" if unsourced else "")
              + (f" ({', '.join(kinds)})" if kinds else ""),
              "Add one result and name the file or command in the repository that reproduces it. "
              "Say whether it was measured, simulated or computed."),
        check("example", "Worked example", 10, has_example, "present" if has_example else "missing",
              "Add one worked example: the steps or the command, and what comes out."),
        check("tests", "Tests pass", 10, tests_ok, tests_found,
              "Get the repository's CI run green. If it has no test workflow on GitHub, record a real local run "
              "under tests_evidence: command, date, passed, failed."),
        check("figures", "At least one figure", 10, len(present) >= 1,
              f"{len(present)} of {len(figures)} listed file(s) found",
              "Put a flow chart or block diagram in the dossier folder and list it under figures."),
        check("claims", "Claims drafted", 15, len(independent) >= 1 and len(dependent) >= 3,
              f"{len(independent)} independent, {len(dependent)} dependent",
              "Draft one independent claim and at least three dependent claims."),
        check("prior_art", "Closest prior art compared", 15, len(prior) >= 3, f"{len(prior)} with a stated difference",
              "List the three closest prior-art references, each with what differs."),
    ]


# ------------------------------------------------------------------ the desk (pure: the public demo feeds it made-up dossiers)

def on_desk(rows: list[dict]) -> list[dict]:
    return [row for row in rows
            if not row.get("fork") and row.get("band") in ("look", "maybe") and row.get("decision") not in OFF_DESK
            and NAME.fullmatch(str(row.get("name") or "")) and set(row["name"]) != {"."}]


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _filing(note: dict) -> dict | None:
    filed = note.get("filed") if isinstance(note.get("filed"), dict) else None
    if not filed or _day(filed.get("date")) is None:
        return None
    return {"date": _day(filed["date"]).isoformat(), "number": _text(filed.get("number"))[:20]}


def project(row: dict, note: dict, dossier: dict | None, fact: dict, today: date, figure_exists=None) -> dict:
    checks = readiness(dossier, fact.get("ci"), figure_exists, today)
    score = sum(check["points"] for check in checks if check["ok"])
    failing = [check for check in checks if not check["ok"]]
    when = row["window"]
    filed = _filing(note)
    cleared = bool(_text(note.get("ownership_cleared")))
    deadline = None
    if filed:
        stage = "filed"
        due = patents.add_months(_day(filed["date"]), FOLLOW_UP_MONTHS)
        deadline = {"label": "non-provisional or PCT application due", "date": due.isoformat(), "days_left": (due - today).days}
        next_step = (f"File the non-provisional or PCT application by {due.isoformat()}. That date cannot be extended. "
                     "Book the attorney at least three months before it.")
        if deadline["days_left"] < 0:
            next_step = (f"The 12 months after the provisional ended on {due.isoformat()}. "
                         "Ask an attorney whether anything can still be done.")
    elif when["state"] == "closed":
        stage = "closed"
        next_step = "Public for over 12 months: nothing to file. Record the decision as publish or skip on the Patent screen card."
    elif row.get("employer") and not cleared:
        stage = "ownership"
        next_step = ("Read your employment agreement, or ask in writing who owns work like this. "
                     "Nothing is drafted until you mark it as yours.")
    elif is_empty(dossier):
        stage = "disclosure"
        next_step = "Write the invention down in the dossier: problem, mechanism in steps, one result with its source. BRIEF.md in the folder says how."
    elif failing:
        stage = "strengthen"
        next_step = failing[0]["fix"]
    else:
        stage = "ready"
        next_step = ("Read the package in the provisional folder. File it yourself at USPTO Patent Center, or hand it to "
                     "an attorney. Then record the date and the application number here.")
    if deadline is None and when["state"] == "grace":
        deadline = {"label": "12-month date of the public repository", "date": when["deadline"], "days_left": when["days_left"]}
    package = fact.get("package") if isinstance(fact.get("package"), dict) else {}
    return {
        "name": row["name"], "url": row.get("url") or "", "private": bool(row.get("private")),
        "band": row["band"], "employer": bool(row.get("employer")), "ownership_cleared": cleared,
        "stage": stage, "stage_label": STAGES[stage], "score": score,
        "passed": len(checks) - len(failing), "total": len(checks),
        "checks": [{key: check[key] for key in ("key", "label", "points", "ok", "found", "fix")} for check in checks],
        "next_step": next_step, "deadline": deadline, "filed": filed,
        "ci": str((fact.get("ci") or {}).get("state") or "unknown"),
        "package": {"built": str(package.get("built") or ""), "pdf": bool(package.get("pdf")),
                    "draft": bool(package.get("draft", True))} if package.get("built") else None,
    }


def build(rows: list[dict], overrides: dict | None, dossiers: dict | None, facts: dict | None, today: date | None = None,
          history: list | None = None, figure_exists=None) -> dict:
    """The desk from the screen's rows, my notes, the dossiers and the facts a refresh stored. Reads no files."""
    today = today or date.today()
    overrides = overrides if isinstance(overrides, dict) else {}
    dossiers = dossiers if isinstance(dossiers, dict) else {}
    facts = facts if isinstance(facts, dict) else {}
    figure_exists = figure_exists or (lambda name, file: False)
    projects = []
    for row in on_desk(rows):
        name = row["name"]
        note = overrides.get(name) if isinstance(overrides.get(name), dict) else {}
        fact = facts.get(name) if isinstance(facts.get(name), dict) else {}
        projects.append(project(row, note, dossiers.get(name), fact, today,
                                lambda file, name=name: figure_exists(name, file)))
    projects.sort(key=lambda item: (STAGE_ORDER.index(item["stage"]), -item["score"], item["name"].lower()))
    counts = {stage: sum(1 for item in projects if item["stage"] == stage) for stage in STAGES}
    return {
        "projects": projects, "counts": counts, "total": len(projects),
        "history": [entry for entry in history or [] if isinstance(entry, dict)][-HISTORY_LIMIT:],
        "note": NOTE,
    }


def changes(previous: list[dict], current: list[dict], today: date) -> list[dict]:
    """What moved since the last run: a project joined or left the desk, its stage changed, its readiness changed."""
    before = {item.get("name"): item for item in previous if isinstance(item, dict)}
    found = []

    def add(name, text):
        found.append({"date": today.isoformat(), "name": name, "text": text})

    for item in current:
        old = before.pop(item["name"], None)
        if old is None:
            add(item["name"], f"On the desk: {item['stage_label']}, readiness {item['score']} of 100.")
            continue
        if old.get("stage") != item["stage"]:
            add(item["name"], f"{STAGES.get(old.get('stage'), 'unknown')} -> {item['stage_label']}.")
        if old.get("score") != item["score"]:
            add(item["name"], f"Readiness {old.get('score')} -> {item['score']} of 100.")
    for name in before:
        add(name, "Left the desk (decided, or no longer rated as a candidate).")
    return found


# ------------------------------------------------------------------ the filing package

def _example_lines(example) -> tuple[list[str], str]:
    if isinstance(example, dict):
        steps = [_text(step) for step in _items(example.get("steps")) if _text(step)]
        if _text(example.get("command")):
            steps.insert(0, "Run: " + _text(example["command"]))
        return steps, str(example.get("output") or "").strip()
    return ([_text(example)] if _text(example) else []), ""


def specification(name: str, dossier: dict, ready: bool) -> list[tuple]:
    """The specification as blocks: ("h1"|"h2"|"p"|"note"|"ol"|"ul"|"pre"|"figure", content)."""
    title = _text(dossier.get("title")) or name
    blocks = [("h1", title)]
    if not ready:
        blocks.append(("note", "DRAFT, NOT READY TO FILE. The patent desk still lists open checks for this project."))
    blocks.append(("note", "Draft specification for a US provisional patent application (35 U.S.C. 111(b)). "
                           "Prepared from the invention dossier. Not legal advice; have an attorney read it before filing."))
    if _text(dossier.get("field")):
        blocks += [("h2", "Technical field"), ("p", _text(dossier["field"]))]
    blocks += [("h2", "Background"), ("p", _text(dossier.get("problem")) or "[not written yet]")]
    steps = [_text(step) for step in _items(dossier.get("mechanism")) if _text(step)]
    blocks += [("h2", "Summary"), ("p", _text(dossier.get("summary")) or _text(dossier.get("abstract")) or "[not written yet]")]
    figures = [figure for figure in _items(dossier.get("figures")) if isinstance(figure, dict) and _text(figure.get("file"))]
    if figures:
        blocks.append(("h2", "Brief description of the drawings"))
        blocks.append(("ul", [f"FIG. {index}: {_text(figure.get('title')) or _text(figure['file'])}"
                              for index, figure in enumerate(figures, 1)]))
    blocks += [("h2", "Detailed description"), ("ol", steps or ["[not written yet]"])]
    variants = [_text(variant) for variant in _items(dossier.get("variants")) if _text(variant)]
    if variants:
        blocks += [("h2", "Variations"), ("ul", variants)]
    example_steps, output = _example_lines(dossier.get("example"))
    if example_steps:
        blocks += [("h2", "Worked example"), ("ol", example_steps)]
        if output:
            blocks.append(("pre", output))
    effects = [effect for effect in _items(dossier.get("effects")) if isinstance(effect, dict) and _text(effect.get("result"))]
    if effects:
        # The heading names how the results were obtained only when every one says so and they agree.
        found = {effect_kind(effect) for effect in effects}
        same = len(found) == 1 and "" not in found
        blocks.append(("h2", f"{found.pop().capitalize()} results" if same else "Results"))
        blocks.append(("ul", [(f"{effect_kind(effect).capitalize()}: " if effect_kind(effect) and not same else "")
                              + _text(effect["result"])
                              + (f" (source: {_text(effect.get('source'))})" if _text(effect.get("source")) else "")
                              for effect in effects]))
    claims = [_text(claim) for claim in _items(dossier.get("claims")) if _text(claim)]
    if claims:
        blocks += [("h2", "Example claims"),
                   ("note", "A provisional application does not require claims. These show the scope the description is meant to support."),
                   ("ol", claims)]
    if _text(dossier.get("abstract")):
        blocks += [("h2", "Abstract"), ("p", _text(dossier["abstract"]))]
    for index, figure in enumerate(figures, 1):
        blocks.append(("figure", (index, _text(figure.get("title")), _text(figure["file"]))))
    return blocks


def to_markdown(blocks: list[tuple]) -> str:
    lines = []
    for kind, content in blocks:
        if kind == "h1":
            lines += [f"# {content}", ""]
        elif kind == "h2":
            lines += [f"## {content}", ""]
        elif kind == "p":
            lines += [content, ""]
        elif kind == "note":
            lines += [f"> {content}", ""]
        elif kind == "ol":
            lines += [f"{index}. {item}" for index, item in enumerate(content, 1)] + [""]
        elif kind == "ul":
            lines += [f"- {item}" for item in content] + [""]
        elif kind == "pre":
            lines += ["```", content, "```", ""]
        elif kind == "figure":
            index, title, file = content
            lines += [f"![FIG. {index}: {title}](../{file})", ""]
    return "\n".join(lines).rstrip() + "\n"


def to_html(blocks: list[tuple]) -> str:
    esc = html.escape
    parts = []
    title = "Specification"
    for kind, content in blocks:
        if kind == "h1":
            title = content
            parts.append(f"<h1>{esc(content)}</h1>")
        elif kind == "h2":
            parts.append(f"<h2>{esc(content)}</h2>")
        elif kind == "p":
            parts.append(f"<p>{esc(content)}</p>")
        elif kind == "note":
            parts.append(f'<p class="note">{esc(content)}</p>')
        elif kind in ("ol", "ul"):
            parts.append(f"<{kind}>" + "".join(f"<li>{esc(item)}</li>" for item in content) + f"</{kind}>")
        elif kind == "pre":
            parts.append(f"<pre>{esc(content)}</pre>")
        elif kind == "figure":
            index, caption, file = content
            parts.append(f'<figure><img src="../{esc(file, quote=True)}" alt="FIG. {index}">'
                         f"<figcaption>FIG. {index}{': ' + esc(caption) if caption else ''}</figcaption></figure>")
    style = ("body{font:12pt/1.5 'Times New Roman',serif;margin:2.5cm;color:#000}h1{font-size:16pt}h2{font-size:13pt;margin-top:1.4em}"
             ".note{font-size:10pt;border-left:3px solid #000;padding-left:8px}pre{font:10pt/1.4 Consolas,monospace;white-space:pre-wrap}"
             "figure{page-break-before:always;margin:0;text-align:center}img{max-width:100%;max-height:22cm}li{margin-bottom:.4em}")
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>{esc(title)}</title>'
            f"<style>{style}</style></head><body>\n" + "\n".join(parts) + "\n</body></html>\n")


def cover_sheet(name: str, dossier: dict, pages: int | None = None, prefilled: bool = False) -> str:
    title = (_text(dossier.get("title")) or name)[:500]
    figures = [figure for figure in _items(dossier.get("figures")) if isinstance(figure, dict) and _text(figure.get("file"))]
    inventors = [inventor for inventor in _items(dossier.get("inventors")) if isinstance(inventor, dict)]
    lines = [
        f"# Cover sheet fields: {name}", "",
        "The fields of form PTO/SB/16 (Provisional Application for Patent Cover Sheet). Copy them into the form, or",
        "into the web form at USPTO Patent Center. Blank lines are yours to fill. Not legal advice.", "",
        "## Inventor(s)", "",
    ]
    for inventor in inventors or [{}]:
        lines += [
            f"- Given name: {_text(inventor.get('given')) or '________'}",
            f"  Family name: {_text(inventor.get('family')) or '________'}",
            f"  Residence (city and country): {_text(inventor.get('residence')) or '________'}",
        ]
    lines += [
        "", "## Title of the invention (500 characters at most)", "", title, "",
        "## Correspondence address", "", "________ (or your USPTO customer number)", "",
        "## Enclosed application parts", "",
        (f"- Specification: specification.pdf has {pages} page(s) in total, drawing sheets included" if pages else
         "- Specification: count the pages of specification.pdf: ____ pages"),
        f"- Drawings: {len(figures)} sheet(s)" if figures else "- Drawings: none listed in the dossier",
    ]
    if pages and figures and pages > len(figures):
        lines.append(f"- On the form: {pages - len(figures)} page(s) of specification and {len(figures)} sheet(s) of drawings. "
                     "Each figure prints on a sheet of its own, after the text.")
    lines += [
        "- Application Data Sheet (37 CFR 1.76): optional", "",
        "## Entity status and fee", "",
        f"Provisional filing fee when this was written: {FEES}. Check the current USPTO fee schedule before paying.",
        "Small and micro entity status have conditions (for micro entity, an income limit and a certification form).",
        "Claim the status you can prove: ________", "",
        "## Payment", "", "Card or deposit account, entered at Patent Center.", "",
        "## US Government interest", "", "Was the invention made under a contract with a US Government agency? Normally: No.", "",
    ]
    if prefilled:
        lines += [
            "## The form itself", "",
            f"{SB16_FILE} in this folder is form PTO/SB/16 from uspto.gov with the inventor, title, page and sheet counts",
            "and the docket number above typed in. It is a draft: check every field against this sheet, then add your",
            "address, entity status, fee and payment, the government interest answer, your signature and the date.",
            "ReachPilot sends it nowhere.", "",
        ]
    return "\n".join(lines)


def checklist(name: str, row: dict | None = None) -> str:
    when = (row or {}).get("window") or {}
    dated = (f"This repository is public. The 12-month date where a grace period applies is {when.get('deadline')}. "
             "Europe and China have no general grace period: ask the attorney what is still open there."
             if when.get("state") == "grace" else
             "This repository is private. Keep it private, and publish nothing about the mechanism, until the application is filed.")
    return "\n".join([
        f"# Filing checklist: {name}", "",
        "ReachPilot prepares and tracks. You file. Not legal advice.", "",
        "1. Read specification.pdf from start to finish. Anything the text does not describe is not protected by it.",
        "2. Read attorney_notes.md: the closest prior art found, and the open questions.",
        "3. Confirm that the invention is yours. Work made for or at an employer usually belongs to the employer.",
        "4. Decide who files. An attorney costs more and is the safer route; a provisional application can also be filed by the inventor.",
        "   Ask the attorney whether your country of residence requires a first filing at home or a permission to file abroad.",
        "5. To file yourself: create a USPTO.gov account, pass its identity verification, and open Patent Center.",
        "6. Upload specification.pdf and the drawings, and fill the cover sheet fields from cover_sheet.md (form PTO/SB/16).",
        f"   When {SB16_FILE} is in this folder, it is that form with those fields typed in: check it, complete it, sign it.",
        f"7. Pay the provisional filing fee: {FEES}. Check the current fee schedule first.",
        "8. Save the filing receipt. On the Patent desk card in ReachPilot, press Record filing and enter the date and the application number.",
        "9. From that date you have 12 months to file the non-provisional or PCT application. That date cannot be extended.",
        "   ReachPilot shows it and raises a to-do at 90 and at 30 days.", "",
        dated, "",
    ])


def attorney_notes(name: str, dossier: dict, item: dict | None = None) -> str:
    prior = [entry for entry in _items(dossier.get("prior_art")) if isinstance(entry, dict) and _text(entry.get("reference"))]
    lines = [f"# Notes for the attorney: {name}", "",
             "Kept out of the specification on purpose: the attorney decides what a specification says about prior art.", ""]
    if item:
        lines += [f"Readiness on the patent desk: {item['score']} of 100 ({item['passed']} of {item['total']} checks).", ""]
        lines += [f"- {'done' if check['ok'] else 'OPEN'}: {check['label']} ({check['found']})" for check in item["checks"]] + [""]
    lines += ["## Closest prior art found", ""]
    for entry in prior:
        lines.append(f"- {_text(entry['reference'])}" + (f" <{_text(entry.get('url'))}>" if _text(entry.get("url")) else ""))
        lines.append(f"  Difference: {_text(entry.get('difference')) or '[not stated]'}")
    if not prior:
        lines.append("None listed yet.")
    questions = [_text(question) for question in _items(dossier.get("questions")) if _text(question)]
    if questions:
        lines += ["", "## Open questions", ""] + [f"- {question}" for question in questions]
    lines += ["", "This list comes from a web search, not from a professional patent search. It can miss patents and applications.", ""]
    return "\n".join(lines)


def complete_pdf(path: Path) -> bool:
    """A PDF that was written to its end: the header, more than 1000 bytes, and the end-of-file mark."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(5)
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - 1024))
            tail = handle.read()
    except OSError:
        return False
    return head.startswith(b"%PDF") and size > 1000 and b"%%EOF" in tail


def _stop_tree(process) -> None:
    """Stop a process and everything it started. A headless Edge leaves its children running when only the
    parent is stopped. This Edge has a profile of its own, so no other Edge window belongs to it."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        process.kill()
        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass


def print_pdf(source: Path, target: Path, timeout: int = 90, edge: Path | None = None,
              launch=subprocess.Popen, stop=_stop_tree) -> bool:
    """HTML to PDF with the Edge that ships with Windows. False when Edge is missing or no complete PDF came out.
    Edge sometimes writes the file and then does not exit: after the timeout that Edge is stopped, and the file
    counts when it is complete. Nothing is read from Edge's output, so a child that holds a pipe cannot block."""
    edge = edge or next((path for path in EDGE if path.exists()), None)
    if edge is None:
        return False
    target.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        try:
            process = launch([str(edge), "--headless=new", "--disable-gpu", "--no-pdf-header-footer", f"--user-data-dir={profile}",
                              f"--print-to-pdf={target}", source.resolve().as_uri()],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError:
            return False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop(process)
        except (OSError, subprocess.SubprocessError):
            return False
    return complete_pdf(target)


def sb16_values(name: str, dossier: dict, pages: int | None = None) -> dict:
    """What the desk can say on form PTO/SB/16, by the form's field names: inventors, title, docket number, and
    the page and sheet counts when they are known. Empty values are left out, so a blank stays blank."""
    values = {SB16_FIELDS["title"]: (_text(dossier.get("title")) or name)[:500], SB16_FIELDS["docket"]: name}
    inventors = [inventor for inventor in _items(dossier.get("inventors")) if isinstance(inventor, dict)]
    for row, inventor in enumerate(inventors[:SB16_ROWS], 1):
        for key in ("given", "family", "residence"):
            if _text(inventor.get(key)):
                values[SB16_FIELDS[key].format(row=row)] = _text(inventor.get(key))
    sheets = len([figure for figure in _items(dossier.get("figures")) if isinstance(figure, dict) and _text(figure.get("file"))])
    if pages and pages > sheets:  # the printed PDF holds the text and then one sheet per figure
        values[SB16_FIELDS["specification"]] = SB16_ON
        values[SB16_FIELDS["pages"]] = str(pages - sheets)
    if sheets:
        values[SB16_FIELDS["drawings"]] = SB16_ON
        values[SB16_FIELDS["sheets"]] = str(sheets)
    return values


def _download(url: str, limit: int = 5_000_000) -> bytes:
    from urllib.request import Request, urlopen

    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 (ReachPilot patent desk)"}), timeout=30) as reply:
        return reply.read(limit)


_sb16_asked = False


def sb16_form(download=None) -> Path | None:
    """The blank form, data/patents/forms/sb0016.pdf. Fetched from uspto.gov when it is not there yet, at most
    once per run. None when it cannot be had; the next run asks again."""
    global _sb16_asked
    path = patents.DATA_DIR / "forms" / "sb0016.pdf"
    if complete_pdf(path):
        return path
    if download is None:
        if _sb16_asked:
            return None
        _sb16_asked = True
    try:
        data = (download or _download)(SB16_URL)
    except Exception:  # no network, a proxy, a moved file: the package is built without the form
        return None
    if not isinstance(data, bytes) or not data.startswith(b"%PDF") or b"%%EOF" not in data[-1024:]:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path if complete_pdf(path) else None


def can_prefill() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def prefill_sb16(form: Path, target: Path, values: dict) -> bool:
    """A copy of the form with the values typed in, read back before it is kept. False, and no file, when pypdf
    is not installed, the form lacks one of the fields, or the copy does not read back as written."""
    target.unlink(missing_ok=True)
    if not can_prefill():
        return False
    from pypdf import PdfReader, PdfWriter

    try:
        writer = PdfWriter(clone_from=str(form))
        for page in writer.pages:
            if "/Annots" in page:
                writer.update_page_form_field_values(page, values, auto_regenerate=False)
        writer.set_need_appearances_writer(True)
        with open(target, "wb") as handle:
            writer.write(handle)
        fields = PdfReader(str(target)).get_fields() or {}
        kept = bool(values) and all(key in fields and str(fields[key].get("/V")) == value for key, value in values.items())
    except Exception:  # a damaged or changed form must not stop the package
        kept = False
    if not kept:
        target.unlink(missing_ok=True)
    return kept


def pdf_pages(path: Path) -> int | None:
    """How many pages a PDF has, read from its page tree. None when the file cannot be read, or when the page
    objects and the count in the page tree disagree (then the number is left for you to fill in)."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return None
    if not data.startswith(b"%PDF"):
        return None
    pages = len(re.findall(rb"/Type\s*/Page(?![A-Za-z])", data))
    stated = {int(count) for count in re.findall(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", data)}
    stated |= {int(count) for count in re.findall(rb"/Count\s+(\d+)[^>]*?/Type\s*/Pages\b", data)}
    return pages if pages and stated == {pages} else None


def build_package(name: str, dossier: dict, item: dict | None = None, row: dict | None = None, pdf: bool = True) -> dict:
    """Write the package into dossiers/NAME/provisional/. Returns what was built."""
    ready = bool(item and item["stage"] == "ready")
    target = folder(name) / "provisional"
    target.mkdir(parents=True, exist_ok=True)
    blocks = specification(name, dossier, ready)
    (target / "specification.md").write_text(to_markdown(blocks), encoding="utf-8")
    (target / "specification.html").write_text(to_html(blocks), encoding="utf-8")
    (target / "filing_checklist.md").write_text(checklist(name, row), encoding="utf-8")
    (target / "attorney_notes.md").write_text(attorney_notes(name, dossier, item), encoding="utf-8")
    made = pdf and print_pdf(target / "specification.html", target / "specification.pdf")
    pages = pdf_pages(target / "specification.pdf") if made else None
    filled = False
    if pdf:  # the form only for a package that is ready: a typed-in cover sheet on a draft invites filing it
        form = sb16_form() if ready and can_prefill() else None
        filled = bool(form) and prefill_sb16(form, target / SB16_FILE, sb16_values(name, dossier, pages))
        if not filled:
            (target / SB16_FILE).unlink(missing_ok=True)
    (target / "cover_sheet.md").write_text(cover_sheet(name, dossier, pages, filled), encoding="utf-8")
    return {"built": date.today().isoformat(), "pdf": bool(made), "draft": not ready, "sb16": filled}


def package_stale(old: dict, fingerprint: str, target: Path, pdf: bool = True, ready: bool = False,
                  edge: bool | None = None, form=sb16_form) -> bool:
    """Whether a package is built again: the dossier, stage or score changed, the specification is gone, or a
    part that could not be made last time (the printed PDF, the typed-in form) can be tried again."""
    if old.get("fingerprint") != fingerprint or not (target / "specification.md").exists():
        return True
    if not pdf:
        return False
    if not old.get("pdf") and (any(path.exists() for path in EDGE) if edge is None else edge):
        return True
    return ready and not old.get("sb16") and can_prefill() and form() is not None


def _fingerprint(dossier: dict, item: dict) -> str:
    payload = json.dumps([dossier, item["stage"], item["score"]], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ------------------------------------------------------------------ the daily run

def summarise_runs(runs, branch: str = "") -> dict:
    """What the completed workflow runs say about the tests. Counts the newest run of each workflow that a push
    or a schedule started on the given branch, and leaves out GitHub's own workflows (path dynamic/...) and
    workflows that only ship something (Pages, deploy, publish, release). One failed workflow makes it a failure;
    nothing left to count is "none"."""
    newest = {}
    ordered = sorted((run for run in runs if isinstance(run, dict)) if isinstance(runs, list) else [],
                     key=lambda run: str(run.get("created_at") or ""), reverse=True)
    for run in ordered:
        path = str(run.get("path") or "")
        name = _text(run.get("name")) or Path(path).stem
        label = f"{Path(path).stem} {name}"
        if path.startswith("dynamic/") or (SHIPS.search(label) and not TESTS.search(label)):
            continue
        if run.get("event") not in TEST_EVENTS or (branch and run.get("head_branch") != branch):
            continue
        if run.get("conclusion") in (None, "", "skipped", "cancelled", "neutral"):
            continue
        newest.setdefault(run.get("workflow_id") or path or name, {
            "name": name, "ok": run.get("conclusion") == "success", "url": str(run.get("html_url") or "")})
    if not newest:
        return {"state": "none"}
    counted = list(newest.values())
    failed = [run for run in counted if not run["ok"]]
    return {"state": "failure" if failed else "success", "url": (failed or counted)[0]["url"],
            "workflows": sorted(run["name"] for run in counted)[:6]}


def ci_state(full_name: str) -> dict | None:
    """The state of the repository's test runs on its default branch. None when GitHub did not answer."""
    from urllib.parse import quote

    import github_source as gh

    repository = gh.gh_api(f"repos/{full_name}")
    branch = repository.get("default_branch") if isinstance(repository, dict) else None
    if not isinstance(branch, str) or not branch:
        return None
    reply = gh.gh_api(f"repos/{full_name}/actions/runs?branch={quote(branch, safe='')}&status=completed"
                      "&per_page=100&exclude_pull_requests=true")
    if not isinstance(reply, dict) or not isinstance(reply.get("workflow_runs"), list):
        return None
    return {**summarise_runs(reply["workflow_runs"], branch), "branch": branch, "checked": date.today().isoformat()}


def load_inputs() -> dict:
    """Dossiers, stored facts and history from disk, shaped for build()."""
    stored = patents._load(desk_file(), {})
    stored = stored if isinstance(stored, dict) else {}
    dossiers = {}
    root = dossiers_dir()
    if root.is_dir():
        for path in root.iterdir():
            if path.is_dir() and NAME.fullmatch(path.name):
                dossier = load_dossier(path.name)
                if dossier is not None:
                    dossiers[path.name] = dossier
    return {
        "dossiers": dossiers,
        "facts": stored.get("facts") if isinstance(stored.get("facts"), dict) else {},
        "history": stored.get("history") if isinstance(stored.get("history"), list) else [],
        "previous": stored.get("projects") if isinstance(stored.get("projects"), list) else [],
        "refreshed_at": str(stored.get("refreshed_at") or ""),
    }


def figure_on_disk(name: str, file: str) -> bool:
    try:
        base = folder(name).resolve()
        path = (base / file).resolve()
    except (OSError, ValueError):
        return False
    return base in path.parents and path.is_file()


def refresh(today: date | None = None, fetch_ci=ci_state, pdf: bool = True) -> dict:
    """One daily run: skeletons, CI state, readiness, what changed, packages. Writes desk.json."""
    today = today or date.today()
    scan = patents._load(patents.SCAN_FILE, None)
    if not isinstance(scan, dict) or not isinstance(scan.get("repos"), list):
        return {"ok": False, "message": "Patent desk: no scan yet, nothing to follow."}
    overrides = patents.load_overrides()
    rows = patents.view(scan, overrides, today)
    full_names = {entry.get("name"): entry.get("full_name") for entry in scan["repos"] if isinstance(entry, dict)}
    desk_rows = on_desk(rows)
    created = sum(1 for row in desk_rows if ensure_skeleton(row))
    inputs = load_inputs()
    facts = {name: fact for name, fact in inputs["facts"].items() if isinstance(fact, dict)}
    for row in desk_rows:
        fact = facts.setdefault(row["name"], {})
        state = fetch_ci(full_names.get(row["name"]) or row["name"]) if fetch_ci else None
        if state:
            fact["ci"] = state
    desk = build(rows, overrides, inputs["dossiers"], facts, today, None, figure_on_disk)
    by_name = {row["name"]: row for row in desk_rows}
    built = 0
    for item in desk["projects"]:
        dossier = inputs["dossiers"].get(item["name"])
        fact = facts[item["name"]]
        if item["stage"] not in ("strengthen", "ready") or is_empty(dossier):
            continue
        fingerprint = _fingerprint(dossier, item)
        old = fact.get("package") if isinstance(fact.get("package"), dict) else {}
        if not package_stale(old, fingerprint, folder(item["name"]) / "provisional", pdf, item["stage"] == "ready"):
            continue
        fact["package"] = {**build_package(item["name"], dossier, item, by_name.get(item["name"]), pdf), "fingerprint": fingerprint}
        built += 1
    desk = build(rows, overrides, inputs["dossiers"], facts, today, None, figure_on_disk)
    moved = changes(inputs["previous"], desk["projects"], today) if inputs["refreshed_at"] else []
    history = (inputs["history"] + moved)[-HISTORY_LIMIT:]
    patents.DATA_DIR.mkdir(parents=True, exist_ok=True)
    patents.save_json(desk_file(), {
        "version": 1, "refreshed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "projects": [{key: item[key] for key in ("name", "stage", "score")} for item in desk["projects"]],
        "facts": {name: fact for name, fact in facts.items() if name in by_name},
        "history": history,
    })
    counts = desk["counts"]
    return {"ok": True, "message": (
        f"Patent desk: {desk['total']} project(s): {counts['ready']} ready to file, {counts['filed']} filed, "
        f"{counts['strengthen']} strengthening, {counts['disclosure']} need a disclosure, {counts['ownership']} waiting on "
        f"ownership; {created} new dossier(s), {built} package(s) rebuilt, {len(moved)} change(s).")}


# ------------------------------------------------------------------ my own notes (ownership, filing)

def _note(name: str) -> tuple[dict, dict]:
    if not NAME.fullmatch(name):
        raise ValueError(f"Not a project name: {name!r}")
    names = {entry.get("name") for entry in (patents._load(patents.SCAN_FILE, {}).get("repos") or []) if isinstance(entry, dict)}
    if name not in names:
        raise ValueError(f"No project called '{name}' in the last scan.")
    overrides = patents.load_overrides()
    note = overrides.get(name) if isinstance(overrides.get(name), dict) else {}
    overrides[name] = note
    return overrides, note


def _save(overrides: dict) -> None:
    patents.DATA_DIR.mkdir(parents=True, exist_ok=True)
    patents.save_json(patents.OVERRIDES_FILE, overrides)


def set_ownership(name: str, state: str, today: date | None = None) -> str:
    if state not in ("cleared", "unsure"):
        raise ValueError("Ownership is either cleared or unsure.")
    overrides, note = _note(name)
    if state == "cleared":
        note["ownership_cleared"] = (today or date.today()).isoformat()
    else:
        note.pop("ownership_cleared", None)
    _save(overrides)
    return (f"{name}: marked as yours. The desk drafts for it from the next run." if state == "cleared"
            else f"{name}: ownership open again. Nothing is drafted until it is cleared.")


def record_filing(name: str, day: str, number: str, today: date | None = None) -> str:
    filed_on = _day(day) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or "") else None
    if filed_on is None:
        raise ValueError("The filing date must look like 2026-12-01.")
    if filed_on > (today or date.today()):
        raise ValueError("The filing date is in the future. Record a filing after it happened.")
    number = " ".join((number or "").split())
    if not NUMBER.fullmatch(number) or not any(character.isdigit() for character in number):
        raise ValueError("The application number looks like 63/123,456.")
    overrides, note = _note(name)
    note["filed"] = {"date": filed_on.isoformat(), "number": number}
    note["decision"] = "file"  # a filing is that decision: the Patent screen stops asking for one
    _save(overrides)
    due = patents.add_months(filed_on, FOLLOW_UP_MONTHS)
    return f"{name}: filing recorded. The non-provisional or PCT application is due by {due.isoformat()}; that date cannot be extended."


def clear_filing(name: str) -> str:
    overrides, note = _note(name)
    note.pop("filed", None)
    _save(overrides)
    return f"{name}: recorded filing removed."


def open_folder(name: str) -> str:
    _note(name)
    target = folder(name)
    if not target.is_dir():
        raise ValueError(f"{name} has no dossier folder yet. It is created by the next daily run.")
    os.startfile(target)  # noqa: S606 - a folder inside data/patents, name checked above
    return f"Opened the dossier folder of {name}."


def show() -> None:
    inputs = load_inputs()
    rows = patents.view(patents._load(patents.SCAN_FILE, {}), patents.load_overrides())
    desk = build(rows, patents.load_overrides(), inputs["dossiers"], inputs["facts"], None, inputs["history"], figure_on_disk)
    for item in desk["projects"]:
        print(f"{item['score']:>3}  {item['stage_label']:<30} {item['name']:<28} {item['next_step']}")


if __name__ == "__main__":
    arguments = sys.argv[1:]
    try:
        if arguments == ["show"]:
            show()
        elif len(arguments) == 3 and arguments[0] == "ownership":
            print(set_ownership(arguments[1], arguments[2]))
        elif len(arguments) == 4 and arguments[0] == "filed":
            print(record_filing(arguments[1], arguments[2], arguments[3]))
        elif len(arguments) == 2 and arguments[0] == "unfiled":
            print(clear_filing(arguments[1]))
        elif not arguments:
            try:
                outcome = refresh()["message"]
            except Exception as error:  # a scheduled run must leave a line in the log, not a silent crash
                outcome = f"Patent desk failed: {type(error).__name__}: {error}"
            patents.log(outcome)
            if sys.stdout:
                print(outcome)
        else:
            print(__doc__)
    except ValueError as error:
        print(error)
