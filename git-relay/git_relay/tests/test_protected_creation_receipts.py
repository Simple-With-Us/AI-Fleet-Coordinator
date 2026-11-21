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


if __name__ == "__main__":
    unittest.main()
