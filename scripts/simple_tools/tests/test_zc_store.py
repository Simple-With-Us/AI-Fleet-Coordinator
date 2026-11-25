"""Helper/fake-runner/real-sqlite-store regressions for the ZC transport evidence (single and turn)."""
import json, sqlite3, unittest
from unittest.mock import patch
from simple_tools import wake_router as WR, zcode_wake as ZC
from simple_tools.tests.test_zcode_wake import Base as ZBase, FakeChild, SID, FAILED_TURN, _holding_row

TID = "t-0123456789abcdef"
SIG = FAILED_TURN


class Base(ZBase):
    def setUp(self):
        super().setUp()
        self.db = self.cli
        c = sqlite3.connect(self.db)
        c.execute("CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, data TEXT)")
        c.execute("CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT)")
        c.commit(); c.close()
        self.cfg = {"workspace": str(self.workspace), "local_host": "server", "resume_timeout_seconds": 5}
        self.tgt = WR.Target(SID, "BB", "server", "", "", 3)

    def put(self, text, role="user", mid="m1", sid=SID, ptype="text", ts=1000):
        c = sqlite3.connect(self.db)
        c.execute("INSERT OR REPLACE INTO message VALUES (?,?,?)", (mid, sid, json.dumps({"role": role})))
        c.execute("INSERT INTO part VALUES (?,?,?,?,?)", (mid + ptype, mid, sid, ts, json.dumps({"type": ptype, "text": text})))
        c.commit(); c.close()

    def ev(self, prompt):
        return ZC._prompt_evidence(self.env, SID, prompt)

    def mk(self, rc, out, persist, role="user", text=None):
        c = sqlite3.connect(self.db); c.execute("DELETE FROM part"); c.execute("DELETE FROM message"); c.commit(); c.close()
        def runner(argv, **kw):
            ch = FakeChild(argv, rc, out, False)
            if persist:
                self.put(text if text is not None else argv[argv.index("-p") + 1], role=role)
            return ch
        return runner


class Evidence(Base):
    P = "[ZC wake turn]  w-aaaaaaaa w-bbbbbbbb " + TID

    def test_exact_user_part_is_true(self):
        self.put(self.P); self.assertIs(self.ev(self.P), True)

    def test_part_only_store_without_input_history_is_true_not_false(self):
        self.put(self.P); self.assertIs(self.ev(self.P), True)

    def test_absence_is_none_never_false(self):
        self.assertIsNone(self.ev(self.P))
        self.put("something else"); self.assertIsNone(self.ev(self.P))

    def test_assistant_quote_prefix_or_other_session_is_not_evidence(self):
        self.put(self.P, role="assistant", mid="m2")
        self.put(self.P + " extra", mid="m3")
        self.put(self.P[:20] + " " + TID, mid="m4")
        self.put(self.P, mid="m5", sid="sess_bbbb")
        self.put(self.P, mid="m6", ptype="reasoning")
        self.assertIsNone(self.ev(self.P))

    def test_exact_current_prompt_found_after_many_old_quoting_rows(self):
        c = sqlite3.connect(self.db)
        for n in range(150):  # old assistant quotes of the same ids, newer ids sort last by id
            c.execute("INSERT INTO message VALUES (?,?,?)", ("o%03d" % n, SID, json.dumps({"role": "assistant"})))
            c.execute("INSERT INTO part VALUES (?,?,?,?,?)", ("z%03d" % n, "o%03d" % n, SID, 10 + n,
                      json.dumps({"type": "text", "text": "re " + self.P})))
        c.commit(); c.close()
        self.assertIsNone(self.ev(self.P))
        self.put(self.P, mid="cur", ts=99999)
        self.assertIs(self.ev(self.P), True)
        ids = ("w-aaaaaaaa", "w-bbbbbbbb", TID)
        self.assertIs(ZC._prompt_evidence(self.env, SID, self.P, ids), True)

    def test_common_words_do_not_fill_the_window(self):
        c = sqlite3.connect(self.db)
        for n in range(120):
            c.execute("INSERT INTO message VALUES (?,?,?)", ("c%03d" % n, SID, json.dumps({"role": "user"})))
            c.execute("INSERT INTO part VALUES (?,?,?,?,?)", ("y%03d" % n, "c%03d" % n, SID, 5,
                      json.dumps({"type": "text", "text": "instructions other wake"})))
        c.commit(); c.close()
        p = "[ZC wake]  instructions w-cccccccc"
        self.put(p, mid="cur", ts=50)
        self.assertIs(self.ev(p), True)
        self.assertIs(ZC._prompt_evidence(self.env, SID, p, ("w-cccccccc",)), True)

    def test_missing_tables_or_store_is_none(self):
        self.db.unlink(); self.assertIsNone(self.ev(self.P))

    def test_wildcards_do_not_widen(self):
        p = "[ZC wake] w_1%x"; self.put("[ZC wake] wA1Bx"); self.assertIsNone(self.ev(p))


