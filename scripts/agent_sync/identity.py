"""Seat precedence and the bot check, shared by every simple-chat entry point.

FLEET-PROTOCOL § Identity Rules (owner 2026-10-09) says a session's seat is the first of:

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
from collections.abc import Mapping
from typing import Any, NamedTuple

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    tomllib = None  # type: ignore[assignment]

ENV_LAUNCHER = "AGENT_LAUNCHER"
ENV_LAUNCH_SEAT = "AGENT_LAUNCH_SEAT"
ENV_ATTACH = "AGENT_SYNC_ATTACH"
ENV_PARTITION = "AGENT_SYNC_PARTITION"
ENV_INSTANCE = "AGENT_SYNC_INSTANCE"
# The launchers the fleet runs (FLEET-PROTOCOL § Identity Rules › Launcher Contract).  Any non-empty
# AGENT_LAUNCHER counts as a launcher; these are the documented values.
LAUNCHERS = ("botfleet", "grok-bot", "agent-sync-wake")
WAKE_LAUNCHER = "agent-sync-wake"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PARTITION_RELPATH = os.path.join("docs", "protocols", "agent-sync-partition.toml")
_SEAT_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}")

# Seat aliases (owner 2026-10-10):  "GROK followed by BUILD, any letter case, with any non-letter,
# non-digit symbols between or after (GROK-BUILD, Grok (Build), grok_build) is an alias for GROK."
# One pattern, matched after trimming, case-insensitively and on ASCII letters and digits only.
# fleet-apps.json carries the same text as the GROK row's `aliasPattern`, for the runtimes that ship
# without this package (the lane tool, the board scripts);  test_seat_alias.py and
# check-fleet-registry.py keep every copy equal.  It never matches GROKBUILDER, grok2build or
# grok-build-bot (a bot email is matched separately, through EMAIL_TAG_OVERRIDES).
GROK_ALIAS_PATTERN = r"^grok[^a-z0-9]*build[^a-z0-9]*$"
_GROK_ALIAS_RE = re.compile(GROK_ALIAS_PATTERN, re.IGNORECASE | re.ASCII)
GROK_SEAT = "GROK"

# Human and legacy spellings accepted anywhere a seat name is resolved.  Keep
# the output canonical so claims, labels, branches and posts carry one key.
SEAT_NAME_ALIASES = {
    "INSTINCT": "WISE",
    "WISE-INSTINCT": "WISE",
    "ECHO-INSTINCT": "WISE",
    "MUSE-ASSIST": "MA",
    "ROB": "MA",
    "ROB-MUSE": "MA",
}


def is_grok_alias(raw: Any) -> bool:
    """True when `raw` is a spelling of the retired GROK-BUILD tag."""
    return bool(_GROK_ALIAS_RE.match(str(raw or "").strip()))


def canonical_seat(raw: Any) -> str:
    """The seat tag a name stands for, upper-cased:  every GROK-BUILD spelling is GROK, anything
    else it resolves published aliases and upper-cases (callers still validate it as a token).  This
    is the one input boundary:  --as, --seat, --to, AGENT_SEAT, AGENT_LAUNCH_SEAT, AGENT_TAG and the
    hooks' platform default all pass through it, and output only ever sees canonical seat keys."""
    text = str(raw or "").strip()
    if _GROK_ALIAS_RE.match(text):
        return GROK_SEAT
    upper = text.upper()
    return SEAT_NAME_ALIASES.get(upper, upper)


# Bot email local parts (minus the "-bot" Zulip appends) whose seat tag is not just the upper-cased
# local part.  Display names are cosmetic (owner 2026-10-07), so labels come from emails, never names.
EMAIL_TAG_OVERRIDES = {
    "muse-assist": "MA",
    "openai-dot": "JET",
    "instinct-bat": "ECHO",
    "instinct-owl": "WISE",  # the bot is instinct-owl-bot@;  WISE is the tag (owner 2026-10-15)
    "grok-build": "GROK",  # terminal Grok and Grok Build are one seat (owner 2026-10-08)
}


def seat_tag_for(user: Mapping[str, Any]) -> str:
    """The seat tag a user or bot signs as: mm-bot@ -> MM, bf-builder-bot@ -> BF-BUILDER,
    compiler-grok-bot@ -> GB-COMPILER, muse-assist-bot@ -> MA.  Humans get their first name."""
    if not user.get("is_bot"):
        first = (str(user.get("full_name") or "").split() or ["USER"])[0]
        return first.upper()
    local = str(user.get("email") or "").split("@", 1)[0].lower()
    local = local.removesuffix("-bot")
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


