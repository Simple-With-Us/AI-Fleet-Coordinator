#!/usr/bin/env python3
"""simple-git's transfer and publish commands:  the bearer stays out of argv and output, and git options cannot be smuggled."""

from __future__ import annotations

import base64
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
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
loader = importlib.machinery.SourceFileLoader("simple_git_impl", str(HERE / "git"))
spec = importlib.util.spec_from_loader("simple_git_impl", loader)
G = importlib.util.module_from_spec(spec)
loader.exec_module(G)

BEARER = "seat-bearer-value"


def ns(**kw):
    return types.SimpleNamespace(**kw)


class Transfer(unittest.TestCase):
    def capture(self, fn, a):
        calls = []

        def popen(argv, **kw):
            calls.append((argv, kw))
            proc = mock.Mock()
            proc.wait.return_value = None
            proc.returncode = 0
            return proc

        with (
            mock.patch.object(G.subprocess, "Popen", popen),
            mock.patch.object(G, "_git_binary", return_value="git"),
            mock.patch.object(G.os, "makedirs"),
            mock.patch.object(G.os, "chmod"),
        ):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(fn(a, BEARER), 0)
        return calls

    def test_the_bearer_is_never_in_argv(self):
        calls = self.capture(
            G.cmd_clone,
            ns(repo="Simple-With-Us/Fleet-OPS", dir="out", depth=None, branch=None, partial=False),
        )
        argv, kw = calls[0]
        self.assertEqual(
            argv,
            [
                "git",
                "clone",
                "--",
                "https://github.com/Simple-With-Us/Fleet-OPS.git",
                os.path.abspath("out"),
            ],
        )
        self.assertNotIn(BEARER, " ".join(argv))
        env = kw["env"]
        config = [
            (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"])
            for i in range(int(env["GIT_CONFIG_COUNT"]))
        ]
        self.assertIn((f"url.{G.GIT_ROUTE_BASE}/git/.insteadOf", "https://github.com/"), config)
        self.assertIn(
            (f"http.{G.GIT_ROUTE_BASE}/.extraHeader", f"Authorization: Bearer {BEARER}"), config
        )
        self.assertIn(("credential.helper", ""), config)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_the_header_is_scoped_to_the_route_alone(self):
        env = G._transport_env(BEARER)
        keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
        self.assertNotIn("http.extraHeader", keys)

    def test_git_stderr_never_reaches_the_terminal(self):
        def popen(argv, **kw):
            self.assertEqual(kw["stderr"], G.subprocess.DEVNULL)
            proc = mock.Mock()
            proc.wait.return_value = None
            proc.returncode = 1
            return proc

        err = io.StringIO()
        with (
            mock.patch.object(G.subprocess, "Popen", popen),
            mock.patch.object(G, "_git_binary", return_value="git"),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(G._run_git(["fetch"], BEARER), 1)
        self.assertEqual(err.getvalue(), "git exited with status 1\n")
        self.assertNotIn(BEARER, err.getvalue())

    def test_an_interrupted_wait_terminates_and_reaps_the_child(self):
        proc = mock.Mock()
        proc.wait.side_effect = [InterruptedError("interrupted by signal 15"), None]

        with (
            mock.patch.object(G.subprocess, "Popen", return_value=proc),
            mock.patch.object(G, "_git_binary", return_value="git"),
            self.assertRaises(InterruptedError),
        ):
            G._run_git(["fetch"], BEARER)
        proc.terminate.assert_called_once_with()
        self.assertEqual(proc.wait.call_count, 2)

    def test_a_stalled_git_is_killed(self):
        proc = mock.Mock()
        proc.wait.side_effect = [G.subprocess.TimeoutExpired(cmd="git", timeout=1), None]
        proc.returncode = None

        with (
            mock.patch.object(G.subprocess, "Popen", return_value=proc),
            mock.patch.object(G, "_git_binary", return_value="git"),
            self.assertRaises(SystemExit),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            G._run_git(["fetch"], BEARER)
        proc.kill.assert_called_once_with()

    def test_clone_destination_permissions_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            # A fresh destination is created mode 0700.
            a = ns(repo="o/name", dir=str(root / "fresh"), depth=None, branch=None, partial=False)
            with (
                mock.patch.object(G, "_run_git", return_value=0) as run,
                mock.patch.object(G, "_repo_url", return_value="https://example/x.git"),
            ):
                self.assertEqual(G.cmd_clone(a, BEARER), 0)
            target = root / "fresh"
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            self.assertEqual(run.call_args[0][0][-1], str(target))
            # A symlinked destination is refused before git runs.
            link = root / "link"
            link.symlink_to(target)
            a = ns(repo="o/name", dir=str(link), depth=None, branch=None, partial=False)
            with (
                mock.patch.object(G, "_run_git", side_effect=AssertionError("git ran")),
                mock.patch.object(G, "_repo_url", return_value="https://example/x.git"),
                self.assertRaises(SystemExit),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                G.cmd_clone(a, BEARER)
            # An existing real directory is reused with its own permissions.
            reused = root / "reused"
            reused.mkdir(mode=0o755)
            a = ns(repo="o/name", dir=str(reused), depth=None, branch=None, partial=False)
            with (
                mock.patch.object(G, "_run_git", return_value=0),
                mock.patch.object(G, "_repo_url", return_value="https://example/x.git"),
            ):
                self.assertEqual(G.cmd_clone(a, BEARER), 0)
            self.assertEqual(reused.stat().st_mode & 0o777, 0o755)

    def test_an_option_cannot_be_passed_as_a_value(self):
        cases = (
            (
                G.cmd_clone,
                ns(repo="--upload-pack=x/y", dir=None, depth=None, branch=None, partial=False),
            ),
            (
                G.cmd_clone,
                ns(repo="a/b", dir="--upload-pack=x", depth=None, branch=None, partial=False),
            ),
            (
                G.cmd_clone,
                ns(repo="a/b", dir=None, depth=None, branch="--upload-pack=x", partial=False),
            ),
            (G.cmd_clone, ns(repo="a/../b", dir=None, depth=None, branch=None, partial=False)),
            (G.cmd_fetch, ns(dir=".", remote="--upload-pack=x", refspec=[], prune=False)),
            (G.cmd_fetch, ns(dir=".", remote="origin", refspec=["--upload-pack=x"], prune=False)),
            (G.cmd_pull, ns(dir=".", remote="--upload-pack=x")),
            (G.cmd_ls_remote, ns(repo="a/b", ref="--upload-pack=x")),
        )
        for fn, a in cases:
            with mock.patch.object(G.subprocess, "Popen", side_effect=AssertionError("git ran")):
                with (
                    self.assertRaises(SystemExit) as caught,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    fn(a, BEARER)
            self.assertEqual(caught.exception.code, G.EXIT_USAGE, a)

    def test_output_is_never_overwritten(self):
        with tempfile.NamedTemporaryFile() as existing:
            for fn in (G.cmd_bundle, G.cmd_archive):
                a = ns(repo="a/b", output=existing.name, ref="main")
                with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                    fn(a, BEARER)

    @unittest.skipUnless(shutil.which("git"), "needs git")
    def test_archive_and_bundle_with_real_git(self):
        """Real git, with the route swapped for a local repo."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(os.path.realpath(tmp))
            origin = root / "origin"
            identity = ["-c", "user.name=t", "-c", "user.email=t@e"]
            subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True, timeout=60)
            (origin / "a.txt").write_text("hello\n")
            subprocess.run(["git", "-C", str(origin), "add", "a.txt"], check=True, timeout=60)
            subprocess.run(
                ["git", *identity, "-C", str(origin), "commit", "-q", "-m", "one"], check=True, timeout=60
            )
            with (
                mock.patch.object(G, "_repo_url", return_value=str(origin)),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(
                    G.cmd_archive(
                        ns(repo="a/b", output=str(root / "out.tar.gz"), ref="main"), BEARER
                    ),
                    0,
                )
                self.assertEqual(
                    G.cmd_bundle(ns(repo="a/b", output=str(root / "out.bundle")), BEARER), 0
                )
            self.assertGreater((root / "out.tar.gz").stat().st_size, 0)
            listed = subprocess.run(
                ["tar", "tzf", str(root / "out.tar.gz")], check=True, capture_output=True, text=True
            ).stdout
            self.assertIn("a.txt", listed)
            checked = subprocess.run(
                ["git", "bundle", "list-heads", str(root / "out.bundle")],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("refs/heads/main", checked.stdout)


class Publish(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sent = []
        previous = os.environ.get("SIMPLE_GIT_STATE_DIR")
        os.environ["SIMPLE_GIT_STATE_DIR"] = str(self.root / "state")
        self.addCleanup(
            lambda: os.environ.__setitem__("SIMPLE_GIT_STATE_DIR", previous)
            if previous is not None
            else os.environ.pop("SIMPLE_GIT_STATE_DIR", None)
        )

    def file(self, name, size):
        path = self.root / name
        path.write_bytes(b"x" * size)
        return str(path)

    def write_store(self, store):
        """Seed durable outcome state the way production writes it:  same-directory temp file, fsync,
        atomic replace, fsync the directory - a torn fixture would not model durable persistence."""
        store_file = self.root / "state" / "publish-outcomes.json"
        store_file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=store_file.parent, prefix=store_file.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w") as handle:
            json.dump(store, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, store_file)
        dirfd = os.open(store_file.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)

    def publish_unmocked(self, specs):
        """publish, leaving validate_arguments to the caller's own patch."""
        tools = {
            name: {"name": name, "read": name == "gr_wf_branch_get", "schema": {"properties": {"identity": {}}}}
            for name in ("gr_wf_branch_get", "gr_wf_branch_create", "gr_wf_files_put")
        }

        def remote(name, args, write, bearer, deadline=None):
            self.sent.append((name, args, write))
            return {"structured": {"branch": {"commit": {"sha": "a" * 40}}, "commit": "b" * 40}}

        a = ns(
            repo="Simple-With-Us/CodeCaps", branch="claude/x", message="m", base=None, file=specs
        )
        with (
            mock.patch.object(G, "remote_call", remote),
            mock.patch.object(G, "declared_seat", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return G.cmd_publish(tools, a, BEARER, "CLAUDE", 0.0)

    def publish(self, specs, base=None, head="a" * 40):
        tools = {
            name: {"read": name == "gr_wf_branch_get", "schema": {"properties": {"identity": {}}}}
            for name in ("gr_wf_branch_get", "gr_wf_branch_create", "gr_wf_files_put")
        }
        counter = {"n": 0}

        def remote(name, args, write, bearer, deadline=None):
            self.sent.append((name, args, write))
            if name == "gr_wf_branch_get":
                return {"structured": {"branch": {"commit": {"sha": head}}}}
            if name == "gr_wf_files_put":
                counter["n"] += 1
                return {"structured": {"commit": f"{counter['n']:040x}"}}
            return {"structured": {}}

        a = ns(
            repo="Simple-With-Us/CodeCaps", branch="claude/x", message="m", base=base, file=specs
        )
        with (
            mock.patch.object(G, "remote_call", remote),
            mock.patch.object(G, "validate_arguments"),
            mock.patch.object(G, "declared_seat", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            return G.cmd_publish(tools, a, BEARER, "CLAUDE", 0.0)

    def puts(self):
        return [args for name, args, _ in self.sent if name == "gr_wf_files_put"]

    def test_a_160_kb_file_needs_no_inline_argument(self):
        path = self.file("MonitorModel.swift", 160 * 1024)
        self.assertEqual(self.publish([f"{path}=Sources/MonitorModel.swift"]), 0)
        put = self.puts()[0]
        self.assertEqual(put["expected_head_sha"], "a" * 40)
        self.assertEqual(put["identity"], "CLAUDE")
        self.assertEqual(put["files"][0]["path"], "Sources/MonitorModel.swift")
        self.assertEqual(len(base64.b64decode(put["files"][0]["content_b64"])), 160 * 1024)

    def test_files_past_one_commit_are_fenced_in_order(self):
        specs = [f"{self.file(f'f{i}', 1024 * 1024)}=f{i}" for i in range(9)]
        self.assertEqual(self.publish(specs), 0)
        puts = self.puts()
        self.assertEqual([len(p["files"]) for p in puts], [4, 4, 1])
        self.assertEqual(puts[0]["expected_head_sha"], "a" * 40)
        self.assertEqual(puts[1]["expected_head_sha"], f"{1:040x}")
        self.assertEqual(puts[2]["expected_head_sha"], f"{2:040x}")
        self.assertIn("part 1 of 3", puts[0]["message"])

    def test_more_than_twenty_small_files_split(self):
        specs = [f"{self.file(f's{i}', 10)}=s{i}" for i in range(21)]
        self.assertEqual(self.publish(specs), 0)
        self.assertEqual([len(p["files"]) for p in self.puts()], [20, 1])

    def test_base_creates_the_branch_first(self):
        self.assertEqual(self.publish([f"{self.file('a', 1)}=a"], base="main"), 0)
        names = [name for name, _, _ in self.sent]
        self.assertEqual(
            names,
            ["gr_wf_branch_get", "gr_wf_branch_create", "gr_wf_branch_get", "gr_wf_files_put"],
        )

    def test_a_problem_in_a_later_batch_stops_the_first_commit(self):
        # The refusal comes from the production pre-flight (path shape mirror), not a stricter mock.
        specs = [f"{self.file(f'f{i}', 1024 * 1024)}=f{i}" for i in range(4)]
        specs.append(f"{self.file('bad', 10)}=../escape")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.publish_unmocked(specs)
        self.assertEqual(self.sent, [])

    def test_a_workflow_path_is_refused_before_any_commit(self):
        good = self.file("good", 10)
        bad = self.file("wf", 10)
        specs = [f"{good}=ok.txt", f"{bad}=.github/workflows/x.yml"]
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.publish_unmocked(specs)
        self.assertEqual(self.sent, [])

    def test_a_local_path_containing_an_equals_sign(self):
        folder = self.root / "build=2"
        folder.mkdir()
        local = folder / "out.bin"
        local.write_bytes(b"data")
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.root)
        # The whole spec is the local file now, not a truncation at the first "=";  its "=" is no valid repo
        # path for the relay, so publishing it takes an explicit mapping, split on the LAST "=".
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.publish(["build=2/out.bin"])
        self.assertEqual(self.sent, [])
        self.assertEqual(self.publish(["build=2/out.bin=dist/out.bin"]), 0)
        self.assertEqual(self.puts()[0]["files"][0]["path"], "dist/out.bin")

    def test_publish_totals_are_capped(self):
        old_files, old_bytes = G.PUBLISH_MAX_FILES, G.PUBLISH_MAX_TOTAL_BYTES
        self.addCleanup(setattr, G, "PUBLISH_MAX_FILES", old_files)
        self.addCleanup(setattr, G, "PUBLISH_MAX_TOTAL_BYTES", old_bytes)
        G.PUBLISH_MAX_FILES = 3
        specs = [f"{self.file(f'c{i}', 10)}=c{i}" for i in range(4)]
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.publish(specs)
        G.PUBLISH_MAX_FILES, G.PUBLISH_MAX_TOTAL_BYTES = old_files, 30
        specs = [f"{self.file(f'd{i}', 20)}=d{i}" for i in range(2)]
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.publish(specs)
        self.assertEqual(self.sent, [])

    def test_a_rerun_skips_batches_already_committed(self):
        specs = [f"{self.file(f'f{i}', 1024 * 1024)}=f{i}" for i in range(9)]
        calls = {"n": 0}

        def flaky(name, args, write, bearer, deadline=None):
            self.sent.append((name, args, write))
            if name == "gr_wf_branch_get":
                return {"structured": {"branch": {"commit": {"sha": "a" * 40}}}}
            if name == "gr_wf_files_put":
                calls["n"] += 1
                if calls["n"] == 2:
                    raise SystemExit(G.EXIT_ERROR)  # a definite refusal: nothing written
                return {"structured": {"commit": f"{calls['n']:040x}"}}
            return {"structured": {}}

        tools = {
            name: {"name": name, "read": name == "gr_wf_branch_get", "schema": {"properties": {"identity": {}}}}
            for name in ("gr_wf_branch_get", "gr_wf_branch_create", "gr_wf_files_put")
        }
        a = ns(repo="Simple-With-Us/CodeCaps", branch="claude/x", message="m", base=None, file=specs)
        with (
            mock.patch.object(G, "remote_call", flaky),
            mock.patch.object(G, "validate_arguments"),
            mock.patch.object(G, "declared_seat", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            G.cmd_publish(tools, a, BEARER, "CLAUDE", 0.0)
        # Rerun: batch 1 is skipped from the done-set, batches 2 and 3 are sent.
        self.sent.clear()
        self.assertEqual(self.publish(specs), 0)
        self.assertEqual([len(p["files"]) for p in self.puts()], [4, 1])
        self.assertEqual(self.puts()[0]["expected_head_sha"], "a" * 40)

    def test_an_unknown_batch_is_never_replayed(self):
        specs = [f"{self.file('u', 10)}=u"]
        store_file = self.root / "state" / "publish-outcomes.json"
        self.assertEqual(self.publish(specs), 0)
        store = json.loads(store_file.read_text())
        key = next(iter(store))
        store[key]["batches"]["1"] = "unknown"
        self.write_store(store)
        self.sent.clear()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.publish(specs)
        self.assertEqual(self.puts(), [])

    def test_files_already_on_the_branch_are_not_resent(self):
        local = self.file("same", 10)
        data = (self.root / "same").read_bytes()
        blob = G._git_blob_sha(data)

        def remote(name, args, write, bearer, deadline=None):
            self.sent.append((name, args, write))
            if name == "gr_wf_branch_get":
                return {"structured": {"branch": {"commit": {"sha": "a" * 40}}}}
            if name == "gr_wf_tree":
                return {"structured": {"tree": [{"type": "blob", "path": "same", "sha": blob}]}}
            return {"structured": {"commit": "b" * 40}}

        tools = {
            name: {"name": name, "read": True, "schema": {"properties": {"identity": {}}}}
            for name in ("gr_wf_branch_get", "gr_wf_tree", "gr_wf_files_put")
        }
        a = ns(repo="Simple-With-Us/CodeCaps", branch="claude/x", message="m", base=None, file=[f"{local}=same"])
        with (
            mock.patch.object(G, "remote_call", remote),
            mock.patch.object(G, "validate_arguments"),
            mock.patch.object(G, "declared_seat", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(G.cmd_publish(tools, a, BEARER, "CLAUDE", 0.0), 0)
        self.assertEqual(self.puts(), [])

    def _writer_source(self, key, batches):
        return f"""
import importlib.machinery, importlib.util, os, sys
os.environ["SIMPLE_GIT_STATE_DIR"] = {str(self.root / 'state')!r}
loader = importlib.machinery.SourceFileLoader("g", {str(HERE / 'git')!r})
spec = importlib.util.spec_from_loader("g", loader)
G = importlib.util.module_from_spec(spec)
loader.exec_module(G)
lock = G._publish_store_lock()
try:
    store = G._publish_store_load()
    state = store.setdefault({key!r}, {{"batches": {{}}}})
    for number, outcome in {batches!r}.items():
        prior = state["batches"].get(number)
        if prior == "unknown":
            sys.exit(3)  # unknown is terminal:  never overwritten, never replayed
        state["batches"][number] = outcome
    G._publish_store_save(store)
finally:
    lock.close()
"""

    def test_concurrent_independent_writers_lose_no_records(self):
        # Independent CLI processes, not threads:  every writer's record survives.
        procs = [
            subprocess.Popen(
                ["python3", "-c", self._writer_source(f"op{n}", {"1": "ok"})],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for n in range(6)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(timeout=60), 0)
        store = G._publish_store_load()
        self.assertEqual(
            {k: v["batches"] for k, v in store.items()},
            {f"op{n}": {"1": "ok"} for n in range(6)},
        )

    def test_a_concurrent_ok_cannot_overwrite_an_unknown(self):
        # The same operation's unknown batch is terminal across processes.
        source = self._writer_source("shared-op", {"1": "unknown"})
        self.assertEqual(
            subprocess.run(["python3", "-c", source], capture_output=True, timeout=60).returncode, 0
        )
        source = self._writer_source("shared-op", {"1": "ok"})
        done = subprocess.run(["python3", "-c", source], capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 3)
        store = G._publish_store_load()
        self.assertEqual(store["shared-op"]["batches"]["1"], "unknown")

    def test_the_store_save_is_an_atomic_replace(self):
        import threading

        errors = []

        def hammer(n):
            try:
                for i in range(30):
                    G._publish_store_save({f"key{n}": {"batches": {str(i): "ok"}}})
            except Exception as exc:  # noqa: BLE001 - any failure is a test failure
                errors.append(exc)

        threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        # Every save is an atomic replace:  the file is always one valid JSON document.
        self.assertIsInstance(G._publish_store_load(), dict)

    def test_base64_that_looks_like_a_token_is_not_refused(self):
        # This decoded content has no secret, but its base64 is one long run of letters and digits.
        data = bytes(range(32, 127)) * 40
        args = {"files": [{"path": "a.bin", "content_b64": base64.b64encode(data).decode("ascii")}]}
        self.assertIsNone(G._scan_arguments(args, BEARER))

    def test_a_secret_inside_the_decoded_content_is_still_refused(self):
        token = "ghp_" + "a1b2c3d4" * 5
        args = {
            "files": [
                {
                    "path": "a.txt",
                    "content_b64": base64.b64encode(f"key = {token}\n".encode()).decode("ascii"),
                }
            ]
        }
        self.assertIn("content_b64", G._scan_arguments(args, BEARER))

    def test_the_bearer_inside_the_decoded_content_is_still_refused(self):
        args = {
            "files": [
                {"path": "a.txt", "content_b64": base64.b64encode(BEARER.encode()).decode("ascii")}
            ]
        }
        self.assertIn("content_b64", G._scan_arguments(args, BEARER))

    def test_nothing_is_sent_when_any_file_is_bad(self):
        good = self.file("good", 1)
        big = self.file("big", G.PUBLISH_MAX_FILE_BYTES + 1)
        link = self.root / "link"
        link.symlink_to(good)
        empty = self.file("empty", 0)
        for bad in (big, empty, str(self.root / "missing"), str(link), f"{good}=dup"):
            specs = [f"{good}=dup", bad] if bad.endswith("=dup") else [good, bad]
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.publish(specs)
        self.assertEqual(self.sent, [])


    def test_a_corrupt_store_is_quarantined_and_rebuilt(self):
        specs = [f"{self.file('c', 10)}=c"]
        self.assertEqual(self.publish(specs), 0)
        store_file = self.root / "state" / "publish-outcomes.json"
        store_file.write_bytes(b"{not json")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.publish(specs), 0)
        quarantined = list((self.root / "state").glob("publish-outcomes.json.corrupt-*"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b"{not json")
        self.assertIn("quarantined", err.getvalue())
        rebuilt = json.loads(store_file.read_text())
        self.assertEqual(rebuilt[next(iter(rebuilt))]["batches"]["1"], "ok")

    def test_an_unreadable_store_fails_closed(self):
        (self.root / "state").mkdir(parents=True)
        (self.root / "state" / "publish-outcomes.json").mkdir()  # a directory: open() raises OSError
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            self.publish([f"{self.file('u', 10)}=u"])
        self.assertEqual(caught.exception.code, G.EXIT_ERROR)
        self.assertEqual(self.puts(), [])

    def test_a_symlinked_state_directory_is_refused(self):
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, self.root / "state")
        with self.assertRaises(SystemExit) as caught, contextlib.redirect_stderr(io.StringIO()):
            self.publish([f"{self.file('s', 10)}=s"])
        self.assertEqual(caught.exception.code, G.EXIT_USAGE)
        self.assertEqual(self.sent, [])

    def test_a_refused_branch_creation_stops_the_publish(self):
        tools = {
            name: {"read": name == "gr_wf_branch_get", "schema": {"properties": {"identity": {}}}}
            for name in ("gr_wf_branch_get", "gr_wf_branch_create", "gr_wf_files_put")
        }

        def remote(name, args, write, bearer, deadline=None):
            self.sent.append((name, args, write))
            if name == "gr_wf_branch_get":
                return {"structured": {"branch": {"commit": {"sha": "a" * 40}}}}
            if name == "gr_wf_branch_create":
                raise SystemExit(G.EXIT_ERROR)  # a definite refusal: nothing created
            return {"structured": {"commit": "b" * 40}}

        a = ns(
            repo="Simple-With-Us/CodeCaps",
            branch="claude/x",
            message="m",
            base="main",
            file=[f"{self.file('r', 10)}=r"],
        )
        with (
            mock.patch.object(G, "remote_call", remote),
            mock.patch.object(G, "validate_arguments"),
            mock.patch.object(G, "declared_seat", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit),
        ):
            G.cmd_publish(tools, a, BEARER, "CLAUDE", 0.0)
        self.assertEqual(self.puts(), [])
        store = json.loads((self.root / "state" / "publish-outcomes.json").read_text())
        self.assertEqual(store[next(iter(store))]["batches"]["branch"], "refused")

    def test_a_base_rerun_fences_on_the_current_branch_head(self):
        specs = [f"{self.file('h', 10)}=h"]
        key = G._publish_key("Simple-With-Us/CodeCaps", "claude/x", "main", "m", [("h", b"x" * 10)])
        self.write_store({key: {"batches": {"branch": "ok"}}})
        heads = {"main": "a" * 40, "claude/x": "c" * 40}
        tools = {
            name: {"read": name == "gr_wf_branch_get", "schema": {"properties": {"identity": {}}}}
            for name in ("gr_wf_branch_get", "gr_wf_branch_create", "gr_wf_files_put")
        }

        def remote(name, args, write, bearer, deadline=None):
            self.sent.append((name, args, write))
            if name == "gr_wf_branch_get":
                return {"structured": {"branch": {"commit": {"sha": heads[args["branch"]]}}}}
            return {"structured": {"commit": "d" * 40}}

        a = ns(
            repo="Simple-With-Us/CodeCaps",
            branch="claude/x",
            message="m",
            base="main",
            file=specs,
        )
        with (
            mock.patch.object(G, "remote_call", remote),
            mock.patch.object(G, "validate_arguments"),
            mock.patch.object(G, "declared_seat", return_value=None),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(G.cmd_publish(tools, a, BEARER, "CLAUDE", 0.0), 0)
        self.assertEqual(self.puts()[0]["expected_head_sha"], "c" * 40)
        self.assertNotIn(("gr_wf_branch_create",), [(n,) for n, _, _ in self.sent])

    def test_the_uncertainty_note_is_classified_unknown(self):
        def write_that_may_have_landed():
            sys.stderr.write(f"hosted: http 502{G._REMOTE_UNKNOWN}")
            raise SystemExit(G.EXIT_ERROR)

        with contextlib.redirect_stderr(io.StringIO()):
            outcome, _ = G._outcome_of(write_that_may_have_landed)
        self.assertEqual(outcome, "unknown")

    def test_a_missing_git_binary_exits_127(self):
        with (
            mock.patch.object(G.subprocess, "Popen", side_effect=FileNotFoundError()),
            mock.patch.object(G, "_git_binary", return_value="/nonexistent/git"),
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as caught,
        ):
            G._run_git(["status"], BEARER)
        self.assertEqual(caught.exception.code, 127)


class Parser(unittest.TestCase):
    def test_new_commands_parse(self):
        p = G.build_parser()
        self.assertEqual(p.parse_args(["clone", "a/b", "dir", "--depth", "1"]).depth, 1)
        self.assertEqual(p.parse_args(["fetch", "origin", "main", "--dir", "x"]).refspec, ["main"])
        self.assertEqual(p.parse_args(["archive", "a/b", "o.zip", "--ref", "v1"]).ref, "v1")
        self.assertEqual(
            p.parse_args(
                ["publish", "--repo", "a/b", "--branch", "c", "--message", "m", "--file", "f"]
            ).file,
            ["f"],
        )


if __name__ == "__main__":
    unittest.main()
