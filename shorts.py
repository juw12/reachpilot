"""Shorts: a channel's finished short videos go to X on fixed days, one at a time, through Buffer.

A channel is a folder of finished videos that something else makes. Next to each video sits a post file
(<anything>_post.json) with "id", "title", "caption", "hashtags" and "video". Every hour this PC

  1. copies newly finished videos and their text into data/shorts/<channel>/, so a post never depends on the
     folder they were made in,
  2. asks Buffer what became of the post that waits there: sent, failed, or removed by hand,
  3. when nothing waits in Buffer, gives the next video a public address and schedules it for the next posting
     day. Buffer publishes it at that time, also when this PC is off.

Never more than one post waits in Buffer. Removing that post in Buffer, or Pause in the dashboard, stops the
channel until Resume. An episode that was sent is never sent again.

Channels are configured in shorts_private.json (git-ignored), posting uses the X account that is connected
through Buffer in secrets.json:

    {"mychannel": {"name": "My channel", "source": "D:\\videos\\out", "order": ["k01", "k02"], "target": 21,
                   "days": ["mon", "wed", "fri", "sun"], "hour": 12, "tags": 2,
                   "status_file": "queue_status.json", "not_connected": ["YouTube", "TikTok"]}}

    python shorts.py            run once (the scheduled task does this every hour)
    python shorts.py install    create the hourly task "\\ReachPilot\\Shorts"
    python shorts.py show       print what the dashboard shows
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from launch_auto import _only_one, leave_low_priority

CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = CODE_DIR / "data" / "shorts"
PRIVATE_FILE = CODE_DIR / "shorts_private.json"
SECRETS_FILE = CODE_DIR / "secrets.json"
CONFIG_FILE = CODE_DIR / "config.json"
PAGES_CHECKOUT = CODE_DIR / ".pages"  # the same local copy of the Pages site that autopilot.py uses
LOG_FILE = CODE_DIR / "shorts.log"
TASK_PATH = "\\ReachPilot\\"
TASK_NAME = "Shorts"

X_LIMIT = 280
DEFAULT_TAGS = 2
DEFAULT_HOUR = 12
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
LEAD_MINUTES = 30  # a post is not scheduled closer to its time than this: Buffer still has to fetch the video
SETTLED_SECONDS = 600  # a video that changed in the last ten minutes may still be written
MAX_TRIES = 2  # a post that X refused goes back in line once; after the second refusal it is left for me
SHOWN_POSTS = 5

CHANNEL = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
EPISODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
ACTION = re.compile(r"^shorts:(pause|resume):([a-z0-9][a-z0-9-]{0,39})$")
SENTENCE = re.compile(r"(?<=[.!?])\s+")

CHANNEL_STATE = """query Channel($input: ChannelInput!) {
  channel(input: $input) { id organizationId isDisconnected isQueuePaused }
}"""
WAITING_POSTS = """query Posts($input: PostsInput!) {
  posts(first: 50, input: $input) { edges { node { id text dueAt status } } }
}"""
POST_STATE = """query Post($input: PostInput!) {
  post(input: $input) { id status dueAt sentAt externalLink error { message } }
}"""
SCHEDULE_POST = """mutation CreatePost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess { post { id status dueAt } }
    ... on MutationError { message }
  }
}"""
DELETE_POST = """mutation DeletePost($input: DeletePostInput!) {
  deletePost(input: $input) {
    ... on DeletePostSuccess { id }
    ... on MutationError { message }
  }
}"""


# ------------------------------------------------------------------ small helpers

def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-199:] if LOG_FILE.exists() else []
        LOG_FILE.write_text("\n".join(lines + [line]) + "\n", encoding="utf-8")
    except OSError:
        pass


class _ToLogFile(logging.Handler):
    """What the video hosting step reports (a failed push, a slow page) lands in the same log file."""

    def emit(self, record: logging.LogRecord) -> None:
        log(record.getMessage())


logger = logging.getLogger("shorts")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    logger.addHandler(_ToLogFile())


def now_local() -> datetime:
    return datetime.now().astimezone()


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _moment(value, like: datetime) -> datetime | None:
    """A saved or Buffer time as an aware datetime in the PC's zone; None when it cannot be read."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (moment if moment.tzinfo else moment.replace(tzinfo=like.tzinfo)).astimezone(like.tzinfo)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def load_config() -> dict:
    """{channel id: settings} for every channel that names a source folder. {} when the file is missing or broken."""
    data = _read_json(PRIVATE_FILE)
    if not isinstance(data, dict):
        return {}
    return {
        channel_id: cfg for channel_id, cfg in data.items()
        if CHANNEL.match(str(channel_id)) and isinstance(cfg, dict) and isinstance(cfg.get("source"), str)
    }


