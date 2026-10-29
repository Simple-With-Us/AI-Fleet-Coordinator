#!/usr/bin/env python3
"""Offline source/transport tests.  No SSH, credentials, installed configs, or models."""

from __future__ import annotations

import base64
import fcntl
import importlib.util
import io
import json
import os
import shlex
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "fleet_cli_sync", Path(__file__).with_name("fleet-cli-sync.py")
)
S = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(S)

POLICY = """import hashlib,json
MAX_BUNDLE_BYTES=8*1024*1024
def allowed_path(path):
    if path in {"scripts/simple-chat","scripts/fleet-cli-release.py","fleet-apps.json","docs/protocols/agent-sync-partition.toml","scripts/linear-board/board","scripts/linear-board/board-ids.txt"}: return True
    parts=path.split("/")
    if len(parts)==3 and parts[:2]==["scripts","agent_sync"] and parts[2].endswith(".py"): return True
    if len(parts)==4 and parts[:3]==["scripts","agent_sync","mcp"] and (parts[3].endswith(".py") or parts[3] in {"tools.json","fixtures.jsonl"}): return True
    return path=="scripts/agent_sync/wake/wake-schema.json"
def manifest_digest(files):
    value={path:{"mode":entry["mode"],"sha256":entry["sha256"]} for path,entry in files.items()}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()
"""


class Transport(S.Runner):
    def __init__(self, mode="ok"):
        super().__init__()
        self.mode = mode
        self.calls = []

    def run(self, argv, **kwargs):
        if argv[0] != "ssh":
            return super().run(argv, **kwargs)
        self.calls.append((argv, kwargs))
        if self.mode == "timeout":
            raise S.SyncError("command_timeout")
        if self.mode == "failure":
            return subprocess.CompletedProcess(argv, 1, b"", b"NEVER_PRINT_THIS_CREDENTIAL")
        bundle = json.loads(kwargs["data"])
        reply = {
            "status": "unchanged" if self.mode == "unchanged" else "activated",
            "source_commit": bundle["source_commit"],
            "bundle_sha256": bundle["bundle_sha256"],
        }
        if self.mode == "paused":
            reply["status"] = "paused"
        if self.mode == "wrong_digest":
            reply["bundle_sha256"] = "0" * 64
        if self.mode == "wrong_commit":
            reply["source_commit"] = "0" * 40
        if self.mode == "malformed":
            return subprocess.CompletedProcess(argv, 0, b"not json", b"")
        return subprocess.CompletedProcess(argv, 0, json.dumps(reply).encode(), b"")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="fleet-cli-fixture-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"
        self.git("init", "-q")
        self.git(
            "remote", "add", "origin", "git@github.com:Simple-With-Us/AI-Fleet-Coordinator.git"
        )
        self.put("scripts/fleet-cli-release.py", POLICY)
        self.put(
            "scripts/simple-chat", "#!/usr/bin/env python3\nprint('fixture')\n", executable=True
        )
        self.put("scripts/agent_sync/core.py", "VALUE = 1\n")
        self.put("fleet-apps.json", "{}\n")
        self.put("docs/private-fixture.md", "PRIVATE_FIXTURE_MUST_NOT_SHIP\n")
        self.commit()

    def git(self, *argv):
        result = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-C",
                str(self.repo),
                *argv,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            env=S.git_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout.decode().strip()

    def put(self, name, text, *, executable=False):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(0o755 if executable else 0o644)

    def commit(self, *, published=True):
        self.git("add", "--all")
        self.git("commit", "-qm", "Fixture change")
        if published:
            self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def assert_code(self, code, callable, *args, **kwargs):
        with self.assertRaises(S.SyncError) as error:
            callable(*args, **kwargs)
        self.assertEqual(error.exception.code, code)


