#!/usr/bin/env python3
"""fleet-ops-control:  start, stop, restart and update the fleet's own processes, and nothing else.

  python3 scripts/fleet-ops-control.py mac start|stop|restart LABEL       (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py pm2 start|stop|restart NAME        (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py server start|stop|restart UNIT     (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py server logs UNIT                   (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py server update NAME                 (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py server update-status fleet-cli.ID.BASELINE --plan [--json]
  python3 scripts/fleet-ops-control.py server redeploy RESOURCE_UUID      (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py server eavesdropper-status server --plan [--json]
  python3 scripts/fleet-ops-control.py --from-ssh                         (the forced command)
  python3 scripts/fleet-ops-control.py --check-token                      (install check, not over ssh)

`scripts/fleet-ops-control.json` is the one allowlist.  A label, a unit or an update that is
not in it cannot be touched, and the allowlist is read and checked again on every run and on
every path, the ssh one included.

What it promises:

  * `--plan` prints the commands it would run and changes nothing.  `--yes` runs them.  With
    neither it refuses.
  * Every command is an argv list run without a shell.  Labels and unit names must match a
    strict pattern before they reach an argv, and their output is kept private.  Update
    stderr is bounded and classified into fixed error codes, never returned verbatim.
    `server logs` is the deliberate exception: a `--yes` run returns
    the last 200 journal lines (at most 64 KiB, with a truncation note when cut) in the
    answer's `output`, while a `--plan` carries no log text at all.  Mac LaunchAgents have
    no log verb, because launchd has no uniform log file to tail.  `pm2` verbs act on the
    Mac pm2 applications the allowlist names, with one `pm2 start|stop|restart NAME` argv
    and no ecosystem fallback.  A `start` first reads `pm2 jlist`:  a stale record (online
    with no process behind it) is planned as a restart, because pm2 answers a start on one
    with "already started" and starts nothing, and an application pm2 no longer remembers
    is refused at plan time rather than recreated from the ecosystem file.
  * `redeploy` takes a uuid only when the latest `server-status.json` lists it as a Coolify
    resource, so no uuid or host is kept in this repo.  The Coolify token is read through
    `fleet-secrets` into memory, sent in one request header and never printed, logged or
    passed in an argv.  A forced command has only the environment sshd gives it, so
    `fleet-secrets` receives the scoped Fleet Ops Control token from `ops-control.env` (0600).
    `--check-token` reads the token the same way and asks
    Coolify for its version, which proves the whole path and changes nothing.
  * `--from-ssh` is the forced command of the `fleet-ops-ctl` key.  It reads
    `SSH_ORIGINAL_COMMAND`, refuses any shell metacharacter, splits it with `shlex`, and
    accepts only a `server` verb, one target, one of `--plan` or `--yes` and an optional
    `--json`.  Anything more is refused.
  * `server eavesdropper-status server --plan` reads the server eavesdropper's `status.json`
    with `docker exec --user simpletools eavesdropper /bin/cat /data/listener/status.json` (docker
    group membership, no sudo, run as the container's own unprivileged account, a pinned
    environment with an empty private docker config, at most 1 MiB, 15 seconds) and answers it
    reduced to names, counts, tokens and times by `scripts/reduced_status.py`.  It
    takes no lock, writes no audit row for a plan and refuses `--yes`.  When the status cannot be
    read the result is `unavailable` with an `errorCode` word (never a path or docker's own
    text) and the exit status is 1, although no command failed.
  * Every `--yes` run appends one JSON line to `~/.local/state/fleet-tools/ops-audit.jsonl`
    (0600): time, host role, verb, target and result.  No command output is recorded, and a
    redeploy is recorded by its resource name, never its uuid.  Each row also has a short
    `id`, and the `--yes` answer carries it (`auditID`) with the row's target (`auditTarget`),
    so the caller's own log can name the row this run wrote.
  * One `--yes` runs at a time on a machine:  a second one while another holds
    `~/.local/state/fleet-tools/ops-control.lock` is refused with `errorCode` `action_busy`.
    Plans, update-status reads and journal reads take no lock.  A chain of commands that
    stops at a failure says how many finished first (`completedCommands`).

An update is submitted until a new successful service invocation is observed; API acceptance
of a redeploy is also submitted.  Neither is called completed from submission alone.

Exit status: 0 planned, submitted or done, 1 a command failed (or, for `server
eavesdropper-status` alone, the status could not be read: result `unavailable`, with no
command failed), 2 refused.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import fcntl
import json
import os
import re
import secrets
import selectors
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import types
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

TOOL = "fleet-ops-control"
SCHEMA_VERSION = 1
CONFIG_PATH = Path(__file__).resolve().parent / "fleet-ops-control.json"
CONFIG_MAX_BYTES = 64 * 1024
STATUS_MAX_BYTES = 8 * 1024 * 1024
# The collector runs every 15 minutes; Muster calls a report older than 45 minutes stale,
# and a redeploy must not trust one either.
STATUS_MAX_AGE_SECONDS = 45 * 60
SSH_COMMAND_MAX = 256

ACTIONS = ("start", "stop", "restart")
SCOPES = ("system", "user")
# Every pattern below is used with `fullmatch`, never `match`, because `$` would also
# accept a trailing newline.
# A launchd label: reverse-DNS letters, digits, dots, dashes and underscores.
LABEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,126}")
# A systemd unit this tool may name: a plain service or timer, no template or path escapes.
UNIT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,120}\.(service|timer)")
UPDATE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# A pm2 application name: the registry's own lowercase shape, never a flag or a path.
PM2_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
LOG_MAX_LINES = 200
LOG_MAX_BYTES = 64 * 1024
LOG_TRUNCATED_NOTE = "[fleet-ops-control: output truncated to the last 200 lines / 64 KiB]"
# Coolify resource uuids are short lowercase letters and digits.
UUID_RE = re.compile(r"[A-Za-z0-9]{6,64}")
# What may appear in SSH_ORIGINAL_COMMAND at all: no quote, no shell metacharacter.
SSH_SAFE_RE = re.compile(r"[A-Za-z0-9 ._@-]{1,%d}" % SSH_COMMAND_MAX)
# Only an application: the collector reads its uuid from the `coolify.applicationUuid`
# label.  A service or a database carries no such label, so its row holds a resource
# name in `uuid`, and a name must never reach Coolify as a uuid.
REDEPLOYABLE_TYPES = ("application",)

LAUNCHCTL = "/bin/launchctl"
SYSTEMCTL_CANDIDATES = ("/usr/bin/systemctl", "/bin/systemctl")
JOURNALCTL_CANDIDATES = ("/usr/bin/journalctl", "/bin/journalctl")
DOCKER_CANDIDATES = ("/usr/bin/docker", "/bin/docker")
# `server eavesdropper-status` reads the status document of the server's eavesdropper (the
# listener daemon) out of its container with one fixed argv, `docker exec --user simpletools
# eavesdropper /bin/cat /data/listener/status.json`: no shell, no sudo, no flag or path from the
# request.  The caller's account needs only membership of the `docker` group, and the read runs
# as the container's own unprivileged account (the file is 0600 and that account's), never as
# root.  /data is the daemon's SIMPLE_TOOLS_STATE_DIR, a docker volume, and the listener's own
# `listener_dir` puts status.json at <state dir>/listener/status.json.
EAVESDROPPER_TARGET = "server"
EAVESDROPPER_CONTAINER = "eavesdropper"
EAVESDROPPER_USER = "simpletools"
EAVESDROPPER_CAT = "/bin/cat"
EAVESDROPPER_STATUS_PATH = "/data/listener/status.json"
EAVESDROPPER_STATUS_MAX_BYTES = 1024 * 1024
EAVESDROPPER_READ_TIMEOUT = 15
# The whole environment of that docker call is built here, never taken from the caller's: a fixed
# PATH, the local socket, and an empty private directory as both DOCKER_CONFIG and HOME, so no
# docker config, context, credential helper or proxy setting of the account can apply.
DOCKER_PATH = "/usr/bin:/bin"
DOCKER_HOST = "unix:///var/run/docker.sock"
# Only the start of docker's stderr is kept, to be compared with known phrases and never repeated.
DOCKER_STDERR_MAX = 4096
# Docker's own words for a container that cannot be exec'd into (compared ignoring case).
DOCKER_NOT_RUNNING_PHRASES = ("is not running", "no such container", "is restarting")
# How long to wait for a child to be reaped after it was killed, so a child that will not die
# cannot hold this helper forever.
KILL_WAIT_SECONDS = 5
# The reducer module sits beside the helper, in the helper's own folder: `scripts/` in a source
# checkout, the root-owned /usr/local/lib/fleet-resource-control on the server.  It is opened
# without following a symlink and judged by fstat on the open descriptor, never by a path
# check.  The layout is read from who owns that folder.  A folder owned by TRUSTED_MODULE_UID (root)
# is the installed layout: the file must be root's too, and neither may be writable by group or
# others.  Any other folder is a checkout: the file may also be owned by the calling account,
# the folder must be that account's too, and still no group or other write bit is allowed.  A
# checkout owned by another account is refused.  Nothing in the request or the environment
# selects a place.
HELPER_DIR = Path(__file__).resolve().parent
TRUSTED_MODULE_UID = 0
REDUCED_STATUS_NAME = "reduced_status.py"
REDUCED_STATUS_MAX_BYTES = 256 * 1024
# The reason words an unavailable answer may carry, each with the plain sentence for `detail`.
EAVESDROPPER_UNAVAILABLE = {
    "docker_unavailable": "docker could not be run",
    "timeout": "reading the status took too long",
    "container_not_running": "the eavesdropper container is not running",
    "read_failed": "the eavesdropper container or its status file could not be read",
    "too_large": "the status file is larger than the allowed size",
    "not_json": "the status file is not a JSON object",
    "module_unavailable": "the status reduction module is not installed beside the helper",
}
# pm2 runs the way Muster's own pm2 client runs it: a pinned node and the pm2 script,
# never a PATH lookup a caller could shadow.
NODE_CANDIDATES = ("/opt/homebrew/bin/node", "/usr/local/bin/node")
PM2_CANDIDATES = (
    "/opt/homebrew/lib/node_modules/pm2/bin/pm2",
    "/usr/local/lib/node_modules/pm2/bin/pm2",
)
FLEET_SECRETS_CANDIDATES = ("~/.local/bin/fleet-secrets", "/usr/local/bin/fleet-secrets")
COOLIFY_TOKEN_NAME = "COOLIFY_API_TOKEN"
COOLIFY_TOKEN_PATH = "/coolify"
# Coolify answers on its own host; COOLIFY_BASE_URL may change the port or the scheme,
# never the host.
COOLIFY_DEFAULT_BASE = "http://127.0.0.1:8000"
# The Infisical identity fleet-secrets may be handed from the credential file, and nothing
# else from it.
CREDENTIAL_NAMES = ("FLEET_OPS_CONTROL_INFISICAL_TOKEN",)
SCOPED_TOKEN_NAME = CREDENTIAL_NAMES[0]
# Keep this boundary independent of optional fleet-secrets imports.
SHARED_AUTH_ENV_NAMES = frozenset(
    {
        "INFISICAL_TOKEN",
        "INFISICAL_AI_FLEET_COORDINATOR_TOKEN",
        "INFISICAL_WRITE_TOKEN",
        "INFISICAL_CLIENT_ID",
        "INFISICAL_CLIENT_SECRET",
        "INFISICAL_AUTOMATION_CLIENT_ID",
        "INFISICAL_AUTOMATION_CLIENT_SECRET",
        "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
        "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
        "INFISICAL_SHARED_CLIENT_ID",
        "INFISICAL_SHARED_CLIENT_SECRET",
        "WISE_RELAY_INFISICAL_TOKEN",
    }
)
CREDENTIALS_MAX_BYTES = 8 * 1024
COMMAND_TIMEOUT = 90
SECRET_TIMEOUT = 30
HTTP_TIMEOUT = 30
SUDO = "/usr/bin/sudo"
UPDATE_SYSTEMCTL = "/usr/bin/systemctl"
FLEET_CLI_UPDATE_UNIT = "fleet-cli-git-update.service"
UPDATE_PROBE_TIMEOUT = 3
UPDATE_POLL_ATTEMPTS = 25
UPDATE_POLL_INTERVAL = 0.5
UPDATE_VERIFY_SECONDS = 12
UPDATE_STATUS_RE = re.compile(r"fleet-cli\.([0-9a-f]{32})\.(0|[1-9][0-9]{0,19})")
UINT64_MAX = (1 << 64) - 1
AUDIT_CONTEXT_MAX_BYTES = 1024 * 1024
UPDATE_PROPERTIES = (
    "LoadState",
    "ActiveState",
    "SubState",
    "Job",
    "Type",
    "InvocationID",
    "Result",
    "ExecMainCode",
    "ExecMainStatus",
    "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
)
# The only variables a launchctl or systemctl child sees.  A pm2 child also needs
# PM2_HOME, which names a directory and never a credential.
CHILD_ENV_KEEP = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "XDG_RUNTIME_DIR",
    "DBUS_SESSION_BUS_ADDRESS",
    "PM2_HOME",
)


class Refused(Exception):
    """The request is not allowed.  The text is a fixed phrase that names no secret."""

    def __init__(self, message, *, error_code=None):
        super().__init__(message)
        self.error_code = error_code


class Failed(Exception):
    """An allowed command ran and did not succeed.

    `completed` is how many commands of the plan finished before this one, when the run
    was a chain;  None when it was not."""

    def __init__(self, message, *, error_code=None, completed=None):
        super().__init__(message)
        self.error_code = error_code
        self.completed = completed


# MARK: - The allowlist


def load_config(path=CONFIG_PATH):
    """The allowlist, checked entry by entry.  Any malformed entry refuses the whole file,
    so a typo can never widen what is allowed."""
    try:
        info = os.stat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > CONFIG_MAX_BYTES:
            raise Refused("the allowlist is not a regular file")
        with open(path, encoding="utf-8") as handle:
            doc = json.load(handle)
    except Refused:
        raise
    except (OSError, ValueError):
        raise Refused("the allowlist cannot be read") from None
    return validate_config(doc)


def validate_config(doc):
    def bad(what):
        raise Refused(f"the allowlist is malformed ({what})")

    if not isinstance(doc, dict) or doc.get("schemaVersion") != SCHEMA_VERSION:
        bad("schemaVersion")
    mac = doc.get("mac") or {}
    server = doc.get("server") or {}
    if not isinstance(mac, dict) or not isinstance(server, dict):
        bad("sections")
    agents = {}
    for row in mac.get("agents") or []:
        if not isinstance(row, dict) or not isinstance(row.get("label"), str):
            bad("mac.agents")
        label = row["label"]
        if not LABEL_RE.fullmatch(label) or label in agents:
            bad("mac.agents label")
        agents[label] = _actions(row, bad)
    pm2_apps = {}
    for row in mac.get("pm2Apps") or []:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            bad("mac.pm2Apps")
        name = row["name"]
        if not PM2_RE.fullmatch(name) or name in pm2_apps:
            bad("mac.pm2Apps name")
        pm2_apps[name] = _actions(row, bad)
    units = {}
    for row in server.get("units") or []:
        if not isinstance(row, dict) or not isinstance(row.get("unit"), str):
            bad("server.units")
        unit = row["unit"]
        if not UNIT_RE.fullmatch(unit) or unit in units or row.get("scope") not in SCOPES:
            bad("server.units unit")
        units[unit] = {"scope": row["scope"], "actions": _actions(row, bad)}
    updates = {}
    for row in server.get("updates") or []:
        if not isinstance(row, dict):
            bad("server.updates")
        name, unit = row.get("name"), row.get("unit")
        if (
            not isinstance(name, str)
            or not UPDATE_RE.fullmatch(name)
            or name in updates
            or not isinstance(unit, str)
            or not UNIT_RE.fullmatch(unit)
            or not unit.endswith(".service")
            or row.get("scope") not in SCOPES
        ):
            bad("server.updates entry")
        updates[name] = {"unit": unit, "scope": row["scope"]}
    coolify = server.get("coolify") or {}
    if not isinstance(coolify, dict) or not isinstance(coolify.get("redeploy", False), bool):
        bad("server.coolify")
    if type(server.get("resourcePolicy", False)) is not bool:
        bad("server.resourcePolicy")
    return {
        "agents": agents,
        "pm2": pm2_apps,
        "units": units,
        "updates": updates,
        "redeploy": bool(coolify.get("redeploy", False)),
        "resourcePolicy": server.get("resourcePolicy", False),
    }


def _actions(row, bad):
    actions = row.get("actions")
    if not isinstance(actions, list) or not actions or any(a not in ACTIONS for a in actions):
        bad("actions")
    return tuple(actions)


# MARK: - Running things


def run_command(
    argv,
    timeout=COMMAND_TIMEOUT,
    env=None,
    want_stdout=False,
    max_bytes=None,
    combine_stderr=False,
):
    """(exit status, captured bytes).  An argv list, never a shell.

    With `max_bytes` the capture itself is capped, keeping only the last bytes.  A timeout
    is status 124 and a missing binary 127, like a shell would say.  Stderr is discarded
    unless explicitly merged for bounded private classification by the update path."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise Refused("internal: not an argv list")
    if want_stdout and max_bytes:
        return _run_capped(argv, timeout, env, max_bytes, combine_stderr=combine_stderr)
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if want_stdout else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if combine_stderr and want_stdout else subprocess.DEVNULL,
            timeout=timeout,
            env=env,
            cwd="/",
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return 124, b""
    except OSError:
        return 127, b""
    return proc.returncode, (proc.stdout or b"") if want_stdout else b""


