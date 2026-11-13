"""CLI login callers use one explicit intent and shared proof; all inputs are synthetic."""

import io
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fleet_secrets import core
from fleet_secrets import sync_cli_login as sync

PATH = "/cli-logins/OPENCODE"
PROJECT = str(uuid.uuid4())


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.value = secrets.token_urlsafe(40)
        self.reader = "st." + secrets.token_urlsafe(32)
        self.writer = "st." + secrets.token_urlsafe(32)
        self.env = {"HOME": str(self.root), "GH_TOKEN": self.value, core.ENV_PROJECT: PROJECT,
                    core.ENV_TOKEN: self.reader, core.ENV_WRITE_TOKEN: self.writer}
        self.intents, self.puts = [], []
        self.failure = None
        self.invalid_receipt = False
        self.receipt_id = self.receipt_time = None

    def factory(self, env, purpose, request_id=None, created_at_ms=None, state_dir=None):
        directory = Path(state_dir) if state_dir else self.root / "writer"
        intent = (core.new_write_intent(directory, purpose) if request_id is None else
                  core.WriteIntent(directory, purpose, request_id, created_at_ms))
        self.intents.append(intent)
        return intent

    def put(self, name, path, value, env, token=None, *, intent=None, **kwargs):
        self.assertEqual((name, path, value, env), ("GH_TOKEN", PATH, self.value, self.env))
        self.puts.append(intent)
        if self.failure:
            raise core.WriteError(self.failure, intent)
        if self.invalid_receipt:
            return "updated"
        return core.WriteReceipt(self.receipt_id or intent.request_id,
                                 self.receipt_time or intent.created_at_ms, "updated")

    def run_sync(self, args=None):
        out, err = io.StringIO(), io.StringIO()
        with (mock.patch.object(sync.caller, "write_intent_for_cli", self.factory, create=True),
              mock.patch.object(core, "put_secret", self.put),
              mock.patch.object(core, "fetch_all", side_effect=AssertionError("no whole-folder target read")),
              mock.patch.object(core.subprocess, "run", side_effect=AssertionError("no application subprocess"))):
            code = sync.main(args or ["--name", "GH_TOKEN", "--path", PATH], self.env, out, err)
        for private in (self.value, self.reader, self.writer):
            self.assertNotIn(private, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_value_source_and_success_never_reflect_value_or_tokens(self):
        code, out, err = self.run_sync()
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out.splitlines()[1], "synced GH_TOKEN at /cli-logins/OPENCODE")
        self.assertIn("requestID=" + self.intents[0].request_id, out)
        self.assertEqual(self.puts, self.intents)

    def test_allowlist_precedes_value_read_or_writer_factory(self):
        for name, path in (("SOMETHING_ELSE", PATH), ("GH_TOKEN", "/linear")):
            with self.subTest(name=name), mock.patch.object(sync, "read_value", side_effect=AssertionError("read anyway")):
                code, out, err = self.run_sync(["--name", name, "--path", path])
                self.assertEqual(code, 2)
                self.assertFalse(out)
                self.assertTrue(err)
        self.assertEqual((self.intents, self.puts), ([], []))

    def test_canonical_write_receipt_replaces_the_redundant_target_reader(self):
        self.env.pop(core.ENV_TOKEN)
        self.assertEqual(self.run_sync()[0], 0)
        self.assertEqual(len(self.puts), 1)

    def test_readback_failure_or_mismatch_propagates_without_claiming_synced(self):
        for failure in ("accepted_unverified", "readback_conflict", "outcome_unknown"):
            with self.subTest(failure=failure):
                self.failure = failure
                code, out, err = self.run_sync()
                self.assertEqual(code, 4)
                self.assertNotIn("synced", out)
                self.assertIn("outcome=" + failure, err)
                self.assertIn(self.intents[-1].request_id, err)

    def test_an_unverified_legacy_string_is_not_a_success_receipt(self):
        self.invalid_receipt = True
        code, out, err = self.run_sync()
        self.assertEqual((code, out), (4, ""))
        self.assertIn("write-receipt-invalid", err)

    def test_wrong_receipt_uuid_or_creation_time_is_not_reported_as_synced(self):
        for field, value in (("receipt_id", str(uuid.uuid4())),
                             ("receipt_time", 1)):
            with self.subTest(field=field):
                setattr(self, field, value)
                code, out, err = self.run_sync()
                self.assertEqual((code, out), (4, ""))
                self.assertIn("write-receipt-invalid", err)
                setattr(self, field, None)

    def test_source_compatibility_spelling_is_one_named_direct_read(self):
        with mock.patch.object(core, "fetch_project", return_value={"GH_TOKEN": self.value}) as read:
            code, out, err = self.run_sync(["--name", "GH_TOKEN", "--path", PATH,
                                            "--from", "fleet-secrets", "--from-path", "/source"])
        self.assertEqual((code, err), (0, ""))
        args, kwargs = read.call_args
        self.assertEqual((args[0], args[1], kwargs["token"], kwargs["names"]),
                         (PROJECT, "/source", self.reader, ("GH_TOKEN",)))
        self.assertEqual(kwargs["timeout"], core.EXPORT_TIMEOUT)

    def test_source_failure_does_not_dispatch_a_write(self):
        with mock.patch.object(core, "fetch_project", side_effect=core.SecretsError("fetch-failed")):
            code, out, err = self.run_sync(["--name", "GH_TOKEN", "--path", PATH, "--from", "fleet-secrets"])
        self.assertEqual((code, out), (4, ""))
        self.assertIn("fetch-failed", err)
        self.assertEqual(self.puts, [])

    def test_file_source_still_requires_owner_only_mode(self):
        target = self.root / "input"
        target.write_text(self.value)
        target.chmod(0o644)
        args = ["--name", "GH_TOKEN", "--path", PATH, "--from", "file", "--file", str(target)]
        code, out, err = self.run_sync(args)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("mode 0600", err)
        target.chmod(0o600)
        self.assertEqual(self.run_sync(args)[0], 0)

    def test_automated_retry_reuses_original_uuid_time_and_explicit_state(self):
        original = core.new_write_intent(self.root / "explicit", "cli-login-sync")
        args = ["--name", "GH_TOKEN", "--path", PATH, "--request-id", original.request_id,
                "--request-created-at", str(original.created_at_ms), "--state-dir", str(original.state_dir)]
        self.assertEqual(self.run_sync(args)[0], 0)
        self.assertEqual(self.run_sync(args)[0], 0)
        self.assertEqual(self.puts, [original, original])

    def test_recovery_reads_no_source_and_never_dispatches_put(self):
        original = core.new_write_intent(self.root / "writer", "cli-login-sync")
        receipt = core.WriteReceipt(original.request_id, original.created_at_ms, "updated")
        with (mock.patch.object(sync, "read_value", side_effect=AssertionError("recovery read input")),
              mock.patch.object(core, "reconcile_secret", return_value=receipt) as reconcile):
            code, out, err = self.run_sync(["--name", "GH_TOKEN", "--path", PATH, "--reconcile",
                                          "--request-id", original.request_id,
                                          "--request-created-at", str(original.created_at_ms)])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.puts, [])
        self.assertEqual(reconcile.call_args.kwargs["intent"], original)
        self.assertIn("synced", out)

    def test_recovery_requires_original_identity_before_any_value_read(self):
        with mock.patch.object(sync, "read_value", side_effect=AssertionError("recovery read input")):
            code, out, err = self.run_sync(["--name", "GH_TOKEN", "--path", PATH, "--reconcile"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn("original request-id", err)
        self.assertEqual(self.intents, [])


if __name__ == "__main__":
    unittest.main()
