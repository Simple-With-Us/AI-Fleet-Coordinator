#!/usr/bin/env python3
"""Install bounded, code-only fleet CLI releases.  Never reads credentials."""

from __future__ import annotations

import argparse
import base64
import binascii
import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from pathlib import Path, PurePosixPath

MAX_BUNDLE_BYTES = 8 * 1024 * 1024
# The reviewed allowlisted runtime contains 141 files. Keep the exact minimum
# ceiling in sender and receiver: a new file still needs an explicit policy review.
# The 132nd file is storage_guard.py (from main);  the 133rd is platform_letter.py (AFC-564, the shared session-letter
# library);  the 134th is platform_letter_codex.py (the Codex adapter);  the 135th and 136th are
# platform_letter_opencode.py and opencode_letter_plugin/index.js (the OpenCode adapter);  the 137th is operator_post.py
# (the Operator's voice for the daemon's own posts, owner ruling Oct 30 2026);  the 138th and 139th are platform_letter_muse.py and muse_session_rename.py (the Muse adapter);  the 140th is platform_letter_grok_leader.py (the Grok adapter);  the 141st is platform_letter_antigravity.py (the Antigravity adapter), added for that review.
MAX_FILES = 141
MAX_FILE_BYTES = 2 * 1024 * 1024
MARKER = "# fleet-cli managed wrapper v1"
# The Agent-Sync Linear catalogs `board commands`, `board run` and `board mcp` read from beside board
# (exported from scripts/agent-sync-mcp by scripts/export-linear-catalog.mjs).
LINEAR_CATALOG = ("named-commands.json", "official-mcp-tools.json", "public-inputs.json")
# `recall` runs from these fleet_rag modules only (its static import closure).  `recall eval` and
# `recall ingest` import eval/ingest lazily;  they are Mac and Hetzner-box jobs and are not shipped.
RECALL_MODULES = (
    "storage_guard",
    "__init__",
    "contribute_guard",
    "core",
    "doctor",
    "health",
    "public_fallback",
    "recall_api",
    "scrub",
    "search_log",
    "telemetry",
)
# `simple-notes` reads the OPERATOR_API_URL knob through these fleet_knobs modules (stdlib only;  the package's
# tests, its inventory CLI and its README stay out).  Without them a server host cannot find the Operator's notes queue.
KNOB_MODULES = ("__init__", "knobs", "lkg", "registry", "source", "values")
# `lane` runs from these fleet_lanes modules plus its bash shim (fleet-apps.json is already shipped).
LANE_MODULES = ("__init__", "doctor", "guard", "lane", "layout")
AFC331_FILES = (
    "scripts/agent_sync/mcp/notes-tools.json",
    "scripts/agent_sync/mcp/recall-tools.json",
    "scripts/linear-board/catalog/core-tools.json",
    "docs/protocols/simple-tools-guide.md",
)
EXACT = frozenset(
    {
        "scripts/simple-chat",
        "scripts/simple-notes",
        "scripts/simple-board",
        "scripts/linear-board/board",
        "scripts/linear-board/board-ids.txt",
        "fleet-apps.json",
        "docs/protocols/agent-sync-partition.toml",
        "scripts/fleet-cli-release.py",
        "scripts/agent_sync/mcp/tools.json",
        "scripts/agent_sync/mcp/fixtures.jsonl",
        # AFC-331:  the stdio server reads these beside tools.json (the listener tools, the removed-name map
        # and the enabled Grok Bot personas).
        "scripts/agent_sync/mcp/eavesdropper-tools.json",
        "scripts/agent_sync/mcp/renamed-tools.json",
        "scripts/agent_sync/mcp/gb-identities.json",
        # AFC-331:  the notes_* and recall_* tool contracts, the core Linear tools the stdio server serves
        # through board, and the guide tool's text.
        *AFC331_FILES,
        "scripts/agent_sync/wake/wake-schema.json",
        *("scripts/linear-board/catalog/" + name for name in LINEAR_CATALOG),
        "scripts/recall",
        *("scripts/fleet_rag/" + name + ".py" for name in RECALL_MODULES),
        "scripts/fleet_lanes/bin/lane",
        *("scripts/fleet_lanes/" + name + ".py" for name in LANE_MODULES),
        *("scripts/fleet_knobs/" + name + ".py" for name in KNOB_MODULES),
        "scripts/fleet-mode",
        "docs/FLEET-MODE.md",
        # The fleetlink_* tool contract (generated from the hosted src/fleetlink.js):  `simple-chat mcp` reads it at startup.
        "scripts/agent_sync/mcp/fleetlink-tools.json",
        # AFC-412 PR2:  the operator registry outbox exporter, shipped as a data file beside the Python modules.
        "scripts/agent_sync/operator_sheets_export.mjs",
        # AFC-564:  the OpenCode letter plugin, a data file beside the Python modules (OpenCode loads it from the path
        # `platform-letter install --platform opencode` writes).  Its node test lives under tests/ and is not shipped.
        "scripts/agent_sync/opencode_letter_plugin/index.js",
    }
)
# What every release ever installed contains.  verify_retained, rollback and status judge a retained
# release by this set, so a release that predates the AFC-331 tools stays valid and can be rolled back to.
# The renamed entry points are NOT here:  a retained pre-rename release carries scripts/agent-sync instead,
# so demanding the new names would make status and rollback refuse exactly the rollback targets they exist for.
BASE_REQUIRED = frozenset(
    {
        "scripts/linear-board/board",
        "fleet-apps.json",
        *("scripts/linear-board/catalog/" + name for name in LINEAR_CATALOG),
        "scripts/agent_sync/__init__.py",
        "scripts/agent_sync/cli.py",
        "scripts/agent_sync/mcp/stdio.py",
        "scripts/agent_sync/mcp/tools.json",
        "scripts/fleet-cli-release.py",
    }
)
# The recall, lane and fleet-mode files (AFC-331).  A new bundle must carry them;  an older retained one need not.
TOOL_REQUIRED = frozenset(
    {
        # The hard-cutover entry points (rename of scripts/agent-sync):  a new bundle must carry them, but a
        # retained older release carries scripts/agent-sync instead and stays a valid rollback target.
        "scripts/simple-chat",
        "scripts/simple-notes",
        "scripts/simple-board",
        "scripts/recall",
        *("scripts/fleet_rag/" + name + ".py" for name in RECALL_MODULES),
        "scripts/fleet_lanes/bin/lane",
        *("scripts/fleet_lanes/" + name + ".py" for name in LANE_MODULES),
        "scripts/fleet-mode",
        "docs/FLEET-MODE.md",
        *AFC331_FILES,
        # The eavesdropper_* MCP tools (AFC-330):  `simple-chat mcp` reads it at startup.
        "scripts/agent_sync/mcp/eavesdropper-tools.json",
        # The knob reader (AFC-331):  a new bundle must carry it;  a retained older release need not (rollback stays valid).
        *("scripts/fleet_knobs/" + name + ".py" for name in KNOB_MODULES),
        # The fleetlink_* MCP tools:  `simple-chat mcp` reads it at startup.  Not in BASE_REQUIRED, so a retained
        # release built before it stays a valid rollback target.
        "scripts/agent_sync/mcp/fleetlink-tools.json",
        # The fleet secret scanner (standard library only):  every remote transport scans its arguments with it
        # before anything is sent, and sends nothing if it is missing.  Not in BASE_REQUIRED, so a retained release
        # built before it stays a valid rollback target.
        "scripts/agent_sync/secretscan.py",
        # Imported by notes_client: new bundles need it, older rollback targets do not.
        "scripts/agent_sync/notes_service_reader.py",
        "scripts/agent_sync/fleet_bearer.py",
        # New stdio bundles must carry the hard-imported own-bearer gate. Older rollback targets stay valid.
        "scripts/agent_sync/mcp/bearer_gate.py",
    }
)
REQUIRED = BASE_REQUIRED | TOOL_REQUIRED


