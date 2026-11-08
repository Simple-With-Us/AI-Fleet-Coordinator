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
    means no units,  never a crash.  `server.watch` names more units to read and grants
    nothing:  a watched row has no actions.  `actions` on a row is what the helper would
    really do for it.  A timer also says when it last ran,  how its job ended and when it
    runs next.
  * `failedUnits` is what `systemctl --failed` says in both scopes,  so a failed unit
    shows even when nothing names it.  The key is left out when neither scope answered.
  * `belongsTo` on a process comes from `/proc/<pid>/cgroup`:  the container or the unit
    it runs in.
  * `collection` says,  in fixed codes,  which half could not be read,  and `hostRole` says
    which kind of machine wrote the report.

A measurement that cannot be taken is omitted,  never reported as a zero:  a container
with no stats row carries no cpu and no memory,  and a file that is not there is
`drift: "missing"`  rather than a zero byte file.

Drift is measured against the previous report:  `new` when the path was not in it,
`changed` when the digest differs,  `missing` when it was there and is gone now,  and
`unchanged` otherwise.  A Coolify resource that was in the previous report and is
gone now reads as `absent`.  Each Coolify row says where its `uuid` came from:
`uuidSource` is `label` for a Coolify uuid label and `name` for a stand-in name.  A change to a `README.md` or a `package.json` is
`info` rather than `changed`:  those files move with ordinary work,  not with config.
`filesSummary` says how many files were found against the bound,  so a reader can tell
a partial list from a whole one.

`drift` only says what moved since the previous report,  about fifteen minutes ago.  Two more
fields on each present file row say how the file stands against what is intended:

  * `intent` compares the file with its checkout's own `origin/main` blob,  read-only and with
    no fetch:  `clean` when they match;  `behind` when the file is what the checkout's HEAD
    holds and HEAD is an ancestor of `origin/main`,  so the checkout has not caught up;  and
    `localEdit` otherwise,  which is an uncommitted edit,  an untracked file or a local commit.
    `unknown` means it could not be told,  and `intentReason` then says why in a fixed code:
    `not_a_checkout`,  `no_origin_main`,  `git_failed` or `git_budget`.  It is never guessed
    to be `clean`.
  * `changedAt` is when the file's content last changed:  the file's modification time when
    the collector first saw this content,  carried from report to report for as long as the
    digest stays the same.  A touch that leaves the content alone does not move it.

`hostConfig` is the host's own config surface,  by digest and never by content.  It holds the
installed unit files and drop-ins of the units the allowlist names,  watches or updates and of
the units this repo ships (`UNIT_SOURCE_FOLDERS`),  where systemd's own `FragmentPath` and
`DropInPaths` say they are;  the resource broker's folder,  where
`scripts/hetzner/install-resource-control.sh` installs it;  the allowlist installed in that
folder;  and the allowlist this run read,  when it is not the one next to this script.  Each row
has `kind` (`unit`,  `dropIn`,  `broker` or `allowlist`),  `path` and `state` (`present`,
`missing`,  `unreadable`,  or `absent` for a broker folder that is not there).  A present row
adds `bytes`,  `modifiedAt`,  `sha256` and a sticky `changedAt`.  `source` names the repo file
the row was installed from,  relative to the checkout this script runs from,  and
`matchesSource` says whether the two digests agree.  A unit or drop-in row also names its `unit`
and `scope`.

Nothing stays forever because it once existed.  An `absent` row carries `absentSince` and a
`missing` row carries `missingSince`,  and each is dropped after seven days.  A resource's
identity is its uuid and its real compose service,  never a per-deploy container name,  so a
redeploy leaves nothing behind.  A throwaway container that carries no Coolify label leaves
no row at all.  When a whole checkout has been removed,  its files become one line in
`removedCheckouts`,  which is information and not a failure.

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
# How long a leftover row is kept:  an `absent` resource,  a `missing` file and a line about
# a removed checkout are each dropped this long after they were first seen gone.
LEFTOVER_KEEP_SECONDS = 7 * 24 * 60 * 60
# The names a throwaway container gets when nothing names it:  Docker's own adjective_noun,
# and the 24-character id Coolify gives a helper container.
THROWAWAY_NAME_RE = re.compile(r"[a-z]+_[a-z0-9]+|[a-z0-9]{24}")
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
        "{{.RestartCount}}",
        '{{index .State "ExitCode"}}',
        '{{index .State "OOMKilled"}}',
        "{{.HostConfig.RestartPolicy.Name}}",
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
# The checkout this script runs from:  the repo that ships the units and the broker's code.
SOURCE_ROOT = Path(__file__).resolve().parent.parent
# Git is only asked read-only questions,  it never fetches,  and each call has a time limit.  The
# whole run's git work has one too,  and a checkout reached after it reads `unknown`.
GIT_TIMEOUT_SECONDS = 10
GIT_BUDGET_SECONDS = 60
INTENT_REF = "refs/remotes/origin/main"
# Where git is looked for:  the server's own folders,  and Homebrew's on a Mac.
GIT_PATH = "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"
# The largest file that is hashed.
FILE_READ_LIMIT = 4 * 1024 * 1024
# Where scripts/hetzner/install-resource-control.sh installs the resource broker,  and the files
# it writes there.  Its `fleet_secrets` package is never listed,  because that path looks like a
# credential.
BROKER_DIR = Path("/usr/local/lib/fleet-resource-control")
BROKER_FILES = (
    "fleet-ops-control.py",
    "fleet-ops-control.json",
    "fleet_resource_control.py",
    "fleet_seat_inventory.py",
    "hetzner/__init__.py",
    "hetzner/fleet_coding_memory.py",
    "hetzner/coding_resources.py",
)
BROKER_ALLOWLIST = "fleet-ops-control.json"
# The folders of this repo that ship systemd units,  relative to the checkout.
UNIT_SOURCE_FOLDERS = (
    "scripts/systemd",
    "scripts/host",
    "scripts/hetzner",
    "scripts/agent_sync/server",
)
# The properties read to find a unit's installed files,  and nothing else.
UNIT_FILE_PROPERTIES = ("Id", "LoadState", "FragmentPath", "DropInPaths", "Triggers")
SCOPES = ("system", "user")
# A unit name is passed to systemctl as an argument,  so it must look like one and never
# like an option.
UNIT_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9@._:\-]{0,200}\.(?:service|timer|socket|path|target|mount)"
)
# A failed unit can be of any kind,  and an instance name can hold systemd's own `\\x2d`.
FAILED_UNIT_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9@._:\\\-]{0,200}"
    r"\.(?:service|timer|socket|path|target|mount|automount|swap|slice|scope|device)"
)
# The properties read for each unit,  and nothing else.  The last four only a timer has.
UNIT_PROPERTIES = (
    "Id",
    "Description",
    "LoadState",
    "ActiveState",
    "SubState",
    "UnitFileState",
    "StateChangeTimestamp",
    "ActiveEnterTimestamp",
    "Triggers",
    "LastTriggerUSec",
    "NextElapseUSecRealtime",
    "NextElapseUSecMonotonic",
)
# The properties read for the job a timer starts.
JOB_PROPERTIES = (
    "Id",
    "ActiveState",
    "SubState",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "ExecMainStartTimestamp",
    "ExecMainExitTimestamp",
)
# The properties read for a unit `systemctl --failed` names.
FAILED_PROPERTIES = (
    "Id",
    "Description",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "StateChangeTimestamp",
)
# systemd's `ExecMainCode` for a process that exited on its own (`CLD_EXITED`);  2 is killed by
# a signal and 3 is a core dump.
CLD_EXITED = "1"
# The timer that starts this collector.  While the collector runs that timer always reads
# `running`,  which says only that the collector is running.
SELF_TIMER = "fleet-server-status.timer"
UNIT_ROLES = ("reporting", "updater")
STOP_CONSEQUENCE_LIMIT = 200
# systemd's time span words,  in seconds.
TIMESPAN_SECONDS = {
    "y": 31557600.0,
    "month": 2629800.0,
    "w": 604800.0,
    "d": 86400.0,
    "h": 3600.0,
    "min": 60.0,
    "s": 1.0,
    "ms": 0.001,
    "us": 0.000001,
}

