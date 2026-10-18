"""What pr-sweep needs from fleet-apps.json: the fleet repos, the owners to search, and the running seat.

The registry is read with fleet_lanes.layout (the same parser `lane` uses), so a seat alias such as GROK-BUILD
resolves exactly as it does for lanes.  FLEET_APPS_JSON overrides the file, as everywhere else.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from fleet_lanes import layout as L

DEFAULT_OWNERS = ("Simple-With-Us", "jaywedgeworth22")
DEFAULT_CHAINS = {"Socratic-Trade": 3, "BotFleet": 2, "AI-Fleet-Coordinator": 2}


class FleetError(ValueError):
    """The registry or the seat cannot be used as asked (exit 64)."""


@dataclass(frozen=True)
class SeatInfo:
    tag: str  # CLAUDE
    folder: str  # claude
    branch_prefix: str  # claude


@dataclass(frozen=True)
class Fleet:
    owner: str
    repos: tuple[str, ...]  # owner/repo of every registered app
    code_dirs: dict[str, str]  # owner/repo -> the folder name under ~/Code
    registry: L.Registry

    def is_fleet(self, full: str) -> bool:
        return full.lower() in {r.lower() for r in self.repos}

    def code_dir(self, full: str) -> str:
        """The repo's folder under ~/Code (what `lane new` takes), else the repo name."""
        for key, value in self.code_dirs.items():
            if key.lower() == full.lower():
                return value
        return full.split("/", 1)[-1]


def load(env: Mapping[str, str] | None = None) -> Fleet:
    env = os.environ if env is None else env
    try:
        registry = L.load_registry(env=env)
    except (OSError, ValueError) as exc:
        raise FleetError(f"cannot read fleet-apps.json: {exc}")
    owner = registry.owner or DEFAULT_OWNERS[0]
    repos: list[str] = []
    dirs: dict[str, str] = {}
    for app in registry.registered_apps:
        full = app.owner_repo or f"{owner}/{app.name}"
        if full not in repos:
            repos.append(full)
            dirs[full] = app.integration_dir_name or app.lane_dir
    return Fleet(owner, tuple(repos), dirs, registry)


def owners(fleet: Fleet, extra: Sequence[str] = ()) -> list[str]:
    out: list[str] = []
    for o in (fleet.owner, *DEFAULT_OWNERS, *extra):
        if o and o.lower() not in {x.lower() for x in out}:
            out.append(o)
    return out


def seat(
    fleet: Fleet, env: Mapping[str, str] | None = None, explicit: str | None = None
) -> SeatInfo:
    """The seat the sweep runs as: --seat when given, else AGENT_SEAT.  Never guessed; a retired seat or one with
    no branch prefix is refused, because its workers could not name a branch."""
    env = os.environ if env is None else env
    raw = (explicit or env.get("AGENT_SEAT") or "").strip()
    live = ", ".join(s.name for s in fleet.registry.seats if not s.retired)
    if not raw:
        raise FleetError(
            f"AGENT_SEAT is not set.  Set it to your seat tag ({live}); pr-sweep never guesses a seat, "
            "and every worker it launches inherits that seat."
        )
    found = fleet.registry.seat_by_name(raw)
    if found is None:
        raise FleetError(f"{raw[:40]!r} is not a seat tag in fleet-apps.json ({live}).")
    if found.retired:
        raise FleetError(f"seat {found.name} is retired in fleet-apps.json.")
    prefix = found.primary_branch_prefix()
    if not prefix:
        raise FleetError(
            f"seat {found.name} has no branch prefix in fleet-apps.json, so its workers cannot name a "
            "branch."
        )
    return SeatInfo(found.name, found.suffix, prefix)


def repo_root() -> Path:
    """The AI-Fleet-Coordinator checkout this package lives in."""
    return Path(__file__).resolve().parents[2]
