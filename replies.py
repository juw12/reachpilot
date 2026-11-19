"""Replies: what people write to you on the connected platforms, answered.

Every 30 minutes this PC

  1. reads what is new and meant for you: replies, mentions and quotes on Bluesky, mentions and replies on
     Mastodon, direct messages on both, and comments on your Dev.to articles,
  2. drops what you already answered in the app itself,
  3. writes an answer where one sentence does it: a thank-you for kind words, the link for someone who asks
     where the project is. The sentences are fixed and the link is the repository the post was about, so
     nothing is made up. Everything else (a real question, criticism, anything longer) waits for your own words,
  4. sends. In "review" (how it starts) nothing leaves by itself: every answer waits on the dashboard and in
     To do for Send, Write my own or Skip, on the PC or the phone. In "auto" the one-sentence answers to public
     comments go out by themselves, at most 10 a day per platform and one a day per person, and only while the
     comment is younger than two days. A direct message is never answered without you.

Only people who wrote to you first get an answer: a stranger's post is never touched.

What it can read depends on the platform, not on ReachPilot:

    Bluesky    comments yes; messages need an app password that allows them (connect Bluesky again once)
    Mastodon   comments and messages, once the sign-in allows reading them (connect Mastodon again once)
    Dev.to     comments are read; Dev.to gives apps no way to post one, so the answer is written on Dev.to
    LinkedIn, X, Threads, Substack, Telegram, Discord: not readable for an app connected the way these are

State lives in data/replies/ (git-ignored): what was found, what was answered and how.

    python replies.py            run once (the scheduled task does this every 30 minutes)
    python replies.py install    create the task "\\ReachPilot\\Replies"
    python replies.py show       print what the dashboard shows
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import channels
import machine
from launch_auto import _only_one, leave_low_priority

CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = CODE_DIR / "data" / "replies"
SECRETS_FILE = CODE_DIR / "secrets.json"
CONFIG_FILE = CODE_DIR / "config.json"
DB_FILE = CODE_DIR / "state.db"
LOG_FILE = CODE_DIR / "replies.log"
TASK_PATH = "\\ReachPilot\\"
TASK_NAME = "Replies"

TIMEOUT = 60
MODES = ("review", "auto")
TAKE_DAYS = 7  # a comment older than this is not taken in: the first run must not dig up last year
ALONE_HOURS = 48  # an answer that arrives later than this is odd from a machine: it waits for me
ALONE_PER_DAY = 10  # answers sent without me, per platform and day
CHECKED_PER_RUN = 30  # waiting comments asked about per platform and run ("did I answer this in the app?")
KEPT = 300  # settled comments kept for the card and the limits; a waiting one is never dropped
SHOWN = 8
BLUESKY_POST = 300
BLUESKY_MESSAGE = 1000
MASTODON_POST = 500
SAID = 500  # what a typed answer may hold; a button's name carries it to this PC

ACTION = re.compile(r"^replies:(?:(mode):(review|auto)|(send|skip):([0-9a-f]{12})|(say):([0-9a-f]{12})\|(.+))$", re.S)

# Platforms that are connected for posting and cannot be read: why, and where their notifications are.
CANNOT = {
    "linkedin": ("LinkedIn lets an app like this one post. Reading comments or messages is closed to it.",
                 "https://www.linkedin.com/notifications/"),
    "x": ("Posts reach X through Buffer, which hands no replies or messages to an app; X's own API is paid.",
          "https://x.com/notifications"),
    "threads": ("Posts reach Threads through Buffer, which hands no replies to an app.", "https://www.threads.com/activity"),
    "substack": ("Posts reach Substack through Buffer, which hands no replies to an app.", "https://substack.com/activity"),
    "telegram": ("Not read: the bot only posts to the channel.", ""),
    "discord": ("A webhook can only post. It cannot read the server.", ""),
}
SIGN_IN_AGAIN = {
    "mastodon": "The saved sign-in may only post. Connect Mastodon again (sign in with the browser) to let it read replies and messages.",
    "bluesky": "The saved app password may not read messages. Connect Bluesky again with your normal password to allow them.",
}

MENTION = re.compile(r"(?<![\w@])@[\w.-]+(@[\w.-]+)?")
TAG = re.compile(r"<[^>]+>")
WANTS_LINK = re.compile(r"\b(link|repo|repository|source|github)\b", re.I)
ASKING = re.compile(
    r"\b(where|is there|are there|got|do you have|have you got|share|any|what'?s the|what is the|can (i|we|you)"
    r"|could (i|we|you)|open[- ]source)\b", re.I)
WHERE_IS_IT = re.compile(r"\bwhere (can|do|could) (i|we) (find|get|see|try|download) (it|this|that)\b", re.I)
KIND_WORDS = re.compile(
    r"\b(thanks|thank you|thx|nice|cool|great|awesome|amazing|neat|brilliant|well done|impressive|congrats|congratulations"
    r"|useful|helpful|love (this|it|that)|good (job|work|stuff|one|idea))\b|[\U0001F525\U0001F44F\U0001F64C\U0001F44D\u2764]", re.I)
# One of these and the comment is mine to answer: criticism, a problem, sarcasm, or someone selling something.
NOT_SO_KIND = re.compile(
    r"\b(not|no|never|but|however|though|except|bug|bugs|broken|crash|crashes|fail|fails|failed|error|wrong|bad|worse|worst"
    r"|slop|spam|scam|fake|stolen|copied|useless|hate|meh|another|nothing|lol|lmao|if only|nice try|cool story|yeah right|sure"
    r"|check out|follow|followers|dm me|my (profile|page|channel|bio)|click|buy|cheap|free|promo|crypto|giveaway|subscribe|visit)\b"
    r"|n't\b|\.\.\.|\u2026", re.I)
THANKS = ("Thanks, glad you like it!", "Thank you, good to hear.", "Thanks for taking a look!")


# ------------------------------------------------------------------ small helpers

def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-199:] if LOG_FILE.exists() else []
        LOG_FILE.write_text("\n".join(lines + [line]) + "\n", encoding="utf-8")
    except OSError:
        pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _moment(value) -> datetime | None:
    """A platform's time as an aware datetime; None when it cannot be read."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _local(value) -> str:
    """The same moment on this PC's clock, as the dashboard prints dates."""
    moment = _moment(value)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M") if moment else ""


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def plain(markup: str) -> str:
    """Mastodon and Dev.to send HTML; what was written is the text in it."""
    text = re.sub(r"<br\s*/?>|</p>\s*<p[^>]*>", "\n", markup or "", flags=re.I)
    return re.sub(r"[ \t]+", " ", html.unescape(TAG.sub("", text))).strip()


