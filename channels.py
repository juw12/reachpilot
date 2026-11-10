"""Other platforms for every ReachPilot post: Bluesky, Mastodon, Dev.to, Telegram, Discord, plus Hacker News / Reddit drafts.

Free, official APIs only (no scraping, no browser bots, no paid tiers):
  bluesky    AT Protocol with an app password                  short post (300 chars), screenshots, clickable link
  mastodon   your instance's REST API with an access token      short post (500 chars), screenshots
  devto      Forem API key                                      an article per launch / release, canonical URL back to you
  telegram   Bot API, posts into your own channel               full post with the first screenshot
  discord    a webhook into your own server                     full post with the first screenshot
  hackernews, reddit  no allowed posting API: a ready "Show HN" / Reddit draft with a prefilled submit link on the
                      dashboard's to-do list, and you press submit yourself.

Every variant is cut from the LinkedIn post that already passed the no-invented-facts guard, so a platform can
shorten a post but never add a claim; anything with a number not in the source is dropped.
Credentials live in secrets.json under "channels" (git-ignored). Set up with:  python autopilot.py channels
"""
from __future__ import annotations

import getpass
import io
import json
import re
import secrets as token_tools
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from PIL import Image

import composer

AUTO = ["bluesky", "mastodon", "devto", "telegram", "discord"]
MANUAL = ["hackernews", "reddit"]
ALL = AUTO + MANUAL
LABELS = {
    "bluesky": "Bluesky",
    "mastodon": "Mastodon",
    "devto": "Dev.to",
    "telegram": "Telegram",
    "discord": "Discord",
    "hackernews": "Hacker News",
    "reddit": "Reddit",
}
MAX_ATTEMPTS = 3
RETRY_WINDOW_DAYS = 3
DEVTO_COOLDOWN_DAYS = 60
TIMEOUT = 60

URL_PATTERN = re.compile(r"https?://[^\s)\]>]+")
HASHTAG_PATTERN = re.compile(r"(?<![\w#])#([A-Za-z][A-Za-z0-9_]*)")
COMMENT_POINTER = re.compile(r"\b(in|see) the (first )?comments?\b|\blinks? (is |are )?in the comments?\b", re.I)


# ------------------------------------------------------------------ storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_posts(
    slot TEXT, channel TEXT, repo TEXT, angle TEXT, title TEXT, body TEXT, extra TEXT, images TEXT,
    status TEXT, attempts INTEGER DEFAULT 0, url TEXT, error TEXT, created_at TEXT, posted_at TEXT,
    PRIMARY KEY(slot, channel));
