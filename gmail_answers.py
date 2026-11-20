"""Gmail answers: what Titip's Autopilot did with new Gmail, shown in ReachPilot, with its buttons.

Usage:
    python gmail_answers.py            print what the card shows (no mail text)

ReachPilot answers no mail itself. Titip (https://github.com/juw12/titip) runs on this PC, reads new
Gmail in Chrome every 10 minutes, answers what is safe to answer, keeps the rest with a draft and
leaves machines alone. Its limits are fixed in Titip's code: it answers only the sender, at most 8 a
day, and never mail about money, contracts, passwords, personal papers or a promise.

This module reads Titip's state and passes button presses on:

    card()         what the dashboard shows. From Titip's desk address on this PC (127.0.0.1:47780);
                   when Titip is closed, from its state file (~/.titip/autopilot.json), read only.
    action(name)   gmail:send:<id>, gmail:say:<id>|<my words>, gmail:skip:<id>, gmail:clear (drop the
                   mails that could not be judged), gmail:mode:<auto|review>, gmail:switch:<on|off>,
                   gmail:check, gmail:start (start Titip when it is closed).

Nothing here goes online: the only address asked is this PC's own. Where Titip was never set up the
card is not shown.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import machine

ROOT = Path(__file__).resolve().parent
TITIP_HOME = Path.home() / ".titip"
STATE_FILE = "autopilot.json"
DESK = "http://127.0.0.1:47780"  # Titip's desk listener: this PC only, refuses any other Host
TITIP_FOLDERS = (ROOT.parent / "titip", Path.home() / "titip")  # where Titip.pyw is looked for by gmail:start
TIMEOUT_S = 3
INTERVAL_S = 10 * 60  # Titip's own time between two looks (titip/autopilot.py INTERVAL_S)
DAILY_LIMIT = 8  # Titip's own limit (MAX_AUTO_PER_DAY); only used when Titip is closed and cannot say it
SAID = 1500  # the longest answer the "Write my own" box takes
SHOWN = 12  # journal lines on the card
ACTION = re.compile(
    r"gmail:(?:(mode):(auto|review)|(switch):(on|off)|(check|clear|start)"
    r"|(send|skip):([0-9a-f]{6,32})|(say):([0-9a-f]{6,32})\|(.+))\Z",
    re.S,
)
# Reasons Titip gives when a mail was not judged at all (the model or the page failed), not kept on purpose.
NOT_JUDGED = ("The model did not answer", "The model's answer could not be read", "Could not read the sender's address")
KINDS = {"sent": "Answered", "held": "Kept for you", "left": "Left alone", "retry": "Trying again",
         "dropped": "Not answered", "error": "Problem", "switch": "You changed"}
NOT_RUNNING = "Titip is not running on this PC, so no Gmail is read or answered."

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # this PC's own address never goes through the proxy


class TitipClosed(Exception):
    """Titip's desk address did not answer."""


