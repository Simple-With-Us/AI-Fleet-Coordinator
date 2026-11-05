"""One canonical per-platform wake-transport support table.

The claim here is a source-tree fact, never a runtime promise.  "code-supported"
means this tree ships a delivery adapter for the platform;  it does not say the
adapter is activated, the target chat is idle, or a delivery will succeed.
Runtime outcomes stay the delivery path's own delivered/queued/unavailable/
failed, and runtime freshness stays the chat catalog's job.  "none" means the
platform was assessed and this tree has no adapter for it.  "unknown" means the
platform is not in the table, which says nothing either way:  a platform that
was never assessed must never read as "none".

Keys are adapter platforms as the chat catalog emits them ("cursor-cloud" and
"cursor-local" separately), not registry letter namespaces (both fold to
"cursor"), so a local-only chat never inherits its cloud sibling's claim.

Claim lifecycle:  derivation only ever fills a gap; it never rewrites a stored
claim.  A new row with no caller claim takes the derived value; a caller claim
is stored verbatim.  On resume, the only stored value ever rewritten is the
pre-derivation "unknown" sentinel:  the row then takes the incoming value, the
derived table value when the session's own adapter platform resolves in the
table or a caller's explicit claim (a caller claim is also the only way a chat
on an unlisted platform leaves "unknown").  Closed rows keep their history.  Once
stored, a claim - explicit or derived - is immutable:  a later registration of
the same chat with a different claim (for example a platform letter freshly
installed and now claiming "live-verified") does not overwrite it, and the row
keeps its original claim and provenance.  A derived claim therefore records
what the tree supported when the row was created.  It is derived metadata, not
a live re-assessment and not an explicit platform claim, and consumers must
not read it as either.  Refreshing stored claims against newer runtime
evidence needs a real provenance and update policy (who may refresh, when, and
how stale rows are marked), which depends on the consumers' design and is
deliberately out of scope here.
"""

from __future__ import annotations

SUPPORTED = "code-supported"
NONE = "none"
UNKNOWN = "unknown"

# Every platform the chat catalog scans, assessed against this tree.  A
# platform not listed derives UNKNOWN, not NONE.
_SUPPORT: dict[str, str] = {
    "grok-leader": SUPPORTED,
    "cursor-cloud": SUPPORTED,
    "codex": SUPPORTED,
    "claude": NONE,
    "cursor-local": NONE,
    "muse": NONE,
    "minimax": NONE,
    "fx": NONE,
    "antigravity": NONE,
    "opencode": NONE,
}


def for_platform(platform: object) -> str:
    """The tree's wake-transport support for one adapter platform.

    Registration derives a session's stored wake_capability from this when the
    caller makes no claim of its own, so a new row carries the tree's honest
    support level instead of the blanket "unknown" default.  A caller with
    fresher runtime knowledge still wins:  only the "unknown" default derives.
    """
    if not isinstance(platform, str):
        return UNKNOWN
    return _SUPPORT.get(platform.strip().lower(), UNKNOWN)


def implemented_platforms() -> frozenset[str]:
    """The adapter platforms this tree ships a delivery adapter for."""
    return frozenset(p for p, state in _SUPPORT.items() if state == SUPPORTED)
