"""Offline-safe checks: README extraction, composer guards, little-text, analytics parsing, scoring."""
import json
import os
import random
import sys
import sqlite3
import tempfile
from pathlib import Path

from openpyxl import Workbook

import analytics
import composer
import github_source as gh
from linkedin_api import to_little_text
import trylinks

config = json.loads((Path(__file__).parent / "config.json").read_text(encoding="utf-8"))
failures = []


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
limits = {"bluesky": 300, "mastodon": 500, "telegram": 1024, "discord": 2000}
for name, limit in limits.items():
    size = len(variants.get(name, {}).get("body", ""))
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

        def publish(self, text, title, images, link, extra, idempotency_key):
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

todo = hub.channels_todo({
    "not_connected": ["Bluesky"],
    "failed": [{"slot": "2026-11-11 08:00", "channel": "Mastodon", "error": "HTTP 503"}],
})
check([item["action"] for item in todo] == ["channels_setup", "channels_setup"], "hub: platform to-dos (connect, failures)")

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
                                                  "google.com", "github.com", "example.org")]
      == ["LinkedIn", "LinkedIn", "Mastodon", "Bluesky", "Dev.to", "Search", "GitHub", "Other"],
      "growth: GitHub referrers mapped to platforms")
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

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