class Runner(Base):
    H, R = WR.Wake("w-aaaaaaaa", message_id=5), WR.Wake("w-bbbbbbbb", message_id=6)

    def single(self, rc, out=b"", persist=True, **k):
        with patch.object(ZC.WR.registry_api, "call", side_effect=self._registry([_holding_row(SID)])):
            return ZC.deliver_local(self.env, self.cfg, SID, self.H, None, runner=self.mk(rc, out, persist, **k))

    def turn(self, rc, out=b"", persist=True, **k):
        return ZC.deliver_turn_local(self.env, self.cfg, self.tgt, self.H, (self.R,), TID,
                                     runner=self.mk(rc, out, persist, **k))

    def test_single_clean_exit_is_sent_never_failed(self):
        for p in (True, False):
            self.assertEqual(self.single(0, persist=p).outcome, "sent")

    def test_single_nonzero_persisted_sent_unproven_refused(self):
        d = self.single(3); self.assertEqual((d.outcome, d.ack), ("sent", "unknown"))
        self.assertEqual(self.single(3, persist=False).outcome, "refused")

    def test_turn_clean_exit_carries_riders_confirms_none(self):
        for p in (True, False):
            t = self.turn(0, persist=p)
            self.assertEqual(t.delivery.outcome, "sent")
            self.assertEqual((t.carried, t.confirmed), (("w-bbbbbbbb",), ()))

    def test_turn_nonzero_persisted_sent_unproven_refused_unreadable(self):
        t = self.turn(3); self.assertEqual(t.delivery.outcome, "sent"); self.assertFalse(t.unreadable)
        t = self.turn(3, persist=False)
        self.assertEqual(t.delivery.outcome, "refused"); self.assertTrue(t.unreadable)

    def test_assistant_message_quoting_every_id_is_not_evidence(self):
        t = self.turn(3, text="noted w-aaaaaaaa w-bbbbbbbb " + TID, role="assistant")
        self.assertEqual(t.delivery.outcome, "refused")

    def test_signature_persisted_is_sent_unproven_is_refused_never_retried(self):
        self.assertEqual(self.single(0, SIG).outcome, "sent")
        d = self.single(0, SIG, persist=False)
        self.assertEqual((d.outcome, d.code), ("refused", "model-auth-login-failed"))
        t = self.turn(0, SIG, persist=False)
        self.assertEqual(t.delivery.outcome, "refused"); self.assertTrue(t.unreadable)
        self.assertIsNone(t.carried)

    def test_echoed_signature_with_no_readable_store_is_not_retried(self):
        c = sqlite3.connect(self.db); c.execute("DROP TABLE part"); c.commit(); c.close()
        def runner(argv, **kw):
            return FakeChild(argv, 0, b"quoted text: " + SIG, False)
        with patch.object(ZC.WR.registry_api, "call", side_effect=self._registry([_holding_row(SID)])):
            d = ZC.deliver_local(self.env, self.cfg, SID, self.H, None, runner=runner)
        t = ZC.deliver_turn_local(self.env, self.cfg, self.tgt, self.H, (self.R,), TID, runner=runner)
        self.assertEqual(d.outcome, "refused"); self.assertEqual(t.delivery.outcome, "refused")
        self.assertTrue(t.unreadable)

    def test_wake_variable_is_bound_in_both_paths(self):
        # The extracted helper must not reference a wake it was not given (NameError after launch).
        for rc, out in ((0, b""), (3, b""), (0, SIG)):
            self.single(rc, out); self.turn(rc, out)


class ConfiguredIndexTransport(Base):
    """The merged index fact and single/turn evidence use one live configuration."""

    def test_external_index_supports_single_turn_evidence_and_refresh(self):
        other = self.tmp / "external.sqlite"
        source = sqlite3.connect(self.index)
        dest = sqlite3.connect(other)
        try:
            source.backup(dest)
        finally:
            source.close()
            dest.close()
        con = sqlite3.connect(self.index)
        try:
            con.execute("DELETE FROM tasks")
            con.commit()
        finally:
            con.close()
        self.cfg["tasks_index"] = str(other)
        h = WR.Wake("w-aaaaaaaa", message_id=5)
        r = WR.Wake("w-bbbbbbbb", message_id=6)
        for role, expected in (("user", "sent"), ("assistant", "refused")):
            with self.subTest(role=role):
                platform = ZC.router_platform(
                    self.env, self.cfg, runner=self.mk(3, b"", True, role=role)
                )
                with patch.object(
                    ZC.WR.registry_api, "call", side_effect=self._registry([_holding_row(SID)])
                ):
                    self.assertEqual(platform.exclude([self.tgt]), set())
                    single = platform.deliver(self.tgt, h)
                self.assertEqual(single.outcome, expected)
                platform = ZC.router_platform(
                    self.env, self.cfg, runner=self.mk(3, b"", True, role=role)
                )
                turn = platform.deliver_turn(self.tgt, h, (r,), TID)
                self.assertEqual(turn.delivery.outcome, expected)
                self.assertEqual(turn.confirmed, ())
                if role == "user":
                    self.assertEqual(turn.carried, (r.wake_id,))
                else:
                    self.assertTrue(turn.unreadable)
                # Refresh the same mapping to the stale HOME index: both paths refuse
                # before launch, and exclusion follows the refreshed index too.
                self.cfg["tasks_index"] = str(self.index)
                self.assertEqual(platform.exclude([self.tgt]), {SID})
                self.assertEqual(platform.deliver(self.tgt, h).code, "session-not-found")
                refused = platform.deliver_turn(self.tgt, h, (r,), TID)
                self.assertEqual(refused.delivery.code, "session-not-found")
                self.assertNotEqual(refused.delivery.outcome, "sent")
                self.cfg["tasks_index"] = str(other)

if __name__ == "__main__": unittest.main()