def resolve_seat(
    env: Mapping[str, str],
    *,
    flag: str | None = None,
    default: str | None = None,
    use_tag: bool = True,
) -> Resolution:
    """Apply the seat precedence.  `flag` is an explicit --as; `default` is the platform default the
    caller knows (never used under a launcher); `use_tag` reads AGENT_TAG as well as AGENT_SEAT."""
    # Every name is canonicalized first (GROK-BUILD in any spelling is GROK), on both sides of the
    # launcher check, so AGENT_LAUNCH_SEAT=GROK with AGENT_SEAT=Grok-Build is the same seat, not a conflict.
    launch_seat = canonical_seat(env.get(ENV_LAUNCH_SEAT))
    who = launcher_of(env)
    named = [("--as", _clean(flag)), ("AGENT_SEAT", _clean(env.get("AGENT_SEAT")))]
    if use_tag:
        named.append(("AGENT_TAG", _clean(env.get("AGENT_TAG"))))
    if launch_seat:
        for label, raw in named:
            if raw and canonical_seat(raw) != launch_seat:
                return Resolution(
                    None,
                    "none",
                    who,
                    (
                        "launcher %s assigned %s, but %s names %s; a launched session keeps its launcher's seat "
                        "(Jay reassigns it in the launcher), so nothing was done"
                        % (who or "(unnamed)", launch_seat, label, canonical_seat(raw))
                    ),
                )
        return Resolution(launch_seat, "launcher", who)
    if who:
        return Resolution(
            None,
            "none",
            who,
            (
                "no seat assigned by %s:  AGENT_LAUNCHER is set but AGENT_LAUNCH_SEAT is not, so this launched "
                "session takes no fleet action (no platform default applies under a launcher)" % who
            ),
        )
    for label, raw in named + [("default", _clean(default))]:
        if raw:
            return Resolution(canonical_seat(raw), label)
    return Resolution(None, "none")


def valid_seat(seat: str | None) -> str | None:
    """The seat when it is a plain token (it names a directory), else None."""
    if not seat:
        return None
    seat = canonical_seat(seat)
    return seat if _SEAT_RE.fullmatch(seat) else None


# --------------------------------------------------------------------------------------------
# Strict seat names and seat apps (fleet-apps.json `seats` and `seatApps`)
# --------------------------------------------------------------------------------------------
#
# canonical_seat() is the lenient input boundary (#457):  it upper-cases anything, so a typo is a
# new seat.  Code that decides something from a seat's name (who owns a claim, which app a vote
# counts for) must not accept a typo, so it calls strict_seat(), which runs canonical_seat() first
# and then refuses a name the registry does not know.  seat_app() says which product a seat runs in.
#
# The data is the top-level `seatApps` key of fleet-apps.json, not a field on the `seats` rows:  a
# dozen tools read `seats` (lanes, skill copies, listener seats), and a BF-* or GB-* row there
# would make each of them.  `seatApps` is read only here.
#
#   bySeat      live row of `seats` -> app          (check-fleet-registry.py wants every live row here)
#   extraSeats  real seats with no `seats` row      (JET, ECHO, WISE:  hosted or owner-run)
#   patterns    families made of a role             (BF-<ROLE> is botfleet, GB-<ROLE> is grok-bot)
#
# Two apps are never one app because they share a model:  GROK (the terminal) and GROK-WEB are two.

ENV_APPS_JSON = "FLEET_APPS_JSON"  # the same override the lane tool reads
APPS_JSON_NAME = "fleet-apps.json"
_APP_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_PATTERN_RE = re.compile(r"[A-Z0-9_*-]+")
_ROLE_CHARS = "[A-Z0-9][A-Z0-9_-]*"  # what one `*` in a pattern stands for:  a non-empty token tail


class UnknownSeat(ValueError):
    """The name is not a live seat:  it is in neither `seats` (not retired), `seatApps.extraSeats` nor
    `seatApps.patterns`.  `seat` is the canonical spelling, `retired` says the registry knows it as a
    retired seat.  Callers that mean "an unknown claimant is an orphan" catch exactly this."""

    def __init__(self, raw: Any, seat: str, retired: bool = False) -> None:
        self.raw = raw
        self.seat = seat
        self.retired = retired
        shown = seat if len(seat) <= 40 else seat[:40] + "..."
        super().__init__(
            "%r is %s"
            % (
                shown,
                "a retired seat in %s" % APPS_JSON_NAME
                if retired
                else "not a live seat in %s (seats, seatApps.extraSeats or seatApps.patterns)"
                % APPS_JSON_NAME,
            )
        )


