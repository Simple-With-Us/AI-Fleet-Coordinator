"""Review-alert relay surface:  origin claims and the service_read lane.

Covers:
  - open_pr origin claim fields (session, letter, generation):  shape-
    validated, recorded untrusted, never sent upstream.  No token field
    exists at all:  a per-session secret is never accepted, so it can never
    reach the audit log, the outcome store, or an idempotency key (the
    registry-row binding, enforced by the wake-router, replaces it);
  - the service_read lane:  dedicated bearer, repo allowlist, fixed verbs,
    narrowed fixed grants, no token export.
"""

import json
import tempfile
import unittest
from pathlib import Path

from git_relay.audit import AuditLog
from git_relay.policy import SeatPolicy
from git_relay.pr_owners import PROwnerStore
from git_relay.server import (
    SERVICE_READ_APP_SLUG,
    SERVICE_READ_GRANTS,
    Relay,
)

ECHO_TOKEN = "e" * 32
SERVICE_TOKEN = "s" * 48
ARGS = {
    "repo": "example/fixture",
    "base": "main",
    "head": "echo/fixture-change",
    "title": "Title",
    "body": "Body",
}
SESSION = "claude-9f8a1c2e-3b4d-4f01-8a2b-1c2d3e4f5a6b"
TOKEN = "tok-" + "a" * 20
POLICY = json.dumps(
    {
        "swu-echo": {
            "token": ECHO_TOKEN,
            "actions": ["open_pr"],
            "repos": [ARGS["repo"]],
            "app_slug": "swu-echo",
            "mint_permissions": {"pull_requests": "write"},
        },
    }
)


class Client:
    def __init__(self, number=1):
        self.calls = []
        self.reads = []
        self.number = number

    def open_pr(self, repo, base, head, title, body, **kwargs):
        self.calls.append(
            {
                "repo": repo,
                "base": base,
                "head": head,
                "title": title,
                "body": body,
                "kwargs": kwargs,
            }
        )
        return {
            "number": self.number,
            "url": f"https://github.com/{repo}/pull/{self.number}",
            "verified": True,
        }

    def _call(self, method, path, bearer=None, timeout=None):
        self.reads.append({"method": method, "path": path, "bearer": bearer})
        return {"ok": True}


class FakeMinter:
    def __init__(self, revoke_ok=True):
        self.mints = []
        self.revokes = []
        self.revoke_ok = revoke_ok

    def token_for_service(self, slug, repo, permissions):
        self.mints.append({"slug": slug, "repo": repo, "permissions": dict(permissions)})
        return "minted-token", 9999999999.0

    def revoke_installation_token(self, token):
        self.revokes.append(token)
        return self.revoke_ok


def make_relay(directory, client, minter=None, service_repos=(), **kwargs):
    return Relay(
        SeatPolicy(POLICY),
        client,
        AuditLog(str(Path(directory) / "audit.jsonl")),
        app_minter=minter,
        pr_owners=PROwnerStore(str(Path(directory) / "owners.jsonl")),
        service_read_token=(SERVICE_TOKEN if service_repos else None),
        service_read_repos=frozenset(service_repos),
        **kwargs,
    )


def open_pr(relay, **extra):
    args = dict(ARGS, **extra)
    return relay.handle(ECHO_TOKEN, "open_pr", args)


