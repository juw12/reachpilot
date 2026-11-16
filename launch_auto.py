"""Launch automations: the steps of a launch test that this PC can do without me.

Three jobs per launch test, all configured in launch_private.json (git-ignored) under "<test id>": {"auto": {...}}:

  reply     watch for the answer to one mail that was already sent. With a mail relay that can read the inbox
            (version 3 or newer) it saves the answer, and sends ONE follow-up when the due date passes without one.
            With an older relay it cannot read anything, so it never sends on its own: it asks me in the dashboard.
  invites   invite the people in a private contacts.csv, never more than the target. E-mail invitations go out only
            after I approved a preview that was mailed to me, and not before the start time. WhatsApp contacts get a
            local page of ready-made links to tap, because WhatsApp cannot be sent from here.
  proposal  keep a document ready while it waits for feedback: run the project's own checks every day, test the
            links it cites, and let a judge score it against a fixed rubric once a week. It reports; it never
            rewrites the wording.

Everything it learns stays in data/launch_auto/ (git-ignored). The dashboard reads rows() and calls action().

    python launch_auto.py            run once (the scheduled task does this every hour)
    python launch_auto.py --force    run once and repeat today's proposal checks
    python launch_auto.py install    create the hourly task "\\ReachPilot\\Launch automations"
    python launch_auto.py show       print what the dashboard shows
"""
from __future__ import annotations

import base64
import csv
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
DATA_DIR = CODE_DIR / "data" / "launch_auto"
PRIVATE_FILE = CODE_DIR / "launch_private.json"
LOG_FILE = CODE_DIR / "launch_auto.log"
TASK_PATH = "\\ReachPilot\\"
TASK_NAME = "Launch automations"
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

READS_INBOX = 3  # first relay version that can look for an answer and send an "only if no answer" follow-up
SETTLED = ("replied", "bounced", "closed")  # nothing more is read or sent for a mail in one of these states
OFFICE_HOURS = (8, 17)  # a follow-up to an organisation goes out on a weekday inside these local hours
SEND_PAUSE_SECONDS = 4  # between two invitations
FOLLOWUP_WAIT_SECONDS = 90  # how long a second sender waits for the first before it gives up
LINK_LIMIT = 40
LINK_STRIKES = 2  # a link is reported after this many failed days in a row, not on one slow answer
SCORE_DROP = 0.5  # judge noise is about 0.3, so only a larger fall is reported
ACTION = re.compile(r"^launch:([a-z_]{2,30}):([a-z0-9][a-z0-9-]{0,39})$")
ADDRESS = re.compile(r"^[^\s@,;<>]+@[^\s@,;<>]+\.[^\s@,;<>]+$")
WEB_LINK = re.compile(r"https?://[^\s<>()\[\]\"'`]+")


# ------------------------------------------------------------------ small helpers

def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()[-199:] if LOG_FILE.exists() else []
        LOG_FILE.write_text("\n".join(lines + [line]) + "\n", encoding="utf-8")
    except OSError:
        pass


def now_local() -> datetime:
    return datetime.now().astimezone()


def _stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d %H:%M")


def _day(value) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _moment(value, like: datetime) -> datetime | None:
    """An ISO time as an aware datetime; a time without a zone is read as local time."""
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=like.tzinfo)


def _nice(day: date | None) -> str:
    return f"{day:%a} {day.day} {day:%b}" if day else "?"


def load_config() -> dict:
    """{test id: its "auto" settings} for every test that has them. {} when the file is missing or broken."""
    try:
        data = json.loads(PRIVATE_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        test_id: entry["auto"]
        for test_id, entry in data.items()
        if isinstance(entry, dict) and isinstance(entry.get("auto"), dict) and ACTION.match(f"launch:xx:{test_id}")
    }


def test_dir(test_id: str) -> Path:
    return DATA_DIR / test_id


def _path(test_id: str, value) -> Path:
    """A configured path; relative ones live in the test's private folder."""
    path = Path(str(value))
    return path if path.is_absolute() else test_dir(test_id) / path


def load_state(test_id: str) -> dict:
    try:
        state = json.loads((test_dir(test_id) / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) else {}


def save_state(test_id: str, state: dict, only: str | None = None) -> None:
    """Write the state. With only="reply" just that part is replaced, so a slow job cannot undo what a tap
    in the dashboard wrote in the meantime."""
    if only is not None:
        part = state.get(only)
        state = load_state(test_id)
        if part is not None:
            state[only] = part
    folder = test_dir(test_id)
    folder.mkdir(parents=True, exist_ok=True)
    draft = folder / "state.json.tmp"
    draft.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
    draft.replace(folder / "state.json")


@contextmanager
def _only_one(name: str, wait: float = 0.0):
    """True for the one holder of this lock on this PC, False for everyone else. The lock ends with its process,
    so a crashed run can never block the next one."""
    import msvcrt

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = open(DATA_DIR / f"{name}.lock", "a+b")
    deadline = time.monotonic() + wait
    held = False
    try:
        while not held:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                held = True
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.2)
        yield held
    finally:
        if held:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        handle.close()


def _open(path: Path) -> None:
    os.startfile(path)  # noqa: S606 - a local file the user asked to open


# ------------------------------------------------------------------ the mail relay (an Apps Script web app)

def _relay_secret(mail: dict) -> dict:
    """{"url", "secret"} from the DPAPI-protected file that the e-mail module already uses."""
    import win32crypt

    blob = Path(mail["relay_secret_path"]).read_bytes()
    return json.loads(win32crypt.CryptUnprotectData(blob, None, None, None, 0)[1].decode("utf-8"))


