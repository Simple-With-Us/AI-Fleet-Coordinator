"""lane CLI tests: the allowlisted runner, input resolution, `lane new`, review checkouts, the shim.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_lane -v

Every `lane new` test runs real git against a throwaway world: a local bare repo as origin (file
URLs, no network), a real clone of it as ~/Code/DealDex in a fake home, a fake fleet-apps.json passed
through FLEET_APPS_JSON, an injected clock, and a fake gh.  The git config is hermetic (no system or
global file, identity in the environment).  The environment given to `lane.main` is an explicit dict, so
neither the real home nor this process's AGENT_SEAT leaks in.  Nothing here touches the network or
anything outside the temp directories.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import tomllib
import unittest
from pathlib import Path
from typing import Callable, Dict, List, Optional
from unittest import mock

from fleet_lanes import doctor as D
from fleet_lanes import lane as K
from fleet_lanes import layout as L

SCRIPTS_DIR = Path(__file__).resolve().parents[2]
SHIM = SCRIPTS_DIR / "fleet_lanes" / "bin" / "lane"
NOW = dt.datetime(2026, 10, 7, 16, 15, 0, tzinfo=dt.timezone.utc)

_APPS = [
    ("DealDex", "DD", "dealdex", "DealDex"),
    ("Socratic-Trade", "ST", "trading", "Socratic-Trade"),
    ("Congress.Trade", "CT", "congress", "Congress.Trade"),
    ("Alpha", "BT", "alpha", "Alpha"),       # acronym BT ...
    ("Beta", "BE", "bt", "Beta"),            # ... equals Beta's prefix: "bt" is ambiguous
]
_SEATS = [
    ("CLAUDE", "claude", ["claude/", "agent/claude"], False),
    ("AG", "antigravity", ["ag/", "agent/antigravity"], False),
    ("MM", "minimax", ["minimax/"], False),
    ("GROK", "grok", ["grok/"], False),
    ("GROK-BUILD", "grok-build", ["grok-build/"], False),
    ("CLUTCH", "clutch", ["clutch/"], False),
    ("CURSOR", "cursor", ["cursor/"], False),
    ("GROK-BOT", "cursor", ["cursor/"], False),
    ("MONET", "monet", ["monet/"], True),
    ("ODD", "odd", ["agent/odd"], False),    # no slash-terminated prefix: cannot name a branch
]
REGISTRY_DATA = {
    "owner": "Simple-With-Us",
    "apps": [{"repo": r, "acronym": a, "worktreePrefix": p, "codeDir": c} for r, a, p, c in _APPS],
    "seats": [{"tag": t, "worktreeSuffix": s, "branchPrefixes": b, **({"retired": True} if ret else {})}
              for t, s, b, ret in _SEATS],
}
REGISTRY = L.parse_registry(REGISTRY_DATA)

_HERMETIC = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C",
    "GIT_AUTHOR_NAME": "Lane Test", "GIT_AUTHOR_EMAIL": "lane@example.invalid",
    "GIT_COMMITTER_NAME": "Lane Test", "GIT_COMMITTER_EMAIL": "lane@example.invalid",
}

# ---- the shared template: a bare origin with main, release and a PR head, built once

_TEMPLATE: Optional[tempfile.TemporaryDirectory] = None
TEMPLATE_WORLD = ""      # origin/DealDex.git (bare) and home/Code/DealDex (a clone of it), copied per test
TEMPLATE_URI = ""        # the file URL the template clone's origin points at, rewritten in each copy
SHAS: Dict[str, str] = {}


def _git(args: List[str], cwd: Optional[str] = None, env: Optional[dict] = None, check: bool = True) -> str:
    full_env = {"PATH": os.environ.get("PATH", ""), **_HERMETIC, **(env or {})}
    full_env.setdefault("HOME", cwd or tempfile.gettempdir())
    proc = subprocess.run(["git", *args], cwd=cwd, env=full_env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def setUpModule() -> None:
    global _TEMPLATE, TEMPLATE_WORLD, TEMPLATE_URI
    _TEMPLATE = tempfile.TemporaryDirectory(prefix="lane-test-template-")
    base = os.path.realpath(_TEMPLATE.name)
    bare, seed = os.path.join(base, "DealDex.git"), os.path.join(base, "seed")
    env = {"HOME": base}
    _git(["init", "--bare", bare], env=env)
    _git(["--git-dir", bare, "symbolic-ref", "HEAD", "refs/heads/main"], env=env)
    _git(["init", seed], env=env)
    _git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=seed, env=env)

    def commit(name: str) -> str:
        with open(os.path.join(seed, name), "w", encoding="utf-8") as fh:
            fh.write(name + "\n")
        _git(["add", name], cwd=seed, env=env)
        _git(["commit", "-q", "-m", name], cwd=seed, env=env)
        return _git(["rev-parse", "HEAD"], cwd=seed, env=env)

    commit("one.txt")
    SHAS["main"] = commit("two.txt")
    _git(["checkout", "-q", "-b", "release"], cwd=seed, env=env)
    SHAS["release"] = commit("release.txt")
    _git(["checkout", "-q", "-b", "prbranch", "main"], cwd=seed, env=env)
    SHAS["pr"] = commit("pr.txt")
    _git(["push", "-q", bare, "main", "release", "prbranch"], cwd=seed, env=env)
    _git(["--git-dir", bare, "update-ref", "refs/pull/7/head", SHAS["pr"]], env=env)
    _git(["--git-dir", bare, "update-ref", "-d", "refs/heads/prbranch"], env=env)
    world = os.path.join(base, "world")
    os.makedirs(os.path.join(world, "origin"))
    os.makedirs(os.path.join(world, "home", "apps"))
    os.makedirs(os.path.join(world, "home", "Code"))
    shutil.copytree(bare, os.path.join(world, "origin", "DealDex.git"))
    TEMPLATE_URI = Path(os.path.join(world, "origin", "DealDex.git")).as_uri()
    _git(["clone", "-q", TEMPLATE_URI, os.path.join(world, "home", "Code", "DealDex")], env=env)
    TEMPLATE_WORLD = world


def tearDownModule() -> None:
    if _TEMPLATE is not None:
        _TEMPLATE.cleanup()


# ---- helpers

def tree_hash(root: "str | Path", skip: "tuple[str | Path, ...]" = ()) -> str:
    """A digest of every path, link target and file content under root, except the skipped trees."""
    h = hashlib.sha256()
    skips = [os.path.realpath(s) for s in skip]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if any(dirpath == s or dirpath.startswith(s + os.sep) for s in skips):
            dirnames[:] = []
            continue
        h.update(("D:" + os.path.relpath(dirpath, root) + "\n").encode())
        for name in sorted(filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                h.update(f"L:{rel}>{os.readlink(p)}\n".encode())
            else:
                with open(p, "rb") as fh:
                    h.update(f"F:{rel}:{os.stat(p).st_mode}:".encode() + hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


class Result:
    def __init__(self, rc: int, out: str, err: str, calls: List[List[str]]) -> None:
        self.rc, self.out, self.err, self.calls = rc, out, err, calls

    @property
    def verbs(self) -> List[tuple]:
        """The git or gh calls, without lane's global git flags."""
        return [tuple(c[4:]) if c[0] == "git" else tuple(c) for c in self.calls]

    def has(self, *prefix: str) -> bool:
        return any(v[:len(prefix)] == prefix for v in self.verbs)

    def json(self) -> dict:
        return json.loads(self.out)


def gh_pr_json(sha: Optional[str] = None, state: str = "OPEN", ref: str = "feature/pr-work") -> D.CmdResult:
    return D.CmdResult(0, json.dumps({"headRefOid": sha or SHAS["pr"], "headRefName": ref, "state": state,
                                      "isCrossRepository": False}), "")


class World(object):
    """A fake home with ~/apps and ~/Code/DealDex (a clone of a local bare origin), copied from a template."""

    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lane-test-")
        case.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        self.home = self.root / "home"
        self.origin = self.root / "origin" / "DealDex.git"
        self.tree = self.home / "Code" / "DealDex"
        self.registry_path = self.root / "fleet-apps.json"
        shutil.copytree(TEMPLATE_WORLD, self.root, symlinks=True, dirs_exist_ok=True)
        config = self.tree / ".git" / "config"
        config.write_text(config.read_text(encoding="utf-8").replace(TEMPLATE_URI, self.origin.as_uri()),
                          encoding="utf-8")
        self.registry_path.write_text(json.dumps(REGISTRY_DATA), encoding="utf-8")
        self.env: Dict[str, str] = {
            "PATH": os.environ.get("PATH", ""), "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / ".config"),
            **_HERMETIC, "FLEET_APPS_JSON": str(self.registry_path), "AGENT_SEAT": "CLAUDE",
        }
        self.gh: Callable[[List[str]], D.CmdResult] = lambda argv: gh_pr_json()
        self.roots = L.make_roots(self.home, self.env, registry=REGISTRY)

    # git against the fake world, hermetic
    def git(self, args: List[str], cwd: "Optional[Path | str]" = None, check: bool = True) -> str:
        return _git(args, cwd=str(cwd) if cwd else None, env=self.env, check=check)

    def lane(self, *argv: str, env: Optional[dict] = None, clock: Optional[Callable[[], dt.datetime]] = None,
             gh: Optional[Callable[[List[str]], D.CmdResult]] = None,
             exec_override: Optional[Callable[[List[str]], Optional[D.CmdResult]]] = None) -> Result:
        full_env = dict(self.env)
        for k, v in (env or {}).items():
            if v is None:
                full_env.pop(k, None)
            else:
                full_env[k] = v
        calls: List[List[str]] = []
        answer_gh = gh or self.gh

        def exec_fn(argv_: List[str], cwd: Optional[str], child_env: dict, timeout: float) -> D.CmdResult:
            calls.append(list(argv_))
            if exec_override is not None:
                forced = exec_override(list(argv_))
                if forced is not None:
                    return forced
            if argv_[0] == "gh":
                return answer_gh(list(argv_))
            return K.default_exec(argv_, cwd, child_env, timeout)

        out, err = io.StringIO(), io.StringIO()
        rc = K.main(list(argv), env=full_env, clock=clock or (lambda: NOW), exec_fn=exec_fn, stdout=out, stderr=err)
        return Result(rc, out.getvalue(), err.getvalue(), calls)

    def lane_path(self, slug: str = "fix-thing", repo: str = "DealDex", seat: str = "claude") -> Path:
        return self.roots.lanes_root / repo / f"{seat}-{slug}"

    def review_path(self, pr: int = 7, seat: "Optional[str]" = None, repo: str = "DealDex") -> Path:
        """lanes/<Repo>/review-pr-<n>, or review-pr-<n>-<seat> for the second seat on that PR."""
        return self.roots.lanes_root / repo / (f"review-pr-{pr}" + (f"-{seat}" if seat else ""))

    def advance_origin(self, branch: str = "main") -> str:
        git_dir = str(self.origin)
        tree = self.git(["--git-dir", git_dir, "rev-parse", f"refs/heads/{branch}^{{tree}}"])
        new = self.git(["--git-dir", git_dir, "commit-tree", tree, "-p", f"refs/heads/{branch}", "-m", "advance"])
        self.git(["--git-dir", git_dir, "update-ref", f"refs/heads/{branch}", new])
        return new

    def everything_hash(self) -> str:
        return tree_hash(self.root)


class WorldCase(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)

    def assertNoChange(self, before: str) -> None:
        self.assertEqual(self.w.everything_hash(), before, "the world changed")


# --------------------------------------------------------------------------- the allowlist

