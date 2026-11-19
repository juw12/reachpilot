"""Short vertical videos built on the demo recording: a hook in the first seconds, captions, a 'star it' end card.

Six formats for one project, all a real page being used inside a phone frame (visuals.record_raw, phone view):
  hook      a line that stops the scroll, then what the project is
  pov       "POV: you just found ...", the first-person style of user-made clips
  question  a viewer's question ("ok but does it actually work?") answered by the recording
  listicle  "3 things about <project>", one caption per feature
  speedrun  "<project> in 15 seconds", the whole tour at speed
  film      "Meet <project>.", the product in use the way a product film shows it: dark, quiet, set to music
and one for several projects (plan_roundup): "<n> projects I built", a caption per project.

The page is the project's own site when it has one; otherwise a page about the project (showcase.py), and then
the hooks that say you are watching the product run (USE_HOOKS) are left out.

Every video is rendered in two cuts: "feed" (4:5, for LinkedIn, Bluesky, Mastodon, Telegram, Discord) and
"tall" (9:16, for X and Threads). choose_format rotates the formats and learns which one earns visitors and stars.

With a voice (voiceover.py) the captions are also read aloud: narrate() gives every caption the time its line
takes to say, so a voiced video is as long as its script (the speedrun keeps its 15 seconds and is read faster
instead). The captions stay, because most people watch without sound; the end card says the voice is an AI one.

The film is the one video nobody reads aloud: it has music instead, written and played here (soundtrack.py),
and plain white captions that fade in and out over a near-black backdrop.

Honest by construction: every caption is cut from the repo's own tagline, description and README bullets (short()
only ever shortens). No invented users, quotes, numbers or results; the hooks make you curious, they never lie.
"""
from __future__ import annotations

import hashlib
import logging
import math
import random
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

import analytics
import composer
import machine
import soundtrack
import visuals
import voiceover
from github_source import CREATE_NO_WINDOW, RepoFacts

log = logging.getLogger("autopilot")

FILM = "film"  # music and no voice; drawn with its own, quieter look
FORMATS = ("hook", "pov", "question", "listicle", "speedrun", FILM)
CUTS = {"feed": (1080, 1350), "tall": (1080, 1920)}  # channels.VIDEO_CUTS mirrors this; selftest keeps them in step
SECONDS = 18
SPEEDRUN_SECONDS = 15
HOOK_SECONDS = 3.2  # the hook stays up this long; most viewers decide to stay or scroll within the first three seconds
END_SECONDS = 2.8
SHORTEST_ROW = 1.6  # a caption stays up at least this long, however quickly its line is said
VOICE_NOTE = "AI voice-over"  # on the end card of a voiced video
SIZE_BUDGET_KBIT = 72000  # sound and picture together: 9 MB, under Discord's 10 MB
MIN_SPEED = 0.8  # a short tour is slowed down at most this much; the last frame holds for the rest
CAPTION_CHARS = 84
SPEEDRUN_CAPTION_CHARS = 60
FILM_FADE = 0.45  # seconds a film caption takes to come and go
ANGLE_BOOST = 1.25
ANGLE_FIT = {
    "launch": ("hook", "pov", FILM),
    "deep_dive": ("listicle", "speedrun"),
    "feedback": ("question",),
    "behind_the_build": ("speedrun", "hook"),
    "update": ("listicle", "hook"),
}

# (line on the video, first line of the post). The post line never mentions the video: a platform may show the screenshots.
HOOKS = [
    ("Stop scrolling. Watch this first.", "Stop scrolling for a second."),
    ("You haven't seen {name} yet.", "You probably haven't seen {name} yet."),
    ("Still doing this the hard way?", "Still doing this the hard way?"),
    ("I built this so you don't have to.", "I built this so you don't have to."),
    ("Nobody asked for this. I built it anyway.", "Nobody asked for this. I built it anyway."),
]
POV_HOOKS = [
    "POV: you just found {name}",
    "POV: you try {name} for the first time",
    "POV: a side project that actually shipped",
]
# (question on the video, first line of the post, the first caption that answers it or "")
QUESTIONS = [
    ("what does it even do?", "What does {name} even do?", ""),
    ("is it really free?", "Is {name} really free?", "Yes. {license} licence, source on GitHub."),
    ("where's the code?", "Where is the code for {name}?", "All on GitHub: {github}"),
    ("ok but does it actually work?", "Does {name} actually work?", "Watch."),
]
# Lines that promise the product itself on screen; never used over a page that only describes the project.
USE_HOOKS = ("POV: you try {name} for the first time", "ok but does it actually work?")
ROUNDUP = "roundup"  # the video about several projects; not one of FORMATS, so it is never chosen for a single project
ROUNDUP_PROJECTS = 3
STAR_IT = "Star it on GitHub"

