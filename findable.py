"""Search: can a search engine find each of my projects?

A page nobody links to and no sitemap names is found late or never: on 21 Nov 2026 Google knew 2 pages of a
site that had more than a hundred. Once a day this PC

  1. lists every page a project has: the site its GitHub repo names, its landing page (p/<name>/) and its
     watch page on the Pages site, plus the addresses in config.json ("findable": {"extra_pages": [...]}).
     A project named under "not_for_search" there may say noindex without that counting as a fault,
  2. reads each page the way a search engine does and writes down what would keep it out: it does not
     answer, it says noindex, it has no title or no description,
  3. keeps one sitemap.xml for the whole Pages site (<user>.github.io) with every page that may be found,
     named in robots.txt, and pushes it when it changed,
  4. tells the IndexNow search engines (Bing, Yandex and the ones they pass it on to) which pages are new or
     changed. No account is needed: the site shows its own key file,
  5. asks Google, where a Search Console bridge is connected (secrets.json, "search_console"), whether each
     page is in its index, and hands Google the sitemap. Without the bridge the page says "not connected" and
     To do says how to connect it. Google has no door without a sign-in.

Nothing here posts anywhere or changes a project's own repo. Whether and when a search engine shows a page
stays that search engine's decision; this only removes the reasons it could not.

    python findable.py            run once (the scheduled task does this every day)
    python findable.py install    create the daily task "\\ReachPilot\\Search"
    python findable.py show       print what the dashboard shows
    python findable.py check NAME exit 1 when a page of that project cannot be found (a gate for other tools)
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import sys
import urllib.parse
import uuid
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import machine

CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = CODE_DIR / "data" / "findable"
STATUS_FILE = DATA_DIR / "status.json"  # what the dashboard shows
STATE_FILE = DATA_DIR / "state.json"  # what was told to whom, and Google's last answers
CONFIG_FILE = CODE_DIR / "config.json"
SECRETS_FILE = CODE_DIR / "secrets.json"
PAGES_CHECKOUT = CODE_DIR / ".pages"  # the same local copy of the Pages site that autopilot.py uses
LOG_FILE = CODE_DIR / "findable.log"
TASK_PATH = "\\ReachPilot\\"
TASK_NAME = "Search"
TASK_MINUTES = 90  # the first pass on an office network took 20 minutes for 164 pages (22 Nov 2026); 30 was too close

AGENT = "ReachPilot (https://github.com/juw12/reachpilot)"
PAGE_SECONDS = 20
MIN_TITLE = 10  # shorter than this says nothing in a search result
MIN_DESCRIPTION = 25  # a short true sentence is fine; a word or two is not
THIN_WORDS = 20  # a page with fewer words and no description gives a search engine nothing to match
INDEXNOW_DOORS = ("api.indexnow.org", "www.bing.com", "yandex.com")  # an office network closes some of them
INDEXNOW_BATCH = 500
GOOGLE_PER_RUN = 40  # pages asked about per run; Search Console allows 2,000 a day for a property
GOOGLE_AGAIN_DAYS = 3  # an answer is asked again after this long
KEY_FILE = re.compile(r"^[0-9a-f]{32}\.txt$")
ACTION = re.compile(r"^findable:(run|tell)$")
KINDS = {"site": "site", "landing": "landing page", "watch": "watch page", "extra": "page"}


def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-199:] if LOG_FILE.exists() else []
        LOG_FILE.write_text("\n".join(lines + [line]) + "\n", encoding="utf-8")
    except OSError:
        pass


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_suffix(".tmp")
    scratch.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    scratch.replace(path)


def load_config() -> dict:
    config = _read_json(CONFIG_FILE) or {}
    own = config.get("findable") if isinstance(config.get("findable"), dict) else {}
    return {
        "user": str(config.get("github_user") or "").strip(),
        "extra_pages": [page for page in own.get("extra_pages") or [] if isinstance(page, str) and page.startswith("http")],
        "skip": {str(name).lower() for name in own.get("skip") or []},
        # projects whose page says noindex because I want it so (a private tool, a page that moved)
        "not_for_search": {str(name).lower() for name in own.get("not_for_search") or []},
    }


def bridge() -> dict | None:
    """Where the Search Console bridge is, or None. It is the only way to ask Google without a browser."""
    secrets = _read_json(SECRETS_FILE) or {}
    own = secrets.get("search_console") if isinstance(secrets.get("search_console"), dict) else {}
    url, token = str(own.get("bridge_url") or "").strip(), str(own.get("token") or "").strip()
    return {"url": url, "token": token} if url.startswith("https://") and token else None


# ------------------------------------------------------------------ reading a page the way a search engine does

def _fetch(url: str) -> dict:
    """{"status", "url" (after redirects), "text", "headers"}. status 0 = no answer."""
    import requests

    try:
        answer = requests.get(url, timeout=PAGE_SECONDS, headers={"User-Agent": AGENT})
    except requests.RequestException as error:
        return {"status": 0, "url": url, "text": "", "headers": {}, "error": type(error).__name__}
    return {"status": answer.status_code, "url": answer.url, "text": answer.text[:600_000], "headers": dict(answer.headers)}


def _meta(head: str, name: str) -> str:
    for tag in re.findall(r"<meta\b[^>]*>", head, re.I):
        if re.search(rf'\b(?:name|property)\s*=\s*["\']{re.escape(name)}["\']', tag, re.I):
            found = re.search(r'\bcontent\s*=\s*"([^"]*)"|\bcontent\s*=\s*\'([^\']*)\'', tag, re.I)
            if found:
                return html.unescape(found[1] if found[1] is not None else found[2]).strip()
    return ""


def _words(text: str) -> int:
    body = re.sub(r"<(script|style|noscript|template)\b.*?</\1>", " ", text, flags=re.S | re.I)
    return len(re.findall(r"\w{2,}", html.unescape(re.sub(r"<[^>]+>", " ", body))))


def read_page(url: str, fetch=_fetch) -> dict:
    """What a search engine would learn from this address: {"url", "status", "title", "description",
    "canonical", "noindex", "moves", "words", "changed", "print", "issues": [...], "listable"}."""
    answer = fetch(url)
    text, headers = answer.get("text") or "", {key.lower(): value for key, value in (answer.get("headers") or {}).items()}
    end = text.lower().find("</head>")
    head = text[: end if end >= 0 else 20_000]
    title = re.search(r"<title[^>]*>(.*?)</title>", head, re.S | re.I)
    link = next((tag for tag in re.findall(r"<link\b[^>]*>", head, re.I) if re.search(r'\brel\s*=\s*["\']canonical["\']', tag, re.I)), "")
    named = re.search(r'\bhref\s*=\s*["\']([^"\']+)["\']', link, re.I)
    page = {
        "url": url,
        "status": answer.get("status", 0),
        "title": re.sub(r"\s+", " ", html.unescape(title[1])).strip() if title else "",
        "description": _meta(head, "description"),
        "canonical": urllib.parse.urljoin(url, html.unescape(named[1])) if named else "",
        "noindex": "noindex" in (_meta(head, "robots") + " " + headers.get("x-robots-tag", "")).lower(),
        "moves": bool(re.search(r'http-equiv\s*=\s*["\']refresh["\']', head, re.I)),
        "words": _words(text),
        "changed": _day(headers.get("last-modified", "")),
        "print": hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16] if text else "",
    }
    page["issues"] = _issues(page, answer)
    # A page goes in the sitemap when it answers, may be indexed, stays where it is and is its own canonical page.
    page["listable"] = (
        page["status"] == 200 and not page["noindex"] and not page["moves"]
        and (not page["canonical"] or _same(page["canonical"], url))
    )
    return page


def _day(http_date: str) -> str:
    try:
        return parsedate_to_datetime(http_date).strftime("%Y-%m-%d")
    except (TypeError, ValueError, IndexError):
        return ""


def _same(first: str, second: str) -> bool:
    def plain(address: str) -> str:
        parts = urllib.parse.urlsplit(address)
        path = re.sub(r"/index\.html?$", "/", parts.path) or "/"
        return f"{parts.netloc.lower()}{path}"

    return plain(first) == plain(second)


def _issues(page: dict, answer: dict) -> list[str]:
    """What would keep this page out of a search engine, worst first. Words a person can act on."""
    if page["status"] != 200:
        return [f"does not answer ({page['status'] or answer.get('error') or 'no connection'})"]
    found = []
    if page["noindex"] and not (page["canonical"] and not _same(page["canonical"], page["url"])):
        # a page that names another page as the real one is kept out on purpose; that is not a fault
        found.append("says noindex, so search engines must leave it out")
    if page["moves"]:
        return found  # a page that sends the reader on is judged by where it sends them
    if not page["title"]:
        found.append("has no title")
    elif len(page["title"]) < MIN_TITLE:
        found.append(f"its title is only {len(page['title'])} characters (\"{page['title']}\")")
    if not page["description"]:
        found.append("has no description, so a search result shows whatever text comes first")
    elif len(page["description"]) < MIN_DESCRIPTION:
        found.append(f"its description is only {len(page['description'])} characters")
    if page["words"] < THIN_WORDS and not page["description"]:
        found.append(f"shows {page['words']} words before any script runs, and no description")
    return found


# ------------------------------------------------------------------ which pages there are

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def project_pages(user: str, repos: list[dict], checkout: Path | None, extra_pages: list[str], skip: set[str]) -> dict[str, list[dict]]:
    """{project: [{"url", "kind"}]}: every address that is some project's face to the world."""
    host = f"https://{user.lower()}.github.io/"
    pages: dict[str, list[dict]] = {}

    def add(project: str, url: str, kind: str) -> None:
        if project.lower() in skip:
            return
        rows = pages.setdefault(project, [])
        if not any(_same(row["url"], url) for row in rows):
            rows.append({"url": url, "kind": kind})

    for repo in repos:
        site = str(repo.get("homepage") or "").strip()
        if site.startswith("http"):
            landing = re.match(rf"{re.escape(host)}p/([^/?#]+)/?$", site, re.I)
            add(repo["name"], site if site.endswith("/") or "." in site.rsplit("/", 1)[-1] or "?" in site else site + "/", "landing" if landing else "site")
    if checkout and checkout.is_dir():
        landings = _read_json(checkout / "p" / "landing.json") or {}
        names = {}
        for source, entry in landings.items() if isinstance(landings, dict) else []:
            address = str((entry or {}).get("landing") or "")
            found = re.match(rf"{re.escape(host)}p/([^/?#]+)/?$", address, re.I)
            if found:
                # a folder of the monorepo is its own project; a repo's landing page belongs to that repo
                repo_name = source.rstrip("/").rsplit("/", 1)[-1]
                names[found[1].lower()] = repo_name if any(repo["name"] == repo_name for repo in repos) else found[1]
        for folder, kind in (("p", "landing"), ("watch", "watch")):
            for page in sorted((checkout / folder).glob("*/index.html")) if (checkout / folder).is_dir() else []:
                slug = page.parent.name
                add(names.get(slug.lower(), slug), f"{host}{folder}/{slug}/", kind)
    for address in extra_pages:
        parts = urllib.parse.urlsplit(address)
        add(_slug(parts.path.strip("/").split("/")[0] if parts.netloc.lower().endswith("github.io") and parts.path.strip("/") else parts.netloc), address, "extra")
    return dict(sorted(pages.items(), key=lambda item: item[0].lower()))


