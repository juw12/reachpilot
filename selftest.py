"""Offline-safe checks: README extraction, composer guards, little-text, analytics parsing, scoring."""
import json
import os
import random
import sys
import sqlite3
import tempfile
import shutil
import base64
import hashlib
import threading
import urllib.parse
import urllib.request
from pathlib import Path

from openpyxl import Workbook

import analytics
import composer
import github_source as gh
from linkedin_api import to_little_text
import trylinks

config = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
failures = []

# A piped Windows console is cp1252: a README bullet outside it must not stop the run.
if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def check(condition, message):
    print(("PASS " if condition else "FAIL ") + message)
    if not condition:
        failures.append(message)


# 1) README extraction on every eligible repo (needs the gh CLI + network; skipped in CI)
if os.environ.get("CI"):
    print("Skipping live README extraction in CI")
else:
    repos = gh.list_public_repos(config["github_user"], config["exclude_repos"])
    print(f"\n{len(repos)} eligible repos")
    for repo in repos:
        facts = gh.repo_facts(config["github_user"], repo)
        bad = [bullet for bullet in facts.bullets if gh.FILE_TOKEN.search(bullet) or bullet[:1] in "-*|"]
        print(f"  {facts.display_name:<26} bullets={len(facts.bullets):<3} images={len(facts.readme_images)}  e.g. {facts.bullets[:1]}")
        check(not bad, f"{repo['name']}: no file-path/markup bullets {bad[:1]}")


# 2) Guards
source = "EarlyStar tracks 24 h / 7 d / 30 d star gains"
check(composer.invented_numbers("Gained 24 stars in 7 d", source) == [], "numbers present in source are allowed")
check(composer.invented_numbers("Used by 5000 developers", source) == ["5000"], "invented number is caught")
original = "One detail in Cairn I'm quietly proud of: TODO comments become tasks that remember file and line."
check(composer.similarity(original, original) == 1.0, "identical text similarity = 1")
check(composer.similarity(original, "Undanu sends wedding invitations over WhatsApp with QR check-in.") < 0.1, "different text similarity is low")

# 3) Little-text escaping keeps hashtags clickable
little = to_little_text("Try it (free) #OpenSource #AI_tools")
check("\\(free\\)" in little, "parentheses escaped")
check("{hashtag|\\#|OpenSource}" in little, "hashtag converted to template")

# 4) Analytics export parsing (LinkedIn layout: two tables side by side)
with tempfile.TemporaryDirectory() as tmp:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "TOP POSTS"
    sheet.append(["Maximum of 50 posts available to include in this list"])
    sheet.append(["Post URL", "Post publish date", "Engagements", None, "Post URL", "Post publish date", "Impressions"])
    sheet.append(["https://www.linkedin.com/feed/update/urn:li:activity:1/", "11/11/2026", 40, None,
                  "https://www.linkedin.com/feed/update/urn:li:activity:1/", "11/11/2026", 1000])
    sheet.append(["https://www.linkedin.com/feed/update/urn:li:activity:2/", "11/12/2026", 5, None,
                  "https://www.linkedin.com/feed/update/urn:li:activity:2/", "11/12/2026", 900])
    path = Path(tmp) / "export.xlsx"
    workbook.save(path)
    records = analytics.parse_export(path)
    first = records.get("https://www.linkedin.com/feed/update/urn:li:activity:1/", {})
    check(first.get("engagements") == 40 and first.get("impressions") == 1000 and first.get("post_date") == "2026-11-11",
          "xlsx export parsed (engagements + impressions + date)")

# 5) Thompson scorer prefers the stronger performer
records = [
    {"repo": "buddy", "angle": "launch", "impressions": 1000, "engagements": 40},
    {"repo": "earlystar", "angle": "launch", "impressions": 900, "engagements": 5},
]
scorer = analytics.ThompsonScorer(records, "repo")
rng = random.Random(1)
wins_vs_weak = sum(scorer.sample("buddy", rng) > scorer.sample("earlystar", rng) for _ in range(1000))
wins_vs_new = sum(scorer.sample("buddy", rng) > scorer.sample("never-posted-repo", rng) for _ in range(1000))
check(scorer.ranked()[0][0] == "buddy", "ranked: buddy first")
check(wins_vs_weak > 950, f"proven winner beats proven loser ({wins_vs_weak}/1000)")
check(600 < wins_vs_new < 990, f"untested projects still get explored ({1000 - wins_vs_new}/1000 picks)")

# 6) Automatic GitHub-based scoring: LinkedIn-referred visitors + stars after a post
from datetime import datetime, timedelta

memory_db = sqlite3.connect(":memory:")
memory_db.executescript(
    """
    CREATE TABLE posts(slot TEXT, source TEXT, repo TEXT, angle TEXT, status TEXT, posted_at TEXT);
    CREATE TABLE metrics(url TEXT, post_date TEXT, impressions INTEGER, engagements INTEGER);
    CREATE TABLE github_signals(day TEXT, repo TEXT, li_views INTEGER, li_uniques INTEGER, stars INTEGER);
    """
)
post_day = datetime.now() - timedelta(days=10)
memory_db.execute("INSERT INTO posts VALUES('s','recycle','buddy','launch','posted',?)", (post_day.strftime("%Y-%m-%d 08:00"),))
for offset, visitors, stars in [(-1, 2, 5), (0, 2, 5), (4, 14, 7), (8, 20, 9)]:
    memory_db.execute(
        "INSERT INTO github_signals VALUES(?, 'buddy', 0, ?, ?)",
        ((post_day + timedelta(days=offset)).strftime("%Y-%m-%d"), visitors, stars),
    )
scored = analytics.github_performance(memory_db)
check(len(scored) == 1 and scored[0]["visitors"] == 18 and scored[0]["stars"] == 2,
      f"GitHub score = +18 LinkedIn visitors, +2 stars in week one ({scored})")
check(scored and scored[0]["engagements"] == 18 + 3 * 2, "combined score = visitors + 3 x stars")

# 7) "Try it" go-links: detection, rewriting, page content, click scoring
comment = "Try it: https://juw12.github.io/watt-reserve/\nDemo: https://juw12.github.io/cairn/?demo=1\nLanding: https://juw12.github.io/p/x/"
links = trylinks.find_site_links(comment, "juw12")
check(set(links) == {"watt-reserve", "cairn"}, f"site links found, reserved paths skipped ({sorted(links)})")
rewritten = trylinks.rewrite_links(comment, "juw12", {"watt-reserve", "cairn"})
check("https://juw12.github.io/go/watt-reserve/" in rewritten and "go/cairn/" in rewritten and "/p/x/" in rewritten,
      "links rewritten to tracked go-links, others untouched")
page = trylinks.render_page("cairn", "https://juw12.github.io/cairn/?demo=1", "juw12")
check("juw12.goatcounter.com/count" in page and 'location.replace(target)' in page and "noindex" in page,
      "go page counts the click, redirects, and is hidden from search")
memory_db.execute("CREATE TABLE try_clicks(day TEXT, repo TEXT, clicks INTEGER)")
memory_db.execute("INSERT INTO try_clicks VALUES(?, 'buddy', 7)", ((post_day + timedelta(days=1)).strftime("%Y-%m-%d"),))
scored = analytics.github_performance(memory_db)
check(scored[0]["try_clicks"] == 7 and scored[0]["engagements"] == 18 + 7 + 3 * 2,
      f"'Try it' clicks are added to the score ({scored[0]['engagements']})")

# Weekly cap: last week's Tue/Wed/Thu posts must not block this Tuesday's 08:00 slot
import autopilot  # noqa: E402
from datetime import datetime  # noqa: E402

cap_db = sqlite3.connect(":memory:")
cap_db.execute("CREATE TABLE posts(slot TEXT, status TEXT, posted_at TEXT)")
for posted_at in ("2026-11-10 08:02", "2026-11-11 08:01", "2026-11-12 08:03"):
    cap_db.execute("INSERT INTO posts VALUES(?, 'posted', ?)", (posted_at[:16], posted_at))
next_tuesday = datetime(2026, 11, 17, 8, 0)
same_thursday = datetime(2026, 11, 12, 9, 0)
check(autopilot.posts_this_week(cap_db, next_tuesday) == 0, "weekly cap resets on Monday (next Tuesday's slot still posts)")
check(autopilot.posts_this_week(cap_db, same_thursday) == 3, "weekly cap counts this week's posts")

# track_links: a failed go-page sync must not block the post - text goes out with the plain link
track_db = sqlite3.connect(":memory:")
track_db.execute("CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT)")
track_db.execute("INSERT INTO kv VALUES('go_pages', ?)", (json.dumps(["watt-reserve"]),))
import unittest.mock as mock  # noqa: E402

original_pages = autopilot.SECRETS_PATH
autopilot.SECRETS_PATH = Path(tempfile.mkdtemp()) / "secrets.json"
autopilot.SECRETS_PATH.parent.mkdir(parents=True, exist_ok=True)
autopilot.SECRETS_PATH.write_text(json.dumps({"goatcounter_token": "t-ok"}), encoding="utf-8")
config = {"github_user": "juw12", "goatcounter_code": "juw12"}
text = "Try it: https://juw12.github.io/watt-reserve/\nNew: https://juw12.github.io/dawn-falcon/"
with mock.patch("autopilot.trylinks.sync_go_pages", side_effect=RuntimeError("diverged .pages")):
    out = autopilot.track_links(config, track_db, text)
check("go/watt-reserve/" in out and "juw12.github.io/dawn-falcon/" in out and "go/dawn-falcon" not in out,
      f"track_links: a failed go-page sync keeps the plain link for new pages, cached go-links still used ({out!r})")
stored = json.loads(track_db.execute("SELECT value FROM kv WHERE key='go_pages'").fetchone()[0])
check(stored == ["watt-reserve"],
      f"track_links: a failed sync preserves the cached go_pages, doesn't overwrite with [] ({stored})")
with mock.patch("autopilot.trylinks.sync_go_pages", return_value={"watt-reserve", "dawn-falcon"}):
    out_ok = autopilot.track_links(config, track_db, text)
check("go/dawn-falcon/" in out_ok and "go/watt-reserve/" in out_ok,
      f"track_links: a successful sync still rewrites the new link and writes the cache ({out_ok!r})")
autopilot.SECRETS_PATH = original_pages

# config.local.json: a repository is excluded by name without the name being committed to this repository
original_config_paths = (autopilot.CONFIG_PATH, autopilot.LOCAL_CONFIG_PATH)
local_home = Path(tempfile.mkdtemp())
autopilot.CONFIG_PATH = local_home / "config.json"
autopilot.LOCAL_CONFIG_PATH = local_home / "config.local.json"
try:
    autopilot.CONFIG_PATH.write_text(json.dumps({"mode": "review", "exclude_repos": ["shown"]}), encoding="utf-8")
    check(autopilot.load_config() == {"mode": "review", "exclude_repos": ["shown"]},
          "config: without config.local.json the settings are exactly those of config.json")
    # Notepad saves UTF-8 with a byte order mark; other keys and non-text entries in the local file are ignored
    autopilot.LOCAL_CONFIG_PATH.write_text(
        "\ufeff" + json.dumps({"exclude_repos": ["Hidden", 7], "mode": "auto"}), encoding="utf-8")
    merged_config = autopilot.load_config()
    check(merged_config == {"mode": "review", "exclude_repos": ["shown", "Hidden"]},
          f"config: config.local.json adds excluded repos and changes nothing else ({merged_config})")
    listed = [{"name": "hidden"}, {"name": "shown"}, {"name": "kept"}, {"name": "secret", "private": True}]
    with mock.patch("github_source.gh_api", return_value=listed):
        eligible = [repo["name"] for repo in gh.list_public_repos("someone", merged_config["exclude_repos"])]
    check(eligible == ["kept"],
          f"config: a repo excluded in config.local.json is never eligible, and neither is a private one ({eligible})")
    autopilot.LOCAL_CONFIG_PATH.write_text("{not json", encoding="utf-8")
    try:
        autopilot.load_config()
        broken_local_stops = False
    except ValueError:
        broken_local_stops = True
    check(broken_local_stops, "config: a broken config.local.json stops the run instead of dropping the exclusions")
finally:
    autopilot.CONFIG_PATH, autopilot.LOCAL_CONFIG_PATH = original_config_paths
ignored_files = (Path(__file__).parent / ".gitignore").read_text(encoding="utf-8").split()
check("config.local.json" in ignored_files, "config: config.local.json is git-ignored, so its names are never committed")

# leakcheck: a private repository name never reaches this public repository, and a hit never prints the name
import leakcheck  # noqa: E402
import subprocess as leak_subprocess  # noqa: E402

leak_home = Path(tempfile.mkdtemp())
leak_scan = leak_home / "scan.json"
leak_local = leak_home / "config.local.json"
leak_scan.write_text(json.dumps({"repos": [
    {"name": "Quiet-Heron", "private": True}, {"name": "open-book", "private": False},
    {"name": "ab", "private": True}, {"name": "weekly", "private": True}]}), encoding="utf-8")
check(leakcheck.local_names(leak_scan, leak_home / "none.json") == ["quiet-heron", "weekly"],
      "leakcheck: the list is the private repos of the scan, lower case, without public ones and 2-letter names")
leak_local.write_text("\ufeff" + json.dumps({"leak_words": ["Tall Crane", 5], "leak_accepted": ["WEEKLY"]}),
                      encoding="utf-8")
leak_names = leakcheck.local_names(leak_scan, leak_local)
check(leak_names == ["quiet-heron", "tall crane"],
      f"leakcheck: config.local.json adds my words and takes out the names I accepted ({leak_names})")
check(leakcheck.local_names(leak_home / "none.json", leak_home / "none2.json") == []
      and leakcheck.compile_names([]) is None, "leakcheck: no scan and no local file means no list, not a crash")
check(leakcheck.parse_names("Quiet-Heron, tall crane\nquiet-heron\n\nab") == ["quiet-heron", "tall crane"],
      "leakcheck: a secret is read one name per line or comma, without duplicates")
leak_pattern = leakcheck.compile_names(leak_names)
check(leakcheck.lines_with_hits("fine\nsee Quiet-Heron here\nold-quiet-heron-2 and quiet-herons\n", leak_pattern) == [2],
      "leakcheck: a name is found in any case, and not inside a longer name")