YELLOW = "#FFE14D"
INK = "#111111"
# zone: where captions sit (left, top, right, bottom). screen: the recording (left, top, width, height), kept in the
# phone view's proportions with even sizes. The tall cut keeps clear of the top and bottom strips apps cover with buttons.
LAYOUTS = {
    "feed": {"zone": (40, 28, 1040, 290), "screen": (192, 312, 696, 960), "bezel": 10, "label_y": 1311},
    "tall": {"zone": (60, 150, 1020, 510), "screen": (93, 540, 894, 1232), "bezel": 14, "label_y": 1838},
}
STYLES = {
    "hook": {"box": YELLOW, "text": INK, "size": 84, "font": "seguibl.ttf", "label": ""},
    "pov": {"box": (17, 17, 17, 225), "text": "#ffffff", "size": 78, "font": "seguibl.ttf", "label": ""},
    "question": {"box": "#ffffff", "text": INK, "size": 72, "font": "segoeuib.ttf", "label": "QUESTION"},
    "beat": {"box": "#ffffff", "text": INK, "size": 62, "font": "segoeuib.ttf", "label": ""},
    # the film: words straight on the backdrop, no box
    "film_hook": {"box": None, "text": "#ffffff", "size": 96, "font": "seguisb.ttf", "label": ""},
    "film_beat": {"box": None, "text": "#f5f5f7", "size": 58, "font": "segoeui.ttf", "label": ""},
}
FILM_BACKDROP = ("#000000", "#1d1d1f")  # near black, a little lighter towards one corner
FILM_PHONE = "#2c2c2e"  # the phone outline, just visible against it
FILM_LABEL = "#86868b"
SMALLEST_FONT = 24
MIN_WORDS = 5  # a README line shorter than this is a label or a fragment ("In a headless browser"), not a caption
# README lines that are not captions: a label before a colon, table rows, paths, env vars, links.
NOT_A_CAPTION = re.compile(r"^[^,]{1,45}: | \| |\\|%\w+%|https?://")
# ... nor caveats, setup instructions and citations: true, but not what the project is.
CAVEAT = re.compile(
    r"^(if|without|when|unless|the first time|requires?|note|make sure|you (must|need|have to))\b"
    r"|\b(not|cannot|never|must|et al|only|currently|yet|planned)\b|n't|error", re.I)
SEPARATORS = (": ", " - ", " \u2013 ", " \u2014 ")
# words a cut must not end on: "A small language with units of measure and…" reads better without the "and"
DANGLING = re.compile(r"(\s+(and|or|but|with|for|the|a|an|of|to|in|on|by|as|at|its|that|which))+$", re.I)


# ------------------------------------------------------------------ what the captions say (repo facts only)

def _clean(text: str) -> str:
    """Source text as one line without Markdown marks; captions are cut from this."""
    return " ".join(re.sub(r"[*`]", "", text or "").split())


def _sentences(text: str) -> list[str]:
    return [sentence for sentence in re.split(r"(?<=[.?!])\s+", _clean(text)) if sentence]


def _first_sentence(text: str) -> str:
    clean = _clean(text)
    sentence = (_sentences(clean) or [""])[0]
    if len(sentence) < 12:  # "e.g." and friends are not a sentence
        sentence = clean
    return sentence.rstrip(" .;:,")


def _head_and_rest(sentence: str, limit: int) -> tuple[str, str]:
    """A sentence too long for a caption, split at its first colon or dash: (the statement before it, what follows)."""
    if len(sentence) > limit:
        for separator in SEPARATORS:
            head, found, rest = sentence.partition(separator)
            if found and len(head) <= limit and len(head.split()) >= 4:  # a shorter head is a label, not a statement
                return head, rest
    return "", ""


