"""Real visuals only: fresh screenshots and a short demo video of a project's page, and media from the repos."""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from github_source import CREATE_NO_WINDOW, RepoFacts, gh_api

FONT_DIR = Path("C:/Windows/Fonts")
PALETTES = [
    ("#0f172a", "#1e3a5f"), ("#0b1f4d", "#2563eb"), ("#083d38", "#0f766e"),
    ("#5b0a1f", "#e11d48"), ("#7c2d12", "#f97316"), ("#3d4a2a", "#6b7d4f"),
    ("#042f2e", "#14b8a6"), ("#2e1065", "#7c3aed"),
]


def _font(size: int) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(FONT_DIR / "segoeui.ttf"), size)
    except OSError:
        return ImageFont.load_default()


def capture(urls: dict[str, str], out_dir: Path) -> dict[str, Path]:
    """Screenshot several pages in one browser session. Returns name -> png path."""
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, Path] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_context(
            viewport={"width": 1280, "height": 800},
            device_scale_factor=1.5,
            color_scheme="light",
        ).new_page()
        for name, url in urls.items():
            target = out_dir / f"{name}.png"
            try:
                page.goto(url, wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(2500)
                page.screenshot(path=str(target))
                results[name] = target
            except Exception:
                continue
        browser.close()
    return results


def browser_frame(screenshot: Image.Image, url: str, width: int) -> Image.Image:
    shot = screenshot.convert("RGB")
    shot = shot.resize((width, int(shot.height * width / shot.width)), Image.LANCZOS)
    bar = 44
    framed = Image.new("RGB", (width, shot.height + bar), "#f1f3f5")
    draw = ImageDraw.Draw(framed)
    for index, color in enumerate(["#ff5f57", "#febc2e", "#28c840"]):
        center = 22 + index * 20
        draw.ellipse((center - 6, 16, center + 6, 28), fill=color)
    draw.rounded_rectangle((90, 9, width - 20, 35), radius=13, fill="white")
    draw.text((106, 12), url.replace("https://", "").rstrip("/"), font=_font(16), fill="#495057")
    framed.paste(shot, (0, bar))
    return framed


def compose_framed(screenshot_path: Path, url: str, output: Path, seed: str) -> Path:
    width, height = 1200, 900
    dark, light = PALETTES[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(PALETTES)]
    gradient = Image.linear_gradient("L").resize((width, height)).rotate(-30)
    canvas = Image.composite(Image.new("RGB", (width, height), light), Image.new("RGB", (width, height), dark), gradient)
    frame = browser_frame(Image.open(screenshot_path), url, width - 120)
    frame = frame.crop((0, 0, frame.width, min(frame.height, height - 100)))
    left = (width - frame.width) // 2
    top = (height - frame.height) // 2
    shadow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        (left + 6, top + 14, left + frame.width + 6, top + frame.height + 14), radius=16, fill=(0, 0, 0, 110)
    )
    canvas = Image.alpha_composite(canvas.convert("RGBA"), shadow.filter(ImageFilter.GaussianBlur(14))).convert("RGB")
    mask = Image.new("L", frame.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, frame.width, frame.height), radius=14, fill=255)
    canvas.paste(frame, (left, top), mask)
    canvas.save(output, quality=92)
    return output


def repo_media(user: str, facts: RepoFacts, out_dir: Path, limit: int = 2) -> list[Path]:
    paths = []
    for relative in facts.readme_images[:limit]:
        clean = relative.lstrip("./")
        data = gh_api(f"repos/{user}/{facts.name}/contents/{clean}", raw=True)
        if not data:
            continue
        suffix = Path(clean).suffix.lower()
        target = out_dir / f"{facts.name}_{Path(clean).stem}{'.gif' if suffix == '.gif' else '.jpg'}"
        if suffix == ".gif":
            target.write_bytes(data)
        else:
            Image.open(io.BytesIO(data)).convert("RGB").save(target, quality=92)
        paths.append(target)
    return paths