def leak_git(*arguments):
    leak_subprocess.run(["git", "-C", str(leak_repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                         "-c", "commit.gpgsign=false", *arguments], capture_output=True, check=True)


leak_repo = leak_home / "repo"
leak_repo.mkdir()
leak_git("init", "-q")
(leak_repo / "notes.txt").write_text("first line\nabout quiet-heron\n", encoding="utf-8")
(leak_repo / "picture.bin").write_bytes(b"\0\0quiet-heron")
leak_git("add", "-A")
leak_git("commit", "-q", "-m", "start")
(leak_repo / "notes.txt").write_text("first line\nabout a bird\n", encoding="utf-8")
leak_git("add", "-A")
leak_git("commit", "-q", "-m", "tidy after Tall Crane")
check(leakcheck.scan_files(leak_repo, leak_pattern) == [],
      "leakcheck: a name that was removed is not reported in the tracked files, and a binary file is skipped")
leak_history = leakcheck.scan_history(leak_repo, leak_pattern)
check(len(leak_history) == 2 and leak_history[0].endswith("(message)") and leak_history[1].endswith(" notes.txt"),
      f"leakcheck: the history still shows the commit that added the name, and the commit message ({leak_history})")
(leak_repo / "notes.txt").write_text("first line\nabout a bird\nQUIET-HERON again\n", encoding="utf-8")
(leak_repo / "tall crane.md").write_text("ok\n", encoding="utf-8")
leak_git("add", "-A")
check(leakcheck.scan_files(leak_repo, leak_pattern) == ["notes.txt:3", "[listed name].md (the path itself)"],
      "leakcheck: a tracked line and a file name that hold a name are reported as a place, the name masked")
original_leak_paths = (leakcheck.CODE_DIR, leakcheck.SCAN_FILE, leakcheck.LOCAL_CONFIG)
leakcheck.CODE_DIR, leakcheck.SCAN_FILE, leakcheck.LOCAL_CONFIG = leak_repo, leak_scan, leak_local
import contextlib  # noqa: E402
import io  # noqa: E402

try:
    with mock.patch.dict(os.environ, {"PRIVATE_NAMES": ""}):
        leak_output = io.StringIO()
        with contextlib.redirect_stdout(leak_output):
            leak_exit = leakcheck.main(["--history"])
        leak_text = leak_output.getvalue().lower()
        check(leak_exit == 1 and "leak notes.txt:3" in leak_text,
              f"leakcheck: a hit ends with exit code 1 and names the place ({leak_exit})")
        check("quiet-heron" not in leak_text and "tall crane" not in leak_text,
              "leakcheck: the output never shows the name it found, so a public CI log stays clean")
    with mock.patch.dict(os.environ, {"PRIVATE_NAMES": "no-such-name"}):
        with contextlib.redirect_stdout(io.StringIO()):
            check(leakcheck.main(["--history"]) == 0, "leakcheck: the CI secret replaces the local list, and no hit is exit code 0")
    leakcheck.SCAN_FILE, leakcheck.LOCAL_CONFIG = leak_home / "none.json", leak_home / "none2.json"
    with mock.patch.dict(os.environ, {"PRIVATE_NAMES": ""}):
        with contextlib.redirect_stdout(io.StringIO()):
            check(leakcheck.main([]) == 0 and leakcheck.main(["--require"]) == 2,
                  "leakcheck: without a list a pull request from a fork passes, and --require on main fails")
    stored = {}

    def fake_secret_run(command, input=None, **keywords):
        stored["command"], stored["body"] = command, input
        return mock.Mock(returncode=0)

    with mock.patch("subprocess.run", side_effect=fake_secret_run):
        sync_message = leakcheck.sync(["quiet-heron", "tall crane"])
    check(stored.get("command") == ["gh", "secret", "set", "PRIVATE_NAMES"] and stored.get("body") == b"quiet-heron\ntall crane"
          and "quiet-heron" not in sync_message and "2 name(s)" in sync_message,
          "leakcheck: --sync sends the list to gh on standard input, never on the command line, and reports only a count")
    check("Nothing stored" in leakcheck.sync([]), "leakcheck: --sync with an empty list does not overwrite the secret")
finally:
    leakcheck.CODE_DIR, leakcheck.SCAN_FILE, leakcheck.LOCAL_CONFIG = original_leak_paths
workflow_text = (Path(__file__).parent / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
check("fetch-depth: 0" in workflow_text and "leakcheck.py --history" in workflow_text
      and "secrets.PRIVATE_NAMES" in workflow_text and "--require" in workflow_text,
      "leakcheck: the test workflow fetches the full history and runs the leak check against the secret")

# refresh_try_links (daily path) must also tolerate a failed go-page sync and not blow the cache
daily_db = sqlite3.connect(":memory:")
daily_db.executescript(
    """
    CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE repos(name TEXT PRIMARY KEY, first_seen TEXT, pushed_at TEXT, release_tag TEXT);
    CREATE TABLE try_clicks(day TEXT, repo TEXT, clicks INTEGER, PRIMARY KEY(day, repo));
    CREATE TABLE queue(id TEXT PRIMARY KEY, slot_date TEXT, repo TEXT, angle TEXT,
        body TEXT, comment TEXT, images TEXT, used INTEGER DEFAULT 0);
    """
)
daily_db.execute("INSERT INTO kv VALUES('go_pages', ?)", (json.dumps(["watt-reserve"]),))
autopilot.SECRETS_PATH = Path(tempfile.mkdtemp()) / "secrets.json"
autopilot.SECRETS_PATH.write_text(json.dumps({"goatcounter_token": "t-ok"}), encoding="utf-8")
with mock.patch("autopilot.trylinks.sync_go_pages", side_effect=RuntimeError("diverged .pages")), \
     mock.patch("autopilot.trylinks.fetch_go_clicks", return_value={}):
    autopilot.refresh_try_links(config, daily_db, {})
stored = json.loads(daily_db.execute("SELECT value FROM kv WHERE key='go_pages'").fetchone()[0])
check(stored == ["watt-reserve"], f"refresh_try_links: a failed sync keeps the cached go_pages, not [] ({stored})")
with mock.patch("autopilot.trylinks.sync_go_pages", return_value={"watt-reserve", "dawn-falcon"}):
    with mock.patch("autopilot.trylinks.fetch_go_clicks", return_value={}):
        autopilot.refresh_try_links(config, daily_db, {})
stored = json.loads(daily_db.execute("SELECT value FROM kv WHERE key='go_pages'").fetchone()[0])
check(stored == ["dawn-falcon", "watt-reserve"], f"refresh_try_links: a successful sync refreshes the cache ({stored})")
autopilot.SECRETS_PATH = original_pages

# Dashboard data: posted history with reach/clicks, and the upcoming schedule from the prepared queue
import dashboard  # noqa: E402

with tempfile.TemporaryDirectory() as temp_dir:
    real_paths = (autopilot.DB_PATH, autopilot.SECRETS_PATH, autopilot.TOKEN_PATH)
    autopilot.DB_PATH = Path(temp_dir) / "state.db"
    autopilot.SECRETS_PATH = Path(temp_dir) / "secrets.json"
    autopilot.TOKEN_PATH = Path(temp_dir) / "token.json"
    try:
        dash_db = autopilot.connect_db()
        posted_day = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        dash_db.execute(
            "INSERT INTO posts(slot, source, repo, angle, body, status, posted_at, url) VALUES(?,?,?,?,?,?,?,?)",
            (posted_day + " 08:00", "queue:x", "buddy", "launch", "Hook line\nmore", "posted", posted_day + " 08:01",
             "https://www.linkedin.com/feed/update/urn:li:share:1"),
        )
        dash_db.execute("INSERT INTO metrics VALUES(?,?,?,?,datetime('now'))", ("u", posted_day, 1200, 48))
        dash_db.execute("INSERT INTO try_clicks VALUES(?,?,?)", (posted_day, "buddy", 4))
        dash_db.execute("INSERT INTO github_signals VALUES(?,?,?,?,?)", (posted_day, "buddy", 9, 5, 2))
        dash_db.execute(
            "INSERT INTO queue(id, slot_date, repo, angle, body, comment, images) VALUES(?,?,?,?,?,?,?)",
            ("seed-x", "2000-01-01", "cairn", "launch", "Cairn hook", "Try it: x", "[]"),
        )
        dash_db.commit()
        dash_db.close()
        data = dashboard.build_data()
        history_row = data["history"][0]
        check(history_row["impressions"] == 1200 and history_row["hook"] == "Hook line",
              "dashboard: posted history carries LinkedIn reach")
        check(data["schedule"][0]["title"] == "cairn" and data["schedule"][0]["kind"] == "prepared",
              "dashboard: next slot shows the due prepared post")
        check(data["github"]["clicks_30d_total"] == 4 and data["overview"]["posted_total"] == 1,
              "dashboard: clicks and totals add up")
        json.dumps(data)
    finally:
        autopilot.DB_PATH, autopilot.SECRETS_PATH, autopilot.TOKEN_PATH = real_paths

# Other platforms: variants fit each limit, never add facts, publish/retry on their own
import logging  # noqa: E402

import channels  # noqa: E402
import hub  # noqa: E402

facts = gh.RepoFacts(
    name="watt-reserve", description="Plan home battery backup for blackouts, offline.",
    html_url="https://github.com/juw12/watt-reserve", homepage="https://juw12.github.io/watt-reserve/",
    topics=["offline-first", "pwa", "energy"], language="TypeScript", pushed_at="2026-11-01T00:00:00Z", license="MIT",
    tagline="Know how long your battery lasts.", title="Watt Reserve",
    bullets=["Adds up every appliance you choose", "Shows runtime for 3 battery sizes", "Works offline as an installable app",
             "Exports the plan as a printable page"],
    readme_images=["https://raw.githubusercontent.com/juw12/watt-reserve/main/shot.png"], has_ci=True,
)
post_body = composer.compose_launch(facts, random.Random(3), set(), True) + "\n\n#OpenSource #BuildInPublic #PWA"
post_comment = composer.links_for(facts)
paragraphs, tags, parsed_links = channels.split_post(post_body, post_comment)
check(not any(channels.COMMENT_POINTER.search(part) for part in paragraphs), "channels: 'links in the comment' pointers removed")
check(tags == ["#OpenSource", "#BuildInPublic", "#PWA"], f"channels: hashtag line split off ({tags})")
check(parsed_links[0] == ("Try it", "https://juw12.github.io/watt-reserve/"), f"channels: links parsed ({parsed_links[:1]})")
variants = channels.build_variants(post_body, post_comment, "launch", facts)
limits = {"bluesky": 300, "mastodon": 500, "telegram": 1024, "discord": 2000, "x": 280, "threads": 500, "substack": 1500}
for name, limit in limits.items():
    size = (channels.x_length if name == "x" else len)(variants.get(name, {}).get("body", ""))
    check(name in variants and size <= limit, f"channels: {name} fits {limit} chars ({size})")
    check("juw12.github.io/watt-reserve" in variants[name]["body"], f"channels: {name} carries the link")
check(set(variants) == set(channels.ALL), f"channels: a launch gets every platform ({sorted(variants)})")
check(variants["devto"]["extra"]["canonical_url"] == facts.homepage and "## What else it does" in variants["devto"]["body"]
      and variants["devto"]["extra"]["tags"][:2] == ["offlinefirst", "pwa"], "channels: Dev.to article has canonical URL, features, tags")
recycle_variants = channels.build_variants(post_body, post_comment, "deep_dive", facts)
check("devto" not in recycle_variants and "mastodon" in recycle_variants, "channels: recycled angles skip Dev.to only")
long_intro = "By day I build hardware. " * 30 + "\n\nSecond paragraph.\n\nPortfolio and GitHub in the first comment.\n\n#OpenSource"
short = channels.short_post(long_intro, "GitHub: https://github.com/juw12", 300, 1, 2, labelled=False)
check(len(short) <= 300 and short.endswith("#OpenSource") and "https://github.com/juw12" in short and "first comment" not in short,
      "channels: an over-long first paragraph is shortened, link and tags kept")
facet_text = "One detail → see https://juw12.github.io/x/ #OpenSource"
facets = channels.bluesky_facets(facet_text)
encoded = facet_text.encode("utf-8")
link_facet = next(facet for facet in facets if facet["features"][0]["$type"].endswith("#link"))
tag_facet = next(facet for facet in facets if facet["features"][0]["$type"].endswith("#tag"))
check(encoded[link_facet["index"]["byteStart"]:link_facet["index"]["byteEnd"]] == b"https://juw12.github.io/x/",
      "channels: Bluesky link facet uses UTF-8 byte offsets")
check(encoded[tag_facet["index"]["byteStart"]:tag_facet["index"]["byteEnd"]] == b"#OpenSource", "channels: Bluesky hashtag facet")

with tempfile.TemporaryDirectory() as temp_dir:
    from PIL import Image as PILImage

    noisy = PILImage.effect_noise((2600, 1600), 90).convert("RGB")
    big_png = Path(temp_dir) / "big_screenshot.png"
    noisy.save(big_png)
    data, width, height = channels.jpeg(big_png, 950_000)
    check(len(data) <= 950_000 and max(width, height) <= 2000, f"channels: screenshots re-encoded under Bluesky's limit ({len(data)} bytes)")

    channel_db = sqlite3.connect(":memory:")
    channel_config = {"channels": channels.ALL}
    channel_secrets = {"channels": {"bluesky": {"handle": "x", "app_password": "y"}, "mastodon": {"instance": "m", "access_token": "t"}}}
    queued = channels.prepare(channel_db, channel_config, channel_secrets, "2026-11-11 08:00", "watt-reserve", "launch",
                              post_body, post_comment, [str(big_png)], facts)
    check(sorted(queued) == ["bluesky", "mastodon"], f"channels: only connected platforms queued ({queued})")
    again = channels.prepare(channel_db, channel_config, channel_secrets, "2026-11-11 08:00", "watt-reserve", "launch",
                             post_body, post_comment, [str(big_png)], facts)
    later = channels.prepare(channel_db, channel_config, channel_secrets, "2026-11-25 08:00", "watt-reserve", "launch",
                             post_body + " again", post_comment, [], facts)
    check(again == [] and "bluesky" in later, "channels: queued once per slot")

    calls = []

    class FakeClient:
        fail = {"mastodon"}

        def __init__(self, creds):
            self.creds = creds

        def publish(self, text, title, images, link, extra, idempotency_key, video=None):
            channel = "mastodon" if "instance" in self.creds else "bluesky"
            calls.append((channel, len(images), idempotency_key))
            if channel in self.fail:
                raise RuntimeError("HTTP 503")
            return f"https://example.test/{channel}"

    real_clients = dict(channels.CLIENTS)
    channels.CLIENTS.update({"bluesky": FakeClient, "mastodon": FakeClient})
    quiet = logging.getLogger("selftest-channels")
    quiet.addHandler(logging.NullHandler())
    quiet.propagate = False
    try:
        for _ in range(5):
            channels.publish_pending(channel_db, channel_secrets, lambda text: text, quiet)
    finally:
        channels.CLIENTS.clear()
        channels.CLIENTS.update(real_clients)
    statuses = {
        (slot, channel): (status, attempts)
        for slot, channel, status, attempts in channel_db.execute("SELECT slot, channel, status, attempts FROM channel_posts")
    }
    check(statuses[("2026-11-11 08:00", "bluesky")] == ("posted", 0), "channels: Bluesky posted")
    check(sum(1 for call in calls if call[0] == "bluesky" and "2026-11-11" in call[2]) == 1, "channels: a posted platform is never re-posted")
    check(statuses[("2026-11-11 08:00", "mastodon")] == ("failed", channels.MAX_ATTEMPTS),
          f"channels: a failing platform stops after {channels.MAX_ATTEMPTS} tries ({statuses[('2026-11-11 08:00', 'mastodon')]})")
    check(any(call[1] == 1 for call in calls if call[0] == "bluesky"), "channels: screenshot attached")


# X: weighted length, monthly budget cap, token refresh (rotating refresh token saved), publish, PKCE sign-in
check(channels.x_length("see https://juw12.github.io/a/very/long/path/that/x/shortens/") == 4 + 23
      and channels.x_length("日本") == 4, "x: links count 23, CJK counts 2")
x_post = variants["x"]["body"]
check(x_post.count("https://") == 1 and channels.x_cost(x_post) == channels.X_COST_LINK,
      f"x: one link per post, priced as a link post ({channels.x_cost(x_post)})")
budget_db = sqlite3.connect(":memory:")
budget_config = {"channels": ["x"], "x_monthly_budget_usd": 0.3}
budget_secrets = {"channels": {"x": {"client_id": "id", "access_token": "a", "refresh_token": "r", "expires_at": 0}}}
first_x = channels.prepare(budget_db, budget_config, budget_secrets, "2026-11-11 08:00", "watt-reserve", "launch",
                           post_body, post_comment, [], facts)
second_x = channels.prepare(budget_db, budget_config, budget_secrets, "2026-11-12 08:00", "watt-reserve", "launch",
                            post_body, post_comment, [], facts)
check(first_x == ["x"] and second_x == [] and channels.x_spent_this_month(budget_db) == channels.X_COST_LINK,
      f"x: monthly budget stops queueing ({first_x}, {second_x}, ${channels.x_spent_this_month(budget_db)})")
zero_budget = channels.prepare(sqlite3.connect(":memory:"), {"channels": ["x"], "x_monthly_budget_usd": 0},
                               budget_secrets, "2026-11-11 08:00", "watt-reserve", "launch", post_body, post_comment, [], facts)
check(zero_budget == [], "x: a budget of 0 never posts")


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status
        self.text = json.dumps(payload)
        self.headers = {"Content-Type": "application/json"}

    def json(self):
        return self.payload


with tempfile.TemporaryDirectory() as temp_dir:
    x_calls = []

    def fake_x_post(url, **kwargs):
        x_calls.append((url, kwargs))
        if url == channels.X_TOKEN:
            form = kwargs["data"]
            if form["grant_type"] == "refresh_token":
                return FakeResponse({"access_token": "fresh", "refresh_token": "rotated", "expires_in": 7200})
            return FakeResponse({"access_token": "first", "refresh_token": "r1", "expires_in": 7200})
        if url.endswith("/2/media/upload"):
            return FakeResponse({"data": {"id": "m1"}})
        if url.endswith("/2/tweets"):
            return FakeResponse({"data": {"id": "123"}}, 201)
        return FakeResponse({"error": "unexpected"}, 500)

    real_x_post = channels.requests.post
    real_secrets_path = channels.SECRETS_PATH
    channels.requests.post = fake_x_post
    channels.SECRETS_PATH = Path(temp_dir) / "secrets.json"
    channels.SECRETS_PATH.write_text(json.dumps({"client_id": "linkedin", "channels": {"bluesky": {"handle": "b"}}}))
    try:
        shot = Path(temp_dir) / "shot.png"
        PILImage.new("RGB", (400, 300), "white").save(shot)
        x_creds = {"client_id": "cid", "client_secret": "sec", "access_token": "old", "refresh_token": "r0",
                   "expires_at": 0, "username": "juw12"}
        x_url = channels.X(x_creds).publish(text=x_post, title="", images=[shot], link="", extra={}, idempotency_key="k")
        saved = json.loads(channels.SECRETS_PATH.read_text())
        tweet_call = next(kwargs for url, kwargs in x_calls if url.endswith("/2/tweets"))
        refresh_call = next(kwargs for url, kwargs in x_calls if url == channels.X_TOKEN)
        check(x_url == "https://x.com/juw12/status/123", f"x: post URL ({x_url})")
        check(tweet_call["json"] == {"text": x_post, "media": {"media_ids": ["m1"]}}
              and tweet_call["headers"]["Authorization"] == "Bearer fresh", "x: post sent with screenshot and fresh token")
        check(refresh_call["auth"] == ("cid", "sec") and refresh_call["data"]["refresh_token"] == "r0",
              "x: confidential app refreshes with Basic auth")
        check(saved["channels"]["x"]["refresh_token"] == "rotated" and saved["client_id"] == "linkedin"
              and "bluesky" in saved["channels"], "x: rotated refresh token saved, other secrets kept")

        def fake_browser(authorize_url):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(authorize_url).query)
            fake_browser.challenge = query["code_challenge"][0]
            callback = f"{channels.X_REDIRECT}?code=abc&state={query['state'][0]}"
            threading.Thread(target=lambda: urllib.request.urlopen(callback, timeout=10).read(), daemon=True).start()

        x_calls.clear()
        signed_in = channels.x_sign_in("cid", "", open_browser=fake_browser, wait_seconds=20)
        exchange = next(kwargs for url, kwargs in x_calls if url == channels.X_TOKEN)
        verifier = exchange["data"]["code_verifier"]
        expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        check(signed_in["access_token"] == "first" and signed_in["refresh_token"] == "r1"
              and "client_secret" not in signed_in and exchange["auth"] is None
              and exchange["data"]["client_id"] == "cid" and exchange["data"]["code"] == "abc",
              "x: browser sign-in swaps the code for tokens (public app)")
        check(fake_browser.challenge == expected_challenge and 43 <= len(verifier) <= 128,
              "x: PKCE S256 challenge matches the verifier")
    finally:
        channels.requests.post = real_x_post
        channels.SECRETS_PATH = real_secrets_path


# X via Buffer (free): account discovery, post payload, no budget cap, setup choice, image choice
buffer_calls = []


def fake_buffer_post(url, **kwargs):
    buffer_calls.append((url, kwargs))
    query = kwargs["json"]["query"]
    if "organizations" in query:
        return FakeResponse({"data": {"account": {"organizations": [{"id": "org1"}]}}})
    if "channels(" in query:
        return FakeResponse({"data": {"channels": [
            {"id": "li1", "name": "Juwen", "service": "linkedin", "isDisconnected": False},
            {"id": "tw1", "name": "juw12", "service": "twitter", "isDisconnected": False},
            {"id": "th1", "name": "juw12", "service": "threads", "isDisconnected": False},
            {"id": "ss1", "name": "Juwen Lie", "service": "substack", "isDisconnected": False},
        ]}})
    if "createPost" in query:
        return FakeResponse({"data": {"createPost": {"post": {"id": "p1", "externalLink": "https://x.com/juw12/status/9"}}}})
    return FakeResponse({"errors": [{"message": "unexpected"}]})


real_buffer_post = channels.requests.post
channels.requests.post = fake_buffer_post
try:
    buffer_creds = channels.x_setup("buffer-key", ask=lambda prompt: "1")
    check(buffer_creds == {"via": "buffer", "api_key": "buffer-key", "channel_id": "tw1", "username": "juw12"},
          f"x/buffer: setup finds the X account in Buffer ({buffer_creds})")
    check(channels.x_client(buffer_creds).verify().startswith("@juw12") and isinstance(channels.x_client(buffer_creds), channels.XViaBuffer)
          and isinstance(channels.x_client({"client_id": "c"}), channels.X), "x/buffer: Buffer creds use Buffer, others the X API")
    buffer_calls.clear()
    buffer_url = channels.XViaBuffer(buffer_creds).publish(
        text=x_post, title="", images=[], link="", extra={"image_url": "https://example.test/shot.png"}, idempotency_key="k")
    create_call = next(kwargs for url, kwargs in buffer_calls if "createPost" in kwargs["json"]["query"])
    sent = create_call["json"]["variables"]["input"]
    check(buffer_url == "https://x.com/juw12/status/9" and create_call["headers"]["Authorization"] == "Bearer buffer-key"
          and sent == {"text": x_post, "channelId": "tw1", "schedulingType": "automatic", "mode": "shareNow",
                       "assets": [{"image": {"url": "https://example.test/shot.png"}}]},
          "x/buffer: posts now to the X channel with text and image")
    threads_creds = channels.buffer_setup("buffer-key", "threads")
    substack_creds = channels.buffer_setup("buffer-key", "substack")
    check(threads_creds["channel_id"] == "th1" and substack_creds["channel_id"] == "ss1"
          and substack_creds["username"] == "Juwen Lie", "threads/substack: setup finds each account in Buffer")
    buffer_calls.clear()
    channels.CLIENTS["threads"](threads_creds).publish(
        text=variants["threads"]["body"], title="", images=[], link="", extra=variants["threads"]["extra"], idempotency_key="k")
    threads_sent = next(kwargs for url, kwargs in buffer_calls if "createPost" in kwargs["json"]["query"])["json"]["variables"]["input"]
    check(threads_sent["channelId"] == "th1" and threads_sent["mode"] == "shareNow" and threads_sent["assets"],
          "threads: posted now through Buffer with the project image")
finally:
    channels.requests.post = real_buffer_post
threads_post = variants["threads"]["body"]
check(threads_post.count("#") <= 1 and threads_post.count("https://") == 1 and "juw12.github.io/watt-reserve" in threads_post,
      f"threads: one tag and the project link ({len(threads_post)} chars)")
free_db = sqlite3.connect(":memory:")
free_secrets = {"channels": {"x": {"via": "buffer", "api_key": "k", "channel_id": "tw1"}}}
free_config = {"channels": ["x"], "x_monthly_budget_usd": 0}
free_queued = [channels.prepare(free_db, free_config, free_secrets, f"2026-11-{day:02d} 08:00", "watt-reserve", "launch",
                                post_body, post_comment, [], facts) for day in (11, 12, 13)]
check(free_queued == [["x"], ["x"], ["x"]], f"x/buffer: free route ignores the paid budget ({free_queued})")
check(variants["x"]["extra"]["image_url"] == "https://raw.githubusercontent.com/juw12/watt-reserve/main/shot.png",
      f"x/buffer: README image used as the post image ({variants['x']['extra'].get('image_url')})")
check(channels.public_image(type("F", (), {"readme_images": ["https://x.test/a.svg"], "html_url": "https://github.com/juw12/kilat"})())
      == "https://opengraph.githubassets.com/reachpilot/juw12/kilat", "x/buffer: GitHub card when the README has no photo")

todo = hub.channels_todo({
    "not_connected": ["Bluesky"],
    "failed": [{"slot": "2026-11-11 08:00", "channel": "Mastodon", "error": "HTTP 503"}],
})
check([item["action"] for item in todo] == ["channels_setup", "channels_setup"], "hub: platform to-dos (connect, failures)")

# hub: launch tests - a project's public summary.json plus private steps kept on this PC
launch_home = Path(tempfile.mkdtemp(prefix="rp-launch-"))
launch_real = (hub._launch_fetch, hub.LAUNCH_CACHE, hub.LAUNCH_PRIVATE)
launch_test = {"id": "demo", "name": "Demo", "summary_url": "https://example.test/summary.json"}
launch_answer = {
    "schema": 1, "name": "Demo app", "phase": "before", "outcome": "waiting",
    "headline": "The launch test has not started yet.",
    "start": "2026-11-28T00:00:00+07:00", "end": "2026-12-13T00:00:00+07:00",
    "totals": {"testers": 0, "regulars": 0, "letters": 0},
    "need": {"testers": 30, "regulars": 10, "letters": 5},
    "stop": {"testers": 10, "letters": 1},
    "labels": {"testers": "People who ran a test"},
    "links": {"app": "https://example.test/app", "repo": "javascript:alert(1)"},
    "needsMe": [
        {"id": "invite", "title": "Message 30 people", "status": "last-tap", "due": "2026-11-28", "detail": "Texts are ready.",
         "links": [{"label": "Texts", "url": "https://example.test/texts"}, {"label": "Bad", "url": "javascript:alert(2)"}]},
        {"id": "name", "title": "Trademark search", "status": "blocked", "detail": "The site blocks robots."},
        {"id": "seen", "title": "Counting off on this PC", "status": "done"},
        {"id": "odd", "title": "Unknown state", "status": "whatever"},
        "not a step",
    ],
}
launch_now = datetime.fromisoformat("2026-11-14T12:00:00+07:00")
try:
    hub.LAUNCH_CACHE = launch_home / "cache"
    hub.LAUNCH_PRIVATE = launch_home / "launch_private.json"
    hub._launch_fetch = lambda url: None
    nothing = hub._launch_view(launch_test, hub._launch_summary(launch_test), now=launch_now)
    check(nothing["available"] is False and nothing["stale"] is True and nothing["needs_me"] == [] and nothing["name"] == "Demo",
          "hub launch: offline with no saved answer shows as not available")

    hub._launch_fetch = lambda url: launch_answer
    fresh = hub._launch_view(launch_test, hub._launch_summary(launch_test), now=launch_now)
    check(fresh["available"] and not fresh["stale"] and fresh["name"] == "Demo app" and fresh["when"] == "starts in 14 days",
          f"hub launch: fresh answer read ({fresh['when']})")
    check([(c["label"], c["have"], c["need"], c["stop"]) for c in fresh["counts"]]
          == [("People who ran a test", 0, 30, 10), ("Regulars", 0, 10, None), ("Letters", 0, 5, 1)],
          "hub launch: each count with what is needed and the stop line")
    check([step["status"] for step in fresh["needs_me"]] == ["last-tap", "blocked", "done", "yours"] and fresh["open"] == 3,
          "hub launch: steps parsed, unknown state shown as 'only you', done ones not counted as open")
    check(fresh["links"] == [{"label": "App", "url": "https://example.test/app"}]
          and [entry["label"] for entry in fresh["needs_me"][0]["links"]] == ["Texts"],
          "hub launch: only web links are kept")
    check(not any(step["private"] for step in fresh["needs_me"]), "hub launch: no private file, only the public steps")

    hub.LAUNCH_PRIVATE.write_text(json.dumps({"demo": {"needsMe": [
        {"id": "form", "title": "Sign the form", "status": "yours", "due": "2026-11-16", "detail": "See my notes."}]}}),
        encoding="utf-8")
    private_steps = hub._launch_dict(hub._launch_private().get("demo")).get("needsMe")
    merged = hub._launch_view(launch_test, hub._launch_summary(launch_test), private_steps, now=launch_now)
    check([step["private"] for step in merged["needs_me"]] == [False, False, False, False, True] and merged["open"] == 4,
          "hub launch: private steps added and marked private")

    hub._launch_fetch = lambda url: None
    offline = hub._launch_view(launch_test, hub._launch_summary(launch_test), now=launch_now)
    check(offline["available"] and offline["stale"] and offline["read_at"] and offline["counts"][0]["need"] == 30,
          "hub launch: offline shows the last good answer and says so")

    launch_items = hub.launch_todo({"tests": [merged]}, today=launch_now.date())
    check([(item["priority"], item["title"]) for item in launch_items] == [
        (2, "Demo app: Message 30 people"), (3, "Demo app: Trademark search"),
        (3, "Demo app: Unknown state"), (1, "Demo app: Sign the form")],
        "hub launch: to-dos skip done steps; urgent within 3 days of the date")
    check(launch_items[0]["detail"] == "One tap left, by 2026-11-28. Texts are ready." and launch_items[0]["action"] is None,
          "hub launch: to-do says the state and the date")
    check(hub.launch_todo(None) == [] and hub.launch_todo({"error": "x"}) == [], "hub launch: no section, no to-dos")
    over = hub.launch_todo({"tests": [{**merged, "phase": "final", "headline": "Go on.", "needs_me": []}]}, today=launch_now.date())
    check(len(over) == 1 and over[0]["priority"] == 1 and over[0]["title"] == "Demo app launch test is over: Go on.",
          "hub launch: the verdict becomes an urgent to-do")
    check([entry["module"] for entry in hub.help_sections({}, None, {"tests": [merged]})][-1] == "Launch"
          and "Launch" not in [entry["module"] for entry in hub.help_sections({}, None, None)],
          "hub launch: help entry only when a launch test exists")
finally:
    hub._launch_fetch, hub.LAUNCH_CACHE, hub.LAUNCH_PRIVATE = launch_real
    for cache_key in [key for key in hub._cache if str(key).startswith("launch:")]:
        hub._cache.pop(cache_key, None)

# Readers: a project's own metrics.csv (hub.py readers_section / readers_view), read-only
from datetime import date  # noqa: E402

metrics_csv = ("date,subscribers,posts_counted,median_views,views_per_100_subscribers\n"
               "2026-11-17,2,5,2,100\n"
               "2026-11-18,2,5,2,100\n")
readers_rows = hub.parse_metrics(metrics_csv)
check([row["date"] for row in readers_rows] == ["2026-11-17", "2026-11-18"]
      and readers_rows[-1]["subscribers"] == 2 and readers_rows[-1]["views"] == 2,
      "readers: the CSV becomes rows, oldest first, subscribers and typical views kept")
check(hub.parse_metrics(metrics_csv + "2026-11-18,7,5,3,43\n")[-1]["subscribers"] == 7
      and len(hub.parse_metrics(metrics_csv + "2026-11-18,7,5,3,43\n")) == 2,
      "readers: a day written twice keeps the last line, not a second row")
check(hub.parse_metrics("") == [] and hub.parse_metrics("nonsense\n") == []
      and hub.parse_metrics("date,subscribers,median_views\n2026-11-18,abc,2\n") == []
      and hub.parse_metrics("\ufeffdate,subscribers,median_views\n2026-11-18,2,2\n")[0]["subscribers"] == 2,
      "readers: an empty, half-written or BOM-prefixed file gives no rows (or the good rows) instead of an error")

readers_today = date(2026, 11, 18)
readers_view = hub.readers_view(readers_rows, 250, "2026-12-31", readers_today)
check(readers_view["available"] and readers_view["subscribers"] == 2 and readers_view["views"] == 2
      and readers_view["days_left"] == 43 and readers_view["date"] == "2026-11-18",
      "readers: the latest numbers, and 43 days left from 18 Nov to 31 Dec")
check(readers_view["needed"] == 248 and readers_view["per_day"] == 6 and readers_view["per_week"] == 42,
      "readers: the pace the 250 target needs (248 to go in 43 days: about 6 a day)")
check(readers_view["progress"] == 1 and readers_view["gained"] == 0 and readers_view["reached"] is False
      and readers_view["headline"] == "2 subscribers, 2 typical views a post",
      "readers: progress against the target, the change since the first row, and a plain headline")
readers_done = hub.readers_view(
    [{"date": "2026-12-20", "subscribers": 260, "views": 40, "posts": 5, "per_100": 15}], 250, "2026-12-31",
    date(2026, 12, 20))
check(readers_done["reached"] and readers_done["needed"] == 0 and readers_done["per_day"] == 0
      and readers_done["progress"] == 100,
      "readers: past the target there is no pace left to ask for")
check(hub.readers_view([], 250, "2026-12-31", readers_today)["available"] is False,
      "readers: no rows says so instead of showing a zero")

readers_real = (hub._readers_fetch, hub.READERS_CACHE)
readers_home = Path(tempfile.mkdtemp())
try:
    hub.READERS_CACHE = readers_home / "cache"
    hub._readers_fetch = lambda url: None
    for cache_key in [key for key in hub._cache if str(key).startswith("readers:")]:
        hub._cache.pop(cache_key, None)
    offline = hub.readers_section(today=readers_today)
    check(offline["installed"] is False and offline["products"][0]["available"] is False
          and offline["products"][0]["name"] == "Morrow Brief",
          "readers: offline with no cache says so, and the dashboard still builds")
    hub.READERS_CACHE.mkdir(parents=True, exist_ok=True)
    (hub.READERS_CACHE / "morrow.csv").write_text(metrics_csv, encoding="utf-8")
    for cache_key in [key for key in hub._cache if str(key).startswith("readers:")]:
        hub._cache.pop(cache_key, None)
    cached_section = hub.readers_section(today=readers_today)["products"][0]
    check(cached_section["available"] and cached_section["stale"] and cached_section["subscribers"] == 2
          and cached_section["target"] == 250 and cached_section["read_at"],
          "readers: offline, the last good file is shown and marked as read from the cache")
    hub._readers_fetch = lambda url: metrics_csv.replace("2026-11-18,2,5,2,100", "2026-11-18,3,5,2,67")
    for cache_key in [key for key in hub._cache if str(key).startswith("readers:")]:
        hub._cache.pop(cache_key, None)
    live = hub.readers_section(today=readers_today)["products"][0]
    check(live["stale"] is False and live["subscribers"] == 3
          and "3,5,2,67" in (hub.READERS_CACHE / "morrow.csv").read_text(encoding="utf-8"),
          "readers: a live answer is used and kept for the next offline start")
    check(all(product["source_url"].endswith("/morrow/social-data/metrics.csv")
              for product in hub.readers_section(today=readers_today)["products"]),
          "readers: the card names the file it read")
finally:
    hub._readers_fetch, hub.READERS_CACHE = readers_real
    shutil.rmtree(readers_home, ignore_errors=True)
    for cache_key in [key for key in hub._cache if str(key).startswith("readers:")]:
        hub._cache.pop(cache_key, None)

# hub: the TradeBot card - five steps from the trading bot's public status page, read only
import re  # noqa: E402
import shutil  # noqa: E402

tradebot_now = datetime.fromisoformat("2026-11-16T15:00:00+07:00")
tradebot_ms = int(tradebot_now.timestamp() * 1000)


def tradebot_status(**indodax):
    block = {"configured": True, "lastReportAt": tradebot_ms - 60000, "mode": "practice", "paused": False,
             "keys": False, "checked": False, "tripped": False, "openCopies": 2, "realOpen": 0}
    block.update(indodax)
    return {"ok": True, "service": "trade-bot", "version": "0.10.0", "build": "test", "money": "practice",
            "configured": True, "bot": "demo_trade_bot", "paired": True, "running": True,
            "lastRound": {"at": tradebot_ms - 2 * 60000, "markets": 12, "errors": 0}, "indodax": block}


def tradebot_card(status, stale=False):
    return hub._tradebot_view({"summary": status, "stale": stale, "read_at": "Mon 16 Nov · 15:00"}, now=tradebot_now)


practice = tradebot_card(tradebot_status())
check(practice["available"] and practice["version"] == "0.10.0" and practice["published"] and not practice["real"],
      "hub tradebot: a full answer makes an available card")
check([step["title"] for step in practice["steps"]] == [
    "The bot reads the markets", "The sender on a PC asks for the signals", "Copies at Indodax",
    "The real-money gate", "Control and alerts in Telegram"], "hub tradebot: five steps in the order the chain runs")
check(practice["headline"] == "Practice: 2 copies open" and [step["state"] for step in practice["steps"]] == ["good"] * 5,
      "hub tradebot: practice with a reporting sender is all good")
check(practice["steps"][3]["state_label"] == "closed" and "No real buy can be sent" in practice["steps"][3]["detail"],
      "hub tradebot: the gate reads closed in practice")
check("@demo_trade_bot" in practice["steps"][4]["detail"], "hub tradebot: the control step names the bot")
check(practice["needs_me"] == [] and hub.tradebot_todo(practice) == [], "hub tradebot: practice needs nothing from me")
check(all(step["how"] and step["detail"] and step["state_label"] for step in practice["steps"]),
      "hub tradebot: every step says how it works and what it is doing now")

real_open = tradebot_card(tradebot_status(mode="real", keys=True, checked=True, openCopies=2, realOpen=1))
check(real_open["real"] and real_open["headline"] == "Real money: 1 real copy open"
      and real_open["steps"][3]["state_label"] == "open" and real_open["steps"][3]["state"] == "warn"
      and real_open["needs_me"] == [], "hub tradebot: real money is said plainly and is not an alarm by itself")

silent_real = tradebot_card(tradebot_status(mode="real", keys=True, checked=True, realOpen=1,
                                            lastReportAt=tradebot_ms - 25 * 60000))
silent_todo = hub.tradebot_todo(silent_real)
check(silent_real["steps"][1]["state"] == "bad" and silent_real["headline"] == "Sender silent with real money in play",
      "hub tradebot: a silent sender with real money is bad")
check(len(silent_todo) == 1 and silent_todo[0]["priority"] == 1 and silent_todo[0]["module"] == "TradeBot"
      and silent_todo[0]["title"] == "TradeBot: the Indodax sender is silent with real money in play"
      and "25 minutes ago" in silent_todo[0]["detail"] and silent_todo[0]["action"] is None,
      "hub tradebot: a silent sender with real money is an urgent to-do with no button")
silent_practice = tradebot_card(tradebot_status(lastReportAt=tradebot_ms - 25 * 60000))
check(silent_practice["steps"][1]["state"] == "warn" and [item["priority"] for item in silent_practice["needs_me"]] == [3],
      "hub tradebot: a silent sender in practice is a low to-do")
check(tradebot_card(tradebot_status(lastReportAt=tradebot_ms - 9 * 60000))["needs_me"] == [],
      "hub tradebot: nine minutes without a report is not silent yet")

tripped_real = tradebot_card(tradebot_status(mode="real", keys=True, checked=True, realOpen=1, tripped=True))
check(tripped_real["headline"] == "Safety stop tripped" and tripped_real["steps"][2]["state"] == "bad"
      and [item["priority"] for item in tripped_real["needs_me"]] == [1]
      and "-Clear" in tripped_real["needs_me"][0]["detail"], "hub tradebot: a tripped stop with real money is urgent")
check([item["priority"] for item in tradebot_card(tradebot_status(tripped=True))["needs_me"]] == [2],
      "hub tradebot: a tripped stop in practice is a normal to-do")

unchecked = tradebot_card(tradebot_status(mode="real", keys=True, checked=False))
check(unchecked["steps"][3]["state"] == "bad" and [item["priority"] for item in unchecked["needs_me"]] == [2]
      and "-Check" in unchecked["needs_me"][0]["detail"], "hub tradebot: real mode with an unchecked key asks for the check")

paused = tradebot_card(tradebot_status(paused=True))
check(paused["headline"] == "Practice: 2 copies open, new buys paused" and paused["steps"][2]["state_label"] == "paused"
      and "Sells and the stop still work" in paused["steps"][2]["detail"] and paused["needs_me"] == [],
      "hub tradebot: a pause is shown and is not a to-do")

old_build = tradebot_status()
del old_build["indodax"]
old_build["version"] = "0.8.0"
old_card = tradebot_card(old_build)
check(old_card["available"] and old_card["headline"] == "The live build has no Indodax sender yet"
      and not old_card["published"] and old_card["needs_me"] == [] and len(old_card["steps"]) == 5
      and old_card["steps"][0]["state"] == "good", "hub tradebot: an older build without Indodax still makes a calm card")

not_set_up = tradebot_card(tradebot_status(configured=False, lastReportAt=None, mode=None, keys=None, checked=None,
                                           tripped=None, openCopies=None, realOpen=None))
check(not_set_up["headline"] == "Indodax sender not set up" and [item["priority"] for item in not_set_up["needs_me"]] == [3],
      "hub tradebot: no sender key is a low to-do")
never = tradebot_card(tradebot_status(lastReportAt=None, mode=None, keys=None, checked=None, tripped=None,
                                      openCopies=None, realOpen=None))
check(never["headline"] == "Sender key issued, sender not started" and never["steps"][1]["state_label"] == "never reported"
      and never["steps"][3]["state_label"] == "no report yet", "hub tradebot: a key without a report says the sender never started")

late_round = tradebot_status()
late_round["lastRound"]["at"] = tradebot_ms - 40 * 60000
check(tradebot_card(late_round)["steps"][0]["state_label"] == "late", "hub tradebot: a round 40 minutes old is late")

offline = tradebot_card(tradebot_status(mode="real", keys=True, checked=True, realOpen=1, tripped=True,
                                        lastReportAt=tradebot_ms - 90 * 60000), stale=True)
check(offline["stale"] and offline["needs_me"] == [] and hub.tradebot_todo(offline) == []
      and offline["steps"][0]["state_label"].startswith("last known")
      and offline["steps"][1]["state_label"].startswith("last known"),
      "hub tradebot: an offline answer is last known and raises no to-do")

for garbage in (None, "text", [], {}, {"service": "something-else", "indodax": {"paused": False}}):
    broken = tradebot_card(garbage)
    check(broken["installed"] and not broken["available"] and broken["steps"] == [] and hub.tradebot_todo(broken) == [],
          f"hub tradebot: {type(garbage).__name__} answer is not a card")
hostile = tradebot_status(mode="<script>", openCopies="many", realOpen=-3, keys="yes", lastReportAt="soon")
hostile["version"] = "<b>9</b>"
hostile["bot"] = "x y <img>"
hostile["lastRound"] = "never"
hostile_card = tradebot_card(hostile)
check(hostile_card["available"] and hostile_card["version"] is None and "<" not in json.dumps(hostile_card["steps"])
      and "the bot" in hostile_card["steps"][4]["detail"], "hub tradebot: odd values from the network are dropped, not shown")

check(hub.tradebot_todo(None) == [] and hub.tradebot_todo({"installed": True, "available": False}) == [],
      "hub tradebot: no section, no to-dos")
check(hub.help_sections({}, None, None, None, practice)[-1]["module"] == "TradeBot"
      and "TradeBot" not in [entry["module"] for entry in hub.help_sections({}, None, None, None)],
      "hub tradebot: help entry only when the card exists")
tradebot_words = json.dumps([practice, real_open, silent_real, tripped_real, hub.help_sections({}, None, None, None, practice)[-1]])
check(not re.search(r"Rp\s?\d|\b(BTC|ETH|USDT|IDR)\b", tradebot_words), "hub tradebot: no amount and no coin on the card")

tradebot_home = Path(tempfile.mkdtemp(prefix="rp-tradebot-"))
tradebot_real = (hub._launch_fetch, hub.LAUNCH_CACHE)
try:
    hub.LAUNCH_CACHE = tradebot_home / "cache"
    hub._cache.pop("tradebot", None)
    hub._launch_fetch = lambda url: None
    check(hub.tradebot_section()["available"] is False, "hub tradebot: no answer and no cache is a card that says so")
    hub._cache.pop("tradebot", None)
    asked = []
    hub._launch_fetch = lambda url: asked.append(url) or tradebot_status()
    first = hub.tradebot_section()
    hub.tradebot_section()
    check(first["available"] and asked == [hub.TRADEBOT_STATUS_URL], "hub tradebot: one read of the status page, then the cache")
    hub._cache.pop("tradebot", None)
    hub._launch_fetch = lambda url: None
    kept = hub.tradebot_section()
    check(kept["available"] and kept["stale"] and hub.tradebot_todo(kept) == [],
          "hub tradebot: offline shows the last good answer and asks for nothing")
finally:
    hub._launch_fetch, hub.LAUNCH_CACHE = tradebot_real
    hub._cache.pop("tradebot", None)
    shutil.rmtree(tradebot_home, ignore_errors=True)

# hub: the podcast tile - the reader mirrors podcast.py's own rules (published + audio on the site; pending = held for review)
podcast_home = Path(tempfile.mkdtemp(prefix="rp-podcast-"))
try:
    (podcast_home / "site" / "audio").mkdir(parents=True)
    (podcast_home / "episodes" / "2026-11-01-ep001").mkdir(parents=True)
    (podcast_home / "episodes" / "2026-11-03-ep002").mkdir(parents=True)
    (podcast_home / "podcast.py").write_text("", encoding="utf-8")
    (podcast_home / "podcast_config.json").write_text(json.dumps({
        "show_title": "Test Show",
        "series": {
            "brief": {"name": "The Brief", "days": ["Monday"], "time": "07:30"},
            "breakdown": {"name": "The Breakdown", "days": ["Wednesday"], "time": "07:30"},
            "drive": {"name": "The Long Drive", "days": ["Friday"], "time": "07:30"},
        },
        "publish": {"enabled": True, "review": True, "base_url": "https://example.test/feed"},
    }), encoding="utf-8")
    (podcast_home / "episodes" / "2026-11-01-ep001" / "episode.json").write_text(json.dumps({
        "number": 1, "slug": "2026-11-01-ep001", "series_name": "The Brief",
        "title": "Test episode one", "published": "2026-11-01T07:30:00+07:00",
        "file": "2026-11-01-ep001.mp3", "seconds": 420,
    }), encoding="utf-8")
    (podcast_home / "episodes" / "2026-11-03-ep002" / "episode.json").write_text(json.dumps({
        "number": 2, "slug": "2026-11-03-ep002", "series_name": "The Breakdown",
        "title": "Held episode two", "published": "2026-11-03T07:30:00+07:00",
        "file": "2026-11-03-ep002.mp3", "seconds": 300, "status": "pending",
    }), encoding="utf-8")
    (podcast_home / "site" / "audio" / "2026-11-01-ep001.mp3").write_bytes(b"audio")
    (podcast_home / "site" / "audio" / "2026-11-03-ep002.mp3").write_bytes(b"audio")
    (podcast_home / "last_run.json").write_text(json.dumps(
        {"ok": True, "title": "Held episode two", "held": True, "awaiting_review": 1, "finished": "2026-11-03 07:35"}),
        encoding="utf-8")

    real_podcast_home = hub.PODCAST
    hub.PODCAST = podcast_home
    try:
        section = hub.podcast_section()
    finally:
        hub.PODCAST = real_podcast_home
    check(section["installed"] and section["episodes_total"] == 1, "hub: podcast reader counts only published episodes on the site")
    check(section["latest"]["number"] == 1 and section["latest"]["seconds"] == 420, "hub: podcast latest episode (number + length)")
    check([item["number"] for item in section["held"]] == [2], "hub: held episode waits in the review queue")
    check(section["feed_url"] == "https://example.test/feed/feed.xml" and section["review"] and section["publish_enabled"],
          "hub: podcast feed link + review state")
    check(len(section["series"]) == 3 and all(item["next_run"] for item in section["series"]),
          f"hub: next run per series computed ({[item['next_run'] for item in section['series']]})")
    friday_after_run = hub._podcast_series_next_run({"days": ["Friday"], "time": "07:30"}, datetime(2026, 11, 13, 9, 0))
    friday_before_run = hub._podcast_series_next_run({"days": ["Friday"], "time": "07:30"}, datetime(2026, 11, 13, 6, 0))
    check(friday_after_run.startswith("Fri 20 Nov") and friday_before_run.startswith("Fri 13 Nov"),
          f"hub: on a series' own day after its time, the next run is next week ({friday_after_run!r}, {friday_before_run!r})")
    check(section["published_by_day"] == {"2026-11-01": 1}, "hub: podcast episodes per day")
    check(section["last_run"].get("held") is True and section["last_run"].get("awaiting_review") == 1,
          "hub: last run's held state reaches the tile")

    held_todo = hub.todo_items(None, {"installed": False}, {"installed": False}, {}, section)
    check([item["action"] for item in held_todo if item["module"] == "Podcast"] == ["podcast:approve:all"],
          "hub: a held episode becomes an urgent approve to-do")
    help_entries = {entry["module"]: entry for entry in hub.help_sections({}, section)}
    check("Podcast" in help_entries and "The Brief" in help_entries["Podcast"]["runs"], "hub: podcast help entry names the schedule")

    hub.PODCAST = podcast_home / "nowhere"
    try:
        missing = hub.podcast_section()
    finally:
        hub.PODCAST = real_podcast_home
    check(missing == {"installed": False}, "hub: missing podcast module shows as not set up")

    # hub: the Podcast tab reads the module's promotion.py (files only) and refreshes OP3 in the background when stale
    hub.PODCAST = podcast_home
    hub._cache.pop("podcast_promotion", None)
    try:
        check(hub.podcast_promotion_section() == {"installed": False}, "hub: no promotion.py means no Podcast tab")
    finally:
        hub.PODCAST = real_podcast_home
    (podcast_home / "promotion.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "STALE = Path(__file__).with_name('stale.flag')\n"
        "MARKS = Path(__file__).with_name('marks.json')\n"
        "def view():\n"
        "    return {'installed': True, 'stale': STALE.exists(), 'listed': 1, 'todo': ['Spotify'],\n"
        "            'feed_url': 'https://example.test/feed/feed.xml',\n"
        "            'directories': [{'id': 'apple', 'name': 'Apple Podcasts', 'url': 'https://a.test', 'status': 'listed', 'why': 'big'},\n"
        "                            {'id': 'spotify', 'name': 'Spotify', 'url': 'https://s.test', 'status': 'todo', 'why': 'second'}]}\n"
        "def set_mark(directory, done):\n"
        "    if directory not in ('apple', 'spotify'):\n"
        "        raise ValueError('Unknown directory: ' + directory)\n"
        "    marks = json.loads(MARKS.read_text()) if MARKS.exists() else {}\n"
        "    marks[directory] = done\n"
        "    MARKS.write_text(json.dumps(marks))\n"
        "    return 'marked' if done else 'unmarked'\n",
        encoding="utf-8")
    started = []

    class FakeRefresh:
        def __init__(self, command, **kwargs):
            started.append(command)

        def poll(self):
            return None  # still running

    real_popen = hub.subprocess.Popen
    hub.PODCAST = podcast_home
    hub.subprocess.Popen = FakeRefresh
    hub._promotion_refresh = None
    hub._cache.pop("podcast_promotion", None)
    try:
        promo = hub.podcast_promotion_section()
        check(promo["installed"] and promo["listed"] == 1 and promo["refreshing"] is False and not started,
              "hub: Podcast tab passes promotion.view() through, no refresh while the numbers are fresh")
        promo_todo = hub.podcast_promotion_todo(promo)
        check(len(promo_todo) == 1 and promo_todo[0]["priority"] == 3 and promo_todo[0]["module"] == "Podcast"
              and [entry["url"] for entry in promo_todo[0]["links"]] == ["https://s.test"],
              "hub: directories still to do become one low-priority to-do with their links")
        check(hub.podcast_promotion_todo({"installed": True, "todo": []}) == [], "hub: no listing to-do once every directory is done")

        check(hub.run_action("podcast:listed:spotify") == "marked"
              and json.loads((podcast_home / "marks.json").read_text()) == {"spotify": True},
              "hub: Mark as submitted goes to promotion.set_mark")
        check(hub.run_action("podcast:unlisted:spotify") == "unmarked", "hub: Undo clears the mark")
        try:
            hub.run_action("podcast:listed:nowhere")
            check(False, "hub: unknown directory is refused")
        except ValueError:
            check(True, "hub: unknown directory is refused")

        (podcast_home / "stale.flag").write_text("", encoding="utf-8")
        hub._cache.pop("podcast_promotion", None)
        promo = hub.podcast_promotion_section()
        hub._cache.pop("podcast_promotion", None)
        hub.podcast_promotion_section()
        check(promo["refreshing"] is True and len(started) == 1 and started[0][-1] == "refresh",
              f"hub: stale numbers start one background refresh, never two ({started})")
        check("already running" in hub.run_action("podcast:promo-refresh") and len(started) == 1,
              "hub: Check now waits for a refresh that is already running")
        hub._promotion_refresh = None
        hub.run_action("podcast:promo-refresh")
        check(started[-1][-2:] == ["refresh", "--force"], "hub: Check now forces Apple too")
    finally:
        hub.PODCAST = real_podcast_home
        hub.subprocess.Popen = real_popen
        hub._promotion_refresh = None
        hub._cache.pop("podcast_promotion", None)
finally:
    import shutil
    shutil.rmtree(podcast_home, ignore_errors=True)

check(channels.bluesky_sign_in("@me.bsky.social", "abcd-efgh-ijkl-mnop")
      == {"handle": "me.bsky.social", "app_password": "abcd-efgh-ijkl-mnop"}, "channels: an existing Bluesky app password is kept as is")


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


bluesky_calls = []


def fake_post(url, json=None, headers=None, timeout=None):
    bluesky_calls.append((url.rsplit("/", 1)[-1], json, headers))
    if url.endswith("createSession") and "authFactorToken" not in json:
        return FakeResponse(401, {"error": "AuthFactorTokenRequired"})
    if url.endswith("createSession"):
        return FakeResponse(200, {"handle": "me.bsky.social", "accessJwt": "main-jwt", "did": "did:plc:x"})
    return FakeResponse(200, {"name": json["name"], "password": "wxyz-wxyz-wxyz-wxyz"})


real_post = channels.requests.post
channels.requests.post = fake_post
try:
    made = channels.bluesky_sign_in("me.bsky.social", "my normal password", ask_code=lambda prompt: "CODE1")
finally:
    channels.requests.post = real_post
check(made == {"handle": "me.bsky.social", "app_password": "wxyz-wxyz-wxyz-wxyz"}
      and bluesky_calls[1][1]["authFactorToken"] == "CODE1"
      and bluesky_calls[2][0] == "com.atproto.server.createAppPassword"
      and bluesky_calls[2][2]["Authorization"] == "Bearer main-jwt"
      and "my normal password" not in json.dumps(made),
      "channels: Bluesky password -> app password (e-mail code handled, main password not stored)")

import growth

check([analytics.platform_of(name) for name in ("linkedin.com", "lnkd.in", "mastodon.social", "bsky.app", "dev.to",
                                                  "t.co", "x.com", "netflix.com", "google.com", "github.com", "example.org",
                                                  "l.threads.com", "threads.net", "substack.com", "juwen.substack.com",
                                                  "t.me", "web.telegram.org", "discord.com", "com.discord")]
      == ["LinkedIn", "LinkedIn", "Mastodon", "Bluesky", "Dev.to", "X", "X", "Other", "Search", "GitHub", "Other",
          "Threads", "Threads", "Substack", "Substack", "Telegram", "Telegram", "Discord", "Other"],
      "growth: GitHub referrers mapped to platforms")
check(set(channels.AUTO) <= set(dashboard.PLATFORM_ACCOUNTS)
      and all(dashboard.PLATFORM_ACCOUNTS[key] == channels.LABELS[key] for key in channels.AUTO),
      "dashboard: every platform ReachPilot posts to has a row (posts, followers, GitHub visitors)")
people = [{"id": str(number), "bot": number == 3} for number in range(40)]
targets = growth.follow_back_targets(people, {"0", "1"}, cap=25)
check(len(targets) == 25 and "0" not in targets and "3" not in targets, "growth: follow-back skips followed + bots, capped at 25")
base_profile = {"bio_short": "a", "links": {"GitHub": "https://github.com/x"}}
check(growth.profile_hash(base_profile) == growth.profile_hash(dict(base_profile))
      and growth.profile_hash(base_profile) != growth.profile_hash({**base_profile, "bio_short": "b"}),
      "growth: profile pushed again only when config changes")
real_profile = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8")).get("profile", {})
check(len(real_profile.get("bio_short", "")) <= 256 and len(real_profile.get("bio_long", "")) <= 500
      and "github.com/juw12" in real_profile.get("bio_short", ""), "growth: bios fit Bluesky 256 / Mastodon 500 and link GitHub")

check(growth.missing_socials(["https://dev.to/x", "https://bsky.app/profile/x/"], ["https://DEV.to/x/"])
      == ["https://bsky.app/profile/x/"], "growth: only missing GitHub profile links are added")
growth_db = sqlite3.connect(":memory:")
growth_db.executescript(channels.SCHEMA)
quiet_growth = logging.getLogger("selftest-growth")
quiet_growth.addHandler(logging.NullHandler())
quiet_growth.propagate = False
growth_summary = growth.run_daily(growth_db, {"github_user": "x", "profile": {"bio_short": "b"}}, {},
                                  lambda path: {"followers": 7} if path == "users/x" else None, quiet_growth)
growth.record(growth_db, "mastodon", "Profile updated", "bio")
check(growth_summary["followers"] == {"github": 7}
      and growth_db.execute("select followers from audience where channel='github'").fetchone()[0] == 7
      and growth_db.execute("select count(*) from growth_log").fetchone()[0] == 1,
      "growth: daily pass records follower counts and the action log")

# Dashboard page script: syntax-checked with Node when it is installed (skipped otherwise)
import re
import shutil
import subprocess
node = shutil.which("node") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "nodejs" / "node.exe")
if Path(node).exists():
    page_html = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    page_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page_html, flags=re.S)
    with tempfile.TemporaryDirectory() as script_dir:
        script_errors = []
        for index, body in enumerate(page_scripts):
            script_file = Path(script_dir) / f"page_{index}.js"
            script_file.write_text(body, encoding="utf-8")
            result = subprocess.run([node, "--check", str(script_file)], capture_output=True, text=True)
            if result.returncode:
                script_errors.append(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "syntax error")
    check(page_scripts and not script_errors, f"dashboard: page script parses in Node {script_errors[:1]}")
else:
    print("Skipping dashboard script check (Node not installed)")

# Dashboard shell: every page in the sidebar has its section, and the fonts the page asks for are bundled and served
shell_root = Path(__file__).parent
shell_page = (shell_root / "dashboard.html").read_text(encoding="utf-8")
shell_tabs = re.findall(r'<button role="tab" data-tab="([a-z]+)"', shell_page)
shell_sections = re.findall(r'<section id="([a-z]+)"', shell_page)
check(shell_tabs and len(set(shell_tabs)) == len(shell_tabs) and sorted(shell_tabs) == sorted(shell_sections),
      f"dashboard: every sidebar page has one section and the other way round "
      f"{sorted(set(shell_tabs) ^ set(shell_sections))}")
shell_fonts = re.findall(r'url\("(/fonts/[^"]+)"\)', shell_page)
check(len(shell_fonts) == 3
      and all(path in dashboard.FONT_FILES and (dashboard.FONTS / dashboard.FONT_FILES[path]).is_file()
              for path in shell_fonts),
      f"dashboard: the three fonts in the page are in fonts/ and on the served list {shell_fonts}")
check((dashboard.FONTS / "LICENSE.md").is_file(), "dashboard: the bundled fonts ship with their licence")
check(not re.search(r"fonts\.googleapis|fonts\.gstatic|@import\s+url|<link[^>]+stylesheet", shell_page),
      "dashboard: no font or stylesheet is loaded from another host")
shell_style = shell_page.split("<style>", 1)[1].split("</style>", 1)[0]
shell_tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", shell_style))


def shell_contrast(front: str, back: str) -> float:
    def light(colour: str) -> float:
        parts = [int(colour[index:index + 2], 16) / 255 for index in (1, 3, 5)]
        red, green, blue = [part / 12.92 if part <= 0.03928 else ((part + 0.055) / 1.055) ** 2.4 for part in parts]
        return 0.2126 * red + 0.7152 * green + 0.0722 * blue
    high, low = sorted((light(front), light(back)), reverse=True)
    return (high + 0.05) / (low + 0.05)


shell_pairs = [("ink", "bg"), ("ink-2", "bg"), ("ink-2", "sunken"), ("muted", "bg"), ("muted", "surface"),
               ("muted", "sunken"), ("blue", "bg"), ("blue", "surface"), ("blue", "blue-soft"),
               ("green", "green-soft"), ("amber", "amber-soft"), ("red", "red-soft"), ("red", "bg")]
shell_low = [f"{front} on {back} {shell_contrast(shell_tokens[front], shell_tokens[back]):.2f}"
             for front, back in shell_pairs if shell_contrast(shell_tokens[front], shell_tokens[back]) < 4.5]
check(not shell_low and shell_contrast("#ffffff", shell_tokens["blue"]) >= 4.5,
      f"dashboard: every text colour reaches 4.5:1 on the fills it is used on {shell_low}")