class SourceTests(Fixture):
    def test_real_receiver_policy_import_and_manifest_contract(self):
        actual = Path(__file__).with_name("fleet-cli-release.py")
        if not actual.is_file():
            self.skipTest("Receiver integration runs from the owned source lane")
        self.put("scripts/fleet-cli-release.py", actual.read_text())
        self.commit()
        bundle, _raw, receiver = S.build_bundle(self.repo)
        policy = S.receiver_policy(receiver.encode())
        self.assertEqual(bundle["bundle_sha256"], policy.manifest_digest(bundle["files"]))
        self.assertTrue(policy.allowed_path("scripts/agent_sync/core.py"))
        self.assertNotIn("docs/private-fixture.md", bundle["files"])

    def test_only_published_allowlisted_blobs_and_regular_modes_ship(self):
        bundle, raw, receiver = S.build_bundle(self.repo)
        self.assertEqual(
            set(bundle["files"]),
            {
                "scripts/fleet-cli-release.py",
                "scripts/simple-chat",
                "scripts/agent_sync/core.py",
                "fleet-apps.json",
            },
        )
        self.assertEqual(bundle["files"]["scripts/simple-chat"]["mode"], 0o755)
        self.assertEqual(bundle["files"]["fleet-apps.json"]["mode"], 0o644)
        self.assertEqual(bundle["source_commit"], self.git("rev-parse", "HEAD"))
        self.assertNotIn(b"PRIVATE_FIXTURE_MUST_NOT_SHIP", raw)
        self.assertEqual(receiver, POLICY)

    def test_untracked_allowlisted_file_and_secret_are_ignored(self):
        before = S.build_bundle(self.repo)[1]
        self.put("scripts/agent_sync/untracked.py", "SHOULD_NOT_SHIP\n")
        self.put(".env", "SECRET_FIXTURE\n")
        self.assertEqual(S.build_bundle(self.repo)[1], before)

    def test_tracked_staged_and_unstaged_changes_are_refused(self):
        self.put("scripts/agent_sync/core.py", "VALUE = 2\n")
        self.assert_code("source_tracked_dirty", S.build_bundle, self.repo)
        self.git("add", "scripts/agent_sync/core.py")
        self.assert_code("source_tracked_dirty", S.build_bundle, self.repo)

    def test_unpublished_head_is_refused(self):
        self.put("scripts/agent_sync/core.py", "VALUE = 2\n")
        self.commit(published=False)
        self.assert_code("source_not_published", S.build_bundle, self.repo)

    def test_wrong_origin_is_refused_without_echoing_it(self):
        self.git("remote", "set-url", "origin", "https://credentials@example.invalid/secret")
        self.assert_code("source_origin_refused", S.build_bundle, self.repo)

    def test_allowlisted_symlink_and_symlink_receiver_are_refused(self):
        (self.repo / "scripts/agent_sync/link.py").symlink_to("../../docs/private-fixture.md")
        self.commit()
        self.assert_code("source_entry_refused", S.build_bundle, self.repo)
        self.git("rm", "scripts/agent_sync/link.py", "scripts/fleet-cli-release.py")
        (self.repo / "scripts/fleet-cli-release.py").symlink_to("../docs/private-fixture.md")
        self.commit()
        self.assert_code("receiver_blob_refused", S.build_bundle, self.repo)

    def test_export_attributes_cannot_hide_or_rewrite_payload(self):
        original = "$Format:%H$\n"
        self.put("scripts/agent_sync/substitute.py", original)
        self.put(
            ".gitattributes",
            "scripts/agent_sync/core.py export-ignore\nscripts/agent_sync/substitute.py export-subst\n",
        )
        self.commit()
        files = S.build_bundle(self.repo)[0]["files"]
        self.assertIn("scripts/agent_sync/core.py", files)
        self.assertEqual(
            base64.b64decode(files["scripts/agent_sync/substitute.py"]["data"]), original.encode()
        )

    def test_git_environment_cannot_redirect_source_or_replace_objects(self):
        with patch.dict(
            os.environ,
            {
                "GIT_DIR": "/missing-fixture",
                "GIT_WORK_TREE": "/missing-fixture",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.worktree",
                "GIT_CONFIG_VALUE_0": "/missing-fixture",
            },
        ):
            self.assertEqual(
                S.build_bundle(self.repo)[0]["source_commit"], self.git("rev-parse", "HEAD")
            )

    def test_file_count_and_encoded_bundle_size_are_bounded(self):
        for number in range(S.MAX_FILES):
            self.put(f"scripts/agent_sync/f{number}.py", "x\n")
        self.commit()
        self.assert_code("bundle_too_many_files", S.build_bundle, self.repo)

    def test_file_count_exact_limit_builds_but_one_more_is_refused(self):
        count = len(S.build_bundle(self.repo)[0]["files"])
        for number in range(S.MAX_FILES - count):
            self.put(f"scripts/agent_sync/boundary_{number}.py", "# boundary\n")
        self.commit()
        self.assertEqual(len(S.build_bundle(self.repo)[0]["files"]), S.MAX_FILES)
        self.put("scripts/agent_sync/one_too_many.py", "# boundary\n")
        self.commit()
        self.assert_code("bundle_too_many_files", S.build_bundle, self.repo)

    def test_encoded_size_limit_is_checked_after_base64(self):
        self.put("scripts/agent_sync/large.py", "x" * (6 * 1024 * 1024))
        self.commit()
        self.assert_code("bundle_too_large", S.build_bundle, self.repo)


