"""fleet_app_processes:  which fleet app's processes run on this host (AFC-576).

The fleet tools doctor (scripts/fleet-tools-doctor.py) imports this module and runs the
scan, so there is one scheduler, one route to the server and one freshness rule:

  * On the server, the doctor's `--json` document carries this host's scan as
    `appProcesses`.  The read-only `fleet-tools-ro` key's forced command runs exactly that,
    so the Mac reaches the server's scan over the route it already uses.
  * On the Mac, when the doctor writes its report (`--write PATH`), it scans the Mac, takes
    the server's `appProcesses` out of the server's half, and writes both to
    `app-processes.json` beside the report, 0600 and atomically.  Muster reads that file.

What a process row may carry, and nothing else:  the app it belongs to, its pid, its
executable (the full path from `/proc/<pid>/exe` on Linux, else the kernel's short command
name), its launchd label, pm2 name or systemd unit, its state, when it started, and which of
those identities matched it.  A process's argv and environment are never read:  argv can carry
a token.  `ps` is asked for the kernel's command name (`comm` on Linux, `ucomm` on the Mac),
never `args` or `command`.  A pm2 name comes from pm2's own pid files (`~/.pm2/pids`),
never from `pm2 jlist`, whose answer carries each app's environment.

A process belongs to an app by its executable, launchd label, pm2 name or systemd unit, by
the rules below, or because its parent does.  A process that matches no app is not written
down at all.

Standard library only, and Python 3.9 compatible (the Mac's LaunchAgent runs /usr/bin/python3).
Nothing here raises to its caller:  a scan that fails is a host section that says so.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import subprocess
import time
from pathlib import Path

TOOL = "fleet-app-processes"
SCHEMA_VERSION = 1
# The file the Mac's doctor writes beside its own report, and Muster reads.
REPORT_NAME = "app-processes.json"
# Bounds on what one host contributes:  the server's half rides inside the doctor's answer,
# which the Mac caps at 2 MiB (MAX_REMOTE_BYTES) beside the doctor's own rows and the server
# status, so it is kept small:  400 rows print as about 100 KiB.
MAX_ROWS = 400
MAX_SECTION_BYTES = 128 * 1024
MAX_TEXT = 512  # any one string in a row
MAX_NOTES = 8
PS_TIMEOUT = 20
LAUNCHCTL_TIMEOUT = 20
PS = "/bin/ps"
LAUNCHCTL = "/bin/launchctl"
APPS_FILE = Path(__file__).resolve().parent.parent / "fleet-apps.json"

# The only keys a process row may carry.  The Mac rebuilds every row the server sends from
# this list, so a key the server added (an argv, say) never reaches the report.
ROW_KEYS = (
    "app",
    "pid",
    "executable",
    "launchdLabel",
    "pm2Name",
    "systemdUnit",
    "state",
    "startedAt",
    "matchedBy",
)
MATCHED_BY = ("launchd label", "pm2 name", "systemd unit", "executable", "parent process")
STATES = ("running", "sleeping", "idle", "waiting", "stopped", "zombie", "dead", "unknown")
STATE_FOR_CODE = {
    "R": "running",
    "S": "sleeping",
    "I": "idle",
    "D": "waiting",
    "U": "waiting",
    "T": "stopped",
    "t": "stopped",
    "Z": "zombie",
    "X": "dead",
}

# The identities this repository ships, by app.  Each is (app, kind, value, exact):  `kind` is
# launchd, pm2, systemd or executable, and `exact` False means the value is a prefix.  These
# come first;  every other app is matched by its own name below (see slug_rules).
# test_fleet_app_processes.py holds the AI-Fleet-Coordinator rows to the templates in
# scripts/launchd, scripts/systemd, scripts/host, scripts/hetzner and scripts/fleet_lanes.
EXPLICIT_RULES = (
    # BotFleet:  its own LaunchAgents, the Mac's launcher for BotFleet Server, and the MCP
    # adapter pm2 runs (docs/MAC-LOCAL-PROCESSES.md).
    ("BF", "launchd", "app.botfleet.", False),
    ("BF", "launchd", "com.jay.botfleet-", False),
    ("BF", "pm2", "botfleet-mcp", True),
    # AI-Fleet-Coordinator's own LaunchAgents and units.
    ("AFC", "launchd", "com.jay.agent-sync-runtime-sync", True),
    ("AFC", "launchd", "com.jay.fleet-gdrive-backup", True),
    ("AFC", "launchd", "com.jay.fleet-mac-services", True),
    ("AFC", "launchd", "com.jay.fleet-tools-doctor", True),
    ("AFC", "launchd", "com.jay.fleet-work-backup", True),
    ("AFC", "launchd", "com.jay.fleet-worker-health", True),
    ("AFC", "launchd", "com.jay.github-outbox-bridge", True),
    ("AFC", "launchd", "com.jay.grok-idle-unload", True),
    ("AFC", "launchd", "com.jay.ios-testflight-freshness", True),
    ("AFC", "launchd", "com.jay.lane-placement-watch", True),
    ("AFC", "launchd", "com.jay.lark-mm-bridge", True),
    ("AFC", "launchd", "com.jay.mac-cleanup", True),
    ("AFC", "launchd", "com.jay.mac-facts-publish", True),
    ("AFC", "launchd", "com.jay.mac-process-watch", True),
    ("AFC", "launchd", "com.jay.mac-resource-watch", True),
    ("AFC", "launchd", "com.jay.mac-seat-watch", True),
    ("AFC", "launchd", "com.jay.needs-owner-notify", True),
    ("AFC", "launchd", "com.jay.operator-notes-runner", True),
    ("AFC", "launchd", "com.jay.operator-wake-alerter", True),
    ("AFC", "launchd", "com.jay.sim-idle-reaper", True),
    ("AFC", "launchd", "com.jays.weekly-fleet-mode-digest", True),
    ("AFC", "pm2", "seat-mcp", True),
    ("AFC", "pm2", "code-main-keeper", True),
    ("AFC", "systemd", "cf-usage-watchdog.service", True),
    ("AFC", "systemd", "fleet-cli-git-update.service", True),
    ("AFC", "systemd", "fleet-coding-memory.service", True),
    ("AFC", "systemd", "fleet-health-recover@", False),
    ("AFC", "systemd", "fleet-housekeep.service", True),
    ("AFC", "systemd", "fleet-server-status.service", True),
    ("AFC", "systemd", "fleet-tools-doctor.service", True),
    ("AFC", "systemd", "fleet-vm-status.service", True),
    ("AFC", "systemd", "lane-placement-watch.service", True),
    ("AFC", "systemd", "operator-sheets-drain.service", True),
    # Clutch's browser UI, and Congress.Trade's scanned-PTR worker (docs/MAC-LOCAL-PROCESSES.md).
    ("C", "pm2", "clutch-web", True),
    ("CT", "pm2", "vision-worker", True),
)
KINDS = ("launchd", "pm2", "systemd", "executable")
MATCHED_BY_KIND = {
    "launchd": "launchd label",
    "pm2": "pm2 name",
    "systemd": "systemd unit",
    "executable": "executable",
}
PM2_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
ETIME_RE = re.compile(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)")


# --- Small helpers ------------------------------------------------------------------------


def utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


def parse_utc(text) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%SZ")))
    except (ValueError, OverflowError):
        return None


def clean(text, limit: int = MAX_TEXT) -> str | None:
    """A string safe to write:  printable, bounded, or None when nothing is left."""
    if not isinstance(text, str):
        return None
    text = "".join(ch for ch in text if ch.isprintable()).strip()
    return text[:limit] or None


def normalized(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def etime_seconds(text: str) -> int | None:
    """`ps`'s elapsed time, `[[dd-]hh:]mm:ss`, in seconds."""
    match = ETIME_RE.fullmatch(text.strip())
    if not match:
        return None
    days, hours, minutes, seconds = match.groups()
    return ((int(days or 0) * 24 + int(hours or 0)) * 60 + int(minutes)) * 60 + int(seconds)


