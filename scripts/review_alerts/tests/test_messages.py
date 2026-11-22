"""Operator message is durable before the existing exact-origin wake."""

import tempfile
import unittest
from unittest import mock

from review_alerts import events, messages, wakes
from review_alerts.tests import test_review_alerts as fixtures


class MessageDelivery(unittest.TestCase):
    def runner(self, directory, poster):
        router = fixtures.FakeRouter()
        svc = fixtures.ServiceCycleTests._service(
            self,
            directory,
            [fixtures.claimed_owner()],
            fixtures.FakeRead(reviews=[fixtures.kody_review(51)]),
            router,
        )
        svc.message_poster = poster
        return svc, router

    def test_message_precedes_wake_and_id_survives_reload(self):
        with tempfile.TemporaryDirectory() as d:
            order = []

            def post(*args):
                order.append("message")
                return 321

            svc, router = self.runner(d, post)
            original = router.submit

            def submit(*args, **kw):
                order.append("wake")
                persisted = events.EventStore(svc.store._path)
                record = persisted.get(fixtures.REPO, 7, "review_submitted", "51")
                self.assertEqual(record["message_id"], 321)
                return original(*args, **kw)

            router.submit = submit
            svc.cycle()
            svc.store = events.EventStore(svc.store._path)
            svc.cycle()
            self.assertEqual(order, ["message", "wake"])
            self.assertEqual(router.submitted[0]["message_id"], 321)
            self.assertEqual(router.submitted[0]["session"], fixtures.SESSION)
            self.assertEqual(router.submitted[0]["generation"], 3)

    def test_lost_wake_answer_reuses_message_and_wake(self):
        with tempfile.TemporaryDirectory() as d:
            poster = mock.Mock(return_value=321)
            svc, router = self.runner(d, poster)
            router.fail_submit = wakes.WakeRetryable("lost answer")
            svc.cycle()
            (pending,) = svc.store.pending()
            router.fail_submit = None
            svc.cycle()
            poster.assert_called_once()
            self.assertEqual(router.submitted[0]["wake_id"], pending["wake_id"])
            self.assertEqual(router.submitted[0]["message_id"], 321)

    def test_certain_undelivered_redrive_reuses_message(self):
        with tempfile.TemporaryDirectory() as d:
            poster = mock.Mock(return_value=321)
            svc, router = self.runner(d, poster)
            svc.cycle()
            first = router.submitted[0]["wake_id"]
            router.receipts[first] = dict(delivered=False, code="required-pr-unbound")
            svc.cycle()
            poster.assert_called_once()
            self.assertNotEqual(router.submitted[-1]["wake_id"], first)
            self.assertEqual(router.submitted[-1]["message_id"], 321)

    def test_uncertain_message_holds_without_wake_or_repost(self):
        for result in (None, 0, True, "321"):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as d:
                poster = mock.Mock(return_value=result)
                svc, router = self.runner(d, poster)
                svc.cycle()
                svc.cycle()
                poster.assert_called_once()
                self.assertEqual(router.submitted, [])
                self.assertEqual(svc.store.held()[0]["reason"], "operator-message-outcome-unknown")
        with tempfile.TemporaryDirectory() as d:
            poster = mock.Mock(side_effect=OSError("response lost"))
            svc, router = self.runner(d, poster)
            svc.cycle()
            svc.cycle()
            poster.assert_called_once()
            self.assertEqual(router.submitted, [])

    def test_definite_presend_unavailability_can_resume(self):
        with tempfile.TemporaryDirectory() as d:
            poster = mock.Mock(side_effect=messages.MessageRetryable("not posted"))
            svc, router = self.runner(d, poster)
            svc.cycle()
            self.assertEqual(router.submitted, [])
            poster.side_effect = None
            poster.return_value = 321
            svc.cycle()
            self.assertEqual(router.submitted[0]["message_id"], 321)

    def test_crash_after_post_intent_is_unknown_not_reposted(self):
        with tempfile.TemporaryDirectory() as d:
            poster = mock.Mock(side_effect=SystemExit("crash"))
            svc, router = self.runner(d, poster)
            with self.assertRaises(SystemExit):
                svc.cycle()
            svc.store = events.EventStore(svc.store._path)
            svc.cycle()
            poster.assert_called_once()
            self.assertEqual(router.submitted, [])
            self.assertEqual(svc.store.held()[0]["reason"], "operator-message-outcome-unknown")

    def test_crash_after_recorded_message_resumes_without_posting_again(self):
        with tempfile.TemporaryDirectory() as d:
            poster = mock.Mock(return_value=321)
            svc, router = self.runner(d, poster)
            router.submit = mock.Mock(side_effect=SystemExit("crash"))
            with self.assertRaises(SystemExit):
                svc.cycle()
            svc.store = events.EventStore(svc.store._path)
            router.submit = fixtures.FakeRouter.submit.__get__(router)
            svc.cycle()
            poster.assert_called_once()
            self.assertEqual(router.submitted[0]["message_id"], 321)

    def test_missing_callback_or_binding_never_posts_or_wakes(self):
        with tempfile.TemporaryDirectory() as d:
            svc, router = self.runner(d, None)
            svc.cycle()
            self.assertEqual(router.submitted, [])
            self.assertEqual(svc.store.held()[0]["reason"], "operator-message-unconfigured")
        with tempfile.TemporaryDirectory() as d:
            poster = mock.Mock(return_value=321)
            svc, router = self.runner(d, poster)
            svc.origin_binder = mock.Mock(side_effect=RuntimeError("binding unavailable"))
            svc.cycle()
            poster.assert_not_called()
            self.assertEqual(router.submitted, [])

    def test_legacy_pending_wake_without_message_is_visible_hold(self):
        with tempfile.TemporaryDirectory() as d:
            svc, router = self.runner(d, mock.Mock(return_value=321))
            svc.cycle()
            (row,) = svc.store.pending()
            row.pop("message_id")
            # Model an old journal written before real message IDs existed.
            import json
            from pathlib import Path

            Path(svc.store._path).write_text(json.dumps(row) + "\n")
            svc.store = events.EventStore(svc.store._path)
            svc.cycle()
            self.assertEqual(svc.store.held()[0]["reason"], "legacy-wake-without-message")
            self.assertEqual(len(router.submitted), 1)

    def test_real_wake_client_serializes_message_id(self):
        client = wakes.WakeClient("http://127.0.0.1:9", lambda: "fixture")
        client._call = mock.Mock(return_value=(202, {"accepted": True}))
        client.submit(
            "CLAUDE",
            "ra-message",
            fixtures.SESSION,
            "AB",
            3,
            fixtures.REPO + "#7",
            "review",
            "",
            message_id=321,
        )
        self.assertEqual(client._call.call_args.args[2]["message_id"], 321)

    def destination_fixture(self, **changes):
        origin = dict(
            seat="CLAUDE",
            platform="claude",
            verified_host="server",
            profile_scope="profile",
            session=fixtures.SESSION,
        )
        stream = dict(stream_id=646987, name="renamed-by-admin", subscribers=[123])
        bot = dict(user_id=123, email="claude-bot@example.invalid", is_bot=True, is_active=True)
        client = mock.Mock()
        streams = changes.get("streams", [stream])
        bots = changes.get("bots", [bot])
        client.get.side_effect = [{"subscriptions": streams}, {"members": bots}]

        def resolve(value):
            return messages.side_chats_destination(
                client,
                value,
                lambda row: "CLAUDE",
                646987,
                lambda seats: " <-> ".join(sorted(seats)),
            )

        return origin, client, resolve

    def test_operator_poster_uses_existing_side_chats_and_only_owner_mention(self):
        origin, client, resolve = self.destination_fixture()
        send = mock.Mock(return_value={"id": 321})
        poster = messages.OperatorMessages(send, resolve)
        event = dict(
            repo=fixtures.REPO,
            number=7,
            kind="review_submitted",
            summary="untrusted summary should not be posted",
            head_matches=True,
        )
        self.assertEqual(poster.post({"_review_origin": origin}, event, ""), 321)
        self.assertEqual(
            client.get.call_args_list,
            [
                mock.call("users/me/subscriptions", {"include_subscribers": "true"}),
                mock.call("users"),
            ],
        )
        params = send.call_args.args[0]
        self.assertEqual(params["type"], "stream")
        self.assertEqual(params["to"], "646987")
        self.assertEqual(params["topic"], "CLAUDE <-> OPERATOR")
        self.assertEqual(
            params["content"], "@**CLAUDE|123** %s#7: review completed" % fixtures.REPO
        )
        client.post.assert_not_called()

    def test_current_stream_and_exact_active_bot_membership_are_required(self):
        bot = dict(user_id=123, is_bot=True, is_active=True)
        stream = dict(stream_id=646987, subscribers=[123])
        cases = [
            dict(streams=[]),
            dict(streams=[dict(stream, stream_id=9)]),
            dict(streams=[dict(stream, is_archived=True)]),
            dict(streams=[dict(stream, subscribers=[])]),
            dict(streams=[dict(stream, subscribers=["123"])]),
            dict(streams=[dict(stream, subscribers=None)]),
            dict(streams=[stream, stream]),
            dict(bots=[]),
            dict(bots=[bot, bot]),
            dict(bots=[dict(bot, is_bot=False)]),
            dict(bots=[dict(bot, is_active=False)]),
            dict(bots=[dict(bot, user_id=True)]),
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                origin, client, resolve = self.destination_fixture(**changes)
                send = mock.Mock()
                poster = messages.OperatorMessages(send, resolve)
                with self.assertRaises(messages.MessageDestinationUnresolved):
                    poster.post({"_review_origin": origin}, {}, "")
                send.assert_not_called()

    def test_no_origin_or_metadata_error_never_posts(self):
        for invalid in (None, {}, dict(seat="CLAUDE")):
            origin, client, resolve = self.destination_fixture()
            send = mock.Mock()
            with self.assertRaises(messages.MessageDestinationUnresolved):
                messages.OperatorMessages(send, resolve).post({"_review_origin": invalid}, {}, "")
            send.assert_not_called()
            client.get.assert_not_called()
        origin, client, resolve = self.destination_fixture()
        client.get.side_effect = OSError("metadata unavailable")
        send = mock.Mock()
        with self.assertRaises(messages.MessageDestinationUnresolved):
            messages.OperatorMessages(send, resolve).post({"_review_origin": origin}, {}, "")
        send.assert_not_called()

    def test_findings_check_and_old_head_are_factual_without_review_body(self):
        for kind, status in (
            ("findings_added", "new review findings"),
            ("review_completed", "review check completed"),
        ):
            origin, client, resolve = self.destination_fixture()
            send = mock.Mock(return_value={"id": 321})
            event = dict(
                repo=fixtures.REPO,
                number=7,
                kind=kind,
                head_matches=False,
                body="must not disclose",
                summary="must not disclose",
            )
            messages.OperatorMessages(send, resolve).post({"_review_origin": origin}, event, "")
            content = send.call_args.args[0]["content"]
            self.assertIn(status, content)
            self.assertIn("older PR head", content)
            self.assertNotIn("must not disclose", content)

    def test_destination_hold_can_recover_without_wake(self):
        with tempfile.TemporaryDirectory() as d:
            poster = mock.Mock(side_effect=messages.MessageDestinationUnresolved())
            svc, router = self.runner(d, poster)
            svc.cycle()
            self.assertEqual(router.submitted, [])
            self.assertEqual(
                svc.store.pending()[0]["reason"], "operator-message-destination-unresolved"
            )
            poster.side_effect = None
            poster.return_value = 321
            svc.cycle()
            self.assertEqual(router.submitted[0]["message_id"], 321)

    def test_only_independently_bound_origin_reaches_destination_resolver(self):
        with tempfile.TemporaryDirectory() as d:
            poster = mock.Mock(return_value=321)
            svc, router = self.runner(d, poster)
            origin = dict(
                platform="claude",
                verified_host="server",
                profile_scope="profile",
                session=fixtures.SESSION,
                seat="CLAUDE",
            )
            svc.origin_binder = mock.Mock(return_value=origin)
            svc.cycle()
            self.assertEqual(poster.call_args.args[0]["_review_origin"], origin)


if __name__ == "__main__":
    unittest.main()
