"""reduced_status v1: the eavesdropper's status.json cut down to names, counts, tokens and times.

The listener daemon (the eavesdropper) writes `status.json` for its own operators.  That document
carries filesystem paths, Zulip user ids, topic keys, session ids, pins, owner ids and the text of
config errors, none of which belongs on an owner's screen or on the wire to the Muster apps.
`reduce_status` is the one place that turns it into the document the apps read.  It is pure and
stdlib only: no file, no network and no clock except the `now` the caller passes.  It is also
kept beside `fleet-ops-control.py` (in `scripts/` of a checkout, flat in the installed folder on
the server) and not in the agent_sync package, which the fleet CLI release ships under a file
budget, so it imports nothing from that package.

The rules that hold for every field:

  * The output holds NAMES, COUNTS, TOKENS and TIMES only.  Never an OS path, a Zulip user id or any
    pin, a post body or topic text, a key or token, a session id, a lease id or an email.  There is
    no free text at all: lease titles (the session titles the owner sees) are never emitted,
    by the design owner's privacy ruling, so nothing a title could hold can reach the output.
  * Every string is bounded and matched against a conservative character set.  A value that does
    not fit is dropped to null (or the entry is omitted): it is never repaired, never truncated
    into something that still carries the raw input, and never echoed.  A TOKEN is at most 24
    ASCII letters, digits, hyphens and underscores, starting with a letter or digit, with at
    least one letter, and not shaped like a key (a long unbroken run, a run of seven or more
    digits, a known key prefix); so a host name, a file name, a version or an id is never one.
  * Nothing here raises.  A document of the wrong type, a field of the wrong type, a hostile string
    or a hostile number each degrade to null or omission, and a seat that cannot be reduced is
    omitted without costing the others.  So every scalar below can be null when the source value
    is missing or malformed;  a real daemon's document never produces one, and a reader decodes
    them as optionals.
  * A flag is true or false only when the source value is a JSON boolean.  `1`, `0`, `1.0`, `0.0` and
    strings such as "true" are malformed, not truthy: a flag typed `bool | null` reads as null, and
    a flag that only switches something on (`known`, `fallback.stands_in`) reads as not set.  The
    daemon writes booleans, so no real document is affected, and the check is by identity on
    purpose (`is True`, never `== 1`).
  * Lists are capped (seats, leases per seat, names), so a hostile document cannot make a large
    answer.

The document:

    {"schema": 1, "generated_at": ISO8601Z, "instance": "server" | "mac" | null,
     "status_updated": ISO8601Z | null,
     "operator_voice": {"available": bool | null, "code": code | null, "since": ISO | null},
     "pause": {"paused": bool | null, "since": ISO | null, "wakes_only": bool | null,
               "set_by": token | null},
     "pause_file_error": "invalid" | null,
     "seats": [{"seat": name, "bot": token | null, "connected": bool | null,
                "running": "running" | "stopped" | "unknown",
                "wake": "inbox" | "muse" | "http" | "claude" | "openrouter" | "mcode" |
                        "fallback" | "other",
                "paused": bool | null, "wakes_24h": int | null, "held_back": int | null,
                "jobs": int | null, "last_event": ISO | null,
                "quota": {"known": bool, "exhausted": bool | null, "remaining": int | null,
                          "reset_at": ISO | null, "used": null, "cap_day": null,
                          "cap_hour": null, "source": token | null, "reason": token | null},
                "leases": [{"letter": "A".."ZZ" | null, "platform": token | null,
                            "alive": bool | null, "pending": int | null,
                            "verified_host": "mac" | "server" | "cloud" | null,
                            "why": token | null, "topics": int | null,
                            "watcher": bool | null}],
                "letters": {"known": bool, "why": "partial" | null}}],
     "skipped": [{"seat": name, "reason": token}],
     "disabled": [name]}

Where each field comes from (`Daemon.status()` in daemon.py, which this module reads and never
changes):

  * `generated_at`: the caller's `now`.  `instance`: the caller's, else the source's own
    `instance`, else null.  `status_updated`: the source's `updated`, so a reader can tell a stale
    document from a current one.
  * `operator_voice` is the source's `operator_voice` block (`OperatorVoice.snapshot()`, AFC-602):
    `available` is true or false once the daemon's Operator voice has finished a build, null
    before the first one; `code` is the reason token of a failure (lower-case snake_case, at most
    48 characters, so `operator_credential_rejected` fits); `since` is when that answer began.
    Each is validated on its own and a bad one is null.  A block that is absent (a listener from
    before the block) or is not an object leaves all three null, exactly as a block whose
    `available` is null does, so the reduced document cannot tell those two apart.
  * `running` is DERIVED, because the source has no field that means "this seat's listener runs".
    `running` in the source is the id of the wake that is executing right now, which is a
    different thing and is not used.  The derivation is three states: when the source document
    is older than STALE_AFTER_SECONDS, undated, or dated more than FUTURE_SKEW_SECONDS ahead of the
    reader's clock (the document and its reader share a host, so only rounding and the order of a
    write and a read can put it ahead) nothing in it can be
    called current, so every seat is "unknown";  otherwise a seat whose `connected` is true is
    "running", false is "stopped" (the listener is up and this seat's queue is down, retrying),
    and anything else is "unknown".  The daemon rewrites the file every two seconds, so five
    minutes is a stopped or wedged listener and not a slow one.
  * `wake` is the source's configured wake kind when it is one of the known kinds, else "other".
    It is "fallback" while the seat's `fallback.stands_in` is true (the primary cannot run and the
    fallback answers in its place).  "mcode" is in the vocabulary but is never produced: the
    source carries no mcode fallback.
  * `paused` is true when the source's `paused` (a full pause) or `wakes_paused` (a pause that
    only stops wakes) is true, false when they say false, else null.  The top-level `pause` is the
    global kill switch only (the `"*"` entry of pause.json, with its `ts`, `wakes_only` and `by`);
    a pause on one seat shows on that seat.  `set_by` is the pause's `by` ("cli", "owner-dm") when it
    is a short token.
  * `held_back` is the source's `held_back.count`.  `last_event` is `last_event` (epoch seconds).
  * `quota` is the provider quota the wake gate reads (the CodeCaps snapshot), and only for a seat
    whose wake names a provider.  A seat the source gives no quota is `known: false` with nulls.
    `exhausted` is the producer's verdict and is null unless `known` (an absent reading is not an
    exhausted one).  `remaining` is a percent, rounded to an integer in 0 to 100.  `reset_at` is
    normalised to UTC.
  * A lease's fields are the source lease row's `letter`, `platform`, `alive`, `pending`,
    `verified_host`, `topics` and `watcher`;  `why` is the source's liveness reason mapped through
    a fixed table to a token ("pid gone" becomes "pid_gone"), and a reason outside the table is
    null.  The lease id and the session id are never copied.
  * Lease titles are never emitted.  The source's lease `title` is not read and the output has no
    lease `title` key, whatever the source holds there (a path, an email, a key, an id or a plain
    title alike).
  * `letters` says whether the session letters of the seat's leases are known.  From the document
    alone only two things can be told: every lease has a letter (or the seat has no lease), which
    is `known: true`, or some do and some do not, which is "partial".  No lease with a letter is
    `known: false` with a null `why`.
  * `skipped` is the source's `skipped_seats`.  Its problems are config error text, so the first
    problem of each seat is classified into a fixed vocabulary and the text is dropped.
    `disabled` is the source's `disabled`.

What the source does NOT carry, and so is null here (or never produced) whatever the document says:

  * `operator_voice`'s `last_post_at`, `last_error_code` and `last_error_at`: the daemon's block has
    only `available`, `code` and `since`, so these three are omitted.
  * `quota.used`, `quota.cap_day` and `quota.cap_hour`: the CodeCaps verdict has no such
    counts.  The seat's wake count caps (`budget.wakes_per_day`, `wakes_per_hour`) are a different
    thing and are not mapped in here.
  * `quota.reason` is free text in the source (`fileSkipReason`), so it is kept only when it is
    already a short token, and is otherwise null.
  * `letters.why` "registry_unavailable" and "scope_miss": the source says only that a lease has
    no letter, not why (the cause goes to the listener log), so neither is produced.
  * `pause_file_error` "unreadable": the source reads an unreadable or invalid pause file as an
    empty one, so the document cannot tell them apart.  "invalid" is produced only when the
    source's own `pause` value is the wrong shape.

Deliberately never read: `config_errors`, `reload_refused`, `reload_at`, `partition_file`,
`owner_user_id`, `loop_guard`, `chat_nudges`, `muse_bridge`, `pid`, `version`, `started`, each
seat's `creds`, `cursor`, `claude`, `muse`, `routine`, `pin`, `pinned`, `model`, `red`, `live`,
`budget`, `routes_24h`, `route_enabled`, `provider` and `fallback` (beyond `stands_in`), and a
lease's `lease_id`, `session_id` and `title`.
"""

