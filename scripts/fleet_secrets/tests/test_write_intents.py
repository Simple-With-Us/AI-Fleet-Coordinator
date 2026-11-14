"""Hermetic durable writer tests; no credential store or real provider is available."""

import contextlib
import json
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

import fleet_infisical as core

PROJECT = str(uuid.uuid4())
PATH = "/linear"
NAME = "CACHE_CODEX"


class Crash(BaseException):
    pass


class WriteIntentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name) / "writer"
        self.token = "st." + secrets.token_urlsafe(32)
        self.value = secrets.token_urlsafe(40)
        self.env = {core.ENV_PROJECT: PROJECT, core.ENV_WRITE_TOKEN: self.token}
        self.intent = self.new_intent()
        self.calls = []
        self.mutations = []
        self.statuses = [200]
        self.stored = self.value
        self.read_status = 200
        self.no_ack = False
        self.crash = None
        self.metadata = dict(
            workspace=PROJECT,
            permissions=["read", "write"],
            scopes=[dict(environment="prod", secretPath=PATH)],
            expiresAt="2099-01-01T00:00:00Z",
        )
        self.transport = mock.patch.object(core, "_api_json", self.api)
        self.actual_api = core._api_json
        self.transport.start()
        self.addCleanup(self.transport.stop)
        guard = mock.patch.object(
            core.subprocess, "run", side_effect=AssertionError("no CLI fallback")
        )
        guard.start()
        self.addCleanup(guard.stop)

    def new_intent(self, purpose="test-writer"):
        return core.new_write_intent(self.directory, purpose)

    def row(self, intent=None):
        intent = intent or self.intent
        namespace = core._write_namespace(core.API_BASE, PROJECT, PATH)
        outcome, raw = core._write_ledger(self.directory).lookup(namespace, intent.request_id)
        return outcome, json.loads(raw)

    def api(self, method, endpoint, token, body, env, **kwargs):
        self.assertEqual(token, self.token)
        self.assertGreater(kwargs["deadline"], 0)
        self.calls.append((method, endpoint, kwargs["deadline"]))
        if endpoint == "/v2/service-token":
            if self.crash == "prepared":
                raise Crash()
            return 200, self.metadata
        if method in ("PATCH", "POST"):
            outcome, record = self.row()
            self.assertEqual((outcome, record["phase"]), ("pending", "sending"))
            if self.crash == "sending":
                raise Crash()
            self.mutations.append(method)
            status = self.statuses.pop(0)
            if status in (0, 200, 201, 202):
                self.stored = body["secretValue"]
            if not self.no_ack:
                kwargs["on_status"](status)
            if self.crash == "acknowledged":
                raise Crash()
            return status, {}
        query = parse_qs(urlsplit(endpoint).query)
        self.assertEqual(query["workspaceId"], [PROJECT])
        for key in ("expandSecretReferences", "recursive", "include_imports"):
            self.assertEqual(query[key], ["false"])
        if "/raw/" not in endpoint:
            self.assertEqual(query["viewSecretValue"], ["false"])
            return 200, {"secrets": []}
        self.assertEqual(query["viewSecretValue"], ["true"])
        return self.read_status, {
            "secret": dict(
                secretKey=NAME,
                secretValue=self.stored,
                type="shared",
                workspace=PROJECT,
                environment="prod",
                secretPath=PATH,
            )
        }

    def put(self, intent=None, value=None):
        return core.put_secret(
            NAME,
            PATH,
            self.value if value is None else value,
            self.env,
            intent=intent or self.intent,
        )

    def reconcile(self, intent=None):
        return core.reconcile_secret(NAME, PATH, self.env, intent=intent or self.intent)

    def assert_outcome(self, outcome, call):
        with self.assertRaises(core.WriteError) as raised:
            call()
        self.assertEqual(raised.exception.outcome, outcome)
        for value in (self.value, self.token):
            self.assertNotIn(value, str(raised.exception))
        return raised.exception

    def test_success_requires_ack_and_named_exact_readback_then_duplicate_never_dispatches(self):
        receipt = self.put()
        self.assertEqual(
            (receipt.result, receipt.outcome, receipt.replayed), ("updated", "ok", False)
        )
        self.assertEqual(receipt.request_id, self.intent.request_id)
        self.assertEqual(self.row()[0:1], ("ok",))
        self.assertEqual(self.row()[1]["ack_status"], 200)
        self.assertEqual(len({deadline for _, _, deadline in self.calls}), 1)
        before = list(self.calls)
        self.assertTrue(self.put().replayed)
        self.assertEqual(self.calls, before)
        self.assertEqual(self.mutations, ["PATCH"])
        for target in self.directory.iterdir():
            data = target.read_bytes()
            self.assertNotIn(self.value.encode(), data)
            self.assertNotIn(self.token.encode(), data)

    def test_missing_secret_posts_once_only_after_definite_patch_404(self):
        self.statuses = [404, 201]
        self.assertEqual(self.put().result, "created")
        self.assertEqual(self.mutations, ["PATCH", "POST"])
        self.assertEqual(self.row()[1]["ack_status"], 201)

    def test_timeout_202_or_server_error_is_unknown_and_no_new_id_purpose_or_noop_bypasses_it(self):
        for status in (0, 202, 500, 503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                self.directory = Path(root) / "writer"
                self.intent = self.new_intent()
                self.statuses, self.no_ack = [status], True
                self.calls.clear()
                self.assert_outcome("outcome_unknown", self.put)
                before = list(self.calls)
                self.assert_outcome("outcome_unknown", self.reconcile)
                self.assert_outcome("outcome_unknown", self.put)
                self.assert_outcome(
                    "prior_outcome_unknown", lambda: self.put(self.new_intent("another-purpose"))
                )
                self.assertEqual(self.calls, before)

    def test_unknown_survives_restart_and_age_and_equal_provider_value_is_not_causal_proof(self):
        self.statuses, self.no_ack = [0], True
        self.assert_outcome("outcome_unknown", self.put)
        self.assertEqual(self.stored, self.value)
        before = list(self.calls)
        with mock.patch.object(
            core.time, "time_ns", return_value=time.time_ns() + 3 * 86_400 * 1_000_000_000
        ):
            self.assert_outcome("outcome_unknown", self.reconcile)
            self.assert_outcome("prior_outcome_unknown", lambda: self.put(self.new_intent()))
        self.assertEqual(self.calls, before)

    def test_causal_ack_is_durable_before_failed_readback_and_can_reconcile_without_mutation(self):
        self.read_status = 503
        self.assert_outcome("accepted_unverified", self.put)
        outcome, record = self.row()
        self.assertEqual(
            (outcome, record["phase"], record["ack_status"]),
            ("outcome_unknown", "acknowledged", 200),
        )
        self.assert_outcome("prior_outcome_unknown", lambda: self.put(self.new_intent()))
        self.read_status = 200
        self.assertEqual(self.reconcile().result, "updated")
        self.assertEqual(self.mutations, ["PATCH"])
        self.assertEqual(self.row()[0], "ok")

    def test_actual_transport_persists_ack_before_reading_a_response_body_that_times_out(self):
        original = core._api_json
        actual = self.actual_api
        outer = self.api
        test = self

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, limit):
                self_seen = test.row()
                test.assertEqual((self_seen[0], self_seen[1]["phase"]), ("pending", "acknowledged"))
                raise TimeoutError()

        def transport(method, *args, **kwargs):
            if method == "PATCH":
                return actual(method, *args, **kwargs)
            return outer(method, *args, **kwargs)

        self.read_status = 503
        with (
            mock.patch.object(core, "_api_json", transport),
            mock.patch.object(
                core,
                "_opener",
                return_value=SimpleNamespace(open=lambda *args, **kwargs: Response()),
            ),
        ):
            self.assert_outcome("accepted_unverified", self.put)
        self.assertEqual(self.row()[1]["ack_status"], 200)
        self.assertIs(core._api_json, original)

    def test_actual_failed_connect_is_unsent_but_timeouts_resets_and_generic_oserrors_stay_unknown(
        self,
    ):
        for failure, outcome in (
            (URLError(ConnectionRefusedError()), "refused"),
            (ConnectionRefusedError(), "refused"),
            (URLError(TimeoutError()), "outcome_unknown"),
            (ConnectionResetError(), "outcome_unknown"),
            (OSError("synthetic provider text"), "outcome_unknown"),
        ):
            with self.subTest(kind=type(failure).__name__), tempfile.TemporaryDirectory() as root:
                self.directory = Path(root) / "writer"
                self.intent = self.new_intent()
                outer = self.api

                def transport(method, *args, **kwargs):
                    return (
                        self.actual_api(method, *args, **kwargs)
                        if method == "PATCH"
                        else outer(method, *args, **kwargs)
                    )

                with (
                    mock.patch.object(core, "_api_json", transport),
                    mock.patch.object(
                        core,
                        "_opener",
                        return_value=SimpleNamespace(open=mock.Mock(side_effect=failure)),
                    ),
                ):
                    error = self.assert_outcome(outcome, self.put)
                self.assertEqual(
                    (error.request_may_have_been_sent, error.retryable),
                    (outcome == "outcome_unknown", outcome == "refused"),
                )

    def test_acknowledged_mismatch_remains_held_and_cannot_clear_from_old_value_or_aba(self):
        self.read_status = 503
        self.assert_outcome("accepted_unverified", self.put)
        self.read_status, self.stored = 200, "another-value"
        self.assert_outcome("readback_conflict", self.reconcile)
        self.assert_outcome(
            "prior_outcome_unknown", lambda: self.put(self.new_intent(), "another-value")
        )
        self.assertEqual(self.row()[0], "outcome_unknown")
        self.assertEqual(self.mutations, ["PATCH"])

    def test_crash_boundaries_distinguish_prepared_cancellation_from_sending_and_acked(self):
        for phase in ("prepared", "sending", "acknowledged"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                self.directory = Path(root) / "writer"
                self.intent = self.new_intent()
                self.statuses, self.crash = [200], phase
                with self.assertRaises(Crash):
                    self.put()
                self.assertEqual(self.row()[1]["phase"], phase)
                self.crash = None
                if phase == "prepared":
                    self.assert_outcome("cancelled", self.reconcile)
                    self.assert_outcome("refused", self.put)
                elif phase == "sending":
                    before = list(self.calls)
                    self.assert_outcome("outcome_unknown", self.reconcile)
                    self.assertEqual(self.calls, before)
                else:
                    self.assertEqual(self.reconcile().outcome, "ok")

    def test_recovery_before_original_claim_leaves_a_nonrearming_cancellation_tombstone(self):
        self.assert_outcome("cancelled", self.reconcile)
        self.assert_outcome("intent_conflict", self.put)
        self.assert_outcome("cancelled", self.reconcile)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.row()[0], "refused")

    def test_rerun_reports_original_pending_identity_not_the_blocked_new_uuid(self):
        self.statuses, self.no_ack = [0], True
        self.assert_outcome("outcome_unknown", self.put)
        fresh = self.new_intent("another-purpose")
        error = self.assert_outcome("prior_outcome_unknown", lambda: self.put(fresh))
        self.assertEqual(
            (error.request_id, error.created_at_ms, error.purpose, error.name),
            (self.intent.request_id, self.intent.created_at_ms, self.intent.purpose, NAME),
        )
        self.assertNotEqual(error.request_id, fresh.request_id)

    def test_absent_reconcile_cannot_add_a_second_pending_row_behind_existing_unknown(self):
        self.statuses, self.no_ack = [0], True
        self.assert_outcome("outcome_unknown", self.put)
        fresh = self.new_intent()
        before = list(self.calls)
        error = self.assert_outcome("prior_outcome_unknown", lambda: self.reconcile(fresh))
        self.assertEqual(error.request_id, self.intent.request_id)
        namespace = core._write_namespace(core.API_BASE, PROJECT, PATH)
        self.assertIsNone(core._write_ledger(self.directory).lookup(namespace, fresh.request_id))
        self.assertEqual(self.calls, before)

    def test_multiple_held_or_malformed_recovery_metadata_fails_closed(self):
        namespace = core._write_namespace(core.API_BASE, PROJECT, PATH)
        ledger = core._write_ledger(self.directory)
        for intent in (self.intent, self.new_intent()):
            record = core._write_record(
                intent, core.API_BASE, PROJECT, PATH, NAME, core._write_digest(self.value)
            )
            ledger.begin(
                namespace,
                intent.request_id,
                time.time() + 60,
                time.time(),
                1000,
                response=core._write_json(record),
            )
        self.assert_outcome("state_unavailable", lambda: self.put(self.new_intent()))
        self.assertEqual(self.calls, [])

    def test_cached_phase_must_agree_with_terminal_outcome(self):
        self.put()
        ledger = core._write_ledger(self.directory)
        namespace = core._write_namespace(core.API_BASE, PROJECT, PATH)
        _, record = self.row()
        record.update(phase="sending", result="", ack_status=0)
        ledger.settle(namespace, self.intent.request_id, "ok", core._write_json(record))
        before = list(self.calls)
        self.assert_outcome("state_unavailable", self.reconcile)
        self.assertEqual(self.calls, before)

    def test_success_stays_known_after_later_external_change_or_read_failure(self):
        self.put()
        self.stored, self.read_status = "external-change", 503
        before = list(self.calls)
        self.assertTrue(self.reconcile().replayed)
        self.assertEqual(self.row()[0], "ok")
        self.assertEqual(self.calls, before)

    def test_id_collision_binds_value_and_time_and_never_dispatches(self):
        self.put()
        self.assert_outcome("intent_conflict", lambda: self.put(value="changed"))
        other = core.WriteIntent(
            self.directory,
            self.intent.purpose,
            self.intent.request_id,
            self.intent.created_at_ms + 1,
        )
        error = self.assert_outcome("state-invalid", lambda: self.put(other))
        self.assertTrue(error.request_may_have_been_sent)
        self.assertEqual(self.mutations, ["PATCH"])

    def test_bad_cached_record_is_fixed_nonreflecting_and_never_dispatches(self):
        self.put()
        ledger = core._write_ledger(self.directory)
        namespace = core._write_namespace(core.API_BASE, PROJECT, PATH)
        ledger.settle(
            namespace, self.intent.request_id, "ok", json.dumps({"secretValue": self.value})
        )
        before = list(self.calls)
        error = self.assert_outcome("state_unavailable", self.reconcile)
        self.assertTrue(error.request_may_have_been_sent)
        self.assertEqual(self.calls, before)

    def test_scope_expiry_and_writer_binding_refuse_before_mutation(self):
        for change in (
            {"expiresAt": None},
            {"permissions": ["read"]},
            {"scopes": [{"environment": "prod", "secretPath": "/"}]},
            {
                "scopes": [
                    {"environment": "prod", "secretPath": PATH},
                    {"environment": "prod", "secretPath": "/"},
                ]
            },
        ):
            with self.subTest(change=change):
                self.metadata.update(change)
                self.assert_outcome("refused", lambda: self.put(self.new_intent()))
                self.assertEqual(self.mutations, [])

    def test_diagnostics_preserve_expiry_reason_and_exact_missing_binding_name(self):
        self.metadata["expiresAt"] = None
        error = self.assert_outcome("refused", self.put)
        self.assertEqual(error.reason, "token-expired-or-invalid")
        self.env.pop(core.ENV_WRITE_TOKEN)
        error = self.assert_outcome("refused", lambda: self.put(self.new_intent()))
        self.assertEqual(error.required_binding, core.ENV_WRITE_TOKEN)
        self.assertIn(core.ENV_WRITE_TOKEN, error.message)

    def test_definite_refusal_preserves_safe_status_retryability_and_terminal_id_on_restart(self):
        for status in (401, 403, 429):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                self.directory = Path(root) / "writer"
                self.intent, self.statuses = self.new_intent(), [status]
                error = self.assert_outcome("refused", self.put)
                self.assertEqual(
                    (error.provider_status, error.retryable, error.request_may_have_been_sent),
                    (status, status == 429, False),
                )
                before = list(self.calls)
                repeated = self.assert_outcome("refused", self.reconcile)
                self.assertEqual(
                    (repeated.provider_status, repeated.retryable), (status, status == 429)
                )
                self.assertEqual(self.calls, before)
                self.intent, self.statuses = self.new_intent(), [200]
                self.assertEqual(self.put().outcome, "ok")

    def test_preflight_outage_is_definitely_unsent_retryable_and_durable(self):
        with mock.patch.object(core, "_api_json", return_value=(503, None)):
            error = self.assert_outcome("refused", self.put)
        self.assertEqual(
            (error.retryable, error.request_may_have_been_sent, error.provider_status),
            (True, False, 503),
        )
        repeated = self.assert_outcome("refused", self.reconcile)
        self.assertEqual((repeated.retryable, repeated.provider_status), (True, 503))
        self.assertEqual(self.mutations, [])

    def test_shared_verifier_distinguishes_provider_outage_from_auth_scope_and_expiry_refusal(self):
        for status, metadata, kind in (
            (0, None, "unavailable"),
            (408, {}, "unavailable"),
            (429, {}, "unavailable"),
            (503, {}, "unavailable"),
            (200, None, "unavailable"),
            (401, {}, "refused"),
            (403, {}, "refused"),
            (200, dict(self.metadata, expiresAt=None), "refused"),
            (200, dict(self.metadata, workspace="foreign"), "refused"),
        ):
            with (
                self.subTest(status=status, kind=kind),
                mock.patch.object(core, "_api_json", return_value=(status, metadata)),
            ):
                with self.assertRaises(core.SecretsError) as raised:
                    core.verify_service_token(self.token, PROJECT, PATH, {}, writer=True)
                self.assertEqual(raised.exception.failure_kind, kind)
                self.assertNotIn(self.token, raised.exception.message)
        self.assertFalse(self.directory.exists())

    def test_poisoned_writer_dependency_origin_refuses_without_provider_access(self):
        with (
            mock.patch.dict(
                sys.modules,
                {"_fleet_infisical_write_ledger": SimpleNamespace(__file__="/wrong/origin.py")},
            ),
            self.assertRaisesRegex(core.SecretsError, "write-state-unavailable"),
        ):
            self.put()
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.calls, [])

    def test_missing_database_after_unknown_never_recreates_history_or_dispatches(self):
        self.statuses, self.no_ack = [0], True
        self.assert_outcome("outcome_unknown", self.put)
        database = self.directory / "nonces.sqlite3"
        database.unlink()
        before = list(self.calls)
        for call in (lambda: self.put(self.new_intent()), self.reconcile):
            with self.assertRaisesRegex(core.SecretsError, "write-state-unavailable"):
                call()
            self.assertFalse(database.exists())
        self.assertEqual(self.calls, before)

    def test_remote_plaintext_provider_refuses_before_state_or_network(self):
        for provider in (
            "http://provider.invalid/api",
            "http://192.0.2.1/api",
            "http://[2001:db8::1]/api",
        ):
            with self.subTest(provider=provider):
                self.env[core.ENV_API] = provider
                with self.assertRaisesRegex(core.SecretsError, "write-provider-invalid"):
                    self.put()
                self.assertEqual(self.calls, [])
                self.assertFalse(self.directory.exists())
        for provider in ("http://127.0.0.1:43210/api", "http://[::1]:43210/api"):
            self.assertEqual(core._write_provider({core.ENV_API: provider}), provider)

    def test_provider_case_default_port_and_trailing_dot_do_not_bypass_unknown_barrier(self):
        self.statuses, self.no_ack = [0], True
        self.assert_outcome("outcome_unknown", self.put)
        before = list(self.calls)
        for provider in (
            "https://APP.INFISICAL.COM/api",
            "https://app.infisical.com:443/api/",
            "https://app.infisical.com./api",
        ):
            self.env[core.ENV_API] = provider
            self.assert_outcome("prior_outcome_unknown", lambda: self.put(self.new_intent()))
        self.env[core.ENV_PROJECT] = PROJECT.upper()
        self.assert_outcome("prior_outcome_unknown", lambda: self.put(self.new_intent()))
        self.assertEqual(self.calls, before)

    def test_same_uuid_cannot_bind_a_second_folder_provider_or_project(self):
        self.put()
        for key, value in (
            (core.ENV_PROJECT, str(uuid.uuid4())),
            (core.ENV_API, "https://another.example/api"),
        ):
            with self.subTest(key=key), mock.patch.dict(self.env, {key: value}):
                self.assert_outcome("intent_conflict", self.put)
        self.assert_outcome(
            "intent_conflict",
            lambda: core.put_secret(
                "GH_TOKEN", "/cli-logins/CODEX", self.value, self.env, intent=self.intent
            ),
        )
        self.assertEqual(self.mutations, ["PATCH"])

    def test_shortened_terminal_expiry_cannot_prune_tombstone_or_allow_old_id_to_write(self):
        self.assert_outcome("cancelled", self.reconcile)
        ledger = core._write_ledger(self.directory)
        with contextlib.closing(ledger.connect()) as db, db:
            db.execute("UPDATE nonces SET expires=1")
        self.assert_outcome("state_unavailable", lambda: self.put(self.new_intent()))
        self.assert_outcome("state_unavailable", self.put)
        self.assertEqual(self.calls, [])

    def test_concurrent_callers_hold_same_lock_and_fresh_second_cannot_pass_unknown_first(self):
        sending, release = threading.Event(), threading.Event()
        original = self.api

        def blocked(method, *args, **kwargs):
            if method == "PATCH":
                sending.set()
                self.assertTrue(release.wait(3))
            return original(method, *args, **kwargs)

        self.statuses, self.no_ack = [0], True
        with (
            mock.patch.object(core, "_api_json", blocked),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(self.assert_outcome, "outcome_unknown", self.put)
            self.assertTrue(sending.wait(3))
            second = pool.submit(
                self.assert_outcome, "prior_outcome_unknown", lambda: self.put(self.new_intent())
            )
            release.set()
            first.result(timeout=5)
            second.result(timeout=5)
        self.assertEqual(self.mutations, ["PATCH"])

    def test_missing_intent_and_bad_identity_are_refused_before_state_or_transport(self):
        with self.assertRaisesRegex(core.SecretsError, "write-intent-required"):
            core.put_secret(NAME, PATH, self.value, self.env)
        for request_id in ("bad", "", None):
            with self.assertRaisesRegex(core.SecretsError, "write-intent-invalid"):
                core.WriteIntent(self.directory, "test-writer", request_id, 1)
        for created in (0, True, 1 << 63):
            with self.assertRaisesRegex(core.SecretsError, "write-intent-invalid"):
                core.WriteIntent(self.directory, "test-writer", self.intent.request_id, created)
        with self.assertRaisesRegex(core.SecretsError, "write-intent-invalid"):
            self.put(self.intent._replace(request_id="malformed"))
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.calls, [])

    def test_trailing_newline_name_is_refused_by_both_shared_gates_before_state_or_provider(self):
        with self.assertRaises(core.SecretsError):
            core.check_name(NAME + "\n")
        with self.assertRaises(core.SecretsError):
            core.check_writable(NAME + "\n", PATH)
        with self.assertRaises(core.SecretsError):
            core.put_secret(NAME + "\n", PATH, self.value, self.env, intent=self.intent)
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.calls, [])

    def test_value_and_names_routes_reject_present_nonboolean_hidden_flags(self):
        metadata = dict(self.metadata, permissions=["read"])
        for hidden in (1, 0, "true", "false", None):
            for names_only in (False, True):
                record = dict(
                    secretKey=NAME,
                    type="shared",
                    workspace=PROJECT,
                    environment="prod",
                    secretPath=PATH,
                    secretValueHidden=hidden,
                )
                if not names_only:
                    record["secretValue"] = self.value

                def respond(method, endpoint, *args, **kwargs):
                    if endpoint == "/v2/service-token":
                        return 200, metadata
                    return 200, {"secrets": [record]} if names_only else {"secret": record}

                with (
                    self.subTest(hidden=hidden, names_only=names_only),
                    mock.patch.object(core, "_api_json", respond),
                ):
                    with self.assertRaisesRegex(core.SecretsError, "unverified-source"):
                        if names_only:
                            core.list_project_names(PROJECT, PATH, {}, token=self.token)
                        else:
                            core.fetch_project(PROJECT, PATH, {}, token=self.token, names=(NAME,))
        self.assertFalse(self.directory.exists())


