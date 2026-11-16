"""Music for the product film (reels.py, the "film" format): a short calm piece written and played right here.

Nothing is downloaded and nothing is licensed: the notes are chosen by score() and turned into sound by render(),
so the music is the project's own and no platform can mute or claim it. It is the kind of bed a product film
has: soft mallet notes over a warm pad, a quiet pulse that comes in after the opening line, and a last chord
that lands on the end card and rings out.

Each project gets its own key, chord order and pattern (from its name), and always the same ones, so a project
sounds like itself from one video to the next. The piece is written to the video: its bars end exactly where the
end card starts.

score() is plain Python. render() needs numpy; without it the film stays silent (available() says which).
config.json: "video_music": false switches the music off.
"""
from __future__ import annotations

import hashlib
import importlib.util
import random
import wave
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 44100
AUDIO_BITRATE = "128k"
AUDIO_FILTER = "alimiter=limit=0.89:level=disabled"  # the level is set in master(); this only catches a stray peak

BAR_SECONDS = 2.4  # four beats at 100 a minute; the real bar is the nearest length that divides the video evenly
BEATS = 4
MAJOR = (0, 2, 4, 5, 7, 9, 11)
KEYS = (46, 48, 50, 53, 55)  # the note the piece is in: B flat, C, D, F, G below middle C
# One chord a bar, as steps of the scale (0 is the home chord). All four orders are the familiar, hopeful ones.
PROGRESSIONS = ((0, 4, 5, 3), (0, 5, 3, 4), (3, 0, 4, 5), (5, 3, 0, 4))
# The mallet's eight notes a bar, as steps above the chord's own note (2 its third, 4 its fifth, 7 the octave).
PATTERNS = ((0, 4, 7, 4, 8, 4, 7, 4), (0, 2, 4, 7, 4, 2, 4, 8), (7, 4, 2, 4, 8, 4, 2, 0))
PAD_STEPS = (0, 4, 9, 8)  # the chord, spread wide: its note, the fifth, the third an octave up, and the ninth for colour
LAST_CHORD = (0, 4, 7, 8, 9, 11)  # rolled upwards on the end card
ROLL = 0.07  # seconds between the notes of that roll
PULSE_FROM_BAR = 2  # the opening line plays over the pad and a few notes; the pulse waits for the first caption
FADE_OUT = 1.4
LOUDNESS = 0.13  # the root mean square the finished piece is brought to; measures about -15 LUFS
PEAK = 0.84
LEVELS = {"pad": 0.085, "mallet": 0.30, "bell": 0.16, "bass": 0.26, "kick": 0.55, "hat": 0.035}
REVERB = {"pad": 0.5, "mallet": 0.38, "bell": 0.6}  # how much of each voice goes through the room
REVERB_SECONDS = 2.2


@dataclass(frozen=True)
class Note:
    start: float  # seconds from the start of the video
    length: float
    pitch: int  # MIDI number: 60 is middle C
    voice: str  # one of LEVELS
    level: float = 1.0
    pan: float = 0.0  # -1 left, 1 right