# ------------------------------------------------------------------ the sitemap of the Pages site

def sitemap_text(rows: list[tuple[str, str]]) -> str:
    lines = "".join(
        f"  <url><loc>{html.escape(url)}</loc>{f'<lastmod>{day}</lastmod>' if day else ''}</url>\n" for url, day in sorted(set(rows))
    )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{lines}</urlset>\n'


def robots_text(current: str, sitemap_url: str) -> str:
    """robots.txt naming the sitemap, with everything else it said left as it was."""
    if re.search(rf"^\s*Sitemap:\s*{re.escape(sitemap_url)}\s*$", current, re.I | re.M):
        return current
    if not current.strip():
        return f"User-agent: *\nAllow: /\n\nSitemap: {sitemap_url}\n"
    return current.rstrip("\n") + f"\nSitemap: {sitemap_url}\n"


def host_key(checkout: Path) -> str:
    """The IndexNow key the site shows in a file at its top. An existing one is kept; else a new one is written."""
    for file in sorted(checkout.glob("*.txt")):
        if KEY_FILE.match(file.name) and file.read_text(encoding="utf-8", errors="replace").strip() == file.stem:
            return file.stem
    key = uuid.uuid4().hex
    (checkout / f"{key}.txt").write_text(key + "\n", encoding="utf-8", newline="\n")
    return key


