#!/usr/bin/env python3
"""simple-git push:  local commits replayed through the relay's git-data tools.

The relay is faked at the same seam the sibling tests use (remote_call), but the fake is not a table of canned
answers:  it is backed by a real bare git repository and builds trees, commits and refs with git plumbing, the
way GitHub does for the relay.  So a push is judged by what ends up in that repository:  the tree the remote
branch points at, the parents of each commit, the message bytes.  The local side is a real temporary clone.
Arguments are checked against the shipped catalog's schemas, as a real call's are against the discovered ones.
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("simple_git_push_impl", str(HERE / "git"))
spec = importlib.util.spec_from_loader("simple_git_push_impl", loader)
G = importlib.util.module_from_spec(spec)
loader.exec_module(G)

BEARER = "seat-bearer-value-for-push-tests"
REPO = "Fixture-Org/Fixture-Repo"
TOOLS = {t["name"]: t for t in json.loads((HERE / "catalog.json").read_text())["tools"]}
READS = {"gr_wf_repo_get", "gr_wf_branch_get", "gr_wf_git_commit_get"}
# git with no user or system configuration, so a signing or hook setting on the host cannot change a test.
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
ME = [
    "-c",
    "user.name=Local Dev",
    "-c",
    "user.email=dev@example.invalid",
    "-c",
    "commit.gpgsign=false",
]


def git(cwd, *args, stdin: bytes | None = None, env: dict | None = None, ok=(0,)) -> str:
    full = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    full.update(GIT_ENV)
    full.update(env or {})
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args], input=stdin, capture_output=True, env=full, timeout=60
    )
    if proc.returncode not in ok:
        raise AssertionError(f"git {args} failed: {proc.stderr.decode()}")
    return proc.stdout.decode().strip()


class FakeRelay:
    """The relay's git-data tools over a real bare repository.  Every call is recorded."""

    def __init__(self, bare: Path, default: str):
        self.bare, self.default = bare, default
        self.calls: list = []
        self.before: dict = {}  # tool -> fn(args, nth call of that tool), may refuse
        self.after: dict = {}  # tool -> fn(args, structured) -> structured
        self.clock = 1_900_000_000
        self.secret_marker = None

    # -- what a test reads back
    def writes(self):
        return [c for c in self.calls if c[2]]

    def named(self, name):
        return [c[1] for c in self.calls if c[0] == name]

    def head(self, branch):
        return (
            git(self.bare, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", ok=(0, 1))
            or None
        )

    # -- the seam
    def refuse(self, name, code="relay_refused", reason=None, status=None, unknown=False):
        detail = (
            f" (reason={reason}" + (f", github_status={status}" if status else "") + ")"
            if reason
            else ""
        )
        sys.stderr.write(
            f"simple-git: {name} failed on the hosted server: {code}{detail}{G._REMOTE_UNKNOWN if unknown else ''}\n"
        )
        raise G.HostedToolExit(1, code, reason, status)

    def remote_call(self, name, args, write, bearer, deadline=None):
        assert bearer == BEARER
        assert write == (name not in READS), name
        assert args.get("identity") == "CLAUDE", (name, args)
        assert args.get("repo") == REPO, args
        self.calls.append((name, json.loads(json.dumps(args)), write))
        nth = len(self.named(name))
        if name in self.before:
            self.before[name](args, nth)
        structured = getattr(self, name.replace("gr_wf_", "t_"))(args)
        if name in self.after:
            structured = self.after[name](args, structured)
        return {"structured": structured, "text": ""}

    # -- the tools
    def t_repo_get(self, args):
        return {"repository": {"default_branch": self.default}}

    def t_branch_get(self, args):
        sha = self.head(args["branch"])
        if sha is None:
            self.refuse("gr_wf_branch_get", reason="github_refused", status=404)
        return {"branch": {"name": args["branch"], "commit": {"sha": sha}}}

    def t_git_commit_get(self, args):
        if git(self.bare, "cat-file", "-t", args["sha"], ok=(0, 128)) != "commit":
            self.refuse("gr_wf_git_commit_get", reason="github_refused", status=422)
        info = G._push_read_commit(str(self.bare), args["sha"])
        return {
            "commit": {
                "sha": args["sha"],
                "tree_sha": info["tree"],
                "parents": info["parents"],
                # GitHub's read drops the message's trailing newline (seen live, Nov 12 2026);  the object keeps it.
                "message": info["message"].rstrip("\n"),
            }
        }

    def t_git_tree_create(self, args):
        files = args["files"]
        texts = [f["text"] for f in files if "text" in f]
        cap = 100000 if args.get("allow_large_text") is True else 40000
        if not 1 <= len(files) <= 20 or sum(len(t.encode()) for t in texts) > cap:
            self.refuse("gr_wf_git_tree_create", reason="bad_request_content_too_large")
        if any(
            ("text" in f) == ("sha" in f) or f["path"].startswith(".github/workflows/")
            for f in files
        ):
            self.refuse("gr_wf_git_tree_create", reason="bad_request_invalid_files")
        if self.secret_marker and any(self.secret_marker in t for t in texts):
            self.refuse("gr_wf_git_tree_create", code="refused_secret")
        with tempfile.TemporaryDirectory() as tmp:
            index = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
            if git(self.bare, "cat-file", "-t", args["base_tree"], ok=(0, 128)) != "tree":
                self.refuse("gr_wf_git_tree_create", reason="github_refused", status=422)
            git(self.bare, "read-tree", args["base_tree"], env=index)
            for f in files:
                if "text" in f:
                    sha = git(self.bare, "hash-object", "-w", "--stdin", stdin=f["text"].encode())
                else:
                    sha = f["sha"]
                    if git(self.bare, "cat-file", "-t", sha, ok=(0, 128)) != "blob":
                        self.refuse("gr_wf_git_tree_create", reason="github_refused", status=404)
                git(
                    self.bare,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    f"{f['mode']},{sha},{f['path']}",
                    env=index,
                )
            tree = git(self.bare, "write-tree", env=index)
        return {"tree": {"sha": tree, "files": len(files)}}

    def t_git_commit_create(self, args):
        for sha, kind in [(args["tree_sha"], "tree")] + [(p, "commit") for p in args["parents"]]:
            if git(self.bare, "cat-file", "-t", sha, ok=(0, 128)) != kind:
                self.refuse("gr_wf_git_commit_create", reason="github_refused", status=422)
        self.clock += 60  # GitHub stamps its own author, committer and date:  never the local ones
        who = {
            "GIT_AUTHOR_NAME": "relay-app[bot]",
            "GIT_AUTHOR_EMAIL": "relay-app@example.invalid",
            "GIT_COMMITTER_NAME": "relay-app[bot]",
            "GIT_COMMITTER_EMAIL": "relay-app@example.invalid",
            "GIT_AUTHOR_DATE": f"{self.clock} +0000",
            "GIT_COMMITTER_DATE": f"{self.clock} +0000",
        }
        parents = [x for p in args["parents"] for x in ("-p", p)]
        proc = subprocess.run(
            ["git", "-C", str(self.bare), "commit-tree", args["tree_sha"], *parents],
            input=args["message"].encode(),
            capture_output=True,
            env={**os.environ, **GIT_ENV, **who},
            check=True,
        )
        return {
            "commit": {
                "sha": proc.stdout.decode().strip(),
                "tree_sha": args["tree_sha"],
                "parents": args["parents"],
            }
        }

    def t_branch_create(self, args):
        if (
            self.head(args["branch"]) is not None
            or git(self.bare, "cat-file", "-t", args["sha"], ok=(0, 128)) != "commit"
        ):
            self.refuse("gr_wf_branch_create", reason="github_refused", status=422)
        git(self.bare, "update-ref", f"refs/heads/{args['branch']}", args["sha"])
        return {"ref": f"refs/heads/{args['branch']}", "sha": args["sha"]}

    def t_branch_update(self, args):
        if args["branch"] == self.default:
            self.refuse("gr_wf_branch_update", reason="bad_request_default_branch_protected")
        current = self.head(args["branch"])
        if current != args["expected_sha"]:
            self.refuse("gr_wf_branch_update", reason="fence_mismatch")
        # force is false upstream:  only a fast-forward is accepted.
        if subprocess.run(
            ["git", "-C", str(self.bare), "merge-base", "--is-ancestor", current, args["sha"]],
            capture_output=True,
        ).returncode:
            self.refuse("gr_wf_branch_update", reason="github_refused", status=422)
        git(self.bare, "update-ref", f"refs/heads/{args['branch']}", args["sha"])
        return {"ref": f"refs/heads/{args['branch']}", "sha": args["sha"]}


@unittest.skipUnless(shutil.which("git"), "needs git")
class PushCase(unittest.TestCase):
    default = "trunk"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(os.path.realpath(tmp.name))
        self.bare, self.work = root / "remote.git", root / "work"
        git(root, "init", "-q", "--bare", "-b", self.default, str(self.bare))
        git(root, "init", "-q", "-b", self.default, str(self.work))
        self.write("README.md", "base\n")
        self.write("src/app.py", "print('one')\n")
        self.write("src/util.py", "def util():\n    return 1\n")
        self.write("docs/guide.md", "guide\n")
        self.write("docs/notes.md", "notes\n")
        self.base = self.commit("base")
        # Seed the remote with the base commit and learn its refs, as a clone would;  from here on the stored
        # URL is a GitHub one and nothing in the client may contact it.
        git(self.work, "remote", "add", "origin", str(self.bare))
        git(self.work, "push", "-q", "origin", self.default)
        git(self.work, "fetch", "-q", "origin")
        git(self.work, "config", "remote.origin.url", f"https://github.com/{REPO}.git")
        git(self.work, "checkout", "-q", "-b", "feature")
        self.relay = FakeRelay(self.bare, self.default)
        # The bearer is in the environment, as it is under the seat helper:  it must never reach local git.
        # The replay map goes to a state folder of this test's own, never the real one.
        self.state = root / "state"
        patch = mock.patch.dict(
            os.environ,
            {**GIT_ENV, G.REMOTE_BEARER_ENV: BEARER, "SIMPLE_GIT_STATE_DIR": str(self.state)},
        )
        patch.start()
        self.addCleanup(patch.stop)
        for key in [k for k in os.environ if k.startswith("GIT_") and k not in GIT_ENV]:
            os.environ.pop(key)

    # -- local repository helpers
    def write(self, path, data, mode=None):
        target = self.work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())
        if mode:
            target.chmod(mode)

    def commit(self, message, *extra):
        git(self.work, "add", "-A")
        git(self.work, *ME, "commit", "-q", "--allow-empty", "-m", message, *extra)
        return git(self.work, "rev-parse", "HEAD")

    def tree(self, rev, where=None):
        return git(where or self.work, "rev-parse", f"{rev}^{{tree}}")

    def raw_message(self, rev, where=None):
        raw = subprocess.run(
            ["git", "-C", str(where or self.work), "cat-file", "commit", rev],
            capture_output=True,
            check=True,
        ).stdout
        return raw.partition(b"\n\n")[2]

    def replay_map(self):
        path = self.state / "push-replay-map.json"
        return json.loads(path.read_text())["replayed"].get(REPO, {}) if path.exists() else None

    # -- the command
    def push(self, branch=None, remote="origin", dry_run=False, force=False, repo=None):
        a = types.SimpleNamespace(
            remote=remote,
            branch=branch,
            dir=str(self.work),
            repo=repo,
            dry_run=dry_run,
            force=force,
            force_with_lease=None,
            force_if_includes=False,
        )
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(G, "remote_call", self.relay.remote_call),
            mock.patch.object(G, "declared_seat", return_value=None),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = G.cmd_push(TOOLS, a, BEARER, "CLAUDE", time.monotonic() + 600)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def assert_nothing_written(self):
        self.assertEqual(self.relay.writes(), [])
        self.assertIsNone(self.relay.head("feature"))


