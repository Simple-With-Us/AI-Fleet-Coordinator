#!/usr/bin/env python3
"""Soft memory controller for coding workloads on the Fleet VPS."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

POLICY_CMD = ["get-verified", "CODING_RAM_POLICY", "--path", "/coding-memory"]
POLICY_FIELDS = {
    "schema_version",
    "budget_bytes",
    "critical_budget_bytes",
    "reserve_bytes",
    "pressure_available_bytes",
    "critical_available_bytes",
    "recovery_available_bytes",
    "pressure_some_avg10",
    "critical_full_avg10",
    "recovery_some_avg10",
    "recovery_seconds",
    "tick_seconds",
    "refresh_seconds",
    "discovery_seconds",
    "coding_units",
    "coding_containers",
    "production_container_prefixes",
    "production_name_prefixes",
}
CGROOT = Path("/sys/fs/cgroup")
STATE_DIR = Path("/var/lib/fleet-coding-memory")
EXCLUDE_FILE = Path("/etc/fleet-coding-memory/exclude-units")
MIB = 1024 * 1024
UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]*$")


def load_excluded_units(
    path: Path = EXCLUDE_FILE, *, expected_uid: int = 0, warn=None
) -> set[str]:
    """Load the host-local exclude list; untrusted files are ignored entirely."""
    warn = warn or (lambda msg: print(msg, file=sys.stderr))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return set()
    except OSError:
        warn(f"fleet-coding-memory: ignoring untrusted exclude file {path}")
        return set()
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != expected_uid or info.st_mode & 0o022:
            warn(f"fleet-coding-memory: ignoring untrusted exclude file {path}")
            return set()
        with os.fdopen(fd, "r") as f:
            fd = -1  # fdopen owns it now
            units = set()
            for line in f:
                name = line.strip()
                if not name or name.startswith("#"):
                    continue
                if UNIT_NAME_RE.match(name):
                    units.add(name)
            return units
    except OSError:
        warn(f"fleet-coding-memory: ignoring untrusted exclude file {path}")
        return set()
    finally:
        if fd >= 0:
            os.close(fd)


def validate_policy(raw: str) -> dict[str, Any]:
    obj = json.loads(raw)
    if not isinstance(obj, dict) or set(obj) != POLICY_FIELDS or obj.get("schema_version") != 1:
        raise ValueError("invalid policy schema")
    ints = POLICY_FIELDS - {
        "schema_version",
        "pressure_some_avg10",
        "critical_full_avg10",
        "recovery_some_avg10",
        "coding_units",
        "coding_containers",
        "production_container_prefixes",
        "production_name_prefixes",
    }
    for k in ints:
        if type(obj[k]) is not int or obj[k] <= 0:
            raise ValueError(f"invalid {k}")
    for k in ("pressure_some_avg10", "critical_full_avg10", "recovery_some_avg10"):
        if type(obj[k]) not in (int, float) or not math.isfinite(obj[k]) or obj[k] < 0:
            raise ValueError(f"invalid {k}")
    for k in (
        "coding_units",
        "coding_containers",
        "production_container_prefixes",
        "production_name_prefixes",
    ):
        if (
            not isinstance(obj[k], list)
            or not obj[k]
            or any(not isinstance(x, str) or not x or "/" in x for x in obj[k])
        ):
            raise ValueError(f"invalid {k}")
    if (
        obj["critical_budget_bytes"] > obj["budget_bytes"]
        or obj["recovery_available_bytes"] < obj["pressure_available_bytes"]
    ):
        raise ValueError("inconsistent policy thresholds")
    return obj


def atomic_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f, sort_keys=True, separators=(",", ":"))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def fetch_policy(timeout: int = 20) -> dict[str, Any]:
    token = read_systemd_token(os.environ)
    cli = os.environ.get("FLEET_SECRETS_CLI", "")
    project = os.environ.get("INFISICAL_FLEET_OPS_PROJECT_ID", "")
    if (
        not cli
        or not Path(cli).is_absolute()
        or not Path(cli).is_file()
        or not os.access(cli, os.X_OK)
        or not project
    ):
        raise RuntimeError("scoped settings bridge metadata unavailable")
    env = {
        key: value for key, value in os.environ.items() if not key.lower().startswith("infisical_")
    }
    env["INFISICAL_FLEET_OPS_PROJECT_ID"] = project
    env["INFISICAL_ENVIRONMENT"] = "prod"
    env["INFISICAL_ENV"] = "prod"
    env["INFISICAL_TOKEN"] = token
    p = subprocess.run(
        [cli, *POLICY_CMD], capture_output=True, text=True, timeout=timeout, check=True, env=env
    )
    return validate_policy(p.stdout)


def read_systemd_token(env: dict[str, str], expected_uid: int = 0) -> str:
    credentials_dir = env.get("CREDENTIALS_DIRECTORY")
    if not credentials_dir:
        raise RuntimeError("systemd credential unavailable")
    path = Path(credentials_dir) / "infisical-token"
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise RuntimeError("systemd credential unavailable") from exc
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != expected_uid
            or info.st_mode & 0o077
            or info.st_size < 1
            or info.st_size > 8192
        ):
            raise RuntimeError("systemd credential rejected")
        raw = os.read(fd, 8193)
    finally:
        os.close(fd)
    if len(raw) > 8192:
        raise RuntimeError("systemd credential rejected")
    try:
        token = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("systemd credential rejected") from exc
    if not token or "\x00" in token:
        raise RuntimeError("systemd credential rejected")
    return token


def acquire_lock(state_dir: Path) -> int:
    fd = os.open(state_dir / "controller.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except Exception:
        os.close(fd)
        raise


def load_lkg(state_dir: Path, now: float | None = None) -> tuple[dict[str, Any], bool]:
    now = time.time() if now is None else now
    try:
        policy = fetch_policy()
        atomic_json(state_dir / "policy.json", policy)
        atomic_json(state_dir / "policy-meta.json", {"fetched_at": now, "stale": False})
        return policy, False
    except Exception:
        p = state_dir / "policy.json"
        if not p.exists():
            raise RuntimeError("policy unavailable and no last-known-good cache")
        os.chmod(p, 0o600)
        policy = validate_policy(p.read_text())
        meta = {"fetched_at": None, "stale": True}
        try:
            meta = json.loads((state_dir / "policy-meta.json").read_text()) | {"stale": True}
        except Exception:
            pass
        atomic_json(state_dir / "policy-meta.json", meta)
        return policy, True


def read_int(path: Path) -> int:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return 0


def write_control(path: Path, value: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, value.encode())
    finally:
        os.close(fd)


def psi_avg(path: Path, row: str) -> float:
    try:
        line = next(x for x in path.read_text().splitlines() if x.startswith(row + " "))
        return float(dict(item.split("=", 1) for item in line.split()[1:])["avg10"])
    except (OSError, ValueError, StopIteration, KeyError):
        return 0.0


def mem_available(proc: Path = Path("/proc")) -> tuple[int, int]:
    vals = dict(
        line.split(":", 1) for line in (proc / "meminfo").read_text().splitlines() if ":" in line
    )
    return int(vals["MemAvailable"].split()[0]) * 1024, int(vals["MemTotal"].split()[0]) * 1024


def cg_path(root: Path, rel: str) -> Path | None:
    p = root / rel.lstrip("/")
    try:
        resolved = p.resolve(strict=True)
        if resolved == root.resolve() or root.resolve() not in resolved.parents:
            return None
        if not (resolved / "memory.current").exists():
            return None
        return resolved
    except OSError:
        return None


def resolve_unit_cgroups(
    units, root: Path, runner=subprocess.run
) -> list[Path]:
    found = []
    for unit in units:
        try:
            r = runner(
                ["systemctl", "show", "--value", "-pControlGroup", unit],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            )
            p = cg_path(root, r.stdout.strip())
            if p:
                found.append(p)
        except Exception:
            continue
    return found


def unit_cgroups(
    policy: dict[str, Any], root: Path, runner=subprocess.run, excluded=None
) -> list[Path]:
    excluded = excluded or set()
    return resolve_unit_cgroups(
        [u for u in policy["coding_units"] if u not in excluded], root, runner=runner
    )


def _is_excluded_path(path: Path, excluded_cgroups) -> bool:
    return any(path == ex or ex in path.parents for ex in excluded_cgroups)


def docker_groups(
    policy: dict[str, Any], root: Path, runner=subprocess.run, proc_root: Path = Path("/proc")
) -> tuple[list[Path], list[Path], dict[Path, int]]:
    coding, production = [], []
    production_limits: dict[Path, int] = {}
    names = set(policy["coding_containers"])
    try:
        r = runner(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
        )
        names.update(
            n
            for n in r.stdout.splitlines()
            if n.startswith(tuple(policy["production_container_prefixes"]))
            or n.startswith(tuple(policy["production_name_prefixes"]))
        )
    except Exception:
        pass
    names = sorted(names)
    try:
        inspected = runner(
            [
                "docker",
                "inspect",
                "--format",
                "{{.Name}}|{{.State.Pid}}|{{.HostConfig.Memory}}",
                *names,
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        rows = {}
        for line in inspected.stdout.splitlines():
            try:
                name, pid, raw_limit = line.strip().lstrip("/").split("|", 2)
                rows[name] = (int(pid), int(raw_limit))
            except (ValueError, TypeError):
                continue
    except Exception:
        rows = {}
    for name in names:
        try:
            pid, limit = rows[name]
            if pid <= 0:
                continue
            groups = []
            for line in (proc_root / str(pid) / "cgroup").read_text().splitlines():
                if line.startswith("0::"):
                    groups.append(line.split("::", 1)[1])
            p = cg_path(root, groups[0]) if groups else None
            if not p:
                continue
            if name in policy["coding_containers"]:
                coding.append(p)
            if name.startswith(tuple(policy["production_container_prefixes"])) or name.startswith(
                tuple(policy["production_name_prefixes"])
            ):
                production.append(p)
                if limit > 0:
                    production_limits[p] = limit
        except Exception:
            continue
    return coding, production, production_limits


def dedupe_nested(paths: list[Path]) -> list[Path]:
    unique = sorted(set(paths), key=lambda p: (len(p.parts), str(p)))
    return [p for p in unique if not any(parent in p.parents for parent in unique if parent != p)]


def allocate(
    target: int, paths: list[Path], state: dict[str, Any] | None = None
) -> dict[Path, int]:
    paths = dedupe_nested(paths)
    if not paths:
        return {}
    available: list[Path] = []
    usage: dict[Path, int] = {}
    caps = {}
    owned = (state or {}).get("owned", {})
    for p in paths:
        try:
            inode = p.stat().st_ino
            current = int((p / "memory.current").read_text().strip())
            old_high = (p / "memory.high").read_text().strip()
        except (OSError, ValueError):
            continue
        available.append(p)
        usage[p] = max(0, current)
        rec = owned.get(f"{p}:memory.high", {})
        old = rec.get("baseline") if rec.get("inode") == inode else old_high
        caps[p] = None if old == "max" else int(old)
    paths = available
    if not paths:
        return {}
    floors = {
        p: min(usage[p] + 32 * MIB, 128 * MIB, caps[p] if caps[p] is not None else target)
        for p in paths
    }
    if sum(floors.values()) > target:
        floors = {
            p: min(
                caps[p] if caps[p] is not None else target,
                target * max(1, usage[p]) // sum(max(1, x) for x in usage.values()),
            )
            for p in paths
        }
    alloc = dict(floors)
    remaining = max(0, target - sum(alloc.values()))
    weights = {p: max(MIB, usage[p]) for p in paths}
    while remaining:
        eligible = [p for p in paths if caps[p] is None or alloc[p] < caps[p]]
        if not eligible:
            break
        denom = sum(weights[p] for p in eligible)
        round_remaining = remaining
        shares = {}
        remainders = {}
        for p in eligible:
            room = remaining if caps[p] is None else caps[p] - alloc[p]
            numerator = round_remaining * weights[p]
            share, remainder = divmod(numerator, denom)
            shares[p] = min(room, share)
            remainders[p] = remainder
        assigned = sum(shares.values())
        # Distribute integer residue from the same snapshot, with caps still
        # respected.  The next round redistributes any share blocked by a cap.
        residue = round_remaining - assigned
        for p in sorted(eligible, key=lambda item: remainders[item], reverse=True):
            if residue <= 0:
                break
            if caps[p] is None or alloc[p] + shares[p] < caps[p]:
                shares[p] += 1
                residue -= 1
                assigned += 1
        if not assigned:
            break
        for p, share in shares.items():
            alloc[p] += share
        remaining -= assigned
    return alloc


def write_owned(path: Path, key: str, value: int, state: dict[str, Any], persist=None) -> bool:
    f = path / key
    record_key = f"{path}:{key}"
    previous: dict[str, Any] | None = None
    try:
        inode = path.stat().st_ino
        current = f.read_text().strip()
        if key == "memory.low" and current == "max":
            return True
        rec = state.setdefault("owned", {}).get(record_key)
        previous = rec.copy() if rec and rec.get("inode") == inode else None
        expected = (
            rec.get("last")
            if rec and rec.get("last") is not None
            else rec.get("baseline")
            if rec
            else None
        )
        if rec and rec.get("inode") == inode and current != expected:
            return False
        baseline = rec["baseline"] if rec and rec.get("inode") == inode else current
        if key == "memory.low" and baseline not in ("max",) and baseline.isdigit():
            value = max(value, int(baseline))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        value = max(0, int(value) // page_size * page_size)
        if current == str(value):
            if rec:
                rec["last"] = current
                rec.pop("pending", None)
                rec.pop("observed", None)
            return True
        state["owned"][record_key] = {
            "inode": inode,
            "baseline": baseline,
            "last": previous.get("last") if previous else None,
            "pending": str(value),
            "observed": current,
        }
        if persist:
            persist()
        write_control(f, str(value))
        state["owned"][record_key]["last"] = str(value)
        state["owned"][record_key].pop("pending", None)
        state["owned"][record_key].pop("observed", None)
        if persist:
            persist()
        return True
    except Exception:
        rec = state.get("owned", {}).get(record_key)
        landed = False
        if rec and rec.get("pending") is not None:
            try:
                current = f.read_text().strip()
            except OSError:
                current = None
            desired, observed = rec["pending"], rec["observed"]
            if current == desired:
                rec["last"] = desired
                rec.pop("pending", None)
                rec.pop("observed", None)
                landed = True
            elif current == observed:
                if previous is None:
                    state["owned"].pop(record_key, None)
                else:
                    state["owned"][record_key] = previous
            else:
                rec.pop("pending", None)
                rec.pop("observed", None)
            if persist:
                try:
                    persist()
                except Exception:
                    pass
        elif rec and rec.get("last") is not None:
            try:
                landed = f.read_text().strip() == rec["last"]
            except OSError:
                pass
        return landed


def reconcile_owned(state: dict[str, Any], persist=None) -> None:
    """Drop stale records for vanished cgroups and writes that never took effect."""
    changed = False
    for name, rec in list(state.get("owned", {}).items()):
        try:
            pth, key = name.rsplit(":", 1)
            p = Path(pth)
            current = (p / key).read_text().strip()
            if p.stat().st_ino != rec["inode"]:
                del state["owned"][name]
                changed = True
                continue
            if rec.get("pending") is not None:
                pending, observed = rec["pending"], rec["observed"]
                if current == pending:
                    rec["last"] = pending
                    rec.pop("pending", None)
                    rec.pop("observed", None)
                elif current == observed:
                    if rec.get("last") is None:
                        del state["owned"][name]
                    else:
                        rec.pop("pending", None)
                        rec.pop("observed", None)
                else:
                    rec.pop("pending", None)
                    rec.pop("observed", None)
                changed = True
            elif current == rec["baseline"] and current != rec.get("last"):
                del state["owned"][name]
                changed = True
        except OSError:
            del state["owned"][name]
            changed = True
    if changed and persist:
        persist()


def restore_owned(state: dict[str, Any], persist=None) -> None:
    reconcile_owned(state, persist)
    for name, rec in list(state.get("owned", {}).items()):
        try:
            pth, key = name.rsplit(":", 1)
            p = Path(pth)
            if p.stat().st_ino != rec["inode"]:
                continue
            f = p / key
            current = f.read_text().strip()
            if current == rec["last"] and current != rec["baseline"]:
                write_control(f, str(rec["baseline"]))
                del state["owned"][name]
                if persist:
                    persist()
            elif current == rec["baseline"]:
                del state["owned"][name]
                if persist:
                    persist()
        except (OSError, ValueError):
            continue


def restore_excluded(
    state: dict[str, Any], excluded_cgroups, persist=None
) -> None:
    """Hand back owned controls on now-excluded cgroups (external edit wins)."""
    if not excluded_cgroups:
        return
    excluded = set(excluded_cgroups)
    for name, rec in list(state.get("owned", {}).items()):
        try:
            pth, key = name.rsplit(":", 1)
            p = Path(pth)
            if p not in excluded:
                continue
            if p.stat().st_ino != rec["inode"]:
                del state["owned"][name]
                if persist:
                    persist()
                continue
            f = p / key
            current = f.read_text().strip()
            if current == rec["last"] and current != rec["baseline"]:
                write_control(f, str(rec["baseline"]))
                del state["owned"][name]
                if persist:
                    persist()
            elif current != rec.get("last"):
                # External edit wins: drop ownership without writing.
                del state["owned"][name]
                if persist:
                    persist()
            elif current == rec["baseline"]:
                del state["owned"][name]
                if persist:
                    persist()
        except (OSError, ValueError, KeyError):
            continue


def oom_kill_delta(paths: list[Path], state: dict[str, Any]) -> int:
    previous = state.get("oom_counters", {})
    current: dict[str, dict[str, int]] = {}
    delta = 0
    for path in dedupe_nested(paths):
        try:
            inode = path.stat().st_ino
            events = dict(
                line.split() for line in (path / "memory.events").read_text().splitlines()
            )
            count = int(events.get("oom_kill", "0"))
        except (OSError, ValueError):
            continue
        key = str(path)
        old = previous.get(key)
        if old and old.get("inode") == inode:
            delta += max(0, count - old.get("oom_kill", count))
        current[key] = {"inode": inode, "oom_kill": count}
    state["oom_counters"] = current
    return delta


def system_slice(root: Path) -> Path | None:
    return cg_path(root, "/system.slice")


def protected_lows(
    paths: list[Path],
    root: Path,
    total: int,
    reserve: int,
    budget: int,
    state: dict[str, Any],
    limits: dict[Path, int] | None = None,
    persist=None,
) -> int:
    # The supported common ancestor is system.slice; set child protection and a sufficient parent protection.
    limit = max(0, total - reserve - budget)
    candidates = dedupe_nested(paths)
    parent = system_slice(root)
    if not parent:
        return 0
    candidates = [p for p in candidates if parent in p.parents]
    if not candidates:
        return 0
    limits = limits or {}
    sizes = {p: max(0, limits.get(p, read_int(p / "memory.max"))) for p in candidates}
    sizes = {p: n for p, n in sizes.items() if n > 0}
    total_caps = sum(sizes.values())
    scale = min(1.0, limit / total_caps) if total_caps else 0
    wanted = {p: int(n * scale) for p, n in sizes.items()}
    failures = int(not write_owned(parent, "memory.low", sum(wanted.values()), state, persist))
    failures += sum(not write_owned(p, "memory.low", n, state, persist) for p, n in wanted.items())
    return failures


def reset_transient_state(state: dict[str, Any]) -> None:
    """Do not carry pressure mode or monotonic recovery timers across restarts/stops."""
    state["mode"] = "normal"
    state.pop("recovery_since", None)


def production_protection_counts(
    paths: list[Path], root: Path, limits: dict[Path, int] | None = None
) -> tuple[int, int]:
    parent = system_slice(root)
    candidates = dedupe_nested(paths)
    limits = limits or {}
    eligible = [p for p in candidates if parent and parent in p.parents]
    protected = sum(1 for p in eligible if max(0, limits.get(p, read_int(p / "memory.max"))) > 0)
    return protected, len(candidates) - protected


def step(
    policy: dict[str, Any],
    root: Path,
    state: dict[str, Any],
    proc: Path = Path("/proc"),
    now: float | None = None,
    discovered: tuple[Any, ...] | None = None,
    persist=None,
    excluded_units=None,
    excluded_cgroups=None,
) -> dict[str, Any]:
    now = time.monotonic() if now is None else now
    avail, total = mem_available(proc)
    some = psi_avg(proc / "pressure" / "memory", "some")
    full = psi_avg(proc / "pressure" / "memory", "full")
    excluded_units = set(excluded_units or set())
    excluded_cgroups = set(excluded_cgroups or set())
    if discovered is None:
        containers, prod, production_limits = docker_groups(policy, root)
        code = unit_cgroups(policy, root, excluded=excluded_units) + containers
        if excluded_units and not excluded_cgroups:
            excluded_cgroups = set(resolve_unit_cgroups(sorted(excluded_units), root))
    else:
        code, prod = discovered[:2]
        production_limits = discovered[2] if len(discovered) > 2 else {}
    if excluded_cgroups:
        restore_excluded(state, excluded_cgroups, persist)
        code = [p for p in code if not _is_excluded_path(p, excluded_cgroups)]
    code = dedupe_nested(code)
    mode = state.get("mode", "normal")
    if not isinstance(mode, str) or mode not in ("normal", "pressure", "critical"):
        mode = "normal"
        state["mode"] = mode
    psi_active = avail < policy["reserve_bytes"]
    if avail < policy["critical_available_bytes"] or (
        psi_active and full >= policy["critical_full_avg10"]
    ):
        mode = "critical"
    elif avail < policy["pressure_available_bytes"] or (
        psi_active and some >= policy["pressure_some_avg10"]
    ):
        mode = "pressure"
    elif avail >= policy["recovery_available_bytes"] and (
        not psi_active or some <= policy["recovery_some_avg10"]
    ):
        since = state.get("recovery_since")
        if type(since) not in (int, float) or not math.isfinite(since) or since < 0 or since > now:
            since = now
            state["recovery_since"] = now
        if now - since >= policy["recovery_seconds"]:
            mode = "normal"
    else:
        state.pop("recovery_since", None)
    if (
        avail < policy["pressure_available_bytes"]
        or (psi_active and some >= policy["pressure_some_avg10"])
        or mode == "normal"
    ):
        state.pop("recovery_since", None)
    budget = policy["critical_budget_bytes"] if mode == "critical" else policy["budget_bytes"]
    target = (
        0
        if not code
        else (
            min(
                total,
                max(
                    budget,
                    sum(read_int(p / "memory.current") for p in code)
                    + avail
                    - policy["reserve_bytes"],
                ),
            )
            if mode == "normal"
            else min(total, budget)
        )
    )
    allocations = allocate(target, code, state)
    write_failures = sum(
        not write_owned(p, "memory.high", n, state, persist) for p, n in allocations.items()
    )
    write_failures += protected_lows(
        prod,
        root,
        total,
        policy["reserve_bytes"],
        policy["budget_bytes"],
        state,
        production_limits,
        persist,
    )
    oom_delta = oom_kill_delta(code + prod, state)
    production_protected, production_unprotected = production_protection_counts(
        prod, root, production_limits
    )
    state["mode"] = mode
    state["last_status"] = {
        "mode": mode,
        "policy_stale": bool(state.get("policy_stale", False)),
        "available_bytes": avail,
        "total_bytes": total,
        "coding_current_bytes": sum(read_int(p / "memory.current") for p in code),
        "coding_target_bytes": target,
        "coding_groups": len(code),
        "production_groups": len(dedupe_nested(prod)),
        "production_protected_groups": production_protected,
        "production_unprotected_groups": production_unprotected,
        "oom_kill_delta": oom_delta,
        "control_write_failures": write_failures,
        "excluded_units": len(excluded_units),
    }
    return state["last_status"]


def refresh_worker(
    state_dir: Path,
    current: list[Any],
    wake: threading.Event,
    stop: threading.Event,
    exclude_file: Path = EXCLUDE_FILE,
) -> None:
    """Refresh one immutable policy snapshot off the controller's time-sensitive loop."""
    while not stop.is_set():
        policy = current[0]
        if wake.wait(policy["refresh_seconds"]):
            wake.clear()
        if stop.is_set():
            return
        # Reload the exclude file on the same cadence; this is the only
        # place (besides startup) it is read, so an untrusted-file warning
        # fires once per refresh, not per tick.
        if len(current) > 2:
            current[2] = load_excluded_units(exclude_file)
        try:
            fresh = fetch_policy()
            atomic_json(state_dir / "policy.json", fresh)
            atomic_json(state_dir / "policy-meta.json", {"fetched_at": time.time(), "stale": False})
            current[0] = fresh
            current[1] = False
        except Exception:
            current[1] = True
            try:
                try:
                    meta = json.loads((state_dir / "policy-meta.json").read_text())
                except Exception:
                    meta = {"fetched_at": None}
                meta["stale"] = True
                atomic_json(state_dir / "policy-meta.json", meta)
            except OSError:
                pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--state-dir", type=Path, default=STATE_DIR)
    ap.add_argument("--cgroot", type=Path, default=CGROOT)
    ap.add_argument("--exclude-file", type=Path, default=EXCLUDE_FILE)
    args = ap.parse_args()
    state_path = args.state_dir / "state.json"
    if args.status:
        try:
            state = json.loads(state_path.read_text())
            print(
                json.dumps(
                    state.get(
                        "last_status",
                        {"mode": "unknown", "coding_groups": 0, "production_groups": 0},
                    )
                )
            )
            return 0
        except Exception:
            print(json.dumps({"mode": "unknown", "coding_groups": 0, "production_groups": 0}))
            return 1
    args.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(args.state_dir, 0o700)
    try:
        lock_fd = acquire_lock(args.state_dir)
    except BlockingIOError:
        print("controller already running", file=sys.stderr)
        return 2
    if state_path.exists():
        os.chmod(state_path, 0o600)
    try:
        state = json.loads(state_path.read_text())
    except FileNotFoundError:
        state = {}
    except Exception:
        print("state cache unreadable; refusing cgroup changes", file=sys.stderr)
        return 1
    if not isinstance(state, dict) or not isinstance(state.get("owned", {}), dict):
        print("state cache invalid; refusing cgroup changes", file=sys.stderr)
        return 1
    for name, rec in state.get("owned", {}).items():
        if (
            not isinstance(name, str)
            or not isinstance(rec, dict)
            or not name.endswith((":memory.high", ":memory.low"))
            or type(rec.get("inode")) is not int
            or not isinstance(rec.get("baseline"), str)
            or rec.get("last") is not None
            and not isinstance(rec.get("last"), str)
            or rec.get("pending") is not None
            and (
                not isinstance(rec.get("pending"), str) or not isinstance(rec.get("observed"), str)
            )
        ):
            print("state ownership record invalid; refusing cgroup changes", file=sys.stderr)
            return 1
    reconcile_owned(state, lambda: atomic_json(state_path, state))
    reset_transient_state(state)
    policy, stale = load_lkg(args.state_dir)
    live: list[Any] = [policy, stale, load_excluded_units(args.exclude_file)]
    quit_flag = False
    refresh = threading.Event()
    stop_refresh = threading.Event()

    def onterm(_s, _f):
        nonlocal quit_flag
        quit_flag = True

    def onsighup(_s, _f):
        refresh.set()

    signal.signal(signal.SIGTERM, onterm)
    signal.signal(signal.SIGINT, onterm)
    signal.signal(signal.SIGHUP, onsighup)
    worker = threading.Thread(
        target=refresh_worker,
        args=(args.state_dir, live, refresh, stop_refresh, args.exclude_file),
        daemon=True,
    )
    worker.start()
    last_discovery = 0.0
    groups: tuple[list[Path], list[Path], dict[Path, int]] = ([], [], {})
    excluded_cgroups: set[Path] = set()
    seen_excluded: set[str] | None = None
    discovered_once = False
    try:
        while True:
            if quit_flag:
                break
            policy, stale = live[0], live[1]
            excluded_units = set(live[2]) if len(live) > 2 else set()
            if excluded_units != seen_excluded:
                discovered_once = False
                seen_excluded = set(excluded_units)
            state["policy_stale"] = stale
            try:
                if (
                    not discovered_once
                    or time.monotonic() - last_discovery >= policy["discovery_seconds"]
                ):
                    containers, production, limits = docker_groups(policy, args.cgroot)
                    groups = (
                        unit_cgroups(policy, args.cgroot, excluded=excluded_units)
                        + containers,
                        production,
                        limits,
                    )
                    excluded_cgroups = set(
                        resolve_unit_cgroups(sorted(excluded_units), args.cgroot)
                    )
                    last_discovery = time.monotonic()
                    discovered_once = True
                report = step(
                    policy,
                    args.cgroot,
                    state,
                    discovered=groups,
                    persist=lambda: atomic_json(state_path, state),
                    excluded_units=excluded_units,
                    excluded_cgroups=excluded_cgroups,
                )
                state["updated_at"] = int(time.time())
                atomic_json(state_path, state)
                if args.once:
                    print(json.dumps(report))
                    break
            except Exception as e:
                print(f"controller tick failed: {type(e).__name__}", file=sys.stderr)
                if args.once:
                    return 1
            if quit_flag:
                break
            time.sleep(policy["tick_seconds"])
    finally:
        stop_refresh.set()
        refresh.set()
        worker.join(timeout=1)
        if quit_flag:
            restore_owned(state, lambda: atomic_json(state_path, state))
            reset_transient_state(state)
            atomic_json(state_path, state)
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