def _http(method: str, url: str, **kwargs):
    """(HTTP status, the JSON answer or {}). The one place that talks to a platform; the self-test replaces it."""
    response = requests.request(method, url, timeout=TIMEOUT, **kwargs)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, {}


def _ok(answer, what: str):
    status, payload = answer
    if status >= 300:
        told = payload if isinstance(payload, dict) else {}
        raise RuntimeError(f"{what} failed: HTTP {status} {str(told.get('message') or told.get('error') or '')[:160]}".strip())
    return payload


class CannotRead(Exception):
    """The platform is connected, but this sign-in may not read what people wrote."""


# ------------------------------------------------------------------ what a comment asks for, and the answer to it

def classify(text: str) -> str:
    """'thanks' (kind words, nothing else), 'where' (asks for the link) or 'other' (needs my own words). Pure.

    Strict on purpose: a wrong 'other' costs me one tap, a wrong 'thanks' answers criticism with a smile."""
    said = " ".join(MENTION.sub(" ", text or "").split())
    if not said or len(said) > 100 or channels.URL_PATTERN.search(said) or NOT_SO_KIND.search(said):
        return "other"
    if said.count("?") == 1:
        asks_for_link = WANTS_LINK.search(said) and (ASKING.search(said) or len(said.split()) <= 3)
        return "where" if asks_for_link or WHERE_IS_IT.search(said) else "other"
    if "?" not in said and len(said) <= 80 and KIND_WORDS.search(said):
        return "thanks"
    return "other"


