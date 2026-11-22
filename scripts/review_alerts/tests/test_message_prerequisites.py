"""Regression coverage for the PR2443 current-head review findings."""

import tempfile
import threading
import unittest
from unittest import mock

from review_alerts import messages, operator_loop
from review_alerts.operator_pr_origin import Held
from review_alerts.tests import test_review_alerts as fixtures


class MessagePrerequisites(unittest.TestCase):
    def test_disabled_legacy_four_argument_start_is_inert(self):
        with mock.patch.object(messages, "OperatorMessages") as constructor:
            self.assertIsNone(operator_loop.start({}, None, threading.Event(), mock.Mock()))
            constructor.assert_not_called()

    def test_enabled_missing_or_noncallable_callbacks_raise_held_before_construction(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(
                RA_ENABLED="true",
                RA_REPOS=fixtures.REPO,
                RA_SEAT_MAP_JSON='{"claude":"CLAUDE"}',
                RA_PLATFORM_MAP_JSON='{"CLAUDE":"claude"}',
                RA_STATE_DIR=directory,
            )
            for kwargs in (
                {},
                {"operator_send": mock.Mock()},
                {"destination_for_origin": mock.Mock()},
                {"operator_send": False, "destination_for_origin": mock.Mock()},
            ):
                with (
                    self.subTest(kwargs=kwargs),
                    mock.patch.object(messages, "OperatorMessages") as constructor,
                ):
                    with self.assertRaisesRegex(Held, "review-service-message-callbacks-required"):
                        operator_loop.start(
                            env, mock.Mock(), threading.Event(), mock.Mock(), **kwargs
                        )
                    constructor.assert_not_called()

    def service(self, directory):
        return fixtures.ServiceCycleTests._service(
            self,
            directory,
            [fixtures.claimed_owner()],
            fixtures.FakeRead(reviews=[fixtures.kody_review(51)]),
            fixtures.FakeRouter(),
        )

    def test_missing_or_empty_bound_origin_holds_before_intent_and_any_effect(self):
        for origin in (None, {}, False):
            with (
                self.subTest(origin=origin),
                tempfile.TemporaryDirectory() as directory,
            ):
                svc = self.service(directory)
                svc.origin_binder = None if origin is None else mock.Mock(return_value=origin)
                svc.message_poster = mock.Mock(return_value=321)
                svc.store.append = mock.Mock(wraps=svc.store.append)
                svc.cycle()
                svc.cycle()
                svc.message_poster.assert_not_called()
                self.assertEqual(svc.router.submitted, [])
                self.assertEqual(svc.status_snapshot()["held"][0]["reason"], "no-origin-claim")
                self.assertEqual(svc.store.pending(), [])
                self.assertNotIn(
                    "message-posting",
                    [call.args[0]["status"] for call in svc.store.append.call_args_list],
                )

    def test_pending_destination_row_without_binder_becomes_visible_hold(self):
        with tempfile.TemporaryDirectory() as directory:
            svc = self.service(directory)
            svc.message_poster = mock.Mock(side_effect=messages.MessageDestinationUnresolved())
            svc.cycle()
            self.assertEqual(svc.store.pending()[0]["status"], "message-pending")
            attempts = svc.message_poster.call_count
            svc.origin_binder = None
            svc.cycle()
            self.assertEqual(svc.message_poster.call_count, attempts)
            self.assertEqual(svc.store.held()[0]["reason"], "no-origin-claim")

    def test_actual_binder_outage_stays_retriable_then_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            svc = self.service(directory)
            svc.message_poster = mock.Mock(side_effect=messages.MessageDestinationUnresolved())
            svc.cycle()
            attempts = svc.message_poster.call_count
            svc.origin_binder = mock.Mock(side_effect=RuntimeError("registry unavailable"))
            svc.cycle()
            self.assertEqual(svc.message_poster.call_count, attempts)
            self.assertEqual(svc.store.pending()[0]["status"], "message-pending")
            svc.origin_binder = fixtures.verified_origin
            svc.message_poster.side_effect = None
            svc.message_poster.return_value = 321
            svc.cycle()
            self.assertEqual(svc.router.submitted[0]["message_id"], 321)
