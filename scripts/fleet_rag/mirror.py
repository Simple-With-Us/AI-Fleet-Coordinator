"""Mac to server mirror of skill and memory files (AFC-687).

The Mac push copies into a sibling incoming directory.  Promote swaps that
directory onto the live one only after every rsynced tree's file count matches.
A failed or partial copy never deletes the live tree, so a later ingest cannot
prune skill or memory from a half-written mirror.

Live layout (the directory ``FLEET_RAG_MIRROR`` names):

    skills/claude/  skills/cursor/  skills/grok/
    memory/claude-projects/  memory/codex/
    .complete

``.complete`` is JSON written only by a successful promote.  The incoming
directory is a sibling of the live directory (``<root>/incoming``), never a
child of it.  This module is stdlib only so ``python3 -I mirror.py`` works
on the server without the package path.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

COMPLETE_NAME = ".complete"
DEFAULT_MAX_AGE_S = 36 * 3600
MIN_MAX_AGE_S = 3600
MAX_MAX_AGE_S = 14 * 24 * 3600
MIRROR_ENV = "FLEET_RAG_MIRROR"
MIRROR_SOURCES = frozenset({"skill", "memory"})
LIVE_NAME = "current"
INCOMING_NAME = "incoming"

# rsync filter for ~/.claude/projects: directory traversal plus memory files only.
CLAUDE_MEMORY_FILTERS = (
    "--prune-empty-dirs",
    "--include=*/",
    "--include=*/memory/***",
    "--exclude=*",
)


class MirrorError(RuntimeError):
    """A push or promote that must leave the live tree untouched."""


@dataclass(frozen=True)
class MirrorStatus:
    fresh: bool
    reason: str
    at: int | None = None


@dataclass(frozen=True)
class Tree:
    rel: str
    kind: str
    local: Path

    @property
    def filters(self) -> tuple[str, ...]:
        if self.kind == "claude-memory":
            return CLAUDE_MEMORY_FILTERS
        return ()


@dataclass(frozen=True)
class PushResult:
    promoted: bool
    reason: str


def mirror_root(env: dict | None = None) -> Path | None:
    """The live mirror directory, or None when the server is not using one."""
    source = os.environ if env is None else env
    raw = (source.get(MIRROR_ENV) or "").strip()
    return Path(raw) if raw else None


def max_age_s(env: dict | None = None) -> int:
    source = os.environ if env is None else env
    raw = (source.get("FLEET_RAG_MIRROR_MAX_AGE_S") or "").strip()
    if not raw:
        return DEFAULT_MAX_AGE_S
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_AGE_S
    if value < MIN_MAX_AGE_S or value > MAX_MAX_AGE_S:
        return DEFAULT_MAX_AGE_S
    return value


def skill_dirs(root: Path) -> tuple[Path, Path, Path]:
    return (
        root / "skills" / "claude",
        root / "skills" / "cursor",
        root / "skills" / "grok",
    )


def memory_claude(root: Path) -> Path:
    return root / "memory" / "claude-projects"


def memory_codex(root: Path) -> Path:
    return root / "memory" / "codex"


def default_trees(home: Path) -> tuple[Tree, ...]:
    return (
        Tree("skills/claude", "tree", home / ".claude" / "skills"),
        Tree("skills/cursor", "tree", home / ".cursor" / "skills"),
        Tree("skills/grok", "tree", home / ".grok" / "skills"),
        Tree("memory/claude-projects", "claude-memory", home / ".claude" / "projects"),
        Tree("memory/codex", "tree", home / ".codex" / "memories"),
    )


def live_dir(root: Path) -> Path:
    return root / LIVE_NAME


def incoming_dir(root: Path) -> Path:
    return root / INCOMING_NAME


def count_files(root: Path) -> int:
    """Files and symlinks under ``root``, skipping dot names such as the stamp."""
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        if path.name.startswith("."):
            continue
        if path.is_symlink() or path.is_file():
            total += 1
    return total


def count_local(tree: Tree) -> int:
    """The files a successful rsync of this tree would place in incoming."""
    if tree.kind == "claude-memory":
        total = 0
        if not tree.local.is_dir():
            return 0
        for project in tree.local.iterdir():
            memory = project / "memory"
            if not memory.is_dir():
                continue
            for path in memory.rglob("*"):
                if path.name.startswith("."):
                    continue
                if path.is_symlink() or path.is_file():
                    total += 1
        return total
    return count_files(tree.local)


def mirror_status(root: Path | None, now: float, max_age: int) -> MirrorStatus:
    """Whether skill and memory may be pruned against this mirror."""
    if root is None or not root.is_dir():
        return MirrorStatus(False, "missing")
    stamp = root / COMPLETE_NAME
    if not stamp.is_file() or stamp.is_symlink():
        return MirrorStatus(False, "missing-stamp")
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MirrorStatus(False, "bad-stamp")
    if not isinstance(data, dict) or data.get("complete") is not True:
        return MirrorStatus(False, "incomplete")
    at = data.get("at")
    if isinstance(at, bool) or not isinstance(at, int):
        return MirrorStatus(False, "bad-stamp")
    if at > int(now) + 600:
        return MirrorStatus(False, "bad-stamp", at)
    if int(now) - at > max_age:
        return MirrorStatus(False, "stale", at)
    return MirrorStatus(True, "fresh", at)


def write_stamp(directory: Path, now: float, counts: dict[str, int]) -> None:
    payload = {"complete": True, "at": int(now), "files": counts}
    tmp = directory / ".complete.tmp"
    path = directory / COMPLETE_NAME
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def _within(child: Path, parent: Path) -> bool:
    child_parts = child.resolve().parts
    parent_parts = parent.resolve().parts
    return len(child_parts) > len(parent_parts) and child_parts[: len(parent_parts)] == parent_parts


def promote(
    incoming: Path, live: Path, expect: dict[str, int | None], now: float
) -> dict[str, int]:
    """Swap a finished incoming directory onto live, or raise and leave live as it was.

    ``expect`` maps a relative tree to the file count a rsync must have produced,
    or None when that tree was copied from the previous live directory because the
    Mac side did not have it.  A mismatch refuses the swap.
    """
    if not expect:
        raise MirrorError("empty-expect")
    if not incoming.is_dir():
        raise MirrorError("incoming-missing")
    if incoming.resolve() == live.resolve() or _within(incoming, live) or _within(live, incoming):
        raise MirrorError("nested")
    counts: dict[str, int] = {}
    for rel, want in expect.items():
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise MirrorError("bad-tree")
        staged = incoming / rel
        if not staged.is_dir():
            raise MirrorError(f"missing-staged:{rel}")
        got = count_files(staged)
        if want is not None and got != want:
            raise MirrorError(f"count-mismatch:{rel}")
        counts[rel] = got
    write_stamp(incoming, now, counts)
    previous = live.parent / f"{live.name}.previous"
    if previous.exists():
        shutil.rmtree(previous)
    moved = False
    try:
        if live.exists():
            os.rename(live, previous)
            moved = True
        os.rename(incoming, live)
    except OSError:
        if moved and previous.exists() and not live.exists():
            os.rename(previous, live)
        raise
    if not (live / COMPLETE_NAME).is_file():
        if moved and previous.exists():
            if live.exists():
                shutil.rmtree(live)
            os.rename(previous, live)
        raise MirrorError("stamp-missing")
    if previous.exists():
        shutil.rmtree(previous)
    return counts


def _ssh(target: str) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", target]


def rsync_argv(
    local: Path, remote_spec: str, *, delete: bool, filters: tuple[str, ...] = ()
) -> list[str]:
    argv = [
        "rsync",
        "-a",
        "--timeout=120",
        "-e",
        "ssh -o BatchMode=yes -o ConnectTimeout=20",
    ]
    if delete:
        argv.append("--delete")
    argv.extend(filters)
    argv.append(str(local).rstrip("/") + "/")
    argv.append(remote_spec)
    return argv


def reset_incoming_argv(target: str, root: str) -> list[str]:
    incoming = shlex.quote(root.rstrip("/") + "/" + INCOMING_NAME)
    script = f"rm -rf -- {incoming} && mkdir -p -- {incoming}"
    return [*_ssh(target), script]


def preserve_argv(target: str, root: str, rel: str) -> list[str]:
    """Copy one live tree into incoming when the Mac does not have that tree.

    The live tree is the source of the copy, never the target of a delete.
    """
    base = root.rstrip("/")
    src = shlex.quote(f"{base}/{LIVE_NAME}/{rel}")
    dst = shlex.quote(f"{base}/{INCOMING_NAME}/{rel}")
    script = (
        f'if [ -d {src} ]; then mkdir -p -- "$(dirname -- {dst})" && rm -rf -- {dst} && '
        f"cp -a -- {src} {dst}; else mkdir -p -- {dst}; fi"
    )
    return [*_ssh(target), script]


def promote_argv(target: str, remote_python: str, remote_script: str, root: str) -> list[str]:
    base = root.rstrip("/")
    command = " ".join(
        shlex.quote(part)
        for part in (
            remote_python,
            "-I",
            remote_script,
            "promote",
            "--incoming",
            f"{base}/{INCOMING_NAME}",
            "--live",
            f"{base}/{LIVE_NAME}",
        )
    )
    return [*_ssh(target), command]


def _check_target(target: str) -> str:
    if not target or any(ch.isspace() or ch in "\"'`$;|&<>" for ch in target):
        raise MirrorError("bad-ssh-target")
    return target


def _check_root(root: str) -> str:
    if not root.startswith("/") or root == "/" or "\n" in root or ".." in Path(root).parts:
        raise MirrorError("bad-remote-root")
    return root.rstrip("/")


def push(
    *,
    home: Path,
    ssh_target: str,
    remote_root: str,
    remote_python: str,
    remote_script: str,
    run,
) -> PushResult:
    """Copy the Mac trees and promote only when every rsync finishes.

    ``run(argv, input_text) -> exit code``.  A non-zero exit before promote
    returns without calling promote, so the live directory is unchanged.
    """
    target = _check_target(ssh_target)
    root = _check_root(remote_root)
    incoming_prefix = f"{target}:{root}/{INCOMING_NAME}/"
    code = run(reset_incoming_argv(target, root), None)
    if code != 0:
        return PushResult(False, "incoming-reset-failed")
    expect: dict[str, int | None] = {}
    for tree in default_trees(home):
        if tree.local.is_dir():
            spec = incoming_prefix + tree.rel + "/"
            if not spec.startswith(incoming_prefix):
                return PushResult(False, "bad-destination")
            code = run(
                rsync_argv(tree.local, spec, delete=True, filters=tree.filters),
                None,
            )
            if code != 0:
                return PushResult(False, f"rsync-failed:{tree.rel}")
            expect[tree.rel] = count_local(tree)
        else:
            code = run(preserve_argv(target, root, tree.rel), None)
            if code != 0:
                return PushResult(False, f"preserve-failed:{tree.rel}")
            expect[tree.rel] = None
    payload = json.dumps(expect, sort_keys=True)
    code = run(
        promote_argv(target, remote_python, remote_script, root),
        payload,
    )
    if code != 0:
        return PushResult(False, "promote-failed")
    return PushResult(True, "promoted")


def _default_run(argv: list[str], input_text: str | None) -> int:
    proc = subprocess.run(
        argv,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=1800,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"mirror: command failed exit={proc.returncode}\n")
    return proc.returncode


def _load_config(path: Path) -> dict:
    if not path.is_file() or path.is_symlink():
        raise MirrorError("config-missing")
    if path.stat().st_mode & 0o022:
        raise MirrorError("config-writable")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise MirrorError("config-invalid")
    return data


def _cmd_push(config_path: Path) -> int:
    cfg = _load_config(config_path)
    if cfg.get("enabled") is not True:
        sys.stderr.write("mirror: disabled\n")
        return 0
    home = Path(cfg.get("home") or Path.home())
    lock_path = Path(cfg.get("lock") or (home / ".local" / "state" / "fleet-rag-mirror.lock"))
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        sys.stderr.write("mirror: skip, another push is active\n")
        return 0
    try:
        result = push(
            home=home,
            ssh_target=str(cfg["ssh_target"]),
            remote_root=str(cfg["remote_root"]),
            remote_python=str(cfg.get("remote_python") or "/usr/bin/python3"),
            remote_script=str(cfg["remote_script"]),
            run=_default_run,
        )
    finally:
        os.close(fd)
    if not result.promoted:
        sys.stderr.write(f"mirror: {result.reason}\n")
        return 1
    sys.stderr.write("mirror: promoted\n")
    return 0


def _cmd_promote(incoming: Path, live: Path) -> int:
    raw = sys.stdin.read()
    try:
        expect = json.loads(raw)
    except ValueError:
        sys.stderr.write("mirror: expect-invalid\n")
        return 1
    if not isinstance(expect, dict):
        sys.stderr.write("mirror: expect-invalid\n")
        return 1
    parsed: dict[str, int | None] = {}
    for key, value in expect.items():
        if not isinstance(key, str):
            sys.stderr.write("mirror: expect-invalid\n")
            return 1
        if value is None:
            parsed[key] = None
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            sys.stderr.write("mirror: expect-invalid\n")
            return 1
        else:
            parsed[key] = value
    try:
        promote(incoming, live, parsed, time.time())
    except (MirrorError, OSError) as exc:
        reason = exc.args[0] if isinstance(exc, MirrorError) else type(exc).__name__
        sys.stderr.write(f"mirror: {reason}\n")
        return 1
    sys.stderr.write("mirror: promoted\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push or promote the skill and memory mirror.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    push_parser = sub.add_parser("push", help="rsync local trees and promote on success")
    push_parser.add_argument("--config", type=Path, required=True)
    promote_parser = sub.add_parser("promote", help="swap incoming onto live when counts match")
    promote_parser.add_argument("--incoming", type=Path, required=True)
    promote_parser.add_argument("--live", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "push":
            return _cmd_push(args.config)
        if args.cmd == "promote":
            return _cmd_promote(args.incoming, args.live)
    except MirrorError as exc:
        sys.stderr.write(f"mirror: {exc}\n")
        return 1
    except KeyError:
        sys.stderr.write("mirror: config-invalid\n")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