def step(key: int, steps: int) -> int:
    """The pitch that many steps of the major scale above the key note."""
    return key + MAJOR[steps % 7] + 12 * (steps // 7)


def bar_seconds(end_at: float) -> tuple[int, float]:
    """(bars before the end card, seconds in each): as near BAR_SECONDS as divides the time evenly."""
    bars = max(2, round(end_at / BAR_SECONDS))
    return bars, end_at / bars


def score(seconds: float, end_at: float, seed: str) -> list[Note]:
    """The piece for a video of that length whose end card starts at end_at. The same seed gives the same piece."""
    if not 0 < end_at < seconds:
        raise ValueError("the end card has to start inside the video")
    rng = random.Random(int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16))
    key = rng.choice(KEYS)
    chords = rng.choice(PROGRESSIONS)
    pattern = rng.choice(PATTERNS)
    bars, bar = bar_seconds(end_at)
    beat = bar / BEATS
    notes: list[Note] = []
    for index in range(bars):
        start = index * bar
        chord = chords[index % len(chords)]
        for voice_index, above in enumerate(PAD_STEPS):
            notes.append(Note(start, bar, step(key + 12, chord + above), "pad", pan=(-0.5, 0.5, -0.25, 0.25)[voice_index]))
        every = 2 if index == 0 else 1  # the first bar only hints at the pattern
        for eighth in range(0, 2 * BEATS, every):
            accent = 1.0 if eighth % 4 == 0 else 0.72 if eighth % 2 == 0 else 0.55
            notes.append(Note(
                start + eighth * beat / 2, beat, step(key + 24, chord + pattern[eighth]), "mallet",
                accent, pan=0.35 if eighth % 2 else -0.35,
            ))
        if index >= 1:
            notes.append(Note(start, 1.5 * beat, step(key - 12, chord), "bass"))
            notes.append(Note(start + 2.5 * beat, beat, step(key - 12, chord), "bass", 0.7))
        if index >= PULSE_FROM_BAR:
            notes.append(Note(start, 0.3, 0, "kick"))
            notes.append(Note(start + 2 * beat, 0.3, 0, "kick", 0.8))
            for offbeat in range(BEATS):
                notes.append(Note(start + (offbeat + 0.5) * beat, 0.08, 0, "hat", 1.0 if offbeat % 2 else 0.7))
    # The end card: the home chord, struck once and left to ring.
    ring = seconds - end_at
    for above in PAD_STEPS:
        notes.append(Note(end_at, ring, step(key + 12, above), "pad"))
    for index, above in enumerate(LAST_CHORD):
        notes.append(Note(end_at + index * ROLL, ring, step(key + 24, above), "mallet", 0.9, pan=-0.4 + 0.16 * index))
    notes.append(Note(end_at + len(LAST_CHORD) * ROLL, ring, step(key + 36, 4), "bell", pan=0.2))
    notes.append(Note(end_at, ring, step(key - 12, 0), "bass"))
    notes.append(Note(end_at, 0.3, 0, "kick", 0.9))
    return notes


def available() -> bool:
    """True when the music can be played here (render needs numpy)."""
    return importlib.util.find_spec("numpy") is not None


def frequency(pitch: int) -> float:
    return 440.0 * 2 ** ((pitch - 69) / 12)


def _voice(note: Note, numpy, noise):
    """One note as (left, right) samples, at full level and without the room."""
    rate = SAMPLE_RATE
    hertz = frequency(note.pitch)
    if note.voice == "pad":  # a few soft overtones, slightly out of tune between the ears, slow to come and go
        attack, release = 0.4, 0.7
        time = numpy.arange(int((note.length + release) * rate)) / rate
        envelope = numpy.minimum(time / attack, 1.0) * numpy.clip((note.length + release - time) / release, 0.0, 1.0)

        def tone(cents: float):
            base = hertz * 2 ** (cents / 1200)
            return sum(numpy.sin(2 * numpy.pi * base * partial * time) / partial ** 2 for partial in (1, 2, 3, 4))

        return (tone(-5) + tone(2)) * envelope, (tone(5) + tone(-2)) * envelope
    if note.voice in ("mallet", "bell"):  # struck: every overtone dies away, the high ones first
        # (overtone, level, seconds to fall to about a third); the last overtone of each is off the scale, as on real bars
        overtones = ((1, 1.0, 0.55), (2, 0.35, 0.28), (3, 0.12, 0.16), (4.2, 0.07, 0.05)) if note.voice == "mallet" \
            else ((1, 1.0, 1.1), (2.76, 0.3, 0.6), (5.4, 0.1, 0.25))
        time = numpy.arange(int(min(note.length + 0.4, 6 * overtones[0][2]) * rate)) / rate
        sound = sum(level * numpy.sin(2 * numpy.pi * hertz * overtone * time) * numpy.exp(-time / fall)
                    for overtone, level, fall in overtones)
        sound = sound * numpy.minimum(time / 0.004, 1.0)
        return sound, sound
    if note.voice == "bass":  # overtones added so it is still heard on a phone's small speaker
        time = numpy.arange(int((note.length + 0.08) * rate)) / rate
        sound = sum(level * numpy.sin(2 * numpy.pi * hertz * overtone * time) for overtone, level in ((1, 1.0), (2, 0.45), (3, 0.2)))
        envelope = numpy.minimum(time / 0.012, 1.0) * numpy.exp(-time / 0.9) * numpy.clip((note.length + 0.08 - time) / 0.08, 0.0, 1.0)
        return sound * envelope, sound * envelope
    if note.voice == "kick":  # a soft thump: a tone that drops quickly in pitch
        time = numpy.arange(int(0.3 * rate)) / rate
        phase = 2 * numpy.pi * (48 * time + 85 * 0.035 * (1 - numpy.exp(-time / 0.035)))
        sound = numpy.sin(phase) * numpy.exp(-time / 0.11) * numpy.minimum(time / 0.002, 1.0)
        return sound, sound
    if note.voice == "hat":  # a breath of hiss
        time = numpy.arange(int(0.08 * rate)) / rate
        hiss = numpy.diff(noise.standard_normal(time.size + 1))
        sound = hiss * numpy.exp(-time / 0.018)
        return sound, sound
    raise ValueError(f"unknown voice {note.voice!r}")