def _run_capped(argv, timeout, env, max_bytes, combine_stderr=False):
    """Capture stdout without ever holding more than its last `max_bytes` bytes."""
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if combine_stderr else subprocess.DEVNULL,
            env=env,
            cwd="/",
            shell=False,
        )
    except OSError:
        return 127, b""
    kept = bytearray()
    selector = None
    timed_out = False
    try:
        deadline = time.monotonic() + timeout
        fd = proc.stdout.fileno()
        selector = selectors.DefaultSelector()
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not selector.select(remaining):
                timed_out = True
                break
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            kept += chunk
            if len(kept) > max_bytes:
                del kept[: len(kept) - max_bytes]
        if not timed_out:
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        if timed_out:
            _kill_and_reap(proc)
            return 124, b""
        return proc.returncode, bytes(kept)
    except BaseException:
        # This is a bounded journal/status capture or update submission.  Keep its
        # lifetime inside this call even when capture or waiting is interrupted.
        try:
            if proc.poll() is None:
                _kill_and_reap(proc)
        except BaseException:  # noqa: BLE001, S110 - Preserve the original interruption.
            pass
        raise
    finally:
        if selector is not None:
            try:
                selector.close()
            except BaseException:  # noqa: BLE001, S110 - Best-effort cleanup must preserve the result.
                pass
        try:
            proc.stdout.close()
        except BaseException:  # noqa: BLE001, S110 - Preserve the original interruption.
            pass


