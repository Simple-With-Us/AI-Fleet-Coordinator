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

Credentials:  `creds = "file"` (the default) is the seat's own bot, named by `bot` (its credential
file stem, e.g. Grok-Build):  read from Infisical Fleet-OPS /zulip as <BOT>_ZULIP_EMAIL, _KEY and _SITE
(BOT is the stem in upper case with hyphens as underscores), or from <dir>/<bot>-zuliprc only when the
explicit override AGENT_SYNC_SECRETS_DIR is set (there is no default folder, AFC-359).
`creds = "infisical"` is the same without the file override.  `creds = "env"`
(server instance only) reads the seat's email and key from environment variables named in the
config, ZULIP_<CODE>_EMAIL and ZULIP_<CODE>_API_KEY by default (CODE is the seat with hyphens
as underscores), and the site from ZULIP_SITE.  The realm host is still enforced.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import live as L
from . import zulip as Z

try:
    import tomllib
except ImportError:  # pragma: no cover - Python 3.10 and older
    tomllib = None  # type: ignore[assignment]

KNOWN_WAKES = ("inbox", "claude", "http", "muse")
WORKER_WAKES = ("claude", "http", "muse")  # adapters that run on a per-seat wake worker thread
# The runtimes a wake may name as its fallback (AFC-291).  `muse` is the primary and `mcode` is the
# fallback:  both shape the answer with `--output-schema`, but only muse can state "hold no tools",
# so the order is fixed and a seat may not invert it (checked against the wake type in from_dict).
KNOWN_FALLBACKS = ("mcode",)
INSTANCES = ("mac", "server")
PARTITION_VALUES = ("mac", "server", "none")
ENV_INSTANCE = (
    "AGENT_SYNC_INSTANCE"  # set by the LaunchAgent (mac) and the container image (server)
)
ENV_PARTITION = "AGENT_SYNC_PARTITION"  # tests point it elsewhere; production uses the repo copy
PARTITION_RELPATH = os.path.join("docs", "protocols", "agent-sync-partition.toml")
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_OWNER_CLIENTS = ["website", "ZulipMobile", "ZulipFlutter", "ZulipElectron", "ZulipDesktop"]
BUDGET_DEFAULTS: dict[str, float] = {
    "wakes_per_hour": 6,
    "wakes_per_day": 40,
    "per_topic_per_hour": 2,
    "owner_per_day": 20,
    "owner_per_topic_per_hour": 6,
    "usd_per_day": 2.0,
    "board_per_day": 0,
    # Route suggestions (owner 2026-10-10):  what one routing seat (today only CLAUDE, the one claude
    # wake) may page on the owner's behalf, counted from that seat's own ledger.  A route also needs
    # route_topic_minutes since the last route posted in the same topic.
    "routes_per_hour": 3,
    "routes_per_day": 10,
    "route_topic_minutes": 30,
}
WAKE_MAX_BUDGET_USD = 0.25
# The muse seat's default model (AFC-291).  The catalog carries three muse-spark-1.3 variants and
# the plain `muse-spark-1.3` is 15x dearer than the contributor tier, so the default names the
# contributor tier explicitly.  It is a default, not a hardcode:  `seat.X.model` still wins, so a
# seat can be moved to another tier deliberately and the change shows up in the config.
MUSE_DEFAULT_MODEL = "muse-spark-1.3-contributor"
# The MiniMax Code fallback runtime's model (AFC-291).  `mcode exec` inherits MiniMax Code's own
# `defaultModel` when it is given no `--model`, and a backup runtime must not be left to whatever
# that install happens to carry.  The fallback therefore names its model on the command line, so the
# backup runtime runs on a model the catalog serves instead of inheriting a stale default.  A seat
# can still move deliberately: `seat.X.mcode_model` wins over this.
#
# Two namespaces, one name, and the trap in it.  The MiniMax Code RUNTIME CATALOG and the public
# MiniMax API id list are different catalogs, and "not in the catalog" does not mean "dead".
# `MiniMax-M3.1-Flash-Preview` was dropped from the runtime catalog on 2026-10-18 (BF-2494) while
# api.minimax.io kept serving it, so the same id fails on one route and answers on another.  An id
# must never be rewritten from one route's evidence; the fleet recall contribution
# contrib/MM/2026-10-19/f11f7046 records that lesson, and it is a recall id rather than a path
# in this repository.  The fallback pins the id the runtime catalog actually carries, which is
# now also the id the CI autofix workflows send to api.minimax.io.
MCODE_DEFAULT_MODEL = "minimax/MiniMax-M3.1-Flash"
# A MiniMax Code model id is `provider/model`, so the case and the slash are part of it (unlike a
# muse model alias, which is lowercase).  This is the shape the fallback's `--model` value must have.
MCODE_MODEL_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]{0,59}$"
# Wakes the budget holds back (heldback.py), per seat:  `held_back = { max = 200, overflow_max = 1000,
# ttl_hours = 24, items_in_wake = 20, owner_digest_minutes = 60, catch_up = true }`.  max is the records
# with names;  past it the oldest move to a compact overflow tier (still listed by id), and only past
# overflow_max is an id lost (counted and told to the owner at once).  ttl_hours applies only to what a
# digest the owner was shown named (a Mac banner), so on the server nothing expires unsurfaced.  catch_up applies to a seat whose
# adapter runs a turn (`claude` or `http`) and defaults to `[daemon] held_back_catch_up` (true);  an
# `inbox` seat never wakes, so it has nothing to hold back or catch up.  owner_digest_minutes = 0 turns
# the owner digest off.  None of these changes a budget:  every wake, a catch-up included, passes them.
HELD_BACK_DEFAULTS: dict[str, float] = {
    "max": 200,
    "overflow_max": 1000,
    "ttl_hours": 24,
    "items_in_wake": 20,
    "owner_digest_minutes": 60,
}
# (minimum, maximum, whole numbers only).  items_in_wake stops at 20, the hosted Worker's cap.
HELD_BACK_LIMITS: dict[str, tuple[float, float, bool]] = {
    "max": (1, 1000, True),
    "overflow_max": (0, 5000, True),
    "ttl_hours": (1, 168, False),
    "items_in_wake": (0, 20, True),
    "owner_digest_minutes": (0, 1440, False),
}
HELD_BACK_MIN_DIGEST_MINUTES = 15
# The `#alerts` held-back digest (AFC-257, heldalert.py), one post for EVERY seat per window.  An hour
# matches the per-seat owner digest:  a hold is durable, so there is nothing to gain by saying it
# sooner than the window, and a seat one under its cap would otherwise post every 30s tick.
HELD_ALERT_DEFAULT_MINUTES = 60
HELD_ALERT_LIMITS: dict[str, tuple[float, float]] = {"held_alert_minutes": (0, 1440)}
# The peer-broadcast reminder (owner, Oct 27 2026, broadcast_audit.py):  how long a fleet bot has to
# explain its `@all` to the owner before the listener DMs the owner about it.  0 turns the reminder
# off;  the wake itself is untouched either way.
PEER_BROADCAST_REMINDER_DEFAULT_MINUTES = 10
PEER_BROADCAST_LIMITS: dict[str, tuple[float, float]] = {
    "peer_broadcast_reminder_minutes": (0, 1440)
}
# Seat tags whose bots are always eligible senders, on top of every tag the seat partition lists
# (fleet_tags).  Owner 2026-10-09:  "everyone should be able to DM to wake anyone else or tag to wake
# anyone else", so every fleet bot (Mac and cloud seats, GB personas, BF role bots) is eligible.
FLEET_SEATS = (
    "CLAUDE",
    "CODEX",
    "AG",
    "CURSOR",
    "GROK",
    "CLUTCH",
    "FX",
    "MM",
    "MC",
    "MA",
    "JET",
    "OPENCODE",
)
DEFAULT_SITE_ENV = "ZULIP_SITE"
# The CLI's single-seat triple.  A listener seat never reads it, so two seats cannot alias one bot.
GENERIC_ENV = frozenset({"ZULIP_EMAIL", "ZULIP_API_KEY", "ZULIP_RC"})
ROUTINE_AUTH = ("header", "bearer", "hmac-sha256")
ROUTINE_METHODS = ("POST", "PUT", "PATCH")
ROUTINE_DEFAULT_HEADER = {
    "header": "X-Routine-Key",
    "bearer": "Authorization",
    "hmac-sha256": "X-Agent-Sync-Signature",
}
ROUTINE_RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "user-agent",
        "idempotency-key",
        "cookie",
    }
)
ROUTINE_TIMEOUT_DEFAULT = 15.0
OUTGOING_WEBHOOK_DEFAULT_PORT = 8787
LISTENER_API_DEFAULT_PORT = 8788
_SEAT_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
_BOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9=:_.-]{0,32}$")