def images_for_post(user: str, angle: str, subjects: list[RepoFacts], out_dir: Path) -> list[Path]:
    """Pick real visuals by angle so recycled posts don't reuse the same picture."""
    out_dir.mkdir(parents=True, exist_ok=True)
    urls = {}
    for facts in subjects:
        if angle == "behind_the_build" or not facts.homepage:
            urls[f"{facts.name}_repo"] = facts.html_url
        if facts.homepage:
            urls[f"{facts.name}_site"] = facts.homepage
    shots = capture(urls, out_dir / "raw")

    framed: list[Path] = []
    for name, shot in shots.items():
        owner = next(facts for facts in subjects if name.startswith(facts.name + "_"))
        page_url = owner.homepage if name.endswith("_site") else owner.html_url
        framed.append(compose_framed(shot, page_url, out_dir / f"{name}.jpg", seed=name))

    media: list[Path] = []
    if angle != "roundup":
        media = repo_media(user, subjects[0], out_dir)

    if angle in ("deep_dive", "feedback") and media:
        ordered = media + framed
    else:
        ordered = framed + media
    return ordered[:6]


# ------------------------------------------------------------------ demo video (a short screen recording of the live site)

DEMO_SIZE = (1280, 720)
PHONE_SIZE = (450, 620)  # the phone-sized view the vertical videos (reels.py) record, in CSS pixels
PHONE_SCALE = 2  # ... shown twice as large, so the recording is 900x1240 and stays sharp
PHONE_WRAPPER_PATH = "/reachpilot-phone-view"  # where record_raw serves the page that holds the phone frame
DEMO_MIN_SECONDS = 8
DEMO_MAX_SECONDS = 22
DEMO_MAX_CLICKS = 3
DEMO_MAX_SCREENS = 5
DEMO_MIN_BYTES = 100_000  # LinkedIn refuses videos under 75 KB; a file this small is a blank page anyway
# Buttons the demo never presses: anything that signs in, pays, leaves the page or throws work away.
DEMO_SKIP_LABEL = re.compile(
    r"delete|remove|reset|clear|sign.?in|log.?in|sign.?up|log.?out|subscribe|buy|pay|donate|checkout|download|install|"
    r"export|import|upload|share|print|github|star|send|submit|cookie", re.I)

# Headless recordings have no mouse pointer, so the page draws one that follows the real mouse events.
DEMO_CURSOR_JS = """
(() => {
  const install = () => {
    if (document.getElementById('rp-demo-cursor')) return;
    const cursor = document.createElement('div');
    cursor.id = 'rp-demo-cursor';
    cursor.style.cssText = 'position:fixed;left:0;top:0;width:26px;height:26px;z-index:2147483647;' +
      'pointer-events:none;transform:translate(-100px,-100px);filter:drop-shadow(0 2px 3px rgba(0,0,0,.45))';
    cursor.innerHTML = '<svg viewBox="0 0 24 24" width="26" height="26"><path d="M4 2l15 11.5-6.6 1 3.9 7.2-2.9 1.5' +
      '-3.8-7.3L5 20.5z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(cursor);
    addEventListener('mousemove', (event) => {
      cursor.style.transform = `translate(${event.clientX - 4}px, ${event.clientY - 2}px)`;
    }, true);
    addEventListener('mousedown', (event) => {
      const ring = document.createElement('div');
      ring.style.cssText = 'position:fixed;z-index:2147483646;pointer-events:none;border-radius:50%;' +
        'width:16px;height:16px;border:3px solid rgba(37,99,235,.9);background:rgba(37,99,235,.25);' +
        `left:${event.clientX - 11}px;top:${event.clientY - 11}px;transition:transform .45s ease-out, opacity .45s ease-out`;
      document.documentElement.appendChild(ring);
      requestAnimationFrame(() => { ring.style.transform = 'scale(3.2)'; ring.style.opacity = '0'; });
      setTimeout(() => ring.remove(), 500);
    }, true);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', install); else install();
})();
"""

# Controls worth pressing that are on screen right now and not covered by something else: [{x, y, label}].
DEMO_TARGETS_JS = """
() => {
  const found = [];
  for (const element of document.querySelectorAll('[role=tab], button, summary, [role=button]')) {
    if (element.disabled || element.closest('form') || element.getAttribute('type') === 'submit') continue;
    if (element.getAttribute('aria-selected') === 'true' || element.getAttribute('aria-current')) continue;
    const box = element.getBoundingClientRect();
    if (box.width < 24 || box.height < 16 || box.width > 600) continue;
    const x = box.left + box.width / 2;
    const y = box.top + box.height / 2;
    if (x < 8 || y < 8 || x > innerWidth - 8 || y > innerHeight - 8) continue;
    const top = document.elementFromPoint(x, y);
    if (!top || !(element === top || element.contains(top))) continue;
    const label = (element.innerText || element.getAttribute('aria-label') || element.title || '').trim().slice(0, 60);
    found.push({x, y, label});
  }
  return found;
}
"""


