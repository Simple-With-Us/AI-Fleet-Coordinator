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
# The names the owner reads on Muster's Server Resource Limits page and its review sheet.
# ResourcePolicyField.title in muster/Muster/Model/ResourcePolicy.swift holds the same words,
# and the contract fixture in muster/MusterTests/Fixtures keeps the two equal.
FIELD_TITLES = {
    "cpu-cores": "CPU Cores",
    "cpu-weight": "CPU Weight",
    "ram-budget-gib": "Pressure RAM Limit",
    "ram-critical-gib": "Critical RAM Limit",
    "ram-reserve-gib": "Host RAM Reserve",
    "borrow-idle": "Borrow Idle Memory",
}
# The controller's own reason codes (coding_resources.public_snapshot) that may pass through.
CONTROLLER_REASONS = frozenset(
    {
        "no-controller-report",
        "policy-stale",
        "no-coding-groups",
        "cpu-controls-unreadable",
        "no-online-cpus",
        "no-production-groups",
        "production-unprotected",
        "cpu-policy-missing",
        "control-write-failures",
        "controls-suspended",
        "cpu-mismatch",
        "memory-limit-mismatch",
    }
)
# A real U+00A0 and a space after each sentence the owner reads.
GAP = "\u00a0 "
REFRESH_ADVICE = "Refresh Server Resource Limits in a minute to see whether it is in effect."


class Refused(Exception):
    pass


class Failed(Exception):
    pass


class WriteUncertain(Failed):
    """The policy write was sent and nothing says whether it landed."""


class SavedUnverified(Failed):
    """The policy write landed, and what was meant to follow it was not confirmed."""


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
            raise WriteUncertain(
                "The server could not confirm whether the new value was saved, "
                "and the write was not retried."
            ) from None
        finally:
            body.clear()
        if status not in (200, 201):
            if not isinstance(status, int) or status in (0, 408) or status >= 500:
                # Status 0 is how the secrets client reports a transport error, a
                # timeout or an oversized body, so the request may have gone out;
                # a 5xx may also have been applied before it failed, and a 408 from a
                # proxy can arrive after the request was passed on.
                raise WriteUncertain(
                    "The server could not confirm whether the new value was saved, "
                    "and the write was not retried."
                )
            if status == 429:
                # Rate limited:  refused before it was applied, so nothing was saved.
                # One click is one write, so the owner retries, never this broker.
                raise Failed(
                    "The secrets service is busy and did not save the new value."
                    + GAP
                    + "Wait a minute and review the change again."
                )
            raise Failed("The server did not accept the new value, and the write was not retried.")


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


def gib_text(value_bytes):
    """Bytes as the page shows them:  whole GiB plainly, otherwise one decimal."""
    amount = value_bytes / GIB
    return f"{int(amount)} GiB" if amount == int(amount) else f"{amount:.1f} GiB"


def validate(policy, bounds):
    """Reuse the controller schema, with the broker's additional host bounds.

    Each refusal names the rule it broke and what would be allowed, so the review sheet
    says what to change instead of a generic schema failure.
    """
    from hetzner.fleet_coding_memory import validate_policy

    online, total = bounds
    budget = policy.get("budget_bytes") if isinstance(policy, dict) else None
    critical = policy.get("critical_budget_bytes") if isinstance(policy, dict) else None
    if type(budget) is int and type(critical) is int and critical > budget:
        raise Refused(
            "Critical RAM Limit cannot be more than Pressure RAM Limit "
            f"({gib_text(critical)} against {gib_text(budget)}).{GAP}"
            "Lower Critical RAM Limit first, or raise Pressure RAM Limit first."
        )
    try:
        validate_policy(canonical(policy))
    except (ValueError, TypeError, KeyError, OverflowError):
        raise Refused("the complete resource policy fails the controller's schema") from None
    cores = policy.get("cpu_cores")
    if "cpu_cores" in policy and (type(cores) is not int or not 1 <= cores <= online - 2):
        raise Refused(
            f"CPU Cores must be 1 through {max(0, online - 2)} on this server, "
            f"which has {online} cores and keeps 2 free."
        )
    weight = policy.get("cpu_weight")
    if "cpu_weight" in policy and (type(weight) is not int or not 1 <= weight <= 10000):
        raise Refused("CPU Weight must be a whole number from 1 to 10,000.")
    if "allow_borrowing" in policy and type(policy["allow_borrowing"]) is not bool:
        raise Refused("Borrow Idle Memory must be on or off.")
    reserve = policy["reserve_bytes"]
    if reserve < 2 * GIB:
        raise Refused("Host RAM Reserve must be at least 2 GiB.")
    if budget + reserve > total:
        raise Refused(
            "Pressure RAM Limit and Host RAM Reserve together must fit in the server's "
            f"{gib_text(total)} of memory.{GAP}"
            f"With the reserve at {gib_text(reserve)} the limit can be at most "
            f"{(total - reserve) // GIB} GiB, and with the limit at {gib_text(budget)} the "
            f"reserve can be at most {(total - budget) // GIB} GiB."
        )
    if (
        not policy["critical_available_bytes"]
        <= policy["pressure_available_bytes"]
        <= policy["recovery_available_bytes"]
        <= total
    ):
        raise Refused("The saved policy's memory pressure levels are out of order for this server.")