"""


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)


def credentials(secrets: dict) -> dict:
    return secrets.get("channels", {}) if secrets else {}


def enabled(config: dict) -> list[str]:
    wanted = config.get("channels", ALL)
    return [channel for channel in ALL if channel in wanted]


def connected(config: dict, secrets: dict) -> list[str]:
    creds = credentials(secrets)
    return [channel for channel in enabled(config) if channel in MANUAL or creds.get(channel)]


# ------------------------------------------------------------------ cutting the LinkedIn post into variants

def _strip_trailing_punctuation(url: str) -> str:
    return url.rstrip(".,;:!?'\"")


def split_post(body: str, comment: str) -> tuple[list[str], list[str], list[tuple[str, str]]]:
    """LinkedIn post -> (paragraphs without the hashtag line and 'links in the comment' pointers, hashtags, links)."""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body or "") if part.strip()]
    tags: list[str] = []
    if paragraphs and all(token.startswith("#") for token in paragraphs[-1].split()):
        tags = paragraphs.pop().split()
    paragraphs = [part for part in paragraphs if not (COMMENT_POINTER.search(part) and len(part) < 90)]
    cleaned = []
    for part in paragraphs:
        # a closer and its pointer can share a paragraph ("Tell me ... \n\nLinks in the first comment." already split above)
        lines = [line for line in part.splitlines() if not (COMMENT_POINTER.search(line) and len(line) < 90)]
        if lines:
            cleaned.append("\n".join(lines).strip())
    links: list[tuple[str, str]] = []
    for line in (comment or "").splitlines():
        match = URL_PATTERN.search(line)
        if not match:
            continue
        label = line[:match.start()].strip().rstrip(":").strip()
        links.append((label, _strip_trailing_punctuation(match.group(0))))
    return cleaned, tags, links


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: max(limit - 1, 0)]
    sentence_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence_end > limit * 0.5:
        return cut[: sentence_end + 1]
    return cut[: cut.rfind(" ")].rstrip(",;:- ") + "…" if " " in cut else cut + "…"


def fit(paragraphs: list[str], tail: str, limit: int) -> str:
    """As many whole paragraphs as fit before the tail (links, tags); the first one is shortened if it alone is too long."""
    room = limit - len(tail)
    kept: list[str] = []
    for part in paragraphs:
        candidate = "\n\n".join(kept + [part])
        if len(candidate) <= room:
            kept.append(part)
        elif not kept:
            kept.append(_truncate(part, room))
            break
        else:
            break
    return ("\n\n".join(kept) + tail).strip()


def short_post(body: str, comment: str, limit: int, max_links: int, max_tags: int, labelled: bool = True) -> str:
    paragraphs, tags, links = split_post(body, comment)
    link_lines = [f"{label}: {url}" if labelled and label else url for label, url in links[:max_links]]
    tail = ""
    if link_lines:
        tail += "\n\n" + "\n".join(link_lines)
    if tags and max_tags:
        tail += "\n\n" + " ".join(tags[:max_tags])
    return fit(paragraphs, tail, limit)


def primary_link(comment: str, facts=None) -> str:
    _, _, links = split_post("", comment)
    if links:
        return links[0][1]
    if facts is not None:
        return facts.homepage or facts.html_url
    return ""


def devto_tags(facts) -> list[str]:
    tags: list[str] = []
    for topic in facts.topics:
        tag = re.sub(r"[^a-z0-9]", "", topic.lower())
        if tag and tag not in tags and len(tag) <= 30:
            tags.append(tag)
    return (tags[:2] + [tag for tag in ("opensource", "showdev") if tag not in tags[:2]])[:4]


def devto_article(body: str, comment: str, facts) -> dict | None:
    """An article for a launch or release: the post, every README feature, how it's built, links. None if too thin."""
    if facts is None or not facts.description or len(facts.bullets) < 2:
        return None
    paragraphs, _, links = split_post(body, comment)
    unused = [bullet for bullet in facts.bullets if bullet not in body]
    sections = ["\n\n".join(paragraphs)]
    if unused:
        sections.append("## What else it does\n\n" + "\n".join(f"- {composer._sentence(bullet)}" for bullet in unused[:10]))
    stack = composer.stack_facts(facts)
    if stack:
        sections.append("## Under the hood\n\n" + "\n".join(f"- {item}" for item in stack[:6]))
    link_lines = [f"- {label or 'Link'}: {url}" for label, url in links] or [f"- Source: {facts.html_url}"]
    sections.append("## Links\n\n" + "\n".join(link_lines))
    markdown = "\n\n".join(section for section in sections if section.strip())
    title = f"{facts.display_name}: {facts.description.rstrip('.')}"
    return {
        "title": _truncate(title, 120),
        "body": markdown,
        "tags": devto_tags(facts),
        "canonical_url": facts.homepage or facts.html_url,
        "main_image": next((image for image in facts.readme_images if image.startswith("https://")), ""),
    }


def manual_drafts(facts) -> dict[str, dict]:
    """Show HN + Reddit drafts with prefilled submit links (you pick the subreddit and press submit)."""
    if facts is None or not facts.description:
        return {}
    url = facts.homepage or facts.html_url
    summary = composer._sentence(facts.description)
    bullets = "\n".join(f"- {composer._sentence(bullet)}" for bullet in facts.bullets[:4])
    text = f"{summary}\n\n{bullets}\n\nSource: {facts.html_url}".strip()
    hn_title = _truncate(f"Show HN: {facts.display_name} – {facts.description.rstrip('.')}", 80).rstrip("…")
    reddit_title = _truncate(f"I built {facts.display_name}: {_lower(facts.description.rstrip('.'))}", 300)
    return {
        "hackernews": {
            "title": hn_title,
            "body": text,
            "url": "https://news.ycombinator.com/submitlink?" + urllib.parse.urlencode({"u": url, "t": hn_title}),
        },
        "reddit": {
            "title": reddit_title,
            "body": text,
            "url": "https://www.reddit.com/submit?" + urllib.parse.urlencode({"url": url, "title": reddit_title}),
        },
    }


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:] if text and not text[:2].isupper() else text