def env_code(seat: str) -> str:
    """GB-COMPILER -> GB_COMPILER, the CODE in ZULIP_<CODE>_EMAIL."""
    return re.sub(r"[^A-Z0-9]", "_", seat.upper())


# Same canonical locator shape the board CLI enforces (scripts/linear-board/board BEARER_LOCATOR_RE).
BEARER_LOCATOR_RE = re.compile(
    r"/[A-Za-z0-9_-]{1,64}(?:/[A-Za-z0-9_-]{1,64}){0,7}:[A-Z][A-Z0-9_]{0,63}"
)


class RoutineConfig:
    """Where a remote `http` wake goes:  the names of the environment variables that hold the
    routine URL and its sender key (never the values), the method, the header and auth mode."""

    def __init__(
        self,
        *,
        url_env: str,
        key_env: str,
        method: str,
        header: str,
        auth: str,
        timeout: float,
        cost_usd: float,
        signature_prefix: str,
    ) -> None:
        self.url_env = url_env
        self.key_env = key_env
        self.method = method
        self.header = header
        self.auth = auth
        self.timeout = timeout
        self.cost_usd = cost_usd
        self.signature_prefix = signature_prefix

    def describe(self) -> dict[str, Any]:
        return {
            "url_env": self.url_env,
            "key_env": self.key_env,
            "method": self.method,
            "header": self.header,
            "auth": self.auth,
            "timeout": self.timeout,
            "cost_usd": self.cost_usd,
        }


class SeatConfig:
    def __init__(
        self,
        seat: str,
        bot: str,
        wake: str,
        model: str,
        budget: dict[str, float],
        live: dict[str, int],
        claude: str | None,
        *,
        instance: str = "mac",
        enabled: bool = True,
        creds: str = "file",
        email_env: str | None = None,
        key_env: str | None = None,
        site_env: str = DEFAULT_SITE_ENV,
        routine: RoutineConfig | None = None,
        route_enabled: bool = True,
        held_back: dict[str, Any] | None = None,
        muse: str | None = None,
        provider: str | None = None,
        mcode: str | None = None,
        mcode_model: str = MCODE_DEFAULT_MODEL,
        fallback: str | None = None,
        fallback_uncontained_ok: bool = False,
        muse_bridge: bool = False,
        typing_indicator: bool = False,
        bearer_locator: str | None = None,
        config_problems: list[str] | None = None,
    ) -> None:
        self.seat = seat
        self.bot = bot
        self.wake = wake
        self.model = model
        self.budget = budget
        self.live = live
        self.claude = (
            claude  # an absolute path to the claude binary, else resolved on the wake PATH
        )
        self.muse = muse  # an absolute path to the muse binary, else resolved on the wake PATH
        self.mcode = mcode  # an absolute path to the mcode binary, else resolved on the wake PATH
        # The model the MiniMax Code fallback runtime runs on.  It is named on the command line
        # (mcode takes `--model provider/model`), so the fallback never inherits a MiniMax Code
        # default that the catalog does not serve.  A seat may name another one deliberately.
        self.mcode_model = mcode_model
        # The fallback runtime, tried only when the primary could not produce a usable answer.  It is
        # never consulted for a quota refusal:  a capped provider stays capped whatever runs.
        self.fallback = fallback
        # The fallback runtime's tools are live while it answers and a parent cannot undo a side
        # effect that already happened inside the child, so trusting it is an explicit, named risk
        # rather than a default.  False means the fallback is held and the hold is logged.
        self.fallback_uncontained_ok = fallback_uncontained_ok
        self.provider = provider  # the CodeCaps provider name this seat's quota is read under
        self.instance = instance
        self.enabled = enabled
        self.creds = creds  # "file", "infisical" or "env"
        # The seat's own simple-board bearer LOCATOR (a Fleet-OPS FOLDER:KEY_NAME, never a secret), from this trusted
        # daemon config only.  It names where the seat's bearer lives;  the CLI still verifies it and takes the seat
        # from the verifier, so a wake or request can never choose a credential.  None = this seat files no board item.
        self.bearer_locator = bearer_locator
        self.email_env = email_env
        self.key_env = key_env
        self.site_env = site_env
        self.routine = routine
        self.muse_bridge = (
            muse_bridge  # default-off central decision path; original adapter stays configured
        )
        # Wake-config problems found in this seat's section (AFC-358).  The seat connects and captures
        # anyway;  its wake was downgraded to inbox, and status shows the seat red until they are fixed.
        self.config_problems: list[str] = list(config_problems or [])
        self.route_enabled = route_enabled  # the per-seat kill switch for wake route suggestions
        # The per-seat switch for the typing indicator (AFC-515).  Off by default:  a seat that
        # shows "is typing" while it thinks changes what every other seat and the owner sees in
        # the conversation, so each seat turns it on for itself.  The indicator is cosmetic and
        # never changes whether the seat answers.
        self.typing_indicator = typing_indicator
        self.held_back: dict[str, Any] = (
            held_back if held_back is not None else held_back_defaults(wake)
        )

    @property
    def wake_max_usd(self) -> float:
        """What one wake may cost, reserved against usd_per_day:  the claude run's maximum, a
        routine's configured cost (default 0:  the daemon spends nothing; the routine's platform
        bills its own run), nothing for inbox, and nothing for muse.

        The muse value is 0.0 DELIBERATELY (AFC-291), not an oversight and not a rounding of the
        claude number.  Three reasons, in order:

        1.  There is no per-run dollar cap.  `muse exec` has no `--max-budget-usd` equivalent, so
            the $0.25 the claude wake reserves is a number about a cap that does not exist.
        2.  A subscription-backed run has no marginal cost.  The seat's user already pays the plan;
            one more or one fewer wake in a window changes no bill, so reserving against
            usd_per_day would subtract a fictional quantity from a real ceiling.
        3.  The owner asked for exactly this (2026-10-11):  cost is not the concern.  What matters is
            not spamming the fleet and not causing downstream cost by fanning one wake into many.

        So the dollar axis carries no signal for a muse seat and the COUNT caps do the bounding:
        wakes_per_hour 6, wakes_per_day 40, per_topic_per_hour 2, owner_per_day 20 and
        owner_per_topic_per_hour 6 (BUDGET_DEFAULTS) are unchanged and still enforced first, exactly
        as they are for a claude seat.  usd_per_day still exists in the config and is still read, so
        an operator who sets it sees it enforced as a plain ceiling of $0 spent against $0 spent:  it
        never blocks, which is correct, because the run it would have blocked was never paid for.

        The consequence to be explicit about:  the held-back machinery (heldback.py) and its
        eviction reserve `reserve_usd` from this value to decide what a wake might still spend.  At
        0.0 a muse seat reserves nothing, so nothing is held back on cost grounds.  That is correct
        rather than a gap:  a wake is held back when a COUNT cap is hit, and holding it back "in
        case it turns out to be expensive" is not a reason that can apply to a run with no cost."""
        if self.wake == "claude":
            return WAKE_MAX_BUDGET_USD
        if self.wake == "http" and self.routine is not None:
            return self.routine.cost_usd
        return 0.0

    def __repr__(self) -> str:
        return "SeatConfig(%s, bot=%s, wake=%s, creds=%s)" % (
            self.seat,
            self.bot,
            self.wake,
            self.creds,
        )


