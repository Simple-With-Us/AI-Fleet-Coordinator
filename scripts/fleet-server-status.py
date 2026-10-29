#!/usr/bin/env python3
"""fleet-server-status:  what the server is doing,  and where its config has drifted.

One JSON report for Muster to read:
`/root/.local/state/fleet-tools/server-status.json`  (mode 0600, written atomically).

  python3 scripts/fleet-server-status.py [--home PATH] [--code-root PATH] [--control PATH]
                                         [--snapshot PATH] [--output PATH] [--dry-run]

What it reads,  and what it refuses to read:

  * Coolify comes from Docker's own labels and Docker's own container stats:  never
    from the Coolify API and never from an `.env`.  `docker ps -a` names the
    containers,  `docker inspect --format` reads the labels, the state and the health
    and nothing else,  and `docker stats --no-stream` gives the cpu and the memory.
  * A process is identified by its executable:  `/proc/<pid>/exe` and
    `/proc/<pid>/comm`.  Its argv and its environment are never read.
  * The file list is the fleet's config surface:  the instruction and settings files
    under the fleet's checkouts.  A path that looks like a credential is never hashed
    and never listed.
  * The host's load,  memory,  swap,  uptime and disks come from `/proc` and
    `os.statvfs`.  The disks are `/` and every real file system mounted under `/mnt`.
  * The fleet's systemd units are the ones `scripts/fleet-ops-control.json` names
    (`server.units`),  read with `systemctl show` and an explicit property list,  one
    call per scope.  A unit is `controllable` only when `scripts/fleet-ops-control.py`
    itself accepts the allowlist and that entry:  the helper refuses the whole file for
    one bad entry,  and then no unit is controllable.  A missing or malformed allowlist
    means no units,  never a crash.

A measurement that cannot be taken is omitted,  never reported as a zero:  a container
with no stats row carries no cpu and no memory,  and a file that is not there is
`drift: "missing"`  rather than a zero byte file.

Drift is measured against the previous report:  `new` when the path was not in it,
`changed` when the digest differs,  `missing` when it was there and is gone now,  and
`unchanged` otherwise.  A Coolify resource that was in the previous report and is
gone now reads as `absent`.  Each Coolify row says where its `uuid` came from:
`uuidSource` is `label` for a Coolify uuid label and `name` for a stand-in name.  A change to a `README.md` or a `package.json` is
`info` rather than `changed`:  those files move with ordinary work,  not with config.
`filesSummary` says how many files were found against the bound,  so a reader can tell
a partial list from a whole one.

The script writes one file and changes nothing else.
"""

from __future__ import annotations

import argparse
import datetime
import errno
import hashlib
import importlib.util
import json
import os
import pwd
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

TOOL = "fleet-server-status"
SCHEMA_VERSION = 1
DEFAULT_CODE_ROOT = Path.home() / "apps"

# The instruction and settings files that carry a repo's config surface.
WATCHED_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "INFISICAL.md",
    "project.yml",
    "package.json",
)
# The folders that hold build output or dependencies,  never config.
SKIP_DIRECTORIES = (
    ".git",
    "node_modules",
    "build",
    "dist",
    ".build",
    "vendor",
)
# A path with one of these in it is a credential,  or may be:  it is never hashed and
# never listed.
SECRET_MARKERS = (
    "secret",
    "credential",
    ".env",
    "key",
    "token",
)

# The docker fields this collector needs,  and nothing else:  the environment of a
# container is not among them.
#
# `index` is used throughout:  docker's template writes a missing key as
# `<no value>` instead of failing,  and a container with no health check has no
# `Health` key at all.
INSPECT_FORMAT = "\x1f".join(
    [
        "{{.Id}}",
        "{{.Name}}",
        "{{.Config.Image}}",
        '{{index .State "Status"}}',
        '{{if index .State "Health"}}{{index (index .State "Health") "Status"}}{{end}}',
        '{{index .State "StartedAt"}}',
        '{{index .State "FinishedAt"}}',
        '{{index .Config.Labels "coolify.applicationUuid"}}',
        '{{index .Config.Labels "coolify.name"}}',
        '{{index .Config.Labels "coolify.type"}}',
        '{{index .Config.Labels "coolify.resourceName"}}',
        '{{index .Config.Labels "coolify.serviceName"}}',
        '{{index .Config.Labels "com.docker.compose.service"}}',
    ]
)

