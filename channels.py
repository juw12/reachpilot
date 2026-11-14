"""Other platforms for every ReachPilot post: Bluesky, Mastodon, Dev.to, Telegram, Discord, X, Threads, Substack.

Official APIs only (no scraping, no browser bots), all free:
  bluesky    AT Protocol; setup turns your password into an    short post (300 chars), demo video or screenshots, link
             app password once and stores only that
  mastodon   your instance's REST API with an access token      short post (500 chars), demo video or screenshots
  devto      Forem API key                                      an article per launch / release, canonical URL back to you
  telegram   Bot API, posts into your own channel               full post with the demo video or the first screenshot
  discord    a webhook into your own server                     full post with the demo video or the first screenshot
  x          free through Buffer's API: Buffer (an official X     short post (280 weighted chars), demo video or image, link
             partner) publishes to your X account, no X fees.
             Optional fallback: the paid X API v2 (OAuth 2.0 PKCE), capped by x_monthly_budget_usd
  threads    free through the same Buffer key                    short post (500 chars), one tag, demo video or image, link
  substack   free through the same Buffer key (Substack Notes)   the post with links, project image

The demo video is the short screen recording of the live site made for a single-project post: a styled vertical
video from reels.py in two cuts ("feed" 4:5 for the platforms posted to here, "tall" 9:16 for X and Threads), or
the plain recording from visuals.record_demo. A platform that refuses the video gets the screenshots instead, so a
video problem never costs a post.

Every variant is cut from the LinkedIn post that already passed the no-invented-facts guard, so a platform can
shorten a post but never add a claim; anything with a number not in the source is dropped.
Credentials live in secrets.json under "channels" (git-ignored). Set up with:  python autopilot.py channels
"""
from __future__ import annotations

import base64
import getpass
import hashlib
import io
import json
import logging
import re
import secrets as token_tools
import time
import urllib.parse
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests
from PIL import Image

import composer

AUTO = ["bluesky", "mastodon", "devto", "telegram", "discord", "x", "threads", "substack"]
ALL = AUTO
LABELS = {
    "bluesky": "Bluesky",
    "mastodon": "Mastodon",
    "devto": "Dev.to",
    "telegram": "Telegram",
    "discord": "Discord",
    "x": "X",
    "threads": "Threads",
    "substack": "Substack",
}
MAX_ATTEMPTS = 3
RETRY_WINDOW_DAYS = 3
DEVTO_COOLDOWN_DAYS = 60
TIMEOUT = 60
SECRETS_PATH = Path(__file__).with_name("secrets.json")  # X rotates its refresh token on every use; saved here

# X API pay-per-use prices (docs.x.com pricing, Nov 2026): a post costs $0.015, or $0.20 when it contains a URL.
X_COST_POST = 0.015
X_COST_LINK = 0.20
X_DEFAULT_MONTHLY_BUDGET = 5.0  # USD; override with "x_monthly_budget_usd" in config.json (0 = never post to X)
X_URL_WEIGHT = 23  # X counts every link as 23 characters (t.co)
BUFFER_API = "https://api.buffer.com"
BUFFER_API_SETTINGS = "https://publish.buffer.com/settings/api"
BUFFER_CHANNELS = "https://publish.buffer.com/channels"
BUFFER_X_SERVICES = ("twitter", "x")
BUFFER_SERVICES = {"x": BUFFER_X_SERVICES, "threads": ("threads",), "substack": ("substack",)}
BUFFER_ONLY = ("threads", "substack")  # channels that only exist through Buffer
BUFFER_VIDEO = ("x", "threads")  # Buffer channels that get the demo video (by public URL); Substack Notes keeps the image
PUBLIC_IMAGE = re.compile(r"^https://\S+\.(png|jpe?g|webp)(\?\S*)?$", re.I)
VIDEO_SUFFIXES = (".mp4",)
DEMO_VIDEO_SIZE = (1280, 720)  # what visuals.record_demo makes; selftest keeps the two in step
VIDEO_CUTS = {"feed": (1080, 1350), "tall": (1080, 1920)}  # what reels.record makes; selftest keeps the two in step
STYLED_VIDEO = re.compile(r"^(?P<base>.*_demo)_(?P<format>[a-z]+)_(?P<cut>feed|tall)$")  # <repo>_demo_<format>_<cut>
VIDEO_POLL_SECONDS = 3
VIDEO_POLL_TRIES = 40  # two minutes for a platform to process a short clip
DISCORD_MAX_VIDEO_BYTES = 9_500_000  # webhook uploads stop at 10 MB
BLUESKY_VIDEO = "https://video.bsky.app"