def build_variants(body: str, comment: str, angle: str | None, facts=None, channels: list[str] | None = None) -> dict[str, dict]:
    """channel -> {"title", "body", "extra"} for every channel this post suits. Pure function (used by preview and tests)."""
    channels = channels or ALL
    source = " ".join([body or "", comment or "", facts.source_text() if facts else ""])
    variants: dict[str, dict] = {}
    short = {
        "bluesky": dict(limit=300, max_links=1, max_tags=2, labelled=False),
        "mastodon": dict(limit=500, max_links=2, max_tags=3),
        "telegram": dict(limit=1024, max_links=3, max_tags=3),
        "discord": dict(limit=2000, max_links=3, max_tags=0),
    }
    link = primary_link(comment, facts)
    for channel, rules in short.items():
        if channel in channels:
            text = short_post(body, comment, **rules)
            if text and not composer.invented_numbers(text, source):
                variants[channel] = {"title": "", "body": text, "extra": {"link": link}}
    if "devto" in channels and angle in ("launch", "update"):
        article = devto_article(body, comment, facts)
        if article and not composer.invented_numbers(article["body"] + " " + article["title"], source):
            variants["devto"] = {"title": article.pop("title"), "body": article.pop("body"), "extra": article}
    if angle == "launch":
        for channel, draft in manual_drafts(facts).items():
            if channel in channels:
                variants[channel] = {"title": draft["title"], "body": draft["body"], "extra": {"submit_url": draft["url"]}}
    return variants


# ------------------------------------------------------------------ queueing (once per slot) and publishing (with retries)