def write_site_files(checkout: Path, user: str, rows: list[tuple[str, str]]) -> list[str]:
    """Write sitemap.xml and robots.txt into the local copy of the Pages site. Returns the files that changed."""
    host = f"https://{user.lower()}.github.io/"
    wanted = {"sitemap.xml": sitemap_text(rows)}
    robots = checkout / "robots.txt"
    wanted["robots.txt"] = robots_text(robots.read_text(encoding="utf-8") if robots.exists() else "", host + "sitemap.xml")
    changed = []
    for name, text in wanted.items():
        target = checkout / name
        if not target.exists() or target.read_text(encoding="utf-8") != text:
            target.write_text(text, encoding="utf-8", newline="\n")
            changed.append(name)
    return changed


def push_site(checkout: Path, files: list[str], message: str) -> str:
    """Commit and push the named files of the Pages site. "" when it went out, else the reason in a few words."""
    import trylinks

    for name in files:
        trylinks._git(["add", "--", name], checkout)
    if trylinks._git(["diff", "--cached", "--quiet"], checkout).returncode == 0:
        return ""
    committed = trylinks._git(["commit", "--quiet", "-m", message], checkout)
    if committed.returncode != 0:
        return "could not commit: " + (committed.stderr or committed.stdout)[-160:].strip()
    pushed = trylinks._git(["push", "--quiet"], checkout)
    if pushed.returncode != 0:
        trylinks._git(["pull", "--rebase", "--quiet"], checkout)
        pushed = trylinks._git(["push", "--quiet"], checkout)
    return "" if pushed.returncode == 0 else "could not push: " + (pushed.stderr or pushed.stdout)[-160:].strip()