def channel_dir(channel_id: str) -> Path:
    return DATA_DIR / channel_id


def load_state(channel_id: str) -> dict:
    state = _read_json(channel_dir(channel_id) / "state.json")
    state = state if isinstance(state, dict) else {}
    if not isinstance(state.get("episodes"), dict):
        state["episodes"] = {}
    return state


def save_state(channel_id: str, state: dict) -> None:
    folder = channel_dir(channel_id)
    folder.mkdir(parents=True, exist_ok=True)
    draft = folder / "state.json.tmp"
    draft.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    draft.replace(folder / "state.json")


def x_account() -> dict | None:
    """The X account that posts through Buffer, or None when X is not connected that way on this PC."""
    secrets = _read_json(SECRETS_FILE)
    creds = (secrets.get("channels") or {}).get("x") if isinstance(secrets, dict) else None
    if isinstance(creds, dict) and creds.get("via") == "buffer" and creds.get("api_key") and creds.get("channel_id"):
        return creds
    return None


# ------------------------------------------------------------------ the text and the day of a post

def x_text(post: dict, tags: int = DEFAULT_TAGS, measure=None) -> str:
    """The title, then as many whole sentences of the caption as X has room for, then the first hashtags."""
    if measure is None:
        import channels

        measure = channels.x_length
    title = str(post.get("title") or "").strip()
    hashtags = [str(tag).strip() for tag in post.get("hashtags") or [] if str(tag).strip().startswith("#")]
    tail = " ".join(hashtags[:max(0, tags)])
    sentences = [part.strip() for part in SENTENCE.split(str(post.get("caption") or "").strip()) if part.strip()]

    def build(count: int, with_tags: bool) -> str:
        blocks = [title, " ".join(sentences[:count]), tail if with_tags else ""]
        return "\n\n".join(block for block in blocks if block)

    for with_tags in (True, False):
        for count in range(len(sentences), -1, -1):
            text = build(count, with_tags)
            if text and measure(text) <= X_LIMIT:
                return text
    words = title.split()
    while words and measure(" ".join(words) + "…") > X_LIMIT:
        words.pop()
    return " ".join(words) + "…" if words else ""


def posting_days(cfg: dict) -> list[int]:
    days = [DAY_NAMES.index(str(day).lower()[:3]) for day in cfg.get("days") or [] if str(day).lower()[:3] in DAY_NAMES]
    return sorted(set(days)) or list(range(7))


def posting_hour(cfg: dict) -> int:
    hour = cfg.get("hour")
    return hour if isinstance(hour, int) and not isinstance(hour, bool) and 0 <= hour <= 23 else DEFAULT_HOUR


def next_slot(after: datetime, cfg: dict) -> datetime:
    """The first posting time that comes after this moment."""
    days, hour = posting_days(cfg), posting_hour(cfg)
    slot = after.replace(hour=hour, minute=0, second=0, microsecond=0)
    while slot <= after or slot.weekday() not in days:
        slot += timedelta(days=1)
    return slot


