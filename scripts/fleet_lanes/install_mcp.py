"""install_mcp: register the fleet's two stdio MCP servers in each harness's own config, in its own format.

    cd scripts
    python3 -m fleet_lanes.install_mcp plan   [--home H] [--with-agent-sync] [TARGET ...]   # read-only
    python3 -m fleet_lanes.install_mcp apply  [--home H] [--with-agent-sync] TARGET ...
    python3 -m fleet_lanes.install_mcp verify [--home H] [--with-agent-sync] [--probe] [TARGET ...]
    python3 -m fleet_lanes.install_mcp remove [--home H] [--with-agent-sync] TARGET ...

The default set is MINIMAL on purpose (each registered server is a process per session, and this Mac has been
through an MCP fan-out overload):  `fleet-recall` only, which is the stdio server that
`scripts/install-fleet-rag.sh` installs under ~/apps/fleet-rag.  `--with-agent-sync` also registers
`agent-sync mcp --default-seat <SEAT>` for the targets that have a FIXED seat (opencode OPENCODE, muse MC,
minimax MM, fx FX, clutch CLUTCH).  A tool with no default seat (Kimi, Copilot CLI) never gets it: the
registration has to name a seat, and these tools have none.  Never a pinned AGENT_SEAT in env (a launcher's seat must still win,
AGENT-SYNC § Identity Rules), and never a token anywhere.

TARGET is opencode, kimi, copilot, muse, minimax, fx or clutch.  `install-fleet-rag.sh` already registers
fleet-recall in Claude, Cursor, Antigravity, Codex and Grok and calls this tool for the rest.

What each target writes (every shape below was written by the tool's own `mcp add`, or copied from an entry the
tool already holds, on a throwaway home):

    opencode  ~/.config/opencode/opencode.json   mcp.servers.<name> = {type: local, command: [argv...]}
    kimi      ~/.kimi-code/mcp.json              mcpServers.<name> = {command, args}
    copilot   ~/.copilot/mcp-config.json         mcpServers.<name> = {tools: ["*"], type: local, command, args}
    muse      ~/.config/muse/settings.json       mcpServers.<name> = {type: stdio, command, args, mode: optional}
    minimax   ~/.minimax/mcp.json                mcpServers.<name> = {type: stdio, enabled, configured, builtin: false, command, args}
    fx        ~/.fx/mcp.json                     mcp.<name> = {type: local, command: [argv...], enabled}
    clutch    ~/.clutch/dsh/cordis.patch.yml     a marked `- insert:` block that mounts @deepseek-ai/dsh-mcp-client
              (agent-sync only: Clutch already carries fleet-recall as a native agent preset, and a second copy
              would double it)

Rules for a config this tool does not own:
  - the tool's home folder must already exist (~/.copilot, ~/.kimi-code, ...), or the target is
    skipped-not-installed and nothing is created; the config FILE is created when the folder exists
  - an entry with the same name and the same command is `present` and is never rewritten (a hand-registered one
    often has extra keys, such as timeouts); an entry with the same name and a different command is
    `skipped-foreign`; a file that is not a JSON object is `skipped-invalid-json`.  Nothing foreign is ever edited
  - one backup per file, `<file>.bak-fleet-mcp-<UTC stamp>` (the original bytes, always mode 0600), then an atomic
    replace;
    a second apply changes nothing and writes no backup
  - an env block that pins AGENT_SEAT on a server we manage is reported, never edited
  - `remove` deletes only an entry that is exactly what `apply` writes

Clutch's patch is checked before it is written:  `apply clutch` refuses unless `@deepseek-ai/dsh-mcp-client` resolves
under the real `~/.clutch/dsh/profiles/node_modules` (the engine repairs a dangling link only when it boots, so restart
clutch-web first), then runs the pinned `dsh --profile P --dump-config` for every profile on a scratch copy of the
profiles (`--dsh PATH` names another engine binary, `--no-dsh-check` skips both).  `--dump-config` is a merge-and-parse
check: it never resolves a package, so it does not prove the plugin loads.  A profile with `patchReload: live` (clutch-web's
`web`) watches the patch, so the write reaches the running engine at once; a `startup` profile reads it at its next start,
and `apply` prints each profile's mode.  `python3 -m fleet_lanes.install_tools verify clutch` on the final file comes next.

`verify --probe` also starts each registered command and sends it MCP `initialize` and `tools/list` (stdio, one
JSON object per line).  Nothing is called, so nothing reaches Zulip or the recall service.  The probe's environment
has no seat, launcher or Zulip credential variable.  An `agent-sync` entry is probed only from a session that IS its
seat (`AGENT_SEAT`, or a launcher's seat, equals the registration's); from any other session that probe is a SKIP,
because `--default-seat S` reads S's credential file and acts as S's bot.

Exit codes: 0 ok, 1 a write or a verify failed, 64 usage error.  Python 3.9 safe.
Tests: cd scripts && python3 -m unittest fleet_lanes.tests.test_install_mcp -v
"""
from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import os
import re
import select
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, TextIO, Tuple