def draft(kind_of: str, key: str, repo: str, user: str) -> str:
    """The answer to a simple comment: a fixed sentence, or the link of what the post was about. '' for the rest."""
    if kind_of == "thanks":
        return THANKS[int(key[:6], 16) % len(THANKS)]
    if kind_of == "where" and user:
        return f"Here it is: https://github.com/{user}/{repo}" if repo else f"Everything I build is here: https://github.com/{user}"
    return ""


def posted(db_file: Path | None = None) -> dict[tuple[str, str], str]:
    """{(platform, last part of the post's address): repository} for every post ReachPilot sent to another platform."""
    db_file = db_file or DB_FILE
    if not db_file.exists():
        return {}
    found = {}
    try:
        connection = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=3)
        try:
            for channel, repo, url in connection.execute(
                    "select channel, repo, url from channel_posts where status='posted' and url is not null and url != ''"):
                found[(channel, url.rstrip("/").rsplit("/", 1)[-1])] = repo or ""
        finally:
            connection.close()
    except sqlite3.Error:
        pass
    return found


# ------------------------------------------------------------------ Bluesky

class BlueskyReplies:
    by_hand = False
    CHAT_SERVICE = "https://api.bsky.chat"

    def __init__(self, creds: dict):
        self.service = creds.get("service", "https://bsky.social").rstrip("/")
        session = _ok(_http("POST", f"{self.service}/xrpc/com.atproto.server.createSession",
                            json={"identifier": creds["handle"].lstrip("@"), "password": creds["app_password"]}), "Bluesky sign-in")
        self.me, self.handle = session["did"], session["handle"]
        self.headers = {"Authorization": f"Bearer {session['accessJwt']}"}
        self.messages = ""  # why messages cannot be read; "" when they can

    def _chat(self, verb: str, method: str, **sent):
        """Messages are asked for at Bluesky's chat service itself, with a pass for that one method.

        bsky.social does not hand a chat call on (HTTP 501 "not implemented", seen 19 Nov 2026), and the account's own
        server (*.host.bsky.network) is closed to some office networks. bsky.social does give out the pass, and
        refuses it to an app password made without "allow direct messages".
        """
        status, ticket = _http("GET", f"{self.service}/xrpc/com.atproto.server.getServiceAuth", headers=self.headers,
                               params={"aud": "did:web:api.bsky.chat", "lxm": method})
        if status >= 300:
            return status, ticket
        return _http(verb, f"{self.CHAT_SERVICE}/xrpc/{method}", headers={"Authorization": f"Bearer {ticket['token']}"}, **sent)

    def _get(self, method: str, params: dict, chat: bool = False):
        if chat:
            return self._chat("GET", method, params=params)
        return _http("GET", f"{self.service}/xrpc/{method}", headers=self.headers, params=params)

    def _post(self, method: str, body: dict, chat: bool = False) -> dict:
        answer = (self._chat("POST", method, json=body) if chat
                  else _http("POST", f"{self.service}/xrpc/{method}", headers=self.headers, json=body))
        return _ok(answer, f"Bluesky {method}")

    def read(self) -> list[dict]:
        found = []
        notes = _ok(self._get("app.bsky.notification.listNotifications", {"limit": 50}), "Bluesky notifications")
        for note in notes.get("notifications") or []:
            if note.get("reason") not in ("reply", "mention", "quote"):
                continue
            record, author = note.get("record") or {}, note.get("author") or {}
            here = {"uri": note["uri"], "cid": note["cid"]}
            found.append({
                "platform": "bluesky", "kind": "comment", "id": note["uri"], "person": author.get("did", ""),
                "author": author.get("handle", ""), "name": author.get("displayName") or "", "text": record.get("text", ""),
                "url": f"https://bsky.app/profile/{author.get('handle', '')}/post/{note['uri'].rsplit('/', 1)[-1]}",
                "at": note.get("indexedAt"),
                "on": str(note.get("reasonSubject") or "").rsplit("/", 1)[-1] if note.get("reason") == "reply" else "",
                "target": {"parent": here, "root": (record.get("reply") or {}).get("root") or here},
            })
        status, chats = self._get("chat.bsky.convo.listConvos", {"limit": 30}, chat=True)
        if status >= 300:
            # an app password made without "allow direct messages" is refused here, and only here (HTTP 400);
            # anything else is Bluesky or the network, and connecting again would not help
            self.messages = SIGN_IN_AGAIN["bluesky"] if status in (400, 401, 403) else f"Bluesky did not hand over the messages (HTTP {status})."
            return found
        for chat in chats.get("convos") or []:
            last = chat.get("lastMessage") or {}
            sender = (last.get("sender") or {}).get("did")
            if not last.get("text") or sender in (None, self.me) or chat.get("muted"):
                continue
            member = next((one for one in chat.get("members") or [] if one.get("did") == sender), {})
            found.append({
                "platform": "bluesky", "kind": "dm", "id": f"{chat['id']}/{last.get('id', '')}", "person": sender,
                "author": member.get("handle", ""), "name": member.get("displayName") or "", "text": last["text"],
                "url": f"https://bsky.app/messages/{chat['id']}", "at": last.get("sentAt"), "on": "",
                "target": {"chat": chat["id"]},
            })
        return found

    def answered(self, item: dict) -> bool | None:
        """True when I already answered in the app, None when what was written is gone."""
        if item["kind"] == "dm":
            chat = _ok(self._get("chat.bsky.convo.getConvo", {"convoId": item["target"]["chat"]}, chat=True), "Bluesky chat")
            return (((chat.get("convo") or {}).get("lastMessage") or {}).get("sender") or {}).get("did") == self.me
        status, payload = self._get("app.bsky.feed.getPostThread",
                                    {"uri": item["target"]["parent"]["uri"], "depth": 1, "parentHeight": 0})
        thread = (payload.get("thread") or {}) if isinstance(payload, dict) else {}
        if (status == 400 and payload.get("error") == "NotFound") or thread.get("notFound") or thread.get("blocked"):
            return None
        _ok((status, payload), "Bluesky thread")
        return any(((reply.get("post") or {}).get("author") or {}).get("did") == self.me for reply in thread.get("replies") or [])

    @staticmethod
    def limit(item: dict) -> int:
        return BLUESKY_MESSAGE if item["kind"] == "dm" else BLUESKY_POST

    def send(self, item: dict, text: str) -> str:
        if item["kind"] == "dm":
            self._post("chat.bsky.convo.sendMessage", {"convoId": item["target"]["chat"], "message": {"text": text}}, chat=True)
            return item["url"]
        record = {"$type": "app.bsky.feed.post", "text": text, "langs": ["en"],
                  "createdAt": now_utc().isoformat().replace("+00:00", "Z"),
                  "reply": {"root": item["target"]["root"], "parent": item["target"]["parent"]}}
        facets = channels.bluesky_facets(text)
        if facets:
            record["facets"] = facets
        created = self._post("com.atproto.repo.createRecord",
                             {"repo": self.me, "collection": "app.bsky.feed.post", "record": record})
        return f"https://bsky.app/profile/{self.handle}/post/{created['uri'].rsplit('/', 1)[-1]}"


