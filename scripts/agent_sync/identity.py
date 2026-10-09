"""Seat precedence and the bot check, shared by every agent-sync entry point.

AGENT-SYNC § Identity Rules (owner 2026-10-09) says a session's seat is the first of:

    1. the owner names it                        (in a launched session, through the launcher)
    2. a trusted launcher assigns it              AGENT_LAUNCH_SEAT, set with AGENT_LAUNCHER
    3. the platform default                       an ordinary session only
    4. ask                                        no default:  no seat

The tools apply it in this order (resolve_seat):

    AGENT_LAUNCH_SEAT        wins.  An --as, AGENT_SEAT or AGENT_TAG that names another seat is
                             refused (exit 3), never obeyed:  a skill or rules file that exports its
                             platform seat cannot move a launched bot onto that seat's bot.
    AGENT_LAUNCHER alone     no seat, so no fleet action at all.  Never filled from a default.
    --as, AGENT_SEAT,        an ordinary session, in this order.
    AGENT_TAG
    the platform default     what the caller passes:  `--default-seat` (an MCP registration or a
                             wrapper), or the `[platform.claude-code]` seat for the Claude hooks.
    nothing                  refuse.

The tools never take an in-session re-pin over a launcher's seat.  Jay changes a launched bot's
seat in the launcher; an in-session override is exactly what an injected "Jay said" would use.

Only a launcher writes AGENT_LAUNCH_SEAT and AGENT_LAUNCHER (owner-run software, before the
session's first turn).  No hook, skill, rules file or wrapper may write them, so they are the one
signal a session's own `export AGENT_SEAT=...` cannot fake.  check_bot() is the second half:  the
key a command authenticates with must be that seat's own bot (users/me, through seat_tag_for).

Standard library only and light (os, re, tomllib, no dataclasses):  the Claude hooks import it on
every prompt.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping, NamedTuple

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

ENV_LAUNCHER = "AGENT_LAUNCHER"
ENV_LAUNCH_SEAT = "AGENT_LAUNCH_SEAT"
ENV_ATTACH = "AGENT_SYNC_ATTACH"
ENV_PARTITION = "AGENT_SYNC_PARTITION"
ENV_INSTANCE = "AGENT_SYNC_INSTANCE"
# The launchers the fleet runs (AGENT-SYNC § Identity Rules › Launcher Contract).  Any non-empty
# AGENT_LAUNCHER counts as a launcher; these are the documented values.
LAUNCHERS = ("botfleet", "grok-bot", "agent-sync-wake")
WAKE_LAUNCHER = "agent-sync-wake"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PARTITION_RELPATH = os.path.join("docs", "protocols", "agent-sync-partition.toml")
_SEAT_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}")

# Bot email local parts (minus the "-bot" Zulip appends) whose seat tag is not just the upper-cased
# local part.  Display names are cosmetic (owner 2026-10-07), so labels come from emails, never names.
EMAIL_TAG_OVERRIDES = {
    "muse-assist": "MA",
    "openai-dot": "JET",
    "instinct-bat": "ECHO",
    "instinct-owl": "INSTINCT",
    "grok-build": "GROK",  # terminal Grok and Grok Build are one seat (owner 2026-10-08)
}


def seat_tag_for(user: Mapping[str, Any]) -> str:
    """The seat tag a user or bot signs as: mm-bot@ -> MM, bf-builder-bot@ -> BF-BUILDER,
    compiler-grok-bot@ -> GB-COMPILER, muse-assist-bot@ -> MA.  Humans get their first name."""
    if not user.get("is_bot"):
        first = (str(user.get("full_name") or "").split() or ["USER"])[0]
        return first.upper()
    local = str(user.get("email") or "").split("@", 1)[0].lower()
    if local.endswith("-bot"):
        local = local[: -len("-bot")]
    if local in EMAIL_TAG_OVERRIDES:
        return EMAIL_TAG_OVERRIDES[local]
    if local.endswith("-grok"):
        return "GB-" + local[: -len("-grok")].upper()
    return local.upper() or "-".join(str(user.get("full_name") or "").upper().split())


# --------------------------------------------------------------------------------------------
# Seat precedence
# --------------------------------------------------------------------------------------------

class Resolution(NamedTuple):
    """The outcome of resolve_seat.  `seat` is upper-cased but not validated as a name (callers
    run their own check, which decides the exit code).  `problem` says why there is no seat when
    the session must not act at all; a session with no seat and no problem simply named none."""
    seat: str | None
    source: str  # launcher, --as, AGENT_SEAT, AGENT_TAG, default, none
    launcher: str | None = None
    problem: str | None = None


def _clean(raw: Any) -> str:
    return str(raw or "").strip()


def launcher_of(env: Mapping[str, str]) -> str | None:
    """The launcher that started this session, or None for an ordinary session."""
    return _clean(env.get(ENV_LAUNCHER)) or None


def launched(env: Mapping[str, str]) -> bool:
    """A launcher started this session (it set AGENT_LAUNCHER, AGENT_LAUNCH_SEAT, or both)."""
    return bool(launcher_of(env) or _clean(env.get(ENV_LAUNCH_SEAT)))


def resolve_seat(env: Mapping[str, str], *, flag: str | None = None, default: str | None = None,
                 use_tag: bool = True) -> Resolution:
    """Apply the seat precedence.  `flag` is an explicit --as; `default` is the platform default the
    caller knows (never used under a launcher); `use_tag` reads AGENT_TAG as well as AGENT_SEAT."""
    launch_seat = _clean(env.get(ENV_LAUNCH_SEAT)).upper()
    who = launcher_of(env)
    named = [("--as", _clean(flag)), ("AGENT_SEAT", _clean(env.get("AGENT_SEAT")))]
    if use_tag:
        named.append(("AGENT_TAG", _clean(env.get("AGENT_TAG"))))
    if launch_seat:
        for label, raw in named:
            if raw and raw.upper() != launch_seat:
                return Resolution(None, "none", who, (
                    "launcher %s assigned %s, but %s names %s; a launched session keeps its launcher's seat "
                    "(Jay reassigns it in the launcher), so nothing was done"
                    % (who or "(unnamed)", launch_seat, label, raw.upper())))
        return Resolution(launch_seat, "launcher", who)
    if who:
        return Resolution(None, "none", who, (
            "no seat assigned by %s:  AGENT_LAUNCHER is set but AGENT_LAUNCH_SEAT is not, so this launched "
            "session takes no fleet action (no platform default applies under a launcher)" % who))
    for label, raw in named + [("default", _clean(default))]:
        if raw:
            return Resolution(raw.upper(), label)
    return Resolution(None, "none")


def valid_seat(seat: str | None) -> str | None:
    """The seat when it is a plain token (it names a directory), else None."""
    if not seat:
        return None
    seat = seat.strip().upper()
    return seat if _SEAT_RE.fullmatch(seat) else None


# --------------------------------------------------------------------------------------------
# The bot check
# --------------------------------------------------------------------------------------------

def check_bot(me: Mapping[str, Any], seat: str, email: str) -> str | None:
    """None when users/me (`me`) is `seat`'s own bot and answers for the credential's `email`,
    else why not.  The text names accounts and seats, never a key."""
    answered = _clean(me.get("email"))
    if me.get("is_bot") is not True:
        return ("the credential (%s) authenticates as %s, which is not a bot account; a seat posts only as its "
                "own bot, never as a person" % (email, answered or "an unknown user"))
    if answered.casefold() != _clean(email).casefold():
        return ("users/me answered as %s, but the credential names %s; refusing it" % (answered or "nobody", email))
    tag = seat_tag_for(me)
    if tag != seat.upper():
        return ("the credential authenticates as %s (%s), not %s; a seat posts only as its own bot, so nothing "
                "was done.  Use %s's own credential, or check AGENT_SEAT, --as and ZULIP_RC" % (tag, answered, seat, seat))
    return None


# --------------------------------------------------------------------------------------------
# Seat partition (which listener instance serves a seat)
# --------------------------------------------------------------------------------------------

def partition_path(env: Mapping[str, str], repo_root: str = REPO_ROOT) -> str:
    return os.path.expanduser(_clean(env.get(ENV_PARTITION)) or os.path.join(repo_root, PARTITION_RELPATH))


def partition_instance(seat: str, env: Mapping[str, str]) -> tuple[str | None, str | None]:
    """(instance, error):  the instance the partition gives `seat` (mac, server or none), None when
    it does not list the seat, and an error when the file cannot be read (callers fail closed).
    config.load_partition is the daemon's full check; this is the light read the hooks can afford."""
    path = partition_path(env)
    if tomllib is None:  # pragma: no cover
        return None, "Python 3.11 or newer is needed to read %s" % path
    try:
        with open(path, "rb") as fh:
            seats = tomllib.load(fh).get("seats")
    except FileNotFoundError:
        return None, "the seat partition %s is missing" % path
    except (OSError, ValueError) as exc:
        return None, "cannot read the seat partition %s: %s" % (path, exc)
    if not isinstance(seats, dict):
        return None, "the seat partition %s has no [seats] table" % path
    for name, value in seats.items():
        if str(name).upper() == seat.upper():
            return (str(value) if isinstance(value, str) else None), None
    return None, None


def local_instance(env: Mapping[str, str]) -> str:
    """The listener instance on this machine:  AGENT_SYNC_INSTANCE, else mac (Claude Code hooks run
    only on the owner's Mac)."""
    return _clean(env.get(ENV_INSTANCE)) or "mac"


def served_here(seat: str, env: Mapping[str, str]) -> tuple[bool, str]:
    """(served, why not).  A seat gets a lease and an inbox only when the partition gives it to this
    machine's listener instance.  Fails closed when the partition cannot be read."""
    instance, error = partition_instance(seat, env)
    if error:
        return False, error
    here = local_instance(env)
    if instance == here:
        return True, ""
    if instance is None:
        return False, "seat %s is not in the seat partition, so no listener serves it; no inbox here" % seat
    return False, "seat %s is not served by the %s listener (partition:  %s); no inbox here" % (
        seat, "Mac" if here == "mac" else here, instance)