MEMORY_UNITS = {
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
    "t": 1024**4,
}

COOLIFY_TYPES = ("application", "service", "database", "standalone")

# A change to one of these is reported as `info`:  they move with ordinary work.
INFO_FILES = ("README.md", "package.json")

# The allowlist that names the fleet's units,  next to this script.
DEFAULT_CONTROL = Path(__file__).resolve().parent / "fleet-ops-control.json"
# The helper that acts on it:  its own check says which units are controllable.
DEFAULT_CONTROLLER = Path(__file__).resolve().parent / "fleet-ops-control.py"
CONTROL_LIMIT = 256 * 1024
SCOPES = ("system", "user")
# A unit name is passed to systemctl as an argument,  so it must look like one and never
# like an option.
UNIT_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9@._:\-]{0,200}\.(?:service|timer|socket|path|target|mount)"
)
# The properties read for each unit,  and nothing else.
UNIT_PROPERTIES = (
    "Id",
    "Description",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "StateChangeTimestamp",
)

# The file systems that hold data.  Everything else under `/mnt` (a tmpfs,  a fuse
# mount,  a bind of a pseudo file system) is not a disk.
REAL_FILE_SYSTEMS = (
    "ext2",
    "ext3",
    "ext4",
    "xfs",
    "btrfs",
    "zfs",
    "f2fs",
    "jfs",
    "reiserfs",
    "vfat",
    "exfat",
    "ntfs",
    "ntfs3",
)


# MARK: - Helpers


def utc_now() -> str:
    """The time this report was made,  in the spelling the doctor uses."""
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_timestamp(value: float) -> str:
    moment = datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(command, timeout=30):
    """Run a command and return its stdout,  or None when it could not be run."""
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def parse_percent(text):
    """A percentage,  or None when the text does not carry one."""
    if not text:
        return None
    cleaned = text.strip().rstrip("%")
    try:
        return round(float(cleaned), 1)
    except ValueError:
        return None


def label_value(text):
    """A label's value,  or an empty string when the container does not carry it.

    Docker's template writes `<no value>` for a label the container does not have.
    """
    value = (text or "").strip()
    if value == "<no value>":
        return ""
    return value


def parse_size(text):
    """A size in bytes,  or None when the text does not carry one.

    Docker writes "123.45MiB / 7.7GiB":  the used part is the measurement.
    """
    if not text:
        return None
    first = text.strip().split("/")[0].strip()
    if not first:
        return None
    digits = ""
    index = 0
    while index < len(first) and (first[index].isdigit() or first[index] in "._"):
        digits += first[index]
        index += 1
    unit = first[index:].strip().lower()
    if not digits:
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    if unit not in MEMORY_UNITS:
        return None
    return int(value * MEMORY_UNITS[unit])


def is_secret_path(path) -> bool:
    """Whether a path looks like a credential:  it is never hashed and never listed."""
    lowered = str(path).lower()
    return any(
        re.search(r"(?:^|[./_\-])" + re.escape(marker) + r"(?:s|ed)?(?:$|[./_\-])", lowered)
        for marker in SECRET_MARKERS
    )