def _relay_version(mail: dict) -> int:
    """What the relay says it is, read with a harmless GET. 0 = no answer, or too old to answer a GET at all."""
    import requests

    try:
        answer = requests.get(_relay_secret(mail)["url"], timeout=30).json()
        return int(answer.get("version") or 0)
    except Exception:  # offline, an HTML error page from an old relay, a missing secret file: all mean "unknown"
        return 0


def _relay_post(mail: dict, payload: dict) -> dict:
    """One POST to the relay. Never raises: a failure comes back as {"ok": False, "error": ...}."""
    import requests

    try:
        relay = _relay_secret(mail)
        answer = requests.post(relay["url"], json={"secret": relay["secret"], **payload}, timeout=120).json()
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {str(error)[:160]}"}
    return answer if isinstance(answer, dict) else {"ok": False, "error": "unexpected answer"}


def _as_html(text: str) -> str:
    def linked(match: re.Match) -> str:
        url = match.group(0).rstrip(".,;:")
        return f'<a href="{html.escape(url)}">{html.escape(url)}</a>' + match.group(0)[len(url):]

    body = WEB_LINK.sub(linked, html.escape(text, quote=False))
    return '<div style="font-family:Arial,sans-serif;font-size:14px;line-height:1.5">' + body.replace("\n", "<br>") + "</div>"


def _mail(mail: dict, subject: str, text: str) -> dict:
    payload = {"subject": subject, "text": text, "html": _as_html(text), "name": str(mail.get("sender_name") or "")}
    if mail.get("reply_to"):
        payload["replyTo"] = mail["reply_to"]
    return payload


# ------------------------------------------------------------------ A. the answer to a mail, and one follow-up

def _keep_answer(test_id: str, reply: dict, answer: dict, now: datetime, status: str = "replied") -> None:
    """Save what came back: an answer ("replied") or a delivery failure for the first mail ("bounced")."""
    text = (
        f"From: {answer.get('from', '')}\nDate: {answer.get('date', '')}\nSubject: {answer.get('subject', '')}\n\n"
        f"{answer.get('body', '')}\n"
    )
    folder = test_dir(test_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "reply.txt").write_text(text, encoding="utf-8")
    reply.update({
        "status": status,
        "replied_at": str(answer.get("date") or _stamp(now))[:16],
        "replied_from": str(answer.get("from") or "")[:120],
        "found_at": _stamp(now),
    })


def _send_followup(test_id: str, cfg: dict, mail: dict, state: dict, now: datetime, only_if_no_answer: bool) -> str:
    """The one follow-up. The attempt is written down BEFORE the request, so it can never go out twice."""
    reply = state.setdefault("reply", {})
    if reply.get("followup_attempt_at"):
        return "The follow-up was already sent or tried; it is never sent twice."
    if not ADDRESS.match(str(cfg.get("to") or "")):
        return "No valid address to follow up with."
    text = _path(test_id, cfg.get("followup_text_file") or "followup.txt").read_text(encoding="utf-8-sig").strip()
    if not text:
        return "The follow-up text is empty; nothing sent."
    payload = {**_mail(mail, str(cfg.get("followup_subject") or "Re: " + str(cfg.get("subject") or "")), text), "to": cfg["to"]}
    if cfg.get("attachment"):
        attachment = _path(test_id, cfg["attachment"])
        payload["attachment"] = {
            "base64": base64.b64encode(attachment.read_bytes()).decode("ascii"),
            "mimeType": "application/pdf",
            "filename": attachment.name,
        }
    if only_if_no_answer:
        payload.update({"action": "followup", "from": cfg["to"], "since": str(cfg.get("sent_at") or "")[:10]})
    with _only_one("followup", wait=FOLLOWUP_WAIT_SECONDS) as mine:  # a tap and the hourly run may arrive together
        if not mine:
            return "Another run is sending the follow-up right now; nothing sent from here."
        on_disk = (load_state(test_id).get("reply") or {}).get("followup_attempt_at")
        if on_disk:
            reply["followup_attempt_at"] = on_disk
            return "The follow-up was already sent or tried; it is never sent twice."
        reply["followup_attempt_at"] = _stamp(now)
        save_state(test_id, state, only="reply")
        answer = _relay_post(mail, payload)
    if answer.get("ok") and answer.get("status") == "replied":
        reply.pop("followup_attempt_at", None)
        _keep_answer(test_id, reply, answer, now)
        outcome = "An answer arrived in the meantime, so no follow-up was sent."
    elif answer.get("ok") and answer.get("status") == "bounced":
        reply.pop("followup_attempt_at", None)
        _keep_answer(test_id, reply, answer, now, status="bounced")
        outcome = "The first mail came back undelivered, so no follow-up was sent."
    elif answer.get("ok"):
        reply["followup_sent_at"] = _stamp(now)
        outcome = f"Follow-up sent to {cfg['to']}."
    else:
        reply["followup_error"] = str(answer.get("error") or "no answer from the relay")[:200]
        outcome = f"The follow-up may not have gone out ({reply['followup_error']}). It is not tried again: check the Sent folder."
    save_state(test_id, state, only="reply")
    log(f"{test_id} follow-up: {outcome}")
    return outcome