# ------------------------------------------------------------------ Mastodon

class MastodonReplies:
    by_hand = False

    def __init__(self, creds: dict):
        client = channels.Mastodon(creds)
        self.instance, self.headers = client.instance, client.headers
        self.me = _ok(_http("GET", f"{self.instance}/api/v1/accounts/verify_credentials", headers=self.headers),
                      "Mastodon sign-in")["id"]
        self.messages = ""

    def read(self) -> list[dict]:
        status, notes = _http("GET", f"{self.instance}/api/v1/notifications", headers=self.headers,
                              params={"types[]": "mention", "limit": 30})
        if status == 403:  # the sign-in was made for posting only
            raise CannotRead(SIGN_IN_AGAIN["mastodon"])
        found = []
        for note in _ok((status, notes), "Mastodon notifications"):
            post, account = note.get("status") or {}, note.get("account") or {}
            if not post or account.get("bot") or account.get("id") == self.me:
                continue
            # a message on Mastodon is a post only the people named in it can see
            found.append({
                "platform": "mastodon", "kind": "dm" if post.get("visibility") == "direct" else "comment", "id": post["id"],
                "person": account.get("id", ""), "author": account.get("acct", ""), "name": account.get("display_name") or "",
                "text": plain(post.get("content", "")), "url": post.get("url") or "", "at": post.get("created_at"),
                "on": post.get("in_reply_to_id") or "",
                "target": {"status": post["id"], "visibility": post.get("visibility") or "public", "acct": account.get("acct", "")},
            })
        return found

    def answered(self, item: dict) -> bool | None:
        status, tree = _http("GET", f"{self.instance}/api/v1/statuses/{item['target']['status']}/context", headers=self.headers)
        if status == 404:
            return None
        return any(reply.get("in_reply_to_id") == item["target"]["status"] and (reply.get("account") or {}).get("id") == self.me
                   for reply in _ok((status, tree), "Mastodon thread").get("descendants") or [])

    @staticmethod
    def limit(item: dict) -> int:
        return MASTODON_POST - len(item["target"]["acct"]) - 2  # "@name " in front of the answer counts too

    def send(self, item: dict, text: str) -> str:
        target = item["target"]
        sent = _ok(_http("POST", f"{self.instance}/api/v1/statuses",
                         headers={**self.headers, "Idempotency-Key": hashlib.sha256(f"{item['key']}|{text}".encode()).hexdigest()},
                         # the answer is seen by whoever could see what it answers: a message stays a message
                         data={"status": f"@{target['acct']} {text}", "in_reply_to_id": target["status"],
                               "visibility": target["visibility"], "language": "en"}), "Mastodon reply")
        return sent.get("url") or item["url"]