# The flag file a package upgrade leaves when the kernel or a core library needs a reboot.
REBOOT_FLAG = Path("/run/reboot-required")

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


def seconds_from_report_time(text):
    """The moment a time this report wrote names,  in seconds,  or None when it names none."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return moment.replace(tzinfo=datetime.timezone.utc).timestamp()


def whole_second_utc(text):
    """An RFC 3339 time in the report's one spelling,  or None when it is not one.

    Docker writes up to nine fractional digits,  and a reader should not have to know that.
    """
    match = re.fullmatch(
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})",
        (text or "").strip(),
    )
    if not match:
        return None
    zone = "+00:00" if match.group(2) == "Z" else match.group(2)
    try:
        moment = datetime.datetime.fromisoformat(match.group(1) + zone)
    except ValueError:
        return None
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def host_role(platform=None):
    """Which kind of machine wrote this report:  `mac` on Darwin,  otherwise `server`."""
    return "mac" if (platform or sys.platform) == "darwin" else "server"


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


def read_bounded(path, limit=FILE_READ_LIMIT):
    """A regular file's bytes,  or None when it is not one,  is a link,  is too large or cannot be
    read.  The bytes are only ever hashed:  they never reach the report."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            data = handle.read(limit + 1)
        return None if len(data) > limit else data
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def short_digest(data):
    """The first sixteen hex characters of the sha256 of some bytes."""
    return hashlib.sha256(data).hexdigest()[:16]


def digest_of(path):
    """The first sixteen hex characters of a file's digest,  or None when it cannot be read."""
    data = read_bounded(path)
    return None if data is None else short_digest(data)


def blob_ids(data):
    """The id git gives these bytes as a blob,  for each of git's two object formats.

    Computed here rather than by `git hash-object`,  so no filter a checkout configures is
    ever run.
    """
    header = b"blob %d\x00" % len(data)
    return {
        "sha1": hashlib.sha1(header + data).hexdigest(),
        "sha256": hashlib.sha256(header + data).hexdigest(),
    }


def changed_at(before, digest, modified, moment):
    """When a file's content last changed,  carried across reports.

    While the digest is the one the previous report saw,  its `changedAt` is kept.  Otherwise
    the content is new to the collector,  and its modification time is the best evidence of
    when it was written:  never later than now.
    """
    if isinstance(before, dict) and before.get("sha256") == digest:
        kept = before.get("changedAt")
        if seconds_from_report_time(kept) is not None:
            return kept
    return iso_from_timestamp(min(modified, moment))


# MARK: - Coolify


def container_status(state, health, exit_code="", restart_policy=""):
    """The status of a container,  in the vocabulary the report uses.

    A state this collector does not know is `unknown`:  an unreadable state is never
    a running one.  A crash loop is `restarting`.  A clean exit from a container nothing was
    asked to keep alive is `finished`,  which is a one-shot job that did its work and not a
    failure.  `on-failure` does ask docker to keep it alive,  so its clean exit stays `exited`:
    that service is down,  and nothing here says it was ever a one-shot job.
    """
    if state == "running":
        if health == "unhealthy":
            return "unhealthy"
        return "running"
    if state == "restarting":
        return "restarting"
    if state == "exited" and exit_code == "0" and restart_policy == "no":
        return "finished"
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


def real_service(compose_service, uuid, container):
    """The compose service that names a part of an application,  or an empty string.

    Coolify fills the compose label of a one-container application with the application
    uuid or the container name,  which can hold a per-deploy stamp.  Neither one says which
    part of the application this is,  and either would mint a new identity on each deploy.
    """
    service = (compose_service or "").strip()
    if not service or service == container or service == uuid:
        return ""
    if service.startswith(uuid + "-"):
        return ""
    return service


class ResourceRows(list):
    complete = True
    # The fixed code for what could not be read,  when something could not.
    error_code = None
    partial = False
    # The full container id to the row's name,  so a process can say what it belongs to.
    names_by_id = {}


def collect_coolify(run_command=run):
    """Every container on the host,  with what Docker knows about it.

    A container that cannot be inspected is left out:  a measurement that cannot be
    taken is omitted,  never reported as a zero.
    """
    listed = run_command(["docker", "ps", "-a", "--format", "{{.ID}}"])
    if listed is None:
        rows = ResourceRows()
        rows.complete = False
        rows.error_code = "docker_unreadable"
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
        rows.error_code = "docker_inspect_failed"
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
    by_id = {}
    if not complete:
        rows.error_code = "docker_inspect_partial"
        rows.partial = True
    elif stats_text is None:
        # The rows are all here,  and only their cpu and memory were not read.
        rows.error_code = "docker_stats_unreadable"
        rows.partial = True
    for line in inspected.splitlines():
        parts = line.split("\x1f")
        if len(parts) < 13:
            rows.complete = False
            rows.error_code = "docker_inspect_partial"
            rows.partial = True
            continue
        # A docker answer from before these fields were asked for reads as not measured.
        extras = (parts[13:] + ["", "", "", ""])[:4]
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
            "compose_service": label_value(parts[12]),
            "restart_count": label_value(extras[0]),
            "exit_code": label_value(extras[1]),
            "oom_killed": label_value(extras[2]),
            "restart_policy": label_value(extras[3]),
        }
        uuid, name, source = resource_identity(fields)
        row = {
            "uuid": uuid,
            "uuidSource": source,
            "name": name,
            "type": coolify_type(fields["label_type"]),
            "status": container_status(
                fields["state"], fields["health"], fields["exit_code"], fields["restart_policy"]
            ),
            "container": fields["container"],
        }
        if fields["id"]:
            by_id[fields["id"]] = row
        if fields["image"]:
            row["image"] = fields["image"]
        updated = fields["started"]
        if fields["state"] in ("exited", "dead") and fields["finished"]:
            updated = fields["finished"]
        if updated and not updated.startswith("0001-01-01"):
            spelled = whole_second_utc(updated)
            if spelled:
                row["updatedAt"] = spelled
        if fields["state"] == "running":
            # `none` says no check exists,  so a row with no check cannot look proven.
            row["health"] = fields["health"] or "none"
        if fields["restart_policy"]:
            row["restartPolicy"] = fields["restart_policy"]
        if fields["restart_count"].isdigit():
            row["restartCount"] = int(fields["restart_count"])
        if fields["state"] in ("exited", "dead"):
            if fields["exit_code"].lstrip("-").isdigit():
                row["exitCode"] = int(fields["exit_code"])
            if fields["oom_killed"] in ("true", "false"):
                row["oomKilled"] = fields["oom_killed"] == "true"
        cpu, memory = stats.get(fields["container"], (None, None))
        if cpu is not None:
            row["cpuPercent"] = cpu
        if memory is not None:
            row["memoryBytes"] = memory
        row["_service"] = real_service(fields["compose_service"], uuid, fields["container"])
        rows.append(row)

    disambiguate(rows)
    # Named only now,  because the service has just been added to the row's name.
    rows.names_by_id = {identifier: row["name"] for identifier, row in by_id.items()}
    return rows


