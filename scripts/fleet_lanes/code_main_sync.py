#!/usr/bin/env python3
"""Fast-forward clean clones in the Code root to origin's main branch.  Never touches dirty work.

The tracked source of the server's `code-main-sync` job, which until AFC-618 lived only on the host.  The Mac's equivalent
is the pm2 job `code-main-keeper` (`~/apps/code-main-keeper.sh`), which is left as it is.

    python3 code_main_sync.py [--dry-run]

Two faults kept the old job failing on every run for days:

  * A stale `.git/index.lock`, left when a process died, makes `git merge --ff-only` exit 1, and `git status`, which takes
    no lock, still reads clean.  So a clean clone failed its fast-forward with nothing to explain it.  A lock older than
    STALE_LOCK_S that no process holds is now cleared.  One that is younger or held stays, and that clone is reported
    as failed.  Removing the lock file loses nothing: git writes the new index there and renames it only on success.
  * git's stderr was thrown away, so `fast-forward exit=1` was all the log said.  It now carries git's first line.

A folder that is not a repo (such as `Docs`) is passed over without a skip count, and a clone with an in-date row in
placement-exceptions.json (such as a service-pinned clone) is left alone with its reason logged.  Those rows use class
`service`.  Roots come from FLEET_CODE_ROOT or the home folder, so no host path is written here.  Exit 1 only if a
clone that should have moved did not.  Python 3.9 safe.  Tests: fleet_lanes/tests/test_code_main_sync.py.
"""
from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

GIT = os.environ.get("FLEET_GIT_SHIM_REAL") or "/usr/bin/git"
TIMEOUT = 120
STALE_LOCK_S = 3600
FLEET_OWNERS = {"simple-with-us", "jaywedgeworth22"}


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {message}", flush=True)


def _first_line(text: str) -> str:
    """One line of git's stderr, with any URL credential gone."""
    for line in text.splitlines():
        line = line.strip()
        if line:
            words = [w.split("@", 1)[-1] if "://" in w and "@" in w else w for w in line.split()]
            return " ".join(words)[:200]
    return ""


