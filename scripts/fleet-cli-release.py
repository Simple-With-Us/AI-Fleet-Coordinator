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
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid

MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_FILES = 128
MAX_FILE_BYTES = 2 * 1024 * 1024
MARKER = "# fleet-cli managed wrapper v1"
EXACT = frozenset({
    "scripts/agent-sync", "scripts/linear-board/board", "scripts/linear-board/board-ids.txt",
    "fleet-apps.json", "docs/protocols/agent-sync-partition.toml", "scripts/fleet-cli-release.py",
    "scripts/agent_sync/mcp/tools.json", "scripts/agent_sync/mcp/fixtures.jsonl",
    "scripts/agent_sync/wake/wake-schema.json",
})
REQUIRED = frozenset({
    "scripts/agent-sync", "scripts/linear-board/board", "fleet-apps.json",
    "scripts/agent_sync/__init__.py", "scripts/agent_sync/cli.py",
    "scripts/agent_sync/mcp/stdio.py", "scripts/agent_sync/mcp/tools.json",
    "scripts/fleet-cli-release.py",
})


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
    return bool(re.fullmatch(r"scripts/agent_sync/(?:mcp/)?[a-z][a-z0-9_]*\.py", path)
                or path in ("scripts/agent_sync/__init__.py", "scripts/agent_sync/mcp/__init__.py"))


def manifest_digest(files: dict) -> str:
    manifest = {p: {"mode": e["mode"], "sha256": e["sha256"]} for p, e in files.items()}
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def decode_bundle(raw: bytes) -> tuple[dict, dict[str, bytes]]:
    if len(raw) > MAX_BUNDLE_BYTES:
        raise Refused("bundle_too_large")
    try:
        bundle = json.loads(raw)
        if set(bundle) != {"schema", "source_commit", "bundle_sha256", "files"} or bundle["schema"] != 1:
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
        if path.is_file() and (path.suffix == ".py" or path.name in ("board", "agent-sync")):
            try:
                compile(path.read_bytes(), str(path), "exec")
            except (SyntaxError, ValueError):
                raise Refused("python_compile_failed") from None
    # Only an empty temporary HOME and a minimal environment reach the smoke processes.
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "AGENT_SEAT": "CODEX"}
    cases = [("agent_sync_version", "scripts/agent-sync", ["--version"], 0),
             ("agent_sync_mcp_help", "scripts/agent-sync", ["mcp", "--help"], 0),
             ("board_help", "scripts/linear-board/board", ["--help"], 0),
             ("board_missing_credentials", "scripts/linear-board/board", ["whoami"], 4)]
    for case, script, args, expected in cases:
        try:
            result = subprocess.run([sys.executable, "-I", "-B", str(release / script), *args],
                                    env=env, cwd=home, capture_output=True, timeout=12)
        except subprocess.TimeoutExpired:
            raise Refused("smoke_timeout_" + case) from None
        except OSError as exc:
            raise Refused("smoke_start_failed_" + case + "_errno_" + str(exc.errno or 0)) from None
        if result.returncode != expected:
            raise Refused("smoke_exit_mismatch_" + case)


def wrapper(home: Path, script: str, extra: list[str] | None = None, codex_only: bool = False) -> bytes:
    guard = ""
    if codex_only:
        guard = '''
if os.environ.get("AGENT_LAUNCHER") and not os.environ.get("AGENT_LAUNCH_SEAT"):
    sys.stderr.write("fleet-cli: launcher has no assigned seat\\n")
    sys.exit(3)
for key in ("AGENT_LAUNCH_SEAT", "AGENT_SEAT"):
    if os.environ.get(key) and os.environ[key].strip().upper() != "CODEX":
        sys.stderr.write("fleet-cli: this MCP registration is CODEX-only\\n")
        sys.exit(3)
'''
    return (f"#!/usr/bin/python3 -I\n{MARKER}\nimport os, sys\nfrom pathlib import Path\n"
            + guard
            + f"release = Path({str(home / '.local/share/fleet-cli/current')!r}).resolve(strict=True)\n"
            + f"argv = ['/usr/bin/python3', '-I', '-B', str(release / {script!r})] + {extra or []!r}\n"
            + ("" if codex_only else "argv += sys.argv[1:]\n")
            + "os.execv(argv[0], argv)\n").encode()


