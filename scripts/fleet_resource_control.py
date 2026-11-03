"""Bounded coding-resource policy operations for the owner-key SSH broker.

The full policy stays private; only explicitly projected fields leave this module.
Infisical has no CAS in this transport: the local lock serializes broker writers,
but an external writer can race the last read and PATCH. No uncertain write retries.
"""

from __future__ import annotations

import contextlib
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import time
from pathlib import Path

GIB = 1024**3
KEY = "CODING_RAM_POLICY"
FOLDER = "/coding-memory"
STATUS_PATH = Path("/run/fleet-coding-resources/status.json")
RELOAD = [
    "sudo",
    "-n",
    "systemctl",
    "kill",
    "--kill-whom=main",
    "--signal=HUP",
    "fleet-coding-memory.service",
]
FIELDS = {
    "cpu-cores": ("cpu_cores", 1),
    "cpu-weight": ("cpu_weight", 1),
    "ram-budget-gib": ("budget_bytes", GIB),
    "ram-critical-gib": ("critical_budget_bytes", GIB),
    "ram-reserve-gib": ("reserve_bytes", GIB),
    "borrow-idle": ("allow_borrowing", 1),
}
TARGET_RE = re.compile(
    r"(cpu-cores|cpu-weight|ram-budget-gib|ram-critical-gib|ram-reserve-gib|borrow-idle)\.([0-9]{1,10})\.([0-9a-f]{64})"
)


class Refused(Exception):
    pass


class Failed(Exception):
    pass


def canonical(policy):
    return json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False)


def revision(policy):
    return hashlib.sha256(canonical(policy).encode()).hexdigest()


def private_text(path, uid, maximum=8192):
    """Descriptor-based check, never following a final symlink or reading special files."""
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_size > maximum
        ):
            raise Refused("the resource-policy credential or metadata is not a private owned file")
        raw = os.read(fd, maximum + 1)
        if len(raw) > maximum:
            raise Refused("the resource-policy credential or metadata is too large")
        return raw.decode("utf-8").strip()
    except (OSError, UnicodeError):
        raise Refused("the resource-policy credential or metadata is unavailable") from None
    finally:
        if fd >= 0:
            os.close(fd)