def prepare(db, config: dict, secrets: dict, slot: str, repo: str | None, angle: str | None, body: str, comment: str,
            images: list[str], facts=None) -> list[str]:
    """Create the channel rows for this slot once. Returns the channels newly queued."""
    ensure_schema(db)
    existing = {row[0] for row in db.execute("SELECT channel FROM channel_posts WHERE slot=?", (slot,))}
    wanted = [channel for channel in connected(config, secrets) if channel not in existing]
    if not wanted:
        return []
    if "devto" in wanted and repo:
        recent = db.execute(
            "SELECT MAX(posted_at) FROM channel_posts WHERE channel='devto' AND status='posted' AND repo=?", (repo,)
        ).fetchone()[0]
        if recent and datetime.now() - datetime.strptime(recent[:16], "%Y-%m-%d %H:%M") < timedelta(days=DEVTO_COOLDOWN_DAYS):
            wanted.remove("devto")
    if repo and any(channel in wanted for channel in MANUAL):
        for channel in MANUAL:
            seen = db.execute("SELECT 1 FROM channel_posts WHERE channel=? AND repo=? LIMIT 1", (channel, repo)).fetchone()
            if seen and channel in wanted:
                wanted.remove(channel)  # one Show HN / Reddit draft per project, ever
    variants = build_variants(body, comment, angle, facts, wanted)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for channel, variant in variants.items():
        status = "manual" if channel in MANUAL else "ready"
        url = variant["extra"].get("submit_url") if channel in MANUAL else None
        db.execute(
            """INSERT OR IGNORE INTO channel_posts(slot, channel, repo, angle, title, body, extra, images, status, url, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (slot, channel, repo, angle, variant["title"], variant["body"], json.dumps(variant["extra"]),
             json.dumps(images if channel in ("bluesky", "mastodon", "telegram", "discord") else []), status, url, stamp),
        )
    db.commit()
    return list(variants)


def publish_pending(db, secrets: dict, rewrite, log) -> list[tuple[str, str, str]]:
    """Publish every queued or retryable channel post. Returns (channel, outcome, detail) per attempt."""
    ensure_schema(db)
    creds = credentials(secrets)
    since = (datetime.now() - timedelta(days=RETRY_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M")
    rows = db.execute(
        """SELECT slot, channel, title, body, extra, images, attempts FROM channel_posts
           WHERE (status='ready' OR (status='failed' AND attempts<?)) AND created_at>=? ORDER BY created_at""",
        (MAX_ATTEMPTS, since),
    ).fetchall()
    results = []
    for slot, channel, title, body, extra_json, images_json, attempts in rows:
        if not creds.get(channel):
            continue
        extra = json.loads(extra_json or "{}")
        try:
            client = CLIENTS[channel](creds[channel])
            text = rewrite(body)
            link = rewrite(extra.get("link", "")) if extra.get("link") else ""
            url = client.publish(
                text=text, title=title, images=[Path(path) for path in json.loads(images_json or "[]") if Path(path).exists()],
                link=link, extra=extra, idempotency_key=f"reachpilot-{slot}-{channel}",
            )
            db.execute(
                "UPDATE channel_posts SET status='posted', url=?, posted_at=?, error=NULL WHERE slot=? AND channel=?",
                (url, datetime.now().strftime("%Y-%m-%d %H:%M"), slot, channel),
            )
            log.info("Posted %s to %s: %s", slot, LABELS[channel], url)
            results.append((channel, "posted", url))
        except Exception as error:
            message = str(error)[:500]
            db.execute(
                "UPDATE channel_posts SET status='failed', attempts=attempts+1, error=? WHERE slot=? AND channel=?",
                (message, slot, channel),
            )
            log.warning("%s post for %s failed (attempt %d): %s", LABELS[channel], slot, attempts + 1, message)
            results.append((channel, "failed" if attempts + 1 >= MAX_ATTEMPTS else "retry", message))
        db.commit()
    return results


# ------------------------------------------------------------------ images

def jpeg(path: Path, max_bytes: int, max_side: int = 2000) -> tuple[bytes, int, int]:
    """Re-encode a screenshot as a JPEG under the platform's size limit."""
    with Image.open(path) as source:
        image = source.convert("RGB")
    if max(image.size) > max_side:
        image.thumbnail((max_side, max_side))
    for quality in (88, 80, 72, 64, 55, 45):
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", quality=quality, optimize=True)
        if buffer.tell() <= max_bytes:
            return buffer.getvalue(), image.width, image.height
        if quality == 64:
            image.thumbnail((int(image.width * 0.75), int(image.height * 0.75)))
    return buffer.getvalue(), image.width, image.height


def alt_text(path: Path) -> str:
    return "Screenshot: " + path.stem.replace("_", " ").strip()


def _check(response: requests.Response, what: str) -> requests.Response:
    if response.status_code >= 300:
        raise RuntimeError(f"{what} failed: HTTP {response.status_code} {response.text[:300]}")
    return response


# ------------------------------------------------------------------ platform clients

def bluesky_facets(text: str) -> list[dict]:
    """Links and hashtags must be marked with UTF-8 byte offsets or Bluesky shows them as plain text."""
    facets = []

    def byte_range(start: int, end: int) -> dict:
        return {"byteStart": len(text[:start].encode("utf-8")), "byteEnd": len(text[:end].encode("utf-8"))}

    for match in URL_PATTERN.finditer(text):
        url = _strip_trailing_punctuation(match.group(0))
        facets.append({"index": byte_range(match.start(), match.start() + len(url)),
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]})
    for match in HASHTAG_PATTERN.finditer(text):
        facets.append({"index": byte_range(match.start(), match.end()),
                       "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": match.group(1)}]})
    return facets


