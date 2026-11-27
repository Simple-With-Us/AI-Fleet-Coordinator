import tempfile

from review_alerts.service import source_identity
from review_alerts.tests import test_review_alerts as t


class Identity(t.ServiceCycleTests):
    def test_source_time_is_persisted_before_submit_and_same_id_recovery(self):
        class Router(t.FakeRouter):
            def submit(self, *args, **kwargs):
                self.identity = kwargs.get("source_event")
                return super().submit(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            router = Router()
            svc = self._service(tmp, [t.claimed_owner()], t.FakeRead(), router)
            svc.cycle()
            svc.gh = t.FakeRead(reviews=[t.kody_review(51)])
            svc.cycle()
            self.assertEqual(router.identity["event_id"], "51")
            row = svc.store.get(t.REPO, 7, "review_submitted", "51")
            self.assertEqual(row["source_time"], "2026-11-21T01:00:00Z")
            before = dict(router.identity)
            svc._resubmit(row, t.claimed_owner(), {"submitted": 0, "held": 0})
            self.assertEqual(router.identity, before)
            self.assertIsNone(source_identity({**row, "source_time": None}))
            self.assertIsNone(source_identity({**row, "kind": "findings_added"}))

    def test_findings_batch_identity_is_exact_membership(self):
        members = [
            {"id": 301, "at": "2026-11-21T01:00:01Z", "head": "a" * 40},
            {"id": 302, "at": "2026-11-21T01:00:02Z", "head": "a" * 40},
        ]
        row = dict(
            repo=t.REPO,
            number=7,
            kind="findings_added",
            event_id="c302",
            epoch=0,
            source_time="2026-11-21T01:00:02Z",
            members=members,
        )
        identity = source_identity(row)
        self.assertEqual(identity["members"], members)
        self.assertIsNot(identity["members"], members)
        self.assertEqual(identity["source_epoch"], 1795222802.0)
        self.assertNotIn("source_time", identity)
        # source_epoch is the newest member INSTANT, which need not belong to
        # the highest-id member the event id names.
        reordered = [
            {"id": 301, "at": "2026-11-21T01:00:02Z", "head": "a" * 40},
            {"id": 302, "at": "2026-11-21T01:00:01Z", "head": "a" * 40},
        ]
        identity = source_identity({**row, "members": reordered})
        self.assertEqual(identity["source_epoch"], 1795222802.0)
        # Absent, oversized, malformed or event-id-mismatched membership is
        # ordinary delivery, never an identity.
        for change in [
            {"members": None},
            {"members": members * 51},
            {"members": [dict(members[0], extra=1), members[1]]},
            {"members": [dict(members[0], head="A" * 40), members[1]]},
            {"members": [members[1], members[0]]},
            {"members": [dict(members[0], at="not-a-time"), members[1]]},
            {"event_id": "c301"},
        ]:
            with self.subTest(change=change):
                self.assertIsNone(source_identity({**row, **change}))

    def test_findings_batch_identity_exported_to_router(self):
        class Router(t.FakeRouter):
            def submit(self, *args, **kwargs):
                self.identity = kwargs.get("source_event")
                return super().submit(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            router = Router()
            svc = self._service(tmp, [t.claimed_owner()], t.FakeRead(), router)
            svc.cycle()
            svc.gh = t.FakeRead(
                comments=[t.sentry_comment(301), t.sentry_comment(302)],
            )
            svc.cycle()
            self.assertEqual(router.identity["event_id"], "c302")
            self.assertEqual(
                router.identity["members"],
                [
                    {"id": 301, "at": "2026-11-21T01:00:00Z", "head": t.HEAD},
                    {"id": 302, "at": "2026-11-21T01:00:00Z", "head": t.HEAD},
                ],
            )