class Config:
    def __init__(self) -> None:
        self.raw: dict[str, Any] = {}
        self.muse_bridge_enabled = False
        self.muse_bridge_remote_seats: tuple[str, ...] = ()
        # The production application the Mac bridge reaches, from the operator's
        # own config.  Empty when the bridge is off.
        self.muse_bridge_remote_app = ""
        self.errors: list[str] = []
        # Seats a config problem kept out entirely (AFC-358):  seat -> why.  They have no runner, so
        # status shows them as a red row instead of letting them vanish.
        self.skipped_seats: dict[str, list[str]] = {}
        self.instance = "mac"
        self.owner_user_id = 0
        # The Operator service bot's own Zulip user id, pinned in this host's listener.toml the
        # same way the owner's is.  A production user id is private inventory, so none is
        # committed:  the host pins it and the runtime reads it (Kody review on AFC-473).  Without
        # it a seat cannot be told "the Operator opened this DM" from "a seat opened it", and the
        # runtime refuses by name instead of guessing.
        self.operator_user_id = 0
        self.eligible_user_ids: set[int] = set()
        # `[operator] dm_allow`:  the user ids besides the pinned owner that the Operator may hand a
        # tagged address's candidate list to (AFC-412).  The list carries internal session ids and
        # titles, so it is empty by default and only the owner is served.
        self.operator_dm_allow: frozenset[int] = frozenset()
        self.owner_clients: set[str] = set(DEFAULT_OWNER_CLIENTS)
        self.stale_after = float(L.STALE_AFTER_DEFAULT)
        self.wake_loop_prevention = True
        self.coalesce_seconds = 20.0
        self.coalesce_max_seconds = 90.0
        self.owner_coalesce_seconds = 5.0
        self.presence: list[tuple[str, str]] = [(c, t) for c, t in L.DEFAULT_PRESENCE]
        self.claude_seat: str | None = None
        self.rewake_verified_cli = False
        self.rewake_verified_desktop = False
        self.seats: dict[str, SeatConfig] = {}
        self.disabled: dict[
            str, SeatConfig
        ] = {}  # parsed and checked, but no queue (enabled = false)
        self.seat_instances: dict[
            str, str | None
        ] = {}  # every [seat.X] section:  its own instance key, if any
        self.notify_banners = True
        self.route_enabled = (
            True  # daemon.route_enabled:  the listener-wide kill switch for route suggestions
        )
        self.held_back_catch_up = (
            True  # the default for every seat's held_back.catch_up (heldback.py)
        )
        # The `#alerts` held-back digest (AFC-257, heldalert.py).  `held_alert_minutes` is the
        # instance-wide window:  at most one post for EVERY seat, so the volume is bounded by the
        # window rather than the fleet size.  0 turns it off.  `held_alert_seat` is still parsed so an
        # old config loads, but it no longer names anything: the Operator bot carries the post, never
        # a seat's bot (owner ruling, Oct 30 2026; operator_post.py).
        self.held_alert_minutes = float(HELD_ALERT_DEFAULT_MINUTES)
        self.held_alert_seat: str | None = None
        # The peer-broadcast reminder (owner, Oct 27 2026).  How long a fleet bot's `@all` may go
        # unexplained before the listener DMs the owner about it;  0 turns the reminder off.
        self.peer_broadcast_reminder_minutes = float(PEER_BROADCAST_REMINDER_DEFAULT_MINUTES)
        self.auto_subscribe = (
            True  # daemon.auto_subscribe:  held seats join every public channel (owner 2026-10-10)
        )
        self.outgoing_webhook_seats: frozenset[str] = frozenset()
        self.outgoing_webhook_enabled = False
        self.outgoing_webhook_bind = "127.0.0.1"
        self.outgoing_webhook_port = OUTGOING_WEBHOOK_DEFAULT_PORT
        # The signed read API for the hosted Worker's listener_* tools (AFC-330, listener_api.py).
        self.listener_api_enabled = False
        # The Operator's Apple Notes queue (AFC-486, notes_queue.py), served under /notes/v1/ by the listener API:
        # the server instance only.  `worker_seats` and `server_seats` are the seats the WORKER and SERVER principals
        # may act for.  The [notes_relay] section of AFC-331 is retired and ignored.
        self.notes_queue_enabled = False
        self.notes_queue_worker_seats: frozenset[str] = frozenset()
        self.notes_queue_server_seats: frozenset[str] = frozenset()
        # The Linear project webhook (AFC-565, linear_projects_hook.py), served under
        # /linear/v1/ by the listener API.  Off by default;  the server instance only.
        self.linear_projects_enabled = False
        self.listener_api_bind = "127.0.0.1"
        self.listener_api_port = LISTENER_API_DEFAULT_PORT
        # Mac-only wake backpressure during host saturation (OPS-950).  Peer wakes are held back
        # when load per core is high; owner wakes are not delayed for load.  A cap limits how
        # many muse/claude children run at once across every seat on this instance.
        self.host_wake_defer = False
        self.host_wake_load_per_core = 3.0
        self.host_wake_defer_max_minutes = 20.0
        self.host_wake_max_running = 0

    @property
    def rewake_verified(self) -> bool:
        """True only when both surfaces are verified (the blanket answer for an unnamed surface)."""
        return self.rewake_verified_cli and self.rewake_verified_desktop

    @property
    def ok(self) -> bool:
        return not self.errors