shell_scrim = re.search(r"\.scrim \{[^}]*z-index:\s*(\d+)", shell_style)
shell_drawer = re.search(r"\.side\.open \{[^}]*z-index:\s*(\d+)", shell_style)
check(shell_scrim and shell_drawer and int(shell_drawer.group(1)) > int(shell_scrim.group(1)),
      "dashboard: the open phone drawer sits above its scrim, so its pages can be tapped")
shell_demo = (shell_root / "docs" / "index.html").read_text(encoding="utf-8")
check('url("fonts/' in shell_demo and 'url("/fonts/' not in shell_demo
      and all((shell_root / "docs" / "fonts" / Path(path).name).is_file() for path in shell_fonts),
      "demo: the public page loads the fonts from its own folder")

# Self-updater: a failed direct fetch is retried through the fallback proxy; a timeout does not crash it
import subprocess as _subprocess
import selfupdate
fetch_calls = []


def fake_git(*args, cwd=None, timeout=None):
    fetch_calls.append(args)
    if "http.proxy=http://localhost:3128" in args:
        return _subprocess.CompletedProcess(args, 0, "", "")
    raise _subprocess.TimeoutExpired("git fetch", timeout)


real_git = selfupdate.git
selfupdate.git = fake_git
try:
    proxy_fetch_ok = selfupdate.fetch_main(Path("."))
finally:
    selfupdate.git = real_git
check(proxy_fetch_ok and len(fetch_calls) == 2 and fetch_calls[0][0] == "fetch",
      "selfupdate: direct fetch timeout falls back to the local proxy")

# Self-updater: packages a new version needs are installed once per requirements.txt; a failure waits a day
import tempfile as _tempfile

with _tempfile.TemporaryDirectory() as package_folder:
    package_folder = Path(package_folder)
    (package_folder / "requirements.txt").write_text("requests>=2.31\nnumpy>=1.26\n", encoding="utf-8")
    pip_calls, pip_answers = [], [False, False, False, True]

    def fake_pip(*args, cwd=None):
        pip_calls.append(args)
        return pip_answers.pop(0)

    real_pip = selfupdate.run_pip
    selfupdate.run_pip = fake_pip
    try:
        first_try = selfupdate.install_new_packages(package_folder, today="2026-11-17")
        same_day = selfupdate.install_new_packages(package_folder, today="2026-11-17")
        calls_after_failure = len(pip_calls)
        next_day = selfupdate.install_new_packages(package_folder, today="2026-11-18")
        settled = selfupdate.install_new_packages(package_folder, today="2026-11-19")
        calls_when_settled = len(pip_calls)
        (package_folder / "requirements.txt").write_text("requests>=2.31\nnumpy>=1.26\npypdf>=4.0\n", encoding="utf-8")
        pip_answers.append(True)
        changed_file = selfupdate.install_new_packages(package_folder, today="2026-11-19")
    finally:
        selfupdate.run_pip = real_pip
    check("failed" in first_try and same_day == "" and calls_after_failure == 2 and pip_calls[1][:2] == ("--proxy", "http://localhost:3128"),
          "selfupdate: a failed package install tries the proxy too, then waits for the next day")
    check("installed" in next_day and settled == "" and calls_when_settled == 4,
          "selfupdate: packages are installed once and not again while requirements.txt is unchanged")
    check("installed" in changed_file and len(pip_calls) == 5 and pip_calls[4] == ("-r", "requirements.txt"),
          "selfupdate: a changed requirements.txt is installed again")
check(selfupdate.install_new_packages(Path("no-such-folder-for-selftest")) == "",
      "selfupdate: no requirements.txt means nothing to install")

# LinkedIn: when the comment API is refused, the links go into the post in place of the pointer line
from linkedin_api import inline_links

intro = "Start building.\n\nPortfolio and GitHub in the first comment.\n\n\\#OpenSource"
inlined = inline_links(intro, "GitHub: https://github.com/juw12")
check("first comment" not in inlined and "GitHub: https://github.com/juw12" in inlined
      and inlined.endswith("\\#OpenSource"), "linkedin: pointer line swapped for the links, hashtags kept")
no_pointer = inline_links("Just a post.", "GitHub: https://github.com/juw12")
check(no_pointer == "Just a post.\n\nGitHub: https://github.com/juw12", "linkedin: links appended when there is no pointer line")

# Demo video: recorded for a single-project post, posted natively where the platform takes one, screenshots as the fallback
import contextlib  # noqa: E402
import copy  # noqa: E402

import creators  # noqa: E402
import linkedin_api  # noqa: E402
import showcase  # noqa: E402
import visuals  # noqa: E402


class VideoResponse:
    def __init__(self, payload=None, status=200, headers=None):
        self.payload = payload if payload is not None else {}
        self.status_code = status
        self.ok = status < 300
        self.text = json.dumps(self.payload)
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def json(self):
        return self.payload


@contextlib.contextmanager
def fake_http(routes):
    """Stand-ins for requests.get/post/put/head: the first (method, url part) route that matches answers.

    A list answers one item per call. Yields the calls made as (method, url, kwargs). No waiting, no network.
    """
    calls = []

    def respond(method):
        def handler(url, **kwargs):
            calls.append((method, url, kwargs))
            for route_method, url_part, reply in routes:
                if route_method == method and url_part in url:
                    return reply.pop(0) if isinstance(reply, list) else reply
            return VideoResponse({"error": f"unexpected {method} {url}"}, 500)
        return handler

    with contextlib.ExitStack() as stack:
        for method in ("get", "post", "put", "head"):
            stack.enter_context(mock.patch(f"channels.requests.{method}", respond(method)))
        stack.enter_context(mock.patch("time.sleep"))
        yield calls


def urls_called(calls, url_part):
    return [url for _, url, _ in calls if url_part in url]


demo_link = "https://juw12.github.io/demo/watt-reserve.mp4"
split_video, split_shots = channels.split_media(["out/watt-reserve_demo.mp4", "out/home.png", "out/list.png"])
check(split_video == Path("out/watt-reserve_demo.mp4") and split_shots == [Path("out/home.png"), Path("out/list.png")]
      and channels.split_media(["out/home.png"]) == (None, [Path("out/home.png")]),
      "demo video: a post's media splits into the video and the screenshots")
check(channels.DEMO_VIDEO_SIZE == visuals.DEMO_SIZE, "demo video: channels and visuals agree on the frame size")
check("demo" in trylinks.RESERVED_NAMES, "demo video: no project go-page can take the demo/ folder")
check(visuals._demo_speed(12) == 1.0 and visuals._demo_speed(2 * visuals.DEMO_MAX_SECONDS) == 2.0,
      "demo video: a tour that ran long on a busy PC is sped up to fit the limit, a short one is left alone")

github_only = copy.copy(facts)
github_only.homepage = "https://github.com/juw12/watt-reserve"
no_site = copy.copy(facts)
no_site.homepage = ""
# Every post gets something to record: the project's own site, else its landing page, else a page built from the repo
def never_asked(url):
    raise AssertionError(f"{url} was looked up although the project has its own site")


landing_page = "https://juw12.github.io/p/watt-reserve/"
built_page = "https://juw12.github.io/reachpilot-showcase/watt-reserve/"
roundup_page = "https://juw12.github.io/reachpilot-showcase/roundup/"
check(showcase.target("juw12", "launch", [facts], is_live=never_asked) == (facts.homepage, {}),
      "showcase: a project with its own site is recorded there")
check(showcase.target("juw12", "launch", [github_only], is_live=lambda url: url == landing_page) == (landing_page, {})
      and showcase.target("juw12", "launch", [no_site], is_live=lambda url: url == landing_page) == (landing_page, {}),
      "showcase: a project without a site is recorded on its landing page when that answers")
built_url, built_served = showcase.target("juw12", "launch", [no_site], is_live=lambda url: False)
check(built_url == built_page and list(built_served) == [built_page] and "Watt Reserve" in built_served[built_page],
      f"showcase: with no site and no landing page a page is built from the repo ({built_url})")
several_url, several_served = showcase.target("juw12", "launch", [facts, github_only], is_live=never_asked)
one_roundup_url, _ = showcase.target("juw12", "roundup", [facts], is_live=never_asked)
check(several_url == one_roundup_url == roundup_page and "2 projects by juw12" in several_served[roundup_page],
      "showcase: a roundup or several projects get one built page with a card each")
check(showcase.target("juw12", "launch", []) == ("", {}), "showcase: nothing to show, nothing to record")

tricky = copy.copy(no_site)
tricky.title = "Watt <Reserve> & Co"
tricky.readme_images = ["docs/shot one.png", "https://example.test/second.png"]
plain_page = showcase.page([tricky], "juw12")
check("Watt &lt;Reserve&gt; &amp; Co" in plain_page and "<Reserve>" not in plain_page
      and "https://raw.githubusercontent.com/juw12/watt-reserve/HEAD/docs/shot%20one.png" in plain_page
      and "git clone https://github.com/juw12/watt-reserve" in plain_page
      and "<video" not in plain_page and "Covered by" not in plain_page and "og:video" not in plain_page,
      "showcase: the built page escapes the repo's words, reads README pictures from the repo, and has no video of its own")
check(all(bullet in plain_page for bullet in facts.bullets) and "MIT licence" in plain_page,
      "showcase: the built page says what the README says")
page_buttons = re.findall(r"<summary>([^<]*)</summary>", plain_page + showcase.page([facts, github_only], "juw12"))
check(len(page_buttons) >= 3 and not any(visuals.DEMO_SKIP_LABEL.search(label) for label in page_buttons)
      and visuals.DEMO_SKIP_LABEL.search("View the code on GitHub") and visuals.DEMO_SKIP_LABEL.search("See them all on GitHub"),
      f"showcase: the tour can open every section of a built page and never presses the link that leaves it ({page_buttons})")
watch_credits = [{"creator": "Dev <Tube>", "url": "https://youtu.be/abc"}]
watch_page = showcase.page([facts], "juw12", demo_link, watch_credits)
check(f'<video src="{demo_link}{showcase.PREVIEW_FRAME}"' in watch_page and f'og:video" content="{demo_link}"' in watch_page
      and '<a href="https://youtu.be/abc">Dev &lt;Tube&gt;</a>' in watch_page and "Covered by" in watch_page,
      "showcase: the watch page plays the video (showing a frame before play) and credits the creators who covered the project")

with tempfile.TemporaryDirectory() as record_dir:
    def fake_raw(url, folder, phone=False, served=None):
        raise RuntimeError(f"stop after the browser step: {sorted(served or {})}")

    with mock.patch("visuals.record_raw", side_effect=fake_raw) as raw_recorder:
        try:
            visuals.record_demo(built_url, Path(record_dir) / "clip.mp4", built_served)
        except RuntimeError as stopped:
            handed_over = str(stopped)
    check(raw_recorder.call_args.kwargs["served"] == built_served and built_page in handed_over,
          "demo video: a built page is handed to the recording browser instead of being published")

import io  # noqa: E402
from PIL import ImageDraw as PILDraw  # noqa: E402

empty_frame = PILImage.new("RGB", (900, 1240), "#ffffff")
pointer_frame = empty_frame.copy()
PILDraw.Draw(pointer_frame).polygon([(400, 600), (400, 640), (428, 628)], fill="#111111")
page_frame = empty_frame.copy()
page_pen = PILDraw.Draw(page_frame)
page_pen.rectangle((40, 60, 860, 200), fill="#2563eb")
for text_row in range(260, 1100, 44):
    page_pen.rectangle((60, text_row, 700, text_row + 10), fill="#334155")
check(visuals.is_blank(empty_frame) and visuals.is_blank(pointer_frame) and not visuals.is_blank(page_frame)
      and visuals.is_blank(PILImage.new("RGB", (900, 1240), "#0f172a")),
      "demo video: a frame of one flat colour (with or without the pointer) is blank, a drawn page is not")


def frames_from(*pictures):
    """A stand-in for ffmpeg that hands recording_is_blank these frames, one per look."""
    shots = []
    for picture in pictures:
        packed = io.BytesIO()
        picture.save(packed, "PNG")
        shots.append(mock.Mock(stdout=packed.getvalue()))
    return shots


with mock.patch("visuals._ffmpeg", return_value="ffmpeg"):
    with mock.patch("visuals.subprocess.run", side_effect=frames_from(empty_frame, pointer_frame, empty_frame)) as looks:
        never_showed = visuals.recording_is_blank(Path("raw.webm"), 4.0, 20.0)
    looked_at = [call.args[0][call.args[0].index("-ss") + 1] for call in looks.call_args_list]
    with mock.patch("visuals.subprocess.run", side_effect=frames_from(empty_frame, page_frame)):
        showed_later = visuals.recording_is_blank(Path("raw.webm"), 4.0, 20.0)
    with mock.patch("visuals.subprocess.run", return_value=mock.Mock(stdout=b"")):
        unreadable = visuals.recording_is_blank(Path("raw.webm"), 4.0, 20.0)
check(never_showed and unreadable and not showed_later and looked_at == ["7.00", "14.00", "21.00"],
      f"demo video: a recording whose page never showed is refused, one that shows the page is kept (looked at {looked_at})")

page_handler = visuals._serve_page(built_served[built_page])
asked_route = mock.Mock()
page_handler(asked_route)
# Playwright hands a two-parameter handler (route, request); a body kept as a default argument is then lost.
check(page_handler.__code__.co_argcount == 1 and asked_route.fulfill.call_args.kwargs["body"] == built_served[built_page],
      "demo video: the recording browser answers with the built page itself")

video_variants = channels.build_variants(post_body, post_comment, "launch", facts, video_url=demo_link)
check(all(video_variants[name]["extra"].get("video_url") == demo_link for name in channels.BUFFER_VIDEO)
      and not any("video_url" in variant["extra"] for name, variant in video_variants.items()
                  if name not in channels.BUFFER_VIDEO)
      and video_variants["x"]["extra"]["image_url"] and "video_url" not in variants["x"]["extra"],
      "demo video: only X and Threads carry the hosted video link, and they keep the image as fallback")

need_db = sqlite3.connect(":memory:")
check(channels.needs_video_url(need_db, free_config, free_secrets, "2026-12-01 08:00")
      and not channels.needs_video_url(free_db, free_config, free_secrets, "2026-11-11 08:00")
      and not channels.needs_video_url(need_db, {"channels": channels.ALL}, {"channels": {"bluesky": {"handle": "a"}}},
                                       "2026-12-01 08:00")
      and not channels.needs_video_url(need_db, {"channels": ["x"]}, {"channels": {"x": {"client_id": "c"}}},
                                       "2026-12-01 08:00"),
      "demo video: hosted only when X or Threads through Buffer is still to be queued")
channels.prepare(need_db, free_config, free_secrets, "2026-12-01 08:00", "watt-reserve", "launch", post_body, post_comment,
                 [], facts, demo_link)
stored_extra = json.loads(need_db.execute("SELECT extra FROM channel_posts WHERE channel='x'").fetchone()[0])
check(stored_extra.get("video_url") == demo_link, "demo video: the hosted link is stored with the queued X post")

with tempfile.TemporaryDirectory() as temp_dir:
    clip = Path(temp_dir) / "watt-reserve_demo.mp4"
    clip.write_bytes(b"0123456789")
    shot = Path(temp_dir) / "home.png"
    PILImage.new("RGB", (640, 360), "navy").save(shot)
    logging.disable(logging.WARNING)  # the fallbacks below warn on purpose
    try:
        # LinkedIn: byte-range upload, finalize with the part ids, wait until available, post with the video
        linkedin = linkedin_api.LinkedInClient({"person_urn": "urn:li:person:me", "access_token": "t"}, "202511")
        with fake_http([
            ("post", "action=initializeUpload", VideoResponse({"value": {
                "video": "urn:li:video:V1", "uploadToken": "tok",
                "uploadInstructions": [
                    {"uploadUrl": "https://upload.test/part1", "firstByte": 0, "lastByte": 3},
                    {"uploadUrl": "https://upload.test/part2", "firstByte": 4, "lastByte": 9},
                ]}})),
            ("put", "part1", VideoResponse(headers={"ETag": '"e1"'})),
            ("put", "part2", VideoResponse(headers={"ETag": '"e2"'})),
            ("post", "action=finalizeUpload", VideoResponse()),
            ("get", "/rest/videos/", [VideoResponse({"status": "PROCESSING"}), VideoResponse({"status": "AVAILABLE"})]),
            ("post", "/rest/posts", VideoResponse(status=201, headers={"x-restli-id": "urn:li:share:99"})),
        ]) as calls:
            video_urn = linkedin.upload_video(clip)
            share_urn = linkedin.create_post("Hello", [("urn:li:image:I1", "alt")], video=(video_urn, "watt-reserve demo"))
        parts = [kwargs["data"] for method, _, kwargs in calls if method == "put"]
        finalize = next(kwargs["json"] for _, url, kwargs in calls if "finalizeUpload" in url)["finalizeUploadRequest"]
        posted = next(kwargs["json"] for _, url, kwargs in calls if url.endswith("/rest/posts"))
        check(video_urn == "urn:li:video:V1" and parts == [b"0123", b"456789"]
              and finalize == {"video": "urn:li:video:V1", "uploadToken": "tok", "uploadedPartIds": ["e1", "e2"]}
              and len(urls_called(calls, "/rest/videos/")) == 2,
              "linkedin: video uploaded in the byte ranges asked for, finalized, and waited for")
        check(share_urn == "urn:li:share:99"
              and posted["content"] == {"media": {"id": "urn:li:video:V1", "title": "watt-reserve demo"}},
              "linkedin: the post carries the video instead of the images")
        with fake_http([
            ("post", "action=initializeUpload", VideoResponse({"value": {
                "video": "urn:li:video:V2", "uploadInstructions": [
                    {"uploadUrl": "https://upload.test/part1", "firstByte": 0, "lastByte": 9}]}})),
            ("put", "part1", VideoResponse(headers={"ETag": "e1"})),
            ("post", "action=finalizeUpload", VideoResponse()),
            ("get", "/rest/videos/", VideoResponse({"status": "PROCESSING_FAILED"})),
        ]):
            try:
                linkedin.upload_video(clip)
                failed_processing = False
            except RuntimeError:
                failed_processing = True
        check(failed_processing, "linkedin: a video LinkedIn cannot process is reported, not posted")

        # autopilot.publish: a refused video post falls back to the screenshots, so the slot still posts
        linkedin_posts = []

        class FakeLinkedIn:
            refuse_video = True

            def __init__(self, token, api_version):
                pass

            def upload_video(self, path):
                return "urn:li:video:V1"

            def upload_image(self, path):
                return "urn:li:image:I1"

            def create_post(self, text, images, video=None):
                linkedin_posts.append((len(images), video))
                if video and self.refuse_video:
                    raise RuntimeError("post failed: HTTP 422")
                return "urn:li:share:1"

            @staticmethod
            def post_url(urn):
                return "https://linkedin.test/" + urn

        publish_db = sqlite3.connect(":memory:")
        publish_db.execute("CREATE TABLE posts(slot TEXT, status TEXT, urn TEXT, url TEXT, posted_at TEXT, error TEXT)")
        publish_db.execute("INSERT INTO posts(slot, status) VALUES('2026-12-01 08:00', 'draft')")
        publish_row = ("auto", "watt-reserve", "launch", "Hello", "", json.dumps([str(clip), str(shot)]))
        publish_config = {"linkedin_api_version": "202511", "timezone_offset_hours": 7}
        with mock.patch("linkedin_api.LinkedInClient", FakeLinkedIn), mock.patch("autopilot.notify"), \
             mock.patch("autopilot.track_links", side_effect=lambda config, db, text: text):
            autopilot.publish(publish_config, publish_db, "2026-12-01 08:00", publish_row, {})
            refused_then_images = list(linkedin_posts)
            linkedin_posts.clear()
            FakeLinkedIn.refuse_video = False
            autopilot.publish(publish_config, publish_db, "2026-12-01 08:00", publish_row, {})
        check(refused_then_images == [(0, ("urn:li:video:V1", "watt-reserve demo")), (1, None)]
              and publish_db.execute("SELECT status FROM posts").fetchone()[0] == "posted",
              f"linkedin: a refused video post goes out with the screenshots instead ({refused_then_images})")
        check(linkedin_posts == [(0, ("urn:li:video:V1", "watt-reserve demo"))],
              f"linkedin: an accepted video post is posted once, without images ({linkedin_posts})")

        # Bluesky: service token for the user's own server, upload, wait for the job, embed the processed blob
        bluesky_session = VideoResponse({
            "accessJwt": "jwt", "did": "did:plc:me", "handle": "juw12.bsky.social",
            "didDoc": {"service": [{"serviceEndpoint": "https://pds.example.test"}]},
        })
        video_blob = {"$type": "blob", "ref": {"$link": "bafyvideo"}, "mimeType": "video/mp4", "size": 10}
        bluesky = channels.Bluesky({"handle": "juw12.bsky.social", "app_password": "p"})
        with fake_http([
            ("post", "createSession", bluesky_session),
            ("get", "getServiceAuth", VideoResponse({"token": "service-token"})),
            ("post", "uploadVideo", VideoResponse({"jobId": "job1", "state": "JOB_STATE_CREATED"})),
            ("get", "getJobStatus", [
                VideoResponse({"jobStatus": {"jobId": "job1", "state": "JOB_STATE_ENCODING"}}),
                VideoResponse({"jobStatus": {"jobId": "job1", "state": "JOB_STATE_COMPLETED", "blob": video_blob}}),
            ]),
            ("post", "uploadBlob", VideoResponse({"blob": {"ref": "image"}})),
            ("post", "createRecord", VideoResponse({"uri": "at://did:plc:me/app.bsky.feed.post/abc"})),
        ]) as calls:
            bluesky_url = bluesky.publish(text="Hello", title="", images=[shot], link=facts.homepage, extra={},
                                          idempotency_key="k", video=clip)
        auth = next(kwargs for _, url, kwargs in calls if "getServiceAuth" in url)
        upload = next(kwargs for _, url, kwargs in calls if "uploadVideo" in url)
        embed = next(kwargs for _, url, kwargs in calls if "createRecord" in url)["json"]["record"]["embed"]
        check(bluesky_url.endswith("/post/abc") and auth["params"]["aud"] == "did:web:pds.example.test"
              and upload["headers"]["Authorization"] == "Bearer service-token" and upload["data"] == b"0123456789"
              and embed["$type"] == "app.bsky.embed.video" and embed["video"] == video_blob
              and embed["aspectRatio"] == {"width": 1280, "height": 720} and not urls_called(calls, "uploadBlob"),
              "bluesky: the processed video is embedded in place of the screenshots")
        with fake_http([
            ("post", "createSession", bluesky_session),
            ("get", "getServiceAuth", VideoResponse({"token": "service-token"})),
            ("post", "uploadVideo", VideoResponse({"error": "upload limit reached"}, 400)),
            ("post", "uploadBlob", VideoResponse({"blob": {"ref": "image"}})),
            ("post", "createRecord", VideoResponse({"uri": "at://did:plc:me/app.bsky.feed.post/def"})),
        ]) as calls:
            bluesky.publish(text="Hello", title="", images=[shot], link="", extra={}, idempotency_key="k", video=clip)
        embed = next(kwargs for _, url, kwargs in calls if "createRecord" in url)["json"]["record"]["embed"]
        check(embed["$type"] == "app.bsky.embed.images" and len(embed["images"]) == 1,
              "bluesky: a refused video falls back to the screenshots")

        # Mastodon: the upload answers 202, the media check 206 until it is ready
        mastodon = channels.Mastodon({"instance": "mastodon.test", "access_token": "t"})
        with fake_http([
            ("post", "/api/v2/media", VideoResponse({"id": "m9"}, 202)),
            ("get", "/api/v1/media/m9", [VideoResponse({"id": "m9"}, 206), VideoResponse({"id": "m9"})]),
            ("post", "/api/v1/statuses", VideoResponse({"url": "https://mastodon.test/@me/1"})),
        ]) as calls:
            mastodon_url = mastodon.publish(text="Hello", title="", images=[shot], link="", extra={}, idempotency_key="k",
                                            video=clip)
        status = next(kwargs for _, url, kwargs in calls if "/api/v1/statuses" in url)
        check(mastodon_url == "https://mastodon.test/@me/1" and status["data"]["media_ids[]"] == ["m9"]
              and len(urls_called(calls, "/api/v1/media/m9")) == 2 and len(urls_called(calls, "/api/v2/media")) == 1,
              "mastodon: waits until the video is processed, then attaches it alone")
        with fake_http([
            ("post", "/api/v2/media", [VideoResponse({"error": "too large"}, 422), VideoResponse({"id": "img1"})]),
            ("post", "/api/v1/statuses", VideoResponse({"url": "https://mastodon.test/@me/2"})),
        ]) as calls:
            mastodon.publish(text="Hello", title="", images=[shot], link="", extra={}, idempotency_key="k", video=clip)
        status = next(kwargs for _, url, kwargs in calls if "/api/v1/statuses" in url)
        check(status["data"]["media_ids[]"] == ["img1"], "mastodon: a refused video falls back to the screenshots")

        # Telegram: sendVideo, or the screenshot when the bot refuses the file
        telegram = channels.Telegram({"bot_token": "b", "chat_id": "@juwenbuilds"})
        with fake_http([("post", "/sendVideo", VideoResponse({"ok": True, "result": {"message_id": 7}}))]) as calls:
            telegram_url = telegram.publish(text="Hello", title="", images=[shot], link="", extra={}, idempotency_key="k",
                                            video=clip)
        check(telegram_url == "https://t.me/juwenbuilds/7" and len(calls) == 1
              and calls[0][2]["files"]["video"][0] == "watt-reserve_demo.mp4" and calls[0][2]["data"]["caption"] == "Hello",
              "telegram: the video is sent with the post as its caption")
        with fake_http([
            ("post", "/sendVideo", VideoResponse({"ok": False, "description": "file is too big"}, 413)),
            ("post", "/sendPhoto", VideoResponse({"ok": True, "result": {"message_id": 8}})),
        ]) as calls:
            telegram_url = telegram.publish(text="Hello", title="", images=[shot], link="", extra={}, idempotency_key="k",
                                            video=clip)
        check(telegram_url == "https://t.me/juwenbuilds/8" and [url.rsplit("/", 1)[-1] for _, url, _ in calls]
              == ["sendVideo", "sendPhoto"], "telegram: a refused video falls back to the screenshot")

        # Discord: the video when it fits the webhook limit and is accepted, otherwise the screenshot
        discord = channels.Discord({"webhook_url": "https://discord.test/api/webhooks/1/abc"})
        discord_message = {"id": "30", "channel_id": "20", "guild_id": "10"}
        with fake_http([("post", "discord.test", VideoResponse(discord_message))]) as calls:
            discord.publish(text="Hello", title="", images=[shot], link="", extra={}, idempotency_key="k", video=clip)
        check(len(calls) == 1 and calls[0][2]["files"]["files[0]"][0] == "watt-reserve_demo.mp4",
              "discord: the video is attached to the post")
        with fake_http([("post", "discord.test", [VideoResponse({"message": "too large"}, 413),
                                                  VideoResponse(discord_message)])]) as calls:
            discord.publish(text="Hello", title="", images=[shot], link="", extra={}, idempotency_key="k", video=clip)
        check([kwargs["files"]["files[0]"][0] for _, _, kwargs in calls] == ["watt-reserve_demo.mp4", "home.jpg"],
              "discord: a refused video falls back to the screenshot")
        with fake_http([("post", "discord.test", VideoResponse(discord_message))]) as calls, \
             mock.patch.object(channels, "DISCORD_MAX_VIDEO_BYTES", 5):
            discord.publish(text="Hello", title="", images=[shot], link="", extra={}, idempotency_key="k", video=clip)
        check([kwargs["files"]["files[0]"][0] for _, _, kwargs in calls] == ["home.jpg"],
              "discord: a video over the webhook size limit is never uploaded")

        # X / Threads through Buffer: the hosted video when its URL answers, otherwise the project image
        buffer_extra = {"image_url": "https://example.test/shot.png", "video_url": demo_link}
        buffer_posted = VideoResponse({"data": {"createPost": {"post": {"id": "p1", "externalLink": "https://x.test/9"}}}})

        def buffer_assets(calls):
            return [kwargs["json"]["variables"]["input"]["assets"] for method, _, kwargs in calls if method == "post"]

        with fake_http([("head", demo_link, VideoResponse()), ("post", channels.BUFFER_API, buffer_posted)]) as calls:
            buffer_video_url = channels.XViaBuffer(buffer_creds).publish(
                text=x_post, title="", images=[], link="", extra=buffer_extra, idempotency_key="k", video=clip)
        check(buffer_video_url == "https://x.test/9" and buffer_assets(calls) == [[{"video": {"url": demo_link}}]],
              "x/buffer: posts the hosted demo video when its link is live")
        with fake_http([("head", demo_link, VideoResponse(status=404)), ("post", channels.BUFFER_API, buffer_posted)]) as calls:
            channels.ThreadsViaBuffer(threads_creds).publish(
                text=x_post, title="", images=[], link="", extra=buffer_extra, idempotency_key="k", video=clip)
        check(buffer_assets(calls) == [[{"image": {"url": "https://example.test/shot.png"}}]],
              "threads/buffer: a video link that is not live yet posts the image")
        with fake_http([
            ("head", demo_link, VideoResponse()),
            ("post", channels.BUFFER_API, [
                VideoResponse({"data": {"createPost": {"message": "Video is not supported"}}}), buffer_posted]),
        ]) as calls:
            channels.XViaBuffer(buffer_creds).publish(
                text=x_post, title="", images=[], link="", extra=buffer_extra, idempotency_key="k", video=clip)
        check(buffer_assets(calls) == [[{"video": {"url": demo_link}}], [{"image": {"url": "https://example.test/shot.png"}}]],
              "x/buffer: a video Buffer refuses is re-posted with the image")

        # autopilot: the video leads the post's media, a failed recording never costs the post, and the switch works
        choice = {"angle": "launch", "facts": [facts]}
        with mock.patch("autopilot.visuals.images_for_post", return_value=[shot]), \
             mock.patch("autopilot.showcase.target", return_value=(facts.homepage, {})):
            with mock.patch("autopilot.visuals.record_demo", return_value=clip) as demo_recorder:
                media = autopilot.build_images({"github_user": "juw12"}, choice, "2026-12-01 08:00")
            led_by_video = choice["media_kind"]
            with mock.patch("autopilot.visuals.record_demo", side_effect=RuntimeError("no browser")):
                media_without = autopilot.build_images({"github_user": "juw12"}, choice, "2026-12-01 08:00")
            led_by_pictures = choice["media_kind"]
            with mock.patch("autopilot.visuals.record_demo", return_value=clip) as switched_off:
                media_off = autopilot.build_images({"github_user": "juw12", "demo_video": False}, choice, "2026-12-01 08:00")
        check(media == [str(clip), str(shot)] and demo_recorder.call_args[0][0] == facts.homepage
              and demo_recorder.call_args[0][1].name == "watt-reserve_demo.mp4" and led_by_video == "video",
              "autopilot: the demo video comes first in a post's media")
        check(media_without == [str(shot)] and led_by_pictures == "pictures",
              "autopilot: a failed recording still posts the screenshots")
        check(media_off == [str(shot)] and not switched_off.called, 'autopilot: "demo_video": false records nothing')

        with mock.patch.object(autopilot, "DRAFTS", Path(temp_dir) / "drafts"):
            draft_text = autopilot.write_draft("2026-12-01 08:00", "Hello", "Links", media).read_text(encoding="utf-8")
        check(f"## Demo video\n\n- {clip}" in draft_text and f"## Images\n\n- {shot}" in draft_text
              and draft_text.count("_demo.mp4") == 1, "autopilot: the draft lists the demo video apart from the images")

        # Hosting for Buffer: demo/<repo>.mp4 on the Pages site, pushed once, reused while recent, "" when it cannot go live
        pages = Path(temp_dir) / ".pages"
        pages.mkdir()
        git_calls = []
        push_code = [0]

        def fake_pages_git(args, cwd):
            git_calls.append(args[0])
            return subprocess.CompletedProcess(args, push_code[0] if args[0] == "push" else 0, "", "rejected")

        with mock.patch("trylinks._pages_checkout", return_value=True), mock.patch("trylinks._git", fake_pages_git), \
             mock.patch("time.sleep"):
            with mock.patch("trylinks._is_live", side_effect=[False, True]):
                hosted_url = trylinks.host_demo_video("juw12", "watt-reserve", clip, pages, quiet)
            first_git = list(git_calls)
            git_calls.clear()
            with mock.patch("trylinks._is_live", return_value=True):
                reused_url = trylinks.host_demo_video("juw12", "watt-reserve", clip, pages, quiet)
            reuse_git = list(git_calls)
            with mock.patch("trylinks._is_live", return_value=False):
                never_live = trylinks.host_demo_video("juw12", "kilat", clip, pages, quiet)
            push_code[0] = 1
            with mock.patch("trylinks._is_live", return_value=True):
                push_failed = trylinks.host_demo_video("juw12", "cairn", clip, pages, quiet)
        with mock.patch("trylinks._pages_checkout", return_value=False):
            no_checkout = trylinks.host_demo_video("juw12", "watt-reserve", clip, pages, quiet)
        check(hosted_url == demo_link and (pages / "demo" / "watt-reserve.mp4").read_bytes() == b"0123456789"
              and first_git == ["add", "commit", "push"], f"demo hosting: the clip is committed, pushed and served ({first_git})")
        check(reused_url == demo_link and reuse_git == [], "demo hosting: a recent live clip is reused without a new commit")
        check(never_live == "" and push_failed == "" and no_checkout == "",
              "demo hosting: no link is handed out unless the clip is really live")
    finally:
        logging.disable(logging.NOTSET)

# Short videos (reels.py): captions cut only from the repo's own words, formats that rotate and learn, two cuts.
# Nothing here opens a browser or runs ffmpeg.
import reels  # noqa: E402

reel_facts = gh.RepoFacts(
    name="watt-reserve", description="Watt Reserve: plan home battery backup for blackouts, offline.",
    html_url="https://github.com/juw12/watt-reserve", homepage="https://juw12.github.io/watt-reserve/",
    topics=["offline-first", "pwa", "energy"], language="TypeScript", pushed_at="2026-11-01T00:00:00Z", license="MIT",
    tagline="Know how long your battery lasts.", title="Watt Reserve",
    bullets=["Adds up every appliance you choose", "Shows runtime for 3 battery sizes", "Works offline as an installable app",
             "Exports the plan as a printable page"],
)
star_facts = gh.RepoFacts(
    name="earlystar", title="EarlyStar", html_url="https://github.com/juw12/earlystar", homepage="https://juw12.github.io/earlystar/",
    description="EarlyStar: Find GitHub repos before they trend: real star gains, activity, Trending history, badges and "
                "a free JSON API. Runs in your browser.",
    topics=[], language="TypeScript", pushed_at="2026-11-01T00:00:00Z", license="",
    bullets=[
        "Works offline as an installable app",
        "Price: free for every user forever",
        "Kilat | 12 ms | Python | 40 ms here",
        "in a headless browser on every push",
        "Fast",
        "Scores every repository by the stars it gained in the last day, week and month against its own history",
        "Works only in Chrome on the desktop",
        "If the site is down it shows a note",
        "It does not track anything you type",
        "Based on Smith et al for the scoring model",
        "Shows runtime for 3 battery sizes",
    ],
)

long_line = "Tracks every appliance you own, adds up the watts they draw, and shows how long each battery size would keep them running."
comma_cut = reels.short(long_line, 60)
colon_head = reels.short(star_facts.description.split(": ", 1)[1], reels.CAPTION_CHARS)
check(reels.short("Works offline as an installable app.", 84) == "Works offline as an installable app",
      "reels: a sentence that fits is kept whole")
check(comma_cut == "Tracks every appliance you own, adds up the watts they draw\u2026" and len(comma_cut) <= 60,
      f"reels: a long sentence is cut at a comma and marked ({comma_cut!r})")
check(reels.short("A small typed scripting language with units of measure and checked dimensions for engineers", 60)
      == "A small typed scripting language with units of measure\u2026", "reels: a cut never ends on a dangling 'and'")
check(colon_head == "Find GitHub repos before they trend", f"reels: a statement before a colon stands alone, unmarked ({colon_head!r})")
shortened_ok = True
for limit in (40, 60, 84):
    for source_line in [long_line, star_facts.description] + star_facts.bullets:
        cut_line = reels.short(source_line, limit)
        shortened_ok = shortened_ok and len(cut_line) <= limit and reels._clean(source_line).startswith(cut_line.rstrip("\u2026"))
check(shortened_ok, "reels: shortening only ever removes words from the end and respects the limit")

check(reels.what_it_is(reel_facts) == "Plan home battery backup for blackouts, offline",
      f"reels: the intro is the description without the project's name ({reels.what_it_is(reel_facts)!r})")
check(reels.pitch_lines(star_facts) == ["Real star gains, activity, Trending history, badges and a free JSON API", "Runs in your browser"],
      f"reels: the rest of the description becomes captions ({reels.pitch_lines(star_facts)})")
check(reels.readme_lines(star_facts) == ["Works offline as an installable app", "Shows runtime for 3 battery sizes"],
      f"reels: labels, table rows, fragments, long lines, caveats and citations are not captions ({reels.readme_lines(star_facts)})")
thin_facts = copy.deepcopy(reel_facts)
thin_facts.description, thin_facts.tagline, thin_facts.bullets = "", "", ["Works offline as an installable app"]
few_facts = copy.deepcopy(reel_facts)
few_facts.bullets = reel_facts.bullets[:2]
check(reels.available(reel_facts) == list(reels.FORMATS), "reels: a project with a description and features gets every format")
check("listicle" not in reels.available(few_facts) and "hook" in reels.available(few_facts),
      '"3 things" needs three features; the other formats do not')
check(reels.available(thin_facts) == [], "reels: a project with nearly nothing to say gets no styled video")