def short(text: str, limit: int) -> str:
    """The first sentence of text, cut to fit limit. Only ever removes words; "…" marks a cut in the middle of a phrase."""
    sentence = _first_sentence(text)
    if len(sentence) <= limit:
        return sentence
    head, _ = _head_and_rest(sentence, limit)
    if head:
        return _balanced(head)
    window = sentence[: limit + 1]
    cut = max(window.rfind(", "), window.rfind("; "), window.rfind(" ("))
    words = sentence[:cut] if cut >= 25 else sentence[:limit].rsplit(" ", 1)[0]
    return _balanced(DANGLING.sub("", _balanced(words))) + "\u2026"


def _balanced(text: str) -> str:
    """Drop a bracket that the cut left open."""
    if text.count("(") > text.count(")"):
        text = text[: text.rfind("(")]
    return text.rstrip(" ,;:-")


def _unnamed(facts: RepoFacts, text: str) -> str:
    """"Kilat: a small language" already sits under a hook that names the project, so the name is dropped."""
    return re.sub(rf"^{re.escape(facts.display_name)}\s*[:\u2013\u2014-]\s+", "", _clean(text), flags=re.I)


def what_it_is(facts: RepoFacts, limit: int = CAPTION_CHARS) -> str:
    """One line saying what the project is: the start of its GitHub description, else its README tagline."""
    for source in (facts.description, facts.tagline):
        line = short(_unnamed(facts, source), limit)
        if len(line.split()) >= 3 and "http" not in line:
            return line[0].upper() + line[1:]
    return ""


def pitch_lines(facts: RepoFacts, limit: int = CAPTION_CHARS) -> list[str]:
    """The rest of the GitHub description, a caption per part: the owner's own pitch, so it goes first.

    "Find repos before they trend: real star gains, badges and a free API." gives its first half to what_it_is
    and the half after the colon to the first caption here; every later sentence is a caption of its own.
    """
    description = _unnamed(facts, facts.description)
    _, after_colon = _head_and_rest(_first_sentence(description), limit)
    lines = [short(part, limit) for part in [after_colon] + _sentences(description)[1:] if part]
    return [line[0].upper() + line[1:] for line in lines if len(line.split()) >= 2 and "http" not in line]


def readme_lines(facts: RepoFacts, limit: int = CAPTION_CHARS) -> list[str]:
    """README bullets whose first sentence fits whole and reads as a statement about the project.

    Left out: anything that would need cutting (half a sentence can say the opposite), labelled rows ("Price: ...",
    "Look (Settings): ..."), table rows, paths, sub-bullets that continue another line, caveats, instructions and
    citations; out of context they say nothing, or say something about another product in a comparison table.
    """
    lines = []
    for bullet in facts.bullets:
        line = _first_sentence(bullet)
        if (
            len(line) <= limit and len(line.split()) >= MIN_WORDS and (line[0].isupper() or line[0].isdigit())
            and not NOT_A_CAPTION.search(_clean(bullet)) and not CAVEAT.search(line) and line not in lines
        ):
            lines.append(line)
    return lines


def feature_lines(facts: RepoFacts, limit: int = CAPTION_CHARS) -> list[str]:
    """Everything the captions can say after the first line: the pitch first, then the README."""
    intro = what_it_is(facts, limit).lower()
    seen = set()
    lines = []
    for line in pitch_lines(facts, limit) + readme_lines(facts, limit):
        key = line.lower().rstrip("\u2026")
        if key not in seen and key not in intro:  # the intro may already hold the whole line ("Who threw? <this>")
            seen.add(key)
            lines.append(line)
    return lines


def available(facts: RepoFacts) -> list[str]:
    """The formats this project has enough real material for."""
    features = feature_lines(facts)
    pieces = len(features) + bool(what_it_is(facts))
    if pieces < 2:
        return []
    return [name for name in FORMATS if name != "listicle" or len(features) >= 3]