class TransportTests(Fixture):
    def test_success_is_cached_force_and_install_still_request_a_receipt(self):
        first = Transport()
        self.assertEqual(S.sync(self.repo, self.state, runner=first)["status"], "activated")
        self.assertEqual(len(first.calls), 1)
        cached = Transport()
        before = json.loads((self.state / "state.json").read_text())
        with patch.object(S.time, "time", return_value=before["last_success_at"] + 123):
            self.assertEqual(S.sync(self.repo, self.state, runner=cached)["status"], "skipped")
        self.assertFalse(cached.calls)
        checked = json.loads((self.state / "state.json").read_text())
        self.assertEqual(checked["last_checked_at"], before["last_success_at"] + 123)
        self.assertEqual(checked["last_check_status"], "skipped")
        self.assertEqual(checked["last_success_at"], before["last_success_at"])
        forced = Transport("unchanged")
        self.assertEqual(
            S.sync(self.repo, self.state, force=True, runner=forced)["status"], "unchanged"
        )
        installer = Transport()
        S.sync(self.repo, self.state, install=True, runner=installer)
        self.assertEqual(shlex.split(installer.calls[0][0][-1])[-1], "--install")
        self.assertNotIn("--install", shlex.split(first.calls[0][0][-1]))
        state = json.loads((self.state / "state.json").read_text())
        self.assertIsNone(state["last_error_code"])
        self.assertEqual((self.state / "state.json").stat().st_mode & 0o777, 0o600)

    def test_dry_run_never_uses_ssh_or_writes_success_state(self):
        transport = Transport()
        result = S.sync(self.repo, self.state, dry_run=True, install=True, runner=transport)
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(transport.calls)
        self.assertFalse((self.state / "state.json").exists())

    def test_failed_send_retries_next_trigger_and_preserves_last_success(self):
        S.sync(self.repo, self.state, runner=Transport())
        before = json.loads((self.state / "state.json").read_text())
        self.put("scripts/agent_sync/core.py", "VALUE = 2\n")
        self.commit()
        failed = Transport("timeout")
        self.assert_code("command_timeout", S.sync, self.repo, self.state, runner=failed)
        state = json.loads((self.state / "state.json").read_text())
        self.assertEqual(state["last_success_bundle_sha256"], before["last_success_bundle_sha256"])
        self.assertEqual(state["last_error_code"], "command_timeout")
        retried = Transport()
        self.assertEqual(S.sync(self.repo, self.state, runner=retried)["status"], "activated")
        self.assertEqual(len(retried.calls), 1)

    def test_paused_bad_receipts_and_failed_ssh_are_not_success(self):
        for mode, code in (
            ("paused", "receiver_paused"),
            ("wrong_digest", "receipt_invalid"),
            ("wrong_commit", "receipt_invalid"),
            ("malformed", "receipt_invalid"),
            ("failure", "ssh_failed"),
        ):
            with self.subTest(mode=mode):
                state_dir = self.state / mode
                self.assert_code(code, S.sync, self.repo, state_dir, runner=Transport(mode))
                state = json.loads((state_dir / "state.json").read_text())
                self.assertNotIn("last_success_bundle_sha256", state)
                self.assertEqual(state["last_error_code"], code)

    def test_ssh_command_has_strict_fixed_options_and_quoted_receiver(self):
        text = "print('shell $(touch should-not-exist) `anything`')\n"
        argv = S.ssh_argv(text)
        self.assertEqual(argv[-2], "root@coolify")
        self.assertEqual(
            shlex.split(argv[-1]), ["/usr/bin/python3", "-I", "-B", "-c", text, "receive"]
        )
        self.assertIn("-T", argv)
        for option in (
            "BatchMode=yes",
            "StrictHostKeyChecking=yes",
            "UpdateHostKeys=no",
            "ControlMaster=no",
            "ConnectTimeout=10",
        ):
            self.assertIn(option, argv)

    def test_timeouts_and_deadline_are_fixed_safe_errors(self):
        with patch.object(S.subprocess, "run", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            self.assert_code("command_timeout", S.Runner().run, ["fixture"])
        self.assert_code("deadline_exceeded", S.Runner(-1).run, ["fixture"])

    def test_concurrent_process_lock_and_state_symlinks_are_refused(self):
        S.private_directory(self.state)
        with (self.state / "sync.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assert_code("already_running", S.sync, self.repo, self.state, runner=Transport())
        secret = self.root / "private-fixture"
        secret.write_text("MUST_REMAIN_UNCHANGED")
        (self.state / "state.json").symlink_to(secret)
        self.assert_code("state_path_refused", S.sync, self.repo, self.state, runner=Transport())
        self.assertEqual(secret.read_text(), "MUST_REMAIN_UNCHANGED")

    def test_cli_never_prints_transport_stderr(self):
        stdout = io.StringIO()
        with patch.object(S, "Runner", return_value=Transport("failure")), redirect_stdout(stdout):
            status = S.main(["--source-repo", str(self.repo), "--state-dir", str(self.state)])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "error", "code": "ssh_failed"})


class HookTests(unittest.TestCase):
    def test_opt_in_hook_precedes_fetch_and_no_change_exit_inside_existing_lock(self):
        hook = Path(__file__).with_name("agent-sync-runtime-sync.sh").read_text()
        start = hook.index("# Code-only Coolify CLI sync")
        self.assertGreater(start, hook.index("trap 'rmdir"))
        self.assertLess(start, hook.index("if ! rgit fetch"))
        self.assertLess(start, hook.index('[ "$old" = "$new" ]'))
        self.assertLess(hook.index('[ -e "$PAUSE_FILE" ]'), start)
        self.assertIn("coolify-cli-sync.enabled", hook)
        self.assertIn("alarm 95", hook)
        self.assertIn("COOLIFY-CLI-SYNC-FAILED", hook)
        self.assertNotIn("--install", hook[start : hook.index("if ! git -C", start)])


if __name__ == "__main__":
    unittest.main()