class Replay(PushCase):
    def test_linear_history_creates_the_branch_with_the_same_trees_and_messages(self):
        self.write("src/app.py", "print('two')\n")
        first = self.commit("Change the app\n\nA body line.\n\nTrailer: kept")
        self.write("docs/new.md", "a new file\n")
        second = self.commit("Add a doc")
        self.write("src/util.py", "def util():\n    return 2\n")
        third = self.commit("Change util")
        code, out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        head = self.relay.head("feature")
        self.assertEqual(self.tree(head, self.bare), self.tree("HEAD"))
        remote = git(self.bare, "rev-list", "--reverse", f"{self.base}..{head}").split()
        self.assertEqual(len(remote), 3)
        for local, there in zip([first, second, third], remote):
            self.assertNotEqual(local, there)  # the relay stamps its own author and date
            self.assertEqual(self.tree(there, self.bare), self.tree(local))
            self.assertEqual(
                self.raw_message(there, self.bare), self.raw_message(local)
            )  # byte for byte
        self.assertEqual(git(self.bare, "rev-parse", f"{remote[0]}^"), self.base)
        self.assertEqual(git(self.bare, "log", "-1", "--format=%an", head), "relay-app[bot]")
        # The ref moved once, last, as a creation;  nothing was forced or updated.
        self.assertEqual([c[0] for c in self.relay.writes()][-1], "gr_wf_branch_create")
        self.assertEqual(self.relay.named("gr_wf_branch_update"), [])
        # The local repository:  untouched commits, and the pairs kept so that work can go on.
        self.assertEqual(git(self.work, "rev-parse", "HEAD"), third)
        self.assertEqual(self.replay_map(), dict(zip([first, second, third], remote)))
        self.assertIn(f"git reset --soft {head}", out)
        self.assertIn("fetch origin feature", out)
        self.assertIn("verified:", out)

    def test_a_second_push_continues_from_the_replay_map_without_repointing(self):
        self.write("a.txt", "a\n")
        first = self.commit("One")
        self.assertEqual(self.push()[0], 0)
        there = self.relay.head("feature")
        self.relay.calls.clear()
        self.write("b.txt", "b\n")
        second = self.commit("Two")
        code, out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        # Only the new commit was replayed, on top of the pair of its local parent, and the ref moved from
        # exactly the sha it was read at.
        self.assertEqual(
            [a["parents"] for a in self.relay.named("gr_wf_git_commit_create")], [[there]]
        )
        update = self.relay.named("gr_wf_branch_update")
        self.assertEqual(
            [(u["expected_sha"], u["sha"]) for u in update], [(there, self.relay.head("feature"))]
        )
        self.assertEqual(self.tree(self.relay.head("feature"), self.bare), self.tree("HEAD"))
        self.assertEqual(set(self.replay_map()), {first, second})
        # A third run has nothing to do and writes nothing.
        self.relay.calls.clear()
        code, out, err = self.push()
        self.assertEqual((code, self.relay.writes()), (0, []))
        self.assertIn("up-to-date", out)

    def test_a_push_after_fetching_the_replayed_commits_still_continues(self):
        # Once this clone has fetched the remote branch, the remote head is a commit it HAS, under an id that
        # is not in the local branch's history.  It is still the pair of a local ancestor (seen live, Nov 12).
        self.write("a.txt", "a\n")
        self.commit("One")
        self.assertEqual(self.push()[0], 0)
        there = self.relay.head("feature")
        git(self.work, "fetch", "-q", str(self.bare), "feature")
        self.assertEqual(git(self.work, "cat-file", "-t", there), "commit")
        self.relay.calls.clear()
        code, out, _err = self.push()
        self.assertEqual((code, self.relay.writes()), (0, []))
        self.assertIn("up-to-date", out)
        self.write("b.txt", "b\n")
        self.commit("Two")
        code, _out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            [a["parents"] for a in self.relay.named("gr_wf_git_commit_create")], [[there]]
        )
        # And after re-pointing the local branch onto the pushed commits, as the push output suggests.
        git(self.work, "fetch", "-q", str(self.bare), "feature")
        git(self.work, "reset", "-q", "--soft", self.relay.head("feature"))
        self.assertEqual(git(self.work, "status", "--porcelain"), "")
        self.write("c.txt", "c\n")
        self.commit("Three")
        self.relay.calls.clear()
        code, _out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.relay.named("gr_wf_git_commit_create")), 1)
        self.assertEqual(self.tree(self.relay.head("feature"), self.bare), self.tree("HEAD"))

    def test_a_merge_commit_keeps_both_parents(self):
        self.write("src/app.py", "print('feature side')\n")
        ours = self.commit("Feature side")
        git(self.work, "checkout", "-q", "-b", "side", self.base)
        self.write("docs/guide.md", "guide from the side\n")
        theirs = self.commit("Side change")
        git(self.work, "checkout", "-q", "feature")
        git(self.work, *ME, "merge", "-q", "--no-ff", "-m", "Merge side into feature", "side")
        merge = git(self.work, "rev-parse", "HEAD")
        code, out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        head = self.relay.head("feature")
        parents = git(self.bare, "rev-list", "--parents", "-1", head).split()[1:]
        pairs = self.replay_map()
        self.assertEqual(parents, [pairs[ours], pairs[theirs]])  # both parents, in order
        self.assertEqual(self.tree(head, self.bare), self.tree(merge))
        self.assertEqual(git(self.bare, "log", "-1", "--format=%s", parents[1]), "Side change")
        # The merge's tree is one parent's tree plus the other side's files:  one tree call, one file.
        merge_args = self.relay.named("gr_wf_git_tree_create")[-1]
        self.assertEqual(len(merge_args["files"]), 1)

    def test_an_executable_file_keeps_its_mode_and_a_mode_only_change_sends_no_content(self):
        self.write("bin/run.sh", "#!/bin/sh\necho run\n", mode=0o755)
        self.commit("Add a script")
        (self.work / "src" / "util.py").chmod(0o755)
        self.commit("Make util executable")
        code, _out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        listing = git(self.bare, "ls-tree", "-r", self.relay.head("feature"))
        self.assertIn(
            "100755 blob", [line for line in listing.splitlines() if line.endswith("bin/run.sh")][0]
        )
        self.assertIn(
            "100755 blob",
            [line for line in listing.splitlines() if line.endswith("src/util.py")][0],
        )
        self.assertIn(
            "100644 blob", [line for line in listing.splitlines() if line.endswith("src/app.py")][0]
        )
        chmod = self.relay.named("gr_wf_git_tree_create")[-1]["files"]
        self.assertEqual(
            [sorted(f) for f in chmod], [["mode", "path", "sha"]]
        )  # by sha:  the remote has the blob

    def test_unchanged_files_are_not_sent_again(self):
        self.write("src/app.py", "print('changed')\n")
        self.commit("Change one file of five")
        self.write("docs/copy-of-guide.md", "guide\n")  # the same content as docs/guide.md
        self.commit("Copy a file")
        code, _out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        trees = self.relay.named("gr_wf_git_tree_create")
        sent = [(f["path"], "text" in f) for call in trees for f in call["files"]]
        # One changed file out of five is the only content that travelled;  the copy went by sha.
        self.assertEqual(sent, [("src/app.py", True), ("docs/copy-of-guide.md", False)])
        self.assertEqual(self.relay.named("gr_wf_git_blob_create"), [])
        self.assertEqual(self.tree(self.relay.head("feature"), self.bare), self.tree("HEAD"))

    def test_a_large_commit_is_split_into_tree_calls_that_fit_the_relay_limits(self):
        for index in range(25):
            self.write(
                f"many/file-{index:02d}.txt", f"{index}\n" * 900
            )  # 25 files, about 2.5 KiB each
        self.write(
            "big.txt", "x" * 60000 + "\n"
        )  # over the 40000-byte class, under the 100000-byte one
        self.commit("Many files and a big one")
        code, _out, err = self.push()
        self.assertEqual(
            (code, err), (0, "")
        )  # the fake refuses a call over 20 files or over the byte cap
        calls = self.relay.named("gr_wf_git_tree_create")
        self.assertGreater(len(calls), 2)
        self.assertEqual([c.get("allow_large_text") for c in calls if len(c["files"]) == 1], [True])
        self.assertEqual(self.tree(self.relay.head("feature"), self.bare), self.tree("HEAD"))
        self.assertEqual(len(self.relay.named("gr_wf_git_commit_create")), 1)  # still one commit

    def test_commits_the_remote_already_has_are_not_replayed(self):
        # The remote got this commit some other way and this clone's remote-tracking refs do not know it.
        self.write("a.txt", "a\n")
        already = self.commit("Already there")
        git(self.work, "push", "-q", str(self.bare), "feature:refs/heads/other")
        self.write("b.txt", "b\n")
        self.commit("New")
        code, _out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            [a["parents"] for a in self.relay.named("gr_wf_git_commit_create")], [[already]]
        )


