"""Offline-only: hostile upstream metadata must not become retained prose."""

import io
import json
import os
import tempfile
import unittest
import urllib.error

from git_relay.audit import AuditLog
from git_relay.github_client import GitHubClient, RelayGitHubError, safe_error_diagnostics
from git_relay.policy import SeatPolicy
from git_relay.server import Relay
from git_relay.tests import test_git_relay as fixtures


class TestErrorDiagnostics(unittest.TestCase):
    def test_valid_metadata_and_fixed_category(self):
        self.assertEqual(
            safe_error_diagnostics(
                {
                    "X-GitHub-Request-Id": "ABCD:12345678:ABCDEF:12345678",
                    "X-Accepted-GitHub-Permissions": "issues=write, pull_requests=read",
                },
                "Resource not accessible by integration",
            ),
            {
                "category": "resource_not_accessible",
                "request_id": "ABCD:12345678:ABCDEF:12345678",
                "accepted_permissions": {"issues": "write", "pull_requests": "read"},
            },
        )

    def test_hostile_reflections_are_discarded(self):
        secrets = [
            "ghs_" + "x" * 40,
            "sbt_ECHO." + "q" * 50,
            "private comment text",
            "ABCD:1234:1234:1234\nsecret",
            "x" * 10000,
        ]
        for secret in secrets:
            with self.subTest(secret=secret[:12]):
                result = safe_error_diagnostics(
                    {
                        "X-GitHub-Request-Id": secret,
                        "X-Accepted-GitHub-Permissions": "issues=write," + secret,
                    },
                    secret,
                )
                self.assertEqual(result, {"category": "unknown"})
                self.assertNotIn(secret, json.dumps(result))

    def test_bad_permissions_reject_whole_header(self):
        for raw in [
            "issues=admin",
            "unknown=write",
            "issues=write;token=abc",
            "issues=write,issues=read",
            "issues =write",
            "issues=write\n",
            "issues=write," + "x" * 513,
            "",
            None,
        ]:
            result = safe_error_diagnostics({"X-Accepted-GitHub-Permissions": raw})
            self.assertNotIn("accepted_permissions", result)

    def test_exception_revalidates_injected_diagnostics(self):
        err = RelayGitHubError(
            403,
            "secret body",
            diagnostics={
                "category": ["hostile"],
                "request_id": "secret body",
                "accepted_permissions": {"issues": "write", "token": "sbt_secret"},
                "body": "secret body",
            },
        )
        self.assertEqual(err.diagnostics, {"category": "unknown"})

    def test_real_client_captures_headers_not_body(self):
        def fail(req, timeout):
            raise urllib.error.HTTPError(
                req.full_url,
                403,
                "Forbidden",
                {
                    "X-GitHub-Request-Id": "ABCD:1234:ABCD:12345678",
                    "X-Accepted-GitHub-Permissions": "issues=write",
                },
                io.BytesIO(
                    json.dumps(
                        {
                            "message": "Resource not accessible by integration",
                            "body": "private comment/token",
                        }
                    ).encode()
                ),
            )

        with self.assertRaises(RelayGitHubError) as ctx:
            GitHubClient("owner-fixture", opener=fail).comment_pr("example/fixture-repo", 1, "text")
        self.assertEqual(ctx.exception.diagnostics["category"], "resource_not_accessible")
        self.assertEqual(ctx.exception.diagnostics["accepted_permissions"], {"issues": "write"})
        self.assertNotIn("private comment", json.dumps(ctx.exception.diagnostics))

    def test_response_audit_and_outcome_semantics_preserved(self):
        for upstream, expected, outcome in [
            (403, 400, "github_terminal"),
            (503, 502, "github_unknown"),
            (429, 429, "github_retryable"),
        ]:
            client = fixtures.TestGithubErrorSplit.ErrClient(upstream)
            client._err = RelayGitHubError(
                upstream,
                "private comment token",
                diagnostics={
                    "category": "resource_not_accessible",
                    "request_id": "ABCD:1234:ABCD:12345678",
                    "accepted_permissions": {"issues": "write"},
                },
            )
            with tempfile.TemporaryDirectory() as tmp:
                path = os.path.join(tmp, "audit")
                relay = Relay(SeatPolicy(fixtures.SEATS_JSON), client, AuditLog(path))
                status, payload = relay.handle(
                    fixtures.SEAT_TOKEN,
                    "open_pr",
                    {
                        "repo": "example/fixture-repo",
                        "base": "main",
                        "head": "b",
                        "title": "t",
                        "body": "x",
                    },
                )
                self.assertEqual(status, expected)
                with open(path) as fh:
                    row = json.loads(fh.read())
                self.assertEqual(row["result"], outcome)
                self.assertEqual(row["github_diagnostics"], payload["github_diagnostics"])
                self.assertNotIn("private comment token", json.dumps([row, payload]))
                self.assertEqual(payload["retryable"], upstream == 429)

    def test_invalid_grant_stays_retryable_without_reflected_metadata(self):
        client = fixtures.TestGithubErrorSplit.ErrClient(401)
        client._err = RelayGitHubError(
            401,
            "do not retain this body",
            code="invalid_grant",
            diagnostics={"request_id": "ghs_reflection", "category": "sbt_reflection"},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit")
            relay = Relay(SeatPolicy(fixtures.SEATS_JSON), client, AuditLog(path))
            status, payload = relay.handle(
                fixtures.SEAT_TOKEN,
                "open_pr",
                {
                    "repo": "example/fixture-repo",
                    "base": "main",
                    "head": "b",
                    "title": "t",
                    "body": "x",
                },
            )
            self.assertEqual(status, 503)
            self.assertTrue(payload["retryable"])
            self.assertEqual(payload["github_diagnostics"], {"category": "unknown"})
            with open(path) as fh:
                row = json.loads(fh.read())
            self.assertEqual(row["result"], "github_auth")
            self.assertNotIn("reflection", json.dumps([row, payload]))

    def test_alternatives_preserve_sets_and_revalidate(self):
        expected = [{"issues": "write"}, {"pull_requests": "write", "contents": "read"}]
        d = safe_error_diagnostics({"X-Accepted-GitHub-Permissions":
                                    "issues=write; pull_requests=write,contents=read"})
        self.assertEqual(d["accepted_permission_sets"], expected)
        self.assertNotIn("accepted_permissions", d)
        self.assertEqual(RelayGitHubError(403, "x", diagnostics=d).diagnostics, d)

    def test_one_poisoned_alternative_rejects_whole_header(self):
        for raw in ["token=secret;issues=write", "issues=write;token=secret",
                    "token=secret;unknown=read", "issues=write;", ";issues=write",
                    ";".join(["issues=write"] * 5), "issues=write;" + "x" * 513]:
            self.assertEqual(safe_error_diagnostics({"X-Accepted-GitHub-Permissions": raw}),
                             {"category": "unknown"})
        err = RelayGitHubError(403, "x", diagnostics={"accepted_permission_sets":
                                                    [{"issues": "write"}, {"secret": "read"}]})
        self.assertEqual(err.diagnostics, {"category": "unknown"})

    def test_category_variants_are_narrow_not_reflections(self):
        for message in ["API rate limit exceeded", "API rate limit exceeded for user ID 123.",
                        "API rate limit exceeded for installation ID 456."]:
            self.assertEqual(safe_error_diagnostics(message=message), {"category": "rate_limit"})
        for message in ["Not Founder secret", "Bad credentialsXYZ", "Bad credentials: secret",
                        "Resource not accessible by integration: https://docs.example/secret",
                        "API rate limit exceeded for user ID abc.",
                        "API rate limit exceeded for user ID 123. secret",
                        "API rate limit exceeded for user ID " + "1" * 21 + ".",
                        "Not Found: " + "x" * 600, "Not Found\nsecret", ["Not Found"]]:
            self.assertEqual(safe_error_diagnostics(message=message), {"category": "unknown"})

    def test_hostile_alternatives_do_not_reach_response_or_audit(self):
        client = fixtures.TestGithubErrorSplit.ErrClient(403)
        client._err = RelayGitHubError(403, "secret-body", diagnostics={
            "category": "secret-body", "accepted_permission_sets":
            [{"issues": "write"}, {"ghs_secret": "write"}]})
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit")
            relay = Relay(SeatPolicy(fixtures.SEATS_JSON), client, AuditLog(path))
            _, payload = relay.handle(fixtures.SEAT_TOKEN, "open_pr", {
                "repo": "example/fixture-repo", "base": "main", "head": "b", "title": "t", "body": "x"})
            with open(path) as fh:
                row = json.loads(fh.read())
            self.assertEqual(payload["github_diagnostics"], {"category": "unknown"})
            self.assertNotIn("secret", json.dumps([payload, row]))