log = logging.getLogger("autopilot")

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
    return [channel for channel in enabled(config) if creds.get(channel)]


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


X_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))


def x_length(text: str) -> int:
    """Length as X counts it: every link is 23, Latin text 1 per character, CJK and emoji 2."""
    total = 0
    position = 0
    for match in URL_PATTERN.finditer(text):
        total += _x_plain_length(text[position:match.start()]) + X_URL_WEIGHT
        position = match.end()
    return total + _x_plain_length(text[position:])


def _x_plain_length(text: str) -> int:
    return sum(1 if any(low <= ord(char) <= high for low, high in X_LIGHT_RANGES) else 2 for char in text)


OPENER_CHARS = 60  # shorter than this, what was kept is only a hook line: the post still has to say something
OPENER_SPARE = 40  # ... so the next paragraph is shortened into the room left, when there is at least this much


def fit(paragraphs: list[str], tail: str, limit: int, measure=len) -> str:
    """As many whole paragraphs as fit before the tail (links, tags); the first one is shortened if it alone is too long.

    When all that fits is a short opener, the paragraph after it is shortened to fit too.
    """
    room = limit - measure(tail)
    kept: list[str] = []
    for part in paragraphs:
        candidate = "\n\n".join(kept + [part])
        if measure(candidate) <= room:
            kept.append(part)
            continue
        used = measure("\n\n".join(kept)) + 2 if kept else 0
        if not kept or (used < OPENER_CHARS and room - used >= OPENER_SPARE):
            kept.append(_truncate(part, room - used))
        break
    return ("\n\n".join(kept) + tail).strip()


def short_post(body: str, comment: str, limit: int, max_links: int, max_tags: int, labelled: bool = True,
               measure=len) -> str:
    paragraphs, tags, links = split_post(body, comment)
    link_lines = [f"{label}: {url}" if labelled and label else url for label, url in links[:max_links]]
    tail = ""
    if link_lines:
        tail += "\n\n" + "\n".join(link_lines)
    if tags and max_tags:
        tail += "\n\n" + " ".join(tags[:max_tags])
    return fit(paragraphs, tail, limit, measure)


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


def public_image(facts) -> str:
    """A hosted image for posts that can't upload local screenshots: the README's first photo, else GitHub's card."""
    if facts is None:
        return ""
    for image in facts.readme_images:
        if PUBLIC_IMAGE.match(image):
            return image
    if facts.html_url.startswith("https://github.com/"):
        return facts.html_url.replace("https://github.com/", "https://opengraph.githubassets.com/reachpilot/", 1)
    return ""


def build_variants(body: str, comment: str, angle: str | None, facts=None, channels: list[str] | None = None,
                   video_url: str = "") -> dict[str, dict]:
    """channel -> {"title", "body", "extra"} for every channel this post suits. Pure function (used by preview and tests)."""
    channels = channels or ALL
    source = " ".join([body or "", comment or "", facts.source_text() if facts else ""])
    variants: dict[str, dict] = {}
    short = {
        "bluesky": dict(limit=300, max_links=1, max_tags=2, labelled=False),
        "mastodon": dict(limit=500, max_links=2, max_tags=3),
        "telegram": dict(limit=1024, max_links=3, max_tags=3),
        "discord": dict(limit=2000, max_links=3, max_tags=0),
        "x": dict(limit=280, max_links=1, max_tags=2, labelled=False, measure=x_length),
        "threads": dict(limit=500, max_links=1, max_tags=1, labelled=False),  # Threads links one topic per post
        "substack": dict(limit=1500, max_links=2, max_tags=0),
    }
    link = primary_link(comment, facts)
    for channel, rules in short.items():
        if channel in channels:
            text = short_post(body, comment, **rules)
            if channel == "x" and x_length(text) > rules["limit"]:
                continue
            if text and not composer.invented_numbers(text, source):
                extra = {"link": link}
                if channel in BUFFER_SERVICES:
                    extra["image_url"] = public_image(facts)  # Buffer takes hosted images only
                if channel in BUFFER_VIDEO and video_url:
                    extra["video_url"] = video_url  # ... and hosted videos; the image stays as the fallback
                variants[channel] = {"title": "", "body": text, "extra": extra}
    if "devto" in channels and angle in ("launch", "update"):
        article = devto_article(body, comment, facts)
        if article and not composer.invented_numbers(article["body"] + " " + article["title"], source):
            variants["devto"] = {"title": article.pop("title"), "body": article.pop("body"), "extra": article}
    return variants


# ------------------------------------------------------------------ queueing (once per slot) and publishing (with retries)