class SeatRegistryError(RuntimeError):
    """fleet-apps.json cannot answer:  missing, not JSON, no usable `seatApps`, or a live seat with no
    app.  Deliberately NOT an UnknownSeat, so a caller that turns unknown names into orphans can never
    turn an unreadable registry into a mass of orphans."""


class SeatPattern(NamedTuple):
    """One `seatApps.patterns` row:  `match` (BF-*) compiled to `regex`, and the app it gives."""

    regex: re.Pattern[str]
    app: str
    match: str


class SeatRegistry(NamedTuple):
    """The parts of fleet-apps.json that strict_seat and seat_app read."""

    live: frozenset[str]
    retired: frozenset[str]
    by_seat: Mapping[str, str]
    extra: Mapping[str, str]
    patterns: tuple[SeatPattern, ...]  # in file order;  the first match wins
    source: str = ""


def _pattern_regex(match: str) -> re.Pattern[str]:
    """`BF-*` -> BF- followed by a non-empty token.  Only A-Z, 0-9, _, - and * are allowed in a match."""
    if not _PATTERN_RE.fullmatch(match):
        raise SeatRegistryError(
            "seatApps.patterns match %r may hold only A-Z, 0-9, _, - and *" % match
        )
    return re.compile("".join(_ROLE_CHARS if ch == "*" else re.escape(ch) for ch in match))


def _app_name(where: str, value: Any) -> str:
    if not isinstance(value, str) or not _APP_RE.fullmatch(value):
        raise SeatRegistryError(
            "%s must be a lowercase app name like claude-code, not %r" % (where, value)
        )
    return value


def parse_seat_registry(data: Any, source: str = "") -> SeatRegistry:
    """Build a SeatRegistry from already-loaded fleet-apps.json.  Strict about `seatApps` (a malformed
    key raises SeatRegistryError) and tolerant of everything else in the file."""
    if not isinstance(data, Mapping):
        raise SeatRegistryError(
            "%s: expected a JSON object at the top level" % (source or APPS_JSON_NAME)
        )
    rows = [
        r
        for r in (data.get("seats") if isinstance(data.get("seats"), list) else [])
        if isinstance(r, Mapping)
    ]
    live: set[str] = set()
    retired: set[str] = set()
    for row in rows:
        tag = str(row.get("tag") or "").strip().upper()
        if tag:
            (retired if row.get("retired") else live).add(tag)
    apps = data.get("seatApps")
    if not isinstance(apps, Mapping):
        raise SeatRegistryError("%s has no seatApps key" % (source or APPS_JSON_NAME))
    maps: dict[str, dict[str, str]] = {}
    for key in ("bySeat", "extraSeats"):
        raw_map = apps.get(key)
        if not isinstance(raw_map, Mapping):
            raise SeatRegistryError("seatApps.%s must be an object" % key)
        maps[key] = {
            str(tag).strip().upper(): _app_name("seatApps.%s.%s" % (key, tag), app)
            for tag, app in raw_map.items()
        }
    raw_patterns = apps.get("patterns")
    if not isinstance(raw_patterns, list):
        raise SeatRegistryError("seatApps.patterns must be a list")
    patterns = []
    for item in raw_patterns:
        if not isinstance(item, Mapping) or not isinstance(item.get("match"), str):
            raise SeatRegistryError("every seatApps.patterns entry needs a match string and an app")
        patterns.append(
            SeatPattern(
                _pattern_regex(item["match"]),
                _app_name("seatApps.patterns %s" % item["match"], item.get("app")),
                item["match"],
            )
        )
    return SeatRegistry(
        frozenset(live),
        frozenset(retired),
        maps["bySeat"],
        maps["extraSeats"],
        tuple(patterns),
        source,
    )


_registry_cache: dict[str, tuple[tuple[int, int], SeatRegistry]] = {}


def seat_registry_path(env: Mapping[str, str] | None = None) -> str:
    """FLEET_APPS_JSON when set, else fleet-apps.json at the repo root."""
    env = os.environ if env is None else env
    return os.path.expanduser(
        _clean(env.get(ENV_APPS_JSON)) or os.path.join(REPO_ROOT, APPS_JSON_NAME)
    )


