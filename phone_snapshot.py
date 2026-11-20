"""Snapshot of the real dashboard, for using ReachPilot from a phone.

Usage:  python phone_snapshot.py [out.html]

Writes generated/phone_snapshot.html (git-ignored): dashboard.html with the current /api/data
answer baked in, the same way docs/build_demo.py bakes in the demo data. Cairn's dashboards
publisher uploads the file to your private relay repo, and the phone reads it from there (works
from any network).

It has no server behind it. A button hands its name to the page that shows the snapshot (Cairn's
Dashboards page on the phone), which passes it to this PC through the relay repo; phone_action.py
runs it here and the answer comes back as the usual message. Opened any other way (the file itself,
an older Cairn), the buttons only explain that actions run on the PC.

Personal data stays out of public places: the file is written to a git-ignored folder and only
ever uploaded to a private repo.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PAGE = ROOT / "dashboard.html"
OUT = ROOT / "generated" / "phone_snapshot.html"
ICON = ROOT / "icons" / "icon.svg"

MAIL_ON_PC = "The words of this mail stay on your PC. Read and answer it in ReachPilot there, or on Titip's own Autopilot page."

READ_ONLY = "Read-only here: use ReachPilot on your PC, or Cairn's Dashboards page on your phone, to run this."

SHIM = """<script>
/* ReachPilot phone snapshot: the data below was read from this PC at %s. */
(() => {
  const DATA = %s;
  const READ_ONLY = %s;
  const realFetch = window.fetch.bind(window);
  const json = (body) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });

  /* Buttons: the page around this one (Cairn's Dashboards page) sends the name to the PC and passes the answer back. */
  const waiting = new Map();
  let asked = 0;
  addEventListener("message", (event) => {
    const reply = event.data;
    if (event.source !== window.parent || !reply || reply.cairnDash !== "reply" || !waiting.has(reply.id)) return;
    const entry = waiting.get(reply.id);
    entry.heard = true;
    if (reply.state === "done") {
      waiting.delete(reply.id);
      entry.resolve(String(reply.message || "Done."));
    } else if (reply.message && typeof window.toast === "function") {
      window.toast(String(reply.message));
    }
  });
  const askPc = (name) => new Promise((resolve) => {
    if (window.parent === window) return resolve(READ_ONLY);
    const id = "rp" + (asked += 1);
    const entry = { resolve, heard: false };
    waiting.set(id, entry);
    window.parent.postMessage({ cairnDash: "action", id, name }, "*");
    setTimeout(() => {  /* nobody answered: this is not Cairn's Dashboards page (or an old copy of it) */
      if (entry.heard) return;
      waiting.delete(id);
      resolve(READ_ONLY);
    }, 5000);
  });

  window.fetch = async (url, options = {}) => {
    const path = String(url);
    if (path.endsWith("/api/data")) return json(DATA);
    if (path.endsWith("/api/action")) {
      let name = "";
      try { name = String(JSON.parse(options.body).name || ""); } catch { name = ""; }
      return json({ message: name ? await askPc(name) : READ_ONLY });
    }
    return realFetch(url, options);
  };

  /* The browser's own confirm() and prompt() never open inside the phone page's frame; the dashboard uses these instead. */
  const sheet = (text, withInput, words) => new Promise((resolve) => {
    const make = (tag, css, words) => {
      const node = document.createElement(tag);
      node.style.cssText = css;
      if (words) node.textContent = words;
      return node;
    };
    const back = make("div", "position:fixed;inset:0;z-index:50;display:flex;align-items:flex-end;justify-content:center;padding:16px;background:rgba(0,0,0,.45)");
    back.setAttribute("role", "dialog");
    back.setAttribute("aria-modal", "true");
    back.setAttribute("aria-label", text);
    const box = make("div", "width:100%%;max-width:460px;padding:18px;border-radius:12px;background:var(--surface,#fff);color:var(--ink,#111);box-shadow:0 10px 40px rgba(0,0,0,.35)");
    const input = make("input", "width:100%%;min-height:44px;margin:0 0 14px;padding:8px 10px;border:1px solid var(--line-strong,#bbb);border-radius:6px;background:var(--bg,#fff);color:inherit;font:inherit");
    input.type = words ? "text" : "url";
    input.setAttribute("aria-label", text);
    const row = make("div", "display:flex;gap:8px;justify-content:flex-end");
    const no = make("button", "", "Cancel");
    const yes = make("button", "", "OK");
    no.className = "btn ghost";
    yes.className = "btn";
    no.type = yes.type = "button";
    row.append(no, yes);
    box.append(make("p", "margin:0 0 14px;white-space:pre-wrap", text), ...(withInput ? [input] : []), row);
    back.append(box);
    const close = (value) => { back.remove(); resolve(value); };
    no.addEventListener("click", (event) => { event.stopPropagation(); close(withInput ? null : false); });
    yes.addEventListener("click", (event) => { event.stopPropagation(); close(withInput ? input.value : true); });
    document.body.append(back);
    (withInput ? input : yes).focus();
  });
  window.rpConfirm = (text) => sheet(text, false);
  window.rpPrompt = (text, words) => sheet(text, true, words);
})();
</script>
"""


def without_local_images(data: dict) -> dict:
    """Post thumbnails are served by the local server (/img?p=...); a phone can't load them."""
    for entry in data.get("schedule") or []:
        if isinstance(entry, dict):
            entry["images"] = []
    return data


