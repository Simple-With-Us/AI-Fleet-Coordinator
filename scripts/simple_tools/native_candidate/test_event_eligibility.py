import hashlib
import importlib
import unittest

# Existing package fixture loads source modules without changing installed adapters.

eligible = importlib.import_module("native_fixture_package.native_event_eligibility").eligible_event


class EventEligibilityTests(unittest.TestCase):
    def setUp(self):
        self.boundary = dict(
            repo="owner/repo",
            number=12,
            review_floor=100,
            check_floor=200,
            complete=True,
            epoch=1000,
        )
        self.wake_id = (
            "ra-" + hashlib.sha256(b"owner/repo#12:review_submitted:101:e0").hexdigest()[:24]
        )
        self.row = dict(
            repo="owner/repo",
            number=12,
            kind="review_submitted",
            event_id="101",
            source_epoch=1001,
            epoch=0,
            wake_id=self.wake_id,
            message_id=55,
        )

    def call(self):
        return eligible(self.boundary, self.row, self.wake_id, 55)

    def test_new_review_and_check(self):
        self.assertTrue(self.call())
        self.wake_id = (
            "ra-" + hashlib.sha256(b"owner/repo#12:review_completed:201:e0").hexdigest()[:24]
        )
        self.row.update(kind="review_completed", event_id="201", wake_id=self.wake_id)
        self.assertTrue(self.call())

    def test_old_identity_delayed_acceptance_does_not_become_fresh(self):
        self.row.update(event_id="100", accepted_at=10000000000, ts="2099-01-01")
        self.assertFalse(self.call())

    def test_delayed_source_time_with_new_id_is_excluded(self):
        self.row.update(source_epoch=999, event_id="999999")
        self.assertFalse(self.call())

    def test_realert_epoch_new_wake_is_excluded(self):
        self.row.update(epoch=1)
        self.assertFalse(self.call())

    def test_incomplete_census_and_unproved_batch_are_excluded(self):
        self.boundary["complete"] = False
        self.assertFalse(self.call())
        self.boundary["complete"] = True
        self.row.update(kind="findings_added", event_id="c999999")
        self.assertFalse(self.call())

    def test_binding_and_numeric_types_fail_closed(self):
        original = dict(self.row)
        for field, value in [
            ("repo", "other/repo"),
            ("number", 13),
            ("epoch", False),
            ("message_id", True),
            ("wake_id", "ra-other"),
            ("event_id", "01"),
            ("event_id", "-3"),
        ]:
            with self.subTest(field=field, value=value):
                self.row = {**original, field: value}
                self.assertFalse(self.call())


class PersistedEligibilityTests(unittest.TestCase):
    def test_authenticated_pending_identity_not_acceptance_time_controls_eligibility(
        self,
    ):
        import json
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from native_fixture_package.native_event_eligibility import persisted_event

        boundary = dict(
            repo="owner/repo",
            number=12,
            review_floor=100,
            check_floor=200,
            complete=True,
            epoch=1000,
        )
        wake_id = "ra-" + hashlib.sha256(b"owner/repo#12:review_submitted:101:e0").hexdigest()[:24]
        head = SimpleNamespace(wake_id=wake_id, message_id=55)
        row = dict(
            repo="owner/repo",
            number=12,
            kind="review_submitted",
            event_id="101",
            epoch=0,
            source_epoch=1001,
        )
        with tempfile.TemporaryDirectory(dir="/dev/shm") as tmp:
            state = Path(tmp)
            (state / "pending").mkdir()
            path = state / "pending" / (wake_id + ".json")
            value = dict(
                wake=dict(
                    wake_id=wake_id,
                    message_id=55,
                    source_event=row,
                    credential_scope="review-alerts",
                ),
                accepted_at=9999999999,
            )
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            self.assertTrue(persisted_event(state, "fixture", boundary, head))
            for change in [
                dict(source_epoch=999),
                dict(epoch=1),
                dict(event_id="100"),
                dict(kind="findings_added"),
            ]:
                value["wake"]["source_event"] = {**row, **change}
                path.write_text(json.dumps(value))
                self.assertFalse(persisted_event(state, "fixture", boundary, head))
            value["wake"].update(source_event=row, credential_scope="routine")
            path.write_text(json.dumps(value))
            self.assertFalse(persisted_event(state, "fixture", boundary, head))
