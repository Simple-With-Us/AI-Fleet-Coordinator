#!/usr/bin/env python3
"""fleet-ops-control:  start, stop, restart and update the fleet's own processes, and nothing else.

  python3 scripts/fleet-ops-control.py mac start|stop|restart LABEL       (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py server start|stop|restart UNIT     (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py server update NAME                 (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py server redeploy RESOURCE_UUID      (--plan | --yes) [--json]
  python3 scripts/fleet-ops-control.py --from-ssh                         (the forced command)
  python3 scripts/fleet-ops-control.py --check-token                      (install check, not over ssh)

`scripts/fleet-ops-control.json` is the one allowlist.  A label, a unit or an update that is
not in it cannot be touched, and the allowlist is read and checked again on every run and on
every path, the ssh one included.

What it promises:

  * `--plan` prints the commands it would run and changes nothing.  `--yes` runs them.  With
    neither it refuses.
  * Every command is an argv list run without a shell.  Labels and unit names must match a
    strict pattern before they reach an argv, and their output is thrown away: only an exit
    status is reported.
  * `redeploy` takes a uuid only when the latest `server-status.json` lists it as a Coolify
    resource, so no uuid or host is kept in this repo.  The Coolify token is read through
    `fleet-secrets` into memory, sent in one request header and never printed, logged or
    passed in an argv.  A forced command has only the environment sshd gives it, so
    fleet-secrets logs in with the Infisical identity in `~/.config/fleet-tools/ops-control.env`
    (0600) when that file exists.  `--check-token` reads the token the same way and asks
    Coolify for its version, which proves the whole path and changes nothing.
  * `--from-ssh` is the forced command of the `fleet-ops-ctl` key.  It reads
    `SSH_ORIGINAL_COMMAND`, refuses any shell metacharacter, splits it with `shlex`, and
    accepts only a `server` verb, one target, one of `--plan` or `--yes` and an optional
    `--json`.  Anything more is refused.
  * Every `--yes` run appends one JSON line to `~/.local/state/fleet-tools/ops-audit.jsonl`
    (0600): time, host role, verb, target and result.  No command output is recorded, and a
    redeploy is recorded by its resource name, never its uuid.

Exit status: 0 planned or done, 1 a command failed, 2 refused.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import time
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
FLEET_SECRETS_CANDIDATES = ("~/.local/bin/fleet-secrets", "/usr/local/bin/fleet-secrets")
COOLIFY_TOKEN_NAME = "COOLIFY_API_TOKEN"
COOLIFY_TOKEN_PATH = "/coolify"
# Coolify answers on its own host; COOLIFY_BASE_URL overrides it.
COOLIFY_DEFAULT_BASE = "http://127.0.0.1:8000"
# The Infisical identity fleet-secrets may be handed from the credential file, and nothing
# else from it.
CREDENTIAL_NAMES = (
    "INFISICAL_TOKEN",
    "INFISICAL_AUTOMATION_CLIENT_ID",
    "INFISICAL_AUTOMATION_CLIENT_SECRET",
)
CREDENTIALS_MAX_BYTES = 8 * 1024
COMMAND_TIMEOUT = 90
SECRET_TIMEOUT = 30
HTTP_TIMEOUT = 30
# The only variables a launchctl or systemctl child sees.
CHILD_ENV_KEEP = ("PATH", "HOME", "LANG", "LC_ALL", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


class Refused(Exception):
    """The request is not allowed.  The text is a fixed phrase that names no secret."""


class Failed(Exception):
    """An allowed command ran and did not succeed."""


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
    return {
        "agents": agents,
        "units": units,
        "updates": updates,
        "redeploy": coolify.get("redeploy", False) is True,
    }


def _actions(row, bad):
    actions = row.get("actions")
    if not isinstance(actions, list) or not actions or any(a not in ACTIONS for a in actions):
        bad("actions")
    return tuple(actions)


# MARK: - Running things


def run_command(argv, timeout=COMMAND_TIMEOUT, env=None, want_stdout=False):
    """(exit status, stdout bytes).  An argv list, never a shell; stderr is dropped.

    A timeout is status 124 and a missing binary 127, like a shell would say."""
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise Refused("internal: not an argv list")
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if want_stdout else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
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

    def child_env(self):
        env = {k: v for k, v in self.environ.items() if k in CHILD_ENV_KEEP}
        env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        if self.platform.startswith("linux") and "XDG_RUNTIME_DIR" not in env:
            # `systemctl --user` from an ssh forced command needs the user manager's socket.
            runtime = f"/run/user/{self.uid}"
            if os.path.isdir(runtime):
                env["XDG_RUNTIME_DIR"] = runtime
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


def server_update_commands(ctx, config, name):
    _server_only(ctx)
    if not UPDATE_RE.fullmatch(name or ""):
        raise Refused("the update name is not valid")
    entry = config["updates"].get(name)
    if entry is None:
        raise Refused("the update is not in the allowlist")
    # --no-block: an update can run for minutes, and the Mac is waiting over ssh.  The unit's
    # own result shows in the services list on the next status report.
    return [
        _systemctl_argv(entry["scope"], "start", "--no-block", entry["unit"])
    ], "runs in the background"


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
            if row.get("type") not in REDEPLOYABLE_TYPES:
                raise Refused("only a Coolify application can be redeployed")
            if not uuid_from_label(row):
                raise Refused("the report has no Coolify uuid for that resource")
            return row
    raise Refused("the uuid is not in the latest server status report")


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
    return moment.replace(tzinfo=datetime.UTC).timestamp()


def coolify_base(ctx):
    base = (ctx.environ.get("COOLIFY_BASE_URL") or COOLIFY_DEFAULT_BASE).strip().rstrip("/")
    parts = urllib.parse.urlsplit(base)
    loopback = parts.hostname in ("127.0.0.1", "localhost", "::1")
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or (parts.scheme == "http" and not loopback)
    ):
        raise Refused("COOLIFY_BASE_URL must be an https origin, or http on loopback")
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

    When the credential file exists it is the one source of the Infisical identity: the
    process's own CREDENTIAL_NAMES are dropped and the file's are used, so a run over ssh
    and a run from a root shell log in the same way.  The file must be a regular file of
    ours with no group or other bits, holding only `NAME=value` lines (no quotes) for
    INFISICAL_TOKEN, or for both INFISICAL_AUTOMATION_CLIENT_ID and _SECRET.  Its values
    reach this one child only, never an argv, a launchctl or systemctl child, the answer or
    the audit.  With no file, the process environment is used as it is."""
    env = dict(ctx.environ)
    try:
        fd = os.open(str(ctx.credentials_path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
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
    pair = [n for n in CREDENTIAL_NAMES[1:] if n in values]
    if len(pair) == 1 or not ("INFISICAL_TOKEN" in values or pair):
        raise Failed("the Infisical credential file is malformed")
    for name in CREDENTIAL_NAMES:
        env.pop(name, None)
    env.update(values)
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
    if scope != "server":
        raise Refused("unknown verb")
    if action in ACTIONS:
        commands, note = server_unit_commands(ctx, config, action, target)
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
    for argv in commands:
        status, _ = ctx.runner(argv, timeout=COMMAND_TIMEOUT, env=ctx.child_env())
        if status != 0:
            verb = next((a for a in argv[1:] if not a.startswith("-")), "")
            raise Failed(f"{os.path.basename(argv[0])} {verb} exited {status}")
    return "done" if commands else "nothing to do"


def audit(ctx, scope, action, target, result):
    """One JSON line, 0600, appended.  Never command output, never a uuid or a hostname."""
    row = {
        "time": datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": "mac" if ctx.platform.startswith("darwin") else "server",
        "verb": f"{scope} {action}",
        "target": target,
        "result": result,
    }
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
    try:
        if mode not in ("plan", "yes"):
            raise Refused("pass --plan to see the commands or --yes to run them")
        config = load_config(ctx.config_path)
        commands, note, audit_target = plan_request(ctx, config, scope, action, target)
        answer["commands"] = commands
        if mode == "plan":
            answer.update(ok=True, result="planned", detail=note)
            code = 0
        else:
            answer.update(
                ok=True, result="done", detail=execute(ctx, scope, action, target, commands)
            )
            code = 0
    except Refused as exc:
        answer.update(ok=False, result="refused", detail=str(exc))
        code = 2
    except Failed as exc:
        answer.update(ok=False, result="failed", detail=str(exc))
        code = 1
    if mode == "yes":
        if scope == "server" and action == "redeploy" and audit_target == target:
            audit_target = "coolify resource"  # refused before the name was known: no uuid
        audit(ctx, scope, action, (audit_target or "")[:120], answer["result"])
    return answer, code


# MARK: - Command line


class _QuietParser(argparse.ArgumentParser):
    """Refuses instead of printing argparse's own usage text and exiting."""

    def error(self, message):
        raise Refused("usage: (mac|server) ACTION TARGET (--plan|--yes) [--json]")


def parse_request(argv):
    """(scope, action, target, mode, json) from a verb argv, or Refused.  Exactly one target."""
    parser = _QuietParser(prog=TOOL, add_help=False)
    parser.add_argument("scope", choices=("mac", "server"))
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
    if plain[1] not in ACTIONS + ("update", "redeploy"):
        raise Refused("unknown server verb")
    if words[:3] != plain:
        raise Refused("the verb and target come first")
    if len(set(flags)) != len(flags) or any(f not in ("--plan", "--yes", "--json") for f in flags):
        raise Refused("only --plan, --yes and --json are accepted over ssh")
    if ("--plan" in flags) == ("--yes" in flags):
        raise Refused("pass exactly one of --plan or --yes")
    return words


def emit(answer, as_json, out):
    if as_json:
        out.write(json.dumps(answer, sort_keys=True) + "\n")
        return
    out.write(f"{answer['result']}: {answer['verb']} {answer['target']}")
    out.write(f" ({answer['detail']})\n" if answer["detail"] else "\n")
    for argv in answer["commands"]:
        out.write("  " + " ".join(shlex.quote(a) for a in argv) + "\n")


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