from . import cordis_patch as CP
from . import dsh_check as DC
from . import marked_block as MB

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 64

BACKUP_TAG = ".bak-fleet-mcp-"
BACKUP_MODE = 0o600
TOOL = "fleet_lanes.install_mcp"
FLEET_RECALL = "fleet-recall"
AGENT_SYNC = "agent-sync"
SERVER_ORDER = (FLEET_RECALL, AGENT_SYNC)


# --------------------------------------------------------------------------- what is registered

def fleet_rag_home(home: str, env: Optional[Dict[str, str]] = None) -> str:
    """Where install-fleet-rag.sh puts fleet-recall-mcp.py: $FLEET_RAG_HOME, else <home>/apps/fleet-rag."""
    env = os.environ if env is None else env
    return env.get("FLEET_RAG_HOME") or os.path.join(home, "apps", "fleet-rag")


def server_argv(name: str, home: str, seat: Optional[str], env: Optional[Dict[str, str]] = None) -> List[str]:
    """The stdio command of one server.  Absolute paths: no client expands `~`."""
    if name == FLEET_RECALL:
        return ["python3", os.path.join(fleet_rag_home(home, env), "fleet-recall-mcp.py")]
    if name == AGENT_SYNC:
        if not seat:
            raise ValueError("agent-sync needs a seat")
        return [os.path.join(home, ".local", "bin", "agent-sync"), "mcp", "--default-seat", seat]
    raise ValueError("unknown server %r" % name)


# --------------------------------------------------------------------------- native entry shapes

Build = Callable[[str, List[str]], dict]
ArgvOf = Callable[[object], Optional[List[str]]]


def _split(argv: List[str]) -> Tuple[str, List[str]]:
    return argv[0], list(argv[1:])


def _command_args_of(entry: object) -> Optional[List[str]]:
    if isinstance(entry, dict) and isinstance(entry.get("command"), str):
        args = entry.get("args", [])
        if isinstance(args, list) and all(isinstance(a, str) for a in args):
            return [entry["command"]] + list(args)
    return None


def _command_list_of(entry: object) -> Optional[List[str]]:
    if isinstance(entry, dict):
        cmd = entry.get("command")
        if isinstance(cmd, list) and cmd and all(isinstance(a, str) for a in cmd):
            return list(cmd)
    return None


def _build_opencode(name: str, argv: List[str]) -> dict:
    return {"type": "local", "command": list(argv)}


def _build_kimi(name: str, argv: List[str]) -> dict:
    cmd, args = _split(argv)
    return {"command": cmd, "args": args}


def _build_copilot(name: str, argv: List[str]) -> dict:
    cmd, args = _split(argv)
    return {"tools": ["*"], "type": "local", "command": cmd, "args": args}


def _build_muse(name: str, argv: List[str]) -> dict:
    cmd, args = _split(argv)
    return {"type": "stdio", "command": cmd, "args": args, "mode": "optional"}


def _build_minimax(name: str, argv: List[str]) -> dict:
    cmd, args = _split(argv)
    return {"type": "stdio", "enabled": True, "configured": True, "builtin": False, "command": cmd, "args": args}


def _build_fx(name: str, argv: List[str]) -> dict:
    entry: dict = {"type": "local", "command": list(argv), "enabled": True}
    if name == FLEET_RECALL:        # the timeouts the existing hand-registered entry carries
        entry.update({"startup_timeout_ms": 30000, "operation_timeout_ms": 60000, "restart_limit": 1})
    return entry


@dataclass(frozen=True)
class JsonTarget:
    key: str
    label: str
    tool_dir: str                       # relative to home; must exist or the target is skipped-not-installed
    rel_path: str                       # the config file, relative to home
    servers_path: Tuple[str, ...]       # keys from the document root to the {name: entry} object
    build: Build
    argv_of: ArgvOf
    seat: Optional[str] = None          # fixed default seat, for agent-sync; None = the tool has no default seat
    note: str = ""


