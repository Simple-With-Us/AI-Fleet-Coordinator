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
LOG_MAX_BYTES = 2 * 1024 * 1024
MIRROR_ENV = "FLEET_RAG_MIRROR"
MIRROR_SOURCES = frozenset({"skill", "memory"})
# Skill and memory files live in the mirror.  Apple Notes are gated with them
# (AFC-760): a missing or stale marker skips ingest and prune for all three.
MIRROR_BACKED_SOURCES = frozenset({"skill", "memory", "apple-note"})
# The server live directory.  FLEET_RAG_MIRROR names this path in production.
DEFAULT_LIVE = Path("/home/jay/fleet-rag-mirror/current")
LIVE_NAME = "current"
INCOMING_NAME = "incoming"

# rsync filter for ~/.claude/projects: directory traversal plus memory files only.
CLAUDE_MEMORY_FILTERS = (
    "--prune-empty-dirs",
    "--include=*/",
    "--include=*/memory/***",
    "--exclude=*",
)
SKILL_TREES = (
    ("claude", ".claude"),
    ("cursor", ".cursor"),
    ("grok", ".grok"),
    ("codex", ".codex"),
    ("agents", ".agents"),
    ("gemini", ".gemini"),
)
LEGACY_DOC_FILTERS = (
    "--no-links",
    "--prune-empty-dirs",
    "--exclude=Generated/",
    "--exclude=.*/",
    "--exclude=credentials/",
    "--exclude=secrets/",
    "--exclude=backups/",
    "--include=*/",
    "--include=*.[mM][dD]",
    "--include=*.[tT][xX][tT]",
    "--include=*.[hH][tT][mM][lL]",
    "--include=*.[hH][tT][mM]",
    "--include=*.[pP][dD][fF]",
    "--exclude=*",
)


class MirrorError(RuntimeError):
    """A push or promote that must leave the live tree untouched."""


@dataclass(frozen=True)
class MirrorStatus:
    fresh: bool
    reason: str
    at: int | None = None
    age_s: int | None = None


@dataclass(frozen=True)
class Tree:
    rel: str
    kind: str
    local: Path

    @property
    def filters(self) -> tuple[str, ...]:
        if self.kind == "claude-memory":
            return CLAUDE_MEMORY_FILTERS
        if self.kind == "legacy-docs":
            return LEGACY_DOC_FILTERS
        return ()


@dataclass(frozen=True)
class PushResult:
    promoted: bool
    reason: str
    folder_counts: dict[str, int]


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


def configured_root(env: dict | None = None) -> Path | None:
    """The live mirror whose marker decides ingest, or None for local trees.

    An explicit ``FLEET_RAG_MIRROR`` wins, including a blank value, which
    leaves the mirror off.  With the variable unset, the server tree is used
    when that directory exists, and also when ``HOME`` is ``/home/jay`` so a
    missing marker is still a skip.  A Mac run with neither hits None and
    keeps reading its local skill and memory files.
    """
    root = mirror_root(env)
    if root is not None:
        return root
    source = os.environ if env is None else env
    if MIRROR_ENV in source:
        return None
    if DEFAULT_LIVE.parent.is_dir():
        return DEFAULT_LIVE
    home = (source.get("HOME") or "").strip()
    if not home and env is None:
        home = str(Path.home())
    if home == "/home/jay":
        return DEFAULT_LIVE
    return None


def _mtime_age(path: Path, now: float) -> int | None:
    try:
        return int(now) - int(path.stat().st_mtime)
    except OSError:
        return None


def _stamp_age(at: object, now: float, fallback: int | None) -> int | None:
    if isinstance(at, bool) or not isinstance(at, int):
        return fallback
    return int(now) - at


def stale_record(root: Path | None, now: float, max_age: int) -> dict | None:
    """The ``mirror_stale`` object, or None when mirror-backed sources may run."""
    if root is None:
        return None
    status = mirror_status(root, now, max_age)
    if status.fresh:
        return None
    return {
        "age_s": status.age_s,
        "reason": status.reason,
        "marker": str(root / COMPLETE_NAME),
        "max_age_s": max_age,
    }


def skill_dirs(root: Path) -> tuple[Path, ...]:
    return tuple(root / "skills" / name for name, _directory in SKILL_TREES)


def memory_claude(root: Path) -> Path:
    return root / "memory" / "claude-projects"


