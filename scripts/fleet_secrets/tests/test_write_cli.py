"""Shared writer callers use stable identity without exposing values or replaying uncertainty."""

import io
import json
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import fleet_infisical as core
import fleet_infisical_cli as cli


class NoInput(io.StringIO):
    def read(self, *args):
        raise AssertionError("stdin must not be read")


class WriteCallerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.env = {"HOME": str(self.home), core.ENV_PROJECT: str(uuid.uuid4())}
        self.intent = cli.write_intent_for_cli(self.env, "fleet-infisical-cli")

    def call(self, args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli.sys, "stdin", NoInput() if stdin is None else stdin):
            code = cli.main(
                ["set", "CACHE_CODEX", "--path", "/linear", *args], env=self.env, out=out, err=err
            )
        return code, out.getvalue(), err.getvalue()

    def identity(self):
        return [
            "--request-id",
            self.intent.request_id,
            "--request-created-at",
            str(self.intent.created_at_ms),
        ]

    def test_factory_default_is_shared_across_purposes_and_has_no_io(self):
        other = cli.write_intent_for_cli(self.env, "cli-login-sync")
        self.assertEqual(other.state_dir, self.home / ".local/state/fleet-infisical-writes")
        self.assertEqual(other.state_dir, self.intent.state_dir)
        self.assertNotEqual(other.request_id, self.intent.request_id)
        self.assertFalse(other.state_dir.exists())
        recovered = cli.write_intent_for_cli(
            self.env, other.purpose, other.request_id, other.created_at_ms, other.state_dir
        )
        self.assertEqual(recovered, other)

    def test_invalid_factory_inputs_do_not_create_state(self):
        for kwargs in (
            {"request_id": self.intent.request_id},
            {"created_at_ms": self.intent.created_at_ms},
            {"state_dir": "relative"},
            {"state_dir": self.home / ".." / "escape"},
            {"purpose": "not a slug"},
            {"request_id": "invalid", "created_at_ms": 1},
            {"request_id": self.intent.request_id, "created_at_ms": True},
        ):
            params = dict(purpose="test-writer")
            params.update(kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(core.SecretsError):
                cli.write_intent_for_cli(self.env, **params)
        self.assertFalse(self.intent.state_dir.exists())

    def test_invalid_options_refuse_before_stdin_state_or_provider(self):
        invalid = (
            ["--request-id", self.intent.request_id],
            ["--request-created-at", "1"],
            ["--request-created-at", "nan"],
            ["--request-created-at", "9" * 21],
            ["--reconcile"],
            ["--state-dir", "relative"],
            ["--state-dir", ""],
            ["--purpose", "bad purpose"],
            ["--purpose=a", "--purpose=b"],
            ["--reconcile", "--reconcile"],
            ["--reconcile=yes"],
            ["--unsupported", "untrusted-marker"],
            ["--project", "other"],
        )
        with (
            mock.patch.object(core, "put_secret") as put,
            mock.patch.object(core, "reconcile_secret") as reconcile,
        ):
            for args in invalid:
                with self.subTest(args=args):
                    code, out, err = self.call(args)
                    self.assertEqual((code, out), (2, ""))
                    self.assertNotIn("untrusted-marker", err)
            put.assert_not_called()
            reconcile.assert_not_called()
        self.assertFalse(self.intent.state_dir.exists())

    def test_reconcile_passes_exact_original_identity_without_reading_stdin(self):
        receipt = core.WriteReceipt(self.intent.request_id, self.intent.created_at_ms, "updated")
        with (
            mock.patch.object(core, "reconcile_secret", return_value=receipt) as reconcile,
            mock.patch.object(core, "put_secret") as put,
        ):
            code, out, err = self.call([*self.identity(), "--reconcile"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(reconcile.call_args.kwargs["intent"], self.intent)
        put.assert_not_called()
        self.assertEqual(json.loads(out)["outcome"], "ok")

    def test_fresh_set_passes_one_intent_and_returns_safe_receipt(self):
        value = secrets.token_urlsafe(40)

        def put(name, path, supplied, env, *, intent):
            self.assertEqual((name, path, supplied), ("CACHE_CODEX", "/linear", value))
            self.assertEqual(intent.state_dir, self.intent.state_dir)
            return core.WriteReceipt(intent.request_id, intent.created_at_ms, "created")

        with mock.patch.object(core, "put_secret", side_effect=put) as operation:
            code, out, err = self.call([], io.StringIO(value + "\n"))
        self.assertEqual((code, err, operation.call_count), (0, "", 1))
        report = json.loads(out)
        self.assertEqual(
            (report["result"], report["replayed"], report["purpose"]),
            ("created", False, "fleet-infisical-cli"),
        )
        self.assertNotIn(value, out + err)

    def test_blocking_original_identity_and_typed_refusal_fields_survive(self):
        original = cli.write_intent_for_cli(self.env, "cli-login-sync")
        for outcome, kwargs in (
            ("prior_outcome_unknown", {}),
            ("refused", {"provider_status": 429, "retryable": True}),
        ):
            error = core.WriteError(outcome, original, name="CACHE_ORIGINAL", **kwargs)
            for reconcile in (False, True):
                target = "reconcile_secret" if reconcile else "put_secret"
                args = [*self.identity(), "--reconcile"] if reconcile else []
                with (
                    self.subTest(outcome=outcome, reconcile=reconcile),
                    mock.patch.object(core, target, side_effect=error),
                ):
                    code, out, err = self.call(
                        args, NoInput() if reconcile else io.StringIO("synthetic-value")
                    )
                self.assertEqual(code, 4)
                report = json.loads(out)
                self.assertEqual(
                    (
                        report["request_id"],
                        report["created_at_ms"],
                        report["purpose"],
                        report["name"],
                    ),
                    (
                        original.request_id,
                        original.created_at_ms,
                        original.purpose,
                        "CACHE_ORIGINAL",
                    ),
                )
                self.assertEqual(report["provider_status"], kwargs.get("provider_status"))
                self.assertEqual(report["retryable"], kwargs.get("retryable", False))
                self.assertEqual(
                    report["request_may_have_been_sent"], outcome == "prior_outcome_unknown"
                )
                self.assertNotIn("synthetic-value", out + err)

    def test_unexpected_success_receipts_refuse_without_stdout(self):
        valid = core.WriteReceipt(self.intent.request_id, self.intent.created_at_ms, "updated")
        other = cli.write_intent_for_cli(self.env, "other-writer")
        invalid = (
            "legacy-status",
            None,
            valid._replace(request_id=other.request_id),
            valid._replace(created_at_ms=self.intent.created_at_ms - 1),
            valid._replace(outcome="outcome_unknown"),
            valid._replace(result="untrusted-provider-text"),
            valid._replace(replayed=1),
        )
        for receipt in invalid:
            for reconcile in (False, True):
                args = self.identity() + (["--reconcile"] if reconcile else [])
                target = "reconcile_secret" if reconcile else "put_secret"
                with (
                    self.subTest(receipt=receipt, reconcile=reconcile),
                    mock.patch.object(core, target, return_value=receipt),
                ):
                    code, out, err = self.call(
                        args, NoInput() if reconcile else io.StringIO("synthetic-value")
                    )
                self.assertEqual((code, out), (4, ""))
                self.assertIn("write-receipt-invalid", err)
                self.assertNotIn("legacy-status", err)
                self.assertNotIn("untrusted-provider-text", err)

    def test_explicit_root_and_purpose_are_passed_without_new_store(self):
        directory = self.home / "declared-writer"
        receipt = core.WriteReceipt(
            self.intent.request_id, self.intent.created_at_ms, "updated", True
        )
        with mock.patch.object(core, "put_secret", return_value=receipt) as put:
            code, out, err = self.call(
                [*self.identity(), "--state-dir=" + str(directory), "--purpose=declared-writer"],
                io.StringIO("synthetic-value"),
            )
        self.assertEqual((code, err), (0, ""))
        supplied = put.call_args.kwargs["intent"]
        self.assertEqual((supplied.state_dir, supplied.purpose), (directory, "declared-writer"))
        self.assertTrue(json.loads(out)["replayed"])
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
