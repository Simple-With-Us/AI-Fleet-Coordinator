import importlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

fixture = importlib.import_module("test_integration")
Qualification = importlib.import_module("native_fixture_package.native_qualification").Qualification


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.root = Path(self.tmp.name)
        for n in ["pending", "done"]:
            (self.root / n).mkdir()
        self.target = SimpleNamespace(
            session_id="sess_fixture", host="server", letter="II", generation=2
        )
        self.head = SimpleNamespace(wake_id="ra-new", message_id=123, link_note="")
        self.now = 101
        self.sent, self.fallbacks = [], []
        self.check = True
        self.write_pending()
        self.q = self.make()

    def write_pending(self):
        x = {
            "wake": {
                "wake_id": "ra-new",
                "message_id": 123,
                "credential_scope": "review-alerts",
                "required_session": "sess_fixture",
                "require_letter": "II",
                "require_generation": 2,
            },
            "pick": {"target": vars(self.target)},
            "accepted_at": 101,
            "attempts": 1,
            "in_flight": True,
        }
        p = self.root / "pending/ra-new.json"
        p.write_text(json.dumps(x))
        p.chmod(0o600)

    def make(self, expires=200, qualification_generation=None):
        return Qualification(
            self.root,
            vars(self.target),
            "/fixture",
            100,
            expires,
            lambda *a: self.sent.append(a) or "unknown",
            lambda *a: self.fallbacks.append(a) or "fallback",
            lambda h, t: "deterministic " + h.wake_id + " " + str(h.message_id),
            lambda code: code,
            lambda *a: self.check,
            clock=lambda: self.now,
            qualification_generation=qualification_generation,
            source_boundary=(
                dict(
                    repo="owner/repo",
                    number=12,
                    review_floor=100,
                    check_floor=200,
                    complete=True,
                    epoch=100,
                )
                if qualification_generation is not None
                else None
            ),
        )

    def tearDown(self):
        self.q.close()
        self.tmp.cleanup()

    def call(self, riders=()):
        return self.q(self.target, self.head, riders, "t-1234567890123456")

    @patch(
        "native_fixture_package.native_event_eligibility.persisted_event",
        return_value=True,
    )
    def test_prospective_namespace_preserves_original_and_remains_cap_one(self, eligibility):
        self.call()
        original = {p.name: p.read_bytes() for p in self.q.store.folder.iterdir() if p.is_file()}
        self.q.close()
        self.q = self.make(qualification_generation="protocol2550-v2")
        self.head = SimpleNamespace(wake_id="ra-future", message_id=124, link_note="")
        self.write_pending()
        pending = self.root / "pending/ra-new.json"
        value = json.loads(pending.read_text())
        value["wake"].update(wake_id="ra-future", message_id=124)
        future = self.root / "pending/ra-future.json"
        future.write_text(json.dumps(value))
        future.chmod(0o600)
        self.assertEqual(self.call(), "unknown")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(
            {
                p.name: p.read_bytes()
                for p in (self.root / "native-qualification").iterdir()
                if p.is_file()
            },
            original,
        )
        self.q.close()
        self.q = self.make(qualification_generation="protocol2550-v2")
        self.assertEqual(self.call(), "native-qualification-slot-consumed")
        self.assertEqual(len(self.sent), 2)

    def test_new_namespace_excludes_old_journal_before_reservation(self):
        self.q.close()
        self.q = self.make(qualification_generation="protocol2550-v2")
        from native_fixture_package.zcode_native import digest

        folder = self.root / "native-commands"
        folder.mkdir()
        journal = folder / (digest({"wake_key": self.head.wake_id}) + ".json")
        journal.write_text("unreadable or uncertain original")
        self.assertEqual(self.call(), "native-qualification-prior-command")
        self.assertFalse((self.q.store.folder / "slot.claim").exists())
        self.assertEqual(self.sent, [])
        self.assertEqual(journal.read_text(), "unreadable or uncertain original")

    def test_generation_cannot_escape_private_namespace(self):
        for name in ["", "../old", "/tmp/other", "UPPER", True, "a" * 65]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.make(qualification_generation=name)

    def test_generation_contract_is_immutable_before_first_claim(self):
        self.q.close()
        self.q = self.make(qualification_generation="immutable-boundary")
        contract = (self.q.store.folder / "generation.json").read_bytes()
        self.q.close()
        self.q = self.make(qualification_generation="immutable-boundary")
        self.assertEqual((self.q.store.folder / "generation.json").read_bytes(), contract)
        with self.assertRaises(ValueError):
            self.make(expires=199, qualification_generation="immutable-boundary")
        self.assertFalse((self.q.store.folder / "slot.claim").exists())
        self.assertEqual((self.q.store.folder / "generation.json").read_bytes(), contract)

    def test_namespace_without_source_export_cannot_reserve(self):
        self.q.close()
        self.q = self.make(qualification_generation="needs-proof")
        self.assertEqual(self.call(), "fallback")
        self.assertFalse((self.q.store.folder / "slot.claim").exists())
        self.assertEqual(self.sent, [])

    @patch(
        "native_fixture_package.native_event_eligibility.persisted_event",
        return_value=True,
    )
    def test_namespace_concurrent_events_reserve_only_once(self, eligibility):
        self.q.close()
        self.q = self.make(qualification_generation="concurrent-generation")
        entered, release = threading.Event(), threading.Event()

        def native(*args):
            self.sent.append(args)
            entered.set()
            release.wait(2)
            return "unknown"

        self.q.native = native
        thread = threading.Thread(target=self.call)
        thread.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.call(), "native-qualification-slot-consumed")
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.sent), 1)
        self.assertTrue((self.q.store.folder / "generation.json").exists())

    def test_changed_generation_epoch_or_expiry_cannot_rearm_unused_slot(self):
        self.q.close()
        self.q = self.make(qualification_generation="frozen-window")
        original = (self.q.store.folder / "generation.json").read_bytes()
        for epoch, expires in [(99, 200), (100, 199), (100, None)]:
            with (
                self.subTest(epoch=epoch, expires=expires),
                self.assertRaises(ValueError),
            ):
                Qualification(
                    self.root,
                    vars(self.target),
                    "/fixture",
                    epoch,
                    expires,
                    lambda *a: None,
                    lambda *a: None,
                    lambda *a: "fixture",
                    lambda *a: None,
                    lambda *a: True,
                    qualification_generation="frozen-window",
                )
        self.assertEqual((self.q.store.folder / "generation.json").read_bytes(), original)
        self.assertFalse((self.q.store.folder / "slot.claim").exists())

    def test_same_generation_rejects_different_complete_source_boundary(self):
        self.q.close()
        self.q = self.make(qualification_generation="same-source-boundary")
        boundary = dict(
            repo="owner/repo",
            number=12,
            review_floor=101,
            check_floor=200,
            complete=True,
            epoch=100,
        )
        with self.assertRaises(ValueError):
            Qualification(
                self.root,
                vars(self.target),
                "/fixture",
                100,
                200,
                lambda *a: None,
                lambda *a: None,
                lambda *a: "fixture",
                lambda *a: None,
                lambda *a: True,
                qualification_generation="same-source-boundary",
                source_boundary=boundary,
            )

    def test_named_generation_rejects_missing_bad_boundary_before_files(self):
        for expires, boundary in [
            (None, None),
            (float("nan"), None),
            (3701, None),
            (200, None),
            (200, {}),
            (
                200,
                dict(
                    repo="owner/repo",
                    number=12,
                    review_floor=100,
                    check_floor=200,
                    complete=False,
                    epoch=100,
                ),
            ),
        ]:
            with (
                self.subTest(expires=expires, boundary=boundary),
                self.assertRaises(ValueError),
            ):
                Qualification(
                    self.root,
                    vars(self.target),
                    "/fixture",
                    100,
                    expires,
                    lambda *a: None,
                    lambda *a: None,
                    lambda *a: "fixture",
                    lambda *a: None,
                    lambda *a: True,
                    qualification_generation="invalid-no-write",
                    source_boundary=boundary,
                )
            self.assertFalse(
                (self.root / "native-qualification-generations/invalid-no-write").exists()
            )

    def test_event_driven_wait_and_restart_cap1(self):
        self.q.close()
        self.q = self.make(expires=None)
        self.now = 1000000
        self.assertEqual(self.call(), "unknown")
        self.assertEqual(len(self.sent), 1)
        self.q.close()
        self.q = self.make(expires=None)
        self.assertEqual(self.call(), "native-qualification-slot-consumed")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.fallbacks, [])
        self.head = SimpleNamespace(wake_id="ra-later", message_id=124, link_note="")
        self.assertEqual(self.call(), "fallback")
        self.assertEqual(len(self.sent), 1)

    def test_event_driven_rejects_pre_epoch_and_nonfinite_time(self):
        self.q.expires = None
        for now in [99, float("nan"), float("inf"), True]:
            self.now = now
            self.assertEqual(self.call(), "fallback")
        self.assertEqual(self.sent, [])
        self.assertFalse((self.q.store.folder / "slot.claim").exists())

    def test_event_driven_constructor_and_consumed_mode_change_fail_closed(self):
        self.q.close()
        self.q = self.make(expires=None)
        self.now = 1000000
        self.call()
        self.q.close()
        self.q = self.make()
        self.assertEqual(self.call(), "native-qualification-claim-invalid")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.fallbacks, [])

    def test_numeric_window_limits_still_enforced(self):
        for value in [True, False, float("inf"), float("nan"), 100, 3701]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.make(expires=value)

    def test_first_only_then_restart_consumed(self):
        self.assertEqual(self.call(), "unknown")
        self.assertEqual(len(self.sent), 1)
        self.call()
        self.assertEqual(len(self.sent), 1)
        self.q.close()
        self.q = self.make()
        self.call()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.fallbacks, [])

    def test_unmatched_target_unchanged(self):
        self.q(
            SimpleNamespace(**{**vars(self.target), "session_id": "sess_other"}),
            self.head,
            (),
            "t",
        )
        self.assertEqual(len(self.fallbacks), 1)
        self.assertEqual(self.sent, [])

    def test_expiry_before_reservation_unchanged(self):
        self.now = 200
        self.assertEqual(self.call(), "fallback")
        self.assertFalse((self.q.store.folder / "slot.claim").exists())

    def test_expiry_after_reservation_consumes(self):
        def custody(*a):
            if (self.q.store.folder / "slot.claim").exists():
                self.now = 201
            return True

        self.q.custody = custody
        self.call()
        self.assertEqual(self.sent, [])
        self.now = 102
        self.call()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fallbacks, [])

    def test_no_lock_held_during_native(self):
        import fcntl

        def native(*a):
            fd = os.open(self.q.store.folder / "slot.lock", os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)
            self.assertTrue(self.q.mutex.acquire(False))
            self.q.mutex.release()
            return "unknown"

        self.q.native = native
        self.assertEqual(self.call(), "unknown")

    def test_failed_preview_save_consumed_across_restart(self):
        with patch(
            "native_fixture_package.native_qualification._save",
            side_effect=OSError("full"),
        ):
            self.call()
        self.q.close()
        self.q = self.make()
        self.call()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fallbacks, [])

    def test_raised_native_never_fallback_or_repeat(self):
        def fail(*a):
            raise OSError("uncertain")

        self.q.native = fail
        self.call()
        self.call()
        self.assertEqual(self.fallbacks, [])
        self.q.close()
        self.q = self.make()
        self.call()
        self.assertEqual(self.sent, [])

    def test_binding_change_after_reservation_consumes(self):
        self.q.custody = lambda *a: not (self.q.store.folder / "slot.claim").exists()
        self.call()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fallbacks, [])

    def test_riders_and_old_receipts_not_selected(self):
        self.call(riders=(self.head,))
        self.assertEqual(len(self.fallbacks), 1)
        p = self.root / "pending/ra-new.json"
        x = json.loads(p.read_text())
        x["receipt"] = {}
        p.write_text(json.dumps(x))
        self.call()
        self.assertEqual(len(self.fallbacks), 2)
        self.assertEqual(self.sent, [])

    def test_concurrent_events_no_shared_wait(self):
        entered = threading.Event()
        release = threading.Event()

        def native(*a):
            entered.set()
            release.wait(2)
            return "unknown"

        self.q.native = native
        t = threading.Thread(target=self.call)
        t.start()
        self.assertTrue(entered.wait(1))
        self.assertEqual(self.call(), "native-qualification-slot-consumed")
        release.set()
        t.join(2)
        self.assertEqual(self.fallbacks, [])

    def test_message_mismatch_refused(self):
        self.head.message_id = 124
        self.call()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.fallbacks, [])

    def test_preview_exact_text_and_identity(self):
        self.call()
        p = json.loads((self.q.store.folder / "slot.json").read_text())
        self.assertEqual(p["message_id"], 123)
        self.assertEqual(p["text"], self.sent[0][2])
        self.assertEqual(p["target"], vars(self.target))

    def test_valid_json_corrupt_claim_never_fallback(self):
        for value in [{}, {"wake_id": "different"}, [], {"version": 2}]:
            with self.subTest(value=value):
                claim = self.q.store.folder / "slot.claim"
                claim.write_text(json.dumps(value))
                claim.chmod(0o600)
                self.call()
                self.assertEqual(self.sent, [])
                self.assertEqual(self.fallbacks, [])
                self.q.close()
                self.q = self.make()
                self.call()
                self.assertEqual(self.fallbacks, [])

    def test_changed_claim_hash_and_target_fail_closed(self):
        self.call()
        claim = self.q.store.folder / "slot.claim"
        original = json.loads(claim.read_text())
        for key, value in [
            ("text", "wrong"),
            ("target", {}),
            ("epoch", 99),
            ("version", 2),
            ("message_id", True),
        ]:
            claim.write_text(json.dumps({**original, key: value}))
            self.call()
            self.assertEqual(len(self.sent), 1)
            self.assertEqual(self.fallbacks, [])

    def test_valid_distinct_claim_callback_specific_fallback(self):
        self.call()
        self.head = SimpleNamespace(wake_id="ra-later", message_id=124, link_note="")
        specific = []
        r = self.q(
            self.target,
            self.head,
            (),
            "t-1234567890123456",
            fallback=lambda *a: specific.append(a) or "turn",
        )
        self.assertEqual(r, "turn")
        self.assertEqual(len(specific), 1)
        self.assertEqual(self.fallbacks, [])

    def test_distinct_contention_fallback_after_mutex_release(self):
        self.q.reservation_attempted = "ra-other"
        entered = threading.Event()
        release = threading.Event()
        answers = []

        def fallback(*a):
            self.assertTrue(self.q.mutex.acquire(False))
            self.q.mutex.release()
            entered.set()
            release.wait(2)
            answers.append("fallback")
            return "fallback"

        self.q.fallback = fallback
        t = threading.Thread(target=self.call)
        t.start()
        self.assertTrue(entered.wait(1))
        self.assertTrue(self.q.mutex.acquire(False))
        self.q.mutex.release()
        release.set()
        t.join(2)
        self.assertEqual(answers, ["fallback"])
        self.assertEqual(self.sent, [])
