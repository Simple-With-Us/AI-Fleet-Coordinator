import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from review_alerts import events, operator_loop, service
from review_alerts.tests.test_review_alerts import (
    REPO,
    FakeRead,
    FakeRouter,
    claimed_owner,
    kody_review,
    verified_origin,
)


class Wiring(unittest.TestCase):
    def test_binding_failure_preserves_cursor_and_retry_delivers(self):
        with tempfile.TemporaryDirectory() as d:
            router = FakeRouter()
            bind = mock.Mock(side_effect=RuntimeError("held"), return_value=verified_origin())
            runner = service.AlertService(
                events.EventStore(str(Path(d) / "events.jsonl")),
                FakeRead(reviews=[kody_review(51)]),
                router,
                "",
                {"claude": "CLAUDE"},
                {REPO},
                gate_seat="CLAUDE",
                message_poster=lambda owner, event, link: 123,
                owner_provider=lambda: [claimed_owner()],
                origin_binder=bind,
            )
            self.assertEqual(runner.cycle()["held"], 1)
            self.assertEqual(router.submitted, [])
            self.assertFalse(runner.store.has_seed(REPO, 7))
            bind.side_effect = None
            self.assertEqual(runner.cycle()["submitted"], 1)
            self.assertEqual(
                router.submitted[0]["session"], claimed_owner()["origin_session_claim"]
            )

    def test_protected_receipts_refuse_bad_census_and_wrong_repo(self):
        read = mock.Mock()
        provider = operator_loop.ProtectedReceipts(read, {REPO})
        read._read.return_value = [dict(claimed_owner(), source="open_pr")]
        self.assertEqual(provider.lookup(REPO, 7)["number"], 7)
        read._read.return_value = [dict(claimed_owner(), source="audit_backfill")]
        with self.assertRaises(Exception):
            provider.rows()
        read._read.return_value = [dict(claimed_owner(), source="open_pr", repo="other/repo")]
        with self.assertRaises(Exception):
            provider.rows()

    def test_operator_start_off_is_inert_and_active_requires_canonical_registry(self):
        self.assertIsNone(
            operator_loop.start(
                {},
                None,
                threading.Event(),
                mock.Mock(),
                operator_send=mock.Mock(),
                destination_for_origin=mock.Mock(),
            )
        )
        with self.assertRaises(Exception):
            operator_loop.start(
                {"RA_ENABLED": "true"},
                None,
                threading.Event(),
                mock.Mock(),
                operator_send=mock.Mock(),
                destination_for_origin=mock.Mock(),
            )

    def test_actual_thread_starts_and_stops_on_existing_operator_event(self):
        with tempfile.TemporaryDirectory() as d:
            token = Path(d) / "service"
            token.write_text("fixture-service-key")
            wake = Path(d) / "wake"
            wake.write_text("fixture-wake-key")
            env = dict(
                RA_ENABLED="true",
                RA_REPOS=REPO,
                RA_SEAT_MAP_JSON='{"claude":"CLAUDE"}',
                RA_PLATFORM_MAP_JSON='{"CLAUDE":"claude"}',
                RA_STATE_DIR=d,
                RA_SERVICE_TOKEN_FILE=str(token),
                RA_WAKE_KEY_FILE=str(wake),
                RA_SELFTEST_SEAT="CLAUDE",
            )
            stop = threading.Event()
            ran = threading.Event()
            log = mock.Mock()

            def cycle(*args):
                ran.set()
                stop.set()
                return dict(prs=0)

            with (
                mock.patch.object(service.AlertService, "cycle", cycle),
                mock.patch.object(operator_loop.wakes.WakeClient, "check_required_session_support"),
            ):
                thread = operator_loop.start(
                    env,
                    mock.Mock(),
                    stop,
                    log,
                    operator_send=mock.Mock(),
                    destination_for_origin=mock.Mock(),
                )
                self.assertTrue(ran.wait(2))
                thread.join(2)
            self.assertFalse(thread.is_alive())
            log.assert_called()

    def test_retry_redrive_gated_but_delivered_reconciled_during_binding_outage(self):
        from review_alerts import wakes

        for receipt in (
            None,
            dict(status="held", delivered=False, code="required-pr-unbound"),
            dict(status="delivered", delivered=True),
        ):
            with self.subTest(receipt=receipt), tempfile.TemporaryDirectory() as d:
                router = FakeRouter()
                router.fail_submit = wakes.WakeRetryable("test")
                bind = mock.Mock(return_value=verified_origin())
                runner = service.AlertService(
                    events.EventStore(str(Path(d) / "events.jsonl")),
                    FakeRead(reviews=[kody_review(51)]),
                    router,
                    "",
                    {"claude": "CLAUDE"},
                    {REPO},
                    gate_seat="CLAUDE",
                    message_poster=lambda owner, event, link: 123,
                    owner_provider=lambda: [claimed_owner()],
                    origin_binder=bind,
                )
                runner.cycle()
                pending = runner.store.pending()[0]
                router.receipts[pending["wake_id"]] = receipt
                router.fail_submit = None
                router.submit = mock.Mock(wraps=router.submit)
                bind.side_effect = RuntimeError("private-credential-must-not-appear")
                runner.cycle()
                router.submit.assert_not_called()
                self.assertEqual(
                    runner.status_snapshot()["binding_holds"][REPO + "#7"], "RuntimeError"
                )
                if receipt and receipt.get("delivered") is True:
                    self.assertEqual(runner.store.counts(), {"delivered": 1})
                else:
                    self.assertTrue(runner.store.pending())

    def test_stop_during_bind_prevents_initial_submit(self):
        with tempfile.TemporaryDirectory() as d:
            stop = threading.Event()
            router = FakeRouter()
            runner = service.AlertService(
                events.EventStore(str(Path(d) / "events.jsonl")),
                FakeRead(reviews=[kody_review(51)]),
                router,
                "",
                {"claude": "CLAUDE"},
                {REPO},
                gate_seat="CLAUDE",
                message_poster=lambda owner, event, link: 123,
                owner_provider=lambda: [claimed_owner()],
                origin_binder=lambda *a: stop.set(),
                cancelled=stop.is_set,
            )
            runner.cycle()
            self.assertEqual(router.submitted, [])

    def test_shared_lock_blocks_duplicate_and_releases_after_stop_and_start_error(self):
        import fcntl
        import os

        with tempfile.TemporaryDirectory() as d:
            secret = Path(d) / "key"
            secret.write_text("fixture-service-key")
            env = dict(
                RA_ENABLED="true",
                RA_REPOS=REPO,
                RA_SEAT_MAP_JSON='{"claude":"CLAUDE"}',
                RA_PLATFORM_MAP_JSON='{"CLAUDE":"claude"}',
                RA_STATE_DIR=d,
                RA_SERVICE_TOKEN_FILE=str(secret),
                RA_WAKE_KEY_FILE=str(secret),
                RA_SELFTEST_SEAT="CLAUDE",
            )
            stop = threading.Event()
            entered = threading.Event()

            def cycle(*args):
                entered.set()
                return {}

            with (
                mock.patch.object(service.AlertService, "cycle", cycle),
                mock.patch.object(operator_loop.wakes.WakeClient, "check_required_session_support"),
            ):
                t = operator_loop.start(
                    env,
                    mock.Mock(),
                    stop,
                    mock.Mock(),
                    operator_send=mock.Mock(),
                    destination_for_origin=mock.Mock(),
                )
                self.assertTrue(entered.wait(2))
                with self.assertRaises(Exception):
                    operator_loop.start(
                        env,
                        mock.Mock(),
                        threading.Event(),
                        mock.Mock(),
                        operator_send=mock.Mock(),
                        destination_for_origin=mock.Mock(),
                    )
                fd = os.open(str(Path(d) / "lock"), os.O_RDWR)
                with self.assertRaises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                stop.set()
                t.join(2)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.close(fd)
            with mock.patch.object(service, "AlertService", side_effect=RuntimeError):
                with self.assertRaises(RuntimeError):
                    operator_loop.start(
                        env,
                        mock.Mock(),
                        threading.Event(),
                        mock.Mock(),
                        operator_send=mock.Mock(),
                        destination_for_origin=mock.Mock(),
                    )
            fd = os.open(str(Path(d) / "lock"), os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.close(fd)

    def test_startup_gate_retries_visibly_before_any_cycle(self):
        with tempfile.TemporaryDirectory() as d:
            key = Path(d) / "key"
            key.write_text("fixture-service-key")
            env = dict(
                RA_ENABLED="true",
                RA_REPOS=REPO,
                RA_SEAT_MAP_JSON='{"claude":"CLAUDE"}',
                RA_PLATFORM_MAP_JSON='{"CLAUDE":"claude"}',
                RA_STATE_DIR=d,
                RA_SERVICE_TOKEN_FILE=str(key),
                RA_WAKE_KEY_FILE=str(key),
                RA_SELFTEST_SEAT="CLAUDE",
            )
            stop = threading.Event()
            ran = threading.Event()
            log = mock.Mock()
            attempts = []

            def probe(*a, **kw):
                attempts.append(1)
                if len(attempts) == 1:
                    raise RuntimeError("transient")

            def wait(_):
                if len(attempts) > 1:
                    stop.set()
                return stop.is_set()

            def cycle(*a):
                ran.set()
                return {}

            with (
                mock.patch.object(stop, "wait", side_effect=wait),
                mock.patch.object(service.AlertService, "cycle", cycle),
                mock.patch.object(
                    operator_loop.wakes.WakeClient,
                    "check_required_session_support",
                    side_effect=probe,
                ),
            ):
                t = operator_loop.start(
                    env,
                    mock.Mock(),
                    stop,
                    log,
                    operator_send=mock.Mock(),
                    destination_for_origin=mock.Mock(),
                )
                t.join(2)
            self.assertFalse(t.is_alive())
            self.assertTrue(ran.is_set())
            self.assertEqual(len(attempts), 2)
            self.assertEqual(log.call_args_list[0].args[0], "review-alert-error")


class Prerequisite(unittest.TestCase):
    def test_old_registry_is_explicitly_held_before_credential_or_network(self):
        with self.assertRaisesRegex(Exception, "canonical-pr-binding-capability-required"):
            operator_loop.start(
                {"RA_ENABLED": "true"},
                object(),
                threading.Event(),
                mock.Mock(),
                operator_send=mock.Mock(),
                destination_for_origin=mock.Mock(),
            )