@dataclass
class Plan:
    format: str
    repo: str
    url: str
    github: str  # "github.com/<user>/<repo>", shown under the phone and on the end card
    hook: str
    post_line: str  # goes on top of the post text; "" when there is nothing safe to add
    beats: list[str]
    seconds: float
    pages: dict = field(default_factory=dict)  # {address: html} the recording browser answers itself (showcase.py)
    end: str = STAR_IT  # the title of the end card
    lengths: list[float] = field(default_factory=list)  # seconds for the hook, each caption and the end card (narrate)

    def timeline(self) -> list[tuple[str, str, float, float]]:
        """(kind, text, start, end): the hook, the captions sharing the middle, then the end card.

        A voiced video (lengths set by narrate) gives each of them the time its line takes to say.
        """
        if self.lengths:
            kinds = ["hook"] + ["beat"] * len(self.beats) + ["end"]
            rows, start = [], 0.0
            for kind, text, length in zip(kinds, [self.hook, *self.beats, self.github], self.lengths):
                rows.append((kind, text, start, start + length))
                start += length
            rows[-1] = (*rows[-1][:3], float(self.seconds))
            return rows
        middle = self.seconds - HOOK_SECONDS - END_SECONDS
        step = middle / len(self.beats)
        rows = [("hook", self.hook, 0.0, HOOK_SECONDS)]
        for index, beat in enumerate(self.beats):
            rows.append(("beat", beat, HOOK_SECONDS + index * step, HOOK_SECONDS + (index + 1) * step))
        rows.append(("end", self.github, self.seconds - END_SECONDS, float(self.seconds)))
        return rows


def plan(facts: RepoFacts, video_format: str, rng: random.Random, own_hooks: list[str] | None = None,
         url: str | None = None, shows_product: bool = True) -> Plan:
    """Write one video: the hook, the captions and the line for the post. own_hooks replaces the 'hook' lines for this repo.

    url is the page to record (the project's own site when left out). shows_product=False says that page describes
    the project instead of running it, so no line may promise a look at it working.
    """
    name = facts.display_name
    github = facts.html_url.split("//", 1)[-1].rstrip("/")
    limit = SPEEDRUN_CAPTION_CHARS if video_format in ("speedrun", FILM) else CAPTION_CHARS  # a film says little
    intro = what_it_is(facts, limit)
    features = feature_lines(facts, limit)
    pitch = [line for line in pitch_lines(facts, limit) if line in features][:2]
    rest = [line for line in features if line not in pitch]
    picked = sorted(rng.sample(range(len(rest)), min(2 - len(pitch), len(rest))))
    some = pitch + [rest[index] for index in picked]  # the owner's pitch first; README lines only fill what is left
    seconds = SECONDS
    lead: list[str] = []

    if video_format == "hook":
        if own_hooks:
            hook = post_line = rng.choice(own_hooks).replace("{name}", name)
        else:
            hook, post_line = (line.format(name=name) for line in rng.choice(HOOKS))
        lead = [f"This is {name}."]
    elif video_format == "pov":
        hook = rng.choice([line for line in POV_HOOKS if shows_product or line not in USE_HOOKS]).format(name=name)
        post_line = hook + "."
    elif video_format == "question":
        licensed = facts.license not in ("", "NOASSERTION", "Other")
        hook, post_line, answer = rng.choice([
            entry for entry in QUESTIONS
            if (licensed or "{license}" not in entry[2]) and (shows_product or entry[0] not in USE_HOOKS)
        ])
        post_line = post_line.format(name=name)
        lead = [answer.format(license=facts.license, github=github)] if answer else []
    elif video_format == "listicle":
        hook, post_line = f"3 things about {name}", f"Three things about {name}."
        intro, some = "", [f"{number}. {line}" for number, line in enumerate(features[:3], 1)]
    elif video_format == "speedrun":
        hook, post_line = f"{name} in {SPEEDRUN_SECONDS} seconds", f"{name}, start to finish."
        seconds = SPEEDRUN_SECONDS
    elif video_format == FILM:
        hook = post_line = f"Meet {name}."
    else:
        raise ValueError(f"unknown video format {video_format!r}")

    beats = [line for line in lead + [intro] + some if line][:3]
    if not beats:
        raise ValueError(f"{facts.name} has nothing to caption")
    if composer.invented_numbers(post_line, facts.source_text()):
        post_line = ""
    return Plan(video_format, facts.name, url or facts.homepage, github, hook, post_line, beats, seconds)


def plan_roundup(subjects: list[RepoFacts], user: str, url: str, pages: dict | None = None) -> Plan:
    """The video for a post about several projects: how many there are, then one caption for each of the first few."""
    beats = []
    for facts in subjects:
        line = what_it_is(facts, CAPTION_CHARS - len(facts.display_name) - 2)
        if line:
            beats.append(f"{facts.display_name}: {line}")
    if len(subjects) < 2 or not beats:
        raise ValueError("a roundup video needs several projects with something to caption")
    return Plan(
        ROUNDUP, ROUNDUP, url, f"github.com/{user}", f"{len(subjects)} projects I built", "",
        beats[:ROUNDUP_PROJECTS], SECONDS, pages or {}, "See them all on GitHub",
    )