def disambiguate(rows):
    """Give two containers of one application a name each.

    The Coolify labels carry the application's uuid and the resource name,  and both
    are the same for every service of that application,  so the service name is added
    to keep one row per container.  The row keeps the service on its own as well,  because
    the uuid and the service together are what identify it from one run to the next.
    """
    for row in rows:
        service = row.pop("_service", None)
        if not service:
            continue
        row["service"] = service
        if service != row["name"]:
            row["name"] = "%s (%s)" % (row["name"], service)
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


def process_home(pid, proc=Path("/proc"), containers=None):
    """The container or the systemd unit a process runs in,  or None when it says neither.

    `/proc/<pid>/cgroup` is readable for every process,  and it names no argument.
    """
    text = read_small(proc / str(pid) / "cgroup")
    if not text:
        return None
    path = text.strip().splitlines()[-1].rpartition(":")[2]
    match = re.search(r"docker[-/]([0-9a-f]{64})", path)
    if match:
        return (containers or {}).get(match.group(1)) or "a container"
    services = [part for part in path.split("/") if part.endswith(".service")]
    return services[-1] if services else None


def collect_processes(limit=15, interval=2.0, proc=Path("/proc"), containers=None):
    """The busiest processes on the host.

    The cpu of a process is the difference between two samples,  so the whole host is
    sampled twice,  once,  rather than each process in turn.  The samples are `interval`
    seconds apart,  which is the reading's own quantum:  at two seconds,  half a percent.
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
    busiest = rows[:limit]
    for row in busiest:
        home = process_home(row["pid"], proc, containers)
        if home:
            row["belongsTo"] = home
    return busiest


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
    usage = {
        "mount": mount,
        "totalBytes": total,
        "usedBytes": max(0, (info.f_blocks - info.f_bfree) * size),
        "freeBytes": max(0, info.f_bavail * size),
    }
    # A file system with no inode count (btrfs says zero) has no inode reading to give.
    if info.f_files > 0:
        usage["inodesTotal"] = info.f_files
        usage["inodesFree"] = max(0, info.f_favail)
    return usage


def booted_at(proc=Path("/proc")):
    """When the host booted,  from `btime` in `/proc/stat`,  or None when it cannot be read."""
    match = re.search(r"^btime (\d+)$", read_small(proc / "stat") or "", re.MULTILINE)
    if not match or int(match.group(1)) <= 0:
        return None
    return iso_from_timestamp(int(match.group(1)))


def collect_host_metrics(
    proc=Path("/proc"), statvfs=os.statvfs, cpu_count=os.cpu_count, reboot_flag=None
):
    """The host's load,  memory,  swap,  uptime and disks,  and when it booted.

    Each measurement that cannot be taken is left out,  never reported as a zero.
    `rebootRequired` is written only when the flag file says so:  its absence proves
    nothing on a system that does not write one.
    """
    metrics = {}
    booted = booted_at(proc)
    if booted:
        metrics["bootedAt"] = booted
    if reboot_flag is not None and os.path.exists(reboot_flag):
        metrics["rebootRequired"] = True
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


def control_server_section(path):
    """The `server` section of the allowlist,  or None when it cannot be read."""
    try:
        if path.stat().st_size > CONTROL_LIMIT:
            return None
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict):
        return None
    server = document.get("server")
    return server if isinstance(server, dict) else None


def control_units(path=DEFAULT_CONTROL, controller=DEFAULT_CONTROLLER):
    """The units the allowlist names,  as (unit, scope, controllable).

    A missing,  unreadable or malformed allowlist names no units.  An entry with a bad
    unit name or scope is skipped,  and a unit named twice in one scope is kept once.
    `controllable` is the control helper's own verdict (`accepted_units`),  so the report
    never offers a unit the helper would refuse.
    """
    server = control_server_section(path)
    if server is None:
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


def helper_verdict(path, controller=DEFAULT_CONTROLLER):
    """The allowlist as the control helper itself reads it,  or None when it refuses.

    The helper's own `load_config` decides,  so this reader can never be wider than
    it.  The helper refuses the whole file for one bad entry,  and a helper that cannot
    be loaded acts on nothing,  so either way the answer is None.
    """
    try:
        spec = importlib.util.spec_from_file_location("fleet_ops_control", controller)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:  # noqa: BLE001 - a helper that will not load acts on nothing
        return None
    try:
        # `load_config`, not just `validate_config`: the helper's own size and file checks
        # count too.
        return module.load_config(path)
    except Exception:  # noqa: BLE001 - anything the helper raises is a refusal
        return None


def accepted_units(path, controller=DEFAULT_CONTROLLER):
    """{unit: scope} for the units the control helper accepts in the allowlist at `path`."""
    verdict = helper_verdict(path, controller)
    if verdict is None:
        return {}
    return {unit: entry["scope"] for unit, entry in verdict["units"].items()}


def unit_actions(path, controller=DEFAULT_CONTROLLER):
    """{unit: the verbs the control helper would really run for it}.

    `logs` is in the list because the helper reads the journal of any unit it accepts.
    """
    verdict = helper_verdict(path, controller)
    if verdict is None:
        return {}
    return {unit: list(entry["actions"]) + ["logs"] for unit, entry in verdict["units"].items()}


def redeploy_allowed(path, controller=DEFAULT_CONTROLLER):
    """Whether the control helper would redeploy a Coolify application at all."""
    verdict = helper_verdict(path, controller)
    return bool(verdict and verdict.get("redeploy"))


def watched_units(path):
    """The units `server.watch` names,  as (unit, scope):  read,  and granted nothing.

    An entry with a bad unit name or scope is skipped.  The helper never reads this key,
    so naming a unit here can never make it controllable.
    """
    server = control_server_section(path)
    entries = server.get("watch") if server else None
    if not isinstance(entries, list):
        return []
    units = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        unit, scope = entry.get("unit"), entry.get("scope")
        if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit) or scope not in SCOPES:
            continue
        if (unit, scope) not in units:
            units.append((unit, scope))
    return units


def unit_notes(path):
    """{(unit, scope): {role, stopConsequence}} from the allowlist's `server.units` entries.

    A value of the wrong shape is left out rather than carried into the report.
    """
    server = control_server_section(path)
    entries = server.get("units") if server else None
    if not isinstance(entries, list):
        return {}
    notes = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        note = {}
        if entry.get("role") in UNIT_ROLES:
            note["role"] = entry["role"]
        consequence = entry.get("stopConsequence")
        if isinstance(consequence, str) and 0 < len(consequence) <= STOP_CONSEQUENCE_LIMIT:
            note["stopConsequence"] = consequence
        if note:
            notes[(entry.get("unit"), entry.get("scope"))] = note
    return notes


def services_problem(path, controller=DEFAULT_CONTROLLER):
    """The fixed code for why the allowlist gave no units,  or None when it was read."""
    if control_server_section(path) is None:
        return "allowlist_unreadable"
    if helper_verdict(path, controller) is None:
        return "allowlist_refused"
    return None


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


def parse_timespan(text):
    """A systemd time span (`1h 47min 34.737538s`) in seconds,  or None when it names none.

    `0` and `infinity` both mean that nothing is scheduled.
    """
    cleaned = (text or "").strip()
    number = r"\d+(?:\.\d+)?"
    word = r"month|min|ms|us|y|w|d|h|s"
    if not cleaned or re.sub(r"(?:%s)(?:%s)|\s" % (number, word), "", cleaned):
        return None
    total = sum(
        float(value) * TIMESPAN_SECONDS[unit]
        for value, unit in re.findall(r"(%s)(%s)" % (number, word), cleaned)
    )
    return total if total > 0 else None


def next_run_at(props, clock=time):
    """When a timer next elapses,  or None when systemd says nothing is scheduled.

    A calendar timer gives a wall clock time.  A boot or unit relative timer gives a time on
    the monotonic clock,  which is moved onto the wall clock here.  A timer with both runs
    at whichever comes first.
    """
    candidates = []
    calendar = seconds_from_report_time(iso_from_systemd(props.get("NextElapseUSecRealtime")))
    if calendar is not None:
        candidates.append(calendar)
    relative = parse_timespan(props.get("NextElapseUSecMonotonic"))
    if relative is not None:
        candidates.append(clock.time() + relative - clock.monotonic())
    if not candidates:
        return None
    return iso_from_timestamp(int(min(candidates)))


def job_of(props):
    """The service a timer starts,  from systemd's own `Triggers`."""
    for name in (props.get("Triggers") or "").split():
        if name.endswith(".service") and UNIT_RE.fullmatch(name):
            return name
    return None


