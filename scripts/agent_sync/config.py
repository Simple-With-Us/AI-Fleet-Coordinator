"""listener.toml:  the daemon's local config, read with tomllib, validated with defaults.

It is local because it is machine-specific, holds no secrets and must work offline.  Budgets and
the kill switch live here and in listener/pause.json (owner decision 2026-10-07).  A bad config
never stops the daemon:  `load()` returns the errors, the daemon shows red in status, sends one
notify-owner and keeps running with what it could read (a seat with a bad section gets no queue).

Python 3.11+, standard library only.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping

from . import live as L

KNOWN_WAKES = ("inbox", "claude")
DEFAULT_OWNER_CLIENTS = ["website", "ZulipMobile", "ZulipFlutter", "ZulipElectron", "ZulipDesktop"]
BUDGET_DEFAULTS: dict[str, float] = {
    "wakes_per_hour": 6, "wakes_per_day": 40, "per_topic_per_hour": 2, "owner_per_day": 20,
    "owner_per_topic_per_hour": 6, "usd_per_day": 2.0, "board_per_day": 0,
}
WAKE_MAX_BUDGET_USD = 0.25
_SEAT_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
_BOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class SeatConfig:
    def __init__(self, seat: str, bot: str, wake: str, model: str, budget: dict[str, float],
                 live: dict[str, int], claude: str | None) -> None:
        self.seat = seat
        self.bot = bot
        self.wake = wake
        self.model = model
        self.budget = budget
        self.live = live
        self.claude = claude  # an absolute path to the claude binary, else resolved on the wake PATH

    def __repr__(self) -> str:
        return "SeatConfig(%s, bot=%s, wake=%s)" % (self.seat, self.bot, self.wake)


class Config:
    def __init__(self) -> None:
        self.raw: dict[str, Any] = {}
        self.errors: list[str] = []
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
        self.notify_banners = True

    @property
    def ok(self) -> bool:
        return not self.errors


def _number(value: Any, name: str, errors: list[str], *, minimum: float = 0) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < minimum:
        errors.append("%s must be a number of at least %g" % (name, minimum))
        return None
    return float(value)


def from_dict(raw: Mapping[str, Any]) -> Config:
    cfg = Config()
    cfg.raw = dict(raw)
    errors = cfg.errors
    daemon = raw.get("daemon") or {}
    if not isinstance(daemon, dict):
        errors.append("[daemon] must be a table")
        daemon = {}
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
    if "notify_banners" in daemon:
        cfg.notify_banners = daemon["notify_banners"] is True
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
        bot = section.get("bot")
        if not isinstance(bot, str) or not _BOT_RE.match(bot):
            problems.append("seat.%s.bot must be the credential file code (e.g. Claude for Claude-zuliprc)" % seat_name)
        wake = section.get("wake", "inbox")
        if wake not in KNOWN_WAKES:
            problems.append("seat.%s.wake must be one of %s (v1)" % (seat_name, ", ".join(KNOWN_WAKES)))
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
        if problems:
            errors.extend(problems)
            continue
        cfg.seats[seat_name] = SeatConfig(seat_name, bot, wake, model, budget, L.live_limits(raw, seat_name), claude)
    return cfg


def load(root: str) -> Config:
    raw, error = L.load_config(root)
    cfg = from_dict(raw)
    if error:
        cfg.errors.insert(0, error)
    elif not raw:
        cfg.errors.insert(0, "no %s in %s; run agent-sync daemon install (it writes a sample)" % (L.CONFIG_NAME, root))
    if cfg.owner_user_id == 0 and raw:
        cfg.errors.append("daemon.owner_user_id is 0 (not pinned): nothing is treated as the owner and nothing wakes; "
                          "run agent-sync daemon init")
    return cfg