def _number(value: Any, name: str, errors: list[str], *, minimum: float = 0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        errors.append("%s must be a number of at least %g" % (name, minimum))
        return None
    return float(value)


def held_back_defaults(wake: str, catch_up: bool = True) -> dict[str, Any]:
    held: dict[str, Any] = {
        k: (int(v) if HELD_BACK_LIMITS[k][2] else float(v)) for k, v in HELD_BACK_DEFAULTS.items()
    }
    held["catch_up"] = bool(catch_up) and wake in WORKER_WAKES
    return held


def _held_back(
    seat: str, wake: str, given: Any, problems: list[str], *, catch_up: bool = True
) -> dict[str, Any]:
    """The seat's `held_back` table over the defaults (catch_up from `[daemon] held_back_catch_up`).  A
    bad value is an error like a bad budget."""
    held = held_back_defaults(wake, catch_up)
    if not isinstance(given, dict):
        problems.append("seat.%s.held_back must be a table" % seat)
        return held
    for key, value in given.items():
        name = "seat.%s.held_back.%s" % (seat, key)
        if key == "catch_up":
            if not isinstance(value, bool):
                problems.append("%s must be true or false" % name)
            elif value and wake not in WORKER_WAKES:
                problems.append(
                    '%s applies only to wake = "claude" or "http" (an inbox seat never wakes)'
                    % name
                )
            else:
                held["catch_up"] = value
            continue
        if key not in HELD_BACK_LIMITS:
            problems.append("%s is not a known held_back setting" % name)
            continue
        low, high, whole = HELD_BACK_LIMITS[key]
        number = _number(value, name, problems, minimum=low)
        if number is None:
            continue
        if number > high or (whole and number != int(number)):
            problems.append(
                "%s must be a%s number from %g to %g" % (name, " whole" if whole else "", low, high)
            )
            continue
        if key == "owner_digest_minutes" and 0 < number < HELD_BACK_MIN_DIGEST_MINUTES:
            problems.append(
                "%s must be 0 (off) or at least %d" % (name, HELD_BACK_MIN_DIGEST_MINUTES)
            )
            continue
        held[key] = int(number) if whole else number
    return held


def _env_name(value: Any, name: str, problems: list[str]) -> str | None:
    if not isinstance(value, str) or not _ENV_NAME_RE.match(value):
        problems.append(
            "%s must be an environment variable name (upper case letters, digits and _)" % name
        )
        return None
    if value in GENERIC_ENV:
        problems.append(
            "%s may not be %s: that is the CLI's single-seat variable, and two seats reading it would "
            "alias one bot" % (name, value)
        )
        return None
    return value


def _routine(seat: str, given: Any, problems: list[str]) -> RoutineConfig | None:
    if not isinstance(given, dict):
        problems.append(
            'seat.%s.routine must be a table (url_env, key_env, method, header, auth) when wake = "http"'
            % seat
        )
        return None
    known = {
        "url_env",
        "key_env",
        "method",
        "header",
        "auth",
        "timeout_seconds",
        "cost_usd",
        "signature_prefix",
    }
    for key in sorted(set(given) - known):
        problems.append("seat.%s.routine.%s is not a known routine setting" % (seat, key))
    code = env_code(seat)
    url_env = _env_name(
        given.get("url_env", code + "_ROUTINE_URL"), "seat.%s.routine.url_env" % seat, problems
    )
    key_env = _env_name(
        given.get("key_env", code + "_ROUTINE_KEY"), "seat.%s.routine.key_env" % seat, problems
    )
    method = str(given.get("method", "POST")).upper()
    if method not in ROUTINE_METHODS:
        problems.append(
            "seat.%s.routine.method must be one of %s" % (seat, ", ".join(ROUTINE_METHODS))
        )
    auth = given.get("auth", "bearer")
    if auth not in ROUTINE_AUTH:
        problems.append("seat.%s.routine.auth must be one of %s" % (seat, ", ".join(ROUTINE_AUTH)))
        auth = "bearer"
    header = given.get("header", ROUTINE_DEFAULT_HEADER[auth])
    if (
        not isinstance(header, str)
        or not _HEADER_RE.match(header)
        or header.casefold() in ROUTINE_RESERVED_HEADERS
    ):
        problems.append(
            "seat.%s.routine.header must be an HTTP header name (letters, digits and -) other than %s"
            % (seat, ", ".join(sorted(ROUTINE_RESERVED_HEADERS)))
        )
    prefix = given.get("signature_prefix", "")
    if not isinstance(prefix, str) or not _PREFIX_RE.match(prefix):
        problems.append(
            "seat.%s.routine.signature_prefix must be up to 32 letters, digits or =:_.-" % seat
        )
        prefix = ""
    elif prefix and auth != "hmac-sha256":
        problems.append(
            'seat.%s.routine.signature_prefix applies only to auth = "hmac-sha256"' % seat
        )
    timeout = _number(
        given.get("timeout_seconds", ROUTINE_TIMEOUT_DEFAULT),
        "seat.%s.routine.timeout_seconds" % seat,
        problems,
        minimum=1,
    )
    if timeout is not None and timeout > 60:
        problems.append("seat.%s.routine.timeout_seconds must be 60 or less" % seat)
    cost = _number(given.get("cost_usd", 0.0), "seat.%s.routine.cost_usd" % seat, problems)
    if url_env and key_env and url_env == key_env:
        problems.append("seat.%s.routine.url_env and key_env must be different variables" % seat)
    if not (url_env and key_env and timeout is not None and cost is not None):
        return None
    return RoutineConfig(
        url_env=url_env,
        key_env=key_env,
        method=method,
        header=str(header),
        auth=str(auth),
        timeout=timeout,
        cost_usd=cost,
        signature_prefix=prefix,
    )


def from_dict(raw: Mapping[str, Any]) -> Config:
    cfg = Config()
    cfg.raw = dict(raw)
    errors = cfg.errors
    bridge = raw.get("muse_bridge") or {}
    if not isinstance(bridge, dict):
        errors.append("[muse_bridge] must be a table")
        bridge = {}
    bridge_enabled = bridge.get("enabled", False)
    if not isinstance(bridge_enabled, bool):
        errors.append("muse_bridge.enabled must be true or false")
    else:
        cfg.muse_bridge_enabled = bridge_enabled
    # The production application the Mac bridge talks to is private deployment
    # inventory, so it lives in the operator's own config and never in this
    # repository.  It stays optional here: the bridge fails closed at request
    # time and says so, which keeps a config that never enabled the bridge
    # valid.
    remote_app = bridge.get("remote_app", "")
    if not isinstance(remote_app, str):
        errors.append("muse_bridge.remote_app must be a string")
        remote_app = ""
    cfg.muse_bridge_remote_app = remote_app.strip()
    remote_seats = bridge.get("remote_seats", [])
    if (
        not isinstance(remote_seats, list)
        or not all(isinstance(s, str) and _SEAT_RE.fullmatch(s) for s in remote_seats)
        or len(set(remote_seats)) != len(remote_seats)
    ):
        errors.append("muse_bridge.remote_seats must be unique canonical seat names")
    else:
        cfg.muse_bridge_remote_seats = tuple(remote_seats)
    daemon = raw.get("daemon") or {}
    if not isinstance(daemon, dict):
        errors.append("[daemon] must be a table")
        daemon = {}
    instance = daemon.get("instance", "mac")
    if instance not in INSTANCES:
        errors.append("daemon.instance must be one of %s" % ", ".join(INSTANCES))
    else:
        cfg.instance = instance
    if cfg.instance == "server" and cfg.muse_bridge_remote_seats:
        errors.append("muse_bridge.remote_seats is for the Mac bridge only")
    owner = daemon.get("owner_user_id", 0)
    if isinstance(owner, bool) or not isinstance(owner, int) or owner < 0:
        errors.append("daemon.owner_user_id must be a user id (0 means not pinned yet)")
    else:
        cfg.owner_user_id = owner
    operator = daemon.get("operator_user_id", 0)
    if isinstance(operator, bool) or not isinstance(operator, int) or operator < 0:
        errors.append("daemon.operator_user_id must be a user id (0 means not pinned yet)")
    else:
        cfg.operator_user_id = operator
    eligible = daemon.get("eligible_user_ids", [])
    if not isinstance(eligible, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) for i in eligible
    ):
        errors.append("daemon.eligible_user_ids must be a list of user ids")
    else:
        cfg.eligible_user_ids = set(eligible)
    clients = daemon.get("owner_clients", DEFAULT_OWNER_CLIENTS)
    if not isinstance(clients, list) or not all(isinstance(c, str) for c in clients):
        errors.append("daemon.owner_clients must be a list of client names")
    else:
        cfg.owner_clients = set(clients)
    for name, attr, scale in (
        ("stale_after_minutes", "stale_after", 60.0),
        ("coalesce_seconds", "coalesce_seconds", 1.0),
        ("coalesce_max_seconds", "coalesce_max_seconds", 1.0),
        ("owner_coalesce_seconds", "owner_coalesce_seconds", 1.0),
    ):
        if name in daemon:
            value = _number(daemon[name], "daemon." + name, errors)
            if value is not None:
                setattr(cfg, attr, value * scale)
    # macOS banners on the Mac; the server has no display, so it keeps only the owner queue.
    cfg.notify_banners = (
        daemon["notify_banners"] is True if "notify_banners" in daemon else cfg.instance == "mac"
    )
    route_on = daemon.get("route_enabled", True)
    if not isinstance(route_on, bool):
        errors.append("daemon.route_enabled must be true or false")
    else:
        cfg.route_enabled = route_on
    loop_prevention = daemon.get("wake_loop_prevention", True)
    if not isinstance(loop_prevention, bool):
        errors.append("daemon.wake_loop_prevention must be true or false")
    else:
        cfg.wake_loop_prevention = loop_prevention
    catch_up = daemon.get("held_back_catch_up", True)
    if not isinstance(catch_up, bool):
        errors.append("daemon.held_back_catch_up must be true or false")
    else:
        cfg.held_back_catch_up = catch_up
    # The `#alerts` held-back digest (AFC-257).  One post for every seat per window, so a busy fleet
    # does not turn a loud trigger into a flood;  0 turns it off.
    if "held_alert_minutes" in daemon:
        value = _number(daemon["held_alert_minutes"], "daemon.held_alert_minutes", errors)
        if value is not None:
            low, high = HELD_ALERT_LIMITS["held_alert_minutes"]
            if not low <= value <= high:
                errors.append("daemon.held_alert_minutes must be between %g and %g" % (low, high))
            else:
                cfg.held_alert_minutes = value
    # The peer-broadcast reminder (owner, Oct 27 2026).  Ten minutes by default;  0 turns it off.
    if "peer_broadcast_reminder_minutes" in daemon:
        value = _number(
            daemon["peer_broadcast_reminder_minutes"],
            "daemon.peer_broadcast_reminder_minutes",
            errors,
        )
        if value is not None:
            low, high = PEER_BROADCAST_LIMITS["peer_broadcast_reminder_minutes"]
            if not low <= value <= high:
                errors.append(
                    "daemon.peer_broadcast_reminder_minutes must be between %g and %g" % (low, high)
                )
            else:
                cfg.peer_broadcast_reminder_minutes = value
    # Which seat's bot carries the digest.  Optional:  the daemon falls back to the claude seat, then
    # to the first enabled seat, so an unset value still posts rather than silently going quiet.
    if "held_alert_seat" in daemon:
        alert_seat = daemon["held_alert_seat"]
        if not isinstance(alert_seat, str) or not _SEAT_RE.match(alert_seat.upper()):
            errors.append("daemon.held_alert_seat must be a seat name such as JET")
        else:
            cfg.held_alert_seat = alert_seat.upper()
    auto_sub = daemon.get("auto_subscribe", True)
    if not isinstance(auto_sub, bool):
        errors.append("daemon.auto_subscribe must be true or false")
    else:
        cfg.auto_subscribe = auto_sub
    host_defer = daemon.get("host_wake_defer")
    if host_defer is None:
        cfg.host_wake_defer = cfg.instance == "mac"
    elif not isinstance(host_defer, bool):
        errors.append("daemon.host_wake_defer must be true or false")
    else:
        cfg.host_wake_defer = host_defer
    if "host_wake_load_per_core" in daemon:
        value = _number(
            daemon["host_wake_load_per_core"], "daemon.host_wake_load_per_core", errors, minimum=0
        )
        if value is not None:
            if value > 100:
                errors.append("daemon.host_wake_load_per_core must be at most 100")
            else:
                cfg.host_wake_load_per_core = value
    if "host_wake_defer_max_minutes" in daemon:
        value = _number(
            daemon["host_wake_defer_max_minutes"],
            "daemon.host_wake_defer_max_minutes",
            errors,
            minimum=0,
        )
        if value is not None:
            if value > 720:
                errors.append("daemon.host_wake_defer_max_minutes must be at most 720")
            else:
                cfg.host_wake_defer_max_minutes = value
    if "host_wake_max_running" in daemon:
        value = daemon["host_wake_max_running"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 32:
            errors.append(
                "daemon.host_wake_max_running must be a whole number from 0 to 32 (0 means no cap)"
            )
        else:
            cfg.host_wake_max_running = value
    elif cfg.host_wake_defer:
        cfg.host_wake_max_running = 2
    webhook_seats = daemon.get("outgoing_webhook_seats", [])
    if not isinstance(webhook_seats, list) or not all(
        isinstance(value, str) for value in webhook_seats
    ):
        errors.append("daemon.outgoing_webhook_seats must be a list of seat names")
    else:
        normalized = [value.upper() for value in webhook_seats]
        invalid = [value for value in normalized if not _SEAT_RE.fullmatch(value)]
        if invalid:
            errors.append("daemon.outgoing_webhook_seats contains an invalid seat name")
        elif len(set(normalized)) != len(normalized):
            errors.append("daemon.outgoing_webhook_seats must not contain duplicates")
        else:
            cfg.outgoing_webhook_seats = frozenset(normalized)
    webhook = raw.get("outgoing_webhook") or {}
    if not isinstance(webhook, dict):
        errors.append("[outgoing_webhook] must be a table")
        webhook = {}
    unknown_webhook = set(webhook) - {"enabled", "bind", "port"}
    if unknown_webhook:
        errors.append(
            "[outgoing_webhook] has unknown setting(s): %s" % ", ".join(sorted(unknown_webhook))
        )
    webhook_enabled = webhook.get("enabled", False)
    if not isinstance(webhook_enabled, bool):
        errors.append("outgoing_webhook.enabled must be true or false")
    else:
        cfg.outgoing_webhook_enabled = webhook_enabled
    webhook_bind = webhook.get("bind", "127.0.0.1")
    if not isinstance(webhook_bind, str) or not webhook_bind or len(webhook_bind) > 253:
        errors.append("outgoing_webhook.bind must be a host or IP address")
    else:
        cfg.outgoing_webhook_bind = webhook_bind
    webhook_port = webhook.get("port", OUTGOING_WEBHOOK_DEFAULT_PORT)
    if (
        isinstance(webhook_port, bool)
        or not isinstance(webhook_port, int)
        or not 1 <= webhook_port <= 65535
    ):
        errors.append("outgoing_webhook.port must be an integer from 1 to 65535")
    else:
        cfg.outgoing_webhook_port = webhook_port
    api = raw.get("listener_api") or {}
    if not isinstance(api, dict):
        errors.append("[listener_api] must be a table")
        api = {}
    unknown_api = set(api) - {"enabled", "bind", "port"}
    if unknown_api:
        errors.append("[listener_api] has unknown setting(s): %s" % ", ".join(sorted(unknown_api)))
    if not isinstance(api.get("enabled", False), bool):
        errors.append("listener_api.enabled must be true or false")
    else:
        cfg.listener_api_enabled = api.get("enabled", False)
    api_bind = api.get("bind", "127.0.0.1")
    if not isinstance(api_bind, str) or not api_bind or len(api_bind) > 253:
        errors.append("listener_api.bind must be a host or IP address")
    else:
        cfg.listener_api_bind = api_bind
    api_port = api.get("port", LISTENER_API_DEFAULT_PORT)
    if isinstance(api_port, bool) or not isinstance(api_port, int) or not 1 <= api_port <= 65535:
        errors.append("listener_api.port must be an integer from 1 to 65535")
    elif (
        cfg.outgoing_webhook_enabled
        and cfg.listener_api_enabled is True
        and api_port == cfg.outgoing_webhook_port
    ):
        errors.append("listener_api.port must differ from outgoing_webhook.port")
    else:
        cfg.listener_api_port = api_port
    operator = raw.get("operator") or {}
    if not isinstance(operator, dict):
        errors.append("[operator] must be a table")
        operator = {}
    unknown_operator = set(operator) - {"dm_allow"}
    if unknown_operator:
        errors.append("[operator] has unknown setting(s): %s" % ", ".join(sorted(unknown_operator)))
    dm_allow = operator.get("dm_allow", [])
    if not isinstance(dm_allow, list) or not all(
        isinstance(i, int) and not isinstance(i, bool) and i > 0 for i in dm_allow
    ):
        # A malformed entry is an error the owner sees, never a silent drop:  the allowlist then
        # stays empty, so a bad section fails closed to the owner alone.
        errors.append("operator.dm_allow must be a list of user ids")
    else:
        cfg.operator_dm_allow = frozenset(dm_allow)
    queue_errors_before = len(errors)
    queue_section = raw.get("notes_queue") or {}
    if not isinstance(queue_section, dict):
        errors.append("[notes_queue] must be a table")
        queue_section = {}
    unknown_queue = set(queue_section) - {"enabled", "worker_seats", "server_seats"}
    if unknown_queue:
        errors.append("[notes_queue] has unknown setting(s): %s" % ", ".join(sorted(unknown_queue)))
    if not isinstance(queue_section.get("enabled", False), bool):
        errors.append("notes_queue.enabled must be true or false")
    else:
        cfg.notes_queue_enabled = queue_section.get("enabled", False)
    for name in ("worker_seats", "server_seats"):
        seats_list = queue_section.get(name, [])
        if not isinstance(seats_list, list) or not all(
            isinstance(s, str) and _SEAT_RE.match(s.upper()) for s in seats_list
        ):
            errors.append("notes_queue.%s must be a list of seat names such as CLAUDE" % name)
        else:
            setattr(cfg, "notes_queue_%s" % name, frozenset(s.upper() for s in seats_list))
    if len(errors) > queue_errors_before:
        cfg.notes_queue_enabled = False  # a section with a problem never turns the queue on
    if cfg.notes_queue_enabled and cfg.instance != "server":
        errors.append(
            "notes_queue.enabled applies to the server instance only:  the Mac writes its own notes directly"
        )
        cfg.notes_queue_enabled = False
    linear_errors_before = len(errors)
    linear_section = raw.get("linear_projects", {})
    if not isinstance(linear_section, dict):
        errors.append("[linear_projects] must be a table")
        linear_section = {}
    unknown_linear = set(linear_section) - {"enabled"}
    if unknown_linear:
        errors.append(
            "[linear_projects] has unknown setting(s): %s" % ", ".join(sorted(unknown_linear))
        )
    if not isinstance(linear_section.get("enabled", False), bool):
        errors.append("linear_projects.enabled must be true or false")
    else:
        cfg.linear_projects_enabled = linear_section.get("enabled", False)
    if len(errors) > linear_errors_before:
        cfg.linear_projects_enabled = False  # a section with a problem never turns the hook on
    cfg.presence = L.presence_topics(raw)
    platform = (
        ((raw.get("platform") or {}).get("claude-code") or {})
        if isinstance(raw.get("platform"), dict)
        else {}
    )
    seat = platform.get("seat") if isinstance(platform, dict) else None
    if seat is not None:
        if isinstance(seat, str) and _SEAT_RE.match(seat.upper()):
            cfg.claude_seat = seat.upper()
        else:
            errors.append("platform.claude-code.seat must be a seat name such as CLAUDE")
    cfg.rewake_verified_cli, cfg.rewake_verified_desktop = L.rewake_flags(raw)
    for key in L.REWAKE_SURFACE_KEYS:
        if key in platform and not isinstance(platform[key], bool):
            # Fail soft:  the flag is False for that surface (never the legacy value) and the seat keeps
            # capturing;  the problem shows in status and raises the config banner.
            errors.append("platform.claude-code.%s must be true or false;  treated as false" % key)
    seats = raw.get("seat") or {}
    if not isinstance(seats, dict):
        errors.append("[seat.X] sections must be tables")
        seats = {}
    for name, section in seats.items():
        seat_name = str(name).upper()
        if not _SEAT_RE.match(seat_name) or not isinstance(section, dict):
            errors.append("seat.%s is not a valid seat section" % name)
            if _SEAT_RE.match(seat_name):  # an unsafe name never becomes a path or a status key
                cfg.skipped_seats[seat_name] = ["not a valid seat section"]
            continue
        # Two kinds of problem (AFC-358).  `problems` stop the seat:  without a valid instance, credential
        # and environment names there is no bot to connect as, so nothing can be captured.
        # `wake_problems` concern only how the seat is WOKEN (the wake type, its binaries, the model,
        # a routine, the budgets):  the seat still connects and still queues every message, and only
        # its wake drops to inbox.  A typo in a wake key once cost a seat 41 minutes of mentions.
        problems: list[str] = []
        wake_problems: list[str] = []
        seat_instance = section.get("instance")
        if seat_instance is not None and seat_instance not in INSTANCES:
            problems.append(
                "seat.%s.instance must be one of %s" % (seat_name, ", ".join(INSTANCES))
            )
            seat_instance = None
        cfg.seat_instances[seat_name] = seat_instance
        enabled = section.get("enabled", True)
        if not isinstance(enabled, bool):
            problems.append("seat.%s.enabled must be true or false" % seat_name)
            enabled = True
        route_enabled = section.get("route_enabled", True)
        if not isinstance(route_enabled, bool):
            wake_problems.append("seat.%s.route_enabled must be true or false" % seat_name)
            route_enabled = True
        muse_bridge = section.get("muse_bridge", False)
        if not isinstance(muse_bridge, bool):
            wake_problems.append("seat.%s.muse_bridge must be true or false" % seat_name)
            muse_bridge = False
        typing_indicator = section.get("typing_indicator", False)
        if not isinstance(typing_indicator, bool):
            wake_problems.append("seat.%s.typing_indicator must be true or false" % seat_name)
            typing_indicator = False
        bearer_locator = section.get("bearer_locator")
        if bearer_locator is not None and (
            not isinstance(bearer_locator, str) or not BEARER_LOCATOR_RE.fullmatch(bearer_locator)
        ):
            wake_problems.append(
                "seat.%s.bearer_locator must be a FOLDER:KEY_NAME location (no secret value)"
                % seat_name
            )
            bearer_locator = None
        creds = section.get("creds", "file")
        if creds not in ("file", "infisical", "env"):
            problems.append(
                'seat.%s.creds must be "file" (the bot\'s own credential:  Infisical /zulip, or a zuliprc '
                'in AGENT_SYNC_SECRETS_DIR), "infisical" or "env" (environment variables)'
                % seat_name
            )
            creds = "file"
        if creds == "env" and cfg.instance != "server":
            problems.append(
                'seat.%s.creds = "env" is for the server instance only; the Mac instance reads each '
                "seat's own bot from Infisical /zulip" % seat_name
            )
        bot = section.get("bot")
        if creds in ("file", "infisical") or bot is not None:
            if not isinstance(bot, str) or not _BOT_RE.match(bot):
                problems.append(
                    "seat.%s.bot must be the credential file code (e.g. Claude for Claude-zuliprc)"
                    % seat_name
                )
        else:
            bot = Z.credential_file_name(seat_name)[
                : -len("-zuliprc")
            ]  # a display name only:  no file is read
        email_env = key_env = None
        site_env = DEFAULT_SITE_ENV
        if creds == "env":
            code = env_code(seat_name)
            email_env = _env_name(
                section.get("email_env", "ZULIP_%s_EMAIL" % code),
                "seat.%s.email_env" % seat_name,
                problems,
            )
            key_env = _env_name(
                section.get("key_env", "ZULIP_%s_API_KEY" % code),
                "seat.%s.key_env" % seat_name,
                problems,
            )
            site_given = section.get("site_env", DEFAULT_SITE_ENV)
            if not isinstance(site_given, str) or not _ENV_NAME_RE.match(site_given):
                problems.append("seat.%s.site_env must be an environment variable name" % seat_name)
            else:
                site_env = site_given
        else:
            for key in ("email_env", "key_env", "site_env"):
                if key in section:
                    problems.append('seat.%s.%s applies only to creds = "env"' % (seat_name, key))
        wake = section.get("wake", "inbox")
        if seat_name == "OPERATOR" and (
            creds != "infisical" or wake != "inbox" or bot != "Operator"
        ):
            problems.append("OPERATOR service requires bot=Operator, creds=infisical, wake=inbox")
        if wake not in KNOWN_WAKES:
            wake_problems.append(
                "seat.%s.wake must be one of %s" % (seat_name, ", ".join(KNOWN_WAKES))
            )
        routine = None
        if wake == "http":
            routine = _routine(seat_name, section.get("routine"), wake_problems)
        elif "routine" in section:
            wake_problems.append('seat.%s.routine applies only to wake = "http"' % seat_name)
        model = section.get("model") or (MUSE_DEFAULT_MODEL if wake == "muse" else "sonnet")
        if not isinstance(model, str) or not re.fullmatch(r"[a-z0-9.\-\[\]]{1,60}", model):
            wake_problems.append("seat.%s.model must be a model alias such as sonnet" % seat_name)
        claude = section.get("claude")
        if claude is not None and (not isinstance(claude, str) or not os.path.isabs(claude)):
            wake_problems.append("seat.%s.claude must be an absolute path" % seat_name)
        muse = section.get("muse")
        if muse is not None and (not isinstance(muse, str) or not os.path.isabs(muse)):
            wake_problems.append("seat.%s.muse must be an absolute path" % seat_name)
        if wake != "muse" and "muse" in section:
            wake_problems.append('seat.%s.muse applies only to wake = "muse"' % seat_name)
        mcode = section.get("mcode")
        if mcode is not None and (not isinstance(mcode, str) or not os.path.isabs(mcode)):
            wake_problems.append("seat.%s.mcode must be an absolute path" % seat_name)
        if "mcode" in section and wake not in ("claude", "muse"):
            wake_problems.append(
                'seat.%s.mcode applies only to wake = "claude" or "muse"' % seat_name
            )
        # The model the mcode fallback runs on:  `provider/model`, named on the command line.
        mcode_model = section.get("mcode_model", MCODE_DEFAULT_MODEL)
        if not isinstance(mcode_model, str) or not re.fullmatch(MCODE_MODEL_PATTERN, mcode_model):
            wake_problems.append(
                "seat.%s.mcode_model must be provider/model, such as %s"
                % (seat_name, MCODE_DEFAULT_MODEL)
            )
        # The fallback runtime, tried only when the primary could not produce a usable answer.  It
        # is never consulted for a quota refusal:  a capped provider stays capped whatever runs.
        fallback = section.get("fallback")
        if fallback is not None and fallback not in KNOWN_FALLBACKS:
            wake_problems.append(
                "seat.%s.fallback must be one of %s" % (seat_name, ", ".join(KNOWN_FALLBACKS))
            )
        if fallback is not None and wake not in WORKER_WAKES:
            wake_problems.append(
                "seat.%s.fallback applies only to a wake that runs a turn" % seat_name
            )
        if fallback is not None and fallback == wake:
            wake_problems.append("seat.%s.fallback may not be its own wake type" % seat_name)
        # Naming a fallback is not enough to run it.  mcode's tools are live while it answers, and a
        # parent cannot undo a side effect that already happened inside the child, so the fallback
        # runtime is not contained to the posture an unattended wake needs (measured: asked to write
        # a file under the wake argv, it wrote one).  A seat must say so explicitly in the config
        # before the fallback is ever trusted, on top of pinning the binary.
        if "fallback_uncontained_ok" in section and fallback is None:
            wake_problems.append(
                "seat.%s.fallback_uncontained_ok applies only with a fallback" % seat_name
            )
        uncontained = section.get("fallback_uncontained_ok", False)
        if not isinstance(uncontained, bool):
            wake_problems.append(
                "seat.%s.fallback_uncontained_ok must be true or false" % seat_name
            )
        provider = section.get("provider")
        if provider is not None:
            if not isinstance(provider, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,60}", provider
            ):
                wake_problems.append(
                    'seat.%s.provider must be a provider name such as "Claude Code" or "MiniMax"'
                    % seat_name
                )
            elif wake not in ("claude", "muse"):
                wake_problems.append(
                    'seat.%s.provider applies only to wake = "claude" or "muse"' % seat_name
                )
        budget = dict(BUDGET_DEFAULTS)
        given = section.get("budget", {})
        if not isinstance(given, dict):
            wake_problems.append("seat.%s.budget must be a table" % seat_name)
            given = {}
        for key, value in given.items():
            if key not in BUDGET_DEFAULTS:
                wake_problems.append("seat.%s.budget.%s is not a known budget" % (seat_name, key))
                continue
            number = _number(value, "seat.%s.budget.%s" % (seat_name, key), wake_problems)
            if number is not None:
                budget[key] = number
        held_back = _held_back(
            seat_name,
            str(wake),
            section.get("held_back", {}),
            wake_problems,
            catch_up=cfg.held_back_catch_up,
        )
        if "allow_admin" in section:
            problems.append(
                "seat.%s.allow_admin is not supported: admin and owner bot keys are always refused, "
                "moderator and member keys are accepted (owner decision 2026-10-07)" % seat_name
            )
        used = [
            name
            for name in (
                email_env,
                key_env,
                site_env if creds == "env" else None,
                routine.url_env if routine else None,
                routine.key_env if routine else None,
            )
            if name
        ]
        routine_names = {routine.url_env, routine.key_env} if routine else set()
        for name in sorted({n for n in used if used.count(n) > 1}):
            message = (
                "seat.%s uses %s for more than one setting; its Zulip email, key and site and its routine "
                "URL and key must be separate variables (a shared one could send the bot key to the "
                "routine)" % (seat_name, name)
            )
            (wake_problems if name in routine_names else problems).append(message)
        if routine is not None and DEFAULT_SITE_ENV in (routine.url_env, routine.key_env):
            wake_problems.append(
                "seat.%s.routine may not read %s, the Zulip site variable"
                % (seat_name, DEFAULT_SITE_ENV)
            )
        if problems:
            errors.extend(problems)
            errors.extend(wake_problems)
            cfg.skipped_seats[seat_name] = problems + wake_problems
            continue
        if wake_problems:
            # Fail soft:  keep the seat connected and capturing, wake it by inbox only, and say why.
            errors.extend(wake_problems)
            wake, routine, fallback, held_back = "inbox", None, None, None
            claude = muse = mcode = None
            uncontained = False
            provider = None
            mcode_model = MCODE_DEFAULT_MODEL
            if not isinstance(model, str) or not re.fullmatch(r"[a-z0-9.\-\[\]]{1,60}", model):
                model = "sonnet"
        seat_cfg = SeatConfig(
            seat_name,
            bot,
            wake,
            model,
            budget,
            L.live_limits(raw, seat_name),
            claude,
            instance=seat_instance or cfg.instance,
            enabled=enabled,
            creds=creds,
            email_env=email_env,
            key_env=key_env,
            site_env=site_env,
            routine=routine,
            route_enabled=route_enabled,
            held_back=held_back,
            muse=muse,
            provider=provider,
            mcode=mcode,
            mcode_model=mcode_model,
            fallback=fallback,
            fallback_uncontained_ok=uncontained,
            muse_bridge=muse_bridge,
            typing_indicator=typing_indicator,
            bearer_locator=bearer_locator,
            config_problems=wake_problems,
        )
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
            cfg.errors.append(
                "environment variable %s is named by more than one seat (%s); a seat's variables must be "
                "its own, so none of them gets a queue" % (name, ", ".join(sorted(seats)))
            )
            for seat in seats:
                cfg.seats.pop(seat, None)
                cfg.disabled.pop(seat, None)
                cfg.skipped_seats.setdefault(seat, []).append(
                    "environment variable %s is shared with another seat" % name
                )