class Refused(Exception):
    """Only fixed, non-sensitive error codes leave this program."""


def allowed_path(path: str) -> bool:
    if not isinstance(path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]+", path):
        return False
    parts = PurePosixPath(path).parts
    if not parts or path.startswith("/") or any(p in (".", "..") for p in parts):
        return False
    if str(PurePosixPath(path)) != path:
        return False
    if path in EXACT:
        return True
    return bool(
        re.fullmatch(r"scripts/agent_sync/(?:mcp/)?[a-z][a-z0-9_]*\.py", path)
        or path in ("scripts/agent_sync/__init__.py", "scripts/agent_sync/mcp/__init__.py")
    )


def manifest_digest(files: dict) -> str:
    manifest = {p: {"mode": e["mode"], "sha256": e["sha256"]} for p, e in files.items()}
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def decode_bundle(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    if len(raw) > MAX_BUNDLE_BYTES:
        raise Refused("bundle_too_large")
    try:
        bundle = json.loads(raw)
        if (
            set(bundle) != {"schema", "source_commit", "bundle_sha256", "files"}
            or bundle["schema"] != 1
        ):
            raise Refused("bad_schema")
        if not re.fullmatch(r"[0-9a-f]{40}", bundle["source_commit"]):
            raise Refused("bad_revision")
        files = bundle["files"]
        if not isinstance(files, dict) or not REQUIRED.issubset(files) or len(files) > MAX_FILES:
            raise Refused("bad_file_set")
        decoded = {}
        for path, entry in files.items():
            if not allowed_path(path) or set(entry) != {"mode", "sha256", "data"}:
                raise Refused("refused_path_or_entry")
            if type(entry["mode"]) is not int or entry["mode"] not in (0o644, 0o755):
                raise Refused("bad_mode")
            data = base64.b64decode(entry["data"], validate=True)
            if len(data) > MAX_FILE_BYTES or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise Refused("bad_file_digest")
            decoded[path] = data
        if manifest_digest(files) != bundle["bundle_sha256"]:
            raise Refused("bad_bundle_digest")
        return bundle, decoded
    except (TypeError, ValueError, KeyError, binascii.Error):
        raise Refused("invalid_bundle") from None


def safe_dir(path: Path) -> None:
    """Create missing directories; never follow a pre-existing directory symlink."""
    missing = []
    p = path
    while not p.exists() and not p.is_symlink():
        missing.append(p)
        p = p.parent
    for parent in (p, *p.parents):
        if parent.is_symlink() or not parent.is_dir():
            raise Refused("unsafe_directory")
    for item in reversed(missing):
        item.mkdir(mode=0o700)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    safe_dir(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise Refused("unsafe_destination")
    fd, tmp = tempfile.mkstemp(prefix=".fleet-cli-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def json_write(path: Path, obj: dict) -> None:
    atomic_write(path, (json.dumps(obj, sort_keys=True, indent=2) + "\n").encode())


def release_at(base: Path, name: str) -> Path | None:
    link = base / name
    if not link.is_symlink():
        if link.exists():
            raise Refused("unsafe_pointer")
        return None
    target = os.readlink(link)
    if not re.fullmatch(r"releases/[0-9a-f]{64}", target):
        raise Refused("unsafe_pointer")
    release = base / target
    if release.is_symlink() or not release.is_dir():
        raise Refused("missing_release")
    return release


def point(base: Path, name: str, release: Path) -> None:
    release_at(base, name)
    temporary = base / (".pointer-" + uuid.uuid4().hex)
    temporary.symlink_to("releases/" + release.name)
    try:
        os.replace(temporary, base / name)
    finally:
        temporary.unlink(missing_ok=True)


def smoke(release: Path, home: Path) -> None:
    for path in release.rglob("*"):
        if path.is_file() and (
            path.suffix == ".py"
            or path.name in ("board", "simple-chat", "simple-notes", "simple-board", "recall")
        ):
            try:
                compile(path.read_bytes(), str(path), "exec")
            except (SyntaxError, ValueError):
                raise Refused("python_compile_failed") from None
    # Only an empty temporary HOME and a minimal environment reach the smoke processes.
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "AGENT_SEAT": "CODEX"}
    cases = [
        ("simple_chat_version", "scripts/simple-chat", ["--version"], 0),
        ("simple_chat_mcp_help", "scripts/simple-chat", ["mcp", "--help"], 0),
        ("simple_notes_version", "scripts/simple-notes", ["--version"], 0),
        ("simple_board_help", "scripts/simple-board", ["--help"], 0),
        # A usage error exits 2 before any credential is read, with or without the host's seat helper.  The
        # earlier case ran `whoami` and expected exit 4 (missing credentials), which depended on the host:  a host that
        # has the helper and a bearer for the smoke seat authenticates and exits 0, and every release was refused.
        ("simple_board_usage_error", "scripts/simple-board", ["show"], 2),
        ("recall_help", "scripts/recall", ["--help"], 0),
        ("lane_help", "scripts/fleet_lanes/bin/lane", ["--help"], 0),
        ("fleet_mode_block", "scripts/fleet-mode", [], 0),
    ]
    for case, script, args, expected in cases:
        # A retained release that predates the AFC-331 tools (a rollback target) has no such script.
        if script in TOOL_REQUIRED and not (release / script).exists():
            continue
        # lane and fleet-mode are bash;  the other cases are Python run in isolated mode.
        argv = (
            ["/bin/bash", str(release / script), *args]
            if script in BASH_SCRIPTS
            else [sys.executable, "-I", "-B", str(release / script), *args]
        )
        try:
            result = subprocess.run(argv, env=env, cwd=home, capture_output=True, timeout=12)
        except subprocess.TimeoutExpired:
            raise Refused("smoke_timeout_" + case) from None
        except OSError as exc:
            raise Refused("smoke_start_failed_" + case + "_errno_" + str(exc.errno or 0)) from None
        if result.returncode != expected:
            raise Refused("smoke_exit_mismatch_" + case)


BASH_SCRIPTS = frozenset({"scripts/fleet_lanes/bin/lane", "scripts/fleet-mode"})
# The volume a host keeps its integration trees and its apps folder on when the
# home directory holds no alias for them.  The server keeps both on a volume and
# links no ~/Code or ~/apps into the home, so a tool that resolves only the home
# finds no tree and no repo.  The path is not in this repository:  the host names
# it in the environment, and a host that names none has no volume.  The managed
# wrappers carry the roots, so every shell that calls them resolves the same
# directories, with or without an interactive login (owner 2026-10-25).
ENV_SERVER_VOLUME = "FLEET_SERVER_VOLUME"
ENV_CODE_ROOT = "FLEET_CODE_ROOT"
ENV_APPS_ROOT = "FLEET_APPS_ROOT"
ENV_LANES_ROOT = "FLEET_LANES_ROOT"
ROOTED_SCRIPTS = frozenset({"scripts/fleet_lanes/bin/lane"})
# Tools whose wrapper runs the host's seat-scoped remote defaults before the tool starts.  The helper is
# host configuration (never part of a release and never holding a credential value);  the wrapper only names it.
# Lifecycle contract:  whether the helper exists is decided when the wrapper is generated.
#   - A host without the helper keeps the plain wrapper.  Provisioning the helper later (its owner, parent
#     custody and exact hash qualified first) takes effect only when the canonical --install regenerates the
#     managed wrappers;  a routine update writes missing wrappers only and never rewrites an existing one.
#   - A wrapper generated with the helper fails closed if the helper is removed later:  runpy raises before the
#     tool is exec'd, so the command exits nonzero.  There is deliberately no runtime fallback to a plain run,
#     because that would silently drop the seat's own route.
HELPER_COMMANDS = {
    "scripts/simple-chat": "simple-chat",
    "scripts/simple-board": "simple-board",
    "scripts/recall": "recall",
}
HELPER_PATH = ".config/fleet-cli/seat-remote-defaults.py"


def expand(candidate: str, home: Path) -> Path:
    """A path the host named, with `~` expanded against the given home.

    The home is the one the release runs under, and a test runs inside a throwaway
    home, so a `~` never resolves against the real one.  A `~user` spelling names
    another account's home, and it is left exactly as it was named.
    """
    text = candidate.strip()
    if text == "~":
        text = str(home)
    elif text.startswith("~/"):
        text = str(home) + text[1:]
    return Path(text)


def host_root(home: Path, name: str) -> str:
    """The host's folder for `name` ("Code" or "apps").

    A root the caller configured wins, because that is the root the rest of the
    fleet is using.  Then the copy under the home wins when it is there; then the
    volume the host names holds these folders, and it holds the real trees when
    the home holds no alias for them.

    The authoritative resolution for the fleet is `server_working_dirs.py`
    (server_volume(), code_root(), apps_root(), lanes_root()) and the roots the
    lane tools read in `fleet_lanes/layout.py`.  This is the release's own copy,
    because the release payload ships no fleet_lanes module, and the two must
    agree.
    """
    configured = (os.environ.get(ENV_CODE_ROOT if name == "Code" else ENV_APPS_ROOT) or "").strip()
    if configured:
        return str(expand(configured, home))
    local = home / name
    if local.is_dir():
        return str(local)
    raw = (os.environ.get(ENV_SERVER_VOLUME) or "").strip()
    if raw:
        volume = expand(raw, home) / name
        if volume.is_dir():
            return str(volume)
    return str(local)


def host_lanes_root(home: Path, apps: str) -> str:
    """The lanes root this host names, or the one beside the apps folder.

    A host that names its lanes root carries that one:  the lanes do not always sit
    beside the apps folder, and a wrapper that derived one would point the lane
    tools at a directory the host does not use.
    """
    named = (os.environ.get(ENV_LANES_ROOT) or "").strip()
    if named:
        return str(expand(named, home))
    candidate = os.path.join(apps, "lanes")
    return candidate if os.path.isdir(candidate) else ""


def root_exports(home: Path) -> str:
    """The root exports a wrapper sets when the caller has not set them itself.

    Every line is a setdefault and never an assignment, so the environment wins
    over the wrapper.
    """
    code = host_root(home, "Code")
    apps = host_root(home, "apps")
    lanes = host_lanes_root(home, apps)
    return (
        f"os.environ.setdefault('FLEET_CODE_ROOT', {code!r})\n"
        f"os.environ.setdefault('FLEET_APPS_ROOT', {apps!r})\n"
        + (f"os.environ.setdefault('FLEET_LANES_ROOT', {lanes!r})\n" if lanes else "")
    )


def wrapper(
    home: Path, script: str, extra: list[str] | None = None, codex_only: bool = False
) -> bytes:
    guard = ""
    if codex_only:
        guard = """
if os.environ.get("AGENT_LAUNCHER") and not os.environ.get("AGENT_LAUNCH_SEAT"):
    sys.stderr.write("fleet-cli: launcher has no assigned seat\\n")
    sys.exit(3)
for key in ("AGENT_LAUNCH_SEAT", "AGENT_SEAT"):
    if os.environ.get(key) and os.environ[key].strip().upper() != "CODEX":
        sys.stderr.write("fleet-cli: this MCP registration is CODEX-only\\n")
        sys.exit(3)
"""
    # Python tools run isolated;  the two bash tools run under bash with the caller's environment.
    interpreter = ["/bin/bash"] if script in BASH_SCRIPTS else ["/usr/bin/python3", "-I", "-B"]
    roots = root_exports(home) if script in ROOTED_SCRIPTS else ""
    helper = ""
    # Only a host that has installed the helper gets the lines:  a wrapper that runs a missing file would break
    # its tool, and a host without the helper keeps the plain wrapper it has today.
    helper_file = home / HELPER_PATH
    if (
        script in HELPER_COMMANDS
        and not codex_only
        and helper_file.is_file()
        and not helper_file.is_symlink()
    ):
        helper = (
            "import runpy\n"
            f"runpy.run_path({str(home / HELPER_PATH)!r}, init_globals={{'fleet_command': {HELPER_COMMANDS[script]!r}}})\n"
        )
    return (
        f"#!/usr/bin/python3 -I\n{MARKER}\nimport os, sys\nfrom pathlib import Path\n"
        + helper
        + guard
        + f"release = Path({str(home / '.local/share/fleet-cli/current')!r}).resolve(strict=True)\n"
        + roots
        + f"argv = {repr(interpreter)[:-1]}, str(release / {script!r})] + {extra or []!r}\n"
        + ("" if codex_only else "argv += sys.argv[1:]\n")
        + "os.execv(argv[0], argv)\n"
    ).encode()


def wrapper_files(home: Path) -> dict[Path, tuple[bytes, int]]:
    """Every managed command wrapper a release provides, by path."""
    bin_dir = home / ".local/bin"
    return {
        bin_dir / "simple-board": (wrapper(home, "scripts/simple-board"), 0o755),
        bin_dir / "simple-chat": (wrapper(home, "scripts/simple-chat"), 0o755),
        bin_dir / "simple-notes": (wrapper(home, "scripts/simple-notes"), 0o755),
        bin_dir / "simple-chat-codex-mcp": (
            wrapper(
                home,
                "scripts/simple-chat",
                ["mcp", "--default-seat", "CODEX", "--codex-thread-meta"],
                True,
            ),
            0o755,
        ),
        bin_dir / "fleet-cli-release": (wrapper(home, "scripts/fleet-cli-release.py"), 0o755),
        bin_dir / "recall": (wrapper(home, "scripts/recall"), 0o755),
        bin_dir / "lane": (wrapper(home, "scripts/fleet_lanes/bin/lane"), 0o755),
        bin_dir / "fleet-mode": (wrapper(home, "scripts/fleet-mode"), 0o755),
        # AFC-564: the session-letter hook and its registry side for the non-Claude platforms.  The git-update path
        # runs the whole checkout, so the script is there once a release holds it.  The paused Mac-push bundle path
        # would also need it in EXACT (and the doctor's SHIPPED_EXACT).
        bin_dir / "platform-letter": (wrapper(home, "scripts/platform-letter"), 0o755),
    }


LEGACY_CODEX_MCP = "agent-sync-codex-mcp"
LEGACY_CODEX_KEY = "agent-sync"  # the retired registration key; migrated, never written
CODEX_KEY = (
    "simple-chat"  # local stdio MCP mode of the simple-chat CLI (the hosted server is ST-MCP)
)


def codex_mcp_expected(bin_dir: Path) -> dict:
    return {
        "command": str(bin_dir / "simple-chat-codex-mcp"),
        "env_vars": [
            "AGENT_LAUNCHER",
            "AGENT_LAUNCH_SEAT",
            "AGENT_SEAT",
            "AGENT_TAG",
            "AGENT_SESSION",
        ],
    }


def migrate_codex_mcp(home: Path, files: dict) -> None:
    """Rewrite a host's pre-rename Codex registration to `[mcp_servers.simple-chat]`, in `files` if it applies.

    Hosts installed before the rename carry `[mcp_servers.agent-sync]`, whose command is either the
    legacy wrapper `<bin>/agent-sync-codex-mcp` (it execs `scripts/agent-sync`, a path this release does
    not ship) or the renamed wrapper under the old key.  The table header and the one command line are
    rewritten, and only when the host's table is exactly the registration this tool wrote (same
    `env_vars`, command one of those two wrappers).  The old key is not kept.  Anything else is still a
    conflict and still refused:  this never rewrites a hand-edited registration.
    """
    bin_dir = home / ".local/bin"
    expected = codex_mcp_expected(bin_dir)
    config = home / ".codex/config.toml"
    if config.is_symlink() or (config.exists() and not config.is_file()):
        raise Refused("unsafe_codex_config")
    original = config.read_bytes() if config.exists() else b""
    if len(original) > 1024 * 1024:
        raise Refused("codex_config_too_large")
    try:
        before = tomllib.loads(original.decode())
    except (ValueError, UnicodeError):
        raise Refused("invalid_codex_config") from None
    servers = before.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise Refused("invalid_codex_config")
    current = servers.get(CODEX_KEY)
    if current is not None and current != expected:
        raise Refused("existing_simple_chat_mcp_conflict")
    present = servers.get(LEGACY_CODEX_KEY)
    if present is None:
        return
    if not isinstance(present, dict):
        raise Refused("existing_agent_sync_mcp_conflict")
    legacy_command = str(bin_dir / LEGACY_CODEX_MCP)
    if (
        set(present) != {"command", "env_vars"}
        or present.get("command") not in (legacy_command, expected["command"])
        or present.get("env_vars") != expected["env_vars"]
    ):
        raise Refused("existing_agent_sync_mcp_conflict")
    old_header = ("[mcp_servers." + LEGACY_CODEX_KEY + "]").encode()
    new_header = ("[mcp_servers." + CODEX_KEY + "]").encode()
    old_line = ("command = " + json.dumps(present["command"])).encode()
    new_line = ("command = " + json.dumps(expected["command"])).encode()
    # Exactly one of each:  an ambiguous file is not one this tool wrote, so do not guess.
    if original.count(old_header) != 1 or original.count(old_line) != 1:
        raise Refused("existing_agent_sync_mcp_conflict")
    if current is not None:
        # Both keys present: not a state this tool wrote, so do not guess which to keep.
        raise Refused("existing_agent_sync_mcp_conflict")
    after_raw = original.replace(old_header, new_header).replace(old_line, new_line)
    desired = copy.deepcopy(before)
    del desired["mcp_servers"][LEGACY_CODEX_KEY]
    desired["mcp_servers"][CODEX_KEY] = expected
    if tomllib.loads(after_raw.decode()) != desired:
        raise Refused("codex_config_semantics_changed")
    mode = stat.S_IMODE(config.stat().st_mode) if config.exists() else 0o600
    files[config] = (after_raw, mode)


def new_wrapper_plan(home: Path) -> dict[Path, tuple[bytes, int]]:
    """On a routine update (no --install), the wrappers a newer release added that this host lacks.
    Only absent paths are written:  an existing file, managed or not, and a symlink are left alone.
    Without this, a command added after the first install (recall, lane, fleet-mode) would ship in
    every release and never reach PATH.

    The Codex config is not written here, but a registration left naming the removed pre-rename
    wrapper is migrated:  that wrapper execs a script this release no longer ships."""
    files = {
        path: entry
        for path, entry in wrapper_files(home).items()
        if not path.exists() and not path.is_symlink()
    }
    migrate_codex_mcp(home, files)
    return files


def install_plan(home: Path) -> dict[Path, tuple[bytes, int]]:
    bin_dir = home / ".local/bin"
    files = wrapper_files(home)
    for path in files:
        if path.is_symlink() or (
            path.exists() and (not path.is_file() or MARKER.encode() not in path.read_bytes())
        ):
            raise Refused("existing_unmanaged_wrapper")
    expected = codex_mcp_expected(bin_dir)
    migrate_codex_mcp(home, files)
    config = home / ".codex/config.toml"
    original = config.read_bytes() if config.exists() else b""
    try:
        before = tomllib.loads(original.decode())
    except (ValueError, UnicodeError):
        raise Refused("invalid_codex_config") from None
    servers = before.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise Refused("invalid_codex_config")
    present = servers.get(CODEX_KEY)
    if present is None and config not in files:
        addition = (
            "\n# Local CODEX-only fleet MCP (simple-chat mcp); no credentials or pinned seat.\n"
            "[mcp_servers.simple-chat]\ncommand = " + json.dumps(expected["command"]) + "\n"
            "env_vars = " + json.dumps(expected["env_vars"]) + "\n"
        ).encode()
        after_raw = original.rstrip(b"\n") + b"\n" + addition
        after = tomllib.loads(after_raw.decode())
        desired = copy.deepcopy(before)
        desired.setdefault("mcp_servers", {})[CODEX_KEY] = expected
        if after != desired:
            raise Refused("codex_config_semantics_changed")
        mode = stat.S_IMODE(config.stat().st_mode) if config.exists() else 0o600
        files[config] = (after_raw, mode)
    return files


def verify_existing(release: Path, files: dict) -> None:
    paths = list(release.rglob("*"))
    if any(p.is_symlink() for p in paths):
        raise Refused("existing_release_changed")
    if {p.relative_to(release).as_posix() for p in paths if p.is_file()} != set(files) | {
        "release.json"
    }:
        raise Refused("existing_release_changed")
    for name, entry in files.items():
        path = release / name
        if (
            not path.is_file()
            or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]
            or stat.S_IMODE(path.stat().st_mode) != entry["mode"]
        ):
            raise Refused("existing_release_changed")


def verify_retained(release: Path) -> dict:
    metadata = release / "release.json"
    if (
        metadata.is_symlink()
        or not metadata.is_file()
        or metadata.stat().st_size > MAX_BUNDLE_BYTES
    ):
        raise Refused("invalid_release_metadata")
    try:
        record = json.loads(metadata.read_bytes())
        files = record["files"]
        if (
            not isinstance(files, dict)
            or not BASE_REQUIRED.issubset(files)
            or len(files) > MAX_FILES
            or not re.fullmatch(r"[0-9a-f]{40}", record["source_commit"])
            or record["bundle_sha256"] != release.name
            or manifest_digest(files) != release.name
        ):
            raise Refused("invalid_release_metadata")
        for name, entry in files.items():
            if (
                not allowed_path(name)
                or set(entry) != {"mode", "sha256"}
                or type(entry["mode"]) is not int
                or entry["mode"] not in (0o644, 0o755)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            ):
                raise Refused("invalid_release_metadata")
    except (KeyError, TypeError, ValueError):
        raise Refused("invalid_release_metadata") from None
    verify_existing(release, files)
    return record


def receive(raw: bytes, home: Path, install: bool = False) -> dict:
    bundle, decoded = decode_bundle(raw)
    base = home / ".local/share/fleet-cli"
    safe_dir(base)
    safe_dir(base / "releases")
    lock_path = base / "update.lock"
    if lock_path.is_symlink():
        raise Refused("unsafe_lock")
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refused("update_locked") from None
        if (base / "paused").exists():
            return {
                "status": "paused",
                "source_commit": bundle["source_commit"],
                "bundle_sha256": bundle["bundle_sha256"],
            }
        old = release_at(base, "current")
        if old is None and not install:
            raise Refused("initial_install_required")
        plan = install_plan(home) if install else new_wrapper_plan(home)
        new = base / "releases" / bundle["bundle_sha256"]
        if new.is_symlink():
            raise Refused("unsafe_release")
        if new.exists():
            verify_retained(new)
            verify_existing(new, bundle["files"])
        else:
            if len(list((base / "releases").iterdir())) >= 32:
                raise Refused("release_retention_limit")
            stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=base))
            try:
                for name, data in decoded.items():
                    atomic_write(stage / name, data, bundle["files"][name]["mode"])
                with tempfile.TemporaryDirectory(prefix=".smoke-", dir=base) as empty:
                    smoke(stage, Path(empty))
                json_write(
                    stage / "release.json",
                    {
                        "source_commit": bundle["source_commit"],
                        "bundle_sha256": bundle["bundle_sha256"],
                        "installed_at": int(time.time()),
                        "files": {
                            name: {"mode": entry["mode"], "sha256": entry["sha256"]}
                            for name, entry in bundle["files"].items()
                        },
                    },
                )
                os.rename(stage, new)
            finally:
                if stage.exists():
                    shutil.rmtree(stage)
        snapshots = {}
        backup = None
        try:
            for path, (data, mode) in plan.items():
                existed = path.exists()
                prior = path.read_bytes() if existed else None
                prior_mode = stat.S_IMODE(path.stat().st_mode) if existed else mode
                if prior == data:
                    continue
                snapshots[path] = (prior, prior_mode)
                if path == home / ".codex/config.toml" and prior is not None:
                    backup = (
                        home
                        / ".codex/backups"
                        / (
                            "fleet-cli-"
                            + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
                            + "-"
                            + uuid.uuid4().hex[:8]
                        )
                        / "config.toml"
                    )
                    atomic_write(backup, prior, prior_mode)
                atomic_write(path, data, mode)
            if old is not None and old != new:
                point(base, "previous", old)
            if old != new:
                point(base, "current", new)
        except BaseException:
            for path, (data, mode) in reversed(list(snapshots.items())):
                if data is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write(path, data, mode)
            raise
        result = {
            "status": "unchanged" if old == new else "activated",
            "source_commit": bundle["source_commit"],
            "bundle_sha256": bundle["bundle_sha256"],
        }
        if old is not None:
            result["previous"] = old.name
        if backup is not None:
            result["config_backup"] = str(backup)
        json_write(base / "status.json", {**result, "checked_at": int(time.time())})
        return result