# ------------------------------------------------------------------ Dev.to (read only: its API has no way to post a comment)

class DevtoReplies:
    by_hand = True
    API = channels.DevTo.API

    def __init__(self, creds: dict):
        self.headers = channels.DevTo(creds).headers
        self.me = _ok(_http("GET", f"{self.API}/users/me", headers=self.headers), "Dev.to sign-in")["username"]
        self.messages = "Dev.to has no messages an app can read."
        self.open: set[str] = set()  # comments nobody answered yet, as read() found them

    def read(self) -> list[dict]:
        found = []
        articles = _ok(_http("GET", f"{self.API}/articles/me/published", headers=self.headers, params={"per_page": 30}),
                       "Dev.to articles")
        for article in articles:
            if not article.get("comments_count"):
                continue
            comments = _ok(_http("GET", f"{self.API}/comments", headers=self.headers, params={"a_id": article["id"]}),
                           "Dev.to comments")
            address = str(article.get("url") or "").rstrip("/").rsplit("/", 1)[-1]
            for comment in comments:
                self._walk(comment, address, found)
        return found

    def _walk(self, comment: dict, address: str, found: list[dict]) -> None:
        user, children = comment.get("user") or {}, comment.get("children") or []
        mine = user.get("username") == self.me
        if not mine and comment.get("id_code") and not any((child.get("user") or {}).get("username") == self.me for child in children):
            self.open.add(comment["id_code"])
            found.append({
                "platform": "devto", "kind": "comment", "id": comment["id_code"], "person": user.get("username", ""),
                "author": user.get("username", ""), "name": user.get("name") or "", "text": plain(comment.get("body_html", "")),
                "url": f"https://dev.to/{user.get('username', '')}/comment/{comment['id_code']}", "at": comment.get("created_at"),
                "on": address, "target": {},
            })
        for child in children:
            self._walk(child, address, found)

    def answered(self, item: dict) -> bool | None:
        return item["id"] not in self.open


READERS = {"bluesky": BlueskyReplies, "mastodon": MastodonReplies, "devto": DevtoReplies}


# ------------------------------------------------------------------ state

def load_state() -> dict:
    state = _read_json(DATA_DIR / "state.json")
    state = state if isinstance(state, dict) else {}
    return {"mode": state.get("mode") if state.get("mode") in MODES else "review",
            "items": state.get("items") if isinstance(state.get("items"), dict) else {},
            "platforms": state.get("platforms") if isinstance(state.get("platforms"), dict) else {},
            "last_run": state.get("last_run") or ""}


def save_state(state: dict) -> None:
    items = state["items"]
    settled = sorted((key for key, item in items.items() if item["status"] != "waiting"),
                     key=lambda key: items[key].get("at") or "")
    for key in settled[:max(0, len(settled) - KEPT)]:
        del items[key]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    scratch = DATA_DIR / "state.json.tmp"
    scratch.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(scratch, DATA_DIR / "state.json")