# ------------------------------------------------------------------ telling the search engines

def _post_json(url: str, body: dict) -> int:
    import requests

    try:
        return requests.post(url, json=body, timeout=30, headers={"User-Agent": AGENT}).status_code
    except requests.RequestException:
        return 0


def tell_indexnow(host: str, key: str, urls: list[str], post=_post_json) -> dict:
    """{"told": n, "door": name or "", "closed": [doors that did not answer]}. Tries each door until one takes the list."""
    outcome = {"told": 0, "door": "", "closed": []}
    if not urls:
        return outcome
    for door in INDEXNOW_DOORS:
        taken = 0
        for start in range(0, len(urls), INDEXNOW_BATCH):
            batch = urls[start:start + INDEXNOW_BATCH]
            status = post(f"https://{door}/indexnow", {"host": host, "key": key, "keyLocation": f"https://{host}/{key}.txt", "urlList": batch})
            if not 200 <= status < 300:
                break
            taken += len(batch)
        if taken == len(urls):
            return {**outcome, "told": taken, "door": door}
        outcome["closed"].append(door)
    return outcome


def _ask_bridge(link: dict, payload: dict) -> dict:
    import requests

    try:
        answer = requests.post(link["url"], json={**payload, "token": link["token"]}, timeout=60, headers={"User-Agent": AGENT})
        data = answer.json()
    except (requests.RequestException, ValueError) as error:
        return {"error": type(error).__name__}
    return data if isinstance(data, dict) else {"error": "unreadable answer"}