JSON_TARGETS: Tuple[JsonTarget, ...] = (
    JsonTarget("opencode", "OpenCode v2", ".config/opencode", ".config/opencode/opencode.json", ("mcp", "servers"),
               _build_opencode, _command_list_of,
               seat="OPENCODE",
               note="the OPENCODE seat since Sat, Oct 10, 2026 (owner, #440), so agent-sync gets --default-seat OPENCODE; "
                    "a launcher's seat still wins.  A session inside Conductor has no default (open question in "
                    "AGENT-SYNC) and shares this global file.  OpenCode also reads a project .opencode/opencode.json; "
                    "this is the global file"),
    JsonTarget("kimi", "Kimi Code", ".kimi-code", ".kimi-code/mcp.json", ("mcpServers",),
               _build_kimi, _command_args_of,
               note="engine-only CLI with no default seat: fleet-recall only"),
    JsonTarget("copilot", "GitHub Copilot CLI", ".copilot", ".copilot/mcp-config.json", ("mcpServers",),
               _build_copilot, _command_args_of,
               note="no default seat: fleet-recall only.  Copilot already holds about a dozen servers"),
    JsonTarget("muse", "Muse Code", ".config/muse", ".config/muse/settings.json", ("mcpServers",),
               _build_muse, _command_args_of, seat="MC",
               note="settings.json also holds the rest of Muse's settings; only mcpServers is touched"),
    JsonTarget("minimax", "MiniMax Code", ".minimax", ".minimax/mcp.json", ("mcpServers",),
               _build_minimax, _command_args_of, seat="MM"),
    JsonTarget("fx", "fx", ".fx", ".fx/mcp.json", ("mcp",),
               _build_fx, _command_list_of, seat="FX"),
)
JSON_BY_KEY = {t.key: t for t in JSON_TARGETS}

CLUTCH_KEY = "clutch"
CLUTCH_SEAT = "CLUTCH"
CLUTCH_DIR = ".clutch/dsh"
CLUTCH_PATCH = ".clutch/dsh/cordis.patch.yml"
CLUTCH_BLOCK = "mcp-agent-sync"
MCP_CLIENT_PLUGIN = "@deepseek-ai/dsh-mcp-client"
TARGET_KEYS = tuple(t.key for t in JSON_TARGETS) + (CLUTCH_KEY,)
ALIASES = {"muse-code": "muse", "mc": "muse", "mm": "minimax", "kimi-code": "kimi", "copilot-cli": "copilot",
           "dsh": "clutch"}


# --------------------------------------------------------------------------- one config, one server

@dataclass
class Outcome:
    target: str
    server: str
    status: str                 # added | unchanged | present | removed | absent | skipped-*
    detail: str = ""
    warnings: List[str] = field(default_factory=list)


def _detect_indent(text: str):
    for line in text.split("\n")[1:]:
        stripped = line.lstrip(" \t")
        if stripped and line != stripped and not stripped.startswith(("}", "]")):
            lead = line[:len(line) - len(stripped)]
            return "\t" if lead.startswith("\t") else len(lead)
    return 2


def _dump(doc: dict, indent) -> str:
    return json.dumps(doc, indent=indent, ensure_ascii=False) + "\n"


def _get_servers(doc: dict, path: Sequence[str], create: bool):
    """The {name: entry} dict at `path`.  (dict, None), or (None, reason) when the shape cannot hold servers."""
    node = doc
    for i, key in enumerate(path):
        nxt = node.get(key)
        if nxt is None:
            if not create:
                return {}, None
            nxt = node[key] = {}
        if not isinstance(nxt, dict):
            return None, "'%s' is not an object" % ".".join(path[:i + 1])
        node = nxt
    return node, None


def _v1_shape_problem(target: JsonTarget, doc: dict) -> Optional[str]:
    """OpenCode v1 wrote mcp.<name> = {...} with no `servers` level.  Adding a `servers` key next to those would
    mix the two shapes, so such a file is left to the owner."""
    if target.servers_path != ("mcp", "servers"):
        return None
    mcp = doc.get("mcp")
    if isinstance(mcp, dict):
        extra = [k for k, v in mcp.items() if k != "servers" and isinstance(v, dict)]
        if extra:
            return "mcp holds v1-style server entries (%s); not mixing in the v2 `servers` level" % ", ".join(extra[:3])
    return None


def _env_pin_warning(entry: object) -> Optional[str]:
    env = entry.get("env") if isinstance(entry, dict) else None
    if isinstance(env, dict) and "AGENT_SEAT" in env:
        return "the entry pins AGENT_SEAT=%s in env, which overrides a launcher's seat; remove the env key by hand" \
            % env.get("AGENT_SEAT")
    return None