def prepare(db, config: dict, secrets: dict, slot: str, repo: str | None, angle: str | None, body: str, comment: str,
            images: list[str], facts=None, video_url: str = "") -> list[str]:
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
    if ("x" in wanted and x_is_paid(credentials(secrets).get("x", {}))
            and x_spent_this_month(db) + X_COST_LINK > x_monthly_budget(config)):
        wanted.remove("x")
    if not wanted:  # build_variants treats an empty list as 'every platform'
        return []
    variants = build_variants(body, comment, angle, facts, wanted, video_url)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for channel, variant in variants.items():
        db.execute(
            """INSERT OR IGNORE INTO channel_posts(slot, channel, repo, angle, title, body, extra, images, status, url, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (slot, channel, repo, angle, variant["title"], variant["body"], json.dumps(variant["extra"]),
             json.dumps(images if channel in ("bluesky", "mastodon", "telegram", "discord", "x") else []), "ready", None, stamp),
        )
    db.commit()
    return list(variants)


def needs_video_url(db, config: dict, secrets: dict, slot: str) -> bool:
    """True when X or Threads (through Buffer) is still to be queued for this slot, so the video needs a public URL."""
    ensure_schema(db)
    existing = {row[0] for row in db.execute("SELECT channel FROM channel_posts WHERE slot=?", (slot,))}
    creds = credentials(secrets)
    return any(
        channel not in existing and not (channel == "x" and x_is_paid(creds.get("x", {})))
        for channel in connected(config, secrets) if channel in BUFFER_VIDEO
    )


def x_is_paid(creds: dict) -> bool:
    return creds.get("via") != "buffer"


def x_monthly_budget(config: dict) -> float:
    return float(config.get("x_monthly_budget_usd", X_DEFAULT_MONTHLY_BUDGET))


def x_cost(text: str) -> float:
    return X_COST_LINK if URL_PATTERN.search(text or "") else X_COST_POST


def x_spent_this_month(db) -> float:
    """Estimated X API spend this calendar month: posts made plus posts still queued (they will be charged)."""
    ensure_schema(db)
    month = datetime.now().strftime("%Y-%m")
    rows = db.execute(
        "SELECT body FROM channel_posts WHERE channel='x' AND status IN ('posted','ready') AND created_at LIKE ?",
        (month + "%",),
    ).fetchall()
    return round(sum(x_cost(body) for (body,) in rows), 3)


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
            video, images = split_media(path for path in json.loads(images_json or "[]") if Path(path).exists())
            url = client.publish(
                text=text, title=title, images=images, link=link, extra=extra,
                idempotency_key=f"reachpilot-{slot}-{channel}", video=video,
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


# ------------------------------------------------------------------ images and the demo video

def split_media(paths, cut: str = "feed") -> tuple[Path | None, list[Path]]:
    """A post's stored media list -> (the demo video or None, the screenshots).

    A styled video comes in several cuts; the one asked for is picked, and the first video otherwise.
    """
    videos = []
    images = []
    for item in paths:
        path = Path(item)
        if path.suffix.lower() in VIDEO_SUFFIXES:
            videos.append(path)
        else:
            images.append(path)
    video = next((path for path in videos if path.stem.endswith(f"_{cut}")), videos[0] if videos else None)
    return video, images


def video_size(path: Path) -> tuple[int, int]:
    """(width, height) of a demo video, told from its name."""
    styled = STYLED_VIDEO.match(Path(path).stem)
    return VIDEO_CUTS[styled["cut"]] if styled else DEMO_VIDEO_SIZE


def video_alt(path: Path) -> str:
    styled = STYLED_VIDEO.match(path.stem)
    return "Screen recording: " + (styled["base"] if styled else path.stem).replace("_", " ").strip()


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

    def upload_video(self, session: dict, path: Path) -> dict:
        """Bluesky's video service transcodes the clip; the blob it hands back is what the post embeds."""
        headers = {"Authorization": f"Bearer {session['accessJwt']}"}
        services = (session.get("didDoc") or {}).get("service") or []
        pds = next((item["serviceEndpoint"] for item in services if item.get("serviceEndpoint")), self.service)
        token = _check(requests.get(
            f"{self.service}/xrpc/com.atproto.server.getServiceAuth", headers=headers,
            params={"aud": "did:web:" + urllib.parse.urlparse(pds).netloc, "lxm": "com.atproto.repo.uploadBlob",
                    "exp": int(time.time()) + 1800}, timeout=TIMEOUT,
        ), "Bluesky video permission").json()["token"]
        response = requests.post(
            f"{BLUESKY_VIDEO}/xrpc/app.bsky.video.uploadVideo", params={"did": session["did"], "name": path.name},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"}, data=path.read_bytes(), timeout=300,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        status = payload.get("jobStatus") or payload  # an already-uploaded clip answers 409 with its job or blob
        job_id = status.get("jobId")
        if not (status.get("blob") or job_id):
            raise RuntimeError(f"Bluesky video upload failed: HTTP {response.status_code} {response.text[:300]}")
        for _ in range(VIDEO_POLL_TRIES):
            if status.get("blob"):
                return status["blob"]
            if status.get("state") == "JOB_STATE_FAILED":
                raise RuntimeError(f"Bluesky could not process the video: {status.get('error') or status.get('message')}")
            time.sleep(VIDEO_POLL_SECONDS)
            status = _check(requests.get(
                f"{BLUESKY_VIDEO}/xrpc/app.bsky.video.getJobStatus", params={"jobId": job_id}, timeout=TIMEOUT,
            ), "Bluesky video status").json()["jobStatus"]
        raise RuntimeError("Bluesky was still processing the video when the wait ran out")

    def publish(self, text, title, images, link, extra, idempotency_key, video=None) -> str:
        session = self.session()
        headers = {"Authorization": f"Bearer {session['accessJwt']}"}
        record = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "langs": ["en"],
            "facets": bluesky_facets(text),
        }
        if video:
            try:
                record["embed"] = {
                    "$type": "app.bsky.embed.video", "video": self.upload_video(session, video),
                    "aspectRatio": dict(zip(("width", "height"), video_size(video))), "alt": video_alt(video),
                }
                images = []
            except Exception as error:
                log.warning("Bluesky did not take the demo video, posting the screenshots instead: %s", error)
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
        elif link and "embed" not in record:
            record["embed"] = {"$type": "app.bsky.embed.external",
                               "external": {"uri": link, "title": title or link, "description": ""}}
        created = _check(requests.post(
            f"{self.service}/xrpc/com.atproto.repo.createRecord", headers=headers,
            json={"repo": session["did"], "collection": "app.bsky.feed.post", "record": record}, timeout=TIMEOUT,
        ), "Bluesky post").json()
        return f"https://bsky.app/profile/{session['handle']}/post/{created['uri'].rsplit('/', 1)[-1]}"



APP_PASSWORD_PATTERN = re.compile(r"^[a-z2-7]{4}(-[a-z2-7]{4}){3}$")


def bluesky_sign_in(handle: str, password: str, service: str = "https://bsky.social", ask_code=input) -> dict:
    """Normal password -> a new app password (the only thing stored). An app password is accepted as is."""
    handle = handle.strip().lstrip("@")
    if APP_PASSWORD_PATTERN.match(password):
        return {"handle": handle, "app_password": password}
    login = {"identifier": handle, "password": password}
    response = requests.post(f"{service}/xrpc/com.atproto.server.createSession", json=login, timeout=TIMEOUT)
    if response.status_code == 401 and "AuthFactorTokenRequired" in response.text:
        login["authFactorToken"] = ask_code("  Bluesky e-mailed you a sign-in code. Code: ").strip()
        response = requests.post(f"{service}/xrpc/com.atproto.server.createSession", json=login, timeout=TIMEOUT)
    session = _check(response, "Bluesky sign-in").json()
    created = _check(requests.post(
        f"{service}/xrpc/com.atproto.server.createAppPassword",
        headers={"Authorization": f"Bearer {session['accessJwt']}"},
        json={"name": f"ReachPilot {datetime.now():%Y-%m-%d %H%M}"}, timeout=TIMEOUT,
    ), "Bluesky app password").json()
    return {"handle": session["handle"], "app_password": created["password"]}


class Mastodon:
    def __init__(self, creds: dict):
        instance = creds["instance"].strip().rstrip("/")
        self.instance = instance if instance.startswith("http") else "https://" + instance
        self.headers = {"Authorization": f"Bearer {creds['access_token']}"}

    def verify(self) -> str:
        account = _check(requests.get(f"{self.instance}/api/v1/accounts/verify_credentials", headers=self.headers,
                                      timeout=TIMEOUT), "Mastodon sign-in").json()
        return f"@{account['acct']} on {urllib.parse.urlparse(self.instance).netloc}"

    def upload_video(self, path: Path) -> str:
        """Upload the clip and wait until the instance has processed it; a status cannot attach it before that."""
        with open(path, "rb") as handle:
            response = _check(requests.post(
                f"{self.instance}/api/v2/media", headers=self.headers,
                files={"file": (path.name, handle, "video/mp4")}, data={"description": video_alt(path)}, timeout=300,
            ), "Mastodon video upload")
        media_id = response.json()["id"]
        for _ in range(VIDEO_POLL_TRIES):
            if response.status_code == 200:
                return media_id
            time.sleep(VIDEO_POLL_SECONDS)  # 202 on upload and 206 on the check both mean "still processing"
            response = _check(requests.get(f"{self.instance}/api/v1/media/{media_id}", headers=self.headers,
                                           timeout=TIMEOUT), "Mastodon video processing")
        raise RuntimeError("Mastodon was still processing the video when the wait ran out")

    def publish(self, text, title, images, link, extra, idempotency_key, video=None) -> str:
        media_ids = []
        if video:
            try:
                media_ids.append(self.upload_video(video))
                images = []
            except Exception as error:
                log.warning("Mastodon did not take the demo video, posting the screenshots instead: %s", error)
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

    def publish(self, text, title, images, link, extra, idempotency_key, video=None) -> str:
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

    def publish(self, text, title, images, link, extra, idempotency_key, video=None) -> str:
        message = None
        if video:
            try:
                with open(video, "rb") as handle:
                    message = self._call(
                        "sendVideo",
                        data={"chat_id": self.chat, "caption": text[:1024], "supports_streaming": "true",
                              **dict(zip(("width", "height"), video_size(video)))},
                        files={"video": (video.name, handle, "video/mp4")},
                    )
            except RuntimeError as error:
                log.warning("Telegram did not take the demo video, sending the screenshot instead: %s", error)
        if message is None and images:
            data, _, _ = jpeg(images[0], 9_000_000)
            message = self._call("sendPhoto", data={"chat_id": self.chat, "caption": text[:1024]},
                                 files={"photo": (images[0].stem + ".jpg", data, "image/jpeg")})
        elif message is None:
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

    def publish(self, text, title, images, link, extra, idempotency_key, video=None) -> str:
        payload = {"content": text[:2000], "allowed_mentions": {"parse": []}}
        attachments = []  # tried in order until the webhook accepts one: the demo video, then the first screenshot
        if video and video.stat().st_size <= DISCORD_MAX_VIDEO_BYTES:
            attachments.append((video.name, video.read_bytes(), "video/mp4"))
        if images:
            data, _, _ = jpeg(images[0], 8_000_000)
            attachments.append((images[0].stem + ".jpg", data, "image/jpeg"))
        response = None
        for attachment in attachments:
            response = requests.post(self.webhook + "?wait=true", data={"payload_json": json.dumps(payload)},
                                     files={"files[0]": attachment}, timeout=120)
            if response.status_code < 300:
                break
            log.warning("Discord did not take %s: HTTP %s", attachment[0], response.status_code)
        if response is None:
            response = requests.post(self.webhook + "?wait=true", json=payload, timeout=TIMEOUT)
        message = _check(response, "Discord post").json()
        return f"https://discord.com/channels/{message.get('guild_id', '@me')}/{message['channel_id']}/{message['id']}" \
            if message.get("guild_id") else ""


class X:
    """X API v2 with an OAuth 2.0 user token. Access tokens last 2 hours; the refresh token rotates on every use."""
    API = "https://api.x.com"

    def __init__(self, creds: dict):
        self.creds = creds  # same dict as in secrets, so a refreshed token is used by the next post in this run

    def token(self) -> str:
        if float(self.creds.get("expires_at", 0)) - 120 > time.time():
            return self.creds["access_token"]
        refreshed = _x_token_request(self.creds, {
            "grant_type": "refresh_token",
            "refresh_token": self.creds["refresh_token"],
        }, "X token refresh")
        self.creds.update(refreshed)
        save_channel(SECRETS_PATH, "x", self.creds)  # the old refresh token is now dead: save the new one at once
        return self.creds["access_token"]

    def verify(self) -> str:
        me = _check(requests.get(f"{self.API}/2/users/me", headers={"Authorization": f"Bearer {self.token()}"},
                                 timeout=TIMEOUT), "X sign-in").json()
        return "@" + me["data"]["username"]

    def publish(self, text, title, images, link, extra, idempotency_key, video=None) -> str:
        headers = {"Authorization": f"Bearer {self.token()}"}
        media_ids = []
        for path in images[:4]:
            data, _, _ = jpeg(path, 4_900_000)
            media = _check(requests.post(
                f"{self.API}/2/media/upload", headers=headers,
                files={"media": (path.stem + ".jpg", data, "image/jpeg")},
                data={"media_category": "tweet_image", "media_type": "image/jpeg"}, timeout=120,
            ), "X image upload").json()
            media_ids.append(media["data"]["id"])
        payload = {"text": text}
        if media_ids:
            payload["media"] = {"media_ids": media_ids}
        created = _check(requests.post(f"{self.API}/2/tweets", headers=headers, json=payload, timeout=TIMEOUT),
                         "X post").json()
        return f"https://x.com/{self.creds.get('username') or 'i/web'}/status/{created['data']['id']}"


def buffer_graphql(api_key: str, query: str, variables: dict | None = None, what: str = "Buffer") -> dict:
    response = _check(requests.post(
        BUFFER_API,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
        timeout=TIMEOUT,
    ), what)
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"{what} failed: {payload['errors'][0].get('message', payload['errors'])}")
    return payload.get("data") or {}