# ------------------------------------------------------------------ the voice (voiceover.py reads the captions)

def spoken(plan_: Plan) -> list[str]:
    """What the voice says while the hook, each caption and the end card are up: the same words, made speakable."""
    return [voiceover.speakable(line) for line in (plan_.hook, *plan_.beats, plan_.end)]


def narrate(plan_: Plan, say, track: Path) -> Path:
    """Read the video aloud into track and time the plan to the voice (sets plan_.lengths and plan_.seconds).

    Each caption stays up for as long as its line takes plus a breath. The speedrun promises its length on screen,
    so there the lines are read faster until they fit and the time left over is shared out.
    Raises, leaving the plan as it was, when the voice fails, takes longer than voiceover.PATIENCE or the speedrun
    cannot be said in time.
    """
    lines = spoken(plan_)
    until = voiceover.deadline()

    def row_lengths(clips: list[bytes]) -> list[float]:
        return [max(SHORTEST_ROW, voiceover.seconds(clip) + voiceover.BREATH) for clip in clips]

    clips = voiceover.read(lines, say, until=until)
    lengths = row_lengths(clips)
    if plan_.format == "speedrun":
        over = sum(lengths) - plan_.seconds
        if over > 0:
            said = sum(voiceover.seconds(clip) for clip in clips)
            clips = voiceover.read(lines, say, room=said - over, until=until)
            lengths = row_lengths(clips)
        spare = plan_.seconds - sum(lengths)
        if spare < 0:
            raise ValueError(f"the captions cannot be said in {plan_.seconds} seconds")
        for_end = min(spare, max(0.0, END_SECONDS - lengths[-1]))
        share = (spare - for_end) / (len(lengths) - 1)
        lengths = [length + share for length in lengths[:-1]] + [lengths[-1] + for_end]
        total = float(plan_.seconds)
    else:
        lengths[-1] = max(lengths[-1], END_SECONDS)
        total = round(sum(lengths), 2)
    starts = [sum(lengths[:index]) for index in range(len(lengths))]
    voiceover.write_track(clips, starts, total, track)
    plan_.lengths, plan_.seconds = lengths, total
    return track


def plain_narrator(subjects: list[RepoFacts], say):
    """For the plain recording, which has no captions: narrate(track) -> seconds of speech written, 0.0 when none.

    One project: its name, what it is, where to star it. Several: how many there are and where to find them.
    """
    if len(subjects) == 1:
        facts = subjects[0]
        lines = [f"This is {facts.display_name}.", what_it_is(facts), f"{STAR_IT}."]
    else:
        lines = [f"{len(subjects)} projects I built.", "See them all on GitHub."]

    def narrate_plain(track: Path) -> float:
        try:
            return voiceover.narration(lines, say, track)
        except Exception as error:
            log.warning("Voice-over failed (%s); this video stays silent", error)
            return 0.0

    return narrate_plain


# ------------------------------------------------------------------ which format next (rotation that learns)

def choose_format(enabled, facts: RepoFacts, angle: str, past: list[tuple[str, str]], records: list[dict],
                  rng: random.Random) -> str | None:
    """Pick the format for this post. past is [(repo, format)], newest first.

    Never the format of the previous video, nor the one this project had last time (while others remain); among
    the rest, Thompson sampling on what each format earned, with a nudge for the formats that suit the post's angle.
    """
    candidates = [name for name in available(facts) if name in enabled]
    if not candidates:
        return None
    last_overall = past[0][1] if past else None
    last_here = next((used for repo, used in past if repo == facts.name), None)
    fresh = [name for name in candidates if name not in (last_overall, last_here)]
    scorer = analytics.ThompsonScorer(records, "format")
    fits = ANGLE_FIT.get(angle, ())
    return max(fresh or candidates, key=lambda name: scorer.sample(name, rng) * (ANGLE_BOOST if name in fits else 1.0))


def history(db) -> list[tuple[str, str]]:
    """(repo, format) of every post that got a styled video, newest first."""
    return db.execute("SELECT repo, video_format FROM posts WHERE video_format IS NOT NULL ORDER BY slot DESC").fetchall()