def step_reply(test_id: str, cfg: dict, mail: dict, state: dict, now: datetime) -> str:
    reply = state.setdefault("reply", {})
    if reply.get("status") in SETTLED:
        return f"reply {reply['status']}"
    reply["status"] = "waiting"
    version = _relay_version(mail)
    reply["relay_checked_at"] = _stamp(now)
    if version == 0 and int(reply.get("relay_version") or 0) >= READS_INBOX:
        reply["last_error"] = "the relay did not answer"  # offline is not the same as "too old": keep what is known
        return "reply: relay not reachable"
    reply["relay_version"] = version
    if version < READS_INBOX:
        return f"reply: relay version {version} cannot read the inbox"
    answer = _relay_post(mail, {"action": "check_reply", "from": cfg.get("to"), "since": str(cfg.get("sent_at") or "")[:10]})
    if not answer.get("ok"):
        reply["last_error"] = str(answer.get("error") or "no answer")[:200]
        return "reply: inbox check failed"
    reply.pop("last_error", None)
    reply["last_check"] = _stamp(now)
    if answer.get("status") == "replied":
        _keep_answer(test_id, reply, answer, now)
        return "reply: answer found and saved"
    if answer.get("status") == "bounced":
        _keep_answer(test_id, reply, answer, now, status="bounced")
        return "reply: the mail came back undelivered"
    due, give_up = _day(cfg.get("followup_due")), _day(cfg.get("give_up"))
    today = now.date()
    if give_up and today > give_up:
        reply["status"] = "closed"
        return "reply: no answer by the last day, closed"
    office = now.weekday() < 5 and OFFICE_HOURS[0] <= now.hour < OFFICE_HOURS[1]
    if due and today >= due and office and not reply.get("followup_attempt_at"):
        return "reply: " + _send_followup(test_id, cfg, mail, state, now, only_if_no_answer=True)
    return "reply: no answer yet"


def reply_rows(test_id: str, cfg: dict, state: dict, now: datetime) -> list[dict]:
    reply = state.get("reply") or {}
    who = str(cfg.get("who") or cfg.get("to") or "them")
    due, give_up = _day(cfg.get("followup_due")), _day(cfg.get("give_up"))
    sent = _nice(_day(cfg.get("sent_at")))
    row = {"id": "auto-reply", "replaces": cfg.get("replaces"), "links": cfg.get("links") or []}
    status = reply.get("status") or "waiting"
    reads = int(reply.get("relay_version") or 0) >= READS_INBOX
    tried = reply.get("followup_attempt_at")
    followed = (
        f" Follow-up sent {reply['followup_sent_at']}." if reply.get("followup_sent_at")
        else f" Follow-up tried {tried}, result unknown ({reply.get('followup_error', 'no result')}): check the Sent folder." if tried
        else ""
    )
    if status == "replied":
        if reply.get("read_at"):
            return [{**row, "title": f"Answer from {who} read", "status": "done", "detail": f"Opened {reply['read_at']}."}]
        if reply.get("by_hand") or not (test_dir(test_id) / "reply.txt").exists():
            detail = "You marked it as arrived; it is in your inbox."
            button = {"name": f"launch:reply_read:{test_id}", "label": "I have read it"}
        else:
            detail = f"From {reply.get('replied_from', who)}, {reply.get('replied_at', '')}. Saved on this PC."
            button = {"name": f"launch:open_reply:{test_id}", "label": "Open the answer"}
        return [{**row, "title": f"{who} answered: read it and decide what to change", "status": "yours", "detail": detail, "actions": [button]}]
    if status == "bounced":
        return [{**row, "title": f"The mail to {who} came back undelivered", "status": "yours",
                 "detail": f"Returned {reply.get('replied_at', '')}; the notice is saved on this PC. No follow-up is sent to an address "
                           "that does not accept mail. Check the address, or close this step and rely on the second reader.",
                 "actions": [{"name": f"launch:open_reply:{test_id}", "label": "Open the notice"},
                             {"name": f"launch:no_reply:{test_id}", "label": "Close this step"}]}]
    if status == "closed":
        return [{**row, "title": f"No answer from {who}", "status": "done",
                 "detail": f"Mail of {sent}, {'one follow-up' if tried else 'no follow-up'}, closed after {_nice(give_up)}.{followed} The second reader is the way on."}]
    if reads:
        problem = f" Last check failed: {reply['last_error']}." if reply.get("last_error") else ""
        plan = followed or f" One follow-up goes out on {_nice(due)} (a weekday, office hours) if there is still none."
        return [{**row, "title": f"Waiting for {who}'s answer: inbox is checked every hour", "status": "auto", "due": cfg.get("give_up"),
                 "detail": f"Mail sent {sent}. Last look {reply.get('last_check', 'not yet')}: no answer.{plan}{problem}"}]
    # The relay is too old to read the inbox: nothing is sent without a tap.
    rows = []
    arrived = {"name": f"launch:replied:{test_id}", "label": "An answer came"}
    today = now.date()
    if tried:
        late = bool(give_up and today > give_up)
        rows.append({**row, "title": f"Did {who} answer the follow-up?" if late else f"Waiting for {who}'s answer after the follow-up",
                     "status": "yours", "due": cfg.get("give_up"),
                     "detail": f"Mail sent {sent}.{followed} This PC cannot read the inbox yet, so tell it what happened.",
                     "actions": [arrived, {"name": f"launch:no_reply:{test_id}", "label": "No answer came"}]})
    elif due and today >= due:
        rows.append({**row, "title": f"No answer from {who} yet? Send the follow-up", "status": "last-tap", "due": cfg.get("followup_due"),
                     "detail": f"Mail sent {sent}; {_nice(due)} has passed. Look in the inbox first. The follow-up is written and goes out once.",
                     "actions": [{"name": f"launch:followup:{test_id}", "label": "No answer: send the follow-up"}, arrived]})
    else:
        rows.append({**row, "title": f"Waiting for {who}'s answer", "status": "yours", "due": cfg.get("followup_due"),
                     "detail": f"Mail sent {sent}. This PC cannot read the inbox yet (next row), so look yourself. "
                               f"The follow-up is written and is offered here on {_nice(due)}.",
                     "actions": [arrived]})
    rows.append({"id": "auto-relay", "title": "Let this PC read the inbox: paste the new relay code (2 minutes, once)",
                 "status": "last-tap", "due": cfg.get("followup_due"),
                 "detail": f"The mail relay in your Google account answers as version {int(reply.get('relay_version') or 0)}; reading needs {READS_INBOX}. "
                           "The button copies the code and opens Apps Script: select all, paste, then Deploy > Manage deployments > Edit > New version > Deploy. "
                           "After that the answer is found and the follow-up sent without you.",
                 "actions": [{"name": f"launch:relay_code:{test_id}", "label": "Copy the code and open Apps Script"}]})
    return rows