def git_full(repo: Path, *args: str, timeout: int = TIMEOUT) -> tuple[int, str, str]:
    try:
        result = subprocess.run([GIT, "-C", str(repo), *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, errors="replace", timeout=timeout, check=False)
        return result.returncode, result.stdout.strip(), result.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timed out"
    except OSError as exc:
        return 127, "", str(exc)


def git(repo: Path, *args: str, timeout: int = TIMEOUT) -> tuple[int, str]:
    code, out, _ = git_full(repo, *args, timeout=timeout)
    return code, out


def remote_is_fleet_github(remote: str) -> bool:
    if remote.startswith("git@"):
        host, separator, path = remote[4:].partition(":")
        if not separator:
            return False
    elif "://" in remote:
        parsed = urlsplit(remote)
        host = parsed.hostname or ""
        path = parsed.path
    else:
        return False
    parts = path.strip("/").split("/")
    return len(parts) >= 2 and host.casefold() == "github.com" and parts[0].casefold() in FLEET_OWNERS


def resolve_github_token(env: Mapping[str, str], home: Path) -> str:
    """Name the token git's credential helper will use, fetching one through fleet-secrets if none is set.  Never logged."""
    for name in ("GITHUB_ADMIN_PAT", "GITHUB_TOKEN"):
        if env.get(name):
            return name
    tool = home / ".local" / "bin" / "fleet-secrets"
    if not tool.is_file():
        return ""
    try:
        result = subprocess.run([str(tool), "get", "GITHUB_ADMIN_PAT"], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, errors="replace", timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    token = result.stdout.strip()
    if result.returncode == 0 and token:
        os.environ["GITHUB_ADMIN_PAT"] = token
        return "GITHUB_ADMIN_PAT"
    return ""


def lock_is_held(lock: Path) -> bool | None:
    """Whether a process has the lock open.  None means it cannot be told, which never justifies removing it."""
    for tool in ("/usr/bin/lsof", "/usr/sbin/lsof", "/bin/lsof"):
        if not os.access(tool, os.X_OK):
            continue
        try:
            res = subprocess.run([tool, "-t", "--", str(lock)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 text=True, timeout=20, check=False)
        except (OSError, subprocess.SubprocessError):
            return None
        return bool(res.stdout.strip())
    return None


def clear_stale_index_lock(repo: Path, now: float, dry_run: bool) -> str:
    """`none`, `cleared`, `would-clear`, or `kept` for a lock this job will not remove."""
    code, git_dir = git(repo, "rev-parse", "--absolute-git-dir")
    if code != 0 or not git_dir:
        return "none"
    lock = Path(git_dir) / "index.lock"
    try:
        age = now - lock.lstat().st_mtime
    except OSError:
        return "none"
    if age < STALE_LOCK_S or lock_is_held(lock) is not False:
        log(f"keep {repo.name}: index.lock is {int(age)}s old and young or possibly held")
        return "kept"
    if dry_run:
        log(f"dry-run {repo.name}: would clear a stale index.lock, {int(age / 3600)}h old")
        return "would-clear"
    try:
        lock.unlink()
    except OSError as exc:
        log(f"keep {repo.name}: index.lock cannot be removed ({exc.strerror})")
        return "kept"
    log(f"cleared {repo.name}: stale index.lock, {int(age / 3600)}h old, held by no process")
    return "cleared"


def checkout_state(repo: Path) -> tuple[str, str, str] | None:
    code, branch = git(repo, "branch", "--show-current")
    if code != 0:
        return None
    code, head = git(repo, "rev-parse", "HEAD")
    if code != 0:
        return None
    code, upstream = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if code != 0:
        upstream = ""
    return branch, head, upstream


def _exception(repo: Path, env: Mapping[str, str], home: Path, code_root: Path):
    """The in-date exception row for this clone, or None."""
    from fleet_lanes import placement as P  # A missing package fails this clone rather than moving a pinned one.

    class _Roots:
        pass

    roots = _Roots()
    roots.home, roots.code_root = home, code_root
    roots.apps_root = Path(env.get("FLEET_APPS_ROOT") or home / "apps")
    roots.lanes_root = Path(env.get("FLEET_LANES_ROOT") or Path(roots.apps_root) / "lanes")
    roots.case_insensitive = sys.platform == "darwin"
    hit = P.find_exception((str(repo),), roots, P.load_exceptions(env=env), host=P.host_kind(env))
    return hit.row if hit is not None and not hit.expired else None


def process_repo(repo: Path, dry_run: bool, counters: dict[str, int], *, env: Mapping[str, str], home: Path,
                 code_root: Path) -> None:
    if repo.is_symlink():
        counters["skipped"] += 1
        log(f"skip {repo.name}: symlink")
        return
    code, top = git(repo, "rev-parse", "--show-toplevel")
    if code != 0:
        log(f"note {repo.name}: not a Git checkout")
        return
    if Path(top).resolve() != repo.resolve():
        counters["skipped"] += 1
        log(f"skip {repo.name}: Git top-level does not match Code folder")
        return
    row = _exception(repo, env, home, code_root)
    if row is not None:
        counters["skipped"] += 1
        log(f"skip {repo.name}: exception {row.id}")
        return
    code, remote = git(repo, "remote", "get-url", "origin")
    if code != 0 or not remote_is_fleet_github(remote):
        counters["skipped"] += 1
        log(f"skip {repo.name}: origin is not a fleet GitHub repo")
        return
    state = checkout_state(repo)
    if state is None:
        counters["skipped"] += 1
        log(f"skip {repo.name}: cannot read branch state")
        return
    branch, head, upstream = state
    if branch != "main" or upstream != "origin/main":
        counters["skipped"] += 1
        log(f"skip {repo.name}: expected main tracking origin/main, on {branch or 'a detached HEAD'}")
        return
    code, dirty = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if code != 0:
        counters["failed"] += 1
        log(f"fail {repo.name}: git status exit={code}")
        return
    if dirty:
        counters["skipped"] += 1
        log(f"skip {repo.name}: working tree is dirty")
        return
    if dry_run:
        code, remote_head = git(repo, "rev-parse", "--verify", "refs/remotes/origin/main")
        if code != 0:
            counters["skipped"] += 1
            log(f"dry-run {repo.name}: origin/main ref is missing; no network fetch performed")
        elif head == remote_head:
            counters["current"] += 1
            log(f"dry-run {repo.name}: main matches local origin/main ref")
        elif git(repo, "merge-base", "--is-ancestor", head, remote_head)[0] == 0:
            clear_stale_index_lock(repo, time.time(), True)
            counters["would_update"] += 1
            log(f"dry-run {repo.name}: local origin/main is ahead; no network fetch performed")
        else:
            counters["skipped"] += 1
            log(f"dry-run {repo.name}: main is ahead of or diverged from local origin/main")
        return
    code, _, err = git_full(repo, "fetch", "--quiet", "--no-tags", "origin", "refs/heads/main:refs/remotes/origin/main")
    if code != 0:
        counters["failed"] += 1
        log(f"fail {repo.name}: fetch exit={code}: {_first_line(err)}")
        return
    after = checkout_state(repo)
    code, dirty_after = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if after is None or after[0] != "main" or after[1] != head or after[2] != "origin/main" or code != 0 or dirty_after:
        counters["skipped"] += 1
        log(f"skip {repo.name}: checkout changed during fetch")
        return
    code, remote_head = git(repo, "rev-parse", "--verify", "refs/remotes/origin/main")
    if code != 0:
        counters["failed"] += 1
        log(f"fail {repo.name}: origin/main ref is missing after fetch")
        return
    if head == remote_head:
        counters["current"] += 1
        log(f"current {repo.name}: already at origin/main")
        return
    if git(repo, "merge-base", "--is-ancestor", head, remote_head)[0] != 0:
        counters["skipped"] += 1
        log(f"skip {repo.name}: local main is ahead of or diverged from origin/main")
        return
    clear_stale_index_lock(repo, time.time(), False)
    code, _, err = git_full(repo, "-c", "core.hooksPath=/dev/null", "merge", "--ff-only", "--quiet", "origin/main")
    if code != 0:
        counters["failed"] += 1
        log(f"fail {repo.name}: fast-forward exit={code}: {_first_line(err)}")
        return
    final = checkout_state(repo)
    code, final_dirty = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if final is None or final[0] != "main" or final[2] != "origin/main" or final[1] != remote_head or code != 0 or final_dirty:
        counters["failed"] += 1
        log(f"fail {repo.name}: post-update verification failed")
        return
    counters["updated"] += 1
    log(f"updated {repo.name}: {head[:12]} -> {remote_head[:12]}")


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    parser = argparse.ArgumentParser(description="Fetch and fast-forward clean clones in the Code root to origin/main.")
    parser.add_argument("--dry-run", action="store_true", help="Inspect local refs only; fetch and change nothing.")
    args = parser.parse_args(argv)
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    root = Path(env.get("FLEET_CODE_ROOT") or home / "Code")
    if not root.is_dir():
        log(f"fail: code root {root.name} does not exist")
        return 1
    state = Path(env.get("FLEET_LANE_GUARD_STATE_DIR") or home / ".local" / "state")
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_fd = os.open(state / "code-main-sync.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("skip: another code-main-sync run is active")
        os.close(lock_fd)
        return 0
    counters = {"updated": 0, "current": 0, "would_update": 0, "skipped": 0, "failed": 0}
    token_source = "" if args.dry_run else resolve_github_token(env, home)
    log(f"start: {'dry-run local refs only' if args.dry_run else 'fetch and fast-forward'}")
    log(f"github token: {token_source or 'none (public repos only)'}")
    for repo in sorted(root.iterdir(), key=lambda path: path.name.casefold()):
        if repo.name.startswith(".") or not repo.is_dir():
            continue
        try:
            process_repo(repo, args.dry_run, counters, env=env, home=home, code_root=root)
        except Exception as exc:  # noqa: BLE001 - one clone must not stop the others
            counters["failed"] += 1
            log(f"fail {repo.name}: {type(exc).__name__}")
    log("summary: " + " ".join(f"{key}={value}" for key, value in counters.items()))
    return 1 if counters["failed"] else 0


if __name__ == "__main__":
    here = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    if here not in sys.path:
        sys.path.insert(0, here)
    sys.exit(main())
