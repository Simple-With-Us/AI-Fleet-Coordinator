"""The seat a command acts as.

The seat is proven by `simple-board`, never by this process's environment:  the board CLI verifies the
admin-minted bearer at the launcher-bound SIMPLE_BOARD_BEARER_LOCATOR against ST-MCP and reports the
verified seat on its `whoami` seat line.  `--by` is only a check that the caller means the same seat
(FLEET-PROTOCOL Identity Rules).  `AGENT_SEAT` and `AGENT_LAUNCH_SEAT` are not consulted and are removed from
every child's environment, so no variable can pick an identity.  No locator, or any verification failure,
refuses (fail closed).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping

from . import f3
from .errors import Refusal

LOCATOR_ENV = "SIMPLE_BOARD_BEARER_LOCATOR"
_SEAT_LINE = re.compile(r"^seat:\s+([A-Z][A-Z0-9-]{0,31}) \(bearer-verified\)$", re.MULTILINE)
_IDENTITY_VARS = ("AGENT_SEAT", "AGENT_LAUNCH_SEAT")


def _only_owner_can_change(st: os.stat_result, me: int) -> bool:
    """Owned by this user or root, and not writable by group or others (a sticky world-writable folder such as
    /tmp is not safe to rename inside, but it is safe to stand under:  only a sticky folder's own entries are
    protected, so it passes only as an ancestor, see _chain_is_safe)."""
    return st.st_uid in (me, 0) and not st.st_mode & 0o022


def _dir_is_safe(path: str, me: int) -> bool:
    st = os.stat(path)
    if _only_owner_can_change(st, me):
        return True
    # a sticky folder only lets an entry's owner rename it:  fine to stand under when root or this user owns it
    return bool(st.st_mode & 0o1000) and st.st_uid in (me, 0)


def _walk_is_safe(path: str, me: int) -> str | None:
    """Resolve `path` one component at a time, and judge every folder the walk stands in and every folder that
    holds a symlink it follows, however many hops the chain has:  each such folder, and the final file, must be
    changeable only by this user or root.  Returns the resolved path, or None when any step is unsafe."""
    cur = os.sep
    if not _dir_is_safe(cur, me):
        return None
    todo = [part for part in path.split(os.sep) if part]
    hops = 0
    while todo:
        part = todo.pop(0)
        if part == ".":
            continue
        if part == "..":
            cur = os.path.dirname(cur) or os.sep
            continue
        nxt = os.path.join(cur, part)
        if os.path.islink(nxt):  # cur holds this link and has already passed the folder check
            hops += 1
            if hops > 40:
                return None
            target = os.readlink(nxt)
            if os.path.isabs(target):
                cur = os.sep
            todo = [t for t in target.split(os.sep) if t] + todo
            continue
        if todo or os.path.isdir(nxt):
            if not _dir_is_safe(nxt, me):
                return None
            cur = nxt
        else:  # the final file
            if not _only_owner_can_change(os.stat(nxt), me):
                return None
            cur = nxt
    return cur


def trusted_binary(name: str, path: str | None) -> str | None:
    """The first `name` in the absolute folders of `path` that only this user (or root) can have placed or
    replaced:  the file, every folder above it, and every folder above any symlink hop on the way are owned by
    this user or root and not group/other-writable.  The seat line a binary prints is only as good as the
    binary, so one anyone else could have written, renamed into place or swapped through a writable parent is
    never run to learn an identity.  The resolved absolute file is what runs.  The first match decides:  an
    unsafe one stops the search rather than falling through to a later safe one."""
    me = os.getuid()
    for folder in (path or os.defpath).split(os.pathsep):
        if not folder or not os.path.isabs(folder):
            continue
        candidate = os.path.join(folder, name)
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        try:
            return _walk_is_safe(os.path.abspath(candidate), me)
        except OSError:
            return None
    return None


# Where the managed `simple-board` entry lives, for the account this process runs as:  ~/.local/bin/simple-board
# (a symlink into the managed runtime on the Mac, a managed wrapper on the server).  It is NOT looked up on PATH
# and the caller's HOME or PATH never moves it:  the home is the account database's.  Tests in this process point
# MANAGED_BIN_DIR at their fake tools (an import-only seam;  no environment variable or option sets it).
MANAGED_BIN_DIR: str | None = None


def managed_bin_dir() -> str | None:
    if MANAGED_BIN_DIR:
        return MANAGED_BIN_DIR
    try:
        import pwd

        return os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".local", "bin")
    except (ImportError, KeyError, OSError):
        return None


def managed_board() -> str | None:
    """The managed simple-board's resolved path, or None when it is absent or unsafe to run."""
    folder = managed_bin_dir()
    return trusted_binary("simple-board", folder) if folder else None


def _probe(env: Mapping[str, str]) -> tuple[int, str]:
    binary = managed_board()
    if not binary:
        raise Refusal(
            "the managed simple-board (~/.local/bin/simple-board of this account) is missing, or it or its "
            "folder is writable by someone other than this user or root;  the seat is not read from "
            "any other simple-board"
        )
    try:
        done = subprocess.run(
            [binary, "whoami"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            stdin=subprocess.DEVNULL,
            env=child_environment(env),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Refusal("simple-board could not verify the seat (%s)" % type(exc).__name__) from None
    return done.returncode, done.stdout or ""


def verified_seat(
    by: str,
    environ: Mapping[str, str] | None = None,
    probe: Callable[[Mapping[str, str]], tuple[int, str]] | None = None,
) -> str:
    """The seat the bearer proves, which must equal `by`.  Raises Refusal."""
    env = os.environ if environ is None else environ
    if not (env.get(LOCATOR_ENV) or "").strip():
        raise Refusal(
            "no verified seat:  %s is not set.  The trusted launcher binds the run's bearer locator;  "
            "no environment variable can choose an identity" % LOCATOR_ENV
        )
    code, out = (probe or _probe)(env)
    match = _SEAT_LINE.search(out) if code == 0 else None
    if not match:
        raise Refusal("the seat bearer could not be verified (simple-board whoami exit %d)" % code)
    seat = f3.identity.valid_seat(match.group(1))
    if not seat or seat != match.group(1):
        raise Refusal("%r is not a canonical seat tag" % match.group(1)[:40])
    wanted = f3.identity.canonical_seat(by)
    if wanted != seat:
        raise Refusal(
            "--by %s does not match this session's verified seat %s.  --by is only a check, never the "
            "source of identity" % (wanted[:40], seat)
        )
    return seat


def child_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with every identity variable removed (the bearer locator stays).

    Python search-path variables are stripped too:  the managed board binary
    must not inherit a caller-controlled PYTHONPATH/PYTHONHOME that could
    shadow its imports and forge a bearer-verified seat line.
    """
    env = dict(os.environ if environ is None else environ)
    for name in _IDENTITY_VARS:
        env.pop(name, None)
    for name in (
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSAFEPATH",
        "PYTHONNOUSERSITE",
        "PYTHONUSERBASE",
        "PYTHONSTARTUP",
    ):
        env.pop(name, None)
    return env