class AllowlistTests(unittest.TestCase):
    ROOT = "/Users/jay/apps/lanes"
    SHA = "a" * 40

    def check(self, argv: List[str], root: Optional[str] = ROOT) -> None:
        K.check_allowed(argv, write_root=root)

    def test_every_allowed_shape(self) -> None:
        dest = self.ROOT + "/dealdex/claude-x"
        allowed = [
            ["git", "fetch", "--no-tags", "origin", "main"],
            ["git", "fetch", "--no-tags", "origin", "claude/fix-thing"],
            ["git", "fetch", "--no-tags", "origin", "pull/7/head"],
            ["git", "worktree", "list", "--porcelain"],
            ["git", "worktree", "add", "-b", "claude/x", dest, self.SHA],
            ["git", "worktree", "add", dest, "claude/x"],
            ["git", "worktree", "add", "--detach", dest, self.SHA],
            ["git", "rev-parse", "--git-dir"],
            ["git", "rev-parse", "--absolute-git-dir"],
            ["git", "rev-parse", "--git-common-dir"],
            ["git", "rev-parse", "--show-toplevel"],
            ["git", "rev-parse", "--verify", "-q", "origin/main^{commit}"],
            ["git", "rev-parse", "--verify", "--quiet", self.SHA + "^{commit}"],
            ["git", "rev-parse", "--verify", "HEAD"],
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/claude/x"],
            ["git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"],
            ["git", "for-each-ref", "--format=%(refname)", "refs/heads/claude/x", "refs/remotes/origin/claude/x"],
            ["git", "config", "--get", "remote.origin.url"],
            ["gh", "pr", "view", "7", "--repo", "Simple-With-Us/DealDex", "--json",
             "headRefOid,headRefName,state,isCrossRepository"],
            ["gh", "pr", "view", "7", "--json", "headRefOid", "--repo", "Simple-With-Us/DealDex"],
        ]
        for argv in allowed:
            with self.subTest(argv=argv):
                self.check(argv)

    def test_refused_commands(self) -> None:
        dest = self.ROOT + "/dealdex/claude-x"
        refused = [
            [], [""], ["git"], ["git", ""], ["/usr/bin/git", "status"], ["bash", "-c", "true"], ["rm", "-rf", dest],
            ["git", "status"], ["git", "diff"], ["git", "log", "-1"], ["git", "checkout", "main"],
            ["git", "switch", "-c", "x"], ["git", "reset", "--hard", "origin/main"], ["git", "clean", "-fd"],
            ["git", "stash"], ["git", "gc"], ["git", "push", "origin", "main"], ["git", "push", "--force"],
            ["git", "pull"], ["git", "merge", "origin/main"], ["git", "rebase", "main"],
            ["git", "branch", "-D", "claude/x"], ["git", "branch", "--show-current"],
            ["git", "remote", "add", "x", "/tmp/y"], ["git", "clone", "x", dest], ["git", "init", dest],
            ["git", "-C", "/tmp/x", "status"], ["git", "--git-dir=/tmp/x", "fetch"],
            ["git", "update-ref", "-d", "refs/heads/main"], ["git", "worktree", "prune"],
            ["git", "worktree", "remove", dest], ["git", "worktree", "remove", "--force", dest],
            ["git", "worktree", "move", dest, "/tmp/x"], ["git", "worktree", "lock", dest],
            ["git", "worktree", "repair"], ["git", "worktree", "list"],
            ["git", "worktree", "list", "--porcelain", "-z"],
            ["git", "worktree", "add", "-f", dest, "claude/x"], ["git", "worktree", "add", "--force", dest, "claude/x"],
            ["git", "worktree", "add", "-B", "claude/x", dest, self.SHA],
            ["git", "worktree", "add", "--orphan", "x", dest], ["git", "worktree", "add", "--lock", dest, "x"],
            ["git", "worktree", "add", "--no-checkout", dest, "claude/x"],
            ["git", "worktree", "add", "-b", "claude/x", dest, "origin/main"],      # a start point must be a sha
            ["git", "worktree", "add", "-b", "claude/x", dest],
            ["git", "worktree", "add", "-b", "-x", dest, self.SHA],
            ["git", "worktree", "add", "-b", "claude/x", "relative/path", self.SHA],
            ["git", "worktree", "add", "-b", "claude/x", dest, self.SHA, "extra"],
            ["git", "worktree", "add", "--detach", dest, "origin/main"],
            ["git", "worktree", "add", "--detach", dest, "HEAD"],
            ["git", "worktree", "add", dest],
            ["git", "fetch"], ["git", "fetch", "origin"], ["git", "fetch", "origin", "main"],
            ["git", "fetch", "--no-tags", "origin"], ["git", "fetch", "--no-tags", "upstream", "main"],
            ["git", "fetch", "--no-tags", "origin", "main:main"], ["git", "fetch", "--no-tags", "origin", "+main"],
            ["git", "fetch", "--no-tags", "origin", "main:refs/heads/main"],
            ["git", "fetch", "--no-tags", "origin", "--upload-pack=/tmp/x"],
            ["git", "fetch", "--no-tags", "origin", "-f"], ["git", "fetch", "--no-tags", "origin", "--all"],
            ["git", "fetch", "--no-tags", "--force", "origin", "main"],
            ["git", "fetch", "--no-tags", "origin", "main", "release"],
            ["git", "fetch", "--no-tags", "https://example.invalid/r.git", "main"],
            ["git", "fetch", "--no-tags", "origin", "a..b"], ["git", "fetch", "--no-tags", "origin", "a b"],
            ["git", "fetch", "--no-tags", "origin", "x\ny"], ["git", "fetch", "--no-tags", "origin", "/abs"],
            ["git", "fetch", "--no-tags", "origin", "x.lock"], ["git", "fetch", "--no-tags", "origin", "$(id)"],
            ["git", "rev-parse", "HEAD"], ["git", "rev-parse", "--verify"], ["git", "rev-parse", "--verify", "-x", "HEAD"],
            ["git", "rev-parse", "--verify", "-q", "-q", "HEAD"], ["git", "rev-parse", "--git-dir", "--foo"],
            ["git", "rev-parse", "--verify", "a..b"], ["git", "rev-parse", "--verify", "HEAD~3"],
            ["git", "rev-parse", "--verify", "--output=x"], ["git", "rev-parse", "--is-inside-work-tree"],
            ["git", "show-ref"], ["git", "show-ref", "--verify", "refs/heads/x"],
            ["git", "show-ref", "--verify", "--quiet", "HEAD"], ["git", "show-ref", "--verify", "--quiet", "refs/tags/v1"],
            ["git", "show-ref", "--verify", "--quiet", "refs/heads/../x"],
            ["git", "for-each-ref"], ["git", "for-each-ref", "--format=%(refname)"],
            ["git", "for-each-ref", "--format=%(objectname)", "refs/heads/x"],
            ["git", "for-each-ref", "--format=%(refname)", "refs/tags/x"],
            ["git", "for-each-ref", "--format=%(refname)", "--sort=-x", "refs/heads/x"],
            ["git", "config", "--get", "user.name"], ["git", "config", "--get", "remote.origin.pushurl"],
            ["git", "config", "--unset", "remote.origin.url"], ["git", "config", "remote.origin.url", "x"],
            ["git", "config", "--get-all", "remote.origin.url"], ["git", "config", "--list"],
            ["gh"], ["gh", "pr", "view"], ["gh", "pr", "view", "7"], ["gh", "pr", "list", "--repo", "a/b"],
            ["gh", "pr", "merge", "7", "--repo", "a/b", "--json", "state"], ["gh", "pr", "checkout", "7"],
            ["gh", "pr", "view", "7", "--repo", "a/b", "--json", "state", "--web"],
            ["gh", "pr", "view", "7", "--repo", "a/b", "--json", "body"],
            ["gh", "pr", "view", "7", "--repo", "a/b", "--json", "state,state"],
            ["gh", "pr", "view", "7", "--repo", "a/b", "--json", "state,"],
            ["gh", "pr", "view", "x", "--repo", "a/b", "--json", "state"],
            ["gh", "pr", "view", "0", "--repo", "a/b", "--json", "state"],
            ["gh", "pr", "view", "-7", "--repo", "a/b", "--json", "state"],
            ["gh", "pr", "view", "7", "--repo", "a b", "--json", "state"],
            ["gh", "pr", "view", "7", "--repo", "ab", "--json", "state"],
            ["gh", "pr", "view", "7", "--repo", "a/b", "--repo", "c/d"],
            ["gh", "api", "repos/a/b"], ["gh", "repo", "clone", "a/b"], ["gh", "auth", "token"],
        ]
        for argv in refused:
            with self.subTest(argv=argv):
                with self.assertRaises(K.CommandRefused):
                    self.check(argv)

    def test_non_string_and_nul_are_refused(self) -> None:
        for argv in (["git", None], ["git", 5], ["git", "fetch\x00", "--no-tags", "origin", "main"]):
            with self.subTest(argv=argv):
                with self.assertRaises(K.CommandRefused):
                    self.check(argv)  # type: ignore[arg-type]

    def test_worktree_add_destination_must_be_strictly_inside_the_write_root(self) -> None:
        sha = self.SHA
        with self.assertRaises(K.CommandRefused):
            self.check(["git", "worktree", "add", "-b", "claude/x", self.ROOT, sha])        # the root itself
        with self.assertRaises(K.CommandRefused):
            self.check(["git", "worktree", "add", "-b", "claude/x", "/Users/jay/Code/DealDex-x", sha])
        with self.assertRaises(K.CommandRefused):
            self.check(["git", "worktree", "add", "-b", "claude/x", self.ROOT + "-evil/x", sha])  # prefix trick
        with self.assertRaises(K.CommandRefused):
            self.check(["git", "worktree", "add", "-b", "claude/x", self.ROOT + "/../x", sha])
        with self.assertRaises(K.CommandRefused):
            self.check(["git", "worktree", "add", "-b", "claude/x", self.ROOT + "/dealdex/claude-x", sha], root=None)

    def test_worktree_add_destination_through_a_symlink_is_judged_by_its_real_path(self) -> None:
        with tempfile.TemporaryDirectory(prefix="lane-test-") as tmp:
            root, outside = Path(os.path.realpath(tmp)) / "lanes", Path(os.path.realpath(tmp)) / "outside"
            (root / "dealdex").mkdir(parents=True)
            outside.mkdir()
            os.symlink(outside, root / "evil")
            ok = ["git", "worktree", "add", "-b", "claude/x", str(root / "dealdex" / "claude-x"), self.SHA]
            bad = ["git", "worktree", "add", "-b", "claude/x", str(root / "evil" / "claude-x"), self.SHA]
            K.check_allowed(ok, write_root=root)
            with self.assertRaises(K.CommandRefused):
                K.check_allowed(bad, write_root=root)

    def test_case_folding_is_a_runner_option(self) -> None:
        argv = ["git", "worktree", "add", "-b", "claude/x", "/USERS/JAY/APPS/LANES/dealdex/claude-x", self.SHA]
        K.check_allowed(argv, write_root=self.ROOT, fold=str.casefold)

    def test_valid_ref_name(self) -> None:
        for ok in ("main", "claude/fix-thing", "release/1.2", "pull/7/head", "a_b", "feature/x.y"):
            self.assertTrue(K.valid_ref_name(ok), ok)
        for bad in ("", "-x", "/x", "x/", "x//y", "a..b", "a b", "a:b", "+a", "a~1", "a^", "a*", "a?", "a[", "a\\b",
                    "a.", ".hidden", "a/.b", "a.lock", "a/b.lock", "x" * 201, "a\nb", "a@{1}", None, 5):
            self.assertFalse(K.valid_ref_name(bad), repr(bad))


