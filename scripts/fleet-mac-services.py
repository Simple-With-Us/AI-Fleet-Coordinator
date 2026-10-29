#!/usr/bin/env python3
"""Write which fleet LaunchAgents are installed and loaded on the Mac, for Muster.

Writes ~/.local/state/fleet-tools/mac-services.json, mode 0600, atomically:

  {"schemaVersion": 1, "tool": "fleet-mac-services", "generatedAt": "<UTC>", "host": "mac",
   "agents": [{"label", "plistInRepo", "installed", "loaded", "pid", "lastExitStatus",
               "controllable"}]}

The expected agents are every `scripts/launchd/*.plist` label plus the `mac.agents` labels in
`scripts/fleet-ops-control.json`.  An agent is installed when
`~/Library/LaunchAgents/<label>.plist` exists, and loaded when `launchctl print
gui/<uid>/<label>` answers.  That output is undocumented, so only its top-level `state`, `pid`
and `last exit code` lines are read, and a line that is missing or odd reads as null.  An agent
is controllable only when the allowlist names it with at least one action.  A label whose
`launchctl print` times out carries `"error": "timeout"`;  when launchctl cannot be run at all,
the document carries `"error": "launchctl_unavailable"` and no further labels are tried.
A missing or malformed allowlist means no extra labels and nothing controllable.  Python 3.9,
standard library only.  It reads launchctl and changes nothing else.

  fleet-mac-services.py [--write PATH] [--launchd-dir DIR] [--control PATH]
                        [--agents-dir DIR] [--dry-run]

--dry-run prints the document and writes nothing.  Exit 0 on a written or dry-run document.
"""

import argparse
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PATH = os.path.expanduser("~/.local/state/fleet-tools/mac-services.json")
DEFAULT_LAUNCHD_DIR = os.path.join(SCRIPTS_DIR, "launchd")
DEFAULT_CONTROL = os.path.join(SCRIPTS_DIR, "fleet-ops-control.json")
DEFAULT_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
SCHEMA_VERSION = 1
# Per `launchctl print` call;  about 20 labels must finish well inside the 300 s StartInterval.
LAUNCHCTL_TIMEOUT = 5
ACTIONS = ("start", "stop", "restart")

# A label becomes part of a launchctl target and a file name, so it is held to this shape.
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
# A top-level `key = value` line of `launchctl print`:  one tab, then the key.  Nested blocks
# are indented further and are skipped.
TOP_LINE_RE = re.compile(r"^\t([a-z][a-z ]*?) = (.*)$")
INT_RE = re.compile(r"^(-?\d+)")


def valid_label(label):
    return isinstance(label, str) and bool(LABEL_RE.match(label))


def repo_labels(launchd_dir):
    """The Label of every plist template in the repo;  the file name when the plist will not parse."""
    labels = set()
    try:
        names = sorted(os.listdir(launchd_dir))
    except OSError:
        return labels
    for name in names:
        if not name.endswith(".plist"):
            continue
        label = None
        try:
            with open(os.path.join(launchd_dir, name), "rb") as handle:
                data = plistlib.load(handle)
            if isinstance(data, dict):
                label = data.get("Label")
        except Exception:  # noqa: BLE001 - expat, binary-plist and OS errors all mean "unreadable"
            label = None
        if not valid_label(label):
            label = name[: -len(".plist")]
        if valid_label(label):
            labels.add(label)
    return labels


