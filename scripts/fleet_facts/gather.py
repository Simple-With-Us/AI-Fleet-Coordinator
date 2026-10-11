"""Run the requested parts and build the facts document (schema 1).

    {"schema": 1, "tool": "fleet-facts", "version": "...", "generated_at": "2026-10-11T09:00:00Z",
     "host": "...", "no_mac": false, "parts": ["github", ...],
     "sources": {"github": {"status": "ok", "reason": null}, "lanes": {"status": "absent", "reason": "--no-mac"}, ...},
     "github": {...}, "linear": {...}, ...,          # a part's data, present only when its status is ok or failed-with-partial-data
     "redacted": []}                                    # paths whose secret-shaped text was withheld

`sources[...].status` is "ok", "absent" (nothing to read here, or --no-mac) or "failed" (with a reason).
Every document passes the F3 `redact_fields` before it leaves this function.  One part crashing is a failed
part, not a failed run;  the only thing that stops a run is a missing safety helper.
"""
from __future__ import annotations

import socket
import time
from datetime import datetime, timezone
from typing import Callable

from . import __version__, backup, board, github_prs, interfaces, lanes, safety
from .runner import Runner, failed
from .runner import run as default_run

SCHEMA = 1
PARTS = ("github", "linear", "lanes", "autopush", "backup") + tuple(p for p in interfaces.PARTS if p != "autopush")
MAC_PARTS = ("lanes", "autopush", "backup") + tuple(p for p in interfaces.PARTS if p != "autopush")


def normalize_parts(requested: list[str] | None) -> list[str]:
    """Order-preserving, de-duplicated;  empty means every part.  Raises ValueError for an unknown name."""
    names: list[str] = []
    for item in requested or []:
        for name in str(item).split(","):
            name = name.strip()
            if name and name not in names:
                if name not in PARTS:
                    raise ValueError("unknown part %r (choose from %s)" % (name, ", ".join(PARTS)))
                names.append(name)
    return names or list(PARTS)


def _one(part: str, run: Runner, now: datetime, no_mac: bool, sleep: Callable[[float], None]) -> dict:
    if part == "github":
        return github_prs.gather(run, sleep=sleep)
    if part == "linear":
        return board.gather(run)
    if part == "lanes":
        return lanes.gather(run, now=now, no_mac=no_mac)
    if part == "backup":
        return backup.gather(run, no_mac=no_mac)
    return interfaces.gather(part, run, no_mac=no_mac)


def build(requested: list[str] | None = None, *, no_mac: bool = False, run: Runner = default_run,
          now: datetime | None = None, sleep: Callable[[float], None] = time.sleep) -> dict:
    parts = normalize_parts(requested)
    current = now or datetime.now(timezone.utc)
    sources: dict[str, dict] = {}
    data: dict[str, dict] = {}
    for part in parts:
        try:
            outcome = _one(part, run, current, no_mac, sleep)
        except safety.HelpersMissing:
            raise
        except Exception as exc:  # one broken part must not take the others down
            outcome = failed("internal error (%s)" % type(exc).__name__)
        sources[part] = {"status": outcome["status"], "reason": outcome.get("reason")}
        if outcome.get("data") is not None:
            data[part] = outcome["data"]
    document: dict = {
        "schema": SCHEMA,
        "tool": "fleet-facts",
        "version": __version__,
        "generated_at": current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": safety.clean(socket.gethostname().split(".")[0], 60) or "unknown",
        "no_mac": bool(no_mac),
        "parts": parts,
        "sources": sources,
    }
    document.update(data)
    redacted, paths = safety.redact(document)
    redacted["redacted"] = paths
    return redacted