class RunnerTests(unittest.TestCase):
    def test_a_refused_command_never_reaches_the_process_starter(self) -> None:
        started: List[list] = []
        run = K.Runner({"PATH": "/usr/bin"}, exec_fn=lambda *a: started.append(list(a)) or D.CmdResult(0))
        for argv in (["git", "push"], ["git", "worktree", "remove", "/x"], ["rm", "-rf", "/"]):
            with self.assertRaises(K.CommandRefused):
                run(argv)
        self.assertEqual(started, [])

    def test_git_gets_global_flags_and_a_clean_environment(self) -> None:
        seen: Dict[str, object] = {}

        def exec_fn(argv, cwd, env, timeout):
            seen.update(argv=list(argv), cwd=cwd, env=dict(env), timeout=timeout)
            return D.CmdResult(0, "")

        base = {"PATH": "/usr/bin", "GIT_DIR": "/elsewhere/.git", "GIT_INDEX_FILE": "/x/index", "GIT_WORK_TREE": "/w",
                "GIT_COMMON_DIR": "/c", "GIT_OBJECT_DIRECTORY": "/o", "AGENT_SEAT": "CLAUDE"}
        K.Runner(base, exec_fn=exec_fn)(["git", "fetch", "--no-tags", "origin", "main"], cwd="/tree")
        self.assertEqual(seen["argv"], ["git", "--no-optional-locks", "-c", "core.fsmonitor=false",
                                        "fetch", "--no-tags", "origin", "main"])
        self.assertEqual(seen["cwd"], "/tree")
        env = seen["env"]
        for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY"):
            self.assertNotIn(name, env)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(seen["timeout"], 300.0)       # fetch gets a long cap
        K.Runner(base, exec_fn=exec_fn)(["git", "rev-parse", "--git-dir"])
        self.assertEqual(seen["timeout"], 30.0)
        K.Runner(base, exec_fn=exec_fn)(["gh", "pr", "view", "7", "--repo", "a/b", "--json", "state"])
        self.assertEqual(seen["argv"][0], "gh")
        self.assertEqual(seen["timeout"], 60.0)

    def test_the_default_starter_reports_a_missing_program_and_a_timeout(self) -> None:
        self.assertFalse(K.default_exec(["definitely-not-a-program-xyz"], None, {"PATH": "/usr/bin"}, 5).ok)
        res = K.default_exec(["sleep", "30"], None, {"PATH": os.environ.get("PATH", "")}, 0.3)
        self.assertTrue(res.timed_out)
        self.assertFalse(res.ok)


# --------------------------------------------------------------------------- inputs

class ResolveTests(unittest.TestCase):
    def test_app_by_each_field_and_any_case(self) -> None:
        for query in ("DealDex", "dealdex", "DEALDEX", "dd", "DD", "Dd"):
            with self.subTest(query=query):
                self.assertEqual(K.resolve_app(REGISTRY, query).name, "DealDex")
        self.assertEqual(K.resolve_app(REGISTRY, "trading").name, "Socratic-Trade")
        self.assertEqual(K.resolve_app(REGISTRY, "ST").name, "Socratic-Trade")
        self.assertEqual(K.resolve_app(REGISTRY, "  st  ").name, "Socratic-Trade")

    def test_dots_and_dashes_are_loose_only_when_nothing_matches_exactly(self) -> None:
        self.assertEqual(K.resolve_app(REGISTRY, "congress.trade").name, "Congress.Trade")
        self.assertEqual(K.resolve_app(REGISTRY, "Congress-Trade").name, "Congress.Trade")
        self.assertEqual(K.resolve_app(REGISTRY, "socratic.trade").name, "Socratic-Trade")

    def test_unknown_app_lists_candidates(self) -> None:
        with self.assertRaises(K.Refusal) as cm:
            K.resolve_app(REGISTRY, "nonesuch")
        text = str(cm.exception)
        self.assertIn("unknown app 'nonesuch'", text)
        self.assertIn("DealDex  (folder DealDex, prefix dealdex, acronym DD)", text)
        with self.assertRaises(K.Refusal) as cm:
            K.resolve_app(REGISTRY, "deal")
        self.assertIn("DealDex", str(cm.exception))

    def test_ambiguous_app_lists_both(self) -> None:
        with self.assertRaises(K.Refusal) as cm:
            K.resolve_app(REGISTRY, "bt")
        text = str(cm.exception)
        self.assertIn("ambiguous", text)
        self.assertIn("Alpha", text)
        self.assertIn("Beta", text)

    def test_empty_app(self) -> None:
        for q in ("", "   "):
            with self.assertRaises(K.Refusal):
                K.resolve_app(REGISTRY, q)

    def test_seat_comes_only_from_agent_seat(self) -> None:
        self.assertEqual(K.resolve_seat({"AGENT_SEAT": "CLAUDE"}, REGISTRY).suffix, "claude")
        self.assertEqual(K.resolve_seat({"AGENT_SEAT": " ag "}, REGISTRY).suffix, "antigravity")
        self.assertEqual(K.resolve_seat({"AGENT_SEAT": "MM"}, REGISTRY).suffix, "minimax")
        self.assertEqual(K.resolve_seat({"AGENT_SEAT": "grok-build"}, REGISTRY).suffix, "grok-build")
        self.assertEqual(K.resolve_seat({"AGENT_SEAT": "CURSOR"}, REGISTRY).suffix, "cursor")

    def test_seat_refusals(self) -> None:
        for env, needle in (({}, "AGENT_SEAT is not set"), ({"AGENT_SEAT": ""}, "not set"), ({"AGENT_SEAT": "  "}, "not set"),
                            ({"AGENT_SEAT": "NOPE"}, "not a seat tag"), ({"AGENT_SEAT": "antigravity"}, "not a seat tag"),
                            ({"AGENT_SEAT": "minimax"}, "not a seat tag"), ({"AGENT_SEAT": "MONET"}, "retired"),
                            ({"AGENT_SEAT": "GROK-BOT"}, "belongs to CURSOR")):
            with self.subTest(env=env):
                with self.assertRaises(K.Refusal) as cm:
                    K.resolve_seat(env, REGISTRY)
                self.assertIn(needle, str(cm.exception))

    def test_a_long_seat_value_is_clipped_in_the_message(self) -> None:
        with self.assertRaises(K.Refusal) as cm:
            K.resolve_seat({"AGENT_SEAT": "X" * 500}, REGISTRY)
        self.assertLess(len(str(cm.exception)), 400)

    def test_branch_is_the_first_registry_prefix_plus_the_slug(self) -> None:
        self.assertEqual(K.branch_for(REGISTRY.seat_by_name("CLAUDE"), "fix-thing"), "claude/fix-thing")
        self.assertEqual(K.branch_for(REGISTRY.seat_by_name("AG"), "fix-thing"), "ag/fix-thing")
        self.assertEqual(K.branch_for(REGISTRY.seat_by_name("MM"), "fix-thing"), "minimax/fix-thing")
        self.assertEqual(K.branch_for(REGISTRY.seat_by_name("GROK-BOT"), "x"), "cursor/x")
        with self.assertRaises(K.Refusal):
            K.branch_for(REGISTRY.seat_by_name("ODD"), "x")
        for bad in ("Bad_Slug", "", "a--b", "-x", "x-", "x" * 41, "a b", "../x", "a/b"):
            with self.subTest(slug=bad):
                with self.assertRaises(K.Refusal):
                    K.branch_for(REGISTRY.seat_by_name("CLAUDE"), bad)

    def test_base_forms(self) -> None:
        self.assertEqual(K.parse_base(None), ("main", "origin/main"))
        self.assertEqual(K.parse_base(""), ("main", "origin/main"))
        self.assertEqual(K.parse_base("origin/release"), ("release", "origin/release"))
        self.assertEqual(K.parse_base("release"), ("release", "origin/release"))
        self.assertEqual(K.parse_base("origin/claude/x"), ("claude/x", "origin/claude/x"))
        self.assertEqual(K.parse_base("refs/remotes/origin/main"), ("main", "origin/main"))
        for bad in ("origin/a..b", "-x", "a:b", "origin/", "a b"):
            with self.assertRaises(K.Refusal):
                K.parse_base(bad)

    def test_the_real_registry_gives_every_live_seat_a_branch_prefix_and_folder_name(self) -> None:
        registry = L.load_registry(L.default_registry_path({}))
        live = [s for s in registry.seats if not s.retired]
        self.assertTrue(live)
        laneless = []
        for seat in live:
            with self.subTest(seat=seat.name):
                self.assertTrue(L.is_valid_slug(seat.suffix))
                if not seat.primary_branch_prefix():
                    # A cloud seat has no Mac lanes (GROK-WEB).  No prefix is how lane
                    # refuses it, before it makes a worktree.
                    laneless.append(seat.name)
                    with self.assertRaises(K.Refusal):
                        K.branch_for(seat, "x")
                    continue
                self.assertEqual(K.branch_for(seat, "x"), seat.primary_branch_prefix() + "/x")
        # Only seats the listener partition gives to the server may go without a prefix.
        partition = tomllib.loads(
            (SCRIPTS_DIR.parent / "docs" / "protocols" / "agent-sync-partition.toml").read_text(encoding="utf-8")
        )["seats"]
        for name in laneless:
            self.assertEqual(partition.get(name), "server", f"{name} has no branch prefix but is not a server seat")
        retired = {s.name for s in registry.seats if s.retired}
        for tag in ("MONET", "RENOIR", "HARNESS", "DSH", "KIMI"):
            self.assertIn(tag, retired)
            with self.assertRaises(K.Refusal):
                K.resolve_seat({"AGENT_SEAT": tag}, registry)

    def test_the_real_registry_gives_a_lane_only_to_the_seat_that_owns_each_folder_name(self) -> None:
        # CURSOR and GROK-BOT share worktreeSuffix `cursor` and prefix `cursor/`.  GROK-BOT is one
        # fleet-wide identity, not a per-app coding seat (TEMPLATE-AGENTS.md), so it gets no lane.
        registry = L.load_registry(L.default_registry_path({}))
        accepted, refused = set(), set()
        for seat in (s for s in registry.seats if not s.retired):
            try:
                accepted.add(K.resolve_seat({"AGENT_SEAT": seat.name}, registry).suffix)
            except K.Refusal:
                refused.add(seat.name)
        self.assertIn("GROK-BOT", refused)
        self.assertIn("CURSOR", {s.name for s in registry.seats if s.suffix in accepted})
        self.assertEqual(len(accepted), len([s for s in registry.seats if not s.retired]) - len(refused),
                         "no two accepted seats share a folder name")


# --------------------------------------------------------------------------- lane new