def edit_json_target(target: JsonTarget, text: Optional[str], names_argv: Sequence[Tuple[str, List[str]]],
                     action: str) -> Tuple[List[Outcome], Optional[str]]:
    """Evaluate every (server name, argv) against one config's text.  Returns the outcomes and the NEW text, or
    None when nothing changes.  `action` is add or remove.  Pure: no file is read or written here."""
    out: List[Outcome] = []
    if text is None or not text.strip():
        doc: dict = {}
        indent = 2
    else:
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            return [Outcome(target.key, n, "skipped-invalid-json", "not valid JSON (%s); left alone" % exc)
                    for n, _ in names_argv], None
        if not isinstance(parsed, dict):
            return [Outcome(target.key, n, "skipped-invalid-json", "the top level is not a JSON object")
                    for n, _ in names_argv], None
        doc = parsed
        indent = _detect_indent(text)
    original = copy.deepcopy(doc)
    problem = _v1_shape_problem(target, doc)
    if problem:
        return [Outcome(target.key, n, "skipped-v1-shape", problem) for n, _ in names_argv], None
    servers, why = _get_servers(doc, target.servers_path, create=(action == "add"))
    if servers is None:
        return [Outcome(target.key, n, "skipped-bad-shape", why or "") for n, _ in names_argv], None
    for name, argv in names_argv:
        want = target.build(name, argv)
        cur = servers.get(name)
        warn = _env_pin_warning(cur)
        warnings = [warn] if warn else []
        if action == "add":
            if cur is None:
                servers[name] = want
                out.append(Outcome(target.key, name, "added", warnings=warnings))
            elif cur == want:
                out.append(Outcome(target.key, name, "unchanged", warnings=warnings))
            elif target.argv_of(cur) == argv:
                out.append(Outcome(target.key, name, "present",
                                   "same command, other keys (left as they are)", warnings))
            else:
                out.append(Outcome(target.key, name, "skipped-foreign",
                                   "an entry named %s runs a different command; left alone" % name, warnings))
        else:
            if cur is None:
                out.append(Outcome(target.key, name, "absent"))
            elif cur == want:
                del servers[name]
                out.append(Outcome(target.key, name, "removed"))
            else:
                out.append(Outcome(target.key, name, "skipped-not-ours",
                                   "the entry is not exactly what apply writes; left alone", warnings))
    if doc == original:
        return out, None
    return out, _dump(doc, indent)


# --------------------------------------------------------------------------- Clutch: a marked block in a cordis patch

def _yaml_str(value: str) -> str:
    return json.dumps(value)        # a JSON string is a valid YAML double-quoted scalar


def clutch_block_body(argv: List[str]) -> List[str]:
    lines = [
        "- insert:",
        "    - id: fleet-mcp-agent-sync",
        "      name: '@deepseek-ai/dsh-mcp-client'",
        "      config:",
        "        serverName: agent-sync",
        "        transport: stdio",
        "        command: %s" % _yaml_str(argv[0]),
        "        args:",
    ]
    lines += ["          - %s" % _yaml_str(a) for a in argv[1:]]
    lines.append("        failOnStartupError: false")
    return lines


def edit_clutch_patch(text: Optional[str], argv: List[str], action: str) -> Tuple[Outcome, Optional[str], bool]:
    """(outcome, new text, delete).  `delete` means the last block went and only comments were left, so the file
    goes (a comment-only patch crashes the engine; see cordis_patch)."""
    name = AGENT_SYNC
    try:
        if text is not None and text.strip() and not CP.is_list_patch(text):
            return Outcome(CLUTCH_KEY, name, "skipped-bad-shape",
                           "the cordis patch is not a top-level list; left alone"), None, False
        if action == "add":
            new, act = CP.upsert(text, CLUTCH_BLOCK, TOOL, clutch_block_body(argv))
            if act == "none":
                return Outcome(CLUTCH_KEY, name, "unchanged"), None, False
            return Outcome(CLUTCH_KEY, name, "added", "block replaced in place" if act == "replace" else ""), new, False
        if text is None:
            return Outcome(CLUTCH_KEY, name, "absent"), None, False
        new, act = CP.remove(text, CLUTCH_BLOCK)
        if act == "none":
            return Outcome(CLUTCH_KEY, name, "absent"), None, False
        if new is None:
            return Outcome(CLUTCH_KEY, name, "removed", "nothing else was in the patch, so the file is removed"), None, True
        return Outcome(CLUTCH_KEY, name, "removed"), new, False
    except MB.BlockError as exc:
        return Outcome(CLUTCH_KEY, name, "skipped-bad-markers", str(exc)), None, False


# --------------------------------------------------------------------------- planning and applying

@dataclass
class TargetPlan:
    key: str
    label: str
    path: str
    state: str                          # ok | skipped-not-installed | unreadable
    outcomes: List[Outcome] = field(default_factory=list)
    new_text: Optional[str] = None
    old_raw: Optional[bytes] = None
    mode: int = 0o600
    existed: bool = False
    delete: bool = False
    notes: List[str] = field(default_factory=list)

    @property
    def will_write(self) -> bool:
        return self.state == "ok" and (self.new_text is not None or self.delete)