def state_word(stat: str) -> str:
    return STATE_FOR_CODE.get(stat[:1], "unknown") if stat else "unknown"


# --- The apps and their rules --------------------------------------------------------------


def load_apps(path: Path | None = None) -> list[dict]:
    """Every app in fleet-apps.json, as {acronym, displayName, slug}, or [] when it cannot be read."""
    try:
        doc = json.loads((path or APPS_FILE).read_text())
    except (OSError, ValueError, RecursionError):
        return []
    entries = doc.get("apps") if isinstance(doc, dict) else None
    apps = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        acronym = clean(entry.get("acronym"), 16)
        if not acronym:
            continue
        name = clean(entry.get("displayName"), 80) or acronym
        slug = normalized(str(entry.get("codeDir") or entry.get("repo") or ""))
        apps.append({"acronym": acronym, "displayName": name, "slug": slug})
    return apps


def slug_rules(apps: list[dict]) -> list[tuple[str, str]]:
    """(slug, acronym) for every app with a name long enough to be its own, longest first,
    so fleetlink-legacy is tried before FleetLink."""
    rules = [(app["slug"], app["acronym"]) for app in apps if len(app.get("slug") or "") >= 6]
    rules.sort(key=lambda rule: -len(rule[0]))
    return rules


def tokens(kind: str, value: str) -> list[str]:
    """The parts of an identity an app's own name may start:  each dotted part of a label,
    the whole of a pm2 name or a unit's name, the executable's file name."""
    if kind == "launchd":
        return [normalized(part) for part in value.split(".")]
    if kind == "systemd":
        return [normalized(value.rsplit(".", 1)[0])]
    if kind == "executable":
        return [normalized(value.rsplit("/", 1)[-1])]
    return [normalized(value)]