def digest_of(path):
    """The first sixteen hex characters of a file's digest,  or None when it cannot be read."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            return None
        hasher = hashlib.sha256()
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            remaining = 4 * 1024 * 1024 + 1
            while remaining:
                chunk = handle.read(min(65536, remaining))
                if not chunk:
                    return hasher.hexdigest()[:16]
                remaining -= len(chunk)
                hasher.update(chunk)
        return None
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


# MARK: - Coolify


def container_status(state, health):
    """The status of a container,  in the vocabulary the report uses.

    A state this collector does not know is `unknown`:  an unreadable state is never
    a running one.
    """
    if state == "running":
        if health == "unhealthy":
            return "unhealthy"
        return "running"
    if state in ("exited", "dead"):
        return "exited"
    return "unknown"


# Where a Coolify row's `uuid` came from:  the Coolify uuid label,  or a stand-in name.
UUID_SOURCE_LABEL = "label"
UUID_SOURCE_NAME = "name"
UUID_SOURCES = (UUID_SOURCE_LABEL, UUID_SOURCE_NAME)


def coolify_type(label):
    """The Coolify type of a container,  and `standalone` when it carries no label."""
    value = (label or "").strip().lower()
    return value if value in COOLIFY_TYPES else "standalone"


def resource_identity(fields):
    """The identity of one container,  from its Coolify labels:  (uuid, name, source).

    A container with no Coolify label is standalone,  and is named by its own name.
    A Coolify application keeps its uuid across a redeploy,  so the uuid is the
    identity and the name only says which resource it is.  `source` is `label` when
    the uuid came from the Coolify uuid label,  and `name` when no such label was
    there and the resource name (or the container name) stands in for it:  a reader
    must never send a stand-in to Coolify as a uuid.
    """
    uuid = fields["application_uuid"].strip()
    resource = fields["resource_name"].strip()
    label_name = fields["label_name"].strip()
    container = fields["container"].strip()
    source = UUID_SOURCE_LABEL
    if not uuid:
        uuid = resource or container
        source = UUID_SOURCE_NAME
    name = resource or label_name or container
    return uuid, name, source


class ResourceRows(list):
    complete = True


def collect_coolify(run_command=run):
    """Every container on the host,  with what Docker knows about it.

    A container that cannot be inspected is left out:  a measurement that cannot be
    taken is omitted,  never reported as a zero.
    """
    listed = run_command(["docker", "ps", "-a", "--format", "{{.ID}}"])
    if listed is None:
        rows = ResourceRows()
        rows.complete = False
        return rows
    ids = [line.strip() for line in listed.splitlines() if line.strip()]
    if not ids:
        return []
    complete = True
    inspected = run_command(["docker", "inspect", "--format", INSPECT_FORMAT] + ids)
    if inspected is None:
        # A container that was removed between the two calls fails the whole batch,
        # so every container is asked on its own and the ones that are gone are left
        # out:  one redeploy must not blank the whole section.
        lines = []
        for identifier in ids:
            answer = run_command(["docker", "inspect", "--format", INSPECT_FORMAT, identifier])
            if answer is None:
                complete = False
                continue
            lines.append(answer.strip())
        inspected = "\n".join(lines)
    if not inspected:
        rows = ResourceRows()
        rows.complete = complete
        return rows

    stats = {}
    stats_text = run_command(
        ["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"],
        timeout=60,
    )
    if stats_text is not None:
        for line in stats_text.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            name = parts[0].strip()
            if name:
                stats[name] = (parse_percent(parts[1]), parse_size(parts[2]))

    rows = ResourceRows()
    rows.complete = complete
    for line in inspected.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 13:
            rows.complete = False
            continue
        fields = {
            "id": parts[0].strip(),
            "container": parts[1].strip().lstrip("/"),
            "image": parts[2].strip(),
            "state": parts[3].strip(),
            "health": parts[4].strip(),
            "started": parts[5].strip(),
            "finished": parts[6].strip(),
            "application_uuid": label_value(parts[7]),
            "label_name": label_value(parts[8]),
            "label_type": label_value(parts[9]),
            "resource_name": label_value(parts[10]),
            "service_name": label_value(parts[11]),
            "compose_service": label_value(parts[12]),
        }
        uuid, name, source = resource_identity(fields)
        row = {
            "uuid": uuid,
            "uuidSource": source,
            "name": name,
            "type": coolify_type(fields["label_type"]),
            "status": container_status(fields["state"], fields["health"]),
        }
        if fields["image"]:
            row["image"] = fields["image"]
        updated = fields["started"]
        if fields["state"] in ("exited", "dead") and fields["finished"]:
            updated = fields["finished"]
        if updated and not updated.startswith("0001-01-01"):
            row["updatedAt"] = updated
        cpu, memory = stats.get(fields["container"], (None, None))
        if cpu is not None:
            row["cpuPercent"] = cpu
        if memory is not None:
            row["memoryBytes"] = memory
        row["_service"] = fields["compose_service"] or fields["container"]
        rows.append(row)

    return disambiguate(rows)


def disambiguate(rows):
    """Give two containers of one application a name each.

    The Coolify labels carry the application's uuid and the resource name,  and both
    are the same for every service of that application,  so the service name is added
    to keep one row per container.
    """
    for row in rows:
        if row.get("_service") and row["_service"] != row["name"]:
            row["name"] = "%s (%s)" % (row["name"], row["_service"])
        row.pop("_service", None)
    return rows


# MARK: - Processes


def process_ticks(pid, proc=Path("/proc")):
    """The cpu time a process has used,  in clock ticks.

    `/proc/<pid>/stat` is read after the last `)`,  so the command name  which may
    hold spaces  cannot shift the fields.  The argv of the process is never read.
    """
    try:
        raw = (proc / str(pid) / "stat").read_text()
    except (OSError, ValueError):
        return None
    close = raw.rfind(")")
    if close < 0:
        return None
    rest = raw[close + 1 :].split()
    if len(rest) < 13:
        return None
    try:
        return int(rest[11]) + int(rest[12])
    except ValueError:
        return None


def process_name(pid, proc=Path("/proc")):
    """The name of a process's executable,  and nothing else.

    `/proc/<pid>/exe` is the executable identity;  the argv of the process,  which can
    hold a token,  is never read.
    """
    try:
        return Path(os.readlink(proc / str(pid) / "exe")).name
    except OSError:
        pass
    try:
        return (proc / str(pid) / "comm").read_text().strip()
    except OSError:
        return None


def process_owner(pid, proc=Path("/proc")):
    """The user a process runs as,  by the owner of its `/proc` folder."""
    try:
        uid = (proc / str(pid)).stat().st_uid
    except OSError:
        return None
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def process_memory(pid, proc=Path("/proc")):
    """The resident memory of a process,  in bytes."""
    try:
        fields = (proc / str(pid) / "statm").read_text().split()
    except (OSError, ValueError, IndexError):
        return None
    try:
        pages = int(fields[1])
    except (ValueError, IndexError):
        return None
    return pages * os.sysconf("SC_PAGE_SIZE")


def collect_processes(limit=15, interval=0.4, proc=Path("/proc")):
    """The busiest processes on the host.

    The cpu of a process is the difference between two samples,  so the whole host is
    sampled twice,  once,  rather than each process in turn.
    """
    try:
        pids = [int(entry.name) for entry in proc.iterdir() if entry.name.isdigit()]
    except OSError:
        return []
    mine = {os.getpid(), os.getppid()}
    pids = [pid for pid in pids if pid not in mine]

    first = {}
    for pid in pids:
        ticks = process_ticks(pid, proc)
        if ticks is not None:
            first[pid] = ticks
    time.sleep(max(0.0, interval))
    second = {}
    for pid in pids:
        ticks = process_ticks(pid, proc)
        if ticks is not None:
            second[pid] = ticks

    clock_ticks = os.sysconf("SC_CLK_TCK") or 100
    rows = []
    for pid in sorted(pids):
        cpu = None
        if pid in first and pid in second:
            delta = second[pid] - first[pid]
            if delta >= 0 and interval > 0:
                cpu = round(delta / clock_ticks / interval * 100.0, 1)
        memory = process_memory(pid, proc)
        if cpu is None and memory is None:
            continue
        row = {"pid": pid}
        owner = process_owner(pid, proc)
        if owner:
            row["owner"] = owner
        name = process_name(pid, proc)
        if name:
            row["name"] = name
        if cpu is not None:
            row["cpuPercent"] = cpu
        if memory is not None:
            row["memoryBytes"] = memory
        rows.append(row)

    rows.sort(
        key=lambda row: (
            -(row.get("cpuPercent") if row.get("cpuPercent") is not None else -1.0),
            -(row.get("memoryBytes") if row.get("memoryBytes") is not None else -1),
            row["pid"],
        )
    )
    return rows[:limit]


# MARK: - The host


def read_small(path, limit=1024 * 1024):
    """The text of a small file under `/proc`,  or None when it cannot be read."""
    try:
        with open(path, "r", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return None


def load_average(proc=Path("/proc")):
    """The 1,  5 and 15 minute load,  or None when `/proc/loadavg` cannot be read."""
    text = read_small(proc / "loadavg")
    if not text:
        return None
    fields = text.split()
    if len(fields) < 3:
        return None
    try:
        return [round(float(field), 2) for field in fields[:3]]
    except ValueError:
        return None


def meminfo(proc=Path("/proc")):
    """`/proc/meminfo` in bytes,  by field name.  A line that does not parse is skipped."""
    text = read_small(proc / "meminfo")
    values = {}
    for line in (text or "").splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if not name or not parts:
            continue
        try:
            number = int(parts[0])
        except ValueError:
            continue
        if number < 0:
            continue
        unit = parts[1].lower() if len(parts) > 1 else ""
        values[name.strip()] = number * 1024 if unit == "kb" else number
    return values


def uptime_seconds(proc=Path("/proc")):
    """How long the host has been up,  in whole seconds."""
    text = read_small(proc / "uptime")
    if not text or not text.split():
        return None
    try:
        value = float(text.split()[0])
    except ValueError:
        return None
    return int(value) if value >= 0 else None


def unescape_mount(text):
    """A mount point as `/proc/mounts` writes it,  with `\\040` for a space,  decoded."""
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), text)


def disk_mounts(proc=Path("/proc")):
    """`/` and every real file system mounted under `/mnt`,  each once,  in mount order."""
    text = read_small(proc / "mounts")
    mounts = []
    for line in (text or "").splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        point = unescape_mount(fields[1])
        if fields[2] not in REAL_FILE_SYSTEMS:
            continue
        if point != "/" and not point.startswith("/mnt/"):
            continue
        if point not in mounts:
            mounts.append(point)
    return mounts


def disk_usage(mount, statvfs=os.statvfs):
    """The size of one mount,  or None when it cannot be measured.

    `freeBytes` is what an ordinary user can still write,  which is what fills up.
    """
    try:
        info = statvfs(mount)
    except OSError:
        return None
    size = info.f_frsize or info.f_bsize
    if not size or info.f_blocks <= 0:
        return None
    total = info.f_blocks * size
    return {
        "mount": mount,
        "totalBytes": total,
        "usedBytes": max(0, (info.f_blocks - info.f_bfree) * size),
        "freeBytes": max(0, info.f_bavail * size),
    }


def collect_host_metrics(proc=Path("/proc"), statvfs=os.statvfs, cpu_count=os.cpu_count):
    """The host's load,  memory,  swap,  uptime and disks.

    Each measurement that cannot be taken is left out,  never reported as a zero.
    """
    metrics = {}
    load = load_average(proc)
    if load is not None:
        metrics["loadAverage"] = load
    cpus = cpu_count()
    if isinstance(cpus, int) and cpus > 0:
        metrics["cpuCount"] = cpus
    memory = meminfo(proc)
    if "MemTotal" in memory and "MemAvailable" in memory:
        metrics["memory"] = {
            "totalBytes": memory["MemTotal"],
            "availableBytes": memory["MemAvailable"],
        }
    if "SwapTotal" in memory and "SwapFree" in memory:
        metrics["swap"] = {"totalBytes": memory["SwapTotal"], "freeBytes": memory["SwapFree"]}
    uptime = uptime_seconds(proc)
    if uptime is not None:
        metrics["uptimeSeconds"] = uptime
    disks = []
    for mount in disk_mounts(proc):
        usage = disk_usage(mount, statvfs)
        if usage is not None:
            disks.append(usage)
    metrics["disks"] = disks
    return metrics


# MARK: - The fleet's units


def control_units(path=DEFAULT_CONTROL, controller=DEFAULT_CONTROLLER):
    """The units the allowlist names,  as (unit, scope, controllable).

    A missing,  unreadable or malformed allowlist names no units.  An entry with a bad
    unit name or scope is skipped,  and a unit named twice in one scope is kept once.
    `controllable` is the control helper's own verdict (`accepted_units`),  so the report
    never offers a unit the helper would refuse.
    """
    try:
        if path.stat().st_size > CONTROL_LIMIT:
            return []
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(document, dict):
        return []
    server = document.get("server")
    if not isinstance(server, dict):
        return []
    entries = server.get("units")
    if not isinstance(entries, list):
        return []
    accepted = accepted_units(path, controller)
    units = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        unit = entry.get("unit")
        scope = entry.get("scope")
        if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
            continue
        if scope not in SCOPES or (unit, scope) in seen:
            continue
        seen.add((unit, scope))
        units.append((unit, scope, accepted.get(unit) == scope))
    return units


def accepted_units(path, controller=DEFAULT_CONTROLLER):
    """{unit: scope} for the units the control helper accepts in the allowlist at `path`.

    The helper's own `load_config` decides,  so this reader can never be wider than
    it.  The helper refuses the whole file for one bad entry,  and a helper that cannot
    be loaded acts on nothing,  so either way the answer is no units.
    """
    try:
        spec = importlib.util.spec_from_file_location("fleet_ops_control", controller)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - a helper that will not load acts on nothing
        return {}
    try:
        # `load_config`, not just `validate_config`: the helper's own size and file checks
        # count too.
        verdict = module.load_config(path)
    except Exception:  # noqa: BLE001 - anything the helper raises is a refusal
        return {}
    return {unit: entry["scope"] for unit, entry in verdict["units"].items()}


def iso_from_systemd(value):
    """A `--timestamp=unix` value (`@1730000000`) as the report spells a time,  or None."""
    match = re.fullmatch(r"@(\d+)", (value or "").strip())
    if not match:
        return None
    seconds = int(match.group(1))
    if seconds <= 0:
        return None
    try:
        return iso_from_timestamp(seconds)
    except (OverflowError, OSError, ValueError):
        return None


def show_units(units, scope, run_command=run):
    """What systemd says about some units in one scope,  by unit,  or None when it could
    not answer.

    `systemctl show` writes one block per unit,  in the order asked,  with a blank line
    between them.
    """
    command = ["systemctl"]
    if scope == "user":
        command.append("--user")
    command += [
        "show",
        "--timestamp=unix",
        "--property=" + ",".join(UNIT_PROPERTIES),
    ]
    command += units
    text = run_command(command)
    if text is None:
        return None
    blocks = []
    current = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, separator, value = line.partition("=")
        if separator:
            current[key] = value
    if current:
        blocks.append(current)
    if len(blocks) != len(units):
        return None
    return dict(zip(units, blocks))


def collect_services(control=DEFAULT_CONTROL, run_command=run):
    """The units the allowlist names,  with their state.

    A scope systemctl could not answer for leaves its units `unknown`,  with nothing
    else measured.
    """
    rows = []
    entries = control_units(control)
    for scope in SCOPES:
        named = [
            (unit, controllable)
            for unit, entry_scope, controllable in entries
            if entry_scope == scope
        ]
        if not named:
            continue
        shown = show_units([unit for unit, _ in named], scope, run_command)
        for unit, controllable in named:
            row = {"unit": unit, "scope": scope}
            props = (shown or {}).get(unit)
            if props is None:
                row["activeState"] = "unknown"
            else:
                if props.get("Description"):
                    row["description"] = props["Description"]
                row["activeState"] = props.get("ActiveState") or "unknown"
                if props.get("SubState"):
                    row["subState"] = props["SubState"]
                since = iso_from_systemd(props.get("StateChangeTimestamp"))
                if since:
                    row["since"] = since
                if props.get("UnitFileState"):
                    row["enabled"] = props["UnitFileState"]
            row["controllable"] = controllable
            rows.append(row)
    return rows


# MARK: - The config files


def is_watched(repo, path):
    """Whether a file is one of the repo's instruction and settings files."""
    try:
        parts = path.relative_to(repo).parts
    except ValueError:
        return False
    if len(parts) == 1:
        return parts[0] in WATCHED_FILES
    if len(parts) == 3 and parts[0] == ".github" and parts[1] == "workflows":
        return parts[2].endswith(".yml")
    if len(parts) == 2 and parts[0] == "config":
        return parts[1].endswith(".json")
    return False