class ClaimTests(unittest.TestCase):
    def test_claims_recorded_untrusted(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client())
            status, _ = open_pr(
                relay,
                origin_session_claim=SESSION,
                origin_letter_claim="AB",
                origin_generation_claim=3,
            )
            self.assertEqual(status, 200)
            row = relay.pr_owners.lookup(ARGS["repo"], 1)
            self.assertEqual(row["origin_session_claim"], SESSION)
            self.assertEqual(row["origin_letter_claim"], "AB")
            self.assertEqual(row["origin_generation_claim"], 3)

    def test_claims_optional_and_default_unset(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client())
            status, _ = open_pr(relay)
            self.assertEqual(status, 200)
            row = relay.pr_owners.lookup(ARGS["repo"], 1)
            self.assertIsNone(row["origin_session_claim"])
            self.assertIsNone(row["origin_letter_claim"])
            self.assertIsNone(row["origin_generation_claim"])

    def test_malformed_claims_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client())
            status, _ = open_pr(relay, origin_session_claim="not a session")
            self.assertEqual(status, 400)
            status, _ = open_pr(relay, origin_letter_claim="ab")
            self.assertEqual(status, 400)
            for bad in (0, -1, 2147483648, "3"):
                status, _ = open_pr(relay, origin_generation_claim=bad)
                self.assertEqual(status, 400, bad)

    def test_session_token_field_is_rejected(self):
        # Regression:  the old per-session token field is gone.  A caller
        # offering one gets a plain unexpected-field refusal, so no token
        # shape can ever enter canonical args, an idempotency key, the audit
        # detail, or the outcome store.
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client())
            status, answer = open_pr(
                relay, origin_session_claim=SESSION, origin_session_token=TOKEN
            )
            self.assertEqual(status, 400)
            self.assertIn("unexpected fields", str(answer.get("error")))

    def test_token_string_absent_from_persisted_files(self):
        # Regression against the ACTUAL persistence sources (audit.py /
        # outcomes.py byte-identical to the host's):  no session-token
        # material and no origin_session_token key may appear in any file the
        # relay writes, across the create AND the idempotent replay of the
        # same write.
        from git_relay.outcomes import OutcomeStore

        with tempfile.TemporaryDirectory() as directory:
            outcomes_path = Path(directory) / "outcomes.json"
            relay = make_relay(directory, Client(), outcomes=OutcomeStore(str(outcomes_path)))
            status, _ = open_pr(
                relay,
                origin_session_claim=SESSION,
                origin_letter_claim="AB",
                origin_generation_claim=2,
            )
            self.assertEqual(status, 200)
            status, _ = open_pr(
                relay,
                origin_session_claim=SESSION,
                origin_letter_claim="AB",
                origin_generation_claim=3,
            )
            self.assertEqual(status, 200)
            for name in ("audit.jsonl", "owners.jsonl", "outcomes.json"):
                raw = (Path(directory) / name).read_bytes()
                self.assertNotIn(TOKEN.encode(), raw, name)
                self.assertNotIn(b"origin_session_token", raw, name)

    def test_claims_never_reach_upstream(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            relay = make_relay(directory, client)
            status, _ = open_pr(
                relay,
                origin_session_claim=SESSION,
                origin_letter_claim="AB",
                origin_generation_claim=7,
            )
            self.assertEqual(status, 200)
            self.assertEqual(client.calls[0]["kwargs"], {})

    def test_retry_across_claim_rotation_keeps_one_pr(self):
        # A duplicate open_pr with DIFFERENT claim values is still the same
        # idempotent write:  claims are not part of the idempotency key, so a
        # refreshed generation never forks a second PR.
        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            relay = make_relay(directory, client)
            status, first = open_pr(relay, origin_session_claim=SESSION, origin_generation_claim=1)
            self.assertEqual(status, 200)
            status, second = open_pr(relay, origin_session_claim=SESSION, origin_generation_claim=2)
            self.assertEqual(status, 200)
            self.assertEqual(first.get("number"), second.get("number"))
            self.assertEqual(len(client.calls), 1)


class ServiceReadTests(unittest.TestCase):
    def test_unconfigured_lane_is_503(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client())
            status, _ = relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 1, "verb": "pull"}
            )
            self.assertEqual(status, 503)

    def test_unconfigured_lane_is_audited(self):
        # An unconfigured-lane probe must leave an audit row, so it is visible
        # in review instead of a silent 503.
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client())
            relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 1, "verb": "pull"}
            )
            rows = [
                json.loads(line)
                for line in (Path(directory) / "audit.jsonl").read_text().splitlines()
                if line.strip()
            ]
            self.assertTrue(
                any(r.get("result") == "error" and r.get("cause") == "not_configured" for r in rows)
            )

    def test_wrong_bearer_is_401_and_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(
                directory, Client(), minter=FakeMinter(), service_repos=[ARGS["repo"]]
            )
            status, _ = relay.handle(
                "wrong" + "x" * 44,
                "service_read",
                {"repo": ARGS["repo"], "number": 1, "verb": "pull"},
            )
            self.assertEqual(status, 401)
            rows = [
                json.loads(line)
                for line in (Path(directory) / "audit.jsonl").read_text().splitlines()
                if line.strip()
            ]
            self.assertTrue(any(r.get("result") == "unauthenticated" for r in rows))

    def test_seat_token_is_not_a_service_bearer(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(
                directory, Client(), minter=FakeMinter(), service_repos=[ARGS["repo"]]
            )
            status, _ = relay.handle(
                ECHO_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 1, "verb": "pull"}
            )
            self.assertEqual(status, 401)

    def test_repo_allowlist_and_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(
                directory, Client(), minter=FakeMinter(), service_repos=[ARGS["repo"]]
            )
            status, _ = relay.handle(
                SERVICE_TOKEN,
                "service_read",
                {"repo": "example/other", "number": 1, "verb": "pull"},
            )
            self.assertEqual(status, 403)
            status, _ = relay.handle(
                SERVICE_TOKEN,
                "service_read",
                {"repo": "example/fix;ture", "number": 1, "verb": "pull"},
            )
            self.assertEqual(status, 400)
            status, _ = relay.handle(
                SERVICE_TOKEN,
                "service_read",
                {"repo": "example/fixture/../x", "number": 1, "verb": "pull"},
            )
            self.assertEqual(status, 400)

    def test_verb_and_number_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(
                directory, Client(), minter=FakeMinter(), service_repos=[ARGS["repo"]]
            )
            status, _ = relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 1, "verb": "delete"}
            )
            self.assertEqual(status, 400)
            status, _ = relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": "1", "verb": "pull"}
            )
            self.assertEqual(status, 400)

    def test_pull_mints_narrowed_fixed_grants_and_exports_no_token(self):
        with tempfile.TemporaryDirectory() as directory:
            minter = FakeMinter()
            client = Client()
            relay = make_relay(directory, client, minter=minter, service_repos=[ARGS["repo"]])
            status, answer = relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 7, "verb": "pull"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(answer["body"], {"ok": True})
            self.assertNotIn("token", json.dumps(answer))
            self.assertEqual(
                minter.mints,
                [
                    {
                        "slug": SERVICE_READ_APP_SLUG,
                        "repo": ARGS["repo"],
                        "permissions": dict(SERVICE_READ_GRANTS),
                    }
                ],
            )
            self.assertEqual(client.reads[0]["method"], "GET")
            self.assertEqual(client.reads[0]["path"], "/repos/example/fixture/pulls/7")
            self.assertEqual(client.reads[0]["bearer"], "minted-token")

    def test_check_runs_verb_requires_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            minter = FakeMinter()
            client = Client()
            relay = make_relay(directory, client, minter=minter, service_repos=[ARGS["repo"]])
            status, _ = relay.handle(
                SERVICE_TOKEN,
                "service_read",
                {"repo": ARGS["repo"], "verb": "check_runs", "sha": "XYZ"},
            )
            self.assertEqual(status, 400)
            status, _ = relay.handle(
                SERVICE_TOKEN,
                "service_read",
                {"repo": ARGS["repo"], "verb": "check_runs", "sha": "abc1234", "page": 2},
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                client.reads[0]["path"],
                "/repos/example/fixture/commits/abc1234/check-runs?per_page=100&page=2",
            )

    def test_reviews_verb_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            relay = make_relay(directory, client, minter=FakeMinter(), service_repos=[ARGS["repo"]])
            status, _ = relay.handle(
                SERVICE_TOKEN,
                "service_read",
                {"repo": ARGS["repo"], "number": 9, "verb": "reviews"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                client.reads[0]["path"],
                "/repos/example/fixture/pulls/9/reviews?per_page=100&page=1",
            )

    def test_without_minter_is_503(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client(), minter=None, service_repos=[ARGS["repo"]])
            status, _ = relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 1, "verb": "pull"}
            )
            self.assertEqual(status, 503)

    def test_without_minter_is_audited(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(directory, Client(), minter=None, service_repos=[ARGS["repo"]])
            relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 1, "verb": "pull"}
            )
            rows = [
                json.loads(line)
                for line in (Path(directory) / "audit.jsonl").read_text().splitlines()
                if line.strip()
            ]
            self.assertTrue(
                any(r.get("result") == "error" and r.get("cause") == "minter_absent" for r in rows)
            )

    def test_ok_read_is_audited_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            relay = make_relay(
                directory, Client(), minter=FakeMinter(), service_repos=[ARGS["repo"]]
            )
            relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 7, "verb": "pull"}
            )
            raw = (Path(directory) / "audit.jsonl").read_text()
            self.assertIn("service_read", raw)
            self.assertNotIn(SERVICE_TOKEN, raw)
            self.assertNotIn("minted-token", raw)


