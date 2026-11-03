#!/usr/bin/env python3
"""Offline contract fixtures only; never read custody, process env, or the network."""

import copy
import datetime as dt
from email.message import Message
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("alert_binding_audit", ROOT / "scripts/audit-alert-infisical-binding.py")
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)
FIXTURE = json.loads((ROOT / "scripts/tests/fixtures/alert-binding-audit/valid-self-metadata.json").read_text())
NONEXPIRING_FIXTURE = json.loads((ROOT / "scripts/tests/fixtures/alert-binding-audit/nonexpiring-self-metadata.json").read_text())
SYNTHETIC_TOKEN = "st." + "synthetic_" * 4
MARKER = "PRIVATE_RESPONSE_MUST_NEVER_ESCAPE"
NOW = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)


def environment():
    return {
        AUDIT.TOKEN_KEY: SYNTHETIC_TOKEN,
        AUDIT.PROJECT_KEY: FIXTURE["workspace"],
        "AUDIT_EXPECTED_SOURCE_SHA": "a" * 40,
        "GITHUB_SHA": "a" * 40,
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "GITHUB_REPOSITORY": "Simple-With-Us/AI-Fleet-Coordinator",
        "GITHUB_REF": "refs/heads/main",
    }


class Response:
    def __init__(self, body=None, *, status=200, content_type="application/json", length=None):
        self.body = json.dumps(FIXTURE).encode() if body is None else body
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        if length is not None:
            self.headers["Content-Length"] = length
        self.reads = []

    def read(self, size):
        self.reads.append(size)
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response or Response()
        self.error = error
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


