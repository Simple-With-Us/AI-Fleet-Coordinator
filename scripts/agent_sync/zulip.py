"""Zulip access for simple-chat: seat and credential loading, a urllib-only HTTP client, event queues.

Credential order (first hit wins, nothing is merged):
  1. --rc PATH
  2. env ZULIP_RC=PATH
  3. $AGENT_SYNC_SECRETS_DIR/<Title>-zuliprc, only when that variable is set (an explicit override
     directory;  there is no default directory any more)
  4. env ZULIP_EMAIL + ZULIP_API_KEY + ZULIP_SITE  (cloud seats)
  5. Infisical Fleet-OPS /zulip  (prod), the resolved seat's own three keys <BOT>_ZULIP_EMAIL, _KEY and
     _SITE, read through `fleet-secrets` (off with AGENT_SYNC_INFISICAL=0;  OPS-818, AFC-359)
No credential folder is read (AFC-359:  the old folder is retired).  A .env file, ./zuliprc and
~/.zuliprc are never read either.  Values fetched from Infisical stay in this process's memory only
(never on disk), are fetched once per process and reused for INFISICAL_CACHE_TTL seconds, and a
failed refresh falls back to the values this process already holds.  A failure with nothing held is
remembered for INFISICAL_FAILURE_BACKOFF seconds, so an outage costs one timeout, not one per seat.

The API key lives only in `Credentials.key` (hidden from repr) and in the Authorization header.
Every error message that leaves this module is scrubbed of the key and of the base64 token, so
a server that echoes the header cannot leak it into stdout, stderr or an exception string.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import base64
import configparser
import http.client
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .identity import canonical_seat
from .operator_status_text import TAG as _OPERATOR_STATUS_TAG
from .operator_status_text import is_status_text
from .textfmt import fix_outbound

__all__ = [
    "DEFAULT_CHANNEL",
    "DM_GUARD_PATHS",
    "DM_GUARD_TYPES",
    "MAX_TOPIC_LENGTH",
    "REALM",
    "RETIRED_CHANNEL_NAMES",
    "USER_AGENT",
    "AgentSyncError",
    "ApiError",
    "CredentialError",
    "Credentials",
    "DmGuardRefused",
    "EventQueue",
    "NetworkError",
    "QueueExpired",
    "RealmMismatch",
    "UsageError",
    "ZulipClient",
    "check_dm_guard",
    "check_owner_pinned",
    "credential_file_name",
    "default_rc_path",
    "direct_recipient_sets",
    "env_credentials",
    "fleet_secrets_bin",
    "infisical_credentials",
    "infisical_has",
    "infisical_key_names",
    "infisical_key_names_for_stem",
    "infisical_names",
    "infisical_project_id",
    "infisical_roster",
    "infisical_seats",
    "message_channel",
    "message_topic",
    "normalise_seat",
    "outbound_params",
    "override_secrets_dir",
    "prefetch_infisical",
    "read_zuliprc",
    "realm_url",
    "resolve_credentials",
    "seat_from_infisical_prefix",
    "seat_from_rc_path",
    "session_tag",
    "verify_realm",
]

REALM = "https://simplewithus.zulipchat.com"
ENV_REALM = "AGENT_SYNC_REALM"
ENV_SECRETS_DIR = "AGENT_SYNC_SECRETS_DIR"
ENV_INFISICAL = "AGENT_SYNC_INFISICAL"  # 0, false, off or no turns the Infisical fallback off
# The Fleet-OPS project id is an infrastructure identifier and this repo is public:  it is never committed.
# It comes from env INFISICAL_FLEET_OPS_PROJECT_ID, or the first line of ~/.agent-sync/fleet-ops-project-id.
ENV_INFISICAL_PROJECT = "INFISICAL_FLEET_OPS_PROJECT_ID"
INFISICAL_PROJECT_FILE = ".agent-sync/fleet-ops-project-id"
INFISICAL_PATH = "/zulip"
# The fleet's Infisical reader (scripts/fleet_secrets, installed as ~/.local/bin/fleet-secrets).  Tests point
# FLEET_SECRETS_BIN at a fake.  Its auth chain (System keychain machine identity) works under launchd and pm2.
ENV_FLEET_SECRETS_BIN = "FLEET_SECRETS_BIN"
FLEET_SECRETS_TIMEOUT = (
    15.0  # interactive commands wait at most this long for one fleet-secrets call
)
# The listener is not interactive, and it starts right after a runtime update, often while the Mac's load
# average is above 100:  a 15 second cap left two seats refused for five minutes on Mon, Oct 19 (AFC-359).
DAEMON_FLEET_SECRETS_TIMEOUT = 90.0
INFISICAL_CACHE_TTL = 600.0
# After a failed fetch with nothing held, the same request fails at once for this long instead of paying the
# timeout again (an Infisical outage must not cost every seat or every identity candidate its own timeout).
INFISICAL_FAILURE_BACKOFF = 120.0
# Key-name prefixes in /zulip that are never a seat:  the owner's own account and the integration bots.
NOT_SEAT_PREFIXES = frozenset(
    {
        "JAY",
        "OWNER",
        "REALM_ADMIN",
        "ZULIPADMIN",
        "BETTERSTACK",
        "DATADOG",
        "LINEAR",
        "PAGERDUTY",
        "SENTRY",
    }
)
ENV_CHANNEL = "AGENT_SYNC_CHANNEL"
# The fleet's live coordination channel.  The realm renamed it on Oct 22, 2026, and a
# hardcoded name that no longer exists makes every seat's post fail with
# STREAM_DOES_NOT_EXIST, so the built-in default follows the realm and the name is
# settable for a deployment that puts the fleet somewhere else.
DEFAULT_CHANNEL = "work-in-progress"
# The Operator seat:  the admin [Operator] Zulip bot (role 200) and the GitHub identity
# `swu-operator` (owner decision, Oct 22 2026).  It is the only seat whose key may be an
# administrator's;  every other seat, and the owner for every seat, stays moderator/member only.
OPERATOR_SEAT = "OPERATOR"
# The Operator bot's Zulip user id.  A production user id is private inventory, so none is
# committed:  the deployment sets it and the runtime reads it at call time.  The same
# variable name carries the Operator's identity for the service, so one setting serves both.
ENV_OPERATOR_USER_ID = "OPERATOR_ZULIP_USER_ID"

#: The shortest credential the preview may describe in full.  Below it the preview shows one
#: leading character only, because eight plus four of a shorter value discloses nearly all of it.
MASK_MIN_LENGTH = 24


def default_channel(env: Mapping[str, str] | None = None) -> str:
    """The channel a post goes to when the caller names none.

    The override is read from the environment mapping the runtime already carries, not
    from the process environment at import, so a caller that hands the runtime an
    explicit environment (every test does) gets the channel it asked for.
    """
    if env:
        override = env.get(ENV_CHANNEL)
        if override and override.strip():
            return override.strip()
    return DEFAULT_CHANNEL


#: The names the fleet's coordination channel has had.  The realm renamed it to
#: Work-In-Progress on Oct 22, 2026 and left the old name behind as a tombstone stream
#: marked DO NOT USE, so a caller that still names it lands in a stream nobody reads.
#: A name here means "wherever the fleet's coordination channel is now".
RETIRED_CHANNEL_NAMES = frozenset({"agent-sync"})


def resolve_channel(name: str | None, env: Mapping[str, str] | None = None) -> str:
    """The live channel a name refers to.

    A retired name resolves to the effective default, so the fleet's coordination channel
    stays one channel however it is named.  A deployment whose realm still calls that
    channel agent-sync keeps the name:  its effective default is the same name, so
    nothing changes for it.
    """
    text = str(name or "").strip()
    if text.startswith("#"):
        text = text[1:].strip()
    if not text:
        return default_channel(env)
    if text.casefold() not in RETIRED_CHANNEL_NAMES:
        return text
    target = default_channel(env)
    if target.casefold() == text.casefold():
        return text
    return target


def operator_user_id(env: Mapping[str, str] | None = None) -> int | None:
    """The Operator bot's Zulip user id, read from the environment at call time.

    None when the variable is unset or unreadable, so a caller fails closed with the
    variable's name instead of guessing an id.  Only an id, never a key, so it is safe to
    log."""
    raw = (env if env is not None else os.environ).get(ENV_OPERATOR_USER_ID)
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def masked_credential(key: Any) -> str:
    """A credential preview safe to hand back to a caller:  the first eight and the last
    four characters only.  A refused key has to be identifiable without ever being written
    out in full, and that is what the preview is for.

    A short value gets one leading character and nothing else:  at 24 characters the preview
    still hides at least half the secret, but below it the first eight and the last four
    would disclose most of the value (a 13-character key showed 12 of its 13 characters),
    and an error that carries the preview may reach a log.  So a value under the threshold
    shows one character, which is enough to tell two refusals apart."""
    text = str(key or "")
    if not text:
        return "missing"
    if len(text) < MASK_MIN_LENGTH:
        return "%s\u2026" % text[:1]
    return "%s\u2026%s" % (text[:8], text[-4:])


MAX_TOPIC_LENGTH = 60
USER_AGENT = "agent-sync/1 (fleet)"

# Seat name -> credential file stem, for names the Title Case default would get wrong.  The stems
# are the ones scripts/zulip_provision_bots.py writes (its ROSTER), which is where the files come from.
# MM and MC are fleet-apps.json tags whose bots are named MiniMax and Muse Code.  MA (Muse Assist) and
# GROK-BOT have no provisioned bot yet, so they are not listed; add them when the files exist.
# File stems follow the seat tag (owner 2026-10-07): split on hyphens, parts of two letters or
# fewer stay upper case, longer parts are Title Case.  CLAUDE -> Claude, GROK-BUILD -> Grok-Build,
# BF-BUILDER -> BF-Builder, AG/FX/MM/MC/MA stay as they are.  Add an override only for a file that
# breaks the rule.
SEAT_FILE_OVERRIDES: dict[str, str] = {
    "GROK": "Grok-Build",  # GROK posts as grok-build-bot@ (owner 2026-10-08)
    "OPENCODE": "OpenCode",  # the credential file is OpenCode-zuliprc (Sat, Oct 10);  the rule would say Opencode
}
SHORT_PART_MAX = 2

MAX_RATE_LIMIT_RETRIES = 3
RETRY_AFTER_CAP = 30.0
GET_RETRY_BACKOFF = (1.0, 3.0)
GATEWAY_STATUSES = frozenset(
    {502, 503, 504}
)  # the proxy in front of Zulip restarting or timing out

_SEAT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_LOOPBACK_NAMES = {"localhost"}
# Endpoints whose `content` is message text this process is sending (a new message, an edit, a
# scheduled message).  Reads, `messages/render` and the rest are not outbound text.
_OUTBOUND_TEXT_PATH = re.compile(r"(?:messages|scheduled_messages)(?:/\d+)?")


class AgentSyncError(Exception):
    """Base error.  `exit_code` is what the CLI returns."""

    exit_code = 1


class UsageError(AgentSyncError):
    exit_code = 2


class CredentialError(AgentSyncError):
    exit_code = 3


class RealmMismatch(CredentialError):
    """The credential's own site is a different host than the realm it would be used against.

    A `CredentialError`, so every caller that already refuses a bad credential still refuses
    this one;  the narrower type says why:  the key is fine, it belongs to another server, so
    the caller skips the target instead of failing the seat."""

    exit_code = 3


class ApiError(AgentSyncError):
    """Zulip answered with an error.  `code` is Zulip's machine code (e.g. BAD_EVENT_QUEUE_ID)."""

    exit_code = 5

    def __init__(
        self,
        msg: str,
        *,
        code: str | None = None,
        status: int | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__("%s [%s]" % (msg, code) if code else msg)
        self.msg = msg
        self.code = code
        self.status = status
        self.data = dict(data or {})


class NetworkError(AgentSyncError):
    """No usable answer.  `timeout` is True for a read or connect timeout.  `maybe_sent` is True
    when the request may have reached the server (a timeout or reset on a non-GET request)."""

    exit_code = 6

    def __init__(self, msg: str, *, timeout: bool = False, maybe_sent: bool = False) -> None:
        super().__init__(msg)
        self.timeout = timeout
        self.maybe_sent = maybe_sent


class DmGuardRefused(AgentSyncError):
    """A direct message the owner-in-the-conversation guard refused (AFC-469).

    The rule (owner, Oct 23 2026):  a bot may send a direct message only when Jay is an actual
    participant in the conversation.  Participation is the authenticated Zulip **user id**:  a
    display name that looks like the owner is not participation, and mentioning Jay in the message
    text is not participation either.

    An AgentSyncError, so the CLI exits 3 (a refusal) and not 2 (a malformed command):  a caller can
    tell "the guard said no" from "you typed it wrong".  `code` is the machine-readable reason, the
    same shape dm_policy.DmRefused and ApiError use, and it rides in the message text so a caller
    that only prints the error still sees it."""

    exit_code = 3
    code = "dm_owner_not_participant"

    def __init__(self, msg: str, *, code: str | None = None) -> None:
        self.code = code or type(self).code
        super().__init__("%s [%s]" % (msg, self.code))
        self.msg = msg


class QueueExpired(Exception):
    """The event queue is gone on the server (BAD_EVENT_QUEUE_ID).  Register a new one."""


# --------------------------------------------------------------------------------------------
# Seat and session identity
# --------------------------------------------------------------------------------------------


def normalise_seat(name: str) -> str:
    """Upper-case a seat name, refusing anything that is not a plain token (it names a directory).
    Every spelling of the retired GROK-BUILD tag (GROK-BUILD, Grok-Build, grok_build, "Grok (Build)",
    GROK.BUILD ...) is the GROK seat (owner 2026-10-10;  identity.canonical_seat), and is canonicalized
    before the token check because several of those spellings are not tokens."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise UsageError(
            "seat name %r is not valid; use letters, digits, hyphen or underscore (e.g. CLAUDE)"
            % cleaned
        )
    canonical = canonical_seat(cleaned)
    if canonical != cleaned.upper():
        return canonical  # an alias:  the canonical tag is a token by construction
    if not _SEAT_RE.match(cleaned):
        raise UsageError(
            "seat name %r is not valid; use letters, digits, hyphen or underscore (e.g. CLAUDE)"
            % cleaned
        )
    return canonical


def credential_file_name(seat: str) -> str:
    """CLAUDE -> Claude-zuliprc, BF-BUILDER -> BF-Builder-zuliprc, MM -> MM-zuliprc.  Parts of
    SHORT_PART_MAX letters or fewer stay upper case; longer parts are Title Case."""
    seat = seat.upper()
    stem = SEAT_FILE_OVERRIDES.get(seat)
    if stem is None:
        parts = seat.split("-")
        stem = "-".join(
            part if len(part) <= SHORT_PART_MAX else part[:1].upper() + part[1:].lower()
            for part in parts
        )
    return stem + "-zuliprc"


def seat_from_rc_path(path: str | os.PathLike[str]) -> str | None:
    """Derive a seat from a file named <Seat>-zuliprc, or None when the name does not say.  The
    inverse of credential_file_name: MM-zuliprc is the seat MM, BF-Builder-zuliprc is BF-BUILDER."""
    name = Path(path).name
    if name.lower().endswith("-zuliprc") and len(name) > len("-zuliprc"):
        stem = name[: -len("-zuliprc")]
        try:
            seat = normalise_seat(stem)
        except UsageError:
            return None
        for known, known_stem in SEAT_FILE_OVERRIDES.items():
            if known_stem.upper() == seat:
                return known
        return seat
    return None


#:  How many characters of the session id make the tag.  It is a name for the id, so an id must
#:  always be strictly LONGER than this:  an id this long is indistinguishable from its own tag,
#:  and writing one where the id belongs would make a resume-wake correlate the wrong session.
SESSION_TAG_LEN = 8


def session_tag(session_id: str | None) -> str | None:
    """First 8 characters of the id with hyphens removed, lowercase.  Only [a-z0-9] survive,
    because the tag names a state directory."""
    if not session_id:
        return None
    cleaned = re.sub(r"[^a-z0-9]", "", session_id.replace("-", "").lower())[:SESSION_TAG_LEN]
    return cleaned or None


# --------------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Credentials:
    email: str
    key: str = field(repr=False)
    site: str
    source: str  # a file path, or "env"


def realm_url(env: Mapping[str, str]) -> str:
    return (env.get(ENV_REALM) or REALM).strip().rstrip("/")


def _normalise_site(site: str) -> str:
    site = site.strip().rstrip("/")
    if "://" not in site:
        site = "https://" + site
    return site


def _strip_quotes(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def read_zuliprc(path: Path) -> Credentials:
    """Parse a zuliprc ([api] email, key, site).  Refuses a group- or world-accessible file.
    Parser errors are replaced by a message naming only the path: configparser quotes the
    offending line, and that line may be the key."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        raise CredentialError("credential file not found: %s" % path) from None
    except OSError as exc:
        raise CredentialError(
            "cannot stat credential file %s (%s)" % (path, exc.strerror or "error")
        ) from None
    if not stat.S_ISREG(st.st_mode):
        raise CredentialError("credential path is not a regular file: %s" % path)
    if st.st_mode & 0o077:
        raise CredentialError(
            "credential file %s is accessible to group or other (mode %04o); run: chmod 600 %s"
            % (path, stat.S_IMODE(st.st_mode), path)
        )
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, encoding="utf-8") as fh:
            parser.read_file(fh)
    except (configparser.Error, UnicodeDecodeError, OSError):
        raise CredentialError(
            "cannot parse %s: expected an INI file with an [api] section holding email, key and site"
            % path
        ) from None
    if not parser.has_section("api"):
        raise CredentialError("%s has no [api] section" % path)
    values = {
        name: _strip_quotes(parser.get("api", name, fallback=""))
        for name in ("email", "key", "site")
    }
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise CredentialError("%s [api] section is missing: %s" % (path, ", ".join(missing)))
    return Credentials(
        email=values["email"],
        key=values["key"],
        site=_normalise_site(values["site"]),
        source=str(path),
    )


