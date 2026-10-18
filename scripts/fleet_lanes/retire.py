"""lane retire and lane prune: remove a seat's finished lanes with `git worktree remove`, never on a guess.

    lane retire <path-or-slug> [--repo APP] [--dry-run] [--json]
    lane prune [--seat NAME] [--all-seats] [--repo APP] [--apply] [--json]

`lane retire` removes ONE lane of your own seat (AGENT_SEAT) once its work is safe on GitHub: run it from
outside the lane (`cd ~ && lane retire <slug>`) when the PR has merged.  `lane prune` looks at every lane of a
seat (default: yours) in every registered repo, prints a table with a decision and a reason for each, and
removes the removable ones only with `--apply`.  The dry run is the default.  `--seat` names a seat (its tag
such as CLAUDE or its folder name such as claude, any case); another seat than yours is refused unless
`--all-seats` is also given, and `--all-seats` alone looks at every seat.

A lane is removable only when ALL of these hold (each is a reason in the table when it does not):

  * it is a linked worktree that git lists in the integration tree, under the lanes root (an external-disk lane
    counts), and never the integration tree or anything else under ~/Code;
  * its folder name (and its lane.json, when there is one) says it belongs to the chosen seat;
  * it is not locked, no process has its cwd inside it (`lsof -d cwd`; if lsof fails nothing is removed), no git
    operation is in progress (MERGE_HEAD, rebase, cherry-pick, revert, bisect, sequencer, index.lock) and it has
    no `.janitor-keep` marker;
  * `git status` shows no tracked edit, no untracked file and no git-ignored local state (a .env, a database:
    anything ignored that is not build output, by the doctor's own list, `doctor.split_ignored`);
  * nothing is unpushed: HEAD is reachable from a branch that exists on origin right now (the shas `git
    ls-remote --heads origin` reports; no other local remote-tracking ref counts, since a branch deleted on
    origin without a `fetch --prune` leaves a stale one, and another remote never vouches), or HEAD is in
    origin/<default> (never deleted or rewound), or a merged or closed PR holds HEAD (its head is HEAD or
    contains it), because a squash-merged branch whose remote ref was pruned is safe on GitHub but on no branch;
  * the work is done: the PR for its branch (the local name or the upstream name, since a sweep lane pushes
    `HEAD:<pr head>`) or for HEAD is MERGED or CLOSED with none still OPEN and its head is HEAD, or contains HEAD
    while the lane has commits of its own (a PR that only used the same branch name, or whose head merely sits
    on today's origin/<default>, is no evidence), or HEAD is merged into origin/<default>, or its upstream
    branch no longer exists on origin.
    A review checkout (review-pr-<n>) is done when PR <n> is MERGED or CLOSED; prune keeps one of an OPEN PR,
    and an explicit `lane retire` may remove it (it is read-only by design).

`lane prune` adds two guards that `lane retire` (an explicit request from the seat that owns the lane) does not:
a lane with activity in the last RECENT_MINUTES (15) minutes is kept (its git dir HEAD, index and reflog, the
folder itself and the manifest's created_at), and a lane whose only "done" evidence is that HEAD sits in
origin/<default> with no upstream and no PR (a lane that was made and never used) is kept until it has been idle
EMPTY_LANE_HOURS (24) hours, so a prune never takes the lane a parallel session of the same seat just opened.

How it removes, under the same per-repo lock as `lane new` (`lane._repo_lock`): `git status` once more, then
`git worktree remove <path>` WITHOUT --force (git itself refuses a dirty or locked worktree, a second guard),
then `git branch -d <branch>` (never -D; after a squash merge git usually says "not fully merged" and the branch
is reported as kept, which is not an error).  `git worktree prune` runs only when a prunable entry is left that
is a lane of the chosen seat under the lanes root, and never while a lanes/<Repo> symlink is dangling (the
external disk is not mounted: prune would delete the registry entries of every lane on it,
docs/protocols/lane-map.md, External Lanes).  It never runs rm, never forces, never pushes, and never deletes a
remote branch.

Every git, gh and lsof call goes through `Runner`, whose allowlist is `check_allowed` below (a separate list
from `lane new`'s, which still refuses every remove).  The gh calls are one bulk `gh pr list --state all` per
repo, plus `gh pr list --head <name>` only for a lane the bulk list could not place.

Exit codes: 0 ok (some lanes may be kept: read the table), 64 usage or refusal (`lane retire` of a lane that is
not removable is a refusal), 69 git, gh or lsof failed, or a removal failed.

Python 3.9 safe.  Tests: fleet_lanes/tests/test_retire.py (real git, a local bare origin, gh and lsof faked).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime as _dt
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import doctor as D
from . import lane as K
from . import layout as L

__all__ = [
    "EMPTY_LANE_HOURS",
    "GH_BULK_LIMIT",
    "RECENT_MINUTES",
    "Facts",
    "PrView",
    "Runner",
    "check_allowed",
    "decide",
    "main",
]

RECENT_MINUTES = 15
EMPTY_LANE_HOURS = 24
GH_BULK_LIMIT = 1000
GH_FIELDS = "number,state,headRefName,headRefOid,mergedAt,closedAt"
WORKERS = 8

_CAPS = {"git": 30.0, "remove": 600.0, "net": 90.0, "gh": 90.0, "lsof": 30.0}
_SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_GH_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REVIEW = re.compile(r"^review-pr-([0-9]{1,9})(?:-([a-z0-9]+(?:-[a-z0-9]+)*))?$")
_BRANCH_KEY = re.compile(r"^branch\.(.+)\.(merge|remote)$")
_REVPARSE_SOLO = frozenset({"--absolute-git-dir", "--git-common-dir", "--show-toplevel"})


# --------------------------------------------------------------------------- the allowlist and the runner


def _git_key(args: Sequence[str], write_root: str | None, fold: Callable[[str], str]) -> str | None:
    """The timeout key for an allowed git command, else None.  Exact shapes only."""
    a = list(args)
    if a == ["worktree", "list", "--porcelain"] or a == ["worktree", "prune"]:
        return "git"
    if len(a) == 3 and a[:2] == ["worktree", "remove"]:
        return "remove" if K._dest_ok(a[2], write_root, fold) else None
    if len(a) == 3 and a[:2] == ["branch", "-d"]:
        return "git" if K.valid_ref_name(a[2]) else None
    if a == ["status", "--porcelain", "--ignored", "--untracked-files=normal"]:
        return "git"
    if len(a) == 5 and a[:4] == ["rev-list", "--count", "HEAD", "--not"]:
        return "git" if _SHA.match(a[4]) else None
    if len(a) >= 5 and a[:5] == ["rev-list", "--count", "--ignore-missing", "HEAD", "--not"]:
        return "git" if all(_SHA.match(x) for x in a[5:]) else None
    if len(a) == 2 and a[0] == "rev-parse" and a[1] in _REVPARSE_SOLO:
        return "git"
    if len(a) == 4 and a[:3] == ["rev-parse", "--verify", "-q"]:
        rev = a[3].removesuffix("^{commit}")
        return "git" if (rev == "HEAD" or _SHA.match(rev)) else None
    if len(a) == 3 and a[:2] == ["config", "--get"]:
        if a[2] == "remote.origin.url":
            return "git"
        m = _BRANCH_KEY.match(a[2])
        return "git" if (m and K.valid_ref_name(m.group(1))) else None
    if len(a) == 4 and a[:3] == ["merge-base", "--is-ancestor", "HEAD"]:
        ref = a[3]
        return "git" if (ref.startswith("refs/remotes/origin/") and K.valid_ref_name(ref)) else None
    if a == ["symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"]:
        return "git"
    if a == ["ls-remote", "--heads", "origin"]:
        return "net"
    return None


def _gh_ok(args: Sequence[str]) -> bool:
    a = list(args)
    if (
        len(a) == 10
        and a[:2] == ["pr", "list"]
        and a[2] == "--repo"
        and a[4:7] == ["--state", "all", "--limit"]
        and a[8] == "--json"
    ):
        return bool(_GH_REPO.match(a[3])) and a[7] == str(GH_BULK_LIMIT) and a[9] == GH_FIELDS
    if (
        len(a) == 12
        and a[:2] == ["pr", "list"]
        and a[2] == "--repo"
        and a[4:7] == ["--state", "all", "--head"]
        and a[8:10] == ["--limit", "20"]
        and a[10] == "--json"
    ):
        return bool(_GH_REPO.match(a[3])) and K.valid_ref_name(a[7]) and a[11] == GH_FIELDS
    return False


def check_allowed(
    argv: Sequence[str],
    *,
    write_root: str | os.PathLike[str] | None = None,
    fold: Callable[[str], str] | None = None,
) -> float:
    """The timeout cap for an allowlisted command, else CommandRefused (no process is started).

    git: worktree list --porcelain | worktree remove <path inside the lanes root> (never --force) | worktree prune |
    branch -d <ref> (never -D) | status --porcelain --ignored --untracked-files=normal | rev-list --count HEAD
    --not <sha> | rev-list --count --ignore-missing HEAD --not <sha>... | rev-parse (--absolute-git-dir | --git-common-dir | --show-toplevel | --verify -q
    HEAD-or-sha) | config --get (remote.origin.url | branch.<ref>.merge | branch.<ref>.remote) | merge-base
    --is-ancestor HEAD refs/remotes/origin/<ref> | symbolic-ref -q --short refs/remotes/origin/HEAD | ls-remote
    --heads origin.  gh: pr list --repo O/R --state all (--limit 1000 | --head <ref> --limit 20) --json <fields>.
    lsof: -d cwd -Fpcn."""
    if not argv or not all(isinstance(a, str) for a in argv) or any("\x00" in a for a in argv):
        raise K.CommandRefused("empty, non-string or NUL-bearing command")
    fold = fold or (lambda s: s)
    root = os.fspath(write_root) if write_root is not None else None
    key: str | None = None
    if argv[0] == "git":
        key = _git_key(argv[1:], root, fold)
    elif argv[0] == "gh":
        key = "gh" if _gh_ok(argv[1:]) else None
    elif argv[0] == "lsof":
        key = "lsof" if list(argv[1:]) == ["-d", "cwd", "-Fpcn"] else None
    if key is None:
        raise K.CommandRefused("not on the lane retire allowlist: " + " ".join(argv[:5]))
    return _CAPS[key]


class Runner:
    """The one place a retire or prune command starts: `check_allowed` first, then the process starter (tests swap
    `exec_fn`).  git gets `--no-optional-locks -c core.fsmonitor=false`, so `git status` never rewrites a lane's
    index, and the GIT_DIR family is stripped from the environment."""

    def __init__(
        self,
        env: Mapping[str, str],
        *,
        write_root: str | os.PathLike[str] | None = None,
        fold: Callable[[str], str] | None = None,
        exec_fn: K.ExecFn | None = None,
    ) -> None:
        self._env = K._child_env(env)
        self._env["GIT_OPTIONAL_LOCKS"] = "0"
        self._write_root = os.fspath(write_root) if write_root is not None else None
        self._fold = fold or (lambda s: s)
        self._exec = exec_fn or K.default_exec

    def __call__(
        self, argv: Sequence[str], cwd: str | os.PathLike[str] | None = None
    ) -> D.CmdResult:
        cap = check_allowed(argv, write_root=self._write_root, fold=self._fold)
        full = list(argv)
        if full[0] == "git":
            full = ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *full[1:]]
        return self._exec(full, os.fspath(cwd) if cwd is not None else None, self._env, cap)


# --------------------------------------------------------------------------- facts and the decision


@dataclass
class PrView:
    """What GitHub says about the PR(s) that match a lane.  `found` is False when no lookup worked (unknown)."""

    found: bool = True
    open_number: int | None = None
    done_number: int | None = None  # a MERGED or CLOSED PR for the branch name or HEAD
    done_state: str = ""
    covers_head: bool = False  # that PR's head is HEAD or contains it
    review_state: str = ""  # review checkouts: the state of PR <n>
    # a MERGED or CLOSED PR that used this branch name but whose head does not hold HEAD (a reused slug, or new
    # commits after the merge): it is not evidence that this lane's work is done
    stale_number: int | None = None


@dataclass
class Facts:
    """Everything `decide` needs about one lane.  None means "could not be read", which never permits removal."""

    kind: str  # lane | review | managed | other
    seat: str | None
    owner_conflict: str | None = None  # lane.json names this other seat
    prunable: bool = False
    locked: bool = False
    processes: list[tuple[int, str]] | None = None
    git_ops: list[str] = field(default_factory=list)
    keep_marker: bool = False
    dirty: int | None = None
    ignored_local: list[str] = field(
        default_factory=list
    )  # git-ignored files that are not build output
    unpushed: int | None = None
    merged_default: bool | None = None
    default_branch: str = "main"
    upstream: str | None = None  # the short branch name on origin this lane tracks
    upstream_gone: bool | None = None
    idle_minutes: float | None = None
    pr: PrView = field(default_factory=PrView)


def decide(f: Facts, *, prune: bool) -> tuple[str, str]:
    """(action, reason).  action is remove, keep or skip.  Pure; the order is the order of the docstring."""
    if f.kind == "managed":
        return "skip", "harness-managed worktree (its app cleans it up)"
    if f.kind not in ("lane", "review"):
        return "skip", "not a lane or review checkout"
    if f.prunable:
        return "keep", "folder is missing (git lists it as prunable)"
    if f.locked:
        return "keep", "locked (git worktree lock)"
    if f.owner_conflict:
        return "keep", f"lane.json names seat {f.owner_conflict}"
    if f.processes is None:
        return "keep", "could not check for processes using it (lsof failed)"
    if f.processes:
        pid, cmd = f.processes[0]
        more = f" and {len(f.processes) - 1} more" if len(f.processes) > 1 else ""
        return "keep", f"in use: process {pid} ({cmd or '?'}){more} has its cwd inside"
    if f.git_ops:
        return "keep", "git operation in progress (" + ", ".join(f.git_ops) + ")"
    if f.keep_marker:
        return "keep", ".janitor-keep marker present"
    if f.dirty is None:
        return "keep", "git status failed"
    if f.dirty:
        return "keep", f"{f.dirty} uncommitted or untracked file{'s' if f.dirty != 1 else ''}"
    if f.ignored_local:
        shown = ", ".join(f.ignored_local[:3])
        more = len(f.ignored_local) - 3
        return "keep", "ignored local state: " + shown + (f" (+{more} more)" if more > 0 else "")
    if not f.pr.found:
        return "keep", "PR lookup failed (gh)"
    if f.kind == "review":
        state = f.pr.review_state
        if prune and state == "OPEN":
            return "keep", "review checkout of an open PR"
        if prune and state not in ("MERGED", "CLOSED"):
            return "keep", f"review checkout; PR state {state or 'unknown'}"
    elif f.pr.open_number is not None:
        return "keep", f"PR #{f.pr.open_number} is open"
    # origin/<default> is never deleted or rewound, so HEAD in it is pushed even when its new tip was not
    # fetched here (the live-sha count drops a sha that is not an object here).
    if not (f.pr.covers_head or f.unpushed == 0 or f.merged_default):
        if f.unpushed is None:
            return "keep", "could not count unpushed commits"
        where = "on no remote ref" + (
            " (the deleted upstream's stale local copy does not count)" if f.upstream_gone else ""
        )
        return "keep", (
            f"{f.unpushed} commit{'s' if f.unpushed != 1 else ''} {where}, and no merged or "
            "closed PR holds HEAD"
        )
    done = []
    if f.kind == "review":
        done.append(f"review checkout, PR {(f.pr.review_state or 'unknown').lower()}")
    else:
        if f.pr.done_number is not None:
            done.append(f"PR #{f.pr.done_number} {f.pr.done_state.lower()}")
        if f.upstream_gone:
            done.append(f"upstream {f.upstream} deleted on origin")
        if f.merged_default:
            done.append(f"merged into origin/{f.default_branch}")
    if not done:
        if f.pr.stale_number is not None:
            return "keep", (
                f"not done: PR #{f.pr.stale_number} used this branch name, but its head does not hold HEAD"
            )
        return (
            "keep",
            "not done: no merged or closed PR, not in origin/"
            + f.default_branch
            + ", upstream still on origin",
        )
    if prune:
        if f.idle_minutes is None:
            return "keep", "last activity unknown"
        if f.idle_minutes < RECENT_MINUTES:
            return "keep", f"active in the last {RECENT_MINUTES} minutes"
        only_ancestry = (
            f.kind == "lane"
            and f.pr.done_number is None
            and not f.upstream_gone
            and f.upstream is None
        )
        if only_ancestry and f.idle_minutes < EMPTY_LANE_HOURS * 60:
            return (
                "keep",
                f"no commits of its own and never pushed; kept until idle {EMPTY_LANE_HOURS}h",
            )
    safe = "HEAD is the PR head" if f.pr.covers_head and f.unpushed != 0 else "HEAD on a remote ref"
    return "remove", "; ".join(done) + "; " + safe


# --------------------------------------------------------------------------- gathering facts


@dataclass
class Row:
    repo: str
    path: str
    kind: str
    seat: str | None
    branch: str | None
    head: str | None
    action: str = "keep"
    reason: str = ""
    pr: int | None = None
    removed: bool = False
    branch_deleted: bool | None = None
    note: str = ""


@dataclass
class _Lane:
    app: L.App
    tree: Path
    rec: D.WorktreeRecord
    kind: str
    seat: str | None
    review_pr: int | None
    facts: Facts | None = None


def _say(stream, message: str) -> None:
    lines = message.splitlines() or [""]
    stream.write(f"lane: {lines[0]}\n")
    for line in lines[1:]:
        stream.write(f"{line}\n")


def _lookup_seat(registry: L.Registry, text: str) -> L.Seat | None:
    t = (text or "").strip()
    if not t:
        return None
    seat = registry.seat_by_name(t)
    if seat is None:
        seat = registry.seat_by_suffix(t.lower())
    return seat


def _norm_seat(token: str | None, registry: L.Registry) -> str | None:
    if not token:
        return None
    try:
        return L.normalize_seat(token, registry).lower()
    except Exception:  # an odd token is simply not a seat
        return token.lower()


def _classify(
    ctx: K.Ctx, run: Runner, app: L.App, tree: Path, rec: D.WorktreeRecord
) -> _Lane | None:
    """The lane a worktree record is, or None when it is not under the lanes root (or is under ~/Code)."""
    roots, registry = ctx.roots, ctx.registry
    if K._in_tree(ctx, rec.path, roots.code_root) or not K._in_tree(
        ctx, rec.path, roots.lanes_root
    ):
        return None
    cls = L.classify_location(rec.path, roots)
    name = os.path.basename(rec.path.rstrip(os.sep))
    if cls is L.LocationClass.REVIEW:
        m = _REVIEW.match(name)
        pr = int(m.group(1)) if m else None
        seat = _norm_seat(m.group(2), registry) if (m and m.group(2)) else None
        return _Lane(app, tree, rec, "review", seat, pr)
    if cls is L.LocationClass.LANE_NESTED:
        res = L.explain_lane_name(rec.path, registry, roots)
        seat = res.seat.lower() if res.seat else None
        return _Lane(app, tree, rec, "lane", seat, None)
    if cls is L.LocationClass.MANAGED:
        return _Lane(app, tree, rec, "managed", L.seat_from_branch(rec.branch, registry), None)
    return _Lane(app, tree, rec, "other", None, None)


def _int(res: D.CmdResult) -> int | None:
    try:
        return int(res.out.strip()) if res.ok else None
    except ValueError:
        return None


def _mtime(path: str | os.PathLike[str]) -> float | None:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None


def _epoch_iso(text: object) -> float | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        when = _dt.datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=_dt.timezone.utc)  # noqa: UP017 (3.9 has no datetime.UTC)
    return when.timestamp()


def _processes_inside(
    ctx: K.Ctx, rows: list[tuple[int, str, str]] | None, path: str
) -> list[tuple[int, str]] | None:
    if rows is None:
        return None
    me = os.getpid()
    out = []
    for pid, comm, cwd in rows:
        if pid != me and K._in_tree(ctx, cwd, path):
            out.append((pid, comm))
    return out


def _manifest_seat(manifest: Mapping[str, object] | None, registry: L.Registry) -> str | None:
    """The seat folder name a lane.json names (its `seat` field, else the folder name of its `tag`), or None."""
    if not manifest:
        return None
    suffix, tag = manifest.get("seat"), manifest.get("tag")
    if isinstance(suffix, str) and suffix.strip():
        return suffix.strip().lower()[:40]
    if isinstance(tag, str) and tag.strip():
        seat = registry.seat_by_name(tag.strip())
        return seat.suffix.lower() if seat is not None else tag.strip().lower()[:40]
    return None


def _inspect(
    ctx: K.Ctx,
    run: Runner,
    lane: _Lane,
    *,
    now: float,
    lsof_rows,
    default_branch: str,
    remote_heads: dict[str, str] | None,
) -> Facts:
    """Read every fact about one lane (local git and files only; the PR view is added later)."""
    rec = lane.rec
    f = Facts(kind=lane.kind, seat=lane.seat, default_branch=default_branch)
    if lane.kind not in ("lane", "review"):
        return f
    f.locked = bool(rec.locked)
    if rec.prunable is not None or not os.path.isdir(rec.path):
        f.prunable = True
        return f
    gd_res = run(["git", "rev-parse", "--absolute-git-dir"], cwd=rec.path)
    gitdir = gd_res.out.strip() if gd_res.ok else ""
    manifest: dict | None = None
    if gitdir:
        try:
            with open(os.path.join(gitdir, K.MANIFEST_NAME), encoding="utf-8") as fh:
                data = json.load(fh)
            manifest = data if isinstance(data, dict) else None
        except (OSError, ValueError):
            manifest = None
    owner = _manifest_seat(manifest, ctx.registry)
    if owner is not None:
        if lane.seat is None and lane.kind == "review":
            lane.seat = f.seat = owner  # plain review-pr-<n>: the manifest is the only owner record
        elif lane.seat is not None and owner != lane.seat:
            f.owner_conflict = owner
    f.processes = _processes_inside(ctx, lsof_rows, rec.path)
    f.git_ops = D.git_ops_in_progress(gitdir) if gitdir else ["unreadable git dir"]
    f.keep_marker = os.path.lexists(os.path.join(rec.path, D.KEEP_MARKER))
    st = run(
        ["git", "status", "--porcelain", "--ignored", "--untracked-files=normal"], cwd=rec.path
    )
    f.dirty = _dirty_count(st.out) if st.ok else None
    f.ignored_local = D.split_ignored(st.out)[0] if st.ok else []
    mb = run(
        ["git", "merge-base", "--is-ancestor", "HEAD", f"refs/remotes/origin/{default_branch}"],
        cwd=rec.path,
    )
    f.merged_default = (
        True if mb.ok else (False if (mb.rc == 1 and not mb.timed_out and not mb.error) else None)
    )
    branch = rec.branch if not rec.detached else None
    if branch:
        merge = run(["git", "config", "--get", f"branch.{branch}.merge"], cwd=rec.path)
        remote = run(["git", "config", "--get", f"branch.{branch}.remote"], cwd=rec.path)
        up = merge.out.strip() if merge.ok else ""
        rname = remote.out.strip() if remote.ok else "origin"
        if up.startswith("refs/heads/") and rname == "origin":
            f.upstream = up[len("refs/heads/") :]
            if remote_heads is not None:
                f.upstream_gone = f.upstream not in remote_heads
    # Unpushed means "on no LIVE origin branch": the shas `git ls-remote --heads origin` reported, never a local
    # remote-tracking ref.  Those can be stale (a branch deleted on origin without a `fetch --prune`, the
    # deleted upstream's or any other; a force-pushed branch), and another remote (a peer lane, a scratch bare
    # repo) never vouches for a push.  This tool never fetches.  --ignore-missing drops a live sha that was
    # never fetched here, which can only overcount (the safe side).  Without ls-remote nothing vouches (None),
    # and only a PR that holds HEAD can.
    if remote_heads is not None:
        f.unpushed = _int(
            run(
                [
                    "git",
                    "rev-list",
                    "--count",
                    "--ignore-missing",
                    "HEAD",
                    "--not",
                    *sorted(set(remote_heads.values())),
                ],
                cwd=rec.path,
            )
        )
    times = [
        t
        for t in (
            _mtime(os.path.join(gitdir, n)) if gitdir else None
            for n in ("HEAD", "index", "logs/HEAD")
        )
        if t is not None
    ]
    folder = _mtime(rec.path)
    if folder is not None:
        times.append(folder)
    created = _epoch_iso((manifest or {}).get("created_at"))
    if created is not None:
        times.append(created)
    f.idle_minutes = max(0.0, (now - max(times)) / 60.0) if times else None
    return f


def _dirty_count(porcelain: str) -> int:
    """Tracked edits and untracked files in `git status --porcelain --ignored` (the `!!` ignored lines excluded)."""
    return len([ln for ln in porcelain.splitlines() if ln.strip() and not ln.startswith("!! ")])


def _contains(run: Runner, path: str, oid: str) -> bool:
    """True when the PR head `oid` is a local commit that contains HEAD (no commit of HEAD is outside it)."""
    if not _SHA.match(oid or ""):
        return False
    if not run(["git", "rev-parse", "--verify", "-q", f"{oid}^{{commit}}"], cwd=path).ok:
        return False
    return _int(run(["git", "rev-list", "--count", "HEAD", "--not", oid], cwd=path)) == 0


def _pr_view(
    run: Runner,
    lane: _Lane,
    prs: list[dict] | None,
    fallback: Callable[[str], list[dict] | None],
    truncated: bool,
) -> PrView:
    rec = lane.rec
    head = rec.head or ""
    if lane.kind == "review":
        if prs is None:
            return PrView(found=False)
        hit = [p for p in prs if p.get("number") == lane.review_pr]
        if not hit:
            # not in the bulk list (an old PR in a big repo): no way to ask by number on this allowlist
            return PrView(found=True, review_state="UNKNOWN")
        p = hit[0]
        state = str(p.get("state") or "")
        covers = (p.get("headRefOid") == head) or _contains(
            run, rec.path, str(p.get("headRefOid") or "")
        )
        return PrView(
            found=True,
            review_state=state,
            covers_head=covers,
            done_number=lane.review_pr if state in ("MERGED", "CLOSED") else None,
            done_state=state,
        )
    names = [
        n
        for n in (
            (rec.branch if not rec.detached else None),
            lane.facts.upstream if lane.facts else None,
        )
        if n
    ]
    names = list(dict.fromkeys(names))

    def matches(pool: list[dict]) -> list[dict]:
        return [
            p
            for p in pool
            if p.get("headRefName") in names or (head and p.get("headRefOid") == head)
        ]

    cands: list[dict] = matches(prs) if prs is not None else []
    if not cands and (prs is None or truncated):
        found_any = prs is not None
        for n in names:
            more = fallback(n)
            if more is None:
                continue
            found_any = True
            cands.extend(matches(more))
        if not found_any:
            return PrView(found=False)
    view = PrView(found=True)
    opens = [p for p in cands if p.get("state") == "OPEN"]
    if opens:
        view.open_number = max(int(p.get("number") or 0) for p in opens)
        return view
    done = [p for p in cands if p.get("state") in ("MERGED", "CLOSED")]
    if not done:
        return view
    # Only a PR whose head IS HEAD or contains it says this lane's work is done.  A PR that merely used the same
    # branch name (a reused slug, or commits added after the merge) is no evidence, so a lane opened under an old
    # name keeps the 24-hour hold for an unused lane.  Containment counts only when the lane has commits of its
    # own (HEAD not in origin/<default>): an old PR whose head sits on top of today's origin/<default> contains
    # every fresh lane's HEAD and holds none of its work.
    covering = [p for p in done if head and p.get("headRefOid") == head]
    own_work = lane.facts is not None and lane.facts.merged_default is False
    if not covering and own_work:
        covering = [p for p in done if _contains(run, rec.path, str(p.get("headRefOid") or ""))]
    if not covering:
        view.stale_number = max(int(p.get("number") or 0) for p in done) or None
        return view
    pick = max(covering, key=lambda p: (p.get("state") == "MERGED", int(p.get("number") or 0)))
    view.done_number = int(pick.get("number") or 0) or None
    view.done_state = str(pick.get("state") or "")
    view.covers_head = True
    return view


def _bulk_prs(run: Runner, repo: str, tree: Path) -> tuple[list[dict] | None, bool]:
    res = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--limit",
            str(GH_BULK_LIMIT),
            "--json",
            GH_FIELDS,
        ],
        cwd=tree,
    )
    if not res.ok:
        return None, False
    try:
        data = json.loads(res.out or "[]")
    except ValueError:
        return None, False
    if not isinstance(data, list):
        return None, False
    return data, len(data) >= GH_BULK_LIMIT


def _head_prs(run: Runner, repo: str, tree: Path, name: str) -> list[dict] | None:
    if not K.valid_ref_name(name):
        return None
    res = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "all",
            "--head",
            name,
            "--limit",
            "20",
            "--json",
            GH_FIELDS,
        ],
        cwd=tree,
    )
    if not res.ok:
        return None
    try:
        data = json.loads(res.out or "[]")
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _default_branch(run: Runner, tree: Path) -> str:
    res = run(["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"], cwd=tree)
    out = res.out.strip()
    if res.ok and out.startswith("origin/") and K.valid_ref_name(out[len("origin/") :]):
        return out[len("origin/") :]
    return "main"


def _remote_heads(run: Runner, tree: Path) -> dict[str, str] | None:
    """{branch: sha} of every branch that exists on origin right now, or None when ls-remote failed."""
    res = run(["git", "ls-remote", "--heads", "origin"], cwd=tree)
    if not res.ok:
        return None
    heads: dict[str, str] = {}
    for line in res.out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].startswith("refs/heads/") and _SHA.match(parts[0]):
            heads[parts[1][len("refs/heads/") :]] = parts[0]
    return heads


def _owner_repo(run: Runner, app: L.App, tree: Path) -> str | None:
    res = run(["git", "config", "--get", "remote.origin.url"], cwd=tree)
    found = D.parse_owner_repo(res.out.strip()) if res.ok else None
    return found or (app.owner_repo or None)


def _lane_apps(ctx: K.Ctx, repo_filter: str | None) -> list[L.App]:
    if repo_filter:
        app = K.resolve_app(ctx.registry, repo_filter)
        K._require_lane_app(app)
        return [app]
    out = []
    for app in ctx.registry.apps:
        if (
            app.registered
            and app.integration_dir_name
            and (Path(ctx.roots.code_root) / app.integration_dir_name).is_dir()
        ):
            out.append(app)
    return out


# --------------------------------------------------------------------------- the run


@dataclass
class _Plan:
    rows: list[Row]
    lanes: list[_Lane]
    errors: list[str]
    others: int
    notes: list[str]


def _gather(
    ctx: K.Ctx,
    *,
    apps: list[L.App],
    seats: set | None,
    now: float,
    prune: bool,
    only_path: str | None = None,
) -> tuple[_Plan, Runner]:
    run = Runner(ctx.env, write_root=ctx.roots.lanes_root, fold=ctx.fold(), exec_fn=ctx.exec_fn)
    errors: list[str] = []
    notes: list[str] = []
    lsof = run(["lsof", "-d", "cwd", "-Fpcn"])
    # lsof exits 1 when some process could not be read but still prints the rest; only no output at all is a failure.
    lsof_rows = D.parse_lsof(lsof.out) if (lsof.ok or (lsof.rc == 1 and lsof.out.strip())) else None
    if lsof_rows is None:
        errors.append(
            f"lsof failed ({K._why(lsof)}); nothing is removed while it cannot be checked"
        )
    rows: list[Row] = []
    lanes_out: list[_Lane] = []
    others = 0
    for app in apps:
        tree = Path(os.path.realpath(Path(ctx.roots.code_root) / app.integration_dir_name))
        try:
            recs = K._worktrees(run, tree)
        except K.ToolFailure as exc:
            errors.append(f"{app.lane_dir}: {exc}")
            continue
        lanes: list[_Lane] = []
        for rec in recs[1:]:
            if only_path is not None and ctx.key(rec.path) != ctx.key(only_path):
                continue
            lane = _classify(ctx, run, app, tree, rec)
            if lane is None:
                continue
            if seats is not None and (lane.kind == "review" and lane.seat is None):
                pass  # the manifest may name the seat; decided after inspection
            elif seats is not None and (lane.seat or "") not in seats:
                others += 1
                continue
            lanes.append(lane)
        if not lanes:
            continue
        default = _default_branch(run, tree)
        heads = _remote_heads(run, tree)
        if heads is None:
            errors.append(
                f"{app.lane_dir}: git ls-remote --heads origin failed; no live origin branch can vouch "
                "for a push or a deleted upstream, so only a lane whose HEAD a merged or closed PR "
                "holds can be removed"
            )
        repo = _owner_repo(run, app, tree)
        prs, truncated = _bulk_prs(run, repo, tree) if repo else (None, False)
        if prs is None:
            errors.append(
                f"{app.lane_dir}: gh pr list failed for {repo or 'an unknown GitHub repo'}"
            )

        def work(
            ln: _Lane,
            default: str = default,
            heads: dict[str, str] | None = heads,
            prs: list[dict] | None = prs,
            repo: str | None = repo,
            tree: Path = tree,
            truncated: bool = truncated,
        ) -> _Lane:
            ln.facts = _inspect(
                ctx,
                run,
                ln,
                now=now,
                lsof_rows=lsof_rows,
                default_branch=default,
                remote_heads=heads,
            )
            if ln.kind in ("lane", "review") and not ln.facts.prunable:

                def fallback(name: str) -> list[dict] | None:
                    return _head_prs(run, repo, tree, name) if repo else None

                ln.facts.pr = _pr_view(run, ln, prs, fallback, truncated)
            return ln

        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            done = list(pool.map(work, lanes))
        for ln in done:
            if seats is not None and ln.kind == "review" and (ln.seat or "") not in seats:
                others += 1
                continue
            action, reason = decide(ln.facts, prune=prune) if ln.facts else ("keep", "no facts")
            pr = ln.facts.pr.open_number or ln.facts.pr.done_number if ln.facts else None
            rows.append(
                Row(
                    app.lane_dir,
                    ln.rec.path,
                    ln.kind,
                    ln.seat,
                    ln.rec.branch,
                    ln.rec.head,
                    action,
                    reason,
                    pr,
                )
            )
            lanes_out.append(ln)
    return _Plan(rows, lanes_out, errors, others, notes), run


def _apply(ctx: K.Ctx, run: Runner, plan: _Plan, seats: set | None) -> None:
    by_tree: dict[str, list[tuple[Row, _Lane]]] = {}
    for row, ln in zip(plan.rows, plan.lanes):
        if row.action == "remove":
            by_tree.setdefault(str(ln.tree), []).append((row, ln))
    links = L.external_links(ctx.roots)
    dangling = [lk for lk in links if lk.status == "dangling"]
    for tree_s, items in by_tree.items():
        tree, app = Path(tree_s), items[0][1].app
        with K._repo_lock(ctx, run, tree):
            for row, ln in items:
                st = run(
                    ["git", "status", "--porcelain", "--ignored", "--untracked-files=normal"],
                    cwd=ln.rec.path,
                )
                if not st.ok or _dirty_count(st.out) or D.split_ignored(st.out)[0]:
                    row.action, row.reason = "keep", "changed since it was inspected (git status)"
                    continue
                res = run(["git", "worktree", "remove", ln.rec.path], cwd=tree)
                if not res.ok:
                    row.action, row.reason = "error", "git worktree remove failed: " + K._why(res)
                    plan.errors.append(f"{row.path}: {row.reason}")
                    continue
                row.removed = True
                if ln.rec.branch and not ln.rec.detached:
                    bd = run(["git", "branch", "-d", ln.rec.branch], cwd=tree)
                    row.branch_deleted = bd.ok
                    if not bd.ok:
                        row.note = "branch kept: git branch -d refused (" + K._why(bd) + ")"
            _maybe_prune(ctx, run, app, tree, seats, dangling, plan)


def _maybe_prune(
    ctx: K.Ctx, run: Runner, app: L.App, tree: Path, seats: set | None, dangling, plan: _Plan
) -> None:
    """`git worktree prune` only for stale entries that are lanes of the chosen seat, and never while a lanes
    symlink is dangling (the external disk is not mounted)."""
    try:
        recs = K._worktrees(run, tree)
    except K.ToolFailure as exc:
        plan.errors.append(str(exc))
        return
    stale = [r for r in recs[1:] if r.prunable is not None]
    if not stale:
        return
    if dangling:
        plan.notes.append(
            f"PRUNE-SKIPPED in {tree.name}: {dangling[0].path} is a dangling symlink (is the external "
            "disk mounted?); git worktree prune would drop the entries of every lane on it"
        )
        return
    for r in stale:
        lane = _classify(ctx, run, app, tree, r)
        if (
            lane is None
            or lane.kind != "lane"
            or (seats is not None and (lane.seat or "") not in seats)
        ):
            plan.notes.append(
                f"PRUNE-SKIPPED in {tree.name}: stale entry {r.path} is not a lane of the chosen seat"
            )
            return
    res = run(["git", "worktree", "prune"], cwd=tree)
    if res.ok:
        plan.notes.append(
            f"git worktree prune in {tree.name}: {len(stale)} stale entr{'y' if len(stale) == 1 else 'ies'}"
        )
    else:
        plan.errors.append(f"git worktree prune failed in {tree}: {K._why(res)}")


def _render(ctx: K.Ctx, plan: _Plan, *, apply: bool, title: str) -> str:
    lines = [title, ""]
    head = ("DECISION", "REPO", "LANE", "BRANCH", "REASON")
    table = []
    for r in sorted(plan.rows, key=lambda r: (r.action != "remove", r.repo.lower(), r.path)):
        action = r.action
        if apply and r.action == "remove":
            action = "removed" if r.removed else "remove"
        reason = r.reason + (f"  [{r.note}]" if r.note else "")
        table.append((action, r.repo, os.path.basename(r.path), r.branch or "(detached)", reason))
    widths = [
        max(len(head[i]), *(len(t[i]) for t in table)) if table else len(head[i]) for i in range(4)
    ]
    fmt = "  ".join("{:<%d}" % w for w in widths) + "  {}"
    lines.append(fmt.format(*head))
    for t in table:
        lines.append(fmt.format(*t))
    counts: dict[str, int] = {}
    for r in plan.rows:
        counts[r.action] = counts.get(r.action, 0) + 1
    removed = sum(1 for r in plan.rows if r.removed)
    lines.append("")
    summary = (
        f"{len(plan.rows)} lane(s): {counts.get('remove', 0)} removable, {counts.get('keep', 0)} kept, "
        f"{counts.get('skip', 0)} skipped"
    )
    if apply:
        summary += f"; {removed} removed, {counts.get('error', 0)} failed"
    else:
        summary += ".  Dry run: nothing changed; add --apply to remove the removable ones"
    if plan.others:
        summary += f"; {plan.others} lane(s) of other seats not considered"
    lines.append(summary)
    for n in plan.notes:
        lines.append("note: " + n)
    for e in plan.errors:
        lines.append("error: " + e)
    return "\n".join(lines) + "\n"


def _json(plan: _Plan, *, apply: bool, seats: set | None, command: str) -> str:
    body = {
        "schema": 1,
        "command": command,
        "apply": apply,
        "seats": sorted(seats) if seats is not None else "all",
        "lanes": [asdict(r) for r in plan.rows],
        "others_not_considered": plan.others,
        "summary": {
            a: sum(1 for r in plan.rows if r.action == a)
            for a in ("remove", "keep", "skip", "error")
        },
        "removed": sum(1 for r in plan.rows if r.removed),
        "notes": plan.notes,
        "errors": plan.errors,
    }
    return json.dumps(body, indent=2) + "\n"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(K.EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _parser(command: str) -> _Parser:
    if command == "retire":
        p = _Parser(
            prog="lane retire",
            description="Remove one lane of your seat once its work is safe on GitHub.  "
            "Run it from outside the lane.  Exit 0 removed (or would be, with --dry-run), 64 refused or kept, "
            "69 git, gh or lsof failed.",
        )
        p.add_argument("target", help="the lane path, or its slug (the part after <seat>-)")
        p.add_argument("--repo", metavar="APP", help="the repo when a slug alone is ambiguous")
        p.add_argument(
            "--dry-run", action="store_true", help="say what would happen; change nothing"
        )
        p.add_argument("--json", action="store_true", help="print the decision as JSON")
        return p
    p = _Parser(
        prog="lane prune",
        description="List a seat's lanes with a decision and a reason for each; remove the "
        "removable ones with --apply.  Exit 0 ok, 64 usage or refusal, 69 git, gh or lsof failed.",
    )
    p.add_argument("--seat", metavar="NAME", help="seat tag or folder name (default: AGENT_SEAT)")
    p.add_argument(
        "--all-seats",
        action="store_true",
        help="allow a --seat other than yours; alone, every seat",
    )
    p.add_argument("--repo", metavar="APP", help="only this repo (name, folder, prefix or acronym)")
    p.add_argument(
        "--apply", action="store_true", help="remove the removable lanes (the default is a dry run)"
    )
    p.add_argument("--json", action="store_true", help="print the table as JSON")
    return p


def _find_by_slug(
    ctx: K.Ctx, run: Runner, seat: L.Seat, slug: str, repo: str | None
) -> tuple[L.App, str]:
    try:
        L.validate_slug(slug)
    except L.LayoutError as exc:
        raise K.Refusal(str(exc))
    hits: list[tuple[L.App, str]] = []
    for app in _lane_apps(ctx, repo):
        try:
            path = L.expected_lane_path(app, seat.suffix, slug, ctx.roots, ctx.registry)
        except L.LayoutError:
            continue
        if os.path.lexists(path):
            hits.append((app, str(path)))
    if not hits:
        raise K.Refusal(
            f"no lane {seat.suffix}-{slug} of seat {seat.name} under {ctx.roots.lanes_root}"
            + (f"/{repo}" if repo else "")
            + ".  Give its path, or check the slug and --repo."
        )
    if len(hits) > 1:
        raise K.Refusal(
            f"slug {slug!r} matches more than one lane; pass --repo or the path:\n"
            + "\n".join(f"  {p}" for _, p in hits)
        )
    return hits[0]


def _find_by_path(ctx: K.Ctx, target: str) -> tuple[L.App, str]:
    path = L.resolve_path(target, ctx.roots)
    root = L.lane_root(path, ctx.roots)
    if root is None or not K._in_tree(ctx, str(root), ctx.roots.lanes_root):
        raise K.Refusal(
            f"{target} is not a lane under {ctx.roots.lanes_root}; lane retire removes nothing else."
        )
    if ctx.key(str(root)) != ctx.key(path):
        raise K.Refusal(f"{target} is inside the lane {root}; give the lane folder itself.")
    folder = root.parent.name
    app = ctx.registry.app_by_lane_dir(folder, case_insensitive=True)
    if app is None:
        # a lane in an old prefix folder (lanes/fleet/claude-x)
        app = ctx.registry.app_by_prefix(folder.lower())
    if app is None or not app.integration_dir_name:
        raise K.Refusal(f"cannot tell which registered repo {root} belongs to.")
    return app, str(root)


def _retire(ctx: K.Ctx, args: argparse.Namespace, now: float) -> int:
    seat = K.resolve_seat(ctx.env, ctx.registry)
    run = Runner(ctx.env, write_root=ctx.roots.lanes_root, fold=ctx.fold(), exec_fn=ctx.exec_fn)
    t = args.target.strip()
    if "/" in t or t.startswith((".", "~")):
        app, path = _find_by_path(ctx, t)
        if args.repo:
            want = K.resolve_app(ctx.registry, args.repo)
            if want.name != app.name:
                raise K.Refusal(f"{path} belongs to {app.name}, not {want.name}.")
    else:
        app, path = _find_by_slug(ctx, run, seat, t, args.repo)
    K._require_lane_app(app)
    seats = {seat.suffix.lower()}
    plan, run = _gather(ctx, apps=[app], seats=None, now=now, prune=False, only_path=path)
    if not plan.rows:
        raise K.Refusal(
            f"{path} is not a worktree that git lists in {Path(ctx.roots.code_root) / app.integration_dir_name}; "
            "lane retire removes only registered lanes."
        )
    row = plan.rows[0]
    if (row.seat or "") not in seats:
        raise K.Refusal(
            f"{path} belongs to seat {row.seat or 'unknown'}, not {seat.name}; lane retire removes only "
            "your own lanes."
        )
    if row.action == "remove" and not args.dry_run:
        _apply(ctx, run, plan, seats)
    if args.json:
        ctx.out.write(_json(plan, apply=not args.dry_run, seats=seats, command="retire"))
    if row.action == "error":
        _say(ctx.err, f"error: {row.reason}")
        return K.EXIT_TOOL
    if row.action != "remove":
        _say(ctx.err, f"refused: kept {path}: {row.reason}")
        for e in plan.errors:
            _say(ctx.err, "error: " + e)
        return (
            K.EXIT_TOOL
            if (plan.errors and row.reason.startswith(("PR lookup failed", "could not check")))
            else K.EXIT_USAGE
        )
    if args.dry_run:
        _say(ctx.err, f"dry run, nothing changed.  Would remove {path} ({row.reason})")
        if not args.json:
            ctx.out.write(f"{path}\n")
        return K.EXIT_OK
    _say(
        ctx.err,
        f"removed {path} ({row.reason})"
        + (f"; {row.note}" if row.note else "")
        + ("; deleted branch " + str(row.branch) if row.branch_deleted else ""),
    )
    for n in plan.notes:
        _say(ctx.err, "note: " + n)
    if not args.json:
        ctx.out.write(f"{path}\n")
    return K.EXIT_OK


def _prune(ctx: K.Ctx, args: argparse.Namespace, now: float) -> int:
    registry = ctx.registry
    own: L.Seat | None = None
    raw_own = (ctx.env.get(K.ENV_SEAT) or "").strip()
    if raw_own:
        own = K.resolve_seat(ctx.env, registry)
    seats: set | None
    if args.seat:
        target = _lookup_seat(registry, args.seat)
        if target is None:
            live = ", ".join(s.name for s in registry.seats if not s.retired)
            raise K.Refusal(
                f"--seat {args.seat!r} is not a seat tag or folder name in fleet-apps.json ({live})."
            )
        if own is None and not args.all_seats:
            raise K.Refusal(
                f"{K.ENV_SEAT} is not set, so lane cannot tell whether {target.name} is your seat.  Set it, "
                "or add --all-seats to look at another seat's lanes on purpose."
            )
        if own is not None and target.suffix.lower() != own.suffix.lower() and not args.all_seats:
            raise K.Refusal(
                f"--seat {target.name} is not your seat ({own.name}).  Add --all-seats to look at another "
                "seat's lanes on purpose; never remove a peer's lane without the owner's word."
            )
        seats = {target.suffix.lower()}
    elif args.all_seats:
        seats = None
    else:
        if own is None:
            raise K.Refusal(
                f"{K.ENV_SEAT} is not set.  Set it to your seat tag, or pass --seat NAME."
            )
        seats = {own.suffix.lower()}
    apps = _lane_apps(ctx, args.repo)
    plan, run = _gather(ctx, apps=apps, seats=seats, now=now, prune=True)
    if args.apply:
        _apply(ctx, run, plan, seats)
    who = "every seat" if seats is None else "seat " + ", ".join(sorted(seats))
    title = (
        f"lane prune ({who}; {'APPLY' if args.apply else 'dry run'}; "
        f"{'repo ' + apps[0].lane_dir if args.repo else str(len(apps)) + ' repos'})"
    )
    ctx.out.write(
        _json(plan, apply=args.apply, seats=seats, command="prune")
        if args.json
        else _render(ctx, plan, apply=args.apply, title=title)
    )
    return K.EXIT_TOOL if plan.errors else K.EXIT_OK


def main(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    clock: Callable[[], _dt.datetime] | None = None,
    exec_fn: K.ExecFn | None = None,
    stdout=None,
    stderr=None,
) -> int:
    """`argv` starts with `retire` or `prune`.  Called from `lane.main`."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    command, rest = argv[0], list(argv[1:])
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            args = _parser(command).parse_args(rest)
        except SystemExit as exc:
            return (
                exc.code
                if isinstance(exc.code, int)
                else (K.EXIT_OK if exc.code is None else K.EXIT_USAGE)
            )
    stamp = clock or (lambda: _dt.datetime.now(_dt.timezone.utc))  # noqa: UP017 (3.9 has no datetime.UTC)
    now = stamp()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)  # noqa: UP017 (3.9 has no datetime.UTC)
    try:
        try:
            registry = L.load_registry(env=env)
            home = env.get("HOME") or os.path.expanduser("~")
            roots = L.make_roots(home, env, registry=registry)
        except (OSError, ValueError) as exc:
            raise K.Refusal(f"cannot read the registry: {exc}")
        ctx = K.Ctx(env, registry, roots, stamp, exec_fn, out, err)
        for note in roots.warnings:
            ctx.say(f"warning: {note}")
        if command == "retire":
            return _retire(ctx, args, now.timestamp())
        return _prune(ctx, args, now.timestamp())
    except K.Refusal as exc:
        _say(err, "refused: " + str(exc))
        return K.EXIT_USAGE
    except K.ToolFailure as exc:
        _say(err, "error: " + str(exc))
        return K.EXIT_TOOL
    except K.CommandRefused as exc:
        _say(err, f"internal error: {exc}")
        return K.EXIT_INTERNAL