def match_identity(kind: str, value: str | None, slugs) -> str | None:
    """The app this one identity names, or None."""
    if not value:
        return None
    for app, rule_kind, rule_value, exact in EXPLICIT_RULES:
        if rule_kind != kind:
            continue
        if (value == rule_value) if exact else value.startswith(rule_value):
            return app
    for part in tokens(kind, value):
        for slug, app in slugs:
            if part and part.startswith(slug):
                return app
    return None


# --- Reading the host ----------------------------------------------------------------------


def run_text(runner, argv, timeout) -> str | None:
    """The command's standard output when it answered 0, else None.  Its error text is never kept."""
    try:
        out = runner(
            argv,
            capture_output=True,
            text=True,
            # One process name that is not UTF-8 must not cost the whole listing.
            errors="replace",
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if getattr(out, "returncode", 1) != 0:
        return None
    stdout = out.stdout
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    return stdout if isinstance(stdout, str) else None


def list_processes(runner, platform: str) -> list[dict] | None:
    """Every process `ps` lists:  pid, parent pid, state code, elapsed seconds and the kernel's
    command name.  The command name is the last column, so a name with spaces stays whole."""
    name = "ucomm=" if platform == "darwin" else "comm="
    argv = [PS, "-A", "-ww", "-o", "pid=", "-o", "ppid=", "-o", "stat=", "-o", "etime=", "-o", name]
    text = run_text(runner, argv, PS_TIMEOUT)
    if text is None:
        return None
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append(
            {
                "pid": int(parts[0]),
                "ppid": int(parts[1]),
                "stat": parts[2],
                "elapsed": etime_seconds(parts[3]),
                "comm": parts[4].strip(),
            }
        )
    return rows


def launchd_labels(runner) -> dict[int, str] | None:
    """pid to label for every running job in this user's launchd domain (`launchctl list`)."""
    text = run_text(runner, [LAUNCHCTL, "list"], LAUNCHCTL_TIMEOUT)
    if text is None:
        return None
    labels = {}
    for line in text.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[0].strip().isdigit():
            continue
        label = clean(parts[2], 200)
        if label:
            labels[int(parts[0])] = label
    return labels


def pm2_names(home: Path) -> dict[int, str]:
    """pid to pm2 name from pm2's own pid files (`<name>-<id>.pid`).  No pm2 is run."""
    names: dict[int, str] = {}
    folder = home / ".pm2" / "pids"
    try:
        entries = sorted(folder.iterdir())
    except OSError:
        return names
    for entry in entries:
        stem, dot, suffix = entry.name.rpartition(".")
        if not dot or suffix != "pid":
            continue
        name, dash, number = stem.rpartition("-")
        if not dash or not number.isdigit() or not PM2_NAME_RE.fullmatch(name):
            continue
        try:
            # A pid file that is not UTF-8 costs that file and nothing else.
            text = entry.read_text(errors="replace")[:32].strip()
        except (OSError, ValueError):
            continue
        if text.isdigit():
            names[int(text)] = name
    return names


def linux_executable(pid: int, proc: Path) -> str | None:
    """The full path of a process's executable, when this user may read it."""
    try:
        path = os.readlink(str(proc / str(pid) / "exe"))
    except OSError:
        return None
    path = path.removesuffix(" (deleted)")
    return clean(path)


def linux_unit(pid: int, proc: Path) -> str | None:
    """The systemd unit a process runs in, from `/proc/<pid>/cgroup`, which names no argument."""
    try:
        # A cgroup file that is not UTF-8 costs this pid its unit, never the whole scan.
        text = (proc / str(pid) / "cgroup").read_text(errors="replace")[:4096]
    except (OSError, ValueError):
        return None
    lines = text.strip().splitlines()
    if not lines:
        return None
    path = lines[-1].rpartition(":")[2]
    units = [part for part in path.split("/") if part.endswith(".service")]
    return clean(units[-1], 200) if units else None


# --- The scan ------------------------------------------------------------------------------


def unreachable_section(host: str, error: str, now: float | None = None) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "host": host,
        "reachable": False,
        "error": error,
        "generatedAt": utc(time.time() if now is None else now),
        "complete": False,
        "truncated": False,
        "notes": [],
        "processes": [],
    }