class LaneNewTests(WorldCase):
    def test_happy_path(self) -> None:
        w = self.w
        res = w.lane("new", "DealDex", "fix-thing", "--purpose", "Fix the thing", "--board", "a7dfde0e")
        path = w.lane_path()
        self.assertEqual((res.rc, res.out), (0, f"{path}\n"), res.err)
        self.assertIn("created", res.err)
        self.assertTrue(path.is_dir())
        self.assertEqual(w.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path), "claude/fix-thing")
        self.assertEqual(w.git(["rev-parse", "HEAD"], cwd=path), SHAS["main"])
        self.assertEqual(w.git(["status", "--porcelain"], cwd=path), "", "git status must be clean")
        self.assertEqual(w.git(["config", "--get", "branch.claude/fix-thing.remote"], cwd=path, check=False), "")
        self.assertEqual(L.classify_location(path, w.roots), L.LocationClass.LANE_NESTED)
        verdict = L.explain_lane_name(path, REGISTRY, w.roots)
        self.assertEqual((verdict.verdict, verdict.seat, verdict.slug), (L.NameVerdict.CONFORMING, "claude", "fix-thing"))
        self.assertEqual(L.check_branch_name("claude/fix-thing", REGISTRY), L.BranchVerdict.CONFORMING)
        listed = w.git(["worktree", "list", "--porcelain"], cwd=w.tree)
        self.assertIn(f"worktree {path}", listed)

    def test_manifest_lives_in_the_private_git_dir_and_has_the_agreed_fields(self) -> None:
        w = self.w
        w.lane("new", "DealDex", "fix-thing", "--purpose", "Fix the thing", "--board", "a7dfde0e")
        path = w.lane_path()
        git_dir = Path(w.git(["rev-parse", "--absolute-git-dir"], cwd=path))
        self.assertEqual(git_dir.parent, w.tree / ".git" / "worktrees")
        manifest = json.loads((git_dir / "lane.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest, {
            "schema": 1, "tool": "lane new", "kind": "lane", "seat": "claude", "tag": "CLAUDE", "app": "DealDex",
            "prefix": "dealdex", "lane_dir": "DealDex", "slug": "fix-thing", "branch": "claude/fix-thing",
            "base": "origin/main",
            "base_sha": SHAS["main"], "purpose": "Fix the thing", "board": "a7dfde0e",
            "created_at": "2026-10-07T16:15:00+00:00",
        })
        self.assertFalse((path / "lane.json").exists(), "the manifest must not be in the working tree")
        self.assertEqual(sorted(p.name for p in git_dir.glob("lane.json*")), ["lane.json"], "no temp file left behind")

    def test_json_output_is_the_manifest_plus_the_path_and_nothing_else_on_stdout(self) -> None:
        res = self.w.lane("new", "DealDex", "fix-thing", "--json")
        body = res.json()
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(body["path"], str(self.w.lane_path()))
        self.assertEqual(body["branch"], "claude/fix-thing")
        self.assertEqual(body["base_sha"], SHAS["main"])
        self.assertIsNone(body["purpose"])
        self.assertIsNone(body["board"])
        self.assertNotIn("existing", body)
        self.assertTrue(res.out.endswith("}\n"))
        self.assertIn("created", res.err)

    def test_it_fetches_the_base_first_and_branches_from_the_new_tip(self) -> None:
        w = self.w
        tip = w.advance_origin("main")
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(w.git(["rev-parse", "HEAD"], cwd=w.lane_path()), tip)
        names = [v[:4] if v[0] == "fetch" else v[:2] for v in res.verbs]
        self.assertLess(names.index(("fetch", "--no-tags", "origin", "main")), names.index(("worktree", "add")))
        add = [v for v in res.verbs if v[:2] == ("worktree", "add")][0]
        self.assertEqual(add[2:4], ("-b", "claude/fix-thing"))
        self.assertEqual(add[5], tip, "the lane starts at the exact sha that went into the manifest")

    def test_only_allowlisted_commands_run_and_none_pushes(self) -> None:
        res = self.w.lane("new", "DealDex", "fix-thing")
        self.assertEqual(res.rc, 0, res.err)
        for argv in res.calls:
            K.check_allowed(argv[:1] + argv[4:] if argv[0] == "git" else argv, write_root=self.w.roots.lanes_root,
                            fold=str.casefold)
        self.assertFalse(res.has("push"))
        self.assertFalse(res.has("worktree", "remove"))
        self.assertFalse(res.has("worktree", "prune"))

    def test_it_writes_nothing_outside_the_lanes_root_and_the_repo_git_dir(self) -> None:
        w = self.w
        before = tree_hash(w.root, skip=(w.roots.lanes_root, w.tree / ".git"))
        origin_before = tree_hash(w.origin)
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(tree_hash(w.root, skip=(w.roots.lanes_root, w.tree / ".git")), before)
        self.assertEqual(tree_hash(w.origin), origin_before, "origin must be untouched: lane never pushes")

    def test_it_never_creates_a_janitor_keep_marker(self) -> None:
        w = self.w
        w.lane("new", "DealDex", "fix-thing")
        w.lane("new", "DealDex", "--review", "--pr", "7")
        found = [p for p in w.root.rglob(".janitor-keep")]
        self.assertEqual(found, [])

    def test_a_lane_in_an_old_folder_is_found_and_not_duplicated(self) -> None:
        # A flat ~/apps/dealdex-claude-legacy lane has not migrated yet.  `lane new DealDex legacy` prints it,
        # exits 0 and makes nothing, instead of failing on "branch already exists" or making a second lane.
        w = self.w
        old = w.roots.apps_root / "dealdex-claude-legacy"
        w.git(["worktree", "add", "-q", "-b", "claude/legacy", str(old), "origin/main"], cwd=w.tree)
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "legacy")
        self.assertEqual((res.rc, res.out), (0, f"{old}\n"), res.err)
        self.assertIn("old folder", res.err)
        self.assertIn(str(w.lane_path("legacy")), res.err, "it says where the migration will put it")
        self.assertFalse(res.has("fetch"))
        self.assertFalse(res.has("worktree", "add"))
        self.assertNoChange(before)
        body = w.lane("new", "DealDex", "legacy", "--json").json()
        self.assertEqual((body["existing"], body["legacy_location"], body["path"]), (True, True, str(old)))
        self.assertEqual(w.lane("new", "DealDex", "legacy", "--reuse-branch").out, f"{old}\n")
        self.assertEqual(w.lane("new", "DealDex", "legacy", "--dry-run").out, f"{old}\n")
        self.assertFalse(w.lane_path("legacy").exists())
        # another seat's slug of the same name is its own lane, not this one
        other = w.lane("new", "DealDex", "legacy", env={"AGENT_SEAT": "AG"})
        self.assertEqual(other.rc, 0, other.err)
        self.assertEqual(other.out, f"{w.lane_path('legacy', seat='antigravity')}\n")

    def test_expected_path_uses_the_repo_folder_for_every_spelling_of_the_app(self) -> None:
        w = self.w
        for query in ("DealDex", "dealdex", "dd"):
            self.assertEqual(w.lane("path", query, "x").out, f"{w.lane_path('x')}\n", query)
        self.assertEqual(w.lane("path", "Congress-Trade", "x").out, f"{w.roots.lanes_root / 'Congress.Trade' / 'claude-x'}\n")
        self.assertEqual(w.lane("path", "ct", "x").out, f"{w.roots.lanes_root / 'Congress.Trade' / 'claude-x'}\n")
        self.assertEqual(w.lane("path", "trading", "x").out, f"{w.roots.lanes_root / 'Socratic-Trade' / 'claude-x'}\n")
        self.assertEqual(w.lane("path", "Socratic-Trade", "x").out, f"{w.roots.lanes_root / 'Socratic-Trade' / 'claude-x'}\n")

    def test_idempotent_second_run(self) -> None:
        w = self.w
        first = w.lane("new", "DealDex", "fix-thing", "--purpose", "Fix the thing")
        self.assertEqual(first.rc, 0, first.err)
        before = w.everything_hash()
        later = NOW + dt.timedelta(days=2)
        second = w.lane("new", "DealDex", "fix-thing", clock=lambda: later)
        self.assertEqual((second.rc, second.out), (0, f"{w.lane_path()}\n"), second.err)
        self.assertIn("already exists", second.err)
        self.assertNoChange(before)
        self.assertFalse(second.has("fetch"))
        self.assertFalse(second.has("worktree", "add"))
        again = w.lane("new", "DealDex", "fix-thing", "--json", clock=lambda: later)
        body = again.json()
        self.assertTrue(body["existing"])
        self.assertEqual(body["created_at"], "2026-10-07T16:15:00+00:00", "the first manifest is kept")
        self.assertEqual(body["purpose"], "Fix the thing")
        self.assertEqual(body["path"], str(w.lane_path()))

    def test_idempotent_run_survives_other_arguments(self) -> None:
        w = self.w
        w.lane("new", "DealDex", "fix-thing")
        for extra in (["--dry-run"], ["--base", "release"], ["--purpose", "other"]):
            with self.subTest(extra=extra):
                res = w.lane("new", "dd", "fix-thing", *extra)
                self.assertEqual((res.rc, res.out), (0, f"{w.lane_path()}\n"), res.err)

    def test_seat_unset_is_refused_before_anything_runs(self) -> None:
        w = self.w
        before = w.everything_hash()
        for env in ({"AGENT_SEAT": None}, {"AGENT_SEAT": ""}, {"AGENT_SEAT": "   "}):
            with self.subTest(env=env):
                res = w.lane("new", "DealDex", "fix-thing", env=env)
                self.assertEqual((res.rc, res.out), (64, ""))
                self.assertIn("AGENT_SEAT is not set", res.err)
                self.assertEqual(res.calls, [], "no git or gh before the seat is known")
        self.assertNoChange(before)

    def test_unknown_seat_and_retired_seat_are_refused(self) -> None:
        w = self.w
        before = w.everything_hash()
        for tag, needle in (("NOPE", "not a seat tag"), ("antigravity", "not a seat tag"), ("MONET", "retired"),
                            ("ODD", "no branch prefix")):
            with self.subTest(tag=tag):
                res = w.lane("new", "DealDex", "fix-thing", env={"AGENT_SEAT": tag})
                self.assertEqual((res.rc, res.out), (64, ""), res.err)
                self.assertIn(needle, res.err)
                self.assertEqual(res.calls, [])
        self.assertNoChange(before)

    def test_the_process_environment_never_supplies_the_seat(self) -> None:
        with mock.patch.dict(os.environ, {"AGENT_SEAT": "CLAUDE"}):
            res = self.w.lane("new", "DealDex", "fix-thing", env={"AGENT_SEAT": None})
        self.assertEqual(res.rc, 64)
        self.assertIn("not set", res.err)

    def test_each_seat_gets_its_whole_name_folder_and_registry_branch(self) -> None:
        w = self.w
        cases = (("CLAUDE", "claude", "claude/x"), ("AG", "antigravity", "ag/x"), ("MM", "minimax", "minimax/x"),
                 ("GROK", "grok", "grok/x"), ("GROK-BUILD", "grok-build", "grok-build/x"),
                 ("CLUTCH", "clutch", "clutch/x"), ("CURSOR", "cursor", "cursor/x"))
        for tag, folder, branch in cases:
            with self.subTest(tag=tag):
                res = w.lane("new", "DealDex", "x", "--dry-run", "--json", env={"AGENT_SEAT": tag})
                body = res.json()
                self.assertEqual(res.rc, 0, res.err)
                self.assertEqual((body["seat"], body["tag"], body["branch"]), (folder, tag, branch))
                self.assertEqual(body["path"], str(w.roots.lanes_root / "DealDex" / f"{folder}-x"))

    def test_a_slug_that_reads_as_another_seat_is_refused(self) -> None:
        # GROK + slug build-x would be folder grok-build-x, which the layout reads as GROK-BUILD's.
        w = self.w
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "build-x", env={"AGENT_SEAT": "GROK"})
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("read as seat", res.err)
        self.assertNoChange(before)
        ok = w.lane("new", "DealDex", "build-x", "--dry-run", env={"AGENT_SEAT": "GROK-BUILD"})
        self.assertEqual(ok.rc, 0, ok.err)

    def test_a_seat_that_shares_another_seats_folder_name_never_gets_that_seats_lane(self) -> None:
        # CURSOR and GROK-BOT share worktreeSuffix `cursor`; the folder and cursor/ branch are CURSOR's.
        w = self.w
        first = w.lane("new", "DealDex", "x", env={"AGENT_SEAT": "CURSOR"})
        path = w.lane_path("x", seat="cursor")
        self.assertEqual((first.rc, first.out), (0, f"{path}\n"), first.err)
        before = w.everything_hash()
        for argv in (("new", "DealDex", "x"), ("new", "DealDex", "x", "--dry-run"), ("new", "DealDex", "y"),
                     ("path", "DealDex", "x"), ("new", "DealDex", "--review", "--pr", "7")):
            with self.subTest(argv=argv):
                res = w.lane(*argv, env={"AGENT_SEAT": "GROK-BOT"})
                self.assertEqual((res.rc, res.out), (64, ""), res.err)
                self.assertIn("belongs to CURSOR", res.err)
                self.assertEqual(res.calls, [], "refused before any git or gh")
        self.assertNoChange(before)

    def test_an_existing_lane_whose_manifest_names_another_seat_is_not_handed_over(self) -> None:
        w = self.w
        self.assertEqual(w.lane("new", "DealDex", "fix-thing").rc, 0)
        manifest_path = Path(w.git(["rev-parse", "--absolute-git-dir"], cwd=w.lane_path())) / "lane.json"
        body = json.loads(manifest_path.read_text(encoding="utf-8"))
        body["tag"] = "CODEX"
        manifest_path.write_text(json.dumps(body), encoding="utf-8")
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("CODEX", res.err)
        self.assertNoChange(before)

    def test_a_review_checkout_that_names_another_seat_is_not_handed_over_either(self) -> None:
        # A review has a way out a lane does not: the second seat gets review-pr-<n>-<seat>.  It is never
        # handed the first seat's folder, and the first seat's folder is left exactly as it was.
        w = self.w
        self.assertEqual(w.lane("new", "DealDex", "--review", "--pr", "7").rc, 0)
        plain = w.review_path()
        manifest_path = Path(w.git(["rev-parse", "--absolute-git-dir"], cwd=plain)) / "lane.json"
        body = json.loads(manifest_path.read_text(encoding="utf-8"))
        body["tag"] = "CODEX"
        manifest_path.write_text(json.dumps(body), encoding="utf-8")
        plain_before = manifest_path.read_text(encoding="utf-8")
        res = w.lane("new", "DealDex", "--review", "--pr", "7")
        self.assertEqual((res.rc, res.out), (0, f"{w.review_path(seat='claude')}\n"), res.err)
        self.assertEqual(manifest_path.read_text(encoding="utf-8"), plain_before)
        again = w.lane("new", "DealDex", "--review", "--pr", "7")
        self.assertEqual((again.rc, again.out), (0, f"{w.review_path(seat='claude')}\n"), again.err)
        self.assertIn("already exists", again.err)
        # the owner itself still gets the plain folder back
        codex = w.lane("new", "DealDex", "--review", "--pr", "7", env={"AGENT_SEAT": "CLUTCH"})
        self.assertEqual(codex.out, f"{w.review_path(seat='clutch')}\n", "a third seat gets its own folder")

    def test_bad_slugs_are_refused(self) -> None:
        w = self.w
        before = w.everything_hash()
        for slug in ("Bad_Slug", "UPPER", "a--b", "x-", "x" * 41, "a b", "../x", "a/b", "\u00e4"):
            with self.subTest(slug=slug):
                res = w.lane("new", "DealDex", slug)
                self.assertEqual((res.rc, res.out), (64, ""), res.err)
                self.assertIn("slug", res.err)
        with contextlib.redirect_stderr(io.StringIO()):      # argparse reads a leading dash as an option
            self.assertEqual(w.lane("new", "DealDex", "-x").rc, 64)
        self.assertNoChange(before)
        self.assertEqual(w.lane("new", "DealDex").rc, 64)
        self.assertEqual(w.lane("new", "DealDex", "").rc, 64)

    def test_existing_local_branch_is_refused(self) -> None:
        w = self.w
        w.git(["branch", "claude/fix-thing", "origin/main"], cwd=w.tree)
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("already exists locally", res.err)
        self.assertIn("--reuse-branch", res.err)
        self.assertNoChange(before)
        self.assertFalse(res.has("fetch"), "refused offline, before any network step")

    def test_branch_known_on_origin_from_an_earlier_fetch_is_refused_offline(self) -> None:
        w = self.w
        w.git(["--git-dir", str(w.origin), "update-ref", "refs/heads/claude/fix-thing", SHAS["main"]])
        w.git(["fetch", "-q", "origin", "claude/fix-thing"], cwd=w.tree)
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("on origin", res.err)
        self.assertFalse(res.has("fetch"))
        self.assertFalse(w.lane_path().exists())

    def test_branch_that_exists_only_on_origin_is_found_by_asking_origin(self) -> None:
        w = self.w
        w.git(["--git-dir", str(w.origin), "update-ref", "refs/heads/claude/fix-thing", SHAS["main"]])
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("already exists on origin", res.err)
        self.assertFalse(w.lane_path().exists())
        self.assertFalse(res.has("worktree", "add"))

    def test_branch_name_collisions_with_parents_and_children_are_refused(self) -> None:
        w = self.w
        w.git(["branch", "claude", "origin/main"], cwd=w.tree)
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("'claude' exists", res.err)
        w.git(["branch", "-D", "claude"], cwd=w.tree)
        w.git(["branch", "claude/fix-thing/deeper", "origin/main"], cwd=w.tree)
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("blocks", res.err)

    def test_existing_foreign_path_is_refused_and_left_alone(self) -> None:
        w = self.w
        path = w.lane_path()
        path.mkdir(parents=True)
        (path / "precious.txt").write_text("keep me\n", encoding="utf-8")
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("already exists and is not a worktree", res.err)
        self.assertNoChange(before)
        self.assertEqual((path / "precious.txt").read_text(encoding="utf-8"), "keep me\n")
        self.assertFalse(res.has("fetch"))

    def test_existing_symlink_at_the_path_is_refused(self) -> None:
        w = self.w
        path = w.lane_path()
        path.parent.mkdir(parents=True)
        os.symlink(w.root, path)
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual(res.rc, 64, res.err)
        self.assertTrue(os.path.islink(path))

    def test_existing_worktree_on_another_branch_is_refused_and_untouched(self) -> None:
        w = self.w
        path = w.lane_path()
        path.parent.mkdir(parents=True)
        w.git(["worktree", "add", "-q", "-b", "claude/other", str(path), "origin/main"], cwd=w.tree)
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("claude/other", res.err)
        self.assertNoChange(before)

    def test_a_registered_worktree_whose_folder_is_missing_is_refused_with_a_prune_hint(self) -> None:
        w = self.w
        w.lane("new", "DealDex", "fix-thing")
        shutil.rmtree(w.lane_path())
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("git worktree prune", res.err)
        self.assertNoChange(before)
        self.assertIn(str(w.lane_path()), w.git(["worktree", "list", "--porcelain"], cwd=w.tree), "not pruned by lane")

    def test_reuse_branch_checks_out_an_existing_local_branch(self) -> None:
        w = self.w
        w.git(["branch", "claude/old-work", "origin/release"], cwd=w.tree)
        res = w.lane("new", "DealDex", "old-work", "--reuse-branch", "--json")
        body = res.json()
        self.assertEqual(res.rc, 0, res.err)
        path = w.lane_path("old-work")
        self.assertEqual(body["path"], str(path))
        self.assertEqual(w.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path), "claude/old-work")
        self.assertEqual(w.git(["rev-parse", "HEAD"], cwd=path), SHAS["release"])
        self.assertEqual(body["base_sha"], SHAS["release"])
        self.assertIsNone(body["base"])
        again = w.lane("new", "DealDex", "old-work", "--reuse-branch")
        self.assertEqual((again.rc, again.out), (0, f"{path}\n"))

    def test_reuse_branch_can_take_a_branch_that_only_exists_on_origin(self) -> None:
        w = self.w
        w.git(["--git-dir", str(w.origin), "update-ref", "refs/heads/claude/remote-work", SHAS["release"]])
        res = w.lane("new", "DealDex", "remote-work", "--reuse-branch")
        self.assertEqual(res.rc, 0, res.err)
        path = w.lane_path("remote-work")
        self.assertEqual(w.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path), "claude/remote-work")
        self.assertEqual(w.git(["rev-parse", "HEAD"], cwd=path), SHAS["release"])

    def test_reuse_branch_refusals(self) -> None:
        w = self.w
        res = w.lane("new", "DealDex", "nothing-here", "--reuse-branch")
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("is not on origin and not local", res.err)
        self.assertFalse(w.lane_path("nothing-here").exists())
        res = w.lane("new", "DealDex", "x", "--reuse-branch", "--base", "release")
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("--base has no effect", res.err)
        w.git(["branch", "claude/busy", "origin/main"], cwd=w.tree)
        other = w.root / "elsewhere-checkout"
        w.git(["worktree", "add", "-q", str(other), "claude/busy"], cwd=w.tree)
        res = w.lane("new", "DealDex", "busy", "--reuse-branch")
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("already checked out", res.err)
        self.assertFalse(w.lane_path("busy").exists())

    def test_base_option_selects_the_branch_to_start_from(self) -> None:
        w = self.w
        for base in ("origin/release", "release"):
            with self.subTest(base=base):
                slug = "from-" + base.replace("/", "-")
                res = w.lane("new", "DealDex", slug, "--base", base, "--json")
                self.assertEqual(res.rc, 0, res.err)
                self.assertEqual(res.json()["base"], "origin/release")
                self.assertEqual(w.git(["rev-parse", "HEAD"], cwd=w.lane_path(slug)), SHAS["release"])

    def test_the_base_is_the_remote_tracking_ref_never_a_same_named_local_branch_or_tag(self) -> None:
        # `origin/main` alone is a DWIM name: refs/tags/origin/main and refs/heads/origin/main both win
        # over refs/remotes/origin/main.  A stale decoy must not become the lane's base.
        for kind in ("branch", "tag"):
            with self.subTest(kind=kind):
                w = World(self)
                stale = w.git(["rev-parse", "origin/main~1"], cwd=w.tree)
                self.assertNotEqual(stale, SHAS["main"])
                w.git([kind, "origin/main", stale], cwd=w.tree)
                dry = w.lane("new", "DealDex", "fix-thing", "--dry-run", "--json")
                self.assertEqual(dry.rc, 0, dry.err)
                self.assertEqual(dry.json()["base_sha"], SHAS["main"])
                res = w.lane("new", "DealDex", "fix-thing", "--json")
                self.assertEqual(res.rc, 0, res.err)
                self.assertEqual(res.json()["base_sha"], SHAS["main"])
                self.assertEqual(res.json()["base"], "origin/main")
                self.assertEqual(w.git(["rev-parse", "HEAD"], cwd=w.lane_path()), SHAS["main"])
                self.assertIn(SHAS["main"][:9], res.err)

    def test_missing_base_branch_is_an_external_failure(self) -> None:
        w = self.w
        before = tree_hash(w.root, skip=(w.tree / ".git",))
        res = w.lane("new", "DealDex", "fix-thing", "--base", "no-such-branch")
        self.assertEqual((res.rc, res.out), (69, ""), res.err)
        self.assertIn("fetch", res.err)
        self.assertFalse(w.lane_path().exists())
        self.assertFalse(res.has("worktree", "add"))
        self.assertEqual(tree_hash(w.root, skip=(w.tree / ".git",)), before)

    def test_fetch_failure_is_exit_69_and_creates_nothing(self) -> None:
        w = self.w
        w.git(["remote", "set-url", "origin", (w.root / "no-such-origin.git").as_uri()], cwd=w.tree)
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (69, ""), res.err)
        self.assertIn("lane: error:", res.err)
        self.assertFalse(w.lane_path().exists())
        self.assertFalse((w.roots.lanes_root).exists(), "no folder is created before the fetch works")

    def test_credentials_in_git_errors_are_redacted(self) -> None:
        w = self.w

        def failing(argv: List[str]) -> Optional[D.CmdResult]:
            if "fetch" in argv:
                return D.CmdResult(128, "", "fatal: unable to access 'https://user:s3cretTOKEN@example.invalid/r.git/': nope\n")
            return None

        res = w.lane("new", "DealDex", "fix-thing", exec_override=failing)
        self.assertEqual(res.rc, 69)
        self.assertNotIn("s3cretTOKEN", res.err)
        self.assertIn("REDACTED", res.err)

    def test_worktree_add_failure_is_exit_69(self) -> None:
        w = self.w

        def failing(argv: List[str]) -> Optional[D.CmdResult]:
            if argv[4:6] == ["worktree", "add"]:
                return D.CmdResult(128, "", "fatal: disk full\n")
            return None

        res = w.lane("new", "DealDex", "fix-thing", exec_override=failing)
        self.assertEqual((res.rc, res.out), (69, ""), res.err)
        self.assertIn("disk full", res.err)

    def test_a_manifest_problem_is_only_a_warning(self) -> None:
        w = self.w
        with mock.patch.object(K.os, "replace", side_effect=OSError("read-only")):
            res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (0, f"{w.lane_path()}\n"), res.err)
        self.assertIn("warning: lane.json was not written", res.err)
        git_dir = Path(w.git(["rev-parse", "--absolute-git-dir"], cwd=w.lane_path()))
        self.assertEqual(list(git_dir.glob("lane.json*")), [], "no stray temp file")

    def test_dry_run_changes_nothing(self) -> None:
        w = self.w
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "fix-thing", "--dry-run")
        self.assertEqual((res.rc, res.out), (0, f"{w.lane_path()}\n"), res.err)
        self.assertIn("dry run", res.err)
        self.assertNoChange(before)
        self.assertFalse(w.roots.lanes_root.exists())
        self.assertFalse(res.has("fetch"))
        self.assertFalse(res.has("worktree", "add"))
        body = w.lane("new", "DealDex", "fix-thing", "--dry-run", "--json").json()
        self.assertTrue(body["dry_run"])
        self.assertEqual(body["base_sha"], SHAS["main"], "from the origin/main already on disk")
        self.assertNoChange(before)
        missing = w.lane("new", "DealDex", "fix-thing", "--dry-run", "--reuse-branch")
        self.assertEqual(missing.rc, 0, missing.err)
        self.assertIn("a real run would fetch it", missing.err)
        self.assertNoChange(before)

    def test_dry_run_still_applies_every_offline_refusal(self) -> None:
        w = self.w
        w.git(["branch", "claude/fix-thing", "origin/main"], cwd=w.tree)
        self.assertEqual(w.lane("new", "DealDex", "fix-thing", "--dry-run").rc, 64)
        self.assertEqual(w.lane("new", "DealDex", "Bad", "--dry-run").rc, 64)
        self.assertEqual(w.lane("new", "DealDex", "x", "--dry-run", env={"AGENT_SEAT": None}).rc, 64)

    def test_alias_app_lookup_gives_the_same_lane(self) -> None:
        w = self.w
        for query in ("DealDex", "dealdex", "DEALDEX", "dd", "DD"):
            with self.subTest(query=query):
                res = w.lane("new", query, "fix-thing", "--dry-run")
                self.assertEqual((res.rc, res.out), (0, f"{w.lane_path()}\n"), res.err)

    def test_unknown_and_ambiguous_apps_exit_64_with_candidates_and_run_nothing(self) -> None:
        w = self.w
        res = w.lane("new", "nonesuch", "x")
        self.assertEqual((res.rc, res.out, res.calls), (64, "", []))
        self.assertIn("unknown app", res.err)
        self.assertIn("DealDex", res.err)
        res = w.lane("new", "bt", "x")
        self.assertEqual((res.rc, res.out, res.calls), (64, "", []))
        self.assertIn("ambiguous", res.err)
        self.assertIn("Alpha", res.err)
        self.assertIn("Beta", res.err)

    def test_app_without_an_integration_tree_is_refused_with_an_explanation(self) -> None:
        res = self.w.lane("new", "upptime-status", "x")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("no integration tree", res.err)
        self.assertEqual(res.calls, [])

    def test_missing_or_broken_integration_trees_are_refused(self) -> None:
        w = self.w
        res = w.lane("new", "Socratic-Trade", "x")
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("does not exist", res.err)
        plain = w.home / "Code" / "Socratic-Trade"
        plain.mkdir()
        res = w.lane("new", "Socratic-Trade", "x")
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("not the top of a git repository", res.err)
        self.assertEqual(sorted(p.name for p in plain.iterdir()), [])
        w.git(["init", "-q", str(plain)])
        res = w.lane("new", "Socratic-Trade", "x")
        self.assertEqual(res.rc, 64, res.err)
        self.assertIn("no 'origin' remote", res.err)
        self.assertFalse(w.roots.lanes_root.exists())

    def test_an_app_without_a_fleet_apps_json_row_is_refused(self) -> None:
        # lane-map.md: an app with lanes but no registry row is added to the registry before it gets lanes.
        w = self.w
        tree = w.home / "Code" / "CodeCaps"
        w.git(["init", "-q", str(tree)])
        w.git(["remote", "add", "origin", w.origin.as_uri()], cwd=tree)
        before = w.everything_hash()
        for argv in (("new", "codecaps", "x"), ("new", "codecaps", "x", "--dry-run"), ("new", "codecaps", "--review", "--pr", "7"),
                     ("path", "codecaps", "x")):
            with self.subTest(argv=argv):
                res = w.lane(*argv)
                self.assertEqual((res.rc, res.out, res.calls), (64, "", []), res.err)
                self.assertIn("has no row in fleet-apps.json", res.err)
        self.assertNoChange(before)
        # the missing-tree explanation wins when both apply
        res = w.lane("new", "upptime-status", "x")
        self.assertIn("no integration tree", res.err)

    def test_flat_layout_mode(self) -> None:
        w = self.w
        res = w.lane("new", "DealDex", "fix-thing", env={"FLEET_LAYOUT": "flat"})
        path = w.roots.apps_root / "dealdex-claude-fix-thing"
        self.assertEqual((res.rc, res.out), (0, f"{path}\n"), res.err)
        flat_roots = L.make_roots(w.home, {**w.env, "FLEET_LAYOUT": "flat"}, registry=REGISTRY)
        self.assertEqual(L.classify_location(path, flat_roots), L.LocationClass.LANE_FLAT)
        self.assertEqual(w.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path), "claude/fix-thing")
        self.assertFalse(w.roots.lanes_root.exists(), "nothing is created under apps/lanes in flat mode")
        self.assertEqual(w.git(["status", "--porcelain"], cwd=path), "")
        again = w.lane("new", "DealDex", "fix-thing", env={"FLEET_LAYOUT": "flat"})
        self.assertEqual((again.rc, again.out), (0, f"{path}\n"))

    def test_outside_the_map_is_refused(self) -> None:
        w = self.w
        if str(w.root).startswith(("/var/tmp", "/private/var/tmp")):
            self.skipTest("the test home itself is under /var/tmp")
        before = w.everything_hash()
        outside = f"/var/tmp/lane-test-no-such-dir-{os.getpid()}"
        cases = (
            ({"FLEET_LANES_ROOT": outside}, "FORBIDDEN_TMP"),
            ({"FLEET_LANES_ROOT": "~/Code/lanes"}, "inside"),
            ({"FLEET_LANES_ROOT": "~/.codex/worktrees"}, "MANAGED"),
            ({"FLEET_LANES_ROOT": str(w.tree)}, "inside"),
        )
        for env, needle in cases:
            with self.subTest(env=env):
                res = w.lane("new", "DealDex", "fix-thing", env=env)
                self.assertEqual((res.rc, res.out), (64, ""), res.err)
                self.assertIn(needle, res.err)
        self.assertFalse(os.path.exists(outside))
        self.assertNoChange(before)

    def test_lane_only_creates_folders_at_or_below_the_lanes_root(self) -> None:
        w = self.w
        shutil.rmtree(w.home / "apps")
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("does not exist", res.err)
        self.assertNoChange(before)
        self.assertFalse(res.has("fetch"), "refused before any network step")
        (w.home / "apps").mkdir()
        self.assertEqual(w.lane("new", "DealDex", "fix-thing").rc, 0)

    def test_a_symlinked_prefix_folder_that_leaves_the_map_is_refused(self) -> None:
        w = self.w
        elsewhere = w.root / "elsewhere"
        elsewhere.mkdir()
        (w.roots.lanes_root).mkdir(parents=True)
        os.symlink(elsewhere, w.roots.lanes_root / "DealDex")
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("outside the lane map", res.err)
        self.assertEqual(list(elsewhere.iterdir()), [])
        self.assertNoChange(before)

    def test_a_classification_other_than_a_lane_is_refused(self) -> None:
        w = self.w
        with mock.patch.object(K.L, "classify_location", return_value=L.LocationClass.UNSANCTIONED):
            res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("outside the lane map", res.err)
        self.assertEqual(res.calls, [])

    def test_a_lane_cannot_be_placed_inside_another_checkout(self) -> None:
        w = self.w
        prefix_dir = w.roots.lanes_root / "DealDex"
        prefix_dir.mkdir(parents=True)
        w.git(["init", "-q", str(prefix_dir)])
        res = w.lane("new", "DealDex", "fix-thing")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("itself a git checkout", res.err)

    def test_purpose_and_board_are_validated(self) -> None:
        w = self.w
        for argv in (["--purpose", "line one\nline two"], ["--purpose", "x" * 301], ["--board", "has space"],
                     ["--board", "x" * 65], ["--board", "-leading"]):
            with self.subTest(argv=argv):
                res = w.lane("new", "DealDex", "fix-thing", *argv)
                self.assertEqual(res.rc, 64, res.err)
        self.assertFalse(w.roots.lanes_root.exists())

    def test_pr_flag_without_review_and_review_flag_misuse(self) -> None:
        w = self.w
        self.assertEqual(w.lane("new", "DealDex", "x", "--pr", "7").rc, 64)
        self.assertEqual(w.lane("new", "DealDex", "--review").rc, 64)
        self.assertEqual(w.lane("new", "DealDex", "x", "--review", "--pr", "7").rc, 64)
        self.assertEqual(w.lane("new", "DealDex", "--review", "--pr", "7", "--reuse-branch").rc, 64)
        self.assertEqual(w.lane("new", "DealDex", "--review", "--pr", "7", "--base", "release").rc, 64)
        for bad in ("0", "-1", "abc", "1.5", "99999999999"):
            with self.subTest(pr=bad):
                self.assertEqual(w.lane("new", "DealDex", "--review", "--pr", bad).rc, 64)

    def test_a_bad_layout_setting_warns_and_falls_back_to_nested(self) -> None:
        res = self.w.lane("new", "DealDex", "fix-thing", "--dry-run", env={"FLEET_LAYOUT": "sideways"})
        self.assertEqual((res.rc, res.out), (0, f"{self.w.lane_path()}\n"), res.err)
        self.assertIn("unrecognized FLEET_LAYOUT", res.err)

    def test_an_unreadable_registry_is_a_refusal(self) -> None:
        res = self.w.lane("new", "DealDex", "x", env={"FLEET_APPS_JSON": str(self.w.root / "missing.json")})
        self.assertEqual((res.rc, res.out), (64, ""))
        self.assertIn("cannot read the registry", res.err)


