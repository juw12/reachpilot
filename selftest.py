"""Offline-safe checks: README extraction, composer guards, little-text, analytics parsing, scoring."""
import json
import os
import random
import sys
import sqlite3
import tempfile
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

import linkedin_api  # noqa: E402
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
with mock.patch("visuals.record_demo", side_effect=lambda url, output: output) as recorder:
    recorded = visuals.demo_for_post("launch", [facts], Path("out"))
    skipped = [
        visuals.demo_for_post("roundup", [facts], Path("out")),
        visuals.demo_for_post("launch", [facts, github_only], Path("out")),
        visuals.demo_for_post("launch", [github_only], Path("out")),
        visuals.demo_for_post("launch", [no_site], Path("out")),
    ]
check(recorded == Path("out") / "watt-reserve_demo.mp4" and recorder.call_args[0][0] == facts.homepage,
      "demo video: a single-project post records its live site")
check(skipped == [None] * 4 and recorder.call_count == 1,
      "demo video: roundups and projects without a live site get no recording")

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
        with mock.patch("autopilot.visuals.images_for_post", return_value=[shot]):
            with mock.patch("autopilot.visuals.demo_for_post", return_value=clip):
                media = autopilot.build_images({"github_user": "juw12"}, choice, "2026-12-01 08:00")
            with mock.patch("autopilot.visuals.demo_for_post", side_effect=RuntimeError("no browser")):
                media_without = autopilot.build_images({"github_user": "juw12"}, choice, "2026-12-01 08:00")
            with mock.patch("autopilot.visuals.demo_for_post", return_value=clip) as switched_off:
                media_off = autopilot.build_images({"github_user": "juw12", "demo_video": False}, choice, "2026-12-01 08:00")
        check(media == [str(clip), str(shot)], "autopilot: the demo video comes first in a post's media")
        check(media_without == [str(shot)], "autopilot: a failed recording still posts the screenshots")
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

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