def scan_host(
    host: str,
    *,
    runner=subprocess.run,
    home: Path | None = None,
    proc: Path = Path("/proc"),
    apps: list[dict] | None = None,
    platform: str | None = None,
    clock=time.time,
    own_pid: int | None = None,
) -> dict:
    """This host's section:  every process that belongs to a fleet app.  Never raises."""
    try:
        return _scan(
            host,
            runner=runner,
            home=home if home is not None else Path(os.environ.get("HOME", str(Path.home()))),
            proc=proc,
            apps=load_apps() if apps is None else apps,
            # The doctor names the host it runs on:  the Mac is macOS and the server is Linux.
            platform=platform or ("darwin" if host == "mac" else "linux"),
            clock=clock,
            own_pid=os.getpid() if own_pid is None else own_pid,
        )
    except Exception as exc:  # the scan is a guest in the doctor and must never fail it
        return unreachable_section(host, f"the process scan stopped ({type(exc).__name__})")


def _scan(host, *, runner, home, proc, apps, platform, clock, own_pid) -> dict:
    now = clock()
    listed = list_processes(runner, platform)
    if listed is None:
        return unreachable_section(host, "ps did not answer, so no process was read", now)
    notes: list[str] = []
    complete = True
    if not apps:
        notes.append(
            "fleet-apps.json could not be read, so only this repository's own rules matched."
        )
    labels: dict[int, str] = {}
    if platform == "darwin":
        read = launchd_labels(runner)
        if read is None:
            complete = False
            notes.append("launchctl list did not answer, so no launchd label was matched.")
        else:
            labels = read
    pm2 = pm2_names(home)
    slugs = slug_rules(apps)

    # The scan's own children (this ps, say) are not the fleet's processes.
    listed = [row for row in listed if row["ppid"] != own_pid]
    by_pid = {row["pid"]: row for row in listed}
    rows: dict[int, dict] = {}
    for item in listed:
        pid = item["pid"]
        identities = {
            "launchd": labels.get(pid),
            "pm2": pm2.get(pid),
            "systemd": linux_unit(pid, proc) if platform != "darwin" else None,
            "executable": (linux_executable(pid, proc) if platform != "darwin" else None)
            or clean(item["comm"]),
        }
        for kind in KINDS:
            app = match_identity(kind, identities[kind], slugs)
            if app:
                rows[pid] = row_for(item, identities, app, MATCHED_BY_KIND[kind], now)
                break
        else:
            item["identities"] = identities

    # A process whose parent belongs to an app belongs to it too:  BotFleet's workers, say.
    children: dict[int, list[int]] = {}
    for item in listed:
        children.setdefault(item["ppid"], []).append(item["pid"])
    queue = sorted(rows)
    while queue:
        parent = queue.pop(0)
        for child in children.get(parent, []):
            if child in rows or child == parent:
                continue
            item = by_pid[child]
            identities = item.get("identities") or {"executable": clean(item["comm"])}
            rows[child] = row_for(item, identities, rows[parent]["app"], "parent process", now)
            queue.append(child)

    ordered = sorted(rows.values(), key=lambda row: (row["app"], row["pid"]))
    truncated = False
    if len(ordered) > MAX_ROWS:
        ordered = ordered[:MAX_ROWS]
        truncated = True
    section = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "host": host,
        "reachable": True,
        "error": None,
        "generatedAt": utc(now),
        "complete": complete and not truncated,
        "truncated": truncated,
        "notes": notes,
        "processes": ordered,
    }
    while ordered and section_bytes(section) > MAX_SECTION_BYTES:
        ordered = ordered[: max(0, len(ordered) - 25)]
        section["processes"] = ordered
        section["truncated"] = True
        section["complete"] = False
    if section["truncated"]:
        notes.append(
            f"More processes matched than the report keeps;  the first {len(ordered)} are listed."
        )
    return section