class PathCommandTests(WorldCase):
    def test_prints_the_would_be_path_without_side_effects(self) -> None:
        w = self.w
        before = w.everything_hash()
        res = w.lane("path", "dd", "fix-thing")
        self.assertEqual((res.rc, res.out, res.calls), (0, f"{w.lane_path()}\n", []))
        self.assertNoChange(before)
        flat = w.lane("path", "DealDex", "fix-thing", env={"FLEET_LAYOUT": "flat"})
        self.assertEqual(flat.out, f"{w.roots.apps_root / 'dealdex-claude-fix-thing'}\n")

    def test_refusals(self) -> None:
        w = self.w
        self.assertEqual(w.lane("path", "DealDex", "fix-thing", env={"AGENT_SEAT": None}).rc, 64)
        self.assertEqual(w.lane("path", "DealDex", "Bad Slug").rc, 64)
        self.assertEqual(w.lane("path", "nonesuch", "x").rc, 64)
        res = w.lane("path", "upptime-status", "x")
        self.assertEqual(res.rc, 64)
        self.assertIn("no integration tree", res.err)
        self.assertEqual(w.lane("path", "DealDex").rc, 64)


# --------------------------------------------------------------------------- review checkouts

class ReviewTests(WorldCase):
    def test_happy_path(self) -> None:
        w = self.w
        res = w.lane("new", "DealDex", "--review", "--pr", "7", "--purpose", "Check the PR", "--json")
        path = w.review_path()
        body = res.json()
        self.assertEqual(res.rc, 0, res.err)
        self.assertEqual(body["path"], str(path))
        self.assertEqual(w.git(["rev-parse", "HEAD"], cwd=path), SHAS["pr"])
        self.assertEqual(w.git(["symbolic-ref", "-q", "HEAD"], cwd=path, check=False), "", "HEAD is detached")
        self.assertEqual(w.git(["status", "--porcelain"], cwd=path), "")
        self.assertEqual(L.classify_location(path, w.roots), L.LocationClass.REVIEW)
        git_dir = Path(w.git(["rev-parse", "--absolute-git-dir"], cwd=path))
        manifest = json.loads((git_dir / "lane.json").read_text(encoding="utf-8"))
        expected = {
            "schema": 1, "tool": "lane new", "kind": "review", "seat": "claude", "tag": "CLAUDE", "app": "DealDex",
            "prefix": "dealdex", "lane_dir": "DealDex", "pr": 7, "head_sha": SHAS["pr"], "head_ref": "feature/pr-work",
            "pr_state": "OPEN", "cross_repo": False, "purpose": "Check the PR", "board": None,
            "created_at": "2026-10-07T16:15:00+00:00", "expires_at": "2026-10-14T16:15:00+00:00",
        }
        self.assertEqual(manifest, expected)
        self.assertEqual({k: v for k, v in body.items() if k != "path"}, expected)
        self.assertFalse((path / "lane.json").exists())

    def test_plain_output_is_the_path_alone(self) -> None:
        res = self.w.lane("new", "dd", "--review", "--pr", "7")
        self.assertEqual((res.rc, res.out), (0, f"{self.w.review_path()}\n"), res.err)
        self.assertIn("review checkout", res.err)

    def test_the_exact_gh_and_git_calls(self) -> None:
        res = self.w.lane("new", "DealDex", "--review", "--pr", "7")
        self.assertEqual(res.rc, 0, res.err)
        self.assertIn(("gh", "pr", "view", "7", "--repo", "Simple-With-Us/DealDex", "--json",
                       "headRefOid,headRefName,state,isCrossRepository"), res.verbs)
        self.assertIn(("fetch", "--no-tags", "origin", "pull/7/head"), res.verbs)
        add = [v for v in res.verbs if v[:2] == ("worktree", "add")]
        self.assertEqual(add, [("worktree", "add", "--detach", str(self.w.review_path()), SHAS["pr"])])
        for argv in res.calls:
            K.check_allowed(argv[:1] + argv[4:] if argv[0] == "git" else argv, write_root=self.w.roots.lanes_root,
                            fold=str.casefold)

    def test_second_run_is_idempotent_and_does_not_update(self) -> None:
        w = self.w
        w.lane("new", "DealDex", "--review", "--pr", "7")
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "--review", "--pr", "7", clock=lambda: NOW + dt.timedelta(days=3))
        self.assertEqual((res.rc, res.out), (0, f"{w.review_path()}\n"), res.err)
        self.assertIn("already exists", res.err)
        self.assertNoChange(before)
        self.assertEqual(res.calls[0][0], "git")
        self.assertFalse(any(c[0] == "gh" for c in res.calls), "no network on an idempotent run")
        self.assertFalse(res.has("fetch"))

    def test_two_seats_get_separate_checkouts_of_the_same_pr(self) -> None:
        # Layout v2 names the check review-pr-<n>, with no seat.  The first seat gets that name and a
        # second seat gets review-pr-<n>-<seat>, so no seat is ever refused another seat's checkout.
        w = self.w
        first = w.lane("new", "DealDex", "--review", "--pr", "7")
        self.assertEqual((first.rc, first.out), (0, f"{w.review_path()}\n"), first.err)
        res = w.lane("new", "DealDex", "--review", "--pr", "7", env={"AGENT_SEAT": "AG"})
        self.assertEqual((res.rc, res.out), (0, f"{w.review_path(seat='antigravity')}\n"), res.err)
        self.assertTrue(w.review_path().is_dir())
        self.assertTrue(w.review_path(seat="antigravity").is_dir())
        self.assertEqual(L.classify_location(w.review_path(seat="antigravity"), w.roots), L.LocationClass.REVIEW)
        # reruns are idempotent for both seats and each finds its own folder
        again = w.lane("new", "DealDex", "--review", "--pr", "7")
        self.assertEqual((again.rc, again.out), (0, f"{w.review_path()}\n"), again.err)
        again = w.lane("new", "DealDex", "--review", "--pr", "7", env={"AGENT_SEAT": "AG"})
        self.assertEqual((again.rc, again.out), (0, f"{w.review_path(seat='antigravity')}\n"), again.err)
        self.assertIn("already exists", again.err)
        # the manifests say who owns which
        for path, seat in ((w.review_path(), "claude"), (w.review_path(seat="antigravity"), "antigravity")):
            git_dir = Path(w.git(["rev-parse", "--absolute-git-dir"], cwd=path))
            self.assertEqual(json.loads((git_dir / "lane.json").read_text(encoding="utf-8"))["seat"], seat)
        # `path --review` agrees with `new --review` for both seats and writes nothing
        self.assertEqual(w.lane("path", "DealDex", "--review", "--pr", "7").out, f"{w.review_path()}\n")
        self.assertEqual(w.lane("path", "DealDex", "--review", "--pr", "7", env={"AGENT_SEAT": "AG"}).out,
                         f"{w.review_path(seat='antigravity')}\n")
        # a third seat sees the plain name taken by someone else and gets its own
        third = w.lane("new", "DealDex", "--review", "--pr", "7", env={"AGENT_SEAT": "MM"})
        self.assertEqual((third.rc, third.out), (0, f"{w.review_path(seat='minimax')}\n"), third.err)

    def test_a_seat_that_made_the_suffixed_checkout_keeps_finding_it_when_the_plain_one_is_gone(self) -> None:
        w = self.w
        w.lane("new", "DealDex", "--review", "--pr", "7")
        w.lane("new", "DealDex", "--review", "--pr", "7", env={"AGENT_SEAT": "AG"})
        w.git(["worktree", "remove", "--force", str(w.review_path())], cwd=w.tree)
        res = w.lane("new", "DealDex", "--review", "--pr", "7", env={"AGENT_SEAT": "AG"})
        self.assertEqual((res.rc, res.out), (0, f"{w.review_path(seat='antigravity')}\n"), res.err)
        self.assertFalse(w.review_path().exists())

    def test_the_review_checkout_sits_beside_the_lanes_of_the_same_repo(self) -> None:
        w = self.w
        w.lane("new", "DealDex", "fix-thing")
        w.lane("new", "DealDex", "--review", "--pr", "7")
        self.assertEqual(sorted(p.name for p in (w.roots.lanes_root / "DealDex").iterdir()),
                         ["claude-fix-thing", "review-pr-7"])
        self.assertEqual(sorted(p.name for p in w.roots.lanes_root.iterdir()), ["DealDex"],
                         "no _review folder, no prefix folder")

    def test_path_review_prints_the_path_and_changes_nothing(self) -> None:
        w = self.w
        before = w.everything_hash()
        res = w.lane("path", "dd", "--review", "--pr", "7")
        self.assertEqual((res.rc, res.out), (0, f"{w.review_path()}\n"), res.err)
        self.assertNoChange(before)
        for argv in (["path", "DealDex", "--review"], ["path", "DealDex", "x", "--review", "--pr", "7"],
                     ["path", "DealDex", "x", "--pr", "7"], ["path", "DealDex"]):
            self.assertEqual(w.lane(*argv).rc, 64, argv)

    def test_dry_run_changes_nothing_and_skips_the_network(self) -> None:
        w = self.w
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "--review", "--pr", "7", "--dry-run")
        self.assertEqual((res.rc, res.out), (0, f"{w.review_path()}\n"), res.err)
        self.assertFalse(any(c[0] == "gh" for c in res.calls))
        self.assertFalse(res.has("fetch"))
        self.assertNoChange(before)
        body = w.lane("new", "DealDex", "--review", "--pr", "7", "--dry-run", "--json").json()
        self.assertTrue(body["dry_run"])
        self.assertIsNone(body["head_sha"])

    def test_seat_rules_apply(self) -> None:
        w = self.w
        for tag in (None, "MONET", "NOPE"):
            res = w.lane("new", "DealDex", "--review", "--pr", "7", env={"AGENT_SEAT": tag})
            self.assertEqual((res.rc, res.out, res.calls), (64, "", []))

    def test_gh_failures(self) -> None:
        w = self.w
        res = w.lane("new", "DealDex", "--review", "--pr", "7", gh=lambda argv: D.CmdResult(1, "", "gh: not logged in\n"))
        self.assertEqual((res.rc, res.out), (69, ""), res.err)
        self.assertIn("gh pr view 7 failed", res.err)
        res = w.lane("new", "DealDex", "--review", "--pr", "999", gh=lambda argv: D.CmdResult(
            1, "", "GraphQL: Could not resolve to a PullRequest with the number of 999. (repository.pullRequest)\n"))
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("PR 999 was not found", res.err)
        for text in ("not json", "{}", '{"headRefOid": "nothex"}', "[]"):
            with self.subTest(text=text):
                res = w.lane("new", "DealDex", "--review", "--pr", "7", gh=lambda argv, t=text: D.CmdResult(0, t, ""))
                self.assertEqual((res.rc, res.out), (69, ""), res.err)
        self.assertFalse(w.review_path().exists())
        self.assertFalse((w.roots.lanes_root / "DealDex").exists(), "nothing is created before the PR is looked up")

    def test_a_pr_head_that_moved_before_the_fetch_is_exit_69(self) -> None:
        w = self.w
        res = w.lane("new", "DealDex", "--review", "--pr", "7", gh=lambda argv: gh_pr_json(sha="a" * 40))
        self.assertEqual((res.rc, res.out), (69, ""), res.err)
        self.assertIn("Run lane again", res.err)
        self.assertFalse(w.review_path().exists())
        self.assertFalse(res.has("worktree", "add"))

    def test_fetch_failure_is_exit_69(self) -> None:
        w = self.w
        res = w.lane("new", "DealDex", "--review", "--pr", "8")      # origin has no pull/8/head
        self.assertEqual((res.rc, res.out), (69, ""), res.err)
        self.assertIn("pull/8/head", res.err)
        self.assertFalse(w.review_path(8).exists())

    def test_a_closed_pr_is_checked_out_with_a_note(self) -> None:
        w = self.w
        res = w.lane("new", "DealDex", "--review", "--pr", "7", gh=lambda argv: gh_pr_json(state="MERGED"))
        self.assertEqual(res.rc, 0, res.err)
        self.assertIn("PR 7 is MERGED", res.err)

    def test_existing_foreign_path_or_attached_worktree_is_refused(self) -> None:
        w = self.w
        path = w.review_path()
        path.mkdir(parents=True)
        res = w.lane("new", "DealDex", "--review", "--pr", "7")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        path.rmdir()
        w.git(["worktree", "add", "-q", "-b", "claude/attached", str(path), "origin/main"], cwd=w.tree)
        before = w.everything_hash()
        res = w.lane("new", "DealDex", "--review", "--pr", "7")
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("claude/attached", res.err)
        self.assertNoChange(before)

    def test_flat_mode_still_puts_review_checkouts_in_the_repo_folder(self) -> None:
        # as before layout v2, a review does not depend on the layout mode (the guard's deny text offers it in both)
        w = self.w
        res = w.lane("new", "DealDex", "--review", "--pr", "7", env={"FLEET_LAYOUT": "flat"})
        self.assertEqual((res.rc, res.out), (0, f"{w.review_path()}\n"), res.err)
        flat_roots = L.make_roots(w.home, dict(w.env, FLEET_LAYOUT="flat"), registry=REGISTRY)
        self.assertEqual(L.classify_location(w.review_path(), flat_roots), L.LocationClass.REVIEW)

    def test_review_root_outside_the_map_is_refused(self) -> None:
        w = self.w
        res = w.lane("new", "DealDex", "--review", "--pr", "7", env={"FLEET_LANES_ROOT": "~/Code/lanes"})
        self.assertEqual((res.rc, res.out), (64, ""), res.err)
        self.assertIn("inside", res.err)


