"""Write LinkedIn posts from grounded GitHub facts.

Each angle has several hooks/structures/closers so recycled posts about the same
project read differently. Nothing here invents metrics, users or backstory:
every factual sentence is built from README/description/topics/commits.
"""
from __future__ import annotations

import json
import random
import re
import urllib.request

from github_source import RepoFacts

RECYCLE_ANGLES = ["launch", "deep_dive", "behind_the_build", "feedback"]

TOPIC_HASHTAGS = {
    "ai": "#AI", "llm": "#LLM", "python": "#Python", "unity": "#Unity", "pwa": "#PWA",
    "github": "#GitHub", "privacy": "#Privacy", "indonesia": "#Indonesia", "cfd": "#CFD",
    "game": "#GameDev", "offline-first": "#OfflineFirst", "local-first": "#LocalFirst",
    "productivity": "#Productivity", "developer-tools": "#DeveloperTools", "sqlite": "#SQLite",
    "ai-agents": "#AIAgents", "ai-safety": "#AISafety", "fortran": "#Fortran",
    "csharp": "#CSharp", "typescript": "#TypeScript", "markdown": "#Markdown",
}

STACK_TOPICS = {
    "offline-first": "Works offline first; the network is optional",
    "local-first": "Local-first: your data stays on your machine",
    "pwa": "Installable as a PWA on phone and desktop",
    "sqlite": "SQLite for storage, no database server to run",
    "zero-dependencies": "Zero dependencies",
    "github-actions": "Automated with GitHub Actions",
    "ollama": "Can run on local models through Ollama",
    "unity": "Built in Unity",
    "tauri": "Desktop build with Tauri",
}


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:] if text and not text[:2].isupper() else text


def _sentence(text: str) -> str:
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else text + "."


def hashtags_for(facts: RepoFacts | None, defaults: list[str], rng: random.Random) -> str:
    tags = []
    if facts:
        for topic in facts.topics:
            tag = TOPIC_HASHTAGS.get(topic)
            if tag and tag not in tags:
                tags.append(tag)
        if facts.language and f"#{facts.language}" not in tags and facts.language.isalpha():
            tags.append(f"#{facts.language}")
    rng.shuffle(tags)
    chosen = tags[:2] + [tag for tag in defaults if tag not in tags]
    return " ".join(chosen[:4])


def links_for(facts: RepoFacts) -> str:
    if facts.doi_url:  # research: there is nothing to "try"; readers get the repo first, then the archive to cite
        return f"Source: {facts.html_url}\nCitable archive (DOI): {facts.doi_url}"
    lines = []
    if facts.homepage:
        lines.append(f"Try it: {facts.homepage}")
    lines.append(f"Source: {facts.html_url}")
    return "\n".join(lines)


def _closer(rng: random.Random, links_in_comment: bool) -> str:
    options = [
        "Feedback welcome, especially the critical kind.",
        "If it's useful to you, a GitHub star helps other people find it.",
        "Tell me what you'd change first.",
        "Open source, so feel free to break it and tell me how.",
        "Curious what you'd use it for.",
    ]
    closer = rng.choice(options)
    if links_in_comment:
        closer += rng.choice(["\n\nLinks in the first comment.", "\n\nLink in the comments.", ""])
    return closer


def compose_launch(facts: RepoFacts, rng: random.Random, used: set[str], links_in_comment: bool) -> str:
    name = facts.display_name
    hooks = [f"New on my GitHub: {name}.", f"Meet {name}.", f"Something I built: {name}."]
    if facts.tagline:
        hooks += [_sentence(facts.tagline), f"{name}: {_lower_first(_sentence(facts.tagline))}"]
    bullets = [bullet for bullet in facts.bullets if bullet not in used] or facts.bullets
    picked = rng.sample(bullets, k=min(len(bullets), rng.choice([3, 4])))
    lead = rng.choice(["What it does:", "Highlights:", "A few things it does:", "In short:"])
    parts = [rng.choice(hooks)]
    if facts.description and facts.description.rstrip(".") not in parts[0]:
        parts.append(_sentence(facts.description))
    if picked:
        parts.append(lead + "\n" + "\n".join(f"- {_sentence(bullet)}" for bullet in picked))
    parts.append(_closer(rng, links_in_comment))
    return "\n\n".join(parts)


def compose_deep_dive(facts: RepoFacts, rng: random.Random, used: set[str], links_in_comment: bool) -> str | None:
    fresh = [bullet for bullet in facts.bullets if bullet not in used]
    if not fresh:
        return None
    focus = rng.choice(fresh)
    name = facts.display_name
    hooks = [
        f"One detail in {name} I'm quietly proud of:",
        f"A closer look at one part of {name}:",
        f"If you only try one thing in {name}, try this:",
        f"Small feature, big difference in {name}:",
    ]
    parts = [rng.choice(hooks), f"→ {_sentence(focus)}"]
    if facts.description:
        parts.append(rng.choice(["For context:", "What it is, in one line:", "The project in one line:"]) + f" {_sentence(facts.description)}")
    others = [bullet for bullet in fresh if bullet != focus]
    if others:
        parts.append(rng.choice(["It also does this:", "Plus:", "And one more:"]) + f"\n- {_sentence(rng.choice(others))}")
    parts.append(_closer(rng, links_in_comment))
    return "\n\n".join(parts)


