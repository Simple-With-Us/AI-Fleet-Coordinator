import tempfile
import unittest

from git_relay.tests.test_review_alert_relay import (
    ARGS,
    ECHO_TOKEN,
    SERVICE_TOKEN,
    SESSION,
    Client,
    make_relay,
    open_pr,
)


class ProtectedReceipts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.relay = make_relay(self.tmp.name, Client(), service_repos={ARGS["repo"]})

    def read(self, token=SERVICE_TOKEN, **over):
        return self.relay.handle(
            token, "service_read", dict(dict(repo=ARGS["repo"], verb="creation_receipts"), **over)
        )

    def test_actual_create_and_service_lookup_no_mint(self):
        status, result = open_pr(
            self.relay,
            origin_session_claim=SESSION,
            origin_letter_claim="AA",
            origin_generation_claim=1,
        )
        self.assertEqual(status, 200, result)
        status, result = self.read()
        self.assertEqual(status, 200, result)
        self.assertEqual(result["body"][0]["origin_session_claim"], SESSION)
        self.assertEqual(result["body"][0]["seat"], "swu-echo")

    def test_seat_credential_cannot_read_private_ledger(self):
        self.assertEqual(self.read(token=ECHO_TOKEN)[0], 401)

    def test_repo_allowlist_and_extra_fields(self):
        self.assertEqual(self.read(repo="other/repo")[0], 403)
        self.assertEqual(self.read(path="/etc/passwd")[0], 400)

    def test_unconfirmed_and_backfill_are_not_creation_receipts(self):
        self.relay.pr_owners.record(
            seat="swu-echo",
            repo=ARGS["repo"],
            number=7,
            head="echo/x",
            base="main",
            confirmed=False,
        )
        self.relay.pr_owners.record(
            seat="swu-echo",
            repo=ARGS["repo"],
            number=8,
            head="echo/x",
            base="main",
            confirmed=True,
            source="audit_backfill",
        )
        self.assertEqual(self.read()[1]["body"], [])

    def test_historical_rows_do_not_exhaust_eligible_census(self):
        from unittest import mock

        self.relay.pr_owners = mock.Mock()
        row = dict(repo=ARGS["repo"], number=7, confirmed=True, source="open_pr")
        self.relay.pr_owners.for_repo.return_value = [dict(row, source="audit_backfill")] * 1001 + [
            row
        ]
        self.assertEqual(self.read()[1]["body"], [row])
        self.relay.pr_owners.for_repo.return_value = [row] * 1001
        self.assertEqual(self.read()[0], 503)

    def test_github_mint_failure_returns_audited_502(self):
        from unittest import mock
        from git_relay.server import RelayGitHubError

        self.relay.app_minter = mock.Mock()
        self.relay.app_minter.token_for_service.side_effect = RelayGitHubError(503, "fixture")
        status, body = self.relay.handle(
            SERVICE_TOKEN, "service_read", dict(repo=ARGS["repo"], number=7, verb="pull")
        )
        self.assertEqual(status, 502)
        self.assertEqual(body, {"error": "read grant unavailable"})


if __name__ == "__main__":
    unittest.main()