def load(root: str, path: str | None = None) -> Config:
    path = path or os.path.join(root, L.CONFIG_NAME)
    raw, error = L.load_config(root, path)
    cfg = from_dict(raw)
    if error:
        cfg.errors.insert(0, error)
    elif not raw:
        cfg.errors.insert(0, "no %s; run simple-chat daemon install (it writes a sample)" % path)
    if cfg.owner_user_id == 0 and raw:
        cfg.errors.append(
            "daemon.owner_user_id is 0 (not pinned): nothing is treated as the owner and nothing wakes; "
            "run simple-chat daemon init"
        )
    return cfg


# --------------------------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------------------------


def seat_credentials(
    seat_cfg: SeatConfig, env: Mapping[str, str], secrets_dir: str | None
) -> Z.Credentials:
    """The one place a listener seat's credentials are read:  its own bot's keys in Infisical /zulip
    (named by `bot`), its zuliprc in `secrets_dir` (only the explicit AGENT_SYNC_SECRETS_DIR override,
    never a default), or (creds = "env") the environment variables its config names.  Never ZULIP_RC
    or the CLI's triple."""
    if seat_cfg.seat == "OPERATOR":
        from .service_identity import operator_credentials

        if seat_cfg.creds != "infisical":
            raise Z.CredentialError(
                "OPERATOR service requires creds=infisical; no file/env fallback"
            )
        return operator_credentials(env)
    if seat_cfg.creds == "env":
        assert seat_cfg.email_env and seat_cfg.key_env
        return Z.env_credentials(
            env,
            email_env=seat_cfg.email_env,
            key_env=seat_cfg.key_env,
            site_env=seat_cfg.site_env,
            label=seat_cfg.seat,
        )
    if seat_cfg.creds == "file" and secrets_dir:
        return Z.read_zuliprc(os.path.join(secrets_dir, seat_cfg.bot + "-zuliprc"))
    return Z.infisical_credentials(
        seat_cfg.seat,
        env=env,
        home=Path(env.get("HOME") or os.path.expanduser("~")),
        stem=seat_cfg.bot,
    )