class ServiceReadRevokeTests(unittest.TestCase):
    def test_successful_read_revokes_the_fresh_token(self):
        with tempfile.TemporaryDirectory() as directory:
            minter = FakeMinter()
            relay = make_relay(directory, Client(), minter=minter, service_repos=[ARGS["repo"]])
            status, _ = relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 1, "verb": "pull"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(minter.revokes, ["minted-token"])

    def test_failed_read_still_revokes(self):
        class FailClient(Client):
            def _call(self, method, path, bearer=None, timeout=None):
                from git_relay.server import RelayGitHubError

                raise RelayGitHubError(500, "boom")

        with tempfile.TemporaryDirectory() as directory:
            minter = FakeMinter()
            relay = make_relay(directory, FailClient(), minter=minter, service_repos=[ARGS["repo"]])
            status, _ = relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 1, "verb": "pull"}
            )
            self.assertEqual(status, 502)
            self.assertEqual(minter.revokes, ["minted-token"])

    def test_revoke_failure_is_audited_secret_free(self):
        with tempfile.TemporaryDirectory() as directory:
            minter = FakeMinter(revoke_ok=False)
            relay = make_relay(directory, Client(), minter=minter, service_repos=[ARGS["repo"]])
            status, _ = relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "number": 1, "verb": "pull"}
            )
            self.assertEqual(status, 200)  # the read succeeded; only cleanup failed
            rows = [
                json.loads(line)
                for line in (Path(directory) / "audit.jsonl").read_text().splitlines()
                if line.strip()
            ]
            revoke_rows = [
                r for r in rows if r.get("result") == "error" and r.get("at") == "revoke"
            ]
            self.assertEqual(len(revoke_rows), 1)
            raw = (Path(directory) / "audit.jsonl").read_bytes()
            self.assertNotIn(b"minted-token", raw)
            self.assertNotIn(SERVICE_TOKEN.encode(), raw)