def _ask(path: str, order: dict | None = None, opener=None) -> dict:
    """GET (no order) or POST one order to Titip's desk listener. TitipClosed when nobody answers;
    ValueError with Titip's own sentence when it refuses the order."""
    request = urllib.request.Request(DESK + path)
    if order is not None:
        request = urllib.request.Request(
            DESK + path, data=json.dumps(order).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Titip": "1"}, method="POST")
    try:
        with (opener or _direct).open(request, timeout=TIMEOUT_S) as answer:
            found = json.loads(answer.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            said = json.loads(error.read().decode("utf-8")).get("error")
        except (ValueError, AttributeError):
            said = ""
        raise ValueError(said or f"Titip answered with HTTP {error.code}.") from error
    except (OSError, ValueError) as error:  # refused, timed out, or not JSON: not Titip
        raise TitipClosed(str(error)) from error
    if not isinstance(found, dict):
        raise TitipClosed("not Titip's answer")
    return found


def _titip(order: dict | None) -> dict:
    """Titip's snapshot: as it is (no order), or after one order."""
    return _ask("/api/auto", order)


def _from_file(home: Path, now: float) -> dict | None:
    """Titip's state file as the same snapshot its desk address gives. None when there is no usable file."""
    try:
        state = json.loads((home / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    today = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    sent = state.get("sent") if isinstance(state.get("sent"), dict) else {}
    boxes = state.get("mailboxes") if isinstance(state.get("mailboxes"), dict) else {}
    return {
        "on": bool(state.get("on")), "mode": state.get("mode") or "auto", "busy": "",
        "lastCheck": state.get("lastCheck") or 0, "nextCheck": 0,
        "mailboxes": [boxes.get(str(index), f"Gmail account {index + 1}") for index in state.get("accounts") or [0]
                      if isinstance(index, int)],
        "sentToday": sum(count for key, count in sent.items() if str(key).startswith(today) and isinstance(count, int)),
        "dailyLimit": DAILY_LIMIT,
        "held": [item for item in state.get("held") or [] if isinstance(item, dict)],
        "events": [event for event in reversed(state.get("events") or []) if isinstance(event, dict)],
    }


def _when(stamp) -> str:
    try:
        return datetime.fromtimestamp(int(stamp)).strftime("%Y-%m-%d %H:%M") if stamp else ""  # the page's own form
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def gmail_link(account, address: str, subject: str) -> str:
    """Gmail's own search for that sender and subject: opens the conversation in the browser."""
    subject = re.sub(r"^\s*((re|fwd?|aw|sv|bls|balas)\s*:\s*)+", "", subject or "", flags=re.I)
    words = re.sub(r'["(){}]', " ", subject).strip()
    query = " ".join(part for part in (f"from:({address})" if address else "", f'subject:("{words}")' if words else "") if part)
    index = account if isinstance(account, int) and 0 <= account < 10 else 0
    return f"https://mail.google.com/mail/u/{index}/#search/{urllib.parse.quote(query, safe='')}" if query else ""


def not_judged(reason: str) -> bool:
    return str(reason or "").startswith(NOT_JUDGED)


def titip_program() -> Path | None:
    return next((folder / "Titip.pyw" for folder in TITIP_FOLDERS if (folder / "Titip.pyw").is_file()), None)


def card(home: Path | None = None, ask=None, now: float | None = None) -> dict:
    """What the dashboard shows. ask(None) -> Titip's snapshot (default: its desk address)."""
    home = home or TITIP_HOME
    now = now if now is not None else datetime.now().timestamp()
    if not (home / STATE_FILE).exists():
        return {"installed": False}
    running = True
    try:
        snapshot = (ask or _titip)(None)
    except (TitipClosed, ValueError):
        running, snapshot = False, _from_file(home, now)
    if not isinstance(snapshot, dict):
        return {"installed": True, "running": False, "readable": False, "error": "Titip's state file could not be read.",
                "on": False, "mode": "review", "waiting": [], "failed": [], "recent": [], "counts": {}, "mailboxes": [],
                "can_start": titip_program() is not None}

    def waiting_row(item: dict) -> dict:
        reason = str(item.get("reason") or "")
        return {
            "id": str(item.get("id") or ""), "at": _when(item.get("at")), "mailbox": str(item.get("mailbox") or ""),
            "sender": str(item.get("sender") or ""), "address": str(item.get("address") or ""),
            "subject": str(item.get("subject") or ""), "summary": str(item.get("summary") or ""), "reason": reason,
            "draft": str(item.get("draft") or ""), "error": str(item.get("error") or ""),
            "sending": bool(item.get("sending")), "failed": not_judged(reason),
            "url": gmail_link(item.get("account"), str(item.get("address") or ""), str(item.get("subject") or "")),
        }

    held = [waiting_row(item) for item in snapshot.get("held") or [] if isinstance(item, dict) and item.get("id")]
    events = [event for event in snapshot.get("events") or [] if isinstance(event, dict)]
    counts = {kind: sum(1 for event in events if event.get("kind") == kind) for kind in ("sent", "held", "left", "retry", "error")}
    mail_events = [event for event in events if event.get("kind") != "switch"]
    newest = mail_events[0] if mail_events else {}
    on = bool(snapshot.get("on"))
    last = int(snapshot.get("lastCheck") or 0)
    return {
        "installed": True, "running": running, "readable": True,
        "error": "" if running else NOT_RUNNING,
        "can_start": titip_program() is not None,
        "version": str(snapshot.get("version") or ""),
        "on": on, "mode": "auto" if snapshot.get("mode") == "auto" else "review",
        "busy": str(snapshot.get("busy") or "") if running else "",
        "mailboxes": [str(name) for name in snapshot.get("mailboxes") or []],
        "last_check": _when(last),
        "next_check": _when(snapshot.get("nextCheck")) if running and on else "",
        # Titip looks every 10 minutes while it is on; three missed looks means it is stuck or the PC slept.
        "late": bool(running and on and last and now - last > 3 * INTERVAL_S + 300),
        "sent_today": int(snapshot.get("sentToday") or 0), "daily_limit": int(snapshot.get("dailyLimit") or DAILY_LIMIT),
        "counts": counts, "since": _when(events[-1].get("at")) if events else "",
        "waiting": [row for row in held if not row["failed"]],
        "failed": [row for row in held if row["failed"]],
        # The newest thing it did was a problem (Chrome signed out, the PC locked, ...): the To do says so.
        "problem": str(newest.get("text") or "") if newest.get("kind") == "error" else "",
        "problem_at": _when(newest.get("at")) if newest.get("kind") == "error" else "",
        "recent": [{"at": _when(event.get("at")), "kind": str(event.get("kind") or ""),
                    "label": KINDS.get(event.get("kind"), str(event.get("kind") or "")),
                    "who": str(event.get("who") or ""), "subject": str(event.get("subject") or ""),
                    "text": str(event.get("text") or "")} for event in mail_events[:SHOWN]],
        "page": DESK + "/auto.html" if running else "",
    }


def action(name: str, ask=None, start=None) -> str:
    """A button. ask(order) -> Titip's snapshot after the order, ask(None) -> as it is (default: its desk address)."""
    match = ACTION.match(name if isinstance(name, str) else "")
    if not match:
        raise ValueError(f"Unknown action: {name}")
    order = ask or _titip
    plain = match.group(5)
    if plain == "start":
        return (start or start_titip)()
    try:
        if match.group(1):
            order({"command": "mode", "mode": match.group(2)})
            return ("Safe answers now go out by themselves, at most 8 a day. Everything else still waits for you."
                    if match.group(2) == "auto" else "Nothing is sent by itself now. Every answer waits for you.")
        if match.group(3):
            order({"command": "switch", "on": match.group(4) == "on"})
            return ("Gmail answers are on: new mail is read every 10 minutes." if match.group(4) == "on"
                    else "Gmail answers are paused. Nothing is read or sent.")
        if plain == "check":
            order({"command": "check"})
            return "Titip is looking at Gmail now; the card updates in a minute or two."
        if plain == "clear":
            held = order(None).get("held") or []
            failed = [item for item in held if isinstance(item, dict) and item.get("id") and not_judged(item.get("reason"))]
            for item in failed:
                order({"command": "drop", "id": item["id"]})
            return (f"Cleared {len(failed)} mail(s) that could not be judged. They are still unread in Gmail."
                    if failed else "Nothing to clear.")
        verb, item_id = match.group(6) or match.group(8), match.group(7) or match.group(9)
        if verb == "skip":
            order({"command": "drop", "id": item_id})
            return "Left unanswered. The mail stays in Gmail."
        text = (match.group(10) or "").strip()
        if verb == "say":
            if not text:
                raise ValueError("The answer is empty. Nothing was sent.")
            if len(text) > SAID:
                raise ValueError(f"That answer has {len(text)} characters; {SAID} is the most here. Nothing was sent.")
            order({"command": "send", "id": item_id, "text": text})
        else:
            order({"command": "send", "id": item_id})  # Titip sends its own draft for that mail
        return "Titip is sending it from Gmail now. It shows under What it did when it has left."
    except TitipClosed as error:
        raise ValueError(NOT_RUNNING + " Start Titip, then try again.") from error


def start_titip() -> str:
    try:
        _titip(None)
        return "Titip is already running."
    except ValueError:  # something answered on Titip's port
        return "Titip is already running."
    except TitipClosed:
        pass
    program = titip_program()
    if program is None:
        raise ValueError("Titip.pyw was not found next to ReachPilot. Start Titip from its own folder.")
    runner = Path(sys.executable).with_name("pythonw.exe")
    runner = runner if runner.exists() else Path(sys.executable)
    subprocess.Popen([str(runner), str(program), "--quiet"], cwd=program.parent, creationflags=machine.NO_WINDOW)
    return "Titip is starting. The card shows it in under a minute."


def show() -> None:
    """What the card shows, without any mail text (sender names, subjects and drafts stay out of the terminal)."""
    found = card()
    if not found.get("installed"):
        print("Titip was never set up on this PC (no ~/.titip/autopilot.json).")
        return
    print(f"running: {found['running']}  on: {found['on']}  mode: {found['mode']}  version: {found.get('version') or '?'}")
    print(f"mailboxes: {', '.join(found['mailboxes']) or '-'}")
    print(f"last look: {found.get('last_check') or '-'}  next: {found.get('next_check') or '-'}")
    print(f"answered today: {found.get('sent_today', 0)} of {found.get('daily_limit', DAILY_LIMIT)}")
    print(f"waiting for you: {len(found['waiting'])}  could not be judged: {len(found['failed'])}")
    print("journal: " + ", ".join(f"{KINDS.get(kind, kind)} {count}" for kind, count in found["counts"].items()))
    if found.get("problem"):
        print(f"problem: {found['problem']}")


if __name__ == "__main__":
    show()