from __future__ import annotations

import datetime
import math
import re
import time
from typing import Any

SCHEMA = 1
INSTANCES = ("server", "mac")

# The daemon rewrites status.json every two seconds (STATUS_TICK_SECONDS), so five minutes is a
# listener that stopped or wedged, not one that is slow.
STALE_AFTER_SECONDS = 300.0
# How far ahead of the reader's clock a document may be dated and still be current.  The document
# and its reader share a host, so only rounding and the order of a write and a read can put it
# ahead.
FUTURE_SKEW_SECONDS = 5.0

MAX_SEATS = 96
MAX_LEASES = 16
MAX_NAMES = 96
MAX_SKIPPED = 64
COUNT_MAX = 1_000_000_000
TOKEN_MAX = 24
CODE_MAX = 48

_EPOCH_MIN = 946684800.0  # 2000-01-01: anything earlier is not a time the daemon wrote
_EPOCH_MAX = 4102444800.0  # 2100-01-01

# Every pattern is used with `fullmatch`, never `match`, because `$` would accept a trailing
# newline.  The classes are spelled out (never \d or \w, which match other scripts' digits).
_SEAT_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,23}")  # no dot: no host name, file or version
_LETTER_RE = re.compile(r"[A-Z]{1,2}")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")
_ISO_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,9})?"
    r"(?:Z|([+-])([0-9]{2}):([0-9]{2}))"
)

