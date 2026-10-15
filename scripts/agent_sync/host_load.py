"""Host load signals for the Mac listener wake path.

Peer wakes during a fleet rollout can otherwise start a muse or claude child on
every seat at once and multiply an already-hot load average.  BotFleet already
defers webhook dispatch on the same load-per-core ceiling; this module gives
agent-sync the same read-only signal without importing BotFleet.
"""
from __future__ import annotations

import os
from typing import NamedTuple

# Same saturated-host ceiling as BotFleet's webhook dispatch and ACP init scaling.
DEFAULT_LOAD_PER_CORE_MAX = 3.0


class HostLoad(NamedTuple):
    load1: float
    cores: int


def read_host_load() -> HostLoad | None:
    """The 1-minute load average and logical core count, or None when unknown."""
    try:
        load1 = os.getloadavg()[0]
    except (AttributeError, OSError):
        return None
    cores = os.cpu_count() or 0
    if not isinstance(load1, (int, float)) or load1 < 0 or cores <= 0:
        return None
    return HostLoad(float(load1), int(cores))


def load_per_core(load: HostLoad | None) -> float | None:
    if load is None:
        return None
    ratio = load.load1 / load.cores
    return ratio if ratio >= 0 else None


def host_busy(load: HostLoad | None, *, max_per_core: float) -> bool:
    """True when the host is at or above the saturation ceiling."""
    if max_per_core <= 0:
        return False
    ratio = load_per_core(load)
    return ratio is not None and ratio >= max_per_core


def format_load_per_core(value: float) -> str:
    rounded = round(value * 10) / 10
    if not isinstance(rounded, float) or rounded != rounded:  # NaN guard without math import
        return "unknown"
    return str(int(rounded)) if float(int(rounded)) == rounded else f"{rounded:.1f}"


def host_busy_reason(load: HostLoad | None, *, max_per_core: float) -> str | None:
    """A hold reason for the ledger, or None when the host is cool enough."""
    if not host_busy(load, max_per_core=max_per_core):
        return None
    ratio = load_per_core(load)
    load_text = format_load_per_core(ratio) if ratio is not None else "unknown"
    return "Host is busy (load %s per core)" % load_text
