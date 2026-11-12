"""Whether a git command would write into a ~/Code mirror clone (AFC-618, the mirror rule).

`~/Code/<Repo>` is a pure mirror of origin's main:  the one job that moves it (code-main-keeper) only fast-forwards a clean clone, and
all work happens in a lane (`~/apps/lane new <app> <slug>`).  A commit or a stash made there is work the mirror then has to step
around, and a daemon that once reset these trees destroyed it.  This module answers one question for the git shim:

    judge(args, cwd, env, real_git)  ->  Hit | None

A Hit means "this argv is a `commit` or a `stash` (anything but `list` and `show`) whose repository is a primary clone sitting directly
under the Code root".  The mode file decides what happens next, in the shim:  `mirror detect` (the default) warns on stderr and runs
git, `mirror block` refuses, `mirror off` and `off` say nothing.  There is no git hook for stash, and a `core.hooksPath` hook would
shadow every repo's own hooks, so the shim, which every `git` found on PATH passes through, covers both commands the same way.

Gaps, by design:  a git that is not found through PATH (an absolute /usr/bin/git, an IDE's bundled git) never reaches the shim;  a `!`
shell alias is not read here;  `--git-dir` with no `--work-tree` is not followed.  A folder that is a linked worktree (its `.git` is a
file) is not a mirror and is left to the placement rules.

Python 3.9 safe, stdlib only.  Tests: fleet_lanes/tests/test_mirror_write.py.
"""
from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

WRITE_WORDS = ("commit", "stash")
STASH_READS = frozenset({"list", "show"})
CLASS_MIRROR = "mirror"
RULE_MIRROR_WRITE = "mirror-write"
LANE_COMMAND = "~/apps/lane new <app> <slug>"
MESSAGE = f"~/Code is a mirror; do this work in a lane: {LANE_COMMAND}"
_GLOBAL_VALUE = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--exec-path",
                           "--attr-source", "--config-env", "--shallow-file"})


@dataclass(frozen=True)
class Hit:
    verb: str  # commit or stash
    repo: str  # the clone's folder name under the Code root


def split_global(args: Sequence[str]) -> tuple[list[str], list[str]]:
    """(global options, the subcommand and its arguments)."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in _GLOBAL_VALUE:
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            break
    return list(args[:i]), list(args[i:])


def write_verb(rest: Sequence[str]) -> str | None:
    """`commit` or `stash` when this subcommand changes the repository, else None.  `stash list`, `stash show` and `commit --dry-run`
    write nothing."""
    if not rest or rest[0] not in WRITE_WORDS:
        return None
    verb, tail = rest[0], list(rest[1:])
    if verb == "stash":
        sub = next((a for a in tail if not a.startswith("-")), "")
        return None if sub in STASH_READS else verb
    return None if "--dry-run" in tail else verb


def code_root(env: Mapping[str, str]) -> str:
    home = env.get("HOME") or os.path.expanduser("~")
    return os.path.realpath((env.get("FLEET_CODE_ROOT") or "").strip() or os.path.join(home, "Code"))


def _effective_dir(global_opts: Sequence[str], cwd: str) -> str | None:
    """The folder git starts in after every -C, or None when --git-dir or --work-tree moves the repo somewhere this cannot follow."""
    here = cwd
    i = 0
    while i < len(global_opts):
        a = global_opts[i]
        if a == "-C" and i + 1 < len(global_opts):
            here = os.path.join(here, global_opts[i + 1])
            i += 2
            continue
        if a in ("--git-dir", "--work-tree") or a.startswith(("--git-dir=", "--work-tree=")):
            return None
        i += 2 if a in _GLOBAL_VALUE else 1
    return here


def _toplevel(real_git: str, folder: str, env: Mapping[str, str]) -> str | None:
    clean = {k: v for k, v in env.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    try:
        res = subprocess.run([real_git, "-C", folder, "rev-parse", "--show-toplevel"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, timeout=1.5, check=False, env=clean)
    except (OSError, subprocess.SubprocessError):
        return None
    top = res.stdout.strip()
    return os.path.realpath(top) if res.returncode == 0 and top else None


def judge(args: Sequence[str], cwd: str, env: Mapping[str, str], real_git: str) -> Hit | None:
    """The Hit for this argv, or None when it is not a write or not in a mirror."""
    global_opts, rest = split_global(args)
    verb = write_verb(rest)
    if verb is None:
        return None
    if (env.get("GIT_DIR") or "").strip() or (env.get("GIT_WORK_TREE") or "").strip():
        return None
    folder = _effective_dir(global_opts, cwd)
    if folder is None or not os.path.isdir(folder):
        return None
    top = _toplevel(real_git, folder, env)
    if top is None:
        return None
    root = code_root(env)
    if os.path.dirname(top) != root or not os.path.isdir(os.path.join(top, ".git")):
        return None
    return Hit(verb, os.path.basename(top))


def reason(hit: Hit, blocked: bool) -> str:
    lead = f"Blocked: `git {hit.verb}` in the mirror ~/Code/{hit.repo}." if blocked else f"Warning: `git {hit.verb}` in the mirror ~/Code/{hit.repo}."
    return f"{lead}\u00a0 {MESSAGE}"