def buffer_x_channels(api_key: str) -> list[dict]:
    """Every X account connected to this Buffer login: [{id, name, disconnected}]."""
    return buffer_channels(api_key, BUFFER_X_SERVICES)


def buffer_channels(api_key: str, services) -> list[dict]:
    """Every Buffer channel of these services (e.g. ("threads",)): [{id, name, service, disconnected}]."""
    organizations = buffer_graphql(api_key, "query { account { organizations { id } } }", what="Buffer sign-in")
    found = []
    for organization in organizations.get("account", {}).get("organizations", []):
        channels = buffer_graphql(
            api_key,
            "query Channels($input: ChannelsInput!) { channels(input: $input) { id name displayName service isDisconnected } }",
            {"input": {"organizationId": organization["id"]}},
            "Buffer channels",
        )
        for channel in channels.get("channels", []):
            if str(channel.get("service", "")).lower() in services:
                found.append({"id": channel["id"], "name": channel.get("name") or channel.get("displayName") or "",
                              "service": str(channel.get("service", "")).lower(),
                              "disconnected": bool(channel.get("isDisconnected"))})
    return found


def video_url_live(url: str) -> bool:
    """Buffer downloads the video itself, so only hand it a URL that already answers."""
    try:
        return requests.head(url, timeout=30, allow_redirects=True).status_code == 200
    except requests.RequestException:
        return False