def exit_code_of(props):
    """The exit code a unit's main process really returned,  or None.

    `ExecMainStatus` holds an exit code only when `ExecMainCode` says the process exited.  When
    the process was killed the same field holds the signal number,  so a stop that timed out
    has 15 there,  which is SIGTERM and not an exit 15.
    """
    status = props.get("ExecMainStatus") or ""
    if props.get("ExecMainCode") == CLD_EXITED and status.isdigit() and status != "0":
        return int(status)
    return None


def timer_fields(props, job, clock=time):
    """What a timer's row adds:  when it last ran,  how that ended and when it runs next.

    A timer stays `active` while its job fails,  so the timer alone proves only that the
    schedule exists.  `lastResult` is the job's own,  and it is left out when the job has not
    run since boot or is running now:  systemd then still says `success`,  which is a default
    and not an outcome.  `lastRunAt` is the later of the timer's trigger and the job's own
    start,  because something other than the timer can start the job.
    """
    fields = {}
    last = iso_from_systemd(props.get("LastTriggerUSec"))
    if last:
        fields["lastRunAt"] = last
    upcoming = next_run_at(props, clock)
    if upcoming:
        fields["nextRunAt"] = upcoming
    name = job_of(props)
    if name:
        fields["job"] = name
    if not job:
        return fields
    started = iso_from_systemd(job.get("ExecMainStartTimestamp"))
    # Both are whole-second UTC in one spelling,  so they compare as text.
    if started and started > fields.get("lastRunAt", ""):
        fields["lastRunAt"] = started
    state, sub = job.get("ActiveState"), job.get("SubState")
    if state in ("activating", "deactivating", "reloading") or (
        state == "active" and sub != "exited"
    ):
        fields["jobRunning"] = True
        return fields
    result = job.get("Result")
    if result and (started or result != "success"):
        fields["lastResult"] = result
        finished = iso_from_systemd(job.get("ExecMainExitTimestamp"))
        if finished:
            fields["lastFinishedAt"] = finished
        code = exit_code_of(job)
        if result != "success" and code is not None:
            fields["lastExitCode"] = code
    return fields


def show_units(units, scope, run_command=run, properties=UNIT_PROPERTIES):
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
        "--property=" + ",".join(properties),
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


def show_jobs(shown, scope, run_command=run):
    """{timer: what systemd says about the service it starts},  for the timers in `shown`.

    A scope that cannot answer leaves its timers without job fields,  never with a pass.
    """
    timers = {}
    for unit, props in (shown or {}).items():
        job = job_of(props) if unit.endswith(".timer") else None
        if job:
            timers[unit] = job
    if not timers:
        return {}
    jobs = sorted(set(timers.values()))
    answered = show_units(jobs, scope, run_command, properties=JOB_PROPERTIES)
    if answered is None:
        return {}
    return {unit: answered.get(job) for unit, job in timers.items()}


def unit_since(unit, props):
    """How long a unit has been in its state.

    A timer's state changes each time it fires,  so its own state change only says when it
    last ran,  and an active timer has been active since it was activated.
    """
    if unit.endswith(".timer") and props.get("ActiveState") == "active":
        entered = iso_from_systemd(props.get("ActiveEnterTimestamp"))
        if entered:
            return entered
    return iso_from_systemd(props.get("StateChangeTimestamp"))


def collect_services(control=DEFAULT_CONTROL, run_command=run, clock=time):
    """The units the allowlist names and the units it watches,  with their state.

    A scope systemctl could not answer for leaves its units `unknown`,  with nothing
    else measured.  `actions` is what the helper would really run,  so a watched row and a
    row the helper refuses have none.  A watched unit this host does not have is left out,
    because a watch list is not a claim that the unit should exist here.
    """
    rows = []
    entries = control_units(control)
    named_units = {unit for unit, _scope, _controllable in entries}
    watched = [item for item in watched_units(control) if item[0] not in named_units]
    verbs = unit_actions(control) if entries else {}
    notes = unit_notes(control) if entries else {}
    for scope in SCOPES:
        named = [
            (unit, controllable, False)
            for unit, entry_scope, controllable in entries
            if entry_scope == scope
        ]
        named += [(unit, False, True) for unit, entry_scope in watched if entry_scope == scope]
        if not named:
            continue
        shown = show_units([unit for unit, _, _ in named], scope, run_command)
        jobs = show_jobs(shown, scope, run_command)
        for unit, controllable, is_watched in named:
            row = {"unit": unit, "scope": scope}
            props = (shown or {}).get(unit)
            if props is None:
                row["activeState"] = "unknown"
            elif is_watched and props.get("LoadState") == "not-found":
                continue
            else:
                if props.get("Description"):
                    row["description"] = props["Description"]
                row["activeState"] = props.get("ActiveState") or "unknown"
                if props.get("SubState"):
                    row["subState"] = props["SubState"]
                since = unit_since(unit, props)
                if since:
                    row["since"] = since
                if props.get("UnitFileState"):
                    row["enabled"] = props["UnitFileState"]
                if unit.endswith(".timer"):
                    row.update(timer_fields(props, jobs.get(unit), clock))
                if unit == SELF_TIMER and row.get("subState") == "running":
                    # This very run is why it reads `running`.  Between runs it waits,  and
                    # the report being written at all is this run's result.
                    row["subState"] = "waiting"
                    row["selfObserved"] = True
                    row.pop("jobRunning", None)
            row["controllable"] = controllable
            row["actions"] = verbs.get(unit, []) if controllable else []
            if is_watched:
                row["watched"] = True
            row.update(notes.get((unit, scope), {}))
            rows.append(row)
    return rows


def collect_failed_units(run_command=run):
    """What `systemctl --failed` says in both scopes,  and which scopes answered.

    The first value is None when no scope answered:  that is not the same as nothing failed.
    """
    rows = []
    answered = []
    for scope in SCOPES:
        command = ["systemctl"]
        if scope == "user":
            command.append("--user")
        command += ["list-units", "--failed", "--all", "--plain", "--no-legend", "--no-pager"]
        text = run_command(command)
        if text is None:
            continue
        answered.append(scope)
        units = []
        for line in text.splitlines():
            fields = line.split()
            if fields and FAILED_UNIT_RE.fullmatch(fields[0]) and fields[0] not in units:
                units.append(fields[0])
        if not units:
            continue
        shown = show_units(units, scope, run_command, properties=FAILED_PROPERTIES)
        for unit in units:
            row = {"unit": unit, "scope": scope}
            props = (shown or {}).get(unit) or {}
            if props.get("Description"):
                row["description"] = props["Description"]
            since = iso_from_systemd(props.get("StateChangeTimestamp"))
            if since:
                row["since"] = since
            if props.get("Result"):
                row["result"] = props["Result"]
            code = exit_code_of(props)
            if code is not None:
                row["exitCode"] = code
            rows.append(row)
    return (rows if answered else None), answered


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