def install_plan(home: Path) -> dict[Path, tuple[bytes, int]]:
    bin_dir = home / ".local/bin"
    files = {
        bin_dir / "board": (wrapper(home, "scripts/linear-board/board"), 0o755),
        bin_dir / "agent-sync": (wrapper(home, "scripts/agent-sync"), 0o755),
        bin_dir / "agent-sync-codex-mcp": (wrapper(home, "scripts/agent-sync", ["mcp", "--default-seat", "CODEX"], True), 0o755),
        bin_dir / "fleet-cli-release": (wrapper(home, "scripts/fleet-cli-release.py"), 0o755),
    }
    for path in files:
        if path.is_symlink() or (path.exists() and (not path.is_file() or MARKER.encode() not in path.read_bytes())):
            raise Refused("existing_unmanaged_wrapper")
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
    expected = {"command": str(bin_dir / "agent-sync-codex-mcp"),
                "env_vars": ["AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT", "AGENT_SEAT", "AGENT_TAG", "AGENT_SESSION"]}
    present = before.get("mcp_servers", {}).get("agent-sync")
    if present is not None and present != expected:
        raise Refused("existing_agent_sync_mcp_conflict")
    if present is None:
        addition = ('\n# Local CODEX-only fleet MCP; no credentials or pinned seat.\n'
                    '[mcp_servers.agent-sync]\ncommand = ' + json.dumps(expected["command"]) + '\n'
                    'env_vars = ' + json.dumps(expected["env_vars"]) + '\n').encode()
        after_raw = original.rstrip(b"\n") + b"\n" + addition
        after = tomllib.loads(after_raw.decode())
        desired = copy.deepcopy(before)
        desired.setdefault("mcp_servers", {})["agent-sync"] = expected
        if after != desired:
            raise Refused("codex_config_semantics_changed")
        mode = stat.S_IMODE(config.stat().st_mode) if config.exists() else 0o600
        files[config] = (after_raw, mode)
    return files


def verify_existing(release: Path, files: dict) -> None:
    paths = list(release.rglob("*"))
    if any(p.is_symlink() for p in paths):
        raise Refused("existing_release_changed")
    if {p.relative_to(release).as_posix() for p in paths if p.is_file()} != set(files) | {"release.json"}:
        raise Refused("existing_release_changed")
    for name, entry in files.items():
        path = release / name
        if (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]
                or stat.S_IMODE(path.stat().st_mode) != entry["mode"]):
            raise Refused("existing_release_changed")


def verify_retained(release: Path) -> dict:
    metadata = release / "release.json"
    if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > MAX_BUNDLE_BYTES:
        raise Refused("invalid_release_metadata")
    try:
        record = json.loads(metadata.read_bytes())
        files = record["files"]
        if (not isinstance(files, dict) or not REQUIRED.issubset(files) or len(files) > MAX_FILES
                or not re.fullmatch(r"[0-9a-f]{40}", record["source_commit"])
                or record["bundle_sha256"] != release.name or manifest_digest(files) != release.name):
            raise Refused("invalid_release_metadata")
        for name, entry in files.items():
            if (not allowed_path(name) or set(entry) != {"mode", "sha256"}
                    or type(entry["mode"]) is not int or entry["mode"] not in (0o644, 0o755)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
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
            return {"status": "paused", "source_commit": bundle["source_commit"], "bundle_sha256": bundle["bundle_sha256"]}
        old = release_at(base, "current")
        if old is None and not install:
            raise Refused("initial_install_required")
        plan = install_plan(home) if install else {}
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
                json_write(stage / "release.json", {"source_commit": bundle["source_commit"],
                           "bundle_sha256": bundle["bundle_sha256"], "installed_at": int(time.time()),
                           "files": {name: {"mode": entry["mode"], "sha256": entry["sha256"]}
                                     for name, entry in bundle["files"].items()}})
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
                    backup = home / ".codex/backups" / ("fleet-cli-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]) / "config.toml"
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
        result = {"status": "unchanged" if old == new else "activated", "source_commit": bundle["source_commit"],
                  "bundle_sha256": bundle["bundle_sha256"]}
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
        return {"status": "paused" if (base / "paused").exists() else "ready",
                "source_commit": metadata.get("source_commit"),
                "bundle_sha256": current.name if current else None,
                "previous": previous.name if previous else None}
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


if __name__ == "__main__":
    raise SystemExit(main())
