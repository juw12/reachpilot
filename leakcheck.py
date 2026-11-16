"""Leak check: no private repository name, and no word I listed, in this public repository.

    python leakcheck.py              the tracked files
    python leakcheck.py --history    plus every commit HEAD descends from (added lines and commit messages)
    python leakcheck.py --require    fail when there is no list to check against (CI on main)
    python leakcheck.py --sync       store today's list as the repository secret PRIVATE_NAMES (needs gh)

The list never lives in this repository. On this PC it is the private repositories of the last patent scan
(data/patents/scan.json, git-ignored) plus "leak_words" from config.local.json, minus "leak_accepted" from the
same file (a name I decided to live with, or an ordinary word such as a tab name). In CI it is the secret
PRIVATE_NAMES. A hit is reported as a file and a line number, or a commit: the name and the line are never
printed, so a public CI log cannot leak what the check found.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
SCAN_FILE = CODE_DIR / "data" / "patents" / "scan.json"
LOCAL_CONFIG = CODE_DIR / "config.local.json"
SECRET = "PRIVATE_NAMES"
SHORTEST = 3  # a shorter name would match by accident all over the code
EDGE = "A-Za-z0-9_-"  # "rig" is not found inside "old-rig" or "rigs": those are other names
MASK = "[listed name]"


def parse_names(text: str) -> list[str]:
    """Names from a secret or a file: one per line or comma, lower case, no duplicates, no very short ones."""
    seen = []
    for part in re.split(r"[\n,]", text or ""):
        name = part.strip().lower()
        if len(name) >= SHORTEST and name not in seen:
            seen.append(name)
    return seen


def local_names(scan_file: Path | None = None, local_config: Path | None = None) -> list[str]:
    """The private repositories of the last scan and my own words, without the ones I accepted."""
    scan_file, local_config = scan_file or SCAN_FILE, local_config or LOCAL_CONFIG
    names, accepted = [], []
    if scan_file.exists():
        scan = json.loads(scan_file.read_text(encoding="utf-8"))
        names += [str(entry.get("name", "")) for entry in scan.get("repos", [])
                  if isinstance(entry, dict) and entry.get("private")]
    if local_config.exists():
        local = json.loads(local_config.read_text(encoding="utf-8-sig"))
        names += [word for word in local.get("leak_words", []) if isinstance(word, str)]
        accepted = parse_names("\n".join(word for word in local.get("leak_accepted", []) if isinstance(word, str)))
    return [name for name in parse_names("\n".join(names)) if name not in accepted]


def compile_names(names: list[str]) -> re.Pattern | None:
    if not names:
        return None
    longest_first = sorted(names, key=len, reverse=True)
    return re.compile(f"(?<![{EDGE}])(?:" + "|".join(re.escape(name) for name in longest_first) + f")(?![{EDGE}])",
                      re.IGNORECASE)


def lines_with_hits(text: str, names: re.Pattern) -> list[int]:
    return [number for number, line in enumerate(text.splitlines(), 1) if names.search(line)]


def shown(path: str, names: re.Pattern) -> str:
    """A path as it may be printed: a listed name inside it is replaced, so the report cannot leak it."""
    return names.sub(MASK, path)


def git(root: Path, *arguments: str) -> bytes:
    return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=True).stdout


def scan_files(root: Path, names: re.Pattern) -> list[str]:
    """'path:line' for every tracked text file line that holds a listed name. A path can hold one too."""
    found = []
    for raw in git(root, "ls-files", "-z").split(b"\0"):
        path = raw.decode("utf-8", "replace")
        if not path:
            continue
        if names.search(path):
            found.append(f"{shown(path, names)} (the path itself)")
        file = root / path
        if not file.is_file():
            continue
        content = file.read_bytes()
        if b"\0" in content[:8000]:  # an image or another binary file
            continue
        found += [f"{shown(path, names)}:{number}"
                  for number in lines_with_hits(content.decode("utf-8", "replace"), names)]
    return found


def scan_history(root: Path, names: re.Pattern) -> list[str]:
    """'commit path' or 'commit (message)' for every commit of HEAD's history that added a listed name."""
    log = git(root, "log", "--format=%x01%h%n%B%x02", "-p", "--no-color", "--unified=0", "HEAD").decode("utf-8", "replace")
    found, commit, path, in_message = [], "", "", False
    for line in log.splitlines():
        if line.startswith("\x01"):
            commit, path, in_message = line[1:], "", True
            continue
        if in_message:
            if "\x02" in line:
                in_message = False
            hit = f"{commit} (message)" if names.search(line) else ""
        elif line.startswith("+++ "):
            path = line[6:].rstrip("\t") if line.startswith("+++ b/") else ""
            hit = f"{commit} {shown(path, names)} (the path itself)" if names.search(path) else ""
        elif line.startswith("+"):
            hit = f"{commit} {shown(path, names)}" if names.search(line[1:]) else ""
        else:
            hit = ""
        if hit and hit not in found:
            found.append(hit)
    return found


def sync(names: list[str]) -> str:
    """Store the list as the repository secret, so CI checks against the same names as this PC."""
    import github_source as gh

    if not names:
        return "Nothing stored: there is no list on this PC."
    command = ["gh", "secret", "set", SECRET]
    body = "\n".join(names).encode("utf-8")
    for environment in (None, gh.proxy_environment()):
        try:
            done = subprocess.run(command, input=body, capture_output=True, cwd=CODE_DIR, env=environment, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            return f"Stored {len(names)} name(s) as the secret {SECRET}."
    return f"Could not store the secret {SECRET} (gh missing, not signed in, or offline)."


def main(arguments: list[str]) -> int:
    from_secret = parse_names(os.environ.get(SECRET, ""))
    names = from_secret or local_names()
    source = f"the secret {SECRET}" if from_secret else "this PC's last scan and config.local.json"
    if "--sync" in arguments:
        print(sync(local_names()))
        return 0
    compiled = compile_names(names)
    if compiled is None:
        print("Leak check: no list to check against (no secret, no scan on this computer).")
        return 2 if "--require" in arguments else 0
    found = scan_files(CODE_DIR, compiled)
    where = "the tracked files"
    if "--history" in arguments:
        found += scan_history(CODE_DIR, compiled)
        where += " and the history"
    print(f"Leak check: {len(names)} name(s) from {source}, checked against {where}.")
    for place in found[:50]:
        print("  LEAK " + place)
    if len(found) > 50:
        print(f"  ... and {len(found) - 50} more")
    print("CLEAN" if not found else f"{len(found)} place(s) hold a listed name. The names are not shown here.")
    return 1 if found else 0


if __name__ == "__main__":
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv[1:]))