def files_in(folder):
    """The sorted paths of the entries directly in a folder that are not folders.

    A folder that is not there,  or that is a link,  holds nothing here:  the checkouts are
    read where they are,  and never through a link out of them.
    """
    try:
        if folder.is_symlink():
            return []
        with os.scandir(folder) as entries:
            return sorted(folder / entry.name for entry in entries if not entry.is_dir())
    except OSError:
        return []


def repo_config_files(repo):
    """The watched files of one checkout,  read from the only three places they can be.

    Config comes first and the informational files last,  so a bound spends itself on the
    files that are config.
    """
    workflows = repo / ".github" / "workflows"
    candidates = files_in(repo) + ([] if (repo / ".github").is_symlink() else files_in(workflows))
    candidates += files_in(repo / "config")
    found = [path for path in candidates if is_watched(repo, path) and not is_secret_path(path)]
    return sorted(found, key=lambda path: path.name in INFO_FILES)


class PathList(list):
    # The checkouts the bound cut short.
    cut = ()


def watched_paths(code_root, limit=200):
    """The config files under the fleet's checkouts,  bounded and filtered.

    The bound is shared out fairly:  every checkout is offered its files in path
    order,  one at a time,  in turn,  so a busy repo never takes the whole budget and
    leaves another repo unseen.
    """
    try:
        repos = sorted(entry for entry in code_root.iterdir() if entry.is_dir())
    except OSError:
        return PathList(), 0
    groups = {}
    for repo in repos:
        found = repo_config_files(repo)
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
    result = PathList(ordered)
    kept = set(ordered)
    result.cut = sorted(
        repo.name for repo, found in groups.items() if any(path not in kept for path in found)
    )
    return result, total


class FileRows(list):
    # {checkout name: how many watched files it took with it} for the checkouts removed whole.
    removed = {}
    # False when the code folder itself cannot be read or holds no checkout,  which says
    # nothing about any one checkout.
    root_readable = True
    cut = ()


def code_root_readable(code_root):
    """Whether the code folder holds any checkout.

    An unmounted volume leaves an empty folder behind,  and that must read as a failure
    rather than as every checkout having been removed on purpose.
    """
    try:
        return any(entry.is_dir() for entry in code_root.iterdir())
    except OSError:
        return False


def collect_files(code_root, previous, limit=200, now=None, git=None):
    """The config files,  each one with its drift against the previous report.

    A file that was in the previous report and is gone now is listed as `missing`:
    a file that is not there is not a zero byte file.  The second value is how many
    files were found,  which is not the number that fit inside the bound.

    A `missing` row says since when,  and it is dropped after `LEFTOVER_KEEP_SECONDS`.  When
    the whole checkout is gone there is no row per file:  the checkout is counted in
    `rows.removed`,  because removing a checkout is not a failure of its files.
    """
    # A present row also carries its `intent` against its checkout's `origin/main`,  asked of
    # `git` (by default `git_output`),  and its sticky `changedAt`.
    moment = time.time() if now is None else now
    rows = FileRows()
    rows.removed = {}
    rows.root_readable = code_root_readable(code_root)
    present = set()
    contents = {}
    paths, total = watched_paths(code_root, limit=limit)
    rows.cut = paths.cut
    for path in paths:
        try:
            resolved = path.resolve()
            resolved.relative_to(code_root.resolve())
            if is_secret_path(resolved):
                continue
            info = path.stat()
        except (OSError, ValueError):
            continue
        data = read_bounded(path)
        if data is None:
            continue
        digest = short_digest(data)
        present.add(str(path))
        contents[str(path)] = blob_ids(data)
        rows.append(
            {
                "path": str(path),
                "bytes": info.st_size,
                "modifiedAt": iso_from_timestamp(info.st_mtime),
                "sha256": digest,
                "drift": drift_of(str(path), digest, previous),
                "changedAt": changed_at(previous.get(str(path)), digest, info.st_mtime, moment),
            }
        )
    intents = file_intents(code_root, contents, git or git_output)
    for row in rows:
        intent, reason = intents.get(row["path"], ("unknown", "git_failed"))
        row["intent"] = intent
        if reason:
            row["intentReason"] = reason
    for path in sorted(previous):
        if path in present or is_secret_path(path):
            continue
        candidate = Path(path)
        try:
            relative = candidate.relative_to(code_root)
            candidate.lstat()
            continue
        except ValueError:
            continue
        except OSError as error:
            if error.errno != errno.ENOENT:
                continue
        checkout = code_root / relative.parts[0]
        if rows.root_readable and len(relative.parts) > 1 and not os.path.lexists(checkout):
            rows.removed[relative.parts[0]] = rows.removed.get(relative.parts[0], 0) + 1
            continue
        since = leftover_since(previous[path].get("missingSince"), moment)
        if since is None:
            continue
        rows.append({"path": path, "drift": "missing", "missingSince": since})
    return rows, total


def leftover_since(text, moment):
    """The time a leftover was first seen gone,  or None when its time is up.

    A leftover with no readable time,  or one from the future,  was first seen gone now.
    """
    since = seconds_from_report_time(text)
    if since is None or since > moment:
        return iso_from_timestamp(moment)
    if moment - since >= LEFTOVER_KEEP_SECONDS:
        return None
    return text


def merge_removed_checkouts(found, previous, code_root, now=None):
    """One line for each checkout removed in the last seven days.

    A line stays only while the checkout stays gone,  and it keeps the time it was first
    noticed,  so it ages out.  `found` is `FileRows.removed` from this run,  and `previous` is
    the list the previous report carried.
    """
    moment = time.time() if now is None else now
    notes = {}
    for note in previous if isinstance(previous, list) else []:
        if not isinstance(note, dict):
            continue
        repo, files = note.get("repo"), note.get("files")
        if not isinstance(repo, str) or not repo or "/" in repo or repo in (".", ".."):
            continue
        if not isinstance(files, int) or os.path.lexists(code_root / repo):
            continue
        noticed = leftover_since(note.get("noticedAt"), moment)
        if noticed:
            notes[repo] = {"repo": repo, "files": files, "noticedAt": noticed}
    for repo, count in found.items():
        if repo in notes:
            notes[repo]["files"] += count
        else:
            notes[repo] = {"repo": repo, "files": count, "noticedAt": iso_from_timestamp(moment)}
    return [notes[repo] for repo in sorted(notes)]


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


# MARK: - What is intended


