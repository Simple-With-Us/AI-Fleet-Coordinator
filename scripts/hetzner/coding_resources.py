"""Runtime CPU ownership and the deliberately redacted public resource receipt."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@-]*$")
PUBLIC_STATUS = Path("/run/fleet-coding-resources/status.json")


def policy_revision(policy):
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def cpu_ids(raw):
    """Parse Linux CPU list syntax, including noncontiguous online IDs."""
    if not isinstance(raw, str):
        raise ValueError("invalid CPU list")
    if not raw.strip():
        return []
    out = set()
    for item in raw.strip().split(","):
        if not re.fullmatch(r"\d+(?:-\d+)?", item):
            raise ValueError("invalid CPU list")
        edges = [int(x) for x in item.split("-")]
        start, end = edges[0], edges[-1]
        if end < start or end > 1048575 or end - start > 1048575:
            raise ValueError("invalid CPU range")
        out.update(range(start, end + 1))
    return sorted(out)


def cpu_list(ids):
    return ",".join(str(n) for n in sorted(set(ids)))


def selected_cpus(online, count):
    ids = cpu_ids(online)
    if type(count) is not int or count < 1 or count > len(ids) - 2:
        raise ValueError("CPU budget must leave two online CPUs available")
    return cpu_list(ids[:count])


def _cpus(raw):
    return cpu_list(cpu_ids(raw))


def _utc():
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class CPUManager:
    """Only explicit CPU intent changes write controls; outside edits always win."""

    def __init__(
        self,
        root,
        state,
        persist=None,
        runner=subprocess.run,
        proc=Path("/proc"),
        online=Path("/sys/devices/system/cpu/online"),
    ):
        self.root = Path(root).resolve()
        self.proc = Path(proc)
        self.online = Path(online)
        self.state = state
        self.persist = persist or (lambda: None)
        self.runner = runner
        self.targets = {}
        self.missing = 0
        self.failures = 0
        self.invalid = False
        if not isinstance(state.get("cpu_owned", {}), dict):
            raise ValueError("invalid CPU ownership journal")
        for key, rec in state.get("cpu_owned", {}).items():
            self._validate_record(key, rec)

    def _path(self, raw):
        p = (self.root / str(raw).lstrip("/")).resolve(strict=True)
        if p == self.root or self.root not in p.parents:
            raise ValueError("invalid CPU cgroup")
        return p

    def _validate_record(self, key, rec):
        if (
            not isinstance(rec, dict)
            or rec.get("kind") not in ("unit", "docker")
            or not isinstance(rec.get("name"), str)
            or not NAME.fullmatch(rec["name"])
        ):
            raise ValueError("invalid CPU ownership target")
        if key != rec["kind"] + ":" + rec["name"] or type(rec.get("inode")) is not int:
            raise ValueError("invalid CPU ownership identity")
        if not isinstance(rec.get("path"), str):
            raise ValueError("invalid CPU ownership path")
        p = Path(rec["path"])
        if not p.is_absolute() or self.root not in p.parents:
            raise ValueError("invalid CPU ownership path")
        if not isinstance(rec.get("fields"), dict):
            raise ValueError("invalid CPU ownership fields")
        for field, values in rec["fields"].items():
            if field not in ("cpus", "weight") or not isinstance(values, dict):
                raise ValueError("invalid CPU ownership fields")
            for label, value in values.items():
                if label not in (
                    "baseline",
                    "last",
                    "pending",
                    "intent",
                ) or not isinstance(value, str):
                    raise ValueError("invalid CPU ownership value")
                if field == "cpus":
                    cpu_ids(value)
                elif not value.isdigit() or not 1 <= int(value) <= 10000:
                    raise ValueError("invalid CPU weight")
            if "baseline" not in values:
                raise ValueError("missing CPU baseline")

    def _run(self, args, check=True):
        return self.runner(args, capture_output=True, text=True, timeout=8, check=check)

    def _read(self, target, effective=False):
        p = Path(target["path"])
        # Resolve on each read to reject replaced paths/symlinks outside cgroupfs.
        self._path(p.relative_to(self.root))
        values = {
            "cpus": _cpus(
                (p / ("cpuset.cpus.effective" if effective else "cpuset.cpus")).read_text()
            )
        }
        weight = (p / "cpu.weight").read_text().strip()
        if not weight.isdigit() or not 1 <= int(weight) <= 10000:
            raise ValueError("invalid kernel CPU weight")
        values["weight"] = str(int(weight))
        return p.stat().st_ino, values

    def discover(self, policy, excluded_units=(), excluded_cgroups=()):
        self.targets = {}
        self.missing = 0
        excluded = set(excluded_units)
        excluded_paths = set(excluded_cgroups)
        intended = {"unit:" + name for name in policy["coding_units"] if name not in excluded}
        intended.update("docker:" + name for name in policy["coding_containers"])
        omitted = set()

        def add(kind, name, path):
            if any(path == ex or ex in path.parents or path in ex.parents for ex in excluded_paths):
                omitted.add(kind + ":" + name)
                return
            target = {"kind": kind, "name": name, "path": str(path)}
            self._read(target)
            self.targets[kind + ":" + name] = target

        for name in policy["coding_units"]:
            if name in excluded:
                continue
            try:
                if not NAME.fullmatch(name):
                    raise ValueError("invalid unit")
                raw = self._run(
                    ["systemctl", "show", "--value", "-pControlGroup", name]
                ).stdout.strip()
                if not raw:
                    raise ValueError("missing cgroup")
                add("unit", name, self._path(raw))
            except Exception:
                self.missing += 1
        for name in policy["coding_containers"]:
            try:
                if not NAME.fullmatch(name):
                    raise ValueError("invalid container")
                raw = self._run(
                    [
                        "docker",
                        "inspect",
                        "--format",
                        "{{.Name}}|{{.State.Pid}}|{{.HostConfig.CpusetCpus}}",
                        name,
                    ]
                ).stdout.strip()
                actual, pid, configured = raw.split("|")
                if actual.lstrip("/") != name or not pid.isdigit() or int(pid) <= 0:
                    raise ValueError("missing container")
                cpu_ids(configured)
                lines = (self.proc / pid / "cgroup").read_text().splitlines()
                rel = next(line[3:] for line in lines if line.startswith("0::"))
                add("docker", name, self._path(rel))
            except Exception:
                self.missing += 1
        # Removed/excluded targets are handed back through the same ownership guard.
        for key, rec in list(self.state.get("cpu_owned", {}).items()):
            if key not in intended or key in omitted:
                self._restore(key, rec)

    def _write(self, target, values, inode, expected):
        if not values or set(values) - {"cpus", "weight"}:
            raise ValueError("invalid CPU control write")
        if target["kind"] == "docker" and set(values) != {"cpus"}:
            raise ValueError("Docker CPU weight is unmanaged")
        if target["kind"] not in ("unit", "docker"):
            raise ValueError("invalid CPU control target")
        path = self._path(Path(target["path"]).relative_to(self.root))
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        directory = os.open(path, flags)
        try:
            if os.fstat(directory).st_ino != inode:
                raise RuntimeError("CPU target identity changed before write")
            # Both systemd and Docker reconciliation can reset unrelated hard
            # controls.  Runtime CPU changes therefore write only these files.
            for field, filename in (("cpus", "cpuset.cpus"), ("weight", "cpu.weight")):
                if field not in values:
                    continue
                read_fd = os.open(
                    filename,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory,
                )
                try:
                    raw = os.read(read_fd, 1048576).decode().strip()
                finally:
                    os.close(read_fd)
                current = _cpus(raw) if field == "cpus" else str(int(raw))
                if current != expected[field]:
                    raise RuntimeError("CPU ownership changed before write")
                fd = os.open(
                    filename,
                    os.O_WRONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory,
                )
                try:
                    # A newline also clears an inherited empty cpuset on restore.
                    data = (values[field] + "\n").encode()
                    if os.write(fd, data) != len(data):
                        raise OSError("incomplete CPU control write")
                finally:
                    os.close(fd)
        finally:
            os.close(directory)

    def _settle(self, rec, current):
        changed = False
        for field, values in rec["fields"].items():
            pending = values.pop("pending", None)
            if pending is not None:
                if current[field] == pending:
                    values["last"] = pending
                changed = True
        if changed:
            self.persist()

    def apply(self, policy, suspended=()):
        self.failures = 0
        self.invalid = False
        try:
            desired = {}
            if "cpu_cores" in policy:
                desired["cpus"] = selected_cpus(self.online.read_text(), policy["cpu_cores"])
            if "cpu_weight" in policy:
                weight = policy["cpu_weight"]
                if type(weight) is not int or not 1 <= weight <= 10000:
                    raise ValueError("invalid CPU weight")
                desired["weight"] = str(weight)
        except (OSError, ValueError):
            self.invalid = True
            return
        # Removing one optional setting relinquishes that field, independently
        # of target discovery or the other CPU setting that remains configured.
        for key, rec in list(self.state.get("cpu_owned", {}).items()):
            if "*" in suspended or key in suspended or rec["path"] in suspended:
                continue
            wanted_fields = {field for field in desired if rec["kind"] == "unit" or field == "cpus"}
            omitted = set(rec["fields"]) - wanted_fields
            if omitted:
                self._restore(key, rec, omitted)
        for key, target in self.targets.items():
            if "*" in suspended or key in suspended or target["path"] in suspended:
                continue
            wanted = {k: v for k, v in desired.items() if target["kind"] == "unit" or k == "cpus"}
            if not wanted:
                continue
            try:
                inode, current = self._read(target)
                owned = self.state.setdefault("cpu_owned", {})
                rec = owned.get(key)
                if rec and rec["inode"] != inode:
                    # A recreated cgroup is a new ownership identity.
                    owned.pop(key)
                    rec = None
                if rec:
                    self._settle(rec, current)
                else:
                    rec = dict(target, inode=inode, fields={})
                changes = {}
                updated = False
                for field, value in wanted.items():
                    fieldrec = rec["fields"].get(field)
                    if fieldrec and current[field] != fieldrec.get("last", fieldrec["baseline"]):
                        # Even a changed policy cannot seize a disputed control.
                        continue
                    if fieldrec and fieldrec.get("intent") == value and current[field] == value:
                        continue
                    if not fieldrec:
                        fieldrec = rec["fields"][field] = {"baseline": current[field]}
                    fieldrec["intent"] = value
                    updated = True
                    if current[field] != value:
                        fieldrec["pending"] = value
                        changes[field] = value
                    else:
                        fieldrec["last"] = value
                if updated:
                    owned[key] = rec
                    self.persist()  # Durable baseline and pending receipt before any mutation.
                if changes:
                    command_failed = False
                    try:
                        self._write(target, changes, inode, current)
                    except Exception:
                        command_failed = True
                    # A failed write can still have applied earlier CPU fields.
                    actual_inode, actual = self._read(target)
                    if actual_inode != inode:
                        raise RuntimeError("CPU target changed during write")
                    self._settle(rec, actual)
                    if command_failed or any(actual[k] != v for k, v in changes.items()):
                        self.failures += 1
            except Exception:
                self.failures += 1

    def _restore(self, key, rec, fields=None):
        try:
            inode, current = self._read(rec)
            if inode != rec["inode"]:
                self.state["cpu_owned"].pop(key, None)
                self.persist()
                return
            self._settle(rec, current)
            fields = set(rec["fields"]) if fields is None else set(fields) & set(rec["fields"])
            values = {
                k: v["baseline"]
                for k, v in rec["fields"].items()
                if k in fields and current[k] == v.get("last") and current[k] != v["baseline"]
            }
            actual = current
            if values:
                # Journal restoration too, including partial CPU control writes.
                for field, value in values.items():
                    rec["fields"][field]["pending"] = value
                self.persist()
                command_failed = False
                try:
                    self._write(rec, values, inode, current)
                except Exception:
                    command_failed = True
                actual_inode, actual = self._read(rec)
                if actual_inode != rec["inode"]:
                    self.state["cpu_owned"].pop(key, None)
                    self.persist()
                    return
                self._settle(rec, actual)
                if command_failed or any(actual[k] != v for k, v in values.items()):
                    self.failures += 1
            for field in fields:
                fieldrec = rec["fields"][field]
                if (
                    field in values
                    and actual[field] != fieldrec["baseline"]
                    and actual[field] == fieldrec.get("last")
                ):
                    # A failed restoration left our last value intact: retain
                    # ownership so a later tick can safely retry the handback.
                    continue
                # Restored, already at baseline, or changed outside our control.
                # The other configured fields retain their ownership records.
                del rec["fields"][field]
            if not rec["fields"]:
                self.state["cpu_owned"].pop(key, None)
            self.persist()
        except FileNotFoundError:
            self.state["cpu_owned"].pop(key, None)
            self.persist()
        except Exception:
            self.failures += 1

    def restore(self, suspended=()):
        for key, rec in list(self.state.get("cpu_owned", {}).items()):
            if "*" in suspended or key in suspended or rec["path"] in suspended:
                continue
            self._restore(key, rec)

    def observe(self, policy):
        try:
            online = cpu_ids(self.online.read_text())
            wanted = (
                selected_cpus(self.online.read_text(), policy["cpu_cores"])
                if "cpu_cores" in policy
                else None
            )
        except (OSError, ValueError):
            online, wanted = [], None
        cores, weights = [], []
        mismatches, readable = self.missing, 0
        for target in self.targets.values():
            try:
                _, actual = self._read(target, effective=True)
                readable += 1
                cores.append(len(cpu_ids(actual["cpus"])))
                mismatch = wanted is not None and actual["cpus"] != wanted
                if target["kind"] == "unit":
                    weights.append(int(actual["weight"]))
                    mismatch |= (
                        "cpu_weight" in policy and int(actual["weight"]) != policy["cpu_weight"]
                    )
                mismatches += int(mismatch)
            except (OSError, ValueError):
                mismatches += 1
        out = {
            "cpuOnline": len(online),
            "cpuGroups": readable,
            "cpuMismatches": mismatches,
        }
        if cores and len(set(cores)) == 1:
            out["cpuCores"] = cores[0]
        if weights and len(set(weights)) == 1:
            out["cpuWeight"] = weights[0]
        return out


def public_snapshot(policy, state, cpu, now=None):
    """Allowlist only: the private journal and inventory never leave this module."""
    now = now or _utc()
    report = state.get("last_status", {})
    report = report if isinstance(report, dict) else {}
    gib = 1024**3
    desired = {
        "ramPressureGiB": policy["budget_bytes"] / gib,
        "ramCriticalGiB": policy["critical_budget_bytes"] / gib,
        "reserveGiB": policy["reserve_bytes"] / gib,
        "allowBorrowing": policy.get("allow_borrowing", True),
    }
    for source, dest in (("cpu_cores", "cpuCores"), ("cpu_weight", "cpuWeight")):
        if source in policy:
            desired[dest] = policy[source]
    observed = cpu.observe(policy)
    for source, dest, default in (
        ("coding_current_bytes", "ramCurrentBytes", 0),
        ("coding_target_bytes", "ramTargetBytes", 0),
        ("total_bytes", "ramTotalBytes", 0),
        ("mode", "mode", "unknown"),
        ("coding_groups", "codingGroups", 0),
        ("production_groups", "productionGroups", 0),
        ("control_write_failures", "controlWriteFailures", 0),
        ("control_suspended", "controlSuspended", 0),
        ("policy_stale", "policyStale", True),
        ("actual_high_mismatches", "actualHighMismatches", 0),
    ):
        value = report.get(source, default)
        if dest == "mode":
            value = value if value in ("normal", "pressure", "critical") else "unknown"
        elif dest == "policyStale":
            value = value if type(value) is bool else True
        else:
            value = value if type(value) is int and value >= 0 else default
        observed[dest] = value
    observed["controlWriteFailures"] += cpu.failures
    observed["updatedAt"] = now
    unavailable = (
        cpu.invalid
        or not report
        or observed["policyStale"]
        or not observed["codingGroups"]
        or not observed["cpuGroups"]
        or cpu.missing
        or not observed["cpuOnline"]
        or not observed["productionGroups"]
        or report.get("production_unprotected_groups", 0)
        or "cpu_cores" not in policy
        or "cpu_weight" not in policy
        or observed["controlWriteFailures"]
        or observed["controlSuspended"]
    )
    drift = observed["cpuMismatches"] or observed["actualHighMismatches"]
    return {
        "schemaVersion": 1,
        "generatedAt": now,
        "policyRevision": policy_revision(policy),
        "desired": desired,
        "observed": observed,
        "status": "unavailable" if unavailable else "drift" if drift else "applied",
    }


def publish_snapshot(snapshot, path=PUBLIC_STATUS):
    path = Path(path)
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if (
        not path.parent.is_dir()
        or path.parent.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o022
    ):
        raise ValueError("untrusted public snapshot directory")
    os.chmod(path.parent, 0o755)
    fd, raw = tempfile.mkstemp(prefix=".status-", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snapshot, f, sort_keys=True, separators=(",", ":"), allow_nan=False)
            f.write("\n")
            f.flush()
            os.fchmod(f.fileno(), 0o644)
            os.fsync(f.fileno())
        os.replace(temporary, path)
        # The ephemeral receipt is already visible atomically; directory fsync
        # only makes the rename durable, and unsupported filesystems may refuse it.
        try:
            directory = os.open(
                path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