def format_records(db) -> list[dict]:
    """analytics.performance records for the posts that carried a styled video, each tagged with its format."""
    formats = {
        (repo, (posted_at or "")[:10]): video_format
        for repo, posted_at, video_format in db.execute(
            "SELECT repo, posted_at, video_format FROM posts WHERE status='posted' AND video_format IS NOT NULL"
        )
    }
    return [
        dict(record, format=formats[(record["repo"], record["date"])])
        for record in analytics.performance(db)
        if (record["repo"], record["date"]) in formats
    ]


# ------------------------------------------------------------------ drawing (Pillow)

def _font(name: str, size: int):
    for candidate in (name, "segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(str(machine.font_path(candidate)), size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)  # no Windows fonts (CI, Linux): Pillow's built-in scalable font
    except TypeError:
        return ImageFont.load_default()


def _wrap(text: str, font, width: int) -> list[str]:
    lines: list[str] = []
    for word in text.split():
        if lines and font.getlength(f"{lines[-1]} {word}") <= width:
            lines[-1] += " " + word
        else:
            lines.append(word)
    return lines


def caption_image(text: str, cut: str, style: str) -> tuple[Image.Image, tuple[int, int]]:
    """One caption as a rounded box, in the largest type that fits the cut's caption zone. Returns (image, top-left)."""
    left, top, right, bottom = LAYOUTS[cut]["zone"]
    zone_width, zone_height = right - left, bottom - top
    look = STYLES[style]
    pad_x, pad_y = 36, 22
    label_height = 40 if look["label"] else 0
    for size in range(look["size"], SMALLEST_FONT - 1, -4):
        font = _font(look["font"], size)
        lines = _wrap(text, font, zone_width - 2 * pad_x)
        line_height = int(size * 1.2)
        box_width = int(max(font.getlength(line) for line in lines)) + 2 * pad_x
        box_height = len(lines) * line_height + 2 * pad_y + label_height
        if box_width <= zone_width and box_height <= zone_height:
            break
    box_width, box_height = min(box_width, zone_width), min(box_height, zone_height)
    image = Image.new("RGBA", (box_width, box_height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    if look["box"]:
        draw.rounded_rectangle((0, 0, box_width - 1, box_height - 1), radius=28, fill=look["box"])
    if look["label"]:
        draw.text((pad_x, pad_y - 4), look["label"], font=_font("segoeuib.ttf", 26), fill="#6b7280")
    for index, line in enumerate(lines):
        x = (box_width - font.getlength(line)) / 2
        draw.text((x, pad_y + label_height + index * line_height), line, font=font, fill=look["text"])
    return image, (left + (zone_width - box_width) // 2, top + (zone_height - box_height) // 2)


def frame_image(cut: str, repo: str, github: str, film: bool = False) -> Image.Image:
    """The backdrop: the project's colours, a phone outline with a see-through screen, and the repo address under it.

    film: the same for every project instead, near black, with the address in grey.
    """
    width, height = CUTS[cut]
    layout = LAYOUTS[cut]
    screen_left, screen_top, screen_width, screen_height = layout["screen"]
    bezel = layout["bezel"]
    dark, light = visuals.PALETTES[int(hashlib.md5(repo.encode()).hexdigest(), 16) % len(visuals.PALETTES)]
    if film:
        dark, light = FILM_BACKDROP
    gradient = Image.linear_gradient("L").resize((width, height)).rotate(-30)
    canvas = Image.composite(Image.new("RGB", (width, height), light), Image.new("RGB", (width, height), dark), gradient)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (screen_left - bezel, screen_top - bezel, screen_left + screen_width + bezel, screen_top + screen_height + bezel),
        radius=30 + bezel, fill=FILM_PHONE if film else "#0b0b0f",
    )
    font = _font("segoeui.ttf" if film else "segoeuib.ttf", 32)
    draw.text(
        ((width - font.getlength(github)) / 2, layout["label_y"] - 20), github, font=font,
        fill=FILM_LABEL if film else "#ffffff",
    )
    scale = 3  # the screen's rounded corners are drawn large and scaled down, so their edge is smooth
    hole = Image.new("L", (width * scale, height * scale), 255)
    ImageDraw.Draw(hole).rounded_rectangle(
        (screen_left * scale, screen_top * scale, (screen_left + screen_width) * scale, (screen_top + screen_height) * scale),
        radius=30 * scale, fill=0,
    )
    canvas = canvas.convert("RGBA")
    canvas.putalpha(hole.resize((width, height), Image.LANCZOS))
    return canvas


def end_image(cut: str, github: str, title: str = STAR_IT, note: str = "") -> Image.Image:
    """The last seconds: the recording dims and the one thing to do next is spelled out. note: small print below."""
    width, height = CUTS[cut]
    image = Image.new("RGBA", (width, height), (8, 10, 18, 205))
    draw = ImageDraw.Draw(image)
    centre_x, centre_y = width // 2, height // 2
    points = []
    for index in range(10):
        radius = 120 if index % 2 == 0 else 50
        angle = math.pi * index / 5 - math.pi / 2
        points.append((centre_x + radius * math.cos(angle), centre_y - 230 + radius * math.sin(angle)))
    draw.polygon(points, fill=YELLOW)
    for title_size in range(84, 50, -4):  # a longer title ("See them all on GitHub") gets a smaller size
        title_font = _font("seguibl.ttf", title_size)
        if title_font.getlength(title) <= width - 120:
            break
    draw.text(((width - title_font.getlength(title)) / 2, centre_y - 70), title, font=title_font, fill="#ffffff")
    for size in range(46, SMALLEST_FONT - 1, -2):
        link_font = _font("segoeuib.ttf", size)
        if link_font.getlength(github) <= width - 200:
            break
    link_width = link_font.getlength(github)
    draw.rounded_rectangle(
        (centre_x - link_width / 2 - 40, centre_y + 70, centre_x + link_width / 2 + 40, centre_y + 70 + size + 44),
        radius=(size + 44) // 2, fill="#ffffff",
    )
    draw.text((centre_x - link_width / 2, centre_y + 70 + 16), github, font=link_font, fill=INK)
    if note:
        note_font = _font("segoeui.ttf", 28)
        draw.text(((width - note_font.getlength(note)) / 2, centre_y + 70 + size + 44 + 48), note, font=note_font, fill="#d1d5db")
    return image


# ------------------------------------------------------------------ rendering (one ffmpeg run per cut)

def ffmpeg_args(ffmpeg: str, raw: Path, lead_in: float, speed: float, cut: str, seconds: float, frame: Path,
                overlays: list[tuple[Path, int, int, float, float]], bar: Path, output: Path,
                track: Path | None = None, music: bool = False, fade: float = 0.0) -> list[str]:
    """The ffmpeg command for one cut. overlays: (png, x, y, from second, to second).

    track: the sound, else silence; music says it is the film's soundtrack and not a voice.
    fade: seconds each overlay takes to come and go (0 cuts). The first is there from the first frame, which is
    the thumbnail, and the last stays to the end.
    """
    width, height = CUTS[cut]
    screen_left, screen_top, screen_width, screen_height = LAYOUTS[cut]["screen"]
    sound_kbit = 96
    if track:
        source = soundtrack if music else voiceover
        sound = ["-i", str(track)]
        sound_out = ["-af", source.AUDIO_FILTER, "-c:a", "aac", "-b:a", source.AUDIO_BITRATE, "-ac", "2", "-ar", "44100"]
        sound_kbit = max(sound_kbit, int(source.AUDIO_BITRATE.rstrip("k")))
    else:
        sound = ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"]
        sound_out = ["-c:a", "aac", "-b:a", "48k"]
    picture_rate = min(3500, int(SIZE_BUDGET_KBIT / seconds) - sound_kbit)  # a longer video gets fewer bits a second, not more megabytes
    command = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{lead_in:.2f}", "-i", str(raw), *sound, "-i", str(frame)]
    for picture, *_ in overlays:
        command += ["-loop", "1", "-framerate", "30", "-i", str(picture)] if fade else ["-i", str(picture)]
    command += ["-i", str(bar)]
    steps = [
        f"[0:v]setpts=PTS/{speed:.3f},fps=30,scale={screen_width}:{screen_height}:flags=lanczos,"
        f"tpad=stop_mode=clone:stop_duration={seconds},"  # a tour shorter than the video holds its last frame
        f"pad={width}:{height}:{screen_left}:{screen_top}:color=black[base]",
        "[base][2:v]overlay=0:0[v0]",
    ]
    for index, (_, x, y, start, end) in enumerate(overlays):
        picture = f"[{index + 3}:v]"
        if fade:
            fades = ["format=rgba"]
            if index:
                fades.append(f"fade=t=in:st={start:.2f}:d={fade}:alpha=1")
            if index < len(overlays) - 1:
                fades.append(f"fade=t=out:st={end - fade:.2f}:d={fade}:alpha=1")
            steps.append(f"{picture}{','.join(fades)}[c{index}]")
            picture = f"[c{index}]"
        steps.append(f"[v{index}]{picture}overlay={x}:{y}:enable='between(t,{start:.2f},{end:.2f})'[v{index + 1}]")
    last = len(overlays)
    steps.append(f"[v{last}][{last + 3}:v]overlay=x='-w+w*t/{seconds}':y=0[bar]")  # a bar that fills as the video plays
    steps.append("[bar]format=yuv420p[v]")
    command += [
        "-filter_complex", ";".join(steps), "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "25", "-maxrate", f"{picture_rate}k", "-bufsize", f"{2 * picture_rate}k",
        *sound_out, "-t", str(seconds), "-movflags", "+faststart", str(output),
    ]
    return command


def render(plan_: Plan, cut: str, raw: Path, lead_in: float, speed: float, work: Path, output: Path,
           track: Path | None = None) -> Path:
    """Draw this cut's overlays and encode it, with the track (the voice; for the film, its music) when there is one.

    Raises when the encoding fails or comes out empty.
    """
    width, _ = CUTS[cut]
    film = plan_.format == FILM
    frame = work / f"{cut}_frame.png"
    frame_image(cut, plan_.repo, plan_.github, film).save(frame)
    overlays = []
    for index, (kind, text, start, end) in enumerate(plan_.timeline()):
        picture = work / f"{cut}_{index}.png"
        if kind == "end":
            image, (x, y) = end_image(cut, text, plan_.end, VOICE_NOTE if track and not film else ""), (0, 0)
        elif film:
            image, (x, y) = caption_image(text, cut, f"film_{kind}")
        else:
            style = (plan_.format if plan_.format in ("pov", "question") else "hook") if kind == "hook" else "beat"
            image, (x, y) = caption_image(text, cut, style)
        image.save(picture)
        overlays.append((picture, x, y, start, end))
    bar = work / f"{cut}_bar.png"
    Image.new("RGBA", (width, 6 if film else 10), "#ffffff" if film else YELLOW).save(bar)
    subprocess.run(
        ffmpeg_args(
            visuals._ffmpeg(), raw, lead_in, speed, cut, plan_.seconds, frame, overlays, bar, output, track,
            music=film, fade=FILM_FADE if film else 0.0,
        ),
        check=True, capture_output=True, timeout=400, creationflags=CREATE_NO_WINDOW,
    )
    if output.stat().st_size < visuals.DEMO_MIN_BYTES:
        raise RuntimeError(f"the {cut} video of {plan_.url} came out empty")
    return output


def record(plan_: Plan, out_dir: Path, say=None, music: bool = True) -> list[Path]:
    """Record the page once and render every cut: [<repo>_demo_<format>_feed.mp4, ..._tall.mp4]. All or nothing.

    say (voiceover.load) reads the captions aloud; a voice that fails or is too slow costs the sound, never the video.
    The film is never read aloud: it gets its music (soundtrack.py) unless music is False, on the same terms.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs = [out_dir / f"{plan_.repo}_demo_{plan_.format}_{cut}.mp4" for cut in CUTS]
    try:
        with tempfile.TemporaryDirectory(prefix="reachpilot_reel_") as folder:
            track = None
            if plan_.format == FILM:
                if music:
                    try:
                        track = soundtrack.write(
                            plan_.seconds, plan_.seconds - END_SECONDS, plan_.repo, Path(folder) / "music.wav",
                        )
                    except Exception as error:
                        log.warning("Soundtrack failed (%s); this video stays silent", error)
            elif say:
                try:
                    track = narrate(plan_, say, Path(folder) / "voice.wav")
                except Exception as error:
                    log.warning("Voice-over failed (%s); this video stays silent", error)
            raw, lead_in, tour_seconds = visuals.record_raw(plan_.url, Path(folder), phone=True, served=plan_.pages)
            speed = max(MIN_SPEED, tour_seconds / plan_.seconds)
            for cut, output in zip(CUTS, outputs):
                render(plan_, cut, raw, lead_in, speed, Path(folder), output, track)
    except Exception:
        for output in outputs:
            output.unlink(missing_ok=True)
        raise
    return outputs