class ReadOnlyInstallTests(unittest.TestCase):
    def test_actual_linux_entrypoint_help_works_with_spec_loaded_fi_and_no_auth(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            (directory / "fleet_infisical.py").write_bytes(Path(core.__file__).read_bytes())
            entry = directory / "linux-entrypoint.py"
            entry.write_bytes(
                (Path(core.__file__).parent / "fleet_secrets" / "linux-entrypoint.py").read_bytes()
            )
            delegate = directory / "fleet-secrets-cli"
            delegate.write_text("#!/bin/sh\nprintf 'isolated help succeeded\\n'\n")
            delegate.chmod(0o700)
            run = subprocess.run(
                [sys.executable, "-I", "-B", str(entry), "--help"],
                capture_output=True,
                text=True,
                env={},
                timeout=10,
            )
            self.assertEqual(
                (run.returncode, run.stdout, run.stderr), (0, "isolated help succeeded\n", "")
            )
            self.assertEqual(
                sorted(path.name for path in directory.iterdir()),
                ["fleet-secrets-cli", "fleet_infisical.py", "linux-entrypoint.py"],
            )

    def test_fi_only_install_reads_without_importing_sqlite_or_creating_writer_state(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root) / "fleet_infisical.py"
            target.write_bytes(Path(core.__file__).read_bytes())
            program = r"""
import builtins, sys, uuid
sys.path.insert(0, sys.argv[1])
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name == 'sqlite3' or name.startswith('simple_tools'):
        raise AssertionError('read-only client loaded writer dependency')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import fleet_infisical as f
project = str(uuid.uuid4())
def api(method, path, token, body, env, **kwargs):
    if path == '/v2/service-token':
        return 200, dict(workspace=project, permissions=['read'], scopes=[dict(environment='prod', secretPath='/')], expiresAt='2099-01-01T00:00:00Z')
    return 200, dict(secret=dict(secretKey='EXAMPLE', secretValue='synthetic', type='shared', workspace=project, environment='prod', secretPath='/'))
f._api_json = api
assert f.fetch_project(project, '/', {}, token='st.' + 'x' * 32, names=('EXAMPLE',)) == {'EXAMPLE':'synthetic'}
assert 'sqlite3' not in sys.modules
"""
            run = subprocess.run(
                [sys.executable, "-I", "-B", "-c", program, root],
                capture_output=True,
                text=True,
                env={},
                timeout=10,
            )
            self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
            self.assertEqual([entry.name for entry in Path(root).iterdir()], ["fleet_infisical.py"])


if __name__ == "__main__":
    unittest.main()