class InfisicalStore:
    """Only the dedicated folder token is accepted; ambient auth is never consulted."""

    def __init__(self, ctx):
        from fleet_secrets import core

        self.core = core
        self.token = private_text(ctx.home / ".config/fleet-tools/resource-policy-token", ctx.uid)
        if not self.token or any(c.isspace() or ord(c) < 33 for c in self.token):
            raise Refused("the resource-policy credential is malformed")
        project = private_text(ctx.home / ".agent-sync/fleet-ops-project-id", ctx.uid)
        if not re.fullmatch(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", project):
            raise Refused("the resource-policy project metadata is malformed")
        self.project = project
        self.env = {"HOME": str(ctx.home), "INFISICAL_FLEET_OPS_PROJECT_ID": project}

    def get(self):
        try:
            return json.loads(self.core.fetch_verified(KEY, FOLDER, self.env, token=self.token))
        except Exception:
            raise Failed("the resource policy could not be read from its verified source") from None

    def save(self, policy):
        body = {
            "workspaceId": self.project,
            "environment": "prod",
            "secretPath": FOLDER,
            "secretValue": canonical(policy),
            "type": "shared",
        }
        try:
            status, _ = self.core._api_json(
                "PATCH", "/v3/secrets/raw/" + KEY, self.token, body, self.env, max_bytes=65536
            )
        except Exception:
            raise Failed("the policy write outcome is uncertain; it was not retried") from None
        finally:
            body.clear()
        if status not in (200, 201):
            raise Failed("the policy write was not confirmed; it was not retried")


def parse_target(target):
    match = TARGET_RE.fullmatch(target or "")
    if not match:
        raise Refused("the resource-policy target is invalid")
    field, raw, rev = match.groups()
    value = int(raw)
    if (
        raw != str(value)
        or (field == "borrow-idle" and raw not in ("0", "1"))
        or (field != "borrow-idle" and value <= 0)
    ):
        raise Refused("the resource-policy value is not canonical")
    return field, value, rev


def host_bounds():
    try:
        spec = Path("/sys/devices/system/cpu/online").read_text().strip()
        online = set()
        for segment in spec.split(","):
            ends = segment.split("-")
            start, end = int(ends[0]), int(ends[-1])
            if not 0 <= start <= end <= 1048576:
                raise ValueError()
            online.update(range(start, end + 1))
        total = next(
            int(line.split()[1]) * 1024
            for line in Path("/proc/meminfo").read_text().splitlines()
            if line.startswith("MemTotal:")
        )
        return len(online), total
    except (OSError, ValueError, StopIteration):
        raise Refused("host resource bounds are unavailable") from None


def validate(policy, bounds):
    """Reuse the controller schema, with the broker's additional host bounds."""
    from hetzner.fleet_coding_memory import validate_policy

    try:
        validate_policy(canonical(policy))
        optional = {
            k: policy[k] for k in ("cpu_cores", "cpu_weight", "allow_borrowing") if k in policy
        }
        online, total = bounds
        if "cpu_cores" in optional and (
            type(optional["cpu_cores"]) is not int or not 1 <= optional["cpu_cores"] <= online - 2
        ):
            raise ValueError()
        if "cpu_weight" in optional and (
            type(optional["cpu_weight"]) is not int or not 1 <= optional["cpu_weight"] <= 10000
        ):
            raise ValueError()
        if "allow_borrowing" in optional and type(optional["allow_borrowing"]) is not bool:
            raise ValueError()
        if (
            policy["reserve_bytes"] < 2 * GIB
            or policy["budget_bytes"] + policy["reserve_bytes"] > total
        ):
            raise ValueError()
        if (
            not policy["critical_available_bytes"]
            <= policy["pressure_available_bytes"]
            <= policy["recovery_available_bytes"]
            <= total
        ):
            raise ValueError()
    except (ValueError, TypeError, KeyError, OverflowError):
        raise Refused("the complete resource policy fails schema or host safety bounds") from None


def desired(policy):
    result = {
        "ramPressureGiB": policy["budget_bytes"] / GIB,
        "ramCriticalGiB": policy["critical_budget_bytes"] / GIB,
        "reserveGiB": policy["reserve_bytes"] / GIB,
        "allowBorrowing": policy.get("allow_borrowing", True),
    }
    for source, dest in (("cpu_cores", "cpuCores"), ("cpu_weight", "cpuWeight")):
        if source in policy:
            result[dest] = policy[source]
    return result


def _stamp(epoch):
    return dt.datetime.fromtimestamp(epoch, dt.UTC).isoformat().replace("+00:00", "Z")


def _fresh(value, now):
    try:
        timestamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return timestamp.tzinfo is not None and -5 <= now - timestamp.timestamp() <= 60
    except (ValueError, TypeError, AttributeError):
        return False


def _safe_stamp(value):
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return _stamp(stamp.timestamp()) if stamp.tzinfo else None
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None


def read_snapshot():
    fd = -1
    try:
        fd = os.open(STATUS_PATH, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_mode & 0o022
            or info.st_size > 65536
        ):
            return {}
        return json.loads(os.read(fd, 65537))
    except (OSError, ValueError):
        return {}
    finally:
        if fd >= 0:
            os.close(fd)


def public_snapshot(raw, now, policy=None):
    """Project individual safe values and recompute health; never pass through a dict."""
    result = {
        "schemaVersion": 1,
        "generatedAt": _stamp(now),
        "observed": {},
        "status": "unavailable",
    }
    if not isinstance(raw, dict):
        raw = {}
    source_time = _safe_stamp(raw.get("generatedAt"))
    if source_time:
        result["generatedAt"] = source_time
    rev = raw.get("policyRevision")
    if isinstance(rev, str) and re.fullmatch(r"[0-9a-f]{64}", rev):
        result["policyRevision"] = rev
    d = raw.get("desired", {})
    if isinstance(d, dict):
        safe = {}
        for key in ("cpuCores", "cpuWeight"):
            if type(d.get(key)) is int and 1 <= d[key] <= (
                10000 if key == "cpuWeight" else 1048576
            ):
                safe[key] = d[key]
        for key in ("ramPressureGiB", "ramCriticalGiB", "reserveGiB"):
            if type(d.get(key)) in (int, float) and 0 < d[key] <= (2**63 - 1) / GIB:
                safe[key] = d[key]
        if type(d.get("allowBorrowing")) is bool:
            safe["allowBorrowing"] = d["allowBorrowing"]
        if safe:
            result["desired"] = safe
    o = raw.get("observed", {})
    if isinstance(o, dict):
        observed = result["observed"]
        for key in (
            "cpuOnline",
            "cpuCores",
            "cpuWeight",
            "cpuGroups",
            "cpuMismatches",
            "ramTotalBytes",
            "ramCurrentBytes",
            "ramTargetBytes",
            "codingGroups",
            "productionGroups",
            "controlWriteFailures",
            "actualHighMismatches",
        ):
            if type(o.get(key)) is int and 0 <= o[key] <= 2**63 - 1:
                observed[key] = o[key]
        if type(o.get("controlSuspended")) is int and 0 <= o["controlSuspended"] <= 2**63 - 1:
            observed["controlSuspended"] = o["controlSuspended"]
        for key in ("policyStale",):
            if type(o.get(key)) is bool:
                observed[key] = o[key]
        if o.get("mode") in ("normal", "pressure", "critical"):
            observed["mode"] = o["mode"]
        if _fresh(o.get("updatedAt"), now):
            observed["updatedAt"] = o["updatedAt"]
        failures = (
            "cpuMismatches",
            "controlWriteFailures",
            "actualHighMismatches",
            "controlSuspended",
            "policyStale",
        )
        required = (
            *failures,
            "updatedAt",
            "cpuOnline",
            "cpuGroups",
            "codingGroups",
            "productionGroups",
            "ramTotalBytes",
            "ramCurrentBytes",
            "ramTargetBytes",
            "mode",
        )
        desired_required = ("ramPressureGiB", "ramCriticalGiB", "reserveGiB", "allowBorrowing")
        d = result.get("desired", {})
        complete = all(k in observed for k in required) and all(k in d for k in desired_required)
        if (
            type(raw.get("schemaVersion")) is int
            and raw["schemaVersion"] == 1
            and "policyRevision" in result
            and _fresh(source_time, now)
            and complete
        ):
            total = observed["ramTotalBytes"]
            ram_valid = (
                0 < total
                and 0 <= observed["ramCurrentBytes"] <= total
                and 0 < observed["ramTargetBytes"] <= total
                and d["ramCriticalGiB"] <= d["ramPressureGiB"]
                and d["reserveGiB"] >= 2
                and d["ramPressureGiB"] + d["reserveGiB"] <= total / GIB
            )
            counts_present = (
                observed["codingGroups"] > 0
                and observed["cpuGroups"] > 0
                and 3 <= observed["cpuOnline"] <= 1048576
            )
            if ram_valid and counts_present and raw.get("status") in ("applied", "drift"):
                result["status"] = (
                    "applied"
                    if raw["status"] == "applied" and not any(observed[k] for k in failures)
                    else "drift"
                )
    if policy is not None:
        expected = revision(policy)
        result["desired"] = desired(policy)
        result["desiredRevision"] = expected
        if result.get("policyRevision") != expected and result["status"] == "applied":
            result["status"] = "drift"
    if result["status"] == "applied":
        d = result.get("desired", {})
        if (
            any(
                key not in d or result["observed"].get(key) != d[key]
                for key in ("cpuCores", "cpuWeight")
            )
            or d.get("cpuCores", 0) > result["observed"]["cpuOnline"] - 2
        ):
            result["status"] = "drift"
    return result


def sanitize_snapshot(raw, now=None, require_fresh=True):
    """Collector entry point; stale input always stays unavailable, even when retained."""
    return public_snapshot(raw, time.time() if now is None else now)


@contextlib.contextmanager
def broker_lock(ctx):
    directory = ctx.home / ".local/state/fleet-tools"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != ctx.uid or info.st_mode & 0o022:
        raise Refused("the resource-policy lock directory is untrusted")
    fd = os.open(
        directory / "resource-policy.lock",
        os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ctx.uid
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise Refused("the resource-policy lock is untrusted")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("another resource-policy write is in progress") from None
        yield
    finally:
        os.close(fd)


def audit(ctx, event):
    """Intent and outcome identify the forced owner key, never caller-supplied identity."""
    record = {"time": _stamp(ctx.now()), "source": "fleet-ops-ctl-owner-key", **event}
    if hasattr(ctx, "resource_audit"):
        ctx.resource_audit(record)
        return
    directory = ctx.home / ".local/state/fleet-tools"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != ctx.uid or info.st_mode & 0o022:
        raise Failed("the resource-policy audit directory is untrusted")
    fd = os.open(
        directory / "resource-policy-audit.jsonl",
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ctx.uid
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise Failed("the resource-policy audit file is untrusted")
        payload = (canonical(record) + "\n").encode()
        offset = 0
        while offset < len(payload):
            written = os.write(fd, payload[offset:])
            if type(written) is not int or not 0 < written <= len(payload) - offset:
                raise Failed("the resource-policy audit record could not be fully written")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)


def handle(ctx, action, target, mode):
    """Return the existing broker answer shape and exit code (0, 1 failed, 2 refused)."""
    answer = {
        "ok": False,
        "verb": "server " + action,
        "target": target,
        "commands": [],
        "result": "refused",
        "detail": "",
    }
    event = None
    saved = False
    try:
        if not ctx.platform.startswith("linux") or mode not in ("plan", "yes"):
            raise Refused("resource operations require the server and an explicit plan or yes")
        snapshot_reader = getattr(ctx, "resource_snapshot", read_snapshot)
        if action == "resource-status":
            if target != "coding" or mode != "plan":
                raise Refused("resource-status accepts only coding with plan")
            policy = None
            try:
                store = getattr(ctx, "resource_store", None) or InfisicalStore(ctx)
                candidate = store.get()
                validate(candidate, getattr(ctx, "resource_host_bounds", host_bounds)())
                policy = candidate
            except (Refused, Failed):
                pass
            answer.update(
                ok=True,
                result="planned",
                detail="coding resource status",
                resourcePolicy=public_snapshot(snapshot_reader(), ctx.now(), policy),
            )
            return answer, 0
        if action != "resource-policy":
            raise Refused("unknown resource operation")
        field, value, expected = parse_target(target)
        store = getattr(ctx, "resource_store", None) or InfisicalStore(ctx)
        with broker_lock(ctx):
            current = store.get()
            validate(current, getattr(ctx, "resource_host_bounds", host_bounds)())
            if revision(current) != expected:
                raise Refused("the resource policy changed; refresh and approve a new plan")
            key, scale = FIELDS[field]
            patched = copy.deepcopy(current)
            patched[key] = bool(value) if field == "borrow-idle" else value * scale
            validate(patched, getattr(ctx, "resource_host_bounds", host_bounds)())
            before = current.get(key, True if key == "allow_borrowing" else None)
            after = patched[key]
            answer["commands"] = [
                ["resource-policy", field, str(value), canonical(before), expected]
            ]
            answer["resourcePolicy"] = public_snapshot(snapshot_reader(), ctx.now(), current)
            if mode == "plan":
                answer.update(
                    ok=True,
                    result="planned",
                    detail="save one policy field and request controller reload; external writers are not serialized by this broker",
                )
                return answer, 0
            event = {
                "target": target,
                "field": field,
                "before": before,
                "after": after,
                "beforeRevision": expected,
                "afterRevision": revision(patched),
            }
            audit(ctx, {**event, "result": "intent"})
            # Read again immediately before the sole PATCH; no automatic retry or rollback.
            if revision(store.get()) != expected:
                raise Refused("the resource policy changed; refresh and approve a new plan")
            store.save(patched)
            saved = True
            persisted = store.get()
            if revision(persisted) != revision(patched):
                raise Failed("SavedButApplyUnverified: the saved policy readback did not match")
            answer["resourcePolicy"] = public_snapshot(snapshot_reader(), ctx.now(), persisted)
            status, _ = ctx.runner(list(RELOAD), timeout=5, env=ctx.child_env())
            if status:
                raise Failed(
                    "SavedButApplyUnverified: policy saved; controller reload was not confirmed"
                )
            monotonic = getattr(ctx, "resource_monotonic", time.monotonic)
            sleep = getattr(ctx, "resource_sleep", time.sleep)
            deadline = monotonic() + 15
            while True:
                snapshot = public_snapshot(snapshot_reader(), ctx.now(), persisted)
                answer["resourcePolicy"] = snapshot
                if snapshot["status"] == "applied" and snapshot.get("policyRevision") == revision(
                    persisted
                ):
                    break
                if monotonic() >= deadline:
                    raise Failed(
                        "SavedButApplyUnverified: policy saved; applied controller state was not verified"
                    )
                sleep(min(0.25, max(0, deadline - monotonic())))
            audit(ctx, {**event, "result": "done"})
            event = None
            answer.update(
                ok=True, result="done", detail="policy saved and controller application verified"
            )
            return answer, 0
    except Refused as exc:
        answer.update(detail=str(exc))
        code = 2
    except Exception as exc:  # noqa: BLE001 - Sanitize all operational failures at the boundary.
        # Unexpected operational failures still need a sanitized answer and outcome audit.
        detail = (
            str(exc) if isinstance(exc, Failed) else "the resource operation could not be verified"
        )
        if saved and not detail.startswith("SavedButApplyUnverified"):
            detail = "SavedButApplyUnverified: policy saved; subsequent verification failed"
        answer.update(result="failed", detail=detail)
        code = 1
    if event:
        try:
            audit(ctx, {**event, "result": answer["result"]})
        except Exception:  # noqa: BLE001 - Audit failure must return a sanitized refusal.
            answer.update(
                result="failed", detail="the resource-policy outcome audit could not be recorded"
            )
            code = 1
    return answer, code