def item_key(platform: str, item_id: str) -> str:
    return hashlib.sha1(f"{platform}|{item_id}".encode()).hexdigest()[:12]


def take_in(state: dict, found: list[dict], posts: dict, user: str, now: datetime) -> int:
    """Add what was not seen before. Returns how many were new."""
    new = 0
    for raw in found:
        written = _moment(raw.get("at"))
        key = item_key(raw["platform"], raw["id"])
        if key in state["items"] or not written or now - written > timedelta(days=TAKE_DAYS) or not (raw.get("text") or "").strip():
            continue
        if raw["kind"] == "dm":  # a newer message in the same chat replaces the one that still waited
            for other_key, other in list(state["items"].items()):
                if (other["status"] == "waiting" and other["platform"] == raw["platform"] and other["kind"] == "dm"
                        and other["person"] == raw["person"]):
                    del state["items"][other_key]
        kind_of = classify(raw["text"])
        state["items"][key] = {
            **raw, "key": key, "at": written.astimezone(timezone.utc).isoformat(), "text": raw["text"].strip()[:1000],
            "kind_of": kind_of, "draft": draft(kind_of, key, posts.get((raw["platform"], raw.get("on") or ""), ""), user),
            "status": "waiting",
        }
        new += 1
    return new


def settle(state: dict, platform: str, reader, now: datetime) -> int:
    """What I answered in the app itself, or what was deleted, no longer waits. Returns how many were settled."""
    done = 0
    waiting = [item for item in state["items"].values() if item["platform"] == platform and item["status"] == "waiting"]
    for item in waiting[:CHECKED_PER_RUN]:
        try:
            answered = reader.answered(item)
        except Exception:  # not known this time: it keeps waiting
            continue
        if answered is False:
            continue
        item.update({"status": "answered" if answered else "gone", "settled_at": now.isoformat()})
        done += 1
    return done


def may_go_alone(item: dict, items: dict, now: datetime) -> bool:
    """True when this answer may leave without me. Pure."""
    written = _moment(item.get("at"))
    if (item["status"] != "waiting" or item["kind"] != "comment" or item["kind_of"] not in ("thanks", "where")
            or not item.get("draft") or item.get("error") or not written or now - written > timedelta(hours=ALONE_HOURS)):
        return False
    day_ago = now - timedelta(hours=24)
    alone = [other for other in items.values()
             if other["platform"] == item["platform"] and other.get("how") == "alone"
             and (_moment(other.get("sent_at")) or day_ago) > day_ago]
    return len(alone) < ALONE_PER_DAY and all(other["person"] != item["person"] for other in alone)


def deliver(item: dict, reader, text: str, how: str, now: datetime) -> None:
    """Send one answer and write down what happened. A refusal stays on the item and is raised."""
    try:
        address = reader.send(item, text)
    except Exception as error:
        item["error"] = " ".join(str(error).split())[:200] or type(error).__name__
        raise
    item.pop("error", None)
    item.update({"status": "sent", "how": how, "sent_text": text, "sent_at": now.isoformat(), "sent_url": address})


# ------------------------------------------------------------------ one run

def _accounts() -> tuple[dict, str]:
    """({platform: credentials} of the connected platforms ReachPilot posts to, the GitHub user)."""
    config = _read_json(CONFIG_FILE) or {}
    creds = channels.credentials(_read_json(SECRETS_FILE) or {})
    return {name: creds[name] for name in channels.enabled(config) if creds.get(name)}, str(config.get("github_user") or "")