def wanted_servers(key: str, with_agent_sync: bool) -> List[str]:
    if key == CLUTCH_KEY:
        return [AGENT_SYNC] if with_agent_sync else []
    t = JSON_BY_KEY[key]
    names = [FLEET_RECALL]
    if with_agent_sync and t.seat:
        names.append(AGENT_SYNC)
    return names


def _read_config(path: str) -> Tuple[Optional[str], Optional[bytes], int, bool, Optional[str]]:
    """(text, raw, mode, existed, problem)."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None, None, 0o600, False, None
    except OSError as exc:
        return None, None, 0o600, True, "cannot stat: %s" % (exc.strerror or exc)
    if not stat.S_ISREG(st.st_mode):
        return None, None, 0o600, True, "not a regular file"
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
        return raw.decode("utf-8"), raw, stat.S_IMODE(st.st_mode), True, None
    except UnicodeDecodeError:
        return None, None, 0o600, True, "not valid UTF-8"
    except OSError as exc:
        return None, None, 0o600, True, "unreadable: %s" % (exc.strerror or exc)


def plan_target(key: str, home: str, action: str, with_agent_sync: bool,
                env: Optional[Dict[str, str]] = None) -> TargetPlan:
    if key == CLUTCH_KEY:
        label, rel, tool_dir = "Clutch (DSH engine)", CLUTCH_PATCH, CLUTCH_DIR
    else:
        t = JSON_BY_KEY[key]
        label, rel, tool_dir = t.label, t.rel_path, t.tool_dir
    path = os.path.join(home, *rel.split("/"))
    plan = TargetPlan(key, label, path, "ok")
    names = wanted_servers(key, with_agent_sync)
    if not os.path.isdir(os.path.join(home, *tool_dir.split("/"))):
        plan.state = "skipped-not-installed"
        plan.notes.append("~/%s does not exist, so %s does not look installed; nothing is created" % (tool_dir, label))
        return plan
    if not names:
        plan.state = "nothing-to-do"
        plan.notes.append("Clutch carries fleet-recall as a native agent preset; add --with-agent-sync for agent-sync")
        return plan
    text, raw, mode, existed, problem = _read_config(path)
    plan.old_raw, plan.mode, plan.existed = raw, mode, existed
    if problem:
        plan.state = "unreadable"
        plan.notes.append("%s: %s" % (path, problem))
        return plan
    if key == CLUTCH_KEY:
        argv = server_argv(AGENT_SYNC, home, CLUTCH_SEAT, env)
        outcome, new, delete = edit_clutch_patch(text, argv, action)
        plan.outcomes, plan.new_text, plan.delete = [outcome], new, delete
        return plan
    t = JSON_BY_KEY[key]
    pairs = [(n, server_argv(n, home, t.seat, env)) for n in names]
    plan.outcomes, plan.new_text = edit_json_target(t, text, pairs, action)
    if plan.new_text is not None and plan.existed:
        hint = credential_hint(raw)
        if hint:
            plan.notes.append("WARNING: %s %s" % (path, hint))
    if not os.path.isfile(os.path.join(fleet_rag_home(home, env), "fleet-recall-mcp.py")) and FLEET_RECALL in names:
        plan.notes.append("%s does not exist yet (scripts/install-fleet-rag.sh creates it)" %
                          os.path.join(fleet_rag_home(home, env), "fleet-recall-mcp.py"))
    if with_agent_sync and AGENT_SYNC in names and not os.path.exists(os.path.join(home, ".local", "bin", "agent-sync")):
        plan.notes.append("~/.local/bin/agent-sync does not exist; the registration would start nothing")
    return plan


# A literal credential in a config this tool is about to back up:  an Authorization or Bearer value, or a JSON string
# under a key named token, api key, password, secret or authorization.  A whole-value ${VAR} (or $VAR) is not one.
_CREDENTIAL = re.compile(
    rb"(?i)(?:bearer[ \t]+(?![$]\{?[A-Za-z_])[A-Za-z0-9._~+/=-]{12,}"
    rb"|\"[A-Za-z_]*(?:token|api_?key|password|secret|authorization)\"[ \t]*:[ \t]*\"(?![^\"\n]*[$]\{?[A-Za-z_])[^\"\n]{12,}\")")


def credential_hint(raw: Optional[bytes]) -> Optional[str]:
    """A warning (never the value) when `raw` looks like it holds a literal credential, else None."""
    if raw and _CREDENTIAL.search(raw):
        return ("this file holds what looks like a literal credential (an Authorization or Bearer value, or a token, key, "
                "password or secret string).  The backup is written owner-only (0600), but rotate the credential and "
                "use the client's whole-value ${VAR} form so no plaintext copy is left behind")
    return None


def _utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")


def write_plan(plan: TargetPlan, stamp: Optional[str] = None) -> Optional[str]:
    """Write plan.new_text, or delete the file when plan.delete.  Returns the backup path (None for a new file).
    Backs up first, replaces atomically, and refuses when the file changed since it was planned."""
    assert plan.new_text is not None or plan.delete
    path = plan.path
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    if plan.existed:
        with open(path, "rb") as fh:
            if fh.read() != plan.old_raw:
                raise OSError("%s changed while the tool was working; nothing written" % path)
    backup = None
    if plan.existed:
        base = "%s%s%s" % (path, BACKUP_TAG, stamp or _utc_stamp())
        for n in range(1, 100):
            cand = base if n == 1 else "%s-%d" % (base, n)
            try:
                fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as fh:
                fh.write(plan.old_raw or b"")
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(cand, BACKUP_MODE)     # owner-only whatever the original's mode: a backup of a 0644 file with a token is a second readable copy
            backup = cand
            break
        else:
            raise OSError("99 backups with stamp %s already exist" % (stamp or "now"))
    if plan.delete:
        try:
            os.unlink(path)
        except OSError:
            if backup:
                os.unlink(backup)
            raise
        return backup
    assert plan.new_text is not None
    fd, tmp = tempfile.mkstemp(dir=directory, prefix="." + os.path.basename(path) + ".", suffix=".fleet-mcp.tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(plan.new_text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, plan.mode if plan.existed else 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if backup:
            try:
                os.unlink(backup)
            except OSError:
                pass
        raise
    return backup


# --------------------------------------------------------------------------- verifying

@dataclass
class Check:
    target: str
    server: str
    status: str         # PASS | FAIL | WARN | SKIP
    detail: str = ""

    def line(self) -> str:
        return ("%-5s %-9s %-12s %s" % (self.status, self.target, self.server, self.detail)).rstrip()


# What a probe must not inherit.  A caller's seat variables would make `agent-sync mcp --default-seat S` serve the
# CALLER's seat (AGENT_SEAT beats --default-seat), and a launcher variable can make it refuse to start; a Zulip
# credential variable would send it to another bot's account.  The server then resolves its seat the way the client
# that registered it would.
PROBE_STRIPPED_ENV = ("AGENT_SEAT", "AGENT_TAG", "AGENT_SESSION", "AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT",
                      "ZULIP_RC", "ZULIP_EMAIL", "ZULIP_API_KEY")


def probe_environment(environ: Dict[str, str]) -> Dict[str, str]:
    """The caller's environment without the seat and credential variables (PROBE_STRIPPED_ENV)."""
    return {k: v for k, v in environ.items() if k not in PROBE_STRIPPED_ENV}