class Refusals(PushCase):
    def test_a_deletion_is_refused_by_name_before_any_write(self):
        git(self.work, "rm", "-q", "docs/notes.md")
        self.commit("Remove the notes")
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn("it deletes 'docs/notes.md'", err)
        self.assertIn("gr_wf_git_tree_create", err)
        self.assertIn("before anything was sent", err)
        self.assert_nothing_written()

    def test_a_binary_file_is_refused_by_name_before_any_write(self):
        self.write("logo.png", b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\xff\xfe")
        self.commit("Add a logo")
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn("'logo.png' is a binary file", err)
        self.assert_nothing_written()

    def test_a_symlink_a_submodule_and_a_workflow_file_are_refused_by_name(self):
        os.symlink("README.md", self.work / "link")
        self.commit("Add a symlink")
        git(self.work, "update-index", "--add", "--cacheinfo", f"160000,{self.base},vendor/lib")
        git(self.work, *ME, "commit", "-q", "-m", "Add a submodule")
        self.write(".github/workflows/ci.yml", "on: push\n")
        git(self.work, "add", ".github")
        git(self.work, *ME, "commit", "-q", "-m", "Add a workflow")
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn("'link' is a symlink (mode 120000)", err)
        self.assertIn("'vendor/lib' is a submodule (mode 160000)", err)
        self.assertIn("'.github/workflows/ci.yml' is a workflow path", err)
        self.assertEqual(
            len([line for line in err.splitlines() if "commit " in line]), 3
        )  # one line per cause
        self.assert_nothing_written()

    def test_an_oversize_file_is_refused_before_any_write_naming_the_path_and_the_limit(self):
        self.write("ok.txt", "fine\n")
        self.commit("A fine commit first")
        self.write("data/big.txt", "y" * (G.PUSH_LARGE_TEXT_BYTES + 1))
        self.commit("Add a big file")
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn(f"'data/big.txt' is {G.PUSH_LARGE_TEXT_BYTES + 1} bytes", err)
        self.assertIn(f"{G.PUSH_LARGE_TEXT_BYTES} bytes at most", err)
        self.assert_nothing_written()  # the earlier, acceptable commit was not sent either

    def test_the_default_branch_is_refused_whatever_it_is_called(self):
        git(self.work, "checkout", "-q", self.default)
        self.write("a.txt", "a\n")
        self.commit("On the default branch")
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn("refusing to push the default branch 'trunk'", err)
        self.assertEqual(self.relay.writes(), [])
        self.assertEqual(self.relay.head("trunk"), self.base)
        # The relay's answer decides, not the name:  the same branch name is pushable when it is not the default.
        self.relay.default = "production"
        self.relay.calls.clear()
        git(self.bare, "update-ref", "-d", "refs/heads/trunk")
        self.assertEqual(self.push("trunk")[0], 0)
        self.assertEqual(self.push("feature:production")[0], G.EXIT_USAGE)
        self.assertEqual(self.push("trunk:main")[0], 0)  # "main" is an ordinary branch here
        self.assertEqual(self.tree(self.relay.head("main"), self.bare), self.tree("trunk"))

    def test_a_ref_outside_refs_heads_is_refused(self):
        for spec in (
            "refs/tags/v1",
            "feature:refs/tags/v1",
            "HEAD:refs/notes/x",
            "feature:refs/pull/1/head",
        ):
            code, _out, err = self.push(spec)
            self.assertEqual(code, G.EXIT_USAGE, spec)
            self.assertIn("is not a branch", err)
        code, _out, err = self.push(":feature")
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn("never deletes", err)
        self.assertEqual(self.relay.calls, [])  # refused before any call at all

    def test_force_is_refused_in_every_spelling_before_any_credential_is_read(self):
        self.write("a.txt", "a\n")
        self.commit("One")
        code, _out, err = self.push(force=True)
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn("push never forces", err)
        code, _out, err = self.push("+feature")
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn("push never forces", err)
        self.assertEqual(self.relay.calls, [])
        for argv in (
            ["push", "--force"],
            ["push", "-f"],
            ["push", "--force-with-lease"],
            ["push", "origin", "+feature"],
        ):
            err = io.StringIO()
            with (
                mock.patch.object(
                    G, "remote_bearer", side_effect=AssertionError("a credential was read")
                ),
                contextlib.redirect_stderr(err),
                self.assertRaises(SystemExit) as caught,
            ):
                G.main(argv + ["--dir", str(self.work)])
            self.assertEqual(caught.exception.code, G.EXIT_USAGE, argv)
            self.assertIn("push never forces", err.getvalue())

    def test_a_stale_remote_head_is_refused_and_nothing_moves(self):
        # Someone else moved the remote branch to a commit this clone has never seen.
        theirs = self.relay.t_git_commit_create(
            {
                "tree_sha": self.tree(self.base),
                "parents": [self.base],
                "message": "Someone else's commit\n",
            }
        )["commit"]["sha"]
        git(self.bare, "update-ref", "refs/heads/feature", theirs)
        self.write("a.txt", "a\n")
        self.commit("Mine")
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertIn(f"the remote branch feature is at {theirs}", err)
        self.assertIn("fetch", err)
        self.assertEqual(self.relay.writes(), [])
        self.assertEqual(self.relay.head("feature"), theirs)

    def test_a_remote_that_moves_during_the_push_is_caught_by_the_expected_old_sha(self):
        git(
            self.work, "push", "-q", str(self.bare), "feature:refs/heads/feature"
        )  # the branch exists at base
        self.write("a.txt", "a\n")
        self.commit("Mine")
        raced = {}

        def race(args, nth):  # between this push's read of the head and its ref move
            raced["sha"] = self.relay.t_git_commit_create(
                {"tree_sha": self.tree(self.base), "parents": [self.base], "message": "Raced in\n"}
            )["commit"]["sha"]
            git(self.bare, "update-ref", "refs/heads/feature", raced["sha"])

        self.relay.before["gr_wf_branch_update"] = race
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertEqual(self.relay.named("gr_wf_branch_update")[0]["expected_sha"], self.base)
        self.assertIn("the remote branch moved while this push ran", err)
        self.assertIn("the branch feature was not moved", err)
        self.assertEqual(
            self.relay.head("feature"), raced["sha"]
        )  # theirs stands;  ours was not forced over it

    def test_a_relay_secret_scan_refusal_names_the_path_only(self):
        marker = "relay-flags-this-sentence"
        self.relay.secret_marker = marker
        self.write("config/settings.txt", f"value = {marker}\n")
        self.commit("Add settings")
        code, out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertIn(
            "the relay's secret scan refused the content of 'config/settings.txt' (content not shown)",
            err,
        )
        self.assertIn("the branch was not moved", err)
        self.assertNotIn(marker, out + err)
        self.assertIsNone(self.relay.head("feature"))
        self.assertEqual(self.relay.named("gr_wf_git_commit_create"), [])

    def test_the_client_secret_scan_refuses_by_path_before_any_write(self):
        token = (
            "ghp_" + "a1b2c3d4" * 5
        )  # assembled here so that this file holds no token-shaped literal
        self.write("deploy/env.txt", f"TOKEN={token}\n")
        self.commit("Add an env file")
        code, out, err = self.push()
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn("'deploy/env.txt' was flagged by the secret scan (content not shown)", err)
        self.assertNotIn(token, out + err)
        self.assertNotIn("a1b2c3d4", out + err)
        self.assert_nothing_written()

    def test_a_policy_denial_stops_before_the_ref_moves(self):
        self.write("a.txt", "a\n")
        self.commit("One")
        self.relay.before["gr_wf_git_tree_create"] = lambda args, nth: self.relay.refuse(
            "gr_wf_git_tree_create", reason="seat_policy"
        )
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertIn("this seat's relay policy does not allow that call", err)
        self.assertIn("the branch was not moved", err)
        self.assertIsNone(self.relay.head("feature"))


class Integrity(PushCase):
    def test_a_tree_mismatch_on_read_back_is_loud_and_says_the_branch_moved(self):
        self.write("a.txt", "a\n")
        tip = self.commit("One")
        wrong = self.tree(self.base)

        def lie(args, structured):  # only once the branch exists:  the read-back of the pushed tip
            if self.relay.named("gr_wf_branch_create"):
                structured["commit"]["tree_sha"] = wrong
            return structured

        self.relay.after["gr_wf_git_commit_get"] = lie
        code, out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertIn("TREE MISMATCH after the push", err)
        self.assertIn(wrong, err)
        self.assertIn(self.tree(tip), err)
        self.assertIn(f"The branch feature WAS moved to {self.relay.head('feature')}", err)
        self.assertNotIn("verified", out)

    def test_a_read_back_that_fails_still_says_the_branch_moved(self):
        self.write("a.txt", "a\n")
        self.commit("One")

        def down(args, nth):
            if self.relay.named("gr_wf_branch_create"):
                self.relay.refuse("gr_wf_git_commit_get", code="unavailable")

        self.relay.before["gr_wf_git_commit_get"] = down
        code, out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertIn("READ-BACK FAILED", err)
        self.assertIn(f"The branch feature WAS moved to {self.relay.head('feature')}", err)
        self.assertNotIn("verified", out)

    def test_a_ref_answer_that_did_not_reach_the_remote_is_caught_by_the_read_back(self):
        # The relay answers a repeated identical request from its outcome store without reaching GitHub.
        self.write("a.txt", "a\n")
        self.commit("One")
        self.relay.t_branch_create = lambda args: {
            "ref": f"refs/heads/{args['branch']}",
            "sha": args["sha"],
        }
        code, out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertIn("READ-BACK MISMATCH", err)
        self.assertIn("the branch reads as missing", err)
        self.assertNotIn("WAS moved", err)
        self.assertNotIn("verified", out)

    def test_a_tree_the_relay_built_differently_stops_before_the_commit_and_the_ref(self):
        self.write("a.txt", "a\n")
        self.commit("One")
        other = self.tree(self.base)
        self.relay.after["gr_wf_git_tree_create"] = lambda args, s: {
            "tree": {"sha": other, "files": s["tree"]["files"]}
        }
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertIn("TREE MISMATCH before the commit was created", err)
        self.assertEqual(self.relay.named("gr_wf_git_commit_create"), [])
        self.assertIsNone(self.relay.head("feature"))

    def test_a_rerun_after_a_failure_midway_finishes_without_duplicating_commits(self):
        shas = []
        for index in range(3):
            self.write(f"f{index}.txt", f"{index}\n")
            shas.append(self.commit(f"Commit {index}"))

        def fail_second(args, nth):
            if nth == 2:
                self.relay.refuse("gr_wf_git_commit_create", code="unavailable")

        self.relay.before["gr_wf_git_commit_create"] = fail_second
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertIn("push stopped at commit " + shas[1][:10], err)
        self.assertIn("the branch was not moved", err)
        self.assertIsNone(self.relay.head("feature"))
        first_pair = self.replay_map()
        self.assertEqual(list(first_pair), [shas[0]])  # the commit that did land is remembered
        # The second run:  the relay is healthy again.
        del self.relay.before["gr_wf_git_commit_create"]
        self.relay.calls.clear()
        code, _out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        created = self.relay.named("gr_wf_git_commit_create")
        self.assertEqual(
            len(created), 2
        )  # commits 1 and 2 only:  commit 0 was not created a second time
        self.assertEqual(created[0]["parents"], [first_pair[shas[0]]])
        head = self.relay.head("feature")
        chain = git(self.bare, "rev-list", "--reverse", f"{self.base}..{head}").split()
        self.assertEqual(chain, [self.replay_map()[s] for s in shas])  # exactly three, no duplicate
        self.assertEqual(self.tree(head, self.bare), self.tree("HEAD"))

    def test_an_unknown_ref_move_says_so_and_a_rerun_settles_it(self):
        self.write("a.txt", "a\n")
        self.commit("One")

        def lost_answer(args, nth):  # the move lands, the answer does not
            self.relay.t_branch_create(args)
            self.relay.refuse("gr_wf_branch_create", code="outcome_unknown", unknown=True)

        self.relay.before["gr_wf_branch_create"] = lost_answer
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_ERROR)
        self.assertIn("the branch feature may or may not have moved", err)
        del self.relay.before["gr_wf_branch_create"]
        self.relay.calls.clear()
        code, out, _err = self.push()
        self.assertEqual(
            (code, self.relay.writes()), (0, [])
        )  # it had landed:  nothing is sent again
        self.assertIn("up-to-date", out)

    def test_a_recorded_pair_the_remote_no_longer_has_is_replayed_not_trusted(self):
        self.write("a.txt", "a\n")
        local = self.commit("One")
        path = self.state / "push-replay-map.json"
        path.parent.mkdir()
        path.write_text(json.dumps({"version": 1, "replayed": {REPO: {local: "f" * 40}}}))
        code, _out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(self.relay.named("gr_wf_git_commit_create")), 1)
        self.assertEqual(self.replay_map()[local], self.relay.head("feature"))

    def test_a_dry_run_lists_the_work_and_sends_nothing(self):
        self.write("a.txt", "aaaa\n")
        first = self.commit("One")
        self.write("b.txt", "bb\n")
        second = self.commit("Two")
        code, out, err = self.push(dry_run=True)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("dry run:  nothing was sent", out)
        self.assertIn("replay 2 commit(s)", out)
        self.assertIn(first[:10], out)
        self.assertIn(second[:10], out)
        self.assertIn("upload 2 new blob(s), 8 bytes, in 2 tree call(s)", out)
        self.assertIn("create the branch feature", out)
        self.assert_nothing_written()
        self.assertIsNone(self.replay_map())  # no local state either
        self.assertFalse(self.state.exists())
        self.assertEqual(
            git(self.work, "status", "--porcelain", "--ignored"), ""
        )  # nothing written into the clone
        # With the branch already there, the dry run names the move and the expected old sha.
        git(self.work, "push", "-q", str(self.bare), f"{self.base}:refs/heads/feature")
        code, out, _err = self.push(dry_run=True)
        self.assertEqual(code, 0)
        self.assertIn(
            f"move the branch feature from {self.base} to the replayed tip (expected old sha {self.base}, never forced)",
            out,
        )
        self.assertEqual(self.relay.writes(), [])


