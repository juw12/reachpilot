"""A spoken voice for the videos: the captions read aloud by a neural voice that runs on this PC.

The voice is Kokoro (kokoro-onnx, Apache 2.0): no account, no cloud, nothing leaves the machine. It is optional.
Without the package or its two model files the videos stay silent, exactly as before:
    pip install kokoro-onnx
    put kokoro-v1.0.onnx and voices-v1.0.bin in models/ next to this file (or name their folder in "voice_models")
config.json: "voice_over": false switches it off; "video_voice" picks the voice, one name ("af_heart") or a blend
({"am_michael": 0.7, "am_fenrir": 0.3}).

The voice only says what the video shows: speakable() turns a caption into a sentence a voice can read (no web
addresses spelled out, "POV" letter by letter) and never adds a word of its own.

Sound is passed around as 16-bit mono samples at 24 kHz in a bytes object, so everything except the voice itself
is plain Python and is tested without the model.
"""
from __future__ import annotations

import importlib.util
import logging
import re
import unicodedata
import wave
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("autopilot")

MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
MODEL_FOLDERS = (ROOT / "models", ROOT / "modules" / "podcast" / "models")
DEFAULT_VOICE = {"am_michael": 0.7, "am_fenrir": 0.3}
SAMPLE_RATE = 24000
LEAD = 0.1  # a line starts this long after its caption appears
BREATH = 0.4  # silence a spoken line needs around it (the lead included), so lines do not run into each other
FASTEST = 1.3  # a line is never read faster than this; past it a voice sounds rushed
SHORTEST_CLIP = 0.2  # a "spoken" line shorter than this is the voice failing, not speech
PEAK = 0.9
AUDIO_BITRATE = "96k"
# The clips come out near -19 LUFS; phones want it louder. The limiter keeps the raised peaks from clipping.
AUDIO_FILTER = "volume=3.5dB,alimiter=limit=0.89:level=disabled"

ADDRESS = re.compile(r"https?://\S+|\b(?:[\w-]+\.)+[a-z]{2,}/\S*", re.I)
REWORDED = [
    (re.compile(r"\bPOV\b:?"), "P.O.V.,"),  # read as a word otherwise
    (re.compile(r"\be\.g\.,?", re.I), "for example,"),
    (re.compile(r"\bi\.e\.,?", re.I), "that is,"),
    (re.compile(r"(?<=\w)\s*(->|\u2192)\s*(?=\w)"), " to "),
    (re.compile(r"->|\u2192"), " "),  # an arrow that joins nothing is decoration
    (re.compile(r"\s+&\s+"), " and "),
    (re.compile(r"\s*%"), " percent"),
    (re.compile(r"(?<=\d)x\b"), " times"),
    (re.compile(r"\s+[-\u2013\u2014]+\s+"), ", "),
    (re.compile(r"\s+/\s+"), ", "),  # "Claude Code / Codex" is a list, not the word "slash"
    (re.compile(r"(?<=\w)/(?=\w)"), " "),
    (re.compile(r"\u2026"), "."),  # the mark of a caption cut short
    (re.compile(r"\(\s*\)"), ""),  # brackets an address was taken out of
]


def speakable(text: str) -> str:
    """The caption as a sentence a voice can read; "" when nothing in it can be said.

    Only ever removes or respells: Markdown marks, symbols and web addresses go (the address is on screen),
    abbreviations a voice trips over are spelled out, and the line ends like a sentence.
    """
    # "->" is kept as an arrow before the Markdown marks go, or its ">" is taken for a quote mark
    line = ADDRESS.sub("", re.sub(r"[*`_#>|]", " ", (text or "").replace("->", "\u2192")))
    for pattern, spoken in REWORDED:
        line = pattern.sub(spoken, line)
    line = "".join(character for character in line if unicodedata.category(character) not in ("So", "Sk", "Cs"))
    line = re.sub(r"\s+([,.!?])", r"\1", " ".join(line.split())).rstrip(" :,;-\u2013\u2014(")
    if not any(character.isalnum() for character in line):
        return ""
    return line if line[-1] in ".!?" else line + "."


def seconds(clip: bytes) -> float:
    return len(clip) / (2 * SAMPLE_RATE)