class MinterServiceLaneTests(unittest.TestCase):
    """Adversarial validation of the strict service-lane mint."""

    def _minter(self, answer, revoke_fails=False, expected_permissions=None):
        expected_permissions = dict(
            self.GRANTS if expected_permissions is None else expected_permissions
        )
        from git_relay.app_tokens import AppTokenMinter

        revoked = []
        calls = []

        class Source:
            def secrets(self, names):
                return {names[0]: "5389035", names[1]: "pem"}

        def api(method, path, bearer, payload=None):
            calls.append((method, path))
            if method == "GET" and path == "/app":
                self.assertEqual(bearer, "jwt")
                self.assertIsNone(payload)
                return {"id": 5389035}
            if method == "GET" and path == "/repos/example/fixture/installation":
                self.assertEqual(bearer, "jwt")
                self.assertIsNone(payload)
                return {"id": 173216048, "account": {"login": "example"}}
            if method == "DELETE" and path == "/installation/token":
                revoked.append(bearer)
                if revoke_fails:
                    raise OSError("revoke transport down")
                return {}
            raise AssertionError((method, path))

        testcase = self

        class ServiceMinter(AppTokenMinter):
            def _mint_installation_token(self, iid, bearer, repositories, permissions):
                testcase.assertEqual(iid, 173216048)
                testcase.assertEqual(bearer, "jwt")
                testcase.assertEqual(repositories, ["fixture"])
                testcase.assertEqual(permissions, expected_permissions)
                return answer

        minter = ServiceMinter(Source(), api, signer=lambda *a: "jwt", clock=lambda: 1000.0)
        return minter, revoked

    def test_fixture_rejects_wrong_semantic_request_and_unexpected_http(self):
        minter, _ = self._minter(self._answer())
        good = (173216048, "jwt", ["fixture"], dict(self.GRANTS))
        wrong = [
            (1, good[1], good[2], good[3]),
            (good[0], "wrong", good[2], good[3]),
            (good[0], good[1], ["other"], good[3]),
            (good[0], good[1], good[2], {"contents": "write"}),
        ]
        for request in wrong:
            with self.subTest(request=request), self.assertRaises(AssertionError):
                minter._mint_installation_token(*request)
        for request in [
            ("POST", "/unexpected", "jwt", {}),
            ("GET", "/repos/other/fixture/installation", "jwt", None),
            ("GET", "/app", "wrong", None),
        ]:
            with self.subTest(request=request), self.assertRaises(AssertionError):
                minter._api(*request)

    GRANTS = {"pull_requests": "read", "checks": "read", "metadata": "read"}

    def _answer(self, **over):
        base = {
            "token": "minted-token",
            "expires_at": "2026-11-21T04:00:00Z",
            "permissions": dict(self.GRANTS),
            "repositories": [{"full_name": "example/fixture"}],
        }
        base.update(over)
        return base

    def test_exact_answer_is_accepted(self):
        minter, revoked = self._minter(self._answer())
        token, expiry = minter.token_for_service(
            "swu-operator", "example/fixture", dict(self.GRANTS)
        )
        self.assertEqual(token, "minted-token")
        self.assertEqual(revoked, [])

    def test_implicit_metadata_read_is_accepted(self):
        grants = {"pull_requests": "read"}
        answer = self._answer(permissions={"pull_requests": "read", "metadata": "read"})
        minter, _ = self._minter(answer, expected_permissions=grants)
        token, _expiry = minter.token_for_service("swu-operator", "example/fixture", grants)
        self.assertEqual(token, "minted-token")

    def test_accidental_write_grant_is_refused_and_revoked(self):
        answer = self._answer(permissions={**self.GRANTS, "contents": "write"})
        minter, revoked = self._minter(answer)
        with self.assertRaises(Exception) as ctx:
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertIn("outside the request", str(ctx.exception))
        self.assertEqual(revoked, ["minted-token"])

    def test_wrong_level_is_refused_and_revoked(self):
        answer = self._answer(permissions={**self.GRANTS, "checks": "write"})
        minter, revoked = self._minter(answer)
        with self.assertRaises(Exception):
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertEqual(revoked, ["minted-token"])

    def test_downgraded_level_is_refused_and_revoked(self):
        # Requested read, granted something else entirely.
        answer = self._answer(permissions={**self.GRANTS, "checks": "none"})
        minter, revoked = self._minter(answer)
        with self.assertRaises(Exception):
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertEqual(revoked, ["minted-token"])

    def test_missing_permissions_object_is_refused_and_revoked(self):
        answer = self._answer()
        del answer["permissions"]
        minter, revoked = self._minter(answer)
        with self.assertRaises(Exception) as ctx:
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertIn("missing or malformed", str(ctx.exception))
        self.assertEqual(revoked, ["minted-token"])

    def test_malformed_permissions_object_is_refused_and_revoked(self):
        minter, revoked = self._minter(self._answer(permissions="read"))
        with self.assertRaises(Exception):
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertEqual(revoked, ["minted-token"])

    def test_missing_requested_scope_is_refused_and_revoked(self):
        grants = dict(self.GRANTS)
        del grants["checks"]
        minter, revoked = self._minter(self._answer(permissions=grants))
        with self.assertRaises(Exception) as ctx:
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertIn("not granted", str(ctx.exception))
        self.assertEqual(revoked, ["minted-token"])

    def test_metadata_above_read_is_refused_and_revoked(self):
        answer = self._answer(permissions={"pull_requests": "read", "metadata": "write"})
        minter, revoked = self._minter(answer, expected_permissions={"pull_requests": "read"})
        with self.assertRaises(Exception):
            minter.token_for_service("swu-operator", "example/fixture", {"pull_requests": "read"})
        self.assertEqual(revoked, ["minted-token"])

    def test_extra_repository_is_refused_and_revoked(self):
        answer = self._answer(
            repositories=[{"full_name": "example/fixture"}, {"full_name": "example/other"}]
        )
        minter, revoked = self._minter(answer)
        with self.assertRaises(Exception) as ctx:
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertIn("repository set", str(ctx.exception))
        self.assertEqual(revoked, ["minted-token"])

    def test_wrong_repository_is_refused_and_revoked(self):
        minter, revoked = self._minter(self._answer(repositories=[{"full_name": "example/other"}]))
        with self.assertRaises(Exception):
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertEqual(revoked, ["minted-token"])

    def test_missing_repositories_is_refused_and_revoked(self):
        answer = self._answer()
        del answer["repositories"]
        minter, revoked = self._minter(answer)
        with self.assertRaises(Exception):
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertEqual(revoked, ["minted-token"])

    def test_case_mismatched_repo_full_name_is_accepted(self):
        answer = self._answer(repositories=[{"full_name": "Example/Fixture"}])
        minter, revoked = self._minter(answer)
        token, _ = minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertEqual(token, "minted-token")

    def test_malformed_expiry_is_refused_and_revoked(self):
        answer = self._answer(expires_at="not-a-timestamp")
        minter, revoked = self._minter(answer)
        with self.assertRaises(Exception) as ctx:
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertIn("expiry malformed", str(ctx.exception))
        self.assertEqual(revoked, ["minted-token"])

    def test_missing_expiry_is_refused_and_revoked(self):
        answer = self._answer(expires_at="")
        minter, revoked = self._minter(answer)
        with self.assertRaises(Exception) as ctx:
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        self.assertIn("malformed", str(ctx.exception))
        self.assertEqual(revoked, ["minted-token"])

    def test_failed_revocation_leaves_secret_free_evidence(self):
        answer = self._answer(
            token="fixture-only",
            permissions={**self.GRANTS, "contents": "write"},
        )
        minter, revoked = self._minter(answer, revoke_fails=True)
        with self.assertRaises(Exception) as ctx:
            minter.token_for_service("swu-operator", "example/fixture", dict(self.GRANTS))
        message = str(ctx.exception)
        self.assertIn("revocation-failed", message)
        self.assertNotIn("fixture-only", message)
        self.assertEqual(revoked, ["fixture-only"])  # attempted, refused upstream


if __name__ == "__main__":
    unittest.main()
