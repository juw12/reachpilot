"""Pages ReachPilot builds itself, so every project can have a video, with or without a site of its own.

target()         what a post's video records: the project's own site, else its landing page (/p/<name>/ on the
                 Pages site), else a page built here. A roundup always gets a built page, one card per project.
page()           that page: phone-first, self-contained, written from repo facts only (name, description, README
                 lines, README pictures, licence, the clone command). Built for a recording it is handed straight to
                 the recording browser and is never published.
publish_watch()  the same page with the finished video on top, published at https://<user>.github.io/watch/<name>/ :
                 one link that plays the demo. Outreach e-mails link to it and creators who covered the project are
                 credited on it.
"""
from __future__ import annotations

import hashlib
import html
import json
import time
import urllib.parse
from pathlib import Path

import reels
import trylinks
import visuals
from github_source import RepoFacts

BUILT_PAGES = "reachpilot-showcase"  # address prefix of pages that exist only inside the recording browser
WATCH_INDEX = "watch_pages.json"  # generated/watch_pages.json: {repo: watch page URL}, only pages that answered
FEATURES_SHOWN = 4
PREVIEW_FRAME = "#t=0.5"  # seconds into the clip shown as the still preview
FEATURES_MORE = 6
KNOWN_LICENSE_EXCLUDES = ("", "NOASSERTION", "Other")

STYLE = """
*{box-sizing:border-box}
body{margin:0;font:17px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:#0f172a;background:#f8fafc}
header{padding:34px 22px 30px;color:#fff;background:linear-gradient(150deg,DARK,LIGHT)}
header .path{margin:0 0 10px;font-size:13px;letter-spacing:.04em;opacity:.85}
h1{margin:0;font-size:38px;line-height:1.08;letter-spacing:-.02em}
header .lead{margin:14px 0 0;font-size:19px;line-height:1.4}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px}
.chips span{padding:5px 12px;border-radius:999px;font-size:13px;font-weight:600;background:rgba(255,255,255,.18)}
main{max-width:720px;margin:0 auto;padding:20px 18px 40px}
video,main img{display:block;width:100%;border-radius:16px;margin:0 0 18px;background:#0b0b0f;box-shadow:0 6px 22px rgba(15,23,42,.14)}
video{max-height:82vh}
section,details,.card{margin:0 0 16px;padding:18px 18px;border-radius:16px;background:#fff;border:1px solid #e2e8f0}
h2{margin:0 0 10px;font-size:21px;letter-spacing:-.01em}
ul{margin:0;padding-left:20px}
li{margin:0 0 9px}
summary{cursor:pointer;font-weight:700;font-size:18px}
details[open] summary{margin-bottom:12px}
pre{margin:0 0 10px;padding:12px 14px;border-radius:10px;overflow-x:auto;font-size:14px;color:#e2e8f0;background:#0f172a}
p{margin:0 0 10px}
a{color:LIGHT_LINK}
.card details{margin:12px 0 0;padding:12px 14px;background:#f8fafc}
.card summary{font-size:16px}
.cta{display:block;margin-top:22px;padding:15px;border-radius:14px;text-align:center;font-weight:700;color:#fff;
text-decoration:none;background:DARK}
"""


def _palette(name: str) -> tuple[str, str]:
    return visuals.PALETTES[int(hashlib.md5(name.encode()).hexdigest(), 16) % len(visuals.PALETTES)]


def _image_url(user: str, facts: RepoFacts, path: str) -> str:
    """A README picture as an address a browser can load: repo-relative paths are read from the repo itself."""
    if path.startswith(("http://", "https://")):
        return path
    return f"https://raw.githubusercontent.com/{user}/{facts.name}/HEAD/{urllib.parse.quote(path.lstrip('./'))}"


def _chips(facts: RepoFacts) -> list[str]:
    licence = f"{facts.license} licence" if facts.license not in KNOWN_LICENSE_EXCLUDES else ""
    return [chip for chip in [facts.language or "", licence, *list(facts.topics or [])[:3]] if chip]


def _list(lines: list[str]) -> str:
    return "<ul>" + "".join(f"<li>{html.escape(line)}</li>" for line in lines) + "</ul>"


def _project_body(facts: RepoFacts, user: str, video_url: str, credits) -> str:
    features = reels.feature_lines(facts)
    pictures = [_image_url(user, facts, path) for path in list(facts.readme_images or [])[:2]]
    parts = []
    if video_url:
        # The time fragment makes the player show a real frame before anyone presses play, not a black box.
        parts.append(f'<video src="{html.escape(video_url, quote=True)}{PREVIEW_FRAME}" controls playsinline muted loop preload="metadata"></video>')
    if pictures:
        parts.append(f'<img src="{html.escape(pictures[0], quote=True)}" alt="{html.escape(facts.display_name)}">')
    if features:
        parts.append("<section><h2>What it does</h2>" + _list(features[:FEATURES_SHOWN]) + "</section>")
    parts.append(
        "<details><summary>How to get it</summary><p>The code and the README are on GitHub.</p>"
        f"<pre>git clone {html.escape(facts.html_url)}</pre></details>"
    )
    more = features[FEATURES_SHOWN:FEATURES_SHOWN + FEATURES_MORE]
    if more:
        parts.append("<details><summary>More details</summary>" + _list(more) + "</details>")
    if len(pictures) > 1:
        parts.append(f'<img src="{html.escape(pictures[1], quote=True)}" alt="{html.escape(facts.display_name)}">')
    if credits:
        rows = "".join(
            f'<li><a href="{html.escape(entry["url"], quote=True)}">{html.escape(entry["creator"])}</a></li>'
            for entry in credits
        )
        parts.append(f"<section><h2>Covered by</h2><ul>{rows}</ul></section>")
    parts.append(f'<a class="cta" href="{html.escape(facts.html_url, quote=True)}">View the code on GitHub</a>')
    return "".join(parts)


