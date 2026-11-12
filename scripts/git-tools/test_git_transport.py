#!/usr/bin/env python3
"""simple-git's transfer and publish commands:  the bearer stays out of argv and output, and git options cannot be smuggled."""

from __future__ import annotations

import base64
import contextlib
import importlib.machinery
import importlib.util
import io
import os
import shutil
import subprocess
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

        def run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, stderr="")

        with (
            mock.patch.object(G.subprocess, "run", run),
            mock.patch.object(G, "_git_binary", return_value="git"),
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
            argv, ["git", "clone", "--", "https://github.com/Simple-With-Us/Fleet-OPS.git", "out"]
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

    def test_stderr_has_the_bearer_taken_out(self):
        def run(argv, **kw):
            return subprocess.CompletedProcess(argv, 1, stderr=f"fatal: header {BEARER} refused\n")

        err = io.StringIO()
        with (
            mock.patch.object(G.subprocess, "run", run),
            mock.patch.object(G, "_git_binary", return_value="git"),
            contextlib.redirect_stderr(err),
        ):
            self.assertEqual(G._run_git(["fetch"], BEARER), 1)
        self.assertNotIn(BEARER, err.getvalue())
        self.assertIn("[bearer]", err.getvalue())

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
            with mock.patch.object(G.subprocess, "run", side_effect=AssertionError("git ran")):
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
            subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
            (origin / "a.txt").write_text("hello\n")
            subprocess.run(["git", "-C", str(origin), "add", "a.txt"], check=True)
            subprocess.run(
                ["git", *identity, "-C", str(origin), "commit", "-q", "-m", "one"], check=True
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

    def file(self, name, size):
        path = self.root / name
        path.write_bytes(b"x" * size)
        return str(path)

    def publish_unmocked(self, specs):
        """publish, leaving validate_arguments to the caller's own patch."""
        tools = {
            name: {"read": name == "gr_wf_branch_get", "schema": {"properties": {"identity": {}}}}
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
        self.assertEqual(names, ["gr_wf_branch_get", "gr_wf_branch_create", "gr_wf_files_put"])

    def test_a_problem_in_a_later_batch_stops_the_first_commit(self):
        specs = [f"{self.file(f'f{i}', 1024 * 1024)}=f{i}" for i in range(4)]
        specs.append(f"{self.file('bad', 10)}=../escape")

        def refuse_path(entry, args, deadline=None):
            if any(".." in f["path"] for f in args["files"]):
                raise SystemExit(G.EXIT_USAGE)

        with mock.patch.object(G, "validate_arguments", refuse_path):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.publish_unmocked(specs)
        self.assertEqual(self.sent, [])

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