def slots_text(cfg: dict) -> str:
    days = [DAY_NAMES[day].capitalize() for day in posting_days(cfg)]
    names = "every day" if len(days) == 7 else (", ".join(days[:-1]) + " and " + days[-1] if len(days) > 1 else days[0])
    return f"{names} at {posting_hour(cfg):02d}:00"


def in_order(cfg: dict, episodes: dict) -> list[str]:
    """Episode ids in the configured order; the ones the order does not name come after, by name."""
    order = [str(item) for item in cfg.get("order") or []]
    return sorted(episodes, key=lambda episode_id: (order.index(episode_id) if episode_id in order else len(order), episode_id))


# ------------------------------------------------------------------ step 1: take in finished videos

def _allowed(source: Path, cfg: dict) -> set[str] | None:
    """Episode ids the maker's own status file calls finished and free of problems; None when no such file is set."""
    if not cfg.get("status_file"):
        return None
    status = _read_json(source / str(cfg["status_file"]))
    rows = status.get("episodes") if isinstance(status, dict) else None
    return {
        str(row.get("id")) for row in rows or []
        if isinstance(row, dict) and row.get("finished") is True and not row.get("problems")
    }


def sync(channel_id: str, cfg: dict, state: dict, now: datetime) -> int:
    """Copy finished videos that are new, or changed while still waiting, into the channel's own folder."""
    source = Path(cfg["source"])
    allowed = _allowed(source, cfg)
    taken = 0
    for post_file in sorted(source.glob("*_post.json")) if source.is_dir() else []:
        post = _read_json(post_file)
        if not isinstance(post, dict) or not EPISODE.match(str(post.get("id") or "")):
            continue
        episode_id = str(post["id"])
        video = source / Path(str(post.get("video") or "")).name
        if (allowed is not None and episode_id not in allowed) or video.suffix.lower() != ".mp4" or not video.is_file():
            continue
        details = video.stat()
        if not details.st_size or time.time() - details.st_mtime < SETTLED_SECONDS:
            continue
        text = x_text(post, cfg.get("tags", DEFAULT_TAGS) if isinstance(cfg.get("tags"), int) else DEFAULT_TAGS)
        if not text:
            continue
        episode = state["episodes"].get(episode_id)
        if episode and episode.get("status") != "waiting":
            continue  # scheduled, sent or given up: what went out stays what it was
        mark = f"{details.st_size}:{int(details.st_mtime)}"
        if episode and episode.get("source_mark") == mark and episode.get("text") == text:
            continue
        folder = channel_dir(channel_id)
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(video, folder / f"{episode_id}.mp4")
        state["episodes"][episode_id] = {
            **(episode or {}),
            "title": str(post.get("title") or episode_id).strip(),
            "text": text,
            "source_mark": mark,
            "added": (episode or {}).get("added") or _stamp(now),
            "status": "waiting",
        }
        taken += 1
    return taken


# ------------------------------------------------------------------ step 2: what became of the post in Buffer

def _buffer(creds: dict, query: str, variables: dict, what: str) -> dict:
    import channels

    return channels.buffer_graphql(creds["api_key"], query, variables, what)


def refresh(state: dict, creds: dict, now: datetime, ask=_buffer) -> list[str]:
    """Read back every post that waits in Buffer. A post I removed there pauses the channel."""
    notes = []
    for episode_id, episode in state["episodes"].items():
        if episode.get("status") != "scheduled" or not episode.get("post_id"):
            continue
        try:
            post = (ask(creds, POST_STATE, {"input": {"id": episode["post_id"]}}, "Buffer post") or {}).get("post") or {}
        except Exception as error:
            if "post not found" not in str(error).lower():
                notes.append(f"{episode_id}: Buffer did not answer ({str(error)[:80]})")
                continue
            episode.update({"status": "waiting", "post_id": "", "due": ""})
            state["paused"] = True
            state["paused_why"] = f"The post for {episode_id} was removed in Buffer."
            notes.append(f"{episode_id}: removed in Buffer, channel paused")
            continue
        status = post.get("status")
        if status == "sent":
            sent_at = _moment(post.get("sentAt"), now) or now
            episode.update({"status": "sent", "sent_at": _stamp(sent_at), "link": post.get("externalLink") or "", "error": ""})
            notes.append(f"{episode_id}: sent")
        elif status == "error":
            episode["tries"] = int(episode.get("tries") or 0) + 1
            episode["error"] = str((post.get("error") or {}).get("message") or "X refused the post")[:200]
            episode["status"] = "failed" if episode["tries"] >= MAX_TRIES else "waiting"
            episode["post_id"] = ""
            notes.append(f"{episode_id}: failed ({episode['error'][:80]})")
    return notes