def run(now: datetime | None = None) -> str:
    """One pass over every platform that can be read. Only one pass works at a time on this PC."""
    with _only_one("run", folder=DATA_DIR) as mine:
        if not mine:
            return "another run is still working; nothing done"
        now = now or now_utc()
        state = load_state()
        accounts, user = _accounts()
        posts = posted()
        outcomes = []
        for platform, reader_class in READERS.items():
            if platform not in accounts:
                state["platforms"].pop(platform, None)
                continue
            seen = {"checked": now.isoformat(), "comments": "", "messages": "", "error": ""}
            try:
                reader = reader_class(accounts[platform])
                new = take_in(state, reader.read(), posts, user, now)
                seen["messages"] = reader.messages
                settled = settle(state, platform, reader, now)
                sent = 0
                if state["mode"] == "auto" and not reader.by_hand:
                    for item in sorted(state["items"].values(), key=lambda item: item["at"]):
                        if item["platform"] == platform and may_go_alone(item, state["items"], now):
                            try:
                                deliver(item, reader, item["draft"], "alone", now)
                                sent += 1
                            except Exception as error:  # it now waits for me, with the reason
                                log(f"{platform}: an answer was refused: {item.get('error') or error}")
                waiting = sum(1 for item in state["items"].values() if item["platform"] == platform and item["status"] == "waiting")
                outcomes.append(f"{platform}: {new} new, {settled} settled, {sent} sent, {waiting} waiting")
            except CannotRead as error:
                seen["comments"] = seen["messages"] = str(error)
                outcomes.append(f"{platform}: cannot be read with this sign-in")
            except Exception as error:  # one platform that is down must not stop the others
                seen["error"] = f"{type(error).__name__}: {str(error)[:160]}"
                outcomes.append(f"{platform}: FAILED {seen['error']}")
            state["platforms"][platform] = seen
        state["last_run"] = now.isoformat()
        save_state(state)
        return " | ".join(outcomes) or "no platform that can be read is connected"


# ------------------------------------------------------------------ the dashboard

def card() -> dict:
    """What the dashboard shows, read from disk only: no network, so the page stays quick."""
    state = load_state()
    accounts, _ = _accounts()
    items = sorted(state["items"].values(), key=lambda item: item["at"], reverse=True)
    day_ago = now_utc() - timedelta(hours=24)

    def row(item: dict) -> dict:
        reader = READERS[item["platform"]]
        return {
            "limit": 0 if reader.by_hand else min(SAID, reader.limit(item)),
            "key": item["key"], "platform": channels.LABELS[item["platform"]], "kind": item["kind"], "author": item["author"],
            "name": item["name"], "text": item["text"], "url": item["url"], "at": _local(item["at"]),
            "draft": item.get("draft", ""), "error": item.get("error", ""), "by_hand": READERS[item["platform"]].by_hand,
            "status": item["status"], "how": item.get("how", ""), "sent_text": item.get("sent_text", ""),
            "sent_at": _local(item.get("sent_at")), "sent_url": item.get("sent_url", ""),
        }

    platforms = []
    for name in READERS:
        if name in accounts:
            seen = state["platforms"].get(name) or {}
            platforms.append({"platform": channels.LABELS[name], "checked": _local(seen.get("checked")),
                              "comments": seen.get("comments", ""), "messages": seen.get("messages", ""),
                              "error": seen.get("error", ""), "by_hand": READERS[name].by_hand,
                              "sign_in": name in SIGN_IN_AGAIN
                              and SIGN_IN_AGAIN[name] in (seen.get("comments"), seen.get("messages"))})
    return {
        "installed": (DATA_DIR / "state.json").exists(),
        "mode": state["mode"],
        "last_run": _local(state["last_run"]),
        "platforms": platforms,
        "cannot": [{"platform": "LinkedIn" if name == "linkedin" else channels.LABELS[name], "why": why, "link": link}
                   for name, (why, link) in CANNOT.items() if name == "linkedin" or name in accounts],
        "waiting": [row(item) for item in reversed(items) if item["status"] == "waiting"],
        "recent": [row(item) for item in items if item["status"] in ("sent", "answered", "skipped")][:SHOWN],
        "alone_today": sum(1 for item in items if item.get("how") == "alone" and (_moment(item.get("sent_at")) or day_ago) > day_ago),
    }


