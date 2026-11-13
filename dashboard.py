"""ReachPilot dashboard: LinkedIn posting, job search, e-mail applications (and later promotion) in one window.

Run:  python autopilot.py dashboard
Opens http://127.0.0.1:8770 (or the next free port) in an app window. It reads every module's data; the only things it
changes are the whitelisted actions in hub.run_action (start a run, open the apply session, pause posting), and those
need the per-session token embedded in the page.
The server stops by itself 15 minutes after the window is closed.
"""
from __future__ import annotations

import json
import mimetypes
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import analytics
import channels
import growth
import hub
import selfupdate
import autopilot as ap
import trylinks

PORTS = range(8770, 8790)
PING_REPLY = b"reachpilot-dashboard"
IDLE_SHUTDOWN_SECONDS = 15 * 60
PAGE = Path(__file__).with_name("dashboard.html")
ICONS = Path(__file__).with_name("icons")
WINDOW_TITLE = "ReachPilot"  # must match <title> in dashboard.html
APP_USER_MODEL_ID = "juw12.ReachPilot.Dashboard"
SESSION_TOKEN = secrets.token_urlsafe(24)
BOOT_ID = secrets.token_hex(6)  # changes on every (re)start; the page reloads itself when it sees a new one
CODE_DIR = Path(__file__).parent
CODE_CHECK_SECONDS = 5
UPDATE_CHECK_SECONDS = 30 * 60  # how often the app looks for a newer version on GitHub
NO_WINDOW_ENV = "REACHPILOT_NO_WINDOW"  # set when the dashboard restarts itself: reuse the open window
ICON_FILES = {  # url path -> (file in icons/, content type); one mark for favicon, app window and taskbar
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/favicon.ico": ("app.ico", "image/x-icon"),
}
MANIFEST = json.dumps({
    "name": "ReachPilot",
    "short_name": "ReachPilot",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#f3f2ef",
    "theme_color": "#0a66c2",
    "icons": [
        {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}).encode("utf-8")
IMAGE_ROOTS = (ap.ASSETS.resolve(), ap.GENERATED.resolve())
AUTO_SLOTS_SHOWN = 3


# ------------------------------------------------------------------ helpers

def image_url(path: str) -> str:
    return "/img?p=" + quote(str(path))


def week_start(moment: datetime) -> str:
    return (moment - timedelta(days=moment.weekday())).strftime("%Y-%m-%d")


def first_line(text: str | None) -> str:
    return (text or "").strip().split("\n")[0]


def title_for(repo: str | None, angle: str) -> str:
    if repo:
        return repo
    return {"intro": "About me", "roundup": "Roundup"}.get(angle, "To be decided")


def upcoming_slot_times(config: dict, now: datetime, count: int) -> list[datetime]:
    """Next slot start times, including a slot that is still inside its catch-up window."""
    times: list[datetime] = []
    day = (now - timedelta(hours=config["catch_up_hours"])).date()
    while len(times) < count:
        for slot in config["slots"]:
            if ap.WEEKDAYS[day.weekday()] != slot["weekday"]:
                continue
            hour, minute = map(int, slot["time"].split(":"))
            start = datetime(day.year, day.month, day.day, hour, minute)
            if start + timedelta(hours=config["catch_up_hours"]) > now:
                times.append(start)
        day += timedelta(days=1)
    return sorted(times)[:count]


# ------------------------------------------------------------------ data

def forecast(db, config: dict, now: datetime) -> list[dict]:
    """Mirror of the picker's order: drafted slot > new GitHub event > prepared post due > picked on the day."""
    events = [row[0] for row in db.execute("SELECT repo FROM events WHERE consumed=0 ORDER BY id")]
    seeds = [
        dict(zip(("id", "slot_date", "repo", "angle", "body", "comment", "images"), row))
        for row in db.execute(
            "SELECT id, slot_date, repo, angle, body, comment, images FROM queue WHERE used=0 ORDER BY slot_date, id"
        )
    ]
    drafted = {
        row[0]: dict(zip(("slot", "status", "repo", "angle", "body", "comment", "images"), row))
        for row in db.execute("SELECT slot, status, repo, angle, body, comment, images FROM posts")
    }
    per_week: dict[str, int] = {}
    for (posted_at,) in db.execute("SELECT posted_at FROM posts WHERE status='posted'"):
        key = week_start(datetime.strptime(posted_at[:10], "%Y-%m-%d"))
        per_week[key] = per_week.get(key, 0) + 1

    plan: list[dict] = []
    auto_left = AUTO_SLOTS_SHOWN
    for start in upcoming_slot_times(config, now, 60):
        key = start.strftime("%Y-%m-%d %H:%M")
        row = drafted.get(key)
        if row and row["status"] in ("posted", "skipped"):
            continue
        entry = {"slot": key, "weekday": ap.WEEKDAYS[start.weekday()], "images": [], "body": "", "comment": ""}
        week = week_start(start)
        if per_week.get(week, 0) >= config["max_posts_per_week"]:
            plan.append({**entry, "kind": "cap", "title": "Skipped", "angle": "-", "repo": None,
                         "hook": "Weekly limit reached, so this slot stays empty."})
            continue
        per_week[week] = per_week.get(week, 0) + 1

        if row:
            images = json.loads(row["images"] or "[]")
            plan.append({**entry, "kind": "drafted", "status": row["status"], "repo": row["repo"], "angle": row["angle"],
                         "title": title_for(row["repo"], row["angle"]), "hook": first_line(row["body"]),
                         "body": row["body"] or "", "comment": row["comment"] or "",
                         "images": [image_url(path) for path in images]})
        elif events:
            repo = events.pop(0)
            plan.append({**entry, "kind": "event", "repo": repo, "angle": "launch", "title": repo,
                         "hook": "New on GitHub. The post is written at 08:00 from the README."})
        else:
            due = [seed for seed in seeds if seed["slot_date"] <= start.strftime("%Y-%m-%d")]
            if due:
                seed = due[0]
                seeds.remove(seed)
                plan.append({**entry, "kind": "prepared", "repo": seed["repo"], "angle": seed["angle"],
                             "title": title_for(seed["repo"], seed["angle"]), "hook": first_line(seed["body"]),
                             "body": seed["body"], "comment": seed["comment"],
                             "images": [image_url(ap.ASSETS / name) for name in json.loads(seed["images"])]})
            else:
                plan.append({**entry, "kind": "auto", "repo": None, "angle": "auto", "title": "Picked on the day",
                             "hook": "Something new on GitHub if there is one, otherwise a fresh take on a "
                                     "project that is doing well, or a roundup."})
                auto_left -= 1
        if auto_left == 0:
            break
    return plan


def history(db) -> list[dict]:
    metrics = {row[0]: row[1:] for row in db.execute("SELECT post_date, impressions, engagements FROM metrics")}
    elsewhere: dict[str, list[dict]] = {}
    for slot, channel, status, url, error in db.execute(
        "SELECT slot, channel, status, url, error FROM channel_posts ORDER BY channel"
    ):
        elsewhere.setdefault(slot, []).append({
            "platform": channels.LABELS.get(channel, channel), "status": status, "url": url, "error": error,
        })
    github = {(record["repo"], record["date"]): record for record in analytics.github_performance(db)}
    posts = []
    for slot, status, repo, angle, body, url, posted_at, error, attempts in db.execute(
        "SELECT slot, status, repo, angle, body, url, posted_at, error, attempts FROM posts ORDER BY slot DESC"
    ):
        day = (posted_at or slot)[:10]
        impressions, engagements = metrics.get(day, (None, None))
        result = github.get((repo, day), {})
        posts.append({
            "slot": slot, "status": status, "repo": repo, "angle": angle, "title": title_for(repo, angle or ""),
            "hook": first_line(body), "body": body or "", "url": url, "posted_at": posted_at, "error": error,
            "attempts": attempts, "impressions": impressions, "engagements": engagements,
            "visitors": result.get("visitors"), "stars": result.get("stars"), "try_clicks": result.get("try_clicks"),
            "elsewhere": elsewhere.get(slot, []),
        })
    return posts


PLATFORM_ACCOUNTS = {
    "mastodon": "Mastodon", "bluesky": "Bluesky", "devto": "Dev.to", "telegram": "Telegram",
    "discord": "Discord", "x": "X", "threads": "Threads", "substack": "Substack", "github": "GitHub",
}


def platforms(db, now: datetime) -> dict:
    """Followers, posts and GitHub visitors per platform, plus what growth.py changed on the accounts."""
    growth.ensure_schema(db)
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    latest_day = db.execute("SELECT MAX(day) FROM audience").fetchone()[0]
    latest = dict(db.execute("SELECT channel, followers FROM audience WHERE day=?", (latest_day,)).fetchall())
    before = dict(db.execute(
        "SELECT channel, followers FROM audience WHERE day=(SELECT MIN(day) FROM audience WHERE day>=?)", (week_ago,)
    ).fetchall())
    posts = dict(db.execute(
        "SELECT channel, COUNT(*) FROM channel_posts WHERE status='posted' GROUP BY channel"
    ).fetchall())
    referrer_day = db.execute("SELECT MAX(day) FROM github_referrers").fetchone()[0]
    visitors = dict(db.execute(
        "SELECT platform, uniques FROM github_referrers WHERE day=?", (referrer_day,)
    ).fetchall())
    rows = [{
        "platform": "LinkedIn", "followers": None, "change": None,
        "posts": db.execute("SELECT COUNT(*) FROM posts WHERE status='posted'").fetchone()[0],
        "visitors": visitors.get("LinkedIn"),
    }]
    for key, label in PLATFORM_ACCOUNTS.items():
        if key not in latest and key not in posts:
            continue
        rows.append({
            "platform": label, "followers": latest.get(key),
            "change": latest[key] - before[key] if key in latest and key in before else None,
            "posts": posts.get(key, 0) if key != "github" else None, "visitors": visitors.get(label),
        })
    actions = [
        {"at": at, "platform": PLATFORM_ACCOUNTS.get(channel, channel), "action": action, "detail": detail}
        for at, channel, action, detail in db.execute(
            "SELECT at, channel, action, detail FROM growth_log ORDER BY at DESC LIMIT 20"
        )
    ]
    return {"rows": rows, "actions": actions, "counted_on": latest_day}


def github_section(db, now: datetime) -> dict:
    latest_day = db.execute("SELECT MAX(day) FROM github_signals").fetchone()[0]
    since = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    clicks_30d = dict(db.execute(
        "SELECT repo, SUM(clicks) FROM try_clicks WHERE day>=? GROUP BY repo", (since,)
    ).fetchall())
    last_posted = dict(db.execute(
        "SELECT repo, MAX(posted_at) FROM posts WHERE status='posted' AND repo IS NOT NULL GROUP BY repo"
    ).fetchall())
    post_counts = dict(db.execute(
        "SELECT repo, COUNT(*) FROM posts WHERE status='posted' AND repo IS NOT NULL GROUP BY repo"
    ).fetchall())
    repos = []
    if latest_day:
        for repo, views, uniques, stars in db.execute(
            "SELECT repo, li_views, li_uniques, stars FROM github_signals WHERE day=? ORDER BY stars DESC, repo",
            (latest_day,),
        ):
            repos.append({"repo": repo, "stars": stars, "li_uniques": uniques, "li_views": views,
                          "clicks_30d": clicks_30d.get(repo, 0), "last_posted": last_posted.get(repo),
                          "posts": post_counts.get(repo, 0)})
    daily = db.execute(
        "SELECT day, SUM(stars), SUM(li_uniques) FROM github_signals GROUP BY day ORDER BY day"
    ).fetchall()
    clicks_daily = db.execute("SELECT day, SUM(clicks) FROM try_clicks GROUP BY day ORDER BY day").fetchall()
    return {
        "latest_day": latest_day,
        "repos": repos,
        "stars_series": [{"x": day, "y": stars or 0} for day, stars, _ in daily],
        "visitors_series": [{"x": day, "y": uniques or 0} for day, _, uniques in daily],
        "clicks_series": [{"x": day, "y": clicks or 0} for day, clicks in clicks_daily],
        "clicks_30d_total": sum(clicks_30d.values()),
    }


def learning(db) -> dict:
    records = analytics.performance(db)
    result = {"records": len(records)}
    for key in ("repo", "angle"):
        scorer = analytics.ThompsonScorer(records, key)
        counts: dict[str, int] = {}
        for record in records:
            if record.get(key) is not None:
                counts[record[key]] = counts.get(record[key], 0) + 1
        result[key] = [{"name": name, "score": round(score * 100, 1), "posts": counts.get(name, 0)}
                       for name, score in scorer.ranked()]
    return result


def overview(db, config: dict, now: datetime, plan: list[dict]) -> dict:
    token = ap.load_token()
    days_left = round((token["expires_at"] - time.time()) / 86400) if token else None
    last_tick = ap.kv_get(db, "last_tick")
    next_post = next((entry for entry in plan if entry["kind"] != "cap"), None)
    totals = db.execute(
        "SELECT COALESCE(SUM(impressions), 0), COALESCE(SUM(engagements), 0), COUNT(*) FROM metrics"
    ).fetchone()
    return {
        "now": now.strftime("%Y-%m-%d %H:%M"),
        "mode": config["mode"],
        "paused": ap.PAUSE_FLAG.exists(),
        "token_days_left": days_left,
        "click_tracking": trylinks.load_goatcounter(config, ap.SECRETS_PATH) is not None,
        "posts_this_week": ap.posts_this_week(db, now),
        "max_per_week": config["max_posts_per_week"],
        "posted_total": db.execute("SELECT COUNT(*) FROM posts WHERE status='posted'").fetchone()[0],
        "failed_total": db.execute("SELECT COUNT(*) FROM posts WHERE status='failed'").fetchone()[0],
        "prepared_left": db.execute("SELECT COUNT(*) FROM queue WHERE used=0").fetchone()[0],
        "next_post": next_post,
        "last_tick": last_tick,
        "impressions": totals[0],
        "engagements": totals[1],
        "posts_with_linkedin_stats": totals[2],
        "slots": [f'{slot["weekday"]} {slot["time"]}' for slot in config["slots"]],
        "awaiting": db.execute("SELECT COUNT(*) FROM posts WHERE status='awaiting_approval'").fetchone()[0],
        "inbox": str(ap.INBOX),
    }


def log_tail(lines: int = 60) -> list[str]:
    path = ap.ROOT / "autopilot.log"
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:][::-1]


def build_data() -> dict:
    config = ap.load_config()
    now = ap.local_now(config).replace(tzinfo=None)
    db = ap.connect_db()
    try:
        plan = forecast(db, config, now)
        summary = overview(db, config, now, plan)
        return {
            "overview": summary,
            "hub": hub.build(summary),
            "schedule": plan,
            "history": history(db),
            "platforms": platforms(db, now),
            "github": github_section(db, now),
            "learning": learning(db),
            "log": log_tail(),
        }
    finally:
        db.close()


# ------------------------------------------------------------------ server

class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.server.last_request = time.time()
        url = urlparse(self.path)
        if url.path == "/":
            page = PAGE.read_text(encoding="utf-8").replace("__RP_TOKEN__", SESSION_TOKEN)
            self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        elif url.path == "/ping":
            self.send(200, PING_REPLY, "text/plain")
        elif url.path == "/version":
            version = {"boot": BOOT_ID, "page": int(PAGE.stat().st_mtime)}
            self.send(200, json.dumps(version).encode("utf-8"), "application/json")
        elif url.path == "/api/data":
            try:
                self.send(200, json.dumps(build_data()).encode("utf-8"), "application/json")
            except Exception as error:
                self.send(500, json.dumps({"error": str(error)}).encode("utf-8"), "application/json")
        elif url.path == "/img":
            self.send_image(parse_qs(url.query).get("p", [""])[0])
        elif url.path in ICON_FILES:
            name, content_type = ICON_FILES[url.path]
            self.send(200, (ICONS / name).read_bytes(), content_type)
        elif url.path == "/manifest.webmanifest":
            self.send(200, MANIFEST, "application/manifest+json")
        else:
            self.send(404, b"Not found", "text/plain")

    def do_POST(self) -> None:
        """Whitelisted actions only, and only from the page this server served (session token + same origin)."""
        self.server.last_request = time.time()
        origin = self.headers.get("Origin", "")
        same_origin = origin == "" or origin.startswith(f"http://127.0.0.1:{self.server.server_port}")
        if urlparse(self.path).path != "/api/action" or self.headers.get("X-ReachPilot-Token") != SESSION_TOKEN or not same_origin:
            self.send(403, b"Forbidden", "text/plain")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            name = json.loads(self.rfile.read(length) or b"{}").get("name", "")
            message = hub.run_action(name)
            self.send(200, json.dumps({"ok": True, "message": message}).encode("utf-8"), "application/json")
        except Exception as error:
            self.send(400, json.dumps({"ok": False, "message": str(error)}).encode("utf-8"), "application/json")

    def send_image(self, raw_path: str) -> None:
        path = Path(raw_path).resolve()
        allowed = any(path.is_relative_to(root) for root in IMAGE_ROOTS)
        if not allowed or not path.is_file():
            self.send(404, b"Not found", "text/plain")
            return
        self.send(200, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    def send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def give_window_own_taskbar_button(timeout: float = 20.0) -> bool:
    """Edge groups --app windows under its own taskbar icon. Giving the window its own AppUserModelID splits it
    into a separate taskbar button that shows the page icon; the relaunch properties make "Pin to taskbar"
    start the dashboard (with icons/app.ico) instead of plain Edge. Windows only; does nothing elsewhere."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("data1", wintypes.DWORD), ("data2", wintypes.WORD), ("data3", wintypes.WORD), ("data4", ctypes.c_ubyte * 8)]

    class PROPERTYKEY(ctypes.Structure):
        _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

    class PROPVARIANT(ctypes.Structure):  # only the VT_LPWSTR case is needed
        _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3), ("value", ctypes.c_wchar_p), ("pad", ctypes.c_void_p)]

    def guid(text: str) -> GUID:
        value = GUID()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p("{" + text + "}"), ctypes.byref(value))
        return value

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def check_window(hwnd, _):
        title = ctypes.create_unicode_buffer(256)
        class_name = ctypes.create_unicode_buffer(64)
        user32.GetWindowTextW(hwnd, title, 256)
        user32.GetClassNameW(hwnd, class_name, 64)
        if title.value == WINDOW_TITLE and class_name.value.startswith("Chrome_WidgetWin") and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    deadline = time.time() + timeout
    while not found and time.time() < deadline:
        user32.EnumWindows(check_window, 0)
        if not found:
            time.sleep(0.25)
    if not found:
        return False

    app_model = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    properties = [
        (5, APP_USER_MODEL_ID),  # System.AppUserModel.ID
        (2, f'"{pythonw if pythonw.exists() else sys.executable}" "{Path(__file__).with_name("autopilot.py")}" dashboard'),
        (3, f"{ICONS / 'app.ico'},0"),  # RelaunchIconResource
        (4, WINDOW_TITLE),  # RelaunchDisplayNameResource
    ]
    store = ctypes.c_void_p()
    shell32 = ctypes.windll.shell32
    if shell32.SHGetPropertyStoreForWindow(found[0], ctypes.byref(guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")), ctypes.byref(store)) != 0:
        return False
    vtable = ctypes.cast(ctypes.cast(store, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(vtable[6])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    try:
        # relaunch properties first: the taskbar reads them when the ID changes
        for pid, text in properties[1:] + properties[:1]:
            key = PROPERTYKEY(guid(app_model), pid)
            variant = PROPVARIANT(vt=31, value=text)  # VT_LPWSTR
            if set_value(store, ctypes.byref(key), ctypes.byref(variant)) != 0:
                return False
    finally:
        release(store)
    return True


def open_window(url: str) -> threading.Thread | None:
    """Open as a standalone app window (Edge --app), falling back to the default browser.
    Returns the thread that gives the window its own taskbar button (join it if the process is about to exit)."""
    candidates = [
        shutil.which("msedge"),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    edge = next((path for path in candidates if path and Path(path).exists()), None)
    if edge:
        subprocess.Popen([edge, f"--app={url}", "--window-size=1320,920"])
        labeller = threading.Thread(target=give_window_own_taskbar_button, daemon=True)
        labeller.start()
        return labeller
    webbrowser.open(url)
    return None


def code_snapshot() -> dict[str, float]:
    """Modification times of the dashboard's own Python files (hub.py, dashboard.py, autopilot.py, ...)."""
    return {path.name: path.stat().st_mtime for path in CODE_DIR.glob("*.py")}


def code_is_healthy() -> bool:
    """True if the edited code imports cleanly in a separate process, so a half-saved file never takes the dashboard down."""
    try:
        check = subprocess.run(
            [str(Path(sys.executable).with_name("python.exe")), "-c", "import hub, dashboard, analytics, trylinks"],
            cwd=CODE_DIR, capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return check.returncode == 0
    except Exception:
        return False


def watch_code(server: ThreadingHTTPServer) -> None:
    """Restart the dashboard in place when its code changes: wait until the files stop changing,
    check that the new code imports, then stop this server so run() can start a fresh process."""
    running = code_snapshot()
    while True:
        time.sleep(CODE_CHECK_SECONDS)
        current = code_snapshot()
        if current == running:
            continue
        time.sleep(CODE_CHECK_SECONDS)
        if code_snapshot() != current:
            continue  # still being edited
        if not code_is_healthy():
            running = current  # broken edit: keep serving the old code, try again on the next change
            continue
        server.restart_requested = True
        server.shutdown()
        return


def restart_process() -> None:
    """Start a fresh dashboard (new code) that reuses the open window; the page notices the new BOOT_ID and reloads."""
    environment = dict(__import__("os").environ, **{NO_WINDOW_ENV: "1"})
    subprocess.Popen(
        [sys.executable, str(CODE_DIR / "autopilot.py"), "dashboard"],
        cwd=CODE_DIR, env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )



def update_from_github() -> None:
    """Keep the app on the newest version; watch_code() then restarts it and the page reloads."""
    time.sleep(60)
    while True:
        selfupdate.log(selfupdate.pull_latest_version())
        time.sleep(UPDATE_CHECK_SECONDS)


def run() -> None:
    """Reuse a dashboard that is already running, otherwise start one on the first free port."""
    server = None
    for port in PORTS:
        url = f"http://127.0.0.1:{port}/"
        try:
            with urllib.request.urlopen(url + "ping", timeout=2) as reply:
                if reply.read() == PING_REPLY:
                    labeller = open_window(url)
                    if labeller:
                        labeller.join()
                    return
        except Exception:
            pass  # nothing of ours here
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue  # port taken by another program
    if server is None:
        raise SystemExit("No free port for the dashboard (8770-8789).")
    server.last_request = time.time()

    def stop_when_idle() -> None:
        while True:
            time.sleep(30)
            if time.time() - server.last_request > IDLE_SHUTDOWN_SECONDS:
                server.shutdown()
                return

    server.restart_requested = False
    threading.Thread(target=stop_when_idle, daemon=True).start()
    threading.Thread(target=watch_code, args=(server,), daemon=True).start()
    threading.Thread(target=update_from_github, daemon=True).start()
    if not __import__("os").environ.pop(NO_WINDOW_ENV, None):
        open_window(url)
    server.serve_forever()
    server.server_close()
    if server.restart_requested:
        restart_process()