class Bluesky:
    def __init__(self, creds: dict):
        self.handle = creds["handle"].lstrip("@")
        self.password = creds["app_password"]
        self.service = creds.get("service", "https://bsky.social").rstrip("/")

    def session(self) -> dict:
        return _check(requests.post(
            f"{self.service}/xrpc/com.atproto.server.createSession",
            json={"identifier": self.handle, "password": self.password}, timeout=TIMEOUT,
        ), "Bluesky sign-in").json()

    def verify(self) -> str:
        return "@" + self.session()["handle"]

    def publish(self, text, title, images, link, extra, idempotency_key) -> str:
        session = self.session()
        headers = {"Authorization": f"Bearer {session['accessJwt']}"}
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "langs": ["en"],
            "facets": bluesky_facets(text),
        }
        embedded = []
        for path in images[:4]:
            data, width, height = jpeg(path, 950_000)
            blob = _check(requests.post(
                f"{self.service}/xrpc/com.atproto.repo.uploadBlob",
                headers={**headers, "Content-Type": "image/jpeg"}, data=data, timeout=TIMEOUT,
            ), "Bluesky image upload").json()["blob"]
            embedded.append({"alt": alt_text(path), "image": blob, "aspectRatio": {"width": width, "height": height}})
        if embedded:
            record["embed"] = {"$type": "app.bsky.embed.images", "images": embedded}
        elif link:
            record["embed"] = {"$type": "app.bsky.embed.external",
                               "external": {"uri": link, "title": title or link, "description": ""}}
        created = _check(requests.post(
            f"{self.service}/xrpc/com.atproto.repo.createRecord", headers=headers,
            json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record}, timeout=TIMEOUT,
        ), "Bluesky post").json()
        return f"https://bsky.app/profile/{session['handle']}/post/{created['uri'].rsplit('/', 1)[-1]}"


class Mastodon:
    def __init__(self, creds: dict):
        instance = creds["instance"].strip().rstrip("/")
        self.instance = instance if instance.startswith("http") else "https://" + instance
        self.headers = {"Authorization": f"Bearer {creds['access_token']}"}

    def verify(self) -> str:
        account = _check(requests.get(f"{self.instance}/api/v1/accounts/verify_credentials", headers=self.headers,
                                      timeout=TIMEOUT), "Mastodon sign-in").json()
        return f"@{account['acct']} on {urllib.parse.urlparse(self.instance).netloc}"

    def publish(self, text, title, images, link, extra, idempotency_key) -> str:
        media_ids = []
        for path in images[:4]:
            data, _, _ = jpeg(path, 8_000_000)
            media = _check(requests.post(
                f"{self.instance}/api/v2/media", headers=self.headers,
                files={"file": (path.stem + ".jpg", data, "image/jpeg")}, data={"description": alt_text(path)}, timeout=120,
            ), "Mastodon image upload").json()
            media_ids.append(media["id"])
        status = _check(requests.post(
            f"{self.instance}/api/v1/statuses", headers={**self.headers, "Idempotency-Key": idempotency_key},
            data={"status": text, "visibility": "public", "language": "en", "media_ids[]": media_ids}, timeout=TIMEOUT,
        ), "Mastodon post").json()
        return status.get("url") or status.get("uri", "")


class DevTo:
    API = "https://dev.to/api"

    def __init__(self, creds: dict):
        self.headers = {"api-key": creds["api_key"], "Accept": "application/vnd.forem.api-v1+json"}
        self.published = creds.get("publish", True)

    def verify(self) -> str:
        user = _check(requests.get(f"{self.API}/users/me", headers=self.headers, timeout=TIMEOUT), "Dev.to sign-in").json()
        return "@" + user["username"]

    def publish(self, text, title, images, link, extra, idempotency_key) -> str:
        article = {"title": title, "body_markdown": text, "published": bool(self.published),
                   "tags": extra.get("tags", []), "canonical_url": extra.get("canonical_url") or None}
        if extra.get("main_image"):
            article["main_image"] = extra["main_image"]
        created = _check(requests.post(f"{self.API}/articles", headers=self.headers, json={"article": article},
                                       timeout=TIMEOUT), "Dev.to article").json()
        return created.get("url", "")