BUFFER_CREATE_POST = """mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id externalLink } }
    ... on MutationError { message }
  }
}"""


class BufferChannel:
    """Free posting through Buffer (free plan + public API): Buffer publishes the post to the account right away."""

    def __init__(self, creds: dict):
        self.api_key = creds["api_key"]
        self.channel_id = creds["channel_id"]
        self.username = creds.get("username", "").lstrip("@")

    def verify(self) -> str:
        every_service = tuple(service for services in BUFFER_SERVICES.values() for service in services)
        channel = next((item for item in buffer_channels(self.api_key, every_service)
                        if item["id"] == self.channel_id), None)
        if channel is None:
            raise RuntimeError("Buffer: that account is no longer connected to this Buffer login")
        if channel["disconnected"]:
            raise RuntimeError(f"Buffer: reconnect {channel['service']} at {BUFFER_CHANNELS}")
        return f"@{channel['name']} (via Buffer, free)"

    def _create(self, text: str, assets: list[dict]) -> str:
        post_input = {
            "text": text,
            "channelId": self.channel_id,
            "schedulingType": "automatic",
            "mode": "shareNow",
            "assets": assets,
        }
        result = buffer_graphql(self.api_key, BUFFER_CREATE_POST, {"input": post_input}, "Post via Buffer")
        created = result.get("createPost") or {}
        if "post" not in created:
            raise RuntimeError(f"Post via Buffer failed: {created.get('message') or created}")
        return created["post"].get("externalLink") or ""

    def publish(self, text, title, images, link, extra, idempotency_key, video=None) -> str:
        """Buffer fetches media by public URL: the hosted demo video when it is live, otherwise the project image."""
        video_url = extra.get("video_url", "")
        if video_url and video_url_live(video_url):
            try:
                return self._create(text, [{"video": {"url": video_url}}])
            except RuntimeError as error:
                log.warning("Buffer did not take the demo video, posting the image instead: %s", error)
        return self._create(text, [{"image": {"url": extra["image_url"]}}] if extra.get("image_url") else [])