def _ffmpeg() -> str:
    """The ffmpeg that ships with imageio-ffmpeg (pip installs it, nothing to set up), else one on PATH."""
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        found = shutil.which("ffmpeg")
        if not found:
            raise RuntimeError("no ffmpeg: pip install imageio-ffmpeg")
        return found


def _demo_speed(recorded_seconds: float) -> float:
    """How much to speed the recording up so it fits DEMO_MAX_SECONDS; 1.0 when it already does.

    On a busy PC every browser step takes longer, so the same tour can run to twice its length in slow motion.
    """
    return max(1.0, recorded_seconds / DEMO_MAX_SECONDS)


class _ScaledMouse:
    """The real mouse, addressed in the phone view's own pixels."""

    def __init__(self, mouse, scale: float, frame):
        self.mouse = mouse
        self.scale = scale
        self.frame = frame

    def move(self, x, y, **options) -> None:
        self.mouse.move(x * self.scale, y * self.scale, **options)

    def click(self, x, y) -> None:
        self.mouse.click(x * self.scale, y * self.scale)

    def wheel(self, delta_x, delta_y) -> None:
        # A real wheel turn does not reach a site inside the enlarged frame, so the frame scrolls itself.
        self.frame.evaluate("([x, y]) => window.scrollBy(x, y)", [delta_x, delta_y])


class _PhoneView:
    """What _demo_tour needs from a page, for a site shown inside the enlarged phone-sized frame."""

    def __init__(self, page, frame):
        self.frame = frame
        self.mouse = _ScaledMouse(page.mouse, PHONE_SCALE, frame)
        self.wait_for_timeout = page.wait_for_timeout

    @property
    def url(self) -> str:
        return self.frame.url

    def evaluate(self, script):
        return self.frame.evaluate(script)


def _phone_wrapper() -> str:
    """A blank page holding one frame of PHONE_SIZE drawn PHONE_SCALE times larger: sites lay out as on a phone."""
    width, height = PHONE_SIZE
    return (
        "<html><head><style>html > #rp-demo-cursor { display: none }</style></head>"  # only the site draws the pointer
        "<body style='margin:0;overflow:hidden;background:#fff'>"
        f"<iframe id='phone' style='border:0;width:{width}px;height:{height}px;"
        f"transform:scale({PHONE_SCALE});transform-origin:0 0'></iframe></body></html>"
    )


def _demo_tour(page, deadline: float, size: tuple[int, int] = DEMO_SIZE) -> None:
    """Use the page the way a visitor would: look, press a few controls, scroll through it, come back up."""
    width, height = size
    start_url = page.url.split("#")[0]
    page.mouse.move(width * 0.78, height * 0.82)
    page.mouse.move(width * 0.5, height * 0.45, steps=30)
    page.wait_for_timeout(900)
    pressed: set[str] = set()
    for screen in range(DEMO_MAX_SCREENS):
        for target in page.evaluate(DEMO_TARGETS_JS):
            if len(pressed) >= DEMO_MAX_CLICKS or time.monotonic() > deadline - 4:
                break
            label = target["label"]
            if len(label) < 2 or label in pressed or DEMO_SKIP_LABEL.search(label):
                continue
            pressed.add(label)
            page.mouse.move(target["x"], target["y"], steps=28)
            page.wait_for_timeout(280)
            page.mouse.click(target["x"], target["y"])
            page.wait_for_timeout(1400)
            if page.url.split("#")[0] != start_url:  # the control was a link in disguise: the demo stays on the product
                return
            if screen or len(pressed) >= 2:  # two controls on the first screen, then one per screen
                break
        if screen and time.monotonic() > deadline - 3:  # the first screen always scrolls: a demo has to move
            break
        before = page.evaluate("window.scrollY")
        page.mouse.move(width * 0.5, height * 0.55, steps=12)
        for _ in range(26):
            if screen and time.monotonic() > deadline:  # on a busy PC one scroll step can stall for seconds
                break
            page.mouse.wheel(0, height * 0.8 / 26)
            page.wait_for_timeout(28)
        page.wait_for_timeout(1000)
        if page.evaluate("window.scrollY") == before:
            break
    page.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})")
    page.wait_for_timeout(1300)


