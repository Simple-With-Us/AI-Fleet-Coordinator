"""Which bots a stdio MCP connection or a CLI run on this host may act as (AFC-331,
docs/protocols/per-call-identity.md section 3).

The allowed set is every agent bot whose Zulip credential file is on this host:

    seats        every live row of fleet-apps.json `seats` and every `seatApps.extraSeats` key
    BF-*         every <secrets dir>/BF-<Role>-zuliprc whose name matches the BF-* pattern
    GB-*         every persona enabled in mcp/gb-identities.json, the one list the hosted Worker
                 imports as GB_IDENTITIES (GB-ENGINEER stays out until it is enabled there)

each kept only when its credentials are on this host:  its credential file (<secrets dir>/<FileCode>-zuliprc),
or, on the server instance, its own environment variables (the listener's `creds = "env"` seats in
listener.toml, else the default ZULIP_<CODE>_EMAIL and ZULIP_<CODE>_API_KEY with ZULIP_SITE, which
Infisical syncs into the container;  docs/protocols/agent-sync-server.md).  A file wins over the
environment.  BF-* bots join from either source.  Only variable names are read here, never logged
values;  a value is read when the identity is first opened.  The owner's own
account and the integration bots (BetterStack, Datadog, Linear, PagerDuty, Sentry) are never seats,
so they never enter the set, whatever files are on disk;  check_bot() still refuses a key that is
not the claimed identity's own bot when the credential is first used.

A launcher pin (AGENT_LAUNCH_SEAT) narrows the set to the pinned seat alone.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Iterable, Mapping, NamedTuple

from . import identity as I
from . import zulip as Z

HERE = os.path.dirname(os.path.abspath(__file__))
GB_IDENTITIES_PATH = os.path.join(HERE, "mcp", "gb-identities.json")


def gb_identities() -> tuple[str, ...]:
    """The enabled Grok Bot personas, in table order."""
    with open(GB_IDENTITIES_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    enabled = data.get("enabled") if isinstance(data, dict) else None
    if not isinstance(enabled, list) or not all(isinstance(name, str) and name.startswith("GB-") for name in enabled):
        raise ValueError("%s: `enabled` must be a list of GB-* names" % GB_IDENTITIES_PATH)
    return tuple(enabled)


def agent_identity_problem(name: str, *, registry: I.SeatRegistry | None = None) -> str | None:
    """None when `name` (canonical) is an agent bot identity a connection may ever act as, else why not.
    Reads the registry;  a registry that cannot be read is a problem (fail closed)."""
    try:
        reg = registry if registry is not None else I.load_seat_registry()
        seat = I.strict_seat(name, registry=reg)
    except I.UnknownSeat as exc:
        return "%s is not an agent bot identity (%s)" % (exc.seat[:40], "a retired seat" if exc.retired else
                                                           "not a live seat, extra seat, BF-* or GB-* name")
    except I.SeatRegistryError as exc:
        return "the seat registry cannot be read:  %s" % exc
    if seat.startswith("GB-") and seat not in gb_identities():
        return "%s is not an enabled Grok Bot persona (scripts/agent_sync/mcp/gb-identities.json)" % seat
    return None


DEFAULT_SITE_ENV = "ZULIP_SITE"  # config.DEFAULT_SITE_ENV
_BF_KEY_ENV_RE = re.compile(r"^ZULIP_(BF_[A-Z0-9][A-Z0-9_]*)_API_KEY$")


def env_code(name: str) -> str:
    """GB-COMPILER -> GB_COMPILER, the CODE in ZULIP_<CODE>_EMAIL (config.env_code)."""
    return re.sub(r"[^A-Z0-9]", "_", name.upper())


class CredentialSource(NamedTuple):
    """Where one identity's key is:  a zuliprc file (`path`), or its own environment variables (names only)."""
    path: Path | None = None
    email_env: str | None = None
    key_env: str | None = None
    site_env: str = DEFAULT_SITE_ENV

    @property
    def is_env(self) -> bool:
        return self.path is None

    def describe(self) -> str:
        """For messages:  the file, or the key variable's name.  Never a value."""
        return str(self.path) if self.path is not None else "env %s" % self.key_env

    def present(self, env: Mapping[str, str]) -> bool:
        if self.path is not None:
            return self.path.is_file()
        return all(str(env.get(name) or "").strip().strip('"').strip() for name in (self.email_env, self.key_env,
                                                                                        self.site_env))

    def credentials(self, env: Mapping[str, str], label: str) -> Z.Credentials:
        """Read the key (file or env).  Errors name the file or the variables, never a value."""
        if self.path is not None:
            return Z.read_zuliprc(self.path)
        return Z.env_credentials(env, email_env=str(self.email_env), key_env=str(self.key_env),
                                 site_env=self.site_env, label=label)