# ------------------------------------------------------------------ step 3: schedule the next one

def next_episode(cfg: dict, state: dict) -> str | None:
    return next((episode_id for episode_id in in_order(cfg, state["episodes"])
                 if state["episodes"][episode_id].get("status") == "waiting"), None)


def next_time(cfg: dict, now: datetime) -> datetime:
    """The next posting time that still leaves Buffer the lead time."""
    return next_slot(now + timedelta(minutes=LEAD_MINUTES), cfg)


def _public_video(channel_id: str, episode_id: str, video: Path) -> str:
    """A public address for the video on the Pages site. The name carries the file's fingerprint, so a video
    that was made again never goes out as its older version."""
    import trylinks

    config = _read_json(CONFIG_FILE) or {}
    user = str(config.get("github_user") or "")
    if not user:
        return ""
    fingerprint = hashlib.sha1(video.read_bytes()).hexdigest()[:8]
    return trylinks.host_demo_video(user, f"{channel_id}-{episode_id.lower()}-{fingerprint}", video, PAGES_CHECKOUT, logger)


def _already_waiting(creds: dict, text: str, ask) -> dict | None:
    """The post with this text that already waits in Buffer, if a run before this one created it and then lost
    the answer. Also says so when the account needs me (disconnected)."""
    channel = (ask(creds, CHANNEL_STATE, {"input": {"id": creds["channel_id"]}}, "Buffer channel") or {}).get("channel") or {}
    if channel.get("isDisconnected"):
        raise RuntimeError("X is disconnected in Buffer: reconnect it at https://publish.buffer.com/channels")
    waiting = ask(creds, WAITING_POSTS, {"input": {
        "organizationId": channel.get("organizationId"),
        "filter": {"channelIds": [creds["channel_id"]], "status": ["scheduled"]},
    }}, "Buffer queue") or {}
    for edge in (waiting.get("posts") or {}).get("edges") or []:
        node = edge.get("node") or {}
        if str(node.get("text") or "").strip() == text.strip():
            return node
    return None


def schedule(channel_id: str, cfg: dict, state: dict, creds: dict, now: datetime, ask=_buffer, host=_public_video) -> str:
    """Put the next waiting video into Buffer for the next posting time. One post at a time."""
    if state.get("paused"):
        return "paused"
    held = next((episode_id for episode_id, episode in state["episodes"].items() if episode.get("status") == "scheduled"), None)
    if held:
        return f"{held} waits in Buffer for {state['episodes'][held].get('due', '?')}"
    episode_id = next_episode(cfg, state)
    if not episode_id:
        return "no finished video is waiting"
    episode = state["episodes"][episode_id]
    due = next_time(cfg, now)
    try:
        post = _already_waiting(creds, episode["text"], ask)
        if post is None:
            url = host(channel_id, episode_id, channel_dir(channel_id) / f"{episode_id}.mp4")
            if not url:
                episode["error"] = "The video has no public address yet; the next run tries again."
                return f"{episode_id}: no public address for the video yet"
            created = (ask(creds, SCHEDULE_POST, {"input": {
                "text": episode["text"],
                "channelId": creds["channel_id"],
                "schedulingType": "automatic",
                "mode": "customScheduled",
                "dueAt": due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                "assets": [{"video": {"url": url}}],
            }}, "Schedule via Buffer") or {}).get("createPost") or {}
            post = created.get("post")
            if not post:
                raise RuntimeError(f"Buffer did not take the post: {created.get('message') or created}")
            episode["video_url"] = url
    except Exception as error:
        episode["error"] = str(error)[:200]
        return f"{episode_id}: FAILED {str(error)[:160]}"
    due = _moment(post.get("dueAt"), now) or due
    episode.update({"status": "scheduled", "post_id": post["id"], "due": _stamp(due), "error": ""})
    return f"{episode_id}: scheduled in Buffer for {_stamp(due)}"