def action(name: str) -> str:
    """A button: replies:mode:<review|auto>, replies:send:<key>, replies:skip:<key> or replies:say:<key>|<my words>."""
    match = ACTION.match(name if isinstance(name, str) else "")
    if not match:
        raise ValueError(f"Unknown action: {name}")
    with _only_one("run", wait=20, folder=DATA_DIR) as mine:
        if not mine:
            return "A run is working right now. Try again in a minute."
        state = load_state()
        if match.group(1):
            state["mode"] = match.group(2)
            save_state(state)
            return ("One-sentence answers to public comments now go out by themselves. Messages and everything else still wait for you."
                    if state["mode"] == "auto" else "Nothing is sent by itself now. Every answer waits for you.")
        verb, key = match.group(3) or match.group(5), match.group(4) or match.group(6)
        item = state["items"].get(key)
        if not item or item["status"] != "waiting":
            raise ValueError("That one no longer waits for an answer.")
        now = now_utc()
        if verb == "skip":
            item.update({"status": "skipped", "settled_at": now.isoformat()})
            save_state(state)
            return "Left unanswered."
        label = channels.LABELS[item["platform"]]
        if READERS[item["platform"]].by_hand:
            raise ValueError(f"{label} gives apps no way to post a comment: open it and answer there.")
        text = " ".join((match.group(7) if verb == "say" else item.get("draft") or "").split())
        if not text:
            raise ValueError("There is no suggested answer for this one: write your own." if verb == "send"
                             else "The answer is empty. Nothing was sent.")
        most = min(SAID, READERS[item["platform"]].limit(item))
        if len(text) > most:  # refused, never cut: half a sentence must not go out under my name
            raise ValueError(f"That answer has {len(text)} characters; {label} takes {most} here. Nothing was sent.")
        accounts, _ = _accounts()
        if item["platform"] not in accounts:
            raise ValueError(f"{label} is not connected.")
        reader = READERS[item["platform"]](accounts[item["platform"]])
        answered = reader.answered(item)  # also true after an answer that went out while its confirmation was lost
        if answered is not False:
            item.update({"status": "answered" if answered else "gone", "settled_at": now.isoformat()})
            save_state(state)
            return (f"You already answered that on {label}. Nothing was sent." if answered
                    else f"That is no longer on {label}. Nothing was sent.")
        try:
            deliver(item, reader, text, "you", now)
        except Exception as error:
            save_state(state)
            raise ValueError(f"{label} did not take the answer: {item.get('error') or error}") from error
        save_state(state)
        return f"Sent on {label}."


# ------------------------------------------------------------------ scheduled task and command line

def install_task() -> None:
    """Every 30 minutes, hidden (pythonw), catching up if the PC was off. Same settings as the other tasks."""
    if not machine.IS_WINDOWS:  # a Mac: the same schedule as a launchd agent
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        print(machine.install_task(TASK_NAME, [sys.executable, str(Path(__file__).resolve())], CODE_DIR, every_minutes=30, limit_minutes=15))
        return
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    script = f"""
$action = New-ScheduledTaskAction -Execute '{runner}' -Argument '"{Path(__file__).resolve()}"' -WorkingDirectory '{CODE_DIR}'
$often = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 30)
$settings = New-ScheduledTaskSettingsSet -Priority 4 -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' -Action $action -Trigger $often -Settings $settings `
    -Description 'Comments and messages on the connected platforms: read them and list each with an answer. Nothing is sent by itself unless that is switched on.' -Force | Out-Null
Get-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' | Select-Object TaskName, State
"""
    print(subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True).stdout.strip())


def show() -> None:
    view = card()
    print(f"Mode: {view['mode']}; last run {view['last_run'] or 'never'}; {view['alone_today']} sent by itself in the last 24 hours")
    for platform in view["platforms"]:
        print(f"== {platform['platform']}: comments {platform['comments'] or 'read'}; messages {platform['messages'] or 'read'}"
              f"{'; ' + platform['error'] if platform['error'] else ''}")
    for platform in view["cannot"]:
        print(f"== {platform['platform']}: {platform['why']}")
    for item in view["waiting"]:
        print(f"   waiting {item['platform']} {item['kind']} from @{item['author']}: {item['text'][:80]!r} -> {item['draft'] or '(your own words)'}")
    for item in view["recent"]:
        print(f"   {item['status']} {item['platform']} @{item['author']}: {item['sent_text'] or item['text'][:60]!r}")


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    arguments = sys.argv[1:]
    if arguments == ["install"]:
        install_task()
    elif arguments == ["show"]:
        show()
    elif not arguments:
        leave_low_priority()
        try:
            outcome = run()
        except Exception as error:  # the task must leave a trace, never a silent crash
            outcome = f"FAILED {type(error).__name__}: {error}"
        log(outcome)
        print(outcome)
    else:
        print(__doc__)