source_lower = reels._clean(reel_facts.source_text()).lower()
github_address = "github.com/juw12/watt-reserve"
allowed_leads = {"This is Watt Reserve.", "Yes. MIT licence, source on GitHub.", f"All on GitHub: {github_address}", "Watch."}
plans_ok = True
seen_free = False
for video_format in reels.FORMATS:
    for seed in range(40):
        video_plan = reels.plan(reel_facts, video_format, random.Random(seed))
        rows = video_plan.timeline()
        grounded = all(
            beat in allowed_leads or re.sub(r"^\d\. ", "", beat).rstrip("\u2026").lower() in source_lower
            for beat in video_plan.beats
        )
        plans_ok = plans_ok and (
            1 <= len(video_plan.beats) <= 3 and grounded and video_plan.hook and video_plan.github == github_address
            and video_plan.seconds == (reels.SPEEDRUN_SECONDS if video_format == "speedrun" else reels.SECONDS)
            and rows[0] == ("hook", video_plan.hook, 0.0, reels.HOOK_SECONDS) and rows[-1][0] == "end"
            and rows[-1][3] == video_plan.seconds
            and all(abs(before[3] - after[2]) < 1e-9 for before, after in zip(rows, rows[1:]))
            and composer.invented_numbers(video_plan.post_line, reel_facts.source_text()) == []
        )
        seen_free = seen_free or video_plan.hook == "is it really free?"
check(plans_ok, "reels: every format gives 1-3 captions from the repo's own words on a gapless timeline, no invented numbers")
check(seen_free and all(reels.plan(star_facts, "question", random.Random(seed)).hook != "is it really free?" for seed in range(60)),
      'reels: "is it really free?" is only asked when the repo has a licence')
listicle_plan = reels.plan(reel_facts, "listicle", random.Random(1))
check(listicle_plan.hook == "3 things about Watt Reserve" and [beat[:3] for beat in listicle_plan.beats] == ["1. ", "2. ", "3. "],
      "reels: the listicle counts its three features")
own_plan = reels.plan(reel_facts, "hook", random.Random(1), ["Blackout tonight? Open {name}."])
check(own_plan.hook == own_plan.post_line == "Blackout tonight? Open Watt Reserve.", 'reels: "video_hooks" replaces the stock hooks')
numbered_plan = reels.plan(reel_facts, "hook", random.Random(1), ["9000 people use {name}."])
check(numbered_plan.post_line == "", "reels: a hook with a number the repo never states is kept out of the post text")
try:
    reels.plan(reel_facts, "dance", random.Random(1))
    unknown_refused = False
except ValueError:
    unknown_refused = True
check(unknown_refused, "reels: an unknown format is refused")

# Rotation: never back to back, the angle nudges, results decide
rotation_past = [("kilat", "hook"), ("watt-reserve", "pov")]
rotated = {reels.choose_format(reels.FORMATS, reel_facts, "launch", rotation_past, [], random.Random(seed)) for seed in range(200)}
check(rotated and not rotated & {"hook", "pov"}, f"reels: never the previous video's format nor this project's last one ({sorted(rotated)})")
check(reels.choose_format(["hook"], reel_facts, "launch", rotation_past, [], random.Random(1)) == "hook",
      "reels: the only enabled format is used even right after itself")
check(reels.choose_format([], reel_facts, "launch", [], [], random.Random(1)) is None
      and reels.choose_format(reels.FORMATS, thin_facts, "launch", [], [], random.Random(1)) is None,
      "reels: no format when none is enabled or the project has nothing to caption")
feedback_picks = [reels.choose_format(reels.FORMATS, reel_facts, "feedback", [], [], random.Random(seed)) for seed in range(1500)]
feedback_counts = {name: feedback_picks.count(name) for name in reels.FORMATS}
check(feedback_counts["question"] > 1.5 * max(count for name, count in feedback_counts.items() if name != "question"),
      f"reels: a feedback post leans towards the question format ({feedback_counts})")
learned_records = [
    {"impressions": 5000, "engagements": 1000, "format": "speedrun"},
    {"impressions": 5000, "engagements": 10, "format": "hook"},
]
learned_picks = [reels.choose_format(reels.FORMATS, reel_facts, "", [], learned_records, random.Random(seed)) for seed in range(300)]
check(learned_picks.count("speedrun") > 270 and "hook" not in learned_picks,
      f"reels: the format that earned the most is picked the most ({learned_picks.count('speedrun')}/300)")
shipped_config = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
check(all(name in reels.FORMATS for fits in reels.ANGLE_FIT.values() for name in fits)
      and set(shipped_config["video_formats"]) <= set(reels.FORMATS)
      and shipped_config.get("demo_video", True),
      "reels: config.json and the angle table only name real formats")

# Two cuts: sizes agree across modules, each platform gets the right one, captions stay in their zone
feed_video = Path("watt-reserve_demo_hook_feed.mp4")
tall_video = Path("watt-reserve_demo_hook_tall.mp4")
plain_video = Path("watt-reserve_demo.mp4")
picture = Path("card.png")
check(channels.VIDEO_CUTS == reels.CUTS, "reels: channels and reels agree on the video sizes")
check(channels.split_media([feed_video, tall_video, picture]) == (feed_video, [picture])
      and channels.split_media([feed_video, tall_video, picture], cut="tall") == (tall_video, [picture])
      and channels.split_media([plain_video, picture], cut="tall") == (plain_video, [picture]),
      "channels: the feed cut by default, the tall cut for X and Threads, the plain clip when that is all there is")
check(channels.video_size(feed_video) == (1080, 1350) and channels.video_size(tall_video) == (1080, 1920)
      and channels.video_size(plain_video) == channels.DEMO_VIDEO_SIZE, "channels: a video's size is told from its name")
check(channels.video_alt(feed_video) == channels.video_alt(plain_video) == "Screen recording: watt-reserve demo",
      "channels: the alt text leaves the format and cut out")
