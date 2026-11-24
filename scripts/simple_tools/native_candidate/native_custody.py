"""Read authoritative router pending/done custody. No mutation or replay.

The router writes attempts=1/in_flight before invoking the platform. A startup
cutover boundary excludes previously queued wakes even if they never dispatched.
"""

import json
import os
import re
import stat
from pathlib import Path


def read_private(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        st = os.fstat(f.fileno())
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_nlink != 1
            or st.st_uid != os.getuid()
            or st.st_mode & 0o077
            or st.st_size > 131072
        ):
            raise ValueError("custody-file-not-private")
        return json.load(f)


def fresh(state: Path, wake_id: str, target, activation_epoch: float):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", wake_id):
        return False
    if not activation_epoch or (state / "done" / (wake_id + ".json")).exists():
        return False
    try:
        r = read_private(state / "pending" / (wake_id + ".json"))
    except (OSError, ValueError):
        return False
    w = r.get("wake", {})
    p = r.get("pick") or {}
    t = p.get("target") or {}
    turn = r.get("turn")
    turn_ok = turn is None or (
        isinstance(turn, dict)
        and set(turn) == {"id", "riders", "message_ids"}
        and isinstance(turn.get("id"), str)
        and bool(turn["id"])
        and turn.get("riders") == []
        and turn.get("message_ids") == [w.get("message_id")]
    )
    return (
        r.get("attempts") == 1
        and r.get("in_flight") is True
        and not r.get("replayed")
        and turn_ok
        and not r.get("ride")
        and r.get("receipt") is None
        and isinstance(r.get("accepted_at"), (int, float))
        and r["accepted_at"] >= activation_epoch
        and w.get("wake_id") == wake_id
        and w.get("credential_scope") == "review-alerts"
        and w.get("required_session") == target.session_id
        and w.get("require_generation") == target.generation
        and w.get("require_letter") == target.letter
        and w.get("message_id", 0) > 0
        and all(
            t.get(k) == getattr(target, k)
            for k in ("session_id", "host", "letter", "generation")
        )
    )