def record_raw(url: str, folder: Path, phone: bool = False, served: dict[str, str] | None = None) -> tuple[Path, float, float]:
    """Record the page being used into folder. Returns (raw video, seconds of loading to cut, seconds of use).

    The browser is a throwaway profile that only reads: every request that is not a GET is blocked, so the
    recording can press buttons without ever submitting, signing up or saving anything.
    phone=True records the site at phone width (PHONE_SIZE, drawn PHONE_SCALE times larger) for the vertical videos.
    served is {address: html}: pages answered by this browser itself, for a project that has no page online
    (showcase.py builds them).
    """
    from playwright.sync_api import sync_playwright

    view_size = PHONE_SIZE if phone else DEMO_SIZE
    scale = PHONE_SCALE if phone else 1
    video_size = {"width": view_size[0] * scale, "height": view_size[1] * scale}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport=video_size, record_video_dir=str(folder), record_video_size=video_size, color_scheme="light",
        )
        context.route("**/*", lambda route: route.continue_() if route.request.method == "GET" else route.abort())
        context.add_init_script(DEMO_CURSOR_JS)
        context.on("page", lambda popup: popup.close() if len(context.pages) > 1 else None)
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.dismiss())
        for address, body in (served or {}).items():
            page.route(address, lambda route, body=body: route.fulfill(content_type="text/html; charset=utf-8", body=body))
        started = time.monotonic()
        try:
            view = page
            if phone:
                # The frame's page is served from the site's own address: framed by a page from anywhere else the
                # site is "third party", and the browser denies it localStorage (apps then show an error, not the app).
                wrapper_url = urllib.parse.urljoin(url, PHONE_WRAPPER_PATH)
                page.route(wrapper_url, lambda route: route.fulfill(content_type="text/html", body=_phone_wrapper()))
                page.goto(wrapper_url)
                frame = page.frames[1]
                response = frame.goto(url, wait_until="networkidle", timeout=45000)
                view = _PhoneView(page, frame)
            else:
                response = page.goto(url, wait_until="networkidle", timeout=45000)
            if response is None or response.status >= 400:
                raise RuntimeError(f"{url} answered HTTP {response.status if response else 'nothing'}")
            page.wait_for_timeout(1200)
            lead_in = max(time.monotonic() - started - 0.4, 0.0)  # the blank loading part, cut from the video
            tour_start = time.monotonic()
            try:
                _demo_tour(view, tour_start + DEMO_MAX_SECONDS, view_size)
            except Exception:
                pass  # a control that misbehaves ends the tour early; what was recorded so far is still a demo
            remaining = DEMO_MIN_SECONDS - (time.monotonic() - tour_start)
            if remaining > 0:
                page.wait_for_timeout(remaining * 1000)
            tour_seconds = time.monotonic() - tour_start + 0.4
            raw = Path(page.video.path())
        finally:
            context.close()  # this is what writes the recording to disk
            browser.close()
    return raw, lead_in, tour_seconds


def record_demo(url: str, output: Path, served: dict[str, str] | None = None) -> Path:
    """Record a short silent MP4 of the page being used. Raises when the page or the encoding fails."""
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reachpilot_demo_") as folder:
        raw, lead_in, tour_seconds = record_raw(url, Path(folder), served=served)
        speed = _demo_speed(tour_seconds)
        subprocess.run(
            [_ffmpeg(), "-y", "-loglevel", "error", "-ss", f"{lead_in:.2f}", "-i", str(raw),
             "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",  # silent track: some players need one
             "-vf", f"setpts=PTS/{speed:.3f},fps=30,format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "27",
             "-c:a", "aac", "-b:a", "48k", "-shortest", "-t", str(DEMO_MAX_SECONDS),  # never longer than the limit
             "-movflags", "+faststart", str(output)],
            check=True, capture_output=True, timeout=240, creationflags=CREATE_NO_WINDOW,
        )
    if output.stat().st_size < DEMO_MIN_BYTES:
        output.unlink()
        raise RuntimeError(f"the recording of {url} came out empty")
    return output

