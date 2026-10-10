"""doctor tests: the read-only runner, discovery, PR matching, safety classes and the CLI.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_doctor -v

Most tests build real throwaway git repos under a temp directory and run the real `run_cmd`
against them, because the property that matters (the doctor changes nothing) cannot be shown with
mocks.  Only the network and process table are faked: `gh_prs` and `lsof_cwds` are injected, the
sweep never scans the real /tmp (`tmp_scan_roots` is a directory inside the test), and the repos
are built with a hermetic git config (no global hooks, templates or signing).  The throwaway home
sits inside the macOS temp directory, which is exactly the case the forbidden-tmp skip rule in
layout.py handles, so the sanctioned-path tests double as a regression test for it.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile
import unittest
from typing import Callable
from unittest import mock

from fleet_lanes import doctor
from fleet_lanes import layout as L

OWNER = "Simple-With-Us"
FIXED_NOW = dt.datetime(2026, 10, 7, 16, 15, tzinfo=dt.timezone.utc)
# Fixtures are made after FIXED_NOW, and ages clamp at zero, so under `fixed_clock` every lane reads as
# brand new (age 0, idle 0).  That is what the fresh-lane tests want; every other test uses `aged()`.
OLD_DAYS = 30

REGISTRY_DATA = {
    "owner": OWNER,
    "apps": [
        {"repo": "DealDex", "acronym": "DD", "worktreePrefix": "dealdex", "codeDir": "DealDex"},
        {"repo": "AI-Fleet-Coordinator", "acronym": "AFC", "worktreePrefix": "fleet",
         "codeDir": "AI-Fleet-Coordinator"},
        {"repo": "Socratic-Trade", "acronym": "ST", "worktreePrefix": "trading", "codeDir": "Socratic-Trade"},
    ],
    "seats": [
        {"tag": "CLAUDE", "worktreeSuffix": "claude", "branchPrefixes": ["claude/"]},
        {"tag": "CODEX", "worktreeSuffix": "codex", "branchPrefixes": ["codex/"]},
        {"tag": "AG", "worktreeSuffix": "antigravity", "branchPrefixes": ["ag/", "agent/antigravity"]},
        {"tag": "MM", "worktreeSuffix": "minimax", "branchPrefixes": ["minimax/"]},
    ],
}
DEALDEX_URL = f"https://github.com/{OWNER}/DealDex.git"
DEALDEX_REPO = f"{OWNER}/DealDex"
KEEP_REASON = "keep marker .janitor-keep: ask the owner first"


def fixed_clock() -> dt.datetime:
    return FIXED_NOW


def hermetic_git_env() -> dict[str, str]:
    """Environment overlay so test repos never read the owner's global git config."""
    return {
        "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Doctor Test", "GIT_AUTHOR_EMAIL": "doctor@test.invalid",
        "GIT_COMMITTER_NAME": "Doctor Test", "GIT_COMMITTER_EMAIL": "doctor@test.invalid",
    }


class FakeGh:
    """repo -> PR list (or None for a failed lookup).  Records every call."""

    def __init__(self, data: dict[str, list[dict] | None] | None = None, default: list[dict] | None = None):
        self.data = data or {}
        self.default = [] if default is None else default
        self.calls: list[str] = []

    def __call__(self, repo: str) -> list[dict] | None:
        self.calls.append(repo)
        return self.data[repo] if repo in self.data else self.default