def load_seat_registry(
    path: str | None = None, env: Mapping[str, str] | None = None
) -> SeatRegistry:
    """The registry at `path` (default:  seat_registry_path), read again only when the file changes.
    Raises SeatRegistryError when it cannot be read or `seatApps` is malformed."""
    target = path or seat_registry_path(env)
    try:
        stat = os.stat(target)
    except OSError as exc:
        raise SeatRegistryError(
            "cannot read %s: %s" % (target, exc.strerror or type(exc).__name__)
        ) from None
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _registry_cache.get(target)
    if cached and cached[0] == stamp:
        return cached[1]
    import json  # only here:  the Claude hooks import this module on every prompt and never need it

    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise SeatRegistryError("cannot read %s: %s" % (target, type(exc).__name__)) from None
    registry = parse_seat_registry(data, target)
    _registry_cache[target] = (stamp, registry)
    return registry


def _pattern_app(seat: str, registry: SeatRegistry) -> str | None:
    if not _SEAT_RE.fullmatch(seat):
        return None
    for pattern in registry.patterns:
        if pattern.regex.fullmatch(seat):
            return pattern.app
    return None


def strict_seat(raw: Any, *, registry: SeatRegistry | None = None) -> str:
    """The canonical seat tag for `raw`, or UnknownSeat.

    canonical_seat() runs first (every GROK-BUILD spelling is GROK, anything else is trimmed and
    upper-cased).  The result must then be a non-retired row of fleet-apps.json `seats`, a key of
    `seatApps.extraSeats`, or match a `seatApps.patterns` row (BF-<ROLE>, GB-<ROLE>).  Everything else
    raises:  MINIMAX (the tag is MM), a retired seat, a typo, an empty name, a name that is not a token.
    Raises SeatRegistryError when the registry cannot be read, which is not the same thing."""
    seat = canonical_seat(raw)
    reg = registry if registry is not None else load_seat_registry()
    if seat in reg.live or seat in reg.extra or _pattern_app(seat, reg):
        return seat
    raise UnknownSeat(raw, seat, retired=seat in reg.retired)


def seat_app(seat: Any, *, registry: SeatRegistry | None = None) -> str:
    """The app `seat` runs in:  claude-code, codex, cursor, grok-terminal, grok-web, antigravity,
    minimax-code, muse-code, muse-assist, clutch, fx, opencode, grok-bot, chatgpt, instinct, botfleet.

    `seat` goes through strict_seat() first, so an unknown or retired name raises UnknownSeat and is
    never taken for a new app.  A live seat the registry lists no app for raises SeatRegistryError."""
    reg = registry if registry is not None else load_seat_registry()
    name = strict_seat(seat, registry=reg)
    app = reg.by_seat.get(name) or reg.extra.get(name) or _pattern_app(name, reg)
    if not app:
        raise SeatRegistryError(
            "%s lists no app for the seat %s (seatApps.bySeat)"
            % (reg.source or APPS_JSON_NAME, name)
        )
    return app


# --------------------------------------------------------------------------------------------
# The bot check
# --------------------------------------------------------------------------------------------


def check_bot(me: Mapping[str, Any], seat: str, email: str) -> str | None:
    """None when users/me (`me`) is `seat`'s own bot and answers for the credential's `email`,
    else why not.  The text names accounts and seats, never a key."""
    answered = _clean(me.get("email"))
    if me.get("is_bot") is not True:
        return (
            "the credential (%s) authenticates as %s, which is not a bot account; a seat posts only as its "
            "own bot, never as a person" % (email, answered or "an unknown user")
        )
    if answered.casefold() != _clean(email).casefold():
        return "users/me answered as %s, but the credential names %s; refusing it" % (
            answered or "nobody",
            email,
        )
    tag = seat_tag_for(me)
    if tag != seat.upper():
        return (
            "the credential authenticates as %s (%s), not %s; a seat posts only as its own bot, so nothing "
            "was done.  Use %s's own credential, or check AGENT_SEAT, --as and ZULIP_RC"
            % (tag, answered, seat, seat)
        )
    return None


# --------------------------------------------------------------------------------------------
# Seat partition (which listener instance serves a seat)
# --------------------------------------------------------------------------------------------


def partition_path(env: Mapping[str, str], repo_root: str = REPO_ROOT) -> str:
    return os.path.expanduser(
        _clean(env.get(ENV_PARTITION)) or os.path.join(repo_root, PARTITION_RELPATH)
    )


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
        return (
            False,
            "seat %s is not in the seat partition, so no listener serves it; no inbox here" % seat,
        )
    return False, "seat %s is not served by the %s listener (partition:  %s); no inbox here" % (
        seat,
        "Mac" if here == "mac" else here,
        instance,
    )
