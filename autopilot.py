"""ReachPilot - LinkedIn posting module for github.com/juw12.

Runs every 30 minutes from Windows Task Scheduler (`autopilot.py install`). Each run:
  1. syncs GitHub (new repos, new releases, real commit activity),
  2. ingests any LinkedIn analytics export dropped in analytics_inbox/,
  3. if a posting slot is due, picks the best content and posts it via the official API:
       seed queue -> new repo -> new release -> active repo update -> top-performer roundup
       -> recycled project with a fresh angle (Thompson sampling on your engagement data),
  4. never posts near-duplicates, invented numbers, or more than the weekly cap.

Each post also goes to the other platforms you connected (channels.py): Bluesky, Mastodon, Dev.to, Telegram,
Discord, and X, Threads and Substack (free through Buffer). Each platform publishes and retries on its own.

A post about one project with a live site also gets a demo video: a short screen recording of the site being used
(visuals.record_demo). The video leads the post wherever the platform takes one and the screenshots are the fallback.
Switch it off with "demo_video": false in config.json.

Commands:  init | auth | channels | tick | preview [N] | status | install | uninstall
Kill switch: create a file named PAUSE in this folder.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import analytics  # noqa: E402
import channels  # noqa: E402
import composer  # noqa: E402
import github_source as gh  # noqa: E402
import growth  # noqa: E402
import trylinks  # noqa: E402
import visuals  # noqa: E402

CONFIG_PATH = ROOT / "config.json"
SECRETS_PATH = ROOT / "secrets.json"
TOKEN_PATH = ROOT / "token.json"
DB_PATH = ROOT / "state.db"
SEED_PATH = ROOT / "seed_queue.json"
ASSETS = ROOT / "assets"
GENERATED = ROOT / "generated"
DRAFTS = ROOT / "drafts"
INBOX = ROOT / "analytics_inbox"
PAUSE_FLAG = ROOT / "PAUSE"
LOCK_PATH = ROOT / ".tick.lock"
PAGES_CHECKOUT = ROOT / ".pages"
TASK_NAME = "LinkedIn posts"
TASK_PATH = "\\ReachPilot\\"
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

log = logging.getLogger("autopilot")


# ------------------------------------------------------------------ plumbing

def setup_logging() -> None:
    handler = RotatingFileHandler(ROOT / "autopilot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    if sys.stdout and sys.stdout.isatty():
        log.addHandler(logging.StreamHandler(sys.stdout))
    log.setLevel(logging.INFO)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def connect_db() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS queue(id TEXT PRIMARY KEY, slot_date TEXT, repo TEXT, angle TEXT,
            body TEXT, comment TEXT, images TEXT, used INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS repos(name TEXT PRIMARY KEY, first_seen TEXT, pushed_at TEXT, release_tag TEXT);
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, repo TEXT, kind TEXT,
            detail TEXT, created_at TEXT, consumed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS posts(slot TEXT PRIMARY KEY, source TEXT, repo TEXT, angle TEXT, body TEXT,
            comment TEXT, images TEXT, status TEXT, attempts INTEGER DEFAULT 0, urn TEXT, url TEXT,
            posted_at TEXT, error TEXT, used_bullets TEXT);
        CREATE TABLE IF NOT EXISTS metrics(url TEXT PRIMARY KEY, post_date TEXT, impressions INTEGER,
            engagements INTEGER, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS github_signals(day TEXT, repo TEXT, li_views INTEGER, li_uniques INTEGER,
            stars INTEGER, PRIMARY KEY(day, repo));
        CREATE TABLE IF NOT EXISTS try_clicks(day TEXT, repo TEXT, clicks INTEGER, PRIMARY KEY(day, repo));
        """
    )
    channels.ensure_schema(db)
    return db