class GitCase(unittest.TestCase):
    """A throwaway home, a fake tmp root, and helpers that build real git repos."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(os.path.realpath(tmp.name))
        self.home = self.base / "home"
        self.faketmp = self.base / "faketmp"
        self.home.mkdir()
        self.faketmp.mkdir()
        patcher = mock.patch.dict(os.environ, hermetic_git_env())
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in (*doctor._STRIP_ENV, "GIT_OPTIONAL_LOCKS"):
            os.environ.pop(name, None)
        self.registry = L.parse_registry(REGISTRY_DATA)
        self.env = {"TMPDIR": str(self.faketmp), "HOME": str(self.home)}

    # ---- git fixtures (raw subprocess: this is test setup, not the doctor)
    def git(self, cwd: pathlib.Path, *args: str) -> str:
        res = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
        return res.stdout.strip()

    def make_repo(self, path: pathlib.Path, *, remote: str | None = DEALDEX_URL, pushed: bool = True,
                  branch: str = "main") -> pathlib.Path:
        path.mkdir(parents=True, exist_ok=True)
        self.git(path, "init", "-q", "-b", branch)
        (path / "README.md").write_text("hello\n")
        self.git(path, "add", "README.md")
        self.git(path, "commit", "-q", "-m", "first")
        if remote:
            self.git(path, "remote", "add", "origin", remote)
            if pushed:
                self.git(path, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
        return path

    def commit(self, path: pathlib.Path, name: str = "work.txt", text: str = "x\n") -> str:
        (path / name).write_text(text)
        self.git(path, "add", name)
        self.git(path, "commit", "-q", "-m", f"add {name}")
        return self.git(path, "rev-parse", "HEAD")

    def push(self, path: pathlib.Path, branch: str) -> None:
        self.git(path, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")

    def add_worktree(self, parent: pathlib.Path, path: pathlib.Path, branch: str, *, pushed: bool = True) -> pathlib.Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.git(parent, "worktree", "add", "-q", "-b", branch, str(path))
        if pushed:
            self.push(path, branch)
        return path

    def integration(self, name: str = "DealDex", **kw) -> pathlib.Path:
        return self.make_repo(self.home / "Code" / name, **kw)

    def lane(self, parent: pathlib.Path, dirname: str, branch: str, **kw) -> pathlib.Path:
        return self.add_worktree(parent, self.home / "apps" / dirname, branch, **kw)

    # ---- running the doctor
    @staticmethod
    def aged(days: float) -> Callable[[], dt.datetime]:
        """A clock `days` past the real time, so a fixture built now reads about that old.  Never
        build it from FIXED_NOW: that date passes and the test would start failing."""
        return lambda: dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)

    def report(self, **kw) -> dict:
        kw.setdefault("env", self.env)
        kw.setdefault("tmp_scan_roots", [self.faketmp])
        kw.setdefault("registry", self.registry)
        kw.setdefault("case_insensitive", False)
        kw.setdefault("lsof_cwds", lambda: [])
        kw.setdefault("gh_prs", FakeGh())
        kw.setdefault("clock", self.aged(OLD_DAYS))
        return doctor.build_report(self.home, **kw)

    @staticmethod
    def by_path(rep: dict) -> dict[str, dict]:
        return {c["realpath"]: c for c in rep["checkouts"]}

    def one(self, rep: dict, path: pathlib.Path) -> dict:
        found = self.by_path(rep)
        self.assertIn(os.path.realpath(path), found, sorted(found))
        return found[os.path.realpath(path)]


# --------------------------------------------------------------------------- the runner

FORBIDDEN_COMMANDS = [
    ["git", "diff"], ["git", "diff", "--name-only", "HEAD"], ["git", "fetch"], ["git", "fetch", "--all"],
    ["git", "gc"], ["git", "checkout", "main"], ["git", "reset", "--hard"], ["git", "stash"],
    ["git", "stash", "list"], ["git", "worktree", "add", "/x"], ["git", "worktree", "remove", "/x"],
    ["git", "worktree", "prune"], ["git", "worktree", "move", "/a", "/b"], ["git", "clean", "-fd"],
    ["git", "commit", "-m", "x"], ["git", "push"], ["git", "merge-base", "a", "b"], ["git", "cherry", "x"],
    ["git", "cat-file", "-p", "HEAD"], ["git", "symbolic-ref", "HEAD"], ["git", "update-ref", "-d", "x"],
    ["git", "branch", "-D", "x"], ["git", "branch"], ["git", "branch", "--show-current", "x"],
    ["git", "config", "--unset", "user.name"], ["git", "config", "user.name", "x"],
    ["git", "config", "--get-regexp", "x"], ["git", "remote", "set-url", "origin", "x"],
    ["git", "remote", "add", "o", "x"], ["git", "remote"], ["git", "log", "--output=/tmp/x"],
    ["git", "log", "-p"], ["git", "status", "--ignored=matching"], ["git", "status", "--ignored", "x.txt"],
    ["git", "status", "x.txt"], ["git", "rev-list", "--count", "HEAD", "--remotes=a b"],
    ["git", "rev-list", "--count", "HEAD", "--remotes=-x"], ["git", "rev-list", "--count", "HEAD", "--remotes=a/b"],
    ["git", "rev-list", "--count", "HEAD", "--remotes="],
    ["git", "-c", "core.fsmonitor=true", "status"], ["git", "-C", "/x", "status"],
    ["git", "--git-dir=/x", "status"], ["git", "rev-list", "--output=x", "HEAD"],
    ["git", "for-each-ref", "--exec=x"], ["git", "worktree", "list", "--verbose"], ["git"], [],
    ["rm", "-rf", "/x"], ["mv", "a", "b"], ["sh", "-c", "git status"], ["/usr/bin/git", "status"],
    ["gh", "pr", "merge", "1"], ["gh", "pr", "list"], ["gh", "pr", "list", "--repo", "a/b", "--web"],
    ["gh", "pr", "list", "--repo", "a/b", "--search", "x"], ["gh", "pr", "list", "--repo", "a;rm/b"],
    ["gh", "pr", "list", "--repo", "a/b", "--limit", "99999"], ["gh", "api", "repos/a/b"],
    ["gh", "repo", "delete", "a/b"], ["lsof"], ["lsof", "-i"], ["lsof", "-d", "cwd"],
    ["du", "-sh", "/x"], ["du", "-sk", "/x"], ["du", "-sk", "-x"], ["du", "-sk", "-x", "-L", "/x"],
]
ALLOWED_COMMANDS = [
    (["git", "status", "--porcelain=v1", "--untracked-files=normal"], "git"),
    (["git", "status", "--porcelain=v1", "--untracked-files=normal", "--ignored"], "git"),
    (["git", "rev-parse", "--show-toplevel", "--git-dir"], "git"),
    (["git", "rev-list", "--count", "HEAD", "--not", "--remotes=origin", "--remotes=up-stream.2"], "git"),
    (["git", "rev-list", "--count", "--branches", "--not", "--remotes=origin"], "git"),
    (["git", "for-each-ref", "--format=%(refname)", "refs/remotes"], "git"),
    (["git", "config", "--get", "remote.scratch.url"], "git"),
    (["git", "rev-parse", "HEAD"], "git"),
    (["git", "rev-list", "--count", "HEAD", "--not", "--remotes"], "git"),
    (["git", "rev-list", "--count", "--branches", "--not", "--remotes"], "git"),
    (["git", "rev-list", "--count", "HEAD", "--not", "0123abc"], "git"),
    (["git", "log", "-1", "--format=%ct"], "git"),
    (["git", "for-each-ref", "--format=%(upstream:short)|%(upstream:track)", "refs/heads/x"], "git"),
    (["git", "for-each-ref", "--count=1", "--contains", "HEAD", "--format=%(refname)", "refs/heads"], "git"),
    (["git", "branch", "--show-current"], "git"),
    (["git", "config", "--get", "remote.origin.url"], "git"),
    (["git", "remote", "get-url", "origin"], "git"),
    (["git", "worktree", "list", "--porcelain"], "git"),
    (["git", "worktree", "list"], "git"),
    (["gh", "pr", "list", "--repo", "a/b", "--state", "all", "--limit", "500", "--json",
      "number,state,headRefName,headRefOid"], "gh"),
    (["lsof", "-d", "cwd", "-Fpcn"], "lsof"),
    (["du", "-sk", "-x", "/some/path"], "du"),
]


class FakeProc:
    def __init__(self, out: bytes = b"", err: bytes = b"", rc: int = 0, timeouts: int = 0):
        self.pid = 4242
        self.returncode = rc
        self._out, self._err, self._timeouts = out, err, timeouts
        self.communicate_timeouts: list[float | None] = []

    def communicate(self, timeout: float | None = None):
        self.communicate_timeouts.append(timeout)
        if self._timeouts > 0:
            self._timeouts -= 1
            raise subprocess.TimeoutExpired("x", timeout or 0)
        return self._out, self._err

    def kill(self) -> None:
        pass


class RunCmdTests(unittest.TestCase):
    def test_every_forbidden_command_is_refused_before_a_process_exists(self) -> None:
        boom = AssertionError("a subprocess was started for a refused command")
        with mock.patch.object(subprocess, "Popen", side_effect=boom) as popen, \
                mock.patch.object(subprocess, "run", side_effect=boom) as run, \
                mock.patch.object(subprocess, "check_output", side_effect=boom), \
                mock.patch.object(os, "system", side_effect=boom):
            for argv in FORBIDDEN_COMMANDS:
                with self.subTest(argv=argv):
                    with self.assertRaises(doctor.CommandRefused):
                        doctor.run_cmd(argv, cwd="/", timeout=1)
                    with self.assertRaises(doctor.CommandRefused):
                        doctor.check_allowed(argv)
            popen.assert_not_called()
            run.assert_not_called()

    def test_allowlisted_commands_pass_validation(self) -> None:
        for argv, tool in ALLOWED_COMMANDS:
            with self.subTest(argv=argv):
                self.assertEqual(doctor.check_allowed(argv), tool)

    def test_environment_and_argv_are_hardened(self) -> None:
        proc = FakeProc(b"ok\n")
        bogus = {"GIT_DIR": "/elsewhere", "GIT_INDEX_FILE": "/elsewhere/index", "GIT_WORK_TREE": "/w",
                 "GIT_COMMON_DIR": "/c", "GIT_OBJECT_DIRECTORY": "/o", "GIT_OPTIONAL_LOCKS": "1", "LC_ALL": "fr_FR"}
        with mock.patch.dict(os.environ, bogus), \
                mock.patch.object(subprocess, "Popen", return_value=proc) as popen:
            res = doctor.run_cmd(["git", "rev-parse", "HEAD"], cwd="/some/repo")
        self.assertTrue(res.ok)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "rev-parse", "HEAD"])
        env = kwargs["env"]
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")
        self.assertEqual(env["LC_ALL"], "C")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        for name in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY"):
            self.assertNotIn(name, env)
        self.assertEqual(kwargs["cwd"], "/some/repo")
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])

    def test_timeouts_are_capped_per_tool(self) -> None:
        def seen(argv: list[str], timeout: float | None) -> float:
            proc = FakeProc()
            with mock.patch.object(subprocess, "Popen", return_value=proc):
                doctor.run_cmd(argv, cwd="/", timeout=timeout)
            return proc.communicate_timeouts[0]
        git = ["git", "rev-parse", "HEAD"]
        self.assertEqual(seen(git, None), 20)
        self.assertEqual(seen(git, 999), 20)
        self.assertEqual(seen(git, 5), 5)
        self.assertEqual(seen(["lsof", "-d", "cwd", "-Fpcn"], 999), 20)
        self.assertEqual(seen(["du", "-sk", "-x", "/p"], None), 30)
        self.assertEqual(seen(["du", "-sk", "-x", "/p"], 999), 30)

    def test_a_timeout_kills_the_process_group_and_is_reported(self) -> None:
        proc = FakeProc(timeouts=1)
        with mock.patch.object(subprocess, "Popen", return_value=proc), \
                mock.patch.object(os, "killpg") as killpg:
            res = doctor.run_cmd(["git", "status", "--porcelain=v1"], cwd="/")
        self.assertTrue(res.timed_out)
        self.assertFalse(res.ok)
        killpg.assert_called_once_with(4242, signal.SIGKILL)
        self.assertEqual(res.failure_text(), "timed out")

    def test_a_missing_binary_is_a_result_not_an_exception(self) -> None:
        with mock.patch.object(subprocess, "Popen", side_effect=FileNotFoundError("gh")):
            res = doctor.run_cmd(["gh", "pr", "list", "--repo", "a/b"], cwd=None)
        self.assertFalse(res.ok)
        self.assertIn("FileNotFoundError", res.error)

    def test_stderr_is_redacted_when_reported(self) -> None:
        res = doctor.CmdResult(128, err="fatal: unable to access 'https://user:s3cret@github.com/o/r.git/'\n")
        self.assertNotIn("s3cret", res.failure_text())
        self.assertIn("REDACTED@", res.failure_text())


# --------------------------------------------------------------------------- pure helpers

class ParsingTests(unittest.TestCase):
    def test_redact_url(self) -> None:
        cases = {
            "https://user:token@github.com/o/r.git": "https://REDACTED@github.com/o/r.git",
            "https://ghp_abc@github.com/o/r.git": "https://REDACTED@github.com/o/r.git",
            "https://x-access-token:abc123@github.com/o/r": "https://REDACTED@github.com/o/r",
            "ssh://git@github.com/o/r.git": "ssh://git@github.com/o/r.git",
            "git@github.com:o/r.git": "git@github.com:o/r.git",
            "https://github.com/o/r.git": "https://github.com/o/r.git",
            "/Users/jay/Code/DealDex": "/Users/jay/Code/DealDex",
        }
        for raw, want in cases.items():
            self.assertEqual(doctor.redact_url(raw), want, raw)
        self.assertEqual(doctor.redact_text("see https://a:b@h/x and ssh://c:d@h2/y"),
                         "see https://REDACTED@h/x and ssh://REDACTED@h2/y")

    def test_redaction_covers_slashes_and_ats_in_passwords_and_scp_tokens(self) -> None:
        leaks = {
            "https://user:pa/ssSECRET@github.com/Simple-With-Us/Scn.git":
                "https://REDACTED@github.com/Simple-With-Us/Scn.git",
            "https://user:p@ssSECRET@github.com/Simple-With-Us/Scn.git":
                "https://REDACTED@github.com/Simple-With-Us/Scn.git",
            "https://:onlySECRET@github.com/o/r.git": "https://REDACTED@github.com/o/r.git",
            "https://tok@enSECRET@github.com/o/r.git": "https://REDACTED@github.com/o/r.git",
            "ghp_SCPSECRET@github.com:Simple-With-Us/Scn.git": "REDACTED@github.com:Simple-With-Us/Scn.git",
            "user:SCPSECRET@host.example:path/r.git": "REDACTED@host.example:path/r.git",
        }
        for raw, want in leaks.items():
            with self.subTest(raw=raw):
                self.assertEqual(doctor.redact_url(raw), want)
                self.assertNotIn("SECRET", doctor.redact_text(f"fatal: could not read from '{raw}': denied"))
                self.assertEqual(doctor.redact_url(want), want)  # redacting twice changes nothing
        untouched = ["git@github.com:o/r.git", "ssh://git@github.com/o/r.git", "https://github.com/o/r.git",
                     "https://registry.npmjs.org/@scope/pkg", "me@example.com", "/Users/jay/a@b:c",
                     "./dir@host:thing", "file:///Users/jay/Code/DealDex"]
        for raw in untouched:
            with self.subTest(raw=raw):
                self.assertEqual(doctor.redact_url(raw), raw)
        self.assertEqual(doctor.redact_text("Permission denied for git@github.com: no key"),
                         "Permission denied for git@github.com: no key")

    def test_failure_text_never_shows_the_unredacted_forms(self) -> None:
        res = doctor.CmdResult(128, err="fatal: unable to access 'https://user:pa/ssSECRET@github.com/o/r.git/'\n")
        self.assertNotIn("SECRET", res.failure_text())
        self.assertNotIn("SECRET", doctor.CmdResult(None, error="OSError: ghp_SCPSECRET@github.com:o/r.git").failure_text())

    def test_parse_owner_repo(self) -> None:
        self.assertEqual(doctor.parse_owner_repo("https://github.com/Simple-With-Us/Congress.Trade.git"),
                         "Simple-With-Us/Congress.Trade")
        self.assertEqual(doctor.parse_owner_repo("git@github.com:Simple-With-Us/DealDex.git"), DEALDEX_REPO)
        self.assertEqual(doctor.parse_owner_repo("https://REDACTED@github.com/o/r/"), "o/r")
        self.assertIsNone(doctor.parse_owner_repo("https://gitlab.com/o/r.git"))
        self.assertIsNone(doctor.parse_owner_repo("/Users/jay/Code/X"))
        self.assertIsNone(doctor.parse_owner_repo(None))

    def test_worktree_porcelain(self) -> None:
        text = (
            "worktree /r/main\nHEAD aaa\nbranch refs/heads/main\n\n"
            "worktree /r/lane\nHEAD bbb\ndetached\nlocked\n\n"
            "worktree /r/gone\nHEAD ccc\nbranch refs/heads/claude/x\nprunable gitdir file points to non-existent location\n\n"
            "worktree /r/bare\nbare\n\n"
        )
        recs = doctor.parse_worktree_porcelain(text)
        self.assertEqual([r.path for r in recs], ["/r/main", "/r/lane", "/r/gone", "/r/bare"])
        self.assertEqual(recs[0].branch, "main")
        self.assertTrue(recs[1].detached and recs[1].locked)
        self.assertEqual(recs[2].branch, "claude/x")
        self.assertIn("non-existent", recs[2].prunable or "")
        self.assertTrue(recs[3].bare)
        self.assertEqual(doctor.parse_worktree_porcelain(""), [])

    def test_count_status(self) -> None:
        self.assertEqual(doctor.count_status(""), (0, 0))
        self.assertEqual(doctor.count_status(" M a\nM  b\nD  c\nR  d -> e\nUU f\n?? g\n?? h/\n"), (5, 2))

    def test_parse_lsof(self) -> None:
        rows = doctor.parse_lsof("p12\ncnode\nn/Users/jay/apps/x\np13\ncbash\nn/\nnrelative\n")
        self.assertEqual(rows, [(12, "node", "/Users/jay/apps/x"), (13, "bash", "/")])


class MatchPrTests(unittest.TestCase):
    SHA = "a" * 40
    OTHER = "b" * 40

    def pr(self, number: int, state: str, branch: str = "claude/x", oid: str | None = None) -> dict:
        return {"number": number, "state": state, "headRefName": branch, "headRefOid": oid or self.SHA}

    def test_merged_head_equal_to_local_head_is_merged(self) -> None:
        m = doctor.match_pr("claude/x", self.SHA, [self.pr(7, "MERGED")])
        self.assertEqual((m.state, m.number), ("MERGED", 7))

    def test_detached_head_matches_by_sha_alone(self) -> None:
        m = doctor.match_pr(None, self.SHA, [self.pr(7, "MERGED", branch="other")])
        self.assertEqual(m.state, "MERGED")

    def test_open_pr_wins_over_merged(self) -> None:
        m = doctor.match_pr("claude/x", self.SHA, [self.pr(7, "MERGED"), self.pr(9, "OPEN", oid=self.OTHER)])
        self.assertEqual((m.state, m.number), ("OPEN", 9))

    def test_commits_beyond_a_merged_pr(self) -> None:
        prs = [self.pr(7, "MERGED", oid=self.OTHER)]
        m = doctor.match_pr("claude/x", self.SHA, prs, beyond=lambda oid: 3)
        self.assertEqual((m.state, m.number, m.beyond), ("BEYOND-MERGED", 7, 3))
        unverified = doctor.match_pr("claude/x", self.SHA, prs, beyond=lambda oid: None)
        self.assertEqual((unverified.state, unverified.beyond), ("BEYOND-MERGED", None))
        contained = doctor.match_pr("claude/x", self.SHA, prs, beyond=lambda oid: 0)
        self.assertEqual(contained.state, "MERGED")

    def test_closed_unmerged_exact_head(self) -> None:
        self.assertEqual(doctor.match_pr("claude/x", self.SHA, [self.pr(3, "CLOSED")]).state, "CLOSED")
        self.assertEqual(doctor.match_pr("claude/x", self.SHA, [self.pr(3, "CLOSED", oid=self.OTHER)]).state, "NONE")

    def test_none_unknown_and_truncated(self) -> None:
        self.assertEqual(doctor.match_pr("claude/x", self.SHA, []).state, "NONE")
        self.assertEqual(doctor.match_pr("claude/x", self.SHA, None).state, "UNKNOWN")
        self.assertEqual(doctor.match_pr("claude/x", self.SHA, [], truncated=True).state, "UNKNOWN")

    def test_gh_command_shape_and_failure_modes(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv, cwd=None, timeout=None):
            calls.append(list(argv))
            return doctor.CmdResult(0, json.dumps([{"number": 1}]))
        fetch = doctor.make_gh_fetcher(fake_run)
        self.assertEqual(fetch("o/r"), [{"number": 1}])
        self.assertEqual(calls, [["gh", "pr", "list", "--repo", "o/r", "--state", "all", "--limit", "500",
                                  "--json", "number,state,headRefName,headRefOid,mergedAt,closedAt"]])
        for bad in (doctor.CmdResult(1, err="auth"), doctor.CmdResult(None, timed_out=True),
                    doctor.CmdResult(None, error="FileNotFoundError"), doctor.CmdResult(0, "not json"),
                    doctor.CmdResult(0, "{}")):
            self.assertIsNone(doctor.make_gh_fetcher(lambda *a, _b=bad, **k: _b)("o/r"))
        read = doctor.make_lsof_reader(lambda *a, **k: doctor.CmdResult(1, "p1\ncx\nn/a\n"))
        self.assertEqual(read(), [(1, "x", "/a")])
        self.assertIsNone(doctor.make_lsof_reader(lambda *a, **k: doctor.CmdResult(None, error="x"))())
        self.assertIsNone(doctor.make_lsof_reader(lambda *a, **k: doctor.CmdResult(0, ""))())


# --------------------------------------------------------------------------- scanning

class ScanTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = pathlib.Path(os.path.realpath(tmp.name))

    def repo(self, *rel: str, git_file: bool = False) -> str:
        d = self.root.joinpath(*rel)
        d.mkdir(parents=True, exist_ok=True)
        if git_file:
            (d / ".git").write_text("gitdir: /nowhere\n")
        else:
            (d / ".git").mkdir()
        return str(d)

    def test_depth_convention(self) -> None:
        d0 = self.repo()
        d1 = self.repo("a")
        d4 = self.repo("b", "c", "d", "e")
        d5 = self.repo("p", "q", "r", "s", "t")
        found = doctor.find_git_dirs(str(self.root), 4, check_root=False)
        self.assertEqual(found, sorted([d1, d4]))
        self.assertNotIn(d5, found)
        self.assertEqual(doctor.find_git_dirs(str(self.root), 4, check_root=True), [d0])

    def test_skips_noise_symlinks_and_nested_checkouts(self) -> None:
        keep = self.repo("keep")
        self.repo("node_modules", "pkg")
        self.repo(".Trash", "old")
        self.repo("Library", "Caches", "x")
        self.repo("keep", "vendor", "inner")  # nested inside a checkout: never reported
        (self.root / "link").symlink_to(self.root / "keep")
        found = doctor.find_git_dirs(str(self.root), 4, check_root=False)
        self.assertEqual(found, [keep])

    def test_file_and_directory_dot_git_both_count(self) -> None:
        a = self.repo("clone")
        b = self.repo("linked", git_file=True)
        self.assertEqual(doctor.find_git_dirs(str(self.root), 2, check_root=False), sorted([a, b]))

    def test_already_found_paths_are_not_descended(self) -> None:
        a = self.repo("outer", "x")
        found = doctor.find_git_dirs(str(self.root), 4, check_root=False, found={a}, fold=str)
        self.assertEqual(found, [])

    def test_the_plan_covers_the_places_checkouts_hide(self) -> None:
        roots = L.make_roots(self.root, {}, registry=self.registry(), case_insensitive=False)
        (self.root / "Code" / "DealDex" / ".claude" / "worktrees").mkdir(parents=True)
        (self.root / "Code" / "DealDex" / ".muse" / "worktrees").mkdir(parents=True)
        plan = {(s.label, s.depth) for s in doctor.scan_plan(roots, [self.root / "t"], deep=False)}
        for want in (("tmp", 4), ("apps", 2), ("lanes", 4), ("code", 1), ("code-claude-worktrees", 1),
                     ("code-muse-worktrees", 1),
                     (".codex/worktrees", 3), (".cursor/worktrees", 3), (".grok", 4), (".fx", 4),
                     (".botfleet", 5), (".gemini/antigravity", 5), (".buzz", 3), ("home", 3)):
            self.assertIn(want, plan)
        labels = {s.label for s in doctor.scan_plan(roots, [], deep=False)}
        self.assertFalse({"documents", "desktop", "downloads"} & labels)
        deep = {(s.label, s.depth) for s in doctor.scan_plan(roots, [], deep=True)}
        self.assertTrue({("documents", 4), ("desktop", 4), ("downloads", 4)} <= deep)
        home_spec = next(s for s in doctor.scan_plan(roots, [], deep=False) if s.label == "home")
        self.assertTrue({"Documents", "Desktop", "Downloads", "Library", "node_modules"} <= home_spec.skip)
        self.assertTrue({".cache", ".npm", ".cargo", ".rustup", ".nvm", ".pyenv", ".Trash"} <= home_spec.skip)

    def registry(self) -> L.Registry:
        return L.parse_registry(REGISTRY_DATA)


# --------------------------------------------------------------------------- classification

class SafetyTests(GitCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = self.integration()

    def test_clean_pushed_lane_is_safe_to_remove_with_evidence(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-clean", "claude/clean")
        co = self.one(self.report(), lane)
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE")
        self.assertFalse(co["dropped_ball"])
        text = " ".join(co["safety_reasons"])
        self.assertIn("working tree clean", text)
        self.assertIn("remote-tracking", text)
        self.assertIn("no process", text)
        self.assertEqual((co["kind"], co["location_class"], co["branch"]), ("LINKED-WORKTREE", "LANE_FLAT_LEGACY", "claude/clean"))
        self.assertEqual(co["name_verdict"], "CONFORMING")
        self.assertEqual(co["branch_verdict"], "CONFORMING")
        self.assertEqual(co["owner_repo"], DEALDEX_REPO)
        self.assertEqual(co["scope"], "fleet")
        self.assertEqual(co["parent_repo"], os.path.realpath(self.parent))
        self.assertEqual(co["pr_state"], "NONE")

    def test_dirty_lane_counts_tracked_and_untracked_and_is_a_dropped_ball(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-dirty", "claude/dirty")
        (lane / "README.md").write_text("changed\n")
        (lane / "new1.txt").write_text("1")
        (lane / "new2.txt").write_text("2")
        co = self.one(self.report(), lane)
        self.assertEqual((co["dirty_tracked"], co["dirty_untracked"]), (1, 2))
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertIn("dirty: 1 tracked-modified, 2 untracked", co["safety_reasons"])
        self.assertIs(co["dropped_ball"], True)

    def test_unpushed_commits_need_review_and_an_open_pr_is_not_a_dropped_ball(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-ahead", "claude/ahead")
        self.commit(lane)
        co = self.one(self.report(), lane)
        self.assertEqual(co["unpushed"], 1)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertIn("unpushed commits: 1", co["safety_reasons"])
        self.assertIs(co["dropped_ball"], True)
        head = self.git(lane, "rev-parse", "HEAD")
        gh = FakeGh({DEALDEX_REPO: [{"number": 5, "state": "OPEN", "headRefName": "claude/ahead", "headRefOid": head}]})
        co = self.one(self.report(gh_prs=gh), lane)
        self.assertEqual((co["pr_state"], co["pr_number"]), ("OPEN", 5))
        self.assertIn("open PR #5", co["safety_reasons"])
        self.assertIs(co["dropped_ball"], False)

    def test_squash_merged_lane_with_no_remote_branch_is_safe(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-squash", "claude/squash", pushed=False)
        head = self.commit(lane)
        self.commit(lane, "more.txt")
        head = self.git(lane, "rev-parse", "HEAD")
        merged = [{"number": 12, "state": "MERGED", "headRefName": "claude/squash", "headRefOid": head,
                   "mergedAt": "2026-10-06T10:00:00Z"}]
        co = self.one(self.report(gh_prs=FakeGh({DEALDEX_REPO: merged})), lane)
        self.assertEqual(co["unpushed"], 2)
        self.assertEqual(co["pr_state"], "MERGED")
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE", co["safety_reasons"])
        self.assertTrue(any("PR #12 merged" in r for r in co["safety_reasons"]))
        self.assertIs(co["dropped_ball"], False)

    def test_commits_beyond_a_merged_pr_need_review(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-beyond", "claude/beyond", pushed=False)
        merged_head = self.commit(lane)
        self.commit(lane, "after-merge.txt")
        pr = {"number": 12, "state": "MERGED", "headRefName": "claude/beyond", "headRefOid": merged_head}
        co = self.one(self.report(gh_prs=FakeGh({DEALDEX_REPO: [pr]})), lane)
        self.assertEqual(co["pr_state"], "BEYOND-MERGED")
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertIn("commits beyond merged PR #12 (1 commit)", co["safety_reasons"])
        self.assertIn("unpushed commits: 2", co["safety_reasons"])
        self.assertIs(co["dropped_ball"], True)

    def test_merged_pr_head_missing_locally_is_beyond_and_unverified(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-nolocal", "claude/nolocal", pushed=False)
        self.commit(lane)
        pr = {"number": 4, "state": "MERGED", "headRefName": "claude/nolocal", "headRefOid": "f" * 40}
        co = self.one(self.report(gh_prs=FakeGh({DEALDEX_REPO: [pr]})), lane)
        self.assertEqual(co["pr_state"], "BEYOND-MERGED")
        self.assertTrue(any("merged head not in local repo" in r for r in co["safety_reasons"]))

    def test_head_contained_in_the_merged_pr_head_is_covered(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-behind", "claude/behind", pushed=False)
        old = self.commit(lane)
        newer = self.commit(lane, "later.txt")
        self.git(lane, "reset", "-q", "--hard", old)
        pr = {"number": 8, "state": "MERGED", "headRefName": "claude/behind", "headRefOid": newer}
        co = self.one(self.report(gh_prs=FakeGh({DEALDEX_REPO: [pr]})), lane)
        self.assertEqual(co["pr_state"], "MERGED")
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE", co["safety_reasons"])

    def test_gh_failure_is_unknown_and_never_hides_work(self) -> None:
        clean = self.lane(self.parent, "dealdex-claude-ghclean", "claude/ghclean")
        ahead = self.lane(self.parent, "dealdex-claude-ghahead", "claude/ghahead", pushed=False)
        self.commit(ahead)
        dirty = self.lane(self.parent, "dealdex-claude-ghdirty", "claude/ghdirty")
        (dirty / "scratch.txt").write_text("x")
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: None}))
        c_clean, c_ahead, c_dirty = (self.one(rep, p) for p in (clean, ahead, dirty))
        self.assertEqual(c_clean["pr_state"], "UNKNOWN")
        self.assertEqual(c_clean["safety"], "UNKNOWN")  # it may have an open PR, which gh would have shown
        self.assertIn("PR state unknown: gh lookup failed or skipped", c_clean["safety_reasons"])
        self.assertEqual(c_ahead["safety"], "NEEDS-REVIEW")
        self.assertIsNone(c_ahead["dropped_ball"])  # cannot claim "no open PR"
        self.assertTrue(any("PR state unknown" in r for r in c_ahead["safety_reasons"]))
        self.assertEqual(c_dirty["safety"], "NEEDS-REVIEW")
        self.assertIsNone(c_dirty["dropped_ball"])
        self.assertEqual(rep["summary"]["dropped_ball"], 0)
        self.assertEqual(rep["summary"]["dropped_ball_unknown"], 2)

    def test_gh_that_raises_is_treated_as_a_failed_lookup(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-boom", "claude/boom")

        def boom(repo: str):
            raise RuntimeError("network down")
        self.assertEqual(self.one(self.report(gh_prs=boom), lane)["pr_state"], "UNKNOWN")

    def test_a_pr_list_that_fills_the_limit_cannot_prove_there_is_no_pr(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-trunc", "claude/trunc", pushed=False)
        self.commit(lane)
        other = {"number": 1, "state": "MERGED", "headRefName": "someone/else", "headRefOid": "e" * 40}
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: [other]}), gh_limit=1)
        co = self.one(rep, lane)
        self.assertEqual(co["pr_state"], "UNKNOWN")
        self.assertIn("1 limit", co["pr_basis"])
        self.assertIsNone(co["dropped_ball"])
        co = self.one(self.report(gh_prs=FakeGh({DEALDEX_REPO: [other]}), gh_limit=2), lane)
        self.assertEqual(co["pr_state"], "NONE")

    def test_no_gh_skips_every_lookup(self) -> None:
        self.lane(self.parent, "dealdex-claude-nogh", "claude/nogh")
        gh = FakeGh()
        rep = self.report(use_gh=False, gh_prs=gh)
        self.assertEqual(gh.calls, [])
        self.assertEqual(rep["gh"], "skipped")
        self.assertEqual({c["pr_state"] for c in rep["checkouts"]} - {"N/A"}, {"UNKNOWN"})

    def test_one_bulk_gh_call_per_repo_and_none_for_default_branches(self) -> None:
        for n in range(3):
            self.lane(self.parent, f"dealdex-claude-n{n}", f"claude/n{n}")
        other = self.make_repo(self.home / "Code" / "Socratic-Trade", remote=f"https://github.com/{OWNER}/Socratic-Trade.git")
        self.lane(other, "trading-claude-one", "claude/one")
        gh = FakeGh()
        self.report(gh_prs=gh)
        self.assertEqual(sorted(gh.calls), [DEALDEX_REPO, f"{OWNER}/Socratic-Trade"])

    def test_active_from_process_cwd_nested_checkout_and_special_dirs(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-busy", "claude/busy")
        sub = lane / "src"
        sub.mkdir()
        rep = self.report(lsof_cwds=lambda: [(321, "node", str(sub)), (322, "zsh", str(lane))])
        co = self.one(rep, lane)
        self.assertEqual(co["safety"], "ACTIVE")
        self.assertEqual(co["cwd_procs"], ["321:node", "322:zsh"])
        self.assertTrue(co["active"])
        self.assertIs(co["dropped_ball"], False)
        # a process inside a lane nested under the integration tree activates only the deepest checkout
        managed = self.add_worktree(self.parent, self.parent / ".claude" / "worktrees" / "x-1a2b3c", "claude/x-1a2b3c")
        rep = self.report(lsof_cwds=lambda: [(9, "claude", str(managed))])
        self.assertEqual(self.one(rep, managed)["safety"], "ACTIVE")
        self.assertNotEqual(self.one(rep, self.parent)["safety"], "ACTIVE")
        # CLAUDE_PROJECT_DIR and the doctor's own cwd count even when lsof sees nothing
        env = dict(self.env, CLAUDE_PROJECT_DIR=str(lane))
        self.assertEqual(self.one(self.report(env=env), lane)["safety"], "ACTIVE")
        rep = self.report(cwd=str(lane / "src"))
        co = self.one(rep, lane)
        self.assertEqual(co["safety"], "ACTIVE")
        self.assertIn("active: is the current working directory", co["safety_reasons"])

    def test_active_checkout_still_lists_its_other_reasons(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-busydirty", "claude/busydirty")
        (lane / "x.txt").write_text("x")
        co = self.one(self.report(lsof_cwds=lambda: [(1, "vim", str(lane))]), lane)
        self.assertEqual(co["safety"], "ACTIVE")
        self.assertTrue(any(r.startswith("dirty") for r in co["safety_reasons"]))
        self.assertIs(co["dropped_ball"], False)

    def test_lsof_failure_makes_clean_checkouts_unknown_but_not_dirty_ones(self) -> None:
        clean = self.lane(self.parent, "dealdex-claude-lc", "claude/lc")
        dirty = self.lane(self.parent, "dealdex-claude-ld", "claude/ld")
        (dirty / "x.txt").write_text("x")
        rep = self.report(lsof_cwds=lambda: None)
        self.assertEqual(rep["lsof"], "unavailable")
        c, d = self.one(rep, clean), self.one(rep, dirty)
        self.assertEqual(c["safety"], "UNKNOWN")
        self.assertIn("process check unavailable (lsof failed)", c["safety_reasons"])
        self.assertIsNone(c["active"])
        self.assertIsNone(c["cwd_procs"])
        self.assertEqual(d["safety"], "NEEDS-REVIEW")

    def test_parent_repo_with_registered_worktrees_is_never_safe(self) -> None:
        self.lane(self.parent, "dealdex-claude-child", "claude/child")
        co = self.one(self.report(), self.parent)
        self.assertEqual(co["registered_worktrees"], 1)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertIn("parent of 1 registered linked worktree", co["safety_reasons"])
        self.assertEqual(co["location_class"], "INTEGRATION_TREE")
        self.assertEqual(co["creating_tool"], "human")

    def test_a_parent_does_not_double_count_its_lanes_unpushed_commits(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-wip", "claude/wip", pushed=False)
        self.commit(lane)
        rep = self.report()
        parent = self.one(rep, self.parent)
        self.assertIn("other local branches hold 1 unpushed commit", parent["safety_reasons"])
        self.assertIs(parent["dropped_ball"], False)
        self.assertIs(self.one(rep, lane)["dropped_ball"], True)
        self.assertEqual(rep["summary"]["dropped_ball"], 1)

    def test_a_lone_clean_integration_tree_on_main_is_safe(self) -> None:
        other = self.make_repo(self.home / "Code" / "Socratic-Trade", remote=f"https://github.com/{OWNER}/Socratic-Trade.git")
        co = self.one(self.report(), other)
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE")
        self.assertEqual(co["pr_state"], "N/A")

    def test_main_ahead_of_origin_is_a_dropped_ball(self) -> None:
        self.commit(self.parent)
        co = self.one(self.report(), self.parent)
        self.assertEqual(co["unpushed"], 1)
        self.assertIs(co["dropped_ball"], True)

    def test_full_clone_other_branches_and_stash(self) -> None:
        clone = self.make_repo(self.home / "apps" / "dealdex-claude-clone")
        self.git(clone, "checkout", "-q", "-b", "side")
        self.commit(clone, "side.txt")
        self.git(clone, "checkout", "-q", "main")
        (clone / "README.md").write_text("stash me\n")
        self.git(clone, "stash", "-q")
        co = self.one(self.report(), clone)
        self.assertEqual(co["kind"], "FULL-CLONE")
        self.assertEqual(co["unpushed_all_branches"], 1)
        self.assertTrue(co["has_stash"])
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertIn("other local branches hold 1 unpushed commit", co["safety_reasons"])
        self.assertIn("stash entries exist", co["safety_reasons"])
        self.assertIs(co["dropped_ball"], True)  # commits on a side branch count as unpushed work

    def test_no_remote_and_local_path_origin(self) -> None:
        norem = self.make_repo(self.home / "apps" / "dealdex-claude-norem", remote=None)
        co = self.one(self.report(), norem)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertIn("no remote configured (origin)", co["safety_reasons"])
        local = self.make_repo(self.home / "apps" / "dealdex-claude-local", remote=str(self.parent))
        co = self.one(self.report(), local)
        self.assertTrue(any(r.startswith("origin is a local path") for r in co["safety_reasons"]))
        self.assertEqual(co["owner_repo"], DEALDEX_REPO)  # followed to the clone's own GitHub origin

    def test_detached_head_unreferenced_vs_referenced(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-det", "claude/det")
        self.git(lane, "checkout", "-q", "--detach")
        co = self.one(self.report(), lane)
        self.assertIsNone(co["branch"])
        self.assertEqual(co["detached_sha"], self.git(lane, "rev-parse", "HEAD")[:9])
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE", co["safety_reasons"])
        self.commit(lane, "orphan.txt")
        co = self.one(self.report(), lane)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertTrue(any("only copy" in r for r in co["safety_reasons"]))

    def test_locked_worktree_needs_review(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-locked", "claude/locked")
        self.git(self.parent, "worktree", "lock", str(lane))
        co = self.one(self.report(), lane)
        self.assertTrue(co["locked"])
        self.assertIn("worktree is locked", co["safety_reasons"])

    def test_janitor_keep_marker_is_flagged_and_does_not_count_as_ignored_data(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-keep", "claude/keep")
        control = self.one(self.report(), lane)
        self.assertFalse(control["janitor_keep"])
        self.assertEqual(control["safety"], "SAFE-TO-REMOVE")
        (lane / ".janitor-keep").write_text("")
        co = self.one(self.report(), lane)
        self.assertTrue(co["janitor_keep"])
        self.assertEqual(co["dirty_untracked"], 1)  # the marker is an untracked file like any other
        self.assertIn(KEEP_REASON, co["safety_reasons"])
        # The real machine hides the marker (global ignore), so the tree is clean and only the
        # marker itself says the owner wants the lane kept.
        with open(self.parent / ".git" / "info" / "exclude", "a", encoding="utf-8") as fh:
            fh.write(".janitor-keep\n")
        co = self.one(self.report(), lane)
        self.assertEqual((co["dirty_tracked"], co["dirty_untracked"]), (0, 0))
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertEqual(co["safety_reasons"], [KEEP_REASON])
        self.assertEqual(co["ignored_local"], [])
        self.assertIs(co["dropped_ball"], False)

    def test_unborn_repo_is_clean_and_has_no_commits(self) -> None:
        path = self.home / "apps" / "dealdex-claude-unborn"
        path.mkdir(parents=True)
        self.git(path, "init", "-q", "-b", "main")
        co = self.one(self.report(), path)
        self.assertIsNone(co["head_sha"])
        self.assertEqual(co["unpushed"], 0)

    def test_sizes_only_with_the_flag(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-size", "claude/size")
        self.assertIsNone(self.one(self.report(), lane)["size_mb"])
        self.assertIsInstance(self.one(self.report(sizes=True), lane)["size_mb"], float)

    def test_ages_use_the_injected_clock(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-age", "claude/age")
        co = self.one(self.report(clock=self.aged(100)), lane)
        self.assertAlmostEqual(co["lane_age_days"], 100, delta=0.5)
        self.assertAlmostEqual(co["idle_days"], 100, delta=0.5)
        self.assertIsNotNone(co["last_commit"])
        co_now = self.one(self.report(clock=fixed_clock), lane)
        self.assertEqual(co_now["lane_age_days"], 0.0)  # the fixture was created "after" the fixed clock


# --------------------------------------------------------------------------- discovery and anomalies

class DiscoveryTests(GitCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = self.integration()

    def test_registered_worktrees_outside_every_scan_root_are_found(self) -> None:
        elsewhere = self.add_worktree(self.parent, self.home / "elsewhere" / "deep" / "er" / "wt", "claude/elsewhere")
        co = self.one(self.report(), elsewhere)
        self.assertTrue(any(s.startswith("worktree-list:") for s in co["found_by"]), co["found_by"])
        self.assertEqual(co["location_class"], "UNSANCTIONED")

    def test_the_parent_of_a_discovered_worktree_is_found_through_its_git_file(self) -> None:
        hidden_parent = self.make_repo(self.base / "elsewhere" / "parent")
        lane = self.add_worktree(hidden_parent, self.home / "apps" / "dealdex-claude-viaparent", "claude/viaparent")
        rep = self.report()
        self.assertEqual(self.one(rep, lane)["parent_repo"], os.path.realpath(hidden_parent))
        co = self.one(rep, hidden_parent)
        self.assertEqual(co["registered_worktrees"], 1)

    def test_orphaned_muse_and_claude_worktrees_are_found_by_the_folder_scan(self) -> None:
        # Git no longer lists either worktree (admin dir removed), so only the folder scan can find them.
        muse = self.add_worktree(self.parent, self.parent / ".muse" / "worktrees" / "x", "muse-code/x")
        claude = self.add_worktree(self.parent, self.parent / ".claude" / "worktrees" / "y", "claude/y")
        live = self.add_worktree(self.parent, self.parent / ".muse" / "worktrees" / "z2", "muse-code/z2")
        for name in ("x", "y"):
            shutil.rmtree(self.parent / ".git" / "worktrees" / name)
        rep = self.report()
        for path, tool in ((muse, "muse-code"), (claude, "claude-cli")):
            with self.subTest(path=path.name):
                co = self.one(rep, path)
                self.assertEqual(co["kind"], "ORPHAN")
                self.assertEqual(co["location_class"], "MANAGED")
                self.assertEqual(co["creating_tool"], tool)
        self.assertEqual(self.one(rep, live)["kind"], "LINKED-WORKTREE")

    def test_symlinked_integration_tree_is_one_checkout_and_listed_once(self) -> None:
        wt = self.add_worktree(self.parent, self.parent / ".claude" / "worktrees" / "foo-1a2b3c", "claude/foo-1a2b3c")
        (self.home / "Code" / "Deal.Dex").symlink_to(self.parent)
        listed: list[str] = []

        def recording(argv, cwd=None, timeout=None):
            if list(argv[1:3]) == ["worktree", "list"]:
                listed.append(os.path.realpath(cwd))
            return doctor.run_cmd(argv, cwd, timeout)
        rep = self.report(run=recording)
        paths = [c["realpath"] for c in rep["checkouts"]]
        self.assertEqual(len(paths), len(set(paths)))
        self.assertEqual(listed.count(os.path.realpath(self.parent)), 1)
        self.assertIn(os.path.realpath(wt), paths)
        self.assertEqual(self.one(rep, wt)["location_class"], "MANAGED")
        self.assertEqual(self.one(rep, wt)["creating_tool"], "claude-desktop")

    def test_forbidden_tmp_is_detected_with_a_fake_tmp_root(self) -> None:
        tmp_clone = self.add_worktree(self.parent, self.faketmp / "bf-x" / "wt", "claude/tmpwork")
        rep = self.report()
        co = self.one(rep, tmp_clone)
        self.assertEqual(co["location_class"], "FORBIDDEN_TMP")
        self.assertEqual(rep["summary"]["forbidden_tmp"], 1)
        self.assertIn("FORBIDDEN_TMP", {a["type"] for a in rep["anomalies"]})
        self.assertEqual(rep["anomalies"][0]["type"], "FORBIDDEN_TMP")  # most urgent first
        self.assertEqual(rep["summary"]["strict_violations"], 1)
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE")  # clean and pushed, but still forbidden

    def test_the_claude_scratchpad_is_still_a_tmp_checkout_for_the_doctor(self) -> None:
        scratch = self.make_repo(self.faketmp / "claude-501" / "scratch", remote=None)
        self.assertEqual(self.one(self.report(), scratch)["location_class"], "FORBIDDEN_TMP")

    def test_tmp_scan_depth_limit(self) -> None:
        shallow = self.make_repo(self.faketmp / "a" / "b" / "c" / "d", remote=None)
        deep = self.make_repo(self.faketmp / "a" / "b" / "c" / "d2" / "e", remote=None)
        paths = self.by_path(self.report())
        self.assertIn(os.path.realpath(shallow), paths)
        self.assertNotIn(os.path.realpath(deep), paths)

    def test_code_toplevel_unsanctioned_and_third_party(self) -> None:
        stray = self.make_repo(self.home / "Code" / "random-clone")
        self.make_repo(self.home / ".oh-my-zsh", remote="https://github.com/ohmyzsh/ohmyzsh.git")
        self.make_repo(self.home / "fleet-stray", remote=DEALDEX_URL)
        cache = self.make_repo(self.home / ".grok" / "installed-plugins" / "x-1234", remote="https://github.com/vendor/plugin.git")
        rep = self.report()
        self.assertEqual(self.one(rep, stray)["location_class"], "FORBIDDEN_CODE_TOPLEVEL")
        cache_co = self.one(rep, cache)
        self.assertTrue(cache_co["tool_cache"])
        self.assertEqual(cache_co["creating_tool"], "grok")
        anomalies = {(a["type"], os.path.basename(a["path"])) for a in rep["anomalies"]}
        self.assertIn(("FORBIDDEN_CODE_TOPLEVEL", "random-clone"), anomalies)
        self.assertIn(("UNSANCTIONED", "fleet-stray"), anomalies)
        self.assertNotIn(("UNSANCTIONED", ".oh-my-zsh"), anomalies)
        self.assertNotIn(("UNSANCTIONED", "x-1234"), anomalies)
        self.assertEqual(rep["summary"]["strict_violations"], 2)
        self.assertEqual(rep["summary"]["tool_cache"], 1)

    def test_naming_anomalies_and_full_clone_in_lane(self) -> None:
        conforming = self.lane(self.parent, "dealdex-claude-ok", "claude/ok")
        alias = self.lane(self.parent, "dealdex-ag-ok", "ag/ok")
        drift = self.lane(self.parent, "afl-claude-oldname", "claude/oldname")
        nonlane = self.lane(self.parent, "random-thing", "claude/random")
        clone = self.make_repo(self.home / "apps" / "dealdex-claude-clone")
        rep = self.report()
        self.assertEqual(self.one(rep, conforming)["name_verdict"], "CONFORMING")
        self.assertEqual(self.one(rep, alias)["name_verdict"], "ALIAS-ONLY")
        self.assertEqual(self.one(rep, drift)["name_verdict"], "NAME-DRIFT")
        self.assertEqual(self.one(rep, nonlane)["name_verdict"], "NAME-DRIFT")  # branch names a seat
        types = {(a["type"], os.path.basename(a["path"])) for a in rep["anomalies"]}
        self.assertIn(("NAME-DRIFT", "afl-claude-oldname"), types)
        self.assertIn(("FULL-CLONE-IN-LANE", "dealdex-claude-clone"), types)
        self.assertNotIn(("NAME-DRIFT", "dealdex-ag-ok"), types)
        self.assertNotIn("NON-LANE", {t for t, _ in types if _ == "dealdex-claude-ok"})
        self.assertIsNone(self.one(rep, self.parent)["name_verdict"])  # not a lane location
        self.assertIn(clone.name, {n for _, n in types})

    def test_nested_layout_lane(self) -> None:
        nested = self.add_worktree(self.parent, self.home / "apps" / "lanes" / "dealdex" / "claude-fix", "claude/fix")
        co = self.one(self.report(), nested)
        self.assertEqual((co["location_class"], co["name_verdict"]), ("LANE_NESTED", "CONFORMING"))
        flat = self.report(env=dict(self.env, FLEET_LAYOUT="flat"))
        self.assertEqual(self.one(flat, nested)["location_class"], "UNSANCTIONED")

    def test_orphan_gitfile_with_missing_gitdir(self) -> None:
        orphan = self.home / "apps" / "dealdex-claude-orphan"
        orphan.mkdir(parents=True)
        (orphan / ".git").write_text(f"gitdir: {self.parent}/.git/worktrees/orphan\n")
        far = self.home / "apps" / "dealdex-claude-far"
        far.mkdir()
        (far / ".git").write_text("gitdir: /nonexistent/place/.git/worktrees/far\n")
        rep = self.report()
        for lane in (orphan, far):
            co = self.one(rep, lane)
            self.assertEqual(co["kind"], "ORPHAN")
            self.assertEqual(co["safety"], "UNKNOWN")
            self.assertTrue(any(r.startswith("orphan") for r in co["safety_reasons"]))
        self.assertEqual(self.one(rep, orphan)["parent_repo"], str(self.parent))
        self.assertEqual(self.one(rep, far)["parent_repo"], "/nonexistent/place")
        self.assertEqual({a["type"] for a in rep["anomalies"] if a["type"] == "ORPHAN"}, {"ORPHAN"})
        self.assertEqual(sum(1 for a in rep["anomalies"] if a["type"] == "ORPHAN"), 2)

    def test_prunable_worktrees_are_anomalies_not_checkouts(self) -> None:
        gone = self.lane(self.parent, "dealdex-claude-gone", "claude/gone")
        shutil.rmtree(gone)
        ghost_admin = self.parent / ".git" / "worktrees" / "ghost"
        ghost_admin.mkdir()
        (ghost_admin / "HEAD").write_text("ref: refs/heads/claude/ghost\n")
        (ghost_admin / "commondir").write_text("../..\n")
        ghost_path = self.home / "apps" / "dealdex-claude-ghost"
        (ghost_admin / "gitdir").write_text(f"{ghost_path}/.git\n")
        rep = self.report()
        prunable = {a["path"] for a in rep["anomalies"] if a["type"] == "PRUNABLE"}
        self.assertIn(str(gone), prunable)
        self.assertIn(str(ghost_path), prunable)
        self.assertNotIn(os.path.realpath(gone), self.by_path(rep))
        self.assertNotIn(os.path.realpath(ghost_path), self.by_path(rep))
        self.assertEqual(self.one(rep, self.parent)["registered_worktrees"], 0)
        self.assertEqual(self.one(rep, self.parent)["safety"], "SAFE-TO-REMOVE")

    def test_a_moved_worktree_is_unregistered_and_its_old_path_prunable(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-moved", "claude/moved")
        moved = self.home / "apps" / "dealdex-claude-moved2"
        lane.rename(moved)
        rep = self.report()
        co = self.one(rep, moved)
        self.assertFalse(co["registered"])
        self.assertIn("not in the parent repo's worktree list (moved or stale)", co["safety_reasons"])
        self.assertIn(str(lane), {a["path"] for a in rep["anomalies"] if a["type"] == "PRUNABLE"})

    def test_layout_v2_statuses_in_the_report(self) -> None:
        """Layout v2 (owner 2026-10-09): every checkout says whether its place is right.  The location
        classes, the strict count and the cleaner contract do not change."""
        apps = self.home / "apps"
        lanes = apps / "lanes"
        correct = self.add_worktree(self.parent, lanes / "DealDex" / "claude-fix", "claude/fix")
        review = lanes / "DealDex" / "review-pr-9"
        review.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.parent, "worktree", "add", "-q", "--detach", str(review))
        desktop = self.add_worktree(self.parent, lanes / "DealDex" / "fix-it-a1b2c3", "claude/fix-it-a1b2c3")
        codex_new = self.add_worktree(self.parent, lanes / "_codex" / "ab12" / "DealDex", "codex/ab12")
        old_flat = self.lane(self.parent, "dealdex-claude-old", "claude/old")
        old_managed = self.add_worktree(self.parent, lanes / "_managed" / "codex" / "DealDex", "codex/managed")
        codex_old = self.add_worktree(self.parent, self.home / ".codex" / "worktrees" / "cd34" / "DealDex", "codex/cd34")
        repo_local = self.add_worktree(self.parent, self.parent / ".claude" / "worktrees" / "y-1a2b3c", "claude/y-1a2b3c")
        tmp_clone = self.add_worktree(self.parent, self.faketmp / "bf-x" / "wt", "claude/tmpwork")
        rep = self.report()
        want = {
            correct: ("correct", "LANE_NESTED"), review: ("correct", "REVIEW"), desktop: ("correct", "MANAGED"),
            codex_new: ("codex-managed", "MANAGED"), old_flat: ("legacy-migrate", "LANE_FLAT_LEGACY"),
            old_managed: ("legacy", "MANAGED"), codex_old: ("legacy-migrate", "MANAGED"),
            repo_local: ("wrong", "MANAGED"), tmp_clone: ("wrong", "FORBIDDEN_TMP"), self.parent: ("human", "INTEGRATION_TREE"),
        }
        for path, (status, cls) in want.items():
            with self.subTest(path=os.path.relpath(path, self.home)):
                co = self.one(rep, path)
                self.assertEqual((co["layout_status"], co["location_class"]), (status, cls), co["layout_reasons"])
        # a legacy checkout names its new home, a correct one does not
        self.assertEqual(self.one(rep, old_flat)["layout_target"], os.path.join(os.path.realpath(lanes), "DealDex", "claude-old"))
        self.assertEqual(self.one(rep, codex_old)["layout_target"],
                         os.path.join(os.path.realpath(lanes), "_codex", "cd34", "DealDex"))
        self.assertIsNone(self.one(rep, correct)["layout_target"])
        self.assertIsNone(self.one(rep, old_managed)["layout_target"])
        # tools are told from the path alone
        self.assertEqual(self.one(rep, desktop)["creating_tool"], "claude-desktop")
        self.assertEqual(self.one(rep, codex_new)["creating_tool"], "codex")
        self.assertIn("lanes/_codex", self.one(rep, codex_new)["creating_tool_basis"])
        # a name verdict is judged only for lanes: a review, a desktop folder and Codex's are not lanes
        for path in (review, desktop, codex_new):
            self.assertIsNone(self.one(rep, path)["name_verdict"])
        # the only new anomaly is WRONG-PLACE, and only for the repo-local harness worktree
        wrong = [a for a in rep["anomalies"] if a["type"] == "WRONG-PLACE"]
        self.assertEqual([os.path.realpath(a["path"]) for a in wrong], [os.path.realpath(repo_local)])
        self.assertIn("inside-~/Code/<Repo>", wrong[0]["detail"])
        self.assertEqual(rep["anomalies"][0]["type"], "FORBIDDEN_TMP", "the order of anomalies is still most urgent first")
        # strict counts the temp checkout only: a worktree in the wrong place is not a new exit code
        self.assertEqual(rep["summary"]["strict_violations"], 1)
        by = rep["summary"]["by_layout_status"]
        self.assertEqual(by, {"correct": 3, "legacy-migrate": 2, "legacy": 1, "codex-managed": 1, "human": 1, "wrong": 2})
        text = doctor.render_text(rep)
        self.assertIn("Layout (v2)", text)
        self.assertIn("legacy (migrate)", text)
        self.assertIn("Codex-managed", text)
        self.assertIn("WRONG-PLACE", text)

    def test_legacy_prefix_folders_keep_their_lane_name_verdict(self) -> None:
        """~67 lanes sit in old prefix folders today.  They must read as legacy, not as name drift."""
        old = self.add_worktree(self.parent, self.home / "apps" / "lanes" / "dealdex" / "claude-old", "claude/old")
        rep = self.report()
        co = self.one(rep, old)
        self.assertEqual((co["location_class"], co["name_verdict"]), ("LANE_NESTED", "CONFORMING"))
        self.assertEqual(co["layout_status"], "legacy-migrate")
        self.assertTrue(any(r.startswith("legacy-prefix-dir:") or r.startswith("repo-dir-case:")
                            for r in co["layout_reasons"]), co["layout_reasons"])
        self.assertNotIn(("NAME-DRIFT", "claude-old"), {(a["type"], os.path.basename(a["path"])) for a in rep["anomalies"]})

    def test_an_unknown_repo_folder_under_lanes_is_wrong_place_and_name_drift(self) -> None:
        stray = self.add_worktree(self.parent, self.home / "apps" / "lanes" / "nonesuch" / "claude-x", "claude/x")
        rep = self.report()
        co = self.one(rep, stray)
        self.assertEqual((co["layout_status"], co["location_class"]), ("wrong", "LANE_NESTED"))
        types = {a["type"] for a in rep["anomalies"] if os.path.basename(a["path"]) == "claude-x"}
        self.assertEqual(types, {"WRONG-PLACE", "NAME-DRIFT"})

    def test_json_keeps_schema_2_and_the_old_fields(self) -> None:
        rep = self.report()
        self.assertEqual(rep["schema"], 2)
        co = self.one(rep, self.parent)
        for key in ("location_class", "name_verdict", "safety", "creating_tool", "lane_root", "layout_status",
                    "layout_reasons", "layout_target"):
            self.assertIn(key, co)
        self.assertIn("by_layout_status", rep["summary"])

    def test_creating_tool_inference(self) -> None:
        roots = L.make_roots(self.home, self.env, registry=self.registry, case_insensitive=False)
        home = self.home
        cases = [
            (home / ".codex" / "worktrees" / "ab12" / "DealDex", None, None, None, "codex"),
            (home / ".cursor" / "worktrees" / "x", None, None, None, "cursor"),
            (home / ".grok" / "worktrees" / "r" / "worktree-1", None, None, None, "grok"),
            (home / ".fx" / "x", None, None, None, "fx"),
            (home / ".gemini" / "antigravity" / "scratch" / "x", None, None, None, "antigravity"),
            (home / ".botfleet" / "workspaces" / "u" / "x", None, None, None, "botfleet"),
            (home / ".buzz" / "REPOS" / "congress-trade", None, None, None, "buzz"),
            (home / "Code" / "DealDex" / ".claude" / "worktrees" / "fix-bug-a1b2c3", "claude/fix-bug-a1b2c3", None, None, "claude-desktop"),
            (home / "Code" / "DealDex" / ".claude" / "worktrees" / "agent-a1b2c3d4e5f6a7b8", None, None, None, "claude-cli"),
            (home / "Code" / "DealDex" / ".claude" / "worktrees" / "myname", "worktree-myname", None, None, "claude-cli"),
            (home / "Code" / "DealDex" / ".muse" / "worktrees" / "fix-login", None, None, None, "muse-code"),
            (home / "Code" / "DealDex" / ".muse" / "worktrees" / "fix-login", "claude/x", None, "claude", "muse-code"),
            (home / "Code" / "DealDex", "main", None, None, "human"),
            # layout v2: Codex nests lanes/_codex/<slug>/<Repo>; the desktop app files lanes/<Repo>/<slug>-<hex>
            (home / "apps" / "lanes" / "_codex" / "ab12" / "DealDex", None, None, None, "codex"),
            (home / "apps" / "lanes" / "_conductor" / "DealDex" / "lagos", None, None, None, "conductor"),
            (home / "conductor" / "workspaces" / "DealDex" / "oslo", "claude/x", None, "claude", "conductor"),
            (home / "apps" / "lanes" / "DealDex" / "fix-bug-a1b2c3", "claude/fix-bug-a1b2c3", None, None, "claude-desktop"),
            (home / "apps" / "lanes" / "DealDex" / "fix-bug-a1b2c3" / "src", None, None, None, "claude-desktop"),
            (home / "apps" / "lanes" / "DealDex" / "claude-fix-a1b2c3", None, "claude", None, "claude-cli"),
            (home / "apps" / "lanes" / "DealDex" / "minimax-x", None, "minimax", None, "minimax"),
            (home / "apps" / "lanes" / "DealDex" / "review-pr-7", None, None, None, "unknown"),
            (home / "apps" / "dealdex-codex-x", None, "codex", None, "codex"),
            (home / "apps" / "dealdex-monet-x", None, "monet", None, "claude-cli"),
            (home / "apps" / "dealdex-mm-x", None, "minimax", None, "minimax"),
            (home / "somewhere" / "z", "minimax/thing", None, "minimax", "minimax"),
            (home / "apps" / "botfleet-kody", None, "BF-KODY", None, "botfleet"),
            (home / "somewhere" / "x", "grok/thing", None, "grok", "grok"),
            (home / "somewhere" / "y", None, None, None, "unknown"),
        ]
        for path, branch, seat, bseat, want in cases:
            with self.subTest(path=str(path.relative_to(home))):
                tool, basis = doctor.infer_tool(str(path), roots, branch, seat, bseat)
                self.assertEqual(tool, want)
                self.assertTrue(basis)
        # the path evidence for a Muse worktree is named as such
        self.assertEqual(
            doctor.infer_tool(str(home / "Code" / "DealDex" / ".muse" / "worktrees" / "x"), roots, None, None, None),
            ("muse-code", "path:.muse/worktrees"))
        # a folder that merely contains the word is not a Muse worktree
        self.assertEqual(
            doctor.infer_tool(str(home / "Code" / "DealDex" / ".muse" / "worktrees-old" / "x"), roots, None, None, None)[0],
            "human")


class ToleranceTests(GitCase):
    def test_a_timeout_an_exception_and_a_corrupt_repo_do_not_stop_the_sweep(self) -> None:
        parent = self.integration()
        timeout_lane = self.lane(parent, "dealdex-claude-slow", "claude/slow")
        raising_lane = self.lane(parent, "dealdex-claude-raises", "claude/raises")
        good_lane = self.lane(parent, "dealdex-claude-good", "claude/good")
        corrupt = self.make_repo(self.home / "apps" / "dealdex-claude-corrupt")
        (corrupt / ".git" / "HEAD").write_text("garbage\n")

        def flaky(argv, cwd=None, timeout=None):
            if cwd and os.path.realpath(cwd) == os.path.realpath(timeout_lane) and argv[1] == "status":
                return doctor.CmdResult(None, timed_out=True)
            if cwd and os.path.realpath(cwd) == os.path.realpath(raising_lane) and argv[1] == "status":
                raise OSError("device not configured")
            return doctor.run_cmd(argv, cwd, timeout)
        rep = self.report(run=flaky)
        slow, raising, good, bad = (self.one(rep, p) for p in (timeout_lane, raising_lane, good_lane, corrupt))
        self.assertEqual(slow["safety"], "UNKNOWN")
        self.assertIn("unreadable: status: timed out", slow["safety_reasons"])
        self.assertEqual(raising["safety"], "UNKNOWN")
        self.assertTrue(any("OSError" in r for r in raising["safety_reasons"]))
        self.assertEqual(good["safety"], "SAFE-TO-REMOVE")
        self.assertEqual(bad["safety"], "UNKNOWN")
        self.assertTrue(bad["read_errors"])

    def test_a_refusal_inside_the_runner_is_a_bug_and_propagates(self) -> None:
        self.integration()

        def refusing(argv, cwd=None, timeout=None):
            raise doctor.CommandRefused("nope")
        with self.assertRaises(doctor.CommandRefused):
            self.report(run=refusing)

    def test_unreadable_registry_degrades_with_a_warning(self) -> None:
        self.integration()
        env = dict(self.env, FLEET_APPS_JSON=str(self.base / "missing.json"))
        rep = doctor.build_report(self.home, env, tmp_scan_roots=[self.faketmp], case_insensitive=False,
                                  lsof_cwds=lambda: [], gh_prs=FakeGh(), clock=fixed_clock)
        self.assertTrue(any("registry unreadable" in w for w in rep["warnings"]))
        self.assertEqual(len(rep["checkouts"]), 1)


class CredentialTests(GitCase):
    def test_credentials_in_remote_urls_never_reach_the_report(self) -> None:
        lane = self.make_repo(self.home / "apps" / "dealdex-claude-cred",
                              remote=f"https://octocat:tok3n-secret@github.com/{OWNER}/DealDex.git")
        rep = self.report()
        co = self.one(rep, lane)
        self.assertEqual(co["remote_url"], f"https://REDACTED@github.com/{OWNER}/DealDex.git")
        self.assertEqual(co["owner_repo"], DEALDEX_REPO)
        blob = json.dumps(rep) + doctor.render_text(rep)
        self.assertNotIn("tok3n-secret", blob)
        self.assertNotIn("octocat", blob)

    def test_the_forms_that_used_to_leak_are_redacted_end_to_end(self) -> None:
        forms = {
            "slash": f"https://user:pa/ssSECRET@github.com/{OWNER}/DealDex.git",
            "at": f"https://user:p@ssSECRET@github.com/{OWNER}/DealDex.git",
            "scp-token": f"ghp_SCPSECRET@github.com:{OWNER}/DealDex.git",
        }
        for label, url in forms.items():
            with self.subTest(form=label):
                lane = self.make_repo(self.home / "apps" / f"dealdex-claude-cred-{label}", remote=url)
                rep = self.report()
                co = self.one(rep, lane)
                self.assertNotIn("SECRET", json.dumps(rep) + doctor.render_text(rep))
                self.assertIn("REDACTED@github.com", co["remote_url"])
                self.assertEqual(co["owner_repo"], DEALDEX_REPO)


# --------------------------------------------------------------------------- review findings

def failing_runner(match, result: "doctor.CmdResult | None" = None):
    """A runner that really runs every command except the ones `match(argv, real cwd)` selects,
    which return `result` (a timeout by default)."""
    result = result or doctor.CmdResult(None, timed_out=True)

    def run(argv, cwd=None, timeout=None):
        if match(list(argv), os.path.realpath(cwd) if cwd else None):
            return result
        return doctor.run_cmd(argv, cwd, timeout)
    return run


class IgnoredStateTests(GitCase):
    """`git status` without --ignored never lists ignored files, so a clean tree with an ignored
    SQLite database used to read SAFE-TO-REMOVE."""

    def setUp(self) -> None:
        super().setUp()
        self.parent = self.integration()

    def ignoring(self, dirname: str, branch: str, patterns: str):
        lane = self.lane(self.parent, dirname, branch)
        self.commit(lane, ".gitignore", patterns)
        self.push(lane, branch)
        return lane

    def test_split_ignored_separates_local_state_from_regenerable_output(self) -> None:
        text = "\n".join([
            "!! data/app.db", "!! data/app.db-wal", "!! secret.env", "!! data/corpus/", "!! .env",
            "!! node_modules/", "!! packages/web/node_modules/", "!! .DS_Store", "!! src/.DS_Store",
            "!! build/", "!! dist/", "!! .next/", "!! .build/", "!! app.tsbuildinfo", "!! pkg/__pycache__/",
            "!! mod.pyc", "!! DerivedData/", "!! .venv/", "!! .janitor-keep", "!! .pytest_cache/",
            "!! venv/", "!! dist-server/", "!! dist-native/bundle.js",
            " M tracked.txt", "?? new.txt", "",
        ])
        local, regenerable = doctor.split_ignored(text)
        self.assertEqual(local, ["data/app.db", "data/app.db-wal", "secret.env", "data/corpus/", ".env"])
        self.assertEqual(regenerable, 17)

    def test_split_ignored_unquotes_git_quoted_names(self) -> None:
        local, regenerable = doctor.split_ignored(
            '!! "my notes.env"\n!! "caf\\303\\251.db"\n!! "say \\"hi\\".db"\n!! "dir name/node_modules/"\n')
        self.assertEqual(local, ["my notes.env", "café.db", 'say "hi".db'])
        self.assertEqual(regenerable, 1)

    def test_count_status_still_leaves_ignored_lines_out_of_the_dirty_counts(self) -> None:
        self.assertEqual(doctor.count_status("!! a\n M b\n?? c\n!! d/\n"), (1, 1))

    def test_an_ignored_secret_makes_a_clean_lane_need_review(self) -> None:
        lane = self.ignoring("dealdex-claude-ignored", "claude/ignored", "*.env\nbuild/\n")
        (lane / "secret.env").write_text("API_KEY=abc\n")
        (lane / "build").mkdir()
        (lane / "build" / "o.bin").write_bytes(b"\x00")
        co = self.one(self.report(), lane)
        self.assertEqual((co["dirty_tracked"], co["dirty_untracked"]), (0, 0))
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertEqual(co["safety_reasons"], ["ignored local state: secret.env"])
        self.assertEqual((co["ignored_local"], co["ignored_local_count"], co["ignored_regenerable"]),
                         (["secret.env"], 1, 1))
        self.assertIs(co["dropped_ball"], False)  # ignored data is not an unpushed or dirty ball

    def test_a_live_sqlite_wal_is_listed_and_long_lists_are_summarised(self) -> None:
        lane = self.ignoring("dealdex-claude-wal", "claude/wal", "*.db*\n")
        (lane / "data").mkdir()
        self.commit(lane, "data/.gitkeep", "")  # a tracked file keeps git from folding the directory into one entry
        self.push(lane, "claude/wal")
        for name in ("app.db", "app.db-shm", "app.db-wal", "old.db", "older.db"):
            (lane / "data" / name).write_text("x")
        co = self.one(self.report(), lane)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertEqual(co["safety_reasons"],
                         ["ignored local state: data/app.db, data/app.db-shm, data/app.db-wal (+2 more)"])
        self.assertEqual(co["ignored_local_count"], 5)

    def test_the_stored_list_is_capped_but_the_count_is_not(self) -> None:
        lane = self.ignoring("dealdex-claude-many", "claude/many", "*.dat\n")
        for n in range(doctor.IGNORED_LIST_CAP + 7):
            (lane / f"f{n:03d}.dat").write_text("x")
        co = self.one(self.report(), lane)
        self.assertEqual(co["ignored_local_count"], doctor.IGNORED_LIST_CAP + 7)
        self.assertEqual(len(co["ignored_local"]), doctor.IGNORED_LIST_CAP)

    def test_regenerable_output_alone_stays_safe_and_says_so(self) -> None:
        lane = self.ignoring("dealdex-claude-regen", "claude/regen",
                             "node_modules/\n.DS_Store\n__pycache__/\n*.tsbuildinfo\ndist/\n")
        (lane / "node_modules").mkdir()
        (lane / "node_modules" / "pkg.js").write_text("x")
        (lane / ".DS_Store").write_text("x")
        (lane / "dist").mkdir()
        (lane / "dist" / "out.js").write_text("x")
        co = self.one(self.report(), lane)
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE", co["safety_reasons"])
        self.assertEqual((co["ignored_local_count"], co["ignored_regenerable"]), (0, 3))
        self.assertIn("ignored files are regenerable build output only (3)", co["safety_reasons"])

    def test_no_ignored_files_at_all_is_safe_without_the_extra_evidence(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-plain", "claude/plain")
        co = self.one(self.report(), lane)
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE")
        self.assertEqual((co["ignored_local_count"], co["ignored_regenerable"]), (0, 0))
        self.assertFalse([r for r in co["safety_reasons"] if "ignored" in r])

    def test_a_full_clone_with_ignored_data_needs_review_too(self) -> None:
        clone = self.make_repo(self.home / "apps" / "dealdex-claude-fullclone")
        self.commit(clone, ".gitignore", "data/\n")
        self.push(clone, "main")
        (clone / "data").mkdir()
        (clone / "data" / "app.db").write_text("x")
        co = self.one(self.report(), clone)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertEqual(co["ignored_local"], ["data/"])

    def test_a_failed_ignored_scan_keeps_the_dirty_facts(self) -> None:
        dirty = self.lane(self.parent, "dealdex-claude-igdirty", "claude/igdirty")
        (dirty / "x.txt").write_text("x")
        clean = self.lane(self.parent, "dealdex-claude-igclean", "claude/igclean")
        run = failing_runner(lambda argv, cwd: argv[1] == "status" and "--ignored" in argv)
        rep = self.report(run=run)
        d, c = self.one(rep, dirty), self.one(rep, clean)
        self.assertEqual(d["safety"], "NEEDS-REVIEW")
        self.assertEqual((d["dirty_tracked"], d["dirty_untracked"]), (0, 1))
        self.assertIn("dirty: 0 tracked-modified, 1 untracked", d["safety_reasons"])
        self.assertIn("unreadable: ignored: timed out", d["safety_reasons"])
        self.assertIsNone(d["ignored_local_count"])
        self.assertIs(d["dropped_ball"], True)
        self.assertEqual(c["safety"], "UNKNOWN")
        self.assertEqual(c["safety_reasons"], ["unreadable: ignored: timed out"])

    def test_status_with_ignored_is_still_read_only(self) -> None:
        lane = self.ignoring("dealdex-claude-ro", "claude/ro", "*.db\n")
        (lane / "a.db").write_text("x")
        os.utime(lane / "README.md", ns=(10**18, 10**18))
        before = snapshot([self.home])
        self.report()
        self.assertEqual(snapshot([self.home]), before)


class ProbeFailureTests(GitCase):
    """A probe that fails must make the checkout UNKNOWN.  It used to read as "nothing there"."""

    FAILURES = (doctor.CmdResult(None, timed_out=True), doctor.CmdResult(128, err="fatal: boom\n"),
                doctor.CmdResult(0, out="not a number\n"))

    def full_clone_with_side_branch(self, name: str):
        clone = self.make_repo(self.home / "apps" / name)
        self.git(clone, "checkout", "-q", "-b", "side")
        self.commit(clone, "side.txt")
        self.git(clone, "checkout", "-q", "main")
        return clone

    def test_a_failed_other_branch_count_is_unknown_not_safe(self) -> None:
        clone = self.full_clone_with_side_branch("dealdex-claude-oc-clone")
        control = self.one(self.report(), clone)
        self.assertEqual(control["safety"], "NEEDS-REVIEW")
        self.assertIn("other local branches hold 1 unpushed commit", control["safety_reasons"])
        for fail in self.FAILURES:
            with self.subTest(result=fail):
                run = failing_runner(lambda argv, cwd: argv[1:4] == ["rev-list", "--count", "--branches"], fail)
                co = self.one(self.report(run=run), clone)
                self.assertEqual(co["safety"], "UNKNOWN", co["safety_reasons"])
                self.assertIsNone(co["unpushed_all_branches"])
                self.assertTrue(any(e.startswith("unpushed-all-branches:") for e in co["read_errors"]))

    def test_a_failed_stash_probe_is_unknown_not_safe(self) -> None:
        clone = self.make_repo(self.home / "apps" / "dealdex-claude-stash-clone")
        (clone / "README.md").write_text("stash me\n")
        self.git(clone, "stash", "-q")
        control = self.one(self.report(), clone)
        self.assertIn("stash entries exist", control["safety_reasons"])
        for fail in self.FAILURES[:2]:
            with self.subTest(result=fail):
                run = failing_runner(lambda argv, cwd: argv[1] == "for-each-ref" and "refs/stash" in argv, fail)
                co = self.one(self.report(run=run), clone)
                self.assertEqual(co["safety"], "UNKNOWN", co["safety_reasons"])
                self.assertIsNone(co["has_stash"])
                self.assertTrue(any(e.startswith("stash:") for e in co["read_errors"]))

    def test_an_unborn_clone_still_reads_clean(self) -> None:
        path = self.home / "apps" / "dealdex-claude-unborn2"
        path.mkdir(parents=True)
        self.git(path, "init", "-q", "-b", "main")
        co = self.one(self.report(), path)
        self.assertEqual(co["read_errors"], [])
        self.assertEqual((co["unpushed_all_branches"], co["has_stash"]), (0, False))

    def test_a_failed_worktree_list_keeps_the_parent_out_of_safe(self) -> None:
        parent = self.integration()
        self.lane(parent, "dealdex-claude-wl-a", "claude/wl-a")
        self.lane(parent, "dealdex-claude-wl-b", "claude/wl-b")
        control = self.one(self.report(), parent)
        self.assertEqual((control["safety"], control["registered_worktrees"]), ("NEEDS-REVIEW", 2))
        run = failing_runner(lambda argv, cwd: argv[1:3] == ["worktree", "list"])
        rep = self.report(run=run)
        co = self.one(rep, parent)
        self.assertEqual(co["safety"], "NEEDS-REVIEW", co["safety_reasons"])
        self.assertEqual(co["registered_worktrees"], 2)  # counted from .git/worktrees, no git needed
        self.assertIn("parent of 2 registered linked worktrees", co["safety_reasons"])
        self.assertIn("worktree list: timed out", co["read_errors"])
        self.assertTrue(any("worktree list failed" in w for w in rep["warnings"]))

    def test_a_failed_worktree_list_on_a_lone_clone_is_unknown(self) -> None:
        lone = self.make_repo(self.home / "Code" / "Socratic-Trade", remote=f"https://github.com/{OWNER}/Socratic-Trade.git")
        control = self.one(self.report(), lone)
        self.assertEqual(control["safety"], "SAFE-TO-REMOVE")
        run = failing_runner(lambda argv, cwd: argv[1:3] == ["worktree", "list"])
        co = self.one(self.report(run=run), lone)
        self.assertEqual(co["safety"], "UNKNOWN")
        self.assertEqual(co["registered_worktrees"], 0)
        self.assertIn("unreadable: worktree list: timed out", co["safety_reasons"])


class NotARepositoryTests(GitCase):
    def test_a_git_directory_git_rejects_does_not_borrow_the_enclosing_repo(self) -> None:
        parent = self.integration()
        self.lane(parent, "dealdex-claude-livelane", "claude/livelane")
        gone = self.lane(parent, "dealdex-claude-gonelane", "claude/gonelane")
        shutil.rmtree(gone)  # a stale registration, so the parent's list holds a PRUNABLE entry
        broken = parent / ".claude" / "worktrees" / "broken"
        (broken / ".git").mkdir(parents=True)  # what an interrupted rm -rf leaves: .git exists but is empty
        (broken / "precious.txt").write_text("the only copy\n")
        rep = self.report()
        co = self.one(rep, broken)
        self.assertEqual(co["kind"], "FULL-CLONE")
        self.assertEqual(co["safety"], "UNKNOWN", co["safety_reasons"])
        self.assertIsNone(co["head_sha"])
        self.assertIsNone(co["branch"])
        self.assertIsNone(co["dirty_tracked"])
        self.assertTrue(any(f"git resolves this directory to {os.path.realpath(parent)}" in e
                            for e in co["read_errors"]), co["read_errors"])
        # git would answer `worktree list` for the enclosing repository, so the broken clone must not
        # be asked: it would inherit the parent's lanes and add a second copy of every anomaly.
        self.assertEqual(co["registered_worktrees"], 0)
        self.assertFalse([r for r in co["safety_reasons"] if "registered linked worktree" in r])
        prunable = [a for a in rep["anomalies"] if a["type"] == "PRUNABLE"]
        self.assertEqual(len(prunable), 1, prunable)
        self.assertNotIn(str(broken), prunable[0]["detail"])
        self.assertEqual(self.one(rep, parent)["registered_worktrees"], 1)

    def test_a_git_dir_other_than_the_one_the_checkout_points_to_is_unknown(self) -> None:
        parent = self.integration()
        lane = self.lane(parent, "dealdex-claude-gitdir", "claude/gitdir")
        elsewhere = f"{self.base}/elsewhere/.git"
        fake = doctor.CmdResult(0, out=f"{os.path.realpath(lane)}\n{elsewhere}\n")
        run = failing_runner(lambda argv, cwd: argv[1:4] == ["rev-parse", "--show-toplevel", "--git-dir"]
                             and cwd == os.path.realpath(lane), fake)
        co = self.one(self.report(run=run), lane)
        self.assertEqual(co["safety"], "UNKNOWN", co["safety_reasons"])
        self.assertTrue(any(e.startswith(f"git uses {elsewhere}") for e in co["read_errors"]), co["read_errors"])
        self.assertIsNone(co["head_sha"])

    def test_a_directory_git_cannot_read_at_all_is_unknown(self) -> None:
        stray = self.home / "apps" / "dealdex-claude-stray"
        (stray / ".git").mkdir(parents=True)
        co = self.one(self.report(), stray)
        self.assertEqual(co["safety"], "UNKNOWN")
        self.assertTrue(co["read_errors"])

    def test_a_checkout_reached_through_a_symlinked_path_is_not_a_mismatch(self) -> None:
        parent = self.integration()
        (self.home / "Code" / "Deal.Dex").symlink_to(parent)
        co = self.one(self.report(), parent)
        self.assertEqual(co["read_errors"], [])
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE")


class RemoteScopeTests(GitCase):
    """Only a remote that is not a local path proves a commit is on a server."""

    def setUp(self) -> None:
        super().setUp()
        self.parent = self.integration()

    def local_remote(self, repo, name: str) -> pathlib.Path:
        bare = self.base / f"{name}.git"
        subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
        self.git(repo, "remote", "add", name, str(bare))
        return bare

    def test_a_second_remote_that_is_a_local_path_does_not_prove_a_push(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-localremote", "claude/localremote")
        bare = self.local_remote(lane, "scratch")
        head = self.commit(lane)
        self.git(lane, "update-ref", "refs/remotes/scratch/claude/localremote", head)
        co = self.one(self.report(), lane)
        self.assertEqual(co["unpushed"], 1)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertIn("unpushed commits: 1", co["safety_reasons"])
        self.assertIn(f"remote 'scratch' is a local path ({bare}); its tracking refs do not count as pushed",
                      co["safety_reasons"])
        self.assertIs(co["dropped_ball"], True)

    def test_a_peer_lane_remote_is_a_local_path_too(self) -> None:
        peer = self.lane(self.parent, "dealdex-claude-peer", "claude/peer")
        lane = self.lane(self.parent, "dealdex-claude-viapeer", "claude/viapeer")
        self.git(lane, "remote", "add", "peer", str(peer))
        head = self.commit(lane)
        self.git(lane, "update-ref", "refs/remotes/peer/claude/viapeer", head)
        co = self.one(self.report(), lane)
        self.assertEqual((co["unpushed"], co["safety"]), (1, "NEEDS-REVIEW"))
        self.assertTrue(any(r.startswith("remote 'peer' is a local path") for r in co["safety_reasons"]))

    def test_a_second_remote_on_a_server_still_proves_a_push(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-serverremote", "claude/serverremote")
        self.git(lane, "remote", "add", "upstream", f"git@github.com:{OWNER}/DealDex.git")
        head = self.commit(lane)
        self.git(lane, "update-ref", "refs/remotes/upstream/claude/serverremote", head)
        co = self.one(self.report(), lane)
        self.assertEqual((co["unpushed"], co["safety"]), (0, "SAFE-TO-REMOVE"), co["safety_reasons"])

    def test_a_full_clone_counts_other_branches_against_the_same_remotes(self) -> None:
        clone = self.make_repo(self.home / "apps" / "dealdex-claude-scopeclone")
        self.local_remote(clone, "scratch")
        self.git(clone, "checkout", "-q", "-b", "side")
        head = self.commit(clone, "side.txt")
        self.git(clone, "update-ref", "refs/remotes/scratch/side", head)
        self.git(clone, "checkout", "-q", "main")
        co = self.one(self.report(), clone)
        self.assertEqual(co["unpushed_all_branches"], 1)
        self.assertIn("other local branches hold 1 unpushed commit", co["safety_reasons"])

    def test_an_unreadable_remote_url_is_treated_as_not_proving_a_push(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-nourl", "claude/nourl")
        head = self.commit(lane)
        self.git(lane, "update-ref", "refs/remotes/ghost/claude/nourl", head)  # tracking ref, no remote.ghost.url
        co = self.one(self.report(), lane)
        self.assertEqual(co["unpushed"], 1)
        self.assertTrue(any(r.startswith("remote 'ghost' has no readable url") for r in co["safety_reasons"]))

    def test_a_failed_remote_listing_is_unknown(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-norefs", "claude/norefs")
        run = failing_runner(lambda argv, cwd: argv[1] == "for-each-ref" and "refs/remotes" in argv)
        co = self.one(self.report(run=run), lane)
        self.assertEqual(co["safety"], "UNKNOWN")
        self.assertIn("remotes: timed out", co["read_errors"])


class UnknownPrStateTests(GitCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = self.integration()
        self.lane_path = self.lane(self.parent, "dealdex-claude-openpr", "claude/openpr")
        head = self.git(self.lane_path, "rev-parse", "HEAD")
        self.open_pr = [{"number": 9, "state": "OPEN", "headRefName": "claude/openpr", "headRefOid": head}]

    def test_with_gh_the_open_pr_lane_needs_review(self) -> None:
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: self.open_pr}))
        co = self.one(rep, self.lane_path)
        self.assertEqual((co["safety"], co["pr_state"]), ("NEEDS-REVIEW", "OPEN"))
        self.assertEqual(rep["gh"], "ok")
        self.assertEqual(rep["warnings"], [])

    def test_a_failed_lookup_leaves_it_unknown_and_the_report_says_gh_failed(self) -> None:
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: None}))
        co = self.one(rep, self.lane_path)
        self.assertEqual((co["safety"], co["pr_state"]), ("UNKNOWN", "UNKNOWN"))
        self.assertIn("PR state unknown: gh lookup failed or skipped", co["safety_reasons"])
        self.assertEqual(rep["gh"], "failed")
        self.assertTrue(any(DEALDEX_REPO in w and "PR state" in w for w in rep["warnings"]), rep["warnings"])
        self.assertEqual(rep["summary"]["by_pr_state"].get("UNKNOWN"), 1)

    def test_no_gh_leaves_it_unknown_without_a_failure_warning(self) -> None:
        rep = self.report(use_gh=False)
        co = self.one(rep, self.lane_path)
        self.assertEqual((co["safety"], co["pr_state"]), ("UNKNOWN", "UNKNOWN"))
        self.assertIn("PR state unknown: gh skipped (--no-gh)", co["safety_reasons"])
        self.assertEqual(rep["gh"], "skipped")
        self.assertEqual(rep["warnings"], [])

    def test_a_gh_that_fails_for_one_repo_is_partial(self) -> None:
        st = self.make_repo(self.home / "Code" / "Socratic-Trade", remote=f"https://github.com/{OWNER}/Socratic-Trade.git")
        st_lane = self.lane(st, "trading-claude-partial", "claude/partial")
        gh = FakeGh({DEALDEX_REPO: self.open_pr, f"{OWNER}/Socratic-Trade": None})
        rep = self.report(gh_prs=gh)
        self.assertEqual(rep["gh"], "partial")
        self.assertTrue(any(f"{OWNER}/Socratic-Trade" in w for w in rep["warnings"]))
        self.assertFalse(any(DEALDEX_REPO in w for w in rep["warnings"]))
        self.assertEqual(self.one(rep, st_lane)["safety"], "UNKNOWN")
        self.assertEqual(self.one(rep, self.lane_path)["safety"], "NEEDS-REVIEW")

    def test_default_branch_trees_never_need_a_lookup(self) -> None:
        co = self.one(self.report(use_gh=False), self.parent)
        self.assertEqual(co["pr_state"], "N/A")
        self.assertNotIn("PR state unknown", " ".join(co["safety_reasons"]))


class DiscoveryGapTests(GitCase):
    def test_checkouts_in_the_blind_spots_the_review_found_are_reported(self) -> None:
        buzz = self.make_repo(self.home / ".buzz" / "REPOS" / "congress-trade", remote=None)
        compare = self.make_repo(self.home / "tmp" / "cleaner-compare" / "MacSai", remote=None)
        cached = self.make_repo(self.home / ".cache" / "thing" / "clone", remote=None)
        cargo = self.make_repo(self.home / ".cargo" / "git" / "checkouts", remote=None)
        too_deep = self.make_repo(self.home / "a" / "b" / "c" / "d", remote=None)
        found = self.by_path(self.report())
        self.assertIn(os.path.realpath(buzz), found)
        self.assertEqual(found[os.path.realpath(buzz)]["creating_tool"], "buzz")
        self.assertIn(os.path.realpath(compare), found)
        for hidden in (cached, cargo, too_deep):
            self.assertNotIn(os.path.realpath(hidden), found)

    def test_deep_reaches_documents_four_levels_down(self) -> None:
        old = self.make_repo(self.home / "Documents" / "Old Documents" / "SimpleWithUs" / "Xcode Git" / "SimpleWithUs",
                             remote=None)
        self.assertNotIn(os.path.realpath(old), self.by_path(self.report()))
        self.assertIn(os.path.realpath(old), self.by_path(self.report(deep=True)))


# --------------------------------------------------------------------------- fresh lanes, tool caches, cleaner contract

def synth(**kw) -> doctor.Checkout:
    """A hand-built checkout that reads as a clean, pushed, idle, old lane unless `kw` says
    otherwise.  `assess` is pure, so the rule matrix runs here without a git process."""
    fields = dict(
        path="/h/apps/dealdex-claude-s", realpath="/h/apps/dealdex-claude-s", kind="LINKED-WORKTREE",
        owner_repo=DEALDEX_REPO, remote_url=DEALDEX_URL, scope="fleet", location_class="LANE_FLAT_LEGACY",
        branch="claude/s", head_sha="a" * 40, unpushed=0, dirty_tracked=0, dirty_untracked=0,
        ignored_local_count=0, ignored_regenerable=0, lane_age_days=30.0, idle_days=30.0,
        registered=True, cwd_procs=[], active=False,
    )
    fields.update(kw)
    co = doctor.Checkout(**fields)
    co._remote_checked = True
    co._git_ok = True
    return co


def assessed(pr: doctor.PrMatch | None = None, active_reasons=(), **kw) -> doctor.Checkout:
    """`synth(**kw)` after `assess`.  A `fresh_days` entry in `kw` goes to `assess`."""
    options = {"fresh_days": kw.pop("fresh_days")} if "fresh_days" in kw else {}
    co = synth(**kw)
    doctor.assess(co, pr or doctor.PrMatch("NONE", basis="no PR matches this branch or HEAD"),
                  list(active_reasons), **options)
    return co


MERGED = doctor.PrMatch("MERGED", 5, "HEAD equals the head of a merged PR")
CLOSED = doctor.PrMatch("CLOSED", 6, "HEAD equals the head of a closed (unmerged) PR")
LANE_LIKE = ("LANE_NESTED", "LANE_FLAT_LEGACY", "LANE_FLAT", "REVIEW", "MANAGED")
OUTSIDE_THE_MAP = ("INTEGRATION_TREE", "UNSANCTIONED", "FORBIDDEN_TMP", "FORBIDDEN_CODE_TOPLEVEL")


class FreshLaneRuleTests(unittest.TestCase):
    def test_the_default_threshold_is_seven_days(self) -> None:
        self.assertEqual(doctor.FRESH_DAYS, 7)

    def test_an_old_clean_lane_is_safe_to_remove(self) -> None:
        self.assertEqual(assessed().safety, "SAFE-TO-REMOVE")

    def test_young_by_age_or_by_idle_needs_review_with_the_exact_reason(self) -> None:
        cases = [
            (dict(lane_age_days=3.0, idle_days=30.0), "fresh lane: age 3d, idle 30d, not merged"),
            (dict(lane_age_days=30.0, idle_days=2.5), "fresh lane: age 30d, idle 2.5d, not merged"),
            (dict(lane_age_days=0.0, idle_days=0.0), "fresh lane: age 0d, idle 0d, not merged"),
            (dict(lane_age_days=2.0, idle_days=None), "fresh lane: age 2d, idle ?, not merged"),
            (dict(lane_age_days=None, idle_days=1.5), "fresh lane: age ?, idle 1.5d, not merged"),
        ]
        for fields, reason in cases:
            with self.subTest(fields=fields):
                co = assessed(**fields)
                self.assertEqual(co.safety, "NEEDS-REVIEW")
                self.assertIn(reason, co.safety_reasons)
                self.assertIs(co.dropped_ball, False)  # a young lane is not lost work

    def test_the_threshold_is_exclusive_and_follows_fresh_days(self) -> None:
        self.assertEqual(assessed(lane_age_days=7.0, idle_days=7.0).safety, "SAFE-TO-REMOVE")
        self.assertEqual(assessed(lane_age_days=6.9, idle_days=30.0).safety, "NEEDS-REVIEW")
        self.assertEqual(assessed(lane_age_days=30.0, idle_days=6.9).safety, "NEEDS-REVIEW")
        self.assertEqual(assessed(lane_age_days=3.0, idle_days=3.0, fresh_days=3).safety, "SAFE-TO-REMOVE")
        self.assertEqual(assessed(lane_age_days=2.9, idle_days=3.0, fresh_days=3).safety, "NEEDS-REVIEW")
        self.assertEqual(assessed(lane_age_days=10.0, idle_days=10.0, fresh_days=14).safety, "NEEDS-REVIEW")
        self.assertEqual(assessed(lane_age_days=0.0, idle_days=0.0, fresh_days=0).safety, "SAFE-TO-REMOVE")

    def test_merged_pr_evidence_overrides_freshness(self) -> None:
        for pr in (MERGED, CLOSED):
            with self.subTest(state=pr.state):
                co = assessed(pr, lane_age_days=0.0, idle_days=0.0)
                self.assertEqual(co.safety, "SAFE-TO-REMOVE", co.safety_reasons)
                self.assertFalse([r for r in co.safety_reasons if r.startswith("fresh lane")])

    def test_beyond_merged_unknown_and_open_prs_do_not_override_freshness(self) -> None:
        cases = [
            doctor.PrMatch("BEYOND-MERGED", 5, "HEAD has commits past the merged PR head", 2),
            doctor.PrMatch("OPEN", 5, "open PR for this branch or HEAD"),
            doctor.PrMatch("NONE", basis="no PR matches this branch or HEAD"),
            doctor.PrMatch("UNKNOWN", basis="gh lookup failed or skipped"),
        ]
        for pr in cases:
            with self.subTest(state=pr.state):
                co = assessed(pr, lane_age_days=1.0, idle_days=1.0)
                self.assertEqual(co.safety, "NEEDS-REVIEW")
                self.assertIn("fresh lane: age 1d, idle 1d, not merged", co.safety_reasons)

    def test_every_lane_like_class_is_covered(self) -> None:
        for cls in LANE_LIKE:
            with self.subTest(location_class=cls):
                young = assessed(location_class=cls, lane_age_days=1.0, idle_days=1.0)
                self.assertEqual(young.safety, "NEEDS-REVIEW")
                self.assertEqual(assessed(location_class=cls).safety, "SAFE-TO-REMOVE")

    def test_locations_outside_the_lane_map_are_unaffected(self) -> None:
        for cls in OUTSIDE_THE_MAP:
            with self.subTest(location_class=cls):
                co = assessed(location_class=cls, lane_age_days=0.0, idle_days=0.0)
                self.assertEqual(co.safety, "SAFE-TO-REMOVE", co.safety_reasons)
                unknown_age = assessed(location_class=cls, lane_age_days=None, idle_days=None)
                self.assertEqual(unknown_age.safety, "SAFE-TO-REMOVE", unknown_age.safety_reasons)

    def test_an_unknown_age_on_a_lane_is_unknown_not_safe(self) -> None:
        for fields in (dict(lane_age_days=None), dict(idle_days=None), dict(lane_age_days=None, idle_days=None)):
            for pr in (None, MERGED):
                with self.subTest(fields=fields, merged=pr is not None):
                    co = assessed(pr, **fields)
                    self.assertEqual(co.safety, "UNKNOWN", co.safety_reasons)
                    self.assertTrue(any(r.startswith("lane age unknown") for r in co.safety_reasons), co.safety_reasons)
        dirty = assessed(lane_age_days=None, dirty_tracked=1)
        self.assertEqual(dirty.safety, "NEEDS-REVIEW")  # a known risk still outranks an unknown
        orphan = assessed(kind="ORPHAN", idle_days=None)
        self.assertEqual(orphan.safety, "UNKNOWN")
        self.assertFalse([r for r in orphan.safety_reasons if r.startswith("lane age unknown")])

    def test_active_still_wins_and_lists_the_fresh_reason(self) -> None:
        co = assessed(active=True, active_reasons=["active: process cwd 9:vim"], lane_age_days=1.0, idle_days=1.0)
        self.assertEqual(co.safety, "ACTIVE")
        self.assertEqual(co.safety_reasons[0], "active: process cwd 9:vim")
        self.assertIn("fresh lane: age 1d, idle 1d, not merged", co.safety_reasons)

    def test_a_fresh_lane_that_is_also_dirty_keeps_every_reason_and_the_dropped_ball(self) -> None:
        co = assessed(lane_age_days=1.0, idle_days=1.0, dirty_untracked=2)
        self.assertEqual(co.safety, "NEEDS-REVIEW")
        self.assertIs(co.dropped_ball, True)
        self.assertEqual(len([r for r in co.safety_reasons if r.startswith(("dirty", "fresh lane"))]), 2)


class ToolCacheAssessTests(unittest.TestCase):
    def test_a_dirty_unpushed_tool_cache_is_labelled_and_never_a_dropped_ball(self) -> None:
        co = assessed(tool_cache=True, location_class="UNSANCTIONED", dirty_tracked=2, unpushed=3, scope="third-party")
        self.assertEqual(co.safety, "TOOL-CACHE")
        self.assertIs(co.dropped_ball, False)
        control = assessed(tool_cache=False, location_class="UNSANCTIONED", dirty_tracked=2, unpushed=3)
        self.assertEqual((control.safety, control.dropped_ball), ("NEEDS-REVIEW", True))

    def test_an_unknown_pr_state_does_not_make_it_a_maybe_dropped_ball(self) -> None:
        pr = doctor.PrMatch("UNKNOWN", basis="gh lookup failed or skipped")
        co = assessed(pr, tool_cache=True, location_class="UNSANCTIONED", dirty_tracked=1)
        self.assertEqual(co.safety, "TOOL-CACHE")
        self.assertIs(co.dropped_ball, False)  # False, not None: it must not reach dropped_ball_unknown

    def test_the_label_wins_over_active_and_keeps_the_underlying_class_in_the_reasons(self) -> None:
        co = assessed(tool_cache=True, active=True, active_reasons=["active: process cwd 3:node"], dirty_tracked=1)
        self.assertEqual(co.safety, "TOOL-CACHE")
        self.assertTrue(co.safety_reasons[0].startswith("tool cache:"), co.safety_reasons)
        self.assertIn("ACTIVE", co.safety_reasons[0])
        self.assertIn("active: process cwd 3:node", co.safety_reasons)
        quiet = assessed(tool_cache=True)
        self.assertIn("SAFE-TO-REMOVE", quiet.safety_reasons[0])

    def test_a_tool_cache_is_never_a_fresh_lane_either(self) -> None:
        co = assessed(tool_cache=True, location_class="MANAGED", lane_age_days=0.0, idle_days=0.0)
        self.assertEqual(co.safety, "TOOL-CACHE")


class ToolCacheReportTests(GitCase):
    PLUGIN = "https://github.com/vendor/plugin.git"

    def caches(self) -> tuple[pathlib.Path, pathlib.Path]:
        plugin = self.make_repo(self.home / ".grok" / "installed-plugins" / "x-1234", remote=self.PLUGIN)
        self.commit(plugin)  # one commit ahead of origin/main
        (plugin / "scratch.txt").write_text("dirty")
        fleet = self.make_repo(self.home / ".grok" / "marketplace-cache" / "fleet-thing", branch="feat/x")
        self.commit(fleet)
        return plugin, fleet

    def test_tool_caches_stay_in_the_report_with_their_own_label(self) -> None:
        plugin, fleet = self.caches()
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: None}))  # the fleet-remote cache would read UNKNOWN
        for cache in (plugin, fleet):
            co = self.one(rep, cache)
            self.assertTrue(co["tool_cache"])
            self.assertEqual(co["safety"], "TOOL-CACHE")
            self.assertIs(co["dropped_ball"], False)
        s = rep["summary"]
        self.assertEqual((s["total"], s["tool_cache"], s["by_safety"]), (2, 2, {"TOOL-CACHE": 2}))
        self.assertEqual((s["dropped_ball"], s["dropped_ball_unknown"], s["strict_violations"]), (0, 0, 0))
        self.assertEqual(rep["anomalies"], [])

    def test_a_process_in_a_tool_cache_still_reads_tool_cache(self) -> None:
        plugin, _ = self.caches()
        co = self.one(self.report(lsof_cwds=lambda: [(7, "node", str(plugin))]), plugin)
        self.assertEqual(co["safety"], "TOOL-CACHE")
        self.assertTrue(co["active"])  # the fact is still reported

    def test_text_output_never_lists_a_tool_cache_as_needing_attention(self) -> None:
        self.caches()
        mine = self.make_repo(self.home / "Code" / "Socratic-Trade", remote=f"https://github.com/{OWNER}/Socratic-Trade.git")
        (mine / "wip.txt").write_text("x")
        out = io.StringIO()
        code = doctor.main(["--home", str(self.home), "--tmp-root", str(self.faketmp)], env=self.env,
                           clock=self.aged(OLD_DAYS), gh_prs=FakeGh(), lsof_cwds=lambda: [], cwd=str(self.base),
                           stdout=out)
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("TOOL-CACHE", text)  # the safety table counts them
        self.assertIn("Tool caches 2", text)
        self.assertIn("DROPPED BALL", text)  # the real dirty tree is listed
        self.assertNotIn("installed-plugins", text)
        self.assertNotIn("marketplace-cache", text)

    def test_a_tool_cache_in_a_temp_directory_is_still_a_strict_violation(self) -> None:
        cache = self.make_repo(self.faketmp / "plugins" / "cache" / "p", remote=self.PLUGIN)
        rep = self.report()
        co = self.one(rep, cache)
        self.assertEqual((co["location_class"], co["tool_cache"], co["safety"]), ("FORBIDDEN_TMP", True, "TOOL-CACHE"))
        self.assertEqual(rep["summary"]["strict_violations"], 1)  # as before: FORBIDDEN_TMP always applies


class FreshLaneReportTests(GitCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = self.integration()

    def merged_pr(self, lane: pathlib.Path, branch: str, state: str = "MERGED") -> FakeGh:
        head = self.git(lane, "rev-parse", "HEAD")
        return FakeGh({DEALDEX_REPO: [{"number": 21, "state": state, "headRefName": branch, "headRefOid": head}]})

    def test_a_brand_new_clean_pushed_lane_is_not_safe_to_remove(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-new", "claude/new")
        rep = self.report(clock=fixed_clock)
        co = self.one(rep, lane)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertEqual(co["safety_reasons"], ["fresh lane: age 0d, idle 0d, not merged"])
        self.assertIs(co["dropped_ball"], False)
        self.assertEqual(rep["summary"]["dropped_ball"], 0)
        self.assertEqual(self.one(self.report(clock=self.aged(OLD_DAYS)), lane)["safety"], "SAFE-TO-REMOVE")

    def test_a_lane_a_few_days_old_is_still_fresh(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-few", "claude/few")
        co = self.one(self.report(clock=self.aged(3)), lane)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertTrue(co["safety_reasons"][0].startswith("fresh lane: age 3"), co["safety_reasons"])
        self.assertEqual(self.one(self.report(clock=self.aged(8)), lane)["safety"], "SAFE-TO-REMOVE")

    def test_a_recent_head_move_makes_an_old_lane_fresh(self) -> None:
        lane = self.lane(self.parent, "dealdex-claude-moved", "claude/moved")
        admin = (lane / ".git").read_text().split("gitdir:", 1)[1].strip()
        soon = dt.datetime.now(dt.timezone.utc).timestamp() + 29 * 86400  # one day before the 30 day clock
        os.utime(os.path.join(admin, "HEAD"), (soon, soon))
        co = self.one(self.report(clock=self.aged(OLD_DAYS)), lane)
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertAlmostEqual(co["idle_days"], 1.0, delta=0.5)
        self.assertGreater(co["lane_age_days"], 20)
        self.assertTrue(co["safety_reasons"][0].startswith("fresh lane: age 30d, idle 1d"), co["safety_reasons"])

    def test_merged_evidence_overrides_freshness_end_to_end(self) -> None:
        for state, name in (("MERGED", "m"), ("CLOSED", "c")):
            with self.subTest(state=state):
                lane = self.lane(self.parent, f"dealdex-claude-{name}", f"claude/{name}", pushed=False)
                self.commit(lane)
                gh = self.merged_pr(lane, f"claude/{name}", state)
                co = self.one(self.report(clock=fixed_clock, gh_prs=gh), lane)
                self.assertEqual((co["pr_state"], co["safety"]), (state, "SAFE-TO-REMOVE"), co["safety_reasons"])

    def lane_like_checkouts(self) -> dict[str, pathlib.Path]:
        review = self.home / "apps" / "lanes" / "_review" / "dealdex" / "pr-9"
        review.parent.mkdir(parents=True)
        self.git(self.parent, "worktree", "add", "-q", "--detach", str(review))
        return {
            "LANE_FLAT_LEGACY": self.lane(self.parent, "dealdex-claude-flat", "claude/flat"),
            "LANE_NESTED": self.add_worktree(self.parent, self.home / "apps" / "lanes" / "dealdex" / "claude-nest",
                                             "claude/nest"),
            "REVIEW": review,
            "MANAGED": self.add_worktree(self.parent, self.parent / ".claude" / "worktrees" / "x-1a2b3c",
                                         "claude/x-1a2b3c"),
        }

    def test_nested_flat_review_and_managed_lanes_are_all_covered(self) -> None:
        made = self.lane_like_checkouts()
        young, old = self.report(clock=fixed_clock), self.report(clock=self.aged(OLD_DAYS))
        for cls, path in made.items():
            with self.subTest(location_class=cls):
                co = self.one(young, path)
                self.assertEqual(co["location_class"], cls)
                self.assertEqual(co["safety"], "NEEDS-REVIEW", co["safety_reasons"])
                self.assertIn("fresh lane: age 0d, idle 0d, not merged", co["safety_reasons"])
                self.assertEqual(self.one(old, path)["safety"], "SAFE-TO-REMOVE", self.one(old, path)["safety_reasons"])

    def test_integration_trees_and_places_outside_the_map_ignore_age(self) -> None:
        solo = self.make_repo(self.home / "Code" / "Socratic-Trade", remote=f"https://github.com/{OWNER}/Socratic-Trade.git")
        tmp = self.make_repo(self.faketmp / "bf-y")
        stray = self.make_repo(self.home / "fleet-stray")
        rep = self.report(clock=fixed_clock)
        for path, cls in ((solo, "INTEGRATION_TREE"), (tmp, "FORBIDDEN_TMP"), (stray, "UNSANCTIONED"), (self.parent, "INTEGRATION_TREE")):
            with self.subTest(location_class=cls, path=path.name):
                co = self.one(rep, path)
                self.assertEqual(co["location_class"], cls)
                self.assertEqual(co["safety"], "SAFE-TO-REMOVE", co["safety_reasons"])


class CleanerContractTests(GitCase):
    def setUp(self) -> None:
        super().setUp()
        self.parent = self.integration()

    def merged_lane(self, dirname: str = "dealdex-claude-done", branch: str = "claude/done",
                    parent: pathlib.Path | None = None, state: str = "MERGED") -> tuple[pathlib.Path, dict]:
        lane = self.lane(parent or self.parent, dirname, branch, pushed=False)
        self.commit(lane)
        pr = {"number": 33, "state": state, "headRefName": branch, "headRefOid": self.git(lane, "rev-parse", "HEAD"),
              "mergedAt": "2026-10-01T00:00:00Z"}
        return lane, pr

    def candidates(self, **kw) -> list[str]:
        kw.setdefault("clock", self.aged(OLD_DAYS))
        return self.report(**kw)["cleaner_candidates"]

    def test_an_old_merged_lane_is_the_candidate_and_the_json_says_how_fresh_the_report_is(self) -> None:
        lane, pr = self.merged_lane()
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: [pr]}))
        self.assertEqual(rep["cleaner_candidates"], [os.path.realpath(lane)])
        self.assertEqual(rep["summary"]["cleaner_candidates"], 1)
        self.assertEqual((rep["schema"], rep["fresh_days"], rep["cleaner_min_days"]), (2, 7, 7))
        self.assertEqual((rep["gh"], rep["lsof"], rep["gh_repos"]), ("ok", "ok", {DEALDEX_REPO: "ok"}))
        dt.datetime.fromisoformat(rep["generated_at"])  # a consumer compares this with its own clock
        self.assertEqual(self.one(rep, lane)["safety"], "SAFE-TO-REMOVE")

    def test_safe_to_remove_alone_is_not_enough(self) -> None:
        clean = self.lane(self.parent, "dealdex-claude-clean", "claude/clean")  # clean, pushed, no PR at all
        rep = self.report()
        self.assertEqual(self.one(rep, clean)["safety"], "SAFE-TO-REMOVE")
        self.assertEqual(self.one(rep, clean)["pr_state"], "NONE")
        self.assertEqual(rep["cleaner_candidates"], [])

    def test_a_merged_lane_younger_than_the_floor_is_safe_but_not_a_candidate(self) -> None:
        lane, pr = self.merged_lane()
        gh = FakeGh({DEALDEX_REPO: [pr]})
        for clock, label in ((fixed_clock, "brand new"), (self.aged(3), "three days")):
            with self.subTest(age=label):
                rep = self.report(gh_prs=gh, clock=clock)
                self.assertEqual(self.one(rep, lane)["safety"], "SAFE-TO-REMOVE")
                self.assertEqual(rep["cleaner_candidates"], [])
        self.assertEqual(len(self.candidates(gh_prs=gh, clock=self.aged(8))), 1)

    def test_a_recent_head_move_blocks_the_candidate(self) -> None:
        lane, pr = self.merged_lane()
        admin = (lane / ".git").read_text().split("gitdir:", 1)[1].strip()
        soon = dt.datetime.now(dt.timezone.utc).timestamp() + 29 * 86400
        os.utime(os.path.join(admin, "HEAD"), (soon, soon))
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: [pr]}))
        co = self.one(rep, lane)
        self.assertEqual(co["safety"], "SAFE-TO-REMOVE")  # merged overrides freshness in the label
        self.assertLess(co["idle_days"], 7)
        self.assertEqual(rep["cleaner_candidates"], [])  # but the cleaner contract wants both ages at the floor

    def test_every_other_disqualifier_removes_the_lane_from_the_list(self) -> None:
        lane, pr = self.merged_lane()
        gh = FakeGh({DEALDEX_REPO: [pr]})
        self.assertEqual(self.candidates(gh_prs=gh), [os.path.realpath(lane)])  # control

        (lane / "wip.txt").write_text("x")
        self.assertEqual(self.candidates(gh_prs=gh), [], "dirty")
        os.remove(lane / "wip.txt")

        (lane / ".janitor-keep").write_text("")
        with open(self.parent / ".git" / "info" / "exclude", "a", encoding="utf-8") as fh:
            fh.write(".janitor-keep\n")
        self.assertEqual(self.candidates(gh_prs=gh), [], "janitor keep")
        os.remove(lane / ".janitor-keep")

        self.assertEqual(self.candidates(gh_prs=gh, lsof_cwds=lambda: [(4, "zsh", str(lane))]), [], "process cwd")
        self.assertEqual(self.candidates(gh_prs=gh, lsof_cwds=lambda: None), [], "lsof failed")
        self.assertEqual(self.candidates(gh_prs=gh, cwd=str(lane)), [], "the doctor's own cwd")
        self.assertEqual(self.candidates(gh_prs=gh, use_gh=False), [], "gh skipped")
        self.assertEqual(self.candidates(gh_prs=FakeGh({DEALDEX_REPO: None})), [], "gh failed")

        def flaky(argv, cwd=None, timeout=None):
            if cwd and os.path.realpath(cwd) == os.path.realpath(lane) and argv[1] == "status":
                return doctor.CmdResult(None, timed_out=True)
            return doctor.run_cmd(argv, cwd, timeout)
        self.assertEqual(self.candidates(gh_prs=gh, run=flaky), [], "a read error")

        open_pr = dict(pr, state="OPEN")
        self.assertEqual(self.candidates(gh_prs=FakeGh({DEALDEX_REPO: [open_pr]})), [], "open PR")
        self.assertEqual(self.candidates(gh_prs=gh), [os.path.realpath(lane)])  # nothing above leaked state

    def test_a_failed_gh_lookup_for_one_repo_only_removes_that_repos_lanes(self) -> None:
        good, good_pr = self.merged_lane()
        st = self.make_repo(self.home / "Code" / "Socratic-Trade", remote=f"https://github.com/{OWNER}/Socratic-Trade.git")
        other, _ = self.merged_lane("trading-claude-done", "claude/done", parent=st)
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: [good_pr], f"{OWNER}/Socratic-Trade": None}))
        self.assertEqual(rep["gh"], "partial")
        self.assertEqual(rep["gh_repos"], {DEALDEX_REPO: "ok", f"{OWNER}/Socratic-Trade": "failed"})
        self.assertEqual(rep["cleaner_candidates"], [os.path.realpath(good)])
        self.assertNotEqual(self.one(rep, other)["pr_state"], "MERGED")

    def test_only_checkouts_in_the_lane_map_that_git_can_remove_qualify(self) -> None:
        elsewhere = self.add_worktree(self.parent, self.home / "elsewhere" / "wt", "claude/elsewhere", pushed=False)
        tmp = self.add_worktree(self.parent, self.faketmp / "bf-x" / "wt", "claude/tmpwork", pushed=False)
        clone = self.make_repo(self.home / "apps" / "dealdex-claude-clone", branch="claude/clone")
        cache = self.make_repo(self.home / ".grok" / "installed-plugins" / "x-1234", branch="claude/cache")
        prs = []
        for number, (path, branch) in enumerate(((elsewhere, "claude/elsewhere"), (tmp, "claude/tmpwork"),
                                                 (clone, "claude/clone"), (cache, "claude/cache")), 50):
            prs.append({"number": number, "state": "MERGED", "headRefName": branch,
                        "headRefOid": self.git(path, "rev-parse", "HEAD")})
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: prs}))
        want = {elsewhere: ("UNSANCTIONED", "SAFE-TO-REMOVE"), tmp: ("FORBIDDEN_TMP", "SAFE-TO-REMOVE"),
                clone: ("LANE_FLAT_LEGACY", "SAFE-TO-REMOVE"), cache: ("UNSANCTIONED", "TOOL-CACHE")}
        for path, (cls, safety) in want.items():
            with self.subTest(path=path.name):
                co = self.one(rep, path)
                self.assertEqual((co["location_class"], co["safety"], co["pr_state"]), (cls, safety, "MERGED"))
        self.assertEqual(self.one(rep, clone)["kind"], "FULL-CLONE")
        self.assertEqual(rep["cleaner_candidates"], [])

    def test_nested_review_managed_and_flat_lanes_can_all_be_candidates_in_sorted_order(self) -> None:
        flat, flat_pr = self.merged_lane()
        nested = self.add_worktree(self.parent, self.home / "apps" / "lanes" / "dealdex" / "claude-nest", "claude/nest",
                                   pushed=False)
        self.commit(nested)
        managed = self.add_worktree(self.parent, self.parent / ".claude" / "worktrees" / "x-1a2b3c", "claude/x-1a2b3c",
                                    pushed=False)
        self.commit(managed)
        review = self.home / "apps" / "lanes" / "_review" / "dealdex" / "pr-9"
        review.parent.mkdir(parents=True)
        self.git(self.parent, "worktree", "add", "-q", "--detach", str(review))
        self.commit(review, "review.txt")  # a detached head of its own, covered by a merged PR
        prs = [flat_pr]
        for number, (path, branch) in enumerate(((nested, "claude/nest"), (managed, "claude/x-1a2b3c"),
                                                 (review, "claude/pr-9")), 60):
            prs.append({"number": number, "state": "MERGED", "headRefName": branch,
                        "headRefOid": self.git(path, "rev-parse", "HEAD")})
        rep = self.report(gh_prs=FakeGh({DEALDEX_REPO: prs}))
        got = rep["cleaner_candidates"]
        self.assertEqual(got, sorted(got))
        self.assertEqual(set(got), {os.path.realpath(p) for p in (flat, nested, managed, review)})
        self.assertEqual({self.one(rep, p)["location_class"] for p in (flat, nested, managed, review)},
                         {"LANE_FLAT_LEGACY", "LANE_NESTED", "MANAGED", "REVIEW"})

    def test_only_class_limits_the_candidates_to_what_is_shown(self) -> None:
        flat, flat_pr = self.merged_lane()
        nested = self.add_worktree(self.parent, self.home / "apps" / "lanes" / "dealdex" / "claude-nest", "claude/nest",
                                   pushed=False)
        self.commit(nested)
        nested_pr = {"number": 61, "state": "MERGED", "headRefName": "claude/nest",
                     "headRefOid": self.git(nested, "rev-parse", "HEAD")}
        gh = FakeGh({DEALDEX_REPO: [flat_pr, nested_pr]})
        rep = self.report(gh_prs=gh, only_class="LANE_NESTED")
        self.assertEqual(rep["cleaner_candidates"], [os.path.realpath(nested)])
        self.assertEqual(rep["summary"]["cleaner_candidates"], 1)


class CleanerPredicateTests(unittest.TestCase):
    """`cleaner_candidates` on hand-built checkouts, one disqualifier at a time."""

    def ready(self, **kw) -> doctor.Checkout:
        return assessed(MERGED, **kw)

    def pick(self, items, **kw) -> list[str]:
        kw.setdefault("gh_ok_repos", {DEALDEX_REPO})
        return doctor.cleaner_candidates(items, **kw)

    def test_the_baseline_qualifies_and_closed_with_the_same_head_counts_as_merged(self) -> None:
        self.assertEqual(self.pick([self.ready()]), ["/h/apps/dealdex-claude-s"])
        self.assertEqual(self.pick([assessed(CLOSED)]), ["/h/apps/dealdex-claude-s"])

    def test_each_condition_is_required(self) -> None:
        cases = [
            ("not merged", dict(pr_state="NONE")),
            ("open pr", dict(pr_state="OPEN")),
            ("beyond merged", dict(pr_state="BEYOND-MERGED")),
            ("safety", dict(safety="NEEDS-REVIEW")),
            ("unknown safety", dict(safety="UNKNOWN")),
            ("tool cache label", dict(safety="TOOL-CACHE")),
            ("age below floor", dict(lane_age_days=6.9)),
            ("age unknown", dict(lane_age_days=None)),
            ("idle below floor", dict(idle_days=6.9)),
            ("idle unknown", dict(idle_days=None)),
            ("janitor keep", dict(janitor_keep=True)),
            ("process cwd", dict(cwd_procs=["4:zsh"])),
            ("lsof unavailable", dict(cwd_procs=None)),
            ("active", dict(active=True)),
            ("active unknown", dict(active=None)),
            ("tool cache flag", dict(tool_cache=True)),
            ("read error", dict(read_errors=["status: timed out"])),
            ("repo not ok", dict(owner_repo="Simple-With-Us/Other")),
            ("no repo", dict(owner_repo=None)),
            ("full clone", dict(kind="FULL-CLONE")),
            ("orphan", dict(kind="ORPHAN")),
            ("unregistered", dict(registered=False)),
            ("registration unknown", dict(registered=None)),
            ("newline in path", dict(realpath="/h/apps/dealdex-claude-s\n/Users/jay/Code")),
            ("carriage return in path", dict(realpath="/h/apps/dealdex-claude-s\r")),
            ("empty path", dict(realpath="")),
        ]
        for cls in OUTSIDE_THE_MAP:
            cases.append((f"class {cls}", dict(location_class=cls)))
        for label, changes in cases:
            with self.subTest(label):
                co = self.ready()
                for name, value in changes.items():
                    setattr(co, name, value)
                self.assertEqual(self.pick([co]), [], label)

    def test_every_lane_like_class_qualifies(self) -> None:
        for cls in LANE_LIKE:
            with self.subTest(cls):
                self.assertEqual(len(self.pick([self.ready(location_class=cls)])), 1)

    def test_the_age_floor_never_drops_below_seven_days(self) -> None:
        young = self.ready(lane_age_days=3.0, idle_days=3.0, fresh_days=2)
        self.assertEqual(self.pick([young], fresh_days=2), [])
        self.assertEqual(self.pick([self.ready(lane_age_days=7.0, idle_days=7.0)], fresh_days=2), ["/h/apps/dealdex-claude-s"])
        ten = self.ready(lane_age_days=10.0, idle_days=10.0)
        self.assertEqual(self.pick([ten], fresh_days=14), [])  # a larger option raises the floor
        self.assertEqual(self.pick([self.ready(lane_age_days=14.0, idle_days=14.0)], fresh_days=14),
                         ["/h/apps/dealdex-claude-s"])

    def test_a_checkout_that_contains_another_checkout_is_left_out(self) -> None:
        outer = self.ready(realpath="/h/apps/lanes/dealdex/claude-outer")
        inner = self.ready(realpath="/h/apps/lanes/dealdex/claude-outer/.claude/worktrees/in-1a2b3c")
        self.assertEqual(self.pick([outer, inner]), [inner.realpath])
        self.assertEqual(self.pick([outer], everything=[outer, inner]), [])  # `everything` is the whole machine
        sibling = self.ready(realpath="/h/apps/lanes/dealdex/claude-outer2")  # a shared name prefix is not containment
        self.assertEqual(self.pick([outer, sibling]), sorted([outer.realpath, sibling.realpath]))

    def test_duplicates_collapse_and_the_result_is_sorted(self) -> None:
        a, b = self.ready(realpath="/h/apps/b"), self.ready(realpath="/h/apps/a")
        self.assertEqual(self.pick([a, b, self.ready(realpath="/h/apps/a")]), ["/h/apps/a", "/h/apps/b"])


# --------------------------------------------------------------------------- the invariant

def snapshot(roots: list[pathlib.Path]) -> dict[str, tuple[str, int]]:
    """path -> (sha1, mtime_ns) for every file under the roots, .git included."""
    out: dict[str, tuple[str, int]] = {}
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                path = os.path.join(dirpath, name)
                try:
                    data = pathlib.Path(path).read_bytes()
                    out[path] = (hashlib.sha1(data).hexdigest(), os.stat(path).st_mtime_ns)
                except OSError:
                    out[path] = ("unreadable", 0)
    return out


class ReadOnlyTests(GitCase):
    def stale_index(self, repo: pathlib.Path) -> None:
        """Touch a tracked file without changing its content, so an unlocked `git status` wants to
        refresh and rewrite the index."""
        target = repo / "README.md"
        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns + 5_000_000_000, st.st_mtime_ns + 5_000_000_000))

    def test_plain_git_status_rewrites_the_index_so_this_test_can_fail(self) -> None:
        control = self.make_repo(self.home / "control")
        self.stale_index(control)
        before = hashlib.sha1((control / ".git" / "index").read_bytes()).hexdigest()
        subprocess.run(["git", "status"], cwd=control, capture_output=True, check=True)
        after = hashlib.sha1((control / ".git" / "index").read_bytes()).hexdigest()
        if before == after:
            self.skipTest("this git does not refresh the index on status; the control cannot discriminate")
        self.assertNotEqual(before, after)

    def test_the_doctor_changes_nothing_in_any_checkout(self) -> None:
        parent = self.integration()
        dirty = self.lane(parent, "dealdex-claude-dirty", "claude/dirty")
        (dirty / "README.md").write_text("edited\n")
        (dirty / "untracked.txt").write_text("u")
        stale = self.lane(parent, "dealdex-claude-stale", "claude/stale")
        ahead = self.lane(parent, "dealdex-claude-ahead", "claude/ahead", pushed=False)
        self.commit(ahead)
        clone = self.make_repo(self.home / "apps" / "dealdex-claude-clone")
        self.git(clone, "checkout", "-q", "-b", "side")
        self.commit(clone, "side.txt")
        self.git(clone, "checkout", "-q", "main")
        tmp_clone = self.make_repo(self.faketmp / "bf-x", remote=None)
        for repo in (parent, stale, ahead, clone, tmp_clone):
            self.stale_index(repo)
        roots = [self.home, self.faketmp]
        before = snapshot(roots)
        self.assertGreater(len(before), 50)
        rep = self.report(sizes=True)
        self.assertGreaterEqual(len(rep["checkouts"]), 6)
        after = snapshot(roots)
        self.assertEqual(sorted(before), sorted(after), "files were created or removed")
        changed = [p for p in before if before[p] != after[p]]
        self.assertEqual(changed, [])
        for repo in (parent, stale, ahead, clone):
            self.assertFalse((repo / ".git" / "index.lock").exists() if (repo / ".git").is_dir() else False)

    def test_the_doctor_leaves_a_hook_style_environment_alone(self) -> None:
        parent = self.integration()
        decoy = self.make_repo(self.home / "decoy")
        self.stale_index(parent)
        before = snapshot([parent / ".git", decoy / ".git"])
        with mock.patch.dict(os.environ, {"GIT_DIR": str(decoy / ".git"), "GIT_INDEX_FILE": str(decoy / ".git" / "index")}):
            rep = self.report()
        self.assertEqual(snapshot([parent / ".git", decoy / ".git"]), before)
        self.assertEqual(self.one(rep, parent)["branch"], "main")


# --------------------------------------------------------------------------- command line

class CliTests(GitCase):
    def run_main(self, *args: str, **kw) -> tuple[int, str]:
        out = io.StringIO()
        kw.setdefault("env", self.env)
        kw.setdefault("clock", fixed_clock)
        kw.setdefault("gh_prs", FakeGh())
        kw.setdefault("lsof_cwds", lambda: [])
        kw.setdefault("cwd", str(self.base))
        code = doctor.main(["--home", str(self.home), "--tmp-root", str(self.faketmp), *args], stdout=out, **kw)
        return code, out.getvalue()

    def test_json_schema_keys(self) -> None:
        parent = self.integration()
        self.lane(parent, "dealdex-claude-one", "claude/one")
        code, text = self.run_main("--json")
        self.assertEqual(code, 0)
        rep = json.loads(text)
        for key in ("schema", "generated_at", "host", "layout_mode", "checkouts", "anomalies", "summary"):
            self.assertIn(key, rep)
        self.assertEqual(rep["schema"], 2)
        self.assertEqual(rep["generated_at"], "2026-10-07T16:15:00+00:00")
        self.assertEqual(rep["layout_mode"], "nested")
        for key in ("by_location_class", "by_safety", "by_creating_tool", "by_repo", "dropped_ball",
                    "forbidden_tmp", "total", "strict_violations", "anomalies_by_type"):
            self.assertIn(key, rep["summary"])
        co = rep["checkouts"][0]
        for key in ("path", "realpath", "kind", "parent_repo", "owner_repo", "location_class", "creating_tool",
                    "creating_tool_basis", "branch", "head_sha", "last_commit", "dirty_tracked",
                    "dirty_untracked", "unpushed", "upstream_state", "pr_state", "cwd_procs", "janitor_keep",
                    "size_mb", "name_verdict", "branch_verdict", "lane_age_days", "safety", "safety_reasons",
                    "dropped_ball"):
            self.assertIn(key, co)
        self.assertFalse([k for k in co if k.startswith("_")])
        self.assertEqual(rep["summary"]["total"], len(rep["checkouts"]))
        self.assertEqual(sum(rep["summary"]["by_safety"].values()), len(rep["checkouts"]))

    def test_output_is_deterministic_under_a_fixed_clock(self) -> None:
        parent = self.integration()
        self.lane(parent, "dealdex-claude-one", "claude/one")
        self.assertEqual(self.run_main("--json")[1], self.run_main("--json")[1])

    def test_strict_exit_code(self) -> None:
        parent = self.integration()
        self.lane(parent, "dealdex-claude-one", "claude/one")
        self.assertEqual(self.run_main("--strict")[0], 0)
        self.add_worktree(parent, self.faketmp / "bad", "claude/bad")
        self.assertEqual(self.run_main("--strict")[0], 2)
        self.assertEqual(self.run_main()[0], 0)  # without --strict the exit code stays 0

    def test_strict_counts_the_whole_machine_even_when_filtered(self) -> None:
        parent = self.integration()
        self.add_worktree(parent, self.faketmp / "bad", "claude/bad")
        code, text = self.run_main("--strict", "--only-class", "integration_tree", "--json")
        rep = json.loads(text)
        self.assertEqual(code, 2)
        self.assertEqual(rep["only_class"], "INTEGRATION_TREE")
        self.assertEqual({c["location_class"] for c in rep["checkouts"]}, {"INTEGRATION_TREE"})
        self.assertEqual(rep["summary"]["total_found"], 2)

    def test_no_gh_never_calls_gh(self) -> None:
        parent = self.integration()
        self.lane(parent, "dealdex-claude-one", "claude/one")

        def explode(repo: str):
            raise AssertionError("gh was called with --no-gh")
        code, text = self.run_main("--json", "--no-gh", gh_prs=explode)
        self.assertEqual(code, 0)
        self.assertIn("UNKNOWN", {c["pr_state"] for c in json.loads(text)["checkouts"]})

    def test_write_is_atomic_and_matches_stdout(self) -> None:
        parent = self.integration()
        self.lane(parent, "dealdex-claude-one", "claude/one")
        outdir = self.base / "out"
        outdir.mkdir()
        target = outdir / "report.json"
        target.write_text("old")
        code, text = self.run_main("--json", "--write", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(target.read_text(), text)
        self.assertEqual(sorted(p.name for p in outdir.iterdir()), ["report.json"])
        code, shown = self.run_main("--write", str(target))  # text mode still writes JSON
        self.assertTrue(shown.startswith("Lane doctor"))
        self.assertEqual(json.loads(target.read_text())["schema"], 2)

    def test_text_output_lists_urgent_sections_first(self) -> None:
        parent = self.integration()
        dirty = self.lane(parent, "dealdex-claude-dirty", "claude/dirty")
        (dirty / "x.txt").write_text("x")
        self.add_worktree(parent, self.faketmp / "bad", "claude/bad")
        self.lane(parent, "dealdex-claude-clean", "claude/clean")
        code, text = self.run_main()
        self.assertEqual(code, 0)
        for heading in ("Location class", "Safety class", "Creating tool", "Repo"):
            self.assertIn(heading, text)
        self.assertLess(text.index("DROPPED BALL"), text.index("FORBIDDEN TMP"))
        self.assertLess(text.index("FORBIDDEN TMP"), text.index("ANOMALIES"))
        self.assertIn("Wed, Oct 7", text)
        self.assertNotIn(str(self.home) + "/apps", text.split("DROPPED BALL", 1)[1].split("\n", 3)[2])
        self.assertIn("dirty: 0 tracked-modified, 1 untracked", text)

    def test_usage_errors_do_not_collide_with_the_strict_exit_code(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            doctor.main(["--bogus"])
        self.assertEqual(cm.exception.code, 64)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            doctor.main(["--only-class", "NOPE"])
        self.assertEqual(cm.exception.code, 64)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            doctor.main(["--gh-limit", "5000"])
        self.assertEqual(cm.exception.code, 64)

    # ---- schema 2: fresh lanes, tool caches and the cleaner contract

    def merged_lane(self, parent: pathlib.Path, dirname: str = "dealdex-claude-done", branch: str = "claude/done"):
        lane = self.lane(parent, dirname, branch, pushed=False)
        self.commit(lane)
        pr = {"number": 33, "state": "MERGED", "headRefName": branch, "headRefOid": self.git(lane, "rev-parse", "HEAD")}
        return lane, FakeGh({DEALDEX_REPO: [pr]})

    def test_json_carries_the_schema_2_additions(self) -> None:
        parent = self.integration()
        lane, gh = self.merged_lane(parent)
        code, text = self.run_main("--json", gh_prs=gh, clock=self.aged(OLD_DAYS))
        rep = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(rep["schema"], 2)
        for key in ("generated_at", "gh", "lsof", "gh_repos", "fresh_days", "cleaner_min_days", "cleaner_candidates"):
            self.assertIn(key, rep)
        self.assertEqual(rep["cleaner_candidates"], [os.path.realpath(lane)])
        self.assertEqual(rep["summary"]["cleaner_candidates"], 1)
        # every schema 1 key is still there
        for key in ("host", "layout_mode", "home", "only_class", "warnings", "scan", "checkouts", "anomalies", "summary"):
            self.assertIn(key, rep)

    def test_text_output_counts_the_cleaner_candidates(self) -> None:
        parent = self.integration()
        _, gh = self.merged_lane(parent)
        _, text = self.run_main(gh_prs=gh, clock=self.aged(OLD_DAYS))
        self.assertIn("Cleaner candidates 1", text)
        _, text = self.run_main(gh_prs=gh)  # the fixed clock makes every lane brand new
        self.assertIn("Cleaner candidates 0", text)

    def test_cleaner_list_prints_only_the_realpaths_one_per_line(self) -> None:
        parent = self.integration()
        first, gh_first = self.merged_lane(parent, "dealdex-claude-b", "claude/b")
        second = self.lane(parent, "dealdex-claude-a", "claude/a", pushed=False)
        self.commit(second)
        prs = gh_first.data[DEALDEX_REPO] + [
            {"number": 34, "state": "MERGED", "headRefName": "claude/a", "headRefOid": self.git(second, "rev-parse", "HEAD")}]
        err = io.StringIO()
        code, text = self.run_main("--cleaner-list", gh_prs=FakeGh({DEALDEX_REPO: prs}), clock=self.aged(OLD_DAYS),
                                   stderr=err)
        self.assertEqual(code, 0)
        self.assertEqual(text, "".join(p + "\n" for p in sorted(os.path.realpath(x) for x in (first, second))))
        self.assertEqual(err.getvalue(), "")

    def test_cleaner_list_is_empty_and_says_why_on_stderr_when_the_facts_are_missing(self) -> None:
        parent = self.integration()
        _, gh = self.merged_lane(parent)
        for flag, args, kw, expect in (
                ("--no-gh", ["--no-gh"], {}, "gh"),
                ("lsof", [], {"lsof_cwds": lambda: None}, "lsof"),
                ("gh failed", [], {"gh_prs": FakeGh({DEALDEX_REPO: None})}, "gh")):
            with self.subTest(flag):
                err = io.StringIO()
                kw.setdefault("gh_prs", gh)
                code, text = self.run_main("--cleaner-list", *args, clock=self.aged(OLD_DAYS), stderr=err, **kw)
                self.assertEqual((code, text), (0, ""))  # nothing at all, not even a blank line
                self.assertIn(expect, err.getvalue())

    def test_cleaner_list_conflicts_with_json_but_not_with_write_or_strict(self) -> None:
        parent = self.integration()
        lane, gh = self.merged_lane(parent)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
            self.run_main("--cleaner-list", "--json")
        self.assertEqual(cm.exception.code, 64)
        target = self.base / "report.json"
        code, text = self.run_main("--cleaner-list", "--write", str(target), gh_prs=gh, clock=self.aged(OLD_DAYS))
        self.assertEqual((code, text), (0, os.path.realpath(lane) + "\n"))
        written = json.loads(target.read_text())
        self.assertEqual((written["schema"], written["cleaner_candidates"]), (2, [os.path.realpath(lane)]))
        self.add_worktree(parent, self.faketmp / "bad", "claude/bad")
        code, text = self.run_main("--cleaner-list", "--strict", gh_prs=gh, clock=self.aged(OLD_DAYS))
        self.assertEqual((code, text), (2, os.path.realpath(lane) + "\n"))  # stdout stays paths only

    def test_fresh_days_option_moves_the_threshold_and_is_reported(self) -> None:
        parent = self.integration()
        lane = self.lane(parent, "dealdex-claude-one", "claude/one")
        clock = self.aged(10)
        _, text = self.run_main("--json", clock=clock)
        rep = json.loads(text)
        self.assertEqual((rep["fresh_days"], rep["cleaner_min_days"]), (7, 7))
        self.assertEqual(self.by_path(rep)[os.path.realpath(lane)]["safety"], "SAFE-TO-REMOVE")
        _, text = self.run_main("--json", "--fresh-days", "14", clock=clock)
        rep = json.loads(text)
        self.assertEqual((rep["fresh_days"], rep["cleaner_min_days"]), (14, 14))
        co = self.by_path(rep)[os.path.realpath(lane)]
        self.assertEqual(co["safety"], "NEEDS-REVIEW")
        self.assertTrue(co["safety_reasons"][0].startswith("fresh lane: age 10"), co["safety_reasons"])
        _, text = self.run_main("--json", "--fresh-days", "2.5", clock=self.aged(3))
        rep = json.loads(text)
        self.assertEqual((rep["fresh_days"], rep["cleaner_min_days"]), (2.5, 7))  # the cleaner floor never drops below 7
        self.assertEqual(self.by_path(rep)[os.path.realpath(lane)]["safety"], "SAFE-TO-REMOVE")
        _, text = self.run_main("--json", "--fresh-days", "0")  # fixed clock: age 0, and 0 is not below 0
        self.assertEqual(self.by_path(json.loads(text))[os.path.realpath(lane)]["safety"], "SAFE-TO-REMOVE")

    def test_a_bad_fresh_days_is_a_usage_error(self) -> None:
        for value in ("-1", "nan", "inf", "abc", "99999"):
            with self.subTest(value), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                doctor.main(["--fresh-days", value])
            self.assertEqual(cm.exception.code, 64)

    def test_help_documents_the_new_options_and_the_safety_classes(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as cm:
            doctor.main(["--help"])
        self.assertEqual(cm.exception.code, 0)
        text = " ".join(out.getvalue().split())
        for needle in ("--fresh-days", "--cleaner-list", "TOOL-CACHE", "fresh", "merged", "one per line"):
            self.assertIn(needle, text)
        for needle in ("--fresh-days", "--cleaner-list", "TOOL-CACHE", "cleaner_candidates", "fresh lane"):
            self.assertIn(needle, doctor.__doc__)


if __name__ == "__main__":
    unittest.main()