def _card(facts: RepoFacts) -> str:
    features = reels.feature_lines(facts)[:3]
    more = ""
    if features:
        more = f"<details><summary>More about {html.escape(facts.display_name)}</summary>{_list(features)}</details>"
    return (
        f'<div class="card"><h2>{html.escape(facts.display_name)}</h2>'
        f"<p>{html.escape(reels.what_it_is(facts) or facts.description or '')}</p>{more}</div>"
    )


def page(subjects: list[RepoFacts], user: str, video_url: str = "", credits=()) -> str:
    """One self-contained page about one project, or a card per project. Every word comes from the repos."""
    single = len(subjects) == 1
    first = subjects[0]
    title = first.display_name if single else f"{len(subjects)} projects by {user}"
    lead = (reels.what_it_is(first) or first.description or "") if single else ""
    path = first.html_url.split("//", 1)[-1].rstrip("/") if single else f"github.com/{user}"
    chips = _chips(first) if single else []
    dark, light = _palette(first.name if single else "roundup")
    style = STYLE.replace("LIGHT_LINK", dark).replace("DARK", dark).replace("LIGHT", light)
    if single:
        body = _project_body(first, user, video_url, credits)
    else:
        body = "".join(_card(facts) for facts in subjects)
        body += f'<a class="cta" href="https://github.com/{html.escape(user, quote=True)}">See them all on GitHub</a>'
    social = ""
    if video_url:
        social = (
            f'<meta property="og:title" content="{html.escape(title, quote=True)}">'
            f'<meta property="og:description" content="{html.escape(lead, quote=True)}">'
            f'<meta property="og:video" content="{html.escape(video_url, quote=True)}">'
        )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>{social}<style>{style}</style></head><body>"
        f'<header><p class="path">{html.escape(path)}</p><h1>{html.escape(title)}</h1>'
        + (f'<p class="lead">{html.escape(lead)}</p>' if lead else "")
        + ('<div class="chips">' + "".join(f"<span>{html.escape(chip)}</span>" for chip in chips) + "</div>" if chips else "")
        + f"</header><main>{body}</main></body></html>"
    )


def landing_url(user: str, name: str) -> str:
    return f"https://{user}.github.io/p/{name}/"


def subject_name(angle: str, subjects: list[RepoFacts]) -> str:
    """What a post's video is filed under: the repo name for one project, "roundup" for several."""
    return subjects[0].name if angle != "roundup" and len(subjects) == 1 else "roundup"


def target(user: str, angle: str, subjects: list[RepoFacts], is_live=None) -> tuple[str, dict[str, str]]:
    """(the address a post's video records, {address: html} of pages to hand to the recording browser).

    One project: its own site; else its landing page when that answers; else a page built from the repo.
    A roundup or several projects: a built page with a card per project. ("", {}) when there is nothing to show.
    """
    if not subjects:
        return "", {}
    name = subject_name(angle, subjects)
    if name != "roundup":
        facts = subjects[0]
        if facts.homepage and "//github.com/" not in facts.homepage:
            return facts.homepage, {}
        landing = landing_url(user, facts.name)
        if (is_live or trylinks._is_live)(landing):
            return landing, {}
    url = f"https://{user}.github.io/{BUILT_PAGES}/{name}/"
    return url, {url: page(subjects, user)}


def watch_url(user: str, name: str) -> str:
    return f"https://{user}.github.io/watch/{name}/"


def publish_watch(user: str, facts: RepoFacts, video_url: str, checkout: Path, log, credits=()) -> str:
    """Publish watch/<name>/index.html on the Pages site. Returns its URL once it answers, else ""."""
    if not trylinks._pages_checkout(user, checkout, log):
        return ""
    url = watch_url(user, facts.name)
    index = checkout / "watch" / facts.name / "index.html"
    content = page([facts], user, video_url, credits)
    if not (index.exists() and index.read_text(encoding="utf-8") == content):
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(content, encoding="utf-8", newline="\n")
        trylinks._git(["add", "watch"], checkout)
        trylinks._git(["commit", "-m", f"Watch page: {facts.name}"], checkout)
    push = trylinks._git(["push", "--quiet"], checkout)
    if push.returncode != 0:
        log.warning("Pushing the watch page for %s failed: %s", facts.name, push.stderr[-300:])
        return ""
    for _ in range(trylinks.DEMO_LIVE_WAIT_SECONDS // 10):
        if trylinks._is_live(url):
            log.info("Watch page for %s is live at %s", facts.name, url)
            return url
        time.sleep(10)
    log.warning("Watch page for %s was pushed but %s is not live yet", facts.name, url)
    return ""


def remember(index_path: Path, name: str, url: str) -> None:
    """Note a watch page that answered, where the outreach module looks for a project's video link."""
    known = {}
    if index_path.exists():
        try:
            known = json.loads(index_path.read_text(encoding="utf-8"))
        except ValueError:
            known = {}
    if known.get(name) != url:
        known[name] = url
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(json.dumps(known, indent=2, sort_keys=True), encoding="utf-8")
