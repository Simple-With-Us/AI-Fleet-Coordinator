"""Actual CLI/sync/factory/ledger recovery; only the network response boundary is faked."""

import io
import json
import re
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

import fleet_infisical as core
import fleet_infisical_cli as cli

from fleet_secrets import sync_cli_login as sync


class LostOutput(io.StringIO):
    def write(self, value):
        raise BrokenPipeError("synthetic caller receipt loss")


class ProtocolIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)
        self.project = str(uuid.uuid4())
        self.value = secrets.token_urlsafe(40)
        self.token = "st." + secrets.token_urlsafe(32)
        self.path, self.name = "/cli-logins/CODEX", "GH_TOKEN"
        self.env = {
            "HOME": str(self.home),
            core.ENV_PROJECT: self.project,
            core.ENV_WRITE_TOKEN: self.token,
            "GH_TOKEN": self.value,
        }
        self.calls, self.mutations = [], []
        self.status, self.read_status = 200, 200
        self.folder_status = 200
        patch = mock.patch.object(core, "_api_json", self.api)
        patch.start()
        self.addCleanup(patch.stop)

    def api(self, method, endpoint, token, body, env, **kwargs):
        self.assertEqual(token, self.token)
        self.calls.append((method, endpoint))
        if endpoint == "/v2/service-token":
            return 200, dict(
                workspace=self.project,
                permissions=["read", "write"],
                scopes=[dict(environment="prod", secretPath=self.path)],
                expiresAt="2099-01-01T00:00:00Z",
            )
        if method in ("PATCH", "POST"):
            self.mutations.append(method)
            self.stored = dict(
                secretKey=self.name,
                secretValue=body["secretValue"],
                workspace=self.project,
                environment="prod",
                secretPath=self.path,
                type="shared",
                secretValueHidden=False,
            )
            if self.status in (200, 201):
                kwargs["on_status"](self.status)
            return self.status, {}
        if urlsplit(endpoint).path == "/v3/secrets/raw":
            self.assertIn("viewSecretValue=false", endpoint)
            return self.folder_status, {"secrets": []}
        return self.read_status, {"secret": self.stored}

    def identity(self, intent):
        return [
            "--request-id",
            intent.request_id,
            "--request-created-at",
            str(intent.created_at_ms),
        ]

    def call_cli(self, extra=(), output=None):
        out, err = output if output is not None else io.StringIO(), io.StringIO()
        with mock.patch.object(cli.sys, "stdin", io.StringIO(self.value + "\n")):
            code = cli.main(["set", self.name, "--path", self.path, *extra], self.env, out, err)
        return code, out.getvalue(), err.getvalue()

    def call_sync(self, extra=(), output=None):
        out, err = output if output is not None else io.StringIO(), io.StringIO()
        code = sync.main(["--name", self.name, "--path", self.path, *extra], self.env, out, err)
        return code, out.getvalue(), err.getvalue()

    def test_lost_success_receipt_then_same_identity_retry_never_writes_again_for_either_entry(
        self,
    ):
        for call, purpose in (
            (self.call_cli, "fleet-infisical-cli"),
            (self.call_sync, "cli-login-sync"),
        ):
            with self.subTest(purpose=purpose):
                intent = cli.write_intent_for_cli(self.env, purpose)
                before_mutations = len(self.mutations)
                with self.assertRaises(BrokenPipeError):
                    call(self.identity(intent), LostOutput())
                before_calls = list(self.calls)
                code, out, err = call(self.identity(intent))
                self.assertEqual((code, err), (0, ""))
                self.assertIn(intent.request_id, out)
                self.assertEqual(self.calls, before_calls)
                self.assertEqual(len(self.mutations), before_mutations + 1)

    def test_fresh_sync_caller_cannot_bypass_cli_unknown_or_obscure_original_recovery_identity(
        self,
    ):
        self.status = 0
        code, out, _ = self.call_cli()
        self.assertEqual(code, 4)
        first = json.loads(out)
        self.assertEqual(first["outcome"], "outcome_unknown")
        before = list(self.calls)
        code, out, err = self.call_sync()
        self.assertEqual((code, out), (4, ""))
        self.assertIn("outcome=prior_outcome_unknown", err)
        self.assertIn("requestID=" + first["request_id"], err)
        self.assertIn("requestCreatedAt=" + str(first["created_at_ms"]), err)
        self.assertIn("purpose=" + first["purpose"], err)
        self.assertEqual(self.calls, before)
        self.assertEqual(self.mutations, ["PATCH"])
        for secret in (self.value, self.token):
            self.assertNotIn(secret, out + err)

    def test_actual_cli_reconcile_uses_stored_ack_without_input_or_second_mutation(self):
        self.read_status = 503
        code, out, _ = self.call_cli()
        first = json.loads(out)
        self.assertEqual((code, first["outcome"]), (4, "accepted_unverified"))
        self.read_status = 200
        intent = cli.write_intent_for_cli(
            self.env, first["purpose"], first["request_id"], first["created_at_ms"]
        )
        with mock.patch.object(cli.sys, "stdin", None):
            output, error = io.StringIO(), io.StringIO()
            code = cli.main(
                ["set", self.name, "--path", self.path, *self.identity(intent), "--reconcile"],
                self.env,
                output,
                error,
            )
        self.assertEqual((code, error.getvalue()), (0, ""))
        self.assertEqual(json.loads(output.getvalue())["outcome"], "ok")
        self.assertEqual(self.mutations, ["PATCH"])

    def test_preflight_500_is_retryable_but_original_identity_stays_terminal(self):
        self.folder_status = 500
        code, out, err = self.call_cli()
        first = json.loads(out)
        self.assertEqual((code, first["outcome"]), (4, "refused"))
        self.assertIn("write-refused", err)
        self.assertEqual(
            (first["provider_status"], first["retryable"], first["request_may_have_been_sent"]),
            (500, True, False),
        )
        self.assertEqual([method for method, _ in self.calls], ["GET", "GET"])
        self.assertEqual(self.calls[0][1], "/v2/service-token")
        self.assertEqual(urlsplit(self.calls[1][1]).path, "/v3/secrets/raw")
        self.assertFalse(self.mutations)
        intent = cli.write_intent_for_cli(
            self.env, first["purpose"], first["request_id"], first["created_at_ms"]
        )
        before = list(self.calls)
        self.folder_status = 200
        for suffix in (self.identity(intent), [*self.identity(intent), "--reconcile"]):
            code, out, _ = self.call_cli(suffix)
            repeated = json.loads(out)
            self.assertEqual((code, repeated["outcome"]), (4, "refused"))
            self.assertEqual(
                (
                    repeated["request_id"],
                    repeated["provider_status"],
                    repeated["retryable"],
                    repeated["request_may_have_been_sent"],
                ),
                (intent.request_id, 500, True, False),
            )
            self.assertEqual(self.calls, before)
        # A deliberate fresh intent can verify the provider again; the refused row is retained.
        code, out, err = self.call_cli()
        fresh = json.loads(out)
        self.assertEqual((code, err, fresh["outcome"]), (0, "", "ok"))
        self.assertNotEqual(fresh["request_id"], intent.request_id)
        self.assertEqual(self.mutations, ["PATCH"])
        held = core._write_ledger(intent.state_dir).lookup(
            core._write_namespace(core.API_BASE, self.project, self.path), intent.request_id
        )
        self.assertEqual(held[0], "refused")

    def test_actual_sync_reconcile_skips_source_and_set_and_settles_only_acknowledged_attempt(self):
        self.read_status = 503
        code, out, err = self.call_sync()
        self.assertEqual((code, out), (4, ""))
        request_id = re.search(r"requestID=([a-f0-9-]+)", err).group(1)
        created = int(re.search(r"requestCreatedAt=([0-9]+)", err).group(1))
        intent = cli.write_intent_for_cli(self.env, "cli-login-sync", request_id, created)
        self.read_status = 200
        with (
            mock.patch.object(
                sync, "read_value", side_effect=AssertionError("recovery read source")
            ),
            mock.patch.object(
                core, "put_secret", side_effect=AssertionError("recovery dispatched put")
            ),
        ):
            code, out, err = self.call_sync([*self.identity(intent), "--reconcile"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn(request_id, out)
        self.assertEqual(self.mutations, ["PATCH"])


if __name__ == "__main__":
    unittest.main()