def _kill_and_reap(proc):
    """Kill `proc` and wait for it, but never without a limit: a child that will not die is left
    to the system instead of holding this helper forever."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        pass


def run_bounded_read(argv, timeout, env, max_bytes, stderr_max=DOCKER_STDERR_MAX):
    """(exit status, stdout, stderr, over).  An argv list, never a shell, stdin closed.

    Stdout is never held beyond `max_bytes`: the first byte past the limit kills the child at
    once and answers `over` (True) with no stdout, so a huge file costs neither memory nor the
    whole timeout.  Stderr is read alongside (so a chatty child cannot stall on a full pipe) and
    only its first `stderr_max` bytes are kept, for a caller that classifies it and never repeats
    it.  A timeout is status 124 and a program that cannot start is 127.  After a kill the wait
    for the child is bounded."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise Refused("internal: not an argv list")
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd="/",
            shell=False,
        )
    except OSError:
        return 127, b"", b"", False
    out = bytearray()
    err = bytearray()
    over = False
    timed_out = False
    selector = selectors.DefaultSelector()
    try:
        deadline = time.monotonic() + timeout
        selector.register(proc.stdout, selectors.EVENT_READ, "out")
        selector.register(proc.stderr, selectors.EVENT_READ, "err")
        while selector.get_map() and not over:
            remaining = deadline - time.monotonic()
            events = selector.select(remaining) if remaining > 0 else []
            if not events:
                timed_out = True
                break
            for key, _ in events:
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "out":
                    out += chunk
                    if len(out) > max_bytes:
                        over = True
                        break
                elif len(err) < stderr_max:
                    err += chunk[: stderr_max - len(err)]
        if not (over or timed_out):
            try:
                proc.wait(timeout=max(0.0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
        if over:
            _kill_and_reap(proc)
            return -9, b"", bytes(err), True
        if timed_out:
            _kill_and_reap(proc)
            return 124, b"", bytes(err), False
        return proc.returncode, bytes(out), bytes(err), False
    except BaseException:
        try:
            if proc.poll() is None:
                _kill_and_reap(proc)
        except BaseException:  # noqa: BLE001, S110 - Preserve the original interruption.
            pass
        raise
    finally:
        try:
            selector.close()
        except BaseException:  # noqa: BLE001, S110 - Best-effort cleanup must preserve the result.
            pass
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except BaseException:  # noqa: BLE001, S110 - Preserve the original interruption.
                pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a 3xx: the token must not be sent to another origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_get(url, headers, timeout=HTTP_TIMEOUT):
    """The HTTP status of one GET, or 0 when there was no answer.  The body is discarded."""
    opener = urllib.request.build_opener(_NoRedirect)
    request = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response:
            response.read(64 * 1024)
            return response.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (urllib.error.URLError, OSError, ValueError):
        return 0


class Context:
    """Everything that touches the machine, so the tests can replace each piece."""

    def __init__(
        self,
        platform=None,
        uid=None,
        home=None,
        environ=None,
        runner=None,
        http=None,
        now=None,
        config_path=CONFIG_PATH,
        status_path=None,
        audit_path=None,
        fleet_secrets=None,
        credentials_path=None,
        pm2_node=None,
        pm2_script=None,
        sleep=None,
        monotonic=None,
        reader=None,
    ):
        self.platform = platform or sys.platform
        self.uid = os.getuid() if uid is None else uid
        self.home = Path(home) if home else Path.home()
        self.environ = dict(os.environ if environ is None else environ)
        self.runner = runner or run_command
        self.http = http or http_get
        self.now = now or time.time
        self.config_path = config_path
        self.status_path = status_path or (
            self.home / ".local" / "state" / "fleet-tools" / "server-status.json"
        )
        self.fleet_secrets = fleet_secrets
        self.credentials_path = credentials_path or (
            self.home / ".config" / "fleet-tools" / "ops-control.env"
        )
        self.audit_path = audit_path or (
            self.home / ".local" / "state" / "fleet-tools" / "ops-audit.jsonl"
        )
        self.pm2_node = pm2_node
        self.pm2_script = pm2_script
        self.sleep = sleep or time.sleep
        self.monotonic = monotonic or time.monotonic
        # The bounded reader of `server eavesdropper-status` (see run_bounded_read).
        self.reader = reader or run_bounded_read

    def child_env(self):
        env = {k: v for k, v in self.environ.items() if k in CHILD_ENV_KEEP}
        env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        if self.platform.startswith("linux") and "XDG_RUNTIME_DIR" not in env:
            # `systemctl --user` from an ssh forced command needs the user manager's socket.
            runtime = f"/run/user/{self.uid}"
            if os.path.isdir(runtime):
                env["XDG_RUNTIME_DIR"] = runtime
        return env

    def pm2_env(self):
        env = self.child_env()
        env.setdefault("PM2_HOME", str(self.home / ".pm2"))
        env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        return env


# MARK: - Building the commands


def mac_commands(ctx, config, action, label):
    if not ctx.platform.startswith("darwin"):
        raise Refused("mac verbs run only on the Mac")
    if not LABEL_RE.fullmatch(label or ""):
        raise Refused("the label is not a valid launchd label")
    allowed = config["agents"].get(label)
    if allowed is None:
        raise Refused("the label is not in the allowlist")
    if action not in allowed:
        raise Refused("that action is not allowed for this label")
    domain = f"gui/{ctx.uid}"
    service = f"{domain}/{label}"
    status, _ = ctx.runner([LAUNCHCTL, "print", service], timeout=15, env=ctx.child_env())
    loaded = status == 0
    if action == "stop":
        return ([[LAUNCHCTL, "bootout", service]] if loaded else []), (
            "stopped" if loaded else "not loaded, nothing to stop"
        )
    if loaded:
        argv = [LAUNCHCTL, "kickstart"] + (["-k"] if action == "restart" else []) + [service]
        return [argv], "loaded"
    plist = ctx.home / "Library" / "LaunchAgents" / f"{label}.plist"
    try:
        info = os.lstat(plist)
    except OSError:
        raise Refused("the agent is not installed") from None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != ctx.uid or info.st_mode & 0o022:
        raise Refused("the agent's plist is not a private regular file")
    return [
        [LAUNCHCTL, "bootstrap", domain, str(plist)],
        [LAUNCHCTL, "kickstart", service],
    ], "not loaded, bootstrapping it"


def _systemctl():
    for candidate in SYSTEMCTL_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return SYSTEMCTL_CANDIDATES[0]


def _systemctl_argv(scope, *rest):
    return [_systemctl()] + (["--user"] if scope == "user" else []) + list(rest)


def server_unit_commands(ctx, config, action, unit):
    _server_only(ctx)
    if not UNIT_RE.fullmatch(unit or ""):
        raise Refused("the unit is not a valid unit name")
    entry = config["units"].get(unit)
    if entry is None:
        raise Refused("the unit is not in the allowlist")
    if action not in entry["actions"]:
        raise Refused("that action is not allowed for this unit")
    return [_systemctl_argv(entry["scope"], action, unit)], f"{entry['scope']} unit"


def _journalctl():
    for candidate in JOURNALCTL_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return JOURNALCTL_CANDIDATES[0]


def server_logs_commands(ctx, config, unit):
    _server_only(ctx)
    if not UNIT_RE.fullmatch(unit or ""):
        raise Refused("the unit is not a valid unit name")
    entry = config["units"].get(unit)
    if entry is None:
        raise Refused("the unit is not in the allowlist")
    argv = (
        [_journalctl()]
        + (["--user"] if entry["scope"] == "user" else [])
        + ["-u", unit, "-n", str(LOG_MAX_LINES), "--no-pager"]
    )
    return [argv], f"last {LOG_MAX_LINES} journal lines, read only"


def truncate_logs(raw):
    """(text, kept journal line count) for a logs `--yes` answer.  At most the last
    LOG_MAX_LINES lines and at most LOG_MAX_BYTES bytes, with the truncation
    note first when anything was cut.  The note is not a journal line, so the count
    is journal lines only, which is how every consumer words it."""
    truncated = len(raw) > LOG_MAX_BYTES
    if truncated:
        raw = raw[-LOG_MAX_BYTES:]
        # Drop a partial first line the byte cut may have made.
        cut = raw.find(b"\n")
        if cut >= 0:
            raw = raw[cut + 1 :]
    text = raw.decode("utf-8", "replace")
    lines = text.splitlines(keepends=True)
    if len(lines) > LOG_MAX_LINES:
        truncated = True
        lines = lines[-LOG_MAX_LINES:]
    kept = "".join(lines)
    if truncated:
        kept = LOG_TRUNCATED_NOTE + "\n" + kept
    return kept, len(lines)


def _docker():
    for candidate in DOCKER_CANDIDATES:
        if os.path.isfile(candidate):
            return candidate
    return DOCKER_CANDIDATES[0]


def _module_trusted(info, owners):
    """True when an opened module file or folder is owned by one of `owners` and neither group
    nor others can write it."""
    return info.st_uid in owners and not info.st_mode & 0o022


def _read_module_source(ctx):
    """The reducer module's bytes from the helper's own folder, or None when it is missing or
    not trusted.

    The folder and then the file are opened with O_NOFOLLOW (so a symlink is refused), both are
    judged by fstat on the descriptor that was opened, and the bytes are read from that same
    descriptor: the path is never looked at again, so a file swapped in afterwards changes
    nothing.  A folder owned by TRUSTED_MODULE_UID is the installed layout (the file must be that
    owner's as well); any other folder is a checkout and may also be the calling account's,
    file and folder both.  The file is opened non-blocking, so a pipe put in its place is refused
    by its type instead of waiting."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    opened = []
    try:
        fd = os.open(str(HELPER_DIR), directory_flags)
        opened.append(fd)
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            return None
        installed = info.st_uid == TRUSTED_MODULE_UID
        owners = (TRUSTED_MODULE_UID,) if installed else (TRUSTED_MODULE_UID, ctx.uid)
        # A checkout folder must be the calling account's own, or root's: nobody else's.
        if not _module_trusted(info, (TRUSTED_MODULE_UID, ctx.uid)):
            return None
        file_fd = os.open(
            REDUCED_STATUS_NAME, os.O_RDONLY | os.O_NONBLOCK | nofollow | cloexec, dir_fd=fd
        )
        opened.append(file_fd)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode) or not _module_trusted(info, owners):
            return None
        if info.st_size > REDUCED_STATUS_MAX_BYTES:
            return None
        chunks = []
        total = 0
        while True:
            chunk = os.read(file_fd, 65536)
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > REDUCED_STATUS_MAX_BYTES:
                return None
            chunks.append(chunk)
    except OSError:
        return None
    finally:
        for descriptor in opened:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _load_reduced_status(ctx):
    """The reduced_status module, or None when it cannot be trusted or loaded.

    The bytes come from `_read_module_source` and are compiled and run in memory into a fresh
    module object: no import system, no sys.path (so no module of the same name can be put
    in front of it), no __pycache__ and no second open of the path."""
    source = _read_module_source(ctx)
    if source is None:
        return None
    try:
        code = compile(source, REDUCED_STATUS_NAME, "exec", dont_inherit=True)
        module = types.ModuleType("reduced_status")
        exec(code, module.__dict__)  # noqa: S102 - the bytes were just judged trusted
    except (Exception, SystemExit):  # noqa: BLE001 - any load failure is the same answer
        return None
    return module if callable(getattr(module, "reduce_status", None)) else None


def _docker_env(config_dir):
    """The whole environment of the docker call: nothing from the caller's."""
    return {
        "PATH": DOCKER_PATH,
        "DOCKER_HOST": DOCKER_HOST,
        "DOCKER_CONFIG": config_dir,
        "HOME": config_dir,
    }


def _docker_failure(stderr):
    """The reason word for a `docker exec` that exited non-zero.  Only the start of stderr is
    looked at, and only to compare it with docker's known phrases for a container that is not
    there or not running; anything else is `read_failed`, and the text is never kept."""
    text = (stderr or b"")[:DOCKER_STDERR_MAX].decode("utf-8", "replace").lower()
    if any(phrase in text for phrase in DOCKER_NOT_RUNNING_PHRASES):
        return "container_not_running"
    return "read_failed"


def read_eavesdropper_status(ctx):
    """(the status.json document, None) or (None, a reason word from EAVESDROPPER_UNAVAILABLE).

    One argv, run without a shell and without sudo:
    `docker exec --user simpletools eavesdropper /bin/cat /data/listener/status.json`.  The environment is
    pinned (`_docker_env`, with an empty private temporary directory as the docker config), the
    read stops at the first byte past the limit and answers `too_large` at once, and the reason
    word is all a caller learns of a failure: docker's own words are only compared with known
    phrases, never kept.

    Privilege: the caller's account reaches the docker socket (root:docker, mode 0660) through its
    membership of the `docker` group, so no sudo, no sudoers entry and no root shell is used.
    `--user simpletools` runs `cat` as the container's own unprivileged account, which owns the
    0600 file, so the read is no wider than that account's.  The container, the user and the
    path are constants here, so no word from a request can reach docker."""
    argv = [
        _docker(),
        "exec",
        "--user",
        EAVESDROPPER_USER,
        EAVESDROPPER_CONTAINER,
        EAVESDROPPER_CAT,
        EAVESDROPPER_STATUS_PATH,
    ]
    try:
        scratch = tempfile.TemporaryDirectory(prefix="fleet-ops-docker-")
    except OSError:
        return None, "docker_unavailable"
    with scratch as config_dir:
        status, out, err, over = ctx.reader(
            argv,
            timeout=EAVESDROPPER_READ_TIMEOUT,
            env=_docker_env(config_dir),
            max_bytes=EAVESDROPPER_STATUS_MAX_BYTES,
        )
    if over:
        return None, "too_large"
    if status == 127:
        return None, "docker_unavailable"
    if status == 124:
        return None, "timeout"
    if status != 0:
        return None, _docker_failure(err)
    try:
        document = json.loads((out or b"").decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "not_json"
    if not isinstance(document, dict):
        return None, "not_json"
    return document, None


def _pm2_prefix(ctx):
    node = ctx.pm2_node
    if node is None:
        node = next((c for c in NODE_CANDIDATES if os.access(c, os.X_OK)), NODE_CANDIDATES[0])
    script = ctx.pm2_script
    if script is None:
        script = next((c for c in PM2_CANDIDATES if os.access(c, os.X_OK)), PM2_CANDIDATES[0])
    return [node, script]


def _pm2_record(ctx, name):
    """(status, pid) pm2 has for `name`, "missing" when it has no record, else None.

    None means pm2 could not be asked or its listing could not be read, so nothing is
    known and the caller plans the plain verb."""
    status, out = ctx.runner(
        _pm2_prefix(ctx) + ["jlist"],
        timeout=15,
        env=ctx.pm2_env(),
        want_stdout=True,
        max_bytes=1024 * 1024,
    )
    if status != 0:
        return None
    try:
        listing = json.loads((out or b"").decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(listing, list):
        return None
    for row in listing:
        if not isinstance(row, dict) or row.get("name") != name:
            continue
        inner = row.get("pm2_env") if isinstance(row.get("pm2_env"), dict) else {}
        pid = row.get("pid") if isinstance(row.get("pid"), int) else inner.get("pid")
        return inner.get("status"), pid if isinstance(pid, int) else 0
    return "missing"


def pm2_commands(ctx, config, action, name):
    if not ctx.platform.startswith("darwin"):
        raise Refused("pm2 verbs run only on the Mac")
    if not PM2_RE.fullmatch(name or ""):
        raise Refused("the pm2 name is not valid")
    allowed = config["pm2"].get(name)
    if allowed is None:
        raise Refused("the pm2 application is not in the allowlist")
    if action not in allowed:
        raise Refused("that action is not allowed for this pm2 application")
    if action == "start":
        record = _pm2_record(ctx, name)
        if record == "missing":
            raise Refused(
                "pm2 no longer remembers this application and this tool does not recreate it"
            )
        if record is not None:
            status_word, pid = record
            if status_word == "online" and pid <= 0:
                return [_pm2_prefix(ctx) + ["restart", name]], "stale pm2 record, restarting it"
    return [_pm2_prefix(ctx) + [action, name]], "pm2 application"


def server_update_commands(ctx, config, name):
    _server_only(ctx)
    if not UPDATE_RE.fullmatch(name or ""):
        raise Refused("the update name is not valid")
    entry = config["updates"].get(name)
    if entry is None:
        raise Refused("the update is not in the allowlist")
    before = update_unit_state(ctx, entry)
    require_update_idle(before)
    return [update_command(ctx, name, entry)], (
        "submits the update and checks a new service invocation for bounded completion"
    )


def update_command(ctx, name, entry):
    # General OS sudo access is not general app authority.  Only this immutable mapping
    # may use the existing grant; config cannot choose another privileged executable/unit.
    if entry["scope"] == "system":
        if name != "fleet-cli" or entry["unit"] != FLEET_CLI_UPDATE_UNIT:
            raise Refused(
                "this system update has no approved application route",
                error_code="update_route_unavailable",
            )
        if not os.path.isfile(UPDATE_SYSTEMCTL) or not os.access(UPDATE_SYSTEMCTL, os.X_OK):
            raise Refused(
                "the approved systemctl executable is unavailable",
                error_code="update_executable_unavailable",
            )
        argv = [UPDATE_SYSTEMCTL, "start", "--no-block", FLEET_CLI_UPDATE_UNIT]
    else:
        argv = _systemctl_argv(entry["scope"], "start", "--no-block", entry["unit"])
    if entry["scope"] == "system" and ctx.uid != 0:
        if not os.path.isfile(SUDO) or not os.access(SUDO, os.X_OK):
            raise Refused(
                "the approved authorization executable is unavailable",
                error_code="authorization_unavailable",
            )
        # Query this exact argv; never grant permission or elevate the broker itself.
        status, _ = ctx.runner(
            [SUDO, "-n", "-l", "--"] + argv,
            timeout=UPDATE_PROBE_TIMEOUT,
            env=ctx.child_env(),
        )
        if status != 0:
            raise Refused(
                "the update needs existing noninteractive permission for this exact system unit",
                error_code="authorization_unavailable",
            )
        argv = [SUDO, "-n"] + argv
    return argv


def update_unit_state(ctx, entry, timeout=UPDATE_PROBE_TIMEOUT):
    status, raw = ctx.runner(
        _systemctl_argv(
            entry["scope"],
            "show",
            entry["unit"],
            "--property=" + ",".join(UPDATE_PROPERTIES),
        ),
        timeout=timeout,
        env=ctx.child_env(),
        want_stdout=True,
        max_bytes=16 * 1024,
    )
    if status != 0:
        raise Refused("the update unit status is unavailable", error_code="unit_status_unavailable")
    state = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in UPDATE_PROPERTIES or key in state:
            raise Refused("the update unit status is invalid", error_code="unit_status_invalid")
        state[key] = value
    if state.get("LoadState") in ("not-found", "masked", "error", "bad-setting"):
        raise Refused("the update unit is missing or unavailable", error_code="unit_unavailable")
    if set(state) != set(UPDATE_PROPERTIES) or state["LoadState"] != "loaded":
        raise Refused("the update unit status is incomplete", error_code="unit_status_invalid")
    if state["Type"] != "oneshot":
        raise Refused(
            "the update unit must be a oneshot service", error_code="unit_type_unsupported"
        )
    if state["InvocationID"] and not re.fullmatch(r"[0-9a-f]{32}", state["InvocationID"]):
        raise Refused("the update invocation status is invalid", error_code="unit_status_invalid")
    for key in (
        "ExecMainCode",
        "ExecMainStatus",
        "ExecMainStartTimestampMonotonic",
        "ExecMainExitTimestampMonotonic",
    ):
        if not re.fullmatch(r"[0-9]{1,20}", state[key]):
            raise Refused("the update process status is invalid", error_code="unit_status_invalid")
        state[key] = int(state[key])
        if state[key] > UINT64_MAX:
            raise Refused("the update process status is invalid", error_code="unit_status_invalid")
    return state


def require_update_idle(state):
    if state["ActiveState"] not in ("inactive", "failed") or state["Job"]:
        raise Refused(
            "the update is already running or has a pending job", error_code="update_busy"
        )


def update_failure_code(status, output):
    """Classify bounded private stderr; none of its text reaches the receipt or audit."""
    text = output.decode("utf-8", "replace").lower()
    if status == 124:
        return "update_submit_timeout"
    if status == 127:
        return "update_executable_unavailable"
    if any(
        word in text
        for word in (
            "access denied",
            "permission denied",
            "authentication required",
            "authentication is required",
            "password is required",
            "not allowed to execute",
            "a terminal is required",
            "incorrect password",
        )
    ):
        return "authorization_unavailable"
    if "not found" in text or "does not exist" in text or "masked" in text:
        return "unit_unavailable"
    return "update_submit_failed"


def execute_update(ctx, config, target, commands):
    entry = config["updates"][target]
    # Recheck immediately before submission, rather than trusting an earlier plan.
    before = update_unit_state(ctx, entry)
    require_update_idle(before)
    command = update_command(ctx, target, entry)
    if commands != [command]:
        raise Refused("the update execution route changed", error_code="update_route_changed")
    status, output = ctx.runner(
        command,
        timeout=UPDATE_PROBE_TIMEOUT,
        env=ctx.child_env(),
        want_stdout=True,
        max_bytes=8192,
        combine_stderr=True,
    )
    if status != 0:
        raise Failed(
            "the update submission was not confirmed",
            error_code=update_failure_code(status, output),
        )
    answer = dict(
        ok=True,
        result="submitted",
        executionState="submitted",
        verified=False,
        detail="the update was submitted; completion has not been observed",
        baselineStartMonotonic=before["ExecMainStartTimestampMonotonic"],
    )
    invocation = None
    deadline = ctx.monotonic() + UPDATE_VERIFY_SECONDS
    for attempt in range(UPDATE_POLL_ATTEMPTS):
        remaining = deadline - ctx.monotonic()
        if remaining <= 0:
            break
        if attempt:
            ctx.sleep(min(UPDATE_POLL_INTERVAL, remaining))
        remaining = deadline - ctx.monotonic()
        if remaining <= 0:
            break
        try:
            state = update_unit_state(ctx, entry, timeout=min(UPDATE_PROBE_TIMEOUT, remaining))
        except Refused:
            answer["detail"] = "the update was submitted; completion status is unavailable"
            return answer, 0
        current = state["InvocationID"]
        if not current or current == before["InvocationID"]:
            continue
        if invocation is not None and current != invocation:
            answer["detail"] = (
                "the update was submitted; another invocation replaced its completion status"
            )
            answer.pop("invocationID", None)
            answer.update(executionState="expired", errorCode="invocation_replaced")
            return answer, 0
        invocation = current
        answer, code = update_observation(state, current, before["ExecMainStartTimestampMonotonic"])
        if answer["verified"]:
            return answer, code
    return answer, 0


def update_observation(state, invocation, previous_start):
    answer = dict(
        ok=True,
        result="submitted",
        executionState="submitted",
        verified=False,
        invocationID=invocation,
        baselineStartMonotonic=previous_start,
        detail="the update invocation has not completed with a verified result",
    )
    if state["InvocationID"] != invocation:
        answer.update(
            executionState="expired",
            errorCode="invocation_not_current",
            detail="the captured update invocation is no longer current; its completion cannot be verified",
        )
        return answer, 0
    terminal = state["ActiveState"] in ("inactive", "failed") and not state["Job"]
    if not terminal:
        return answer, 0
    if state["Result"] and state["Result"] != "success":
        answer.update(
            ok=False,
            result="failed",
            executionState="failed",
            verified=True,
            errorCode="update_invocation_failed",
            detail="the captured update service invocation failed",
        )
        return answer, 1
    started = state["ExecMainStartTimestampMonotonic"]
    if (
        state["Result"] == "success"
        and state["ActiveState"] == "inactive"
        and state["ExecMainCode"] == 1
        and state["ExecMainStatus"] == 0
        and started > previous_start
        and state["ExecMainExitTimestampMonotonic"] >= started
    ):
        answer.update(
            result="done",
            executionState="completed",
            verified=True,
            detail="the captured update service invocation completed successfully",
        )
    return answer, 0


def update_status(ctx, config, target, mode):
    _server_only(ctx)
    if mode != "plan":
        raise Refused("update status is read only; pass --plan", error_code="read_only_required")
    match = UPDATE_STATUS_RE.fullmatch(target or "")
    if not match:
        raise Refused(
            "update status requires a captured fleet-cli invocation ID",
            error_code="invocation_id_invalid",
        )
    baseline = int(match.group(2))
    if baseline > UINT64_MAX:
        raise Refused("the captured update baseline is invalid", error_code="invocation_id_invalid")
    entry = config["updates"].get("fleet-cli")
    if entry != {"unit": FLEET_CLI_UPDATE_UNIT, "scope": "system"}:
        raise Refused(
            "the fleet-cli status route is unavailable", error_code="update_route_unavailable"
        )
    if captured_update_baseline(ctx, match.group(1)) != baseline:
        raise Refused(
            "the update receipt does not match its recorded baseline",
            error_code="receipt_context_mismatch",
        )
    # Never submit or query sudo on this path, and never substitute a newer ID.
    state = update_unit_state(ctx, entry)
    return update_observation(state, match.group(1), baseline)


def captured_update_baseline(ctx, invocation):
    """Read the bounded existing private audit; never trust client freshness alone.

    This is capture provenance for the restricted route, not protection against code
    already running as the server account that owns the audit.
    """
    descriptors = []
    try:
        path = Path(ctx.audit_path).absolute()
        directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open("/", directory_flags)
        descriptors.append(parent)
        for component in path.parts[1:-1]:
            parent = os.open(component, directory_flags, dir_fd=parent)
            descriptors.append(parent)
            info = os.fstat(parent)
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, ctx.uid) or (info.st_mode & 0o022 and not sticky_root):
                raise OSError("unsafe audit directory")
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
            dir_fd=parent,
        )
        descriptors.append(fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ctx.uid
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OSError("unsafe audit file")
        offset = max(0, info.st_size - AUDIT_CONTEXT_MAX_BYTES)
        os.lseek(fd, offset, os.SEEK_SET)
        remaining = min(info.st_size, AUDIT_CONTEXT_MAX_BYTES)
        chunks = []
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if offset:
            data = data.partition(b"\n")[2]
        lines = data.decode("utf-8").splitlines()
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("invocationID") != invocation:
                continue
            baseline = row.get("baselineStartMonotonic")
            if (
                row.get("host") == "server"
                and row.get("verb") == "server update"
                and row.get("target") == "fleet-cli"
                and row.get("route") in ("sudo-system-update", "root-system-update")
                and row.get("result") in ("submitted", "done", "failed")
                and type(baseline) is int
                and 0 <= baseline <= UINT64_MAX
            ):
                return baseline
            break
    except (OSError, ValueError, UnicodeError, RecursionError):
        pass
    finally:
        for fd in reversed(descriptors):
            try:
                os.close(fd)
            except OSError:
                pass
    raise Refused(
        "the captured update receipt context is unavailable",
        error_code="receipt_context_unavailable",
    )


def _server_only(ctx):
    if not ctx.platform.startswith("linux"):
        raise Refused("server verbs run only on the server")


def coolify_resource(ctx, uuid):
    """The Coolify row in the latest server-status.json whose uuid is `uuid`, or Refused.

    The report must be a private regular file of ours, written by fleet-server-status, and
    no older than STATUS_MAX_AGE_SECONDS."""
    path = ctx.status_path
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        raise Refused("there is no server status report") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ctx.uid
            or info.st_mode & 0o022
            or info.st_size > STATUS_MAX_BYTES
        ):
            raise Refused("the server status report is not a private regular file")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(STATUS_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise Refused("the server status report is not JSON") from None
    if not isinstance(doc, dict) or doc.get("tool") != "fleet-server-status":
        raise Refused("the server status report is not from fleet-server-status")
    generated = _parse_time(doc.get("generatedAt"))
    if generated is None or ctx.now() - generated > STATUS_MAX_AGE_SECONDS:
        raise Refused("the server status report is stale")
    for row in doc.get("coolify") or []:
        if isinstance(row, dict) and row.get("uuid") == uuid:
            refusal = redeploy_refusal(row)
            if refusal:
                raise Refused(refusal)
            return row
    raise Refused("the uuid is not in the latest server status report")


def redeploy_refusal(row):
    """Why the first report row with a uuid cannot be redeployed, or None when it can."""
    if row.get("type") not in REDEPLOYABLE_TYPES:
        return "only a Coolify application can be redeployed"
    if not uuid_from_label(row):
        return "the report has no Coolify uuid for that resource"
    # The collector puts live rows first, so a first row that is not live says that
    # nothing with this uuid is on the host now.
    if row.get("status") in ("absent", "unknown"):
        return "that application is not running on the server"
    return None


def uuid_from_label(row):
    """True only when the row's `uuid` came from a Coolify uuid label.

    With no uuid label, fleet-server-status puts the resource name (or the container name)
    in `uuid`, and may add `" (service)"` to `name`.  A row that says where its uuid came
    from (`uuidSource`) is believed only when it says `label`; otherwise a uuid that is the
    row's own name is taken for a name."""
    source = row.get("uuidSource")
    if source is not None:
        return source == "label"
    uuid, name = row.get("uuid"), row.get("name")
    if not isinstance(uuid, str) or not isinstance(name, str):
        return False
    return name != uuid and not name.startswith(uuid + " (")


def _parse_time(text):
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return moment.replace(tzinfo=datetime.timezone.utc).timestamp()


def coolify_base(ctx):
    """The Coolify origin the token is sent to.  It is always on this host:  the variable
    may change the scheme or the port, never the host, so no environment can carry the
    token to another machine."""
    base = (ctx.environ.get("COOLIFY_BASE_URL") or COOLIFY_DEFAULT_BASE).strip().rstrip("/")
    # The path is appended to the base as text, so an empty query or fragment (`?`, `#`)
    # or empty userinfo (`@`) would move it; none of them belongs in an origin.
    if any(c in base for c in "@?#"):
        raise Refused("COOLIFY_BASE_URL must be a loopback origin")
    try:
        parts = urllib.parse.urlsplit(base)
        parts.port
    except ValueError:
        raise Refused("COOLIFY_BASE_URL must be a loopback origin") from None
    if (
        parts.scheme not in ("http", "https")
        or parts.hostname not in ("127.0.0.1", "localhost", "::1")
        or parts.path
    ):
        raise Refused("COOLIFY_BASE_URL must be a loopback origin")
    return base


def read_coolify_token(ctx):
    """The Coolify API token, from fleet-secrets, into memory only.  Raises Failed."""
    binary = ctx.fleet_secrets
    for candidate in () if binary else FLEET_SECRETS_CANDIDATES:
        path = os.path.expanduser(candidate)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            binary = path
            break
    if binary is None:
        raise Failed("fleet-secrets is not installed")
    status, out = ctx.runner(
        [binary, "get", COOLIFY_TOKEN_NAME, "--path", COOLIFY_TOKEN_PATH],
        timeout=SECRET_TIMEOUT,
        env=secrets_env(ctx),
        want_stdout=True,
    )
    token = out.decode("utf-8", "replace").strip() if status == 0 else ""
    out = b""
    if not token or any(c.isspace() or ord(c) < 33 for c in token):
        raise Failed("the Coolify token is not available")
    return token


def secrets_env(ctx):
    """The environment the fleet-secrets child runs with.  Raises Failed.

    The private file supplies only this caller's scoped read-only service token.  It is mapped
    to INFISICAL_TOKEN for the one fleet-secrets child, never to an argv or a service-control
    child.  Inherited shared token and machine-identity bindings are discarded."""
    env = dict(ctx.environ)
    process_token = env.pop(SCOPED_TOKEN_NAME, "")
    for name in SHARED_AUTH_ENV_NAMES:
        env.pop(name, None)
    try:
        fd = os.open(str(ctx.credentials_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        value = process_token
        if value:
            if (
                not value.startswith("st.")
                or not 20 <= len(value) <= 2048
                or any(c.isspace() for c in value)
            ):
                raise Failed("the scoped Infisical token is malformed")
            env["INFISICAL_TOKEN"] = value
        return env
    except OSError:
        raise Failed("the Infisical credential file cannot be read") from None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != ctx.uid
            or info.st_mode & 0o077
            or info.st_size > CREDENTIALS_MAX_BYTES
        ):
            raise Failed("the Infisical credential file is not private")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(CREDENTIALS_MAX_BYTES + 1)
    finally:
        os.close(fd)
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise Failed("the Infisical credential file is malformed") from None
    values = {}
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, sep, value = line.partition("=")
        if (
            not sep
            or name not in CREDENTIAL_NAMES
            or name in values
            or not value
            or any(c.isspace() or ord(c) < 33 or c in "\"'" for c in value)
        ):
            raise Failed("the Infisical credential file is malformed")
        values[name] = value
    if SCOPED_TOKEN_NAME not in values:
        raise Failed("the Infisical credential file is malformed")
    token = values.get(SCOPED_TOKEN_NAME)
    if token and (
        not token.startswith("st.")
        or not 20 <= len(token) <= 2048
        or any(c.isspace() for c in token)
    ):
        raise Failed("the Infisical credential file is malformed")
    if token:
        env["INFISICAL_TOKEN"] = token
    return env


def check_token(ctx):
    """(answer, exit status) for `--check-token`: read the token as a redeploy would, then
    ask Coolify for its version with it.  Nothing is changed and the token is not shown."""
    answer = _refusal("")
    answer["verb"] = "check-token"
    try:
        _server_only(ctx)
        base = coolify_base(ctx)
        token = read_coolify_token(ctx)
        try:
            status = ctx.http(
                base + "/api/v1/version",
                {"Authorization": "Bearer " + token, "Accept": "application/json"},
            )
        finally:
            token = ""
        if not 200 <= status < 300:
            raise Failed(f"Coolify answered HTTP {status}" if status else "Coolify did not answer")
    except Refused as exc:
        answer.update(detail=str(exc))
        return answer, 2
    except Failed as exc:
        answer.update(result="failed", detail=str(exc))
        return answer, 1
    answer.update(ok=True, result="done", detail="the Coolify token is available and accepted")
    return answer, 0


# MARK: - The verbs


def plan_request(ctx, config, scope, action, target):
    """(commands, note, audit target) for one request, or Refused."""
    if scope == "mac":
        if action not in ACTIONS:
            raise Refused("unknown mac verb")
        commands, note = mac_commands(ctx, config, action, target)
        return commands, note, target
    if scope == "pm2":
        if action not in ACTIONS:
            raise Refused("unknown pm2 verb")
        commands, note = pm2_commands(ctx, config, action, target)
        return commands, note, target
    if scope != "server":
        raise Refused("unknown verb")
    if action in ACTIONS:
        commands, note = server_unit_commands(ctx, config, action, target)
        return commands, note, target
    if action == "logs":
        commands, note = server_logs_commands(ctx, config, target)
        return commands, note, target
    if action == "update":
        commands, note = server_update_commands(ctx, config, target)
        return commands, note, target
    if action == "redeploy":
        _server_only(ctx)
        if not config["redeploy"]:
            raise Refused("redeploy is not allowed")
        if not UUID_RE.fullmatch(target or ""):
            raise Refused("the uuid is not valid")
        row = coolify_resource(ctx, target)
        query = urllib.parse.urlencode({"uuid": target})
        name = row.get("name") if isinstance(row.get("name"), str) else "coolify resource"
        # A redeploy covers the whole application, so the audit names it and not one service.
        service = row.get("service")
        if isinstance(service, str) and name.endswith(f" ({service})"):
            name = name[: -len(f" ({service})")]
        return [["coolify", "GET", f"/api/v1/deploy?{query}"]], "redeploy", name[:120]
    raise Refused("unknown server verb")


def execute(ctx, scope, action, target, commands):
    """Run the planned commands in order; the first failure stops the rest.  Returns detail."""
    if scope == "server" and action == "redeploy":
        base = coolify_base(ctx)
        token = read_coolify_token(ctx)
        try:
            status = ctx.http(
                base + commands[0][2],
                {"Authorization": "Bearer " + token, "Accept": "application/json"},
            )
        finally:
            token = ""
        if not 200 <= status < 300:
            raise Failed(f"Coolify answered HTTP {status}" if status else "Coolify did not answer")
        return f"Coolify accepted the redeploy (HTTP {status})"
    env = ctx.pm2_env() if scope == "pm2" else ctx.child_env()
    finished = 0
    for argv in commands:
        status, _ = ctx.runner(argv, timeout=COMMAND_TIMEOUT, env=env)
        if status != 0:
            verb = next((a for a in argv[1:] if not a.startswith("-") and "/" not in a), action)
            raise Failed(f"{os.path.basename(argv[0])} {verb} exited {status}", completed=finished)
        finished += 1
    return "done" if commands else "nothing to do"


@contextlib.contextmanager
def action_lock(ctx):
    """Hold the one `--yes` lock on this machine, or refuse at once.

    A second caller, such as a second Muster or a shell, gets a fixed refusal instead of
    racing the first one on the same unit.  The lock is released when the process exits,
    however it exits."""
    directory = ctx.home / ".local" / "state" / "fleet-tools"
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.lstat()
        fd = os.open(
            directory / "ops-control.lock",
            os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError:
        raise Refused(
            "the action lock is unavailable", error_code="action_lock_unavailable"
        ) from None
    try:
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise Refused(
                "the action lock directory is untrusted", error_code="action_lock_unavailable"
            )
        lock_info = os.fstat(fd)
        if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_uid != os.getuid():
            raise Refused("the action lock is untrusted", error_code="action_lock_unavailable")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused(
                "another action is still running on this machine", error_code="action_busy"
            ) from None
        yield
    finally:
        os.close(fd)


def execute_logs(ctx, commands):
    """(detail, text, line count) for a logs `--yes`.  The journal text goes to the
    answer only; the detail and the audit carry the count and nothing else."""
    argv = commands[0]
    status, out = ctx.runner(
        argv,
        timeout=COMMAND_TIMEOUT,
        env=ctx.child_env(),
        want_stdout=True,
        max_bytes=LOG_MAX_BYTES * 4,
    )
    if status != 0:
        raise Failed(f"journalctl exited {status}")
    text, lines = truncate_logs(out or b"")
    return f"{lines} journal lines", text, lines


def audit(
    ctx,
    scope,
    action,
    target,
    result,
    lines=None,
    route=None,
    invocation_id=None,
    verified=None,
    baseline=None,
    row_id=None,
):
    """One JSON line, 0600, appended.  Never command output, never a uuid or a hostname.
    A finished logs run adds its line count and nothing of the text."""
    row = {
        "time": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": "mac" if ctx.platform.startswith("darwin") else "server",
        "verb": f"{scope} {action}",
        "target": target,
        "result": result,
    }
    # A short id the caller's own log can store, so the two rows of one action link up.
    # The function answers True only when the row was written.
    if isinstance(row_id, str) and re.fullmatch(r"[0-9a-f]{12}", row_id):
        row["id"] = row_id
    if lines is not None:
        row["lines"] = lines
    if route in ("sudo-system-update", "root-system-update"):
        row["route"] = route
    if isinstance(invocation_id, str) and re.fullmatch(r"[0-9a-f]{32}", invocation_id):
        row["invocationID"] = invocation_id
    if isinstance(verified, bool):
        row["verified"] = verified
    if type(baseline) is int and 0 <= baseline <= UINT64_MAX:
        row["baselineStartMonotonic"] = baseline
    path = ctx.audit_path
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            str(path),
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError:
        return False
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != ctx.uid:
            return False
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(row, sort_keys=True) + "\n").encode("utf-8"))
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


def handle(ctx, scope, action, target, mode):
    """The whole request: the JSON answer and the exit status.  `mode` is plan, yes or None."""
    answer = {
        "ok": False,
        "verb": f"{scope} {action}",
        "target": target,
        "commands": [],
        "result": "refused",
        "detail": "",
    }
    audit_target = target
    log_lines = None
    try:
        if mode not in ("plan", "yes"):
            raise Refused("pass --plan to see the commands or --yes to run them")
        config = load_config(ctx.config_path)
        if scope == "server" and action == "update-status":
            receipt, code = update_status(ctx, config, target, mode)
            answer.update(receipt)
            return answer, code
        if scope == "server" and action == "seat-inventory":
            # A read of names and counts under this account's home, so it takes no lock, asks
            # for no sudo and writes no audit row.  Only `--plan` is accepted:  there is
            # nothing to confirm and nothing to run.
            # Like every server verb, it runs on the server only:  off it the module would read
            # another machine's home and still label the answer as the server's.
            _server_only(ctx)
            if mode != "plan":
                raise Refused("seat-inventory only reads:  pass --plan")
            # -I omits the script directory. Only this reviewed sibling directory is
            # added; neither the request nor the environment selects an import path.
            scripts = str(Path(__file__).resolve().parent)
            if scripts not in sys.path:
                sys.path.insert(0, scripts)
            import fleet_seat_inventory

            if not fleet_seat_inventory.PLATFORM_RE.fullmatch(target or "") or (
                target not in fleet_seat_inventory.PLATFORMS
            ):
                raise Refused("that platform has no seat inventory")
            document = fleet_seat_inventory.inventory(target, str(ctx.home), host="server")
            answer.update(
                ok=True,
                result="planned",
                detail="read-only inventory of what the platform has installed",
                inventory=document,
            )
            return answer, 0
        if scope == "server" and action == "eavesdropper-status":
            # A read of the server eavesdropper's own status document, reduced to names, counts,
            # tokens and times.  Like seat-inventory it takes no lock, asks for no sudo and writes
            # no audit row for a plan, and only `--plan` is accepted: there is nothing to run.
            # It runs on the server only, because the container it reads is the server's.
            _server_only(ctx)
            if mode != "plan":
                raise Refused("eavesdropper-status only reads: pass --plan")
            if target != EAVESDROPPER_TARGET:
                raise Refused("only the server's eavesdropper has a status here")
            document = reason = None
            module = _load_reduced_status(ctx)
            if module is None:
                reason = "module_unavailable"
            else:
                document, reason = read_eavesdropper_status(ctx)
            if document is not None:
                try:
                    reduced = module.reduce_status(document, ctx.now(), "server")
                except Exception:  # noqa: BLE001 - an unusable module is the same answer
                    document, reason = None, "module_unavailable"
            if document is None:
                # Not a refusal (the request was fine) and not a failed command: the status
                # cannot be shown.  The reason word is fixed, and no path or docker text is kept.
                answer.update(
                    ok=False,
                    result="unavailable",
                    detail=EAVESDROPPER_UNAVAILABLE[reason],
                    errorCode=reason,
                )
                return answer, 1
            answer.update(
                ok=True,
                result="planned",
                detail="read-only status of the server's eavesdropper",
                status=reduced,
            )
            return answer, 0
        if scope == "server" and action in ("resource-status", "resource-policy"):
            if not config["resourcePolicy"]:
                raise Refused("coding resource controls are not in the allowlist")
            # -I omits the script directory. Only this reviewed sibling directory is
            # added; neither the request nor the environment selects an import path.
            scripts = str(Path(__file__).resolve().parent)
            if scripts not in sys.path:
                sys.path.insert(0, scripts)
            from fleet_resource_control import handle as handle_resource

            if mode == "yes" and action == "resource-policy":
                # A write holds the same one-action lock as every other `--yes` write.
                with action_lock(ctx):
                    return handle_resource(ctx, action, target, mode)
            return handle_resource(ctx, action, target, mode)
        with contextlib.ExitStack() as held:
            # A logs run only reads the journal, so it takes no write lock.
            if mode == "yes" and not (scope == "server" and action == "logs"):
                held.enter_context(action_lock(ctx))
            commands, note, audit_target = plan_request(ctx, config, scope, action, target)
            answer["commands"] = commands
            if mode == "plan":
                answer.update(ok=True, result="planned", detail=note)
                code = 0
            elif scope == "server" and action == "logs":
                detail, text, log_lines = execute_logs(ctx, commands)
                answer.update(ok=True, result="done", detail=detail, output=text, lines=log_lines)
                code = 0
            elif scope == "server" and action == "update":
                receipt, code = execute_update(ctx, config, target, commands)
                answer.update(receipt)
            elif scope == "server" and action == "redeploy":
                detail = execute(ctx, scope, action, target, commands)
                answer.update(
                    ok=True,
                    result="submitted",
                    detail=detail,
                    executionState="submitted",
                    verified=False,
                )
                code = 0
            else:
                answer.update(
                    ok=True, result="done", detail=execute(ctx, scope, action, target, commands)
                )
                answer["completedCommands"] = len(commands)
                code = 0
    except Refused as exc:
        answer.update(ok=False, result="refused", detail=str(exc))
        if exc.error_code:
            answer["errorCode"] = exc.error_code
        code = 2
    except Failed as exc:
        answer.update(ok=False, result="failed", detail=str(exc))
        if exc.error_code:
            answer["errorCode"] = exc.error_code
        if exc.completed is not None:
            answer["completedCommands"] = exc.completed
        code = 1
    if mode == "yes" and not (scope == "server" and action == "update-status"):
        if scope == "server" and action == "redeploy" and audit_target == target:
            audit_target = "coolify resource"  # refused before the name was known: no uuid
        route = None
        if scope == "server" and action == "update":
            direct = [UPDATE_SYSTEMCTL, "start", "--no-block", FLEET_CLI_UPDATE_UNIT]
            if answer["commands"] == [[SUDO, "-n"] + direct]:
                route = "sudo-system-update"
            elif ctx.uid == 0 and answer["commands"] == [direct]:
                route = "root-system-update"
        row_id = secrets.token_hex(6)
        written_target = (audit_target or "")[:120]
        if audit(
            ctx,
            scope,
            action,
            written_target,
            answer["result"],
            lines=log_lines if answer["result"] == "done" else None,
            route=route,
            invocation_id=answer.get("invocationID"),
            verified=answer.get("verified"),
            baseline=answer.get("baselineStartMonotonic"),
            row_id=row_id,
        ):
            # Only a row that was written is named, so the caller never links to nothing.
            answer["auditID"] = row_id
            answer["auditTarget"] = written_target
    return answer, code


# MARK: - Command line


class _QuietParser(argparse.ArgumentParser):
    """Refuses instead of printing argparse's own usage text and exiting."""

    def error(self, message):
        raise Refused("usage: (mac|pm2|server) ACTION TARGET (--plan|--yes) [--json]")


def parse_request(argv):
    """(scope, action, target, mode, json) from a verb argv, or Refused.  Exactly one target."""
    parser = _QuietParser(prog=TOOL, add_help=False)
    parser.add_argument("scope", choices=("mac", "pm2", "server"))
    parser.add_argument("action")
    parser.add_argument("target")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true")
    group.add_argument("--yes", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    mode = "plan" if args.plan else "yes" if args.yes else None
    return args.scope, args.action, args.target, mode, args.json


def parse_ssh_command(text):
    """The verb argv inside SSH_ORIGINAL_COMMAND, or Refused.

    Server verbs only, one target, one of --plan or --yes, an optional --json, nothing else."""
    if not isinstance(text, str) or not SSH_SAFE_RE.fullmatch(text):
        raise Refused("the ssh command is missing or has characters that are not allowed")
    try:
        words = shlex.split(text)
    except ValueError:
        raise Refused("the ssh command cannot be split") from None
    flags = [w for w in words if w.startswith("-")]
    plain = [w for w in words if not w.startswith("-")]
    if len(plain) != 3 or plain[0] != "server":
        raise Refused("only a server verb with one target is accepted over ssh")
    if plain[1] not in ACTIONS + (
        "update",
        "update-status",
        "redeploy",
        "logs",
        "resource-status",
        "resource-policy",
        "seat-inventory",
        "eavesdropper-status",
    ):
        raise Refused("unknown server verb")
    if words[:3] != plain:
        raise Refused("the verb and target come first")
    if len(set(flags)) != len(flags) or any(f not in ("--plan", "--yes", "--json") for f in flags):
        raise Refused("only --plan, --yes and --json are accepted over ssh")
    if ("--plan" in flags) == ("--yes" in flags):
        raise Refused("pass exactly one of --plan or --yes")
    return words


def sanitize_for_terminal(text):
    """Journal text with escape and control sequences removed, for a terminal.

    Newlines and tabs stay.  The JSON answer keeps the text as the journal held it."""
    kept = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\x1b":
            index += 1
            if index < len(text) and text[index] == "[":
                index += 1
                while index < len(text) and not "@" <= text[index] <= "~":
                    index += 1
                index += 1
            elif index < len(text) and text[index] == "]":
                index += 1
                while index < len(text) and text[index] not in ("\x07", "\x1b"):
                    index += 1
                if index < len(text) and text[index] == "\x07":
                    index += 1
            elif index < len(text):
                index += 1
            continue
        if char in ("\n", "\t") or not (ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F):
            kept.append(char)
        index += 1
    return "".join(kept)


def emit(answer, as_json, out):
    if as_json:
        out.write(json.dumps(answer, sort_keys=True) + "\n")
        return
    out.write(f"{answer['result']}: {answer['verb']} {answer['target']}")
    out.write(f" ({answer['detail']})\n" if answer["detail"] else "\n")
    for argv in answer["commands"]:
        out.write("  " + " ".join(shlex.quote(a) for a in argv) + "\n")
    if answer.get("output"):
        out.write(sanitize_for_terminal(answer["output"]))


def main(argv=None, ctx=None, out=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ctx = ctx or Context()
    out = out or sys.stdout
    if "--from-ssh" in argv:
        # The forced command: nothing but --from-ssh may come from the command line, and
        # the answer is always JSON.
        if argv != ["--from-ssh"]:
            emit(_refusal("--from-ssh takes no other arguments"), True, out)
            return 2
        try:
            words = parse_ssh_command(ctx.environ.get("SSH_ORIGINAL_COMMAND"))
        except Refused as exc:
            emit(_refusal(str(exc)), True, out)
            return 2
        argv = words
        force_json = True
    elif argv == ["--check-token"]:
        # Local only: the ssh gate above accepts nothing but a server verb.
        answer, code = check_token(ctx)
        emit(answer, True, out)
        return code
    else:
        force_json = False
    try:
        scope, action, target, mode, as_json = parse_request(argv)
    except Refused as exc:
        emit(_refusal(str(exc)), force_json or "--json" in argv, out)
        return 2
    if force_json and scope != "server":
        emit(_refusal("only a server verb is accepted over ssh"), True, out)
        return 2
    answer, code = handle(ctx, scope, action, target, mode)
    emit(answer, as_json or force_json, out)
    return code


def _refusal(detail):
    return {
        "ok": False,
        "verb": "",
        "target": "",
        "commands": [],
        "result": "refused",
        "detail": detail,
    }


if __name__ == "__main__":
    sys.exit(main())