def production_protection_bytes(policy, total):
    """The most memory production can be promised, as the controller computes its pool.

    fleet_coding_memory.protected_lows shares max(0, total - reserve - budget) among the
    production groups as memory.low, scaled down to their own caps, so this is an upper bound.
    """
    return max(0, total - policy["reserve_bytes"] - policy["budget_bytes"])


def field_display(field, value):
    """One field's value in the owner's units."""
    if value is None:
        return "not set"
    if field == "borrow-idle":
        return "On" if value else "Off"
    if field in ("ram-budget-gib", "ram-critical-gib", "ram-reserve-gib"):
        return gib_text(value)
    if field == "cpu-cores":
        return f"{value} core" if value == 1 else f"{value} cores"
    return str(value)


def plan_detail(field, before, after, current, patched, total):
    """The review sheet's plain account of one change and its real effect."""
    title = FIELD_TITLES[field]
    sentences = [
        f"{title} changes from {field_display(field, before)} to {field_display(field, after)}."
    ]
    if field in ("ram-budget-gib", "ram-reserve-gib"):
        now = production_protection_bytes(current, total)
        then = production_protection_bytes(patched, total)
        sentence = (
            f"This change leaves production up to {gib_text(then)} of protected memory "
            f"(now up to {gib_text(now)})."
        )
        if then < now:
            sentence = "Warning: " + sentence
        sentences.append(sentence)
    sentences.append("The memory controller reloads its policy; no service is restarted.")
    sentences.append(
        "If something else edits the same policy at the same moment, this change can replace it."
    )
    return GAP.join(sentences)