class XViaBuffer(BufferChannel):
    """X for free: Buffer is an official X partner, so no X API fees."""

    def publish(self, text, title, images, link, extra, idempotency_key, video=None) -> str:
        url = super().publish(text, title, images, link, extra, idempotency_key, video)
        return url or (f"https://x.com/{self.username}" if self.username else "")


class ThreadsViaBuffer(BufferChannel):
    """Threads for free through Buffer; links to the profile when Buffer has no post link yet."""

    def publish(self, text, title, images, link, extra, idempotency_key, video=None) -> str:
        url = super().publish(text, title, images, link, extra, idempotency_key, video)
        return url or (f"https://www.threads.com/@{self.username}" if self.username else "")


def x_client(creds: dict):
    """Buffer (free) when set up that way, otherwise the paid X API."""
    return XViaBuffer(creds) if creds.get("via") == "buffer" else X(creds)


CLIENTS = {"bluesky": Bluesky, "mastodon": Mastodon, "devto": DevTo, "telegram": Telegram, "discord": Discord,
           "x": x_client, "threads": ThreadsViaBuffer, "substack": BufferChannel}


# ------------------------------------------------------------------ Mastodon: sign in with the browser (no token to copy)

MASTODON_PORT = 8767
MASTODON_REDIRECT = f"http://127.0.0.1:{MASTODON_PORT}/callback"
MASTODON_SCOPES = "read:accounts read:follows write:statuses write:media write:accounts write:follows"