class Identity(PushCase):
    def test_local_git_is_plumbing_only_and_never_sees_the_bearer(self):
        self.write("a.txt", "a\n")
        self.commit("One")
        seen = []
        real = subprocess.run

        def spy(argv, **kw):
            seen.append((list(argv), kw.get("env")))
            return real(argv, **kw)

        with mock.patch.object(G.subprocess, "run", spy):
            code, _out, err = self.push()
        self.assertEqual((code, err), (0, ""))
        mine = [(argv, env) for argv, env in seen if argv[1:3] == ["-C", str(self.work)]]
        self.assertTrue(mine)
        allowed = {
            "rev-parse",
            "symbolic-ref",
            "config",
            "rev-list",
            "cat-file",
            "diff-tree",
            "ls-tree",
            "merge-base",
        }
        for argv, env in mine:
            self.assertIn(argv[3], allowed, argv)
            # Booleans, never assertNotIn:  a failure must not print the environment it is checking.
            self.assertTrue(G.REMOTE_BEARER_ENV not in env, "the bearer variable reached local git")
            self.assertTrue(
                all(BEARER not in value for value in [*argv, *env.values()]),
                "the bearer reached local git",
            )

    def test_the_repository_comes_from_the_remote_url_or_the_flag(self):
        for url in (
            f"git@github.com:{REPO}.git",
            f"ssh://git@github.com/{REPO}",
            f"https://github.com/{REPO}",
        ):
            git(self.work, "config", "remote.origin.url", url)
            self.assertEqual(
                G._push_repo(types.SimpleNamespace(repo=None, remote="origin", dir=str(self.work))),
                REPO,
            )
        git(self.work, "config", "remote.origin.url", "https://example.invalid/a/b.git")
        code, _out, err = self.push()
        self.assertEqual(code, G.EXIT_USAGE)
        self.assertIn("pass --repo OWNER/NAME", err)
        self.assertEqual(self.relay.calls, [])
        self.write("a.txt", "a\n")
        self.commit("One")
        self.assertEqual(self.push(repo=REPO)[0], 0)

    def test_the_parser_offers_push_and_refuses_what_it_does_not_do(self):
        a = G.build_parser().parse_args(["push", "origin", "HEAD:topic", "--dry-run"])
        self.assertEqual(
            (a.cmd, a.remote, a.branch, a.dry_run, a.dir),
            ("push", "origin", "HEAD:topic", True, "."),
        )
        for extra in (["--mirror"], ["--all"], ["--delete", "x"], ["--tags"]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                G.build_parser().parse_args(["push", *extra])


if __name__ == "__main__":
    unittest.main()
