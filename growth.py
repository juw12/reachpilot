"""Grow the audience on every connected platform and point all of it at GitHub. Runs once a day from tick().

What it does (official APIs only, nothing that looks like a spam bot):
  profiles    Mastodon bio + link fields (GitHub, portfolio, Dev.to, Bluesky) + featured hashtags + "discoverable";
              Bluesky bio. Pushed only when config.json "profile" changes, so edits made in the apps are kept.
  pin         the first post on Mastodon and Bluesky is pinned to the profile (the intro that links to GitHub).
  follow back new followers on Mastodon and Bluesky, at most FOLLOW_BACK_CAP a day each.
  audience    follower counts per platform (Mastodon, Bluesky, Dev.to, GitHub) saved daily for the dashboard.
  github      the Mastodon / Bluesky / Dev.to accounts listed on the GitHub profile (profile "socials"), using the
              GitHub login Git already stores; only when that list changes.
Every action is written to growth_log, which the dashboard shows.

Not done on purpose: following or liking strangers' posts automatically. Both Mastodon instances and Bluesky
treat that as spam and can suspend the account.
LinkedIn has no API for profile edits or follower counts on a self-serve app, so it is not touched here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import requests

import channels

FOLLOW_BACK_CAP = 25
TIMEOUT = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS growth_state(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS audience(day TEXT, channel TEXT, followers INTEGER, PRIMARY KEY(day, channel));
CREATE TABLE IF NOT EXISTS growth_log(at TEXT, channel TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS github_referrers(day TEXT, platform TEXT, views INTEGER, uniques INTEGER,
    PRIMARY KEY(day, platform));
"""


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)


