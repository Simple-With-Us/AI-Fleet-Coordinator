#!/usr/bin/env python3
"""Move the existing lanes to the layout v2 tree (docs/protocols/lanes-v2-migration.md).

    python3 scripts/lanes-v2-migrate.py                      # dry run: print the plan, change nothing
    python3 scripts/lanes-v2-migrate.py --apply              # do it
    python3 scripts/lanes-v2-migrate.py --json               # the plan as JSON (a dry run unless --apply)
    python3 scripts/lanes-v2-migrate.py --include-codex      # also move Codex's worktrees (~/.codex/worktrees and
                                                             # lanes/_managed/codex) to lanes/_codex
    python3 scripts/lanes-v2-migrate.py --remove-links       # list the old-path symlinks that are due (dry run)
    python3 scripts/lanes-v2-migrate.py --remove-links --apply

Run it from outside the lanes tree (`cd ~`), never from a lane: a lane with a process in it is skipped,
and so is the lane this script itself lives in.

What it does, per lane found under ~/apps/lanes (the disk is read at run time, never a saved list):
  * a LINKED WORKTREE moves with `git worktree move` (git rewrites its own bookkeeping), a FULL CLONE with
    a plain rename; the new place is ~/apps/lanes/<Repo>/<same name>, where <Repo> comes from git (the
    folder of the repo the worktree belongs to under ~/Code, or the clone's origin), not from the old folder
    name.  A name that is not lowercase kebab loses a trailing -<Repo> when that makes it valid.
  * a symlink is left at the old path pointing at the new one, and logged with a removal date (7 days out by
    default), so a shell or an editor that still has the old path keeps working.  `--remove-links` deletes
    only those symlinks, only when due, only when they still point where they were made to point.
  * an old folder that differs from <Repo> only in letter case (botfleet -> BotFleet; one folder on APFS) is
    renamed as a whole (one rename(2), so no lane leaves its path; a temporary name only as a fallback),
    and every lane in it is repaired with
    `git worktree repair`.  This happens only when EVERY checkout in the folder is safe to touch; otherwise
    the whole folder is deferred.  No symlink is made for it: the two spellings are one path.
  * empty leftovers (lanes/_managed/*, lanes/_review/*, lanes/_notes) are removed with rmdir, which refuses
    anything that is not empty.  A `.DS_Store` is the only file deleted.
  * lanes/_managed/fleet/agent-sync-runtime is live infrastructure and is always skipped.

What it refuses (skip, with the reason in the plan; run again when the cause is gone):
  * a lane with a process whose working directory is inside it, or when lsof cannot be read at all
  * uncommitted work (tracked or untracked), a locked worktree, a prunable one, a detached state git
    cannot report, a worktree with submodules (git refuses to move those), a target that already exists
  * a linked worktree, submodule or clone-with-worktrees below the lane (a gitignored .claude/worktrees/<name>
    reads clean in `git status`, and moving its parent would break its link back to the repository)
  * a symlink (already migrated), anything that is not a checkout, a repo it cannot place
  Nothing is ever removed with rm -rf, git is only asked `worktree move` and `worktree repair` besides
  read-only commands, and every path it writes is checked to be inside the lanes root (or, for Codex,
  ~/.codex/worktrees on the way out).

External lanes (docs/protocols/lane-map.md): a lanes/<Repo> folder may be a symlink onto the external disk
(/Volumes/External/Lanes/<Repo>, FLEET_LANES_EXTERNAL_ROOT).  Such a folder is read like any other, so its lanes
report already-correct.  A move INTO one from a folder on the internal disk is planned as a skip: git and rename(2)
cannot move a worktree across disks, so that lane stays where it is until it retires.  A link whose target is missing
(the disk is not mounted) or that points anywhere but the external disk is noted and its folder is not read.

Default is a dry run.  The log (`<lanes root>/.lanes-v2-migration.json`) records every link made and every
step taken.  With --json the one JSON document is the only thing on stdout; progress goes to stderr.
Exit codes: 0 ok (skips are not failures), 2 a step failed with --apply or the plan could not be read,
64 usage error.  Pause the disk janitor while --apply runs (docs/protocols/lanes-v2-migration.md).

Python 3.9 safe, standard library only, plus the fleet_lanes package beside this file.
Tests: scripts/fleet_lanes/tests/test_migrate_v2.py.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from fleet_lanes import doctor as D  # noqa: E402
from fleet_lanes import layout as L  # noqa: E402

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_USAGE = 64

LOG_NAME = ".lanes-v2-migration.json"
GRACE_DAYS = 7
RUNTIME_NAME = "agent-sync-runtime"

# Actions in a plan.
MOVE = "move"                  # a lane to its new folder, with a symlink left behind
CODEX = "move-codex"           # ~/.codex/worktrees/<slug>/<Repo> to lanes/_codex/<slug>/<Repo>
BUCKET = "rename-folder"       # a case-only folder rename (one path on APFS), no symlink
RMDIR = "remove-empty"         # an empty leftover folder
OK = "already-correct"
SKIP = "skip"                  # refused: the reason says why, and a rerun may allow it
DEFER = "defer"                # a folder rename that waits for its lanes to be safe
NOTE = "note"                  # nothing to do, said so

CODEX_WHY = ("Codex worktrees move only with --include-codex, with the Codex app closed and its "
             "git-worktree-root already changed (docs/protocols/lanes-v2-migration.md)")
CODEX_MARKER = ".codex-worktree-name"
_KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_OLD_REVIEW = re.compile(r"^pr-([0-9]{1,9})(?:-([a-z0-9]+(?:-[a-z0-9]+)*))?$")
_SAFE_GIT = {
    ("rev-parse", "--absolute-git-dir"), ("rev-parse", "--git-common-dir"), ("rev-parse", "--show-toplevel"),
    ("status", "--porcelain"), ("worktree", "list", "--porcelain"), ("config", "--get", "remote.origin.url"),
    ("rev-parse", "--abbrev-ref", "HEAD"),
}


class MigrateError(Exception):
    """A step that cannot go on; the message is shown and the item fails."""


# --------------------------------------------------------------------------- git, restricted

def check_git(argv: Sequence[str]) -> None:
    """Raise MigrateError unless `argv` (without the leading `git`) is a read-only command we use, or one
    of the two writes: `worktree move OLD NEW` and `worktree repair PATH...`.  No force flag is accepted."""
    a = tuple(argv)
    if a in _SAFE_GIT:
        return
    if len(a) == 4 and a[:2] == ("worktree", "move") and not a[2].startswith("-") and not a[3].startswith("-"):
        return
    if len(a) >= 3 and a[:2] == ("worktree", "repair") and all(not p.startswith("-") for p in a[2:]):
        return
    raise MigrateError("git command not allowed: " + " ".join(a[:4]))


def run_git(cwd: str, argv: Sequence[str]) -> "tuple[int, str, str]":
    check_git(argv)
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") or k in ("GIT_SSH_COMMAND",)}
    env.update({"GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0", "GIT_PAGER": "cat"})
    try:
        proc = subprocess.run(["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", cwd, *argv],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 255, "", f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout.decode("utf-8", "replace"), D.redact_text(proc.stderr.decode("utf-8", "replace"))


# --------------------------------------------------------------------------- context and plan

@dataclass
class Ctx:
    home: str
    roots: L.Roots
    registry: L.Registry
    lsof: "Callable[[], list[tuple[int, str, str]] | None]"
    today: _dt.date
    grace_days: int = GRACE_DAYS
    include_codex: bool = False
    log_path: str = ""
    git: "Callable[[str, Sequence[str]], tuple[int, str, str]]" = run_git
    self_paths: "tuple[str, ...]" = ()
    _cwds: "list[tuple[int, str, str]] | None" = field(default=None, repr=False)
    _cwds_read: bool = field(default=False, repr=False)

    @property
    def lanes(self) -> str:
        return os.fspath(self.roots.lanes_root)

    def fold(self, text: str) -> str:
        return text.casefold() if self.roots.case_insensitive else text

    def real(self, path: str) -> str:
        return os.path.realpath(path)

    def cwds(self) -> "list[tuple[int, str, str]] | None":
        if not self._cwds_read:
            self._cwds_read = True
            self._cwds = self.lsof()
        return self._cwds

    def refresh(self) -> None:
        """Forget what was learned about running processes; the next check asks again."""
        self._cwds_read = False
        self._cwds = None


@dataclass
class Item:
    action: str
    old: str
    new: "str | None" = None
    kind: str = ""
    reasons: "list[str]" = field(default_factory=list)
    notes: "list[str]" = field(default_factory=list)
    members: "list[str]" = field(default_factory=list)      # BUCKET: the checkouts inside
    main: "str | None" = None                                # the repo a linked worktree belongs to
    clone: bool = False
    extra: dict = field(default_factory=dict)                # BUCKET: renames inside the folder, and their repos
    result: str = ""                                         # filled by apply: done | failed | skipped
    detail: str = ""

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], "", False)} | {"action": self.action}


@dataclass
class Facts:
    path: str
    is_checkout: bool = False
    is_clone: bool = False
    main: "str | None" = None
    branch: "str | None" = None
    dirty: int = 0
    locked: bool = False
    prunable: bool = False
    detached: bool = False
    origin: "str | None" = None
    problems: "list[str]" = field(default_factory=list)


# --------------------------------------------------------------------------- reading the disk

def _under(child: str, parent: str, fold: Callable[[str], str]) -> bool:
    c, p = fold(child), fold(parent).rstrip(os.sep)
    return c == p or c.startswith(p + os.sep)


def processes_in(ctx: Ctx, path: str) -> "list[str] | None":
    """Commands (pid) whose working directory is inside `path`; None when lsof could not be read."""
    rows = ctx.cwds()
    if rows is None:
        return None
    targets = {path, ctx.real(path)}
    hits = [f"{comm}({pid})" for pid, comm, cwd in rows if any(_under(cwd, t, ctx.fold) for t in targets)]
    for mine in ctx.self_paths:
        if any(_under(mine, t, ctx.fold) for t in targets):
            hits.append("this script or its shell")
    return hits


def inspect(ctx: Ctx, path: str) -> Facts:
    """Read-only facts about one checkout: kind, owner repo, branch, dirt, lock."""
    f = Facts(path)
    dot_git = os.path.join(path, ".git")
    if not os.path.lexists(dot_git):
        return f
    f.is_checkout = True
    f.is_clone = os.path.isdir(dot_git) and not os.path.islink(dot_git)
    rc, out, err = ctx.git(path, ["rev-parse", "--absolute-git-dir"])
    if rc != 0:
        f.problems.append("git cannot read it: " + (err.strip().splitlines() or ["?"])[-1][:160])
        return f
    git_dir = ctx.real(out.strip())
    rc, out, _ = ctx.git(path, ["rev-parse", "--git-common-dir"])
    common = ctx.real(os.path.join(path, out.strip())) if rc == 0 and out.strip() else git_dir
    if os.path.basename(common) != ".git":
        f.problems.append("its repository is bare or oddly placed")
    f.main = os.path.dirname(common)
    rc, out, _ = ctx.git(path, ["rev-parse", "--abbrev-ref", "HEAD"])
    f.branch = out.strip() if rc == 0 else None
    f.detached = f.branch in (None, "", "HEAD")
    rc, out, err = ctx.git(path, ["status", "--porcelain"])
    if rc != 0:
        f.problems.append("git status failed: " + (err.strip().splitlines() or ["?"])[-1][:160])
    else:
        f.dirty = len([ln for ln in out.splitlines() if ln.strip()])
    rc, out, _ = ctx.git(path, ["config", "--get", "remote.origin.url"])
    f.origin = out.strip() if rc == 0 and out.strip() else None
    if not f.is_clone and f.main and os.path.isdir(f.main):
        rc, out, err = ctx.git(f.main, ["worktree", "list", "--porcelain"])
        if rc == 0:
            mine = ctx.fold(ctx.real(path))
            rec = next((r for r in D.parse_worktree_porcelain(out) if ctx.fold(ctx.real(r.path)) == mine), None)
            if rec is None:
                f.problems.append("its repository does not list it as a worktree")
            else:
                f.locked, f.prunable = rec.locked, rec.prunable is not None
        else:
            f.problems.append("git worktree list failed: " + (err.strip().splitlines() or ["?"])[-1][:160])
    elif f.is_clone:
        rc, out, _ = ctx.git(path, ["worktree", "list", "--porcelain"])
        if rc == 0 and len(D.parse_worktree_porcelain(out)) > 1:
            f.problems.append("other worktrees hang off this clone, so moving it would break them")
    return f


def repo_dir_for(ctx: Ctx, f: Facts) -> "tuple[str | None, str]":
    """(folder under the lanes root, why or the reason there is none).  Git decides, not the old folder."""
    reg, roots = ctx.registry, ctx.roots
    if not f.is_clone:
        main = ctx.real(f.main or "")
        for app in reg.apps:
            if app.integration_dir_name and ctx.fold(ctx.real(os.path.join(os.fspath(roots.code_root), app.integration_dir_name))) == ctx.fold(main):
                return app.lane_dir, f"worktree of {app.integration_dir_name}"
        if ctx.fold(os.path.dirname(main)) == ctx.fold(ctx.real(os.fspath(roots.code_root))) and L.is_valid_repo_dir(os.path.basename(main)):
            return os.path.basename(main), "worktree of a repo under ~/Code that has no registry row"
        return None, f"its repository ({main}) is not under ~/Code"
    owner_repo = D.parse_owner_repo(f.origin)
    if not owner_repo:
        return None, "a full clone with no GitHub origin, so its repo cannot be told"
    for app in reg.apps:
        if app.owner_repo and app.owner_repo.casefold() == owner_repo.casefold():
            return app.lane_dir, f"clone of {owner_repo}"
    tail = owner_repo.rsplit("/", 1)[-1]
    if L.is_valid_repo_dir(tail):
        return tail, f"clone of {owner_repo}"
    return None, f"cannot make a folder name from {owner_repo}"


def new_name(old_name: str, repo_dir: str, *, review: bool = False) -> "tuple[str, list[str]]":
    """The folder name inside <Repo>, and notes.  The old name is kept unless it is not lowercase kebab
    and a trailing -<Repo> makes it valid (minimax-zulip-stanza-FleetLink -> minimax-zulip-stanza)."""
    notes: "list[str]" = []
    name = old_name
    if review:
        m = _OLD_REVIEW.match(old_name)
        if m:
            return L.REVIEW_PREFIX + m.group(1) + (f"-{m.group(2)}" if m.group(2) else ""), notes
    if not _KEBAB.match(name):
        tail = "-" + repo_dir.casefold()
        if name.casefold().endswith(tail) and _KEBAB.match(name[: -len(tail)].lower()):
            notes.append(f"renamed from {old_name}: a name is lowercase kebab, so the trailing -{repo_dir} is dropped")
            return name[: -len(tail)].lower(), notes
        notes.append(f"the name {old_name!r} is not lowercase kebab; kept as it is")
    return name, notes


def name_note(ctx: Ctx, repo_dir: str, name: str) -> "str | None":
    """A note when the finished name would not read as <seat>-<slug> (the doctor would call it NAME-DRIFT)."""
    if L.is_review_dir_name(name) or L.is_desktop_dir_name(name, ctx.roots.seat_tokens):
        return None
    verdict = L._eval_name(name, ctx.registry, nested_prefix_dir=repo_dir)[0]
    if verdict in (L.NameVerdict.CONFORMING, L.NameVerdict.ALIAS_ONLY):
        return None
    return f"{name!r} does not read as <seat>-<slug> ({verdict}); the doctor will report it as a name problem, not a move problem"


# --------------------------------------------------------------------------- planning

def _checkouts_below(root: str, depth: int) -> "list[str]":
    """Directories at or below `root` (not following symlinks) that hold a .git entry, `depth` levels down."""
    found: "list[str]" = []
    stack = [(root, 0)]
    while stack:
        d, level = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        if any(e.name == ".git" for e in entries):
            found.append(d)
            continue
        if level >= depth:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, level + 1))
            except OSError:
                continue
    return sorted(found)


_INNER_SKIP = (".git", "node_modules")
_INNER_DEPTH = 4


def _links_back(d: str, git_entry: "os.DirEntry[str]") -> bool:
    """Does the `.git` entry in `d` tie that folder to a path git has written down elsewhere?

    A `.git` FILE is a linked worktree (or a submodule): the repository it belongs to records this folder's
    path in its own admin directory.  A `.git` directory with worktrees hanging off it is the same problem
    the other way round.  A plain `.git` directory (a SwiftPM or Cargo checkout, an npm git dependency) is a
    standalone clone that carries no path to the lane, so moving the lane around it breaks nothing."""
    try:
        if git_entry.is_file(follow_symlinks=False):
            return True
        if git_entry.is_dir(follow_symlinks=False):
            with os.scandir(os.path.join(git_entry.path, "worktrees")) as it:
                return any(True for _ in it)
    except OSError:
        return False
    return False


def inner_checkouts(root: str, depth: int = _INNER_DEPTH) -> "list[str]":
    """Folders below `root` (never `root` itself) that git is tied to by a path: a nested linked worktree, a
    submodule, or a clone with worktrees hanging off it.

    `claude -w` or a subagent with `isolation: worktree` started inside a lane puts its checkout under
    `<lane>/.claude/worktrees/<name>`, and every fleet repo ignores `.claude/`, so `git status` reads clean while
    the lane holds a whole checkout whose repository names the lane's old path.  Moving the lane would leave
    that inner worktree's admin entry pointing at the old path, where the symlink hides it until the symlink
    goes and `git worktree prune` strands the checkout.  Symlinks are not followed; `.git` and `node_modules`
    are not entered; `depth` is how many folders down the walk goes."""
    found: "list[str]" = []
    stack = [(root, 0)]
    while stack:
        d, level = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError:
            continue
        if d != root:
            git_entry = next((e for e in entries if e.name == ".git"), None)
            if git_entry is not None and _links_back(d, git_entry):
                found.append(d)
                continue
        if level >= depth:
            continue
        for e in entries:
            if e.name in _INNER_SKIP:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append((e.path, level + 1))
            except OSError:
                continue
    return sorted(found)


def _empty_tree(path: str) -> "list[str] | None":
    """Directories under `path` (deepest first, `path` last) when the whole tree holds nothing but empty
    folders and .DS_Store files; None when anything else is in it (a file, a symlink, a checkout)."""
    order: "list[str]" = []

    def walk(d: str) -> bool:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return False
        for e in entries:
            if e.is_symlink():
                return False
            if e.is_dir(follow_symlinks=False):
                if not walk(e.path):
                    return False
            elif e.name != ".DS_Store":
                return False
        order.append(d)
        return True

    return order if walk(path) else None


def _blockers(ctx: Ctx, f: Facts) -> "list[str]":
    """Reasons a checkout must not be touched now (empty when it is safe)."""
    why = list(f.problems)
    procs = processes_in(ctx, f.path)
    if procs is None:
        why.append("lsof could not be read, so a running process cannot be ruled out")
    elif procs:
        why.append("in use: " + ", ".join(procs[:4]) + (" ..." if len(procs) > 4 else ""))
    if f.dirty:
        why.append(f"uncommitted work ({f.dirty} changed or untracked paths)")
    if f.locked:
        why.append("the worktree is locked")
    if f.prunable:
        why.append("git lists the worktree as prunable (its folder or admin entry is broken)")
    inner = inner_checkouts(f.path)
    if inner:
        rel = [os.path.relpath(p, f.path) for p in inner[:3]]
        why.append("it holds an inner checkout (" + ", ".join(rel) + (" ..." if len(inner) > 3 else "")
                   + "; a gitignored one such as .claude/worktrees/<name> reads clean in git status): "
                   "close or remove it first, because moving this lane would break the inner worktree's link")
    return why


def plan_lanes(ctx: Ctx) -> "list[Item]":
    lanes = ctx.lanes
    items: "list[Item]" = []
    try:
        top = sorted(os.scandir(lanes), key=lambda e: e.name)
    except OSError as exc:
        raise MigrateError(f"cannot read {lanes}: {exc}")
    links = {link.name: link for link in L.external_links(ctx.roots)}
    for entry in top:
        name = entry.name
        path = entry.path
        link = links.get(name)
        if entry.is_symlink() and not (link is not None and link.status in ("ok", "renamed")):
            if link is not None and link.status == "dangling":
                why = (f"a symlink to {link.target}, which does not exist (is the external disk mounted?); "
                       "its lanes are not read")
            elif link is not None:
                why = f"a symlink to {link.real}, which is not on the external lanes disk; not touched"
            else:
                why = "a symlink at the top of the lanes root; not touched"
            items.append(Item(NOTE, path, kind="folder", reasons=[why]))
            continue
        # a symlink that is left here is a lanes/<Repo> folder stored on the external disk: read it like a folder
        if not (entry.is_dir(follow_symlinks=False) or link is not None) or name.startswith("."):
            continue
        if name == L.CODEX_DIR:
            continue
        if name in (L.LEGACY_MANAGED_DIR, L.LEGACY_REVIEW_DIR, "_notes"):
            items.extend(plan_legacy_folder(ctx, path, name))
            continue
        if name.startswith("_"):
            items.append(Item(NOTE, path, kind="folder", reasons=["a reserved folder v2 does not know; not touched"]))
            continue
        try:
            items.extend(plan_bucket(ctx, path, name))
        except OSError as exc:
            items.append(Item(SKIP, path, kind="folder", reasons=[f"cannot be read right now ({exc}); run again"]))
    return items


def plan_bucket(ctx: Ctx, bucket: str, name: str) -> "list[Item]":
    out: "list[Item]" = []
    children: "list[tuple[str, str]]" = []
    with os.scandir(bucket) as it:
        entries = sorted(it, key=lambda e: e.name)
    for e in entries:
        if e.is_symlink():
            out.append(Item(NOTE, e.path, kind="link", reasons=["a symlink (already migrated, or not ours); not touched"]))
        elif e.is_dir(follow_symlinks=False):
            if os.path.lexists(os.path.join(e.path, ".git")):
                children.append((e.name, e.path))
            else:
                inner = _checkouts_below(e.path, 2)
                if inner:
                    out.append(Item(SKIP, e.path, kind="folder", reasons=[f"not a checkout but holds {len(inner)}; move them by hand"]))
                elif _empty_tree(e.path) is None:
                    out.append(Item(NOTE, e.path, kind="folder", reasons=["not a checkout and not empty; not touched"]))
    case_only: "list[Item]" = []
    for cname, cpath in children:
        try:
            facts = inspect(ctx, cpath)
            it = plan_one_lane(ctx, cname, cpath, facts, bucket_name=name)
        except OSError as exc:
            out.append(Item(SKIP, cpath, kind="folder", reasons=[f"cannot be read right now ({exc}); run again"]))
            continue
        if it.action == BUCKET:
            case_only.append(it)
        else:
            out.append(it)
    if case_only:
        out.extend(plan_case_bucket(ctx, bucket, name, case_only, out))
    return out


def _same_folder(a: str, b: str) -> bool:
    """True when both exist and are one folder (a broken symlink or a vanished path is simply False)."""
    try:
        return os.path.lexists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def plan_one_lane(ctx: Ctx, cname: str, cpath: str, f: Facts, *, bucket_name: str) -> Item:
    kind = "clone" if f.is_clone else "worktree"
    it = Item(MOVE, cpath, kind=kind, clone=f.is_clone, main=f.main)
    if not f.is_checkout:
        return Item(SKIP, cpath, kind="folder", reasons=["not a checkout"])
    if cname == RUNTIME_NAME:
        it.action = SKIP
        it.reasons.append("live infrastructure: the agent-sync listener, ~/.local/bin/agent-sync and the runtime-sync "
                          "LaunchAgent use this path, so it needs its own coordinated step and the owner's choice of a new path")
        return it
    if f.problems and f.main is None:
        it.action = SKIP
        it.reasons.extend(f.problems)
        return it
    repo_dir, why = repo_dir_for(ctx, f)
    if repo_dir is None:
        it.action = SKIP
        it.reasons.append(why)
        return it
    old_bucket_app = ctx.registry.app_by_lane_dir(bucket_name, case_insensitive=ctx.roots.case_insensitive) \
        or ctx.registry.app_by_prefix(bucket_name)
    if old_bucket_app is not None and old_bucket_app.lane_dir.casefold() != repo_dir.casefold():
        it.notes.append(f"the old folder {bucket_name} is {old_bucket_app.name}'s, but git says {why}; git wins")
    name, notes = new_name(cname, repo_dir)
    it.notes.extend(notes)
    target = os.path.join(ctx.lanes, repo_dir, name)
    it.new = target
    nn = name_note(ctx, repo_dir, name)
    if nn:
        it.notes.append(nn)
    old_parent = os.path.dirname(cpath)
    same_folder = _same_folder(old_parent, os.path.join(ctx.lanes, repo_dir))
    blockers = _blockers(ctx, f)
    exact_folder = same_folder and os.path.basename(old_parent) == repo_dir
    if same_folder and name == cname:
        if exact_folder:
            it.action = OK
            it.new = cpath
            it.reasons.append("already in its folder")
            return it
        it.action = BUCKET
        it.reasons.append(f"the folder is {repo_dir} spelled {os.path.basename(old_parent)}; one path on this volume")
        it.reasons.extend(blockers)
        return it
    if same_folder and not exact_folder:
        # the folder differs only in case AND the lane gets a new name: the folder is renamed first, then the lane
        if os.path.lexists(target):
            it.action = SKIP
            it.reasons.append(f"the target {target} already exists")
            return it
        it.action = BUCKET
        it.reasons.append(f"the folder is {repo_dir} spelled {os.path.basename(old_parent)}; one path on this volume, "
                          f"and the lane is renamed to {name} after the folder")
        it.reasons.extend(blockers)
        return it
    if os.path.lexists(target):
        it.action = SKIP
        it.reasons.append(f"the target {target} already exists")
        return it
    if blockers:
        it.action = SKIP
        it.reasons.extend(blockers)
    return it


def plan_case_bucket(ctx: Ctx, bucket: str, name: str, members: "list[Item]", others: "list[Item]") -> "list[Item]":
    """One whole-folder rename for the lanes that only differ by case, or a defer naming why."""
    repo_dir = os.path.basename(os.path.dirname(members[0].new or ""))
    target = os.path.join(ctx.lanes, repo_dir)
    stuck = [i for i in others if i.action in (SKIP, DEFER) and i.kind in ("worktree", "clone") and _under(i.old, bucket, ctx.fold)]
    blocked = [m for m in members if len(m.reasons) > 1]       # the first reason is the case note; the rest are blockers
    plan = Item(BUCKET, bucket, target, kind="folder", members=[m.old for m in members])
    renames = {os.path.basename(m.old): os.path.basename(m.new or "") for m in members
               if os.path.basename(m.new or "") != os.path.basename(m.old)}
    if renames:
        plan.extra = {"renames": renames, "mains": {os.path.basename(m.old): m.main for m in members if m.main},
                      "clones": [os.path.basename(m.old) for m in members if m.clone]}
    for m in members:
        plan.notes.extend(m.notes)
    if stuck:
        plan.action = DEFER
        plan.reasons.append("the folder also holds lanes that cannot move yet, and a rename would carry them to the wrong folder: "
                            + ", ".join(os.path.basename(i.old) for i in stuck[:6]))
    if blocked:
        plan.action = DEFER
        for m in blocked[:12]:
            plan.reasons.append(f"{os.path.basename(m.old)}: " + "; ".join(m.reasons[1:]))
        if len(blocked) > 12:
            plan.reasons.append(f"... and {len(blocked) - 12} more")
    if plan.action == BUCKET:
        procs = processes_in(ctx, bucket)
        if procs is None:
            plan.action = DEFER
            plan.reasons.append("lsof could not be read")
        elif procs:
            plan.action = DEFER
            plan.reasons.append("in use: " + ", ".join(procs[:4]))
    if plan.action == BUCKET:
        n, r = len(members), len(renames)
        plan.reasons.append(f"folder {os.path.basename(bucket)} becomes {repo_dir} with {n} lane{'s' if n != 1 else ''} in it"
                            + (f" ({r} also renamed after the folder)" if r else ""))
    return [plan]                      # the member lanes are folded into the one folder action


def plan_legacy_folder(ctx: Ctx, folder: str, name: str) -> "list[Item]":
    """lanes/_managed, lanes/_review, lanes/_notes: move the checkouts that are safe, remove what is empty."""
    out: "list[Item]" = []
    for path in _checkouts_below(folder, 4):
        rel = os.path.relpath(path, folder).split(os.sep)
        f = inspect(ctx, path)
        if name == L.LEGACY_MANAGED_DIR and rel[0] == "codex" and len(rel) == 3:
            blockers = _blockers(ctx, f)
            it = Item(CODEX, path, os.path.join(ctx.lanes, L.CODEX_DIR, rel[1], rel[2]), kind="worktree", main=f.main)
            if not ctx.include_codex:
                it.action = SKIP
                it.reasons.append(CODEX_WHY + "; this one is Codex's current git-worktree-root")
            elif blockers:
                it.action = SKIP
                it.reasons.extend(blockers)
            elif os.path.lexists(it.new or ""):
                it.action = SKIP
                it.reasons.append(f"the target {it.new} already exists")
            out.append(it)
            continue
        base = rel[-1]
        it = plan_one_lane(ctx, base, path, f, bucket_name=rel[0])
        if name == L.LEGACY_REVIEW_DIR and it.new and it.action == MOVE:
            repo_dir = os.path.basename(os.path.dirname(it.new))
            nm, notes = new_name(base, repo_dir, review=True)
            it.new = os.path.join(ctx.lanes, repo_dir, nm)
            it.notes.extend(notes)
            if os.path.lexists(it.new):
                it.action = SKIP
                it.reasons.append(f"the target {it.new} already exists")
        if it.action == BUCKET:
            it.action = SKIP
            it.reasons.append("inside a legacy folder; not a case-only move")
        out.append(it)
    gone = _empty_tree(folder)
    if gone is not None:
        out.append(Item(RMDIR, folder, kind="folder", members=gone, reasons=["empty (only empty folders and .DS_Store inside)"]))
    else:
        # remove the empty sub-folders that exist now; the folder itself waits for its checkouts
        for sub in sorted(os.scandir(folder), key=lambda e: e.name):
            if sub.is_dir(follow_symlinks=False) and not sub.is_symlink():
                tree = _empty_tree(sub.path)
                if tree is not None:
                    out.append(Item(RMDIR, sub.path, kind="folder", members=tree, reasons=["empty (only empty folders and .DS_Store inside)"]))
    return out


def plan_codex(ctx: Ctx) -> "list[Item]":
    out: "list[Item]" = []
    base = os.path.join(ctx.home, ".codex", "worktrees")
    if not os.path.isdir(base):
        return out
    holders = set()
    for path in _checkouts_below(base, 2):
        rel = os.path.relpath(path, base).split(os.sep)
        if len(rel) != 2:
            continue
        holders.add(rel[0])
        f = inspect(ctx, path)
        it = Item(CODEX, path, os.path.join(ctx.lanes, L.CODEX_DIR, rel[0], rel[1]), kind="worktree", main=f.main)
        if not ctx.include_codex:
            it.action = SKIP
            it.reasons.append(CODEX_WHY)
        else:
            blockers = _blockers(ctx, f)
            if blockers:
                it.action = SKIP
                it.reasons.extend(blockers)
            elif os.path.lexists(it.new or ""):
                it.action = SKIP
                it.reasons.append(f"the target {it.new} already exists")
        out.append(it)
    try:
        slugs = sorted(e.name for e in os.scandir(base) if e.is_dir(follow_symlinks=False))
    except OSError:
        slugs = []
    for slug in slugs:
        if slug not in holders and os.path.lexists(os.path.join(base, slug, CODEX_MARKER)):
            out.append(Item(NOTE, os.path.join(base, slug), kind="folder",
                            reasons=[f"only a {CODEX_MARKER} marker, no checkout; not touched"]))
    return out


def cross_disk_reason(ctx: Ctx, old: str, new: str) -> "str | None":
    """Why `old` cannot be moved to `new` with a rename, or None.  `git worktree move` and a clone's rename are
    rename(2), which cannot cross disks (EXDEV), and a lanes/<Repo> symlink onto the external disk puts `new`
    on another disk than a lane in an old internal folder.  A link in the way that leads nowhere is the other
    case: the disk is not mounted, and a folder made through it would land on the wrong disk or fail halfway."""
    probe = os.path.dirname(new)
    while probe and probe != os.path.dirname(probe):
        if os.path.islink(probe) and not os.path.exists(probe):
            return (f"{probe} is a symlink to {os.readlink(probe)}, which does not exist (is the external disk "
                    "mounted?), so the lane cannot be moved there")
        if os.path.exists(probe):
            break
        probe = os.path.dirname(probe)
    try:
        if os.stat(old).st_dev != os.stat(probe).st_dev:
            return (f"{new} is on another disk (the external lanes disk) than {old}, and git cannot move a worktree "
                    "across disks, so it stays where it is until it retires (a new lane there is made with `lane new`)")
    except OSError:
        return None
    return None


def build_plan(ctx: Ctx) -> "list[Item]":
    items = plan_lanes(ctx)
    items.extend(plan_codex(ctx))
    for it in items:
        if it.action in (MOVE, CODEX) and it.new:
            why = cross_disk_reason(ctx, it.old, it.new)
            if why:
                it.action = SKIP
                it.reasons.append(why)
    # order: lane moves, then folder renames, then empty-folder removal
    rank = {MOVE: 0, CODEX: 0, BUCKET: 1, RMDIR: 2}
    return sorted(items, key=lambda i: rank.get(i.action, 3))


# --------------------------------------------------------------------------- the log

def load_log(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("links"), list):
            return data
    except (OSError, ValueError):
        pass
    return {"schema": 1, "links": [], "events": []}


def save_log(path: str, data: dict) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _stamp(ctx: Ctx) -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- applying

def _inside(ctx: Ctx, path: str, root: str) -> bool:
    return _under(os.path.abspath(path), os.path.abspath(root), ctx.fold) and ctx.fold(os.path.abspath(path)) != ctx.fold(os.path.abspath(root))


def _link_ok(ctx: Ctx, path: str) -> bool:
    """A symlink this script may remove sits in the lanes root, or in ~/.codex/worktrees (Codex moves)."""
    return _inside(ctx, path, ctx.lanes) or _inside(ctx, path, os.path.join(ctx.home, ".codex", "worktrees"))


def apply_move(ctx: Ctx, item: Item, log: dict) -> None:
    old, new = item.old, item.new or ""
    codex_root = os.path.join(ctx.home, ".codex", "worktrees")
    if not (_inside(ctx, old, ctx.lanes) or _inside(ctx, old, codex_root)) or not _inside(ctx, new, ctx.lanes):
        raise MigrateError("refusing: a path is outside the lanes root")
    if os.path.islink(old) or not os.path.isdir(old):
        raise MigrateError("the old path is no longer a folder")
    ctx.refresh()
    facts = inspect(ctx, old)
    blockers = _blockers(ctx, facts)
    if blockers:
        raise Skipped("; ".join(blockers))
    if os.path.lexists(new):
        raise Skipped(f"the target {new} appeared")
    why = cross_disk_reason(ctx, old, new)
    if why:
        raise Skipped(why)
    os.makedirs(os.path.dirname(new), exist_ok=True)
    if facts.is_clone:
        os.rename(old, new)
    else:
        rc, _out, err = ctx.git(facts.main or "", ["worktree", "move", old, new])
        if rc != 0:
            raise Skipped("git refused: " + (err.strip().splitlines() or ["?"])[-1][:200])
    after = inspect(ctx, new)
    if not after.is_checkout or after.problems:
        raise MigrateError("the move finished but the new place does not check out: " + "; ".join(after.problems or ["not a checkout"]))
    os.symlink(new, old)
    remove_after = (ctx.today + _dt.timedelta(days=ctx.grace_days)).isoformat()
    log["links"].append({"link": old, "target": new, "made": _stamp(ctx), "remove_after": remove_after, "removed": None})
    log["events"].append({"at": _stamp(ctx), "action": item.action, "old": old, "new": new})
    item.detail = f"symlink left at the old path until {remove_after}"
    if item.action == CODEX:
        # Codex keeps <slug>/.codex-worktree-name beside <Repo>; copy it (never move or delete one in ~/.codex)
        src_marker = os.path.join(os.path.dirname(old), CODEX_MARKER)
        dst_marker = os.path.join(os.path.dirname(new), CODEX_MARKER)
        if os.path.isfile(src_marker) and not os.path.lexists(dst_marker):
            try:
                shutil.copyfile(src_marker, dst_marker)
            except OSError as exc:
                item.detail += f"; the {CODEX_MARKER} marker was not copied ({exc})"


class Skipped(Exception):
    """A step that was refused at the last moment; not a failure."""


def fix_gitdir_case(member_path: str, main: str) -> bool:
    """Make the worktree's admin `gitdir` file carry the spelling of the path it is at now.

    `git worktree repair` leaves a pointer alone when it differs only in letter case and the repository has
    core.ignorecase on (every repo made on this volume), so after a case-only folder rename `git worktree
    list` would keep printing the old spelling.  This rewrites that one line, and only that: the file must
    be inside <main>/.git/worktrees and the two spellings must be the same text ignoring case.  Returns True
    when it changed the file."""
    try:
        with open(os.path.join(member_path, ".git"), encoding="utf-8") as fh:
            line = fh.read().strip()
        if not line.startswith("gitdir:"):
            return False
        admin = os.path.realpath(line[len("gitdir:"):].strip())
        admin_root = os.path.realpath(os.path.join(main, ".git", "worktrees"))
        if os.path.dirname(admin) != admin_root:
            return False
        gitdir_file = os.path.join(admin, "gitdir")
        with open(gitdir_file, encoding="utf-8") as fh:
            current = fh.read().strip()
        want = os.path.join(os.path.realpath(member_path), ".git")
        if current == want or current.casefold() != want.casefold():
            return False
        tmp = f"{gitdir_file}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(want + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, gitdir_file)
        return True
    except OSError:
        return False


def case_rename(ctx: Ctx, bucket: str, target: str) -> None:
    """Give a folder its exact spelling (botfleet -> BotFleet; one folder on this volume).

    One rename(2) first: the folder never leaves its path, so a `git worktree prune` that runs at that
    moment (the janitor, every 30 minutes) still finds every lane.  Only when the volume did not take the
    new spelling does it go through a temporary name, which leaves a short moment with no folder at all;
    that is why the doc asks for the cleaners to be paused while `--apply` runs."""
    parent = os.path.dirname(target)
    old_name, new_name_ = os.path.basename(bucket), os.path.basename(target)
    try:
        os.rename(bucket, target)
    except OSError:
        pass
    try:
        listing = os.listdir(parent)
    except OSError as exc:
        raise MigrateError(f"cannot read {parent} after the rename: {exc}")
    if new_name_ in listing and old_name not in listing:
        return
    tmp = os.path.join(ctx.lanes, f".{old_name}-case-rename-{os.getpid()}")
    os.rename(bucket, tmp)
    try:
        os.rename(tmp, target)
    except OSError as exc:
        try:
            os.rename(tmp, bucket)
        except OSError:
            raise MigrateError(f"the folder is stuck at {tmp} ({exc}); rename it back by hand")
        raise MigrateError(f"could not rename to {target}: {exc}")


def apply_bucket(ctx: Ctx, item: Item, log: dict) -> None:
    bucket, target = item.old, item.new or ""
    if not _inside(ctx, bucket, ctx.lanes) or not _inside(ctx, target, ctx.lanes):
        raise MigrateError("refusing: a path is outside the lanes root")
    ctx.refresh()
    procs = processes_in(ctx, bucket)
    if procs is None or procs:
        raise Skipped("in use or lsof unreadable; folder rename deferred")
    mains: "dict[str, list[str]]" = {}
    for member in item.members:
        f = inspect(ctx, member)
        blockers = _blockers(ctx, f)
        if blockers:
            raise Skipped(f"{os.path.basename(member)}: " + "; ".join(blockers))
        if f.main:
            mains.setdefault(f.main, []).append(os.path.join(target, os.path.basename(member)))
    member_keys = {ctx.fold(os.path.abspath(m)) for m in item.members}
    with os.scandir(bucket) as it:
        for e in it:
            if e.is_symlink() or not e.is_dir(follow_symlinks=False):
                continue
            if os.path.lexists(os.path.join(e.path, ".git")) and ctx.fold(os.path.abspath(e.path)) not in member_keys:
                raise Skipped(f"{e.name} is also in the folder and has not moved out yet; "
                              "a rename would carry it to the wrong folder")
    renames: "dict[str, str]" = dict(item.extra.get("renames", {}))
    mains_by_name: "dict[str, str]" = dict(item.extra.get("mains", {}))
    clones = set(item.extra.get("clones", []))
    for old_base, new_base in renames.items():
        if os.path.lexists(os.path.join(target, new_base)):
            raise Skipped(f"{new_base} already exists in the folder")
    case_rename(ctx, bucket, target)
    problems: "list[str]" = []
    for main, paths in mains.items():
        rc, _out, err = ctx.git(main, ["worktree", "repair", *paths])
        if rc != 0:
            problems.append(f"git worktree repair in {main}: " + (err.strip().splitlines() or ["?"])[-1][:160])
        for path in paths:
            fix_gitdir_case(path, main)
    log["events"].append({"at": _stamp(ctx), "action": item.action, "old": bucket, "new": target})
    for old_base, new_base in renames.items():
        src, dst = os.path.join(target, old_base), os.path.join(target, new_base)
        if old_base in clones:
            # git refuses `worktree move` for a main working tree, so a full clone is a plain rename
            try:
                os.rename(src, dst)
            except OSError as exc:
                problems.append(f"rename {old_base} -> {new_base}: {exc}")
                continue
        else:
            rc, _out, err = ctx.git(mains_by_name.get(old_base, ""), ["worktree", "move", src, dst])
            if rc != 0:
                problems.append(f"git worktree move {old_base} -> {new_base}: " + (err.strip().splitlines() or ["?"])[-1][:160])
                continue
        os.symlink(dst, src)
        remove_after = (ctx.today + _dt.timedelta(days=ctx.grace_days)).isoformat()
        log["links"].append({"link": src, "target": dst, "made": _stamp(ctx), "remove_after": remove_after, "removed": None})
        log["events"].append({"at": _stamp(ctx), "action": MOVE, "old": src, "new": dst})
    if problems:
        raise MigrateError("renamed, but " + "; ".join(problems) + ".  Run `git -C <repo> worktree repair <lane>...` by hand")


def apply_rmdir(ctx: Ctx, item: Item, log: dict) -> None:
    if not _inside(ctx, item.old, ctx.lanes):
        raise MigrateError("refusing: a path is outside the lanes root")
    tree = _empty_tree(item.old)
    if tree is None:
        raise Skipped("no longer empty")
    for d in tree:
        for e in os.scandir(d):
            if e.name == ".DS_Store" and not e.is_dir(follow_symlinks=False):
                os.unlink(e.path)
        os.rmdir(d)
    log["events"].append({"at": _stamp(ctx), "action": item.action, "old": item.old})


def apply_plan(ctx: Ctx, items: "list[Item]", out: Callable[[str], None]) -> int:
    log = load_log(ctx.log_path)
    failures = 0
    for item in items:
        runner = {MOVE: apply_move, CODEX: apply_move, BUCKET: apply_bucket, RMDIR: apply_rmdir}.get(item.action)
        if runner is None:
            continue
        try:
            runner(ctx, item, log)
            item.result = "done"
            out(f"done     {item.old}" + (f" -> {item.new}" if item.new else "") + (f"  ({item.detail})" if item.detail else ""))
        except Skipped as exc:
            item.result, item.detail = "skipped", str(exc)
            out(f"skipped  {item.old}  ({exc})")
        except (MigrateError, OSError) as exc:
            item.result, item.detail = "failed", str(exc)
            failures += 1
            out(f"FAILED   {item.old}  ({exc})")
        save_log(ctx.log_path, log)
    return failures


# --------------------------------------------------------------------------- removing the links

def plan_links(ctx: Ctx) -> "list[dict]":
    log = load_log(ctx.log_path)
    rows: "list[dict]" = []
    for ln in log["links"]:
        if ln.get("removed"):
            continue
        row = dict(ln)
        due = ln.get("remove_after", "") <= ctx.today.isoformat()
        p = ln.get("link", "")
        if not _link_ok(ctx, p):
            row["state"] = "refused: not inside the lanes root"
        elif not os.path.islink(p):
            row["state"] = "gone or not a symlink any more; nothing to remove"
        elif os.readlink(p) != ln.get("target"):
            row["state"] = "refused: it points somewhere else now"
        elif not os.path.isdir(ln.get("target", "")):
            # the janitor retired the moved lane: the link is ours (it still points where we put it) and
            # leads nowhere, so it goes on its date like any other, which lets the old prefix folder empty
            row["state"] = "due" if due else f"waiting until {ln.get('remove_after')}"
            row["target_gone"] = True
        else:
            row["state"] = "due" if due else f"waiting until {ln.get('remove_after')}"
        rows.append(row)
    return rows


def remove_links(ctx: Ctx, rows: "list[dict]", apply: bool, out: Callable[[str], None]) -> int:
    log = load_log(ctx.log_path)
    failures = 0
    for row in rows:
        out(f"{row['state']:<28} {row['link']} -> {row['target']}" + ("  (its target is gone)" if row.get("target_gone") else ""))
        if not apply or row["state"] != "due":
            continue
        try:
            os.unlink(row["link"])
            parent = os.path.dirname(row["link"])
            keep = {a.lane_dir for a in ctx.registry.apps} | {L.CODEX_DIR}
            if os.path.basename(parent) not in keep:
                try:
                    os.rmdir(parent)            # an old prefix folder, once its last link is gone
                except OSError:
                    pass
            for ln in log["links"]:
                if ln.get("link") == row["link"] and ln.get("target") == row["target"]:
                    ln["removed"] = _stamp(ctx)
            out(f"removed  {row['link']}")
        except OSError as exc:
            failures += 1
            out(f"FAILED   {row['link']}  ({exc})")
    if apply:
        save_log(ctx.log_path, log)
    return failures


# --------------------------------------------------------------------------- output and command line

def render(items: "list[Item]", ctx: Ctx, out: Callable[[str], None]) -> None:
    def tilde(p: "str | None") -> str:
        if not p:
            return ""
        return "~" + p[len(ctx.home):] if p == ctx.home or p.startswith(ctx.home + os.sep) else p

    counts: "dict[str, int]" = {}
    for it in items:
        counts[it.action] = counts.get(it.action, 0) + 1
        if it.action == OK or (it.action == NOTE and not it.reasons):
            continue                       # already where it belongs: counted in the summary, not listed
        head = f"{it.action:<14} {tilde(it.old)}" + (f"  ->  {tilde(it.new)}" if it.new and it.action not in (SKIP, OK) else "")
        out(head)
        for r in it.reasons:
            out(f"                 - {r}")
        for n in it.notes:
            out(f"                 note: {n}")
    out("")
    out("Summary: " + ", ".join(f"{n} {a}" for a, n in sorted(counts.items())) if counts else "Summary: nothing to do")


def make_ctx(home: str, env: "Mapping[str, str] | None" = None, *, lsof: "Callable | None" = None,
             today: "_dt.date | None" = None, include_codex: bool = False, log_path: str = "",
             grace_days: int = GRACE_DAYS, case_insensitive: "bool | None" = None) -> Ctx:
    env = os.environ if env is None else env
    registry = L.load_registry(env=env)
    roots = L.make_roots(home, env, registry=registry, case_insensitive=case_insensitive)
    log = log_path or os.path.join(os.fspath(roots.lanes_root), LOG_NAME)
    here = os.path.dirname(SCRIPTS_DIR)
    return Ctx(
        home=os.fspath(roots.home), roots=roots, registry=registry,
        lsof=lsof or D.make_lsof_reader(), today=today or _dt.date.today(), grace_days=grace_days,
        include_codex=include_codex, log_path=log,
        self_paths=tuple(dict.fromkeys(os.path.realpath(p) for p in (os.getcwd(), here))),
    )


def main(argv: "Sequence[str] | None" = None, *, home: "str | None" = None, env: "Mapping[str, str] | None" = None,
         lsof: "Callable | None" = None, today: "_dt.date | None" = None, stdout=None) -> int:
    out_stream = sys.stdout if stdout is None else stdout

    def out(line: str) -> None:
        out_stream.write(line + "\n")

    p = argparse.ArgumentParser(description="Move the existing lanes to the layout v2 tree.  A dry run unless --apply.")
    p.add_argument("--apply", action="store_true", help="do it (the default prints the plan and changes nothing)")
    p.add_argument("--json", action="store_true", help="print the plan as JSON")
    p.add_argument("--include-codex", action="store_true", help="also move ~/.codex/worktrees/<slug>/<Repo> to lanes/_codex")
    p.add_argument("--remove-links", action="store_true", help="list (and with --apply, delete) the due symlinks the migration left")
    p.add_argument("--grace-days", type=int, default=GRACE_DAYS, metavar="N", help="days the old-path symlinks stay (default 7)")
    p.add_argument("--log", default="", metavar="PATH", help=f"the migration log (default <lanes root>/{LOG_NAME})")
    p.add_argument("--home", default="", metavar="PATH", help="a different home (tests)")
    try:
        args = p.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code not in (0, None) else EXIT_OK
    if args.grace_days < 0 or args.grace_days > 365:
        p.print_usage(sys.stderr)
        return EXIT_USAGE
    real_home = args.home or home or os.path.expanduser("~")
    if not os.path.isdir(real_home):
        sys.stderr.write(f"lanes-v2-migrate: {real_home} is not a directory\n")
        return EXIT_USAGE
    try:
        ctx = make_ctx(real_home, env, lsof=lsof, today=today, include_codex=args.include_codex,
                       log_path=args.log, grace_days=args.grace_days)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"lanes-v2-migrate: cannot read the registry: {exc}\n")
        return EXIT_USAGE
    if not os.path.isdir(ctx.lanes):
        out(f"Nothing to do: {ctx.lanes} does not exist.")
        return EXIT_OK
    if args.remove_links:
        rows = plan_links(ctx)
        if not rows:
            out("No migration symlinks are recorded.")
            return EXIT_OK
        if not args.apply:
            out("Dry run (add --apply to remove the due links):")
        return EXIT_FAILED if remove_links(ctx, rows, args.apply, out) else EXIT_OK
    try:
        items = build_plan(ctx)
    except (MigrateError, OSError) as exc:
        sys.stderr.write(f"lanes-v2-migrate: {exc}\n")
        return EXIT_FAILED
    if args.json and not args.apply:
        out_stream.write(json.dumps({"apply": False, "lanes_root": ctx.lanes, "items": [i.as_dict() for i in items]}, indent=2) + "\n")
        return EXIT_OK
    if not args.apply:
        out(f"Dry run: nothing is changed.  Lanes root {ctx.lanes}.  Add --apply to do it.")
        out("")
        render(items, ctx, out)
        return EXIT_OK
    # with --json the one document on stdout must parse, so the progress lines go to stderr
    progress = (lambda line: sys.stderr.write(line + "\n")) if args.json else out
    progress(f"Applying.  Lanes root {ctx.lanes}.  Log {ctx.log_path}.")
    failures = apply_plan(ctx, items, progress)
    if args.json:
        out_stream.write(json.dumps({"apply": True, "lanes_root": ctx.lanes, "failures": failures,
                                     "items": [i.as_dict() for i in items]}, indent=2) + "\n")
    skipped = sum(1 for i in items if i.action in (SKIP, DEFER) or i.result == "skipped")
    progress("")
    progress(f"Finished: {sum(1 for i in items if i.result == 'done')} done, {skipped} skipped or deferred, {failures} failed.  "
             "Run it again once the skipped lanes are free.")
    return EXIT_FAILED if failures else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