def browser_authorize(authorize_url: str, port: int, service: str, open_browser=webbrowser.open,
                      wait_seconds: int = 300, trace=None) -> dict:
    """Open an OAuth Authorize page and catch the redirect (?code=...&state=...) on 127.0.0.1:<port>."""
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
            self.wfile.write(f"<h2>ReachPilot is connected to {service}. You can close this tab.</h2>".encode())

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", port), Handler)
    server.timeout = 5
    if trace:
        trace("AUTHORIZE " + authorize_url.split("?")[0])
    open_browser(authorize_url)
    deadline = datetime.now() + timedelta(seconds=wait_seconds)
    while "code" not in received and "error" not in received and datetime.now() < deadline:
        server.handle_request()
    server.server_close()
    return received


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
    authorize_url = f"{base}/oauth/authorize?" + urllib.parse.urlencode({
        "client_id": app["client_id"],
        "redirect_uri": MASTODON_REDIRECT,
        "response_type": "code",
        "scope": MASTODON_SCOPES,
        "state": state,
    })
    received = browser_authorize(authorize_url, MASTODON_PORT, "Mastodon", open_browser, wait_seconds, trace)
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


# ------------------------------------------------------------------ X: OAuth 2.0 with PKCE in the browser

X_PORT = 8768
X_REDIRECT = f"http://127.0.0.1:{X_PORT}/callback"
X_SCOPES = "tweet.read tweet.write users.read media.write offline.access"
X_AUTHORIZE = "https://x.com/i/oauth2/authorize"
X_TOKEN = "https://api.x.com/2/oauth2/token"


def _x_token_request(creds: dict, form: dict, what: str) -> dict:
    """POST to X's token endpoint. A confidential app (with a client secret) uses Basic auth, a public one sends its id."""
    secret = creds.get("client_secret")
    form = {**form, "client_id": creds["client_id"]}
    token = _check(requests.post(X_TOKEN, data=form, auth=(creds["client_id"], secret) if secret else None,
                                 timeout=TIMEOUT), what).json()
    return {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token") or creds.get("refresh_token", ""),
        "expires_at": time.time() + int(token.get("expires_in", 7200)),
    }


def x_sign_in(client_id: str, client_secret: str = "", open_browser=webbrowser.open, wait_seconds: int = 300,
              trace=None) -> dict:
    """Open X's Authorize page (PKCE), catch the code on localhost, swap it for an access + refresh token."""
    verifier = token_tools.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = token_tools.token_urlsafe(16)
    authorize_url = X_AUTHORIZE + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": X_REDIRECT,
        "scope": X_SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    received = browser_authorize(authorize_url, X_PORT, "X", open_browser, wait_seconds, trace)
    if received.get("state") != state or "code" not in received:
        raise RuntimeError(f"X sign-in failed: {received.get('error_description') or received.get('error') or 'timed out'}")
    creds = {"client_id": client_id}
    if client_secret:
        creds["client_secret"] = client_secret
    creds.update(_x_token_request(creds, {
        "grant_type": "authorization_code",
        "code": received["code"],
        "redirect_uri": X_REDIRECT,
        "code_verifier": verifier,
    }, "X token"))
    return creds


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
        "steps": "Type your normal Bluesky password once: ReachPilot uses it to create an app password named "
                 "'ReachPilot' and stores only that (revoke it any time in Settings > App passwords). "
                 "An existing app password works too.",
        "fields": [("handle", "Your handle (e.g. juw12.bsky.social)", False),
                   ("app_password", "Bluesky password (used once, not stored)", True)],
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
    "x": {
        "steps": "Free through Buffer (X's own API now charges per post). Once: sign up at buffer.com (free plan), "
                 f"connect your X account at {BUFFER_CHANNELS}, then create an API key at {BUFFER_API_SETTINGS} "
                 "(opening it now) and paste it here. Type 'api' instead to use the paid X API.",
        "fields": [("api_key", "Buffer API key (or 'api')", True)],
    },
    "threads": {
        "steps": f"Free through Buffer: connect Threads at {BUFFER_CHANNELS}. Uses the same Buffer API key as X.",
        "fields": [("api_key", "Buffer API key (Enter = the one saved for X)", True)],
    },
    "substack": {
        "steps": f"Free through Buffer (posts Substack Notes): connect Substack at {BUFFER_CHANNELS}. Same Buffer API key.",
        "fields": [("api_key", "Buffer API key (Enter = the one saved for X)", True)],
    },
}