def git_output(repo, arguments, timeout=GIT_TIMEOUT_SECONDS):
    """(exit status, stdout bytes) of one read-only git question about `repo`,  or None when
    git could not be run or ran out of time.

    Nothing here writes or fetches.  Optional locks are off,  so not even the index is
    refreshed;  fsmonitor,  which can start a program,  is off;  pathspecs are literal;  git never
    prompts.  Git gets a fixed environment rather than this process's own,  which the collector
    never reads,  so no `GIT_` variable can point it somewhere else.
    """
    environment = {
        "PATH": GIT_PATH,
        "HOME": str(Path.home()),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LITERAL_PATHSPECS": "1",
        "LC_ALL": "C",
    }
    command = ["git", "--no-pager", "-C", str(repo), "-c", "core.fsmonitor=false"]
    try:
        result = subprocess.run(
            command + list(arguments),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env=environment,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode, result.stdout


def tree_blobs(repo, commit, relative_paths, git=git_output):
    """{path: blob id} for the paths that `commit` holds as files,  or None when git could not
    say.  A path the commit does not hold is simply not in the answer."""
    answer = git(repo, ["ls-tree", "-r", "-z", commit, "--"] + sorted(relative_paths))
    if answer is None or answer[0] != 0:
        return None
    blobs = {}
    for entry in answer[1].split(b"\0"):
        meta, tab, name = entry.partition(b"\t")
        fields = meta.split()
        if tab and len(fields) == 3 and fields[1] == b"blob":
            blobs[os.fsdecode(name)] = fields[2].decode("ascii", "replace")
    return blobs


def commit_id(repo, ref, git=git_output):
    """The commit `ref` names in `repo`:  a string,  False when there is no such ref,  or None
    when git could not answer."""
    answer = git(repo, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"])
    if answer is None:
        return None
    code, output = answer
    if code != 0:
        return False
    text = output.decode("ascii", "replace").strip()
    return text if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", text) else None


def checkout_intents(repo, files, git=git_output, deadline=None):
    """{path: (intent, reason)} for one checkout's files against its own `origin/main`.

    `files` is {path: blob_ids(content)}.  `reason` is None unless the intent is `unknown`.  A
    file is `clean` when its blob is the one `origin/main` holds,  `behind` when it is the one
    HEAD holds and HEAD is an ancestor of `origin/main`,  and `localEdit` otherwise.
    """

    def unknown(reason):
        return {path: ("unknown", reason) for path in files}

    if deadline is not None and time.monotonic() > deadline:
        return unknown("git_budget")
    answer = git(repo, ["rev-parse", "--show-toplevel"])
    if answer is None:
        return unknown("git_failed")
    if answer[0] != 0:
        return unknown("not_a_checkout")
    try:
        top = Path(os.fsdecode(answer[1].strip())).resolve()
        if top != repo.resolve():
            # The folder sits inside some other repo:  it is not a checkout of its own.
            return unknown("not_a_checkout")
    except (OSError, RuntimeError):
        return unknown("git_failed")
    main = commit_id(repo, INTENT_REF, git)
    if main is None:
        return unknown("git_failed")
    if main is False:
        return unknown("no_origin_main")
    head = commit_id(repo, "HEAD", git)
    if head is None:
        return unknown("git_failed")
    relative = {path: Path(path).relative_to(repo).as_posix() for path in files}
    on_main = tree_blobs(repo, main, relative.values(), git)
    at_head = tree_blobs(repo, head, relative.values(), git) if head else {}
    if on_main is None or at_head is None:
        return unknown("git_failed")
    algorithm = "sha256" if len(main) == 64 else "sha1"
    ancestor = []
    intents = {}
    for path, ids in files.items():
        mine = ids[algorithm]
        name = relative[path]
        if on_main.get(name) == mine:
            intents[path] = ("clean", None)
            continue
        if not head or at_head.get(name) != mine:
            intents[path] = ("localEdit", None)
            continue
        if not ancestor:
            # Exit status 0 is yes and 1 is no;  anything else is no answer.
            verdict = git(repo, ["merge-base", "--is-ancestor", head, main])
            answered = verdict is not None and verdict[0] in (0, 1)
            ancestor.append(verdict[0] == 0 if answered else None)
        if ancestor[0] is None:
            intents[path] = ("unknown", "git_failed")
        else:
            intents[path] = ("behind", None) if ancestor[0] else ("localEdit", None)
    return intents


def file_intents(code_root, contents, git=git_output, budget=GIT_BUDGET_SECONDS):
    """{path: (intent, reason)} for every present file,  checkout by checkout."""
    deadline = time.monotonic() + budget
    groups = {}
    for path, ids in contents.items():
        try:
            name = Path(path).relative_to(code_root).parts[0]
        except (ValueError, IndexError):
            continue
        groups.setdefault(name, {})[path] = ids
    intents = {}
    for name in sorted(groups):
        intents.update(checkout_intents(code_root / name, groups[name], git, deadline))
    return intents


# MARK: - The host's config


def shipped_units(source_root=SOURCE_ROOT):
    """{unit: its file,  relative to the checkout} for the units this repo ships.

    A template (`name@.service`) is left out:  only an instance of one is ever loaded.
    """
    units = {}
    for folder in UNIT_SOURCE_FOLDERS:
        for path in files_in(source_root / folder):
            name = path.name
            if UNIT_RE.fullmatch(name) and "@." not in name and name not in units:
                units[name] = "%s/%s" % (folder, name)
    return units


def allowlist_unit_names(control):
    """The units the allowlist names,  watches or updates.  Naming one here grants nothing."""
    server = control_server_section(control) or {}
    names = []
    for key in ("units", "watch", "updates"):
        entries = server.get(key)
        for entry in entries if isinstance(entries, list) else []:
            unit = entry.get("unit") if isinstance(entry, dict) else None
            if isinstance(unit, str) and UNIT_RE.fullmatch(unit) and unit not in names:
                names.append(unit)
    return names


def installed_unit_files(units, run_command=run):
    """[(unit, scope, kind, path)] for the files systemd loaded each unit from,  in both scopes.

    systemd's own `FragmentPath` and `DropInPaths` say where they are,  so no folder is guessed.
    A unit a timer or another unit triggers is looked up too,  one round deep.  A unit this
    host does not have is left out.
    """
    found = []
    for scope in SCOPES:
        asked = sorted(units)
        seen = set(asked)
        for _round in range(2):
            if not asked:
                break
            shown = show_units(asked, scope, run_command, properties=UNIT_FILE_PROPERTIES)
            if shown is None:
                break
            more = []
            for unit in asked:
                props = shown.get(unit) or {}
                if props.get("LoadState") in (None, "", "not-found", "masked"):
                    continue
                fragment = props.get("FragmentPath") or ""
                if fragment.startswith("/"):
                    found.append((unit, scope, "unit", fragment))
                for drop_in in (props.get("DropInPaths") or "").split():
                    if drop_in.startswith("/"):
                        found.append((unit, scope, "dropIn", drop_in))
                for name in (props.get("Triggers") or "").split():
                    if UNIT_RE.fullmatch(name) and name not in seen:
                        seen.add(name)
                        more.append(name)
            asked = sorted(more)
    return found


def host_file_row(row, path, previous, moment, source=None, source_root=SOURCE_ROOT):
    """`row` with what is at `path`:  its digest and its times,  never its content.

    `source` is the repo file it was installed from,  and `matchesSource` compares the two.
    """
    if source:
        row["source"] = source
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        row["state"] = "missing"
        return row
    except OSError:
        row["state"] = "unreadable"
        return row
    data = read_bounded(path)
    if data is None:
        row["state"] = "unreadable"
        return row
    digest = short_digest(data)
    row["state"] = "present"
    row["bytes"] = len(data)
    row["modifiedAt"] = iso_from_timestamp(info.st_mtime)
    row["sha256"] = digest
    row["changedAt"] = changed_at(previous.get(row["path"]), digest, info.st_mtime, moment)
    if source:
        wanted = digest_of(source_root / source)
        if wanted is not None:
            row["matchesSource"] = wanted == digest
    return row


def broker_rows(broker_dir, previous, moment, source_root=SOURCE_ROOT):
    """The resource broker's installed files,  the expected ones and any others beside them.

    A folder that is not there is one `absent` row,  and a link is never followed.
    """
    try:
        if broker_dir.is_symlink() or not broker_dir.is_dir():
            return [{"kind": "broker", "path": str(broker_dir), "state": "absent"}]
    except OSError:
        return [{"kind": "broker", "path": str(broker_dir), "state": "unreadable"}]
    names = set(BROKER_FILES)
    for path in files_in(broker_dir) + files_in(broker_dir / "hetzner"):
        names.add(path.relative_to(broker_dir).as_posix())
    rows = []
    for name in sorted(names):
        path = broker_dir / name
        if is_secret_path(path):
            continue
        kind = "allowlist" if name == BROKER_ALLOWLIST else "broker"
        source = "scripts/" + name if (source_root / "scripts" / name).is_file() else None
        row = {"kind": kind, "path": str(path)}
        rows.append(host_file_row(row, path, previous, moment, source, source_root))
    return rows


def collect_host_config(
    control,
    previous,
    run_command=run,
    *,
    source_root=SOURCE_ROOT,
    broker_dir=BROKER_DIR,
    now=None,
    platform=None,
):
    """The host's own config surface,  by digest:  installed units,  the broker,  the allowlists.

    `previous` is the previous report's `hostConfig`,  by path,  which keeps `changedAt` sticky.
    A path that looks like a credential is never hashed and never listed.
    """
    moment = time.time() if now is None else now
    shipped = shipped_units(source_root)
    units = set(shipped) | set(allowlist_unit_names(control))
    rows = []
    listed = set()
    for unit, scope, kind, place in installed_unit_files(units, run_command):
        if place in listed or is_secret_path(place):
            continue
        listed.add(place)
        row = {"kind": kind, "path": place, "unit": unit, "scope": scope}
        source = shipped.get(unit) if kind == "unit" else None
        # A unit file can be a link (`systemctl link`),  so it is read where it really is.
        real = Path(os.path.realpath(place))
        rows.append(host_file_row(row, real, previous, moment, source, source_root))
    if host_role(platform) == "server":
        rows += broker_rows(broker_dir, previous, moment, source_root)
    own = source_root / "scripts" / BROKER_ALLOWLIST
    try:
        separate = Path(control).resolve() != own.resolve()
    except (OSError, RuntimeError):
        separate = True
    if separate and not is_secret_path(control):
        row = {"kind": "allowlist", "path": str(control)}
        source = "scripts/" + BROKER_ALLOWLIST
        rows.append(host_file_row(row, Path(control), previous, moment, source, source_root))
    return rows


def previous_host_config(path):
    """The previous report's `hostConfig` rows,  by path."""
    rows = previous_document(path).get("hostConfig")
    previous = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("path"), str):
            previous[row["path"]] = row
    return previous


# MARK: - The report


def previous_document(path):
    """The previous report,  or an empty one when it cannot be read."""
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return {}
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return document if isinstance(document, dict) else {}


def previous_files(path):
    """The files of the previous report,  by path.

    A report that cannot be read is an empty previous report:  everything is then
    `new`,  which is what a first run says.
    """
    files = previous_document(path).get("files")
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
    resources = previous_document(path).get("coolify")
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


def collect_resource_policy(path=Path("/run/fleet-coding-resources/status.json"), *, owner_uid=0):
    """Read only the controller's public receipt; never its private policy or journal."""
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner_uid
            or info.st_mode & 0o022
            or info.st_size > 64 * 1024
        ):
            return None
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            return None
        # The collector is launched with -I, so load the reviewed sibling explicitly.
        spec = importlib.util.spec_from_file_location(
            "fleet_resource_control",
            Path(__file__).resolve().with_name("fleet_resource_control.py"),
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        snapshot = module.sanitize_snapshot(json.loads(raw), require_fresh=False)
        return respell_policy_times(snapshot)
    except (OSError, ValueError, ImportError, AttributeError, TypeError):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def respell_policy_times(snapshot):
    """The resource receipt with its times in the report's one spelling."""
    if not isinstance(snapshot, dict):
        return snapshot
    result = dict(snapshot)
    if whole_second_utc(result.get("generatedAt")):
        result["generatedAt"] = whole_second_utc(result["generatedAt"])
    observed = result.get("observed")
    if isinstance(observed, dict) and whole_second_utc(observed.get("updatedAt")):
        result["observed"] = dict(observed, updatedAt=whole_second_utc(observed["updatedAt"]))
    return result


def leftover_resource(key, before, live, complete, moment):
    """What a row of the previous report becomes when this run did not see it,  or None.

    The row is gone from the report,  rather than kept as `absent`,  in five cases:

      * This run saw it,  by name or by its uuid and service.
      * A report from before these fields existed names it,  and this run saw its uuid.  Those
        rows carried a per-deploy container name,  so their name cannot be matched.
      * A report from before `absentSince` existed already calls it `absent`.  Its age cannot
        be known,  and this is the one cleanup of the rows those reports piled up.
      * It is a throwaway container that nothing labelled.
      * It has been gone for `LEFTOVER_KEEP_SECONDS`.
    """
    uuid, name = key
    if key in live["names"] or (uuid, before.get("service")) in live["services"]:
        return None
    from_old_report = not any(field in before for field in ("container", "service", "absentSince"))
    if from_old_report and uuid in live["uuids"]:
        return None
    if before.get("status") == "absent" and "absentSince" not in before:
        return None
    kind = before.get("type", "standalone")
    is_labelled = before.get("uuidSource") == UUID_SOURCE_LABEL
    # A restart policy says someone wants this container kept,  whatever its name looks like.
    is_kept = before.get("restartPolicy") not in (None, "", "no")
    if (
        kind == "standalone"
        and not is_labelled
        and not is_kept
        and THROWAWAY_NAME_RE.fullmatch(str(uuid))
    ):
        return None
    since = None
    if "absentSince" in before:
        since = leftover_since(before["absentSince"], moment)
        if since is None:
            return None
    # Failed inspection is unknown, not evidence that resources vanished.
    gone = {
        "uuid": uuid,
        "name": name,
        "type": kind,
        "status": "absent" if complete else "unknown",
    }
    if complete:
        gone["absentSince"] = since or iso_from_timestamp(moment)
    elif since:
        gone["absentSince"] = since
    # The source is carried over only when the previous report said it;  a row
    # from a report written before `uuidSource` existed is left without one.
    if before.get("uuidSource") in UUID_SOURCES:
        gone["uuidSource"] = before["uuidSource"]
    for field in ("service", "container", "restartPolicy"):
        if isinstance(before.get(field), str) and before[field]:
            gone[field] = before[field]
    return gone


def helper_would_take(row):
    """Whether the helper's checks of the row it picks pass:  a labelled Coolify application
    that is on the host and whose state could be read."""
    return (
        row.get("type") == "application"
        and row.get("uuidSource") == UUID_SOURCE_LABEL
        and row.get("status") not in ("absent", "unknown")
    )


def add_redeploy_action(row, allowed, seen_now, first_row=None):
    """Say on a row whether the helper would redeploy it,  and if not,  why not.

    The helper reads the first row of the report with a uuid,  and redeploys a live Coolify
    application whose uuid came from a Coolify uuid label.  So `first_row`,  the first row with
    this one's uuid,  decides too.  `redeployBlockedReason` is a fixed code,  and only an
    application row has one,  because nothing else on the host could be redeployed in the first
    place.
    """
    if row.get("type") != "application":
        row["actions"] = []
        return
    reason = None
    if not seen_now:
        reason = "not_seen"
    elif row.get("uuidSource") != UUID_SOURCE_LABEL:
        reason = "no_uuid_label"
    elif row.get("status") == "unknown":
        reason = "state_unknown"
    elif first_row is not None and not helper_would_take(first_row):
        reason = "first_row_refused"
    elif not allowed:
        reason = "not_allowed"
    row["actions"] = [] if reason else ["redeploy"]
    if reason:
        row["redeployBlockedReason"] = reason


def application_rows(rows):
    """One line for each live labelled application,  with every container it holds.

    A redeploy acts on the whole application,  so this is the size of what it restarts.
    """
    applications = {}
    for row in rows:
        if row.get("type") != "application" or row.get("uuidSource") != UUID_SOURCE_LABEL:
            continue
        service = row.get("service")
        name = row["name"]
        if service and name.endswith(" (%s)" % service):
            name = name[: -len(" (%s)" % service)]
        line = applications.setdefault(
            row["uuid"], {"uuid": row["uuid"], "name": name, "containers": 0, "services": []}
        )
        line["containers"] += 1
        if service:
            line["services"] = sorted(line["services"] + [service])
    return [applications[uuid] for uuid in sorted(applications)]


def build_report(
    coolify,
    processes,
    files,
    previous_resources_map,
    host=None,
    host_metrics=None,
    services=None,
    files_summary=None,
    resource_policy=None,
    *,
    now=None,
    can_redeploy=None,
    failed_units=None,
    removed_checkouts=None,
    collection=None,
    process_sample_seconds=None,
    platform=None,
    host_config=None,
):
    """The report,  as Muster reads it.

    `hostMetrics`,  `services` and `filesSummary` are optional:  a key that was not
    collected is left out,  so a reader built before they existed reads the report as
    it always has.  So are the keys added after them.  `can_redeploy` is the helper's own
    answer,  and rows say nothing about actions when it was not asked.
    """
    moment = time.time() if now is None else now
    complete = getattr(coolify, "complete", True)
    live_rows = list(coolify or [])
    live = {
        "names": {(row["uuid"], row["name"]) for row in live_rows},
        "services": {(row["uuid"], row.get("service")) for row in live_rows},
        "uuids": {row["uuid"] for row in live_rows},
    }
    leftovers = []
    ordered = sorted(
        previous_resources_map.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))
    )
    for key, before in ordered:
        gone = leftover_resource(key, before, live, complete, moment)
        if gone:
            leftovers.append(gone)
    if can_redeploy is not None:
        first_rows = {}
        for row in live_rows:
            first_rows.setdefault(row["uuid"], row)
        for row in live_rows:
            add_redeploy_action(row, can_redeploy, True, first_rows[row["uuid"]])
        for row in leftovers:
            add_redeploy_action(row, can_redeploy, False)
    document = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "generatedAt": utc_now(),
        "host": host or socket.gethostname(),
        "hostRole": host_role(platform),
        # Live rows come first,  because the helper redeploys the first row with a uuid.
        "coolify": live_rows + leftovers,
        "processes": processes,
        "files": files,
    }
    if can_redeploy is not None:
        document["applications"] = application_rows(live_rows)
    if process_sample_seconds is not None:
        document["processSampleSeconds"] = process_sample_seconds
    if failed_units is not None:
        document["failedUnits"] = failed_units
    if removed_checkouts is not None:
        document["removedCheckouts"] = removed_checkouts
    if collection is not None:
        document["collection"] = collection
    if host_metrics is not None:
        document["hostMetrics"] = host_metrics
    if services is not None:
        document["services"] = services
    if files_summary is not None:
        document["filesSummary"] = files_summary
    if resource_policy is not None:
        document["resourcePolicy"] = resource_policy
    if host_config is not None:
        document["hostConfig"] = host_config
    return document


