#!/usr/bin/env python3
"""make-server-status-fixture:  turn a real fleet-server-status report into a Muster test fixture.

  python3 scripts/fleet-server-status.py --dry-run --code-root PATH > report.json
  python3 scripts/make-server-status-fixture.py report.json \\
      muster/MusterTests/Fixtures/server_status_live_shape.json

The fixture keeps what a test needs from the real report:  how many rows there are,  the words
the collector uses,  how its times are spelled,  which fields each row carries,  and how the
rows relate to each other (two rows of one application still share a uuid).  It keeps nothing
that names the server:  the host,  every uuid,  resource,  container,  image,  service,  unit,
process,  owner,  checkout and path are replaced with stable made-up tokens,  and descriptions
are dropped.  Times are kept because their spelling is under test.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# File names that say nothing about the server,  and that the collector treats by name.
GENERIC_FILE_NAMES = (
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "INFISICAL.md",
    "project.yml",
    "package.json",
)
HOST = "example-server"


class Tokens:
    """One stable made-up token for each real string,  counted per kind."""

    def __init__(self):
        self.known = {}

    def token(self, kind, value):
        family = self.known.setdefault(kind, {})
        if value not in family:
            family[value] = "%s-%02d" % (kind, len(family) + 1)
        return family[value]


def base_of(row):
    """A row's name with its service taken off,  so two services share one base name."""
    name = row.get("name") or ""
    service = row.get("service")
    if service and name.endswith(" (%s)" % service):
        return name[: -len(" (%s)" % service)]
    return name


def sanitize_unit(tokens, unit):
    stem, dot, suffix = unit.rpartition(".")
    return "%s.%s" % (tokens.token("unit", stem), suffix) if dot else tokens.token("unit", unit)


def sanitize_resource(tokens, row):
    result = dict(row)
    base = tokens.token("app", base_of(row))
    result["name"] = base
    if row.get("service"):
        result["service"] = tokens.token("service", row["service"])
        if row["service"] != base_of(row):
            result["name"] = "%s (%s)" % (base, result["service"])
    # A stand-in uuid is the row's own base name,  and that sameness is part of the shape.
    if row.get("uuid") == base_of(row):
        result["uuid"] = base
    else:
        result["uuid"] = tokens.token("uuid", row.get("uuid") or "")
    if row.get("container"):
        result["container"] = tokens.token("container", row["container"])
    if row.get("image"):
        result["image"] = tokens.token("image", row["image"])
    return result


def sanitize_application(tokens, row):
    result = dict(row)
    result["uuid"] = tokens.token("uuid", row.get("uuid") or "")
    result["name"] = tokens.token("app", row.get("name") or "")
    result["services"] = [tokens.token("service", name) for name in row.get("services") or []]
    return result


def sanitize_process(tokens, index, row):
    result = dict(row)
    result["pid"] = index + 1
    if row.get("name"):
        result["name"] = tokens.token("process", row["name"])
    if row.get("owner"):
        result["owner"] = tokens.token("owner", row["owner"])
    if row.get("belongsTo"):
        result["belongsTo"] = tokens.token("home", row["belongsTo"])
    return result


def sanitize_path(tokens, path):
    """A watched path,  keeping the part of its shape the collector's rules depend on."""
    parts = Path(path).parts
    if len(parts) < 2:
        return "/srv/example/%s" % tokens.token("file", path)
    name = parts[-1]
    suffix = Path(name).suffix
    if name not in GENERIC_FILE_NAMES:
        name = tokens.token("file", name) + suffix
    middle = ""
    if len(parts) >= 3 and parts[-2] == "workflows":
        middle = ".github/workflows/"
        checkout = parts[-4] if len(parts) >= 4 else ""
    elif len(parts) >= 2 and parts[-2] == "config":
        middle = "config/"
        checkout = parts[-3] if len(parts) >= 3 else ""
    else:
        checkout = parts[-2]
    return "/srv/example/%s/%s%s" % (tokens.token("checkout", checkout), middle, name)


def sanitize_file(tokens, index, row):
    result = dict(row)
    result["path"] = sanitize_path(tokens, row.get("path") or "")
    if row.get("sha256"):
        result["sha256"] = "%016x" % (index + 1)
    return result


def sanitize_service(tokens, row):
    result = dict(row)
    result.pop("description", None)
    result["unit"] = sanitize_unit(tokens, row.get("unit") or "")
    if row.get("job"):
        result["job"] = sanitize_unit(tokens, row["job"])
    return result


def sanitize_metrics(tokens, metrics):
    result = dict(metrics)
    disks = []
    for disk in metrics.get("disks") or []:
        mount = disk.get("mount") or ""
        spelled = "/" if mount == "/" else "/mnt/%s" % tokens.token("disk", mount)
        disks.append(dict(disk, mount=spelled))
    result["disks"] = disks
    return result


def sanitize_report(report):
    tokens = Tokens()
    result = dict(report)
    result["host"] = HOST
    result["coolify"] = [sanitize_resource(tokens, row) for row in report.get("coolify") or []]
    result["processes"] = [
        sanitize_process(tokens, index, row) for index, row in enumerate(report.get("processes") or [])
    ]
    result["files"] = [
        sanitize_file(tokens, index, row) for index, row in enumerate(report.get("files") or [])
    ]
    if "applications" in report:
        result["applications"] = [
            sanitize_application(tokens, row) for row in report.get("applications") or []
        ]
    if "services" in report:
        result["services"] = [sanitize_service(tokens, row) for row in report.get("services") or []]
    if "failedUnits" in report:
        result["failedUnits"] = [
            sanitize_service(tokens, row) for row in report.get("failedUnits") or []
        ]
    if "removedCheckouts" in report:
        result["removedCheckouts"] = [
            dict(note, repo=tokens.token("checkout", note.get("repo") or ""))
            for note in report.get("removedCheckouts") or []
        ]
    if isinstance(report.get("filesSummary"), dict) and report["filesSummary"].get("cutCheckouts"):
        result["filesSummary"] = dict(
            report["filesSummary"],
            cutCheckouts=[
                tokens.token("checkout", name) for name in report["filesSummary"]["cutCheckouts"]
            ],
        )
    if isinstance(report.get("hostMetrics"), dict):
        result["hostMetrics"] = sanitize_metrics(tokens, report["hostMetrics"])
    if isinstance(report.get("resourcePolicy"), dict):
        policy = dict(report["resourcePolicy"])
        if policy.get("policyRevision"):
            policy["policyRevision"] = "a" * 64
        result["resourcePolicy"] = policy
    return result


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 2:
        print("usage: make-server-status-fixture.py REPORT.json FIXTURE.json", file=sys.stderr)
        return 2
    report = json.loads(Path(arguments[0]).read_text())
    Path(arguments[1]).write_text(json.dumps(sanitize_report(report), indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