def watched_paths(code_root, limit=200):
    """The config files under the fleet's checkouts,  bounded and filtered.

    The bound is shared out fairly:  every checkout is offered its files in path
    order,  one at a time,  in turn,  so a busy repo never takes the whole budget and
    leaves another repo unseen.
    """
    try:
        repos = sorted(entry for entry in code_root.iterdir() if entry.is_dir())
    except OSError:
        return [], 0
    groups = {}
    for repo in repos:
        found = []
        for root, directories, names in os.walk(repo, followlinks=False):
            directories[:] = sorted(name for name in directories if name not in SKIP_DIRECTORIES)
            for name in sorted(names):
                path = Path(root) / name
                if not is_watched(repo, path):
                    continue
                if is_secret_path(path):
                    continue
                found.append(path)
        if found:
            groups[repo] = found
    total = sum(len(found) for found in groups.values())
    ordered = []
    index = 0
    while len(ordered) < limit:
        added = False
        for repo in sorted(groups):
            found = groups[repo]
            if index < len(found):
                ordered.append(found[index])
                added = True
                if len(ordered) >= limit:
                    break
        if not added:
            break
        index += 1
    return ordered, total


def collect_files(code_root, previous, limit=200):
    """The config files,  each one with its drift against the previous report.

    A file that was in the previous report and is gone now is listed as `missing`:
    a file that is not there is not a zero byte file.  The second value is how many
    files were found,  which is not the number that fit inside the bound.
    """
    rows = []
    present = set()
    paths, total = watched_paths(code_root, limit=limit)
    for path in paths:
        try:
            resolved = path.resolve()
            resolved.relative_to(code_root.resolve())
            if is_secret_path(resolved):
                continue
            info = path.stat()
        except (OSError, ValueError):
            continue
        digest = digest_of(path)
        if digest is None:
            continue
        present.add(str(path))
        rows.append(
            {
                "path": str(path),
                "bytes": info.st_size,
                "modifiedAt": iso_from_timestamp(info.st_mtime),
                "sha256": digest,
                "drift": drift_of(str(path), digest, previous),
            }
        )
    for path in sorted(previous):
        if path in present or is_secret_path(path):
            continue
        candidate = Path(path)
        try:
            candidate.relative_to(code_root)
            candidate.lstat()
            continue
        except ValueError:
            continue
        except OSError as error:
            if error.errno != errno.ENOENT:
                continue
        rows.append({"path": path, "drift": "missing"})
    return rows, total