# What a key or token looks like, so a hostile value in a token field is dropped even when it
# would otherwise fit the character set.  A vendor prefix is distinctive enough to stand alone;
# a plain word prefix (`sk-`, `key_`, `Bearer.`) counts only when what follows it looks like a
# secret too (`_tail_secretish`), so `key-rotation-plan` is not a key.
_RUN_RE = re.compile(r"[A-Za-z0-9]{20,}")
_VENDOR_PREFIX_RE = re.compile(
    r"gh[pousr]_[A-Za-z0-9]{6,}"
    r"|github_pat_[A-Za-z0-9_]{6,}"
    r"|glpat-[A-Za-z0-9_-]{6,}"
    r"|xox[a-z]-[A-Za-z0-9-]{6,}"
    r"|shpat_[A-Za-z0-9]{6,}"
    r"|npm_[A-Za-z0-9]{6,}"
    r"|eyj[A-Za-z0-9_-]{6,}",
    re.IGNORECASE,
)
_AWS_PREFIX_RE = re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{6,}|AIza[A-Za-z0-9_-]{6,}")
_WORD_PREFIX_RE = re.compile(r"(?:sk|pk|rk|tok|key|secret|bearer|pat)[-_.]", re.IGNORECASE)
_ST_PREFIX_RE = re.compile(r"st\.", re.IGNORECASE)
_LABEL_SPLIT_RE = re.compile(r"[:=]")

_WAKES = ("inbox", "muse", "http", "claude", "openrouter", "mcode", "fallback")
_HOSTS = ("mac", "server", "cloud")
# The source's liveness reasons (`Daemon.lease_alive`), and the token each one becomes.
_LEASE_WHY = {
    "ok": "ok",
    "wait grace": "wait_grace",
    "pid gone": "pid_gone",
    "pid reused": "pid_reused",
    "ended": "ended",
    "no lease": "no_lease",
    "no heartbeat": "no_heartbeat",
}
_NOT_INTERACTIVE = "not an interactive claude"

# The first matching fragment of a skipped seat's first config problem picks its reason.
_SKIP_REASONS = (
    ("not a valid seat section", "invalid_section"),
    (".instance must", "invalid_instance"),
    (".enabled must", "invalid_enabled"),
    (".creds", "invalid_credentials"),
    ("OPERATOR service requires", "invalid_credentials"),
    (".bot must", "invalid_bot"),
    ("shared with another seat", "shared_environment"),
    ("environment variable", "invalid_environment"),
    ("_env", "invalid_environment"),
)