def ask_google(link: dict, property_url: str, urls: list[str], state: dict, today: str, ask=_ask_bridge) -> str:
    """Ask Google about the pages whose answer is oldest, at most GOOGLE_PER_RUN of them. Answers go into
    state["google"][url] = {"verdict", "coverage", "crawled", "asked"}. Returns "" or what went wrong."""
    known = state.setdefault("google", {})

    def age(url: str) -> str:
        return (known.get(url) or {}).get("asked", "")

    def stale(url: str) -> bool:
        if not age(url):
            return True
        return (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(age(url), "%Y-%m-%d")).days >= GOOGLE_AGAIN_DAYS

    for url in sorted((url for url in urls if stale(url)), key=age)[:GOOGLE_PER_RUN]:
        answer = ask(link, {"action": "inspect", "property": property_url, "url": url})
        if answer.get("error"):
            return f"Google was not asked about every page: {str(answer['error'])[:120]}"
        known[url] = {
            "verdict": str(answer.get("verdict") or ""),
            "coverage": str(answer.get("coverage") or "")[:120],
            "crawled": str(answer.get("crawled") or "")[:10],
            "asked": today,
        }
    return ""


def google_word(answer: dict | None) -> str:
    """One word for a page's place in Google: "in Google", "not in Google yet", or "" when nobody asked."""
    if not answer or not answer.get("asked"):
        return ""
    return "in Google" if answer.get("verdict") == "PASS" else "not in Google yet"


# ------------------------------------------------------------------ one run

def _repos(user: str) -> list[dict]:
    import github_source

    return github_source.list_public_repos(user, [])


def run(now: datetime | None = None, fetch=_fetch, repos=None, checkout: Path | None = None, push=push_site,
        post=_post_json, ask=_ask_bridge, link: dict | None = None, prepare=None) -> str:
    """One pass: read every page, keep the sitemap, tell the search engines, write the status file."""
    from launch_auto import _only_one

    with _only_one("run", folder=DATA_DIR) as mine:
        if not mine:
            return "another run is still working; nothing done"
        now = now or datetime.now()
        today = f"{now:%Y-%m-%d}"
        config = load_config()
        user = config["user"]
        if not user:
            return "config.json names no github_user; nothing done"
        checkout = checkout or PAGES_CHECKOUT
        if prepare is None:
            import logging
            import trylinks

            have_site = trylinks._pages_checkout(user, checkout, logging.getLogger("findable"))
        else:
            have_site = prepare(checkout)
        host = f"{user.lower()}.github.io"
        state = _read_json(STATE_FILE) or {}
        told = state.setdefault("told", {})  # url -> {"print", "day"}
        listed = project_pages(user, repos if repos is not None else _repos(user), checkout if have_site else None,
                               config["extra_pages"], config["skip"])

        projects, rows, to_tell = [], [], []
        for name, pages in listed.items():
            read = []
            for page in pages:
                facts = {**read_page(page["url"], fetch), "kind": page["kind"]}
                if facts["noindex"] and name.lower() in config["not_for_search"]:
                    facts["issues"] = []  # kept out of search on purpose (config.json, "not_for_search")
                on_site = urllib.parse.urlsplit(facts["url"]).netloc.lower() == host
                facts["on_site"] = on_site
                if on_site and facts["listable"]:
                    rows.append((facts["url"], facts["changed"]))
                    if (told.get(facts["url"]) or {}).get("print") != facts["print"]:
                        to_tell.append(facts)
                read.append(facts)
            projects.append({"name": name, "pages": read})

        notes = []
        sitemap_url = f"https://{host}/sitemap.xml"
        if have_site and rows:
            key = host_key(checkout)
            changed = write_site_files(checkout, user, rows)
            files = changed + [f"{key}.txt"]
            problem = push(checkout, files, f"sitemap.xml: {len(set(rows))} pages search engines may find (ReachPilot)") if files else ""
            if problem:
                notes.append("sitemap " + problem)
            elif changed:
                notes.append(f"sitemap written ({len(set(rows))} pages)")
                state["sitemap_changed"] = today
            if not problem and to_tell:
                outcome = tell_indexnow(host, key, [page["url"] for page in to_tell], post)
                if outcome["told"]:
                    for page in to_tell:
                        told[page["url"]] = {"print": page["print"], "day": today}
                    state["door"] = outcome["door"]
                    notes.append(f"told {outcome['told']} new or changed page(s) through {outcome['door']}")
                else:
                    notes.append("the search engines could not be told from this PC (" + ", ".join(outcome["closed"]) + " closed)")
        elif not have_site:
            notes.append("no local copy of the Pages site, so no sitemap was written")

        link = link if link is not None else bridge()
        if link:
            trouble = ask_google(link, f"https://{host}/", [url for url, _ in sorted(set(rows))], state, today, ask)
            if state.get("sitemap_handed") != state.get("sitemap_changed", today):
                handed = ask(link, {"action": "sitemap", "property": f"https://{host}/", "url": sitemap_url})
                if not handed.get("error"):
                    state["sitemap_handed"] = state.get("sitemap_changed", today)
            if trouble:
                notes.append(trouble)

        google = state.get("google") or {}
        for project in projects:
            for page in project["pages"]:
                page["in_sitemap"] = bool(page["on_site"] and page["listable"] and have_site)
                page["told"] = (told.get(page["url"]) or {}).get("day", "")
                page["google"] = google_word(google.get(page["url"]))
                page["crawled"] = (google.get(page["url"]) or {}).get("crawled", "")
        every = [page for project in projects for page in project["pages"]]
        status = {
            "ran": f"{now:%Y-%m-%d %H:%M}",
            "host": host,
            "sitemap": sitemap_url if have_site and rows else "",
            "google_connected": bool(link),
            "door": state.get("door", ""),
            "counts": {
                "projects": len(projects), "pages": len(every),
                "trouble": sum(1 for page in every if page["issues"]),
                "in_sitemap": sum(1 for page in every if page["in_sitemap"]),
                "told": sum(1 for page in every if page["told"]),
                "in_google": sum(1 for page in every if page["google"] == "in Google"),
                "asked_google": sum(1 for page in every if page["google"]),
            },
            "notes": notes,
            "projects": projects,
        }
        _write_json(STATE_FILE, state)
        _write_json(STATUS_FILE, status)
        counts = status["counts"]
        return (f"{counts['projects']} projects, {counts['pages']} pages, {counts['trouble']} with something in the way; "
                + ("; ".join(notes) or "nothing new to tell"))