def env_credentials(
    env: Mapping[str, str], *, email_env: str, key_env: str, site_env: str, label: str
) -> Credentials:
    """One seat's credentials from the environment, under the variable names its config gives
    (the listener's server instance, where Infisical syncs each bot's email and key into the
    container).  A missing variable is named in the error; a value never is."""
    values = {name: _strip_quotes(env.get(name) or "") for name in (email_env, key_env, site_env)}
    missing = [name for name in (email_env, key_env, site_env) if not values[name]]
    if missing:
        raise CredentialError(
            "seat %s: environment variable%s not set: %s"
            % (label, "" if len(missing) == 1 else "s", ", ".join(missing))
        )
    if any(ch in values[key_env] for ch in "\r\n\x00"):
        raise CredentialError(
            "seat %s: %s holds a control character; refusing it" % (label, key_env)
        )
    return Credentials(
        email=values[email_env],
        key=values[key_env],
        site=_normalise_site(values[site_env]),
        source="env %s" % key_env,
    )


def override_secrets_dir(env: Mapping[str, str]) -> str | None:
    """The explicit credential-file directory (env AGENT_SYNC_SECRETS_DIR), or None.  There is no
    default:  the old per-seat file folder is retired (AFC-359), and seats read Infisical instead."""
    value = (env.get(ENV_SECRETS_DIR) or "").strip()
    return os.path.expanduser(value) if value else None


