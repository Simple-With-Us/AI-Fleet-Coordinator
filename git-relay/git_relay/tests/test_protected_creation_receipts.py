import json
import tempfile
import unittest
from pathlib import Path

from git_relay.audit import AuditLog
from git_relay.outcomes import OutcomeStore
from git_relay.policy import SeatPolicy
from git_relay.pr_owners import PROwnerStore
from git_relay.server import Relay
from git_relay.tests.test_review_alert_relay import (
    ARGS,
    ECHO_TOKEN,
    SERVICE_TOKEN,
    SESSION,
    Client,
    FakeMinter,
    make_relay,
    open_pr,
)

OPERATOR_TOKEN = "o" * 48


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
        # A seat credential is not the configured lane bearer:  it falls
        # through to the normal policy path and gets its own authenticated
        # refusal from its seat grant, never the ledger rows.
        status, result = self.read(token=ECHO_TOKEN)
        self.assertEqual(status, 403)
        self.assertNotIn("body", result)

    def test_repo_allowlist_and_extra_fields(self):
        self.assertEqual(self.read(repo="other/repo")[0], 403)
        self.assertEqual(self.read(path="/etc/passwd")[0], 400)

    def test_rows_and_coverage_are_one_coherent_snapshot(self):
        # The reviewer's interleaving:  a pending fence resolves and its
        # confirmed row lands between a row read and a coverage read taken
        # under separate locks.  One locked snapshot makes that ordering
        # impossible to mislabel:  an answer either predates the resolution
        # (rows without PR2, partial) or follows it (rows with PR2,
        # complete) — never rows-without-PR2 marked complete while its own
        # snapshot still had the fence.
        open_pr(self.relay)
        self.relay.pr_owners.begin_creation("b" * 32)
        rows, overflow, partial = self.relay.pr_owners.confirmed_snapshot_for_repo(
            ARGS["repo"], 1000
        )
        self.assertEqual([r["number"] for r in rows], [1])
        self.assertFalse(overflow)
        self.assertTrue(partial)
        self.relay.pr_owners.record(
            seat="swu-echo", repo=ARGS["repo"], number=2, head="h", base="main", corr="b" * 32
        )
        rows, overflow, partial = self.relay.pr_owners.confirmed_snapshot_for_repo(
            ARGS["repo"], 1000
        )
        self.assertEqual([r["number"] for r in rows], [1, 2])
        self.assertFalse(overflow)
        self.assertFalse(partial)

    def test_lane_rows_and_marker_take_the_store_lock_once(self):
        # The lane's receipt answer is one store snapshot, not a row read
        # followed by a separate coverage read.
        open_pr(self.relay)
        store = self.relay.pr_owners
        real_lock = store._lock
        acquisitions = []

        class CountingLock:
            def __enter__(self):
                acquisitions.append(1)
                return real_lock.__enter__()

            def __exit__(self, *args):
                return real_lock.__exit__(*args)

            def acquire(self, *args, **kwargs):
                return real_lock.acquire(*args, **kwargs)

            def release(self):
                return real_lock.release()

            def locked(self):
                return real_lock.locked()

        store._lock = CountingLock()
        try:
            status, body = self.read()
        finally:
            store._lock = real_lock
        self.assertEqual(status, 200)
        self.assertEqual(len(acquisitions), 1)
        self.assertEqual(body["coverage"], "complete")

    def test_allowed_seat_reader_succeeds_through_ordinary_dispatch(self):
        # A seat reader with its own grant is not the configured lane
        # bearer, so it takes the ordinary authenticated path — and it
        # succeeds there while the lane is configured.  The seat answer is
        # the protected census (no lane coverage marker);  the configured
        # bearer still gets the lane answer in the same relay.
        policy = json.dumps(
            {
                "swu-operator": {
                    "token": OPERATOR_TOKEN,
                    "actions": ["service_read"],
                    "repos": [ARGS["repo"]],
                    "app_slug": "swu-operator",
                    "mint_permissions": {
                        "pull_requests": "read",
                        "checks": "read",
                        "metadata": "read",
                    },
                }
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            relay = Relay(
                SeatPolicy(policy),
                Client(),
                AuditLog(str(Path(directory) / "audit.jsonl")),
                app_minter=FakeMinter(),
                pr_owners=PROwnerStore(str(Path(directory) / "owners.jsonl")),
                outcomes=OutcomeStore(str(Path(directory) / "outcomes.json")),
                service_read_token=SERVICE_TOKEN,
                service_read_repos=frozenset({ARGS["repo"]}),
            )
            relay.pr_owners.record(
                seat="swu-echo", repo=ARGS["repo"], number=3, head="h", base="main"
            )
            status, result = relay.handle(
                OPERATOR_TOKEN, "service_read", {"repo": ARGS["repo"], "verb": "creation_receipts"}
            )
            self.assertEqual(status, 200)
            self.assertEqual([r["number"] for r in result["body"]], [3])
            self.assertNotIn("coverage", result)
            status, result = relay.handle(
                SERVICE_TOKEN, "service_read", {"repo": ARGS["repo"], "verb": "creation_receipts"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(result["coverage"], "complete")

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
        # The store answers under the bound:  the eligible rows and whether
        # more exist than the limit allows.
        self.relay.pr_owners.confirmed_snapshot_for_repo.return_value = ([row], False, False)
        self.assertEqual(self.read()[1]["body"], [row])
        self.relay.pr_owners.confirmed_snapshot_for_repo.return_value = ([], True, False)
        self.assertEqual(self.read()[0], 503)

    def test_real_store_answers_under_the_bound(self):
        # The real store, not a mock:  a long ineligible history does not
        # exhaust the census, and too many eligible rows still fail closed.
        store = self.relay.pr_owners
        for number in range(1, 1002):
            store.record(
                seat="swu-echo",
                repo=ARGS["repo"],
                number=number,
                head="echo/x",
                base="main",
                confirmed=True,
                source="audit_backfill",
            )
        eligible = dict(repo=ARGS["repo"], number=2000, confirmed=True, source="open_pr")
        store.record(
            seat="swu-echo",
            repo=ARGS["repo"],
            number=2000,
            head="echo/x",
            base="main",
            confirmed=True,
        )
        status, result = self.read()
        self.assertEqual(status, 200, result)
        self.assertEqual([r["number"] for r in result["body"]], [2000])
        self.assertEqual(eligible["number"], 2000)
        for number in range(2001, 3001):
            store.record(
                seat="swu-echo",
                repo=ARGS["repo"],
                number=number,
                head="echo/x",
                base="main",
                confirmed=True,
            )
        status, _ = self.read()
        self.assertEqual(status, 503)

    def test_census_refusal_is_audited(self):
        from unittest import mock

        self.relay.pr_owners = mock.Mock()
        self.relay.pr_owners.confirmed_snapshot_for_repo.return_value = ([], True, False)
        self.assertEqual(self.read()[0], 503)
        rows = [
            json.loads(line)
            for line in (Path(self.tmp.name) / "audit.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.assertTrue(
            any(r.get("result") == "error" and r.get("cause") == "census_exceeded" for r in rows)
        )

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