def control(home: Path, command: str) -> dict:
    base = home / ".local/share/fleet-cli"
    if not base.is_dir():
        raise Refused("not_installed")
    safe_dir(base)
    if not (base / "releases").is_dir():
        raise Refused("not_installed")
    safe_dir(base / "releases")
    if command == "status":
        current = release_at(base, "current")
        previous = release_at(base, "previous")
        metadata = verify_retained(current) if current else {}
        return {
            "status": "paused" if (base / "paused").exists() else "ready",
            "source_commit": metadata.get("source_commit"),
            "bundle_sha256": current.name if current else None,
            "previous": previous.name if previous else None,
        }
    if (base / "update.lock").is_symlink():
        raise Refused("unsafe_lock")
    with (base / "update.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if command == "pause":
            atomic_write(base / "paused", b"paused by operator\n")
        elif command == "resume":
            (base / "paused").unlink(missing_ok=True)
        elif command == "rollback":
            previous, current = release_at(base, "previous"), release_at(base, "current")
            if not previous or not current:
                raise Refused("no_previous_release")
            verify_retained(previous)
            with tempfile.TemporaryDirectory(prefix=".smoke-", dir=base) as empty:
                smoke(previous, Path(empty))
            atomic_write(base / "paused", b"paused after rollback\n")
            point(base, "current", previous)
            point(base, "previous", current)
        return control(home, "status")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["receive", "status", "pause", "resume", "rollback"])
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--install", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "receive":
            result = receive(sys.stdin.buffer.read(MAX_BUNDLE_BYTES + 1), args.home, args.install)
        else:
            result = control(args.home, args.command)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Refused as exc:
        print("fleet-cli: " + str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, BlockingIOError):
        print("fleet-cli: local_operation_failed", file=sys.stderr)
        return 2
    except Exception:
        print("fleet-cli: internal_error", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