# ------------------------------------------------------------------ one run, the dashboard card and its buttons

def run(now: datetime | None = None) -> str:
    """One pass over every configured channel. Only one pass works at a time on this PC."""
    with _only_one("run", folder=DATA_DIR) as mine:
        if not mine:
            return "another run is still working; nothing done"
        now = now or now_local()
        creds = x_account()
        outcomes = []
        for channel_id, cfg in load_config().items():
            state = load_state(channel_id)
            try:
                taken = sync(channel_id, cfg, state, now)
                notes = [f"{taken} new video{'' if taken == 1 else 's'}"] if taken else []
                if creds:
                    notes += refresh(state, creds, now)
                    notes.append(schedule(channel_id, cfg, state, creds, now))
                else:
                    notes.append("X is not connected through Buffer")
            except Exception as error:  # one broken channel must not stop the others
                notes = [f"FAILED {type(error).__name__}: {str(error)[:160]}"]
            state["last_run"] = _stamp(now)
            save_state(channel_id, state)
            outcomes.append(f"{channel_id}: " + "; ".join(notes))
        return " | ".join(outcomes) or "nothing configured"


def card(now: datetime | None = None) -> list[dict]:
    """One row per channel for the dashboard, read from disk only: no network, so the page stays quick."""
    now = now or now_local()
    creds = x_account()
    rows = []
    for channel_id, cfg in load_config().items():
        state = load_state(channel_id)
        episodes = state["episodes"]
        order = in_order(cfg, episodes)
        sent = [episode_id for episode_id in order if episodes[episode_id].get("status") == "sent"]
        failed = [episode_id for episode_id in order if episodes[episode_id].get("status") == "failed"]
        held = next((episode_id for episode_id in order if episodes[episode_id].get("status") == "scheduled"), None)
        waiting = [episode_id for episode_id in order if episodes[episode_id].get("status") == "waiting"]
        upcoming = None
        if held:
            upcoming = {"episode": held, "title": episodes[held].get("title", ""), "due": episodes[held].get("due", ""),
                        "state": "scheduled"}
        elif waiting and not state.get("paused"):
            upcoming = {"episode": waiting[0], "title": episodes[waiting[0]].get("title", ""),
                        "due": _stamp(next_time(cfg, now)), "state": "planned"}
        # The day the last finished video goes out: one posting time for each video after the next one.
        last_due = _moment(upcoming["due"], now) if upcoming else None
        if last_due:
            for _ in range(len(waiting) - (0 if held else 1)):
                last_due = next_slot(last_due, cfg)
        target = cfg.get("target") if isinstance(cfg.get("target"), int) and cfg.get("target") > 0 else len(episodes)
        trouble = next((episodes[episode_id]["error"] for episode_id in waiting[:1] if episodes[episode_id].get("error")), "")
        rows.append({
            "id": channel_id,
            "name": str(cfg.get("name") or channel_id),
            "account": "@" + str(creds.get("username", "")).lstrip("@") if creds and creds.get("username") else "",
            "connected": bool(creds),
            "paused": bool(state.get("paused")),
            "paused_why": str(state.get("paused_why") or "") if state.get("paused") else "",
            "finished": len(episodes),
            "target": target,
            "sent": len(sent),
            "left": len(waiting) + (1 if held else 0),
            "next": upcoming,
            "covered_until": _stamp(last_due)[:10] if last_due else "",
            "posts": [{"episode": episode_id, "title": episodes[episode_id].get("title", ""),
                       "sent_at": episodes[episode_id].get("sent_at", ""), "link": episodes[episode_id].get("link", "")}
                      for episode_id in sorted(sent, key=lambda item: episodes[item].get("sent_at", ""), reverse=True)[:SHOWN_POSTS]],
            "failed": [{"episode": episode_id, "title": episodes[episode_id].get("title", ""),
                        "error": episodes[episode_id].get("error", "")} for episode_id in failed],
            "trouble": trouble,
            "slots": slots_text(cfg),
            "not_connected": [str(item) for item in cfg.get("not_connected") or [] if isinstance(item, str)][:6],
            "last_run": state.get("last_run", ""),
        })
    return rows