def read(lines: list[str], say, room: float | None = None) -> list[bytes]:
    """Every line as a clip of speech (an empty line gives an empty clip).

    room is how many seconds all the speech together may take. When it takes longer the lines are read again,
    just fast enough to fit; ValueError when that would be faster than FASTEST or it still does not fit.
    """
    def clips_at(speed: float) -> list[bytes]:
        clips = []
        for line in lines:
            clip = say(line, speed) if line else b""
            if line and seconds(clip) < SHORTEST_CLIP:
                raise ValueError(f"the voice gave no sound for {line!r}")
            clips.append(clip)
        return clips

    clips = clips_at(1.0)
    spoken = sum(seconds(clip) for clip in clips)
    if room is None or spoken <= room:
        return clips
    speed = spoken / room * 1.03 if room > 0 else FASTEST + 1
    if speed > FASTEST:
        raise ValueError(f"{spoken:.1f} s of speech does not fit in {room:.1f} s")
    clips = clips_at(speed)
    if sum(seconds(clip) for clip in clips) > room:
        raise ValueError(f"the speech still does not fit in {room:.1f} s at {speed:.2f}x")
    return clips


def write_track(clips: list[bytes], starts: list[float], total: float, path: Path) -> Path:
    """One WAV of total seconds with each clip placed LEAD after its start; whatever runs past the end is cut."""
    frames = bytearray(int(round(total * SAMPLE_RATE)) * 2)
    for clip, start in zip(clips, starts):
        at = int(round((start + LEAD) * SAMPLE_RATE)) * 2
        clip = clip[: max(0, len(frames) - at)]
        frames[at: at + len(clip)] = clip
    with wave.open(str(path), "wb") as track:
        track.setnchannels(1)
        track.setsampwidth(2)
        track.setframerate(SAMPLE_RATE)
        track.writeframes(bytes(frames))
    return path


def narration(lines: list[str], say, path: Path) -> float:
    """Read the lines one after another into a WAV; returns its length in seconds. For a video without captions."""
    lines = [line for line in map(speakable, lines) if line]
    clips = read(lines, say)
    if not clips:
        raise ValueError("nothing to say")
    starts, total = [], 0.0
    for clip in clips:
        starts.append(total)
        total += seconds(clip) + BREATH
    write_track(clips, starts, total, path)
    return total


def model_folder(config) -> Path | None:
    """The folder that holds both model files: "voice_models" in config.json, else models/, else the podcast module's."""
    named = config.get("voice_models")
    for folder in ([Path(named)] if named else []) + list(MODEL_FOLDERS):
        if all((folder / name).is_file() for name in MODEL_FILES):
            return folder
    return None


def load(config):
    """say(text, speed=1.0) -> clip, or None when the videos stay silent (switched off, or the voice is not installed).

    Cheap to call: the model is only loaded the first time something is said.
    """
    if not config.get("voice_over", True):
        return None
    if importlib.util.find_spec("kokoro_onnx") is None or importlib.util.find_spec("numpy") is None:
        log.info("No voice-over: kokoro-onnx is not installed (pip install kokoro-onnx)")
        return None
    folder = model_folder(config)
    if folder is None:
        log.info("No voice-over: %s not found in %s", " and ".join(MODEL_FILES), MODEL_FOLDERS[0])
        return None
    voice = config.get("video_voice") or DEFAULT_VOICE

    @lru_cache(maxsize=1)
    def engine_and_style():
        from kokoro_onnx import Kokoro

        engine = Kokoro(str(folder / MODEL_FILES[0]), str(folder / MODEL_FILES[1]))
        blend = {voice: 1.0} if isinstance(voice, str) else voice
        total = sum(blend.values())
        return engine, sum(engine.get_voice_style(name) * (weight / total) for name, weight in blend.items())

    def say(text: str, speed: float = 1.0) -> bytes:
        import numpy

        engine, style = engine_and_style()
        samples, rate = engine.create(text, voice=style, speed=speed, lang="en-us")
        if rate != SAMPLE_RATE:
            raise RuntimeError(f"the voice speaks at {rate} Hz, expected {SAMPLE_RATE}")
        samples = numpy.asarray(samples, dtype="float32").reshape(-1)
        loudest = float(numpy.abs(samples).max()) if samples.size else 0.0
        if loudest < 1e-4:
            return b""
        heard = numpy.flatnonzero(numpy.abs(samples) > 0.02 * loudest)  # drop the silence the model adds at both ends
        pad = SAMPLE_RATE // 50
        samples = samples[max(0, heard[0] - pad): heard[-1] + pad]
        return (samples * (PEAK / loudest) * 32767).astype("<i2").tobytes()

    return say