# ------------------------------------------------------------------ what the dashboard shows

def card() -> dict | None:
    """The last run's status file, trimmed for the page. None where the task never ran on this PC."""
    status = _read_json(STATUS_FILE)
    if not isinstance(status, dict) or not isinstance(status.get("projects"), list):
        return None
    rows = []
    for project in status["projects"]:
        pages = [
            {key: page.get(key) for key in ("url", "kind", "title", "issues", "in_sitemap", "told", "google", "crawled", "status", "on_site")}
            for page in project.get("pages") or []
        ]
        rows.append({"name": project.get("name", ""), "pages": pages, "trouble": sum(1 for page in pages if page["issues"])})
    rows.sort(key=lambda row: (-row["trouble"], row["name"].lower()))
    return {
        "ran": status.get("ran", ""), "host": status.get("host", ""), "sitemap": status.get("sitemap", ""),
        "google_connected": bool(status.get("google_connected")), "door": status.get("door", ""),
        "counts": status.get("counts") or {}, "notes": status.get("notes") or [], "projects": rows,
    }


def todo(section: dict | None) -> list[dict]:
    """What only I can do: pages a search engine cannot use, and the one-time Google connection."""
    if not section or not section.get("installed"):
        return []
    items = []
    broken = [(project["name"], page) for project in section.get("projects") or [] for page in project["pages"] if page.get("issues")]
    if broken:
        items.append({
            "priority": 3, "module": "Search",
            "title": f"{len(broken)} page(s) have something that keeps search engines out",
            "detail": "Each line says what. The fix is in that project's own page; the next daily check clears the line.",
            "action": None, "action_label": None,
            "links": [{"label": f"{name} ({KINDS.get(page.get('kind'), 'page')})", "url": page["url"], "note": "; ".join(page["issues"])[:200]}
                      for name, page in broken[:12]],
        })
    if not section.get("google_connected"):
        items.append({
            "priority": 4, "module": "Search",
            "title": "Connect Google Search Console, once",
            "detail": "Without it ReachPilot cannot ask Google which pages it knows; Bing and Yandex are told either way. "
                      "The steps are in the README under Search: one small Apps Script with your own Google sign-in, its address in secrets.json.",
            "action": None, "action_label": None,
            "links": [{"label": "Search Console", "url": "https://search.google.com/search-console", "note": ""}],
        })
    return items