layout_ok = True
captions_ok = True
phone_ratio = visuals.PHONE_SIZE[0] / visuals.PHONE_SIZE[1]
for cut, (cut_width, cut_height) in reels.CUTS.items():
    zone_left, zone_top, zone_right, zone_bottom = reels.LAYOUTS[cut]["zone"]
    screen_left, screen_top, screen_width, screen_height = reels.LAYOUTS[cut]["screen"]
    layout_ok = layout_ok and (
        screen_width % 2 == 0 and screen_height % 2 == 0 and abs(screen_width / screen_height - phone_ratio) < 0.005
        and screen_left >= 0 and screen_left + screen_width <= cut_width and screen_top + screen_height <= cut_height
        and zone_bottom <= screen_top and 0 <= zone_left < zone_right <= cut_width
    )
    for style in reels.STYLES:
        for caption in ("Watch.", "Real star gains, activity, Trending history, badges and a free JSON API, in your browser"):
            caption_picture, (caption_x, caption_y) = reels.caption_image(caption, cut, style)
            captions_ok = captions_ok and (
                zone_left <= caption_x and zone_top <= caption_y
                and caption_x + caption_picture.width <= zone_right and caption_y + caption_picture.height <= zone_bottom
            )
    backdrop = reels.frame_image(cut, "watt-reserve", github_address)
    screen_centre = (screen_left + screen_width // 2, screen_top + screen_height // 2)
    layout_ok = layout_ok and backdrop.size == (cut_width, cut_height) and backdrop.getpixel(screen_centre)[3] == 0 \
        and backdrop.getpixel((2, 2))[3] == 255 and reels.end_image(cut, github_address).size == (cut_width, cut_height)
check(layout_ok, "reels: in both cuts the recording keeps the phone's shape, sits inside the picture, and shows through the frame")
check(captions_ok, "reels: every caption style stays inside its zone above the phone, in both cuts")

timed_overlays = [(Path(f"caption_{index}.png"), 10, 20, start, end)
                  for index, (_, _, start, end) in enumerate(listicle_plan.timeline())]
for cut, scale in (("feed", "scale=696:960"), ("tall", "scale=894:1232")):
    command = reels.ffmpeg_args("ffmpeg", Path("raw.webm"), 1.5, 1.1, cut, 18, Path("frame.png"), timed_overlays,
                                Path("bar.png"), Path("out.mp4"))
    filters = command[command.index("-filter_complex") + 1]
    check(scale in filters and "between(t,0.00,3.20)" in filters and "between(t,15.20,18.00)" in filters
          and command[command.index("-t") + 1] == "18" and command.count("-i") == len(timed_overlays) + 4
          and command[-1] == "out.mp4" and command[command.index("-ss") + 1] == "1.50",
          f"reels: the {cut} cut is scaled into its phone, timed caption by caption, and cut to length")
check(urllib.parse.urljoin(reel_facts.homepage, visuals.PHONE_WRAPPER_PATH).startswith("https://juw12.github.io/"),
      "visuals: the phone view is served from the site's own origin, so the site keeps its storage")

# The voice (voiceover.py): the captions read aloud, the video timed to the reading. No model is loaded here:
# a stand-in "voice" gives 15 characters a second of non-silent samples.
import wave  # noqa: E402

import voiceover  # noqa: E402


def fake_voice(characters_a_second=15.0, speeds=None):
    def say(text, speed=1.0):
        if speeds is not None:
            speeds.append(speed)
        return b"\x01\x10" * int(voiceover.SAMPLE_RATE * len(text) / characters_a_second / speed)
    return say


def broken_voice(text, speed=1.0):
    raise RuntimeError("no voice today")


def track_facts(path):
    """(seconds, the samples as bytes) of a WAV written by voiceover.write_track."""
    with wave.open(str(path), "rb") as track:
        return track.getnframes() / track.getframerate(), track.readframes(track.getnframes())


def sounds_at(samples, second):
    at = int(round(second * voiceover.SAMPLE_RATE)) * 2
    return samples[at: at + 2] != b"\0\0"


spoken_cases = {
    "POV: you just found EarlyStar": "P.O.V., you just found EarlyStar.",
    f"All on GitHub: {github_address}": "All on GitHub.",
    "Works offline as an installable app": "Works offline as an installable app.",
    "is it really free?": "is it really free?",
    "Use Claude Code / Codex / OpenCode skills with any model": "Use Claude Code, Codex, OpenCode skills with any model.",
    "Runs on Windows/macOS": "Runs on Windows macOS.",
    "Tracks every appliance you own, adds up the watts they draw\u2026": "Tracks every appliance you own, adds up the watts they draw.",
    "**3x** faster & 40% smaller \u2014 e.g. on phones": "3 times faster and 40 percent smaller, for example, on phones.",
    github_address: "",
    "https://juw12.github.io/earlystar/": "",
    "\u2b50 \u2192": "",
    "Paste a link \u2192 get the video": "Paste a link to get the video.",
    "Paste a link -> get the video": "Paste a link to get the video.",
}
spoken_wrong = {caption: voiceover.speakable(caption) for caption, wanted in spoken_cases.items()
                if voiceover.speakable(caption) != wanted}
check(not spoken_wrong, f"voiceover: a caption becomes a sentence a voice can read, addresses left out ({spoken_wrong})")

read_lines = ["Know how long your battery lasts.", "", "Star it on GitHub."]
read_clips = voiceover.read(read_lines, fake_voice())
read_seconds = sum(voiceover.seconds(clip) for clip in read_clips)
check(read_clips[1] == b"" and abs(read_seconds - (33 + 18) / 15) < 0.01, "voiceover: every line is read once, an empty line is silence")
read_speeds = []
hurried = voiceover.read(read_lines, fake_voice(speeds=read_speeds), room=read_seconds / 1.2)
check(sum(voiceover.seconds(clip) for clip in hurried) <= read_seconds / 1.2 and 1.2 < max(read_speeds) <= voiceover.FASTEST,
      f"voiceover: speech that runs over is read again just fast enough to fit ({max(read_speeds):.2f}x)")
for label, refused_call in (
    ("is never read faster than a person would", lambda: voiceover.read(read_lines, fake_voice(), room=read_seconds / 1.5)),
    ("counts a voice that gives no sound as a failure", lambda: voiceover.read(read_lines, lambda text, speed=1.0: b"")),
):
    try:
        refused_call()
        refused = False
    except ValueError:
        refused = True
    check(refused, f"voiceover: speech {label}")
late_lines = []
try:
    voiceover.read(read_lines, lambda text, speed=1.0: late_lines.append(text) or b"", until=voiceover.deadline() - voiceover.PATIENCE - 1)
    gave_up = False
except TimeoutError:
    gave_up = True
check(gave_up and not late_lines and voiceover.read(read_lines, fake_voice(), until=voiceover.deadline()) == read_clips,
      "voiceover: a voice that is past its time limit stops before the next line, one within it reads as usual")

with tempfile.TemporaryDirectory() as voice_dir:
    voice_dir = Path(voice_dir)
    # A voiced video is as long as its script: every caption stays up while its line is said
    voiced_ok = True
    for video_format in ("hook", "pov", "question", "listicle"):
        voiced_plan = reels.plan(reel_facts, video_format, random.Random(3))
        track = reels.narrate(voiced_plan, fake_voice(), voice_dir / f"{video_format}.wav")
        rows = voiced_plan.timeline()
        said = reels.spoken(voiced_plan)
        track_seconds, samples = track_facts(track)
        voiced_ok = voiced_ok and (
            [row[0] for row in rows] == ["hook"] + ["beat"] * len(voiced_plan.beats) + ["end"]
            and [row[1] for row in rows] == [voiced_plan.hook, *voiced_plan.beats, voiced_plan.github]
            and rows[0][2] == 0.0 and rows[-1][3] == voiced_plan.seconds == round(sum(voiced_plan.lengths), 2)
            and all(abs(before[3] - after[2]) < 1e-9 for before, after in zip(rows, rows[1:]))
            and all(end - start >= max(reels.SHORTEST_ROW, len(line) / 15 + voiceover.BREATH) - 0.01
                    for (_, _, start, end), line in zip(rows, said))
            and rows[-1][3] - rows[-1][2] >= reels.END_SECONDS - 1e-9
            and abs(track_seconds - voiced_plan.seconds) < 0.001
            and all(sounds_at(samples, start + voiceover.LEAD + 0.05) and not sounds_at(samples, max(0.0, start + voiceover.LEAD - 0.05))
                    for (_, _, start, _), line in zip(rows, said) if line)
        )
    check(voiced_ok, "reels: a voiced video gives every caption the time its line takes, gapless, and each line starts with its caption")

    # The speedrun promises 15 seconds on screen: it is read faster instead of running longer
    speedrun_plan = reels.plan(reel_facts, "speedrun", random.Random(3))
    reels.narrate(speedrun_plan, fake_voice(), voice_dir / "speedrun.wav")
    speedrun_rows = speedrun_plan.timeline()
    check(speedrun_plan.seconds == reels.SPEEDRUN_SECONDS and abs(sum(speedrun_plan.lengths) - reels.SPEEDRUN_SECONDS) < 1e-6
          and speedrun_rows[-1][3] == reels.SPEEDRUN_SECONDS and abs(track_facts(voice_dir / "speedrun.wav")[0] - 15) < 0.001
          and speedrun_rows[-1][3] - speedrun_rows[-1][2] >= reels.END_SECONDS - 1e-6,
          f"reels: a voiced speedrun still lasts {reels.SPEEDRUN_SECONDS} seconds and keeps its end card ({speedrun_plan.lengths})")
    hurried_runs = []
    for characters_a_second in (12, 11, 10, 9, 8, 7, 6, 5):
        slow_plan, slow_speeds = reels.plan(reel_facts, "speedrun", random.Random(3)), []
        try:
            reels.narrate(slow_plan, fake_voice(characters_a_second, slow_speeds), voice_dir / "slow.wav")
            fitted = slow_plan.seconds == reels.SPEEDRUN_SECONDS and abs(sum(slow_plan.lengths) - 15) < 1e-6 \
                and max(slow_speeds) <= voiceover.FASTEST
        except ValueError:
            fitted = slow_plan.lengths == [] and slow_plan.seconds == reels.SPEEDRUN_SECONDS  # refused: the plan is untouched
        hurried_runs.append((fitted, max(slow_speeds), bool(slow_plan.lengths)))
    check(all(fitted for fitted, _, _ in hurried_runs)
          and any(speed > 1 and voiced for _, speed, voiced in hurried_runs) and not hurried_runs[-1][2],
          f"reels: a speedrun that runs over is read faster, and refused when even that does not fit ({hurried_runs})")

    # The plain recording has no captions, so its voice says what the video is
    plain_seconds = reels.plain_narrator([reel_facts], fake_voice())(voice_dir / "plain.wav")
    plain_said = ["This is Watt Reserve.", "Plan home battery backup for blackouts, offline.", "Star it on GitHub."]
    check(abs(plain_seconds - sum(len(line) / 15 + voiceover.BREATH for line in plain_said)) < 0.01
          and abs(track_facts(voice_dir / "plain.wav")[0] - plain_seconds) < 0.001,
          f"reels: the plain recording is introduced, described and ends on the star ({plain_seconds:.2f} s)")
    logging.disable(logging.CRITICAL)
    try:
        check(reels.plain_narrator([reel_facts, star_facts], broken_voice)(voice_dir / "broken.wav") == 0.0,
              "reels: a voice that fails leaves the plain recording silent")
        with mock.patch.object(voiceover, "PATIENCE", -1):
            check(reels.plain_narrator([reel_facts], fake_voice())(voice_dir / "slow_plain.wav") == 0.0,
                  "reels: a voice that is too slow leaves the plain recording silent")

        # record(): the voice is only ever an addition; when it fails the video is made without it
        def recorded_with(say):
            record_plan = reels.plan(reel_facts, "hook", random.Random(3))
            with mock.patch("reels.visuals.record_raw", return_value=(Path("raw.webm"), 1.0, 12.0)), \
                 mock.patch("reels.render") as renderer:
                outputs = reels.record(record_plan, voice_dir / "out", say)
            return record_plan, renderer, outputs

        record_plan, renderer, outputs = recorded_with(fake_voice())
        check(renderer.call_count == 2 and all(call[0][7].name == "voice.wav" for call in renderer.call_args_list)
              and record_plan.lengths
              and [path.name for path in outputs] == ["watt-reserve_demo_hook_feed.mp4", "watt-reserve_demo_hook_tall.mp4"],
              "reels: with a voice both cuts are rendered with the same spoken track")
        record_plan, renderer, _ = recorded_with(broken_voice)
        check(renderer.call_count == 2 and all(call[0][7] is None for call in renderer.call_args_list)
              and record_plan.lengths == [] and record_plan.seconds == reels.SECONDS,
              "reels: when the voice fails the video is still made, silent and on its usual timeline")
        with mock.patch.object(voiceover, "PATIENCE", -1):
            record_plan, renderer, _ = recorded_with(fake_voice())
        check(renderer.call_count == 2 and all(call[0][7] is None for call in renderer.call_args_list)
              and record_plan.lengths == [] and record_plan.seconds == reels.SECONDS,
              "reels: when the voice is too slow the video is still made, silent and on its usual timeline")
        record_plan, renderer, _ = recorded_with(None)
        check(all(call[0][7] is None for call in renderer.call_args_list) and record_plan.seconds == reels.SECONDS,
              "reels: without a voice nothing about the video changes")
    finally:
        logging.disable(logging.NOTSET)

    # Where the voice comes from: switched off, not installed or no model files all mean "silent", never an error
    model_dir = voice_dir / "models"
    model_dir.mkdir()
    with mock.patch.object(voiceover, "MODEL_FOLDERS", (voice_dir / "nowhere",)):
        check(voiceover.model_folder({}) is None and voiceover.model_folder({"voice_models": str(model_dir)}) is None,
              "voiceover: no model files, no voice")
        logging.disable(logging.CRITICAL)
        try:
            check(voiceover.load({"voice_over": False}) is None and voiceover.load({"voice_models": str(model_dir)}) is None,
                  'voiceover: "voice_over": false or a missing model keeps the videos silent')
        finally:
            logging.disable(logging.NOTSET)
        for model_file in voiceover.MODEL_FILES:
            (model_dir / model_file).write_bytes(b"")
        check(voiceover.model_folder({"voice_models": str(model_dir)}) == model_dir, 'voiceover: "voice_models" names the folder with both files')
check(shipped_config.get("voice_over") is True, "voiceover: the shipped config has the voice switched on")

# The film (reels.FILM) and its music (soundtrack.py): the product in use, nobody speaking, a piece written to the
# video. The notes are plain Python and are checked everywhere; playing them needs numpy, checked where it is installed.
import soundtrack  # noqa: E402

film_plan = reels.plan(reel_facts, reels.FILM, random.Random(3))
check(film_plan.hook == film_plan.post_line == "Meet Watt Reserve." and film_plan.seconds == reels.SECONDS
      and film_plan.beats and all(len(beat) <= reels.SPEEDRUN_CAPTION_CHARS for beat in film_plan.beats),
      f"reels: the film opens on the project's name and says little ({film_plan.beats})")
film_end = reels.SECONDS - reels.END_SECONDS
film_piece = soundtrack.score(reels.SECONDS, film_end, "watt-reserve")
other_pieces = {tuple(soundtrack.score(reels.SECONDS, film_end, name)) for name in ("earlystar", "kilat", "cairn", "reachpilot")}
check(film_piece == soundtrack.score(reels.SECONDS, film_end, "watt-reserve") and len(other_pieces | {tuple(film_piece)}) > 2,
      "soundtrack: a project always gets the same piece, and not every project the same one")
film_pitches = [note.pitch for note in film_piece if note.voice not in ("kick", "hat")]
check(any(all((pitch - key) % 12 in soundtrack.MAJOR for pitch in film_pitches) for key in soundtrack.KEYS)
      and all(note.voice in soundtrack.LEVELS and note.length > 0 and -1 <= note.pan <= 1 for note in film_piece),
      "soundtrack: every note is in the piece's key and played by a voice that exists")
film_bars, film_bar = soundtrack.bar_seconds(film_end)
last_chord = [note for note in film_piece if note.voice == "mallet" and note.start >= film_end - 1e-9]
check(abs(film_bars * film_bar - film_end) < 1e-9 and abs(film_bar - soundtrack.BAR_SECONDS) < 0.5
      and all(0 <= note.start < reels.SECONDS for note in film_piece)
      and max(note.start for note in film_piece if note.voice == "pad" and note.start < film_end - 1e-9) == (film_bars - 1) * film_bar
      and min(note.start for note in film_piece if note.voice in ("kick", "hat")) >= soundtrack.PULSE_FROM_BAR * film_bar
      and len(last_chord) == len(soundtrack.LAST_CHORD) and min(note.start for note in last_chord) == film_end,
      f"soundtrack: the bars end where the end card starts, the pulse waits for the first caption, the last chord lands on the card ({film_bars} bars of {film_bar:.2f} s)")
try:
    soundtrack.score(reels.SECONDS, reels.SECONDS, "watt-reserve")
    card_refused = False
except ValueError:
    card_refused = True
check(card_refused, "soundtrack: an end card outside the video is refused")

film_command = reels.ffmpeg_args("ffmpeg", Path("raw.webm"), 1.5, 1.1, "tall", 18, Path("frame.png"), timed_overlays,
                                 Path("bar.png"), Path("out.mp4"), Path("music.wav"), music=True, fade=reels.FILM_FADE)
film_filters = film_command[film_command.index("-filter_complex") + 1]
voiced_command = reels.ffmpeg_args("ffmpeg", Path("raw.webm"), 1.5, 1.1, "tall", 18, Path("frame.png"), timed_overlays,
                                   Path("bar.png"), Path("out.mp4"), Path("voice.wav"))
check(film_command[film_command.index("-af") + 1] == soundtrack.AUDIO_FILTER
      and film_command[film_command.index("-b:a") + 1] == soundtrack.AUDIO_BITRATE
      and film_command.count("-i") == len(timed_overlays) + 4 and film_command.count("-loop") == len(timed_overlays)
      and "fade=t=in:st=3.20" in film_filters and "fade=t=in:st=0.00" not in film_filters
      and film_filters.count("fade=t=in") == film_filters.count("fade=t=out") == len(timed_overlays) - 1
      and film_command[film_command.index("-t") + 1] == "18",
      "reels: the film is encoded with its music, its captions fade, the first is there from the first frame and the end card stays")
check(voiced_command[voiced_command.index("-af") + 1] == voiceover.AUDIO_FILTER and "-loop" not in voiced_command
      and "fade=" not in voiced_command[voiced_command.index("-filter_complex") + 1],
      "reels: the other formats are encoded as before: the voice's own filter, captions that cut")
film_picture, _ = reels.caption_image("Meet Watt Reserve.", "tall", "film_hook")
boxed_picture, _ = reels.caption_image("Meet Watt Reserve.", "tall", "hook")
check(film_picture.getpixel((0, film_picture.height // 2))[3] == 0 and boxed_picture.getpixel((0, boxed_picture.height // 2))[3] == 255
      and reels.frame_image("tall", "watt-reserve", github_address, film=True).getpixel((2, 2))[:3] == (0, 0, 0),
      "reels: the film's words stand straight on a near-black backdrop, without a box")


def filmed(video_format=reels.FILM, music=True, write=None):
    filmed_plan = reels.plan(reel_facts, video_format, random.Random(3))
    with tempfile.TemporaryDirectory() as film_dir, \
         mock.patch("reels.visuals.record_raw", return_value=(Path("raw.webm"), 1.0, 12.0)), \
         mock.patch("reels.render") as renderer, \
         mock.patch("reels.soundtrack.write", **(write or {"side_effect": lambda seconds, end_at, seed, path: path})) as writer:
        said = []
        voice = fake_voice()
        reels.record(filmed_plan, Path(film_dir) / "out", lambda text, speed=1.0: said.append(text) or voice(text, speed), music=music)
    return filmed_plan, [call[0][7] for call in renderer.call_args_list], writer, said


logging.disable(logging.CRITICAL)
try:
    filmed_plan, tracks, writer, said = filmed()
    check([track.name for track in tracks] == ["music.wav", "music.wav"] and said == [] and filmed_plan.lengths == []
          and writer.call_args[0][:3] == (reels.SECONDS, film_end, "watt-reserve"),
          "reels: the film is never read aloud; both cuts get the project's own piece, written to the video's length")
    _, tracks, writer, _ = filmed(write={"side_effect": RuntimeError("numpy is not installed")})
    check(tracks == [None, None] and writer.called, "reels: when the music fails the film is still made, silent")
    _, tracks, writer, _ = filmed(music=False)
    check(tracks == [None, None] and not writer.called, 'reels: "video_music": false keeps the film silent')
    _, tracks, writer, said = filmed("hook")
    check([track.name for track in tracks] == ["voice.wav", "voice.wav"] and not writer.called and said,
          "reels: the other formats keep their voice and get no music")
    with mock.patch.object(soundtrack, "available", return_value=False):
        try:
            soundtrack.write(reels.SECONDS, film_end, "watt-reserve", Path("never_written.wav"))
            music_refused = False
        except RuntimeError:
            music_refused = True
    check(music_refused and not Path("never_written.wav").exists(), "soundtrack: without numpy no music is written, so the film goes out silent")
finally:
    logging.disable(logging.NOTSET)
check("film" in shipped_config["video_formats"] and shipped_config.get("video_music") is True,
      "reels: the shipped config has the film in rotation and its music switched on")
if soundtrack.available():  # playing the notes; a short piece, the same code path as a full one
    import numpy  # noqa: E402

    with tempfile.TemporaryDirectory() as music_dir:
        with wave.open(str(soundtrack.write(8.0, 5.2, "watt-reserve", Path(music_dir) / "music.wav")), "rb") as music_file:
            music_shape = (music_file.getnchannels(), music_file.getsampwidth(), music_file.getframerate(), music_file.getnframes())
            music = numpy.frombuffer(music_file.readframes(music_file.getnframes()), "<i2").reshape(-1, 2) / 32767
    music_loud = float(numpy.sqrt(numpy.mean(music[soundtrack.SAMPLE_RATE: 5 * soundtrack.SAMPLE_RATE] ** 2)))
    check(music_shape == (2, 2, soundtrack.SAMPLE_RATE, 8 * soundtrack.SAMPLE_RATE)
          and float(numpy.abs(music).max()) <= soundtrack.PEAK + 0.001 and 0.05 < music_loud < 0.25
          and float(numpy.abs(music[-2205:]).max()) < 0.01 and float(numpy.abs(music[:, 0] - music[:, 1]).max()) > 0.01,
          f"soundtrack: the piece is as long as its video, in stereo, never over its peak, and fades to nothing ({music_loud:.3f} rms)")

voiced_command = reels.ffmpeg_args("ffmpeg", Path("raw.webm"), 1.5, 1.1, "feed", 21.4, Path("frame.png"), timed_overlays,
                                   Path("bar.png"), Path("out.mp4"), Path("voice.wav"))
silent_command = reels.ffmpeg_args("ffmpeg", Path("raw.webm"), 1.5, 1.1, "feed", 18, Path("frame.png"), timed_overlays,
                                   Path("bar.png"), Path("out.mp4"))
check("voice.wav" in voiced_command and not any("anullsrc" in part for part in voiced_command)
      and voiced_command[voiced_command.index("-af") + 1] == voiceover.AUDIO_FILTER
      and voiced_command[voiced_command.index("-b:a") + 1] == voiceover.AUDIO_BITRATE
      and voiced_command[voiced_command.index("-t") + 1] == "21.4" and voiced_command.count("-i") == len(timed_overlays) + 4
      and "1:a" in voiced_command,
      "reels: a voiced cut takes the spoken track as its sound and lasts as long as the script")
check(any("anullsrc" in part for part in silent_command) and "-af" not in silent_command
      and silent_command[silent_command.index("-b:a") + 1] == "48k" and silent_command[silent_command.index("-maxrate") + 1] == "3500k",
      "reels: a silent cut is encoded exactly as before")
size_ok = all(
    (int(command[command.index("-maxrate") + 1][:-1]) + 96) * seconds <= reels.SIZE_BUDGET_KBIT
    for seconds in (15, 18, 21.4, 26, 34)
    for command in [reels.ffmpeg_args("ffmpeg", Path("raw.webm"), 1.5, 1.1, "tall", seconds, Path("frame.png"), timed_overlays,
                                      Path("bar.png"), Path("out.mp4"), Path("voice.wav"))]
)
check(size_ok and reels.SIZE_BUDGET_KBIT <= 10 * 8000 * 0.9, "reels: a longer voiced video gets fewer bits a second, never more than 9 MB")
check(reels.end_image("feed", github_address, note=reels.VOICE_NOTE).tobytes() != reels.end_image("feed", github_address).tobytes(),
      "reels: the end card of a voiced video says the voice is an AI one")

silent_demo = visuals.demo_args("ffmpeg", Path("raw.webm"), 1.5, 12.0, Path("demo.mp4"))
voiced_demo = visuals.demo_args("ffmpeg", Path("raw.webm"), 1.5, 4.0, Path("demo.mp4"), Path("voice.wav"), 9.5)
check(any("anullsrc" in part for part in silent_demo) and "-shortest" in silent_demo and "-af" not in silent_demo
      and silent_demo[silent_demo.index("-t") + 1] == str(visuals.DEMO_MAX_SECONDS),
      "visuals: the silent plain recording is encoded exactly as before")
check("voice.wav" in voiced_demo and voiced_demo[voiced_demo.index("-af") + 1] == f"{voiceover.AUDIO_FILTER},apad"
      and "tpad=stop_mode=clone" in voiced_demo[voiced_demo.index("-vf") + 1]
      and float(voiced_demo[voiced_demo.index("-t") + 1]) == 9.5 and "-shortest" not in voiced_demo,
      "visuals: a narrated plain recording lasts until the voice is done, the last frame held")

# autopilot: styled video first, the plain recording when that fails, screenshots always; the hook opens the post
logging.disable(logging.CRITICAL)
try:
    styled_pair = [Path("watt-reserve_demo_hook_feed.mp4"), Path("watt-reserve_demo_hook_tall.mp4")]
    styled_config = {"github_user": "juw12", "video_formats": ["hook"], "video_hooks": {"watt-reserve": ["Blackout tonight? Open {name}."]}}

    def media_for(video_config, angle="launch", subjects=None, record_result=None, extra=None, db=None, earlier=0):
        video_choice = {"angle": angle, "facts": subjects or [reel_facts], "body": "The post itself.", **(extra or {})}
        with mock.patch("autopilot.visuals.images_for_post", return_value=[picture]), \
             mock.patch("autopilot.trylinks._is_live", return_value=False), \
             mock.patch("autopilot.reels.record", **(record_result or {"return_value": styled_pair})) as styled_recorder, \
             mock.patch("autopilot.visuals.record_demo", return_value=plain_video) as plain_recorder:
            made = autopilot.build_images(video_config, video_choice, "2026-12-01 08:00", db, earlier=earlier)
        return made, video_choice, styled_recorder, plain_recorder

    made, video_choice, styled_recorder, plain_recorder = media_for(styled_config)
    check(made == [str(path) for path in styled_pair + [picture]] and not plain_recorder.called
          and styled_recorder.call_args[0][0].format == "hook"
          and video_choice["video"] == {"format": "hook", "hook": "Blackout tonight? Open Watt Reserve."},
          f"autopilot: a styled video leads the media in both cuts and names its format and hook ({video_choice.get('video')})")
    made, video_choice, _, plain_recorder = media_for(styled_config, record_result={"side_effect": RuntimeError("no browser")})
    check(made == [str(plain_video), str(picture)] and "video" not in video_choice and plain_recorder.called,
          "autopilot: when the styled video fails the plain recording is used and no hook is added")
    made, video_choice, styled_recorder, _ = media_for({"github_user": "juw12", "video_formats": []})
    check(made == [str(plain_video), str(picture)] and not styled_recorder.called and "video" not in video_choice,
          'autopilot: "video_formats": [] keeps the plain recording')
    made, _, styled_recorder, plain_recorder = media_for({**styled_config, "demo_video": False})
    check(made == [str(picture)] and not styled_recorder.called and not plain_recorder.called,
          'autopilot: "demo_video": false makes no video of either kind')
    made, video_choice, styled_recorder, plain_recorder = media_for(styled_config, angle="roundup", subjects=[reel_facts, star_facts])
    roundup_plan = styled_recorder.call_args[0][0]
    roundup_address = "https://juw12.github.io/reachpilot-showcase/roundup/"
    check(made[:2] == [str(path) for path in styled_pair] and not plain_recorder.called and roundup_plan.format == "roundup"
          and roundup_plan.url == roundup_address and "EarlyStar" in roundup_plan.pages[roundup_address]
          and video_choice["video"] == {"format": "roundup", "hook": ""} and video_choice["media_kind"] == "video",
          f"autopilot: a roundup gets a video of a page built for it, and no hook line is put on the post ({video_choice.get('video')})")
    _, _, _, plain_recorder = media_for({"github_user": "juw12"}, angle="roundup", subjects=[reel_facts, star_facts])
    check(plain_recorder.call_args[0][0] == roundup_address and plain_recorder.call_args[0][1].name == "roundup_demo.mp4"
          and roundup_address in plain_recorder.call_args[0][2],
          "autopilot: without styled formats a roundup still gets the plain recording of its built page")

    # The voice goes to whichever recording is made; no voice means the same calls as before, with nothing to say
    with mock.patch("autopilot.voiceover.load", return_value=broken_voice):
        _, _, styled_recorder, _ = media_for(styled_config)
        _, _, _, plain_recorder = media_for({"github_user": "juw12", "video_formats": []})
    check(styled_recorder.call_args[0][2] is broken_voice and callable(plain_recorder.call_args[0][3]),
          "autopilot: the voice is handed to the styled video, and to the plain recording as its narrator")
    with mock.patch("autopilot.voiceover.load", return_value=None):
        _, _, styled_recorder, _ = media_for(styled_config)
        _, _, _, plain_recorder = media_for({"github_user": "juw12", "video_formats": []})
    check(styled_recorder.call_args[0][2] is None and plain_recorder.call_args[0][3] is None,
          "autopilot: without a voice both recordings are made silent")

    # A site-less project: the video tours a page about it, so no line may promise a look at the product working
    described = copy.deepcopy(reel_facts)
    described.homepage = ""
    made, video_choice, styled_recorder, _ = media_for(
        {"github_user": "juw12", "video_formats": ["pov"]}, subjects=[described])
    described_plan = styled_recorder.call_args[0][0]
    described_address = "https://juw12.github.io/reachpilot-showcase/watt-reserve/"
    check(described_plan.url == described_address and described_address in described_plan.pages
          and described_plan.hook not in [line.format(name="Watt Reserve") for line in reels.USE_HOOKS],
          f"autopilot: a project without a site is filmed on a page built from its repo ({described_plan.url})")
    use_lines = {line.format(name="Watt Reserve") for line in reels.USE_HOOKS}
    honest = all(
        reels.plan(reel_facts, video_format, random.Random(seed), url=described_address, shows_product=False).hook not in use_lines
        for video_format in ("pov", "question") for seed in range(80)
    )
    shown = {reels.plan(reel_facts, video_format, random.Random(seed)).hook
             for video_format in ("pov", "question") for seed in range(80)}
    check(honest and use_lines <= shown,
          "reels: 'you try it' and 'does it work' hooks are kept for videos that show the product running")

    three = [reel_facts, star_facts, described]
    roundup_video = reels.plan_roundup(three + [reel_facts], "juw12", roundup_address, {roundup_address: "<html>"})
    check(roundup_video.hook == "4 projects I built" and len(roundup_video.beats) == reels.ROUNDUP_PROJECTS
          and roundup_video.beats[0].startswith("Watt Reserve: ") and roundup_video.beats[1].startswith("EarlyStar: ")
          and all(len(beat) <= reels.CAPTION_CHARS for beat in roundup_video.beats)
          and roundup_video.end == "See them all on GitHub" and roundup_video.github == "github.com/juw12"
          and roundup_video.post_line == "" and roundup_video.timeline()[-1][0] == "end",
          f"reels: a roundup video counts the projects and gives the first three a caption each ({roundup_video.beats})")
    try:
        reels.plan_roundup([reel_facts], "juw12", roundup_address)
        check(False, "reels: one project is not a roundup")
    except ValueError:
        check(True, "reels: one project is not a roundup")
    check(reels.ROUNDUP not in reels.FORMATS and reels.end_image("feed", "github.com/juw12", "See them all on GitHub").size == (1080, 1350),
          "reels: the roundup is never drawn for a single project, and its end card says where the rest is")

    # A prepared seed post: its own pictures and words are kept, and the repo behind it still gets a video in front
    seed_extra = {"source": "queue:1", "facts": [], "video_subjects": [reel_facts], "images": ["seed_card.png"]}
    with mock.patch("autopilot.visuals.images_for_post", side_effect=AssertionError("a seed post has its pictures")):
        seed_choice = {"angle": "launch", "body": "My own first line.", **seed_extra}
        with mock.patch("autopilot.reels.record", return_value=styled_pair) as seed_recorder:
            seed_media = autopilot.build_images(styled_config, seed_choice, "2026-12-01 08:00")
    autopilot.lead_with_hook({}, seed_choice)
    check(seed_media == [str(path) for path in styled_pair] + ["seed_card.png"] and seed_recorder.called
          and seed_choice["video"]["format"] == "hook" and seed_choice["body"] == "My own first line.",
          "autopilot: a prepared seed post gets a video in front of its pictures and keeps its own first line")
    textless = {"angle": "launch", "body": "Words only.", "source": "queue:2", "facts": []}
    with mock.patch("autopilot.visuals.images_for_post", return_value=[]), \
         mock.patch("autopilot.visuals.record_demo") as nothing_recorded:
        check(autopilot.build_images(styled_config, textless, "2026-12-01 08:00") == [] and textless["media_kind"] == "text"
              and not nothing_recorded.called, "autopilot: a post about no project stays a text post")

    # Hosting names: one file per project and format, a fresh one for every roundup
    with tempfile.TemporaryDirectory() as host_dir:
        hosted_styled = Path(host_dir) / "watt-reserve_demo_hook_tall.mp4"
        hosted_plain = Path(host_dir) / "watt-reserve_demo.mp4"
        hosted_roundup = Path(host_dir) / "roundup_demo_roundup_tall.mp4"
        for hosted_file in (hosted_styled, hosted_plain, hosted_roundup):
            hosted_file.write_bytes(b"video")
        with mock.patch("autopilot.trylinks.host_demo_video", return_value="https://x.test/v.mp4") as host:
            host_results = [
                autopilot.host_video({"github_user": "juw12"}, "2026-12-01 08:00", "watt-reserve", [str(hosted_styled), "card.png"]),
                autopilot.host_video({"github_user": "juw12"}, "2026-12-01 08:00", "watt-reserve", [str(hosted_plain)]),
                autopilot.host_video({"github_user": "juw12"}, "2026-12-01 08:00", None, [str(hosted_roundup)]),
                autopilot.host_video({"github_user": "juw12"}, "2026-12-01 08:00", "watt-reserve", ["card.png"]),
            ]
        hosted_names = [call[0][1] for call in host.call_args_list]
        with mock.patch("autopilot.trylinks.host_demo_video", side_effect=RuntimeError("no network")):
            failed_host = autopilot.host_video({"github_user": "juw12"}, "2026-12-01 08:00", "watt-reserve", [str(hosted_plain)])
    check(hosted_names == ["watt-reserve-hook", "watt-reserve", "roundup-2026-12-01"]
          and host_results == ["https://x.test/v.mp4"] * 3 + [""] and failed_host == "",
          f"autopilot: videos are hosted per project and format, a roundup under its date ({hosted_names})")

    hooked = {"body": "The post itself.", "video": {"format": "hook", "hook": "Stop scrolling for a second."}}
    autopilot.lead_with_hook({}, hooked)
    autopilot.lead_with_hook({}, hooked)
    check(hooked["body"] == "Stop scrolling for a second.\n\nThe post itself.", "autopilot: the video's hook opens the post, once")
    untouched = [
        ({"video_hook_in_post": False}, {"body": "The post itself.", "video": {"format": "hook", "hook": "Stop scrolling."}}),
        ({}, {"body": "The post itself.", "video": {"format": "hook", "hook": ""}}),
        ({}, {"body": "The post itself."}),
    ]
    for hook_config, hook_choice in untouched:
        autopilot.lead_with_hook(hook_config, hook_choice)
    check(all(hook_choice["body"] == "The post itself." for _, hook_choice in untouched),
          'autopilot: no hook line without a styled video, without a safe line, or with "video_hook_in_post": false')

    # A hook on top must not push the substance out of the short platforms
    opener = "Stop scrolling for a second."
    hooked_body = opener + "\n\n" + composer.compose_launch(reel_facts, random.Random(3), set(), True) + "\n\n#OpenSource #PWA"
    hooked_variants = channels.build_variants(hooked_body, composer.links_for(reel_facts), "launch", reel_facts)
    for name in ("x", "bluesky"):
        short_body = hooked_variants[name]["body"]
        said = re.sub(r"https?://\S+|#\w+", "", short_body.replace(opener, "")).strip()
        check(short_body.startswith(opener) and len(said) >= 40 and "juw12.github.io/watt-reserve" in short_body,
              f"channels: {name} keeps the hook, real content and the link ({len(said)} chars of content)")
    paragraph = "This sentence says something about the project and it goes on for a while. " * 4
    check(channels.fit([opener, paragraph], "\n\nhttps://x.test", 200).startswith(opener + "\n\nThis sentence says")
          and len(channels.fit([opener, paragraph], "\n\nhttps://x.test", 200)) <= 200,
          "channels: after a short opener the next paragraph is shortened to fit")
    check(channels.fit([paragraph[:76].strip(), paragraph], "\n\nhttps://x.test", 200) == paragraph[:76].strip() + "\n\nhttps://x.test",
          "channels: after a full first paragraph nothing is cut in half")

    check(dashboard.post_media([str(feed_video), str(tall_video), str(picture)]) == [feed_video, picture],
          "dashboard: a post card shows its video once, then the pictures")
    dashboard_page = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
    reel_labels = re.search(r"const REEL_FORMATS = \{([^}]*)\}", dashboard_page)
    check(reel_labels is not None and all(f"{name}:" in reel_labels[1] for name in reels.FORMATS) and "video.reel" in dashboard_page,
          "dashboard: the page labels every video format")
    check(re.search(r"\.todo \.text \{[^}]*overflow-wrap:\s*anywhere", dashboard_page) is not None,
          "dashboard: a long link or word in a to-do note wraps instead of widening the phone page")

    # The format is stored with the post and read back for the rotation and for learning
    with tempfile.TemporaryDirectory() as reel_dir:
        real_paths = (autopilot.DB_PATH, autopilot.SECRETS_PATH, autopilot.TOKEN_PATH)
        autopilot.DB_PATH = Path(reel_dir) / "state.db"
        autopilot.SECRETS_PATH = Path(reel_dir) / "secrets.json"
        autopilot.TOKEN_PATH = Path(reel_dir) / "token.json"
        try:
            reel_db = autopilot.connect_db()
            reel_db.close()
            reel_db = autopilot.connect_db()  # a second start must not try to add the column again
            stored_posts = [
                ("2026-11-03 08:00", "watt-reserve", "posted", "2026-11-03 08:01", "hook"),
                ("2026-11-04 08:00", "kilat", "posted", "2026-11-04 08:01", "speedrun"),
                ("2026-11-05 08:00", "cairn", "posted", "2026-11-05 08:01", None),
                ("2026-11-10 08:00", "earlystar", "ready", None, "pov"),
            ]
            reel_db.executemany(
                "INSERT INTO posts(slot, source, repo, angle, body, status, posted_at, video_format) VALUES(?,'x',?,'launch','b',?,?,?)",
                stored_posts,
            )
            reel_db.execute("INSERT INTO metrics VALUES(?,?,?,?,datetime('now'))", ("u1", "2026-11-03", 900, 30))
            reel_db.execute("INSERT INTO metrics VALUES(?,?,?,?,datetime('now'))", ("u2", "2026-11-05", 700, 20))
            reel_db.commit()
            stored_history = [tuple(row) for row in reels.history(reel_db)]
            stored_records = reels.format_records(reel_db)

            # One post in every "picture_every" leads with its pictures: the baseline the videos are measured against
            every_fifth = {"github_user": "juw12", "picture_every": 5}
            check(not autopilot.picture_turn(every_fifth, reel_db), "autopilot: posts made before media was recorded do not count")
            reel_db.execute("UPDATE posts SET media_kind = 'video'")
            reel_db.commit()
            turns = [autopilot.picture_turn(every_fifth, reel_db, earlier) for earlier in range(6)]
            check(turns == [True, False, False, False, False, True],
                  f"autopilot: with four posts made the fifth leads with pictures, then every fifth after it ({turns})")
            check(not autopilot.picture_turn({"picture_every": 1}, reel_db) and not autopilot.picture_turn({}, reel_db)
                  and not autopilot.picture_turn(every_fifth, None),
                  'autopilot: no picture posts without "picture_every" of 2 or more, or without a history to count')
            made, video_choice, styled_recorder, plain_recorder = media_for({**styled_config, "picture_every": 5}, db=reel_db)
            check(made == [str(picture)] and video_choice["media_kind"] == "pictures" and "video" not in video_choice
                  and not styled_recorder.called and not plain_recorder.called,
                  "autopilot: the picture post records no video and gets no hook line")
            made, video_choice, _, _ = media_for({**styled_config, "picture_every": 5}, db=reel_db, earlier=1)
            check(made[-1] == str(picture) and len(made) == 3 and video_choice["media_kind"] == "video",
                  "autopilot: the post after it leads with a video again")
            with mock.patch("autopilot.visuals.images_for_post", return_value=[]), \
                 mock.patch("autopilot.reels.record", return_value=styled_pair):
                bare_choice = {"angle": "launch", "facts": [reel_facts], "body": "The post itself."}
                bare_media = autopilot.build_images({**styled_config, "picture_every": 5}, bare_choice, "2026-12-01 08:00", reel_db)
            check(bare_media == [str(path) for path in styled_pair] and bare_choice["media_kind"] == "video",
                  "autopilot: a picture turn with no pictures to show still gets its video")
            reel_db.close()
        finally:
            autopilot.DB_PATH, autopilot.SECRETS_PATH, autopilot.TOKEN_PATH = real_paths
    check(stored_history == [("earlystar", "pov"), ("kilat", "speedrun"), ("watt-reserve", "hook")],
          f"reels: the posts table remembers each video's format, newest first ({stored_history})")
    check([(record["repo"], record["format"], record["impressions"]) for record in stored_records
           if record["repo"] == "watt-reserve"] == [("watt-reserve", "hook", 900)]
          and all(record["repo"] != "cairn" for record in stored_records),
          f"reels: results are credited to the format of the post that earned them ({stored_records})")
finally:
    logging.disable(logging.NOTSET)

# Creators who covered a project are thanked in the next post about it and listed on its watch page (creators.py)
cover_a = {"id": 3, "creator": "Dev  Tube", "url": "https://youtu.be/abc", "repo": "watt-reserve", "credited": False}
cover_b = {"id": 4, "creator": "Kode Pagi", "url": "https://youtu.be/def", "repo": "watt-reserve", "credited": True}
cover_c = {"id": 5, "creator": "Offline Pod", "url": "https://pod.example/9", "repo": "kilat", "credited": False}
unusable = [{"id": 6, "creator": "", "url": "https://youtu.be/x", "repo": "watt-reserve"},
            {"id": 7, "creator": "No Link", "url": "javascript:alert(1)", "repo": "watt-reserve"}]
with mock.patch("creators._promote", return_value=json.dumps([cover_a, cover_b, cover_c] + unusable)) as promote_call:
    all_covers = creators.coverage()
    repo_covers = creators.coverage("watt-reserve")
    waiting = creators.pending("watt-reserve")
check([entry["id"] for entry in all_covers] == [3, 4, 5] and [entry["id"] for entry in repo_covers] == [3, 4]
      and [entry["id"] for entry in waiting] == [3] and waiting[0]["creator"] == "Dev Tube"
      and promote_call.call_args[0] == ("credits",),
      "creators: covers are read from the outreach module; entries without a name or a real link are dropped")
with mock.patch("creators._promote", return_value="not json"):
    broken_covers = creators.coverage()
with mock.patch.object(creators, "PROMOTE_SCRIPT", Path("no-such-folder") / "promote.py"), \
     mock.patch("creators.subprocess.run", side_effect=AssertionError("nothing to run")):
    absent_covers = creators.pending("watt-reserve")
check(broken_covers == [] and absent_covers == [], "creators: without the outreach module, or when it fails, nobody is credited")

tagged_post = "Watt Reserve plans your backup.\n\nLinks in the first comment.\n\n#OpenSource #PWA"
one_body, one_comment = creators.credit(tagged_post, "GitHub: https://github.com/juw12/watt-reserve", waiting, "Watt Reserve")
check(one_body == "Watt Reserve plans your backup.\n\nLinks in the first comment.\n\n"
                  "Thanks to Dev Tube for covering Watt Reserve.\n\n#OpenSource #PWA"
      and one_comment == "GitHub: https://github.com/juw12/watt-reserve\nDev Tube: https://youtu.be/abc",
      "creators: one creator is thanked before the hashtags and their video is linked where the post keeps its links")
many_body, many_comment = creators.credit("No tags here.", "", all_covers, "Watt Reserve")
check(many_body == "No tags here.\n\nThanks to Dev Tube, Kode Pagi and Offline Pod for covering Watt Reserve.\n"
                   "Dev Tube: https://youtu.be/abc\nKode Pagi: https://youtu.be/def\nOffline Pod: https://pod.example/9"
      and many_comment == "",
      "creators: several creators are named together; without a links comment their links go under the thanks")
check(creators.credit(tagged_post, "Links", [], "Watt Reserve") == (tagged_post, "Links"),
      "creators: no cover, no change to the post")
with mock.patch("creators._promote", return_value="") as promote_call:
    creators.mark(waiting)
check(promote_call.call_args[0] == ("action", "credited", "3"), "creators: a thanked cover is marked so it is not thanked twice")

thanked_choice = {"repo": "watt-reserve", "facts": [facts], "body": tagged_post, "comment": None}
with mock.patch("autopilot.creators.pending", return_value=waiting), mock.patch("autopilot.creators.mark") as marker:
    draft_thanks = autopilot.thank_creators(dict(thanked_choice))
    not_marked_for_draft = not marker.called
    autopilot.thank_creators(thanked_choice, mark=True)
check(draft_thanks == waiting and not_marked_for_draft and marker.call_args[0][0] == waiting
      and "Thanks to Dev Tube for covering Watt Reserve.\nDev Tube: https://youtu.be/abc" in thanked_choice["body"]
      and thanked_choice["body"].endswith("#OpenSource #PWA"),
      "autopilot: the thank-you goes into the post; only a real post marks the cover as thanked, a preview does not")
roundup_choice = {"repo": None, "facts": [facts], "body": tagged_post, "comment": ""}
with mock.patch("autopilot.creators.pending", side_effect=AssertionError("a roundup has no single project")):
    check(autopilot.thank_creators(roundup_choice) == [] and roundup_choice["body"] == tagged_post,
          "autopilot: a roundup thanks nobody")
logging.disable(logging.CRITICAL)
try:
    with mock.patch("autopilot.creators.pending", side_effect=RuntimeError("module broke")):
        survived = autopilot.thank_creators(dict(thanked_choice, body=tagged_post)) == []
finally:
    logging.disable(logging.NOTSET)
check(survived, "autopilot: a failing outreach module never blocks a post")

# Watch page: the project's video on a page of its own, published on the Pages site and noted for the outreach e-mails
check({"watch", "reachpilot-showcase"} <= trylinks.RESERVED_NAMES, "watch page: no project go-page can take watch/ or the built pages' folder")
with tempfile.TemporaryDirectory() as watch_dir:
    watch_checkout = Path(watch_dir) / ".pages"
    watch_checkout.mkdir()
    watch_address = "https://juw12.github.io/watch/watt-reserve/"
    git_calls = []

    def fake_git(arguments, cwd):
        git_calls.append(arguments[0])
        return mock.Mock(returncode=0, stderr="")

    watch_log = logging.getLogger("selftest.watch")
    watch_log.disabled = True
    with mock.patch("showcase.trylinks._pages_checkout", return_value=True), \
         mock.patch("showcase.trylinks._git", side_effect=fake_git), \
         mock.patch("showcase.trylinks._is_live", return_value=True), mock.patch("time.sleep"):
        published = showcase.publish_watch("juw12", facts, demo_link, watch_checkout, watch_log, watch_credits)
        first_calls = list(git_calls)
        published_again = showcase.publish_watch("juw12", facts, demo_link, watch_checkout, watch_log, watch_credits)
    watch_file = watch_checkout / "watch" / "watt-reserve" / "index.html"
    check(published == published_again == watch_address == showcase.watch_url("juw12", "watt-reserve")
          and watch_file.read_text(encoding="utf-8") == watch_page
          and first_calls == ["add", "commit", "push"] and git_calls[3:] == ["push"],
          f"watch page: written once with the video and the credits, pushed, and not committed again unchanged ({git_calls})")
    with mock.patch("showcase.trylinks._pages_checkout", return_value=True), \
         mock.patch("showcase.trylinks._git", side_effect=fake_git), \
         mock.patch("showcase.trylinks._is_live", return_value=False), mock.patch("time.sleep"):
        never_live = showcase.publish_watch("juw12", facts, demo_link, watch_checkout, watch_log)
    with mock.patch("showcase.trylinks._pages_checkout", return_value=False), \
         mock.patch("showcase.trylinks._git", side_effect=AssertionError("no checkout, no git")):
        no_checkout = showcase.publish_watch("juw12", facts, demo_link, watch_checkout, watch_log)
    check(never_live == "" and no_checkout == "", "watch page: no address is handed out until the page answers")

    watch_index = Path(watch_dir) / "generated" / showcase.WATCH_INDEX
    showcase.remember(watch_index, "watt-reserve", watch_address)
    showcase.remember(watch_index, "kilat", "https://juw12.github.io/watch/kilat/")
    showcase.remember(watch_index, "watt-reserve", watch_address)
    check(json.loads(watch_index.read_text(encoding="utf-8")) == {
        "kilat": "https://juw12.github.io/watch/kilat/", "watt-reserve": watch_address},
        "watch page: live pages are noted by repo, where the outreach e-mails look for a video link")

    watch_row = ("composer", "watt-reserve", "launch", "body", "", json.dumps(["card.png"]))
    with mock.patch.object(autopilot, "GENERATED", Path(watch_dir) / "generated"), \
         mock.patch("autopilot.creators.coverage", return_value=watch_credits) as covers_asked, \
         mock.patch("autopilot.showcase.publish_watch", return_value=watch_address) as watch_publisher, \
         mock.patch("autopilot.host_video", return_value="") as host_asked:
        with_link = autopilot.publish_watch_page({"github_user": "juw12"}, "2026-12-01 08:00", watch_row, facts, demo_link)
        without_video = autopilot.publish_watch_page({"github_user": "juw12"}, "2026-12-01 08:00", watch_row, facts)
        switched_off_page = autopilot.publish_watch_page(
            {"github_user": "juw12", "watch_pages": False}, "2026-12-01 08:00", watch_row, facts, demo_link)
        roundup_row = ("composer", None, "roundup", "body", "", "[]")
        for_roundup = autopilot.publish_watch_page({"github_user": "juw12"}, "2026-12-01 08:00", roundup_row, facts, demo_link)
    check(with_link == watch_address and watch_publisher.call_count == 1 and watch_publisher.call_args[0][2] == demo_link
          and watch_publisher.call_args[0][5] == watch_credits and covers_asked.call_args[0] == ("watt-reserve",)
          and host_asked.call_count == 1 and (without_video, switched_off_page, for_roundup) == ("", "", ""),
          'autopilot: a posted video gets its watch page; none without a hosted video, for a roundup, or with "watch_pages": false')

# patent screen (patents.py): scorer, filing window, route, changes, notes, the hub card and its to-dos.
# Everything here is offline and uses made-up projects.
import patents  # noqa: E402
from datetime import date as patent_date  # noqa: E402

patent_today = patent_date(2026, 11, 14)
rig_text = ("A printed wave spring preloads a split bearing. The solver fits the spring compliance to the "
            "measured torque curve.")
strong_text = (rig_text + " An interpolation table and a heuristic set the preload. The algorithm was checked "
               "against a benchmark: accuracy is within about 4 percent, compared with 18 percent for the stock part.")
marked_text = rig_text + " Unfiled patentable material: do not publish this repository before a filing decision."
game_text = ("Match the tiles. An algorithm with a solver, eigenvalues, a p-norm, multigrid, a voxel grid, cosine "
             "distance, a checksum, a parser and a heuristic. Measured against a benchmark: accuracy and recall "
             "are up and it is faster. Novel and written from scratch.")

rig = patents.score_text("Friction hinge with a printed torque limiter.", rig_text)
strong = patents.score_text("Friction hinge with a printed torque limiter.", strong_text)
marked = patents.score_text("Friction hinge with a printed torque limiter.", marked_text)
game = patents.score_text("A cozy puzzle game for the browser.", game_text)
check(rig["subject"] == "physical" and rig["score"] == 40 and rig["band"] == "maybe",
      f"patents: a hardware README scores by subject, mechanism and result ({rig['score']})")
check(strong["score"] >= patents.BAND_LOOK and strong["band"] == "look",
      f"patents: more mechanism and a measured result reach 'worth an attorney look' ({strong['score']})")
check(all(signal["evidence"] and signal["words"] for signal in strong["signals"]),
      "patents: every signal carries the words found and a quoted sentence")
check(game["subject"] == "content" and game["score"] == patents.BAND_MAYBE - 1 and game["band"] == "unlikely",
      f"patents: a game is capped below 'maybe' however many technical words it uses ({game['score']})")
check(marked["score"] == rig["score"] + patents.MARKED_POINTS and marked["band"] == "look" and marked["confidential"]
      and not rig["confidential"],
      "patents: your own 'patent material' note adds 40 and marks the repo as meant to stay private")
spellings = patents.score_text("A note.", "It pins each entry, and pinning stops silent changes.")
check([signal["points"] for signal in spellings["signals"] if signal["key"] == "mechanism"] == [patents.MECHANISM_POINTS],
      "patents: two spellings of one idea count once")
check(patents.score_text("", "") == {"score": 3, "band": "unlikely", "subject": "content", "confidential": False,
                                    "signals": [{"key": "subject", "label": "App, content, game or list", "points": 3,
                                                 "words": [], "evidence": ""}]},
      "patents: an empty repo scores as content, no crash")


def patent_repo(name, description, created, private=False, fork=False, licence="mit", pushed="2026-11-01T08:00:00Z"):
    return {
        "name": name, "full_name": f"someone/{name}", "html_url": f"https://example.test/{name}",
        "private": private, "fork": fork, "archived": False, "description": description,
        "created_at": created, "pushed_at": pushed, "license": {"key": licence} if licence else None,
    }


hinge = "Friction hinge with a printed torque limiter."
borrowed = patents.build_entry(patent_repo("borrowed", hinge, "2026-06-01T00:00:00Z", fork=True), marked_text)
check(borrowed["score"] == 0 and borrowed["band"] == "unlikely" and borrowed["signals"] == [] and not borrowed["confidential"],
      "patents: a fork is never scored")

still_open = patents.window(True, "2020-01-01T00:00:00Z", patent_today)
in_grace = patents.window(False, "2026-01-21T09:30:00Z", patent_today)
too_late = patents.window(False, "2024-01-01T00:00:00Z", patent_today)
check(still_open["state"] == "open" and still_open["deadline"] is None, "patents: a private repo has disclosed nothing")
check(in_grace["state"] == "grace" and in_grace["deadline"] == "2027-01-21" and in_grace["days_left"] == 68,
      f"patents: a public repo has 12 months from its creation date ({in_grace['days_left']} days left)")
check(too_late["state"] == "closed" and too_late["days_left"] < 0, "patents: after 12 months the window is closed")
check(patents.window(False, "not a date", patent_today)["state"] == "unknown"
      and patents.window(False, None, patent_today)["state"] == "unknown",
      "patents: a missing creation date is reported as unknown, not guessed")
check(patents.add_months(patent_date(2024, 2, 29), 12) == patent_date(2025, 2, 28)
      and patents.add_months(patent_date(2026, 1, 31), 1) == patent_date(2026, 2, 28)
      and patents.add_months(patent_date(2026, 11, 14), 12) == patent_date(2027, 11, 14),
      "patents: 12 months later, also from 29 February and month ends")

check([patents.route(*case)["key"] for case in (
    ("look", False, True, False, "grace", "permissive"),
    ("unlikely", True, False, False, "open", "none"),
    ("unlikely", False, False, False, "closed", "permissive"),
    ("look", False, False, True, "grace", "none"),
    ("look", True, False, False, "open", "none"),
    ("maybe", False, False, False, "grace", "permissive"),
    ("look", False, False, False, "closed", "permissive"),
)] == ["none", "private", "licence", "none", "attorney", "attorney", "defensive"],
    "patents: route for a fork, private, public, archived, in grace and too late")
check([patents.licence_kind(key)[0] for key in ("", None, "mit", "Apache-2.0", "agpl-3.0")]
      == ["none", "none", "permissive", "permissive", "custom"], "patents: licence kinds")

rubric = patents.RUBRIC
earlier = [
    {"name": "a", "private": True, "band": "look", "rubric": rubric},
    {"name": "b", "private": False, "band": "unlikely", "rubric": rubric},
    {"name": "c", "private": False, "band": "unlikely", "rubric": "older"},
    "damaged",
]
later = [
    {"name": "a", "private": False, "band": "look", "rubric": rubric},
    {"name": "b", "private": False, "band": "maybe", "rubric": rubric},
    {"name": "c", "private": False, "band": "maybe", "rubric": rubric},
    {"name": "d", "private": False, "band": "look", "rubric": rubric},
    {"name": "e", "private": False, "band": "unlikely", "rubric": rubric},
]
check([(change["repo"], change["kind"]) for change in patents.changes_since(earlier, later)]
      == [("a", "public"), ("b", "band"), ("d", "new")],
      "patents: changes are a repo turned public, a band that moved and a new candidate; a new rubric is not a change")
check(patents.changes_since([], later) == [], "patents: the first scan reports no changes")

patent_scan = {
    "version": 1, "rubric": rubric, "scanned_at": "2026-11-09 09:00", "includes_private": True,
    "repos": [
        patents.build_entry(patent_repo("hinge-lab", hinge, "2026-05-01T00:00:00Z", private=True, licence=""), strong_text),
        patents.build_entry(patent_repo("pocket-rig", hinge, "2025-12-09T00:00:00Z"), strong_text),
        patents.build_entry(patent_repo("gear-rig", hinge, "2026-01-21T00:00:00Z"), rig_text),
        patents.build_entry(patent_repo("old-rig", hinge, "2024-01-01T00:00:00Z"), rig_text),
        patents.build_entry(patent_repo("work-rig", hinge, "2026-10-01T00:00:00Z"), rig_text),
        patents.build_entry(patent_repo("leaky", hinge, "2026-10-01T00:00:00Z"), marked_text),
        patents.build_entry(patent_repo("tile-game", "A cozy puzzle game for the browser.", "2026-03-01T00:00:00Z",
                                        licence="", pushed="2026-11-10T08:00:00Z"), game_text),
        patents.build_entry(patent_repo("borrowed", hinge, "2026-06-01T00:00:00Z", fork=True), marked_text),
        None,
        {"name": ""},
    ],
    "changes": [
        {"repo": "gear-rig", "kind": "band", "text": "Was unlikely, now maybe."},
        {"repo": "gone", "kind": "new", "text": "No longer in the scan."},
        "damaged",
    ],
}
patent_notes = {
    "old-rig": {"decision": "publish", "rating": "maybe", "reviewed": "2026-11-12", "verdict": "Prior art already."},
    "work-rig": {"owner": "employer?"},
    "tile-game": {"rating": "candidate", "reviewed": "2026-11-05"},
    "gear-rig": "damaged",
}
check(patents.view(None) == [] and patents.view({}) == [] and patents.view({"repos": "broken"}) == [],
      "patents: no scan or a damaged scan gives an empty list")
bare = patents.view({"repos": [None, {"name": ""}, {"name": "x", "band": "nonsense", "signals": "broken"}]}, "nonsense")
check(len(bare) == 1 and bare[0]["band"] == "unlikely" and bare[0]["score"] == 0 and bare[0]["window"]["state"] == "unknown"
      and bare[0]["signals"] == [], "patents: a half-written entry and damaged notes are tolerated")

patent_rows = patents.view(patent_scan, patent_notes, patent_today)
by_name = {row["name"]: row for row in patent_rows}
check([row["name"] for row in patent_rows]
      == ["leaky", "hinge-lab", "pocket-rig", "tile-game", "gear-rig", "old-rig", "work-rig", "borrowed"],
      f"patents: candidates first, then by score, forks last ({[row['name'] for row in patent_rows]})")
check(by_name["tile-game"]["band"] == "look" and by_name["tile-game"]["screen_band"] == "unlikely"
      and by_name["tile-game"]["rating"] == "candidate", "patents: a reviewed rating outranks the keyword band")
check(by_name["tile-game"]["changed_since_review"] and not by_name["old-rig"]["changed_since_review"]
      and not by_name["hinge-lab"]["changed_since_review"],
      "patents: a push after the review date is flagged")
check(by_name["work-rig"]["route"]["key"] == "employer" and by_name["work-rig"]["employer"]
      and by_name["borrowed"]["route"]["key"] == "none", "patents: a project made at work is routed to the employment agreement")
check(by_name["leaky"]["exposed"] and not by_name["hinge-lab"]["exposed"] and not by_name["borrowed"]["exposed"],
      "patents: a public repo that says it must stay private is reported as exposed")
check(by_name["old-rig"]["decision"] == "publish" and by_name["old-rig"]["decision_label"] == "publish defensively"
      and by_name["old-rig"]["route"]["key"] == "defensive" and by_name["gear-rig"]["decision"] == "",
      "patents: the recorded decision is shown; none is invented")
check(by_name["hinge-lab"]["licence_kind"] == "none" and by_name["pocket-rig"]["licence_kind"] == "permissive",
      "patents: licence shown per project")

patent_card = hub.patents_summary(patent_scan, patent_notes, patent_today)
check(patent_card["installed"] and patent_card["has_scan"] and patent_card["total"] == 7 and patent_card["includes_private"]
      and patent_card["counts"] == {"look": 4, "maybe": 3, "unlikely": 0, "forks": 1, "undecided": 6, "exposed": 1},
      f"hub patents: counts on the card ({patent_card['counts']})")
check(patent_card["next_deadline"] == {"name": "pocket-rig", "date": "2026-12-09", "days_left": 25},
      f"hub patents: the nearest filing date among undecided candidates ({patent_card['next_deadline']})")
check(patent_card["changes"] == [{"repo": "gear-rig", "kind": "band", "text": "Was unlikely, now maybe."}],
      "hub patents: changes kept only for projects still in the scan")
check("not legal advice" in patent_card["disclaimer"] and "not legal advice" in hub.PATENTS_DISCLAIMER
      and "prior art" in hub.PATENTS_DISCLAIMER, "hub patents: the card says it is a screen and not legal advice")
quiet_card = hub.patents_summary({**patent_scan, "repos": [entry for entry in patent_scan["repos"]
                                                           if isinstance(entry, dict) and entry.get("name") == "tile-game"]},
                                 {}, patent_today)
check(quiet_card["candidates"] == [] and [row["name"] for row in quiet_card["others"]] == ["tile-game"]
      and isinstance(quiet_card["others"][0]["route"], str), "hub patents: projects with no patent route are listed apart")
for damaged in (None, [], {"repos": "broken"}, "text"):
    empty_card = hub.patents_summary(damaged, None, patent_today)
    check(empty_card["installed"] and not empty_card["has_scan"] and empty_card["candidates"] == []
          and hub.patents_todo(empty_card) == [], f"hub patents: a missing or damaged scan gives an empty card ({damaged!r})")

patent_items = hub.patents_todo(patent_card)
check([(item["priority"], item["title"]) for item in patent_items] == [
    (1, "leaky says it must stay private, but the repo is public"),
    (1, "pocket-rig: 25 days left to file where a grace period applies"),
    (2, "gear-rig: 68 days left to file where a grace period applies"),
    (3, "3 projects worth an attorney look, no decision yet"),
], f"hub patents: to-dos by urgency ({[(item['priority'], item['title']) for item in patent_items]})")
check(all(item["module"] == "Patents" and item["action"] is None for item in patent_items)
      and patent_items[1]["links"] == [{"label": "Open the repository", "url": "https://example.test/pocket-rig", "note": ""}]
      and "2026-12-09" in patent_items[1]["detail"] and "old-rig" not in json.dumps(patent_items),
      "hub patents: to-dos name the date, link the repo and skip decided projects")
check(hub.patents_todo(None) == [] and hub.patents_todo({"installed": False}) == [], "hub patents: not installed, no to-dos")
check([entry["module"] for entry in hub.help_sections({}, None, None, patent_card)][-1] == "Patents"
      and "Patents" not in [entry["module"] for entry in hub.help_sections({}, None, None, {"installed": False})]
      and "Patents" not in [entry["module"] for entry in hub.help_sections({}, None, None)],
      "hub patents: help entry only where the screen is installed")

patent_home = Path(tempfile.mkdtemp(prefix="rp-patents-"))
patent_real = (patents.DATA_DIR, patents.SCAN_FILE, patents.OVERRIDES_FILE, patents.LOG_FILE)
try:
    patents.DATA_DIR = patent_home / "patents"
    patents.SCAN_FILE = patents.DATA_DIR / "scan.json"
    patents.OVERRIDES_FILE = patents.DATA_DIR / "overrides.json"
    patents.LOG_FILE = patent_home / "patents.log"
    check(hub.patents_section(patent_today) == {"installed": False}, "hub patents: no card where the screen is not installed")

    patents.DATA_DIR.mkdir(parents=True)
    waiting = hub.patents_section(patent_today)
    check(waiting["installed"] and not waiting["has_scan"], "hub patents: installed but not scanned yet shows an empty card")
    patents.SCAN_FILE.write_text("{ not json", encoding="utf-8")
    patents.OVERRIDES_FILE.write_text("[1, 2]", encoding="utf-8")
    broken = hub.patents_section(patent_today)
    check(broken["installed"] and not broken["has_scan"] and patents.load_overrides() == {},
          "hub patents: damaged files on disk do not break the dashboard")

    patents.SCAN_FILE.write_text(json.dumps(patent_scan), encoding="utf-8")
    patents.OVERRIDES_FILE.write_text(json.dumps(patent_notes), encoding="utf-8")
    check(hub.patents_section(patent_today)["counts"] == patent_card["counts"], "hub patents: the card reads the scan and the notes from disk")
    check(patents.decide("hinge-lab", "patent it").startswith("Unknown decision")
          and patents.decide("nowhere", "file") == "No project called 'nowhere' in the last scan."
          and json.loads(patents.OVERRIDES_FILE.read_text(encoding="utf-8")) == patent_notes,
          "patents: an unknown decision or project changes nothing")
    check(patents.decide("tile-game", "licence") == "tile-game: licence only."
          and patents.load_overrides()["tile-game"]["decision"] == "licence"
          and patents.load_overrides()["tile-game"]["rating"] == "candidate"
          and patents.load_overrides()["old-rig"] == patent_notes["old-rig"],
          "patents: a decision is saved next to the existing notes")
    check(hub.run_action("patents:decide:hinge-lab:file") == "hinge-lab: file."
          and patents.view(patent_scan, patents.load_overrides(), patent_today)[1]["decision"] == "file",
          "hub patents: the card's buttons record the decision")
    check(hub.run_action("patents:decide:hinge-lab:clear") == "hinge-lab: decision cleared."
          and "decision" not in patents.load_overrides()["hinge-lab"], "hub patents: a decision can be cleared")
    for refused in ("patents:decide:bad name:file", "patents:decide:../x:file", "patents:nonsense", "patents:decide:hinge-lab"):
        try:
            hub.run_action(refused)
            check(False, f"hub patents: malformed action refused ({refused})")
        except ValueError:
            check(True, f"hub patents: malformed action refused ({refused})")

    # A scan whose GitHub sign-in does not answer sees public repos only. It must not forget the private ones.
    scan_real = (patents.list_repos, patents.read_readme)
    try:
        public_listing = [patent_repo("pocket-rig", hinge, "2025-12-09T00:00:00Z"),
                          patent_repo("gear-rig", hinge, "2026-01-21T00:00:00Z")]
        private_repo = patent_repo("hinge-lab", hinge, "2026-05-01T00:00:00Z", private=True, licence="")
        patents.SCAN_FILE.write_text(json.dumps(patent_scan), encoding="utf-8")
        patents.read_readme = lambda full_name: strong_text
        patents.list_repos = lambda user: (public_listing, False)
        outcome = patents.scan("someone")
        saved = json.loads(patents.SCAN_FILE.read_text(encoding="utf-8"))
        found = {entry["name"]: entry for entry in saved["repos"]}
        check(outcome["ok"] and sorted(found) == ["gear-rig", "hinge-lab", "pocket-rig"]
              and found["hinge-lab"]["private"] and found["hinge-lab"]["stale"] is True
              and found["hinge-lab"]["score"] == patent_scan["repos"][0]["score"]
              and saved["includes_private"] and saved["signed_in"] is False
              and "1 private project(s) kept from the last scan" in outcome["message"],
              "patents: a scan that cannot sign in keeps the private projects of the last scan, marked as not read again")
        check("stale" not in found["pocket-rig"] and "stale" not in found["gear-rig"] and saved["changes"] == [],
              "patents: the public projects of that scan are read as usual and nothing is reported as changed")
        check(hub.patents_section(patent_today)["signed_in"] is False and hub.patents_section(patent_today)["includes_private"],
              "hub patents: the card knows the private projects are from an earlier scan")
        check(not list(patents.DATA_DIR.glob("*.tmp")), "patents: the scan is saved through a temporary file that does not stay")

        patents.scan("someone")
        again = json.loads(patents.SCAN_FILE.read_text(encoding="utf-8"))
        check([entry["name"] for entry in again["repos"] if entry.get("stale")] == ["hinge-lab"],
              "patents: a second scan without a sign-in still keeps them")

        patents.list_repos = lambda user: (public_listing + [private_repo], True)
        outcome = patents.scan("someone")
        saved = json.loads(patents.SCAN_FILE.read_text(encoding="utf-8"))
        found = {entry["name"]: entry for entry in saved["repos"]}
        check(saved["signed_in"] is True and "stale" not in found["hinge-lab"] and found["hinge-lab"]["private"]
              and "sign-in" not in outcome["message"] and hub.patents_section(patent_today)["signed_in"] is True,
              "patents: once the sign-in answers again the private project is current")

        patents.list_repos = lambda user: (public_listing, True)
        patents.scan("someone")
        check("hinge-lab" not in [entry["name"] for entry in json.loads(patents.SCAN_FILE.read_text(encoding="utf-8"))["repos"]],
              "patents: a project that is really gone (signed in, not listed) leaves the scan")

        before = {entry["name"]: entry for entry in json.loads(patents.SCAN_FILE.read_text(encoding="utf-8"))["repos"]}
        patents.list_repos = lambda user: ([patent_repo("pocket-rig", hinge, "2025-12-09T00:00:00Z", pushed="2026-11-12T08:00:00Z"),
                                            public_listing[1]], True)
        patents.read_readme = lambda full_name: ""
        outcome = patents.scan("someone")
        found = {entry["name"]: entry for entry in json.loads(patents.SCAN_FILE.read_text(encoding="utf-8"))["repos"]}
        check(found["pocket-rig"]["stale"] is True and found["pocket-rig"]["score"] == before["pocket-rig"]["score"] > 0
              and found["pocket-rig"]["pushed"] == before["pocket-rig"]["pushed"]
              and "1 README(s) could not be read" in outcome["message"],
              "patents: a README that cannot be read does not wipe the last reading, and is tried again next time")
        patents.read_readme = lambda full_name: rig_text
        patents.scan("someone")
        found = {entry["name"]: entry for entry in json.loads(patents.SCAN_FILE.read_text(encoding="utf-8"))["repos"]}
        check("stale" not in found["pocket-rig"] and found["pocket-rig"]["pushed"] == "2026-11-12T08:00:00Z"
              and found["pocket-rig"]["score"] != before["pocket-rig"]["score"],
              "patents: when the README answers again the project is read afresh")
    finally:
        patents.list_repos, patents.read_readme = scan_real
finally:
    patents.DATA_DIR, patents.SCAN_FILE, patents.OVERRIDES_FILE, patents.LOG_FILE = patent_real


class GhAnswer:
    def __init__(self, returncode, stdout=b"", stderr=b""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


gh_real = (gh.run_gh, gh.system_proxy, gh.public_api, gh._GH_BROKEN, gh._GH_ENV)
proxy_real = os.environ.get("HTTPS_PROXY")
try:
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:9"
    gh_calls = []

    def proxy_down_gh(args, environment):
        gh_calls.append(environment)
        if environment is None:
            return GhAnswer(1, stderr=b"connectex: No connection could be made because the target machine actively refused it.")
        return GhAnswer(0, stdout=b'[{"name": "hinge-lab", "private": true}]')

    gh.run_gh = proxy_down_gh
    gh.public_api = lambda path, raw=False: "public only"
    gh.system_proxy = lambda: "http://proxy.example.test:8080"
    gh._GH_BROKEN, gh._GH_ENV = False, None
    check(gh.gh_api("user/repos") == [{"name": "hinge-lab", "private": True}] and len(gh_calls) == 2
          and gh_calls[1]["HTTPS_PROXY"] == "http://proxy.example.test:8080" and not gh._GH_BROKEN,
          "github: when the proxy named in the environment is down, gh is tried once through the computer's own proxy")
    gh_calls.clear()
    check(gh.gh_api("user/repos") == [{"name": "hinge-lab", "private": True}] and len(gh_calls) == 1
          and gh_calls[0]["HTTPS_PROXY"] == "http://proxy.example.test:8080",
          "github: the proxy that worked is remembered for the rest of the run")

    gh._GH_BROKEN, gh._GH_ENV = False, None
    gh.system_proxy = lambda: "http://127.0.0.1:9/"
    gh_calls.clear()
    check(gh.gh_api("user/repos") == "public only" and len(gh_calls) == 1 and gh._GH_BROKEN,
          "github: no second try when the computer's proxy is the one already in use; the public answer is used")
    gh._GH_BROKEN, gh._GH_ENV = False, None
    gh.system_proxy = lambda: ""
    gh_calls.clear()
    check(gh.gh_api("user/repos") == "public only" and len(gh_calls) == 1 and gh._GH_BROKEN,
          "github: no second try where the computer has no proxy setting")

    gh._GH_BROKEN, gh._GH_ENV = False, None
    gh.system_proxy = lambda: "http://proxy.example.test:8080"
    gh_calls.clear()
    gh.run_gh = lambda args, environment: (gh_calls.append(environment), GhAnswer(1, stderr=b"gh: Not Found (HTTP 404)"))[1]
    check(gh.gh_api("repos/someone/none/readme", raw=True) == "public only" and len(gh_calls) == 1 and not gh._GH_BROKEN,
          "github: 'not found' is an answer, not a broken connection")
    gh.run_gh = lambda args, environment: None
    check(gh.gh_api("user/repos") == "public only" and gh._GH_BROKEN, "github: gh missing or timing out falls back to the public answer")
finally:
    gh.run_gh, gh.system_proxy, gh.public_api, gh._GH_BROKEN, gh._GH_ENV = gh_real
    if proxy_real is None:
        os.environ.pop("HTTPS_PROXY", None)
    else:
        os.environ["HTTPS_PROXY"] = proxy_real

patent_page = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
check(all(piece in patent_page for piece in ("function patentCard", "patents_run_now", "why-row", "patents:decide:")),
      "dashboard: the Patent screen card, its Scan now button and its decision buttons are present")
patent_source = (Path(__file__).parent / "patents.py").read_bytes()
check(not patent_source.startswith(b"\xef\xbb\xbf") and "\u00c3" not in patent_source.decode("utf-8")
      and "\u00e2\u20ac" not in patent_source.decode("utf-8"), "patents.py: clean UTF-8, no byte-order mark")
check("data/patents/" in (Path(__file__).parent / ".gitignore").read_text(encoding="utf-8").split(),
      "patents: scan results are git-ignored")
check(("patents" in hub.TASKS) == (hub.ROOT / "data" / "patents").exists(),
      "hub patents: the daily task is listed only where the screen is installed")

# Jobs tab, Auto-apply card (hub.auto_apply_view): what the scheduled auto-apply did, read from its log and its queue.
# Offline, made-up companies, files in a temporary folder.
AUTO_APPLY_LOG = """Auto-apply 2030-01-02 08:15 (dry run)
  trying        100 Northwind - Test Engineer (Springfield)
  ready         100 Northwind - Test Engineer (Springfield): rehearsal reached Review
Submitted 0 application(s).

Auto-apply 2030-01-02 13:15
  left for you   90 Initech - Design Engineer: needs a sign-in; no saved account
  trying        100 Northwind - Test Engineer (Springfield)
  APPLIED       100 Northwind - Test Engineer (Springfield)  proof: proofs/northwind.png
  trying         95 Contoso - Mechanical Engineer (Shelbyville)
  needs you      95 Contoso - Mechanical Engineer (Shelbyville): 'Application Questions' needs: Notice period
  (2 job(s) stay queued: company limit per run)
Submitted 1 application(s).

Auto-apply 2030-01-02 19:15
  left for you  100 Globex - Graduate Engineer: title does not fit (graduate / staff level)
  left for you  100 Globex - Staff Engineer: title does not fit (graduate / staff level)
  left for you  100 Globex - Staff Engineer: title does not fit (graduate / staff level)
  left for you   90 Initech - Design Engineer: needs a sign-in; no saved account
  trying        100 Contoso - Mechanical Engineer (Shelbyville)
Auto-apply CRASHED 2030-01-02 19:40
Traceback (most recent call last):
  File "auto_apply.py", line 1, in <module>
TimeoutError: BrowserType.launch: Timeout 180000ms exceeded.
"""
with tempfile.TemporaryDirectory() as auto_apply_tmp:
    auto_apply_folder = Path(auto_apply_tmp)
    check(hub.auto_apply_view(auto_apply_folder) is None, "hub auto-apply: no log, no card")
    auto_apply_log = auto_apply_folder / "logs" / "auto_apply.log"
    auto_apply_log.parent.mkdir()
    auto_apply_log.write_text(AUTO_APPLY_LOG.split("\n\n")[0] + "\n", encoding="utf-8")
    check(hub.auto_apply_view(auto_apply_folder) is None, "hub auto-apply: a dry run alone gives no card")
    auto_apply_log.write_text(AUTO_APPLY_LOG, encoding="utf-8")
    (auto_apply_folder / "digest_apply_state.json").write_text(json.dumps({
        "a": {"company": "Globex", "title": "Process Engineer", "status": "queued"},
        "b": {"company": "Globex", "title": "Test Engineer"},
        "c": {"company": "Initech", "title": "Design Engineer", "status": "queued"},
        "d": {"company": "Northwind", "title": "Test Engineer", "status": "applied"},
        "e": {"company": "Contoso", "title": "Mechanical Engineer", "status": "needs_you",
              "detail": "Notice period", "url": "https://example.com/job/1"},
        "f": {"company": "Umbrella", "title": "Quality Engineer", "status": "skipped", "date": "2030-01-02",
              "detail": "the posting is closed", "url": "https://example.com/job/2"},
        "g": {"company": "Umbrella", "title": "Hardware Engineer", "status": "applied", "date": "2030-01-02",
              "detail": "submitted on the careers site", "url": "https://example.com/job/3"},
        "h": {"company": "Hooli", "title": "Reliability Engineer", "status": "already", "date": "2030-01-01"},
        "i": {"company": "Hooli", "title": "Process Engineer", "status": "later", "date": "2030-01-02"},
    }), encoding="utf-8")
    auto_view = hub.auto_apply_view(auto_apply_folder)
    crashed_run, finished_run = auto_view["runs"]
    check([run["when"] for run in auto_view["runs"]] == ["2030-01-02 19:15", "2030-01-02 13:15"],
          "hub auto-apply: real runs only, newest first (the dry run is left out)")
    check(crashed_run["state"] == "crashed" and crashed_run["crashed"] == "2030-01-02 19:40"
          and crashed_run["submitted"] is None and not any(crashed_run["counts"].values()),
          f"hub auto-apply: a crash closes the run it interrupted and the traceback is not read as results ({crashed_run})")
    check(finished_run["state"] == "finished" and finished_run["submitted"] == 1 and finished_run["left"] == 1
          and finished_run["counts"] == {"applied": 1, "needs_you": 1, "later": 0, "already": 0, "ready": 0}
          and finished_run["notes"] == ["2 job(s) stay queued: company limit per run"],
          f"hub auto-apply: a finished run with its counts and its note ({finished_run})")
    check([(row["kind"], row["job"], row["detail"]) for row in auto_view["results"]] == [
        ("applied", "Northwind - Test Engineer (Springfield)", "proof: proofs/northwind.png"),
        ("needs_you", "Contoso - Mechanical Engineer (Shelbyville)", "'Application Questions' needs: Notice period"),
    ], f"hub auto-apply: each result names the job and how it ended ({auto_view['results']})")
    check(auto_view["left"] == [
        {"reason": "title does not fit (graduate / staff level)", "jobs": ["Globex - Graduate Engineer", "Globex - Staff Engineer"]},
        {"reason": "needs a sign-in", "jobs": ["Initech - Design Engineer"]},
    ] and crashed_run["left"] == 3, f"hub auto-apply: jobs left for you, grouped by reason, each job once ({auto_view['left']})")
    check(auto_view["digest"]["counts"] == {"queued": 3, "applied": 2, "needs_you": 1, "skipped": 1, "already": 1, "later": 1}
          and auto_view["digest"]["queued"] == [{"company": "Globex", "titles": ["Process Engineer", "Test Engineer"]},
                                                {"company": "Initech", "titles": ["Design Engineer"]}]
          and auto_view["digest"]["needs_you"] == [{"company": "Contoso", "title": "Mechanical Engineer",
                                                    "detail": "Notice period", "url": "https://example.com/job/1"}],
          f"hub auto-apply: the queue by company and the jobs that need you ({auto_view['digest']})")
    check([(job["date"], job["status"], job["company"], job["title"]) for job in auto_view["digest"]["done"]] == [
        ("2030-01-02", "applied", "Umbrella", "Hardware Engineer"),
        ("2030-01-02", "skipped", "Umbrella", "Quality Engineer"),
        ("2030-01-01", "already", "Hooli", "Reliability Engineer"),
        ("", "applied", "Northwind", "Test Engineer"),
    ] and auto_view["digest"]["done"][1]["detail"] == "the posting is closed"
          and auto_view["digest"]["done"][0]["url"] == "https://example.com/job/3",
          "hub auto-apply: settled jobs of the mail come from the queue file, newest day first, applied first within a day, "
          f"with the reason and the link; none of them is in the log of the runs ({auto_view['digest']['done']})")
    (auto_apply_folder / "digest_apply_state.json").write_text("{not json", encoding="utf-8")
    auto_apply_log.write_text("Auto-apply 2030-01-03 08:15\n  trying        100 Contoso - Mechanical Engineer (Shelbyville)\n",
                              encoding="utf-8")
    open_run = hub.auto_apply_view(auto_apply_folder)
    check(open_run["runs"][0]["state"] == "running"
          and open_run["digest"] == {"counts": {}, "queued": [], "needs_you": [], "done": []},
          "hub auto-apply: no closing line and a fresh log means still running; a damaged queue file gives an empty queue")
    os.utime(auto_apply_log, (0, 0))
    check(hub.auto_apply_view(auto_apply_folder)["runs"][0]["state"] == "stopped",
          "hub auto-apply: no closing line and an old log means it was cut off")
jobs_page = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
check("function autoApplyCard" in jobs_page and "autoApplyCard(j.auto_apply)" in jobs_page,
      "dashboard: the Jobs tab shows the Auto-apply card")
check("digest.done" in jobs_page and "From the Big tech mail" in jobs_page,
      "dashboard: the Auto-apply card lists the settled jobs of the Big tech mail (digest.done)")

# patent desk (patent_desk.py): readiness, stages, the dates after a filing, history, the package, the daily run.
# Offline, made-up projects, no PDF (Edge is not on the CI runner) and no folder is opened.
import patent_desk  # noqa: E402

full_dossier = {
    "title": "Friction hinge with a printed torque limiter",
    "field": "Hinges with a set holding torque.",
    "problem": ("A friction hinge loses its holding torque as the bearing wears, and the usual fix is a metal spring "
                "washer that has to be chosen per batch and cannot be printed with the part."),
    "mechanism": ["Print a wave spring in the hinge knuckle.", "Split the bearing so the spring can close it.",
                  "Measure the torque curve of the assembled hinge.",
                  "Fit the spring compliance to that curve and set the preload."],
    "variants": ["A moulded spring with a rate < 2 N/mm instead of a printed one."],
    "effects": [{"result": "Holding torque within about 4 percent over the test.", "source": "bench/torque.csv"},
                {"result": "Feels smoother."}],
    "example": {"steps": ["Print the knuckle.", "Assemble and measure."], "output": "torque 0.42 N m"},
    "figures": [{"title": "Section through the knuckle", "file": "fig1.svg"}, {"title": "Not drawn yet", "file": "fig9.svg"}],
    "claims": [
        "A hinge comprising a split bearing and a printed wave spring that preloads it.",
        "The hinge of claim 1, wherein the spring is printed in one piece with the knuckle.",
        "The hinge of claim 1, wherein the preload is set from a measured torque curve.",
        "The hinge according to any one of claims 1 to 3, wherein the bearing is split along its axis.",
    ],
    "prior_art": [
        {"reference": "Spring washer hinge", "url": "https://example.test/a", "difference": "A separate metal washer."},
        {"reference": "Detent hinge", "difference": "Holds at fixed angles only."},
        {"reference": "Clutch hinge", "difference": "A wrapped band, not a split bearing."},
        {"reference": "Listed without a difference"},
    ],
    "abstract": "A hinge whose split bearing is preloaded by a printed wave spring.",
    "inventors": [{"given": "Ada", "family": "Example", "residence": "Springfield, Nowhere"}],
    "questions": ["Is the fitted preload a method claim or a product claim?"],
}
partial_dossier = {"problem": full_dossier["problem"], "mechanism": full_dossier["mechanism"]}


def desk_points(checks):
    return sum(entry["points"] for entry in checks if entry["ok"])


def desk_found(checks, key):
    return next(entry for entry in checks if entry["key"] == key)


ready_checks = patent_desk.readiness(full_dossier, {"state": "success"}, lambda file: file == "fig1.svg")
check(len(ready_checks) == 8 and sum(entry["points"] for entry in ready_checks) == 100 and desk_points(ready_checks) == 100,
      "patent desk: eight checks, 100 points, a complete dossier passes them all")
check([desk_found(ready_checks, key)["found"] for key in ("effect", "figures", "claims", "prior_art")]
      == ["1 with a source, 1 without", "1 of 2 listed file(s) found", "1 independent, 3 dependent", "3 with a stated difference"],
      "patent desk: each check says what it found (a result without a source and a figure that is not on disk do not count)")
check(desk_points(patent_desk.readiness(None)) == 0 and desk_points(patent_desk.readiness("damaged", "damaged")) == 0
      and all(entry["fix"] for entry in patent_desk.readiness(None)),
      "patent desk: no dossier scores 0 and every open check says what to do")
check(desk_points(patent_desk.readiness(patent_desk.skeleton({"name": "x"}), {"state": "success"})) == 10,
      "patent desk: an empty dossier with green CI scores 10, tests only")
local_evidence = {"command": "python -m pytest", "date": "2026-11-10", "passed": 41, "failed": 0}


def desk_tests(evidence, state):
    ci = {"state": state} if state else {}
    return desk_found(patent_desk.readiness({"tests_evidence": evidence}, ci, None, patent_today), "tests")


check(desk_tests(local_evidence, "none")["ok"]
      and desk_tests(local_evidence, "none")["found"]
      == "no test runs on GitHub; local run on 2026-11-10: 41 passed, 0 failed (python -m pytest)"
      and desk_tests(None, "success")["ok"] and desk_tests(None, "success")["found"] == "last CI run passed",
      "patent desk: tests count with a green CI run, or with a recorded local run when GitHub has no test runs")
check(not desk_tests(local_evidence, "failure")["ok"] and "does not count while CI fails" in desk_tests(local_evidence, "failure")["found"]
      and not desk_tests(local_evidence, None)["ok"] and "once GitHub has answered" in desk_tests(local_evidence, None)["found"]
      and not desk_tests(local_evidence, "something else")["ok"]
      and not desk_tests(None, "failure")["ok"] and not desk_tests(None, "none")["ok"],
      "patent desk: a local run never outweighs a failed CI run and does not count while the CI state is unknown")
check(not desk_tests("pytest, 2026-11-10, 41 passed", "none")["ok"]
      and "a sentence, not a run" in desk_tests("pytest, 2026-11-10, 41 passed", "none")["found"]
      and not desk_tests("pytest, 2026-11-10, 41 passed", "failure")["ok"],
      "patent desk: a sentence about the tests is not a test run")
check(all(not desk_tests({**local_evidence, **change}, "none")["ok"] for change in (
    {"failed": 1}, {"passed": 0}, {"command": ""}, {"date": ""}, {"date": "2026-11-15"}, {"date": "soon"}, {"date": 20261110},
    {"passed": "41"}, {"passed": True}, {"failed": None}, {"failed": False}))
      and "incomplete or has failures" in desk_tests({**local_evidence, "failed": 1}, "none")["found"]
      and desk_tests({**local_evidence, "date": "2026-11-14"}, "none")["ok"]
      and patent_desk.local_run(local_evidence, patent_today) == local_evidence
      and patent_desk.local_run(["damaged"], patent_today) is None
      and patent_desk.skeleton({"name": "x"})["tests_evidence"] == {"command": "", "date": "", "passed": 0, "failed": 0}
      and desk_tests(patent_desk.skeleton({"name": "x"})["tests_evidence"], "none")["found"] == "no test runs on GitHub",
      "patent desk: a local run needs a command, a date that is not in the future, at least one pass and no failure")


def workflow_run(name, path, conclusion, created, event="push", branch="main", workflow_id=None):
    return {"name": name, "path": path, "conclusion": conclusion, "created_at": created, "event": event,
            "head_branch": branch, "workflow_id": workflow_id or path, "html_url": f"https://example.test/{created}"}


ci_run = workflow_run("CI", ".github/workflows/ci.yml", "success", "2026-11-10T08:00:00Z")
pages_run = workflow_run("pages-build-deployment", "dynamic/pages/pages-build-deployment", "success", "2026-11-13T08:00:00Z")
deploy_run = workflow_run("Deploy site", ".github/workflows/pages.yml", "success", "2026-11-13T09:00:00Z")
check(patent_desk.summarise_runs([pages_run, deploy_run], "main") == {"state": "none"}
      and patent_desk.summarise_runs([pages_run, deploy_run, ci_run], "main")
      == {"state": "success", "url": "https://example.test/2026-11-10T08:00:00Z", "workflows": ["CI"]},
      "patent desk: a Pages build or a deploy is not a test run; the check names the workflows it counted")
failed_ci = workflow_run("CI", ".github/workflows/ci.yml", "failure", "2026-11-12T08:00:00Z")
lint_run = workflow_run("Lint", ".github/workflows/lint.yml", "success", "2026-11-13T10:00:00Z")
check(patent_desk.summarise_runs([deploy_run, lint_run, failed_ci, ci_run], "main")
      == {"state": "failure", "url": "https://example.test/2026-11-12T08:00:00Z", "workflows": ["CI", "Lint"]}
      and patent_desk.summarise_runs([ci_run, failed_ci], "main")["state"] == "failure"
      and patent_desk.summarise_runs([failed_ci, workflow_run("CI", ".github/workflows/ci.yml", "success", "2026-11-13T08:00:00Z")],
                                     "main")["state"] == "success",
      "patent desk: the newest run of each workflow counts, and a newer deploy or lint success does not hide a failed test run")
check(patent_desk.summarise_runs([ci_run, workflow_run("CI", ".github/workflows/ci.yml", "skipped", "2026-11-13T08:00:00Z"),
                                  workflow_run("CI", ".github/workflows/ci.yml", "cancelled", "2026-11-13T09:00:00Z")],
                                 "main")["state"] == "success"
      and patent_desk.summarise_runs([workflow_run("CI", ".github/workflows/ci.yml", "skipped", "2026-11-13T08:00:00Z")], "main")
      == {"state": "none"}
      and patent_desk.summarise_runs([workflow_run("Bench", ".github/workflows/bench.yml", "success", "2026-11-13T08:00:00Z",
                                                   event="workflow_dispatch")], "main") == {"state": "none"}
      and patent_desk.summarise_runs([workflow_run("CI", ".github/workflows/ci.yml", "success", "2026-11-13T08:00:00Z",
                                                   branch="agent/try")], "main") == {"state": "none"}
      and patent_desk.summarise_runs([workflow_run("Nightly tests", ".github/workflows/nightly.yml", "failure",
                                                   "2026-11-13T08:00:00Z", event="schedule")], "main")["state"] == "failure"
      and patent_desk.summarise_runs([workflow_run("Release checks", ".github/workflows/release.yml", "failure",
                                                   "2026-11-13T08:00:00Z")], "main")["state"] == "failure",
      "patent desk: skipped, cancelled, manual and other-branch runs are not counted; a scheduled test run and a release check are")
check(all(patent_desk.summarise_runs(damaged, "main") == {"state": "none"} for damaged in (None, [], "damaged", [None, "x", 3], [{}], {}))
      and desk_tests(None, "success")["ok"]
      and desk_found(patent_desk.readiness({}, {"state": "success", "workflows": ["CI", "Lint"]}), "tests")["found"]
      == "last CI run passed (CI, Lint)",
      "patent desk: empty or damaged run lists give no test runs; the tests check says which workflows it counted")


def results_heading(effects):
    blocks = patent_desk.specification("x", {"effects": effects}, True)
    position = next(index for index, block in enumerate(blocks) if block[0] == "h2" and block[1].endswith("esults"))
    return blocks[position][1], blocks[position + 1][1]


sourced = {"result": "Holds 0.42 N m.", "source": "bench/torque.csv"}
check(results_heading([{**sourced, "kind": "measured"}]) == ("Measured results", ["Holds 0.42 N m. (source: bench/torque.csv)"])
      and results_heading([{**sourced, "kind": "Simulated"}, {"result": "Detects 9 of 10.", "kind": "simulated"}])[0] == "Simulated results"
      and results_heading([{**sourced, "kind": "computed"}])[0] == "Computed results",
      "patent desk: the specification says measured, simulated or computed only when every result says so and they agree")
check(results_heading([{**sourced, "kind": "measured"}, {**sourced, "kind": "simulated"}])
      == ("Results", ["Measured: Holds 0.42 N m. (source: bench/torque.csv)", "Simulated: Holds 0.42 N m. (source: bench/torque.csv)"])
      and results_heading([sourced]) == ("Results", ["Holds 0.42 N m. (source: bench/torque.csv)"])
      and results_heading([{**sourced, "kind": "measured"}, sourced])
      == ("Results", ["Measured: Holds 0.42 N m. (source: bench/torque.csv)", "Holds 0.42 N m. (source: bench/torque.csv)"])
      and results_heading([{**sourced, "kind": "guessed"}])[0] == "Results"
      and "Measured results" not in json.dumps(patent_desk.specification("x", full_dossier, True)),
      "patent desk: results that are mixed, unlabelled or labelled with something else are headed Results, never Measured")
check(desk_found(patent_desk.readiness({"effects": [{**sourced, "kind": "measured"}, sourced, {"result": "Feels smoother."}]}),
                 "effect")["found"] == "2 with a source, 1 without (1 measured, 1 not labelled)"
      and desk_found(patent_desk.readiness({"effects": [{**sourced, "kind": "simulated"}, {**sourced, "kind": "computed"}]}),
                     "effect")["found"] == "2 with a source (1 simulated, 1 computed)"
      and desk_found(patent_desk.readiness({"effects": [sourced]}), "effect")["found"] == "1 with a source"
      and desk_found(patent_desk.readiness({}), "effect")["label"] == "Result with a source",
      "patent desk: the result check counts how each sourced result was obtained when the dossier says")
check(not desk_found(patent_desk.readiness({"problem": "x" * 119}), "problem")["ok"]
      and desk_found(patent_desk.readiness({"problem": "x" * 120}), "problem")["ok"]
      and not desk_found(patent_desk.readiness({"mechanism": ["a", "b", "c", ""]}), "mechanism")["ok"]
      and not desk_found(patent_desk.readiness({"claims": full_dossier["claims"][:3]}), "claims")["ok"]
      and not desk_found(patent_desk.readiness({"prior_art": full_dossier["prior_art"][:2]}), "prior_art")["ok"]
      and not desk_found(patent_desk.readiness({"effects": [{"result": "Faster."}]}), "effect")["ok"],
      "patent desk: thresholds (120 characters, 4 steps, 1 + 3 claims, 3 prior-art entries, a sourced result)")
check(patent_desk.split_claims(full_dossier["claims"]) == (full_dossier["claims"][:1], full_dossier["claims"][1:])
      and patent_desk.split_claims("damaged") == ([], []), "patent desk: dependent claims are the ones that refer to a claim")

desk_notes = {**patent_notes, "tile-game": {**patent_notes["tile-game"], "filed": {"date": "2026-01-10", "number": "63/123,456"}}}
desk_rows = patents.view(patent_scan, desk_notes, patent_today)
desk_dossiers = {"hinge-lab": full_dossier, "pocket-rig": partial_dossier, "work-rig": full_dossier, "old-rig": full_dossier}
desk_facts = {
    "hinge-lab": {"ci": {"state": "success"}, "package": {"built": "2026-11-13", "pdf": True, "draft": False}},
    "pocket-rig": {"ci": {"state": "failure"}},
    "leaky": "damaged",
}


def desk_figure(name, file):
    return (name, file) in (("hinge-lab", "fig1.svg"), ("pocket-rig", "fig1.svg"))


desk = patent_desk.build(desk_rows, desk_notes, desk_dossiers, desk_facts, patent_today, None, desk_figure)
on_desk = {item["name"]: item for item in desk["projects"]}
check([(item["name"], item["stage"], item["score"]) for item in desk["projects"]] == [
    ("hinge-lab", "ready", 100), ("tile-game", "filed", 0), ("pocket-rig", "strengthen", 25),
    ("gear-rig", "disclosure", 0), ("leaky", "disclosure", 0), ("work-rig", "ownership", 80),
], f"patent desk: one stage per project, ready first ({[(item['name'], item['stage'], item['score']) for item in desk['projects']]})")
check(desk["total"] == 6 and desk["counts"] == {"filed": 1, "closed": 0, "ownership": 1, "disclosure": 2, "strengthen": 1, "ready": 1}
      and "old-rig" not in on_desk and "borrowed" not in on_desk,
      "patent desk: follows candidates only; a decided project and a fork are not on it")
check(on_desk["hinge-lab"]["deadline"] is None and on_desk["hinge-lab"]["package"] == {"built": "2026-11-13", "pdf": True, "draft": False}
      and on_desk["pocket-rig"]["package"] is None and on_desk["pocket-rig"]["ci"] == "failure" and on_desk["leaky"]["ci"] == "unknown",
      "patent desk: a private project has no date; package and CI state come from the last run")
check(on_desk["pocket-rig"]["next_step"].startswith("Add one result and name the file or command")
      and on_desk["pocket-rig"]["deadline"] == {"label": "12-month date of the public repository", "date": "2026-12-09", "days_left": 25}
      and (on_desk["pocket-rig"]["passed"], on_desk["pocket-rig"]["total"]) == (2, 8),
      "patent desk: a half-written dossier names its first gap and keeps the public repository's date")
check(on_desk["tile-game"]["filed"] == {"date": "2026-01-10", "number": "63/123,456"}
      and on_desk["tile-game"]["deadline"] == {"label": "non-provisional or PCT application due", "date": "2027-01-10", "days_left": 57}
      and "cannot be extended" in on_desk["tile-game"]["next_step"],
      f"patent desk: a recorded filing starts the 12 months for the next application ({on_desk['tile-game']['deadline']})")
check(on_desk["work-rig"]["employer"] and not on_desk["work-rig"]["ownership_cleared"]
      and "employment agreement" in on_desk["work-rig"]["next_step"] and on_desk["gear-rig"]["stage_label"] == "needs an invention disclosure",
      "patent desk: a project made at work waits on ownership however complete its dossier is")

cleared_notes = {**desk_notes, "work-rig": {"owner": "employer?", "ownership_cleared": "2026-11-14"}}
cleared = patent_desk.build(patents.view(patent_scan, cleared_notes, patent_today), cleared_notes, desk_dossiers, {}, patent_today)
cleared_rig = next(item for item in cleared["projects"] if item["name"] == "work-rig")
check(cleared_rig["stage"] == "strengthen" and cleared_rig["score"] == 80 and cleared_rig["ownership_cleared"]
      and cleared_rig["next_step"].startswith("Get the repository's CI run green"),
      "patent desk: once ownership is cleared the same dossier is scored and its first gap named")
closed_notes = {"old-rig": {"rating": "maybe"}}
closed = patent_desk.build(patents.view(patent_scan, closed_notes, patent_today), closed_notes, desk_dossiers, {}, patent_today)
closed_rig = next(item for item in closed["projects"] if item["name"] == "old-rig")
check(closed_rig["stage"] == "closed" and closed_rig["deadline"] is None and "nothing to file" in closed_rig["next_step"]
      and closed["projects"][-1]["name"] == "old-rig",
      "patent desk: public for over 12 months means window closed, whatever the dossier says")
late_notes = {"hinge-lab": {"filed": {"date": "2025-10-01", "number": "63/000,001"}}, "pocket-rig": {"filed": {"date": "soon"}}}
late = patent_desk.build(patents.view(patent_scan, late_notes, patent_today), late_notes, desk_dossiers, {}, patent_today)
late_lab = next(item for item in late["projects"] if item["name"] == "hinge-lab")
check(late_lab["stage"] == "filed" and late_lab["deadline"]["date"] == "2026-10-01" and late_lab["deadline"]["days_left"] == -44
      and "ended on 2026-10-01" in late_lab["next_step"]
      and next(item for item in late["projects"] if item["name"] == "pocket-rig")["filed"] is None,
      "patent desk: a missed 12 months is said plainly; a filing without a real date is ignored")
check(patent_desk.build([], None, None, None, patent_today) == {
    "projects": [], "counts": {stage: 0 for stage in patent_desk.STAGES}, "total": 0, "history": [], "note": patent_desk.NOTE}
      and patent_desk.build(desk_rows, "damaged", "damaged", "damaged", patent_today)["total"] == 6
      and "not legal advice" in patent_desk.NOTE and "files nothing" in patent_desk.NOTE and patent_desk.FOLLOW_UP_MONTHS == 12,
      "patent desk: an empty or damaged input gives an empty desk; the note says it files nothing")

desk_before = [{"name": "hinge-lab", "stage": "strengthen", "score": 75}, {"name": "pocket-rig", "stage": "strengthen", "score": 25},
               {"name": "gone", "stage": "disclosure", "score": 0}, "damaged"]
desk_moves = patent_desk.changes(desk_before, desk["projects"], patent_today)
check([(move["name"], move["text"]) for move in desk_moves if move["name"] in ("hinge-lab", "pocket-rig", "gone", "work-rig")] == [
    ("hinge-lab", "strengthening -> ready to file."), ("hinge-lab", "Readiness 75 -> 100 of 100."),
    ("work-rig", "On the desk: waiting on ownership, readiness 80 of 100."),
    ("gone", "Left the desk (decided, or no longer rated as a candidate)."),
] and len(desk_moves) == 7 and {move["date"] for move in desk_moves} == {"2026-11-14"},
    "patent desk: history records a stage that moved, a readiness that moved, a project that joined and one that left")
check(patent_desk.changes(desk["projects"], desk["projects"], patent_today) == [], "patent desk: nothing moved, nothing recorded")
long_history = [{"date": "2026-11-01", "name": "x", "text": str(number)} for number in range(70)] + ["damaged"]
kept_history = patent_desk.build([], None, None, None, patent_today, long_history)["history"]
check(len(kept_history) == patent_desk.HISTORY_LIMIT == 60 and kept_history[0]["text"] == "10" and kept_history[-1]["text"] == "69",
      "patent desk: history keeps the last 60 entries")

desk_card = hub.patents_summary(patent_scan, desk_notes, patent_today, {
    "dossiers": desk_dossiers, "facts": desk_facts, "history": desk_moves, "figure_exists": desk_figure,
    "refreshed_at": "2026-11-14 09:01"})
check(desk_card["desk"]["projects"] == desk["projects"] and desk_card["desk"]["refreshed_at"] == "2026-11-14 09:01"
      and desk_card["desk"]["history"] == desk_moves and json.loads(json.dumps(desk_card))["desk"]["total"] == 6,
      "hub patent desk: the card carries the desk and it can be sent to the page as JSON")
check(patent_card["desk"] is None and hub.patents_summary(None, None, patent_today, {})["desk"] is None
      and hub.patent_desk_todo(None) == [] and hub.patent_desk_todo({"projects": []}) == [],
      "hub patent desk: no desk, no desk card and no desk to-dos")
desk_items = hub.patent_desk_todo(desk_card["desk"])
check([(item["priority"], item["title"]) for item in desk_items] == [
    (2, "hinge-lab: the provisional package is ready to file"),
    (2, "tile-game: 57 days left for the non-provisional or PCT application"),
    (2, "work-rig: who owns it?"),
    (3, "3 patent candidates not ready to file yet"),
], f"hub patent desk: to-dos ({[(item['priority'], item['title']) for item in desk_items]})")
check(desk_items[0]["action"] == "patents:open:hinge-lab" and desk_items[0]["action_label"] == "Open the folder"
      and "files nothing" in desk_items[0]["detail"] and desk_items[1]["action"] is None
      and "pocket-rig (25/100): Add one result and name the file or command" in desk_items[3]["detail"]
      and all(item["module"] == "Patents" for item in desk_items)
      and hub.patents_todo(desk_card)[-4:] == desk_items,
      "hub patent desk: the ready to-do opens the folder; the gaps to-do names each project's next step")
urgent_notes = {"hinge-lab": {"filed": {"date": "2025-12-01", "number": "63/000,002"}},
                "leaky": {"filed": {"date": "2025-10-01", "number": "63/000,003"}},
                "gear-rig": {"filed": {"date": "2026-11-01", "number": "63/000,004"}}}
urgent = patent_desk.build(patents.view(patent_scan, urgent_notes, patent_today), urgent_notes,
                           {"pocket-rig": full_dossier}, {"pocket-rig": {"ci": {"state": "success"}}}, patent_today, None, desk_figure)
urgent_items = hub.patent_desk_todo(urgent)
check([(item["priority"], item["title"]) for item in urgent_items][:3] == [
    (1, "pocket-rig: the provisional package is ready to file"),
    (1, "hinge-lab: 17 days left for the non-provisional or PCT application"),
    (1, "leaky: the 12 months have ended for the non-provisional or PCT application"),
] and "2026-12-09" in urgent_items[0]["detail"] and "gear-rig" not in json.dumps(urgent_items),
    f"hub patent desk: urgent inside 30 days; a filing with months left raises nothing ({[(item['priority'], item['title']) for item in urgent_items]})")

for bad_name in ("..", ".", "", "a/b", "a b", "a\\b", "x" * 101):
    try:
        patent_desk.folder(bad_name)
        check(False, f"patent desk: not a folder name ({bad_name[:12]!r})")
    except ValueError:
        check(True, f"patent desk: not a folder name ({bad_name[:12]!r})")

desk_home = Path(tempfile.mkdtemp(prefix="rp-desk-"))
desk_real = (patents.DATA_DIR, patents.SCAN_FILE, patents.OVERRIDES_FILE, patents.LOG_FILE)
try:
    patents.DATA_DIR = desk_home / "patents"
    patents.SCAN_FILE = patents.DATA_DIR / "scan.json"
    patents.OVERRIDES_FILE = patents.DATA_DIR / "overrides.json"
    patents.LOG_FILE = desk_home / "patents.log"
    check(patent_desk.refresh(patent_today, fetch_ci=None, pdf=False) == {"ok": False, "message": "Patent desk: no scan yet, nothing to follow."}
          and not patent_desk.desk_file().exists(), "patent desk: no scan, nothing written")

    patents.DATA_DIR.mkdir(parents=True)
    patents.SCAN_FILE.write_text(json.dumps(patent_scan), encoding="utf-8")
    patents.OVERRIDES_FILE.write_text(json.dumps(desk_notes), encoding="utf-8")
    for name, dossier in (("hinge-lab", full_dossier), ("pocket-rig", partial_dossier)):
        patent_desk.folder(name).mkdir(parents=True)
        (patent_desk.folder(name) / "dossier.json").write_text(json.dumps(dossier), encoding="utf-8")
    (patent_desk.folder("hinge-lab") / "fig1.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>", encoding="utf-8")
    asked_ci = []

    def fake_ci(full_name):
        asked_ci.append(full_name)
        return {"someone/hinge-lab": {"state": "success", "checked": "2026-11-14"},
                "someone/pocket-rig": {"state": "failure", "checked": "2026-11-14"}}.get(full_name)

    first_run = patent_desk.refresh(patent_today, fetch_ci=fake_ci, pdf=False)
    check(first_run == {"ok": True, "message": (
        "Patent desk: 6 project(s): 1 ready to file, 1 filed, 1 strengthening, 2 need a disclosure, 1 waiting on ownership; "
        "4 new dossier(s), 2 package(s) rebuilt, 0 change(s).")}, f"patent desk: the daily run reports what it did ({first_run['message']})")
    check(sorted(asked_ci) == sorted(f"someone/{name}" for name in on_desk),
          "patent desk: the CI state is asked once per project on the desk, by its full name")
    stored_desk = json.loads(patent_desk.desk_file().read_text(encoding="utf-8"))
    check(stored_desk["version"] == 1 and stored_desk["history"] == [] and stored_desk["refreshed_at"]
          and [entry["name"] for entry in stored_desk["projects"]] == [item["name"] for item in desk["projects"]]
          and set(stored_desk["facts"]) == set(on_desk) and stored_desk["facts"]["hinge-lab"]["ci"]["state"] == "success"
          and stored_desk["facts"]["hinge-lab"]["package"]["draft"] is False and stored_desk["facts"]["hinge-lab"]["package"]["pdf"] is False
          and stored_desk["facts"]["pocket-rig"]["package"]["draft"] is True and "package" not in stored_desk["facts"]["gear-rig"],
          "patent desk: desk.json keeps stage, readiness, CI state and what was built; the first run records no changes")

    package = patent_desk.folder("hinge-lab") / "provisional"
    check(sorted(path.name for path in package.iterdir())
          == ["attorney_notes.md", "cover_sheet.md", "filing_checklist.md", "specification.html", "specification.md"],
          "patent desk: the package is a specification (Markdown and HTML), cover sheet fields, a checklist and attorney notes")
    specification_text = (package / "specification.md").read_text(encoding="utf-8")
    check(all(piece in specification_text for piece in (
        "# Friction hinge with a printed torque limiter", "35 U.S.C. 111(b)", "## Detailed description",
        "4. Fit the spring compliance to that curve and set the preload.", "## Variations", "## Worked example", "torque 0.42 N m",
        "(source: bench/torque.csv)", "## Example claims", "![FIG. 1: Section through the knuckle](../fig1.svg)", "Not legal advice"))
          and "DRAFT, NOT READY" not in specification_text and "Spring washer hinge" not in specification_text,
          "patent desk: the specification is written from the dossier and leaves the prior art out")
    specification_page = (package / "specification.html").read_text(encoding="utf-8")
    check("rate &lt; 2 N/mm" in specification_page and '<img src="../fig1.svg" alt="FIG. 1">' in specification_page
          and specification_page.startswith("<!doctype html>"), "patent desk: the printable page escapes the dossier's text")
    attorney_text = (package / "attorney_notes.md").read_text(encoding="utf-8")
    check(all(piece in attorney_text for piece in (
        "Readiness on the patent desk: 100 of 100 (8 of 8 checks).", "- Spring washer hinge <https://example.test/a>",
        "Difference: A separate metal washer.", "Is the fitted preload a method claim or a product claim?",
        "not from a professional patent search")), "patent desk: the attorney notes carry the prior art, the open questions and the limits")
    cover_text = (package / "cover_sheet.md").read_text(encoding="utf-8")
    check(all(piece in cover_text for piece in ("PTO/SB/16", "Given name: Ada", "Family name: Example", "Drawings: 2 sheet(s)",
                                                "325 USD (large entity), 130 USD (small entity), 65 USD (micro entity)", "________")),
          "patent desk: the cover sheet lists the form's fields and leaves yours blank")
    with tempfile.TemporaryDirectory() as pages_folder:
        two_pages = Path(pages_folder) / "two.pdf"
        two_pages.write_bytes(b"%PDF-1.4\n1 0 obj<</Type /Pages /Count 2 /Kids [2 0 R 3 0 R]>>endobj\n"
                              b"2 0 obj<</Type /Page /Parent 1 0 R>>endobj\n3 0 obj<</Type/Page /Parent 1 0 R>>endobj\n%%EOF\n")
        mismatch = Path(pages_folder) / "mismatch.pdf"
        mismatch.write_bytes(two_pages.read_bytes().replace(b"/Count 2", b"/Count 3"))
        not_a_pdf = Path(pages_folder) / "text.pdf"
        not_a_pdf.write_bytes(b"/Type /Page /Type /Pages /Count 1")
        check(patent_desk.pdf_pages(two_pages) == 2 and patent_desk.pdf_pages(mismatch) is None
              and patent_desk.pdf_pages(not_a_pdf) is None and patent_desk.pdf_pages(Path(pages_folder) / "missing.pdf") is None,
              "patent desk: the page count comes from the PDF itself, and is left out when the file does not agree with itself")
        # Edge prints a longer document with a page tree in branches: 8 pages under one branch, 2 under the next.
        branches = Path(pages_folder) / "branches.pdf"
        branches.write_bytes(b"%PDF-1.4\n1 0 obj<</Type /Pages /Count 8 /Parent 3 0 R /Kids [4 0 R]>>endobj\n"
                             b"2 0 obj<</Type /Pages /Count 2 /Parent 3 0 R /Kids [12 0 R]>>endobj\n"
                             b"3 0 obj<</Type /Pages /Count 10 /Kids [1 0 R 2 0 R]>>endobj\n"
                             + b"".join(b"%d 0 obj<</Type /Page /Parent 1 0 R>>endobj\n" % number for number in range(4, 14))
                             + b"%%EOF\n")
        short_root = Path(pages_folder) / "short_root.pdf"
        short_root.write_bytes(branches.read_bytes().replace(b"/Count 10", b"/Count 9"))
        check(patent_desk.pdf_pages(branches) == 10 and patent_desk.pdf_pages(short_root) is None
              and patent_desk.sb16_values("hinge-lab", patent_desk.load_dossier("hinge-lab"), patent_desk.pdf_pages(branches))
              [patent_desk.SB16_FIELDS["pages"]] == "8",
              "patent desk: a page tree in branches is counted by its root, so a longer specification still gets its page number on the form")
    counted_sheet = patent_desk.cover_sheet("hinge-lab", patent_desk.load_dossier("hinge-lab"), 5)
    check("specification.pdf has 5 page(s) in total, drawing sheets included" in counted_sheet and "____ pages" not in counted_sheet
          and "Drawings: 2 sheet(s)" in counted_sheet and "count the pages of specification.pdf: ____ pages" in cover_text,
          "patent desk: the cover sheet states the page count of the printed specification, and asks you to count when no PDF was printed")
    # The printed PDF: Edge may write the file and then not exit. The form PTO/SB/16 with the dossier's values typed in.
    hinge = patent_desk.load_dossier("hinge-lab")
    form_field = patent_desk.SB16_FIELDS
    typed = patent_desk.sb16_values("hinge-lab", hinge, 5)
    check(typed == {
        form_field["title"]: "Friction hinge with a printed torque limiter", form_field["docket"]: "hinge-lab",
        form_field["given"].format(row=1): "Ada", form_field["family"].format(row=1): "Example",
        form_field["residence"].format(row=1): "Springfield, Nowhere",
        form_field["specification"]: "/On", form_field["pages"]: "3", form_field["drawings"]: "/On", form_field["sheets"]: "2"},
        "patent desk: the form gets the title, the docket number, the inventor and the two counts, and nothing else")
    uncounted = patent_desk.sb16_values("hinge-lab", hinge)
    check(form_field["pages"] not in uncounted and form_field["specification"] not in uncounted and uncounted[form_field["sheets"]] == "2"
          and form_field["pages"] not in patent_desk.sb16_values("hinge-lab", hinge, 2),
          "patent desk: without a page count that leaves text pages, the form's page number and its box stay empty")
    crowd = patent_desk.sb16_values("crowd", {"title": "T" * 600, "inventors": [
        {"given": f"Given{number}", "family": f"Family{number}"} for number in range(1, 8)]})
    check(len(crowd[form_field["title"]]) == 500 and crowd[form_field["family"].format(row=5)] == "Family5"
          and form_field["given"].format(row=6) not in crowd and not any("Residence" in key for key in crowd)
          and form_field["drawings"] not in crowd and form_field["sheets"] not in crowd
          and patent_desk.sb16_values("bare", {"inventors": [{"given": "", "family": None}, "damaged"]})
          == {form_field["title"]: "bare", form_field["docket"]: "bare"},
          "patent desk: the title is cut to the form's 500 characters, five inventor rows are typed, and a blank stays blank")
    typed_sheet = patent_desk.cover_sheet("hinge-lab", hinge, 5, True)
    check("- On the form: 3 page(s) of specification and 2 sheet(s) of drawings." in typed_sheet
          and "## The form itself" in typed_sheet and "sb16_prefilled.pdf" in typed_sheet and "ReachPilot sends it nowhere." in typed_sheet
          and "- On the form: 3 page(s) of specification" in counted_sheet and "## The form itself" not in counted_sheet
          and "sb16_prefilled.pdf" not in counted_sheet and "On the form" not in cover_text
          and "sb16_prefilled.pdf" in (package / "filing_checklist.md").read_text(encoding="utf-8"),
          "patent desk: the cover sheet splits the pages into text and drawing sheets, and names the typed-in form only when it was made")

    fake_pdf = (b"%PDF-1.4\n1 0 obj<</Type /Pages /Count 5 /Kids [2 0 R 3 0 R 4 0 R 5 0 R 6 0 R]>>endobj\n"
                + b"".join(b"%d 0 obj<</Type /Page /Parent 1 0 R>>endobj\n" % number for number in range(2, 7))
                + b"% " + b"x" * 1000 + b"\n%%EOF\n")
    edge_options = []
    edge_stopped = []

    def fake_edge(content, hangs):
        def launch(command, **options):
            printed = Path(next(part for part in command if part.startswith("--print-to-pdf=")).split("=", 1)[1])
            process = mock.Mock()

            def wait(timeout=None):
                if content is not None:
                    printed.write_bytes(content)
                if hangs:
                    raise patent_desk.subprocess.TimeoutExpired(command, timeout)
                return 0

            process.wait = wait
            edge_options.append(options)
            return process
        return launch

    def no_edge(command, **options):
        raise OSError("cannot start")

    with tempfile.TemporaryDirectory() as print_folder:
        page_source = Path(print_folder) / "page.html"
        page_source.write_text("<p>page</p>", encoding="utf-8")
        printed_pdf = Path(print_folder) / "page.pdf"
        check(patent_desk.pdf_pages(Path(print_folder) / "none.pdf") is None and not patent_desk.complete_pdf(printed_pdf), "patent desk: no file is no PDF")
        printed_pdf.write_bytes(fake_pdf)
        whole = patent_desk.complete_pdf(printed_pdf) and patent_desk.pdf_pages(printed_pdf) == 5
        printed_pdf.write_bytes(fake_pdf[:600])
        cut_short = patent_desk.complete_pdf(printed_pdf)
        printed_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
        too_small = patent_desk.complete_pdf(printed_pdf)
        printed_pdf.write_bytes(b"<html>" + b"x" * 2000 + b"%%EOF")
        check(whole and not cut_short and not too_small and not patent_desk.complete_pdf(printed_pdf),
              "patent desk: a PDF counts when it has its header, its end-of-file mark and more than a stub's bytes")

        def edge_print(content, hangs, launch=None):
            return patent_desk.print_pdf(page_source, printed_pdf, timeout=1, edge=Path("edge"),
                                         launch=launch or fake_edge(content, hangs), stop=edge_stopped.append)

        hung_but_written = edge_print(fake_pdf, True)
        check(hung_but_written is True and len(edge_stopped) == 1 and printed_pdf.read_bytes() == fake_pdf
              and edge_options[-1]["stdout"] == patent_desk.subprocess.DEVNULL and edge_options[-1]["stderr"] == patent_desk.subprocess.DEVNULL,
              "patent desk: an Edge that wrote the PDF and did not exit is stopped, and its complete file is kept; no pipe is read")
        hung_half_written = edge_print(fake_pdf[:600], True)
        hung_and_empty = edge_print(None, True)
        check(hung_half_written is False and hung_and_empty is False and len(edge_stopped) == 3 and not printed_pdf.exists(),
              "patent desk: an Edge that hung on a half-written or missing PDF gives no PDF")
        printed_pdf.write_bytes(fake_pdf)
        stale_print = edge_print(None, False)
        clean_print = edge_print(fake_pdf, False)
        with mock.patch.object(patent_desk, "EDGE", ()):
            without_edge = patent_desk.print_pdf(page_source, printed_pdf)
        check(stale_print is False and clean_print is True and len(edge_stopped) == 3 and edge_print(None, False, no_edge) is False
              and without_edge is False,
              "patent desk: a normal print stops nothing; an old PDF is not passed off as new; no Edge, no PDF")

    def no_network(url):
        raise OSError("no network")

    form_asked = []

    def form_host(url):
        form_asked.append(url)
        return fake_pdf

    check(patent_desk.sb16_form(no_network) is None and patent_desk.sb16_form(lambda url: b"<html>blocked</html>") is None
          and patent_desk.sb16_form(lambda url: fake_pdf[:600]) is None and not (patents.DATA_DIR / "forms").exists(),
          "patent desk: no network, a block page or a cut-off download leaves no form behind")
    kept_form = patent_desk.sb16_form(form_host)
    check(kept_form == patents.DATA_DIR / "forms" / "sb0016.pdf" and kept_form.read_bytes() == fake_pdf
          and form_asked == [patent_desk.SB16_URL] and patent_desk.SB16_URL.startswith("https://www.uspto.gov/")
          and patent_desk.sb16_form(no_network) == kept_form,
          "patent desk: the blank form comes from uspto.gov once and is then read from data/patents/forms")
    kept_form.unlink()
    asked_before = patent_desk._sb16_asked
    try:
        patent_desk._sb16_asked = False
        with mock.patch.object(patent_desk, "_download", side_effect=OSError("no network")) as asked_form:
            check(patent_desk.sb16_form() is None and patent_desk.sb16_form() is None and asked_form.call_count == 1,
                  "patent desk: a form that cannot be fetched is asked for once per run, not once per package")
    finally:
        patent_desk._sb16_asked = asked_before

    stale = patent_desk.package_stale
    built_whole = {"fingerprint": "f1", "pdf": True, "sb16": True}
    check(stale({}, "f1", package) and stale(built_whole, "f2", package) and stale(built_whole, "f1", desk_home / "nowhere")
          and not stale(built_whole, "f1", package, True, True, True, lambda: package)
          and not stale({"fingerprint": "f1"}, "f1", package, False, True, True, lambda: package),
          "patent desk: a package is built again when the dossier changed or the specification is gone, and otherwise left alone")
    check(stale({**built_whole, "pdf": False}, "f1", package, True, False, True, lambda: None)
          and not stale({**built_whole, "pdf": False}, "f1", package, True, False, False, lambda: None)
          and stale({"fingerprint": "f1", "pdf": True}, "f1", package, True, True, True, lambda: package) is patent_desk.can_prefill()
          and not stale({"fingerprint": "f1", "pdf": True}, "f1", package, True, False, True, lambda: package)
          and not stale({"fingerprint": "f1", "pdf": True}, "f1", package, True, True, True, lambda: None),
          "patent desk: a PDF or a typed-in form that could not be made is tried again on the next run, when it can be made")
    with tempfile.TemporaryDirectory() as counted_folder:
        counted = Path(counted_folder)
        (counted / "specification.md").write_text("text", encoding="utf-8")
        no_pdf_yet = stale(built_whole, "f1", counted, True, True, True, lambda: package)
        (counted / "specification.pdf").write_bytes(fake_pdf)
        check(not no_pdf_yet and stale(built_whole, "f1", counted, True, True, True, lambda: package)
              and stale({**built_whole, "pages": None}, "f1", counted, True, True, True, lambda: package)
              and not stale({**built_whole, "pages": 5}, "f1", counted, True, True, True, lambda: package)
              and not stale(built_whole, "f1", counted, True, False, True, lambda: package),
              "patent desk: a form typed in without its page count is made again once the printed PDF can be counted, and then left alone")

    form_made = package / patent_desk.SB16_FILE
    with mock.patch.dict(sys.modules, {"pypdf": None}):
        check(not patent_desk.can_prefill() and patent_desk.prefill_sb16(package / "specification.md", form_made, typed) is False
              and not form_made.exists() and not stale({"fingerprint": "f1", "pdf": True}, "f1", package, True, True, True, lambda: package),
              "patent desk: without pypdf no form is typed in, and nothing fails")

    def fake_print(source, target, *rest, **options):
        target.write_bytes(fake_pdf)
        return True

    desk_row = {row["name"]: row for row in desk_rows}

    def build_printed(name, form):
        with mock.patch.object(patent_desk, "print_pdf", fake_print), mock.patch.object(patent_desk, "sb16_form", form):
            return patent_desk.build_package(name, patent_desk.load_dossier(name), on_desk[name], desk_row.get(name), True)

    if patent_desk.can_prefill():
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, DictionaryObject, FloatObject, NameObject, NumberObject, StreamObject, TextStringObject

        def tiny_form(path, text_names, box_names):
            """A one-page form with the kinds of field PTO/SB/16 has: text fields, and check boxes with /On and /Off."""
            def box(*numbers):
                return ArrayObject(FloatObject(number) for number in numbers)

            writer = PdfWriter()
            page = writer.add_blank_page(width=612, height=792)
            font = writer._add_object(DictionaryObject({
                NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica")}))
            widgets = ArrayObject()
            for index, title in enumerate(list(text_names) + list(box_names)):
                top = 760 - 20 * index
                widget = DictionaryObject({
                    NameObject("/Type"): NameObject("/Annot"), NameObject("/Subtype"): NameObject("/Widget"),
                    NameObject("/T"): TextStringObject(title), NameObject("/Rect"): box(40, top - 14, 400, top),
                    NameObject("/F"): NumberObject(4), NameObject("/P"): page.indirect_reference})
                if title in box_names:
                    marks = DictionaryObject()
                    for state in ("/On", "/Off"):
                        mark = StreamObject()
                        mark[NameObject("/Type")] = NameObject("/XObject")
                        mark[NameObject("/Subtype")] = NameObject("/Form")
                        mark[NameObject("/BBox")] = box(0, 0, 14, 14)
                        mark._data = b"0 0 14 14 re f" if state == "/On" else b""
                        marks[NameObject(state)] = writer._add_object(mark)
                    widget.update({NameObject("/FT"): NameObject("/Btn"), NameObject("/V"): NameObject("/Off"),
                                   NameObject("/AS"): NameObject("/Off"), NameObject("/AP"): DictionaryObject({NameObject("/N"): marks})})
                else:
                    widget.update({NameObject("/FT"): NameObject("/Tx"), NameObject("/DA"): TextStringObject("/Helv 9 Tf 0 g")})
                widgets.append(writer._add_object(widget))
            page[NameObject("/Annots")] = widgets
            writer._root_object[NameObject("/AcroForm")] = DictionaryObject({
                NameObject("/Fields"): widgets, NameObject("/DA"): TextStringObject("/Helv 9 Tf 0 g"),
                NameObject("/DR"): DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/Helv"): font})})})
            with open(path, "wb") as handle:
                writer.write(handle)

        def form_state(path):
            reader = PdfReader(str(path))
            values = {key: (None if field.get("/V") is None else str(field.get("/V"))) for key, field in reader.get_fields().items()}
            shown = {str(widget.get_object()["/T"]): str(widget.get_object().get("/AS"))
                     for widget in reader.pages[0]["/Annots"] if widget.get_object().get("/FT") == "/Btn"}
            return values, shown

        yours = ["Signature", "Mailing Address", "Micro entity"]
        blank_form = desk_home / "blank_form.pdf"
        tiny_form(blank_form,
                  [form_field[key].format(row=row) for row in (1, 2) for key in ("given", "family", "residence")]
                  + [form_field["title"], form_field["pages"], form_field["sheets"], form_field["docket"]] + yours[:2],
                  [form_field["specification"], form_field["drawings"], yours[2]])
        blank_bytes = blank_form.read_bytes()
        filled_ok = patent_desk.prefill_sb16(blank_form, form_made, typed)
        filled_values, filled_boxes = form_state(form_made) if filled_ok else ({}, {})
        check(filled_ok and patent_desk.complete_pdf(form_made) and all(filled_values.get(key) == value for key, value in typed.items())
              and filled_boxes == {form_field["specification"]: "/On", form_field["drawings"]: "/On", yours[2]: "/Off"}
              and filled_values[yours[0]] is None and filled_values[yours[1]] is None and filled_values[yours[2]] == "/Off"
              and filled_values[form_field["given"].format(row=2)] is None and blank_form.read_bytes() == blank_bytes,
              "patent desk: the form is a copy with the values typed in and read back; signature, address and entity stay untouched")
        logging.disable(logging.CRITICAL)
        try:
            unknown_field = patent_desk.prefill_sb16(blank_form, form_made, {**typed, "Not on this form": "x"})
            left_behind = form_made.exists()
            not_a_form = patent_desk.prefill_sb16(package / "specification.md", form_made, typed)
            nothing_to_type = patent_desk.prefill_sb16(blank_form, form_made, {})
        finally:
            logging.disable(logging.NOTSET)
        check(unknown_field is False and not left_behind and not_a_form is False and nothing_to_type is False and not form_made.exists(),
              "patent desk: a form that lacks a field, or is not the form, leaves no typed-in copy behind")

        form_calls = mock.Mock(return_value=blank_form)
        ready_build = build_printed("hinge-lab", form_calls)
        ready_values = form_state(form_made)[0] if form_made.exists() else {}
        check(ready_build == {"built": patent_date.today().isoformat(), "pdf": True, "draft": False, "sb16": True, "pages": 5}
              and ready_values.get(form_field["pages"]) == "3" and ready_values.get(form_field["sheets"]) == "2"
              and ready_values.get(form_field["docket"]) == "hinge-lab"
              and "## The form itself" in (package / "cover_sheet.md").read_text(encoding="utf-8")
              and "- On the form: 3 page(s) of specification and 2 sheet(s) of drawings." in (package / "cover_sheet.md").read_text(encoding="utf-8"),
              "patent desk: a ready package gets the typed-in form, with the page count of the PDF that was just printed")
        draft_build = build_printed("pocket-rig", form_calls)
        draft_package = patent_desk.folder("pocket-rig") / "provisional"
        check(draft_build == {"built": patent_date.today().isoformat(), "pdf": True, "draft": True, "sb16": False, "pages": 5}
              and form_calls.call_count == 1 and not (draft_package / patent_desk.SB16_FILE).exists()
              and "The form itself" not in (draft_package / "cover_sheet.md").read_text(encoding="utf-8"),
              "patent desk: a draft package never gets a typed-in form")
        no_form_build = build_printed("hinge-lab", lambda: None)
        check(no_form_build["sb16"] is False and no_form_build["pdf"] is True and not form_made.exists()
              and "The form itself" not in (package / "cover_sheet.md").read_text(encoding="utf-8")
              and "specification.pdf has 5 page(s)" in (package / "cover_sheet.md").read_text(encoding="utf-8"),
              "patent desk: when the blank form cannot be had, an older typed-in copy is removed and the package is still built")
    else:
        no_pypdf_build = build_printed("hinge-lab", lambda: package / "specification.md")
        check(no_pypdf_build["sb16"] is False and no_pypdf_build["pdf"] is True and not form_made.exists(),
              "patent desk: without pypdf the package is built without the typed-in form")
    for name in ("hinge-lab", "pocket-rig"):  # back to what the run without a PDF left, for the checks below
        for printed_name in ("specification.pdf", patent_desk.SB16_FILE):
            (patent_desk.folder(name) / "provisional" / printed_name).unlink(missing_ok=True)
        patent_desk.build_package(name, patent_desk.load_dossier(name), on_desk[name], desk_row.get(name), False)
    check(sorted(path.name for path in package.iterdir())
          == ["attorney_notes.md", "cover_sheet.md", "filing_checklist.md", "specification.html", "specification.md"]
          and (package / "cover_sheet.md").read_text(encoding="utf-8") == cover_text,
          "patent desk: a build without a PDF writes no form and no page count")
    draft_folder = patent_desk.folder("pocket-rig") / "provisional"
    check("This repository is private" in (package / "filing_checklist.md").read_text(encoding="utf-8")
          and "cannot be extended" in (package / "filing_checklist.md").read_text(encoding="utf-8")
          and "is 2026-12-09" in (draft_folder / "filing_checklist.md").read_text(encoding="utf-8")
          and "DRAFT, NOT READY TO FILE" in (draft_folder / "specification.md").read_text(encoding="utf-8")
          and "- Given name: ________" in (draft_folder / "cover_sheet.md").read_text(encoding="utf-8"),
          "patent desk: a package with open checks is marked as a draft; the checklist gives the date of a public repository")

    starter = json.loads((patent_desk.folder("gear-rig") / "dossier.json").read_text(encoding="utf-8"))
    check(starter["problem"] == "" and starter["claims"] == [] and starter["hints"] and all(isinstance(hint, str) for hint in starter["hints"])
          and "No invented numbers" in (patent_desk.folder("gear-rig") / "BRIEF.md").read_text(encoding="utf-8")
          and not (patent_desk.folder("gear-rig") / "provisional").exists()
          and not patent_desk.folder("old-rig").exists() and not patent_desk.folder("borrowed").exists(),
          "patent desk: a new candidate gets an empty dossier with the quoted sentences and a brief; no package until something is written")
    second_run = patent_desk.refresh(patent_today, fetch_ci=fake_ci, pdf=False)
    check(second_run["message"].endswith("0 new dossier(s), 0 package(s) rebuilt, 0 change(s)."),
          f"patent desk: a second run with nothing new rebuilds nothing ({second_run['message']})")
    written = {**starter, "problem": full_dossier["problem"]}
    (patent_desk.folder("gear-rig") / "dossier.json").write_text(json.dumps(written), encoding="utf-8")
    third_run = patent_desk.refresh(patent_today, fetch_ci=fake_ci, pdf=False)
    check(third_run["message"].endswith("0 new dossier(s), 1 package(s) rebuilt, 2 change(s).")
          and json.loads((patent_desk.folder("gear-rig") / "dossier.json").read_text(encoding="utf-8")) == written
          and [entry["text"] for entry in json.loads(patent_desk.desk_file().read_text(encoding="utf-8"))["history"]]
          == ["needs an invention disclosure -> strengthening.", "Readiness 0 -> 10 of 100."],
          f"patent desk: what you write is never overwritten, and the next run records the move ({third_run['message']})")
    quiet_run = patent_desk.refresh(patent_today, fetch_ci=None, pdf=False)
    check(quiet_run["ok"] and json.loads(patent_desk.desk_file().read_text(encoding="utf-8"))["facts"]["hinge-lab"]["ci"]["state"] == "success",
          "patent desk: when GitHub does not answer, the last known CI state is kept")

    live_desk = hub.patents_section(patent_today)["desk"]
    live_names = {item["name"]: item for item in live_desk["projects"]}
    check(live_desk["total"] == 6 and live_desk["refreshed_at"] and len(live_desk["history"]) == 2
          and live_names["hinge-lab"]["stage"] == "ready" and live_names["hinge-lab"]["package"]["draft"] is False
          and live_names["gear-rig"]["stage"] == "strengthen" and live_names["gear-rig"]["score"] == 10,
          "hub patent desk: the card reads dossiers, figures and the last run from disk")
    check(patent_desk.figure_on_disk("hinge-lab", "fig1.svg") and not patent_desk.figure_on_disk("hinge-lab", "fig9.svg")
          and not patent_desk.figure_on_disk("hinge-lab", "provisional")
          and not patent_desk.figure_on_disk("hinge-lab", "../pocket-rig/dossier.json")
          and not patent_desk.figure_on_disk("hinge-lab", "../../scan.json") and not patent_desk.figure_on_disk("..", "scan.json"),
          "patent desk: a figure counts only as a file inside its own dossier folder")

    notes_before = patents.load_overrides()
    check(hub.run_action("patents:ownership:work-rig:cleared") == "work-rig: marked as yours. The desk drafts for it from the next run."
          and patents.load_overrides()["work-rig"]["owner"] == "employer?" and patents.load_overrides()["work-rig"]["ownership_cleared"]
          and {item["name"]: item["stage"] for item in hub.patents_section(patent_today)["desk"]["projects"]}["work-rig"] == "disclosure",
          "hub patent desk: Ownership cleared is recorded next to the existing notes and moves the project on")
    check(hub.run_action("patents:ownership:work-rig:unsure").startswith("work-rig: ownership open again")
          and patents.load_overrides() == notes_before, "hub patent desk: ownership can be opened again")
    check(hub.run_action("patents:filed:hinge-lab:2020-01-15:63/987,654")
          == "hinge-lab: filing recorded. The non-provisional or PCT application is due by 2021-01-15; that date cannot be extended."
          and patents.load_overrides()["hinge-lab"]["filed"] == {"date": "2020-01-15", "number": "63/987,654"}
          and patents.load_overrides()["hinge-lab"]["decision"] == "file"
          and {item["name"]: item["stage"] for item in hub.patents_section(patent_today)["desk"]["projects"]}["hinge-lab"] == "filed",
          "hub patent desk: Record filing saves the date and the application number")
    check(hub.run_action("patents:unfiled:hinge-lab") == "hinge-lab: recorded filing removed."
          and "filed" not in patents.load_overrides()["hinge-lab"], "hub patent desk: a recorded filing can be removed")
    notes_before = patents.load_overrides()
    for refused_filing in (("hinge-lab", "2026-11-15", "63/123,456"), ("hinge-lab", "14/11/2026", "63/123,456"),
                           ("hinge-lab", "2026-02-30", "63/123,456"), ("hinge-lab", "2026-11-10", "abc"),
                           ("hinge-lab", "2026-11-10", "63/123,456; x"), ("hinge-lab", "2026-11-10", "//////"),
                           ("nowhere", "2026-11-10", "63/123,456"), ("../x", "2026-11-10", "63/123,456")):
        try:
            patent_desk.record_filing(*refused_filing, today=patent_today)
            check(False, f"patent desk: filing refused {refused_filing}")
        except ValueError:
            check(True, f"patent desk: filing refused {refused_filing}")
    for refused in ("patents:filed:hinge-lab:2020-01-15", "patents:filed:hinge-lab:2999-01-01:63/123,456",
                    "patents:ownership:work-rig:maybe", "patents:ownership:nowhere:cleared", "patents:unfiled:hinge-lab:extra",
                    "patents:unfiled:../x", "patents:open:..", "patents:open:nowhere", "patents:open:old-rig", "patents:open:hinge-lab:x",
                    "patents:build:hinge-lab"):
        try:
            hub.run_action(refused)
            check(False, f"hub patent desk: malformed action refused ({refused})")
        except ValueError:
            check(True, f"hub patent desk: malformed action refused ({refused})")
    check(patents.load_overrides() == notes_before, "patent desk: a refused filing or action changes nothing")
    with mock.patch.object(patent_desk.os, "startfile", create=True) as opened_folder:
        check(hub.run_action("patents:open:hinge-lab") == "Opened the dossier folder of hinge-lab."
              and opened_folder.call_args[0] == (patent_desk.folder("hinge-lab"),),
              "hub patent desk: Open the dossier folder opens that project's folder and nothing else")
finally:
    patents.DATA_DIR, patents.SCAN_FILE, patents.OVERRIDES_FILE, patents.LOG_FILE = desk_real

check(all(piece in patent_page for piece in ("function patentDeskCard", "function keepingState", "data-filed", "Patent desk",
                                             "patents:ownership:", "patents:unfiled:", "patents:open:", "patents:filed:")),
      "dashboard: the Patent desk card, its checklist, its filing form and its buttons are present")
desk_source = (Path(__file__).parent / "patent_desk.py").read_bytes()
check(not desk_source.startswith(b"\xef\xbb\xbf") and "\u00c3" not in desk_source.decode("utf-8")
      and "\u00e2\u20ac" not in desk_source.decode("utf-8"), "patent_desk.py: clean UTF-8, no byte-order mark")
check("New-ScheduledTaskTrigger -Daily -At 09:00" in patent_source.decode("utf-8") and "patent_desk.refresh()" in patent_source.decode("utf-8"),
      "patents.py: the task runs every day and refreshes the desk after the screen")
desk_help = json.dumps(hub.help_sections({}, None, None, patent_card)[-1])
check("Every day 09:00" in desk_help and "files nothing" in desk_help and "Record filing" in desk_help,
      "hub patent desk: the help entry says it runs daily and files nothing")

# launch automations (launch_auto.py): the answer to a sent mail and one follow-up, invitations, the waiting proposal
import launch_auto  # noqa: E402

auto_home = Path(tempfile.mkdtemp(prefix="rp-launch-auto-"))
auto_real = (launch_auto.DATA_DIR, launch_auto.PRIVATE_FILE, launch_auto.LOG_FILE, launch_auto._relay_version,
             launch_auto._relay_post, launch_auto._open, launch_auto._sync_repo, launch_auto._run_command,
             launch_auto._link_answers, launch_auto.SEND_PAUSE_SECONDS, launch_auto.rows)
auto_posts, auto_opened, auto_calls = [], [], {"sync": 0, "judge": 0}
auto_memory = {"contacts": None}  # the last contact list a case wrote
auto_world = {"version": 2, "inbox": "none", "at_send": "none", "relay_ok": True, "refuse": set(),
              "sync_ok": True, "checks_ok": True, "dead": set(), "score": 9.1}
auto_answer = {"ok": True, "status": "replied", "from": "Office <help@office.test>", "date": "2026-11-20 10:00",
               "subject": "Re: Question", "body": "Thank you. Our answer: point 2 is fine."}


def auto_post(mail, payload):
    auto_posts.append(payload)
    if not auto_world["relay_ok"] or payload.get("to") in auto_world["refuse"]:
        return {"ok": False, "error": "relay down"}
    if payload.get("action") == "check_reply":
        return {**auto_answer, "status": auto_world["inbox"]} if auto_world["inbox"] != "none" else {"ok": True, "status": "none"}
    if payload.get("action") == "followup":
        return {**auto_answer, "status": auto_world["at_send"]} if auto_world["at_send"] != "none" else {"ok": True, "status": "sent"}
    return {"ok": True}


def auto_sync(url, folder):
    auto_calls["sync"] += 1
    if not auto_world["sync_ok"]:
        return False, "could not reach GitHub"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pack.md").write_text("See https://example.test/law and https://example.test/app.\n", encoding="utf-8")
    return True, "abc1234"


def auto_command(command, folder, timeout, replace=None):
    if replace:  # the judge: it writes its verdict to {out}
        auto_calls["judge"] += 1
        auto_world["judge_budget"] = timeout
        if not auto_world.get("judge_ok", True):
            return False, "TimeoutExpired"
        Path(replace["out"]).write_text(json.dumps({"council_score": auto_world["score"],
                                                    "judges": {"one": {"total": 9.3}, "two": {"total": 8.9}, "three": {"error": "timed out"}},
                                                    "fixes": [{"fix": "Say who you are in the first line."}]}), encoding="utf-8")
        return True, "judged"
    return auto_world["checks_ok"], "1 passed" if auto_world["checks_ok"] else "pack.md: a link is missing"


def auto_at(text):
    return datetime.fromisoformat(text + ":00+07:00")


def auto_fresh(keep_contacts=False):
    """Forget everything the automation learned; start the next case clean."""
    folder = auto_home / "data" / "demo"
    if (folder / "contacts.csv").exists():
        auto_memory["contacts"] = (folder / "contacts.csv").read_text(encoding="utf-8")
    contacts = auto_memory["contacts"] if keep_contacts else None
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir(parents=True)
    (folder / "followup.txt").write_text("Dear Office,\nmay I ask again about my mail of 14 November?\nhttps://example.test/pack\n", encoding="utf-8")
    if contacts is not None:
        (folder / "contacts.csv").write_text(contacts, encoding="utf-8")
    auto_posts.clear()
    auto_world.update({"version": 2, "inbox": "none", "at_send": "none", "relay_ok": True, "refuse": set(),
                       "sync_ok": True, "checks_ok": True, "dead": set(), "score": 9.1})


def auto_row(row_id, now):
    return next((row for row in launch_auto.rows("demo", now) if row["id"] == row_id), None)


def auto_sent():
    return [payload for payload in auto_posts if payload.get("to")]


auto_settings = {
    "mail": {"sender_name": "Me", "reply_to": "me@example.test"},
    "reply": {"who": "Office", "to": "help@office.test", "sent_at": "2026-11-14T17:24:10+07:00", "subject": "Question",
              "followup_due": "2026-11-24", "give_up": "2026-12-03", "followup_text_file": "followup.txt", "replaces": "odd"},
    "invites": {"start": "2026-11-28T10:00:00+07:00", "target": 30, "contacts_file": "contacts.csv", "subject": "Try it?",
                "message": "Hello {name}, one test tonight? https://example.test/app", "reminder": "Now is the busy hour.",
                "day7": "Thank you.", "replaces": "invite"},
    "proposal": {"name": "Pack", "repo": "https://example.test/repo.git",
                 "checks": [{"name": "pack check", "command": ["node", "check.mjs"]}], "link_files": ["pack.md"],
                 "judge": {"command": ["judge", "--out", "{out}"], "every_days": 7}},
}
try:
    launch_auto.DATA_DIR = auto_home / "data"
    launch_auto.PRIVATE_FILE = auto_home / "launch_private.json"
    launch_auto.LOG_FILE = auto_home / "launch_auto.log"
    launch_auto._relay_version = lambda mail: auto_world["version"]
    launch_auto._relay_post = auto_post
    launch_auto._open = auto_opened.append
    launch_auto._sync_repo = auto_sync
    launch_auto._run_command = auto_command
    launch_auto._link_answers = lambda url: url not in auto_world["dead"]
    launch_auto.SEND_PAUSE_SECONDS = 0

    check(launch_auto.load_config() == {} and launch_auto.rows("demo") == [] and launch_auto.run() == "nothing configured",
          "launch auto: no private file, nothing configured, nothing done")
    launch_auto.PRIVATE_FILE.write_text(json.dumps({
        "demo": {"needsMe": [], "auto": auto_settings}, "Bad Id": {"auto": auto_settings}, "plain": {"needsMe": []}}), encoding="utf-8")
    check(list(launch_auto.load_config()) == ["demo"], "launch auto: only tests with settings and a safe id are run")

    # A. a relay that cannot read the inbox (older than version 3): nothing is ever sent without a tap
    auto_fresh()
    launch_auto.run(now=auto_at("2026-11-16T09:00"))
    check(auto_posts == [], "launch auto reply: an old relay is neither asked to read nor to send")
    check(auto_row("auto-reply", auto_at("2026-11-16T09:00"))["status"] == "yours"
          and auto_row("auto-relay", auto_at("2026-11-16T09:00"))["actions"][0]["name"] == "launch:relay_code:demo",
          "launch auto reply: old relay shows 'look yourself' and offers the new relay code")
    check(launch_auto.action("launch:followup:demo", auto_at("2026-11-16T09:05")).startswith("Too early") and auto_posts == [],
          "launch auto reply: a follow-up before its date is refused")
    launch_auto.run(now=auto_at("2026-11-24T10:00"))
    due_row = auto_row("auto-reply", auto_at("2026-11-24T10:00"))
    check(auto_posts == [] and due_row["status"] == "last-tap" and due_row["actions"][0]["name"] == "launch:followup:demo",
          "launch auto reply: on the date an old relay still sends nothing; the follow-up is one tap")
    first_tap = launch_auto.action("launch:followup:demo", auto_at("2026-11-24T10:05"))
    second_tap = launch_auto.action("launch:followup:demo", auto_at("2026-11-24T10:06"))
    launch_auto.run(now=auto_at("2026-11-24T11:00"))
    check(first_tap == "Follow-up sent to help@office.test." and "never sent twice" in second_tap and len(auto_sent()) == 1
          and auto_sent()[0]["to"] == "help@office.test" and auto_sent()[0]["subject"] == "Re: Question"
          and "mail of 14 November" in auto_sent()[0]["text"] and "action" not in auto_sent()[0],
          "launch auto reply: the tapped follow-up goes out exactly once")
    check(auto_row("auto-reply", auto_at("2026-11-24T11:00"))["status"] == "yours"
          and launch_auto.action("launch:no_reply:demo") == "Noted: no answer. This step is closed."
          and auto_row("auto-reply", auto_at("2026-12-04T09:00"))["status"] == "done"
          and auto_row("auto-relay", auto_at("2026-12-04T09:00")) is None,
          "launch auto reply: after the follow-up I say what happened and the step closes")

    # A broken relay during the tapped follow-up: the attempt is kept, so it can never be repeated
    auto_fresh()
    auto_world["relay_ok"] = False
    broken_tap = launch_auto.action("launch:followup:demo", auto_at("2026-11-24T10:05"))
    auto_world["relay_ok"] = True
    check("may not have gone out" in broken_tap and "never sent twice" in launch_auto.action("launch:followup:demo", auto_at("2026-11-24T10:07"))
          and len(auto_sent()) == 1, "launch auto reply: an unclear send is not retried (no double mail)")

    # Two runs at once (the hourly task and a run by hand): the second one does nothing, the next one works again
    auto_fresh()
    auto_world["version"] = 3
    with launch_auto._only_one("run") as first_holder:
        second_run = launch_auto.run(now=auto_at("2026-11-16T09:00"))
        check(first_holder and second_run == "another run is still working; nothing done" and auto_posts == []
              and not (auto_home / "data" / "demo" / "state.json").exists(),
              "launch auto lock: a second run at the same time reads, sends and writes nothing")
    after_release = launch_auto.run(now=auto_at("2026-11-16T09:00"))
    check("another run" not in after_release and [payload.get("action") for payload in auto_posts] == ["check_reply"]
          and auto_sent() == [], "launch auto lock: the lock is released, the next run works and only reads")

    # A tap that lands while the slow proposal check is still running is not undone by that run
    auto_fresh()


    def auto_sync_with_tap(url, folder):
        launch_auto.action("launch:replied:demo", auto_at("2026-11-17T09:01"))
        return auto_sync(url, folder)


    launch_auto._sync_repo = auto_sync_with_tap
    try:
        launch_auto.run(now=auto_at("2026-11-17T09:00"))
    finally:
        launch_auto._sync_repo = auto_sync
    kept = launch_auto.load_state("demo")
    check(kept["reply"].get("status") == "replied" and kept["reply"].get("by_hand") is True
          and kept["proposal"].get("commit") == "abc1234" and kept.get("last_run"),
          "launch auto lock: 'an answer came' tapped during a slow check survives, and the check result is kept too")

    # A run that still holds old state cannot send the follow-up a second time: the marker on disk decides
    auto_fresh()
    stale = launch_auto.load_state("demo")
    tapped = launch_auto.action("launch:followup:demo", auto_at("2026-11-24T10:05"))
    late = launch_auto._send_followup("demo", auto_settings["reply"], auto_settings["mail"], stale,
                                      auto_at("2026-11-24T10:06"), only_if_no_answer=False)
    check(tapped == "Follow-up sent to help@office.test." and "never sent twice" in late and len(auto_sent()) == 1,
          "launch auto lock: a second sender with old state sends nothing (one follow-up, ever)")

    # Two senders at the very same moment: the one that does not get the lock sends nothing
    auto_fresh()
    auto_wait = launch_auto.FOLLOWUP_WAIT_SECONDS
    launch_auto.FOLLOWUP_WAIT_SECONDS = 0
    try:
        with launch_auto._only_one("followup"):
            blocked = launch_auto.action("launch:followup:demo", auto_at("2026-11-24T10:05"))
    finally:
        launch_auto.FOLLOWUP_WAIT_SECONDS = auto_wait
    check("nothing sent from here" in blocked and auto_sent() == []
          and not (launch_auto.load_state("demo").get("reply") or {}).get("followup_attempt_at"),
          "launch auto lock: while another sender works, this one sends nothing and leaves no false marker")

    # A relay that reads the inbox (version 3): it looks every run and follows up once, by itself
    auto_fresh()
    auto_world["version"] = 3
    launch_auto.run(now=auto_at("2026-11-16T09:00"))
    waiting = auto_row("auto-reply", auto_at("2026-11-16T09:00"))
    check([payload.get("action") for payload in auto_posts] == ["check_reply"] and auto_posts[0]["from"] == "help@office.test"
          and auto_posts[0]["since"] == "2026-11-14" and waiting["status"] == "auto" and "Tue 24 Nov" in waiting["detail"]
          and auto_row("auto-relay", auto_at("2026-11-16T09:00")) is None,
          "launch auto reply: a reading relay is asked for the answer and the row runs itself")
    launch_auto.run(now=auto_at("2026-11-24T07:00"))  # the date, but before office hours
    launch_auto.run(now=auto_at("2026-11-22T10:00"))  # office hours, but before the date
    check(auto_sent() == [], "launch auto reply: no follow-up before the date or outside office hours")
    launch_auto.run(now=auto_at("2026-11-24T10:00"))
    launch_auto.run(now=auto_at("2026-11-24T11:00"))
    launch_auto.run(now=auto_at("2026-11-25T10:00"))
    followups = [payload for payload in auto_posts if payload.get("action") == "followup"]
    check(len(followups) == 1 and len(auto_sent()) == 1 and followups[0]["to"] == "help@office.test"
          and followups[0]["from"] == "help@office.test" and followups[0]["since"] == "2026-11-14"
          and followups[0]["replyTo"] == "me@example.test" and '<a href="https://example.test/pack">' in followups[0]["html"],
          "launch auto reply: with no answer on the date, one follow-up is sent and never a second")
    check("Follow-up sent 2026-11-24 10:00" in auto_row("auto-reply", auto_at("2026-11-25T10:00"))["detail"],
          "launch auto reply: the row says when the follow-up went out")
    launch_auto.run(now=auto_at("2026-12-04T10:00"))
    check(auto_row("auto-reply", auto_at("2026-12-04T10:00"))["status"] == "done" and len(auto_sent()) == 1,
          "launch auto reply: no answer by the last day closes the step")

    auto_fresh()
    auto_world.update({"version": 3, "inbox": "replied"})
    launch_auto.run(now=auto_at("2026-11-24T10:00"))
    answered = auto_row("auto-reply", auto_at("2026-11-24T10:00"))
    check(auto_sent() == [] and "point 2 is fine" in (auto_home / "data" / "demo" / "reply.txt").read_text(encoding="utf-8")
          and answered["status"] == "yours" and answered["actions"][0]["name"] == "launch:open_reply:demo",
          "launch auto reply: an answer is saved, shown as mine to read, and no follow-up is sent")
    check(launch_auto.action("launch:open_reply:demo") == "Answer opened." and auto_opened[-1].name == "reply.txt"
          and auto_row("auto-reply", auto_at("2026-11-24T10:10"))["status"] == "done"
          and launch_auto.action("launch:followup:demo", auto_at("2026-11-24T10:20")).startswith("This mail is already settled"),
          "launch auto reply: opening the answer finishes the step")

    auto_fresh()
    auto_world.update({"version": 3, "at_send": "replied"})  # the answer lands between the check and the send
    launch_auto.run(now=auto_at("2026-11-24T10:00"))
    check(launch_auto.load_state("demo")["reply"]["status"] == "replied"
          and "followup_attempt_at" not in launch_auto.load_state("demo")["reply"],
          "launch auto reply: the relay's own last look wins, so an answer just before the send stops the follow-up")

    # The first mail never arrived: say so, send no follow-up, and let me close the step
    auto_fresh()
    auto_world.update({"version": 3, "inbox": "bounced"})
    launch_auto.run(now=auto_at("2026-11-24T10:00"))
    launch_auto.run(now=auto_at("2026-11-25T10:00"))
    returned = auto_row("auto-reply", auto_at("2026-11-25T10:00"))
    check(auto_sent() == [] and len(auto_posts) == 1 and returned["status"] == "yours" and "undelivered" in returned["title"]
          and [button["name"] for button in returned["actions"]] == ["launch:open_reply:demo", "launch:no_reply:demo"]
          and launch_auto.action("launch:followup:demo", auto_at("2026-11-25T10:05")).startswith("This mail is already settled"),
          "launch auto reply: a returned mail is reported, checked no further, and never followed up")
    check(launch_auto.action("launch:no_reply:demo") == "Noted: no answer. This step is closed."
          and "no follow-up" in auto_row("auto-reply", auto_at("2026-11-25T10:10"))["detail"],
          "launch auto reply: closing a returned mail does not claim a follow-up was sent")

    auto_fresh()
    auto_world.update({"version": 3, "at_send": "bounced"})  # the bounce is only seen by the relay's last look
    launch_auto.run(now=auto_at("2026-11-24T10:00"))
    late_bounce = launch_auto.load_state("demo")["reply"]
    check(late_bounce["status"] == "bounced" and "followup_sent_at" not in late_bounce and "followup_attempt_at" not in late_bounce,
          "launch auto reply: a bounce found at send time is not written down as a sent follow-up")

    auto_fresh()
    auto_world["version"] = 3
    launch_auto.run(now=auto_at("2026-11-16T09:00"))
    auto_world["version"] = 0  # offline: the GET gets no answer
    launch_auto.run(now=auto_at("2026-11-16T10:00"))
    offline_row = auto_row("auto-reply", auto_at("2026-11-16T10:00"))
    check(offline_row["status"] == "auto" and "the relay did not answer" in offline_row["detail"]
          and auto_row("auto-relay", auto_at("2026-11-16T10:00")) is None,
          "launch auto reply: offline is not mistaken for an old relay")

    # B. invitations: nobody invented, a preview to me first, each person once, never more than the target
    auto_fresh()
    launch_auto.run(now=auto_at("2026-11-16T09:00"))
    empty = auto_row("auto-invites", auto_at("2026-11-16T09:00"))
    check(empty["status"] == "yours" and empty["actions"][0]["name"] == "launch:open_contacts:demo" and auto_posts == [],
          "launch auto invites: an empty list asks me for people and mails nobody")
    check(launch_auto.action("launch:open_contacts:demo").startswith("Contact list opened")
          and "name,email,whatsapp" in (auto_home / "data" / "demo" / "contacts.csv").read_text(encoding="utf-8")
          and auto_row("auto-invites", auto_at("2026-11-16T09:01"))["status"] == "yours",
          "launch auto invites: the button makes an empty template (still nobody to invite)")
    share_urls = [entry["url"] for entry in empty["links"]]
    check(len(share_urls) == 3 and all(url.startswith("https://wa.me/?text=") for url in share_urls)
          and urllib.parse.unquote(share_urls[0].split("text=", 1)[1]) == "Hello, one test tonight? https://example.test/app"
          and empty["actions"][1] == {"name": "launch:shared:demo", "label": "I shared it"},
          "launch auto invites: an empty list offers WhatsApp share links that carry the text and no phone number")
    empty_card = hub._launch_view(launch_test, {"summary": launch_answer, "stale": False, "read_at": "now"}, now=launch_now,
                                  auto_rows=launch_auto.rows("demo", auto_at("2026-11-16T09:00")))
    empty_step = next(step for step in empty_card["needs_me"] if step["id"] == "auto-invites")
    check([entry["label"] for entry in empty_step["links"]] == [entry["label"] for entry in empty["links"]] + ["Texts"],
          "hub launch auto: a row with its own links also keeps the links of the step it replaces")
    shared_row = None
    if launch_auto.action("launch:shared:demo", auto_at("2026-11-28T10:30")) == "Noted: you shared the invitation yourself.":
        shared_row = auto_row("auto-invites", auto_at("2026-11-28T11:00"))
    check(shared_row is not None and shared_row["status"] == "done" and len(shared_row["links"]) == 3 and auto_posts == [],
          "launch auto invites: sharing it myself closes the step, and this PC still sends to nobody")
    people_lines = ["# my list", "name,email,whatsapp", "Ani,,0812-3456-7890", "Budi,,+62 813 0000 1111"]
    people_lines += [f"Friend {index},friend{index}@example.test," for index in range(1, 34)]
    people_lines += ["Friend 1 again,FRIEND1@example.test,", "Nobody,,", "Broken,not-an-address,"]
    (auto_home / "data" / "demo" / "contacts.csv").write_text("\n".join(people_lines) + "\n", encoding="utf-8")
    found = launch_auto.load_contacts(auto_home / "data" / "demo" / "contacts.csv", 30)
    check(len(found["people"]) == 30 and found["over"] == 5 and found["skipped"] == 3
          and found["people"][0] == {"name": "Ani", "email": "", "phone": "6281234567890"}
          and len({person["email"] or person["phone"] for person in found["people"]}) == 30,
          "launch auto invites: doubles and unusable lines dropped, never more than the target")
    launch_auto.run(now=auto_at("2026-11-16T10:00"))
    launch_auto.run(now=auto_at("2026-11-16T11:00"))
    check(len(auto_posts) == 1 and "to" not in auto_posts[0] and auto_posts[0]["subject"] == "Preview, please approve: Try it?"
          and "friend28@example.test" in auto_posts[0]["text"] and "friend29@example.test" not in auto_posts[0]["text"],
          "launch auto invites: one preview, with no recipient (the relay mails only its owner)")
    approve_row = auto_row("auto-invites", auto_at("2026-11-16T11:00"))
    whatsapp_row = auto_row("auto-invites-wa", auto_at("2026-11-16T11:00"))
    whatsapp_page = (auto_home / "data" / "demo" / "invites.html").read_text(encoding="utf-8")
    check(approve_row["status"] == "last-tap" and approve_row["actions"][0]["name"] == "launch:approve_invites:demo"
          and whatsapp_row["status"] == "last-tap" and "https://wa.me/6281234567890?text=Hello%20Ani%2C" in whatsapp_page
          and "https://wa.me/6281300001111?text=" in whatsapp_page and "example.test" not in approve_row["detail"],
          "launch auto invites: approval is one tap; WhatsApp people get ready links on a local page")
    launch_auto.run(now=auto_at("2026-11-28T10:05"))
    check(auto_sent() == [], "launch auto invites: nothing goes out without my approval, even after the start")
    check(launch_auto.action("launch:approve_invites:demo", auto_at("2026-11-20T09:00")).startswith("Approved: 28 invitations"),
          "launch auto invites: approving covers exactly the previewed list")
    launch_auto.run(now=auto_at("2026-11-27T10:00"))
    check(auto_sent() == [] and auto_row("auto-invites", auto_at("2026-11-27T10:00"))["status"] == "auto",
          "launch auto invites: approved invitations wait for the start time")
    auto_world["refuse"] = {"friend7@example.test"}
    launch_auto.run(now=auto_at("2026-11-28T10:05"))
    launch_auto.run(now=auto_at("2026-11-28T11:05"))
    auto_world["refuse"] = set()
    launch_auto.run(now=auto_at("2026-11-28T12:05"))
    invited = [payload["to"] for payload in auto_sent()]
    check(sorted(invited) == sorted(f"friend{index}@example.test" for index in range(1, 29)) and len(invited) == 28,
          "launch auto invites: at the start each previewed person is mailed once, across several runs")
    check(auto_sent()[0]["text"] == "Hello Friend 1, one test tonight? https://example.test/app" and auto_sent()[0]["subject"] == "Try it?",
          "launch auto invites: the mail is the approved text with the person's name")
    failed_row = auto_row("auto-invites", auto_at("2026-11-28T12:05"))
    check(failed_row["status"] == "blocked" and failed_row["title"] == "E-mail invitations: 27 of 28 sent" and "invite them by hand" in failed_row["detail"],
          "launch auto invites: a mail that failed is reported, not retried")
    check(launch_auto.action("launch:open_invites:demo") == "WhatsApp links opened." and auto_opened[-1].name == "invites.html"
          and launch_auto.action("launch:wa_done:demo") == "Noted: the WhatsApp invitations are sent."
          and auto_row("auto-invites-wa", auto_at("2026-11-28T12:10"))["status"] == "done",
          "launch auto invites: WhatsApp is opened by me and ticked off by me")

    auto_fresh(keep_contacts=True)
    launch_auto.run(now=auto_at("2026-11-16T10:00"))
    launch_auto.action("launch:approve_invites:demo", auto_at("2026-11-16T10:30"))
    contacts_file = auto_home / "data" / "demo" / "contacts.csv"
    contacts_file.write_text(contacts_file.read_text(encoding="utf-8").replace("friend2@example.test", "stranger@example.test"), encoding="utf-8")
    stale_tap = launch_auto.action("launch:approve_invites:demo", auto_at("2026-11-28T09:00"))
    launch_auto.run(now=auto_at("2026-11-28T10:05"))
    check(stale_tap.startswith("The list or the text changed") and auto_sent() == [] and len(auto_posts) == 2
          and "stranger@example.test" in auto_posts[1]["text"] and "to" not in auto_posts[1]
          and auto_row("auto-invites", auto_at("2026-11-28T10:05"))["status"] == "last-tap",
          "launch auto invites: a changed list voids the approval and asks again with a new preview")

    # C. the waiting proposal: checked daily, judged weekly, reported, never rewritten
    auto_fresh()
    auto_calls.update({"sync": 0, "judge": 0})
    launch_auto.run(now=auto_at("2026-11-16T09:00"))
    launch_auto.run(now=auto_at("2026-11-16T10:00"))
    healthy = auto_row("auto-proposal", auto_at("2026-11-16T10:00"))
    check(auto_calls == {"sync": 1, "judge": 1} and healthy["status"] == "auto" and healthy["actions"] == [{"name": "launch_run_now", "label": "Check again now"}]
          and "1 of 1 checks pass, 2 of 2 cited links answer" in healthy["detail"] and "Judge: 9.10 on 2026-11-16" in healthy["detail"]
          and "Say who you are in the first line." in healthy["detail"],
          "launch auto proposal: checked once a day, with the judge's score and top suggestion")
    check("Judge: 9.10 on 2026-11-16 at commit abc1234, 2 of 3 judges answered (best 9.10, 1 run)." in healthy["detail"]
          and "The page changed after that score" not in healthy["detail"],
          "launch auto proposal: the score names the commit it judged and how many judges answered")
    moved_on = launch_auto.load_state("demo")
    moved_on["proposal"]["commit"] = "def5678"
    launch_auto.save_state("demo", moved_on)
    check("at commit abc1234" in auto_row("auto-proposal", auto_at("2026-11-16T10:30"))["detail"]
          and "The page changed after that score; the weekly run judges the new text." in auto_row("auto-proposal", auto_at("2026-11-16T10:30"))["detail"],
          "launch auto proposal: a score for an older text is not shown as the score of the new one")
    moved_on["proposal"]["commit"] = "abc1234"
    launch_auto.save_state("demo", moved_on)
    launch_auto.run(force=True, now=auto_at("2026-11-16T11:00"))
    check(auto_calls == {"sync": 2, "judge": 1}, "launch auto proposal: 'Check again now' repeats the checks, not the weekly judge")
    auto_world["dead"] = {"https://example.test/law"}
    launch_auto.run(now=auto_at("2026-11-17T09:00"))
    check(auto_row("auto-proposal", auto_at("2026-11-17T09:00"))["status"] == "auto", "launch auto proposal: one slow day does not report a link")
    launch_auto.run(now=auto_at("2026-11-18T09:00"))
    dead_row = auto_row("auto-proposal", auto_at("2026-11-18T09:00"))
    check(dead_row["status"] == "blocked" and "link does not answer for 2 days: https://example.test/law" in dead_row["detail"],
          "launch auto proposal: a cited link that is gone for two days blocks the row")
    auto_world.update({"dead": set(), "checks_ok": False})
    launch_auto.run(now=auto_at("2026-11-19T09:00"))
    failing = auto_row("auto-proposal", auto_at("2026-11-19T09:00"))
    check(failing["status"] == "blocked" and 'check "pack check" fails (pack.md: a link is missing)' in failing["detail"],
          "launch auto proposal: a failing check blocks the row and says which one")
    auto_world.update({"checks_ok": True, "sync_ok": False})
    launch_auto.run(now=auto_at("2026-11-20T09:00"))
    unreachable = auto_row("auto-proposal", auto_at("2026-11-20T09:00"))
    check(unreachable["status"] == "blocked" and "GitHub was not reachable on 2026-11-20" in unreachable["detail"],
          "launch auto proposal: without GitHub it keeps the last check and says so")
    auto_world.update({"sync_ok": True, "score": 8.1})
    launch_auto.run(now=auto_at("2026-11-22T09:00"))
    check(auto_calls["judge"] == 1 and auto_row("auto-proposal", auto_at("2026-11-22T09:00"))["status"] == "auto",
          "launch auto proposal: the judge is not asked again within the week")
    launch_auto.run(now=auto_at("2026-11-23T09:00"))
    fallen = auto_row("auto-proposal", auto_at("2026-11-23T09:00"))
    check(auto_calls["judge"] == 2 and fallen["status"] == "blocked" and "score fell from 9.10 to 8.10" in fallen["detail"],
          "launch auto proposal: a clear fall in the weekly score is reported")
    quiet = {"proposal": {"checked_on": "2026-11-17", "checked_at": "2026-11-17 09:00", "commit": "8cb148e",
                          "checks": [{"name": "pack check", "ok": True}], "links": {"total": 11, "strikes": {}},
                          "scores": [{"date": "2026-11-10", "score": 9.75, "commit": "ee2de23", "judges": "3 of 3"},
                                     {"date": "2026-11-17", "score": 9.15, "commit": "14c35eb", "judges": "3 of 3"}]}}
    quiet_row = launch_auto.proposal_rows("demo", {"name": "Review pack", "judge": {"every_days": 7}}, quiet, auto_at("2026-11-17T09:00"))[0]
    quiet["proposal"]["scores"][-1]["score"] = 8.15
    loud_row = launch_auto.proposal_rows("demo", {"name": "Review pack", "judge": {"every_days": 7}}, quiet, auto_at("2026-11-17T09:00"))[0]
    check(quiet_row["status"] == "auto" and "score fell" not in quiet_row["detail"],
          "launch auto proposal: the 0.60 the judges differ by on near-identical pages (9.75 then 9.15) is not reported as a fall")
    check(loud_row["status"] == "blocked" and "score fell from 9.75 to 8.15" in loud_row["detail"],
          "launch auto proposal: a fall of 1.60 from the run before is still reported, against the previous run, not the best ever")
    check("Next weekly run Mon 23 Nov." in healthy["detail"] and auto_world["judge_budget"] >= 1200,
          "launch auto proposal: the row says when the next weekly run is due, and the judge gets more than the 549 s "
          "the slowest observed run needed in 600 s")

    # C2. a weekly run that gives no score is reported, not papered over with last week's score
    auto_fresh()
    auto_calls.update({"sync": 0, "judge": 0}); auto_world["judge_budget"] = 0
    auto_world["judge_ok"] = False
    launch_auto.run(now=auto_at("2026-11-16T09:00"))
    mute = auto_row("auto-proposal", auto_at("2026-11-16T09:00"))
    check(auto_calls["judge"] == 1 and mute["status"] == "blocked"
          and mute["title"] == "Pack: the weekly judge on Mon 16 Nov gave no score"
          and "it did not finish within 25 minutes (TimeoutExpired)" in mute["detail"]
          and "There is no score yet." in mute["detail"],
          "launch auto proposal: a judge that gives no score is shown on the row, with the reason")
    auto_world["judge_ok"] = True
    launch_auto.run(force=True, now=auto_at("2026-11-16T10:00"))
    again = auto_row("auto-proposal", auto_at("2026-11-16T10:00"))
    check(auto_calls["judge"] == 2 and again["status"] == "auto" and "Judge: 9.10 on 2026-11-16" in again["detail"],
          "launch auto proposal: 'Check again now' asks a judge that gave no score again, on the same day")
    auto_world["judge_ok"] = False
    launch_auto.run(now=auto_at("2026-11-23T09:00"))
    missed = auto_row("auto-proposal", auto_at("2026-11-23T09:00"))
    check(auto_calls["judge"] == 3 and missed["status"] == "blocked"
          and missed["title"] == "Pack: the weekly judge on Mon 23 Nov gave no score"
          and "The last score is 9.10 on 2026-11-16 at commit abc1234, so this week has no score of its own." in missed["detail"]
          and "letters are unchanged" in missed["detail"],
          "launch auto proposal: a missed weekly run says the newest score is not this week's one, and the letters stand")
    auto_world["judge_ok"] = True

    # D. the dashboard card and its buttons
    auto_fresh(keep_contacts=True)
    auto_world["version"] = 3
    launch_auto.run(now=auto_at("2026-11-16T09:00"))
    card_rows = launch_auto.rows("demo", auto_at("2026-11-16T09:00"))
    card = hub._launch_view(launch_test, {"summary": launch_answer, "stale": False, "read_at": "now"}, now=launch_now, auto_rows=card_rows)
    check([step["id"] for step in card["needs_me"]] == ["name", "seen", "auto-reply", "auto-invites", "auto-invites-wa", "auto-proposal"]
          and [step["status"] for step in card["needs_me"]] == ["blocked", "done", "auto", "last-tap", "last-tap", "auto"]
          and card["open"] == 3 and card["automatic"] == 2,
          "hub launch auto: automation rows replace the public steps they take over; 'runs itself' is not counted as open")
    card_invite = card["needs_me"][3]
    check(card_invite["automation"] and card_invite["private"] and [entry["label"] for entry in card_invite["links"]] == ["Texts"]
          and card_invite["actions"][0] == {"name": "launch:approve_invites:demo", "label": "Approve the invitations"}
          and card["needs_me"][2]["status_label"] == "runs itself",
          "hub launch auto: a row keeps the links of the step it replaces and carries its buttons")
    spoofed = hub._launch_view(launch_test, {"summary": {**launch_answer, "needsMe": [
        {"id": "x", "title": "Trust me", "status": "auto", "replaces": "name", "actions": [{"name": "launch:followup:demo", "label": "Send"}]}]},
        "stale": False, "read_at": "now"}, now=launch_now)
    check(spoofed["needs_me"][0]["status"] == "yours" and spoofed["needs_me"][0]["actions"] == [] and not spoofed["needs_me"][0]["automation"]
          and spoofed["needs_me"][0]["replaces"] is None and spoofed["open"] == 1,
          "hub launch auto: a public summary cannot claim 'runs itself' or put a button on the page")
    filtered = hub._launch_steps([{"title": "Row", "status": "auto", "actions": [
        {"name": "patents_run_now", "label": "no"}, {"name": "launch:followup:demo\" onclick=\"x", "label": "no"}, "junk",
        {"name": "launch:followup:demo", "label": "L" * 90}, {"name": "launch_run_now"}]}], True, trusted=True)[0]
    check([entry["name"] for entry in filtered["actions"]] == ["launch:followup:demo", "launch_run_now"]
          and len(filtered["actions"][0]["label"]) == 60 and filtered["actions"][1]["label"] == "Do it",
          "hub launch auto: only launch buttons with a well-formed name reach the page")
    auto_todo = hub.launch_todo({"tests": [card]}, today=launch_now.date())
    check([item["title"] for item in auto_todo] == ["Demo app: Trademark search",
                                                    "Demo app: E-mail invitations: approve the preview (28 people)",
                                                    "Demo app: WhatsApp invitations: tap 2 ready links on Sat 28 Nov 10:00"]
          and auto_todo[1]["action"] == "launch:approve_invites:demo" and auto_todo[1]["action_label"] == "Approve the invitations"
          and auto_todo[0]["action"] is None,
          "hub launch auto: only rows that need a tap become to-dos, with their button")
    check(hub.run_action("launch:replied:demo") == "Noted: an answer came. No follow-up will be sent."
          and launch_auto.load_state("demo")["reply"]["by_hand"] is True
          and auto_row("auto-reply", auto_at("2026-11-16T09:30"))["actions"][0]["name"] == "launch:reply_read:demo"
          and hub.run_action("launch:reply_read:demo") == "Marked as read.",
          "hub launch auto: the card's buttons reach the automation")
    for refused in ("launch:followup:other", "launch:followup:../x", "launch:nonsense:demo", "launch:followup", "launch:FOLLOWUP:demo",
                    "launch:followup:demo:extra", "launch:followup:Bad Id"):
        try:
            hub.run_action(refused)
            check(False, f"hub launch auto: malformed action refused ({refused})")
        except ValueError:
            check(True, f"hub launch auto: malformed action refused ({refused})")
    check(len(hub._launch_auto_rows("demo")) >= 3 and hub._launch_auto_rows("nobody") == [], "hub launch auto: rows are read for a configured test only")
    launch_auto.rows = lambda test_id, now=None: 1 / 0
    check(hub._launch_auto_rows("demo") == [], "hub launch auto: a broken automation does not break the card")
    launch_auto.rows = auto_real[-1]
    check("runs itself" in hub.help_sections({}, None, {"tests": [card]})[-1]["auto"], "hub launch auto: the help entry explains the hourly automations")

    # the reminder to the owner: one mail per step, on the day the step needs a hand that this PC does not have
    auto_plain_config = launch_auto.PRIVATE_FILE.read_text(encoding="utf-8")
    nudge_settings = {**auto_settings, "mail": {**auto_settings["mail"], "nudge": True}}
    launch_auto.PRIVATE_FILE.write_text(json.dumps({"demo": {"needsMe": [], "auto": nudge_settings}}), encoding="utf-8")
    auto_fresh()
    launch_auto.run(now=auto_at("2026-11-23T10:00"))
    launch_auto.run(now=auto_at("2026-11-24T09:00"))
    check(auto_posts == [], "launch auto nudge: nothing is mailed before the day, or on the day before the reminder hours")
    nudge_run = launch_auto.run(now=auto_at("2026-11-24T10:00"))
    first_nudge = list(auto_posts)
    launch_auto.run(now=auto_at("2026-11-24T11:00"))
    launch_auto.run(now=auto_at("2026-11-25T10:00"))
    check(len(first_nudge) == 1 and auto_posts == first_nudge and "to" not in first_nudge[0] and "action" not in first_nudge[0]
          and "Send the follow-up" in first_nudge[0]["text"] and '"No answer: send the follow-up"' in first_nudge[0]["text"]
          and "nudge: 2 steps mailed to the owner" in nudge_run,
          "launch auto nudge: on the day, one mail to the owner only, naming each step and its button, never repeated")
    launch_auto.run(now=auto_at("2026-11-28T10:00"))
    check(len(auto_posts) == 2 and "to" not in auto_posts[1] and "https://wa.me/?text=" in auto_posts[1]["text"]
          and auto_posts[1]["subject"] == "Launch test demo: 1 step for you today",
          "launch auto nudge: the invitation day brings the share links, and a step already mailed is left out")
    auto_fresh()
    auto_world["relay_ok"] = False
    failed_nudges = [launch_auto.run(now=auto_at(f"2026-11-24T{hour}:00")) for hour in (10, 11, 12, 13)]
    auto_world["relay_ok"] = True
    launch_auto.run(now=auto_at("2026-11-24T14:00"))
    check(len(auto_posts) == launch_auto.NUDGE_TRIES == 3 and "nudge: could not be mailed" in failed_nudges[2]
          and "nudge" not in failed_nudges[3],
          "launch auto nudge: a reminder that could not be mailed is tried three times, then left to the card")
    auto_fresh()
    launch_auto.action("launch:replied:demo", auto_at("2026-11-20T09:00"))
    launch_auto.run(now=auto_at("2026-11-24T10:00"))
    check(auto_posts == [], "launch auto nudge: a step that is settled is not mailed")
    auto_fresh()
    auto_world["version"] = 3
    launch_auto.run(now=auto_at("2026-11-24T10:00"))
    check(len(auto_posts) == 2 and all(payload.get("action") for payload in auto_posts),
          "launch auto nudge: a relay that reads the inbox does the step itself, so there is no reminder")
    launch_auto.PRIVATE_FILE.write_text(auto_plain_config, encoding="utf-8")
finally:
    (launch_auto.DATA_DIR, launch_auto.PRIVATE_FILE, launch_auto.LOG_FILE, launch_auto._relay_version, launch_auto._relay_post,
     launch_auto._open, launch_auto._sync_repo, launch_auto._run_command, launch_auto._link_answers,
     launch_auto.SEND_PAUSE_SECONDS, launch_auto.rows) = auto_real

auto_page = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")
check(all(piece in auto_page for piece in ('auto: "good"', "s.automation", "actionButton(a.name, a.label, true)", "run by this PC")),
      "dashboard: launch rows show 'automatic', their detail and their buttons")
check('class="hint step-detail"' in auto_page
      and "td .badge + .hint.step-detail { white-space: normal; overflow-wrap: anywhere; }" in auto_page,
      "dashboard: an automation's detail wraps instead of running off the card")
auto_source = (Path(__file__).parent / "launch_auto.py").read_bytes()
check(not auto_source.startswith(b"\xef\xbb\xbf") and "\u00c3" not in auto_source.decode("utf-8")
      and "\u00e2\u20ac" not in auto_source.decode("utf-8"), "launch_auto.py: clean UTF-8, no byte-order mark")
check(b"New-ScheduledTaskSettingsSet -Priority 4 " in auto_source,
      "launch auto: the hourly task is installed at normal priority, so it finishes on a busy PC")
auto_ignored = (Path(__file__).parent / ".gitignore").read_text(encoding="utf-8").split()
check("data/launch_auto/" in auto_ignored and "launch_auto.log" in auto_ignored and "launch_private.json" in auto_ignored,
      "launch auto: contacts, answers, state and the log are git-ignored")
check(("launch" in hub.TASKS) == (hub.ROOT / "data" / "launch_auto").exists(),
      "hub launch auto: the hourly task is listed only where it is installed")

# A busy PC that cannot answer in time is not a missing task
busy_run = hub.subprocess.run
try:
    def no_answer(*args, **kwargs):
        raise subprocess.TimeoutExpired("schtasks", 10)
    hub.subprocess.run = no_answer
    hub._cache.pop("tasks", None)
    busy_tasks = hub.task_status()
    check(busy_tasks and all(task == {"name": task["name"], "installed": False, "unknown": True} for task in busy_tasks.values()),
          "hub tasks: no answer from Task Scheduler is recorded as unknown, not as a missing task")
    busy_todo = [item["title"] for item in hub.todo_items(None, {"installed": False}, {"installed": False}, busy_tasks)]
    check(not any("is missing" in title for title in busy_todo)
          and busy_todo.count(f"Could not check {len(busy_tasks)} scheduled tasks") == 1,
          "hub tasks: a busy PC gives one 'could not check' line and no false 'is missing' alarm")
    hub.subprocess.run = lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", "ERROR: The system cannot find the file specified.")
    hub._cache.pop("tasks", None)
    gone_tasks = hub.task_status()
    gone_todo = [item["title"] for item in hub.todo_items(None, {"installed": False}, {"installed": False}, gone_tasks)]
    check(all(task["installed"] is False and task["unknown"] is False for task in gone_tasks.values())
          and sum("is missing" in title for title in gone_todo) == len(gone_tasks)
          and not any(title.startswith("Could not check") for title in gone_todo),
          "hub tasks: a task that Task Scheduler says is not there is still reported as missing")
finally:
    hub.subprocess.run = busy_run
    hub._cache.pop("tasks", None)

# A run that Windows started at low priority puts itself back at normal
if sys.platform == "win32":
    import ctypes
    priority_kernel = ctypes.windll.kernel32
    priority_kernel.GetCurrentProcess.restype = ctypes.c_void_p
    priority_kernel.GetPriorityClass.argtypes = [ctypes.c_void_p]
    priority_kernel.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    this_test = priority_kernel.GetCurrentProcess()
    priority_before = priority_kernel.GetPriorityClass(this_test)
    try:
        priority_kernel.SetPriorityClass(this_test, 0x4000)
        raised = launch_auto.leave_low_priority()
        check(raised and priority_kernel.GetPriorityClass(this_test) == 0x20,
              "launch auto: a run started below normal priority puts itself back at normal")
        check(launch_auto.leave_low_priority() is False and priority_kernel.GetPriorityClass(this_test) == 0x20,
              "launch auto: a run that is already at normal priority is left alone")
    finally:
        priority_kernel.SetPriorityClass(this_test, priority_before)
else:
    check(launch_auto.leave_low_priority() is False, "launch auto: priority is left alone outside Windows")
auto_main = auto_source.decode("utf-8").split('if __name__ == "__main__":')[1]
check(auto_main.index("leave_low_priority()") < auto_main.index("run(force="),
      "launch auto: the hourly run fixes its priority before it starts working")
tick_main = (Path(__file__).parent / "autopilot.py").read_text(encoding="utf-8").split("def main() -> None:")[1]
check(tick_main.index('if args.command == "tick":\n        if LOCK_PATH') < tick_main.index("launch_auto.leave_low_priority()")
      < tick_main.index("tick(config, db)"),
      "autopilot: the scheduled post run fixes its priority before it makes a video, a preview by hand does not")

phone_main = (Path(__file__).parent / "phone_snapshot.py").read_text(encoding="utf-8").split('if __name__ == "__main__":')[1]
check(phone_main.index("leave_low_priority()") < phone_main.index("build("),
      "phone page: the build fixes its priority first, so Task Scheduler answers it on a busy PC")

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