class Telegram:
    def __init__(self, creds: dict):
        self.base = f"https://api.telegram.org/bot{creds['bot_token']}"
        self.chat = creds["chat_id"].strip()

    def _call(self, method: str, **kwargs) -> dict:
        response = requests.post(f"{self.base}/{method}", timeout=120, **kwargs)
        payload = response.json() if response.headers.get("Content-Type", "").startswith("application/json") else {}
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {payload.get('description') or response.status_code}")
        return payload["result"]

    def verify(self) -> str:
        bot = self._call("getMe")
        chat = self._call("getChat", data={"chat_id": self.chat})
        return f"@{bot['username']} -> {chat.get('title') or self.chat}"

    def publish(self, text, title, images, link, extra, idempotency_key) -> str:
        if images:
            data, _, _ = jpeg(images[0], 9_000_000)
            message = self._call("sendPhoto", data={"chat_id": self.chat, "caption": text[:1024]},
                                 files={"photo": (images[0].stem + ".jpg", data, "image/jpeg")})
        else:
            message = self._call("sendMessage", data={"chat_id": self.chat, "text": text[:4096]})
        if self.chat.startswith("@"):
            return f"https://t.me/{self.chat[1:]}/{message['message_id']}"
        return ""


class Discord:
    def __init__(self, creds: dict):
        self.webhook = creds["webhook_url"].strip()

    def verify(self) -> str:
        hook = _check(requests.get(self.webhook, timeout=TIMEOUT), "Discord webhook").json()
        return f"webhook '{hook.get('name', '?')}'"

    def publish(self, text, title, images, link, extra, idempotency_key) -> str:
        payload = {"content": text[:2000], "allowed_mentions": {"parse": []}}
        if images:
            data, _, _ = jpeg(images[0], 8_000_000)
            response = requests.post(self.webhook + "?wait=true", data={"payload_json": json.dumps(payload)},
                                     files={"files[0]": (images[0].stem + ".jpg", data, "image/jpeg")}, timeout=120)
        else:
            response = requests.post(self.webhook + "?wait=true", json=payload, timeout=TIMEOUT)
        message = _check(response, "Discord post").json()
        return f"https://discord.com/channels/{message.get('guild_id', '@me')}/{message['channel_id']}/{message['id']}" \
            if message.get("guild_id") else ""


CLIENTS = {"bluesky": Bluesky, "mastodon": Mastodon, "devto": DevTo, "telegram": Telegram, "discord": Discord}


# ------------------------------------------------------------------ Mastodon: sign in with the browser (no token to copy)

MASTODON_PORT = 8767
MASTODON_REDIRECT = f"http://127.0.0.1:{MASTODON_PORT}/callback"
MASTODON_SCOPES = "read:accounts write:statuses write:media"


def mastodon_sign_in(instance: str, open_browser=webbrowser.open, wait_seconds: int = 300, trace=None) -> dict:
    """Register a ReachPilot app on the instance, open its Authorize page, catch the code on localhost, swap it for a token."""
    base = instance.strip().rstrip("/")
    base = base if base.startswith("http") else "https://" + base
    app = _check(requests.post(f"{base}/api/v1/apps", data={
        "client_name": "ReachPilot",
        "redirect_uris": MASTODON_REDIRECT,
        "scopes": MASTODON_SCOPES,
        "website": "https://github.com/juw12/reachpilot",
    }, timeout=TIMEOUT), "Mastodon app registration").json()
    state = token_tools.token_urlsafe(16)
    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if trace:
                trace(f"GET {parsed.path} keys={sorted(query)}")  # never the values
            if "code" not in query and "error" not in query:
                self.send_response(204)  # favicon, pre-connect: ignore quietly
                self.end_headers()
                return
            received.update({key: values[0] for key, values in query.items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h2>ReachPilot is connected to Mastodon. You can close this tab.</h2>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", MASTODON_PORT), Handler)
    server.timeout = 5
    authorize_url = f"{base}/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": app["client_id"],
        "redirect_uri": MASTODON_REDIRECT,
        "response_type": "code",
        "scope": MASTODON_SCOPES,
        "state": state,
    })
    if trace:
        trace("AUTHORIZE " + authorize_url.split("?")[0])
    open_browser(authorize_url)
    deadline = datetime.now() + timedelta(seconds=wait_seconds)
    while "code" not in received and "error" not in received and datetime.now() < deadline:
        server.handle_request()
    server.server_close()
    if received.get("state") != state or "code" not in received:
        raise RuntimeError(f"Mastodon sign-in failed: {received.get('error_description') or received.get('error') or 'timed out'}")
    token = _check(requests.post(f"{base}/oauth/token", data={
        "grant_type": "authorization_code",
        "code": received["code"],
        "client_id": app["client_id"],
        "client_secret": app["client_secret"],
        "redirect_uri": MASTODON_REDIRECT,
        "scope": MASTODON_SCOPES,
    }, timeout=TIMEOUT), "Mastodon token").json()
    return {"instance": base, "access_token": token["access_token"]}