def memory_codex(root: Path) -> Path:
    return root / "memory" / "codex"


def default_log_path(home: Path) -> Path:
    return home / "Library" / "Logs" / "fleet-rag-mirror.log"


def maybe_rotate_log(path: Path, *, max_bytes: int = LOG_MAX_BYTES) -> None:
    """Rotate ``path`` to ``path.1`` when it grows past ``max_bytes`` (single generation)."""
    try:
        if path.is_file() and path.stat().st_size > max_bytes:
            rotated = path.with_name(path.name + ".1")
            if rotated.is_file():
                rotated.unlink()
            os.replace(path, rotated)
    except OSError:
        pass


def append_mirror_log(path: Path, line: str, *, max_bytes: int = LOG_MAX_BYTES) -> None:
    """Append one line to the Mac mirror log, rotating on size when needed."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        maybe_rotate_log(path, max_bytes=max_bytes)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line.rstrip("\n") + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def format_push_summary(
    *,
    outcome: str,
    duration_s: float,
    folder_counts: dict[str, int],
    detail: str = "",
) -> str:
    """One parseable summary line for a push run."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    parts = [f"{stamp} mirror summary outcome={outcome} duration_s={duration_s:.3f}"]
    if detail:
        parts.append(f"detail={detail}")
    if folder_counts:
        files = ",".join(f"{rel}:{count}" for rel, count in sorted(folder_counts.items()))
        parts.append(f"files={files}")
    return " ".join(parts)