# ------------------------------------------------------------------ B. the invitations

def _phone(value: str, country_code: str) -> str:
    digits = re.sub(r"\D", "", value)
    if value.strip().startswith("0") and not value.strip().startswith("00"):
        digits = country_code + digits[1:]
    return digits if 8 <= len(digits) <= 15 else ""


def load_contacts(path: Path, target: int, country_code: str = "62") -> dict:
    """People from the private list: one channel each (e-mail first), no doubles, never more than the target."""
    people, seen, skipped = [], set(), 0
    try:
        lines = [line for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    except OSError:
        lines = []
    for record in csv.DictReader(lines):
        fields = {str(key or "").strip().lower(): str(value or "").strip() for key, value in record.items()}
        email = fields.get("email", "").lower()
        phone = _phone(fields.get("whatsapp", ""), country_code)
        if not ADDRESS.match(email):
            email = ""
        key = email or phone
        if not key or key in seen:
            skipped += 1
            continue
        seen.add(key)
        people.append({"name": fields.get("name", "")[:60], "email": email, "phone": "" if email else phone})
    return {"people": people[:target], "skipped": skipped, "over": max(0, len(people) - target)}


def _personal(text: str, name: str) -> str:
    return text.replace("{name}", name or "").replace("  ", " ")


def _invite_digest(cfg: dict, emails: list[dict]) -> str:
    """Changes when the text or the list of addresses changes, so an approval covers exactly what was shown."""
    parts = [str(cfg.get("subject") or ""), str(cfg.get("message") or "")] + sorted(person["email"] for person in emails)
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def _invite_page(test_id: str, cfg: dict, phones: list[dict]) -> Path:
    """A local page with one ready WhatsApp link per person and message. It never leaves this PC."""
    texts = [("Invite", cfg.get("message")), ("20:00 reminder", cfg.get("reminder")), ("Day 7", cfg.get("day7"))]
    rows = []
    for person in phones:
        cells = "".join(
            f'<td><a href="https://wa.me/{person["phone"]}?text={urllib.parse.quote(_personal(str(text), person["name"]))}" '
            f'target="_blank" rel="noopener">{html.escape(label)}</a></td>'
            for label, text in texts if text
        )
        rows.append(f"<tr><td>{html.escape(person['name'] or '+' + person['phone'])}</td>{cells}</tr>")
    page = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Invitations: {html.escape(test_id)}</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; margin: 24px auto; max-width: 720px; padding: 0 16px; }}