def summarize_files(found, limit, cut=()):
    """How many config files were found against the bound,  and which checkouts it cut."""
    summary = {"found": found, "limit": limit, "truncated": found > limit}
    if cut:
        summary["cutCheckouts"] = list(cut)
    return summary


def collection_entry(code=None, partial=False):
    """How one half of the report went:  `ok`,  and a fixed code when it did not go well.

    A half that is `partial` was read and is missing something.  A half that is not `ok` was
    not read at all.  Muster words the codes.
    """
    entry = {"ok": code is None or partial, "partial": partial}
    if code:
        entry["errorCode"] = code
    return entry


def collection_report(coolify, services, code_problem, answered_scopes, processes, files):
    """What each half could and could not read."""
    if not answered_scopes:
        failed = collection_entry("systemctl_unreadable")
    elif len(answered_scopes) < len(SCOPES):
        failed = collection_entry("systemctl_scope_unreadable", partial=True)
    else:
        failed = collection_entry()
    if getattr(files, "root_readable", True):
        truncated = bool(getattr(files, "cut", ()))
        file_entry = (
            collection_entry("files_truncated", partial=True) if truncated else collection_entry()
        )
    else:
        file_entry = collection_entry("code_root_unreadable")
    unknown_units = [row for row in services if row.get("activeState") == "unknown"]
    if code_problem:
        service_entry = collection_entry(code_problem)
    elif unknown_units:
        service_entry = collection_entry("systemctl_scope_unreadable", partial=True)
    else:
        service_entry = collection_entry()
    return {
        "coolify": collection_entry(
            getattr(coolify, "error_code", None), getattr(coolify, "partial", False)
        ),
        "services": service_entry,
        "failedUnits": failed,
        "processes": collection_entry() if processes else collection_entry("proc_unreadable"),
        "files": file_entry,
    }


