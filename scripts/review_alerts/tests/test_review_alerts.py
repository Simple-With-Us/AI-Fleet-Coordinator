"""review_alerts:  event derivation, store durability, wake contract, cycle."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from review_alerts import detect, events, github_read, service, wakes

REPO = "Simple-With-Us/HogHunter"
SESSION = "claude-9f8a1c2e-3b4d-4f01-8a2b-1c2d3e4f5a6b"
HEAD = "a1b2c3d4" * 5
CURSOR0 = {"review_id": 0, "comment_id": 0, "check_id": 0, "completed_heads": []}


def kody_review(rid, state="CHANGES_REQUESTED", commit=HEAD, submitted_at="2026-11-21T01:00:00Z"):
    return {
        "id": rid,
        "state": state,
        "commit_id": commit,
        "submitted_at": submitted_at,
        "user": {"login": "kody-ai[bot]"},
    }


def sentry_comment(cid, commit=HEAD, created_at="2026-11-21T01:00:00Z"):
    return {
        "id": cid,
        "original_commit_id": commit,
        "created_at": created_at,
        "user": {"login": "sentry[bot]"},
        "body": "finding",
    }


def kody_check(rid, head=HEAD, status="completed", conclusion="success"):
    return {
        "id": rid,
        "name": "Kody Code Review",
        "status": status,
        "conclusion": conclusion,
        "head_sha": head,
        "completed_at": "2026-11-21T01:00:00Z" if status == "completed" else None,
        "app": {"id": 413034, "slug": "kody-ai"},
    }


def claimed_owner(**over):
    row = {
        "seat": "claude",
        "repo": REPO,
        "number": 7,
        "confirmed": True,
        "ts": "2026-11-21T00:00:00Z",
        "origin_session_claim": SESSION,
        "origin_letter_claim": "AB",
        "origin_generation_claim": 3,
    }
    row.update(over)
    return {k: v for k, v in row.items() if v is not None or k not in over}


class DetectTests(unittest.TestCase):
    def test_new_kody_review_is_completion(self):
        found = detect.derive(REPO, 7, HEAD, [kody_review(11)], [], [], dict(CURSOR0))
        self.assertEqual(len(found), 1)
        event = found[0]
        self.assertEqual(event["kind"], "review_submitted")
        self.assertEqual(event["event_id"], "11")
        self.assertIs(event["head_matches"], True)

    def test_old_head_review_is_not_current_head_completion(self):
        found = detect.derive(
            REPO, 7, HEAD, [kody_review(11, commit="0" * 40)], [], [], dict(CURSOR0)
        )
        self.assertIs(found[0]["head_matches"], False)
        self.assertEqual(found[0]["head"], "0" * 40)

    def test_seen_reviews_and_other_actors_are_ignored(self):
        reviews = [
            kody_review(11),
            kody_review(12),
            {
                "id": 13,
                "state": "APPROVED",
                "commit_id": HEAD,
                "user": {"login": "jaywedgeworth22"},
            },
        ]
        found = detect.derive(REPO, 7, HEAD, reviews, [], [], dict(CURSOR0, review_id=11))
        self.assertEqual([e["event_id"] for e in found], ["12"])

    def test_kody_check_completion_on_current_head_alerts(self):
        found = detect.derive(REPO, 7, HEAD, [], [], [kody_check(55)], dict(CURSOR0))
        self.assertEqual(len(found), 1)
        event = found[0]
        self.assertEqual(event["kind"], "review_completed")
        self.assertEqual(event["head"], HEAD)
        self.assertIn("success", event["summary"])

    def test_check_on_old_head_does_not_alert(self):
        found = detect.derive(REPO, 7, HEAD, [], [], [kody_check(55, head="0" * 40)], dict(CURSOR0))
        self.assertEqual(found, [])

    def test_incomplete_check_does_not_alert(self):
        found = detect.derive(
            REPO,
            7,
            HEAD,
            [],
            [],
            [kody_check(55, status="in_progress", conclusion=None)],
            dict(CURSOR0),
        )
        self.assertEqual(found, [])

    def test_semantic_dedupe_review_and_check_same_head(self):
        # A submitted review and a completed check on the SAME head are one
        # round:  the review alerts, the check does not repeat it.
        found = detect.derive(REPO, 7, HEAD, [kody_review(11)], [], [kody_check(55)], dict(CURSOR0))
        self.assertEqual([e["kind"] for e in found], ["review_submitted"])
        # And a head already alerted is not alerted again.
        found = detect.derive(
            REPO, 7, HEAD, [], [], [kody_check(56)], dict(CURSOR0, completed_heads=[HEAD])
        )
        self.assertEqual(found, [])

    def test_other_app_check_is_ignored(self):
        run = kody_check(55)
        run["app"] = {"id": 15368, "slug": "github-actions"}
        self.assertEqual(detect.derive(REPO, 7, HEAD, [], [], [run], dict(CURSOR0)), [])

    def test_sentry_comments_batch_with_head_binding(self):
        comments = [sentry_comment(21), sentry_comment(22), sentry_comment(23, commit="0" * 40)]
        found = detect.derive(REPO, 7, HEAD, [], comments, [], dict(CURSOR0, comment_id=20))
        self.assertEqual(len(found), 1)
        event = found[0]
        self.assertEqual(event["kind"], "findings_added")
        self.assertEqual(event["event_id"], "c23")
        self.assertIs(event["head_matches"], False)
        self.assertIn("3 new finding comments", event["summary"])

    def test_unrelated_comments_are_ignored(self):
        comments = [{"id": 30, "original_commit_id": HEAD, "user": {"login": "someone"}}]
        self.assertEqual(detect.derive(REPO, 7, HEAD, [], comments, [], dict(CURSOR0)), [])


class EventStoreTests(unittest.TestCase):
    def test_append_reload_and_status_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            store = events.EventStore(path)
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "status": "submitted",
                    "wake_id": "ra-x",
                }
            )
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "status": "delivered",
                    "wake_id": "ra-x",
                }
            )
            again = events.EventStore(path)
            row = again.get(REPO, 7, "review_submitted", "11")
            self.assertEqual(row["status"], "delivered")
            self.assertEqual(again.pending(), [])
            self.assertEqual(again.counts(), {"delivered": 1})

    def test_terminal_status_is_never_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            store = events.EventStore(path)
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "status": "delivered",
                    "wake_id": "ra-x",
                }
            )
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "status": "submitted",
                    "wake_id": "ra-x",
                }
            )
            self.assertEqual(store.get(REPO, 7, "review_submitted", "11")["status"], "delivered")

    def test_epoch_replaces_dead_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            store = events.EventStore(path)
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "epoch": 0,
                    "status": "held",
                    "wake_id": "ra-a",
                    "reason": "required-session-not-live",
                }
            )
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "epoch": 1,
                    "status": "submitted",
                    "wake_id": "ra-b",
                }
            )
            row = store.get(REPO, 7, "review_submitted", "11")
            self.assertEqual(row["epoch"], 1)
            self.assertEqual(row["wake_id"], "ra-b")
            # An older epoch arriving late never displaces the newer one.
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "epoch": 0,
                    "status": "held",
                    "wake_id": "ra-a",
                }
            )
            self.assertEqual(store.get(REPO, 7, "review_submitted", "11")["wake_id"], "ra-b")

    def test_torn_tail_is_cut_and_complete_final_row_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            good = {
                "repo": REPO,
                "number": 7,
                "kind": "review_submitted",
                "event_id": "11",
                "status": "submitted",
            }
            path.write_text(json.dumps(good) + '\n{"repo": "S', encoding="utf-8")
            store = events.EventStore(str(path))
            self.assertIsNotNone(store.get(REPO, 7, "review_submitted", "11"))
            self.assertTrue(path.read_bytes().endswith(b"\n"))
            # A complete final row missing only its newline is preserved, not cut.
            path.write_text(
                json.dumps(good) + "\n" + json.dumps(dict(good, event_id="12")), encoding="utf-8"
            )
            store = events.EventStore(str(path))
            self.assertIsNotNone(store.get(REPO, 7, "review_submitted", "12"))
            self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_seed_blocks_history_and_cursor_covers_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            store = events.EventStore(path)
            store.seed(REPO, 7, 100, 200, 300)
            store.seed(REPO, 7, 999, 999, 999)  # first seed wins
            cursor = store.cursor(REPO, 7)
            self.assertEqual(cursor["review_id"], 100)
            self.assertEqual(cursor["comment_id"], 200)
            self.assertEqual(cursor["check_id"], 300)
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_completed",
                    "event_id": "301",
                    "status": "delivered",
                    "head": HEAD,
                }
            )
            cursor = store.cursor(REPO, 7)
            self.assertEqual(cursor["check_id"], 301)
            self.assertIn(HEAD, cursor["completed_heads"])


class FakeRouter:
    receipt_terminal = staticmethod(wakes.WakeClient.receipt_terminal)

    def __init__(self):
        self.submitted = []
        self.receipts = {}
        self.fail_submit = None

    def submit(self, seat, wake_id, session, letter, generation, require_pr, topic, link):
        if self.fail_submit is not None:
            raise self.fail_submit
        self.submitted.append(
            {
                "seat": seat,
                "wake_id": wake_id,
                "session": session,
                "letter": letter,
                "generation": generation,
                "topic": topic,
            }
        )
        return {"accepted": True}

    def receipt(self, seat, wake_id):
        return self.receipts.get(wake_id)

    def check_require_pr_support(self, seat, probe_id):
        if getattr(self, "fail_gate", None) is not None:
            raise self.fail_gate


class FakeRead:
    def __init__(self, pr=None, reviews=(), comments=(), checks=()):
        self._pr = pr or {
            "state": "open",
            "head": {"sha": HEAD},
            "html_url": "https://example/pr/7",
        }
        self._reviews = list(reviews)
        self._comments = list(comments)
        self._checks = list(checks)

    def pull(self, repo, number):
        return self._pr

    def reviews(self, repo, number, after_id=0):
        return self._reviews

    def review_comments(self, repo, number, after_id=0):
        return self._comments

    def check_runs(self, repo, sha):
        return self._checks


class ServiceCycleTests(unittest.TestCase):
    def _service(self, directory, owners, read=None, router=None):
        owners_path = Path(directory) / "owners.jsonl"
        owners_path.write_text("".join(json.dumps(o) + "\n" for o in owners), encoding="utf-8")
        store = events.EventStore(str(Path(directory) / "events.jsonl"))
        svc = service.AlertService(
            store,
            read or FakeRead(),
            router or FakeRouter(),
            str(owners_path),
            {"claude": "CLAUDE"},
            repos={REPO},
            gate_seat="CLAUDE",
        )
        return svc

    def test_first_sight_seeds_and_alerts_nothing(self):
        # Activity BEFORE the registration ts is baseline;  nothing alerts.
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(
                directory,
                [claimed_owner(ts="2026-11-21T02:00:00Z")],
                FakeRead(
                    reviews=[kody_review(50)],
                    comments=[sentry_comment(60)],
                    checks=[kody_check(70)],
                ),
                router,
            )
            counts = svc.cycle()
            self.assertEqual(counts["seeded"], 1)
            self.assertEqual(counts["events"], 0)
            self.assertEqual(router.submitted, [])
            # Next cycle:  only NEW activity alerts.
            svc.gh = FakeRead(reviews=[kody_review(50), kody_review(51)])
            counts = svc.cycle()
            self.assertEqual(counts["events"], 1)
            self.assertEqual(len(router.submitted), 1)
            self.assertEqual(router.submitted[0]["session"], SESSION)
            self.assertEqual(router.submitted[0]["letter"], "AB")
            self.assertEqual(router.submitted[0]["generation"], 3)

    def test_first_sight_race_review_after_registration_alerts(self):
        # A review landing between PR registration and the FIRST poll is not
        # silently discarded:  the seed baselines only pre-registration
        # activity, and the same first cycle alerts the new review.
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(
                directory,
                [claimed_owner(ts="2026-11-21T00:30:00Z")],
                FakeRead(
                    reviews=[kody_review(50)],
                    comments=[sentry_comment(60)],
                    checks=[kody_check(70)],
                ),
                router,
            )
            counts = svc.cycle()
            self.assertEqual(counts["seeded"], 1)
            # review + findings alert;  the check on the SAME head is the same
            # round (semantic dedupe), so 2 events, not 3.
            self.assertEqual(counts["events"], 2)
            self.assertEqual(len(router.submitted), 2)
            cursor = svc.store.cursor(REPO, 7)
            self.assertEqual(cursor["review_id"], 50)
            self.assertEqual(cursor["comment_id"], 60)
            # Nothing re-alerts next cycle (the deduped check is suppressed by
            # the alerted head, not by its watermark).
            counts = svc.cycle()
            self.assertEqual(counts["events"], 0)
            self.assertEqual(len(router.submitted), 2)

    def test_owner_row_without_ts_baselines_everything(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(
                directory, [claimed_owner(ts=None)], FakeRead(reviews=[kody_review(50)]), router
            )
            counts = svc.cycle()
            self.assertEqual(counts["seeded"], 1)
            self.assertEqual(counts["events"], 0)

    def test_unbound_claim_is_held_never_delivered(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = claimed_owner(
                origin_session_claim=None, origin_letter_claim=None, origin_generation_claim=None
            )
            router = FakeRouter()
            svc = self._service(directory, [owner], FakeRead(), router)
            svc.cycle()  # seed
            svc.gh = FakeRead(reviews=[kody_review(51)])
            counts = svc.cycle()
            self.assertEqual(counts["held"], 1)
            self.assertEqual(router.submitted, [])
            held = svc.store.held()
            self.assertEqual(held[0]["reason"], "no-origin-claim")

    def test_retryable_submit_marks_submit_pending_then_reconciles(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()  # seed
            svc.gh = FakeRead(reviews=[kody_review(51)])
            router.fail_submit = wakes.WakeRetryable("router unreachable: URLError")
            counts = svc.cycle()
            self.assertEqual(counts["held"], 0)
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["status"], "submit-pending")
            wake_id = row["wake_id"]
            # Router has no record (the submit was lost):  SAME wake id is
            # resubmitted, not a fresh one.
            router.fail_submit = None
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            self.assertEqual(len(router.submitted), 1)
            self.assertEqual(router.submitted[0]["wake_id"], wake_id)
            self.assertEqual(
                svc.store.get(REPO, 7, "review_submitted", "51")["status"], "submitted"
            )

    def test_permanent_submit_refusal_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            router.fail_submit = wakes.WakeError("wake refused: HTTP 400 bad")
            counts = svc.cycle()
            self.assertEqual(counts["held"], 1)
            self.assertIn("wake-submit", svc.store.held()[0]["reason"])

    def test_not_live_hold_redrives_with_new_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            first_wake = row["wake_id"]
            router.receipts[first_wake] = {
                "delivered": False,
                "reason": "required-session-not-live",
            }
            counts = svc.cycle()
            self.assertEqual(counts["redriven"], 1)
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["epoch"], 1)
            self.assertNotEqual(row["wake_id"], first_wake)
            self.assertEqual(row["status"], "submitted")

    def test_pr_unbound_hold_redrives_with_new_epoch(self):
        # The binding raced the alert (bind-pr landed after dispatch):  the
        # hold is certainly-undelivered, so a bounded redrive with a fresh
        # epoch is permitted, same reconciliation as session holds.
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            first_wake = row["wake_id"]
            router.receipts[first_wake] = {"delivered": False, "reason": "required-pr-unbound"}
            counts = svc.cycle()
            self.assertEqual(counts["redriven"], 1)
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["epoch"], 1)
            self.assertNotEqual(row["wake_id"], first_wake)

    def test_pr_unbound_redrive_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            for _ in range(10):
                row = svc.store.get(REPO, 7, "review_submitted", "51")
                if row["status"] != "submitted":
                    break
                router.receipts[row["wake_id"]] = {
                    "delivered": False,
                    "reason": "required-pr-unbound",
                }
                svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["status"], "held")
            self.assertEqual(row["reason"], "required-pr-unbound")

    def test_generation_mismatch_is_terminal_held(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            router.receipts[row["wake_id"]] = {
                "delivered": False,
                "reason": "required-generation-mismatch",
            }
            counts = svc.cycle()
            self.assertEqual(counts["redriven"], 0)
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["status"], "held")
            self.assertEqual(row["reason"], "required-generation-mismatch")

    def test_delivered_and_unknown_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            router.receipts[row["wake_id"]] = {"delivered": True}
            svc.cycle()
            self.assertEqual(
                svc.store.get(REPO, 7, "review_submitted", "51")["status"], "delivered"
            )

    def test_delivered_null_stays_unknown_regardless_of_status_wording(self):
        self.assertEqual(
            wakes.WakeClient.receipt_terminal({"delivered": None, "status": "delivered"}),
            (True, "unknown"),
        )
        self.assertEqual(
            wakes.WakeClient.receipt_terminal({"status": "delivered"}), (True, "unknown")
        )
        self.assertEqual(
            wakes.WakeClient.receipt_terminal({"delivered": None, "status": "accepted"}),
            (False, ""),
        )
        self.assertEqual(
            wakes.WakeClient.receipt_terminal({"delivered": False, "reason": "no-live-session"}),
            (True, "held:no-live-session"),
        )

    def test_read_error_advances_no_cursor(self):
        with tempfile.TemporaryDirectory() as directory:

            class BrokenRead(FakeRead):
                def reviews(self, repo, number, after_id=0):
                    raise github_read.GithubReadUncertain("service_read:reviews")

            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], BrokenRead(), router)
            counts = svc.cycle()
            self.assertEqual(counts["read_errors"], 1)
            self.assertFalse(svc.store.has_seed(REPO, 7))

    def test_status_snapshot_surfaces_held_and_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            owner = claimed_owner(origin_session_claim=None)
            svc = self._service(directory, [owner], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            snap = svc.status_snapshot()
            self.assertEqual(snap["counts"].get("held"), 1)
            self.assertEqual(snap["held"][0]["reason"], "no-origin-claim")
            self.assertIn("pending", snap)


class WakeContractTests(unittest.TestCase):
    def test_deterministic_ids_differ_by_epoch(self):
        a = wakes.wake_id_for(REPO, 7, "review_submitted", "51", 0)
        b = wakes.wake_id_for(REPO, 7, "review_submitted", "51", 1)
        self.assertTrue(a.startswith("ra-"))
        self.assertNotEqual(a, b)
        self.assertEqual(a, wakes.wake_id_for(REPO, 7, "review_submitted", "51", 0))

    def test_submit_validates_required_fields(self):
        client = wakes.WakeClient("http://127.0.0.1:9", lambda: "k" * 48)
        with self.assertRaises(wakes.WakeError):
            client.submit("CLAUDE", "ra-x", "bad session", "AB", 1, "o/r#1", "t", "")
        with self.assertRaises(wakes.WakeError):
            client.submit("CLAUDE", "ra-x", SESSION, "ab", 1, "o/r#1", "t", "")
        with self.assertRaises(wakes.WakeError):
            client.submit("CLAUDE", "ra-x", SESSION, "AB", 0, "o/r#1", "t", "")


if __name__ == "__main__":
    unittest.main()


class InstantParsingTests(unittest.TestCase):
    """First-sight seeding compares INSTANTS, never lexical strings.

    Probe:  a pre-registration review is baselined (no event);  a
    post-registration one alerts (an event is recorded).
    """

    def _events(self, owner_ts, review_ts):
        with tempfile.TemporaryDirectory() as directory:
            owners_path = Path(directory) / "owners.jsonl"
            owners_path.write_text(json.dumps(claimed_owner(ts=owner_ts)) + "\n", encoding="utf-8")
            store = events.EventStore(str(Path(directory) / "events.jsonl"))
            svc = service.AlertService(
                store,
                FakeRead(reviews=[kody_review(50, submitted_at=review_ts)]),
                FakeRouter(),
                str(owners_path),
                {"claude": "CLAUDE"},
                repos={REPO},
                gate_seat="CLAUDE",
            )
            return svc.cycle()["events"]

    def test_z_and_fractional_seconds(self):
        # 00:00:00.500Z is AFTER 00:00:00Z:  not baselined, it alerts.
        self.assertEqual(self._events("2026-11-21T00:00:00Z", "2026-11-21T00:00:00.500Z"), 1)
        # ...and before-with-fraction baselines.
        self.assertEqual(self._events("2026-11-21T00:00:00.500Z", "2026-11-21T00:00:00Z"), 0)

    def test_offsets_compare_as_instants_not_strings(self):
        # 20:00-05:00 == 01:00Z:  after the 00:30Z registration it alerts,
        # where a lexical compare would seed it away.
        self.assertEqual(self._events("2026-11-21T00:30:00Z", "2026-11-20T20:00:00-05:00"), 1)
        # Before the 02:00Z registration it baselines.
        self.assertEqual(self._events("2026-11-21T02:00:00Z", "2026-11-20T20:00:00-05:00"), 0)

    def test_naive_item_treated_as_utc(self):
        self.assertEqual(self._events("2026-11-21T02:00:00Z", "2026-11-21T01:00:00"), 0)

    def test_unparseable_owner_baselines_everything(self):
        self.assertEqual(self._events("not-a-time", "2026-11-21T01:00:00Z"), 0)

    def test_unparseable_review_time_is_suppressed(self):
        self.assertEqual(self._events("2026-11-21T00:30:00Z", "garbage"), 0)

    def test_unparseable_check_time_still_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            owners_path = Path(directory) / "owners.jsonl"
            owners_path.write_text(
                json.dumps(claimed_owner(ts="2026-11-21T00:30:00Z")) + "\n", encoding="utf-8"
            )
            store = events.EventStore(str(Path(directory) / "events.jsonl"))
            check = kody_check(70)
            check["completed_at"] = "garbage"
            svc = service.AlertService(
                store,
                FakeRead(checks=[check]),
                FakeRouter(),
                str(owners_path),
                {"claude": "CLAUDE"},
                repos={REPO},
                gate_seat="CLAUDE",
            )
            counts = svc.cycle()
            self.assertEqual(counts["events"], 1)


class PerCycleGateTests(unittest.TestCase):
    def _gated_service(self, directory, router):
        owners_path = Path(directory) / "owners.jsonl"
        owners_path.write_text(json.dumps(claimed_owner()) + "\n", encoding="utf-8")
        store = events.EventStore(str(Path(directory) / "events.jsonl"))
        return service.AlertService(
            store,
            FakeRead(),
            router,
            str(owners_path),
            {"claude": "CLAUDE"},
            repos={REPO},
            gate_seat="CLAUDE",
        )

    def test_gate_ok_cycle_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._gated_service(directory, router)
            counts = svc.cycle()
            self.assertEqual(counts["prs"], 1)

    def test_gate_failure_holds_the_whole_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            router.fail_gate = wakes.WakeError("router lacks require_pr validation")
            svc = self._gated_service(directory, router)
            with self.assertRaises(wakes.WakeError):
                svc.cycle()
            # Nothing dispatched, nothing seeded:  next cycle retries clean.
            self.assertEqual(router.submitted, [])
            self.assertFalse(svc.store.has_seed(REPO, 7))
