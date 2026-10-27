#!/usr/bin/env python3
"""fleet-server-status:  what the server is doing,  and where its config has drifted.

One JSON report for Muster to read:
`/root/.local/state/fleet-tools/server-status.json`  (mode 0600, written atomically).

  python3 scripts/fleet-server-status.py [--home PATH] [--code-root PATH]
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

A measurement that cannot be taken is omitted,  never reported as a zero:  a container
with no stats row carries no cpu and no memory,  and a file that is not there is
`drift: "missing"`  rather than a zero byte file.

Drift is measured against the previous report:  `new` when the path was not in it,
`changed` when the digest differs,  `missing` when it was there and is gone now,  and
`unchanged` otherwise.  A Coolify resource that was in the previous report and is
gone now reads as `absent`.

The script writes one file and changes nothing else.
"""

from __future__ import annotations

import argparse
import datetime
import errno
import hashlib
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
DEFAULT_CODE_ROOT = Path("/mnt/fleet-extra/Code")

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


# MARK: - Helpers


def utc_now() -> str:
    """The time this report was made,  in the spelling the doctor uses."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_timestamp(value: float) -> str:
    return datetime.datetime.fromtimestamp(value, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def coolify_type(label):
    """The Coolify type of a container,  and `standalone` when it carries no label."""
    value = (label or "").strip().lower()
    return value if value in COOLIFY_TYPES else "standalone"


def resource_identity(fields):
    """The identity of one container,  from its Coolify labels.

    A container with no Coolify label is standalone,  and is named by its own name.
    A Coolify application keeps its uuid across a redeploy,  so the uuid is the
    identity and the name only says which resource it is.
    """
    uuid = fields["application_uuid"].strip()
    resource = fields["resource_name"].strip()
    label_name = fields["label_name"].strip()
    container = fields["container"].strip()
    if not uuid:
        uuid = resource or container
    name = resource or label_name or container
    return uuid, name


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
        uuid, name = resource_identity(fields)
        row = {
            "uuid": uuid,
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
    return "changed" if earlier != digest else "unchanged"


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
):
    """The report,  as Muster reads it."""
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
        resources.append(
            {
                "uuid": key[0],
                "name": key[1],
                "type": before.get("type", "standalone"),
                "status": "unknown" if not getattr(coolify, "complete", True) else "absent",
            }
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "generatedAt": utc_now(),
        "host": host or socket.gethostname(),
        "coolify": resources,
        "processes": processes,
        "files": files,
    }


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


def main(argv=None):
    args = parse_args(argv)
    output = args.output or (
        Path(args.home) / ".local" / "state" / "fleet-tools" / "server-status.json"
    )
    snapshot = args.snapshot or output

    coolify = collect_coolify()
    processes = collect_processes(limit=args.process_limit, interval=args.cpu_interval)
    files, found = collect_files(args.code_root, previous_files(snapshot), limit=args.file_limit)
    document = build_report(
        coolify,
        processes,
        files,
        previous_resources(snapshot),
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
