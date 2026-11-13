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
check(out == text and "go/watt-reserve" in out,
      f"track_links: a failed go-page sync keeps the plain link and does not crash ({out!r})")
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

print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
sys.exit(1 if failures else 0)
