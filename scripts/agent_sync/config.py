"""listener.toml:  the daemon's local config, read with tomllib, validated with defaults.

It is local because it is machine-specific, holds no secrets and must work offline.  Budgets and
the kill switch live here and in listener/pause.json (owner decision 2026-10-07).  A bad config
never stops the daemon:  `load()` returns the errors, the daemon shows red in status, sends one
notify-owner and keeps running with what it could read (a seat with a bad section gets no queue).

Two instances of this package run the listener (owner decision, Thu, Oct 8):  `mac` holds
the nine Mac seats (AG, CLAUDE, CLUTCH, CODEX, CURSOR, FX, GROK, MC and MM), and `server`
(a container on the Coolify box) holds the Grok Bot personas and the other cloud seats.  The
one exception to "never stops" is the seat partition (docs/protocols/agent-sync-partition.toml,
required on both instances).  It fails closed:  a daemon whose config holds a seat, enabled or
not, that the partition does not give to its own instance (a seat of the other instance, a
`none` seat, or a seat the partition does not list), or a section that reads another listed
seat's credential, refuses to start, so a seat is never held twice.

Credentials:  `creds = "file"` (the default) reads <secrets dir>/<bot>-zuliprc.  `creds = "env"`
(server instance only) reads the seat's email and key from environment variables named in the
config, ZULIP_<CODE>_EMAIL and ZULIP_<CODE>_API_KEY by default (CODE is the seat with hyphens
as underscores), and the site from ZULIP_SITE.  The realm host is still enforced.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping

from . import live as L
from . import zulip as Z

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 and older
    tomllib = None  # type: ignore[assignment]

KNOWN_WAKES = ("inbox", "claude", "http")
WORKER_WAKES = ("claude", "http")  # adapters that run on a per-seat wake worker thread
INSTANCES = ("mac", "server")
PARTITION_VALUES = ("mac", "server", "none")
ENV_INSTANCE = "AGENT_SYNC_INSTANCE"  # set by the LaunchAgent (mac) and the container image (server)
ENV_PARTITION = "AGENT_SYNC_PARTITION"  # tests point it elsewhere; production uses the repo copy
PARTITION_RELPATH = os.path.join("docs", "protocols", "agent-sync-partition.toml")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_OWNER_CLIENTS = ["website", "ZulipMobile", "ZulipFlutter", "ZulipElectron", "ZulipDesktop"]
BUDGET_DEFAULTS: dict[str, float] = {
    "wakes_per_hour": 6, "wakes_per_day": 40, "per_topic_per_hour": 2, "owner_per_day": 20,
    "owner_per_topic_per_hour": 6, "usd_per_day": 2.0, "board_per_day": 0,
}
WAKE_MAX_BUDGET_USD = 0.25
DEFAULT_SITE_ENV = "ZULIP_SITE"
# The CLI's single-seat triple.  A listener seat never reads it, so two seats cannot alias one bot.
GENERIC_ENV = frozenset({"ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_RC"})
ROUTINE_AUTH = ("header", "bearer", "hmac-sha256")
ROUTINE_METHODS = ("POST", "PUT", "PATCH")
ROUTINE_DEFAULT_HEADER = {"header": "X-Routine-Key", "bearer": "Authorization", "hmac-sha256": "X-Agent-Sync-Signature"}
ROUTINE_RESERVED_HEADERS = frozenset({"host", "content-length", "content-type", "transfer-encoding", "connection",
                                      "user-agent", "idempotency-key", "cookie"})
ROUTINE_TIMEOUT_DEFAULT = 15.0
_SEAT_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
_BOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9=:_.-]{0,32}$")


def env_code(seat: str) -> str:
    """GB-COMPILER -> GB_COMPILER, the CODE in ZULIP_<CODE>_EMAIL."""
    return re.sub(r"[^A-Z0-9]", "_", seat.upper())


class RoutineConfig:
    """Where a remote `http` wake goes:  the names of the environment variables that hold the
    routine URL and its sender key (never the values), the method, the header and auth mode."""

    def __init__(self, *, url_env: str, key_env: str, method: str, header: str, auth: str, timeout: float,
                 cost_usd: float, signature_prefix: str) -> None:
        self.url_env = url_env
        self.key_env = key_env
        self.method = method
        self.header = header
        self.auth = auth
        self.timeout = timeout
        self.cost_usd = cost_usd
        self.signature_prefix = signature_prefix

    def describe(self) -> dict[str, Any]:
        return {"url_env": self.url_env, "key_env": self.key_env, "method": self.method, "header": self.header,
                "auth": self.auth, "timeout": self.timeout, "cost_usd": self.cost_usd}


class SeatConfig:
    def __init__(self, seat: str, bot: str, wake: str, model: str, budget: dict[str, float],
                 live: dict[str, int], claude: str | None, *, instance: str = "mac", enabled: bool = True,
                 creds: str = "file", email_env: str | None = None, key_env: str | None = None,
                 site_env: str = DEFAULT_SITE_ENV, routine: RoutineConfig | None = None) -> None:
        self.seat = seat
        self.bot = bot
        self.wake = wake
        self.model = model
        self.budget = budget
        self.live = live
        self.claude = claude  # an absolute path to the claude binary, else resolved on the wake PATH
        self.instance = instance
        self.enabled = enabled
        self.creds = creds  # "file" or "env"
        self.email_env = email_env
        self.key_env = key_env
        self.site_env = site_env
        self.routine = routine

    @property
    def wake_max_usd(self) -> float:
        """What one wake may cost, reserved against usd_per_day:  the claude run's maximum, a
        routine's configured cost (default 0:  the daemon spends nothing; the routine's platform
        bills its own run), nothing for inbox."""
        if self.wake == "claude":
            return WAKE_MAX_BUDGET_USD
        if self.wake == "http" and self.routine is not None:
            return self.routine.cost_usd
        return 0.0

    def __repr__(self) -> str:
        return "SeatConfig(%s, bot=%s, wake=%s, creds=%s)" % (self.seat, self.bot, self.wake, self.creds)


class Config:
    def __init__(self) -> None:
        self.raw: dict[str, Any] = {}
        self.errors: list[str] = []
        self.instance = "mac"
        self.owner_user_id = 0
        self.eligible_user_ids: set[int] = set()
        self.owner_clients: set[str] = set(DEFAULT_OWNER_CLIENTS)
        self.stale_after = float(L.STALE_AFTER_DEFAULT)
        self.coalesce_seconds = 20.0
        self.coalesce_max_seconds = 90.0
        self.owner_coalesce_seconds = 5.0
        self.presence: list[tuple[str, str]] = [(c, t) for c, t in L.DEFAULT_PRESENCE]
        self.claude_seat: str | None = None
        self.rewake_verified = False
        self.seats: dict[str, SeatConfig] = {}
        self.disabled: dict[str, SeatConfig] = {}  # parsed and checked, but no queue (enabled = false)
        self.seat_instances: dict[str, str | None] = {}  # every [seat.X] section:  its own instance key, if any
        self.notify_banners = True

    @property
    def ok(self) -> bool:
        return not self.errors


def _number(value: Any, name: str, errors: list[str], *, minimum: float = 0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        errors.append("%s must be a number of at least %g" % (name, minimum))
        return None
    return float(value)


def _env_name(value: Any, name: str, problems: list[str]) -> str | None:
    if not isinstance(value, str) or not _ENV_NAME_RE.match(value):
        problems.append("%s must be an environment variable name (upper case letters, digits and _)" % name)
        return None
    if value in GENERIC_ENV:
        problems.append("%s may not be %s: that is the CLI's single-seat variable, and two seats reading it would "
                        "alias one bot" % (name, value))
        return None
    return value


def _routine(seat: str, given: Any, problems: list[str]) -> RoutineConfig | None:
    if not isinstance(given, dict):
        problems.append("seat.%s.routine must be a table (url_env, key_env, method, header, auth) when wake = \"http\""
                        % seat)
        return None
    known = {"url_env", "key_env", "method", "header", "auth", "timeout_seconds", "cost_usd", "signature_prefix"}
    for key in sorted(set(given) - known):
        problems.append("seat.%s.routine.%s is not a known routine setting" % (seat, key))
    code = env_code(seat)
    url_env = _env_name(given.get("url_env", code + "_ROUTINE_URL"), "seat.%s.routine.url_env" % seat, problems)
    key_env = _env_name(given.get("key_env", code + "_ROUTINE_KEY"), "seat.%s.routine.key_env" % seat, problems)
    method = str(given.get("method", "POST")).upper()
    if method not in ROUTINE_METHODS:
        problems.append("seat.%s.routine.method must be one of %s" % (seat, ", ".join(ROUTINE_METHODS)))
    auth = given.get("auth", "bearer")
    if auth not in ROUTINE_AUTH:
        problems.append("seat.%s.routine.auth must be one of %s" % (seat, ", ".join(ROUTINE_AUTH)))
        auth = "bearer"
    header = given.get("header", ROUTINE_DEFAULT_HEADER[auth])
    if not isinstance(header, str) or not _HEADER_RE.match(header) or header.casefold() in ROUTINE_RESERVED_HEADERS:
        problems.append("seat.%s.routine.header must be an HTTP header name (letters, digits and -) other than %s"
                        % (seat, ", ".join(sorted(ROUTINE_RESERVED_HEADERS))))
    prefix = given.get("signature_prefix", "")
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        problems.append("seat.%s.routine.signature_prefix must be up to 32 letters, digits or =:_.-" % seat)
        prefix = ""
    elif prefix and auth != "hmac-sha256":
        problems.append("seat.%s.routine.signature_prefix applies only to auth = \"hmac-sha256\"" % seat)
    timeout = _number(given.get("timeout_seconds", ROUTINE_TIMEOUT_DEFAULT), "seat.%s.routine.timeout_seconds" % seat,
                      problems, minimum=1)
    if timeout is not None and timeout > 60:
        problems.append("seat.%s.routine.timeout_seconds must be 60 or less" % seat)
    cost = _number(given.get("cost_usd", 0.0), "seat.%s.routine.cost_usd" % seat, problems)
    if url_env and key_env and url_env == key_env:
        problems.append("seat.%s.routine.url_env and key_env must be different variables" % seat)
    if not (url_env and key_env and timeout is not None and cost is not None):
        return None
    return RoutineConfig(url_env=url_env, key_env=key_env, method=method, header=str(header), auth=str(auth),
                         timeout=timeout, cost_usd=cost, signature_prefix=prefix)


def from_dict(raw: Mapping[str, Any]) -> Config:
    cfg = Config()
    cfg.raw = dict(raw)
    errors = cfg.errors
    daemon = raw.get("daemon") or {}
    if not isinstance(daemon, dict):
        errors.append("[daemon] must be a table")
        daemon = {}
    instance = daemon.get("instance", "mac")
    if instance not in INSTANCES:
        errors.append("daemon.instance must be one of %s" % ", ".join(INSTANCES))
    else:
        cfg.instance = instance
    owner = daemon.get("owner_user_id", 0)
    if isinstance(owner, bool) or not isinstance(owner, int) or owner < 0:
        errors.append("daemon.owner_user_id must be a user id (0 means not pinned yet)")
    else:
        cfg.owner_user_id = owner
    eligible = daemon.get("eligible_user_ids", [])
    if not isinstance(eligible, list) or not all(isinstance(i, int) and not isinstance(i, bool) for i in eligible):
        errors.append("daemon.eligible_user_ids must be a list of user ids")
    else:
        cfg.eligible_user_ids = set(eligible)
    clients = daemon.get("owner_clients", DEFAULT_OWNER_CLIENTS)
    if not isinstance(clients, list) or not all(isinstance(c, str) for c in clients):
        errors.append("daemon.owner_clients must be a list of client names")
    else:
        cfg.owner_clients = set(clients)
    for name, attr, scale in (("stale_after_minutes", "stale_after", 60.0), ("coalesce_seconds", "coalesce_seconds", 1.0),
                              ("coalesce_max_seconds", "coalesce_max_seconds", 1.0),
                              ("owner_coalesce_seconds", "owner_coalesce_seconds", 1.0)):
        if name in daemon:
            value = _number(daemon[name], "daemon." + name, errors)
            if value is not None:
                setattr(cfg, attr, value * scale)
    # macOS banners on the Mac; the server has no display, so it keeps only the owner queue.
    cfg.notify_banners = daemon["notify_banners"] is True if "notify_banners" in daemon else cfg.instance == "mac"
    cfg.presence = L.presence_topics(raw)
    platform = ((raw.get("platform") or {}).get("claude-code") or {}) if isinstance(raw.get("platform"), dict) else {}
    seat = platform.get("seat") if isinstance(platform, dict) else None
    if seat is not None:
        if isinstance(seat, str) and _SEAT_RE.match(seat.upper()):
            cfg.claude_seat = seat.upper()
        else:
            errors.append("platform.claude-code.seat must be a seat name such as CLAUDE")
    cfg.rewake_verified = L.rewake_verified(raw)
    seats = raw.get("seat") or {}
    if not isinstance(seats, dict):
        errors.append("[seat.X] sections must be tables")
        seats = {}
    for name, section in seats.items():
        seat_name = str(name).upper()
        if not _SEAT_RE.match(seat_name) or not isinstance(section, dict):
            errors.append("seat.%s is not a valid seat section" % name)
            continue
        problems: list[str] = []
        seat_instance = section.get("instance")
        if seat_instance is not None and seat_instance not in INSTANCES:
            problems.append("seat.%s.instance must be one of %s" % (seat_name, ", ".join(INSTANCES)))
            seat_instance = None
        cfg.seat_instances[seat_name] = seat_instance
        enabled = section.get("enabled", True)
        if not isinstance(enabled, bool):
            problems.append("seat.%s.enabled must be true or false" % seat_name)
            enabled = True
        creds = section.get("creds", "file")
        if creds not in ("file", "env"):
            problems.append("seat.%s.creds must be \"file\" (a zuliprc) or \"env\" (environment variables)" % seat_name)
            creds = "file"
        if creds == "env" and cfg.instance != "server":
            problems.append("seat.%s.creds = \"env\" is for the server instance only; the Mac instance reads one "
                            "credential file per seat" % seat_name)
        bot = section.get("bot")
        if creds == "file" or bot is not None:
            if not isinstance(bot, str) or not _BOT_RE.match(bot):
                problems.append("seat.%s.bot must be the credential file code (e.g. Claude for Claude-zuliprc)"
                                % seat_name)
        else:
            bot = Z.credential_file_name(seat_name)[: -len("-zuliprc")]  # a display name only:  no file is read
        email_env = key_env = None
        site_env = DEFAULT_SITE_ENV
        if creds == "env":
            code = env_code(seat_name)
            email_env = _env_name(section.get("email_env", "ZULIP_%s_EMAIL" % code), "seat.%s.email_env" % seat_name,
                                  problems)
            key_env = _env_name(section.get("key_env", "ZULIP_%s_API_KEY" % code), "seat.%s.key_env" % seat_name,
                                problems)
            site_given = section.get("site_env", DEFAULT_SITE_ENV)
            if not isinstance(site_given, str) or not _ENV_NAME_RE.match(site_given):
                problems.append("seat.%s.site_env must be an environment variable name" % seat_name)
            else:
                site_env = site_given
        else:
            for key in ("email_env", "key_env", "site_env"):
                if key in section:
                    problems.append("seat.%s.%s applies only to creds = \"env\"" % (seat_name, key))
        wake = section.get("wake", "inbox")
        if wake not in KNOWN_WAKES:
            problems.append("seat.%s.wake must be one of %s" % (seat_name, ", ".join(KNOWN_WAKES)))
        routine = None
        if wake == "http":
            routine = _routine(seat_name, section.get("routine"), problems)
        elif "routine" in section:
            problems.append("seat.%s.routine applies only to wake = \"http\"" % seat_name)
        model = section.get("model", "sonnet")
        if not isinstance(model, str) or not re.fullmatch(r"[a-z0-9.\-\[\]]{1,60}", model):
            problems.append("seat.%s.model must be a model alias such as sonnet" % seat_name)
        claude = section.get("claude")
        if claude is not None and (not isinstance(claude, str) or not os.path.isabs(claude)):
            problems.append("seat.%s.claude must be an absolute path" % seat_name)
        budget = dict(BUDGET_DEFAULTS)
        given = section.get("budget", {})
        if not isinstance(given, dict):
            problems.append("seat.%s.budget must be a table" % seat_name)
            given = {}
        for key, value in given.items():
            if key not in BUDGET_DEFAULTS:
                problems.append("seat.%s.budget.%s is not a known budget" % (seat_name, key))
                continue
            number = _number(value, "seat.%s.budget.%s" % (seat_name, key), problems)
            if number is not None:
                budget[key] = number
        if "allow_admin" in section:
            problems.append("seat.%s.allow_admin is not supported: admin and owner bot keys are always refused, "
                            "moderator and member keys are accepted (owner decision 2026-10-07)" % seat_name)
        used = [name for name in (email_env, key_env, site_env if creds == "env" else None,
                                  routine.url_env if routine else None, routine.key_env if routine else None) if name]
        for name in sorted({n for n in used if used.count(n) > 1}):
            problems.append("seat.%s uses %s for more than one setting; its Zulip email, key and site and its routine "
                            "URL and key must be separate variables (a shared one could send the bot key to the "
                            "routine)" % (seat_name, name))
        if routine is not None and DEFAULT_SITE_ENV in (routine.url_env, routine.key_env):
            problems.append("seat.%s.routine may not read %s, the Zulip site variable" % (seat_name, DEFAULT_SITE_ENV))
        if problems:
            errors.extend(problems)
            continue
        seat_cfg = SeatConfig(seat_name, bot, wake, model, budget, L.live_limits(raw, seat_name), claude,
                              instance=seat_instance or cfg.instance, enabled=enabled, creds=creds,
                              email_env=email_env, key_env=key_env, site_env=site_env, routine=routine)
        (cfg.seats if enabled else cfg.disabled)[seat_name] = seat_cfg
    _refuse_shared_env(cfg)
    return cfg


def _refuse_shared_env(cfg: Config) -> None:
    """Two seats reading the same variable would alias one bot (or send one seat's wakes to
    another's routine):  every seat that shares a name is dropped with an error."""
    users: dict[str, set[str]] = {}
    for seat_cfg in list(cfg.seats.values()) + list(cfg.disabled.values()):
        names = [seat_cfg.email_env, seat_cfg.key_env]
        if seat_cfg.routine is not None:
            names += [seat_cfg.routine.url_env, seat_cfg.routine.key_env]
        for name in names:
            if name:
                users.setdefault(name, set()).add(seat_cfg.seat)
    for name, seats in sorted(users.items()):
        if len(seats) > 1:
            cfg.errors.append("environment variable %s is named by more than one seat (%s); a seat's variables must be "
                              "its own, so none of them gets a queue" % (name, ", ".join(sorted(seats))))
            for seat in seats:
                cfg.seats.pop(seat, None)
                cfg.disabled.pop(seat, None)


def load(root: str, path: str | None = None) -> Config:
    path = path or os.path.join(root, L.CONFIG_NAME)
    raw, error = L.load_config(root, path)
    cfg = from_dict(raw)
    if error:
        cfg.errors.insert(0, error)
    elif not raw:
        cfg.errors.insert(0, "no %s; run agent-sync daemon install (it writes a sample)" % path)
    if cfg.owner_user_id == 0 and raw:
        cfg.errors.append("daemon.owner_user_id is 0 (not pinned): nothing is treated as the owner and nothing wakes; "
                          "run agent-sync daemon init")
    return cfg


# --------------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------------

def seat_credentials(seat_cfg: SeatConfig, env: Mapping[str, str], secrets_dir: str) -> Z.Credentials:
    """The one place a listener seat's credentials are read:  its own zuliprc file, or (creds =
    "env") the environment variables its config names.  Never ZULIP_RC or the CLI's triple."""
    if seat_cfg.creds == "env":
        assert seat_cfg.email_env and seat_cfg.key_env
        return Z.env_credentials(env, email_env=seat_cfg.email_env, key_env=seat_cfg.key_env,
                                 site_env=seat_cfg.site_env, label=seat_cfg.seat)
    return Z.read_zuliprc(os.path.join(secrets_dir, seat_cfg.bot + "-zuliprc"))


# --------------------------------------------------------------------------------------------
# Seat partition (mac or server)
# --------------------------------------------------------------------------------------------

def partition_path(env: Mapping[str, str], repo_root: str = REPO_ROOT) -> str:
    return os.path.expanduser(env.get(ENV_PARTITION) or os.path.join(repo_root, PARTITION_RELPATH))


def load_partition(path: str) -> tuple[dict[str, str] | None, str | None]:
    """(seat -> instance, error).  The file is required on both instances (the Mac reads the
    checkout's copy, the image carries one), so a missing file is an error like an unreadable one
    or a bad entry:  the partition fails closed and no seat is held without it."""
    if tomllib is None:  # pragma: no cover
        return None, "Python 3.11 or newer is needed to read %s" % path
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        return None, ("the seat partition %s is missing (it ships with the package; %s may point at another copy)"
                      % (path, ENV_PARTITION))
    except (OSError, ValueError) as exc:
        return None, "cannot read the seat partition %s: %s" % (path, exc)
    seats = data.get("seats")
    if not isinstance(seats, dict):
        return None, "the seat partition %s has no [seats] table" % path
    found: dict[str, str] = {}
    for name, value in seats.items():
        seat = str(name).upper()
        if not _SEAT_RE.match(seat) or value not in PARTITION_VALUES:
            return None, "the seat partition %s has a bad entry for %s (instances: %s)" % (
                path, name, ", ".join(PARTITION_VALUES))
        found[seat] = value
    return found, None


_DEFAULT_ENV_RE = re.compile(r"^ZULIP_([A-Z0-9_]+)_(EMAIL|API_KEY)$")


def credential_aliases(seat_cfg: SeatConfig, known: set[str]) -> list[tuple[str, str]]:
    """(what, other seat) for each credential this section reads that belongs to another known
    seat by name:  a `bot` file stem (GB-Compiler-zuliprc is GB-COMPILER's), or an `email_env` or
    `key_env` in another seat's default pattern (ZULIP_GB_COMPILER_API_KEY).  Custom names that
    match no known seat pass here; the connect-time users/me check still refuses them."""
    found: list[tuple[str, str]] = []
    if seat_cfg.creds == "file":
        owner = Z.seat_from_rc_path(seat_cfg.bot + "-zuliprc")
        if owner and owner != seat_cfg.seat and owner in known:
            found.append(("bot = %r (%s-zuliprc)" % (seat_cfg.bot, seat_cfg.bot), owner))
    else:
        for name in (seat_cfg.email_env, seat_cfg.key_env):
            match = _DEFAULT_ENV_RE.match(name or "")
            if not match or match.group(1) == env_code(seat_cfg.seat):
                continue
            for other in sorted(known):
                if other != seat_cfg.seat and env_code(other) == match.group(1):
                    found.append(("%s" % name, other))
    return found


def partition_errors(cfg: Config, partition: Mapping[str, str] | None, *, partition_file: str = "",
                     env_instance: str | None = None) -> list[str]:
    """Why this config must not run on this instance, or [].  Every [seat.X] section counts,
    enabled or not.  It fails closed:  a seat the partition does not give to this instance (one
    of the other instance, a `none` seat, or one it does not list) is refused, and so is a
    section that reads another known seat's credential, so aliasing a name cannot hold a bot
    twice.  `partition` None (no readable file) refuses every seat."""
    problems: list[str] = []
    where_file = partition_file or "the partition"
    if env_instance and env_instance != cfg.instance:
        problems.append("this listener runs as the %r instance (%s) but its config says daemon.instance = %r"
                        % (env_instance, ENV_INSTANCE, cfg.instance))
    assigned_all = dict(partition or {})
    for seat, declared in sorted(cfg.seat_instances.items()):
        if declared and declared != cfg.instance:
            problems.append("seat %s is marked instance = %r, but this is the %r listener; a seat must never be "
                            "configured in both instances" % (seat, declared, cfg.instance))
            continue
        assigned = assigned_all.get(seat)
        if assigned is None:
            problems.append("seat %s is configured here (the %r listener), but %s does not list it; a seat that "
                            "is not listed is held by no instance (assign it in a PR, with the owner's OK)"
                            % (seat, cfg.instance, where_file))
        elif assigned != cfg.instance:
            where = ("no listener instance holds it" if assigned == "none"
                     else "it belongs to the %r instance" % assigned)
            problems.append("seat %s is configured here (the %r listener), but %s per %s; a seat must never be "
                            "configured in both instances" % (seat, cfg.instance, where, where_file))
    known = set(assigned_all) | set(cfg.seat_instances)
    for seat_cfg in sorted(list(cfg.seats.values()) + list(cfg.disabled.values()), key=lambda c: c.seat):
        for what, other in credential_aliases(seat_cfg, known):
            problems.append("seat %s reads %s, which is the credential of seat %s; a seat holds only its own bot, "
                            "so one bot is never held twice" % (seat_cfg.seat, what, other))
    return problems