def caller_seat(environ: Dict[str, str]) -> Optional[str]:
    """The seat this session runs as, upper case, or None: a launcher's AGENT_LAUNCH_SEAT (with AGENT_LAUNCHER) first,
    else AGENT_SEAT, else AGENT_TAG."""
    if environ.get("AGENT_LAUNCHER", "").strip() and environ.get("AGENT_LAUNCH_SEAT", "").strip():
        return environ["AGENT_LAUNCH_SEAT"].strip().upper()
    for name in ("AGENT_SEAT", "AGENT_TAG"):
        if environ.get(name, "").strip():
            return environ[name].strip().upper()
    return None


def probe_stdio(argv: Sequence[str], timeout: float = 15.0, env: Optional[Dict[str, str]] = None) -> Tuple[bool, str]:
    """Start `argv`, send MCP initialize and tools/list (one JSON object per line), and read the answers.  Nothing
    is called.  Returns (ok, detail)."""
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": "fleet-install-mcp-probe", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    try:
        proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=env, start_new_session=True)
    except OSError as exc:
        return False, "cannot start: %s" % (exc.strerror or exc)
    deadline = time.monotonic() + timeout
    got: Dict[int, dict] = {}
    buf = b""
    why = ""
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(("\n".join(json.dumps(m) for m in msgs) + "\n").encode("utf-8"))
        proc.stdin.flush()
        while not {1, 2} <= set(got):
            if time.monotonic() >= deadline:
                why = "timeout after %gs" % timeout
                break
            ready, _, _ = select.select([proc.stdout], [], [], max(0.0, min(0.5, deadline - time.monotonic())))
            if not ready:
                if proc.poll() is not None:
                    why = "exit %s" % proc.returncode
                    break
                continue
            chunk = os.read(proc.stdout.fileno(), 65536)
            if not chunk:
                proc.wait(timeout=2)
                why = "exit %s" % proc.returncode
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    obj = json.loads(line.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("id"), int):
                    got[obj["id"]] = obj
    except (OSError, subprocess.TimeoutExpired) as exc:
        why = "pipe error: %s" % (getattr(exc, "strerror", None) or exc)
    finally:
        try:
            proc.kill()
        except OSError:
            pass
        for pipe in (proc.stdin, proc.stdout):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass
        proc.wait()
    if 1 not in got or "result" not in got[1]:
        return False, "no initialize result (%s)" % (why or "no answer")
    tools = (got.get(2, {}).get("result") or {}).get("tools")
    if not isinstance(tools, list):
        return False, "initialize answered, tools/list did not"
    return True, "initialize ok, %d tool%s listed" % (len(tools), "" if len(tools) == 1 else "s")