_DIGITS_RE = re.compile(r"[0-9]{7,}")
_WORD_EDGE = ".,:;!?'\"()[]{}<>*#"
_SEGMENT_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")


# ---- scalars -----------------------------------------------------------------------------------


def _bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _count(value: Any) -> int | None:
    """A non-negative integer, capped.  A bool, a float and a negative number are not counts."""
    if type(value) is not int or value < 0:
        return None
    return min(value, COUNT_MAX)


def _percent(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
        return max(0, min(100, int(round(value))))
    except (OverflowError, ValueError):
        return None


def _epoch(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or not _EPOCH_MIN <= value <= _EPOCH_MAX:
            return None
    except (OverflowError, ValueError):
        return None
    return float(value)


def _stamp(epoch: float) -> str:
    moment = datetime.datetime.fromtimestamp(int(epoch), datetime.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _when(value: Any) -> str | None:
    """UTC ISO 8601 with a Z, from epoch seconds or a strict ISO 8601 string, else null."""
    if isinstance(value, str):
        return _when_iso(value) if len(value) <= 40 else None
    epoch = _epoch(value)
    return _stamp(epoch) if epoch is not None else None


def _when_iso(text: str) -> str | None:
    found = _ISO_RE.fullmatch(text)
    if found is None:
        return None
    try:
        year, month, day, hour, minute, second = (int(found.group(i)) for i in range(1, 7))
        moment = datetime.datetime(year, month, day, hour, minute, second, tzinfo=datetime.UTC)
        if found.group(7):
            hours, minutes = int(found.group(8)), int(found.group(9))
            if hours > 23 or minutes > 59:
                return None
            offset = datetime.timedelta(hours=hours, minutes=minutes)
            moment = moment - offset if found.group(7) == "+" else moment + offset
        epoch = moment.timestamp()
    except (ValueError, OverflowError):
        return None
    return _stamp(epoch) if _EPOCH_MIN <= epoch <= _EPOCH_MAX else None


def _has_case_mix(run: str, share: float, swaps: int) -> bool:
    """Both cases in strength (at least `share` of the letters each) and switching often."""
    letters = [char for char in run if char.isalpha()]
    if len(letters) < 4:
        return False
    upper = sum(1 for char in letters if char.isupper())
    changes = sum(1 for a, b in zip(letters, letters[1:]) if a.isupper() != b.isupper())
    return (
        upper / len(letters) >= share
        and (len(letters) - upper) / len(letters) >= share
        and changes >= swaps
    )


def _tail_secretish(tail: str) -> bool:
    """What follows a word prefix looks like a key: eight or more letters and digits, mixing
    digits with letters or mixing upper and lower case."""
    run = "".join(_SEGMENT_SPLIT_RE.split(tail))
    if len(run) < 8:
        return False
    if any(char.isdigit() for char in run) and any(char.isalpha() for char in run):
        return True
    return _has_case_mix(run, 0.25, 3)


def _has_key_prefix(text: str) -> bool:
    """A vendor prefix, or a word prefix (sk-, key_, Bearer.) followed by something key-shaped,
    at the start of the text or after a label (`key:` or `token=`) inside it."""
    for part in _LABEL_SPLIT_RE.split(text[:600]):
        part = part.strip(_WORD_EDGE)
        if _VENDOR_PREFIX_RE.match(part) or _AWS_PREFIX_RE.match(part):
            return True
        found = _WORD_PREFIX_RE.match(part)
        if found and _tail_secretish(part[found.end() :]):
            return True
        found = _ST_PREFIX_RE.match(part)
        if found and len(part) - found.end() >= 16 and _tail_secretish(part[found.end() :]):
            return True
    return False


def _secret_shaped(text: str) -> bool:
    """A long unbroken run, a run of digits (an id) or a key prefix."""
    return bool(_RUN_RE.search(text) or _DIGITS_RE.search(text) or _has_key_prefix(text))


def _token(value: Any) -> str | None:
    """A short ASCII token (a bot name, a source word) with a letter in it, else null."""
    if not isinstance(value, str) or len(value) > TOKEN_MAX:
        return None
    if not _TOKEN_RE.fullmatch(value) or _secret_shaped(value):
        return None
    return value if any(char.isalpha() for char in value) else None


def _name(value: Any) -> str | None:
    """A seat name, in the shape the daemon's own config requires, with a letter in it."""
    if not isinstance(value, str) or len(value) > 32:
        return None
    if not _SEAT_RE.fullmatch(value) or _secret_shaped(value):
        return None
    return value if any(char.isalpha() for char in value) else None


def _code(value: Any) -> str | None:
    """A failure's reason code: lower-case snake_case, at most CODE_MAX characters, not key-shaped."""
    if not isinstance(value, str) or len(value) > CODE_MAX:
        return None
    if not _CODE_RE.fullmatch(value) or _secret_shaped(value):
        return None
    return value


def _letter(value: Any) -> str | None:
    if isinstance(value, str) and len(value) <= 2 and _LETTER_RE.fullmatch(value):
        return value
    return None


# ---- pieces of the document -----------------------------------------------------------------------


def _operator_voice(raw: Any) -> dict[str, Any]:
    """The source's `operator_voice` block, each field validated; all null when it is absent."""
    out: dict[str, Any] = {"available": None, "code": None, "since": None}
    if isinstance(raw, dict):
        out["available"] = _bool(raw.get("available"))
        out["code"] = _code(raw.get("code"))
        out["since"] = _when(raw.get("since"))
    return out


def _pause(raw: Any) -> tuple[dict[str, Any], str | None]:
    out: dict[str, Any] = {"paused": None, "since": None, "wakes_only": None, "set_by": None}
    if raw is None:
        return out, None
    if not isinstance(raw, dict):
        return out, "invalid"
    error = "invalid" if any(not isinstance(v, dict) for v in raw.values()) else None
    entry = raw.get("*")
    if isinstance(entry, dict):
        out.update(
            paused=True,
            since=_when(entry.get("ts")),
            wakes_only=_bool(entry.get("wakes_only")),
            set_by=_token(entry.get("by")),
        )
    else:
        out["paused"] = False
    return out, error


def _seat_paused(row: dict[str, Any]) -> bool | None:
    full, wakes = row.get("paused"), row.get("wakes_paused")
    if full is True or wakes is True:
        return True
    if full is False and (wakes is False or wakes is None):
        return False
    return None


def _wake(row: dict[str, Any]) -> str:
    fallback = row.get("fallback")
    if isinstance(fallback, dict) and fallback.get("stands_in") is True:
        return "fallback"
    wake = row.get("wake")
    if isinstance(wake, str) and wake in _WAKES:
        return wake
    return "other"


def _running(connected: bool | None, fresh: bool) -> str:
    if not fresh or connected is None:
        return "unknown"
    return "running" if connected else "stopped"


def _quota(raw: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "known": False,
        "exhausted": None,
        "remaining": None,
        "reset_at": None,
        "used": None,
        "cap_day": None,
        "cap_hour": None,
        "source": None,
        "reason": None,
    }
    if not isinstance(raw, dict):
        return out
    out["known"] = raw.get("known") is True
    out["source"] = _token(raw.get("source"))
    if out["known"]:
        out["exhausted"] = _bool(raw.get("exhausted"))
        out["remaining"] = _percent(raw.get("remaining"))
        out["reset_at"] = _when(raw.get("reset_at"))
        out["reason"] = _token(raw.get("reason"))
    return out


def _why(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    if value in _LEASE_WHY:
        return _LEASE_WHY[value]
    if value.startswith(_NOT_INTERACTIVE):
        return "not_interactive"
    return None


def _lease(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    host = raw.get("verified_host")
    return {
        "letter": _letter(raw.get("letter")),
        "platform": _token(raw.get("platform")),
        "alive": _bool(raw.get("alive")),
        "pending": _count(raw.get("pending")),
        "verified_host": host if isinstance(host, str) and host in _HOSTS else None,
        "why": _why(raw.get("why")),
        "topics": _count(raw.get("topics")),
        "watcher": _bool(raw.get("watcher")),
    }


def _letters(leases: list[Any]) -> dict[str, Any]:
    rows = [lease for lease in leases if isinstance(lease, dict)][:MAX_LEASES]
    lettered = sum(1 for lease in rows if _letter(lease.get("letter")))
    if lettered == len(rows):
        return {"known": True, "why": None}
    return {"known": False, "why": "partial" if lettered else None}


def _seat(name: str, row: dict[str, Any], fresh: bool) -> dict[str, Any]:
    raw_leases = row.get("leases")
    leases = raw_leases[:MAX_LEASES] if isinstance(raw_leases, list) else []
    held = row.get("held_back")
    connected = _bool(row.get("connected"))
    return {
        "seat": name,
        "bot": _token(row.get("bot")),
        "connected": connected,
        "running": _running(connected, fresh),
        "wake": _wake(row),
        "paused": _seat_paused(row),
        "wakes_24h": _count(row.get("wakes_24h")),
        "held_back": _count(held.get("count")) if isinstance(held, dict) else None,
        "jobs": _count(row.get("jobs")),
        "last_event": _when(row.get("last_event")),
        "quota": _quota(row.get("quota")),
        "leases": [item for item in (_lease(lease) for lease in leases) if item is not None],
        "letters": _letters(leases),
    }


def _skip_reason(problems: Any) -> str:
    first = problems[0] if isinstance(problems, list) and problems else None
    if isinstance(first, str):
        for fragment, reason in _SKIP_REASONS:
            if fragment in first[:512]:
                return reason
    return "config_problem"


def _seats(raw: Any, fresh: bool) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    out = []
    for key in sorted(k for k in raw if isinstance(k, str)):
        name = _name(key)
        if name is None or not isinstance(raw[key], dict):
            continue
        try:
            out.append(_seat(name, raw[key], fresh))
        except Exception:  # noqa: BLE001 - one hostile seat must not cost the others
            continue
        if len(out) >= MAX_SEATS:
            break
    return out


def _skipped(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, dict):
        return []
    out = []
    for key in sorted(k for k in raw if isinstance(k, str)):
        name = _name(key)
        if name is not None:
            out.append({"seat": name, "reason": _skip_reason(raw[key])})
        if len(out) >= MAX_SKIPPED:
            break
    return out


def _disabled(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    # Every valid name, once, in order, and only then the cap: the smallest names win, as in
    # `_seats`, whatever the order of a long list.
    names = {name for name in (_name(item) for item in raw) if name is not None}
    return sorted(names)[:MAX_NAMES]


# ---- the entry point ---------------------------------------------------------------------------


def _now(value: Any) -> float:
    epoch = _epoch(value)
    return epoch if epoch is not None else time.time()


def _skeleton(now: float, instance: str | None) -> dict[str, Any]:
    pause, error = _pause(None)
    return {
        "schema": SCHEMA,
        "generated_at": _stamp(now),
        "instance": instance,
        "status_updated": None,
        "operator_voice": _operator_voice(None),
        "pause": pause,
        "pause_file_error": error,
        "seats": [],
        "skipped": [],
        "disabled": [],
    }


def reduce_status(status: Any, now: Any, instance: Any) -> dict[str, Any]:
    """The reduced_status v1 document for one listener's `status.json` document.

    `status` is the parsed `Daemon.status()` document (anything else yields an empty reduced
    document), `now` is epoch seconds from the caller's clock, and `instance` is "server" or
    "mac".  Never raises, and never returns a string it did not build itself or scrub."""
    moment = _now(now)
    where = instance if isinstance(instance, str) and instance in INSTANCES else None
    try:
        if not isinstance(status, dict):
            return _skeleton(moment, where)
        if where is None:
            own = status.get("instance")
            where = own if isinstance(own, str) and own in INSTANCES else None
        updated = _epoch(status.get("updated"))
        fresh = (
            updated is not None and -FUTURE_SKEW_SECONDS <= moment - updated <= STALE_AFTER_SECONDS
        )
        pause, error = _pause(status.get("pause"))
        return {
            "schema": SCHEMA,
            "generated_at": _stamp(moment),
            "instance": where,
            "status_updated": _stamp(updated) if updated is not None else None,
            "operator_voice": _operator_voice(status.get("operator_voice")),
            "pause": pause,
            "pause_file_error": error,
            "seats": _seats(status.get("seats"), fresh),
            "skipped": _skipped(status.get("skipped_seats")),
            "disabled": _disabled(status.get("disabled")),
        }
    except Exception:  # noqa: BLE001 - a hostile document must degrade, never raise
        return _skeleton(moment, where)
