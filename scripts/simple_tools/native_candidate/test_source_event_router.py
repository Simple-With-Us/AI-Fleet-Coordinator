import hashlib
import json
import unittest
from dataclasses import asdict

from simple_tools import wake_router as W


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.row = dict(
            repo="owner/repo",
            number=12,
            kind="review_submitted",
            event_id="101",
            epoch=0,
            source_epoch=1001,
        )
        wake_id = "ra-" + hashlib.sha256(b"owner/repo#12:review_submitted:101:e0").hexdigest()[:24]
        self.payload = dict(
            wake_id=wake_id,
            message_id=55,
            require_pr="owner/repo#12",
            source_event=self.row,
        )
        self.wake = W.wake_from_payload(self.payload, "@fixture")

    def test_generic_payload_parser_never_trusts_source_identity(self):
        self.assertIsNone(self.wake.source_event)

    def test_exact_authenticated_identity_is_copy_and_persists(self):
        proof = W.source_event(self.row, self.wake)
        self.assertEqual(proof, self.row)
        self.assertIsNot(proof, self.row)
        from dataclasses import replace

        accepted = replace(self.wake, credential_scope=W.REVIEW_ALERT_SCOPE, source_event=proof)
        restored = W.Wake(**json.loads(json.dumps(asdict(accepted))))
        self.assertEqual(restored.source_event, self.row)

    def test_old_wake_can_be_restored_without_field(self):
        self.assertIsNone(W.Wake(wake_id="old").source_event)

    def test_wrong_binding_epoch_batch_and_unbounded_values_fail_closed(self):
        for key, value in [
            ("repo", "other/repo"),
            ("number", True),
            ("event_id", "102"),
            ("event_id", "1" * 21),
            ("kind", "findings_added"),
            ("epoch", 1),
            ("source_epoch", float("nan")),
            ("source_epoch", True),
        ]:
            with self.subTest(key=key, value=value):
                self.assertIsNone(W.source_event({**self.row, key: value}, self.wake))


class AuthenticatedHttpIdentity(unittest.TestCase):
    def setUp(self):
        from simple_tools.tests import test_wake_router as fixture

        self.fixture = fixture
        fixture.Base.setUp(self)
        self.review = "fixture-distinct-service-key"
        self.router_instance = fixture.Base.router(self)
        self.router_instance.review_key = lambda: (self.review, "")
        self.port = fixture.Http.serve(self, self.router_instance)
        self.row = dict(
            repo="owner/repo",
            number=12,
            kind="review_submitted",
            event_id="101",
            epoch=0,
            source_epoch=1001,
        )
        self.wake_id = (
            "ra-" + hashlib.sha256(b"owner/repo#12:review_submitted:101:e0").hexdigest()[:24]
        )
        self.body = dict(
            contract=W.CONTRACT,
            seat="CLAUDE",
            wake_id=self.wake_id,
            message_id=55,
            require_pr="owner/repo#12",
            require_session=fixture.SID_A,
            require_letter="AA",
            require_generation=1,
            channel="review-alerts",
            source_event=self.row,
        )

    def __getattr__(self, name):
        from simple_tools.tests import test_wake_router as fixture

        value = getattr(fixture.Base, name, None)
        if callable(value):
            return value.__get__(self)
        raise AttributeError(name)

    def request(self, body, key):
        return self.fixture.Http.request(self, self.port, body=body, key=key)

    def test_authenticated_identity_persisted_duplicate_keeps_original(self):
        self.assertEqual(self.request(self.body, self.review)[0], 202)
        path = self.pending(self.wake_id)
        initial = path.read_bytes()
        self.assertEqual(json.loads(initial)["wake"]["source_event"], self.row)
        self.request(
            {**self.body, "source_event": {**self.row, "source_epoch": 2000}},
            self.review,
        )
        self.assertEqual(path.read_bytes(), initial)

    def test_routine_scope_cannot_export_identity(self):
        body = {**self.body, "wake_id": "ordinary"}
        self.assertEqual(self.request(body, self.key)[0], 202)
        self.assertIsNone(json.loads(self.pending("ordinary").read_text())["wake"]["source_event"])

    def test_bad_service_identity_retains_ordinary_delivery_metadata(self):
        body = {**self.body, "source_event": {**self.row, "event_id": "102"}}
        self.assertEqual(self.request(body, self.review)[0], 202)
        self.assertIsNone(
            json.loads(self.pending(self.wake_id).read_text())["wake"]["source_event"]
        )


class CoherentQualification(AuthenticatedHttpIdentity):
    def test_real_accepted_identity_eligibility_and_concurrent_single_reservation(self):
        import threading
        from types import SimpleNamespace
        from native_fixture_package.native_qualification import Qualification

        self.body["require_session"] = "sess_01234567-89ab-cdef-0123-456789abcdef"
        self.assertEqual(self.request(self.body, self.review)[0], 202)
        path = self.pending(self.wake_id)
        record = json.loads(path.read_text())
        target = dict(
            session_id=self.body["require_session"],
            host="server",
            letter="AA",
            generation=1,
        )
        record.update(attempts=1, in_flight=True, accepted_at=1002, pick=dict(target=target))
        path.write_text(json.dumps(record))
        path.chmod(0o600)
        sent, fallback = [], []
        entered, release = threading.Event(), threading.Event()
        boundary = dict(
            repo="owner/repo",
            number=12,
            review_floor=100,
            check_floor=200,
            complete=True,
            epoch=1000,
        )

        def native(*args):
            sent.append(args)
            entered.set()
            release.wait(2)
            return "unknown"

        def make():
            return Qualification(
                self.state,
                target,
                "/fixture",
                1000,
                1200,
                native,
                lambda *a: fallback.append(a) or "fallback",
                lambda *a: "fixture bounded notification",
                lambda code: code,
                lambda *a: True,
                clock=lambda: 1002,
                qualification_generation="coherent-source",
                source_boundary=boundary,
            )

        first, second = make(), make()
        head = SimpleNamespace(wake_id=self.wake_id, message_id=55)
        target_object = SimpleNamespace(**target)
        thread = threading.Thread(
            target=lambda: first(target_object, head, (), "t-1234567890123456")
        )
        try:
            thread.start()
            self.assertTrue(entered.wait(1))
            self.assertEqual(
                second(target_object, head, (), "t-1234567890123456"),
                "native-qualification-slot-consumed",
            )
            self.assertEqual(len(sent), 1)
            self.assertEqual(fallback, [])
            self.assertTrue(
                (
                    self.state / "native-qualification-generations/coherent-source/slot.claim"
                ).exists()
            )
        finally:
            release.set()
            thread.join(2)
            first.close()
            second.close()