def _room(numpy, noise):
    """What a clap sounds like in the room, left and right: hiss that dies away, the highs first."""
    time = numpy.arange(int(REVERB_SECONDS * SAMPLE_RATE)) / SAMPLE_RATE
    sides = []
    for _ in range(2):
        echo = noise.standard_normal(time.size) * numpy.exp(-time * 6.9 / REVERB_SECONDS)
        echo[: int(0.012 * SAMPLE_RATE)] = 0.0  # the room answers a moment after the note
        smooth = numpy.cumsum(numpy.concatenate(([0.0], echo)))
        echo = (smooth[10:] - smooth[:-10]) / 10  # averaged over ten samples: the room swallows the highs
        sides.append(echo / numpy.sqrt(numpy.sum(echo ** 2)))
    return sides


def _through(sound, echo, numpy):
    """The sound as the room answers it (convolution, done in one pass in the frequency domain)."""
    size = 1 << (sound.size + echo.size).bit_length()
    return numpy.fft.irfft(numpy.fft.rfft(sound, size) * numpy.fft.rfft(echo, size), size)[: sound.size]


def master(mix, seconds: float, numpy):
    """The finished piece: brought to LOUDNESS, never over PEAK, in from nothing and out with FADE_OUT."""
    loud = float(numpy.sqrt(numpy.mean(mix ** 2)))
    if loud < 1e-6:
        raise ValueError("the piece came out silent")
    mix = mix * (LOUDNESS / loud)
    mix = numpy.tanh(mix / PEAK) * PEAK  # the few peaks above the rest are rounded off, not cut
    time = numpy.arange(mix.shape[0]) / SAMPLE_RATE
    fade = numpy.minimum(time / 0.03, 1.0) * numpy.clip((seconds - time) / FADE_OUT, 0.0, 1.0) ** 2
    return mix * fade[:, None]


def render(notes: list[Note], seconds: float, seed: str):
    """The piece as samples: an array of seconds * SAMPLE_RATE rows, left and right, between -1 and 1."""
    import numpy

    noise = numpy.random.default_rng(int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:8], 16))
    frames = int(round(seconds * SAMPLE_RATE))
    dry = numpy.zeros((frames, 2))
    wet = numpy.zeros((frames, 2))
    duck = numpy.ones(frames)  # the pad steps back for a moment under every thump, which is what makes the pulse felt
    for note in notes:
        if note.voice == "kick":
            at = int(note.start * SAMPLE_RATE)
            time = numpy.arange(max(0, min(frames - at, int(0.5 * SAMPLE_RATE)))) / SAMPLE_RATE
            duck[at: at + time.size] *= 1 - 0.3 * note.level * numpy.exp(-time / 0.16)
    for note in notes:
        at = int(round(note.start * SAMPLE_RATE))
        if at >= frames:
            continue
        left, right = _voice(note, numpy, noise)
        level = LEVELS[note.voice] * note.level
        left = left[: frames - at] * level * min(1.0, 1 - note.pan)
        right = right[: frames - at] * level * min(1.0, 1 + note.pan)
        if note.voice == "pad":
            left = left * duck[at: at + left.size]
            right = right * duck[at: at + right.size]
        dry[at: at + left.size, 0] += left
        dry[at: at + right.size, 1] += right
        send = REVERB.get(note.voice, 0.0)
        if send:
            wet[at: at + left.size, 0] += left * send
            wet[at: at + right.size, 1] += right * send
    for side, echo in enumerate(_room(numpy, noise)):
        dry[:, side] += _through(wet[:, side], echo, numpy)
    return master(dry, seconds, numpy)


def write(seconds: float, end_at: float, seed: str, path: Path) -> Path:
    """Write the piece for this video as a 16-bit stereo WAV. Raises when it cannot be played here."""
    if not available():
        raise RuntimeError("numpy is not installed (pip install numpy)")
    import numpy

    samples = render(score(seconds, end_at, seed), seconds, seed)
    with wave.open(str(path), "wb") as track:
        track.setnchannels(2)
        track.setsampwidth(2)
        track.setframerate(SAMPLE_RATE)
        track.writeframes((numpy.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes())
    return path