def seat_credential_present(
    seat_cfg: SeatConfig, env: Mapping[str, str], secrets_dir: str | None
) -> bool:
    """Whether the seat's credential exists (names only, never a value):  the override file, or its
    three /zulip names in Infisical, or (creds = "env") its variables."""
    if seat_cfg.creds == "env":
        return all(
            (env.get(n) or "").strip()
            for n in (seat_cfg.email_env, seat_cfg.key_env, seat_cfg.site_env)
            if n
        )
    if seat_cfg.creds == "file" and secrets_dir:
        return os.path.isfile(os.path.join(secrets_dir, seat_cfg.bot + "-zuliprc"))
    names = Z.infisical_names(env)
    return all(n in names for n in Z.infisical_key_names_for_stem(seat_cfg.bot))


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
        return None, (
            "the seat partition %s is missing (it ships with the package; %s may point at another copy)"
            % (path, ENV_PARTITION)
        )
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
                path,
                name,
                ", ".join(PARTITION_VALUES),
            )
        found[seat] = value
    return found, None


def fleet_tags(partition: Mapping[str, str] | None) -> frozenset[str]:
    """Every seat tag whose bot is an eligible sender:  each seat the partition lists, whatever its
    instance (`none` seats such as the BF role bots and GROK-BUILD count too), plus FLEET_SEATS.
    The partition also lists the deterministic service identities (service_identity.SERVICE_SEATS,
    such as OPERATOR); those hold no agent and never wake one, so they stay out of the eligible set
    and out of every `route.seat`."""
    from .service_identity import SERVICE_SEATS

    listed = frozenset(FLEET_SEATS) | frozenset(str(seat).upper() for seat in (partition or {}))
    return listed - SERVICE_SEATS