def default_trees(home: Path) -> tuple[Tree, ...]:
    return (
        *(
            Tree("skills/" + name, "tree", home / directory / "skills")
            for name, directory in SKILL_TREES
        ),
        Tree("memory/claude-projects", "claude-memory", home / ".claude" / "projects"),
        Tree("memory/codex", "tree", home / ".codex" / "memories"),
        Tree("docs/legacy", "legacy-docs", home / "Code" / "Docs"),
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
    if tree.kind == "legacy-docs":
        total = 0
        for directory, dirs, files in os.walk(tree.local, followlinks=False):
            dirs[:] = [
                name
                for name in dirs
                if not name.startswith(".")
                and name not in ("Generated", "credentials", "secrets", "backups")
                and not (Path(directory) / name).is_symlink()
            ]
            for name in files:
                path = Path(directory) / name
                if (
                    path.suffix.casefold() in (".md", ".txt", ".html", ".htm", ".pdf")
                    and path.is_file()
                    and not path.is_symlink()
                ):
                    total += 1
        return total
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
    """Whether skill and memory may be read and pruned against this mirror.

    ``age_s`` is seconds since the stamp's ``at`` when that field is an int,
    otherwise seconds since the marker's mtime when the file exists.
    """
    if root is None or not root.is_dir():
        return MirrorStatus(False, "missing")
    stamp = root / COMPLETE_NAME
    if not stamp.is_file() or stamp.is_symlink():
        return MirrorStatus(False, "missing-stamp")
    file_age = _mtime_age(stamp, now)
    try:
        data = json.loads(stamp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return MirrorStatus(False, "bad-stamp", age_s=file_age)
    if not isinstance(data, dict) or data.get("complete") is not True:
        at = data.get("at") if isinstance(data, dict) else None
        return MirrorStatus(False, "incomplete", age_s=_stamp_age(at, now, file_age))
    at = data.get("at")
    if isinstance(at, bool) or not isinstance(at, int):
        return MirrorStatus(False, "bad-stamp", age_s=file_age)
    age = int(now) - at
    if at > int(now) + 600:
        return MirrorStatus(False, "bad-stamp", at, age)
    if age > max_age:
        return MirrorStatus(False, "stale", at, age)
    return MirrorStatus(True, "fresh", at, age)


def _fsync_dir(directory: Path) -> None:
    """Fsync a directory so a rename or replace inside it survives a crash."""
    fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_stamp(directory: Path, now: float, counts: dict[str, int]) -> None:
    payload = json.dumps({"complete": True, "at": int(now), "files": counts}) + "\n"
    tmp = directory / ".complete.tmp"
    path = directory / COMPLETE_NAME
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)  # the stamp's contents must be durable before the rename publishes it
    finally:
        os.close(fd)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    _fsync_dir(directory)


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
    swapped = False
    try:
        if live.exists():
            os.rename(live, previous)
            moved = True
        os.rename(incoming, live)
        swapped = True
        _fsync_dir(live.parent)
    except OSError:
        if swapped:
            # The durability step failed after the swap:  put every directory back,
            # so a raised promote still leaves live exactly as it was.
            os.rename(live, incoming)
            if moved and previous.exists():
                os.rename(previous, live)
        elif moved and previous.exists() and not live.exists():
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
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", "--", target]


def rsync_argv(
    local: Path, remote_spec: str, *, delete: bool, filters: tuple[str, ...] = ()
) -> list[str]:
    argv = [
        "rsync",
        "-a",
        # The destination is incoming/<group>/<tree>/ and incoming starts empty;  without
        # --mkpath rsync creates only the last component and exits 11 (AFC-687).
        "--mkpath",
        "--timeout=120",
        "-e",
        "ssh -o BatchMode=yes -o ConnectTimeout=20",
    ]
    if delete:
        argv.append("--delete")
    argv.extend(filters)
    argv.append("--")
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
            f"--incoming={base}/{INCOMING_NAME}",
            f"--live={base}/{LIVE_NAME}",
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
    folder_counts: dict[str, int] = {}
    incoming_prefix = f"{target}:{root}/{INCOMING_NAME}/"
    code = run(reset_incoming_argv(target, root), None)
    if code != 0:
        return PushResult(False, "incoming-reset-failed", folder_counts)
    expect: dict[str, int | None] = {}
    for tree in default_trees(home):
        if tree.local.is_dir():
            spec = incoming_prefix + tree.rel + "/"
            if not spec.startswith(incoming_prefix):
                return PushResult(False, "bad-destination", folder_counts)
            code = run(
                rsync_argv(tree.local, spec, delete=True, filters=tree.filters),
                None,
            )
            if code != 0:
                folder_counts[tree.rel] = count_local(tree)
                return PushResult(False, f"rsync-failed:{tree.rel}", folder_counts)
            expect[tree.rel] = count_local(tree)
            folder_counts[tree.rel] = expect[tree.rel]
        else:
            code = run(preserve_argv(target, root, tree.rel), None)
            if code != 0:
                return PushResult(False, f"preserve-failed:{tree.rel}", folder_counts)
            expect[tree.rel] = None
            folder_counts[tree.rel] = 0
    payload = json.dumps(expect, sort_keys=True)
    code = run(
        promote_argv(target, remote_python, remote_script, root),
        payload,
    )
    if code != 0:
        return PushResult(False, "promote-failed", folder_counts)
    return PushResult(True, "promoted", folder_counts)


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


def _write_push_summary(
    log_path: Path,
    *,
    outcome: str,
    duration_s: float,
    folder_counts: dict[str, int],
    detail: str = "",
) -> None:
    line = format_push_summary(
        outcome=outcome,
        duration_s=duration_s,
        folder_counts=folder_counts,
        detail=detail,
    )
    append_mirror_log(log_path, line)
    sys.stderr.write(line + "\n")


def _cmd_push(config_path: Path) -> int:
    started = time.monotonic()
    cfg = _load_config(config_path)
    home = Path(cfg.get("home") or Path.home())
    log_path = Path(cfg.get("log") or default_log_path(home))

    def finish(
        outcome: str,
        *,
        folder_counts: dict[str, int] | None = None,
        detail: str = "",
        exit_code: int,
    ) -> int:
        duration_s = time.monotonic() - started
        _write_push_summary(
            log_path,
            outcome=outcome,
            duration_s=duration_s,
            folder_counts=folder_counts or {},
            detail=detail,
        )
        return exit_code

    if cfg.get("enabled") is not True:
        sys.stderr.write("mirror: disabled\n")
        return finish("disabled", exit_code=0)
    lock_path = Path(cfg.get("lock") or (home / ".local" / "state" / "fleet-rag-mirror.lock"))
    lock_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        sys.stderr.write("mirror: skip, another push is active\n")
        return finish("skipped", detail="concurrent", exit_code=0)
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
        return finish(
            "failed",
            folder_counts=result.folder_counts,
            detail=result.reason,
            exit_code=1,
        )
    sys.stderr.write("mirror: promoted\n")
    return finish("promoted", folder_counts=result.folder_counts, exit_code=0)


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
