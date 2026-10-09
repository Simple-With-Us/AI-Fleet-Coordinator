"""Lane CLI: create a lane (or a read-only PR review checkout) in the right place, the easy way.

    cd scripts && python3 -m fleet_lanes.lane new <app> <slug> [--base REF] [--purpose TEXT]
                                              [--board ID] [--reuse-branch] [--dry-run] [--json]
    cd scripts && python3 -m fleet_lanes.lane new <app> --review --pr N [--dry-run] [--json]
    cd scripts && python3 -m fleet_lanes.lane path <app> <slug>
    cd scripts && python3 -m fleet_lanes.lane path <app> --review --pr N
    cd scripts && python3 -m fleet_lanes.lane ls|doctor [doctor options]

Layout v2 (owner decision 2026-10-09).  A lane is ~/apps/lanes/<Repo>/<seat>-<slug> and a read-only PR
check is ~/apps/lanes/<Repo>/review-pr-<n>, where <Repo> is the repo's folder name under ~/Code exactly
as spelled there (AI-Fleet-Coordinator, Congress.Trade, congress-trading-shared).  <app> may be that
name, the old worktree prefix (fleet, trading) or the acronym.  When another seat already holds
review-pr-<n>, a second seat gets review-pr-<n>-<seat>.  A lane that still sits in an old prefix folder
(lanes/fleet/claude-x) is found and printed, not duplicated, until the migration moves it.

`bin/lane` in this package is the shell shim that runs this module with `python3 -I` from its own
checkout, so a fleet_lanes package in the caller's working directory never shadows it.

What `lane new` does, in order: resolve the app from the registry (repo name, code dir, worktree
prefix or acronym, any case), take the seat ONLY from the AGENT_SEAT environment variable (never
inferred, and only a seat that owns its worktreeSuffix: GROK-BOT shares CURSOR's `cursor` and is
refused), compute the lane path and branch from the layout rules, check that the path is a
conforming spot in the map, then ask the integration tree (`~/Code/<codeDir>`) for a linked
worktree.  Every refusal happens before anything is written.  Then, holding a per-repo lock
(`<git common dir>/fleet-lane-new.flock`, see `_repo_lock`), it checks the path again, fetches the
base branch, resolves `refs/remotes/origin/<base>` (the full ref, so a local branch or tag named
origin/main cannot stand in for it), refuses a branch that already exists, runs `git worktree add
-b <branch> <path> <base sha>`, confirms from `git worktree list` that the lane is really there on
that branch and sha, and writes a manifest (`lane.json`) inside the new worktree's private git dir,
so `git status` in the lane stays clean.  The new branch has no upstream; the first push sets it.
The lane path alone goes to stdout (`cd "$(lane new app slug)"` works) and every human message
goes to stderr; `--json` prints the manifest plus the path on stdout instead.

Safety invariants, all covered by tests:
  * Every git or gh call goes through ONE runner (`Runner`) whose allowlist is `check_allowed`:
    git fetch --no-tags origin <ref>; git worktree add (three exact shapes, destination inside the
    lanes root); git worktree list --porcelain; git rev-parse (a few shapes); git show-ref --verify
    --quiet; git for-each-ref --format=%(refname); git config --get remote.origin.url; and
    gh pr view <n> --repo <owner/repo> --json <fields>.  Anything else raises CommandRefused
    before a process exists.  There is no remove, prune, move, reset, checkout, push or force.
  * The tool never removes, moves or modifies an existing worktree and never touches another lane.
    An existing path is either the very lane that was asked for, by the same seat (idempotent:
    print it, exit 0), or an error.  A lane whose manifest names another seat is refused.
  * Two runs for one repo never overlap: the second waits for the first's lock (at most LOCK_WAIT
    seconds, then exit 69) and then finds the lane already there.  A lane that is not where git
    said it put it is exit 69, never a printed path.
  * It writes only under the lanes root (the apps root in flat mode) and inside the target repo's
    git directory (fetch, the worktree admin entry, `lane.json` and the lock file, which stays).
  * `--dry-run` makes no change at all: no fetch, no directory, no worktree, no manifest, no lock.
  * It never creates `.janitor-keep`.  It never pushes.

Who gets a lane.  The app must have an integration tree under ~/Code and a row in fleet-apps.json
(an app without one is refused, tree first), the seat must be a live registry tag in AGENT_SEAT, and
the parent of the lanes root (~/apps) must already exist.  A new lane's branch is the seat's first
registry branch prefix plus the slug.

Limits worth knowing.  "The branch exists on origin" is checked by fetching that one branch name
(the only network probe the allowlist offers), not by `ls-remote`.  Repo hooks such as
post-checkout run during `git worktree add`, exactly as they would by hand.  The lock serializes
lane runs only: a fetch can still fail on a ref lock if some other process is fetching into the
same integration tree at that moment (exit 69; run lane again).  `--dry-run` for a review checkout
skips the gh lookup, so it shows no head.

Exit codes: 0 ok, 64 usage or refusal, 69 an external tool (git or gh) failed, 70 an internal
error.  `ls` and `doctor` return the doctor's own codes (0, 2 for --strict hits, 64).

Python 3.9 safe (macOS /usr/bin/python3): no match statements, no runtime `X | Y`.

Tests: fleet_lanes/tests/test_lane.py (real git, a local bare repo as origin, a fake home).
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import errno
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from . import doctor as D
from . import guard as G
from . import layout as L

__all__ = [
    "EXIT_OK", "EXIT_USAGE", "EXIT_TOOL", "EXIT_INTERNAL", "SCHEMA", "MANIFEST_NAME", "REVIEW_DAYS",
    "ENV_SEAT", "LOCK_NAME", "LOCK_WAIT", "Refusal", "ToolFailure", "CommandRefused", "valid_ref_name",
    "check_allowed", "Runner", "resolve_app", "resolve_seat", "branch_for", "parse_base", "main",
]

EXIT_OK = 0
EXIT_USAGE = 64      # usage error or a refusal (EX_USAGE)
EXIT_TOOL = 69       # git or gh failed (EX_UNAVAILABLE)
EXIT_INTERNAL = 70   # a bug in lane itself (EX_SOFTWARE)

SCHEMA = 1
MANIFEST_NAME = "lane.json"
REVIEW_DAYS = 7
ENV_SEAT = "AGENT_SEAT"
TOOL_NAME = "lane new"
DEFAULT_BASE_BRANCH = "main"
PURPOSE_MAX = 300
# The per-repo lock that serializes `lane new` runs, in the integration tree's git common dir.  Not
# a `*.lock` name: tools that sweep stale git lock files would delete it while it is held, and two
# runs would then lock two different files.  The file is never removed (unlink plus flock races).
LOCK_NAME = "fleet-lane-new.flock"
# Seconds to wait for another run's lock: longer than one holder's fetch plus worktree add caps.
LOCK_WAIT = 1300.0
_LOCK_POLL = 0.2

ExecFn = Callable[[Sequence[str], "str | None", Mapping[str, str], float], D.CmdResult]


class Refusal(Exception):
    """The request cannot be honoured as asked: exit 64."""


class ToolFailure(Exception):
    """git or gh (or the disk) failed: exit 69."""


class CommandRefused(ValueError):
    """The command is not on the allowlist, so no process was started."""


# --------------------------------------------------------------------------- the one runner

# Seconds.  fetch and worktree add can legitimately take minutes on a big repo, and lane never
# cleans up after a killed process, so these caps are generous.  Everything else is local and quick.
_CAPS = {"fetch": 300.0, "worktree-add": 900.0, "git": 30.0, "gh": 60.0}

# Variables that would point git at some other repository, index or object store.
_STRIP_ENV = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_PREFIX",
)
_FORCED_ENV = {
    "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_PAGER": "cat", "GH_PROMPT_DISABLED": "1",
    "GH_PAGER": "cat", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1",
    "GH_NO_EXTENSION_UPDATE_NOTIFIER": "1",
}

_REF_CHARS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_REV = re.compile(r"^(?:[0-9a-f]{40}|[A-Za-z0-9][A-Za-z0-9._/-]*)(?:\^\{commit\})?$")
_GH_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_GH_FIELDS = frozenset({"headRefOid", "headRefName", "state", "isCrossRepository"})
_REVPARSE_SOLO = frozenset({"--git-dir", "--absolute-git-dir", "--git-common-dir", "--show-toplevel"})


def valid_ref_name(ref: object) -> bool:
    """A plain branch or ref name: no leading dash, no `:`, `+`, `~`, `^`, `*`, `?`, `[`, space,
    backslash or control character, no `..`, `//`, trailing `/` or `.`, no `.lock` or dot-leading
    component.  Stricter than git, never looser."""
    if not isinstance(ref, str) or not 1 <= len(ref) <= 200 or not _REF_CHARS.match(ref):
        return False
    if ".." in ref or "//" in ref or ref.endswith(("/", ".")):
        return False
    return not any(c.startswith(".") or c.endswith(".lock") for c in ref.split("/"))


def _under_dir(path: str, root: str, fold: Callable[[str], str]) -> bool:
    p, r = fold(os.path.realpath(path)), fold(os.path.realpath(root))
    return p != r and p.startswith(r.rstrip(os.sep) + os.sep)


def _dest_ok(path: str, write_root: "str | None", fold: Callable[[str], str]) -> bool:
    """A worktree destination: absolute, not an option, inside the write root (and not the root)."""
    if not path or path.startswith("-") or "\x00" in path or "\n" in path or not os.path.isabs(path):
        return False
    return write_root is not None and _under_dir(path, write_root, fold)


def _worktree_add_ok(a: Sequence[str], write_root: "str | None", fold: Callable[[str], str]) -> bool:
    """Exactly three shapes:  -b BRANCH PATH SHA | PATH BRANCH | --detach PATH SHA."""
    if len(a) == 4 and a[0] == "-b":
        return valid_ref_name(a[1]) and _dest_ok(a[2], write_root, fold) and bool(_SHA.match(a[3]))
    if len(a) == 3 and a[0] == "--detach":
        return _dest_ok(a[1], write_root, fold) and bool(_SHA.match(a[2]))
    if len(a) == 2 and not a[0].startswith("-"):
        return _dest_ok(a[0], write_root, fold) and valid_ref_name(a[1])
    return False


def _under_origin_refs(name: str) -> bool:
    return (name.startswith("refs/heads/") or name.startswith("refs/remotes/origin/")) and valid_ref_name(name)


def _git_allowed(args: Sequence[str], write_root: "str | None", fold: Callable[[str], str]) -> "str | None":
    """The timeout key for an allowed git command, else None."""
    if not args:
        return None
    sub, rest = args[0], list(args[1:])
    if sub == "fetch":
        ok = len(rest) == 3 and rest[0] == "--no-tags" and rest[1] == "origin" and valid_ref_name(rest[2])
        return "fetch" if ok else None
    if sub == "worktree":
        if rest == ["list", "--porcelain"]:
            return "git"
        if rest[:1] == ["add"] and _worktree_add_ok(rest[1:], write_root, fold):
            return "worktree-add"
        return None
    if sub == "rev-parse":
        if len(rest) == 1 and rest[0] in _REVPARSE_SOLO:
            return "git"
        if rest[:1] == ["--verify"]:
            tail = rest[1:]
            if tail[:1] in (["-q"], ["--quiet"]):
                tail = tail[1:]
            if len(tail) == 1 and _REV.match(tail[0]) and ".." not in tail[0] and "//" not in tail[0]:
                return "git"
        return None
    if sub == "show-ref":
        ok = len(rest) == 3 and rest[:2] == ["--verify", "--quiet"] and _under_origin_refs(rest[2])
        return "git" if ok else None
    if sub == "for-each-ref":
        ok = (len(rest) >= 2 and rest[0] == "--format=%(refname)"
              and all(_under_origin_refs(p) for p in rest[1:]))
        return "git" if ok else None
    if sub == "config":
        return "git" if rest == ["--get", "remote.origin.url"] else None
    return None


def _gh_allowed(args: Sequence[str]) -> bool:
    """gh pr view N --repo OWNER/REPO --json FIELD[,FIELD...]  (the two flags in either order)."""
    if len(args) != 7 or list(args[:2]) != ["pr", "view"]:
        return False
    if not (args[2].isdigit() and args[2].isascii() and 1 <= len(args[2]) <= 9 and int(args[2]) > 0):
        return False
    pairs = {args[3]: args[4], args[5]: args[6]}
    if set(pairs) != {"--repo", "--json"}:
        return False
    fields = pairs["--json"].split(",")
    return bool(_GH_REPO.match(pairs["--repo"])) and all(f in _GH_FIELDS for f in fields) and len(set(fields)) == len(fields)


def check_allowed(argv: Sequence[str], *, write_root: "str | os.PathLike[str] | None" = None,
                  fold: "Callable[[str], str] | None" = None) -> float:
    """Return the timeout cap (seconds) when `argv` is an allowlisted command, else raise
    CommandRefused.  `argv[0]` must be the bare name `git` or `gh`.  A `worktree add` is allowed
    only with a destination strictly inside `write_root`, and is refused when no root is given."""
    if not argv or not all(isinstance(a, str) for a in argv) or any("\x00" in a for a in argv):
        raise CommandRefused("empty, non-string or NUL-bearing command")
    fold = fold or (lambda s: s)
    root = os.fspath(write_root) if write_root is not None else None
    key: "str | None" = None
    if argv[0] == "git":
        key = _git_allowed(argv[1:], root, fold)
    elif argv[0] == "gh":
        key = "gh" if _gh_allowed(argv[1:]) else None
    if key is None:
        raise CommandRefused("not on the lane allowlist: " + " ".join(argv[:5]))
    return _CAPS[key]


def _child_env(base: Mapping[str, str]) -> dict:
    env = {k: v for k, v in base.items() if k not in _STRIP_ENV}
    env.update(_FORCED_ENV)
    return env


def _kill_group(proc: "subprocess.Popen") -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # start_new_session makes the pgid equal the pid
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def default_exec(argv: Sequence[str], cwd: "str | None", env: Mapping[str, str], timeout: float) -> D.CmdResult:
    """Start the process.  Only `Runner` calls this, and only after `check_allowed`."""
    try:
        proc = subprocess.Popen(
            list(argv), cwd=cwd, env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
    except OSError as exc:
        return D.CmdResult(None, error=f"{type(exc).__name__}: {exc}")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - already dead; reaping is best effort
            pass
        return D.CmdResult(None, timed_out=True)
    return D.CmdResult(proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"))


class Runner:
    """The one place a git or gh command starts.

    `check_allowed` runs first, so a refused command never reaches `exec_fn` (tests swap `exec_fn`
    to fake gh and to record the calls).  git gets `--no-optional-locks -c core.fsmonitor=false`
    added here, and the child environment is built from the `env` passed in with the GIT_DIR family
    removed, so an inherited variable cannot redirect a command at another repository.
    """

    def __init__(self, env: Mapping[str, str], *, write_root: "str | os.PathLike[str] | None" = None,
                 fold: "Callable[[str], str] | None" = None, exec_fn: "ExecFn | None" = None) -> None:
        self._env = _child_env(env)
        self._write_root = os.fspath(write_root) if write_root is not None else None
        self._fold = fold or (lambda s: s)
        self._exec = exec_fn or default_exec

    def __call__(self, argv: Sequence[str], cwd: "str | os.PathLike[str] | None" = None) -> D.CmdResult:
        cap = check_allowed(argv, write_root=self._write_root, fold=self._fold)
        full = list(argv)
        if full[0] == "git":
            full = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *full[1:]]
        return self._exec(full, os.fspath(cwd) if cwd is not None else None, self._env, cap)


# --------------------------------------------------------------------------- resolving inputs

def _loose(text: str) -> str:
    return re.sub(r"[.\-]", "", text.lower())


def _app_keys(app: L.App) -> "set[str]":
    return {k.lower() for k in (app.name, app.integration_dir_name, app.lane_dir, app.prefix, app.acronym) if k}


def _app_line(app: L.App) -> str:
    bits = [f"folder {app.lane_dir}", f"prefix {app.prefix}"]
    if app.acronym:
        bits.append(f"acronym {app.acronym}")
    if not app.registered:
        bits.append("not in fleet-apps.json")
    return f"  {app.name}  ({', '.join(bits)})"


def resolve_app(registry: L.Registry, query: str) -> L.App:
    """The one app `query` names: repo name, code dir, worktree prefix or acronym, any case.  If
    nothing matches exactly, dots and dashes are ignored (Congress-Trade finds Congress.Trade).
    More than one app, or none, raises Refusal with the candidates listed."""
    q = (query or "").strip()
    if not q:
        raise Refusal("the app name is empty")
    ql = q.lower()
    hits = [a for a in registry.apps if ql in _app_keys(a)]
    if not hits:
        loose = _loose(q)
        hits = [a for a in registry.apps if loose in {_loose(k) for k in _app_keys(a)}]
    unique: "list[L.App]" = []
    for a in hits:
        if a not in unique:
            unique.append(a)
    if len(unique) == 1:
        return unique[0]
    if unique:
        raise Refusal(f"app {q!r} is ambiguous; it matches:\n" + "\n".join(_app_line(a) for a in unique))
    near = [a for a in registry.apps if any(ql in k or k in ql for k in _app_keys(a))]
    pool = near or list(registry.registered_apps) or list(registry.apps)
    raise Refusal(f"unknown app {q!r}; candidates:\n" + "\n".join(_app_line(a) for a in pool))


def resolve_seat(env: Mapping[str, str], registry: L.Registry) -> L.Seat:
    """The seat from AGENT_SEAT, and nothing else.  Unset, unknown or retired raises Refusal, and
    so does a seat whose folder name belongs to another seat (GROK-BOT shares CURSOR's `cursor`)."""
    live = ", ".join(s.name for s in registry.seats if not s.retired)
    raw = (env.get(ENV_SEAT) or "").strip()
    if not raw:
        raise Refusal(f"{ENV_SEAT} is not set.  Set it to your seat tag ({live}) and run lane again; "
                      "lane never guesses a seat from a login, a path or a branch.")
    seat = registry.seat_by_name(raw)
    if seat is None:
        shown = raw if len(raw) <= 40 else raw[:40] + "..."
        raise Refusal(f"{ENV_SEAT}={shown!r} is not a seat tag in fleet-apps.json.  Known tags: {live}.")
    if seat.retired:
        raise Refusal(f"seat {seat.name} is retired in fleet-apps.json, and lane never creates lanes for a "
                      f"retired seat.  Set {ENV_SEAT} to your own seat tag ({live}).")
    if not L.is_valid_slug(seat.suffix):
        raise Refusal(f"seat {seat.name} has no usable folder name in fleet-apps.json (worktreeSuffix "
                      f"{seat.suffix!r}).")
    if not G.seat_owns_folder(seat, registry):
        owner = registry.seat_by_suffix(seat.suffix)
        owner_name = owner.name if owner is not None else "another seat"
        raise Refusal(f"seat {seat.name} shares the folder name {seat.suffix!r} with {owner_name} in fleet-apps.json, "
                      f"and that name belongs to {owner_name}, so a lane for {seat.name} would be {owner_name}'s "
                      f"lane.  A seat gets lanes only under a worktreeSuffix of its own.")
    return seat


def branch_for(seat: L.Seat, slug: str) -> str:
    """<first branch prefix of the seat in the registry>/<slug>.  No prefix is a refusal."""
    prefix = seat.primary_branch_prefix()
    if not prefix or not valid_ref_name(prefix):
        raise Refusal(f"seat {seat.name} has no branch prefix in fleet-apps.json, so lane cannot name a branch.")
    try:
        L.validate_slug(slug)
    except L.LayoutError as exc:
        raise Refusal(str(exc))
    return f"{prefix}/{slug}"


def parse_base(text: "str | None") -> "tuple[str, str]":
    """(branch, ref) for --base.  `origin/<branch>` and a bare `<branch>` both mean the branch on
    origin; the default is origin/main."""
    t = (text or "").strip() or f"origin/{DEFAULT_BASE_BRANCH}"
    for lead in ("refs/remotes/origin/", "origin/"):
        if t.startswith(lead):
            t = t[len(lead):]
            break
    if not valid_ref_name(t):
        raise Refusal(f"--base must be origin/<branch> or <branch> (a plain branch name on origin), got {text!r}")
    return t, f"origin/{t}"


_BOARD_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._#:/-]{0,63}$")


def _clean_purpose(text: "str | None") -> "str | None":
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    if len(t) > PURPOSE_MAX or any(ord(c) < 32 or ord(c) == 127 for c in t):
        raise Refusal(f"--purpose must be one line of at most {PURPOSE_MAX} characters")
    return t


def _clean_board(text: "str | None") -> "str | None":
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    if not _BOARD_RE.match(t):
        raise Refusal(f"--board must look like a board id (letters, digits, . _ # : / -, at most 64): {text!r}")
    return t


# --------------------------------------------------------------------------- context and output

@dataclass
class Ctx:
    env: Mapping[str, str]
    registry: L.Registry
    roots: L.Roots
    clock: Callable[[], _dt.datetime]
    exec_fn: "ExecFn | None"
    out: object
    err: object

    def say(self, message: str) -> None:
        for line in message.splitlines() or [""]:
            self.err.write(f"lane: {line}\n")

    def fold(self) -> Callable[[str], str]:
        return str.casefold if self.roots.case_insensitive else (lambda s: s)

    def key(self, path: "str | os.PathLike[str]") -> str:
        return L.real_key(path, self.roots)


def _iso(moment: _dt.datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _why(res: D.CmdResult) -> str:
    """One short redacted line saying why a command failed."""
    if res.timed_out:
        return "timed out"
    if res.error:
        return D.redact_text(res.error)[:200]
    lines = [ln.strip() for ln in res.err.splitlines() if ln.strip()]
    pick = next((ln for ln in reversed(lines) if ln.startswith(("fatal:", "error:", "ERROR"))), lines[-1] if lines else "")
    return D.redact_text(pick)[:200] or f"exit {res.rc}"


def _emit(ctx: Ctx, as_json: bool, path: Path, manifest: Mapping[str, object], **flags: object) -> None:
    if as_json:
        body = dict(manifest)
        body["path"] = str(path)
        body.update(flags)
        ctx.out.write(json.dumps(body, indent=2) + "\n")
    else:
        ctx.out.write(f"{path}\n")


# --------------------------------------------------------------------------- shared checks

def _in_tree(ctx: Ctx, path: "str | os.PathLike[str]", root: "str | os.PathLike[str]") -> bool:
    a, b = ctx.key(path), ctx.key(root)
    return a == b or a.startswith(b.rstrip(os.sep) + os.sep)


def _plan_path(ctx: Ctx, app: L.App, seat: L.Seat, slug: str, branch: str) -> "tuple[Path, Path]":
    """(lane path, write root).  Refuses a path that is not a conforming spot in the map."""
    roots, registry = ctx.roots, ctx.registry
    try:
        path = L.expected_lane_path(app, seat.suffix, slug, roots, registry)
    except L.LayoutError as exc:
        raise Refusal(str(exc))
    flat = roots.layout_mode == L.LAYOUT_FLAT
    want = L.LocationClass.LANE_FLAT if flat else L.LocationClass.LANE_NESTED
    got = L.classify_location(path, roots)
    if got is not want:
        raise Refusal(f"{path} is outside the lane map (it classifies as {got}, a new lane must be {want}); "
                      "check FLEET_LANES_ROOT and FLEET_LAYOUT.")
    write_root = roots.apps_root if flat else roots.lanes_root
    if _in_tree(ctx, write_root, roots.code_root):
        raise Refusal(f"the lanes folder {write_root} is inside {roots.code_root}; nothing new may be added "
                      "under ~/Code, so check FLEET_LANES_ROOT.")
    # The name rule is shared with the temp-checkout guard (guard.lane_name_reads_back), so the lane
    # a deny reason names is always one this accepts.  The path check after it also catches a
    # prefix folder that resolves somewhere else.
    res = L.explain_lane_name(path, registry, roots)
    if (not G.lane_name_reads_back(app.prefix, seat.suffix, slug, registry, flat=flat)
            or res.verdict is not L.NameVerdict.CONFORMING or res.app is None or res.app.prefix != app.prefix
            or res.seat != seat.suffix or res.slug != slug):
        raise Refusal(f"slug {slug!r} would make the lane name {path.name!r} read as seat {res.seat!r} and "
                      f"slug {res.slug!r}, not seat {seat.suffix!r}.  Pick a slug that does not start with "
                      "a seat name.")
    if L.check_branch_name(branch, registry) is not L.BranchVerdict.CONFORMING:
        raise Refusal(f"branch {branch!r} does not conform to the registry branch prefixes.")
    return path, Path(write_root)


def _plan_review_path(ctx: Ctx, app: L.App, seat: L.Seat, pr: int, *, own: bool = False) -> "tuple[Path, Path]":
    """(review path, lanes root).  The plain form is lanes/<Repo>/review-pr-<n>; `own` asks for the
    review-pr-<n>-<seat> form a second seat gets.  The destination must classify as a review checkout,
    and folders may be created anywhere at or below the lanes root."""
    roots = ctx.roots
    try:
        path = L.review_lane_path(app, pr, roots, ctx.registry, seat=seat.suffix if own else None)
    except L.LayoutError as exc:
        raise Refusal(str(exc))
    got = L.classify_location(path, roots)
    if got is not L.LocationClass.REVIEW:
        raise Refusal(f"{path} is outside the lanes root (it classifies as {got}); check FLEET_LANES_ROOT.")
    if _in_tree(ctx, roots.lanes_root, roots.code_root):
        raise Refusal(f"the lanes folder {roots.lanes_root} is inside {roots.code_root}; check FLEET_LANES_ROOT.")
    return path, Path(roots.lanes_root)


def _choose_review_path(ctx: Ctx, run: Runner, app: L.App, seat: L.Seat, pr: int) -> "tuple[Path, Path]":
    """Which review folder this seat uses for PR `pr`: the plain review-pr-<n>, unless another seat's
    manifest is on it, in which case review-pr-<n>-<seat>.  A rerun finds its own folder first, so
    the answer does not change once a seat has one.  Reads only; nothing is written."""
    plain, root = _plan_review_path(ctx, app, seat, pr)
    own, _ = _plan_review_path(ctx, app, seat, pr, own=True)
    if os.path.lexists(own):
        return own, root
    if os.path.lexists(plain):
        manifest = _read_manifest(run, plain)
        if manifest is not None and _owner_mismatch(manifest, seat) is not None:
            return own, root
    return plain, root


def _integration_tree(ctx: Ctx, app: L.App) -> Path:
    tree = Path(ctx.roots.code_root) / app.integration_dir_name
    if not tree.is_dir():
        raise Refusal(f"the integration tree {tree} does not exist.")
    return Path(os.path.realpath(tree))


def _check_tree(ctx: Ctx, run: Runner, tree: Path) -> None:
    top = run(["git", "rev-parse", "--show-toplevel"], cwd=tree)
    if not top.ok or ctx.key(top.out.strip()) != ctx.key(tree):
        raise Refusal(f"{tree} is not the top of a git repository.")
    origin = run(["git", "config", "--get", "remote.origin.url"], cwd=tree)
    if origin.rc == 1 and not origin.timed_out and not origin.error:
        raise Refusal(f"{tree} has no 'origin' remote.")
    if not origin.ok:
        raise ToolFailure(f"git config failed in {tree}: {_why(origin)}")


def _guard_destination(ctx: Ctx, tree: Path, path: Path, write_root: Path) -> None:
    if _in_tree(ctx, path, tree):
        raise Refusal(f"{path} is inside the integration tree {tree}.")
    if not write_root.is_dir() and not write_root.parent.is_dir():
        raise Refusal(f"{write_root.parent} does not exist.  Lane creates folders only at or below {write_root}, "
                      "so create the parent first.")
    key, root = ctx.key(path), ctx.key(write_root)
    parent = Path(os.path.realpath(path)).parent
    while ctx.key(parent) != root and ctx.key(parent).startswith(root + os.sep):
        if os.path.lexists(parent / ".git"):
            raise Refusal(f"{parent} is itself a git checkout; a lane cannot be created inside it.")
        parent = parent.parent
    if not (key == root or key.startswith(root + os.sep)):
        raise Refusal(f"{path} is not inside {write_root}.")


def _worktrees(run: Runner, tree: Path) -> "list[D.WorktreeRecord]":
    res = run(["git", "worktree", "list", "--porcelain"], cwd=tree)
    if not res.ok:
        raise ToolFailure(f"git worktree list failed in {tree}: {_why(res)}")
    return D.parse_worktree_porcelain(res.out)


def _path_state(ctx: Ctx, run: Runner, tree: Path, path: Path, branch: "str | None") -> str:
    """'new' when nothing is at `path`, 'same' when it is already the worktree asked for (on
    `branch`, or detached when `branch` is None).  Anything else is a Refusal; nothing is touched."""
    want = ctx.key(path)
    rec = next((r for r in _worktrees(run, tree) if ctx.key(r.path) == want), None)
    if rec is None:
        if os.path.lexists(path):
            raise Refusal(f"{path} already exists and is not a worktree of {tree}; lane does not touch it.")
        return "new"
    if rec.prunable is not None or not os.path.isdir(path):
        raise Refusal(f"{tree} lists a worktree at {path} whose folder is missing.  Run `git worktree prune` "
                      "there yourself; lane never prunes or forces.")
    on = "detached HEAD" if rec.detached else f"branch {rec.branch}"
    same = rec.detached if branch is None else (not rec.detached and rec.branch == branch)
    if not same:
        raise Refusal(f"{path} is already a worktree of {tree} on {on}, not on "
                      f"{'a detached HEAD' if branch is None else 'branch ' + branch}; lane does not touch it.")
    return "same"


def _legacy_lane(ctx: Ctx, run: Runner, tree: Path, path: Path, branch: str) -> "Path | None":
    """A checkout of `branch` that sits in an old folder and would migrate to exactly `path`."""
    want = ctx.key(path)
    for rec in _worktrees(run, tree):
        if rec.detached or rec.branch != branch or rec.prunable is not None or not os.path.isdir(rec.path):
            continue
        if ctx.key(rec.path) == want:
            continue
        res = L.explain_layout(rec.path, ctx.registry, ctx.roots)
        if res.status is L.LayoutStatus.LEGACY_MIGRATE and res.target is not None and ctx.key(res.target) == want:
            return Path(rec.path)
    return None


def _git_dir_of(run: Runner, path: Path) -> "Path | None":
    res = run(["git", "rev-parse", "--absolute-git-dir"], cwd=path)
    return Path(res.out.strip()) if res.ok and res.out.strip() else None


def _read_manifest(run: Runner, path: Path) -> "dict | None":
    gd = _git_dir_of(run, path)
    if gd is None:
        return None
    try:
        with open(gd / MANIFEST_NAME, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _common_dir(run: Runner, tree: Path) -> "Path | None":
    """The integration tree's git common dir (its .git), resolved; None if git cannot say."""
    res = run(["git", "rev-parse", "--git-common-dir"], cwd=tree)
    out = res.out.strip()
    return Path(os.path.realpath(os.path.join(tree, out))) if res.ok and out else None


@contextlib.contextmanager
def _repo_lock(ctx: Ctx, run: Runner, tree: Path) -> Iterator[None]:
    """Hold an exclusive flock on <common dir>/LOCK_NAME: one `lane new` per repo at a time.

    Without it, two runs for the same lane both find an empty path, both run `git worktree add`,
    and the loser's clean-up deletes the folder the winner just made and printed.  The lock covers
    everything from the second path check to the manifest.  It waits at most LOCK_WAIT seconds
    (exit 69 after that) and is released when the descriptor closes, also when the process dies.
    The file stays: removing it would let a waiting run lock a file that is no longer the lock."""
    common = _common_dir(run, tree)
    if common is None or not common.is_dir():
        raise ToolFailure(f"cannot find the git directory of {tree} to take the lane lock.")
    lock_path = common / LOCK_NAME
    deadline = time.monotonic() + LOCK_WAIT
    told = False
    while True:
        try:
            fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0), 0o644)
        except OSError as exc:
            raise ToolFailure(f"cannot open the lane lock {lock_path}: {exc}")
        locked = False
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
                        raise ToolFailure(f"cannot take the lane lock {lock_path}: {exc}")
                if not told:
                    ctx.say(f"waiting for another lane new in {tree} to finish")
                    told = True
                if time.monotonic() >= deadline:
                    raise ToolFailure(f"another lane new held the lock on {tree} for over {int(LOCK_WAIT)} "
                                      f"seconds ({lock_path}).  Run lane again when it is done.")
                time.sleep(_LOCK_POLL)
            try:
                current = os.path.samestat(os.fstat(fd), os.stat(str(lock_path)))
            except OSError:
                current = False
        except BaseException:
            os.close(fd)
            raise
        if locked and current:
            break
        os.close(fd)        # someone replaced or removed the file while we waited: lock the new one
    try:
        yield
    finally:
        os.close(fd)


def _confirm_added(ctx: Ctx, run: Runner, tree: Path, path: Path, branch: "str | None", sha: "str | None") -> None:
    """After `git worktree add` said yes, check that the lane really is there before printing it:
    registered, not prunable, a folder on disk, on `branch` (detached when None) and at `sha` when
    one is given.  Anything else is exit 69, and nothing is removed."""
    want = ctx.key(path)
    rec = next((r for r in _worktrees(run, tree) if ctx.key(r.path) == want), None)
    problem = ""
    if rec is None:
        problem = "git does not list it as a worktree"
    elif rec.prunable is not None:
        problem = "git lists it as prunable"
    elif not os.path.isdir(path):
        problem = "the folder is missing"
    elif branch is None and not rec.detached:
        problem = f"it is on branch {rec.branch}, not detached"
    elif branch is not None and (rec.detached or rec.branch != branch):
        problem = f"it is on {'a detached HEAD' if rec.detached else 'branch ' + str(rec.branch)}, not branch {branch}"
    elif sha is not None and rec.head != sha:
        problem = f"its HEAD is {str(rec.head)[:9]}, not {sha[:9]}"
    if problem:
        raise ToolFailure(f"git worktree add reported success, but {path} is not the checkout that was asked for "
                          f"({problem}).  Lane removes nothing; look at `git -C {tree} worktree list` and run lane again.")


def _owner_mismatch(manifest: Mapping[str, object], seat: L.Seat) -> "str | None":
    """The other seat's name when the manifest says the checkout is not `seat`'s, else None."""
    tag, suffix = manifest.get("tag"), manifest.get("seat")
    if isinstance(tag, str) and tag.strip():
        mine, theirs, shown = seat.name.lower(), tag.strip().lower(), tag.strip()
    elif isinstance(suffix, str) and suffix.strip():
        mine, theirs, shown = seat.suffix.lower(), suffix.strip().lower(), suffix.strip()
    else:
        return None
    return shown[:40] if mine != theirs else None


def _refuse_other_owner(run: Runner, path: Path, seat: L.Seat) -> "dict | None":
    """The manifest of an existing checkout, after making sure it is not another seat's."""
    manifest = _read_manifest(run, path)
    if manifest is None:
        return None
    other = _owner_mismatch(manifest, seat)
    if other is not None:
        raise Refusal(f"{path} already exists, and its {MANIFEST_NAME} says it belongs to {other}, not {seat.name}.  "
                      "Lane never hands one seat's checkout to another.")
    return manifest


def _write_manifest(ctx: Ctx, run: Runner, tree: Path, path: Path, manifest: Mapping[str, object]) -> None:
    """Write lane.json atomically into the new worktree's private git dir, and only there.  A
    problem becomes a warning: the lane itself exists and is usable."""
    try:
        gd = _git_dir_of(run, path)
        common = _common_dir(run, tree)
        if gd is None or common is None:
            raise OSError("could not locate the worktree git dir")
        admin = common / "worktrees"
        real = Path(os.path.realpath(gd))
        if not (ctx.key(real).startswith(ctx.key(admin) + os.sep)):
            raise OSError(f"{gd} is not under {admin}")
        target = real / MANIFEST_NAME
        tmp = real / f".{MANIFEST_NAME}.tmp.{os.getpid()}"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(manifest, indent=2) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink()
    except OSError as exc:
        ctx.say(f"warning: {MANIFEST_NAME} was not written ({exc}); the worktree itself is fine.")


def _make_parent(ctx: Ctx, parent: Path, write_root: Path) -> None:
    """Create the missing folders between the write root and the lane, nothing else."""
    if not _in_tree(ctx, parent, write_root):
        raise Refusal(f"{parent} is not inside {write_root}.")
    try:
        os.makedirs(parent, exist_ok=True)
    except OSError as exc:
        raise ToolFailure(f"cannot create {parent}: {exc}")


def _branch_state(run: Runner, tree: Path, branch: str) -> "tuple[bool, bool]":
    """(exists locally, exists as a remote-tracking ref).  Raises Refusal if the name collides with
    another branch (a parent `claude` or children `claude/x/y`)."""
    pieces = branch.split("/")
    for i in range(1, len(pieces)):
        parent = "/".join(pieces[:i])
        res = run(["git", "show-ref", "--verify", "--quiet", f"refs/heads/{parent}"], cwd=tree)
        if res.ok:
            raise Refusal(f"a branch named {parent!r} exists, which blocks {branch!r}; pick another slug.")
        if res.rc != 1 or res.timed_out or res.error:
            raise ToolFailure(f"git show-ref failed in {tree}: {_why(res)}")
    res = run(["git", "for-each-ref", "--format=%(refname)", f"refs/heads/{branch}",
               f"refs/remotes/origin/{branch}"], cwd=tree)
    if not res.ok:
        raise ToolFailure(f"git for-each-ref failed in {tree}: {_why(res)}")
    names = [ln.strip() for ln in res.out.splitlines() if ln.strip()]
    local, tracking = f"refs/heads/{branch}" in names, f"refs/remotes/origin/{branch}" in names
    children = [n for n in names if n not in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")]
    if children:
        raise Refusal(f"branches under {branch!r} exist (for example {children[0]}), which blocks it; pick another slug.")
    return local, tracking


def _refuse_if_checked_out(run: Runner, tree: Path, branch: str) -> None:
    for rec in _worktrees(run, tree):
        if not rec.detached and rec.branch == branch:
            raise Refusal(f"branch {branch} is already checked out at {rec.path}; a branch can live in one "
                          "worktree at a time.")


def _fetch(run: Runner, tree: Path, ref: str) -> D.CmdResult:
    return run(["git", "fetch", "--no-tags", "origin", ref], cwd=tree)


def _missing_remote_ref(res: D.CmdResult) -> bool:
    return res.rc not in (0, None) and "couldn't find remote ref" in res.err.lower()


def _head_sha(run: Runner, cwd: Path, rev: str) -> "str | None":
    res = run(["git", "rev-parse", "--verify", "-q", rev], cwd=cwd)
    sha = res.out.strip()
    return sha if res.ok and _SHA.match(sha) else None


# --------------------------------------------------------------------------- lane new

def _new_lane(ctx: Ctx, args: argparse.Namespace, app: L.App, seat: L.Seat) -> int:
    slug = args.slug
    branch = branch_for(seat, slug)
    if args.reuse_branch and args.base:
        raise Refusal("--base has no effect with --reuse-branch; the existing branch is used as it is.")
    base_branch, base_ref = parse_base(args.base)
    purpose, board = _clean_purpose(args.purpose), _clean_board(args.board)
    path, write_root = _plan_path(ctx, app, seat, slug, branch)
    tree = _integration_tree(ctx, app)
    run = Runner(ctx.env, write_root=write_root, fold=ctx.fold(), exec_fn=ctx.exec_fn)
    _check_tree(ctx, run, tree)
    _guard_destination(ctx, tree, path, write_root)

    def existing() -> int:
        manifest = _refuse_other_owner(run, path, seat) or {
            "schema": SCHEMA, "tool": TOOL_NAME, "kind": "lane", "seat": seat.suffix, "tag": seat.name,
            "app": app.name, "prefix": app.prefix, "lane_dir": app.lane_dir, "slug": slug, "branch": branch}
        ctx.say(f"already exists, nothing changed: {path} (branch {branch})")
        _emit(ctx, args.json, path, manifest, existing=True)
        return EXIT_OK

    # Phase one, without the lock: every refusal happens here, before anything is written.
    if _path_state(ctx, run, tree, path, branch) == "same":
        return existing()

    local, tracking = _branch_state(run, tree, branch)

    # The same lane in an old folder (lanes/fleet/claude-x, a flat ~/apps/fleet-claude-x): print it, do
    # not make a second one.  The layout migration moves it to `path`.  Only a branch that already
    # exists can be checked out in one, so a brand-new lane skips the extra listing.
    old = _legacy_lane(ctx, run, tree, path, branch) if (local or tracking) else None
    if old is not None:
        manifest = _refuse_other_owner(run, old, seat) or {
            "schema": SCHEMA, "tool": TOOL_NAME, "kind": "lane", "seat": seat.suffix, "tag": seat.name,
            "app": app.name, "prefix": app.prefix, "lane_dir": app.lane_dir, "slug": slug, "branch": branch}
        ctx.say(f"already exists in an old folder, nothing changed: {old} (branch {branch}).  "
                f"The layout migration moves it to {path}.")
        _emit(ctx, args.json, old, manifest, existing=True, legacy_location=True)
        return EXIT_OK
    if args.reuse_branch:
        if local or tracking:
            _refuse_if_checked_out(run, tree, branch)
    elif local or tracking:
        where = "locally" if local else "on origin (as last fetched)"
        raise Refusal(f"branch {branch} already exists {where}.  Pick another slug, or pass --reuse-branch to "
                      "check that branch out in a new lane.")

    created = _iso(ctx.clock())
    manifest: "dict[str, object]" = {
        "schema": SCHEMA, "tool": TOOL_NAME, "kind": "lane", "seat": seat.suffix, "tag": seat.name,
        "app": app.name, "prefix": app.prefix, "lane_dir": app.lane_dir, "slug": slug, "branch": branch,
        "base": None if args.reuse_branch else base_ref, "base_sha": None,
        "purpose": purpose, "board": board, "created_at": created,
    }

    # The full remote-tracking ref, never the DWIM name origin/<branch>: a local branch or a tag
    # named origin/main would win over refs/remotes/origin/main.  base_ref stays the display string.
    base_rev = f"refs/remotes/origin/{base_branch}^{{commit}}"
    if args.dry_run:
        known = None if args.reuse_branch else _head_sha(run, tree, base_rev)
        manifest["base_sha"] = known
        ctx.say(f"dry run, nothing changed.  Would create {path}")
        if args.reuse_branch:
            ctx.say(f"  branch {branch} (existing, --reuse-branch)" + ("" if local or tracking else
                    "; not on disk, so a real run would fetch it from origin and refuse if it is not there"))
        else:
            ctx.say(f"  branch {branch} from {base_ref} "
                    f"({known[:9] if known else 'not fetched yet; a real run fetches it first'})")
        ctx.say(f"  repo {tree}")
        _emit(ctx, args.json, path, manifest, dry_run=True)
        return EXIT_OK

    # Phase two, under the per-repo lock until the manifest is written.  Another run may have made
    # this very lane while we waited, so the path is checked again first.
    with _repo_lock(ctx, run, tree):
        if _path_state(ctx, run, tree, path, branch) == "same":
            return existing()
        if args.reuse_branch:
            res = _fetch(run, tree, branch)
            if _missing_remote_ref(res):
                if not local:
                    raise Refusal(f"branch {branch} is not on origin and not local; drop --reuse-branch to create it.")
            elif not res.ok:
                raise ToolFailure(f"git fetch of {branch} failed: {_why(res)}")
            local, tracking = _branch_state(run, tree, branch)
            if not (local or tracking):
                raise ToolFailure(f"branch {branch} did not appear after the fetch.")
            _refuse_if_checked_out(run, tree, branch)
            start = None
        else:
            res = _fetch(run, tree, base_branch)
            if not res.ok:
                raise ToolFailure(f"git fetch of {base_ref} failed: {_why(res)}")
            start = _head_sha(run, tree, base_rev)
            if start is None:
                raise ToolFailure(f"{base_ref} did not resolve after the fetch; is {base_branch!r} a branch on origin?")
            probe = _fetch(run, tree, branch)
            if probe.ok:
                raise Refusal(f"branch {branch} already exists on origin.  Pick another slug, or pass --reuse-branch.")
            if not _missing_remote_ref(probe):
                raise ToolFailure(f"could not check origin for branch {branch}: {_why(probe)}")
            manifest["base_sha"] = start

        _make_parent(ctx, path.parent, write_root)
        argv = (["git", "worktree", "add", str(path), branch] if args.reuse_branch
                else ["git", "worktree", "add", "-b", branch, str(path), start])
        added = run(argv, cwd=tree)
        if not added.ok:
            raise ToolFailure(f"git worktree add failed: {_why(added)}")
        _confirm_added(ctx, run, tree, path, branch, start)
        if args.reuse_branch:
            manifest["base_sha"] = _head_sha(run, path, "HEAD")
        _write_manifest(ctx, run, tree, path, manifest)
    ctx.say(f"created {path}")
    ctx.say(f"  branch {branch}" + ("" if args.reuse_branch else f" from {base_ref} at {str(start)[:9]}")
            + f"; repo {tree}")
    _emit(ctx, args.json, path, manifest)
    return EXIT_OK


# --------------------------------------------------------------------------- lane new --review

def _new_review(ctx: Ctx, args: argparse.Namespace, app: L.App, seat: L.Seat) -> int:
    pr = args.pr
    purpose, board = _clean_purpose(args.purpose), _clean_board(args.board)
    if not app.owner_repo or not _GH_REPO.match(app.owner_repo):
        raise Refusal(f"{app.name} has no GitHub repo in the registry, so lane cannot look up PR {pr}.")
    _plain, lanes_root = _plan_review_path(ctx, app, seat, pr)
    tree = _integration_tree(ctx, app)
    run = Runner(ctx.env, write_root=lanes_root, fold=ctx.fold(), exec_fn=ctx.exec_fn)
    _check_tree(ctx, run, tree)
    path, _ = _choose_review_path(ctx, run, app, seat, pr)
    _guard_destination(ctx, tree, path, lanes_root)

    def existing() -> int:
        manifest = _refuse_other_owner(run, path, seat) or {
            "schema": SCHEMA, "tool": TOOL_NAME, "kind": "review", "seat": seat.suffix, "tag": seat.name,
            "app": app.name, "prefix": app.prefix, "lane_dir": app.lane_dir, "pr": pr}
        ctx.say(f"review checkout already exists, nothing changed (not updated to the PR's newest head): {path}")
        _emit(ctx, args.json, path, manifest, existing=True)
        return EXIT_OK

    if _path_state(ctx, run, tree, path, None) == "same":
        return existing()

    created_dt = ctx.clock()
    manifest: "dict[str, object]" = {
        "schema": SCHEMA, "tool": TOOL_NAME, "kind": "review", "seat": seat.suffix, "tag": seat.name,
        "app": app.name, "prefix": app.prefix, "lane_dir": app.lane_dir, "pr": pr, "head_sha": None, "head_ref": None,
        "pr_state": None, "cross_repo": None, "purpose": purpose, "board": board,
        "created_at": _iso(created_dt),
        "expires_at": _iso(created_dt + _dt.timedelta(days=REVIEW_DAYS)),
    }
    if args.dry_run:
        ctx.say(f"dry run, nothing changed.  Would look up PR {pr} of {app.owner_repo} with gh, fetch pull/{pr}/head "
                f"and create a detached checkout at {path}")
        _emit(ctx, args.json, path, manifest, dry_run=True)
        return EXIT_OK

    with _repo_lock(ctx, run, tree):
        if _path_state(ctx, run, tree, path, None) == "same":
            return existing()
        view = run(["gh", "pr", "view", str(pr), "--repo", app.owner_repo, "--json",
                    "headRefOid,headRefName,state,isCrossRepository"], cwd=tree)
        if not view.ok:
            if "could not resolve to a pullrequest" in view.err.lower():
                raise Refusal(f"PR {pr} was not found in {app.owner_repo}.")
            raise ToolFailure(f"gh pr view {pr} failed: {_why(view)}")
        try:
            info = json.loads(view.out)
            sha = info["headRefOid"]
            if not isinstance(sha, str) or not _SHA.match(sha):
                raise ValueError("headRefOid is not a commit id")
        except (ValueError, KeyError, TypeError) as exc:
            raise ToolFailure(f"gh pr view {pr} gave an unusable answer: {exc}")
        state = str(info.get("state") or "")
        manifest.update(head_sha=sha, head_ref=info.get("headRefName"), pr_state=state or None,
                        cross_repo=bool(info.get("isCrossRepository")))
        if state and state != "OPEN":
            ctx.say(f"note: PR {pr} is {state}.")

        res = _fetch(run, tree, f"pull/{pr}/head")
        if not res.ok:
            raise ToolFailure(f"git fetch of pull/{pr}/head failed: {_why(res)}")
        if _head_sha(run, tree, f"{sha}^{{commit}}") is None:
            raise ToolFailure(f"PR {pr}'s head {sha[:9]} is not what pull/{pr}/head holds now; the PR was probably "
                              "updated.  Run lane again.")
        _make_parent(ctx, path.parent, lanes_root)
        added = run(["git", "worktree", "add", "--detach", str(path), sha], cwd=tree)
        if not added.ok:
            raise ToolFailure(f"git worktree add failed: {_why(added)}")
        _confirm_added(ctx, run, tree, path, None, sha)
        _write_manifest(ctx, run, tree, path, manifest)
    ctx.say(f"created review checkout {path}")
    ctx.say(f"  PR {pr} head {sha[:9]} (detached); read-only, expires {manifest['expires_at']}; repo {tree}")
    _emit(ctx, args.json, path, manifest)
    return EXIT_OK


# --------------------------------------------------------------------------- command handlers

def _require_lane_app(app: L.App) -> None:
    """An app gets lanes only if it has an integration tree to hang worktrees on and a row in
    fleet-apps.json (docs/protocols/lane-map.md: an app without a row is added before it gets lanes)."""
    if not app.integration_dir_name:
        raise Refusal(f"{app.name} has no integration tree under ~/Code (its clone lives elsewhere), so lane "
                      "cannot create a worktree for it.")
    if not app.registered:
        raise Refusal(f"{app.name} has no row in fleet-apps.json.  Add one first (docs/protocols/lane-map.md); "
                      "a lane is only created for a registered app.")


def _cmd_new(ctx: Ctx, args: argparse.Namespace) -> int:
    app = resolve_app(ctx.registry, args.app)
    _require_lane_app(app)
    seat = resolve_seat(ctx.env, ctx.registry)
    if args.review:
        if args.pr is None:
            raise Refusal("--review needs --pr N.")
        if args.slug or args.base or args.reuse_branch:
            raise Refusal("--review takes only the app and --pr N (no slug, --base or --reuse-branch).")
        return _new_review(ctx, args, app, seat)
    if args.pr is not None:
        raise Refusal("--pr belongs with --review.")
    if not args.slug:
        raise Refusal("a slug is required: lane new <app> <slug>.")
    return _new_lane(ctx, args, app, seat)


def _cmd_path(ctx: Ctx, args: argparse.Namespace) -> int:
    app = resolve_app(ctx.registry, args.app)
    _require_lane_app(app)
    seat = resolve_seat(ctx.env, ctx.registry)
    if args.review:
        if args.pr is None:
            raise Refusal("--review needs --pr N.")
        if args.slug:
            raise Refusal("--review takes only the app and --pr N (no slug).")
        _plain, lanes_root = _plan_review_path(ctx, app, seat, args.pr)
        run = Runner(ctx.env, write_root=lanes_root, fold=ctx.fold(), exec_fn=ctx.exec_fn)
        path, _ = _choose_review_path(ctx, run, app, seat, args.pr)
    else:
        if args.pr is not None:
            raise Refusal("--pr belongs with --review.")
        if not args.slug:
            raise Refusal("a slug is required: lane path <app> <slug>.")
        path, _ = _plan_path(ctx, app, seat, args.slug, branch_for(seat, args.slug))
    ctx.out.write(f"{path}\n")
    return EXIT_OK


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        # argparse exits 2; this tool's contract is 64 for any usage error.
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _build_parser() -> _Parser:
    p = _Parser(prog="lane", description="Create lanes and review checkouts in the right place.  Seat comes "
                f"only from {ENV_SEAT}.  Exit 0 ok, 64 usage or refusal, 69 git or gh failed.")
    sub = p.add_subparsers(dest="command", metavar="{new,path,ls,doctor}")
    new = sub.add_parser("new", help="create a lane (or a review checkout with --review --pr N)",
                         description="Create a lane worktree off origin/main and print its path.")
    new.add_argument("app", help="repo name, code dir, worktree prefix or acronym (any case)")
    new.add_argument("slug", nargs="?", help="lowercase kebab, 1 to 40 characters")
    new.add_argument("--base", metavar="REF", help="origin/<branch> or <branch> to start from (default origin/main)")
    new.add_argument("--purpose", metavar="TEXT", help="one line saved in the manifest")
    new.add_argument("--board", metavar="ID", help="board id saved in the manifest")
    new.add_argument("--reuse-branch", action="store_true", help="check out an existing branch instead of creating one")
    new.add_argument("--review", action="store_true", help="read-only checkout of a PR head (needs --pr)")
    new.add_argument("--pr", metavar="N", type=_positive_int, help="pull request number for --review")
    new.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    new.add_argument("--json", action="store_true", help="print the manifest plus the path as JSON on stdout")
    path = sub.add_parser("path", help="print the path a lane (or, with --review --pr N, a review checkout) would get; "
                          "no side effects")
    path.add_argument("app", help="repo name, code dir, worktree prefix or acronym (any case)")
    path.add_argument("slug", nargs="?", help="lowercase kebab, 1 to 40 characters")
    path.add_argument("--review", action="store_true", help="the review checkout of a PR (needs --pr)")
    path.add_argument("--pr", metavar="N", type=_positive_int, help="pull request number for --review")
    sub.add_parser("ls", help="every checkout on this Mac (the doctor report; takes doctor options)")
    sub.add_parser("doctor", help="same as ls")
    return p


def _positive_int(text: str) -> int:
    if not (text.isascii() and text.isdigit() and 1 <= len(text) <= 9 and int(text) > 0):
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return int(text)


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def main(argv: "Sequence[str] | None" = None, *, env: "Mapping[str, str] | None" = None,
         clock: "Callable[[], _dt.datetime] | None" = None, exec_fn: "ExecFn | None" = None,
         stdout=None, stderr=None) -> int:
    """CLI entry.  `env` carries AGENT_SEAT, HOME, FLEET_LAYOUT, FLEET_LANES_ROOT, FLEET_APPS_JSON
    and the base environment for git and gh; nothing is read from os.environ when it is given.
    `exec_fn(argv, cwd, env, timeout)` replaces the process starter (tests fake gh with it); it is
    only reached after the allowlist check."""
    env = os.environ if env is None else env
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    args_in = list(sys.argv[1:] if argv is None else argv)
    if args_in and args_in[0] in ("ls", "doctor"):
        # The doctor owns its flags and its exit codes (0, 2 for --strict, 64).
        try:
            return int(D.main(args_in[1:], env=env, stdout=out))
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_USAGE)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            args = _build_parser().parse_args(args_in)
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else (EXIT_OK if exc.code is None else EXIT_USAGE)
        if not args.command:
            _build_parser().print_usage(err)
            return EXIT_USAGE
    stamp = clock or _utc_now
    ctx: "Ctx | None" = None
    try:
        try:
            registry = L.load_registry(env=env)
            home = env.get("HOME") or os.path.expanduser("~")
            roots = L.make_roots(home, env, registry=registry)
        except (OSError, ValueError) as exc:
            raise Refusal(f"cannot read the registry: {exc}")
        ctx = Ctx(env, registry, roots, stamp, exec_fn, out, err)
        for note in roots.warnings:
            ctx.say(f"warning: {note}")
        return _cmd_new(ctx, args) if args.command == "new" else _cmd_path(ctx, args)
    except Refusal as exc:
        _say(err, "refused: " + str(exc))
        return EXIT_USAGE
    except ToolFailure as exc:
        _say(err, "error: " + str(exc))
        return EXIT_TOOL
    except CommandRefused as exc:
        _say(err, f"internal error: {exc}")
        return EXIT_INTERNAL


def _say(stream, message: str) -> None:
    lines = message.splitlines() or [""]
    stream.write(f"lane: {lines[0]}\n")
    for line in lines[1:]:
        stream.write(f"{line}\n")


if __name__ == "__main__":
    sys.exit(main())