def row_for(item: dict, identities: dict, app: str, matched_by: str, now: float) -> dict:
    row = {"app": app, "pid": item["pid"]}
    executable = identities.get("executable")
    if executable:
        row["executable"] = executable
    for kind, key in (("launchd", "launchdLabel"), ("pm2", "pm2Name"), ("systemd", "systemdUnit")):
        if identities.get(kind):
            row[key] = identities[kind]
    row["state"] = state_word(item["stat"])
    if item["elapsed"] is not None:
        row["startedAt"] = utc(now - item["elapsed"])
    row["matchedBy"] = matched_by
    return row


def section_bytes(section: dict) -> int:
    return len(json.dumps(section, indent=2).encode("utf-8"))


# --- The Mac's side:  checking the server's section and writing the report -----------------


def json_true(value) -> bool:
    """True only for a JSON `true`.  The scan writes these flags as booleans, and Muster's
    reader decodes them as Bool, so a 1 or a "yes" is a malformed flag:  it reads as false
    here exactly as it does there, the cautious answer (not reached, not complete)."""
    return isinstance(value, bool) and value


def sanitized_row(row) -> dict | None:
    """A row rebuilt from ROW_KEYS alone, or None when it is not one."""
    if not isinstance(row, dict):
        return None
    pid = row.get("pid")
    app = clean(row.get("app"), 16)
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 0 or not app:
        return None
    out = {"app": app, "pid": pid}
    for key in ("executable", "launchdLabel", "pm2Name", "systemdUnit"):
        value = clean(row.get(key))
        if value:
            out[key] = value
    out["state"] = row.get("state") if row.get("state") in STATES else "unknown"
    if parse_utc(row.get("startedAt")) is not None:
        out["startedAt"] = row["startedAt"]
    if row.get("matchedBy") in MATCHED_BY:
        out["matchedBy"] = row["matchedBy"]
    return out