def verify_target(key: str, home: str, with_agent_sync: bool, *, probe: bool = False,
                  env: Optional[Dict[str, str]] = None) -> List[Check]:
    plan = plan_target(key, home, "add", with_agent_sync, env)
    if plan.state == "skipped-not-installed":
        return [Check(key, "-", "SKIP", plan.notes[0])]
    if plan.state in ("nothing-to-do",):
        return [Check(key, "-", "SKIP", plan.notes[0])]
    if plan.state != "ok":
        return [Check(key, "-", "FAIL", "; ".join(plan.notes))]
    checks: List[Check] = []
    for o in plan.outcomes:
        if o.status in ("unchanged", "present"):
            detail = "registered" + (" (%s)" % o.detail if o.detail else "")
            checks.append(Check(key, o.server, "PASS", detail))
            for w in o.warnings:
                checks.append(Check(key, o.server, "WARN", w))
            if probe and key != CLUTCH_KEY:
                t = JSON_BY_KEY[key]
                argv = server_argv(o.server, home, t.seat, env)
                probe_env = probe_environment(os.environ)
                if o.server == AGENT_SYNC:
                    # `agent-sync mcp --default-seat S` reads S's credential file and calls the realm as S's bot.  A
                    # probe is allowed only from a session that IS that seat, never as another seat's bot.
                    mine = caller_seat(os.environ)
                    if mine != (t.seat or "").upper():
                        checks.append(Check(key, o.server, "SKIP",
                                            "probe: not run.  It would use the %s seat's credential, and this session is %s;"
                                            "  run `verify --probe --with-agent-sync` from a %s session"
                                            % (t.seat, ("the %s seat" % mine) if mine else "no seat", t.seat)))
                        continue
                    probe_env["AGENT_SEAT"] = t.seat or ""
                ok, detail = probe_stdio(argv, env=probe_env)
                checks.append(Check(key, o.server, "PASS" if ok else "FAIL", "probe: " + detail))
        elif o.status == "added":
            checks.append(Check(key, o.server, "FAIL", "not registered yet (apply would add it)"))
        else:
            checks.append(Check(key, o.server, "FAIL", "%s: %s" % (o.status, o.detail)))
    return checks


# --------------------------------------------------------------------------- command line

class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        raise _UsageError(message)


def _resolve_targets(names: Sequence[str], explicit_required: bool) -> List[str]:
    if not names:
        if explicit_required:
            raise _UsageError("name at least one target: " + ", ".join(TARGET_KEYS))
        return list(TARGET_KEYS)
    out: List[str] = []
    for raw in names:
        key = ALIASES.get(raw.strip().lower(), raw.strip().lower())
        if key not in TARGET_KEYS:
            raise _UsageError("unknown target %r; choose from %s" % (raw, ", ".join(TARGET_KEYS)))
        if key not in out:
            out.append(key)
    return out


def _resolve_home(raw: Optional[str]) -> str:
    if raw is None:
        home = os.path.expanduser("~")
        if not os.environ.get("HOME", "x").strip():
            raise _UsageError("HOME is set but empty; pass --home DIR")
    else:
        if not raw.strip():
            raise _UsageError("--home must not be empty")
        home = os.path.expanduser(raw)
    if not os.path.isdir(home):
        raise _UsageError("home %s is not a directory" % home)
    if os.path.realpath(home) == os.sep:
        raise _UsageError("home resolves to /; refusing")
    return os.path.abspath(home)