def drift_of(path, digest, previous):
    """The drift of a file against the previous report."""
    before = previous.get(path)
    if before is None:
        return "new"
    earlier = before.get("sha256")
    if not earlier:
        # It was missing before,  so there is nothing to compare against.
        return "new"
    if earlier == digest:
        return "unchanged"
    # A README or a package manifest moves with ordinary work,  not with config.
    return "info" if Path(path).name in INFO_FILES else "changed"


# MARK: - The report


def previous_files(path):
    """The files of the previous report,  by path.

    A report that cannot be read is an empty previous report:  everything is then
    `new`,  which is what a first run says.
    """
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return {}
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    files = document.get("files")
    if not isinstance(files, list):
        return {}
    previous = {}
    for row in files:
        if not isinstance(row, dict):
            continue
        name = row.get("path")
        if isinstance(name, str) and name:
            previous[name] = row
    return previous


def previous_resources(path):
    """The Coolify resources of the previous report,  by identity."""
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return {}
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(document, dict):
        return {}
    resources = document.get("coolify")
    if not isinstance(resources, list):
        return {}
    previous = {}
    for row in resources:
        if not isinstance(row, dict):
            continue
        uuid = row.get("uuid")
        name = row.get("name")
        if isinstance(uuid, str) and uuid:
            previous[(uuid, name)] = row
    return previous


