"""The session id a notes write must carry (AFC-361, docs/protocols/resume-wake-safety-design.md section 4).

A headless resume-wake finds a seat's session from the `Session: <id>` line stamped in the note, so
`notes_create`, `notes_update` and `simple-notes create|update` carry the writer's session id, and the Mac
listener refuses a relayed request that has none.

The contract is the `sessionIds` block of fleet-apps.json:  the constants below are copies of it
(scripts/test_session_ids_parity.py fails when they drift;  scripts/agent-sync-mcp/src/session-ids.js and the
board and notes scripts carry the same copy).  A writer with seat S finds its id in this order:

  1. the explicit value (the `session` tool argument, or `--session`), checked against ID_PATTERN;  an invalid
     explicit value is an error, never ignored;
  2. the environment variable AGENT_SESSION;
  3. S's own variables in ENV_BY_SEAT (the first non-empty one);  a seat with no entry there tries every
     variable in FALLBACK_ENV, in order;
  4. none found:  an exempt seat writes without one, every other seat is refused (`require`).

The exemption is decided from the RESOLVED seat (the per-call identity, or a relay sender's bot), after
identity.canonical_seat, and never from an argument the caller sets:  there is no flag that makes a seat exempt.
An id found for an exempt seat is still recorded.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping

from . import identity as I

# ---- copies of fleet-apps.json `sessionIds` (keep them equal;  the parity test enforces it) ----------------------
ID_PATTERN = "^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$"
GENERIC_ENV = "AGENT_SESSION"
EXEMPT_SEATS = ["ECHO", "GROK-BOT", "JET", "JET-CHAT", "MA", "WISE"]
EXEMPT_PATTERNS = ["GB-*"]
ENV_BY_SEAT = {
    "CLAUDE": ["CLAUDE_CODE_SESSION_ID"],
    "CODEX": ["CODEX_THREAD_ID"],
    "CURSOR": ["CURSOR_CONVERSATION_ID", "CURSOR_AGENT_CHAT_ID"],
    "GROK": ["GROK_SESSION_ID"],
    "MM": ["__MAVIS_RUNTIME_SESSION_ID"],
    "OPENCODE": ["OPENCODE_SESSION_ID"],
}
FALLBACK_ENV = [
    "CLAUDE_CODE_SESSION_ID",
    "CODEX_THREAD_ID",
    "CURSOR_CONVERSATION_ID",
    "CURSOR_AGENT_CHAT_ID",
    "GROK_SESSION_ID",
    "OPENCODE_SESSION_ID",
    "__MAVIS_RUNTIME_SESSION_ID",
]

_ID_RE = re.compile(ID_PATTERN)
SURFACES = ("tool", "cli", "relay")


class SessionRequired(ValueError):
    """A non-exempt writer with no session id.  The message says how to pass one;  it never holds a note body."""


def _seat(seat: str | None) -> str:
    return I.canonical_seat(seat or "")


def is_exempt(seat: str | None) -> bool:
    """True when this seat has a single thread with the owner and needs no session id:  the seat is in
    EXEMPT_SEATS or matches an EXEMPT_PATTERNS entry, after canonical_seat (ROB is MA, INSTINCT is WISE)."""
    name = _seat(seat)
    if not name:
        return False
    return name in EXEMPT_SEATS or any(
        fnmatch.fnmatchcase(name, pattern) for pattern in EXEMPT_PATTERNS
    )


def check(value: object, where: str = "the session id") -> str:
    """The value as a session id, or ValueError.  Surrounding whitespace is trimmed first."""
    text = value.strip() if isinstance(value, str) else None
    if text is None or not _ID_RE.match(text):
        raise ValueError(
            "%s is not a valid session id:  it must be 4 to 128 characters, start with a letter or digit, and hold "
            "only letters, digits, . _ : and -" % where
        )
    return text


def own_variables(seat: str | None) -> list[str]:
    """The environment variables that hold this seat's session id (ENV_BY_SEAT), or [] for a seat with none."""
    return list(ENV_BY_SEAT.get(_seat(seat), []))


def _first_set(names: list[str], env: Mapping[str, str]) -> tuple[str, str] | None:
    for name in names:
        value = str(env.get(name) or "").strip()
        if value:
            return name, value
    return None


def _from_env(seat: str | None, env: Mapping[str, str]) -> str | None:
    found = _first_set([GENERIC_ENV], env)
    if found is None:
        name = _seat(seat)
        found = _first_set(ENV_BY_SEAT.get(name, FALLBACK_ENV), env)
    return check(found[1], "the %s environment variable" % found[0]) if found else None


def detect(
    seat: str | None, explicit: str | None = None, env: Mapping[str, str] | None = None
) -> str | None:
    """The session id for `seat` (steps 1 to 3), or None.  `env` defaults to os.environ;  pass {} to look at
    nothing (the relay does, since the listener's own environment is not the sender's).  Raises ValueError for
    an explicit or environment value that is not a valid id."""
    if isinstance(explicit, str) and explicit.strip():
        return check(explicit, "the session argument")
    if explicit is not None and not isinstance(explicit, str):
        raise ValueError("the session argument must be text")
    return _from_env(seat, os.environ if env is None else env)


def refusal(seat: str | None, surface: str = "tool", board: bool = False) -> str:
    """The refusal text for a non-exempt writer with no session id (no note body, no secret).  `board` words it for
    the Linear board writes (linear_file, linear_claim, linear_comment, linear_status) instead of Apple Notes."""
    name = _seat(seat) or "this writer"
    own = own_variables(seat)
    if surface == "relay":
        return (
            "a session id is required to write an Apple Note as %s and this request carries none.  Resend it with "
            "the `session` argument of notes_create or notes_update%s, so a headless resume can find the session "
            "from the note's Session line."
            % (name, " (for %s it is $%s)" % (name, " or $".join(own)) if own else "")
        )
    how = "the `session` argument" if surface == "tool" else "--session ID"
    what = "write to the Linear board" if board else "write an Apple Note"
    text = (
        "a session id is required to %s as %s.  Pass it as %s, or set the AGENT_SESSION environment variable"
        % (what, name, how)
    )
    if own:
        text += ";  %s's own variable is %s, and it is empty here" % (name, " or ".join(own))
    text += (
        ".  A headless resume finds the session from the issue's Sessions line."
        if board
        else ".  A headless resume finds the session from the note's Session line."
    )
    return text


def require(
    seat: str | None,
    explicit: str | None = None,
    env: Mapping[str, str] | None = None,
    surface: str = "tool",
    board: bool = False,
) -> str | None:
    """The session id for `seat`, None for an exempt seat that has none, or SessionRequired.  `surface` is
    "tool" (the `session` argument), "cli" (--session) or "relay" (the listener judging an envelope).  A bad
    explicit value is a ValueError for everyone;  a bad environment value is a ValueError for a non-exempt
    seat and is passed over for an exempt one, whose id is optional."""
    if surface not in SURFACES:
        raise ValueError("surface must be one of %s" % ", ".join(SURFACES))
    exempt = is_exempt(seat)
    try:
        found = detect(seat, explicit, env)
    except ValueError:
        if not exempt or (isinstance(explicit, str) and explicit.strip()):
            raise
        found = None
    if found is not None or exempt:
        return found
    raise SessionRequired(refusal(seat, surface, board))
