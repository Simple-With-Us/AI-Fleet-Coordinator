"""Masked doctor counts use the real strict reader, JSON transport and verifier."""

import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import fleet_infisical as core
import fleet_infisical_cli as cli

import fleet_secrets.tests.test_run_named_reads as transport_fixture


class DoctorDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.transport = transport_fixture.RunNamedReadTests()
        self.transport.setUp()
        self.addCleanup(self.transport.doCleanups)
        patch = mock.patch.object(core, "find_cli", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)
        self.env = self.transport.env
        self.transport.values["SECOND_REFERENCE"] = "prefix-${prod.other.ABSENT}-suffix"

    def doctor(self, *selection):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["doctor", "--json", *selection], self.env, out, err)
        return code, json.loads(out.getvalue()), out.getvalue() + err.getvalue()

    def test_selected_doctor_counts_all_references_and_never_returns_values(self):
        code, report, emitted = self.doctor("--project", "fleet-ops", "--path", "/")
        self.assertEqual(code, 1)
        self.assertEqual(
            (report["fetch_names_ok"], report["names_count"], report["unresolved_reference_count"]),
            (True, 4, 2),
        )
        self.assertEqual(report["failure_reason"], "unresolved-reference")
        self.assertFalse(report["ok"])
        self.assertTrue(
            all(type(value) in (bool, int, str, type(None)) for value in report.values())
        )
        for secret in (self.transport.token, *self.transport.values.values()):
            self.assertNotIn(secret, emitted)
        self.assertNotIn("SECOND_REFERENCE", emitted)
        self.assertEqual(self.transport.requests, ["/v2/service-token", "/v3/secrets/raw"])

    def test_default_doctor_preserves_primary_diagnostic_counts(self):
        code, report, _ = self.doctor()
        self.assertEqual(
            (code, report["names_count"], report["unresolved_reference_count"]), (1, 4, 2)
        )
        self.assertFalse(report["ok"])
        self.assertIn("ai_fleet_coordinator_fetch_ok", report)

    def test_actual_default_get_and_run_still_refuse_unresolved_values(self):
        for args in (
            ["get", "UNREQUESTED"],
            ["run", "--", "synthetic-command"],
            ["run", "--only", "SECOND_REFERENCE", "--", "synthetic-command"],
        ):
            with self.subTest(args=args):
                out, err = io.StringIO(), io.StringIO()
                self.assertEqual(cli.main(args, self.env, out, err), 5)
                self.assertEqual(out.getvalue(), "")
                self.assertIn("unresolved-reference", err.getvalue())
        self.assertFalse(self.transport.executions)

    def test_unverified_grant_never_becomes_successful_diagnostic_counts(self):
        self.transport.metadata["permissions"] = ["read", "write"]
        code, report, _ = self.doctor("--path", "/")
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertFalse(report["fetch_names_ok"])
        self.assertEqual((report["names_count"], report["unresolved_reference_count"]), (0, 0))
        self.assertEqual(self.transport.requests, ["/v2/service-token"])

    def test_unverified_row_never_becomes_successful_diagnostic_counts(self):
        def corrupt_response(request, *, timeout):
            response = self.transport.http(request, timeout=timeout)
            with response:
                payload = json.loads(response.read())
            if "secrets" in payload:
                payload["secrets"][0]["workspace"] = "wrong-project"
            response = io.BytesIO(json.dumps(payload).encode())
            response.status = 200
            return response

        with mock.patch.object(
            core, "_opener", return_value=SimpleNamespace(open=corrupt_response)
        ):
            code, report, _ = self.doctor("--path", "/")
        self.assertEqual(code, 1)
        self.assertEqual(report["failure_reason"], "unverified-source")
        self.assertFalse(report["fetch_names_ok"])
        self.assertEqual((report["names_count"], report["unresolved_reference_count"]), (0, 0))

    def test_count_metadata_rejects_unbounded_and_noninteger_values(self):
        for total, unresolved in (
            (True, 1),
            (2, True),
            (2.0, 1),
            (-1, 1),
            (1, 2),
            (core.MAX_JSON_NODES + 1, 1),
        ):
            with self.subTest(total=total, unresolved=unresolved):
                with self.assertRaisesRegex(core.SecretsError, "diagnostic-count-invalid"):
                    core.UnresolvedReferenceError("FIXTURE", total, unresolved)


if __name__ == "__main__":
    unittest.main()