def build_parser() -> argparse.ArgumentParser:
    p = _Parser(prog="python3 -m fleet_lanes.install_mcp", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", parser_class=_Parser)
    for name, helptext in (("plan", "show what apply would do (read-only; default: all targets)"),
                           ("apply", "register the servers in the named targets"),
                           ("verify", "check that the servers are registered"),
                           ("remove", "remove the entries apply wrote")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("targets", nargs="*", metavar="TARGET", help=", ".join(TARGET_KEYS))
        sp.add_argument("--home", default=None, help="home directory to work in (default: $HOME)")
        sp.add_argument("--with-agent-sync", action="store_true",
                        help="also register agent-sync for targets with a fixed seat")
        if name in ("apply", "remove"):
            sp.add_argument("--dry-run", action="store_true", help="same as plan")
        if name == "apply":
            sp.add_argument("--dsh", default=None, metavar="PATH",
                            help="clutch: the pinned engine binary for the --dump-config proof "
                                 "(default ~/apps/clutch-runtime/node_modules/.bin/dsh)")
            sp.add_argument("--no-dsh-check", action="store_true",
                            help="clutch: write the patch without the engine proof")
        if name == "verify":
            sp.add_argument("--probe", action="store_true", help="also start each command and list its tools")
    return p


def main(argv: Optional[Sequence[str]] = None, out: Optional[TextIO] = None, err: Optional[TextIO] = None,
         env: Optional[Dict[str, str]] = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or (args[0].startswith("-") and args[0] not in ("-h", "--help")):
        args.insert(0, "plan")
    try:
        ns = build_parser().parse_args(args)
        if ns.command is None:
            raise _UsageError("a command is required: plan, apply, verify or remove")
        home = _resolve_home(ns.home)
        targets = _resolve_targets(ns.targets, explicit_required=ns.command in ("apply", "remove"))
    except _UsageError as exc:
        print("install_mcp: %s" % exc, file=err)
        return EXIT_USAGE
    wa = ns.with_agent_sync
    if ns.command == "verify":
        checks: List[Check] = []
        for key in targets:
            checks += verify_target(key, home, wa, probe=getattr(ns, "probe", False), env=env)
        for c in checks:
            print(c.line(), file=out)
        return EXIT_FAILED if any(c.status == "FAIL" for c in checks) else EXIT_OK
    action = "remove" if ns.command == "remove" else "add"
    dry = ns.command == "plan" or getattr(ns, "dry_run", False)
    failed = False
    changed = 0
    for key in targets:
        plan = plan_target(key, home, action, wa, env)
        print("== %s  (%s)" % (key, plan.label), file=out)
        print("   file: %s" % plan.path, file=out)
        if plan.state != "ok":
            print("   %s" % plan.state.upper(), file=out)
            for n in plan.notes:
                print("   %s" % n, file=out)
            print(file=out)
            continue
        for o in plan.outcomes:
            verb = o.status
            if dry and o.status in ("added", "removed"):
                verb = "would-" + o.status.replace("added", "add").replace("removed", "remove")
            print("   %-12s %s%s" % (o.server, verb, ("  (%s)" % o.detail) if o.detail else ""), file=out)
            for w in o.warnings:
                print("   WARNING: %s" % w, file=out)
        for n in plan.notes:
            print("   note: %s" % n, file=out)
        if plan.will_write and not dry and key == CLUTCH_KEY and ns.command == "apply" and not plan.delete \
                and not getattr(ns, "no_dsh_check", False):
            binary = getattr(ns, "dsh", None) or DC.find_dsh(home)
            if binary is None:
                failed = True
                print("   REFUSED: the pinned dsh was not found at ~/apps/clutch-runtime/node_modules/.bin/dsh (pass --dsh PATH, "
                      "or --no-dsh-check to write the patch unproven)", file=out)
                print(file=out)
                continue
            ok, detail = DC.check_patch(binary, plan.new_text or "", os.path.join(home, ".clutch", "dsh", "profiles"),
                                        (MCP_CLIENT_PLUGIN,))
            if not ok:
                failed = True
                print("   REFUSED: the Clutch check does not accept the new patch: %s" % detail, file=out)
                print(file=out)
                continue
            print("   engine check: %s" % detail, file=out)
        if plan.will_write and not dry:
            try:
                backup = write_plan(plan)
                changed += 1
                verb = "removed" if plan.delete else "wrote"
                print("   %s %s%s" % (verb, plan.path, ("; backup " + backup) if backup else "; new file, no backup"), file=out)
            except OSError as exc:
                failed = True
                print("   FAILED: %s" % exc, file=out)
        elif plan.will_write:
            print("   (plan only: nothing written)", file=out)
        print(file=out)
    if ns.command != "plan":
        print("Summary: %d file%s %s." % (changed, "" if changed == 1 else "s", "would change" if dry else "changed"),
              file=out)
    return EXIT_FAILED if failed else EXIT_OK


if __name__ == "__main__":      # pragma: no cover
    sys.exit(main())