class ContractTests(unittest.TestCase):
    def invoke(self, *, env=None, metadata=None, response=None, error=None, argv=None):
        if metadata is not None:
            response = Response(json.dumps(metadata).encode())
        opener = Opener(response, error)
        output = io.StringIO()
        code = AUDIT.main(["--audit"] if argv is None else argv, env=environment() if env is None else env,
                          opener=opener, now=NOW, output=output)
        text = output.getvalue()
        self.assertNotIn(SYNTHETIC_TOKEN, text)
        self.assertNotIn(hashlib.sha256(SYNTHETIC_TOKEN.encode()).hexdigest(), text)
        self.assertNotIn(MARKER, text)
        return code, json.loads(text), opener

    def assert_refusal(self, expected, **kwargs):
        code, result, opener = self.invoke(**kwargs)
        self.assertEqual((code, result["status"]), (2, expected))
        self.assertFalse(result["grant_verified"])
        self.assertFalse(result["purpose_verified"])
        self.assertFalse(result["expiry_verified"])
        self.assertFalse(result["nonexpiring"])
        for key in ("token_id_sha256", "token_name_sha256", "project_id_sha256"):
            self.assertEqual(result[key], "")
        self.assertLessEqual(len(opener.calls), 1)
        return result, opener

    def test_valid_fixture_gets_only_self_metadata_and_hashes_validated_identity(self):
        code, result, opener = self.invoke()
        self.assertEqual((code, result["status"]), (0, "verified"))
        self.assertTrue(result["grant_verified"])
        self.assertTrue(result["expiry_verified"])
        self.assertFalse(result["nonexpiring"])
        self.assertFalse(result["purpose_verified"])
        self.assertEqual(result["token_id_sha256"], hashlib.sha256(FIXTURE["id"].encode()).hexdigest())
        self.assertEqual(result["token_name_sha256"], hashlib.sha256(FIXTURE["name"].encode()).hexdigest())
        self.assertEqual(result["project_id_sha256"], hashlib.sha256(FIXTURE["workspace"].encode()).hexdigest())
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual((request.get_method(), request.full_url, request.data), ("GET", AUDIT.ENDPOINT, None))
        self.assertEqual(request.get_header("Authorization"), "Bearer " + SYNTHETIC_TOKEN)
        self.assertEqual(timeout, 10)
        self.assertEqual(opener.response.reads, [AUDIT.MAX_BYTES + 1])

    def test_output_has_exact_schema_no_raw_inventory(self):
        _, result, _ = self.invoke()
        self.assertEqual(set(result), {
            "schema", "status", "source_verified", "token_delivered", "project_delivered",
            "grant_verified", "purpose_verified", "expiry_verified", "nonexpiring", "token_id_sha256",
            "token_name_sha256", "project_id_sha256",
        })
        text = json.dumps(result)
        for value in (FIXTURE["id"], FIXTURE["name"], FIXTURE["workspace"], FIXTURE["expiresAt"]):
            self.assertNotIn(value, text)

    def test_missing_effective_bindings_mean_missing_delivery_not_object_absence(self):
        for key in (AUDIT.TOKEN_KEY, AUDIT.PROJECT_KEY):
            for value in (None, "", "   "):
                with self.subTest(key=key, value=value):
                    env = environment()
                    env[key] = value
                    result, opener = self.assert_refusal("missing_delivery", env=env)
                    self.assertFalse(result["token_delivered" if key == AUDIT.TOKEN_KEY else "project_delivered"])
                    self.assertEqual(opener.calls, [])

    def test_source_guard_prevents_network_for_missing_or_wrong_context(self):
        for key, value in {
            "AUDIT_EXPECTED_SOURCE_SHA": "", "GITHUB_SHA": "b" * 40,
            "GITHUB_EVENT_NAME": "push", "GITHUB_REPOSITORY": "other/repo",
            "GITHUB_REF": "refs/heads/feature",
        }.items():
            with self.subTest(key=key):
                env = environment()
                env[key] = value
                _, opener = self.assert_refusal("source_unverified", env=env)
                self.assertEqual(opener.calls, [])

    def test_other_tokens_machine_identity_and_endpoint_knobs_are_not_fallbacks(self):
        env = environment()
        env[AUDIT.TOKEN_KEY] = ""
        env.update(INFISICAL_TOKEN=SYNTHETIC_TOKEN, INFISICAL_CLIENT_ID=MARKER,
                   INFISICAL_CLIENT_SECRET=MARKER, INFISICAL_SITE_URL="https://other.invalid")
        _, opener = self.assert_refusal("missing_delivery", env=env)
        self.assertEqual(opener.calls, [])
        env[AUDIT.TOKEN_KEY] = SYNTHETIC_TOKEN
        _, _, opener = self.invoke(env=env)
        self.assertEqual(opener.calls[0][0].full_url, AUDIT.ENDPOINT)

    def test_expected_source_requires_exact_lowercase_commit_sha(self):
        for value in ("a" * 39, "A" * 40, "main", MARKER, None):
            env = environment()
            env["AUDIT_EXPECTED_SOURCE_SHA"] = value
            _, opener = self.assert_refusal("source_unverified", env=env)
            self.assertEqual(opener.calls, [])

    def test_explicit_audit_invocation_required(self):
        for argv in ([], ["--identity"], ["--audit", "--endpoint", MARKER]):
            _, opener = self.assert_refusal("invalid_invocation", argv=argv)
            self.assertEqual(opener.calls, [])

    def test_invalid_bindings_do_not_attempt_login_or_any_request(self):
        for key, value in ((AUDIT.TOKEN_KEY, "machine-identity"), (AUDIT.TOKEN_KEY, "st.\n" + MARKER),
                           (AUDIT.PROJECT_KEY, MARKER), (AUDIT.PROJECT_KEY, "../../")):
            env = environment()
            env[key] = value
            _, opener = self.assert_refusal("invalid_binding", env=env)
            self.assertEqual(opener.calls, [])

    def test_metadata_identity_is_required_and_aliases_cannot_contradict(self):
        for key, value in (("id", None), ("id", MARKER), ("name", ""), ("name", "bad\nname"),
                           ("name", SYNTHETIC_TOKEN), ("name", "st.reflected"),
                           ("workspace", None), ("workspace", MARKER), ("_id", "3" * 36)):
            with self.subTest(key=key, value=value):
                metadata = copy.deepcopy(FIXTURE)
                metadata[key] = value
                self.assert_refusal("invalid_metadata", metadata=metadata)

    def test_wrong_project_or_explicit_project_alias_refused(self):
        for key in ("workspace", "projectId"):
            metadata = copy.deepcopy(FIXTURE)
            metadata[key] = "33333333-3333-4333-8333-333333333333"
            self.assert_refusal("grant_mismatch", metadata=metadata)

    def test_exact_singleton_scope_required(self):
        for scopes in (None, [], [{"environment": "staging", "secretPath": "/"}],
                       [{"environment": "prod", "secretPath": "/*"}],
                       [{"environment": "prod", "secretPath": "/nested"}],
                       FIXTURE["scopes"] * 2,
                       [{"environment": "prod", "secretPath": "/", "recursive": True}]):
            with self.subTest(scopes=scopes):
                metadata = copy.deepcopy(FIXTURE)
                metadata["scopes"] = scopes
                self.assert_refusal("grant_mismatch", metadata=metadata)

    def test_read_only_permission_must_be_exact_singleton(self):
        for permissions in (None, "read", [], ["write"], ["read", "write"], ["read", "read"]):
            metadata = copy.deepcopy(FIXTURE)
            metadata["permissions"] = permissions
            self.assert_refusal("grant_mismatch", metadata=metadata)

    def test_null_or_absent_expiry_reports_nonexpiring_without_verifying_purpose(self):
        for omitted in (False, True):
            with self.subTest(omitted=omitted):
                metadata = copy.deepcopy(NONEXPIRING_FIXTURE)
                if omitted:
                    metadata.pop("expiresAt")
                code, result, opener = self.invoke(metadata=metadata)
                self.assertEqual((code, result["status"]), (0, "verified"))
                self.assertTrue(result["grant_verified"])
                self.assertTrue(result["expiry_verified"])
                self.assertTrue(result["nonexpiring"])
                self.assertFalse(result["purpose_verified"])
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(result["token_id_sha256"], hashlib.sha256(FIXTURE["id"].encode()).hexdigest())

    def test_non_null_expiry_must_be_well_formed_timezone_aware_and_future(self):
        for expiry in ("", 123, MARKER, "2099-01-01", "2099-01-01T00:00:00",
                       "2099-13-01T00:00:00Z", "2099-01-01T00:00:00+99:00",
                       "2020-01-01T00:00:00Z", NOW.isoformat()):
            with self.subTest(expiry=expiry):
                metadata = copy.deepcopy(FIXTURE)
                metadata["expiresAt"] = expiry
                self.assert_refusal("invalid_expiry", metadata=metadata)

    def test_nonexpiring_metadata_still_requires_the_exact_identity_and_grant(self):
        for key, value, status in (
            ("id", MARKER, "invalid_metadata"),
            ("workspace", "33333333-3333-4333-8333-333333333333", "grant_mismatch"),
            ("scopes", [{"environment": "prod", "secretPath": "/*"}], "grant_mismatch"),
            ("permissions", ["read", "write"], "grant_mismatch"),
        ):
            with self.subTest(key=key):
                metadata = copy.deepcopy(NONEXPIRING_FIXTURE)
                metadata[key] = value
                self.assert_refusal(status, metadata=metadata)

    def test_expiry_offset_is_respected(self):
        metadata = copy.deepcopy(FIXTURE)
        metadata["expiresAt"] = "2026-11-03T00:01:00+01:00"
        self.assert_refusal("invalid_expiry", metadata=metadata)

    def test_impossible_numeric_offsets_are_rejected_before_datetime_parsing(self):
        for offset in ("+00:60", "+00:99", "-00:60", "-00:99", "+24:00", "-24:00", "+99:99"):
            with self.subTest(offset=offset):
                metadata = copy.deepcopy(FIXTURE)
                metadata["expiresAt"] = "2099-01-01T00:00:00" + offset
                with patch.object(AUDIT, "dt") as dates:
                    self.assert_refusal("invalid_expiry", metadata=metadata)
                    dates.datetime.fromisoformat.assert_not_called()

    def test_valid_nonzero_offsets_are_accepted(self):
        for offset in ("+05:30", "-04:00", "+00:59", "-00:59", "+23:59", "-23:59"):
            with self.subTest(offset=offset):
                metadata = copy.deepcopy(FIXTURE)
                metadata["expiresAt"] = "2099-01-01T00:00:00" + offset
                code, result, opener = self.invoke(metadata=metadata)
                self.assertEqual((code, result["status"]), (0, "verified"))
                self.assertTrue(result["expiry_verified"])
                self.assertFalse(result["nonexpiring"])
                self.assertFalse(result["purpose_verified"])
                self.assertEqual(len(opener.calls), 1)

    def test_hostile_extra_fields_never_reach_receipt(self):
        metadata = copy.deepcopy(FIXTURE)
        metadata.update(secretValue=MARKER, clientSecret=MARKER, token=SYNTHETIC_TOKEN, url=MARKER)
        code, _, _ = self.invoke(metadata=metadata)
        self.assertEqual(code, 0)

    def test_non_object_and_missing_metadata_refused(self):
        for metadata in ([], [FIXTURE], True, "value", {}):
            self.assert_refusal("invalid_metadata", response=Response(json.dumps(metadata).encode()))

    def test_duplicate_keys_nonfinite_json_invalid_utf8_and_malformed_json_refused(self):
        for body in (b'{"id":1,"id":2}', b'{"x":NaN}', b'{"x":Infinity}', b'\xff', b'{"x":'):
            self.assert_refusal("invalid_json", response=Response(body))

    def test_response_bound_covers_declared_and_streamed_oversize(self):
        response = Response(length=str(AUDIT.MAX_BYTES + 1))
        self.assert_refusal("response_too_large", response=response)
        self.assertEqual(response.reads, [])
        self.assert_refusal("response_too_large", response=Response(b"x" * (AUDIT.MAX_BYTES + 1)))

    def test_invalid_content_type_or_length_is_not_parsed(self):
        for response in (Response(content_type="text/html"), Response(length=MARKER), Response(length="-1")):
            self.assert_refusal("invalid_metadata", response=response)
            self.assertEqual(response.reads, [])

    def test_provider_errors_and_redirects_are_never_read_or_retried(self):
        for status in (300, 301, 302, 303, 307, 308, 401, 403, 404, 429, 500):
            with self.subTest(status=status):
                error = urllib.error.HTTPError(AUDIT.ENDPOINT, status, MARKER, Message(), io.BytesIO(MARKER.encode()))
                _, opener = self.assert_refusal("redirect_refused" if status < 400 else "metadata_http_error", error=error)
                self.assertEqual(len(opener.calls), 1)

    def test_non_success_response_body_is_never_read(self):
        response = Response(MARKER.encode(), status=503)
        self.assert_refusal("metadata_http_error", response=response)
        self.assertEqual(response.reads, [])

    def test_actual_redirect_handler_refuses_without_constructing_followup(self):
        with self.assertRaises(AUDIT.Refusal) as raised:
            AUDIT.NoRedirect().redirect_request(None, None, 302, MARKER, {}, "https://other.invalid/" + MARKER)
        self.assertEqual(raised.exception.code, "redirect_refused")

    def test_timeouts_and_transport_errors_are_sanitized_without_retry(self):
        for error, expected in ((TimeoutError(MARKER), "request_timeout"),
                                (urllib.error.URLError(socket.timeout(MARKER)), "request_timeout"),
                                (urllib.error.URLError(MARKER), "metadata_unavailable"),
                                (ValueError(MARKER), "audit_failed"),
                                (AUDIT.Refusal(MARKER), "audit_failed")):
            _, opener = self.assert_refusal(expected, error=error)
            self.assertEqual(len(opener.calls), 1)

    def test_read_timeout_is_sanitized_without_retry(self):
        response = Response()
        with patch.object(response, "read", side_effect=TimeoutError(MARKER)):
            _, opener = self.assert_refusal("request_timeout", response=response)
        self.assertEqual(len(opener.calls), 1)

    def test_default_transport_disables_ambient_proxy_and_installs_redirect_refusal(self):
        opener = Opener()
        with patch.object(AUDIT.urllib.request, "build_opener", return_value=opener) as build:
            result = AUDIT.audit(environment(), now=NOW)
        self.assertEqual(result["status"], "verified")
        handlers = build.call_args.args
        self.assertTrue(any(isinstance(handler, AUDIT.NoRedirect) for handler in handlers))
        proxy = next(handler for handler in handlers if isinstance(handler, AUDIT.urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})

    def test_workflow_is_separate_manual_only_with_two_fixed_secret_bindings(self):
        workflow = (ROOT / ".github/workflows/alert-infisical-binding-audit.yml").read_text()
        self.assertIn("  workflow_dispatch:", workflow)
        for trigger in ("  push:", "  pull_request:", "  schedule:", "  workflow_run:", "  workflow_call:"):
            self.assertNotIn(trigger, workflow)
        self.assertEqual(workflow.count("${{ secrets."), 2)
        for key in (AUDIT.TOKEN_KEY, AUDIT.PROJECT_KEY):
            self.assertIn(key + ": ${{ secrets." + key + " }}", workflow)
        self.assertEqual(workflow.count("uses:"), 1)
        self.assertIn("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", workflow)
        self.assertIn("ref: ${{ github.sha }}", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("if: github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'", workflow)
        self.assertIn("  contents: read", workflow)
        self.assertNotIn("write", workflow)
        self.assertNotIn("GITHUB_ENV", workflow)
        self.assertNotIn("infisical-fetch.mjs", workflow)
        self.assertLess(workflow.index("Verify Approved Source"), workflow.index("${{ secrets."))
        self.assertLess(workflow.index("test-audit-alert-infisical-binding.py"), workflow.index("${{ secrets."))
        self.assertNotIn("${{ inputs.", workflow.split("run: |", 1)[1].split("- uses:", 1)[0])


if __name__ == "__main__":
    unittest.main()
