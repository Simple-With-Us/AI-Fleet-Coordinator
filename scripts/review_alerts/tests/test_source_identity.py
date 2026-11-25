import tempfile
import unittest
from review_alerts.tests import test_review_alerts as t
from review_alerts.service import source_identity


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