# --------------------------------------------------------------------------- two runs at once

class ConcurrencyTests(WorldCase):
    """Two `lane new` runs for the same lane at the same moment.  Without a per-repo lock, both see
    an empty path, both run `git worktree add`, and the loser's clean-up deletes the winner's folder
    while the winner prints its path.  Run B is started from inside run A's `worktree add` and given
    three seconds, which is when the unlocked B used to finish first."""

    def _race(self, argv: "tuple[str, ...]", path: Path) -> "tuple[Result, Result]":
        w = self.w
        box: List[Result] = []
        threads: List[threading.Thread] = []

        def start_b_inside_a(argv_: List[str]) -> Optional[D.CmdResult]:
            if argv_[4:6] == ["worktree", "add"] and not threads:
                t = threading.Thread(target=lambda: box.append(w.lane(*argv)), daemon=True)
                threads.append(t)
                t.start()
                t.join(3)
            return None

        a = w.lane(*argv, exec_override=start_b_inside_a)
        self.assertEqual(len(threads), 1, a.err)
        threads[0].join(120)
        self.assertFalse(threads[0].is_alive(), "run B never finished")
        b = box[0]
        for name, res in (("A", a), ("B", b)):
            self.assertEqual((res.rc, res.out), (0, f"{path}\n"), f"run {name}: {res.err}")
        self.assertTrue(path.is_dir(), "the lane both runs printed must exist")
        listed = w.git(["worktree", "list", "--porcelain"], cwd=w.tree)
        self.assertNotIn("prunable", listed)
        self.assertEqual(listed.count(f"worktree {path}\n"), 1)
        self.assertIn("already exists", b.err)
        # Whether B printed "waiting for another" depends on timing (B may only reach the lock after
        # A is done); test_a_held_lock_makes_lane_wait_then_give_up_with_69 proves the wait itself.
        return a, b

    def test_two_reuse_branch_runs_for_the_same_lane(self) -> None:
        w = self.w
        w.git(["branch", "--no-track", "claude/race", "origin/main"], cwd=w.tree)
        self._race(("new", "DealDex", "race", "--reuse-branch"), w.lane_path("race"))
        self.assertEqual(w.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=w.lane_path("race")), "claude/race")

    def test_two_review_runs_for_the_same_pr(self) -> None:
        w = self.w
        self._race(("new", "DealDex", "--review", "--pr", "7"), w.review_path())
        self.assertEqual(w.git(["rev-parse", "HEAD"], cwd=w.review_path()), SHAS["pr"])

    def test_two_fresh_branch_runs_for_the_same_lane(self) -> None:
        w = self.w
        self._race(("new", "DealDex", "fix-thing"), w.lane_path())
        self.assertEqual(w.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=w.lane_path()), "claude/fix-thing")

    def test_the_lock_lives_in_the_git_common_dir_and_is_never_removed(self) -> None:
        w = self.w
        lock = w.tree / ".git" / K.LOCK_NAME
        self.assertFalse(lock.exists())
        w.lane("new", "DealDex", "fix-thing", "--dry-run")
        self.assertFalse(lock.exists(), "a dry run writes nothing, not even the lock")
        self.assertEqual(w.lane("new", "DealDex", "fix-thing").rc, 0)
        self.assertTrue(lock.is_file())
        self.assertFalse(lock.name.endswith(".lock"), "git-style *.lock names get swept by stale-lock cleaners")
        self.assertEqual(w.lane("new", "DealDex", "--review", "--pr", "7").rc, 0)
        self.assertTrue(lock.is_file())

    def test_a_held_lock_makes_lane_wait_then_give_up_with_69(self) -> None:
        w = self.w
        self.assertEqual(w.lane("new", "DealDex", "first").rc, 0)
        fd = os.open(str(w.tree / ".git" / K.LOCK_NAME), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            with mock.patch.object(K, "LOCK_WAIT", 0.5):
                res = w.lane("new", "DealDex", "fix-thing")
        finally:
            os.close(fd)
        self.assertEqual((res.rc, res.out), (69, ""), res.err)
        self.assertIn("waiting for another", res.err)
        self.assertFalse(res.has("fetch"))
        self.assertFalse(res.has("worktree", "add"))
        self.assertFalse(w.lane_path().exists())
        self.assertEqual(w.lane("new", "DealDex", "fix-thing").rc, 0, "a later run goes through")

    def test_a_worktree_that_is_gone_right_after_add_is_exit_69_not_a_printed_path(self) -> None:
        w = self.w
        for argv, path in ((("new", "DealDex", "fix-thing"), w.lane_path()),
                           (("new", "DealDex", "--review", "--pr", "7"), w.review_path())):
            with self.subTest(argv=argv):
                def vanish(argv_: List[str]) -> Optional[D.CmdResult]:
                    if argv_[4:6] == ["worktree", "add"]:
                        real = K.default_exec(argv_, str(w.tree), K._child_env(w.env), 900)
                        shutil.rmtree(path, ignore_errors=True)       # what a losing run's clean-up did
                        return real
                    return None

                res = w.lane(*argv, exec_override=vanish)
                self.assertEqual((res.rc, res.out), (69, ""), res.err)
                self.assertIn("removes nothing", res.err)

    def test_a_worktree_on_the_wrong_commit_after_add_is_exit_69(self) -> None:
        w = self.w

        def wrong_head(argv_: List[str]) -> Optional[D.CmdResult]:
            if argv_[4:6] == ["worktree", "list"] and any("worktree add" in " ".join(c) for c in seen):
                real = K.default_exec(argv_, str(w.tree), K._child_env(w.env), 30)
                return D.CmdResult(real.rc, real.out.replace(SHAS["main"], SHAS["release"]), real.err)
            seen.append(list(argv_))
            return None

        seen: List[List[str]] = []
        res = w.lane("new", "DealDex", "fix-thing", exec_override=wrong_head)
        self.assertEqual((res.rc, res.out), (69, ""), res.err)
        self.assertIn(SHAS["release"][:9], res.err)


# --------------------------------------------------------------------------- command line

class CommandLineTests(WorldCase):
    def test_ls_and_doctor_delegate_with_the_same_argv(self) -> None:
        for name in ("ls", "doctor"):
            with self.subTest(name=name):
                out = io.StringIO()
                with mock.patch.object(D, "main", return_value=2) as doctor_main:
                    rc = K.main([name, "--json", "--no-gh", "--only-class", "LANE_NESTED"], env=self.w.env, stdout=out)
                self.assertEqual(rc, 2)
                args, kwargs = doctor_main.call_args
                self.assertEqual(args[0], ["--json", "--no-gh", "--only-class", "LANE_NESTED"])
                self.assertIs(kwargs["stdout"], out)
                self.assertEqual(kwargs["env"], self.w.env)

    def test_doctor_usage_error_keeps_the_doctors_exit_code(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = K.main(["ls", "--no-such-flag"], env=self.w.env, stdout=out)
        self.assertEqual(rc, 64)

    def test_usage_errors_exit_64(self) -> None:
        w = self.w
        for argv in ([], ["bogus"], ["new"], ["path"], ["path", "DealDex"], ["new", "DealDex", "x", "--no-such-flag"],
                     ["new", "DealDex", "x", "--pr", "abc"]):
            with self.subTest(argv=argv):
                with contextlib.redirect_stderr(io.StringIO()):
                    res = w.lane(*argv)
                self.assertEqual((res.rc, res.out), (64, ""))
                self.assertEqual(res.calls, [])

    def test_help_exits_zero_on_stdout(self) -> None:
        res = self.w.lane("--help")
        self.assertEqual(res.rc, 0)
        self.assertIn("usage: lane", res.out)
        res = self.w.lane("new", "--help")
        self.assertEqual(res.rc, 0)
        for flag in ("--base", "--purpose", "--board", "--reuse-branch", "--dry-run", "--json", "--review", "--pr"):
            self.assertIn(flag, res.out)

    def test_a_refused_command_inside_lane_is_an_internal_error(self) -> None:
        w = self.w
        with mock.patch.object(K, "_head_sha", side_effect=K.CommandRefused("boom")):
            res = w.lane("new", "DealDex", "fix-thing", "--dry-run")
        self.assertEqual((res.rc, res.out), (70, ""))
        self.assertIn("internal error", res.err)


# --------------------------------------------------------------------------- the shim

class ShimTests(WorldCase):
    def run_shim(self, *argv: str, shim: "Path | str" = SHIM, env: Optional[dict] = None,
                 cwd: "Path | str | None" = None) -> "subprocess.CompletedProcess":
        full = dict(self.w.env)
        for k, v in (env or {}).items():
            if v is None:
                full.pop(k, None)
            else:
                full[k] = v
        # A neutral cwd by default: the test process runs from scripts/, whose own fleet_lanes would
        # hide a shim that imports from the working directory.
        return subprocess.run([str(shim), *argv], capture_output=True, text=True, env=full, timeout=60,
                              cwd=str(cwd if cwd is not None else self.w.root))

    def test_a_fleet_lanes_package_in_the_working_directory_never_shadows_the_checkout(self) -> None:
        # Every AFC lane has scripts/fleet_lanes, so running the shim from another lane's scripts/
        # must still run THIS checkout's code.
        hijack = self.w.root / "hijack"
        (hijack / "fleet_lanes").mkdir(parents=True)
        (hijack / "fleet_lanes" / "__init__.py").write_text("", encoding="utf-8")
        (hijack / "fleet_lanes" / "lane.py").write_text('print("HIJACKED")\n', encoding="utf-8")
        older = self.w.root / "older"
        (older / "fleet_lanes").mkdir(parents=True)
        (older / "fleet_lanes" / "__init__.py").write_text("", encoding="utf-8")   # a checkout without lane.py
        pythons = [None] + (["/usr/bin/python3"] if os.access("/usr/bin/python3", os.X_OK) else [])
        for cwd in (hijack, older):
            for python in pythons:
                with self.subTest(cwd=cwd.name, python=python):
                    env = {"FLEET_PYTHON": python} if python else None
                    proc = self.run_shim("path", "DealDex", "fix-thing", cwd=cwd, env=env)
                    self.assertNotIn("HIJACKED", proc.stdout + proc.stderr)
                    self.assertEqual((proc.returncode, proc.stdout), (0, f"{self.w.lane_path()}\n"), proc.stderr)
                    self.assertEqual(self.run_shim("path", "DealDex", "x", cwd=cwd, env={**(env or {}),
                                                                                       "AGENT_SEAT": None}).returncode, 64)

    def test_python_variables_cannot_redirect_the_import_either(self) -> None:
        hijack = self.w.root / "hijack"
        (hijack / "fleet_lanes").mkdir(parents=True)
        (hijack / "fleet_lanes" / "__init__.py").write_text("", encoding="utf-8")
        (hijack / "fleet_lanes" / "lane.py").write_text('print("HIJACKED")\n', encoding="utf-8")
        proc = self.run_shim("path", "DealDex", "fix-thing", env={"PYTHONPATH": str(hijack)})
        self.assertNotIn("HIJACKED", proc.stdout)
        self.assertEqual((proc.returncode, proc.stdout), (0, f"{self.w.lane_path()}\n"), proc.stderr)

    def test_the_header_does_not_suggest_pointing_the_installed_path_at_the_shim(self) -> None:
        text = SHIM.read_text(encoding="utf-8")
        self.assertNotIn("such as ~/apps/lane works", text)
        self.assertIn("-I", text)

    def test_it_is_an_executable_posix_shell_script(self) -> None:
        self.assertTrue(os.access(SHIM, os.X_OK))
        first = SHIM.read_text(encoding="utf-8").splitlines()[0]
        self.assertEqual(first, "#!/bin/sh")
        self.assertEqual(subprocess.run(["sh", "-n", str(SHIM)]).returncode, 0)

    def test_path_command_prints_the_path_and_nothing_else(self) -> None:
        proc = self.run_shim("path", "DealDex", "fix-thing")
        self.assertEqual((proc.returncode, proc.stdout), (0, f"{self.w.lane_path()}\n"), proc.stderr)

    def test_it_follows_a_symlink_to_find_the_scripts_dir(self) -> None:
        link_dir = self.w.root / "bin"
        link_dir.mkdir()
        link = link_dir / "lane"
        os.symlink(SHIM, link)
        proc = self.run_shim("path", "DealDex", "fix-thing", shim=link)
        self.assertEqual((proc.returncode, proc.stdout), (0, f"{self.w.lane_path()}\n"), proc.stderr)
        chain = link_dir / "lane2"
        os.symlink("lane", chain)         # a relative link to a link
        proc = self.run_shim("path", "DealDex", "fix-thing", shim=chain)
        self.assertEqual((proc.returncode, proc.stdout), (0, f"{self.w.lane_path()}\n"), proc.stderr)

    def test_without_fleet_apps_json_it_uses_the_repo_registry(self) -> None:
        proc = self.run_shim("path", "DealDex", "smoke", env={"FLEET_APPS_JSON": None})
        self.assertEqual((proc.returncode, proc.stdout), (0, f"{self.w.lane_path('smoke')}\n"), proc.stderr)

    def test_exit_codes_pass_through(self) -> None:
        proc = self.run_shim("path", "DealDex", "x", env={"AGENT_SEAT": None})
        self.assertEqual((proc.returncode, proc.stdout), (64, ""))
        self.assertIn("AGENT_SEAT is not set", proc.stderr)
        self.assertEqual(self.run_shim().returncode, 64)

    def test_new_through_the_shim_creates_the_lane(self) -> None:
        proc = self.run_shim("new", "DealDex", "fix-thing")
        self.assertEqual((proc.returncode, proc.stdout), (0, f"{self.w.lane_path()}\n"), proc.stderr)
        self.assertEqual(self.w.git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=self.w.lane_path()), "claude/fix-thing")

    def test_a_bad_scripts_dir_is_reported_not_crashed(self) -> None:
        proc = self.run_shim("path", "DealDex", "x", env={"FLEET_SCRIPTS_DIR": str(self.w.root / "nowhere")})
        self.assertEqual(proc.returncode, 69)
        self.assertIn("FLEET_SCRIPTS_DIR", proc.stderr)


if __name__ == "__main__":
    unittest.main()