def build_report(
    coolify,
    processes,
    files,
    previous_resources_map,
    host=None,
    host_metrics=None,
    services=None,
    files_summary=None,
):
    """The report,  as Muster reads it.

    `hostMetrics`,  `services` and `filesSummary` are optional:  a key that was not
    collected is left out,  so a reader built before they existed reads the report as
    it always has.
    """
    resources = []
    seen = set()
    for row in coolify or []:
        key = (row["uuid"], row["name"])
        seen.add(key)
        resources.append(row)
    for key, before in sorted(previous_resources_map.items()):
        if key in seen:
            continue
        # Failed inspection is unknown, not evidence that resources vanished.
        gone = {
            "uuid": key[0],
            "name": key[1],
            "type": before.get("type", "standalone"),
            "status": "unknown" if not getattr(coolify, "complete", True) else "absent",
        }
        # The source is carried over only when the previous report said it;  a row
        # from a report written before `uuidSource` existed is left without one.
        if before.get("uuidSource") in UUID_SOURCES:
            gone["uuidSource"] = before["uuidSource"]
        resources.append(gone)
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "generatedAt": utc_now(),
        "host": host or socket.gethostname(),
        "coolify": resources,
        "processes": processes,
        "files": files,
    }
    if host_metrics is not None:
        document["hostMetrics"] = host_metrics
    if services is not None:
        document["services"] = services
    if files_summary is not None:
        document["filesSummary"] = files_summary
    return document