def default_rc_path(seat: str, env: Mapping[str, str], home: Path | None = None) -> Path | None:
    """<AGENT_SYNC_SECRETS_DIR>/<Title>-zuliprc for the seat, or None when that override is not set.
    `home` is unused (kept for callers);  no path under the home directory is ever implied."""
    directory = override_secrets_dir(env)
    if directory is None:
        return None
    return Path(directory) / credential_file_name(seat)


def infisical_enabled(env: Mapping[str, str]) -> bool:
    return (env.get(ENV_INFISICAL) or "").strip().lower() not in ("0", "false", "off", "no")


def infisical_prefix_for_stem(stem: str) -> str:
    """Grok-Build -> GROK_BUILD, BF-Builder -> BF_BUILDER:  the credential file stem in upper case with
    hyphens as underscores, the prefix of the bot's /zulip key names."""
    return re.sub(r"[^A-Z0-9]", "_", stem.upper())


def infisical_key_names_for_stem(stem: str) -> tuple[str, str, str]:
    prefix = infisical_prefix_for_stem(stem)
    return tuple("%s_ZULIP_%s" % (prefix, part) for part in ("EMAIL", "KEY", "SITE"))  # type: ignore[return-value]


def infisical_key_names(seat: str) -> tuple[str, str, str]:
    """The seat's three Fleet-OPS /zulip secret names (EMAIL, KEY, SITE):  the credential file stem
    in upper case with hyphens as underscores, so CLAUDE -> CLAUDE_ZULIP_EMAIL, GROK -> GROK_BUILD_ZULIP_KEY
    (the GROK bot's file is Grok-Build), BF-BUILDER -> BF_BUILDER_ZULIP_SITE."""
    return infisical_key_names_for_stem(credential_file_name(seat)[: -len("-zuliprc")])


def seat_from_infisical_prefix(prefix: str) -> str | None:
    """The inverse of infisical_key_names:  GROK_BUILD -> GROK, BF_PLUMBER -> BF-PLUMBER, None for a
    prefix that is never a seat (NOT_SEAT_PREFIXES:  the owner's account and the integration bots)."""
    prefix = prefix.upper()
    if not re.match(r"^[A-Z0-9][A-Z0-9_]{0,31}$", prefix) or prefix in NOT_SEAT_PREFIXES:
        return None
    for known, stem in SEAT_FILE_OVERRIDES.items():
        if infisical_prefix_for_stem(stem) == prefix:
            return known
    try:
        return normalise_seat(prefix.replace("_", "-"))
    except UsageError:
        return None


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def infisical_project_id(env: Mapping[str, str], home: Path) -> str:
    """The Fleet-OPS Infisical project id, from env INFISICAL_FLEET_OPS_PROJECT_ID or the first line of
    ~/.agent-sync/fleet-ops-project-id.  Fails closed, naming both, and never echoes a malformed value.
    (fleet-secrets reads the same two places itself;  this copy is for messages and callers.)"""
    value = (env.get(ENV_INFISICAL_PROJECT) or "").strip()
    if not value:
        try:
            value = (
                (home / INFISICAL_PROJECT_FILE)
                .read_text(encoding="utf-8")
                .strip()
                .splitlines()[0]
                .strip()
            )
        except (OSError, UnicodeDecodeError, IndexError):
            value = ""
    if not value:
        raise CredentialError(
            "the Fleet-OPS Infisical project id is not set:  set %s or put it in ~/%s "
            "(it lives in the private fleet-ops inventory)"
            % (ENV_INFISICAL_PROJECT, INFISICAL_PROJECT_FILE)
        )
    if not _UUID_RE.match(value):
        raise CredentialError(
            "the Fleet-OPS Infisical project id (%s or ~/%s) is not a UUID"
            % (ENV_INFISICAL_PROJECT, INFISICAL_PROJECT_FILE)
        )
    return value


def fleet_secrets_bin(env: Mapping[str, str], home: Path) -> str | None:
    """env FLEET_SECRETS_BIN, else ~/.local/bin/fleet-secrets, else `fleet-secrets` on PATH."""
    explicit = (env.get(ENV_FLEET_SECRETS_BIN) or "").strip()
    if explicit:
        return os.path.expanduser(explicit)
    candidate = home / ".local" / "bin" / "fleet-secrets"
    if os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which("fleet-secrets", path=env.get("PATH") or os.defpath)


# fleet-secrets exit codes (scripts/fleet_secrets/cli.py) as reason tokens:  its stderr is never read out.
_FLEET_SECRETS_EXITS = {
    2: "refused-configuration",
    3: "key-absent",
    4: "fetch-or-auth-failure",
    5: "unresolved-reference",
    126: "cannot-start",
    127: "cannot-start",
}
# Run by `fleet-secrets run --only ...`:  prints just the named variables, as JSON, to the pipe we read.
_PRINT_NAMED_ENV = (
    "import json,os,sys;json.dump({n:os.environ.get(n,'') for n in sys.argv[1:]},sys.stdout)"
)

_cache_lock = threading.Lock()
_value_cache: dict[tuple[str, str, str, str], tuple[float, str]] = {}  # (bin, project, path, name)
_names_cache: dict[tuple[str, str], tuple[float, frozenset[str]]] = {}  # (bin, project) -> names
_failures: dict[
    tuple[str, ...], tuple[float, str]
] = {}  # (bin, project, what...) -> (failed at, reason), memory only


