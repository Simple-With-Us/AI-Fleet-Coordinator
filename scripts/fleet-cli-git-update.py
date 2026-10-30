#!/usr/bin/python3
"""Run this host's fleet CLI tools straight from a git checkout of AI-Fleet-Coordinator main.

Replaces the Mac-pushed release copies (fleet-cli-sync.py and fleet-cli-release.py receive) on the
Hetzner server, owner 2026-10-19:  git is the history, so no copies are stored.

  ~/.local/share/fleet-cli/repo      a clone of AFC, checked out detached at the live commit
  ~/.local/share/fleet-cli/current   -> repo  (the wrappers in ~/.local/bin exec current/<script>)

Commands:

  setup     clone (once), move to origin/main after a smoke test, point `current` at the checkout,
            write any wrapper the host lacks, and pause the old receiver so a Mac push cannot
            repoint `current`.
  update    fetch;  when origin/main moved, check it out, smoke-test it, and keep it, or go back to
            the last good commit and remember the bad one until main moves again.  Run by the
            fleet-cli-git-update systemd unit:  started by GitHub on a merge to main, and hourly.
  rollback  go back to the previous good commit and skip the current one until main moves.
  status    the live commit, the previous one, and any skipped commit, as JSON.

Never reads credentials.  Python 3.11+, standard library only.

A host that is stuck on an old commit.  The smoke list is read from the commit being tested,
so a CLI that a release removed cannot fail the smoke and lock a host that runs this
updater.  A host that runs an updater from before that change can still be stuck, because
its own list asks for the removed CLI:  recover it by running the target's updater once, so
the smoke reads the target's own list.

    cd ~/.local/share/fleet-cli/repo && git checkout --detach origin/main
    python3 -I scripts/fleet-cli-git-update.py update

If the state file still remembers the target as bad, clear it first:  the `bad`, `bad_reason`
and `checked_at` keys in ~/.local/share/fleet-cli/git-state.json.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

REMOTE = "https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git"
GIT = "/usr/bin/git"
GIT_TIMEOUT = 120
SMOKE_TIMEOUT = 20
# The same commands the release receiver smoke-tested:  each must start and exit as expected.
SMOKE_CASES = (
    ("simple_chat_version", "scripts/simple-chat", ["--version"], 0),
    ("simple_chat_mcp_help", "scripts/simple-chat", ["mcp", "--help"], 0),
    ("simple_notes_version", "scripts/simple-notes", ["--version"], 0),
    ("simple_board_help", "scripts/simple-board", ["--help"], 0),
    # A usage error exits 2 before any credential is read, with or without the host's seat helper (see the same case in
    # fleet-cli-release.py).  The earlier `whoami` case expected exit 4 and failed on a host that has the helper and a
    # bearer for the smoke seat.
    ("simple_board_usage_error", "scripts/simple-board", ["show"], 2),
    ("recall_help", "scripts/recall", ["--help"], 0),
    ("lane_help", "scripts/fleet_lanes/bin/lane", ["--help"], 0),
    ("fleet_mode_block", "scripts/fleet-mode", [], 0),
    # The updater runs from this checkout next time, and imports the release module for wrappers.
    ("updater_help", "scripts/fleet-cli-git-update.py", ["--help"], 0),
    ("release_help", "scripts/fleet-cli-release.py", ["--help"], 0),
)
BASH_SCRIPTS = frozenset({"scripts/fleet_lanes/bin/lane", "scripts/fleet-mode"})
PAUSE_NOTE = b"retired 2026-10-19:  this host runs the tools from git (fleet-cli-git-update.py)\n"


class Failed(Exception):
    """An update that did not happen:  the message is a short code, never a secret."""


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + "  " + message, flush=True)


def paths(home: Path) -> dict[str, Path]:
    base = home.absolute() / ".local/share/fleet-cli"
    return {
        "base": base,
        "repo": base / "repo",
        "state": base / "git-state.json",
        "lock": base / "update.lock",
    }


def git(repo: Path, *args: str, timeout: int = GIT_TIMEOUT) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            [GIT, "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Failed("git_timeout_" + args[0]) from None
    if result.returncode:
        raise Failed("git_failed_" + args[0])
    return result.stdout.strip()


def private_directory(path: Path) -> None:
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise Failed("unsafe_state_directory")
    path.chmod(0o700)


def load_state(path: Path) -> dict:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise Failed("unsafe_state_file") from None
    try:
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise Failed("unsafe_state_file")
            data = json.load(stream)
    except (OSError, ValueError):
        raise Failed("invalid_state_file") from None
    if not isinstance(data, dict):
        raise Failed("invalid_state_file")
    for key in ("commit", "previous", "bad"):
        value = data.get(key)
        if value is not None and (
            not isinstance(value, str)
            or not value
            or any(c not in "0123456789abcdef" for c in value)
            or len(value) not in (40, 64)
        ):
            raise Failed("invalid_state_file")
    return data


def save_state(path: Path, state: dict) -> None:
    private_directory(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".git-state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(json.dumps(state, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def smoke_settings(
    repo: Path,
) -> tuple[tuple[tuple[str, str, list[str], int], ...], frozenset[str], int]:
    """The smoke's own settings, taken from the checkout being tested.

    The running updater's settings are not the right ones:  when a release removes a CLI, the
    installed updater still asks for it, the target checkout no longer ships it, and the smoke
    fails on a file that is meant to be gone.  That deadlock held a host on an old commit for
    hours (the Oct 22, 2026 cutover from agent-sync to simple-chat).  The target's own settings
    are what must work, so read them from the target;  fall back to the running ones when the
    target has no updater to read.
    """
    target = repo / "scripts/fleet-cli-git-update.py"
    if target.exists():
        try:
            spec = importlib.util.spec_from_file_location("_fleet_cli_smoke_cases", target)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            cases = tuple(getattr(module, "SMOKE_CASES", ()))
            if cases:
                scripts = frozenset(getattr(module, "BASH_SCRIPTS", BASH_SCRIPTS))
                timeout = int(getattr(module, "SMOKE_TIMEOUT", SMOKE_TIMEOUT))
                return cases, scripts, timeout
        except Exception:  # a broken target falls back to the running settings, which still smoke
            pass
    return SMOKE_CASES, BASH_SCRIPTS, SMOKE_TIMEOUT


def smoke(repo: Path) -> None:
    """Every fleet command starts from this checkout, with an empty HOME and a minimal environment."""
    cases, bash_scripts, timeout = smoke_settings(repo)
    with tempfile.TemporaryDirectory(prefix="fleet-cli-smoke-") as home:
        env = {"HOME": home, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "AGENT_SEAT": "CODEX"}
        for case, script, args, expected in cases:
            target = repo / script
            if not target.exists():
                raise Failed("smoke_missing_" + case)
            argv = (
                ["/bin/bash", str(target), *args]
                if script in bash_scripts
                else [sys.executable, "-I", "-B", str(target), *args]
            )
            try:
                result = subprocess.run(
                    argv, env=env, cwd=home, capture_output=True, timeout=timeout, check=False
                )
            except subprocess.TimeoutExpired:
                raise Failed("smoke_timeout_" + case) from None
            if result.returncode != expected:
                raise Failed("smoke_exit_mismatch_" + case)


def release_module(repo: Path):
    """fleet-cli-release.py from the checkout:  its wrapper writer, so wrappers match what the receiver wrote."""
    spec = importlib.util.spec_from_file_location(
        "_fleet_cli_release", repo / "scripts/fleet-cli-release.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_new_wrappers(home: Path, repo: Path) -> list[str]:
    """Write only the wrappers this host lacks;  an existing file or link is never touched."""
    release = release_module(repo)
    written = []
    for path, (data, mode) in release.new_wrapper_plan(home).items():
        release.atomic_write(path, data, mode)
        written.append(path.name)
    return written


def point_current(base: Path) -> None:
    temporary = base / (".pointer-" + uuid.uuid4().hex)
    temporary.symlink_to("repo")
    try:
        os.replace(temporary, base / "current")
    finally:
        temporary.unlink(missing_ok=True)


def move_to(repo: Path, target: str, state: dict, state_path: Path) -> dict:
    """Check out `target`, smoke-test it, and keep it, or return to the last validated commit and remember
    `target` as bad.  Only a commit in state["commit"] counts as validated:  a checkout that HEAD reached
    without a finished smoke test (a killed run) is never treated as good."""
    head = git(repo, "rev-parse", "HEAD")
    good = state.get("commit")
    if good == target and head == target:
        return {"status": "unchanged", "commit": target}
    if git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise Failed("checkout_has_tracked_changes")
    git(repo, "checkout", "-q", "--detach", target)
    try:
        smoke(repo)
    except Failed as exc:
        if good:
            git(repo, "checkout", "-q", "--detach", good)
        state.update(bad=target, bad_reason=str(exc), checked_at=int(time.time()))
        save_state(state_path, state)
        log(
            f"SMOKE-FAILED  {target[:7]} ({exc});  {'stayed on ' + good[:7] if good else 'no validated commit yet'}"
        )
        return {"status": "smoke_failed", "commit": good, "bad": target, "reason": str(exc)}
    now = int(time.time())
    state.update(
        commit=target, previous=good, bad=None, bad_reason=None, updated_at=now, checked_at=now
    )
    save_state(state_path, state)
    log(f"UPDATED  {(good or 'none')[:7]} -> {target[:7]}")
    return {"status": "updated", "commit": target, "previous": good}


def restore_validated(repo: Path, state: dict) -> None:
    """A run killed mid-smoke can leave HEAD on an unvalidated commit:  go back to the validated one first."""
    good = state.get("commit")
    if good and git(repo, "rev-parse", "HEAD") != good:
        git(repo, "checkout", "-q", "--detach", good)
        log(f"RESTORED  HEAD to the validated {good[:7]}")


def wrappers(home: Path, repo: Path, result: dict) -> dict:
    """Every run writes any wrapper the host still lacks, so a failed write is retried next time."""
    written = write_new_wrappers(home, repo)
    if written:
        result["wrappers_written"] = written
        log("WRAPPERS  " + ", ".join(written))
    return result


def locked(base: Path):
    private_directory(base)
    try:
        fd = os.open(base / "update.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise Failed("unsafe_lock_file") from None
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise Failed("unsafe_lock_file")
    handle = os.fdopen(fd, "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise Failed("update_locked") from None
    return handle


def setup(home: Path) -> dict:
    p = paths(home)
    with locked(p["base"]):
        if not (p["repo"] / ".git").exists():
            try:
                subprocess.run(
                    [GIT, "clone", "-q", "--no-checkout", "--", REMOTE, str(p["repo"])],
                    check=True,
                    timeout=600,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                raise Failed("clone_failed") from None
            git(p["repo"], "checkout", "-q", "--detach", "origin/main")
        git(p["repo"], "fetch", "-q", "origin", "main")
        state = load_state(p["state"])
        restore_validated(p["repo"], state)
        result = move_to(p["repo"], git(p["repo"], "rev-parse", "origin/main"), state, p["state"])
        if not load_state(p["state"]).get("commit"):
            raise Failed(
                "setup_smoke_failed"
            )  # nothing validated yet:  `current` is left as it was
        # The old receiver returns "paused" before touching `current`, so a Mac push cannot repoint it.
        (p["base"] / "paused").write_bytes(PAUSE_NOTE)
        point_current(p["base"])
        return wrappers(home, p["repo"], result)


def update(home: Path) -> dict:
    p = paths(home)
    if not (p["repo"] / ".git").exists():
        raise Failed("not_set_up")
    with locked(p["base"]):
        git(p["repo"], "fetch", "-q", "origin", "main")
        target = git(p["repo"], "rev-parse", "origin/main")
        state = load_state(p["state"])
        restore_validated(p["repo"], state)
        if target == state.get("bad"):
            state["checked_at"] = int(time.time())
            save_state(p["state"], state)
            return {
                "status": "skipped_bad",
                "commit": git(p["repo"], "rev-parse", "HEAD"),
                "bad": target,
            }
        return wrappers(home, p["repo"], move_to(p["repo"], target, state, p["state"]))


def rollback(home: Path) -> dict:
    p = paths(home)
    with locked(p["base"]):
        state = load_state(p["state"])
        previous, current = state.get("previous"), git(p["repo"], "rev-parse", "HEAD")
        if not previous or previous == current:
            raise Failed("no_previous_commit")
        git(p["repo"], "checkout", "-q", "--detach", previous)
        state.update(
            commit=previous,
            previous=None,
            bad=current,
            bad_reason="rolled_back",
            updated_at=int(time.time()),
        )
        save_state(p["state"], state)
        log(
            f"ROLLED-BACK  {current[:7]} -> {previous[:7]}  ({current[:7]} skipped until main moves)"
        )
        return {"status": "rolled_back", "commit": previous, "bad": current}


def status(home: Path) -> dict:
    p = paths(home)
    state = load_state(p["state"])
    live = git(p["repo"], "rev-parse", "HEAD") if (p["repo"] / ".git").exists() else None
    return {
        "mode": "git",
        "commit": live,
        "previous": state.get("previous"),
        "bad": state.get("bad"),
        "bad_reason": state.get("bad_reason"),
        "updated_at": state.get("updated_at"),
        "checked_at": state.get("checked_at"),
        "current_is_checkout": (p["base"] / "current").resolve() == p["repo"].resolve(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["setup", "update", "rollback", "status"])
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args(argv)
    try:
        result = {"setup": setup, "update": update, "rollback": rollback, "status": status}[
            args.command
        ](args.home)
    except Failed as exc:
        print(json.dumps({"status": "error", "code": str(exc)}))
        return 2
    print(json.dumps(result, sort_keys=True))
    # A commit that failed its smoke test is handled (the host stays on the validated one), but it exits 1
    # so the systemd unit shows as failed and the owner sees it.
    return 1 if result.get("status") == "smoke_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