_DEFAULT_ENV_RE = re.compile(r"^ZULIP_([A-Z0-9_]+)_(EMAIL|API_KEY)$")


def credential_aliases(seat_cfg: SeatConfig, known: set[str]) -> list[tuple[str, str]]:
    """(what, other seat) for each credential this section reads that belongs to another known
    seat by name:  a `bot` file stem (GB-Compiler-zuliprc is GB-COMPILER's), or an `email_env` or
    `key_env` in another seat's default pattern (ZULIP_GB_COMPILER_API_KEY).  Custom names that
    match no known seat pass here; the connect-time users/me check still refuses them."""
    found: list[tuple[str, str]] = []
    if seat_cfg.creds in ("file", "infisical"):
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


def partition_errors(
    cfg: Config,
    partition: Mapping[str, str] | None,
    *,
    partition_file: str = "",
    env_instance: str | None = None,
) -> list[str]:
    """Why this config must not run on this instance, or [].  Every [seat.X] section counts,
    enabled or not.  It fails closed:  a seat the partition does not give to this instance (one
    of the other instance, a `none` seat, or one it does not list) is refused, and so is a
    section that reads another known seat's credential, so aliasing a name cannot hold a bot
    twice.  `partition` None (no readable file) refuses every seat."""
    problems: list[str] = []
    where_file = partition_file or "the partition"
    if env_instance and env_instance != cfg.instance:
        problems.append(
            "this listener runs as the %r instance (%s) but its config says daemon.instance = %r"
            % (env_instance, ENV_INSTANCE, cfg.instance)
        )
    assigned_all = dict(partition or {})
    for seat, declared in sorted(cfg.seat_instances.items()):
        if declared and declared != cfg.instance:
            problems.append(
                "seat %s is marked instance = %r, but this is the %r listener; a seat must never be "
                "configured in both instances" % (seat, declared, cfg.instance)
            )
            continue
        assigned = assigned_all.get(seat)
        if assigned is None:
            problems.append(
                "seat %s is configured here (the %r listener), but %s does not list it; a seat that "
                "is not listed is held by no instance (assign it in a PR, with the owner's OK)"
                % (seat, cfg.instance, where_file)
            )
        elif assigned != cfg.instance:
            where = (
                "no listener instance holds it"
                if assigned == "none"
                else "it belongs to the %r instance" % assigned
            )
            problems.append(
                "seat %s is configured here (the %r listener), but %s per %s; a seat must never be "
                "configured in both instances" % (seat, cfg.instance, where, where_file)
            )
    known = set(assigned_all) | set(cfg.seat_instances)
    for seat_cfg in sorted(
        list(cfg.seats.values()) + list(cfg.disabled.values()), key=lambda c: c.seat
    ):
        for what, other in credential_aliases(seat_cfg, known):
            problems.append(
                "seat %s reads %s, which is the credential of seat %s; a seat holds only its own bot, "
                "so one bot is never held twice" % (seat_cfg.seat, what, other)
            )
    return problems
