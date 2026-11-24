import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from native_custody import fresh
from native_store import reconcile


class Custody(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.p = Path(self.tmp.name)
        (self.p / "pending").mkdir()
        (self.p / "done").mkdir()
        self.t = SimpleNamespace(
            session_id="s", host="server", letter="II", generation=2
        )
        self.r = {
            "wake": {
                "wake_id": "ra-new",
                "credential_scope": "review-alerts",
                "required_session": "s",
                "require_generation": 2,
                "require_letter": "II",
                "message_id": 123,
            },
            "pick": {"target": vars(self.t)},
            "attempts": 1,
            "in_flight": True,
            "accepted_at": 100,
        }

    def tearDown(self):
        self.tmp.cleanup()

    def check(self):
        p = self.p / "pending/ra-new.json"
        p.write_text(json.dumps(self.r))
        p.chmod(0o600)
        return fresh(self.p, "ra-new", self.t, 99)

    def test_current_empty_head_turn(self):
        self.r["turn"] = {"id": "t-new", "riders": [], "message_ids": [123]}
        self.assertTrue(self.check())

    def test_turn_with_riders_refused(self):
        self.r["turn"] = {"id": "t-new", "riders": ["other"], "message_ids": [123, 456]}
        self.assertFalse(self.check())

    def test_new_exact(self):
        self.assertTrue(self.check())

    def test_legacy(self):
        self.r["accepted_at"] = 98
        self.assertFalse(self.check())

    def test_replayed(self):
        self.r["replayed"] = True
        self.assertFalse(self.check())

    def test_attempt2(self):
        self.r["attempts"] = 2
        self.assertFalse(self.check())

    def test_done(self):
        (self.p / "done/ra-new.json").write_text("{}")
        self.assertFalse(self.check())

    def test_wrong_generation(self):
        self.r["wake"]["require_generation"] = 3
        self.assertFalse(self.check())

    def test_nonauth(self):
        self.r["wake"]["credential_scope"] = ""
        self.assertFalse(self.check())

    def test_no_inflight(self):
        self.r.pop("in_flight")
        self.assertFalse(self.check())

    def test_rider(self):
        self.r["ride"] = {"head": "other"}
        self.assertFalse(self.check())


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.p = Path(self.tmp.name) / "db"
        self.c = sqlite3.connect(self.p)
        self.c.executescript(
            "CREATE TABLE session(id,directory);CREATE TABLE session_input(session_id,status,status_reason,promoted_message_id,payload);CREATE TABLE message(id,session_id,data);CREATE TABLE part(id,message_id,session_id,sequence,time_created,data);"
        )
        self.c.execute("INSERT INTO session VALUES(?,?)", ("s", "/fixture"))
        self.b = {"session_id": "s", "workspace": "/fixture"}

    def tearDown(self):
        self.c.close()
        self.tmp.cleanup()

    def add(self, text="fixture", key="cmd"):
        self.c.execute(
            "INSERT INTO message VALUES(?,?,?)",
            (
                "m",
                "s",
                json.dumps(
                    {
                        "role": "user",
                        "metadata": {
                            "conversationInputIntent": {"sourceCommandId": key}
                        },
                    }
                ),
            ),
        )
        self.c.execute(
            "INSERT INTO part VALUES(?,?,?,?,?,?)",
            ("p", "m", "s", 1, 1, json.dumps({"type": "text", "text": text})),
        )
        self.c.commit()

    def test_no_input(self):
        self.assertEqual(
            reconcile(self.p, self.b, "cmd", "fixture")["native_input"], "unverified"
        )

    def test_exact(self):
        self.add()
        self.assertEqual(
            reconcile(self.p, self.b, "cmd", "fixture")["native_input"], "persisted"
        )

    def test_wrong_text(self):
        self.add("wrong")
        self.assertTrue(reconcile(self.p, self.b, "cmd", "fixture")["conflict"])

    def test_wrong_key(self):
        self.add(key="other")
        self.assertEqual(
            reconcile(self.p, self.b, "cmd", "fixture")["native_input"], "unverified"
        )

    def test_wrong_workspace(self):
        self.add()
        self.b["workspace"] = "/other"
        self.assertEqual(
            reconcile(self.p, self.b, "cmd", "fixture")["native_input"], "unverified"
        )

    def test_anchor(self):
        self.add()
        self.c.execute(
            "UPDATE message SET data=?",
            (json.dumps({"role": "user", "anchor": {"sourceCommandId": "cmd"}}),),
        )
        self.c.commit()
        self.assertEqual(
            reconcile(self.p, self.b, "cmd", "fixture")["native_input"], "persisted"
        )

    def test_discarded_never_delivery(self):
        self.c.execute(
            "INSERT INTO session_input VALUES(?,?,?,?,?)",
            (
                "s",
                "discarded",
                "session_resumed",
                None,
                json.dumps(
                    {
                        "text": "fixture",
                        "conversationInputIntent": {
                            "sourceCommandId": "cmd",
                            "text": "fixture",
                        },
                    }
                ),
            ),
        )
        self.c.commit()
        r = reconcile(self.p, self.b, "cmd", "fixture")
        self.assertEqual(r["input_ledger"], "discarded")
        self.assertEqual(r["native_input"], "unverified")

    def test_promoted_message_conflict(self):
        self.add()
        self.c.execute(
            "INSERT INTO session_input VALUES(?,?,?,?,?)",
            (
                "s",
                "promoted",
                None,
                "other",
                json.dumps(
                    {
                        "text": "fixture",
                        "conversationInputIntent": {
                            "sourceCommandId": "cmd",
                            "text": "fixture",
                        },
                    }
                ),
            ),
        )
        self.c.commit()
        self.assertTrue(reconcile(self.p, self.b, "cmd", "fixture")["conflict"])

    def test_duplicate_messages_refused(self):
        self.add()
        self.c.execute(
            "INSERT INTO message VALUES(?,?,?)",
            (
                "m2",
                "s",
                json.dumps({"role": "user", "anchor": {"sourceCommandId": "cmd"}}),
            ),
        )
        self.c.commit()
        self.assertEqual(
            reconcile(self.p, self.b, "cmd", "fixture")["native_input"], "unverified"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Fetch(Store):
    def prepared(
        self,
        tool="mcp__fleet-tools__zulip_get_message",
        identity="ZC",
        response_id=123,
        status="completed",
        parent="m",
        truncated=False,
        body="alert fixture",
    ):
        self.add()
        self.c.execute(
            "INSERT INTO message VALUES(?,?,?)",
            (
                "a",
                "s",
                json.dumps(
                    {"role": "assistant", "parentID": parent, "time": {"completed": 4}}
                ),
            ),
        )
        output = (
            'simple-tools get_message:  message_id "123"\n1 message\nfixture wrapper\nBEGIN_UNTRUSTED_ZULIP nonce=0123456789abcdef\n'
            + json.dumps({"id": response_id, "body": body, "truncated": truncated})
            + "\nEND_UNTRUSTED_ZULIP nonce=0123456789abcdef"
        )
        self.part = {
            "type": "tool",
            "tool": tool,
            "state": {
                "status": status,
                "input": {"identity": identity, "message_id": 123},
                "output": output,
            },
        }
        self.c.execute(
            "INSERT INTO part VALUES(?,?,?,?,?,?)",
            ("fetch", "a", "s", 2, 2, json.dumps(self.part)),
        )
        self.c.commit()

    def evidence(self):
        return reconcile(self.p, self.b, "cmd", "fixture", 123)

    def test_authoritative_completed_fetch(self):
        self.prepared()
        self.assertEqual(self.evidence()["owning_alert"], "fetched")
        self.assertEqual(self.evidence()["turn"], "completed")

    def test_wrong_tool_only_candidate(self):
        self.prepared(tool="Bash")
        self.assertEqual(self.evidence()["owning_alert"], "unverified")

    def test_wrong_identity(self):
        self.prepared(identity="OPERATOR")
        self.assertEqual(self.evidence()["owning_alert"], "unverified")

    def test_wrong_response_id(self):
        self.prepared(response_id=124)
        self.assertEqual(self.evidence()["owning_alert"], "unverified")

    def test_incomplete_fetch(self):
        self.prepared(status="running")
        self.assertEqual(self.evidence()["owning_alert"], "unverified")

    def test_wrong_parent(self):
        self.prepared(parent="unrelated")
        self.assertEqual(self.evidence()["owning_alert"], "unverified")

    def test_truncated_response(self):
        self.prepared(truncated=True)
        self.assertEqual(self.evidence()["owning_alert"], "unverified")

    def test_empty_response_body(self):
        self.prepared(body="")
        self.assertEqual(self.evidence()["owning_alert"], "unverified")

    def test_wrapper_only_not_fetch(self):
        self.prepared()
        self.part["state"]["output"] = "output present but not a message"
        self.c.execute(
            "UPDATE part SET data=? WHERE id=?", (json.dumps(self.part), "fetch")
        )
        self.c.commit()
        self.assertEqual(self.evidence()["owning_alert"], "unverified")
