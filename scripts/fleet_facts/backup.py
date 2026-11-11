"""The fleet-work-backup health report (open-loops P1), passed through.

    python3 ~/apps/fleet-work-backup/fleet_work_backup.py health --json

`health` is read-only:  it reads the job's state file, the newest run line and the installed files, and
never touches credentials, restic or B2.  Its report is copied as it is, with the free-text fields (the
reason and the newest run line) cleaned.  `fleet-work-backup` on PATH is used when it exists;  otherwise the
stable copy under `~/apps/fleet-work-backup/`.  Neither one present gives "absent", not an error.
"""
from __future__ import annotations

import json
import os
import sys

from . import safety
from .runner import Runner, absent, failed, home_path, ok, which

TIMEOUT_S = 120
_TEXT_FIELDS = {"reason": 300, "last_line": 300, "last_complete_text": 80, "last_ok_text": 80, "checked_text": 80}


def _argv() -> list[str] | None:
    installed = which("fleet-work-backup")
    if installed:
        return [installed, "health", "--json"]
    script = home_path("apps", "fleet-work-backup", "fleet_work_backup.py")
    if not script.is_file():
        return None
    python = next((p for p in ("/opt/homebrew/bin/python3", which("python3"), sys.executable) if p and os.access(p, os.X_OK)), None)
    return [python, str(script), "health", "--json"] if python else None


def gather(run: Runner, *, no_mac: bool = False) -> dict:
    """The `backup` source."""
    if no_mac:
        return absent("--no-mac")
    argv = _argv()
    if argv is None:
        return absent("fleet-work-backup is not installed on this host")
    result = run(argv, timeout=TIMEOUT_S)
    if result.reason:
        return failed("fleet-work-backup health %s" % result.reason)
    if result.rc != 0:
        return failed("fleet-work-backup health exited %s" % result.rc)
    try:
        report = json.loads(result.out)
    except ValueError:
        return failed("fleet-work-backup health did not print JSON")
    if not isinstance(report, dict) or not isinstance(report.get("verdict"), str):
        return failed("fleet-work-backup health printed an unexpected shape")
    cleaned = dict(report)
    for key, cap in _TEXT_FIELDS.items():
        if key in cleaned and isinstance(cleaned[key], str):
            cleaned[key] = safety.clean(cleaned[key], cap)
    cleaned["verdict"] = safety.clean(cleaned["verdict"], 20)
    if isinstance(cleaned.get("warn"), list):
        cleaned["warn"] = [safety.clean(w, 40) for w in cleaned["warn"]][:20]
    return ok({"report": cleaned, "verdict": cleaned["verdict"]})