def write_report(path, document):
    """Atomic and private:  a 0600 temporary file in the same folder,  fsynced,  then
    renamed over.  The folder is created 0700 when it is absent,  and a folder that is a link
    is refused:  the report goes where the Mac and the helper will look for it,  or nowhere."""
    if path.parent.is_symlink():
        raise OSError("the report's folder is a symlink;  refusing to write")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise OSError("the report's folder is a symlink;  refusing to write")
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
        folder = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(folder)
        finally:
            os.close(folder)
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
        default=2.0,
        help="the seconds between the two cpu samples (default: 2)",
    )
    parser.add_argument(
        "--file-limit",
        type=int,
        default=500,
        help="the most config files to report (default: 500)",
    )
    parser.add_argument(
        "--broker-dir",
        type=Path,
        default=BROKER_DIR,
        help="where the resource broker is installed (default: %s)" % BROKER_DIR,
    )
    return parser.parse_args(argv)


def main(argv=None, run_command=run, proc=Path("/proc")):
    args = parse_args(argv)
    output = args.output or (
        Path(args.home) / ".local" / "state" / "fleet-tools" / "server-status.json"
    )
    snapshot = args.snapshot or output

    coolify = collect_coolify(run_command=run_command)
    processes = collect_processes(
        limit=args.process_limit,
        interval=args.cpu_interval,
        proc=proc,
        containers=getattr(coolify, "names_by_id", None),
    )
    files, found = collect_files(args.code_root, previous_files(snapshot), limit=args.file_limit)
    services = collect_services(args.control, run_command=run_command)
    failed_units, answered_scopes = collect_failed_units(run_command)
    document = build_report(
        coolify,
        processes,
        files,
        previous_resources(snapshot),
        host_metrics=collect_host_metrics(proc=proc, reboot_flag=REBOOT_FLAG),
        services=services,
        files_summary=summarize_files(found, args.file_limit, files.cut),
        resource_policy=collect_resource_policy(),
        can_redeploy=redeploy_allowed(args.control),
        failed_units=failed_units,
        removed_checkouts=merge_removed_checkouts(
            files.removed,
            previous_document(snapshot).get("removedCheckouts"),
            args.code_root,
        ),
        collection=collection_report(
            coolify,
            services,
            services_problem(args.control),
            answered_scopes,
            processes,
            files,
        ),
        process_sample_seconds=args.cpu_interval if args.cpu_interval > 0 else None,
        host_config=collect_host_config(
            args.control,
            previous_host_config(snapshot),
            run_command,
            broker_dir=args.broker_dir,
        ),
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