def action(name: str) -> str:
    if not ACTION.match(name):
        raise ValueError(f"Unknown action: {name}")
    if machine.IS_MAC:
        machine.start_task(TASK_NAME)
    else:
        subprocess.run(["schtasks", "/run", "/tn", TASK_PATH + TASK_NAME], capture_output=True, creationflags=machine.NO_WINDOW)
    return "Search check started; the page updates in a few minutes."


def install_task() -> None:
    """Once a day, hidden, catching up if the PC was off."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not machine.IS_WINDOWS:  # a Mac: the same schedule as a launchd agent
        print(machine.install_task(TASK_NAME, [sys.executable, str(Path(__file__).resolve())], CODE_DIR, daily_at="06:40", limit_minutes=TASK_MINUTES))
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    script = f"""
$action = New-ScheduledTaskAction -Execute '{runner}' -Argument '"{Path(__file__).resolve()}"' -WorkingDirectory '{CODE_DIR}'
$daily = New-ScheduledTaskTrigger -Daily -At 06:40
$settings = New-ScheduledTaskSettingsSet -Priority 4 -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes {TASK_MINUTES}) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' -Action $action -Trigger $daily -Settings $settings `
    -Description 'Search: check that search engines can find every project page, keep the sitemap, tell Bing and Yandex.' -Force | Out-Null
Get-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' | Select-Object TaskName, State
"""
    print(subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True).stdout.strip())


def show() -> None:
    status = card()
    if not status:
        print("The search check has not run on this PC yet: python findable.py")
        return
    counts = status["counts"]
    print(f"Checked {status['ran']}: {counts.get('projects', 0)} projects, {counts.get('pages', 0)} pages, "
          f"{counts.get('in_sitemap', 0)} in the sitemap, {counts.get('told', 0)} told to Bing and Yandex, "
          + (f"{counts.get('in_google', 0)} of {counts.get('asked_google', 0)} asked are in Google" if status["google_connected"] else "Google not connected"))
    for project in status["projects"]:
        for page in project["pages"]:
            if page["issues"]:
                print(f"  {project['name']}: {page['url']} {'; '.join(page['issues'])}")


def check(name: str) -> int:
    """0 when every page of this project can be found, 1 when one cannot, 2 when the project is not known."""
    status = card()
    project = next((row for row in (status or {}).get("projects") or [] if row["name"].lower() == name.lower()), None)
    if not project:
        print(f"{name}: no page known. Run python findable.py first, or the project has no site.")
        return 2
    bad = 0
    for page in project["pages"]:
        gaps = list(page["issues"]) + ([] if page["in_sitemap"] or not page["on_site"] or page["issues"] else ["is in no sitemap"])
        print(f"{'GAP ' if gaps else 'ok  '}{page['url']}{': ' + '; '.join(gaps) if gaps else ''}")
        bad += bool(gaps)
    return 1 if bad else 0


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments == ["install"]:
        install_task()
    elif arguments == ["show"]:
        show()
    elif len(arguments) == 2 and arguments[0] == "check":
        sys.exit(check(arguments[1]))
    elif not arguments:
        from launch_auto import leave_low_priority

        leave_low_priority()
        try:
            outcome = run()
        except Exception as error:  # the task must leave a trace, never a silent crash
            outcome = f"FAILED {type(error).__name__}: {error}"
        log(outcome)
        print(outcome)
    else:
        print(__doc__)