def _project_scope(env: Mapping[str, str], home: Path) -> str:
    """The Infisical project this read targets, for cache keying only (never echoed).  One binary can
    serve several projects (the AFC /zulip the Notes service reads, the Fleet-OPS /zulip the seats
    read), so values, names and outages are kept apart per project:  an outage in one store must not
    refuse a fetch from a healthy one.  Fails closed like infisical_project_id, which resolves the same
    two places and is what fleet-secrets itself does:  an unresolvable project is never cached under
    an empty scope, and no value can be fetched without one."""
    return infisical_project_id(env, home)


def _recent_failure(key: tuple[str, ...], now: float) -> str | None:
    held = _failures.get(key)
    if held is not None and now - held[0] < INFISICAL_FAILURE_BACKOFF:
        return held[1]
    return None


def _child_env(env: Mapping[str, str]) -> dict[str, str]:
    child = {k: v for k, v in env.items() if k not in ("ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_RC")}
    child.setdefault("PATH", os.environ.get("PATH") or os.defpath)
    return child


def _run_fleet_secrets(argv: list[str], env: Mapping[str, str]) -> bytes:
    """Run fleet-secrets, return its stdout.  Raises CredentialError with a reason token, never output."""
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=FLEET_SECRETS_TIMEOUT,
            cwd="/",
            env=_child_env(env),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CredentialError("fleet-secrets timeout") from None
    except OSError:
        raise CredentialError("fleet-secrets cannot-start") from None
    if proc.returncode != 0:
        raise CredentialError(
            "fleet-secrets %s (exit %d)"
            % (_FLEET_SECRETS_EXITS.get(proc.returncode, "failed"), proc.returncode)
        )
    return proc.stdout