def default_env_source(name: str) -> CredentialSource:
    code = env_code(name)
    return CredentialSource(email_env="ZULIP_%s_EMAIL" % code, key_env="ZULIP_%s_API_KEY" % code)


def secrets_dir(env: Mapping[str, str], home: Path) -> Path:
    """Where the credential files are:  the directory default_rc_path() looks in."""
    return Z.default_rc_path("X", env, home).parent


def candidates(env: Mapping[str, str], home: Path, *, registry: I.SeatRegistry | None = None,
               cred_env: Mapping[str, str] | None = None, extra: Iterable[str] = ()) -> list[str]:
    """Every identity name the registry and the GB table allow, before the credential check.  BF-* bots
    come from their credential files and from ZULIP_BF_<ROLE>_API_KEY variables in `cred_env`;  `extra`
    (the listener's env seats) is filtered the same way."""
    reg = registry if registry is not None else I.load_seat_registry(env=env)
    names = set(reg.live) | set(reg.extra) | set(gb_identities())
    directory = secrets_dir(env, home)
    try:
        entries = list(directory.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        seat = Z.seat_from_rc_path(entry)
        if seat and seat.startswith("BF-"):
            names.add(seat)  # agent_identity_problem() below keeps it only when it matches the BF-* pattern
    for variable in (cred_env or {}):
        match = _BF_KEY_ENV_RE.match(str(variable))
        if match:
            names.add(match.group(1).replace("_", "-"))
    names.update(str(name).upper() for name in extra)
    return sorted(name for name in names if agent_identity_problem(name, registry=reg) is None)


def allowed_identities(env: Mapping[str, str], home: Path, *, registry: I.SeatRegistry | None = None,
                       cred_env: Mapping[str, str] | None = None,
                       env_seats: Mapping[str, CredentialSource] | None = None) -> dict[str, CredentialSource]:
    """identity -> where its key is, for every agent bot whose credentials are on this host:  its file
    (looked up with `env`), else its own environment variables in `cred_env` (`env_seats`, the
    listener's configured names, first;  then the default ZULIP_<CODE>_* names).  `cred_env` defaults to
    `env`.  An unreadable registry gives an empty set (the caller then allows its default identity alone)."""
    cred_env = env if cred_env is None else cred_env
    env_seats = dict(env_seats or {})
    try:
        names = candidates(env, home, registry=registry, cred_env=cred_env, extra=env_seats)
    except (I.SeatRegistryError, OSError, ValueError):
        return {}
    found: dict[str, CredentialSource] = {}
    for name in names:
        found_source = source_for(name, env, home, cred_env=cred_env, env_seats=env_seats)
        if found_source is not None:
            found[name] = found_source
    return found


def source_for(name: str, env: Mapping[str, str], home: Path, *, cred_env: Mapping[str, str],
               env_seats: Mapping[str, CredentialSource] | None = None) -> CredentialSource | None:
    """Where `name`'s key is on this host (file first, then its environment variables), or None."""
    path_source = CredentialSource(path=Z.default_rc_path(name, env, home))
    if path_source.present(env):
        return path_source
    for candidate in ((env_seats or {}).get(name), default_env_source(name)):
        if candidate is not None and candidate.present(cred_env):
            return candidate
    return None


def resolve_name(raw: object) -> str | None:
    """The canonical identity a caller's `identity` argument names (aliases resolved, upper-cased), or
    None when it is not a plain seat-shaped token."""
    if not isinstance(raw, str):
        return None
    return I.valid_seat(raw.strip())