def _state(db, key, default=None):
    row = db.execute("SELECT value FROM growth_state WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def _set_state(db, key, value) -> None:
    db.execute("INSERT OR REPLACE INTO growth_state(key, value) VALUES(?, ?)", (key, str(value)))
    db.commit()


def record(db, channel: str, action: str, detail: str = "") -> None:
    """One line in the dashboard's growth history."""
    db.execute("INSERT INTO growth_log(at, channel, action, detail) VALUES(?,?,?,?)",
               (datetime.now().strftime("%Y-%m-%d %H:%M"), channel, action, detail[:300]))
    db.commit()


def _check(response: requests.Response, what: str) -> requests.Response:
    if response.status_code >= 300:
        raise RuntimeError(f"{what} failed: HTTP {response.status_code} {response.text[:200]}")
    return response


def profile_hash(profile: dict) -> str:
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def follow_back_targets(followers: list[dict], already_following: set[str], cap: int = FOLLOW_BACK_CAP) -> list[str]:
    """Ids of followers we don't follow yet, oldest first in the list order given, capped. Pure (tested)."""
    targets = []
    for follower in followers:
        if follower["id"] in already_following or follower.get("bot"):
            continue
        targets.append(follower["id"])
        if len(targets) >= cap:
            break
    return targets


# ------------------------------------------------------------------ Mastodon

class MastodonGrowth:
    def __init__(self, creds: dict):
        client = channels.Mastodon(creds)
        self.instance = client.instance
        self.headers = client.headers
        self.me = _check(requests.get(f"{self.instance}/api/v1/accounts/verify_credentials", headers=self.headers,
                                      timeout=TIMEOUT), "Mastodon account").json()

    def followers_count(self) -> int:
        return int(self.me.get("followers_count", 0))

    def update_profile(self, profile: dict) -> None:
        data = {"note": profile["bio_long"], "discoverable": "true", "indexable": "true"}
        for index, (name, value) in enumerate(list(profile["links"].items())[:4]):
            data[f"fields_attributes[{index}][name]"] = name
            data[f"fields_attributes[{index}][value]"] = value
        _check(requests.patch(f"{self.instance}/api/v1/accounts/update_credentials", headers=self.headers,
                              data=data, timeout=TIMEOUT), "Mastodon profile")
        existing = {tag["name"].lower() for tag in _check(requests.get(
            f"{self.instance}/api/v1/featured_tags", headers=self.headers, timeout=TIMEOUT), "Mastodon tags").json()}
        for tag in profile.get("hashtags", []):
            if tag.lower() not in existing:
                requests.post(f"{self.instance}/api/v1/featured_tags", headers=self.headers,
                              data={"name": tag}, timeout=TIMEOUT)

    def pin(self, post_url: str) -> None:
        status_id = post_url.rstrip("/").rsplit("/", 1)[-1]
        response = requests.post(f"{self.instance}/api/v1/statuses/{status_id}/pin", headers=self.headers,
                                 timeout=TIMEOUT)
        if response.status_code >= 300 and "already" not in response.text.lower():
            _check(response, "Mastodon pin")

    def follow_back(self, cap: int) -> int:
        followers = _check(requests.get(f"{self.instance}/api/v1/accounts/{self.me['id']}/followers",
                                        headers=self.headers, params={"limit": 80}, timeout=TIMEOUT),
                           "Mastodon followers").json()
        if not followers:
            return 0
        relations = _check(requests.get(f"{self.instance}/api/v1/accounts/relationships", headers=self.headers,
                                        params=[("id[]", follower["id"]) for follower in followers], timeout=TIMEOUT),
                           "Mastodon relationships").json()
        following = {relation["id"] for relation in relations if relation.get("following") or relation.get("requested")}
        done = 0
        for account_id in follow_back_targets(followers, following, cap):
            if requests.post(f"{self.instance}/api/v1/accounts/{account_id}/follow", headers=self.headers,
                             timeout=TIMEOUT).status_code < 300:
                done += 1
        return done


# ------------------------------------------------------------------ Bluesky

class BlueskyGrowth:
    def __init__(self, creds: dict):
        client = channels.Bluesky(creds)
        self.service = client.service
        session = client.session()
        self.did = session["did"]
        self.headers = {"Authorization": f"Bearer {session['accessJwt']}"}

    def _get(self, method: str, params: dict) -> dict:
        return _check(requests.get(f"{self.service}/xrpc/{method}", headers=self.headers, params=params,
                                   timeout=TIMEOUT), f"Bluesky {method}").json()

    def _post(self, method: str, body: dict) -> dict:
        return _check(requests.post(f"{self.service}/xrpc/{method}", headers=self.headers, json=body,
                                    timeout=TIMEOUT), f"Bluesky {method}").json()

    def followers_count(self) -> int:
        return int(self._get("app.bsky.actor.getProfile", {"actor": self.did}).get("followersCount", 0))

    def _profile_record(self) -> dict:
        response = requests.get(f"{self.service}/xrpc/com.atproto.repo.getRecord", headers=self.headers,
                                params={"repo": self.did, "collection": "app.bsky.actor.profile", "rkey": "self"},
                                timeout=TIMEOUT)
        return response.json().get("value", {}) if response.ok else {}

    def _put_profile(self, record: dict) -> None:
        record["$type"] = "app.bsky.actor.profile"
        self._post("com.atproto.repo.putRecord", {"repo": self.did, "collection": "app.bsky.actor.profile",
                                                  "rkey": "self", "record": record})

    def update_profile(self, profile: dict) -> None:
        record = self._profile_record()  # keeps avatar, banner and pinned post
        record["description"] = profile["bio_short"]
        record.setdefault("displayName", profile["name"])
        self._put_profile(record)

    def pin(self, post_url: str) -> None:
        rkey = post_url.rstrip("/").rsplit("/", 1)[-1]
        post = self._get("com.atproto.repo.getRecord", {"repo": self.did, "collection": "app.bsky.feed.post",
                                                        "rkey": rkey})
        record = self._profile_record()
        record["pinnedPost"] = {"uri": post["uri"], "cid": post["cid"]}
        self._put_profile(record)

    def follow_back(self, cap: int) -> int:
        followers = self._get("app.bsky.graph.getFollowers", {"actor": self.did, "limit": 100}).get("followers", [])
        candidates = [{"id": follower["did"], "bot": False} for follower in followers]
        following = {follower["did"] for follower in followers if follower.get("viewer", {}).get("following")}
        done = 0
        for did in follow_back_targets(candidates, following, cap):
            self._post("com.atproto.repo.createRecord", {
                "repo": self.did, "collection": "app.bsky.graph.follow",
                "record": {"$type": "app.bsky.graph.follow", "subject": did,
                           "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")},
            })
            done += 1
        return done


GROWERS = {"mastodon": MastodonGrowth, "bluesky": BlueskyGrowth}


# ------------------------------------------------------------------ GitHub profile links