td {{ padding: 8px 12px 8px 0; border-bottom: 1px solid #ddd; }}
a {{ display: inline-block; padding: 6px 12px; border: 1px solid #1a7f5a; border-radius: 6px; color: #1a7f5a; text-decoration: none; }}
a:visited {{ color: #777; border-color: #bbb; }}
</style></head><body>
<h1>WhatsApp invitations ({len(phones)})</h1>
<p>Each button opens WhatsApp with the text ready; you press send. Grey buttons were already opened in this browser.
Send the 20:00 reminder only to people who answered, and Day 7 only to people who tested.</p>
<table>{''.join(rows)}</table>
</body></html>
"""
    target = test_dir(test_id) / "invites.html"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(page, encoding="utf-8")
    return target


def _invite_people(test_id: str, cfg: dict) -> dict:
    target = int(cfg.get("target") or 30)
    contacts = load_contacts(_path(test_id, cfg.get("contacts_file") or "contacts.csv"), target, str(cfg.get("country_code") or "62"))
    people = contacts["people"]
    return {
        **contacts,
        "target": target,
        "emails": [person for person in people if person["email"]],
        "phones": [person for person in people if person["phone"]],
    }


def step_invites(test_id: str, cfg: dict, mail: dict, state: dict, now: datetime) -> str:
    invites = state.setdefault("invites", {})
    found = _invite_people(test_id, cfg)
    emails, phones = found["emails"], found["phones"]
    invites["counts"] = {"email": len(emails), "whatsapp": len(phones), "skipped": found["skipped"], "over": found["over"]}
    if phones:
        _invite_page(test_id, cfg, phones)
    if not emails:
        return "invites: no e-mail contacts" + (f", {len(phones)} WhatsApp links ready" if phones else "")
    digest = _invite_digest(cfg, emails)
    subject, message = str(cfg.get("subject") or "").strip(), str(cfg.get("message") or "").strip()
    if not subject or not message:
        return "invites: no subject or text configured, nothing sent"
    if invites.get("preview_hash") != digest:
        invites.pop("approved_hash", None)  # the text or the list changed: an old approval no longer counts
        listing = "\n".join(f"  {index}. {person['name'] or '(no name)'} <{person['email']}>" for index, person in enumerate(emails, 1))
        preview = (
            f"This is the invitation that ReachPilot will send for {test_id}. Nothing has been sent to anyone yet.\n\n"
            f"Subject: {subject}\n\n{_personal(message, emails[0]['name'])}\n\n"
            f"It goes to these {len(emails)} addresses, one mail each, not before {cfg.get('start')}:\n{listing}\n\n"
            "To allow it: open ReachPilot > Overview > Launch test and press \"Approve the invitations\".\n"
            "To stop it: do nothing, or change the contacts file (a changed list needs a new approval)."
        )
        # No "to": the relay then mails its own fixed owner address, so a preview cannot reach anyone else.
        answer = _relay_post(mail, _mail(mail, f"Preview, please approve: {subject}", preview))
        if not answer.get("ok"):
            invites["preview_error"] = str(answer.get("error") or "no answer")[:200]
            return "invites: preview could not be mailed"
        invites.pop("preview_error", None)
        invites.update({"preview_hash": digest, "preview_sent_at": _stamp(now)})
        return f"invites: preview for {len(emails)} addresses mailed to the owner"
    if invites.get("approved_hash") != digest:
        return "invites: waiting for approval"
    start = _moment(cfg.get("start"), now)
    if start is None or now < start:
        return "invites: approved, waiting for the start time"
    sent = invites.setdefault("sent", {})
    count = 0
    for person in emails:
        address = person["email"]
        if address in sent:
            continue  # tried once; never mail the same person twice
        if len(sent) >= found["target"]:
            break
        sent[address] = {"at": _stamp(now), "ok": None}
        save_state(test_id, state, only="invites")
        answer = _relay_post(mail, {**_mail(mail, subject, _personal(message, person["name"])), "to": address})
        sent[address]["ok"] = bool(answer.get("ok"))
        if not answer.get("ok"):
            sent[address]["error"] = str(answer.get("error") or "no answer")[:160]
        save_state(test_id, state, only="invites")
        count += 1
        time.sleep(SEND_PAUSE_SECONDS)
    if count:
        log(f"{test_id} invitations: {count} mailed this run")
    return f"invites: {count} mailed this run, {sum(1 for entry in sent.values() if entry.get('ok'))} in total"


def invite_rows(test_id: str, cfg: dict, state: dict, now: datetime) -> list[dict]:
    invites = state.get("invites") or {}
    found = _invite_people(test_id, cfg)
    emails, phones, target = found["emails"], found["phones"], found["target"]
    start = _moment(cfg.get("start"), now)
    when = f"{_nice(start.date())} {start:%H:%M}" if start else "the start"
    due = str(cfg.get("start") or "")[:10] or None
    row = {"id": "auto-invites", "replaces": cfg.get("replaces"), "links": cfg.get("links") or [], "due": due}
    contacts_file = _path(test_id, cfg.get("contacts_file") or "contacts.csv")
    edit = {"name": f"launch:open_contacts:{test_id}", "label": "Open the contact list"}
    if not emails and not phones:
        return [{**row, "title": f"Invitations: add the people to invite (up to {target})", "status": "yours",
                 "detail": f"The list is empty, and nobody is invented. One line per person in {contacts_file}: name, e-mail and/or WhatsApp number. "
                           f"E-mail invitations then go out by themselves on {when} after you approve a preview; WhatsApp ones become one-tap links.",
                 "actions": [edit]}]
    notes = f" {found['skipped']} line(s) skipped (double or no usable address)." if found["skipped"] else ""
    notes += f" {found['over']} over the limit of {target} left out." if found["over"] else ""
    rows = []
    if emails:
        digest = _invite_digest(cfg, emails)
        sent = invites.get("sent") or {}
        delivered = sum(1 for person in emails if (sent.get(person["email"]) or {}).get("ok"))
        failed = sum(1 for person in emails if (sent.get(person["email"]) or {}).get("ok") is False)
        tried = sum(1 for person in emails if person["email"] in sent)
        if invites.get("preview_hash") != digest:
            problem = f" Last try failed: {invites['preview_error']}." if invites.get("preview_error") else ""
            rows.append({**row, "title": f"E-mail invitations: preview for {len(emails)} people is on its way to you", "status": "auto",
                         "detail": f"Nothing goes to anyone before you approve it. The preview is mailed within the hour.{problem}{notes}",
                         "actions": [edit]})
        elif invites.get("approved_hash") != digest:
            rows.append({**row, "title": f"E-mail invitations: approve the preview ({len(emails)} people)", "status": "last-tap",
                         "detail": f"The exact text and the list were mailed to you on {invites.get('preview_sent_at', '?')}. "
                                   f"After your tap they go out on {when}, one mail per person, each person once.{notes}",
                         "actions": [{"name": f"launch:approve_invites:{test_id}", "label": "Approve the invitations"}, edit]})
        elif tried < len(emails):
            rows.append({**row, "title": f"E-mail invitations: {delivered} of {len(emails)} sent", "status": "auto",
                         "detail": f"Approved. They go out on {when}, one mail per person.{notes}", "actions": [edit]})
        else:
            trouble = f" {failed} could not be sent and are not retried: invite them by hand." if failed else ""
            rows.append({**row, "title": f"E-mail invitations: {delivered} of {len(emails)} sent", "status": "blocked" if failed else "done",
                         "detail": f"Each person was mailed once.{trouble}{notes}"})
    if phones:
        page = {"name": f"launch:open_invites:{test_id}", "label": "Open the WhatsApp links"}
        tapped = {"name": f"launch:wa_done:{test_id}", "label": "All sent"}
        base = {**row, "id": "auto-invites-wa", "replaces": None if emails else cfg.get("replaces")}
        if invites.get("whatsapp_done_at"):
            rows.append({**base, "title": f"WhatsApp invitations: {len(phones)} sent by you", "status": "done",
                         "detail": f"Marked done {invites['whatsapp_done_at']}. The page keeps the reminder and day-7 links.", "actions": [page]})
        else:
            rows.append({**base, "title": f"WhatsApp invitations: tap {len(phones)} ready links on {when}", "status": "last-tap",
                         "detail": "WhatsApp cannot be sent from this PC, so each person is one tap: the page opens the chat with the text ready."
                                   + ("" if emails else notes),
                         "actions": [page, tapped] + ([] if emails else [edit])})
    return rows


# ------------------------------------------------------------------ C. the proposal: keep it ready while it waits

def _tool(name: str) -> str | None:
    """Task Scheduler runs with a short PATH, so look in the usual install folders too."""
    known = {
        "node": [r"%ProgramFiles%\nodejs\node.exe", r"%LOCALAPPDATA%\Programs\nodejs\node.exe"],
        "git": [r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe", r"%ProgramFiles%\Git\cmd\git.exe"],
        "python": [sys.executable],
    }
    candidates = [shutil.which(name)] + [os.path.expandvars(path) for path in known.get(name, [])]
    return next((path for path in candidates if path and Path(path).exists()), None)


def _sync_repo(url: str, folder: Path) -> tuple[bool, str]:
    """An own, throw-away copy of the project at origin/main. (True, commit) or (False, why)."""
    git = _tool("git")
    if not git:
        return False, "git not found"
    routes = [[], ["-c", "http.proxy=http://localhost:3128"]]

    def run(arguments: list[str], cwd: Path | None, timeout: int) -> bool:
        try:
            return subprocess.run([git, *arguments], cwd=cwd, capture_output=True, timeout=timeout, creationflags=NO_WINDOW).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    if not (folder / ".git").exists():
        folder.parent.mkdir(parents=True, exist_ok=True)
        if not any(run([*route, "clone", "-q", "--depth", "1", "--branch", "main", url, str(folder)], None, 180) for route in routes):
            return False, "could not download the project"
    elif not any(run([*route, "fetch", "-q", "--depth", "1", "origin", "main"], folder, 120) for route in routes):
        return False, "could not reach GitHub"
    elif not run(["reset", "-q", "--hard", "FETCH_HEAD"], folder, 60):
        return False, "could not update the copy"
    commit = subprocess.run([git, "rev-parse", "--short", "HEAD"], cwd=folder, capture_output=True, text=True, creationflags=NO_WINDOW).stdout.strip()
    return True, commit


def _run_command(command: list, folder: Path, timeout: int, replace: dict | None = None) -> tuple[bool, str]:
    words = [str(word) for word in command]
    for key, value in (replace or {}).items():
        words = [word.replace("{" + key + "}", value) for word in words]
    program = _tool(words[0]) or words[0]
    try:
        done = subprocess.run([program, *words[1:]], cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError) as error:
        return False, f"{type(error).__name__}"
    tail = (done.stdout + "\n" + done.stderr).strip().splitlines()[-3:]
    return done.returncode == 0, " | ".join(line.strip()[:120] for line in tail)


def _link_answers(url: str) -> bool:
    import requests

    for proxies in (None, {"http": "http://localhost:3128", "https": "http://localhost:3128"}):
        try:
            with requests.get(url, timeout=15, stream=True, proxies=proxies, headers={"User-Agent": "Mozilla/5.0 (link check)"}) as answer:
                if answer.status_code < 400 or answer.status_code in (401, 403, 429):  # alive, just not for a script
                    return True
        except Exception:
            continue
    return False


def _check_links(folder: Path, files: list, strikes: dict) -> dict:
    urls = []
    for name in files:
        try:
            text = (folder / str(name)).read_text(encoding="utf-8")
        except OSError:
            continue
        for match in WEB_LINK.findall(text):
            url = match.rstrip(".,;:*")
            if url not in urls:
                urls.append(url)
    urls = urls[:LINK_LIMIT]
    counts = {url: (0 if _link_answers(url) else int(strikes.get(url) or 0) + 1) for url in urls}
    return {"total": len(urls), "strikes": {url: count for url, count in counts.items() if count}}


def _judges_answered(verdict: dict) -> str:
    """"2 of 3" when one of three judges gave no score; "" when the verdict does not list its judges."""
    judges = verdict.get("judges")
    if not isinstance(judges, dict) or not judges:
        return ""
    answered = sum(1 for entry in judges.values() if isinstance(entry, dict) and "total" in entry)
    return f"{answered} of {len(judges)}"


def step_proposal(test_id: str, cfg: dict, state: dict, now: datetime, force: bool = False) -> str:
    proposal = state.setdefault("proposal", {})
    today = now.date().isoformat()
    if proposal.get("checked_on") == today and not force:
        return "proposal: already checked today"
    folder = test_dir(test_id) / "repo"
    synced, commit = _sync_repo(str(cfg.get("repo") or ""), folder)
    proposal["sync_error"] = None if synced else commit
    if not synced:
        proposal["sync_failed_on"] = today
        return f"proposal: {commit}"
    checks = []
    for check in cfg.get("checks") or []:
        passed, tail = _run_command(check["command"], folder, 300)
        checks.append({"name": str(check.get("name") or " ".join(map(str, check["command"])))[:80], "ok": passed, "tail": tail})
    links = _check_links(folder, cfg.get("link_files") or [], (proposal.get("links") or {}).get("strikes") or {})
    proposal.update({"checked_on": today, "checked_at": _stamp(now), "commit": commit, "checks": checks, "links": links})
    judge = cfg.get("judge") or {}
    history = proposal.setdefault("scores", [])
    last = _day(history[-1]["date"]) if history else None
    if judge.get("command") and (last is None or (now.date() - last).days >= int(judge.get("every_days") or 7)):
        out = test_dir(test_id) / "judge.json"
        out.unlink(missing_ok=True)
        _run_command(judge["command"], folder, 600, {"repo": str(folder), "out": str(out), "python": sys.executable})
        try:
            verdict = json.loads(out.read_text(encoding="utf-8"))
            score = float(verdict["council_score"])
        except (OSError, ValueError, KeyError, TypeError):
            proposal["judge_error"] = f"no score on {today}"
        else:
            proposal.pop("judge_error", None)
            history.append({"date": today, "score": round(score, 2), "commit": commit, "judges": _judges_answered(verdict)})
            del history[:-26]
            proposal["fixes"] = [str(entry.get("fix") if isinstance(entry, dict) else entry)[:240] for entry in (verdict.get("fixes") or [])[:3]]
    failed = [check["name"] for check in checks if not check["ok"]]
    return f"proposal: {len(checks) - len(failed)} of {len(checks)} checks pass, {links['total']} links, commit {commit}"


def proposal_rows(test_id: str, cfg: dict, state: dict, now: datetime) -> list[dict]:
    proposal = state.get("proposal") or {}
    name = str(cfg.get("name") or "Proposal")
    row = {"id": "auto-proposal", "replaces": cfg.get("replaces"), "links": cfg.get("links") or []}
    again = [{"name": "launch_run_now", "label": "Check again now"}]
    if not proposal.get("checked_on"):
        why = f" Last try: {proposal['sync_error']}." if proposal.get("sync_error") else ""
        return [{**row, "title": f"{name}: first check is waiting", "status": "auto",
                 "detail": f"Checked every day and judged every week while it waits for feedback.{why}", "actions": again}]
    checks = proposal.get("checks") or []
    failed = [check for check in checks if not check.get("ok")]
    links = proposal.get("links") or {}
    dead = [url for url, count in (links.get("strikes") or {}).items() if count >= LINK_STRIKES]
    scores = proposal.get("scores") or []
    problems = [f"check \"{check['name']}\" fails ({check.get('tail') or 'no output'})" for check in failed]
    problems += [f"link does not answer for {LINK_STRIKES} days: {url}" for url in dead[:3]]
    judged = ""
    if scores:
        best = max(entry["score"] for entry in scores)
        latest = scores[-1]
        where = f" at commit {latest['commit']}" if latest.get("commit") else ""
        who = f", {latest['judges']} judges answered" if latest.get("judges") else ""
        judged = f" Judge: {latest['score']:.2f} on {latest['date']}{where}{who} (best {best:.2f}, {len(scores)} run{'' if len(scores) == 1 else 's'})."
        if latest.get("commit") and proposal.get("commit") and latest["commit"] != proposal["commit"]:
            judged += " The page changed after that score; the weekly run judges the new text."
        if latest["score"] < best - SCORE_DROP:
            problems.append(f"the judge's score fell from {best:.2f} to {latest['score']:.2f}")
    elif (cfg.get("judge") or {}).get("command"):
        judged = f" Judge: {proposal.get('judge_error') or 'first run pending'}."
    stale = f" GitHub was not reachable on {proposal['sync_failed_on']}; this is the last good check." if proposal.get("sync_error") else ""
    summary = (
        f"Last check {proposal.get('checked_at')} at commit {proposal.get('commit')}: "
        f"{len(checks) - len(failed)} of {len(checks)} checks pass, {links.get('total', 0) - len(dead)} of {links.get('total', 0)} cited links answer.{judged}{stale}"
    )
    fixes = proposal.get("fixes") or []
    ideas = (" Judge's top suggestion: " + fixes[0]) if fixes else ""
    if problems:
        return [{**row, "title": f"{name}: needs a fix before anyone reads it", "status": "blocked",
                 "detail": "; ".join(problems)[:420] + ". " + summary, "actions": again}]
    return [{**row, "title": f"{name}: checked every day, judged every week", "status": "auto",
             "detail": (summary + ideas)[:560] + " It reports only; the wording is never changed without you.", "actions": again}]


# ------------------------------------------------------------------ what the dashboard reads and calls

STEPS = (("reply", reply_rows), ("invites", invite_rows), ("proposal", proposal_rows))


def rows(test_id: str, now: datetime | None = None) -> list[dict]:
    """The automation rows of one launch test, from what is saved on disk. No network."""
    cfg = load_config().get(test_id)
    if not cfg:
        return []
    now = now or now_local()
    state = load_state(test_id)
    result = []
    for key, reader in STEPS:
        if not isinstance(cfg.get(key), dict):
            continue
        try:
            result.extend(reader(test_id, cfg[key], state, now))
        except Exception as error:  # one broken setting must not hide the other rows
            result.append({"id": f"auto-{key}", "title": f"Automation \"{key}\" has a problem", "status": "blocked",
                           "detail": f"{type(error).__name__}: {str(error)[:200]}"})
    return result


def run(force: bool = False, now: datetime | None = None) -> str:
    """One pass over every configured test. Only one pass works at a time on this PC."""
    with _only_one("run") as mine:
        if not mine:
            return "another run is still working; nothing done"
        return _run(force, now or now_local())


def _run(force: bool, now: datetime) -> str:
    outcomes = []
    for test_id, cfg in load_config().items():
        state = {}
        mail = cfg.get("mail") or {}
        jobs = (
            ("reply", lambda: step_reply(test_id, cfg["reply"], mail, state, now)),
            ("invites", lambda: step_invites(test_id, cfg["invites"], mail, state, now)),
            ("proposal", lambda: step_proposal(test_id, cfg["proposal"], state, now, force)),
        )
        for key, job in jobs:
            if not isinstance(cfg.get(key), dict):
                continue
            state.clear()
            state.update(load_state(test_id))  # fresh for every job: a tap may have landed during the last one
            try:
                outcomes.append(f"{test_id} {job()}")
            except Exception as error:
                outcomes.append(f"{test_id} {key}: FAILED {type(error).__name__}: {str(error)[:160]}")
            save_state(test_id, state, only=key)
        save_state(test_id, {"last_run": _stamp(now)}, only="last_run")
    return "; ".join(outcomes) or "nothing configured"


def action(name: str, now: datetime | None = None) -> str:
    """One button of the dashboard. Anything that is not exactly launch:<verb>:<configured test> is refused."""
    match = ACTION.match(name if isinstance(name, str) else "")
    config = load_config()
    if not match or match.group(2) not in config:
        raise ValueError(f"Unknown action: {name}")
    verb, test_id = match.groups()
    cfg = config[test_id]
    mail = cfg.get("mail") or {}
    now = now or now_local()
    state = load_state(test_id)
    reply = state.setdefault("reply", {})
    invites = state.setdefault("invites", {})
    reply_cfg = cfg.get("reply") if isinstance(cfg.get("reply"), dict) else None
    invite_cfg = cfg.get("invites") if isinstance(cfg.get("invites"), dict) else None

    if verb == "followup" and reply_cfg:
        if reply.get("status") in SETTLED:
            return "This mail is already settled; no follow-up sent."
        due = _day(reply_cfg.get("followup_due"))
        if not due or now.date() < due:
            return f"Too early: the follow-up is due on {_nice(due)}."
        reads = _relay_version(mail) >= READS_INBOX
        return _send_followup(test_id, reply_cfg, mail, state, now, only_if_no_answer=reads)
    if verb == "replied" and reply_cfg:
        reply.update({"status": "replied", "by_hand": True, "found_at": _stamp(now)})
        save_state(test_id, state, only="reply")
        return "Noted: an answer came. No follow-up will be sent."
    if verb == "no_reply" and reply_cfg:
        reply["status"] = "closed"
        save_state(test_id, state, only="reply")
        return "Noted: no answer. This step is closed."
    if verb in ("open_reply", "reply_read") and reply_cfg:
        saved = test_dir(test_id) / "reply.txt"
        if verb == "open_reply":
            if not saved.exists():
                return "No saved answer on this PC."
            _open(saved)
        reply["read_at"] = _stamp(now)
        save_state(test_id, state, only="reply")
        return "Answer opened." if verb == "open_reply" else "Marked as read."
    if verb == "relay_code":
        code = Path(str(mail.get("relay_code_path") or ""))
        if not code.is_file():
            return "The new relay code is not on this PC."
        literal = str(code).replace("'", "''")
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"Set-Clipboard -Value (Get-Content -LiteralPath '{literal}' -Raw -Encoding UTF8)"],
            capture_output=True, creationflags=NO_WINDOW, timeout=30,
        )
        import webbrowser

        webbrowser.open("https://script.google.com/home")
        return "Code copied. In Apps Script open the relay project, select all, paste, save, then Deploy > Manage deployments > Edit > New version > Deploy."
    if verb == "approve_invites" and invite_cfg:
        emails = _invite_people(test_id, invite_cfg)["emails"]
        digest = _invite_digest(invite_cfg, emails) if emails else None
        if not digest or invites.get("preview_hash") != digest:
            return "The list or the text changed since the preview. A new preview is mailed to you within the hour; approve that one."
        invites.update({"approved_hash": digest, "approved_at": _stamp(now)})
        save_state(test_id, state, only="invites")
        return f"Approved: {len(emails)} invitations go out at the start time, each person once."
    if verb == "open_invites" and invite_cfg:
        phones = _invite_people(test_id, invite_cfg)["phones"]
        if not phones:
            return "No WhatsApp numbers in the contact list."
        _open(_invite_page(test_id, invite_cfg, phones))
        return "WhatsApp links opened."
    if verb == "open_contacts" and invite_cfg:
        contacts = _path(test_id, invite_cfg.get("contacts_file") or "contacts.csv")
        if not contacts.exists():
            contacts.parent.mkdir(parents=True, exist_ok=True)
            contacts.write_text(
                "# One person per line. Give an e-mail (invited automatically after your approval) and/or a WhatsApp number (one-tap link).\n"
                "# Lines that start with # are ignored. This file stays on this PC.\n"
                "name,email,whatsapp\n",
                encoding="utf-8",
            )
        _open(contacts)
        return "Contact list opened. Save it when you are done; the next hourly run picks it up."
    if verb == "wa_done" and invite_cfg:
        invites["whatsapp_done_at"] = _stamp(now)
        save_state(test_id, state, only="invites")
        return "Noted: the WhatsApp invitations are sent."
    raise ValueError(f"Unknown action: {name}")


# ------------------------------------------------------------------ scheduled task and command line

def install_task() -> None:
    """Every hour, hidden (pythonw), catching up if the PC was off."""
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    runner = pythonw if pythonw.exists() else Path(sys.executable)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    script = f"""
$action = New-ScheduledTaskAction -Execute '{runner}' -Argument '"{Path(__file__).resolve()}"' -WorkingDirectory '{CODE_DIR}'
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Hours 1)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' -Action $action -Trigger $hourly -Settings $settings `
    -Description 'Launch tests: watch for the answer to a sent mail, send approved invitations at the start time, keep the proposal checked.' -Force | Out-Null
Get-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName '{TASK_NAME}' | Select-Object TaskName, State
"""
    print(subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True).stdout.strip())


def show() -> None:
    for test_id in load_config():
        print(f"== {test_id}")
        for row in rows(test_id):
            print(f"[{row.get('status', '?'):<8}] {row['title']}")
            print(f"           {row.get('detail', '')}")


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if arguments == ["install"]:
        install_task()
    elif arguments == ["show"]:
        show()
    elif arguments in ([], ["--force"]):
        try:
            outcome = run(force=bool(arguments))
        except Exception as error:  # the task must leave a trace, never a silent crash
            outcome = f"FAILED {type(error).__name__}: {error}"
        log(outcome)
        print(outcome)
    else:
        print(__doc__)
