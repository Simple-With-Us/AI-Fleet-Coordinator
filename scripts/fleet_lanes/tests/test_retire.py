"""lane retire / lane prune tests: the allowlist, the pure decision, and real removals in a throwaway world.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_retire -v

The world is the one test_lane uses, built here on its own: a local bare origin (file URLs, no network), real
clones of it as ~/Code/DealDex and ~/Code/Socratic-Trade in a fake home, a fake fleet-apps.json passed through
FLEET_APPS_JSON, and lanes made by `lane new` itself.  gh and lsof are faked through `exec_fn`; every git call
is real.  The environment given to `lane.main` is an explicit dict, so neither the real home nor this process's
AGENT_SEAT leaks in.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path

from fleet_lanes import doctor as D
from fleet_lanes import lane as K
from fleet_lanes import retire as R

REGISTRY_DATA = {
    "owner": "Simple-With-Us",
    "apps": [
        {"repo": "DealDex", "acronym": "DD", "worktreePrefix": "dealdex", "codeDir": "DealDex"},
        {
            "repo": "Socratic-Trade",
            "acronym": "ST",
            "worktreePrefix": "trading",
            "codeDir": "Socratic-Trade",
        },
    ],
    "seats": [
        {"tag": "CLAUDE", "worktreeSuffix": "claude", "branchPrefixes": ["claude/"]},
        {"tag": "CODEX", "worktreeSuffix": "codex", "branchPrefixes": ["codex/"]},
        {"tag": "MONET", "worktreeSuffix": "monet", "branchPrefixes": ["monet/"], "retired": True},
    ],
}
_HERMETIC = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "LC_ALL": "C",
    "GIT_AUTHOR_NAME": "Lane Test",
    "GIT_AUTHOR_EMAIL": "lane@example.invalid",
    "GIT_COMMITTER_NAME": "Lane Test",
    "GIT_COMMITTER_EMAIL": "lane@example.invalid",
}
_TEMPLATE: tempfile.TemporaryDirectory | None = None
TEMPLATE_WORLD = ""
TEMPLATE_URIS: dict[str, str] = {}
SHAS: dict[str, str] = {}
REAL_NOW = dt.datetime.now(dt.timezone.utc)  # noqa: UP017 (3.9 has no datetime.UTC)
LATER = REAL_NOW + dt.timedelta(
    hours=1
)  # past the 15-minute guard, inside the 24-hour empty-lane hold
MUCH_LATER = REAL_NOW + dt.timedelta(days=2)


def _git(
    args: list[str], cwd: str | None = None, env: dict | None = None, check: bool = True
) -> str:
    full_env = {"PATH": os.environ.get("PATH", ""), **_HERMETIC, **(env or {})}
    full_env.setdefault("HOME", cwd or tempfile.gettempdir())
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=full_env, capture_output=True, text=True, check=False
    )
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def setUpModule() -> None:
    global _TEMPLATE, TEMPLATE_WORLD
    _TEMPLATE = tempfile.TemporaryDirectory(prefix="retire-test-template-")
    base = os.path.realpath(_TEMPLATE.name)
    env = {"HOME": base}
    world = os.path.join(base, "world")
    os.makedirs(os.path.join(world, "home", "apps", "lanes"))
    os.makedirs(os.path.join(world, "home", "Code"))
    os.makedirs(os.path.join(world, "origin"))
    for repo in ("DealDex", "Socratic-Trade"):
        bare, seed = (
            os.path.join(world, "origin", f"{repo}.git"),
            os.path.join(base, f"seed-{repo}"),
        )
        _git(["init", "--bare", bare], env=env)
        _git(["--git-dir", bare, "symbolic-ref", "HEAD", "refs/heads/main"], env=env)
        _git(["init", seed], env=env)
        _git(["symbolic-ref", "HEAD", "refs/heads/main"], cwd=seed, env=env)
        for name in ("one.txt", "two.txt"):
            Path(seed, name).write_text(name + "\n", encoding="utf-8")
            _git(["add", name], cwd=seed, env=env)
            _git(["commit", "-q", "-m", name], cwd=seed, env=env)
        SHAS[repo] = _git(["rev-parse", "HEAD"], cwd=seed, env=env)
        _git(["push", "-q", bare, "main"], cwd=seed, env=env)
        if repo == "DealDex":
            _git(["checkout", "-q", "-b", "prbranch"], cwd=seed, env=env)
            Path(seed, "pr.txt").write_text("pr\n", encoding="utf-8")
            _git(["add", "pr.txt"], cwd=seed, env=env)
            _git(["commit", "-q", "-m", "pr"], cwd=seed, env=env)
            SHAS["pr"] = _git(["rev-parse", "HEAD"], cwd=seed, env=env)
            _git(["push", "-q", bare, "prbranch"], cwd=seed, env=env)
            _git(["--git-dir", bare, "update-ref", "refs/pull/7/head", SHAS["pr"]], env=env)
            _git(["--git-dir", bare, "update-ref", "-d", "refs/heads/prbranch"], env=env)
        uri = Path(bare).as_uri()
        TEMPLATE_URIS[repo] = uri
        _git(["clone", "-q", uri, os.path.join(world, "home", "Code", repo)], env=env)
    TEMPLATE_WORLD = world


def tearDownModule() -> None:
    if _TEMPLATE is not None:
        _TEMPLATE.cleanup()


def tree_hash(root: str | Path) -> str:
    import hashlib

    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        h.update(("D:" + os.path.relpath(dirpath, root) + "\n").encode())
        for name in sorted(filenames):
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            if os.path.islink(p):
                h.update(f"L:{rel}>{os.readlink(p)}\n".encode())
            else:
                with open(p, "rb") as fh:
                    h.update(f"F:{rel}:".encode() + hashlib.sha256(fh.read()).digest())
    return h.hexdigest()


class Result:
    def __init__(self, rc: int, out: str, err: str, calls: list[list[str]]) -> None:
        self.rc, self.out, self.err, self.calls = rc, out, err, calls

    @property
    def verbs(self) -> list[tuple]:
        return [tuple(c[4:]) if c[0] == "git" else tuple(c) for c in self.calls]

    def has(self, *prefix: str) -> bool:
        return any(v[: len(prefix)] == prefix for v in self.verbs)

    def json(self) -> dict:
        return json.loads(self.out)

    def row(self, name: str) -> dict:
        for r in self.json()["lanes"]:
            if os.path.basename(r["path"]) == name:
                return r
        raise AssertionError(f"no row for {name}: {self.out}")


class World:
    def __init__(self, case: unittest.TestCase) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="retire-test-")
        case.addCleanup(tmp.cleanup)
        self.root = Path(os.path.realpath(tmp.name))
        shutil.copytree(TEMPLATE_WORLD, self.root, symlinks=True, dirs_exist_ok=True)
        self.home = self.root / "home"
        self.lanes = self.home / "apps" / "lanes"
        self.trees = {r: self.home / "Code" / r for r in ("DealDex", "Socratic-Trade")}
        self.origins = {r: self.root / "origin" / f"{r}.git" for r in self.trees}
        for repo, tree in self.trees.items():
            cfg = tree / ".git" / "config"
            cfg.write_text(
                cfg.read_text(encoding="utf-8").replace(
                    TEMPLATE_URIS[repo], self.origins[repo].as_uri()
                ),
                encoding="utf-8",
            )
        reg = self.root / "fleet-apps.json"
        reg.write_text(json.dumps(REGISTRY_DATA), encoding="utf-8")
        self.env: dict[str, str] = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            **_HERMETIC,
            "FLEET_APPS_JSON": str(reg),
            "AGENT_SEAT": "CLAUDE",
        }
        self.prs: dict[str, list[dict]] = {
            "Simple-With-Us/DealDex": [],
            "Simple-With-Us/Socratic-Trade": [],
        }
        self.lsof: Callable[[], D.CmdResult] = lambda: D.CmdResult(0, "", "")

    def git(self, args: list[str], cwd: Path | str, check: bool = True) -> str:
        return _git(args, cwd=str(cwd), env=self.env, check=check)

    def gh(self, argv: list[str]) -> D.CmdResult:
        repo = argv[argv.index("--repo") + 1]
        pool = list(self.prs.get(repo, []))
        if "--head" in argv:
            name = argv[argv.index("--head") + 1]
            pool = [p for p in pool if p.get("headRefName") == name]
        return D.CmdResult(0, json.dumps(pool), "")

    def run(
        self,
        *argv: str,
        env: dict | None = None,
        clock: dt.datetime = LATER,
        override: Callable[[list[str]], D.CmdResult | None] | None = None,
    ) -> Result:
        full_env = dict(self.env)
        for k, v in (env or {}).items():
            if v is None:
                full_env.pop(k, None)
            else:
                full_env[k] = v
        calls: list[list[str]] = []

        def exec_fn(a: list[str], cwd, child_env, timeout) -> D.CmdResult:
            calls.append(list(a))
            if override is not None:
                forced = override(list(a))
                if forced is not None:
                    return forced
            if a[0] == "gh":
                return self.gh(list(a))
            if a[0] == "lsof":
                return self.lsof()
            return K.default_exec(a, cwd, child_env, timeout)

        out, err = io.StringIO(), io.StringIO()
        rc = K.main(
            list(argv), env=full_env, clock=lambda: clock, exec_fn=exec_fn, stdout=out, stderr=err
        )
        return Result(rc, out.getvalue(), err.getvalue(), calls)

    # building lanes
    def new_lane(self, slug: str, repo: str = "DealDex", seat: str = "CLAUDE") -> Path:
        res = self.run("new", repo, slug, env={"AGENT_SEAT": seat}, clock=REAL_NOW)
        assert res.rc == 0, res.err
        return Path(res.out.strip())

    def commit(self, lane: Path, name: str = "") -> str:
        # A file named after the lane, so two lanes committed in the same second never make the same commit object
        # (identical tree, parent, author and time give an identical sha, and then one lane's push covers the other).
        name = name or f"{lane.name}.txt"
        (lane / name).write_text(name + "\n", encoding="utf-8")
        self.git(["add", name], lane)
        self.git(["commit", "-q", "-m", name], lane)
        return self.git(["rev-parse", "HEAD"], lane)

    def push(self, lane: Path, head: str) -> None:
        self.git(["push", "-q", "-u", "origin", f"HEAD:{head}"], lane)

    def merged_pr(
        self, lane: Path, head: str, number: int = 11, repo: str = "DealDex", state: str = "MERGED"
    ) -> None:
        sha = self.git(["rev-parse", "HEAD"], lane)
        self.old_pr(head, sha, number=number, repo=repo, state=state)

    def old_pr(
        self, head: str, sha: str, number: int, repo: str = "DealDex", state: str = "MERGED"
    ) -> None:
        """A PR for the branch name `head` whose head commit is `sha` (any sha, local or not)."""
        self.prs[f"Simple-With-Us/{repo}"].append(
            {
                "number": number,
                "state": state,
                "headRefName": head,
                "headRefOid": sha,
                "mergedAt": None,
                "closedAt": None,
            }
        )

    def worktree_paths(self, repo: str = "DealDex") -> list[str]:
        out = self.git(["worktree", "list", "--porcelain"], self.trees[repo])
        return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith("worktree ")]


class WorldCase(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)


# --------------------------------------------------------------------------- the allowlist


class AllowlistTests(unittest.TestCase):
    ROOT = "/fleet-retire-test/apps/lanes"  # a path that does not exist, so no real symlink changes the answer
    SHA = "b" * 40

    def ok(self, argv: list[str]) -> None:
        R.check_allowed(argv, write_root=self.ROOT)

    def test_every_allowed_shape(self) -> None:
        lane = self.ROOT + "/DealDex/claude-x"
        for argv in [
            ["git", "worktree", "list", "--porcelain"],
            ["git", "worktree", "remove", lane],
            ["git", "worktree", "prune"],
            ["git", "branch", "-d", "claude/x"],
            ["git", "status", "--porcelain", "--ignored", "--untracked-files=normal"],
            ["git", "rev-list", "--count", "--ignore-missing", "HEAD", "--not", self.SHA],
            ["git", "rev-list", "--count", "--ignore-missing", "HEAD", "--not", self.SHA, "c" * 40],
            ["git", "rev-list", "--count", "--ignore-missing", "HEAD", "--not"],
            ["git", "rev-list", "--count", "HEAD", "--not", self.SHA],
            ["git", "rev-parse", "--absolute-git-dir"],
            ["git", "rev-parse", "--git-common-dir"],
            ["git", "rev-parse", "--verify", "-q", self.SHA + "^{commit}"],
            ["git", "config", "--get", "remote.origin.url"],
            ["git", "config", "--get", "branch.claude/x.merge"],
            ["git", "config", "--get", "branch.claude/x.remote"],
            ["git", "merge-base", "--is-ancestor", "HEAD", "refs/remotes/origin/main"],
            ["git", "symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD"],
            ["git", "ls-remote", "--heads", "origin"],
            [
                "gh",
                "pr",
                "list",
                "--repo",
                "Simple-With-Us/DealDex",
                "--state",
                "all",
                "--limit",
                "1000",
                "--json",
                R.GH_FIELDS,
            ],
            [
                "gh",
                "pr",
                "list",
                "--repo",
                "Simple-With-Us/DealDex",
                "--state",
                "all",
                "--head",
                "claude/x",
                "--limit",
                "20",
                "--json",
                R.GH_FIELDS,
            ],
            ["lsof", "-d", "cwd", "-Fpcn"],
        ]:
            with self.subTest(argv=argv):
                self.ok(argv)

    def test_refused_commands(self) -> None:
        lane = self.ROOT + "/DealDex/claude-x"
        for argv in [
            [],
            ["rm", "-rf", lane],
            ["git", "worktree", "remove", "--force", lane],
            ["git", "worktree", "remove", "-f", lane],
            ["git", "worktree", "remove", lane, "--force"],
            ["git", "worktree", "remove", "/fleet-retire-test/Code/DealDex"],
            ["git", "worktree", "remove", "DealDex/claude-x"],
            ["git", "worktree", "remove", self.ROOT],
            ["git", "worktree", "remove", "/tmp/x"],
            ["git", "worktree", "prune", "--expire", "now"],
            ["git", "branch", "-D", "claude/x"],
            ["git", "branch", "-d", "--force", "claude/x"],
            ["git", "branch", "-d", "-f"],
            ["git", "push", "origin", "--delete", "x"],
            ["git", "push", "--force"],
            ["git", "reset", "--hard"],
            ["git", "clean", "-fdx"],
            ["git", "status"],
            ["git", "status", "--porcelain", "--ignored"],
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            ["git", "rev-list", "--count", "HEAD", "--not", "--all"],
            [
                "git",
                "rev-list",
                "--count",
                "HEAD",
                "--not",
                "--exclude=refs/remotes/origin/x",
                "--remotes",
            ],
            ["git", "rev-list", "--count", "HEAD", "--not", "--exclude=*", "--remotes"],
            ["git", "rev-list", "--count", "HEAD", "--not", "--remotes"],
            ["git", "rev-list", "--count", "HEAD", "--not", "--exclude=origin/x", "--remotes"],
            ["git", "rev-list", "--count", "--ignore-missing", "HEAD", "--not", "--remotes"],
            ["git", "rev-list", "--count", "--ignore-missing", "HEAD", "--not", "main"],
            ["git", "rev-list", "--count", "--ignore-missing", "HEAD", "--not", self.SHA, "--all"],
            ["git", "config", "--get", "core.sshCommand"],
            ["git", "config", "branch.x.merge", "y"],
            ["git", "merge-base", "--is-ancestor", "HEAD", "main"],
            ["git", "ls-remote", "origin"],
            ["git", "fetch", "origin"],
            ["gh", "pr", "merge", "1"],
            ["gh", "pr", "list", "--repo", "a/b"],
            ["gh", "api", "graphql"],
            ["lsof"],
            ["lsof", "-p", "1"],
            ["bash", "-c", "true"],
        ]:
            with self.subTest(argv=argv), self.assertRaises(K.CommandRefused):
                self.ok(argv)

    def test_lane_new_allowlist_still_refuses_every_remove(self) -> None:
        for argv in (
            ["git", "worktree", "remove", self.ROOT + "/DealDex/claude-x"],
            ["git", "worktree", "prune"],
            ["git", "branch", "-d", "claude/x"],
        ):
            with self.assertRaises(K.CommandRefused):
                K.check_allowed(argv, write_root=self.ROOT)


# --------------------------------------------------------------------------- the decision


def facts(**kw) -> R.Facts:
    base = dict(
        kind="lane",
        seat="claude",
        processes=[],
        dirty=0,
        unpushed=0,
        merged_default=False,
        upstream="x",
        upstream_gone=False,
        idle_minutes=120.0,
        pr=R.PrView(done_number=5, done_state="MERGED", covers_head=True),
    )
    base.update(kw)
    return R.Facts(**base)


class DecideTests(unittest.TestCase):
    def test_merged_and_clean_is_removable(self) -> None:
        self.assertEqual(R.decide(facts(), prune=True)[0], "remove")

    def test_each_guard_keeps_it(self) -> None:
        cases = {
            "prunable": facts(prunable=True),
            "locked": facts(locked=True),
            "owner": facts(owner_conflict="codex"),
            "lsof failed": facts(processes=None),
            "in use": facts(processes=[(42, "zsh")]),
            "git op": facts(git_ops=["MERGE_HEAD"]),
            "keep marker": facts(keep_marker=True),
            "status failed": facts(dirty=None),
            "dirty": facts(dirty=2),
            "gh failed": facts(pr=R.PrView(found=False)),
            "open pr": facts(pr=R.PrView(open_number=9)),
            "unpushed": facts(unpushed=3, pr=R.PrView()),
            "not done": facts(pr=R.PrView()),
            "stale pr": facts(pr=R.PrView(stale_number=4)),
            "ignored local": facts(ignored_local=["local.db"]),
        }
        for name, f in cases.items():
            with self.subTest(name):
                self.assertEqual(R.decide(f, prune=True)[0], "keep", name)
                self.assertEqual(R.decide(f, prune=False)[0], "keep", name)

    def test_a_merged_pr_holding_head_overrides_unpushed(self) -> None:
        action, reason = R.decide(facts(unpushed=4), prune=True)
        self.assertEqual(action, "remove")
        self.assertIn("HEAD is the PR head", reason)

    def test_upstream_gone_or_ancestry_is_done(self) -> None:
        self.assertEqual(
            R.decide(facts(pr=R.PrView(), upstream_gone=True), prune=True)[0], "remove"
        )
        self.assertEqual(
            R.decide(facts(pr=R.PrView(), merged_default=True), prune=True)[0], "remove"
        )

    def test_prune_only_guards(self) -> None:
        recent = facts(idle_minutes=3.0)
        self.assertEqual(R.decide(recent, prune=True), ("keep", "active in the last 15 minutes"))
        self.assertEqual(R.decide(recent, prune=False)[0], "remove")
        empty = facts(pr=R.PrView(), merged_default=True, upstream=None, idle_minutes=60.0)
        self.assertEqual(R.decide(empty, prune=True)[0], "keep")
        self.assertEqual(R.decide(empty, prune=False)[0], "remove")
        self.assertEqual(
            R.decide(
                facts(pr=R.PrView(), merged_default=True, upstream=None, idle_minutes=3000.0),
                prune=True,
            )[0],
            "remove",
        )
        self.assertEqual(R.decide(facts(idle_minutes=None), prune=True)[0], "keep")

    def test_review_checkouts(self) -> None:
        open_review = facts(kind="review", pr=R.PrView(review_state="OPEN", covers_head=True))
        self.assertEqual(
            R.decide(open_review, prune=True), ("keep", "review checkout of an open PR")
        )
        self.assertEqual(R.decide(open_review, prune=False)[0], "remove")
        merged = facts(kind="review", pr=R.PrView(review_state="MERGED", covers_head=True))
        self.assertEqual(R.decide(merged, prune=True)[0], "remove")

    def test_managed_and_other_are_skipped(self) -> None:
        self.assertEqual(R.decide(facts(kind="managed"), prune=True)[0], "skip")
        self.assertEqual(R.decide(facts(kind="other"), prune=True)[0], "skip")


# --------------------------------------------------------------------------- prune in a real world


class PruneTests(WorldCase):
    def test_a_merged_sweep_lane_is_removable_and_the_dry_run_changes_nothing(self) -> None:
        lane = self.w.new_lane("pr11-sweep")
        self.w.commit(lane)
        self.w.push(lane, "feature/eleven")  # a sweep lane pushes HEAD:<pr head>
        self.w.merged_pr(lane, "feature/eleven")
        before = tree_hash(self.w.root)
        res = self.w.run("prune", "--json")
        self.assertEqual(res.rc, 0, res.err)
        row = res.row("claude-pr11-sweep")
        self.assertEqual(row["action"], "remove", row)
        self.assertIn("PR #11 merged", row["reason"])
        self.assertEqual(tree_hash(self.w.root), before, "the dry run changed something")
        self.assertFalse(res.has("worktree", "remove"))

    def test_apply_removes_with_git_worktree_remove_and_never_forces(self) -> None:
        lane = self.w.new_lane("pr12-sweep")
        self.w.commit(lane)
        self.w.push(lane, "feature/twelve")
        self.w.merged_pr(lane, "feature/twelve", number=12)
        res = self.w.run("prune", "--apply", "--json")
        self.assertEqual(res.rc, 0, res.err)
        self.assertTrue(res.row("claude-pr12-sweep")["removed"])
        self.assertFalse(lane.exists())
        self.assertNotIn(str(lane), self.w.worktree_paths())
        removes = [v for v in res.verbs if v[:2] == ("worktree", "remove")]
        self.assertEqual(removes, [("worktree", "remove", str(lane))])
        self.assertTrue(res.has("branch", "-d", "claude/pr12-sweep"))
        self.assertFalse(any("-D" in v or "--force" in v for v in res.verbs))
        self.assertTrue(
            (self.w.trees["DealDex"] / "one.txt").exists(), "the integration tree was touched"
        )

    def test_dirty_untracked_and_unpushed_lanes_are_kept(self) -> None:
        dirty = self.w.new_lane("dirty")
        self.w.commit(dirty)
        self.w.push(dirty, "claude/dirty")
        self.w.merged_pr(dirty, "claude/dirty", number=21)
        (dirty / "one.txt").write_text("changed\n", encoding="utf-8")
        untracked = self.w.new_lane("untracked")
        self.w.commit(untracked)
        self.w.push(untracked, "claude/untracked")
        self.w.merged_pr(untracked, "claude/untracked", number=22)
        (untracked / "new.txt").write_text("new\n", encoding="utf-8")
        unpushed = self.w.new_lane("unpushed")
        self.w.commit(unpushed)
        res = self.w.run("prune", "--apply", "--json")
        self.assertIn("uncommitted or untracked", res.row("claude-dirty")["reason"])
        self.assertIn("uncommitted or untracked", res.row("claude-untracked")["reason"])
        self.assertIn("on no remote ref", res.row("claude-unpushed")["reason"])
        for p in (dirty, untracked, unpushed):
            self.assertTrue(p.exists())
        self.assertFalse(res.has("worktree", "remove"))

    def test_ignored_build_output_does_not_keep_a_lane_but_ignored_local_state_does(self) -> None:
        exclude = self.w.trees["DealDex"] / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text("node_modules/\n.env\n", encoding="utf-8")
        built = self.w.new_lane("built")
        self.w.commit(built)
        self.w.push(built, "claude/built")
        self.w.merged_pr(built, "claude/built", number=23)
        (built / "node_modules" / "pkg").mkdir(parents=True)
        (built / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")
        local = self.w.new_lane("local-state")
        self.w.commit(local)
        self.w.push(local, "claude/local-state")
        self.w.merged_pr(local, "claude/local-state", number=24)
        (local / ".env").write_text("NAME=value\n", encoding="utf-8")
        res = self.w.run("prune", "--apply", "--json")
        self.assertTrue(res.row("claude-built")["removed"], res.out)
        self.assertEqual(res.row("claude-local-state")["reason"], "ignored local state: .env")
        self.assertTrue(local.exists())

    def test_ignored_local_state_is_named_and_refuses_a_retire(self) -> None:
        exclude = self.w.trees["DealDex"] / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(".env\n*.sqlite\n__pycache__/\n", encoding="utf-8")
        lane = self.w.new_lane("envfile")
        self.w.commit(lane)
        self.w.push(lane, "claude/envfile")
        self.w.merged_pr(lane, "claude/envfile", number=27)
        (lane / ".env").write_text("LOCAL=1\n", encoding="utf-8")
        (lane / "data.sqlite").write_bytes(b"\0")
        (lane / "__pycache__").mkdir()
        (lane / "__pycache__" / "m.cpython-313.pyc").write_bytes(b"\0")
        row = self.w.run("prune", "--apply", "--json").row("claude-envfile")
        self.assertEqual(row["action"], "keep", row)
        self.assertIn(".env", row["reason"])
        self.assertIn("data.sqlite", row["reason"])
        self.assertNotIn("__pycache__", row["reason"])
        retire = self.w.run("retire", "envfile")
        self.assertEqual(retire.rc, 64, retire.err)
        self.assertIn(".env", retire.err)
        self.assertTrue((lane / ".env").exists())

    def test_apply_rechecks_ignored_state_under_the_lock(self) -> None:
        lane = self.w.new_lane("late-env")
        self.w.commit(lane)
        self.w.push(lane, "claude/late-env")
        self.w.merged_pr(lane, "claude/late-env", number=25)
        seen: list[int] = []

        def late_env(a: list[str]) -> D.CmdResult | None:
            # the inspection's git status is clean; a .env appears before the re-check under the lock
            # (git prints `!!` lines only when it is asked to, with --ignored)
            if a[4:6] == ["status", "--porcelain"]:
                seen.append(1)
                if len(seen) == 2 and "--ignored" in a:
                    return D.CmdResult(0, "!! .env\n", "")
            return None

        res = self.w.run("prune", "--apply", "--json", override=late_env)
        row = res.row("claude-late-env")
        self.assertEqual(len(seen), 2, res.verbs)
        self.assertEqual(row["action"], "keep", row)
        self.assertIn("changed since it was inspected", row["reason"])
        self.assertFalse(res.has("worktree", "remove"))
        self.assertTrue(lane.exists())

    def test_squash_merged_with_the_remote_branch_deleted_is_covered_by_the_pr_head(self) -> None:
        lane = self.w.new_lane("squashed")
        self.w.commit(lane)
        self.w.push(lane, "claude/squashed")
        self.w.merged_pr(lane, "claude/squashed", number=31)
        self.w.git(
            ["push", "-q", "origin", "--delete", "claude/squashed"], lane
        )  # also drops the tracking ref
        self.assertEqual(
            self.w.git(["rev-list", "--count", "HEAD", "--not", "--remotes"], lane), "1"
        )
        res = self.w.run("prune", "--json")
        row = res.row("claude-squashed")
        self.assertEqual(row["action"], "remove", row)
        self.assertIn("HEAD is the PR head", row["reason"])

    def test_an_open_pr_keeps_the_lane(self) -> None:
        lane = self.w.new_lane("open")
        self.w.commit(lane)
        self.w.push(lane, "claude/open")
        self.w.merged_pr(lane, "claude/open", number=41, state="OPEN")
        self.assertEqual(
            self.w.run("prune", "--json").row("claude-open")["reason"], "PR #41 is open"
        )

    def test_upstream_deleted_on_origin_is_done_when_head_is_on_a_live_ref(self) -> None:
        lane = self.w.new_lane("gone")
        self.w.commit(lane)
        self.w.git(
            ["push", "-q", "origin", "HEAD:claude/gone-copy"], lane
        )  # the same commit on a live branch
        self.w.push(lane, "claude/gone")
        self.w.git(
            [
                "--git-dir",
                str(self.w.origins["DealDex"]),
                "update-ref",
                "-d",
                "refs/heads/claude/gone",
            ],
            self.w.root,
        )
        row = self.w.run("prune", "--json").row("claude-gone")
        self.assertEqual(row["action"], "remove", row)
        self.assertIn("upstream claude/gone deleted on origin", row["reason"])

    def test_work_only_on_the_deleted_upstreams_stale_local_ref_is_kept(self) -> None:
        lane = self.w.new_lane("stale-ref")
        self.w.commit(lane)
        self.w.push(lane, "claude/stale-ref")
        self.w.git(
            [
                "--git-dir",
                str(self.w.origins["DealDex"]),
                "update-ref",
                "-d",
                "refs/heads/claude/stale-ref",
            ],
            self.w.root,
        )  # gone on origin; refs/remotes/origin/... still local
        row = self.w.run("prune", "--apply", "--json").row("claude-stale-ref")
        self.assertEqual(row["action"], "keep", row)
        self.assertIn("stale local copy", row["reason"])
        self.assertTrue(lane.exists())

    def test_a_reused_slug_does_not_inherit_an_old_merged_pr(self) -> None:
        self.w.new_lane("reused")
        self.w.prs["Simple-With-Us/DealDex"].append(
            {
                "number": 55,
                "state": "MERGED",
                "headRefName": "claude/reused",
                "headRefOid": "c" * 40,
            }
        )
        row = self.w.run("prune", "--json").row("claude-reused")
        self.assertEqual(row["action"], "keep", row)
        self.assertIn("never pushed", row["reason"])

    def test_pushed_work_under_a_reused_slug_is_not_done_by_the_old_pr(self) -> None:
        work = self.w.new_lane("reused-work")
        self.w.commit(work)
        self.w.push(work, "claude/reused-work")
        # the old PR's head is a real local commit (today's origin/main) that does not contain HEAD
        self.w.old_pr("claude/reused-work", self.w.git(["rev-parse", "HEAD~1"], work), number=6)
        row = self.w.run("prune", "--apply", "--json").row("claude-reused-work")
        self.assertEqual(row["action"], "keep", row)
        self.assertIn("not done", row["reason"])
        self.assertTrue(work.exists())

    def test_an_old_pr_whose_head_only_contains_main_is_not_done_evidence(self) -> None:
        # The old PR's head is a local commit on top of today's origin/main, so it "contains" a fresh lane's
        # HEAD.  But that lane has no commits of its own: the PR holds none of its work.
        first = self.w.new_lane("first")
        old_head = self.w.commit(first)
        self.w.push(first, "claude/first")
        again = self.w.new_lane("again")
        self.w.old_pr("claude/again", old_head, number=8, state="CLOSED")
        row = self.w.run("prune", "--json").row("claude-again")
        self.assertEqual(row["action"], "keep", row)
        self.assertIn("never pushed", row["reason"])
        self.assertTrue(again.exists())

    def delete_on_origin(self, branch: str) -> None:
        """Delete a branch on the bare origin only: the local remote-tracking ref stays, stale."""
        self.w.git(
            [
                "--git-dir",
                str(self.w.origins["DealDex"]),
                "update-ref",
                "-d",
                f"refs/heads/{branch}",
            ],
            self.w.root,
        )

    def test_no_stale_or_non_origin_tracking_ref_vouches_for_a_push(self) -> None:
        # Leaving out only the deleted upstream's own stale ref is not enough: the same commit can sit on a
        # second stale ref (pushed under another name, deleted too) or on another remote's ref.  Only a
        # branch that exists on origin now (git ls-remote) vouches.
        lane = self.w.new_lane("two-names")
        self.w.commit(lane)
        self.w.git(["push", "-q", "origin", "HEAD:claude/two-names-copy"], lane)
        self.w.push(lane, "claude/two-names")
        self.delete_on_origin("claude/two-names-copy")
        self.delete_on_origin("claude/two-names")
        tree = self.w.trees["DealDex"]
        for ref in (
            "refs/remotes/origin/claude/two-names-copy",
            "refs/remotes/origin/claude/two-names",
        ):
            self.assertTrue(self.w.git(["rev-parse", "--verify", "-q", ref], tree), ref)
        res = self.w.run("prune", "--apply", "--json")
        row = res.row("claude-two-names")
        self.assertEqual(row["action"], "keep", row)
        self.assertIn("1 commit on no remote ref", row["reason"])
        self.assertTrue(lane.exists())
        self.assertFalse(res.has("worktree", "remove"))
        self.assertFalse(res.has("branch", "-d"))
        self.assertTrue(
            self.w.git(["rev-parse", "--verify", "-q", "refs/heads/claude/two-names"], tree)
        )
        # a peer remote that holds the commit (another lane, a scratch bare repo) never vouches either
        peer = self.w.root / "peer.git"
        self.w.git(["init", "-q", "--bare", str(peer)], self.w.root)
        self.w.git(["remote", "add", "peer", peer.as_uri()], tree)
        self.w.git(["push", "-q", "peer", "HEAD:refs/heads/copy"], lane)
        self.assertTrue(self.w.git(["rev-parse", "--verify", "-q", "refs/remotes/peer/copy"], tree))
        row = self.w.run("prune", "--apply", "--json").row("claude-two-names")
        self.assertEqual(row["action"], "keep", row)
        self.assertTrue(lane.exists())

    def test_a_failed_ls_remote_never_vouches_for_a_push(self) -> None:
        # Without the live origin heads no branch proves HEAD is pushed (its local tracking ref may be
        # stale), so only origin/main or a PR holding HEAD can.
        lane = self.w.new_lane("blind")
        self.w.commit(lane)
        self.w.push(lane, "claude/blind")
        landed = self.w.new_lane("blind-main")
        self.w.commit(landed)
        self.w.push(landed, "claude/blind-main")
        self.w.git(["push", "-q", "origin", "HEAD:main"], landed)
        covered = self.w.new_lane("blind-pr")
        self.w.commit(covered)
        self.w.push(covered, "claude/blind-pr")
        self.w.merged_pr(covered, "claude/blind-pr", number=26)
        fail = lambda a: (
            D.CmdResult(128, "", "fatal: unable to access origin")
            if a[4:6] == ["ls-remote", "--heads"]
            else None
        )
        res = self.w.run("prune", "--json", override=fail)
        self.assertEqual(res.rc, 69)
        self.assertEqual(res.row("claude-blind")["reason"], "could not count unpushed commits")
        self.assertEqual(res.row("claude-blind-main")["action"], "remove")
        self.assertEqual(res.row("claude-blind-pr")["action"], "remove")
        self.assertTrue(any("ls-remote" in e for e in res.json()["errors"]))

    def test_closed_pr_counts_as_done(self) -> None:
        lane = self.w.new_lane("closed")
        self.w.commit(lane)
        self.w.push(lane, "claude/closed")
        self.w.merged_pr(lane, "claude/closed", number=51, state="CLOSED")
        self.assertEqual(self.w.run("prune", "--json").row("claude-closed")["action"], "remove")

    def test_recent_activity_keeps_a_merged_lane_in_prune_only(self) -> None:
        lane = self.w.new_lane("fresh")
        self.w.commit(lane)
        self.w.push(lane, "claude/fresh")
        self.w.merged_pr(lane, "claude/fresh", number=61)
        res = self.w.run("prune", "--json", clock=REAL_NOW)
        self.assertEqual(res.row("claude-fresh")["reason"], "active in the last 15 minutes")

    def test_an_unused_lane_is_held_for_a_day(self) -> None:
        self.w.new_lane("unused")
        row = self.w.run("prune", "--json").row("claude-unused")
        self.assertEqual(row["action"], "keep")
        self.assertIn("never pushed", row["reason"])
        self.assertEqual(
            self.w.run("prune", "--json", clock=MUCH_LATER).row("claude-unused")["action"], "remove"
        )

    def test_main_moving_on_origin_does_not_make_a_fresh_lane_look_unpushed(self) -> None:
        # Main advances on origin from another clone, so its new tip is not an object here yet (no fetch).
        # The live-sha count drops that missing sha, but HEAD is in origin/main, which is never deleted or
        # rewound, so the unused lane is still pushed and goes after its 24-hour hold.
        lane = self.w.new_lane("idle")
        other = self.w.root / "other-clone"
        self.w.git(["clone", "-q", self.w.origins["DealDex"].as_uri(), str(other)], self.w.root)
        (other / "later.txt").write_text("later\n", encoding="utf-8")
        self.w.git(["add", "later.txt"], other)
        self.w.git(["commit", "-q", "-m", "later"], other)
        self.w.git(["push", "-q", "origin", "HEAD:main"], other)
        new_tip = self.w.git(["rev-parse", "HEAD"], other)
        tree = self.w.trees["DealDex"]
        self.assertEqual(
            self.w.git(["rev-parse", "--verify", "-q", new_tip + "^{commit}"], tree, check=False),
            "",
            "the new tip is an object here",
        )
        self.assertNotEqual(self.w.git(["rev-parse", "refs/remotes/origin/main"], tree), new_tip)
        row = self.w.run("prune", "--json", clock=MUCH_LATER).row("claude-idle")
        self.assertEqual(row["action"], "remove", row)
        self.assertTrue(lane.exists())

    def test_a_process_in_the_lane_or_a_failed_lsof_keeps_it(self) -> None:
        lane = self.w.new_lane("busy")
        self.w.commit(lane)
        self.w.push(lane, "claude/busy")
        self.w.merged_pr(lane, "claude/busy", number=71)
        self.w.lsof = lambda: D.CmdResult(0, f"p4242\nczsh\nn{lane}/sub\n", "")
        self.assertIn(
            "process 4242 (zsh)", self.w.run("prune", "--json").row("claude-busy")["reason"]
        )
        self.w.lsof = lambda: D.CmdResult(None, error="FileNotFoundError: lsof")
        res = self.w.run("prune", "--apply", "--json")
        self.assertEqual(res.rc, 69)
        self.assertIn("lsof failed", res.row("claude-busy")["reason"])
        self.assertTrue(lane.exists())

    def test_seat_scope(self) -> None:
        mine = self.w.new_lane("mine")
        theirs = self.w.new_lane("theirs", seat="CODEX")
        for lane, head, n in ((mine, "claude/mine", 81), (theirs, "codex/theirs", 82)):
            self.w.commit(lane)
            self.w.push(lane, head)
            self.w.merged_pr(lane, head, number=n)
        res = self.w.run("prune", "--json")
        names = [os.path.basename(r["path"]) for r in res.json()["lanes"]]
        self.assertEqual(names, ["claude-mine"])
        self.assertEqual(res.json()["others_not_considered"], 1)
        refused = self.w.run("prune", "--seat", "codex")
        self.assertEqual(refused.rc, 64)
        self.assertIn("not your seat", refused.err)
        self.assertEqual(self.w.run("prune", "--seat", "CLAUDE", "--json").rc, 0)
        other = self.w.run("prune", "--seat", "codex", "--all-seats", "--json")
        self.assertEqual(
            [os.path.basename(r["path"]) for r in other.json()["lanes"]], ["codex-theirs"]
        )
        both = self.w.run("prune", "--all-seats", "--json")
        self.assertEqual(
            sorted(os.path.basename(r["path"]) for r in both.json()["lanes"]),
            ["claude-mine", "codex-theirs"],
        )
        self.assertEqual(self.w.run("prune", env={"AGENT_SEAT": None}).rc, 64)
        self.assertEqual(self.w.run("prune", "--seat", "nobody").rc, 64)

    def test_repo_filter_and_review_checkouts(self) -> None:
        st = self.w.new_lane("elsewhere", repo="Socratic-Trade")
        self.w.commit(st)
        self.w.push(st, "claude/elsewhere")
        self.w.merged_pr(st, "claude/elsewhere", number=91, repo="Socratic-Trade")
        gh = lambda a: D.CmdResult(
            0,
            json.dumps(
                {
                    "headRefOid": SHAS["pr"],
                    "headRefName": "prbranch",
                    "state": "OPEN",
                    "isCrossRepository": False,
                }
            ),
            "",
        )
        made = self.w.run(
            "new",
            "DealDex",
            "--review",
            "--pr",
            "7",
            clock=REAL_NOW,
            override=lambda a: gh(a) if a[0] == "gh" else None,
        )
        self.assertEqual(made.rc, 0, made.err)
        self.w.prs["Simple-With-Us/DealDex"].append(
            {"number": 7, "state": "OPEN", "headRefName": "prbranch", "headRefOid": SHAS["pr"]}
        )
        res = self.w.run("prune", "--repo", "dealdex", "--json")
        self.assertEqual(
            [os.path.basename(r["path"]) for r in res.json()["lanes"]], ["review-pr-7"]
        )
        self.assertEqual(res.row("review-pr-7")["reason"], "review checkout of an open PR")
        self.w.prs["Simple-With-Us/DealDex"][-1]["state"] = "MERGED"
        self.assertEqual(
            self.w.run("prune", "--repo", "DD", "--json").row("review-pr-7")["action"], "remove"
        )
        self.assertEqual(
            self.w.run("prune", "--repo", "ST", "--json").row("claude-elsewhere")["action"],
            "remove",
        )

    def test_a_failed_remove_is_exit_69_and_reported(self) -> None:
        lane = self.w.new_lane("stuck")
        self.w.commit(lane)
        self.w.push(lane, "claude/stuck")
        self.w.merged_pr(lane, "claude/stuck", number=101)
        fail = lambda a: (
            D.CmdResult(128, "", "fatal: simulated refusal")
            if a[4:6] == ["worktree", "remove"]
            else None
        )
        res = self.w.run("prune", "--apply", "--json", override=fail)
        self.assertEqual(res.rc, 69)
        self.assertEqual(res.row("claude-stuck")["action"], "error")
        self.assertTrue(lane.exists())

    def test_worktree_prune_runs_only_for_own_stale_entries_and_never_with_a_dangling_link(
        self,
    ) -> None:
        done = self.w.new_lane("done")
        self.w.commit(done)
        self.w.push(done, "claude/done")
        self.w.merged_pr(done, "claude/done", number=111)
        stale = self.w.new_lane("stale")
        shutil.rmtree(stale)  # the folder vanished (this test simulates it; the tool never does)
        (self.w.lanes / "Gone").symlink_to(self.w.root / "no-such-disk")
        res = self.w.run("prune", "--apply", "--json")
        self.assertTrue(res.row("claude-done")["removed"])
        self.assertEqual(
            res.row("claude-stale")["reason"], "folder is missing (git lists it as prunable)"
        )
        self.assertFalse(res.has("worktree", "prune"))
        self.assertTrue(any("PRUNE-SKIPPED" in n for n in res.json()["notes"]))
        (self.w.lanes / "Gone").unlink()
        again = self.w.new_lane("done2")
        self.w.commit(again)
        self.w.push(again, "claude/done2")
        self.w.merged_pr(again, "claude/done2", number=112)
        res2 = self.w.run("prune", "--apply", "--json")
        self.assertTrue(res2.has("worktree", "prune"))
        self.assertNotIn(str(stale), self.w.worktree_paths())

    def test_the_integration_tree_is_never_a_row(self) -> None:
        res = self.w.run("prune", "--all-seats", "--json")
        self.assertEqual(res.rc, 0, res.err)
        for r in res.json()["lanes"]:
            self.assertNotIn("/Code/", r["path"])

    def test_text_table(self) -> None:
        lane = self.w.new_lane("table")
        self.w.commit(lane)
        res = self.w.run("prune")
        self.assertEqual(res.rc, 0, res.err)
        self.assertIn("DECISION", res.out)
        self.assertIn("claude-table", res.out)
        self.assertIn("Dry run: nothing changed", res.out)


class RetireTests(WorldCase):
    def merged(self, slug: str, n: int, repo: str = "DealDex") -> Path:
        lane = self.w.new_lane(slug, repo=repo)
        self.w.commit(lane)
        self.w.push(lane, f"claude/{slug}")
        self.w.merged_pr(lane, f"claude/{slug}", number=n, repo=repo)
        return lane

    def test_retire_by_slug_removes_even_when_fresh(self) -> None:
        lane = self.merged("finished", 201)
        res = self.w.run("retire", "finished", clock=REAL_NOW)
        self.assertEqual(res.rc, 0, res.err)
        self.assertFalse(lane.exists())
        self.assertEqual(res.out.strip(), str(lane))

    def test_retire_by_path_and_dry_run(self) -> None:
        lane = self.merged("bypath", 202)
        dry = self.w.run("retire", str(lane), "--dry-run")
        self.assertEqual(dry.rc, 0, dry.err)
        self.assertTrue(lane.exists())
        self.assertIn("Would remove", dry.err)
        self.assertEqual(self.w.run("retire", str(lane)).rc, 0)
        self.assertFalse(lane.exists())

    def test_retire_refuses_what_is_not_removable(self) -> None:
        lane = self.w.new_lane("busywork")
        self.w.commit(lane)
        res = self.w.run("retire", "busywork")
        self.assertEqual(res.rc, 64)
        self.assertIn("on no remote ref", res.err)
        self.assertTrue(lane.exists())

    def test_retire_refuses_another_seats_lane_and_unknown_or_ambiguous_slugs(self) -> None:
        theirs = self.w.new_lane("peer", seat="CODEX")
        res = self.w.run("retire", str(theirs))
        self.assertEqual(res.rc, 64)
        self.assertIn("not CLAUDE", res.err)
        self.assertEqual(self.w.run("retire", "nothing-here").rc, 64)
        self.merged("twin", 301)
        self.merged("twin", 302, repo="Socratic-Trade")
        amb = self.w.run("retire", "twin")
        self.assertEqual(amb.rc, 64)
        self.assertIn("more than one lane", amb.err)
        self.assertEqual(self.w.run("retire", "twin", "--repo", "ST").rc, 0)
        self.assertEqual(self.w.run("retire", "/etc").rc, 64)
        self.assertEqual(self.w.run("retire", str(self.w.trees["DealDex"])).rc, 64)

    def test_retire_needs_agent_seat(self) -> None:
        self.merged("noseat", 401)
        self.assertEqual(self.w.run("retire", "noseat", env={"AGENT_SEAT": None}).rc, 64)


class ParserTests(unittest.TestCase):
    def test_usage_errors_are_64(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(
            K.main(["prune", "--bogus"], env={"HOME": "/nonexistent"}, stdout=out, stderr=err), 64
        )
        self.assertEqual(
            K.main(["retire"], env={"HOME": "/nonexistent"}, stdout=out, stderr=err), 64
        )

    def test_help_lists_the_new_commands(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        K.main(["--help"], env={"HOME": "/nonexistent"}, stdout=out, stderr=err)
        self.assertIn("retire", out.getvalue())
        self.assertIn("prune", out.getvalue())


if __name__ == "__main__":
    unittest.main()