def github_token() -> str:
    """The GitHub login Git already keeps (Git Credential Manager). Never printed or stored by ReachPilot."""
    import os
    import subprocess

    from trylinks import _git_exe

    result = subprocess.run(
        [_git_exe(), "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, timeout=60, creationflags=0x08000000,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
    )
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    return fields.get("password", "")


def missing_socials(wanted: list[str], existing: list[str]) -> list[str]:
    """Links not yet on the GitHub profile (trailing slashes ignored). Pure (tested)."""
    have = {url.rstrip("/").lower() for url in existing}
    return [url for url in wanted if url.rstrip("/").lower() not in have]


def sync_github_socials(socials: list[str]) -> list[str]:
    """Add any missing links to the GitHub profile's social accounts. Returns the links added."""
    token = github_token()
    if not token:
        raise RuntimeError("no GitHub login stored by Git")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    current = _check(requests.get("https://api.github.com/user/social_accounts", headers=headers, timeout=TIMEOUT),
                     "GitHub social accounts").json()
    missing = missing_socials(socials, [account["url"] for account in current])
    if missing:
        _check(requests.post("https://api.github.com/user/social_accounts", headers=headers,
                             json={"account_urls": missing}, timeout=TIMEOUT), "GitHub add social accounts")
    return missing


# ------------------------------------------------------------------ daily run

def devto_followers(creds: dict) -> int:
    response = _check(requests.get("https://dev.to/api/followers/users", headers={"api-key": creds["api_key"]},
                                   params={"per_page": 1000}, timeout=TIMEOUT), "Dev.to followers")
    return len(response.json())


def run_daily(db, config: dict, secrets: dict, gh_api, log) -> dict:
    """One pass per day. Every step is independent: a failure is logged and the rest still runs."""
    ensure_schema(db)
    creds = channels.credentials(secrets)
    profile = config.get("profile")
    today = datetime.now().strftime("%Y-%m-%d")
    summary: dict = {}
    counts: dict[str, int] = {}

    for channel, grower_class in GROWERS.items():
        if not creds.get(channel):
            continue
        try:
            grower = grower_class(creds[channel])
        except Exception as error:
            log.warning("Growth sign-in for %s failed: %s", channel, error)
            summary[f"{channel}_error"] = str(error)[:200]
            continue

        def step(name, action):
            try:
                return action()
            except Exception as error:  # one step failing never stops the others
                log.warning("Growth %s for %s failed: %s", name, channel, error)
                summary[f"{channel}_{name}_error"] = str(error)[:200]
                return None

        if profile and _state(db, f"profile_{channel}") != profile_hash(profile):
            if step("profile", lambda: grower.update_profile(profile)) is None and f"{channel}_profile_error" not in summary:
                _set_state(db, f"profile_{channel}", profile_hash(profile))
                summary[f"{channel}_profile"] = "updated"
                record(db, channel, "Profile updated", profile["bio_short"])
        if not _state(db, f"pinned_{channel}"):
            first = db.execute("SELECT url FROM channel_posts WHERE channel=? AND status='posted' AND url IS NOT NULL "
                               "ORDER BY posted_at LIMIT 1", (channel,)).fetchone()
            if first and step("pin", lambda: grower.pin(first[0])) is None and f"{channel}_pin_error" not in summary:
                _set_state(db, f"pinned_{channel}", first[0])
                summary[f"{channel}_pinned"] = first[0]
                record(db, channel, "Intro post pinned", first[0])
        followed = step("follow_back", lambda: grower.follow_back(FOLLOW_BACK_CAP))
        if followed is not None:
            summary[f"{channel}_followed_back"] = followed
            if followed:
                record(db, channel, "Followed back", f"{followed} new follower(s)")
        followers = step("followers", grower.followers_count)
        if followers is not None:
            counts[channel] = followers

    if creds.get("devto"):
        try:
            counts["devto"] = devto_followers(creds["devto"])
        except Exception as error:
            log.warning("Dev.to follower count failed: %s", error)
    socials = (profile or {}).get("socials", [])
    if socials and _state(db, "github_socials") != profile_hash({"socials": socials}):
        try:
            added = sync_github_socials(socials)
            _set_state(db, "github_socials", profile_hash({"socials": socials}))
            if added:
                record(db, "github", "Profile links added", ", ".join(added))
            summary["github_socials_added"] = added
        except Exception as error:
            log.warning("GitHub profile links failed: %s", error)
            summary["github_socials_error"] = str(error)[:200]
    user = gh_api(f"users/{config['github_user']}")
    if user:
        counts["github"] = int(user.get("followers", 0))

    for channel, followers in counts.items():
        db.execute("INSERT OR REPLACE INTO audience(day, channel, followers) VALUES(?,?,?)", (today, channel, followers))
    db.commit()
    summary["followers"] = counts
    log.info("Growth: %s", summary)
    return summary