def x_setup(answer: str, ask=input, ask_secret=getpass.getpass) -> dict:
    """'api' -> paid X API sign-in; anything else is a Buffer API key -> pick the connected X account."""
    if answer.strip().lower() == "api":
        print("  Paid X API: about $0.20 per post with a link, capped by x_monthly_budget_usd in config.json. "
              f"console.x.com > credits > your app > User authentication: 'Read and write', callback {X_REDIRECT}.")
        client_id = ask("  OAuth 2.0 Client ID: ").strip()
        client_secret = ask_secret("  Client Secret (Enter if none): ").strip()
        print("  Opening X in your browser: press Authorize app there.")
        return x_sign_in(client_id, client_secret)
    return buffer_setup(answer.strip(), "x", ask)


def buffer_setup(api_key: str, channel: str, ask=input) -> dict:
    """Find this platform's account in Buffer (asks only if there are several) and return its credentials."""
    label = LABELS[channel]
    accounts = [account for account in buffer_channels(api_key, BUFFER_SERVICES[channel]) if not account["disconnected"]]
    if not accounts:
        webbrowser.open(BUFFER_CHANNELS)
        raise RuntimeError(f"No {label} account in Buffer yet: connect it at {BUFFER_CHANNELS} (opened), then run this again.")
    chosen = accounts[0]
    if len(accounts) > 1:
        for number, account in enumerate(accounts, 1):
            print(f"  {number}. {account['name']}")
        picked = ask(f"  Which {label} account? [1]: ").strip() or "1"
        chosen = accounts[int(picked) - 1]
    return {"via": "buffer", "api_key": api_key, "channel_id": chosen["id"], "username": chosen["name"]}


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
        if channel == "x":
            webbrowser.open(BUFFER_API_SETTINGS)
        values = {}
        for key, prompt, hidden in SETUP[channel]["fields"]:
            values[key] = (getpass.getpass(f"  {prompt}: ") if hidden else input(f"  {prompt}: ")).strip()
        if channel == "bluesky" and all(values.values()):
            try:
                values = bluesky_sign_in(values["handle"], values["app_password"])
            except Exception as error:
                print(f"  {error}\n  Nothing saved for {label}.\n")
                continue
        if channel == "mastodon" and values.get("instance") and not values.get("access_token"):
            try:
                print("  Opening Mastodon in your browser: press Authorize there.")
                values = mastodon_sign_in(values["instance"])
            except Exception as error:
                print(f"  {error}\n")
                continue
        if channel in BUFFER_ONLY:
            buffer_key = values.get("api_key") or creds.get("x", {}).get("api_key", "")
            if buffer_key:
                try:
                    values = buffer_setup(buffer_key, channel)
                except Exception as error:
                    print(f"  {error}\n  Nothing saved for {label}.\n")
                    continue
        if channel == "x" and values.get("api_key"):
            try:
                values = x_setup(values["api_key"])
            except Exception as error:
                print(f"  {error}\n  Nothing saved for {label}.\n")
                continue
        if not all(values.values()):
            print("  Skipped (empty value).\n")
            continue
        try:
            who = CLIENTS[channel](values).verify()
        except Exception as error:
            print(f"  Could not connect: {error}\n  Nothing saved for {label}.\n")
            continue
        if channel == "x" and x_is_paid(values):
            values["username"] = who.lstrip("@")  # for post links, without a paid lookup per post
        save_channel(secrets_path, channel, values)
        print(f"  {label} connected as {who}.\n")
    final = credentials(json.loads(secrets_path.read_text(encoding="utf-8")) if secrets_path.exists() else {})
    ready = [LABELS[channel] for channel in AUTO if final.get(channel)]
    print("Posting to: LinkedIn" + "".join(f", {name}" for name in ready))
