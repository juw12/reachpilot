"""One button pressed on the phone.

Usage:  python phone_action.py <name>

The phone shows a snapshot of the dashboard (phone_snapshot.py) inside Cairn's Dashboards page. A button
there cannot reach this PC, so the name of the button travels through the private relay repo, and Cairn's
worker on this PC (`cairn dashboards actions`) starts this script with it. Set up once:

    cairn dashboards add reachpilot <...>/generated/phone_snapshot.html --build "python <...>/phone_snapshot.py"
        --cwd <...> --actions "python <...>/phone_action.py"
    cairn dashboards schedule

The name runs through the same list as a button on the PC (hub.run_action), after a stricter check:
hub.phone_allowed leaves out everything that opens a window, a file or a sign-in on this PC. The last line
printed is what the phone shows. Every request is written to phone_actions.log (git-ignored).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = ROOT / "phone_actions.log"


def main(arguments: list[str], run=None, allowed=None, log: Path = LOG) -> tuple[int, str]:
    """(exit code, the one line for the phone). run and allowed are hub's, replaced in the self-test."""

    def note(name: str, outcome: str) -> None:
        try:
            with open(log, "a", encoding="utf-8") as file:
                file.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {name[:120]!r}  {outcome[:200]}\n")
        except OSError:
            pass

    if len(arguments) != 1:
        return 2, "Usage: python phone_action.py <name>"
    name = arguments[0]
    if run is None or allowed is None:
        import hub  # imported here: it loads the private modules

        run, allowed = run or hub.run_action, allowed or hub.phone_allowed
        refused = hub.PHONE_PC_ONLY
    else:
        refused = "This button only works on your PC."
    if not allowed(name):
        note(name, "refused")
        return 1, refused
    try:
        message = " ".join(str(run(name) or "Done.").split())
    except Exception as error:  # the phone gets the reason, as the PC page does
        reason = " ".join(str(error).split()) or "That didn't work."
        note(name, f"failed: {reason}")
        return 1, reason
    note(name, f"ok: {message}")
    return 0, message


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None:
            stream.reconfigure(encoding="utf-8", errors="replace")
    code, line = main(sys.argv[1:])
    print(line, file=sys.stdout if code == 0 else sys.stderr)
    sys.exit(code)