def desired(policy):
    result = {
        "ramPressureGiB": policy["budget_bytes"] / GIB,
        "ramCriticalGiB": policy["critical_budget_bytes"] / GIB,
        "reserveGiB": policy["reserve_bytes"] / GIB,
        "allowBorrowing": policy.get("allow_borrowing", True),
        "pressureAvailableGiB": policy["pressure_available_bytes"] / GIB,
        "criticalAvailableGiB": policy["critical_available_bytes"] / GIB,
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
    # Why the status is not applied, as fixed codes:  the controller's own allowlisted codes
    # first, then what this broker found.  Never free text from the receipt.
    reasons = []

    def reason(code):
        if code not in reasons:
            reasons.append(code)

    if not raw:
        reason("receipt-missing")
    raw_reasons = raw.get("reasons")
    if isinstance(raw_reasons, list):
        for code in raw_reasons[:32]:
            if isinstance(code, str) and code in CONTROLLER_REASONS:
                reason(code)
    result["reasons"] = reasons
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
        for key in (
            "ramPressureGiB",
            "ramCriticalGiB",
            "reserveGiB",
            "pressureAvailableGiB",
            "criticalAvailableGiB",
        ):
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
            "availableBytes",
            "productionProtectedGroups",
            "productionUnprotectedGroups",
            "oomKillDelta",
            "excludedUnits",
        ):
            if type(o.get(key)) is int and 0 <= o[key] <= 2**63 - 1:
                observed[key] = o[key]
        if observed.get("ramTotalBytes") == 0:
            # An older controller writes a zero total before its first memory check.  A
            # server has memory, so zero means not measured, and it stays absent.
            del observed["ramTotalBytes"]
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
        if raw:
            if (
                not (
                    type(raw.get("schemaVersion")) is int
                    and raw["schemaVersion"] == 1
                    and "policyRevision" in result
                )
                or source_time is None
                or (o.get("updatedAt") is not None and _safe_stamp(o.get("updatedAt")) is None)
            ):
                reason("receipt-invalid")
            elif not _fresh(source_time, now) or (
                o.get("updatedAt") is not None and "updatedAt" not in observed
            ):
                reason("receipt-stale")
            elif not complete:
                reason("receipt-incomplete")
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
                # The controller writes a zero target when no coding group is running.
                and (0 < observed["ramTargetBytes"] or observed["codingGroups"] == 0)
                and observed["ramTargetBytes"] <= total
                and d["ramCriticalGiB"] <= d["ramPressureGiB"]
                and d["reserveGiB"] >= 2
                and d["ramPressureGiB"] + d["reserveGiB"] <= total / GIB
            )
            counts_present = (
                observed["codingGroups"] > 0
                and observed["cpuGroups"] > 0
                and 3 <= observed["cpuOnline"] <= 1048576
            )
            if not ram_valid:
                reason("receipt-invalid")
            elif not counts_present and not reasons:
                reason("receipt-incomplete")
            for key, code in (
                ("policyStale", "policy-stale"),
                ("controlWriteFailures", "control-write-failures"),
                ("controlSuspended", "controls-suspended"),
                ("cpuMismatches", "cpu-mismatch"),
                ("actualHighMismatches", "memory-limit-mismatch"),
            ):
                if observed[key]:
                    reason(code)
            if ram_valid and counts_present and raw.get("status") in ("applied", "drift"):
                result["status"] = (
                    "applied"
                    if raw["status"] == "applied" and not any(observed[k] for k in failures)
                    else "drift"
                )
    elif raw:
        reason("receipt-invalid")
    if policy is not None:
        expected = revision(policy)
        result["desired"] = desired(policy)
        result["desiredRevision"] = expected
        if "policyRevision" in result and result["policyRevision"] != expected:
            # The saved policy is ahead of (or behind) what the controller is running.
            reason("policy-not-in-effect")
            if result["status"] == "applied":
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
            reason("cpu-values-differ")
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
    in_effect = False
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
            snapshot = public_snapshot(snapshot_reader(), ctx.now(), policy)
            if policy is None:
                # The saved values shown are then the controller's copy, not the source's.
                snapshot["reasons"].append("saved-policy-unreadable")
            answer.update(
                ok=True,
                result="planned",
                detail="coding resource status",
                resourcePolicy=snapshot,
            )
            return answer, 0
        if action != "resource-policy":
            raise Refused("unknown resource operation")
        field, value, expected = parse_target(target)
        store = getattr(ctx, "resource_store", None) or InfisicalStore(ctx)
        with broker_lock(ctx):
            bounds = getattr(ctx, "resource_host_bounds", host_bounds)()
            current = store.get()
            validate(current, bounds)
            if revision(current) != expected:
                raise Refused(
                    "The saved resource policy changed since this page read it.\u00a0 "
                    "Refresh Server Resource Limits and review the change again."
                )
            key, scale = FIELDS[field]
            patched = copy.deepcopy(current)
            patched[key] = bool(value) if field == "borrow-idle" else value * scale
            validate(patched, bounds)
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
                    detail=plan_detail(field, before, after, current, patched, bounds[1]),
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
                raise Refused(
                    "The saved resource policy changed since this page read it.\u00a0 "
                    "Refresh Server Resource Limits and review the change again."
                )
            store.save(patched)
            saved = True
            persisted = store.get()
            if revision(persisted) != revision(patched):
                raise SavedUnverified(
                    "The new value was sent, but reading the saved policy back did not match, "
                    "so the memory controller was not asked to reload."
                )
            answer["resourcePolicy"] = public_snapshot(snapshot_reader(), ctx.now(), persisted)
            status, _ = ctx.runner(list(RELOAD), timeout=5, env=ctx.child_env())
            if status:
                raise SavedUnverified(
                    "The new value is saved, but the memory controller did not confirm "
                    "that it reloaded the policy."
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
                    raise SavedUnverified(
                        "The new value is saved, but the memory controller did not report it "
                        "in effect within 15 seconds."
                    )
                sleep(min(0.25, max(0, deadline - monotonic())))
            in_effect = True
            audit(ctx, {**event, "result": "done"})
            event = None
            answer.update(
                ok=True,
                result="done",
                detail="The new value is saved and the memory controller reports it in effect.",
            )
            return answer, 0
    except Refused as exc:
        answer.update(detail=str(exc))
        code = 2
    except Exception as exc:  # noqa: BLE001 - Sanitize all operational failures at the boundary.
        # Unexpected operational failures still need a sanitized answer and outcome audit.
        if in_effect:
            # Only the closing audit record failed:  the change is saved and in effect.
            answer.update(
                ok=True,
                result="done",
                detail="The new value is saved and the memory controller reports it in effect."
                + GAP
                + "The outcome could not be written to the server's audit log.",
            )
            event = None
            code = 0
        elif saved:
            # The write landed, so this did not fail:  it is saved and not yet confirmed in
            # effect.  It is answered as submitted, the route's word for work that was sent
            # and not verified, so Muster never titles a persisted change "failed".
            what = (
                str(exc)
                if isinstance(exc, SavedUnverified)
                else "The new value is saved, but what happened next could not be checked."
            )
            answer.update(
                ok=True,
                result="submitted",
                executionState="saved",
                verified=False,
                saved=True,
                errorCode="saved_apply_unverified",
                detail=what + GAP + REFRESH_ADVICE,
            )
            code = 0
        elif isinstance(exc, WriteUncertain):
            # The write was sent and may have landed, so this is not known to have failed:
            # the route's word for that is unknown, never failed.
            answer.update(result="unknown", detail=str(exc) + GAP + REFRESH_ADVICE)
            code = 1
        else:
            detail = (
                str(exc)
                if isinstance(exc, Failed)
                else "the resource operation could not be verified"
            )
            answer.update(result="failed", detail=detail)
            code = 1
    if event:
        try:
            audit(ctx, {**event, "result": answer["result"]})
        except Exception:  # noqa: BLE001 - Audit failure must return a sanitized refusal.
            if saved:
                # Nothing can undo the save, so the saved fact stays in the answer.
                answer["detail"] += (
                    GAP + "The outcome could not be written to the server's audit log."
                )
            else:
                answer.update(
                    result="failed",
                    detail="the resource-policy outcome audit could not be recorded",
                )
                code = 1
    return answer, code
