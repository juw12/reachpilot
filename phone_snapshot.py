"""Read-only snapshot of the real dashboard, for viewing ReachPilot from a phone.

Usage:  python phone_snapshot.py [out.html]

Writes generated/phone_snapshot.html (git-ignored): dashboard.html with the current /api/data
answer baked in, the same way docs/build_demo.py bakes in the demo data. It has no server behind
it, so the buttons only explain that actions run on the PC. Cairn's `live` publisher uploads the
file to your private relay repo, and the phone reads it from there (works from any network).

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

READ_ONLY = "Read-only on your phone: open ReachPilot on your PC to run this."

SHIM = """<script>
/* ReachPilot phone snapshot: the data below was read from this PC at %s. */
(() => {
  const DATA = %s;
  const READ_ONLY = %s;
  const realFetch = window.fetch.bind(window);
  const json = (body) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  window.fetch = async (url, options = {}) => {
    const path = String(url);
    if (path.endsWith("/api/data")) return json(DATA);
    if (path.endsWith("/api/action")) return json({ message: READ_ONLY });
    return realFetch(url, options);
  };
})();
</script>
"""


def without_local_images(data: dict) -> dict:
    """Post thumbnails are served by the local server (/img?p=...); a phone can't load them."""
    for entry in data.get("schedule") or []:
        if isinstance(entry, dict):
            entry["images"] = []
    return data


def render(data: dict, page: str, built_at: str) -> str:
    page = page.replace("__RP_TOKEN__", "demo")  # "demo" also turns off the local auto-update poll
    icon = "data:image/svg+xml;base64," + base64.b64encode(ICON.read_bytes()).decode("ascii")
    page = page.replace('src="/icon.svg"', f'src="{icon}"').replace('href="/icon.svg"', f'href="{icon}"')
    shim = SHIM % (built_at, json.dumps(without_local_images(data), ensure_ascii=False).replace("</", "<\\/"),
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
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else OUT))
