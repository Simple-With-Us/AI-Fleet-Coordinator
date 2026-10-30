#!/usr/bin/env python3
"""Push published fleet CLI blobs through the existing Coolify SSH connection.

No fetch, checkout, credentials, listener controls, or model calls.  The existing
Mac runtime updater supplies the next published revision on its next tick.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_SOURCE = "/Users/jay/apps/lanes/_managed/fleet/agent-sync-runtime"
RECEIVER_PATH = "scripts/fleet-cli-release.py"
# The reviewed allowlisted runtime contains 133 files. Keep the exact minimum
# ceiling in sender and receiver: a new file still needs an explicit policy review.
# The 132nd file is storage_guard.py (from main);  the 133rd is platform_letter.py (AFC-564, the shared session-letter
# library), added for that review.
MAX_FILES = 133
MAX_BYTES = 8 * 1024 * 1024
OVERALL_SECONDS = 90
SSH_SECONDS = 65
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
SAFE_PATH = re.compile(r"[A-Za-z0-9_./-]+\Z")
EXPECTED_ORIGINS = frozenset(
    {
        "https://github.com/Simple-With-Us/AI-Fleet-Coordinator",
        "https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git",
        "git@github.com:Simple-With-Us/AI-Fleet-Coordinator.git",
        "ssh://git@github.com/Simple-With-Us/AI-Fleet-Coordinator.git",
    }
)


class SyncError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class Runner:
    def __init__(self, seconds: float = OVERALL_SECONDS):
        self.deadline = time.monotonic() + seconds

    def run(
        self,
        argv: list[str],
        *,
        data: bytes | None = None,
        timeout: float = 10,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise SyncError("deadline_exceeded")
        try:
            return subprocess.run(
                argv,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=min(timeout, remaining),
            )
        except subprocess.TimeoutExpired:
            raise SyncError("command_timeout") from None
        except OSError:
            raise SyncError("command_unavailable") from None


def git_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_TERMINAL_PROMPT="0",
        GIT_OPTIONAL_LOCKS="0",
    )
    return env


def git(
    runner: Runner,
    source: Path,
    *args: str,
    data: bytes | None = None,
    codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess:
    result = runner.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(source), *args], data=data, env=git_env()
    )
    if result.returncode not in codes:
        raise SyncError("source_git_failed")
    return result


def published_commit(runner: Runner, source: Path) -> str:
    origin = (
        git(runner, source, "remote", "get-url", "origin").stdout.decode("utf-8", "strict").strip()
    )
    if origin not in EXPECTED_ORIGINS:
        raise SyncError("source_origin_refused")
    head = (
        git(runner, source, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode("ascii").strip()
    )
    if not HEX40.fullmatch(head):
        raise SyncError("source_commit_invalid")
    ancestor = git(
        runner,
        source,
        "merge-base",
        "--is-ancestor",
        head,
        "refs/remotes/origin/main",
        codes=(0, 1),
    )
    if ancestor.returncode:
        raise SyncError("source_not_published")
    dirty = git(
        runner,
        source,
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--quiet",
        "HEAD",
        "--",
        codes=(0, 1),
    )
    if dirty.returncode:
        raise SyncError("source_tracked_dirty")
    return head


def tree_entries(
    runner: Runner, source: Path, commit: str
) -> list[tuple[str, str, str, int | None]]:
    raw = git(runner, source, "ls-tree", "-r", "-l", "-z", "--full-tree", commit).stdout
    if len(raw) > 16 * MAX_BYTES:
        raise SyncError("source_tree_too_large")
    entries = []
    try:
        for row in raw.split(b"\0"):
            if not row:
                continue
            metadata, name = row.split(b"\t", 1)
            mode, kind, oid, size = metadata.split()
            if kind != b"blob":
                # Keep non-blobs visible so an allowlisted gitlink cannot disappear silently.
                count = None
            else:
                count = int(size)
            entries.append((name.decode("utf-8"), mode.decode("ascii"), oid.decode("ascii"), count))
    except (ValueError, UnicodeError):
        raise SyncError("source_tree_invalid") from None
    return entries


def blobs(
    runner: Runner, source: Path, entries: list[tuple[str, str, str, int | None]]
) -> dict[str, bytes]:
    if not entries:
        return {}
    result = git(
        runner,
        source,
        "cat-file",
        "--batch",
        data=("\n".join(row[2] for row in entries) + "\n").encode("ascii"),
    )
    raw, offset, found = result.stdout, 0, {}
    for path, _mode, oid, expected_size in entries:
        try:
            end = raw.index(b"\n", offset)
            got_oid, kind, count = raw[offset:end].split()
            size = int(count)
        except (ValueError, IndexError):
            raise SyncError("source_blob_invalid") from None
        offset = end + 1
        if got_oid.decode("ascii") != oid or kind != b"blob" or size != expected_size or size < 0:
            raise SyncError("source_blob_invalid")
        body = raw[offset : offset + size]
        offset += size
        if len(body) != size or raw[offset : offset + 1] != b"\n":
            raise SyncError("source_blob_invalid")
        offset += 1
        found[path] = body
    if offset != len(raw):
        raise SyncError("source_blob_invalid")
    return found


def receiver_policy(text: bytes):
    """Import only the verified receiver blob; checkout files are never the policy source."""
    try:
        with tempfile.TemporaryDirectory(prefix="fleet-cli-policy-") as scratch:
            file = Path(scratch) / "receiver.py"
            file.write_bytes(text)
            spec = importlib.util.spec_from_file_location("_fleet_cli_published_receiver", file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            try:
                spec.loader.exec_module(module)
            finally:
                sys.modules.pop(spec.name, None)
        if (
            module.MAX_BUNDLE_BYTES != MAX_BYTES
            or not callable(module.allowed_path)
            or not callable(module.manifest_digest)
        ):
            raise ValueError
        return module
    except Exception:
        raise SyncError("receiver_policy_invalid") from None


def build_bundle(source: Path, runner: Runner | None = None) -> tuple[dict, bytes, str]:
    runner = runner or Runner()
    commit = published_commit(runner, source)
    entries = tree_entries(runner, source, commit)
    receiver = next((row for row in entries if row[0] == RECEIVER_PATH), None)
    if (
        receiver is None
        or receiver[1] not in ("100644", "100755")
        or receiver[3] is None
        or receiver[3] > MAX_BYTES
    ):
        raise SyncError("receiver_blob_refused")
    receiver_text = blobs(runner, source, [receiver])[RECEIVER_PATH]
    policy = receiver_policy(receiver_text)
    selected = [row for row in entries if policy.allowed_path(row[0])]
    if len(selected) > MAX_FILES:
        raise SyncError("bundle_too_many_files")
    if not selected:
        raise SyncError("bundle_empty")
    for path, mode, oid, size in selected:
        if (
            not SAFE_PATH.fullmatch(path)
            or mode not in ("100644", "100755")
            or not HEX40.fullmatch(oid)
            or size is None
            or size < 0
        ):
            raise SyncError("source_entry_refused")
    if sum(row[3] for row in selected) > MAX_BYTES:
        raise SyncError("bundle_too_large")
    bodies = blobs(runner, source, selected)
    files = {
        path: {
            "mode": 0o755 if mode == "100755" else 0o644,
            "sha256": hashlib.sha256(bodies[path]).hexdigest(),
            "data": base64.b64encode(bodies[path]).decode("ascii"),
        }
        for path, mode, _oid, _size in selected
    }
    digest = policy.manifest_digest(files)
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        raise SyncError("receiver_policy_invalid")
    bundle = {"schema": 1, "source_commit": commit, "bundle_sha256": digest, "files": files}
    encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise SyncError("bundle_too_large")
    return bundle, encoded, receiver_text.decode("utf-8")


def ssh_argv(receiver_text: str, *, install: bool = False) -> list[str]:
    command = ["/usr/bin/python3", "-I", "-B", "-c", receiver_text, "receive"]
    if install:
        command.append("--install")
    return [
        "ssh",
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "ControlMaster=no",
        "-o",
        "ConnectTimeout=10",
        "root@coolify",
        shlex.join(command),
    ]


def receipt(result: subprocess.CompletedProcess, bundle: dict) -> dict:
    if result.returncode:
        raise SyncError("ssh_failed")
    if len(result.stdout) > 8192:
        raise SyncError("receipt_invalid")
    try:
        response = json.loads(result.stdout)
    except (ValueError, UnicodeError):
        raise SyncError("receipt_invalid") from None
    if not isinstance(response, dict):
        raise SyncError("receipt_invalid")
    if response.get("status") == "paused":
        raise SyncError("receiver_paused")
    if (
        response.get("status") not in ("activated", "unchanged")
        or response.get("source_commit") != bundle["source_commit"]
        or response.get("bundle_sha256") != bundle["bundle_sha256"]
    ):
        raise SyncError("receipt_invalid")
    return {key: response[key] for key in ("status", "source_commit", "bundle_sha256")}


def private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SyncError("state_path_refused")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir():
        raise SyncError("state_path_refused")
    os.chmod(path, 0o700)


@contextmanager
def run_lock(path: Path):
    private_directory(path)
    fd = os.open(path / "sync.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("already_running") from None
        yield
    finally:
        os.close(fd)


def load_state(path: Path) -> dict:
    if path.is_symlink():
        raise SyncError("state_path_refused")
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, state: dict) -> None:
    if path.is_symlink():
        raise SyncError("state_path_refused")
    fd, name = tempfile.mkstemp(prefix="state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(state, file, sort_keys=True, separators=(",", ":"))
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def sync(
    source: Path,
    state_dir: Path,
    *,
    dry_run: bool = False,
    force: bool = False,
    install: bool = False,
    runner: Runner | None = None,
) -> dict:
    runner = runner or Runner()
    with run_lock(state_dir):
        path = state_dir / "state.json"
        state = load_state(path)
        try:
            bundle, encoded, receiver_text = build_bundle(source, runner)
            basic = {
                "source_commit": bundle["source_commit"],
                "bundle_sha256": bundle["bundle_sha256"],
            }
            if dry_run:
                return dict(basic, status="dry_run", files=len(bundle["files"]), bytes=len(encoded))
            if (
                not force
                and not install
                and state.get("last_success_bundle_sha256") == bundle["bundle_sha256"]
            ):
                state.update(last_checked_at=int(time.time()), last_check_status="skipped")
                save_state(path, state)
                return dict(basic, status="skipped")
            response = receipt(
                runner.run(
                    ssh_argv(receiver_text, install=install), data=encoded, timeout=SSH_SECONDS
                ),
                bundle,
            )
            state.update(
                last_success_bundle_sha256=bundle["bundle_sha256"],
                last_success_source_commit=bundle["source_commit"],
                last_success_at=int(time.time()),
                last_checked_at=int(time.time()),
                last_check_status=response["status"],
                last_error_code=None,
                last_error_at=None,
            )
            save_state(path, state)
            return response
        except SyncError as exc:
            if not dry_run:
                state.update(
                    last_error_code=exc.code,
                    last_error_at=int(time.time()),
                    last_checked_at=int(time.time()),
                    last_check_status="error",
                )
                save_state(path, state)
            raise
        except Exception:
            if not dry_run:
                state.update(
                    last_error_code="local_error",
                    last_error_at=int(time.time()),
                    last_checked_at=int(time.time()),
                    last_check_status="error",
                )
                save_state(path, state)
            raise SyncError("local_error") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-repo", type=Path, default=Path(DEFAULT_SOURCE))
    parser.add_argument(
        "--state-dir", type=Path, default=Path.home() / ".agent-sync/coolify-cli-sync"
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--install",
        action="store_true",
        help="Register wrappers and MCP on the initial installation only",
    )
    args = parser.parse_args(argv)
    try:
        print(
            json.dumps(
                sync(
                    args.source_repo,
                    args.state_dir,
                    dry_run=args.dry_run,
                    force=args.force,
                    install=args.install,
                ),
                sort_keys=True,
            )
        )
        return 0
    except SyncError as exc:
        print(json.dumps({"status": "error", "code": exc.code}, sort_keys=True))
        return 0 if exc.code == "already_running" else 1
    except Exception:
        # Never expose git/SSH stderr, remote URLs, filesystem details, or exception messages.
        print('{"code":"local_error","status":"error"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