def control_agents(control_path):
    """{label: [actions]} from the allowlist's mac.agents;  {} when it is missing or malformed."""
    try:
        with open(control_path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    mac = data.get("mac") if isinstance(data, dict) else None
    agents = mac.get("agents") if isinstance(mac, dict) else None
    if not isinstance(agents, list):
        return {}
    found = {}
    for entry in agents:
        if not isinstance(entry, dict) or not valid_label(entry.get("label")):
            continue
        actions = entry.get("actions")
        if not isinstance(actions, list):
            actions = []
        found[entry["label"]] = [a for a in actions if a in ACTIONS]
    return found


def parse_int(value):
    match = INT_RE.match((value or "").strip())
    return int(match.group(1)) if match else None


def parse_print(text):
    """The top-level `state`, `pid` and `last exit code` of `launchctl print` output.

    `last exit code` may read `0`, `78: EX_CONFIG` or `(never exited)`;  only a leading integer
    counts.  A pid is kept only while the state is `running`."""
    top = {}
    for line in (text or "").splitlines():
        match = TOP_LINE_RE.match(line)
        if match and match.group(1) not in top:
            top[match.group(1)] = match.group(2).strip()
    state = top.get("state")
    pid = parse_int(top.get("pid"))
    if state is not None and state != "running":
        pid = None
    if pid is not None and pid <= 0:
        pid = None
    return {"state": state, "pid": pid, "lastExitStatus": parse_int(top.get("last exit code"))}


class LaunchctlUnavailable(Exception):
    """launchctl could not be run at all, so no label can be read."""


def launchd_status(label, uid, runner=subprocess.run):
    """{loaded, pid, lastExitStatus}, with `error: "timeout"` when this one call timed out.

    Raises LaunchctlUnavailable when launchctl cannot be run (missing, not executable)."""
    try:
        out = runner(
            ["launchctl", "print", "gui/%d/%s" % (uid, label)],
            capture_output=True,
            text=True,
            timeout=LAUNCHCTL_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # One slow label says nothing about the others;  mark only this agent.
        return {"loaded": False, "pid": None, "lastExitStatus": None, "error": "timeout"}
    except (OSError, subprocess.SubprocessError) as exc:
        raise LaunchctlUnavailable(str(exc))
    if out.returncode != 0:
        return {"loaded": False, "pid": None, "lastExitStatus": None}
    parsed = parse_print(out.stdout)
    return {"loaded": True, "pid": parsed["pid"], "lastExitStatus": parsed["lastExitStatus"]}


def document(
    launchd_dir=DEFAULT_LAUNCHD_DIR,
    control_path=DEFAULT_CONTROL,
    agents_dir=DEFAULT_AGENTS_DIR,
    uid=None,
    runner=subprocess.run,
    generated_at=None,
):
    in_repo = repo_labels(launchd_dir)
    allowed = control_agents(control_path)
    uid = os.getuid() if uid is None else uid
    agents = []
    unavailable = False
    for label in sorted(in_repo | set(allowed)):
        status = None
        if not unavailable:
            try:
                status = launchd_status(label, uid, runner)
            except LaunchctlUnavailable:
                # Running it again for every other label would fail the same way.
                unavailable = True
        if status is None:
            status = {"loaded": False, "pid": None, "lastExitStatus": None}
        agent = {
            "label": label,
            "plistInRepo": label in in_repo,
            "installed": os.path.isfile(os.path.join(agents_dir, label + ".plist")),
            "loaded": status["loaded"],
            "pid": status["pid"],
            "lastExitStatus": status["lastExitStatus"],
            "controllable": bool(allowed.get(label)),
        }
        if "error" in status:
            # Extra key:  this agent's "not loaded" is unknown, not observed.
            agent["error"] = status["error"]
        agents.append(agent)
    doc = {
        "schemaVersion": SCHEMA_VERSION,
        "tool": "fleet-mac-services",
        "generatedAt": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ",
            time.gmtime(generated_at if generated_at is not None else time.time()),
        ),
        "host": "mac",
        "agents": agents,
    }
    if unavailable:
        # launchctl could not be run, so "not loaded" above is a guess;  say so.
        doc["error"] = "launchctl_unavailable"
    return doc


def write_atomic(path, doc):
    directory = os.path.dirname(path)
    if os.path.islink(directory):
        raise OSError("state directory is a symlink; refusing to write")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.islink(directory):
        raise OSError("state directory is a symlink; refusing to write")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".mac-services-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(doc, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv=None, runner=subprocess.run):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--write", default=DEFAULT_PATH)
    parser.add_argument("--launchd-dir", default=DEFAULT_LAUNCHD_DIR)
    parser.add_argument("--control", default=DEFAULT_CONTROL)
    parser.add_argument("--agents-dir", default=DEFAULT_AGENTS_DIR)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    doc = document(args.launchd_dir, args.control, args.agents_dir, runner=runner)
    if args.dry_run:
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        write_atomic(args.write, doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