def stack_facts(facts: RepoFacts) -> list[str]:
    items = []
    if facts.language:
        items.append(f"Written mostly in {facts.language}")
    for topic, phrase in STACK_TOPICS.items():
        if topic in facts.topics:
            items.append(phrase)
    if "zero-dependencies" not in facts.topics and re.search(r"zero[- ]dependenc", facts.source_text(), re.I):
        items.append("Zero dependencies")
    if "github.io" in facts.homepage:
        items.append("Hosted for free on GitHub Pages")
    if facts.has_ci:
        items.append("Tests run in GitHub Actions on every push")
    if facts.license:
        items.append(f"{facts.license} licensed")
    return items


def compose_behind_the_build(facts: RepoFacts, rng: random.Random, used: set[str], links_in_comment: bool) -> str | None:
    items = stack_facts(facts)
    if len(items) < 3:
        return None
    name = facts.display_name
    hooks = [
        f"How {name} is built, for the curious:",
        f"Behind the build: {name}.",
        f"A quick look under the hood of {name}.",
    ]
    parts = [rng.choice(hooks)]
    if facts.description:
        parts.append(_sentence(facts.description))
    parts.append("\n".join(f"- {item}" for item in items[:6]))
    if "Zero dependencies" in items:
        parts.append(rng.choice([
            "Same rule I follow in hardware: keep the bill of materials short.",
            "Fewer parts, fewer things to break.",
        ]))
    elif facts.has_ci:
        parts.append("Same habit as a DVT build: if the tests aren't green, it isn't done.")
    parts.append(_closer(rng, links_in_comment))
    return "\n\n".join(parts)


def compose_feedback(facts: RepoFacts, rng: random.Random, used: set[str], links_in_comment: bool) -> str | None:
    options = [bullet for bullet in facts.bullets if len(bullet) <= 110]
    if len(options) < 3:
        return None
    name = facts.display_name
    picked = rng.sample(options, 3)
    hooks = [
        f"Help me decide what to improve next in {name}.",
        f"Quick question for anyone who'd use {name}:",
        f"I'd like your opinion on {name}.",
    ]
    parts = [rng.choice(hooks)]
    if facts.description:
        parts.append(rng.choice(["In one line:", "Quick context:"]) + f" {_sentence(facts.description)}")
    parts.append(
        rng.choice(["Which of these matters most to you?", "Which would you use first?"])
        + "\n"
        + "\n".join(f"{index}. {_sentence(option)}" for index, option in enumerate(picked, start=1))
    )
    parts.append(rng.choice(["Reply with a number, or tell me what's missing.", "A number is enough. Reasons are even better."]))
    if links_in_comment:
        parts.append("Links in the comments.")
    return "\n\n".join(parts)


def compose_update(facts: RepoFacts, rng: random.Random, changes: list[str], release_tag: str | None, links_in_comment: bool) -> str | None:
    if len(changes) < 2:
        return None
    name = facts.display_name
    if release_tag:
        hooks = [f"{name} {release_tag} is out.", f"New release: {name} {release_tag}."]
    else:
        hooks = [f"What's new in {name}:", f"Recent updates to {name}:", f"Kept shipping on {name}. What changed:"]
    parts = [rng.choice(hooks)]
    if facts.description:
        parts.append(f"Quick recap: {_sentence(facts.description)}")
    parts.append("\n".join(f"- {_sentence(change)}" for change in changes[:5]))
    parts.append(_closer(rng, links_in_comment))
    return "\n\n".join(parts)


def compose_roundup(top: list[RepoFacts], rng: random.Random, user: str) -> str | None:
    if len(top) < 3:
        return None
    hooks = [
        f"The {len(top)} projects people here liked most so far:",
        "Going by your reactions, these are the projects that landed:",
        "A recap of the most popular projects on my GitHub right now:",
    ]
    lines = [f"- {facts.display_name}: {facts.description.rstrip('.')}" for facts in top]
    closer = rng.choice([
        "All open source. Which one should get the next big update?",
        "Thanks to everyone who tried them. Tell me which one to push further.",
    ])
    return "\n\n".join([rng.choice(hooks), "\n".join(lines), closer, f"Everything: github.com/{user}"])


# ---------------------------------------------------------------- safety checks

def invented_numbers(body: str, source: str) -> list[str]:
    """Numbers in the post that don't appear in the grounded source (list markers excepted)."""
    allowed = set(re.findall(r"\d+(?:\.\d+)?", source)) | {"1", "2", "3", "4", "5", "6"}
    return [number for number in re.findall(r"\d+(?:\.\d+)?", body) if number not in allowed]


def shingles(text: str, size: int = 3) -> set[tuple[str, ...]]:
    words = re.findall(r"[a-z0-9']+", text.lower())
    return {tuple(words[index:index + size]) for index in range(max(len(words) - size + 1, 0))}


def similarity(text_a: str, text_b: str) -> float:
    set_a = shingles(text_a)
    set_b = shingles(text_b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def max_similarity(body: str, history: list[str]) -> float:
    return max((similarity(body, previous) for previous in history), default=0.0)


# ------------------------------------------------------------ optional LLM polish

def polish_with_ollama(body: str, source: str, model: str) -> str | None:
    """Optional: rephrase in a natural voice with a local model. Rejected if it adds facts."""
    if not model:
        return None
    prompt = (
        "Rewrite this LinkedIn post so it sounds natural and human, first person, short lines, "
        "no emojis spam, no hype words like 'excited' or 'game-changer'. Keep every fact. "
        "Do NOT add any facts, numbers, users, metrics or story that is not in the post. "
        "Return only the post text.\n\nPOST:\n" + body
    )
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            text = json.loads(response.read())["response"].strip()
    except Exception:
        return None
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    if invented_numbers(text, source + " " + body) or not 300 <= len(text) <= 2600:
        return None
    return text