def summarize_files(found, limit):
    """How many config files were found against the bound."""
    return {"found": found, "limit": limit, "truncated": found > limit}


def write_report(path, document):
    """Atomic and private:  a 0600 temporary file in the same folder,  fsynced,  then
    renamed over.  The folder is created 0700 when it is absent."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temporary = tempfile.mkstemp(
        prefix=".server-status.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        os.fchmod(handle, 0o600)
        with os.fdopen(handle, "w") as output:
            json.dump(document, output, indent=2, sort_keys=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Write the fleet server status report that Muster reads.",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=Path.home(),
        help="the home folder the default report path is under (default: this user's home)",
    )
    parser.add_argument(
        "--code-root",
        type=Path,
        default=DEFAULT_CODE_ROOT,
        help="the folder the fleet's checkouts live in (default: %s)" % DEFAULT_CODE_ROOT,
    )
    parser.add_argument(
        "--control",
        type=Path,
        default=DEFAULT_CONTROL,
        help="the allowlist that names the fleet's units (default: fleet-ops-control.json "
        "next to this script)",
    )
    parser.add_argument(
        "--snapshot",
        type=Path,
        default=None,
        help="the report to read as the previous run (default: the output path)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="where to write the report (default: <home>/.local/state/fleet-tools/server-status.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the report and write nothing",
    )
    parser.add_argument(
        "--process-limit",
        type=int,
        default=15,
        help="how many processes to report (default: 15)",
    )
    parser.add_argument(
        "--cpu-interval",
        type=float,
        default=0.4,
        help="the seconds between the two cpu samples (default: 0.4)",
    )
    parser.add_argument(
        "--file-limit",
        type=int,
        default=200,
        help="the most config files to report (default: 200)",
    )
    return parser.parse_args(argv)


def main(argv=None, run_command=run, proc=Path("/proc")):
    args = parse_args(argv)
    output = args.output or (
        Path(args.home) / ".local" / "state" / "fleet-tools" / "server-status.json"
    )
    snapshot = args.snapshot or output

    coolify = collect_coolify(run_command=run_command)
    processes = collect_processes(limit=args.process_limit, interval=args.cpu_interval, proc=proc)
    files, found = collect_files(args.code_root, previous_files(snapshot), limit=args.file_limit)
    document = build_report(
        coolify,
        processes,
        files,
        previous_resources(snapshot),
        host_metrics=collect_host_metrics(proc=proc),
        services=collect_services(args.control, run_command=run_command),
        files_summary=summarize_files(found, args.file_limit),
    )

    if args.dry_run:
        json.dump(document, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    write_report(output, document)
    print(
        "wrote %s  (%d resources, %d processes, %d of %d config files)"
        % (
            output,
            len(document["coolify"]),
            len(document["processes"]),
            len(document["files"]),
            found,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