def without_mail_text(data: dict) -> dict:
    """The Gmail answers card (gmail_answers.py) on the phone: counts, senders and subjects only.
    What a mail says, why it was kept, the suggested answer and the answers that were sent stay on this PC:
    the snapshot is a plain file in a private repo, and Titip's own phone page is the encrypted way to read them.
    Without the words there is nothing to send from here, so those buttons go too."""
    hub = data.get("hub") if isinstance(data.get("hub"), dict) else {}
    card = hub.get("gmail")
    if isinstance(card, dict) and card.get("installed"):
        for row in card.get("waiting") or []:
            row.update({"summary": "", "reason": "", "draft": "", "error": "", "hidden": MAIL_ON_PC})
        for row in card.get("failed") or []:  # its reason is Titip's own sentence ("The model did not answer"), not the mail
            row.update({"summary": "", "draft": "", "error": ""})
        for event in card.get("recent") or []:
            event["text"] = ""
        card["problem"] = "The last look had a problem; ReachPilot on your PC says which." if card.get("problem") else ""
        card["page"] = ""
    for item in hub.get("todo") or []:
        if isinstance(item, dict) and item.get("module") == "Gmail":
            for entry in item.get("links") or []:
                if isinstance(entry, dict) and entry.get("mail"):
                    entry["note"] = MAIL_ON_PC
                    entry.pop("actions", None)
    return data


def without_submission_details(data: dict) -> dict:
    """The Submissions card on the phone: names, states, scores and dates only. "detail" (the full title, who
    reviews it and what they answered) names other people and stays on this PC."""
    hub = data.get("hub") if isinstance(data.get("hub"), dict) else {}
    card = hub.get("submissions")
    for board in (card.get("boards") or []) if isinstance(card, dict) else []:
        for item in (board.get("items") or []) if isinstance(board, dict) else []:
            if isinstance(item, dict):
                item["detail"] = ""
    return data


def render(data: dict, page: str, built_at: str) -> str:
    page = page.replace("__RP_TOKEN__", "demo")  # "demo" also turns off the local auto-update poll
    icon = "data:image/svg+xml;base64," + base64.b64encode(ICON.read_bytes()).decode("ascii")
    page = page.replace('src="/icon.svg"', f'src="{icon}"').replace('href="/icon.svg"', f'href="{icon}"')
    shim = SHIM % (built_at, json.dumps(without_submission_details(without_mail_text(without_local_images(data))), ensure_ascii=False).replace("</", "<\\/"),
                   json.dumps(READ_ONLY))
    return page.replace("<head>", "<head>\n" + shim, 1)


def build(out: Path = OUT) -> Path:
    import dashboard  # imported here: it loads the private modules and the local database

    data = dashboard.build_data()
    built_at = (data.get("overview") or {}).get("now") or ""
    html = render(data, PAGE.read_text(encoding="utf-8"), built_at)
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(".tmp")
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(out)  # never leave a half-written file for the publisher to pick up
    return out


if __name__ == "__main__":
    try:  # started at low priority on a busy PC, the build waits minutes and Task Scheduler does not answer in time
        import launch_auto

        launch_auto.leave_low_priority()
    except Exception:
        pass
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT))