def save_channel(secrets_path: Path, channel: str, values: dict | None) -> None:
    """Re-read secrets.json and change only this platform, so other windows or LinkedIn/GoatCounter keys are never lost."""
    current = json.loads(secrets_path.read_text(encoding="utf-8")) if secrets_path.exists() else {}
    stored = current.setdefault("channels", {})
    if values:
        stored[channel] = values
    else:
        stored.pop(channel, None)
    secrets_path.write_text(json.dumps(current, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ one-time setup (python autopilot.py channels)

SETUP = {
    "bluesky": {
        "steps": "Bluesky app > Settings > Privacy and security > App passwords > Add. Never use your main password.",
        "fields": [("handle", "Your handle (e.g. juw12.bsky.social)", False), ("app_password", "App password", True)],
    },
    "mastodon": {
        "steps": "Leave the token empty to sign in with your browser (just press Authorize). Or paste a token from "
                 "Preferences > Development > your app > 'Your access token'.",
        "fields": [("instance", "Instance (e.g. mastodon.social)", False),
                   ("access_token", "Access token (Enter = sign in with browser)", True)],
    },
    "devto": {
        "steps": "dev.to > Settings > Extensions > 'DEV Community API Keys' > Generate API key.",
        "fields": [("api_key", "API key", True)],
    },
    "telegram": {
        "steps": "In Telegram, message @BotFather: /newbot, copy the token. Create a public channel, add the bot as an "
                 "admin with 'Post messages'. The chat id is the channel's @username.",
        "fields": [("bot_token", "Bot token", True), ("chat_id", "Channel (e.g. @juw12_builds)", False)],
    },
    "discord": {
        "steps": "Your server > Server settings > Integrations > Webhooks > New webhook > pick the channel > Copy webhook URL.",
        "fields": [("webhook_url", "Webhook URL", True)],
    },
}


def setup_interactive(secrets_path: Path) -> None:
    print("ReachPilot: connect other platforms (all free). Press Enter to skip one.\n")
    for channel in AUTO:
        label = LABELS[channel]
        creds = credentials(json.loads(secrets_path.read_text(encoding="utf-8")) if secrets_path.exists() else {})
        state = "connected" if creds.get(channel) else "not connected"
        answer = input(f"{label} ({state}). Set up {'again' if creds.get(channel) else 'now'}? [y/N/r=remove] ").strip().lower()
        if answer == "r":
            save_channel(secrets_path, channel, None)
            print(f"  {label} removed.\n")
            continue
        if answer != "y":
            print()
            continue
        print("  " + SETUP[channel]["steps"])
        values = {}
        for key, prompt, hidden in SETUP[channel]["fields"]:
            values[key] = (getpass.getpass(f"  {prompt}: ") if hidden else input(f"  {prompt}: ")).strip()
        if channel == "mastodon" and values.get("instance") and not values.get("access_token"):
            try:
                print("  Opening Mastodon in your browser: press Authorize there.")
                values = mastodon_sign_in(values["instance"])
            except Exception as error:
                print(f"  {error}\n")
                continue
        if not all(values.values()):
            print("  Skipped (empty value).\n")
            continue
        try:
            who = CLIENTS[channel](values).verify()
        except Exception as error:
            print(f"  Could not connect: {error}\n  Nothing saved for {label}.\n")
            continue
        save_channel(secrets_path, channel, values)
        print(f"  {label} connected as {who}.\n")
    final = credentials(json.loads(secrets_path.read_text(encoding="utf-8")) if secrets_path.exists() else {})
    ready = [LABELS[channel] for channel in AUTO if final.get(channel)]
    print("Posting to: LinkedIn" + "".join(f", {name}" for name in ready))
    print("Hacker News and Reddit drafts appear on the dashboard to-do list for each new project.")