def _fetch_from_fleet_secrets(
    names: tuple[str, ...], env: Mapping[str, str], home: Path, path: str = INFISICAL_PATH
) -> Mapping[str, str]:
    """Just these names, through `fleet-secrets run --only` (one Infisical call for all of them).
    Cached in memory for INFISICAL_CACHE_TTL;  a failed refresh returns the values this process already
    holds.  Raises CredentialError with a reason token, never output.

    `path` is the Infisical folder that holds the names.  It is a parameter because the
    Operator service keeps its own folder, /zulip_operator, and the seat keys are in
    /zulip: the two stores are separate, and the cache and the outage memory are keyed
    by folder so one store's outage never refuses the other store's fetch.
    """
    wanted = tuple(sorted(set(names)))
    binary = fleet_secrets_bin(env, home)
    if not binary:
        raise CredentialError("fleet-secrets is not installed (~/.local/bin/fleet-secrets)")
    now = time.monotonic()
    with _cache_lock:
        project = _project_scope(env, home)
        held = {n: _value_cache.get((binary, project, path, n)) for n in wanted}
        if all(v is not None and now - v[0] < INFISICAL_CACHE_TTL for v in held.values()):
            return {n: v[1] for n, v in held.items() if v is not None}
        failure_key = (binary, project, path, "values", *wanted)
        # A timeout or an auth or fetch failure is about Infisical, not about these names:  it is remembered
        # per binary, project and folder, so the seats after a failed prefetch fail at once instead of each
        # waiting out the cap, without one store's outage refusing another store's fetch.
        # A missing key stays a failure of this request only.
        outage_key = (binary, project, path, "outage")
        recent = _recent_failure(failure_key, now) or _recent_failure(outage_key, now)
        if recent is not None and not all(v is not None for v in held.values()):
            raise CredentialError(
                "%s (not retried for %d seconds)" % (recent, INFISICAL_FAILURE_BACKOFF)
            )
        argv = [
            binary,
            "run",
            "--path",
            path,
            "--only",
            ",".join(wanted),
            "--",
            sys.executable,
            "-I",
            "-c",
            _PRINT_NAMED_ENV,
            *wanted,
        ]
        try:
            out = _run_fleet_secrets(argv, env)
            try:
                payload = json.loads(out.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise CredentialError("fleet-secrets bad-output") from None
            finally:
                out = b""
            if not isinstance(payload, dict):
                raise CredentialError("fleet-secrets bad-output")
            got = {n: payload[n] for n in wanted if isinstance(payload.get(n), str)}
        except CredentialError as exc:
            _failures[failure_key] = (now, str(exc))
            if "key-absent" not in str(exc):
                _failures[outage_key] = (now, str(exc))
            if all(v is not None for v in held.values()):
                return {
                    n: v[1] for n, v in held.items() if v is not None
                }  # last known good, memory only
            raise
        _failures.pop(failure_key, None)
        _failures.pop(outage_key, None)
        for n, v in got.items():
            _value_cache[(binary, project, path, n)] = (now, v)
        return got


def _list_from_fleet_secrets(env: Mapping[str, str], home: Path) -> frozenset[str]:
    """The key NAMES in /zulip (`fleet-secrets names`:  no value reaches this process).  Cached like values."""
    binary = fleet_secrets_bin(env, home)
    if not binary:
        raise CredentialError("fleet-secrets is not installed (~/.local/bin/fleet-secrets)")
    now = time.monotonic()
    with _cache_lock:
        project = _project_scope(env, home)
        held = _names_cache.get((binary, project))
        if held is not None and now - held[0] < INFISICAL_CACHE_TTL:
            return held[1]
        failure_key = (binary, project, "names")
        recent = _recent_failure(failure_key, now)
        if recent is not None and held is None:
            raise CredentialError(
                "%s (not retried for %d seconds)" % (recent, INFISICAL_FAILURE_BACKOFF)
            )
        try:
            out = _run_fleet_secrets([binary, "names", "--path", INFISICAL_PATH], env)
        except CredentialError as exc:
            _failures[failure_key] = (now, str(exc))
            if held is not None:
                return held[1]
            raise
        _failures.pop(failure_key, None)
        try:
            text = out.decode("utf-8")
        except UnicodeDecodeError:
            raise CredentialError("fleet-secrets bad-output") from None
        names = frozenset(
            line.strip()
            for line in text.splitlines()
            if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", line.strip())
        )
        _names_cache[(binary, project)] = (now, names)
        return names


# The names tests replace (agent_sync/tests/__init__.py), so none can reach the real fleet-secrets.
_default_infisical_fetch = _fetch_from_fleet_secrets
_default_infisical_names = _list_from_fleet_secrets


def infisical_names(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> frozenset[str]:
    """Every key name in Fleet-OPS /zulip, or an empty set when Infisical is off or cannot be read
    (a presence check must never fail a command;  opening the identity reports the reason)."""
    env = env if env is not None else os.environ
    if not infisical_enabled(env):
        return frozenset()
    try:
        return frozenset(
            _default_infisical_names(
                env, home if home is not None else Path(env.get("HOME") or os.path.expanduser("~"))
            )
        )
    except Exception:
        return frozenset()


def infisical_seats(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> frozenset[str]:
    """Every seat whose three /zulip names are all present (never the owner or an integration bot)."""
    names = infisical_names(env, home)
    seats = set()
    for name in names:
        if name.endswith("_ZULIP_KEY"):
            seat = seat_from_infisical_prefix(name[: -len("_ZULIP_KEY")])
            if seat and all(n in names for n in infisical_key_names(seat)):
                seats.add(seat)
    return frozenset(seats)


_ZULIP_NAME_RE = re.compile(r"^(?P<prefix>.+)_ZULIP_(?:EMAIL|KEY|SITE)$")


def infisical_roster(
    env: Mapping[str, str] | None = None, home: Path | None = None
) -> dict[str, tuple[str, ...]]:
    """Every seat that has ANY of its three /zulip names, mapped to the names it is missing.

    `infisical_seats` only answers for a seat whose three names are all present, so a seat whose
    key is missing (but whose email and site are there) vanishes from it and nobody notices that
    the seat was never addressed.  The roster is the wider enumeration:  the seat is named, and
    the tuple says which of its three names are absent (an empty tuple means the seat is
    complete).  The names are names, never values.

    An empty roster means Infisical is off or could not be read:  the caller fails closed."""
    names = infisical_names(env, home)
    if not names:
        return {}
    roster: dict[str, tuple[str, ...]] = {}
    for name in names:
        match = _ZULIP_NAME_RE.match(name)
        if not match:
            continue
        seat = seat_from_infisical_prefix(match.group("prefix"))
        if not seat:
            continue
        missing = tuple(wanted for wanted in infisical_key_names(seat) if wanted not in names)
        if seat not in roster or len(missing) < len(roster[seat]):
            roster[seat] = missing
    return roster


def infisical_has(
    seat: str, env: Mapping[str, str] | None = None, home: Path | None = None
) -> bool:
    return seat.upper() in infisical_seats(env, home)


def prefetch_infisical(
    seats: Iterable[str], env: Mapping[str, str], home: Path, *, stems: Iterable[str] = ()
) -> None:
    """Warm this process's cache with every listed seat's (and bot stem's) names in ONE fetch, so a
    daemon holding several seats makes one Infisical call at start, not one per seat.  Never raises;
    each seat still reports its own failure when it opens."""
    if not infisical_enabled(env) or _default_infisical_fetch is not _fetch_from_fleet_secrets:
        return
    names: list[str] = []
    for seat in seats:
        names.extend(infisical_key_names(seat))
    for stem in stems:
        names.extend(infisical_key_names_for_stem(stem))
    if not names:
        return
    try:
        _fetch_from_fleet_secrets(tuple(names), env, home)
    except Exception:
        pass


def infisical_credentials(
    seat: str,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
    fetch: Callable[[tuple[str, ...]], Mapping[str, str]] | None = None,
    stem: str | None = None,
) -> Credentials:
    """One seat's credentials from Infisical Fleet-OPS /zulip, held in memory only.  Only this seat's
    three names are requested (`stem`, a listener `bot =` value, picks the names when given).  Every
    failure names the folder and the key names, never a value."""
    names = infisical_key_names_for_stem(stem) if stem else infisical_key_names(seat)
    where = "Infisical Fleet-OPS %s (prod) keys %s" % (INFISICAL_PATH, ", ".join(names))
    env = env if env is not None else os.environ
    if not infisical_enabled(env):
        raise CredentialError("cannot read %s: AGENT_SYNC_INFISICAL turns Infisical off" % where)
    try:
        got = (
            fetch(names)
            if fetch is not None
            else _default_infisical_fetch(
                names,
                env,
                home if home is not None else Path(env.get("HOME") or os.path.expanduser("~")),
            )
        )
    except CredentialError as exc:
        raise CredentialError("cannot read %s: %s" % (where, exc)) from None
    except Exception as exc:  # a fetch must not leak anything through its own message
        raise CredentialError("cannot read %s: %s" % (where, type(exc).__name__)) from None
    email_name, key_name, site_name = names
    absent = [
        name for name in names if (got.get(name) or "").strip().lower() in ("", "*not found*")
    ]
    if absent:
        raise CredentialError("%s: not set: %s" % (where, ", ".join(absent)))
    # Same validation as the listener's env credentials (control characters, site shape).
    try:
        creds = env_credentials(
            {name: got[name] for name in names},
            email_env=email_name,
            key_env=key_name,
            site_env=site_name,
            label=seat,
        )
    except CredentialError as exc:
        raise CredentialError("%s: %s" % (where, exc)) from None
    return replace(creds, source="infisical Fleet-OPS %s %s" % (INFISICAL_PATH, key_name))


def resolve_credentials(
    env: Mapping[str, str],
    *,
    rc_arg: str | None,
    seat: str | None,
    home: Path | None = None,
    infisical_fetch: Callable[[tuple[str, ...]], Mapping[str, str]] | None = None,
) -> Credentials:
    """Apply the credential order.  An explicit --rc or ZULIP_RC that cannot be read is an error;
    it never falls through to the next source.  Infisical /zulip is the primary source for a seat
    (AFC-359), and its failure is an error naming the folder and key names, never a value."""
    home = home if home is not None else Path(env.get("HOME") or os.path.expanduser("~"))
    if rc_arg:
        return read_zuliprc(Path(rc_arg).expanduser())
    if env.get("ZULIP_RC"):
        return read_zuliprc(Path(env["ZULIP_RC"]).expanduser())
    tried: list[str] = []
    if seat:
        candidate = default_rc_path(seat, env, home)
        if candidate is not None:
            tried.append(str(candidate))
            if candidate.exists():
                return read_zuliprc(candidate)
    triple = {
        name: (env.get(name) or "").strip()
        for name in ("ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_SITE")
    }
    if all(triple.values()):
        return Credentials(
            email=triple["ZULIP_EMAIL"],
            key=triple["ZULIP_API_KEY"],
            site=_normalise_site(triple["ZULIP_SITE"]),
            source="env",
        )
    missing = [name for name, value in triple.items() if not value]
    where = ("; looked for %s" % ", ".join(tried)) if tried else ""
    fallback = ""
    if seat and infisical_enabled(env):
        try:
            return infisical_credentials(seat, env=env, home=home, fetch=infisical_fetch)
        except CredentialError as exc:
            fallback = "; %s" % exc
    elif seat:
        fallback = "; Infisical is off (AGENT_SYNC_INFISICAL)"
    raise CredentialError(
        "no Zulip credentials found%s%s; or set ZULIP_EMAIL, ZULIP_API_KEY and ZULIP_SITE "
        "(missing: %s), or pass --rc PATH" % (where, fallback, ", ".join(missing))
    )


def _host_key(url: str) -> tuple[str, int | None, str]:
    try:
        parts = urllib.parse.urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port or (
            443 if parts.scheme == "https" else 80 if parts.scheme == "http" else None
        )
    except ValueError:
        return ("", None, "")
    return (host, port, parts.scheme)


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def verify_realm(creds: Credentials, realm: str) -> None:
    """Refuse credentials whose site host is not the realm host, and plain http to a non-loopback host.

    A host that is not the realm's is a `RealmMismatch` (a `CredentialError`):  the key is fine,
    it belongs to another server, so a caller skips that target instead of failing the seat.
    The message names only hosts, so a caller may print it without exposing an identity."""
    c_host, c_port, _ = _host_key(creds.site)
    r_host, r_port, r_scheme = _host_key(realm)
    if not r_host or not c_host:
        raise CredentialError(
            "could not read a host from the realm or from the credential site (%s)" % creds.source
        )
    if (c_host, c_port) != (r_host, r_port):
        raise RealmMismatch(
            "credential site host %s does not match the realm host %s; refusing to send it there"
            % (c_host, r_host)
        )
    if r_scheme != "https" and not _is_loopback(r_host):
        raise CredentialError(
            "refusing to send credentials over %s to %s; the realm must be https"
            % (r_scheme, r_host)
        )


# --------------------------------------------------------------------------------------------
# HTTP client
# --------------------------------------------------------------------------------------------


def outbound_params(
    method: str, path: str, params: Mapping[str, Any] | None
) -> Mapping[str, Any] | None:
    """`params` with the sentence gap in any message `content` this call sends.  The one place every
    outbound path passes through (CLI post, reply and dm, the daemon's wake replies and owner
    DMs, the listener probe).  It is a safety net:  authors still write the gap, and Zulip's renderer
    collapses plain doubled spaces.  Only `content` changes, never the topic, the recipients or
    anything this client reads."""
    if (
        method in ("POST", "PATCH")
        and params
        and isinstance(params.get("content"), str)
        and _OUTBOUND_TEXT_PATH.fullmatch(path.strip("/"))
    ):
        return {**params, "content": fix_outbound(params["content"], limit=10000)[0]}
    return params


def encode_params(params: Mapping[str, Any] | None) -> str:
    """Form-encode.  Strings go through unchanged; bools, ints and floats become Zulip's
    textual forms; lists and dicts are JSON-encoded (narrow, subscriptions, event_types)."""
    pairs: list[tuple[str, str]] = []
    for name, value in (params or {}).items():
        if value is None:
            continue
        if isinstance(value, bool):
            text = "true" if value else "false"
        elif isinstance(value, (int, float)):
            text = str(value)
        elif isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
        pairs.append((name, text))
    return urllib.parse.urlencode(pairs)


_UPLOAD_NAME_RE = re.compile(r"^[A-Za-z0-9._ -]{1,100}$")


def multipart_body(filename: str, data: bytes, content_type: str) -> tuple[bytes, str]:
    """A multipart/form-data body with one file part, and its Content-Type.  The file name is
    checked against a plain-character pattern so it can never break the part header."""
    if not _UPLOAD_NAME_RE.fullmatch(filename) or filename.strip(" .") == "":
        raise UsageError(
            "the upload file name may use letters, digits, '.', '_', '-' and spaces only (1 to 100)"
        )
    if not re.fullmatch(r"[A-Za-z0-9.+-]{1,60}/[A-Za-z0-9.+-]{1,60}", content_type):
        content_type = "application/octet-stream"
    boundary = "agent-sync-" + base64.b16encode(os.urandom(12)).decode("ascii").lower()
    head = (
        '--%s\r\nContent-Disposition: form-data; name="filename"; filename="%s"\r\nContent-Type: %s\r\n\r\n'
        % (boundary, filename, content_type)
    ).encode("ascii")
    return head + data + ("\r\n--%s--\r\n" % boundary).encode(
        "ascii"
    ), "multipart/form-data; boundary=" + boundary


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would re-send the Authorization header to the new host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


# --------------------------------------------------------------------------------------------
# The owner-in-the-conversation guard (AFC-469)
# --------------------------------------------------------------------------------------------

#: The POST paths whose body can carry a direct message.  The guard sits on the client, so one
#: check covers the CLI, the stdio MCP, the listener and the hosted Worker together.
DM_GUARD_PATHS = frozenset({"messages", "scheduled_messages", "drafts"})

#: The message types that make a send a direct message.  Zulip has used both names for it.
DM_GUARD_TYPES = frozenset({"direct", "private"})

#: The tag an Operator status direct message opens with (owner ruling, Thu, Oct 29 2026, about
#: 3:35 AM:  "DM one to one since from bot to another bot but since deterministic bot not something
#: about like the project or details I want to be able to see").  It tells the sender what became
#: of a wake.  The kind is never `wake` or `route`:  the router reads those as seat replies and
#: would count them on the loop guard.  A clarifying question is not here yet, because its reply
#: leg is not built:  the answer would be a direct message from the sender to the Operator, which
#: the owner guard refuses for a bot, and nothing reads that answer.
#:
#: The tag alone is not what lets a message through:  `check_operator_status_send` accepts only a
#: content that is exactly a text the fixed vocabulary builds (operator_status_text.is_status_text).
OPERATOR_STATUS_TAGS = (_OPERATOR_STATUS_TAG,)

#: The longest Operator status message.  The text is built from a fixed vocabulary, so this is a
#: ceiling on a bug, not a limit anyone should reach.  It is checked first, so the strict match
#: below never reads an enormous string.
OPERATOR_STATUS_MAX_CHARS = 1000


def _guard_endpoint(path: Any) -> str:
    """The endpoint a path names, for the guard:  its first segment.

    ``drafts/123`` is the same endpoint as ``drafts``, and a draft's recipient set is rewritten
    by a PATCH, so the guard has to recognise the path with the id on it."""
    return str(path or "").strip("/").split("/", 1)[0]


def _recipient_ids(value: Any) -> list[int]:
    """The user ids in a `to` value, or an empty list when it names no user id.

    A list of ids is the normal shape.  A JSON string is the same list after encoding, and a
    string that is not JSON names a stream, not a person.  Anything unreadable is an empty list,
    which fails the guard closed:  a recipient set nobody can read is a recipient set nobody may
    claim to include the owner."""
    if isinstance(value, (list, tuple)):
        ids: list[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                try:
                    item = int(str(item).strip())
                except (TypeError, ValueError):
                    return []
            ids.append(int(item))
        return ids
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith("["):
            return []
        try:
            parsed = json.loads(text)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return _recipient_ids(parsed)
    return []


def direct_recipient_sets(params: Mapping[str, Any] | None) -> list[list[int]] | None:
    """The recipient set of every direct conversation a request would deliver, or None when the
    request carries no direct conversation at all.

    Four shapes carry a direct message:

    * ``{"type": "direct", "to": [id, ...]}``  (messages, scheduled_messages)
    * ``{"type": "private", "to": [id, ...]}``  (the older name for the same thing)
    * ``{"drafts": [{"type": "private", "to": [id, ...]}, ...]}``  (creating drafts)
    * ``{"draft": {"type": "private", "to": [id, ...]}}``  (editing one draft)

    The last one is the body of ``PATCH drafts/<id>``, so a draft's recipient set is checked when
    it is rewritten and not only when it is created.

    A batch carries one set per direct conversation and every one of them has to carry the owner:
    a union would let one owner-addressed draft authorise an unrelated bot to bot draft beside it.

    A channel send names a stream, not a user, so it is never a direct message and never one of
    this guard's business.

    Every shape the body carries contributes its own set and none of them short-circuits another:
    a body that names a direct message at the top level and also carries a draft key is two
    conversations, and both of them are checked."""
    if not isinstance(params, Mapping):
        return None
    sets: list[list[int]] = []
    drafts = params.get("drafts")
    if isinstance(drafts, list):
        for index, draft in enumerate(drafts):
            if not isinstance(draft, Mapping):
                raise ValueError("drafts[%d] is not a draft object:  %r" % (index, draft))
            if str(draft.get("type") or "").strip().lower() in DM_GUARD_TYPES:
                sets.append(_recipient_ids(draft.get("to")))
    if "draft" in params:
        draft = params.get("draft")
        if not isinstance(draft, Mapping):
            raise ValueError("draft is not a draft object:  %r" % (draft,))
        if str(draft.get("type") or "").strip().lower() in DM_GUARD_TYPES:
            sets.append(_recipient_ids(draft.get("to")))
    if str(params.get("type") or "").strip().lower() in DM_GUARD_TYPES:
        sets.append(_recipient_ids(params.get("to")))
    #  A body with no direct conversation in it is not a direct message at all.
    return sets or None


def check_owner_pinned(owner_user_id: Any) -> int:
    """The pinned owner id, or DmGuardRefused when there is none.

    It **fails closed**:  with no owner id pinned (0, unset, or unreadable) a direct send is
    refused, because a send nobody can check is a send nobody may make.  Both the normal guard and
    the Operator status exemption go through here, so neither can run on an unpinned client."""
    try:
        owner = int(owner_user_id)
    except (TypeError, ValueError):
        owner = 0
    if owner <= 0:
        raise DmGuardRefused(
            "the owner's Zulip user id is not pinned, so this direct message cannot be checked "
            "against it:  pin daemon.owner_user_id in listener.toml (or set OWNER_USER_ID) and try "
            "again",
            code="dm_owner_not_pinned",
        )
    return owner


def check_dm_guard(
    path: str,
    params: Mapping[str, Any] | None,
    owner_user_id: Any,
) -> None:
    """Raise DmGuardRefused unless a direct send has the owner in its recipient list.

    The rule (owner, Oct 23 2026):  a bot may send a direct message only when Jay is an actual
    participant in the conversation.  Participation is the authenticated Zulip **user id**, never a
    display name:  a recipient whose name looks like the owner is not the owner, and mentioning
    Jay in the message text is not participation either.

    It **fails closed**:  with no owner id pinned (0, unset, or unreadable) a direct send is
    refused, because a send nobody can check is a send nobody may make.  A channel post is never
    touched by this guard."""
    if _guard_endpoint(path) not in DM_GUARD_PATHS:
        return
    sets = direct_recipient_sets(params)
    if sets is None:
        return  # not a direct send:  a channel message is none of this guard's business
    owner = check_owner_pinned(owner_user_id)
    #  Every direct conversation the request carries has to have the owner in it.  Checking the
    #  union instead would let one owner-addressed conversation authorise another beside it.
    for recipients in sets:
        if owner in recipients:
            continue
        raise DmGuardRefused(
            "a direct message may go out only when the owner is in the conversation:  add the owner "
            "to the recipients (the `dm --owner` flag, or the owner's user id), or post in a channel "
            "instead",
            code="dm_owner_not_participant",
        )


def check_operator_status_send(method: Any, path: Any, params: Mapping[str, Any] | None) -> None:
    """Raise DmGuardRefused unless this send has exactly the shape of an Operator status message.

    The one shape:  a POST to `messages`, with only `type`, `to` and `content`, `type` being
    `direct`, `to` being one positive user id, and `content` being exactly a text the fixed status
    vocabulary builds (`operator_status_text.is_status_text`), at most OPERATOR_STATUS_MAX_CHARS
    long.  A tag with anything else after it is not enough:  the sentences are the vocabulary's
    own, so no project detail can ride under the tag.  Everything else, a group conversation, a
    draft, a scheduled message, a PATCH or a message with free text, is refused here, so the
    exemption cannot carry anything but the status message even if a caller holds a verified client
    and passes the flag on some other call."""
    body = params if isinstance(params, Mapping) else {}
    if (
        str(method).upper() != "POST"
        or _guard_endpoint(path) != "messages"
        or set(body) != {"type", "to", "content"}
        or body.get("type") != "direct"
    ):
        raise DmGuardRefused(
            "an Operator status message is one direct POST to messages with a type, a recipient "
            "and a content, and nothing else",
            code="dm_status_content",
        )
    to = body.get("to")
    if (
        not isinstance(to, list)
        or len(to) != 1
        or isinstance(to[0], bool)
        or not isinstance(to[0], int)
        or to[0] <= 0
    ):
        raise DmGuardRefused(
            "an Operator status message goes to exactly one recipient, named by user id",
            code="dm_status_recipient",
        )
    content = body.get("content")
    if (
        not isinstance(content, str)
        or len(content) > OPERATOR_STATUS_MAX_CHARS
        or not is_status_text(content)
    ):
        raise DmGuardRefused(
            "an Operator status message must be exactly a text the fixed status vocabulary "
            "builds, at most %d characters, with nothing else in it" % OPERATOR_STATUS_MAX_CHARS,
            code="dm_status_content",
        )


class ZulipClient:
    """Minimal Zulip REST client.  Thread-safe: each request builds its own urllib objects."""

    def __init__(
        self,
        creds: Credentials,
        realm: str,
        *,
        timeout: float = 30.0,
        events_timeout: float = 100.0,
        sleep: Callable[[float], None] = time.sleep,
        owner_user_id: int = 0,
    ) -> None:
        self.creds = creds
        self.realm = realm.rstrip("/")
        self.base = self.realm + "/api/v1/"
        self.timeout = timeout
        self.events_timeout = events_timeout
        self.sleep = sleep
        # The owner's Zulip user id, for the owner-in-the-conversation guard (AFC-469).  0 means
        # "not pinned", and the guard then refuses every direct send:  it fails closed.
        try:
            self.owner_user_id = int(owner_user_id)
        except (TypeError, ValueError):
            self.owner_user_id = 0
        token = base64.b64encode(("%s:%s" % (creds.email, creds.key)).encode("utf-8")).decode(
            "ascii"
        )
        self._auth = "Basic " + token
        self._hidden = [value for value in (creds.key, token) if value]
        # Set only by mark_operator_verified, after `users/me` was checked against the pinned
        # Operator bot.  Nothing else turns the one-to-one status exemption on, and the marker
        # carries the Operator's id and address and the owner pin it was checked against, so a
        # client whose credential is swapped for another bot's afterwards, or whose owner pin is
        # removed or rotated, stops qualifying.
        self._operator_verified = False
        self._operator_user_id = 0
        self._operator_email = ""
        self._operator_owner_id = 0
        host = _host_key(self.realm)[0]
        handlers: list[Any] = [_NoRedirect()]
        if _is_loopback(host):
            handlers.insert(
                0, urllib.request.ProxyHandler({})
            )  # a proxy must not see local traffic
        self._opener = urllib.request.build_opener(*handlers)

    # ---- secrecy ------------------------------------------------------------------------
    def scrub(self, text: str) -> str:
        for hidden in self._hidden:
            text = text.replace(hidden, "[redacted]")
        return text

    # ---- the Operator's one-to-one status message ---------------------------------------
    def mark_operator_verified(self, user_id: int, email: str, owner_user_id: int) -> None:
        """Record that this client's `users/me` was checked against the pinned Operator bot.

        Only `operator_send.connect_operator` calls this, after `service_identity.operator_bot`
        accepted the answer for `owner_user_id`, the owner pin the Operator was checked against.
        The marker carries the Operator's user id and address and that owner pin, and the
        exemption below is honoured only while this client still authenticates as that address and
        still holds that same pin.  A client built against another pin, or against none, cannot be
        marked, and a client that was not verified cannot send a status message."""
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("the Operator's user id must be a positive integer")
        owner = check_owner_pinned(owner_user_id)
        if owner != check_owner_pinned(self.owner_user_id):
            raise DmGuardRefused(
                "this client holds a different owner pin than the one the Operator was checked "
                "against, so it cannot be marked as the Operator",
                code="dm_operator_not_verified",
            )
        self._operator_user_id = user_id
        self._operator_email = str(email or "").strip().casefold()
        self._operator_owner_id = owner
        self._operator_verified = bool(self._operator_email)

    def _require_operator(self) -> None:
        """Refuse unless this client is still the Operator it was verified as.

        Three things have to hold on every exempt call, not only when the marker was set:  the
        owner pin is still there (the same fail-closed rule as the normal guard), it is still the
        pin the Operator was checked against, and the client still authenticates as the verified
        address.  The Operator's user id is recorded with the marker and fixed by it:  Zulip binds
        an address and key to one user, so a client that still authenticates as the verified address
        is that user.  `operator_send.send_status_dm` also re-reads `users/me` before every send."""
        check_owner_pinned(self.owner_user_id)
        if (
            not self._operator_verified
            or self._operator_user_id <= 0
            or self._operator_owner_id != self.owner_user_id
            or str(self.creds.email or "").strip().casefold() != self._operator_email
        ):
            raise DmGuardRefused(
                "the owner-in-the-conversation guard can be skipped only by a client verified as "
                "the pinned Operator bot, still holding the owner pin it was checked against and "
                "still authenticating as it",
                code="dm_operator_not_verified",
            )

    def operator_status_dm(self, recipient_id: int, content: str) -> dict[str, Any]:
        """Send one Operator status message to one recipient, without the owner in it.

        The rule (owner, Thu, Oct 29 2026, about 3:35 AM):  the Operator's reports of what became
        of a wake go one to one to the sender.  They are deterministic text with no project name,
        no topic and no quoted message, so there is nothing in them the owner needs to see.  This
        is the one send that skips the owner-in-the-conversation guard (AFC-469), and the skip is
        enforced in `request`, on the exact shape of the call:  a client verified as the
        Operator that still holds its owner pin, one recipient, and a content that is exactly a
        text the fixed status vocabulary builds."""
        return self.request(
            "POST",
            "messages",
            {"type": "direct", "to": [recipient_id], "content": content},
            owner_guard_exempt=True,
        )

    # ---- core ---------------------------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        long_poll: bool = False,
        deadline: float | None = None,
        upload: tuple[str, bytes, str] | None = None,
        owner_guard_exempt: bool = False,
    ) -> dict[str, Any]:
        """One API call.  429 is retried (Retry-After, at most 3 times).  GET also retries
        network failures and a 502, 503 or 504 twice (1s, then 3s); a long poll does not retry a
        timeout, because a quiet queue is normal.  Non-GET requests are never retried on a
        network failure or a gateway error: a POST /messages that timed out may have posted.

        A POST or a PATCH that would deliver a direct message is checked before anything is sent:
        the owner has to be in the recipient list (AFC-469).  A PATCH matters because editing a
        draft rewrites its recipient set, and a draft is a message nobody has sent yet.  The check
        raises DmGuardRefused and sends nothing.

        `owner_guard_exempt` is for `operator_status_dm` alone.  Only exactly True is read, and then
        the call must come from a client verified as the Operator and must have the exact shape of
        a status message (`check_operator_status_send`);  any other call that passes it is refused."""
        method = method.upper()
        if owner_guard_exempt is not False:
            if owner_guard_exempt is not True:
                raise DmGuardRefused(
                    "owner_guard_exempt must be exactly True or False", code="dm_status_content"
                )
            self._require_operator()
            check_operator_status_send(method, path, params)
        elif method in ("POST", "PATCH"):
            check_dm_guard(path, params, self.owner_user_id)
        query = encode_params(outbound_params(method, path, params))
        url = self.base + path.lstrip("/")
        data: bytes | None = None
        if method == "GET":
            if query:
                url += "?" + query
        elif method == "DELETE":
            # Zulip reads DELETE parameters from the body; the query string is sent too so a
            # server variant that only looks there still sees the queue id.
            if query:
                url += "?" + query
                data = query.encode("utf-8")
        else:
            data = query.encode("utf-8")
        content_type = "application/x-www-form-urlencoded"
        if upload is not None:  # POST user_uploads:  one multipart file part, nothing else
            data, content_type = multipart_body(*upload)
        headers = {
            "Authorization": self._auth,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = content_type
        rate_retries = 0
        net_retries = 0

        def pause(seconds: float) -> None:
            if deadline is not None:
                seconds = min(seconds, max(0.0, deadline - time.monotonic()))
            self.sleep(seconds)

        while True:
            budget = timeout if timeout is not None else self.timeout
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise NetworkError("request deadline exhausted", timeout=True, maybe_sent=False)
                budget = min(budget, remaining)
            request = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._opener.open(request, timeout=budget) as response:
                    body = response.read()
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
                try:
                    body = exc.read()
                except (OSError, http.client.HTTPException):
                    body = b""
                finally:
                    exc.close()
                if status == 429 and rate_retries < MAX_RATE_LIMIT_RETRIES:
                    rate_retries += 1
                    pause(self._retry_after(exc.headers, body))
                    continue
                if (
                    method == "GET"
                    and status in GATEWAY_STATUSES
                    and net_retries < len(GET_RETRY_BACKOFF)
                ):
                    pause(GET_RETRY_BACKOFF[net_retries])
                    net_retries += 1
                    continue
                raise self._api_error(status, body) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                reason = getattr(exc, "reason", exc)
                is_timeout = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
                retryable = (
                    method == "GET"
                    and net_retries < len(GET_RETRY_BACKOFF)
                    and not (long_poll and is_timeout)
                )
                if retryable:
                    pause(GET_RETRY_BACKOFF[net_retries])
                    net_retries += 1
                    continue
                refused = isinstance(reason, ConnectionRefusedError)
                detail = (
                    "timed out" if is_timeout else self.scrub(str(reason) or type(reason).__name__)
                )
                raise NetworkError(
                    "%s %s failed: %s%s"
                    % (
                        method,
                        path,
                        detail,
                        "; Zulip may or may not have received it"
                        if method != "GET" and not refused
                        else "",
                    ),
                    timeout=is_timeout,
                    maybe_sent=(method != "GET" and not refused),
                ) from None
            return self._parse(status, body)

    def get(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.request("GET", path, params, **kw)

    def post(self, path: str, params: Mapping[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.request("POST", path, params, **kw)

    def patch(
        self, path: str, params: Mapping[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        return self.request("PATCH", path, params, **kw)

    def delete(
        self, path: str, params: Mapping[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
        return self.request("DELETE", path, params, **kw)

    def upload(
        self, filename: str, data: bytes, content_type: str = "application/octet-stream"
    ) -> dict[str, Any]:
        """POST user_uploads with one file.  Returns Zulip's answer ({uri, url, filename}).  Never
        retried on a network failure, like every other non-GET request."""
        return self.request("POST", "user_uploads", None, upload=(filename, data, content_type))

    # ---- helpers ------------------------------------------------------------------------
    def _retry_after(self, headers: Any, body: bytes) -> float:
        value: Any = None
        try:
            value = headers.get("Retry-After") if headers is not None else None
        except Exception:  # noqa: BLE001 - a malformed header object is not worth failing over
            value = None
        if value is None:
            try:
                value = json.loads(body.decode("utf-8")).get("retry-after")
            except (ValueError, AttributeError, UnicodeDecodeError):
                value = None
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            seconds = 1.0
        return max(0.0, min(seconds, RETRY_AFTER_CAP))

    def _api_error(self, status: int, body: bytes) -> ApiError:
        data: dict[str, Any] = {}
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict):
                data = parsed
        except (ValueError, UnicodeDecodeError):
            pass
        msg = self.scrub(str(data.get("msg") or "HTTP %d" % status))
        code = data.get("code")
        # The code goes into the exception text too, so it is scrubbed like the message.
        return ApiError(msg, code=self.scrub(str(code)) if code else None, status=status, data=data)

    def _parse(self, status: int, body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(
                "Zulip returned a response that is not JSON (HTTP %d)" % status, status=status
            ) from None
        if not isinstance(parsed, dict):
            raise ApiError(
                "Zulip returned an unexpected response shape (HTTP %d)" % status, status=status
            )
        if parsed.get("result") == "error":
            raise self._api_error(status, body)
        return parsed


# --------------------------------------------------------------------------------------------
# Messages and event queues
# --------------------------------------------------------------------------------------------


def message_channel(message: Mapping[str, Any]) -> str:
    recipient = message.get("display_recipient")
    return recipient if isinstance(recipient, str) else "DM"


def message_topic(message: Mapping[str, Any]) -> str:
    return str(
        message.get("subject") if message.get("subject") is not None else message.get("topic") or ""
    )


class EventQueue:
    """One Zulip event queue.  `narrow` is the pair-shaped list [["channel", C], ["topic", T]]
    (or None for everything the bot receives).  Messages come back with `flags` copied in from
    the event, so callers see the same shape as GET /messages.

    `event_types` defaults to ["message"] (the CLI's `wait` and `listen`).  The listener daemon
    registers more types and reads every event with `poll_events`.  `register_state` keeps the
    register response (initial user and topic state, the long-poll timeout), and `last_event_at`
    is the monotonic time of the last event of any kind, heartbeats included."""

    def __init__(
        self,
        client: ZulipClient,
        narrow: list[list[str]] | None,
        event_types: list[str] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.narrow = narrow
        self.event_types = list(event_types or ["message"])
        self.queue_id: str | None = None
        self.last_event_id: int = -1
        self.register_state: dict[str, Any] = {}
        self.clock = clock
        self.last_event_at: float | None = None

    def register(self) -> None:
        params: dict[str, Any] = {"event_types": self.event_types, "apply_markdown": False}
        if self.narrow is not None:
            params["narrow"] = self.narrow
        result = self.client.post("register", params)
        self.queue_id = str(result["queue_id"])
        self.last_event_id = int(result.get("last_event_id", -1))
        self.register_state = result
        self.last_event_at = self.clock()

    @property
    def longpoll_timeout(self) -> float | None:
        """The server's long-poll timeout from the register response, when it gave one."""
        value = self.register_state.get("event_queue_longpoll_timeout_seconds")
        return (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0
            else None
        )

    def poll_events(self, timeout: float) -> list[dict[str, Any]]:
        """Block up to `timeout` seconds; return every event except heartbeats, as received.
        Message events get the event's `flags` copied into the message.  Raises QueueExpired."""
        if self.queue_id is None:
            raise QueueExpired()
        try:
            result = self.client.get(
                "events",
                {"queue_id": self.queue_id, "last_event_id": self.last_event_id},
                timeout=max(0.05, timeout),
                long_poll=True,
            )
        except ApiError as exc:
            if exc.code == "BAD_EVENT_QUEUE_ID":
                self.queue_id = None
                raise QueueExpired() from None
            raise
        except NetworkError as exc:
            if exc.timeout:
                return []
            raise
        events: list[dict[str, Any]] = []
        raw = result.get("events", [])
        if raw:
            self.last_event_at = self.clock()
        for event in raw:
            event_id = event.get("id")
            if isinstance(event_id, int):
                self.last_event_id = max(self.last_event_id, event_id)
            if event.get("type") == "heartbeat":
                continue
            if event.get("type") == "message" and isinstance(event.get("message"), dict):
                message = dict(event["message"])
                message["flags"] = list(event.get("flags") or message.get("flags") or [])
                event = {**event, "message": message}
            events.append(event)
        return events

    def poll(self, timeout: float) -> list[dict[str, Any]]:
        """Block up to `timeout` seconds for events.  Returns the message events (possibly none).
        Raises QueueExpired when the server has dropped the queue."""
        return [
            event["message"]
            for event in self.poll_events(timeout)
            if event.get("type") == "message" and isinstance(event.get("message"), dict)
        ]

    def close(self) -> str | None:
        """Best-effort DELETE of the queue; never raises.  Returns a one-line problem (never the
        key) when the delete failed, or None.  A queue the server already dropped is not a problem."""
        queue_id, self.queue_id = self.queue_id, None
        if queue_id is None:
            return None
        try:
            self.client.delete("events", {"queue_id": queue_id}, timeout=5.0)
        except ApiError as exc:
            return None if exc.code == "BAD_EVENT_QUEUE_ID" else str(exc)
        except (AgentSyncError, OSError) as exc:
            return str(exc)
        return None
