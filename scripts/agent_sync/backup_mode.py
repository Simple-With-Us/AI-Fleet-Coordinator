"""AFC-649 known-cap mode: an explicit, reversible bridge consult switch.

WHY THIS EXISTS.  The owner has declared the primary consult runtime capped and asked for the
approved backup to serve bridge consults now.  Automatic cap detection is NOT part of this module:
the installed Muse 1.4.14 exec stream does not expose the typed quota signal (the only verified
embedding is MSP turn/completed params.error.quota, and the bridge's headless exec runs never see
it), so nothing here tries to infer a cap from error text, exit codes, or record shapes.  The mode
is entered only by explicit operator declaration and exited only by explicit removal or by its own
stated review time.

EXPIRY IS A HOLD, NOT A REVERT.  When the declared review time passes, consults HOLD: pending rows
stay pending, no cursor advances, no runtime is invoked at all, and a review-due signal is logged.
The mode never silently outlives its stated condition, and expiry never switches to unverified
spending or model behavior (neither back to the capped primary nor onward on the backup).

ABSENT VS MALFORMED.  A declaration that was never configured (absent/null) selects the primary,
i.e. exactly the behavior the bridge has today.  A declaration that IS configured but partial or
malformed fails CLOSED:  consults hold, pending work is preserved, and a misconfigured signal is
logged - silently falling back to the primary would spend against the cap the operator declared,
and silently degrading would consume rows nobody decided.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Mapping

MUSE = "muse"
MINIMAX = "minimax"
HOLD = "backup_review_due"

_REQUIRED_KEYS = ("runtime", "reason", "declared_at", "review_by")


@dataclass(frozen=True)
class BackupDeclaration:
    """One operator declaration that the primary consult runtime is at a known cap.

    runtime:    the approved backup runtime id; only "minimax" is recognised.
    reason:     the operator's own words for why the mode was entered (logged verbatim).
    declared_at / review_by: epoch seconds.  review_by is REQUIRED - the mode must state the
                condition it lives under, and that condition includes when it is re-examined."""

    runtime: str
    reason: str
    declared_at: float
    review_by: float


def parse_declaration(raw: Any) -> BackupDeclaration | None:
    """The declaration from bridge config, or None when absent or malformed (fail-safe)."""
    if not isinstance(raw, Mapping):
        return None
    if any(k not in raw for k in _REQUIRED_KEYS):
        return None
    runtime = raw["runtime"]
    reason = raw["reason"]
    declared_at = raw["declared_at"]
    review_by = raw["review_by"]
    if runtime != MINIMAX:
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    for value in (declared_at, review_by):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
    if review_by <= declared_at:
        return None
    return BackupDeclaration(
        runtime=MINIMAX,
        reason=reason.strip(),
        declared_at=float(declared_at),
        review_by=float(review_by),
    )


MISCONFIGURED = "backup_misconfigured"
REVIEW_DUE = "backup_review_due"


def select_consult_runtime(
    raw: Any, *, now: float | None = None
) -> tuple[str, "BackupDeclaration | None", "str | None"]:
    """(which runtime a bridge consult uses, the declaration, why consults hold).

    (MUSE, None, None)          - never enabled: no declaration configured.
    (MINIMAX, decl, None)       - a complete declaration inside its stated window.
    (HOLD, None, MISCONFIGURED) - a declaration IS configured but partial or malformed: fail
                                  closed, preserve pending work, never silently use the primary.
    (HOLD, decl, REVIEW_DUE)    - the declaration's review time has passed: hold every consult,
                                  preserve pending work, signal for re-examination.  No runtime runs."""
    if raw is None:
        return MUSE, None, None
    decl = parse_declaration(raw)
    if decl is None:
        return HOLD, None, MISCONFIGURED
    moment = time.time() if now is None else now
    if moment >= decl.review_by:
        return HOLD, decl, REVIEW_DUE
    return MINIMAX, decl, None
