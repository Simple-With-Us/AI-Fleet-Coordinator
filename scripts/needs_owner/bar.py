"""The bar:  what may reach Jay through `needs-owner file`.

A decision waits on Jay only when it needs his identity, his credential, his money, a legal act, an
irreversible approval or a decision only he can make.  Everything else the seat decides, asks a peer in
Side-Chats, or posts to Pending-for-Jay.  The filing seat names which of the six it is with `--why-owner`;
the class is stored in the block (`Why owner:`) and the knobs decide how hard the bar is:

  NEEDS_OWNER_BAR              off (the default, today's behaviour exactly), warn (file it, record the class,
                               print one warning line) or enforce (refuse a filing without a class)
  NEEDS_OWNER_SEAT_DAILY_MAX   most filings one seat may make in 24 hours (0, the default, means no cap);
                               it applies in warn (a warning) and enforce (a refusal), never in off

Both knobs are read once, through `fleet_knobs` (the tests pass a fake reader).  A knob that cannot be read, or
holds anything but one of the three modes, is `off`:  the bar never stops a filing it cannot be sure about.
A Grok Bot mirror (`--mirror-of`) is exempt:  the persona already opened its own OWNER topic, so there is
nothing left to keep from Jay.

The cap counts this host's own filings, kept in `~/apps/needs-owner/filings/filings.json` (mode 0600, seat
names, times and identifiers only).  A closed item still counts, because the filer removes the label when it
closes one and Linear alone would forget it.  A rerun that files nothing new is never counted or refused.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass

from . import ledger
from .block import WHY_RE
from .errors import Refusal

MODE_KNOB = "NEEDS_OWNER_BAR"
CAP_KNOB = "NEEDS_OWNER_SEAT_DAILY_MAX"

OFF, WARN, ENFORCE = "off", "warn", "enforce"
MODES = (OFF, WARN, ENFORCE)

# The six things that need Jay (identity, credential, money, a legal act, an irreversible approval, a decision only he
# can make).  What each one means is in docs/protocols/needs-owner.md, "What May Reach Jay";  a test keeps the two equal.
CLASSES = ("identity", "credential", "money", "legal", "irreversible", "policy")
# A class word as stored.  The block reader checks the shape and not this list, so an item filed under a class
# that is later retired still reads.
CLASS_RE = WHY_RE

WINDOW_SECONDS = 24 * 3600
KEEP_SECONDS = 2 * 24 * 3600
MAX_ROWS = 500
FILINGS_DIR = "filings"
FILINGS_FILE = "filings.json"
SCHEMA = 1

POINTER = (
    "everything else is yours to decide, or goes to Side-Chats (bot to bot, visible to Jay) or to "
    "Pending-for-Jay (one updated post per bot, for what only the owner has to act on)"
)


@dataclass(frozen=True)
class Policy:
    mode: str = OFF
    cap: int = 0

    @property
    def on(self) -> bool:
        return self.mode != OFF


# ---- the knobs ------------------------------------------------------------------------------------


def _fleet_knobs() -> object:
    from fleet_knobs import knobs  # the stable copy carries it;  a copy without it reads `off`

    return knobs


def read_policy(knobs: object | None = None) -> Policy:
    """The bar's mode and cap.  Anything that cannot be read, or is not one of the three modes, is `off`."""
    try:
        reader = knobs if knobs is not None else _fleet_knobs()
        mode = reader.get(MODE_KNOB)
    except Exception:  # noqa: BLE001 - an unreadable knob never stops a filing
        return Policy()
    if mode not in (WARN, ENFORCE):
        return Policy()
    try:
        raw = reader.get(CAP_KNOB)
    except Exception:  # noqa: BLE001
        return Policy(mode)
    cap = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else 0
    return Policy(mode, cap)


# ---- the class --------------------------------------------------------------------------------------


def _message(problem: str) -> str:
    return (
        "%s;  needs-owner is only for what needs Jay's identity, credential, money, a legal act, an irreversible "
        "approval or a decision only he can make (--why-owner %s);  %s"
        % (problem, "|".join(CLASSES), POINTER)
    )


def check_class(mode: str, given: str | None) -> tuple[str | None, str | None]:
    """(the class to store, a warning line), or a Refusal.

    Off looks at nothing.  Warn stores a good class and warns about a missing or unknown one (the filing goes
    ahead without a class).  Enforce refuses a missing or unknown class.  The answer never repeats the text
    that was given, because a seat may have typed anything."""
    if mode not in (WARN, ENFORCE):
        return None, None
    word = (given or "").strip().lower()
    if word in CLASSES:
        return word, None
    problem = (
        "no --why-owner class was given"
        if not word
        else "--why-owner is not one of the six classes"
    )
    if mode == ENFORCE:
        raise Refusal(_message(problem))
    return None, _message(problem)


# ---- the daily cap ------------------------------------------------------------------------------------


def _path(environ: Mapping[str, str]) -> str:
    home = environ.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, "apps", "needs-owner", FILINGS_DIR, FILINGS_FILE)


def _rows(environ: Mapping[str, str]) -> list[dict]:
    data = ledger.read_json(_path(environ))
    rows = data.get("rows") if isinstance(data, dict) and data.get("schema") == SCHEMA else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def filings_in_window(environ: Mapping[str, str], seat: str, now: float) -> int:
    """How many items `seat` filed from this host in the last 24 hours."""
    count = 0
    for row in _rows(environ):
        at = row.get("t")
        if (
            row.get("seat") == seat
            and isinstance(at, (int, float))
            and not isinstance(at, bool)
            and 0 <= now - at < WINDOW_SECONDS
        ):
            count += 1
    return count


def record_filing(environ: Mapping[str, str], seat: str, identifier: str, now: float) -> None:
    """Remember one new filing.  A write that fails is ignored:  the bar never blocks a filing that landed."""
    try:
        rows = [
            r
            for r in _rows(environ)
            if isinstance(r.get("t"), (int, float)) and 0 <= now - r["t"] < KEEP_SECONDS
        ]
        rows.append({"seat": seat, "t": now, "id": identifier})
        path = _path(environ)
        ledger.private_dir(os.path.dirname(path))
        ledger.write_private(
            path, json.dumps({"schema": SCHEMA, "rows": rows[-MAX_ROWS:]}, sort_keys=True)
        )
    except OSError:
        pass


def check_cap(policy: Policy, environ: Mapping[str, str], seat: str, now: float) -> str | None:
    """A warning line when `seat` is at its daily cap in warn mode, a Refusal in enforce, else None."""
    if not policy.on or policy.cap <= 0:
        return None
    used = filings_in_window(environ, seat, now)
    if used < policy.cap:
        return None
    message = (
        "%s has filed %d needs-owner items in the last 24 hours (the daily cap is %d);  hold this one, "
        "decide it yourself or post it to Pending-for-Jay" % (seat, used, policy.cap)
    )
    if policy.mode == ENFORCE:
        raise Refusal(message)
    return message


def wall(clock: object | None) -> float:
    """Now as seconds since the epoch:  the caller's clock (a function giving an aware datetime) or the real one."""
    if callable(clock):
        return float(clock().timestamp())
    return time.time()