def kv_get(db, key, default=None):
    row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(db, key, value) -> None:
    db.execute("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, str(value)))
    db.commit()


def notify(title: str, message: str) -> None:
    """Windows toast; best effort, never blocks."""
    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")[:240]
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$nodes = $template.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($template.CreateTextNode('{safe_title}')) > $null
$nodes.Item(1).AppendChild($template.CreateTextNode('{safe_message}')) > $null
$appId = '{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show([Windows.UI.Notifications.ToastNotification]::new($template))
"""
    try:
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=gh.CREATE_NO_WINDOW,
        )
    except OSError:
        pass
    log.info("NOTIFY %s: %s", title, message)


def local_now(config: dict) -> datetime:
    """Wall-clock time in the configured zone (WIB by default), independent of the PC's zone."""
    return datetime.now(timezone.utc) + timedelta(hours=config["timezone_offset_hours"])


# ------------------------------------------------------------------ schedule

def due_slot(config: dict, db, now: datetime) -> str | None:
    for days_back in (0, 1):
        day = (now - timedelta(days=days_back)).replace(tzinfo=None)
        for slot in config["slots"]:
            if WEEKDAYS[day.weekday()] != slot["weekday"]:
                continue
            hour, minute = map(int, slot["time"].split(":"))
            start = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            now_naive = now.replace(tzinfo=None)
            if start <= now_naive < start + timedelta(hours=config["catch_up_hours"]):
                key = start.strftime("%Y-%m-%d %H:%M")
                row = db.execute("SELECT status, attempts FROM posts WHERE slot=?", (key,)).fetchone()
                if row is None or row[0] in ("ready", "awaiting_approval") or (row[0] == "failed" and row[1] < 3):
                    return key
    return None


def posts_this_week(db, now: datetime) -> int:
    """Posts since Monday 00:00 of the current week.

    A rolling 7-day window would count last Tuesday's 08:0x post at this Tuesday's 08:00 slot
    and skip it, so the cap is applied per calendar week instead.
    """
    week_start = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d 00:00")
    return db.execute("SELECT COUNT(*) FROM posts WHERE status='posted' AND posted_at>=?", (week_start,)).fetchone()[0]


# ------------------------------------------------------------------ GitHub sync

def sync_github(config: dict, db, force: bool = False) -> dict[str, gh.RepoFacts]:
    """Returns facts for all eligible repos; records new repos/releases as events."""
    user = config["github_user"]
    repos = gh.list_public_repos(user, config["exclude_repos"])
    if not repos:
        log.warning("GitHub returned no repos (offline or gh not authenticated)")
        return {}
    first_sync = db.execute("SELECT COUNT(*) FROM repos").fetchone()[0] == 0
    known = {row[0]: row for row in db.execute("SELECT name, first_seen, pushed_at, release_tag FROM repos")}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for repo in repos:
        name = repo["name"]
        if name not in known:
            db.execute("INSERT INTO repos(name, first_seen, pushed_at) VALUES(?,?,?)", (name, stamp, repo["pushed_at"]))
            if not first_sync:
                db.execute("INSERT INTO events(repo, kind, detail, created_at) VALUES(?,?,?,?)", (name, "new_repo", "{}", stamp))
                log.info("New repo detected: %s", name)
        changed = name not in known or known[name][2] != repo["pushed_at"]
        if changed or force or first_sync:
            release = gh.latest_release(user, name)
            previous_tag = known.get(name, (None, None, None, None))[3]
            if release and release["tag"] != previous_tag:
                if not first_sync and name in known:
                    db.execute(
                        "INSERT INTO events(repo, kind, detail, created_at) VALUES(?,?,?,?)",
                        (name, "release", json.dumps(release), stamp),
                    )
                    log.info("New release detected: %s %s", name, release["tag"])
                db.execute("UPDATE repos SET release_tag=? WHERE name=?", (release["tag"], name))
            db.execute("UPDATE repos SET pushed_at=? WHERE name=?", (repo["pushed_at"], name))
    db.commit()
    kv_set(db, "last_github_sync", stamp)
    return {repo["name"]: repo for repo in repos}


# ------------------------------------------------------------------ content selection

class Picker:
    def __init__(self, config, db, repos: dict, now: datetime, rng: random.Random):
        self.config = config
        self.db = db
        self.repos = repos
        self.now = now
        self.rng = rng
        self.user = config["github_user"]
        self.links_in_comment = config["links_in_first_comment"]
        self.history = [row[0] for row in db.execute("SELECT body FROM posts WHERE status='posted'")]
        self.history += [row[0] for row in db.execute("SELECT body FROM queue")]
        self.facts_cache: dict[str, gh.RepoFacts] = {}
        self.simulated_recent: set[str] = set()
        records = analytics.performance(db)
        self.repo_scores = analytics.ThompsonScorer(records, "repo")
        self.angle_scores = analytics.ThompsonScorer(records, "angle")

    def facts(self, name: str) -> gh.RepoFacts | None:
        if name not in self.facts_cache:
            repo = self.repos.get(name)
            self.facts_cache[name] = gh.repo_facts(self.user, repo) if repo else None
        return self.facts_cache[name]

    def last_post_days(self, repo: str) -> float:
        if repo in self.simulated_recent:
            return 0.0
        row = self.db.execute("SELECT MAX(posted_at) FROM posts WHERE status='posted' AND repo=?", (repo,)).fetchone()
        if not row[0]:
            seeded = self.db.execute("SELECT MAX(slot_date) FROM queue WHERE used=1 AND repo=?", (repo,)).fetchone()[0]
            if not seeded:
                return 9999.0
            row = (seeded + " 08:00",)
        last = datetime.strptime(row[0][:16], "%Y-%m-%d %H:%M")
        return (self.now.replace(tzinfo=None) - last).total_seconds() / 86400

    def used_bullets(self, repo: str) -> set[str]:
        used = set()
        for (value,) in self.db.execute("SELECT used_bullets FROM posts WHERE repo=? AND used_bullets IS NOT NULL", (repo,)):
            used.update(json.loads(value))
        return used

    def acceptable(self, body: str | None, facts: gh.RepoFacts | None, extra_source: str = "") -> bool:
        if not body or len(body) < 250 or len(body) > 2800:
            return False
        source = (facts.source_text() if facts else "") + " " + extra_source
        if facts and composer.invented_numbers(body, source):
            log.info("Rejected draft (numbers not in source): %s", composer.invented_numbers(body, source))
            return False
        score = composer.max_similarity(body, self.history)
        if score > self.config["max_similarity"]:
            log.info("Rejected draft (too similar to an earlier post: %.2f)", score)
            return False
        return True

    def finish(self, source, repo, angle, body, facts_list, extra_comment=None, used_bullets=None):
        facts = facts_list[0] if facts_list else None
        polished = composer.polish_with_ollama(body, facts.source_text() if facts else "", self.config.get("ollama_model", ""))
        if polished and self.acceptable(polished, facts):
            body = polished
        tags = composer.hashtags_for(facts, self.config["default_hashtags"], self.rng)
        comment = extra_comment or (composer.links_for(facts) if facts else f"https://github.com/{self.user}")
        if not self.links_in_comment:
            body = body + "\n\n" + comment
            comment = ""
        return {
            "source": source,
            "repo": repo,
            "angle": angle,
            "body": body + "\n\n" + tags,
            "comment": comment,
            "facts": facts_list,
            "used_bullets": sorted(used_bullets or []),
        }

    # -- candidates, in priority order --------------------------------------
    def from_seed_queue(self, slot_date: str):
        row = self.db.execute(
            "SELECT id, repo, angle, body, comment, images FROM queue WHERE used=0 AND slot_date<=? ORDER BY slot_date, id LIMIT 1",
            (slot_date,),
        ).fetchone()
        if not row:
            return None
        queue_id, repo, angle, body, comment, images = row
        return {"source": f"queue:{queue_id}", "repo": repo, "angle": angle, "body": body, "comment": comment,
                "images": [str(ASSETS / name) for name in json.loads(images)], "facts": [], "used_bullets": []}

    def from_events(self):
        for event_id, repo, kind, detail in self.db.execute(
            "SELECT id, repo, kind, detail FROM events WHERE consumed=0 ORDER BY id"
        ).fetchall():
            facts = self.facts(repo)
            if facts is None:
                self.db.execute("UPDATE events SET consumed=1 WHERE id=?", (event_id,))
                continue
            if self.last_post_days(repo) < self.config["min_days_between_posts_same_repo"]:
                continue
            if kind == "new_repo":
                if len(facts.bullets) < 3 or not facts.description:
                    log.info("New repo %s has too little README to post about yet; waiting", repo)
                    continue
                body = composer.compose_launch(facts, self.rng, set(), self.links_in_comment)
                angle = "launch"
            else:
                release = json.loads(detail)
                changes = gh.release_notes_bullets(release["body"]) or gh.meaningful_commits(
                    self.user, repo, (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
                )
                body = composer.compose_update(facts, self.rng, changes, release["tag"], self.links_in_comment)
                angle = "update"
                if body is None:
                    continue
            changes_text = " ".join(changes) + " " + release["tag"] if kind == "release" else ""
            if self.acceptable(body, facts, changes_text):
                self.db.execute("UPDATE events SET consumed=1 WHERE id=?", (event_id,))
                return self.finish(f"event:{kind}", repo, angle, body, [facts])
        return None

    def from_activity(self):
        recent = sorted(self.repos.values(), key=lambda repo: repo["pushed_at"], reverse=True)[:6]
        for repo in recent:
            name = repo["name"]
            days = self.last_post_days(name)
            if days < 14:
                continue
            since = self.now - timedelta(days=min(days, 45))
            changes = gh.meaningful_commits(self.user, name, since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
            if len(changes) < self.config["update_min_commits"]:
                continue
            facts = self.facts(name)
            body = composer.compose_update(facts, self.rng, changes[:5], None, self.links_in_comment)
            if self.acceptable(body, facts, " ".join(changes)):
                return self.finish("activity", name, "update", body, [facts])
        return None

    def roundup(self):
        every = self.config["roundup_every_n_posts"]
        generated = self.db.execute("SELECT COUNT(*) FROM posts WHERE status='posted' AND source NOT LIKE 'queue:%'").fetchone()[0]
        last_roundup = self.db.execute("SELECT MAX(posted_at) FROM posts WHERE angle='roundup' AND status='posted'").fetchone()[0]
        if generated == 0 or generated % every != every - 1:
            return None
        if last_roundup and (self.now.replace(tzinfo=None) - datetime.strptime(last_roundup[:16], "%Y-%m-%d %H:%M")).days < 28:
            return None
        top_names = [name for name, _ in self.repo_scores.ranked() if name in self.repos][:4]
        if len(top_names) < 3:
            return None
        top = [self.facts(name) for name in top_names]
        body = composer.compose_roundup(top, self.rng, self.user)
        if self.acceptable(body, None):
            comment = "\n".join(f"{facts.display_name}: {facts.homepage or facts.html_url}" for facts in top)
            return self.finish("roundup", None, "roundup", body, top, extra_comment=comment)
        return None

    def recycle(self):
        candidates = []
        for name in self.repos:
            if self.last_post_days(name) < self.config["project_cooldown_days"]:
                continue
            candidates.append((self.repo_scores.sample(name, self.rng), name))
        for _, name in sorted(candidates, reverse=True)[:8]:
            facts = self.facts(name)
            if not facts or not facts.description or len(facts.bullets) < 2:
                continue
            used = self.used_bullets(name)
            last_angle = self.db.execute(
                "SELECT angle FROM posts WHERE repo=? AND status='posted' ORDER BY posted_at DESC LIMIT 1", (name,)
            ).fetchone()
            angles = sorted(composer.RECYCLE_ANGLES, key=lambda angle: self.angle_scores.sample(angle, self.rng), reverse=True)
            if last_angle:
                angles = [angle for angle in angles if angle != last_angle[0]] + [last_angle[0]]
            for angle in angles:
                for _attempt in range(4):
                    builder = {
                        "launch": composer.compose_launch,
                        "deep_dive": composer.compose_deep_dive,
                        "behind_the_build": composer.compose_behind_the_build,
                        "feedback": composer.compose_feedback,
                    }[angle]
                    body = builder(facts, self.rng, used, self.links_in_comment)
                    if self.acceptable(body, facts):
                        bullets_used = {bullet for bullet in facts.bullets if bullet in body}
                        return self.finish("recycle", name, angle, body, [facts], used_bullets=bullets_used)
        return None

    def pick(self, slot: str):
        slot_date = slot[:10]
        # Brand-new repos and releases jump ahead of the prepared queue.
        for step in (self.from_events, lambda: self.from_seed_queue(slot_date), self.from_activity, self.roundup, self.recycle):
            choice = step()
            if choice:
                return choice
        return None


def build_images(config, choice, slot: str) -> list[str]:
    """The post's media: the demo video first (when the project has a live site), then the screenshots."""
    if choice.get("images"):
        return choice["images"]
    out_dir = GENERATED / slot.replace(":", "").replace(" ", "_")
    try:
        paths = visuals.images_for_post(config["github_user"], choice["angle"], choice["facts"], out_dir)
    except Exception as error:
        log.warning("Image capture failed (%s); posting text-only", error)
        paths = []
    video = None
    if config.get("demo_video", True):
        try:
            video = visuals.demo_for_post(choice["angle"], choice["facts"], out_dir)
        except Exception as error:
            log.warning("Demo video failed (%s); posting the screenshots only", error)
    return [str(path) for path in ([video] if video else []) + list(paths)]


# ------------------------------------------------------------------ posting

def load_token() -> dict | None:
    if not TOKEN_PATH.exists():
        return None
    return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))


def load_secrets() -> dict:
    if not SECRETS_PATH.exists():
        return {}
    return json.loads(SECRETS_PATH.read_text(encoding="utf-8"))


def site_link_targets(config, db, repos: dict) -> dict[str, str]:
    """Every live-site URL the bot may post: repo homepages + links in the remaining seed queue."""
    user = config["github_user"]
    targets: dict[str, str] = {}
    for repo in repos.values():
        targets.update(trylinks.find_site_links(repo.get("homepage") or "", user))
    for (comment,) in db.execute("SELECT comment FROM queue WHERE used=0"):
        for name, url in trylinks.find_site_links(comment or "", user).items():
            targets.setdefault(name, url)
    return targets


def refresh_try_links(config, db, repos: dict) -> None:
    """Daily: publish/refresh go-link pages and pull yesterday's + today's 'Try it' click counts."""
    goat = trylinks.load_goatcounter(config, SECRETS_PATH)
    if not goat:
        return
    code, token = goat
    targets = site_link_targets(config, db, repos)
    try:
        available = trylinks.sync_go_pages(config["github_user"], targets, code, PAGES_CHECKOUT, log)
    except Exception as error:  # a slow or failed git pull must not fail the whole run (and block posting)
        log.warning("Go-page sync failed, keeping the existing pages: %s", error)
        available = set()
    if available:
        kv_set(db, "go_pages", json.dumps(sorted(available)))
    for day in ((datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), datetime.now().strftime("%Y-%m-%d")):
        try:
            for name, clicks in trylinks.fetch_go_clicks(code, token, day).items():
                db.execute("INSERT OR REPLACE INTO try_clicks(day, repo, clicks) VALUES(?,?,?)", (day, name, clicks))
        except Exception as error:
            log.warning("GoatCounter read failed for %s: %s", day, error)
    db.commit()


def track_links(config, db, text: str) -> str:
    """Swap live-site links for tracked go-links (publishing any missing go page first).

    A failed or slow go-page sync must not block the post: keep the existing cached
    go_pages, log a warning, and post: links that already have a go page are still tracked,
    new ones go out as the plain link.
    """
    goat = trylinks.load_goatcounter(config, SECRETS_PATH)
    if not goat or not text:
        return text
    user = config["github_user"]
    needed = trylinks.find_site_links(text, user)
    available = set(json.loads(kv_get(db, "go_pages", "[]")))
    missing = {name: url for name, url in needed.items() if name not in available}
    if missing:
        try:
            synced = trylinks.sync_go_pages(user, missing, goat[0], PAGES_CHECKOUT, log)
        except Exception as error:  # timeouts, diverged checkout, push rejected - never block the post
            log.warning("Go-page sync raised, posting with the plain link: %s", error)
            synced = set()
        if synced:
            available = synced
            kv_set(db, "go_pages", json.dumps(sorted(available)))
            time.sleep(90)  # give GitHub Pages time to deploy the new page
        else:
            log.warning("Go-page sync produced no pages; posting with the plain link(s) instead")
    return trylinks.rewrite_links(text, user, available)


def token_health(db, now: datetime) -> dict | None:
    token = load_token()
    today = now.strftime("%Y-%m-%d")
    if token is None:
        if kv_get(db, "warned_token") != today:
            notify("ReachPilot", "Not connected to LinkedIn yet. Run: python autopilot.py auth")
            kv_set(db, "warned_token", today)
        return None
    days_left = (token["expires_at"] - time.time()) / 86400
    if days_left <= 0:
        if kv_get(db, "warned_token") != today:
            notify("ReachPilot", "LinkedIn login expired. Run: python autopilot.py auth")
            kv_set(db, "warned_token", today)
        return None
    if days_left < 7 and kv_get(db, "warned_token") != today:
        notify("ReachPilot", f"LinkedIn login expires in {days_left:.0f} days. Run: python autopilot.py auth")
        kv_set(db, "warned_token", today)
    return token


def publish(config, db, slot, row, token) -> None:
    from linkedin_api import LinkedInClient

    source, repo, angle, body, comment, images_json = row
    comment = track_links(config, db, comment)
    body = track_links(config, db, body)
    client = LinkedInClient(token, config["linkedin_api_version"])
    video, image_paths = channels.split_media(json.loads(images_json))
    post_urn = None
    if video:
        try:
            video_urn = client.upload_video(video)
        except Exception as error:
            log.warning("Demo video upload failed (%s); posting the screenshots instead", error)
            video_urn = None
        if video_urn:
            try:
                post_urn = client.create_post(body, [], video=(video_urn, f"{repo or 'Project'} demo"))
            except RuntimeError as error:  # LinkedIn refused the video post, so nothing was posted yet
                log.warning("LinkedIn refused the video post (%s); posting the screenshots instead", error)
    if post_urn is None:
        uploaded = []
        for path in image_paths:
            try:
                uploaded.append((client.upload_image(path), f"Screenshot: {Path(path).stem.replace('_', ' ')}"))
            except Exception as error:
                log.warning("Skipping image %s: %s", path, error)
        post_urn = client.create_post(body, uploaded)
    url = client.post_url(post_urn)
    posted_at = local_now(config).strftime("%Y-%m-%d %H:%M")
    db.execute(
        "UPDATE posts SET status='posted', urn=?, url=?, posted_at=?, error=NULL WHERE slot=?",
        (post_urn, url, posted_at, slot),
    )
    if source.startswith("queue:"):
        db.execute("UPDATE queue SET used=1 WHERE id=?", (source.split(":", 1)[1],))
    db.commit()
    log.info("Posted %s (%s/%s): %s", slot, repo, angle, url)
    if comment:
        time.sleep(20)
        try:
            client.comment(post_urn, comment)
        except Exception as error:
            log.warning("First comment failed: %s", error)
            try:  # commenting needs a partner product; editing your own post only needs w_member_social
                client.inline_comment(post_urn, comment)
                log.info("Links put into the post itself instead of a comment")
            except Exception as inline_error:
                log.warning("Inline links failed too: %s", inline_error)
                notify("ReachPilot", "Posted, but add the links comment yourself: " + comment.replace("\n", " "))
    notify("ReachPilot", f"Posted: {(repo or angle)}")


def facts_for(config, db, repo: str | None, repos: dict) -> object | None:
    """README facts for one repo (Dev.to articles need them); None if unavailable."""
    if not repo:
        return None
    if not repos:
        repos = sync_github(config, db)
    try:
        return gh.repo_facts(config["github_user"], repos[repo]) if repo in repos else None
    except Exception as error:
        log.warning("Could not read facts for %s: %s", repo, error)
        return None


def queue_channels(config, db, slot: str, row, facts) -> None:
    """Once per slot: the Bluesky / Mastodon / Dev.to / Telegram / Discord versions."""
    source, repo, angle, body, comment, images_json = row[:6]
    media = json.loads(images_json or "[]")
    secrets = load_secrets()
    video, _ = channels.split_media(media)
    video_url = ""
    if video and repo and video.exists() and channels.needs_video_url(db, config, secrets, slot):
        try:  # X and Threads go through Buffer, which fetches media by public URL
            video_url = trylinks.host_demo_video(config["github_user"], repo, video, PAGES_CHECKOUT, log)
        except Exception as error:
            log.warning("Hosting the demo video failed (%s); X and Threads get the image", error)
    queued = channels.prepare(db, config, secrets, slot, repo, angle, body, comment or "", media, facts, video_url)
    if queued:
        log.info("Queued %s for %s", ", ".join(channels.LABELS[name] for name in queued), slot)


def publish_channels(config, db) -> None:
    """Publish queued channel posts and retry failed ones (3 attempts within 3 days), independent of LinkedIn."""
    results = channels.publish_pending(db, load_secrets(), lambda text: track_links(config, db, text), log)
    posted = [channels.LABELS[name] for name, outcome, _ in results if outcome == "posted"]
    if posted:
        notify("ReachPilot", "Also posted to " + ", ".join(posted))
    for name, outcome, detail in results:
        if outcome == "failed":
            notify("ReachPilot", f"{channels.LABELS[name]} post failed 3 times: {detail[:120]}")


def tick(config, db) -> None:
    if PAUSE_FLAG.exists():
        log.info("PAUSE file present; doing nothing")
        return
    now = local_now(config)
    kv_set(db, "last_tick", now.strftime("%Y-%m-%d %H:%M"))

    repos: dict = {}
    last_sync = kv_get(db, "last_github_sync_local", "")
    slot = due_slot(config, db, now)
    if slot or not last_sync or now.strftime("%Y-%m-%d %H") > last_sync:
        repos = sync_github(config, db)
        kv_set(db, "last_github_sync_local", (now + timedelta(hours=2)).strftime("%Y-%m-%d %H"))
    today = now.strftime("%Y-%m-%d")
    if kv_get(db, "last_signals_day") != today:
        if not repos:
            repos = sync_github(config, db)
        if repos:
            analytics.collect_github_signals(db, config["github_user"], repos, gh.gh_api, log)
            refresh_try_links(config, db, repos)
            try:
                growth.run_daily(db, config, load_secrets(), gh.gh_api, log)
            except Exception as error:  # growth must never block posting
                log.exception("Growth pass failed: %s", error)
            kv_set(db, "last_signals_day", today)
    analytics.ingest_inbox(INBOX, db, log)
    token = token_health(db, now)

    if not slot:
        publish_channels(config, db)
        return
    if posts_this_week(db, now) >= config["max_posts_per_week"]:
        log.info("Weekly cap reached; skipping slot %s", slot)
        db.execute("INSERT OR REPLACE INTO posts(slot, status, source) VALUES(?, 'skipped', 'cap')", (slot,))
        db.commit()
        return

    row = db.execute("SELECT source, repo, angle, body, comment, images, status FROM posts WHERE slot=?", (slot,)).fetchone()
    fresh_facts = None
    if row is None:
        if not repos:
            repos = sync_github(config, db)
        rng = random.Random(f"{slot}-{time.time()}")
        choice = Picker(config, db, repos, now, rng).pick(slot)
        if choice is None:
            log.info("Nothing fresh enough to post for %s; skipping rather than repeating", slot)
            db.execute("INSERT OR REPLACE INTO posts(slot, status, source) VALUES(?, 'skipped', 'nothing-new')", (slot,))
            db.commit()
            return
        images = build_images(config, choice, slot)
        db.execute(
            """INSERT INTO posts(slot, source, repo, angle, body, comment, images, status, used_bullets)
               VALUES(?,?,?,?,?,?,?,?,?)""",
            (slot, choice["source"], choice["repo"], choice["angle"], choice["body"], choice["comment"],
             json.dumps(images), "ready", json.dumps(choice["used_bullets"])),
        )
        db.commit()
        write_draft(slot, choice["body"], choice["comment"], images)
        row = db.execute("SELECT source, repo, angle, body, comment, images, status FROM posts WHERE slot=?", (slot,)).fetchone()
        fresh_facts = choice["facts"][0] if choice["facts"] and choice["repo"] else None

    if config["mode"] == "review":
        approved = DRAFTS / (slot.replace(":", "").replace(" ", "_") + ".approved")
        if not approved.exists():
            if row[6] != "awaiting_approval":
                db.execute("UPDATE posts SET status='awaiting_approval' WHERE slot=?", (slot,))
                db.commit()
                notify("ReachPilot", f"Draft ready for {slot}. Approve: python autopilot.py approve")
            return

    try:
        queue_channels(config, db, slot, row, fresh_facts or facts_for(config, db, row[1], repos))
    except Exception as error:  # other platforms must never block LinkedIn
        log.exception("Preparing other platforms failed for %s: %s", slot, error)

    if token is not None:
        try:
            publish(config, db, slot, row[:6], token)
        except Exception as error:
            db.execute("UPDATE posts SET status='failed', attempts=attempts+1, error=? WHERE slot=?", (str(error)[:500], slot))
            db.commit()
            log.error("Posting failed for %s: %s", slot, error)
            notify("ReachPilot", f"Posting failed, will retry: {str(error)[:120]}")
    publish_channels(config, db)


def write_draft(slot: str, body: str, comment: str, images: list[str], variants: dict | None = None) -> Path:
    DRAFTS.mkdir(exist_ok=True)
    path = DRAFTS / (slot.replace(":", "").replace(" ", "_") + ".md")
    video, pictures = channels.split_media(images)
    image_lines = "\n".join(f"- {image}" for image in pictures)
    text = f"# Slot {slot}\n\n{body}\n\n## First comment\n\n{comment}\n\n"
    if video:
        text += f"## Demo video\n\n- {video}\n\n"
    text += f"## Images\n\n{image_lines}\n"
    for channel, variant in (variants or {}).items():
        heading = channels.LABELS[channel] + (f": {variant['title']}" if variant["title"] else "")
        text += f"\n## {heading} ({len(variant['body'])} chars)\n\n{variant['body']}\n"
        if variant["extra"].get("submit_url"):
            text += f"\nSubmit: {variant['extra']['submit_url']}\n"
    path.write_text(text, encoding="utf-8")
    return path


# ------------------------------------------------------------------ commands

def cmd_init(config, db) -> None:
    if SEED_PATH.exists():
        for item in json.loads(SEED_PATH.read_text(encoding="utf-8")):
            db.execute(
                "INSERT OR IGNORE INTO queue(id, slot_date, repo, angle, body, comment, images) VALUES(?,?,?,?,?,?,?)",
                (item["id"], item["slot_date"], item["repo"], item["angle"], item["body"], item["comment"], json.dumps(item["images"])),
            )
        db.commit()
    repos = sync_github(config, db)
    print(f"Queue: {db.execute('SELECT COUNT(*) FROM queue').fetchone()[0]} seed posts. GitHub baseline: {len(repos)} repos.")


def cmd_auth() -> None:
    from linkedin_api import run_oauth

    if not SECRETS_PATH.exists():
        client_id = input("LinkedIn app Client ID: ").strip()
        client_secret = input("LinkedIn app Client Secret: ").strip()
        SECRETS_PATH.write_text(json.dumps({"client_id": client_id, "client_secret": client_secret}), encoding="utf-8")
    secrets_data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    token = run_oauth(secrets_data["client_id"], secrets_data["client_secret"])
    TOKEN_PATH.write_text(json.dumps(token), encoding="utf-8")
    days = (token["expires_at"] - time.time()) / 86400
    print(f"Connected as {token['name']}. Token valid for {days:.0f} days.")


def cmd_channels() -> None:
    channels.setup_interactive(SECRETS_PATH)


def cmd_preview(config, db, count: int) -> None:
    """Generate the next N non-queue posts as drafts (nothing is posted or consumed)."""
    repos = sync_github(config, db)
    now = local_now(config)
    preview_dir = DRAFTS / "preview"
    preview_dir.mkdir(parents=True, exist_ok=True)
    picker = Picker(config, db, repos, now, random.Random())
    savepoint_events = db.execute("SELECT id FROM events WHERE consumed=0").fetchall()
    for index in range(count):
        choice = picker.from_events() or picker.from_activity() or picker.recycle()
        if not choice:
            print("No more fresh content available.")
            break
        picker.history.append(choice["body"])
        if choice["repo"]:
            picker.simulated_recent.add(choice["repo"])
        slot = f"preview-{index + 1:02d}"
        images = build_images(config, choice, slot)
        facts = choice["facts"][0] if choice["facts"] and choice["repo"] else None
        variants = channels.build_variants(choice["body"], choice["comment"], choice["angle"], facts, channels.enabled(config))
        path = write_draft(slot, choice["body"], choice["comment"], images, variants)
        print(f"{slot}: {choice['repo']} / {choice['angle']} ({choice['source']}) -> {path}")
    for (event_id,) in savepoint_events:
        db.execute("UPDATE events SET consumed=0 WHERE id=?", (event_id,))
    db.commit()


def cmd_status(config, db) -> None:
    now = local_now(config)
    token = load_token()
    print(f"Now (WIB): {now:%a %Y-%m-%d %H:%M}   mode: {config['mode']}   paused: {PAUSE_FLAG.exists()}")
    if token:
        print(f"LinkedIn: {token.get('name')} - token expires in {(token['expires_at'] - time.time()) / 86400:.0f} days")
    else:
        print("LinkedIn: not connected (run: python autopilot.py auth)")
    print(f"Seed queue left: {db.execute('SELECT COUNT(*) FROM queue WHERE used=0').fetchone()[0]}")
    print(f"Pending GitHub events: {db.execute('SELECT COUNT(*) FROM events WHERE consumed=0').fetchone()[0]}")
    print("Recent posts:")
    for slot, repo, angle, status, url in db.execute(
        "SELECT slot, repo, angle, status, url FROM posts ORDER BY slot DESC LIMIT 8"
    ):
        print(f"  {slot}  {status:<18} {repo or '-':<28} {angle or '-':<16} {url or ''}")
    results = analytics.performance(db)
    ranked = analytics.ThompsonScorer(results, "repo").ranked()
    if ranked:
        print("Best performers (learned score):")
        for name, rate in ranked[:5]:
            print(f"  {name:<28} {rate:.3f}")
    signal_days = db.execute("SELECT COUNT(DISTINCT day) FROM github_signals").fetchone()[0]
    print(f"GitHub signal snapshots: {signal_days} day(s); posts scored so far: {len(results)}")
    if trylinks.load_goatcounter(config, SECRETS_PATH):
        since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        rows = db.execute(
            "SELECT repo, SUM(clicks) FROM try_clicks WHERE day>=? GROUP BY repo ORDER BY 2 DESC LIMIT 5", (since,)
        ).fetchall()
        print("'Try it' clicks, last 30 days: " + (", ".join(f"{name} {count}" for name, count in rows) or "none yet"))
    else:
        print("'Try it' click tracking: off (run: python autopilot.py goatcounter)")
    creds = channels.credentials(load_secrets())
    print("Other platforms: " + ", ".join(
        f"{channels.LABELS[name]} {'on' if creds.get(name) else 'off'}" for name in channels.AUTO if name in channels.enabled(config)
    ) + "   (connect: python autopilot.py channels)")
    for slot, channel, status, url, error in db.execute(
        "SELECT slot, channel, status, url, error FROM channel_posts ORDER BY created_at DESC LIMIT 8"
    ):
        print(f"  {slot}  {channels.LABELS[channel]:<12} {status:<8} {url or (error or '')[:80]}")


def cmd_approve(db) -> None:
    for (slot,) in db.execute("SELECT slot FROM posts WHERE status='awaiting_approval'"):
        flag = DRAFTS / (slot.replace(":", "").replace(" ", "_") + ".approved")
        flag.touch()
        print(f"Approved {slot}; it posts on the next run.")


def cmd_goatcounter(config) -> None:
    import getpass

    print("GoatCounter site code = the part before .goatcounter.com (e.g. 'juw12' for juw12.goatcounter.com)")
    code = input("Site code: ").strip().lower()
    token = getpass.getpass("API token (input hidden): ").strip()
    who = trylinks.check_goatcounter(code, token)
    secrets_data = json.loads(SECRETS_PATH.read_text(encoding="utf-8")) if SECRETS_PATH.exists() else {}
    secrets_data["goatcounter_token"] = token
    SECRETS_PATH.write_text(json.dumps(secrets_data), encoding="utf-8")
    config["goatcounter_code"] = code
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(f"GoatCounter connected ({who}). Publishing go-links...")
    db = connect_db()
    try:
        refresh_try_links(config, db, sync_github(config, db))
        pages = json.loads(kv_get(db, "go_pages", "[]"))
        example = trylinks.go_url(config["github_user"], pages[0]) if pages else "-"
        print(f"{len(pages)} tracked links live, e.g. {example}")
    finally:
        db.close()


def cmd_install() -> None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    script = ROOT / "autopilot.py"
    powershell = f"""
$action = New-ScheduledTaskAction -Execute '{pythonw}' -Argument '"{script}" tick' -WorkingDirectory '{ROOT}'
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 30)
$logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 20) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
Register-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' -Action $action -Trigger @($repeat, $logon) -Settings $settings `
    -Description 'Posts juw12 GitHub projects to LinkedIn on schedule' -Force | Out-Null
Get-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' | Select-Object TaskName, State
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", powershell], check=True)


def cmd_uninstall() -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"Unregister-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' -Confirm:$false"],
        check=False,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["init", "auth", "channels", "goatcounter", "tick", "preview", "status", "approve", "install", "uninstall", "dashboard"])
    parser.add_argument("count", nargs="?", type=int, default=3)
    args = parser.parse_args()
    setup_logging()
    config = load_config()

    if args.command == "auth":
        cmd_auth()
        return
    if args.command == "channels":
        cmd_channels()
        return
    if args.command == "goatcounter":
        cmd_goatcounter(config)
        return
    if args.command == "install":
        cmd_install()
        return
    if args.command == "uninstall":
        cmd_uninstall()
        return
    if args.command == "dashboard":
        import dashboard

        dashboard.run()
        return

    if args.command == "tick":
        if LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < 1800:
            return
        LOCK_PATH.write_text(str(os.getpid()))
    db = connect_db()
    try:
        if args.command == "init":
            cmd_init(config, db)
        elif args.command == "tick":
            tick(config, db)
        elif args.command == "preview":
            cmd_preview(config, db, args.count)
        elif args.command == "status":
            cmd_status(config, db)
        elif args.command == "approve":
            cmd_approve(db)
    except Exception:
        log.exception("Run failed")
        if args.command == "tick":
            notify("ReachPilot", "A run failed; see autopilot.log")
        else:
            raise
    finally:
        db.close()
        if args.command == "tick":
            LOCK_PATH.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