def action(name: str, ask=_buffer) -> str:
    """Pause or resume one channel. Anything that is not exactly shorts:<pause|resume>:<configured channel> is refused."""
    match = ACTION.match(name if isinstance(name, str) else "")
    if not match or match.group(2) not in load_config():
        raise ValueError(f"Unknown action: {name}")
    verb, channel_id = match.groups()
    with _only_one("run", wait=20, folder=DATA_DIR) as mine:
        if not mine:
            return "A run is working right now. Try again in a minute."
        state = load_state(channel_id)
        if verb == "resume":
            state["paused"] = False
            state.pop("paused_why", None)
            save_state(channel_id, state)
            return "Resumed. The next video is scheduled on the next run, within the hour."
        state["paused"] = True
        state["paused_why"] = "Paused in the dashboard."
        message = "Paused. Nothing waits in Buffer."
        creds = x_account()
        for episode_id, episode in state["episodes"].items():
            if episode.get("status") != "scheduled":
                continue
            try:
                removed = (ask(creds, DELETE_POST, {"input": {"id": episode["post_id"]}}, "Remove from Buffer") or {}).get("deletePost") or {}
                if not removed.get("id"):
                    raise RuntimeError(removed.get("message") or "Buffer did not confirm")
                episode.update({"status": "waiting", "post_id": "", "due": ""})
                message = f"Paused. The post for {episode_id} was removed from Buffer."
            except Exception as error:
                message = (f"Paused, but the post for {episode_id} is still in Buffer ({str(error)[:80]}). "
                           "Delete it at https://publish.buffer.com if it must not go out.")
        save_state(channel_id, state)
        return message


# ------------------------------------------------------------------ scheduled task and command line

def install_task() -> None:
    """Every hour, hidden (pythonw), catching up if the PC was off. Same settings as the other hourly tasks."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    script = f"""
$action = New-ScheduledTaskAction -Execute '{runner}' -Argument '"{Path(__file__).resolve()}"' -WorkingDirectory '{CODE_DIR}'
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -Priority 4 -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' -Action $action -Trigger $hourly -Settings $settings `
    -Description 'Short videos: take in finished ones and keep the next one scheduled for X in Buffer.' -Force | Out-Null
Get-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' | Select-Object TaskName, State
"""
    print(subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True).stdout.strip())


def show() -> None:
    for row in card():
        print(f"== {row['name']} on X {row['account']}{' (paused)' if row['paused'] else ''}")
        print(f"   {row['finished']} of {row['target']} videos finished, {row['sent']} sent, {row['left']} waiting; {row['slots']}")
        if row["next"]:
            print(f"   next: {row['next']['episode']} {row['next']['due']} ({row['next']['state']}) {row['next']['title']}")
        for post in row["posts"]:
            print(f"   sent {post['sent_at']} {post['episode']} {post['link']}")
        for post in row["failed"]:
            print(f"   failed {post['episode']}: {post['error']}")


if __name__ == "__main__":
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