def section_from(doc, host: str) -> dict:
    """A host's section as the report keeps it:  checked, with every row rebuilt.  A section
    that is not one becomes an unreachable section that says why."""
    if not isinstance(doc, dict) or doc.get("tool") != TOOL:
        return unreachable_section(
            host, f"the {host}'s answer carried no process scan this doctor reads"
        )
    if doc.get("schemaVersion") != SCHEMA_VERSION:
        return unreachable_section(
            host,
            f"the {host}'s process scan is version {doc.get('schemaVersion')!r}, not {SCHEMA_VERSION}",
        )
    if doc.get("host") != host:
        return unreachable_section(
            host, f"the process scan says it is from {doc.get('host')!r}, not the {host}"
        )
    if parse_utc(doc.get("generatedAt")) is None:
        return unreachable_section(host, f"the {host}'s process scan carries no time")
    processes = doc.get("processes")
    if not isinstance(processes, list):
        return unreachable_section(host, f"the {host}'s process scan has no process list")
    rows = [row for row in (sanitized_row(item) for item in processes[:MAX_ROWS]) if row]
    raw_notes = doc.get("notes") if isinstance(doc.get("notes"), list) else []
    notes = [note for note in (clean(text, 300) for text in raw_notes[:MAX_NOTES]) if note]
    reachable = json_true(doc.get("reachable"))
    # Only a JSON false says the list was not cut:  a missing or odd flag reads as partial.
    not_cut = isinstance(doc.get("truncated"), bool) and not doc["truncated"]
    section = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "host": host,
        "reachable": reachable,
        "error": None
        if reachable
        else (clean(doc.get("error"), 300) or f"the {host} could not scan itself"),
        "generatedAt": doc["generatedAt"],
        "complete": reachable
        and json_true(doc.get("complete"))
        and len(rows) == len(processes)
        and not_cut,
        "truncated": json_true(doc.get("truncated")) or len(processes) > MAX_ROWS,
        "notes": notes,
        "processes": rows if reachable else [],
    }
    if section_bytes(section) > MAX_SECTION_BYTES:
        return unreachable_section(
            host, f"the {host}'s process scan is larger than {MAX_SECTION_BYTES} bytes"
        )
    return section


def server_section(server) -> dict:
    """The server's half of the report, from the server's half of the doctor's answer.
    The answer's `appProcesses` is taken out, so it never enters the tools report."""
    if not isinstance(server, dict):
        return unreachable_section("server", "no read-only route to the server (no --remote given)")
    scan = server.pop("appProcesses", None)
    if not server.get("reachable"):
        return unreachable_section(
            "server", clean(server.get("error"), 300) or "the doctor could not reach the server"
        )
    if scan is None:
        return unreachable_section(
            "server",
            "the server's doctor does not carry a process scan yet (its runtime may be behind)",
        )
    return section_from(scan, "server")


def build_report(sections: list[dict], apps: list[dict], now: float | None = None) -> dict:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL,
        "generatedAt": utc(time.time() if now is None else now),
        "apps": [{"acronym": app["acronym"], "displayName": app["displayName"]} for app in apps],
        "hosts": sections,
    }


def keep(
    server, mac: dict, path: Path, writer, apps: list[dict] | None = None, now: float | None = None
) -> bool:
    """On the Mac:  take the server's scan out of its half, and write the Mac's and the
    server's sections to `path` with the doctor's own atomic 0600 writer.  True when written.
    Never raises:  a failed write leaves the previous file, which Muster ages out."""
    try:
        sections = [
            section_from(mac, "mac") if mac.get("reachable") else mac,
            server_section(server),
        ]
        report = build_report(sections, load_apps() if apps is None else apps, now)
        writer(path, report)
        return True
    except Exception:
        if isinstance(server, dict):
            server.pop("appProcesses", None)
        return False
