from __future__ import annotations

import os
import tempfile
import unittest

from agent_sync import operator_registration as OREG
from agent_sync import session_ids as SI


def _session_id(seed: str = "a") -> str:
    # A value that passes the fleet's own session id checker (letters, digits, . _ : -).
    return "sess-" + seed * 8


class FakeRegistry:
    """The injected registry:  a scan and a writer, with every write recorded."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = list(rows or [])
        self.writes: list[dict] = []

    def registrations(self, platform: str, session_id: str) -> list[dict]:
        return [
            row
            for row in self.rows
            if row.get("platform") == platform and row.get("native_session_id") == session_id
        ]

    def register(self, **identity) -> dict:
        self.writes.append(dict(identity))
        row = {
            "platform": identity["platform"],
            "native_session_id": identity["native_session_id"],
            "verified_host": identity["verified_host"],
            "label": "A",
            "generation": 1,
        }
        self.rows.append(row)
        return {"label": "A", "generation": 1, "outcome": "registered"}


class OperatorRegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.db")
        self.registry = FakeRegistry()

    def begin(self, **overrides):
        kwargs = {
            "platform": "minimax",
            "profile_scope": "default",
            "session_id": _session_id(),
            "seat": "MM",
            "requester": "jay",
            "registrations": self.registry.registrations,
            "now": 1000.0,
        }
        kwargs.update(overrides)
        return OREG.begin(self.db, **kwargs)

    def answer(self, entry_id: str, location: str, **overrides):
        kwargs = {
            "entry_id": entry_id,
            "location": location,
            "registrations": self.registry.registrations,
            "register": self.registry.register,
            "now": 2000.0,
        }
        kwargs.update(overrides)
        return OREG.answer(self.db, **kwargs)

    def test_begin_creates_one_pending_question(self):
        out = self.begin()
        self.assertEqual(out["status"], "ask")
        entry = out["entry"]
        self.assertEqual(entry["state"], OREG.STATE_ASKED)
        self.assertIn(entry["entry_id"], out["question"])
        for word in ("mac", "server", "cloud"):
            self.assertIn(word, out["question"])
        # The question never carries the whole session id.
        self.assertNotIn(_session_id(), out["question"])

    def test_second_begin_returns_the_live_question(self):
        first = self.begin()
        second = self.begin()
        self.assertEqual(second["status"], "pending")
        self.assertEqual(second["entry"]["entry_id"], first["entry"]["entry_id"])
        self.assertEqual(len(OREG.pending_entries(self.db)), 1)

    def test_begin_short_circuits_a_lettered_session(self):
        self.registry.rows.append(
            {
                "platform": "minimax",
                "native_session_id": _session_id(),
                "verified_host": "server",
                "label": "C",
                "generation": 2,
            }
        )
        out = self.begin()
        self.assertEqual(out["status"], "already_registered")
        self.assertEqual(out["location"], "server")
        self.assertEqual(out["label"], "C")
        self.assertEqual(OREG.pending_entries(self.db), [])

    def test_begin_reports_a_session_lettered_everywhere(self):
        for host in ("mac", "server"):
            self.registry.rows.append(
                {
                    "platform": "minimax",
                    "native_session_id": _session_id(),
                    "verified_host": host,
                    "label": "A",
                    "generation": 1,
                }
            )
        out = self.begin()
        self.assertEqual(out["status"], "ambiguous")
        self.assertEqual(len(out["registrations"]), 2)
        self.assertEqual(OREG.pending_entries(self.db), [])

    def test_answer_registers_at_the_validated_location(self):
        entry = self.begin()["entry"]
        out = self.answer(entry["entry_id"], "Server")
        self.assertEqual(out["status"], "registered")
        self.assertEqual(out["location"], "server")
        self.assertEqual(out["label"], "A")
        self.assertEqual(len(self.registry.writes), 1)
        write = self.registry.writes[0]
        self.assertEqual(write["verified_host"], "server")
        self.assertEqual(write["seat"], "MM")
        # The queue entry closes with the letter on it.
        self.assertEqual(OREG.pending_entries(self.db), [])

    def test_answer_refuses_a_word_that_is_not_a_location(self):
        entry = self.begin()["entry"]
        out = self.answer(entry["entry_id"], "laptop")
        self.assertEqual(out["status"], "invalid")
        self.assertEqual(sorted(out["allowed"]), ["cloud", "mac", "server"])
        self.assertEqual(self.registry.writes, [])
        # The question stays open for a real answer.
        self.assertEqual(len(OREG.pending_entries(self.db)), 1)

    def test_answer_conflicts_with_discovery_evidence(self):
        entry = self.begin(evidence_location="mac")["entry"]
        out = self.answer(entry["entry_id"], "cloud")
        self.assertEqual(out["status"], "conflict")
        self.assertEqual(out["reason"], OREG.REASON_EVIDENCE_MISMATCH)
        self.assertEqual(self.registry.writes, [])

    def test_an_unsettled_conflict_is_not_reasked(self):
        entry = self.begin(evidence_location="mac")["entry"]
        out = self.answer(entry["entry_id"], "cloud")
        self.assertEqual(out["status"], "conflict")
        again = self.begin(evidence_location="mac")
        # The contradiction is the owner's to settle:  no re-ask, no duplicate entry.
        self.assertEqual(again["status"], "conflict")
        self.assertEqual(again["entry"]["entry_id"], entry["entry_id"])
        self.assertEqual(len(OREG.load(self.db)["entries"]), 1)

    def test_answer_matching_evidence_registers(self):
        entry = self.begin(evidence_location="mac")["entry"]
        out = self.answer(entry["entry_id"], "mac")
        self.assertEqual(out["status"], "registered")
        self.assertEqual(self.registry.writes[0]["verified_host"], "mac")

    def test_answer_conflicts_when_lettered_at_another_location(self):
        entry = self.begin()["entry"]
        # Between the ask and the answer the session was lettered on the server.
        self.registry.rows.append(
            {
                "platform": "minimax",
                "native_session_id": _session_id(),
                "verified_host": "server",
                "label": "B",
                "generation": 1,
            }
        )
        out = self.answer(entry["entry_id"], "mac")
        self.assertEqual(out["status"], "conflict")
        self.assertEqual(out["reason"], OREG.REASON_OTHER_LOCATION)
        self.assertEqual(self.registry.writes, [])

    def test_unknown_and_late_answers_are_ignored(self):
        self.assertEqual(self.answer("reg-missing", "mac")["status"], "ignored")
        entry = self.begin()["entry"]
        dropped = OREG.sweep(self.db, now=1000.0 + OREG.DEFAULT_TIMEOUT_SECONDS + 1)
        self.assertEqual([row["entry_id"] for row in dropped], [entry["entry_id"]])
        self.assertEqual(dropped[0]["reason"], OREG.REASON_NO_REPLY)
        out = self.answer(entry["entry_id"], "mac")
        self.assertEqual(out["status"], "ignored")
        self.assertEqual(self.registry.writes, [])

    def test_sweep_leaves_unexpired_questions_open(self):
        entry = self.begin()["entry"]
        self.assertEqual(OREG.sweep(self.db, now=1000.0 + 60), [])
        self.assertEqual(len(OREG.pending_entries(self.db)), 1)
        out = self.answer(entry["entry_id"], "cloud")
        self.assertEqual(out["status"], "registered")

    def test_secret_shaped_text_is_refused_and_nothing_is_stored(self):
        with self.assertRaises(OREG.RegistrationRefused):
            self.begin(requester="api_key=sk-abcdef0123456789abcdef0123456789")
        self.assertEqual(OREG.pending_entries(self.db), [])

    def test_a_bad_session_id_is_refused(self):
        with self.assertRaises(ValueError):
            self.begin(session_id="no way")
        with self.assertRaises(ValueError):
            self.begin(platform="")

    def test_an_existing_malformed_queue_raises(self):
        with open(OREG.queue_path(self.db), "w") as fh:
            fh.write("{not json")
        with self.assertRaises(ValueError):
            OREG.pending_entries(self.db)

    def test_queue_file_is_private(self):
        self.begin()
        import stat

        mode = stat.S_IMODE(os.stat(OREG.queue_path(self.db)).st_mode)
        self.assertEqual(mode, 0o600)

    def test_session_id_checker_accepts_our_fixture(self):
        self.assertEqual(SI.check(_session_id(), "test"), _session_id())




class OperatorRegistrationRouteRefreshTest(unittest.TestCase):
    """A pending question's route:  kept while its topic stays attached, lost when the
    topic vanishes (lease shape never guesses a continuation), and re-pointed ONLY by a
    whole-topic rename event's own provenance (retarget_routes)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.db")
        self.registry = FakeRegistry()

    def begin(self, route=None, topics=None, now=1000.0):
        return OREG.begin(
            self.db,
            platform="minimax",
            profile_scope="default",
            session_id=_session_id(),
            seat="MM",
            requester="jay",
            route=route,
            route_topics=topics,
            registrations=self.registry.registrations,
            now=now,
        )

    def test_an_unrelated_new_topic_does_not_steal_the_route(self):
        self.begin(
            route={"channel": "agent-sync", "topic": "AFC Work"},
            topics=[{"channel": "agent-sync", "topic": "AFC Work", "expires": 5000.0}],
        )
        out = self.begin(
            route={"channel": "agent-sync", "topic": "Shiny New"},
            topics=[
                {"channel": "agent-sync", "topic": "AFC Work", "expires": 5000.0},
                {"channel": "agent-sync", "topic": "Shiny New", "expires": 9000.0},
            ],
        )
        self.assertEqual(out["entry"]["route"], {"channel": "agent-sync", "topic": "AFC Work"})
        self.assertIsNone(out.get("route_change"))

    def test_a_topic_gone_marks_the_route_lost_once(self):
        self.begin(
            route={"channel": "agent-sync", "topic": "AFC Work"},
            topics=[{"channel": "agent-sync", "topic": "AFC Work", "expires": 5000.0}],
        )
        out = self.begin(
            route={"channel": "agent-sync", "topic": "Elsewhere"},
            topics=[{"channel": "agent-sync", "topic": "Elsewhere", "expires": 9000.0}],
        )
        self.assertEqual(out.get("route_change"), "lost")
        self.assertTrue(out["entry"].get("route_lost"))
        # The stale route is kept - the question was asked there and nowhere honest.
        self.assertEqual(out["entry"]["route"], {"channel": "agent-sync", "topic": "AFC Work"})
        again = self.begin(
            route={"channel": "agent-sync", "topic": "Elsewhere"},
            topics=[{"channel": "agent-sync", "topic": "Elsewhere", "expires": 9000.0}],
        )
        self.assertIsNone(again.get("route_change"))

    def test_an_equal_expiry_replacement_is_not_a_rename(self):
        """An unrelated topic carrying the SAME expiry (permanent None most of all) is
        not a continuation:  expiry equality is not rename provenance, so the entry
        fails closed to route_lost rather than moving the question."""
        self.begin(
            route={"channel": "agent-sync", "topic": "AFC Work"},
            topics=[{"channel": "agent-sync", "topic": "AFC Work", "expires": None}],
        )
        out = self.begin(
            route={"channel": "agent-sync", "topic": "Unrelated Lane"},
            topics=[{"channel": "agent-sync", "topic": "Unrelated Lane", "expires": None}],
        )
        self.assertEqual(out.get("route_change"), "lost")
        self.assertEqual(out["entry"]["route"], {"channel": "agent-sync", "topic": "AFC Work"})

    def test_retarget_routes_repoints_the_pending_question(self):
        first = self.begin(
            route={"channel": "agent-sync", "topic": "AFC Work"},
            topics=[{"channel": "agent-sync", "topic": "AFC Work", "expires": None}],
        )
        moved = OREG.retarget_routes(
            self.db,
            seat="MM",
            old_channel="agent-sync",
            old_topic="AFC Work",
            new_channel="agent-sync",
            new_topic="AFC Work Renamed",
        )
        self.assertEqual([entry["entry_id"] for entry in moved], [first["entry"]["entry_id"]])
        entry = OREG.pending_entries(self.db)[0]
        self.assertEqual(entry["route"], {"channel": "agent-sync", "topic": "AFC Work Renamed"})

    def test_retarget_routes_matches_only_the_seat_and_route(self):
        self.begin(
            route={"channel": "agent-sync", "topic": "AFC Work"},
            topics=[{"channel": "agent-sync", "topic": "AFC Work", "expires": None}],
        )
        # A rename in another seat, or of another topic, leaves the entry alone.
        moved = OREG.retarget_routes(
            self.db, seat="CLAUDE",
            old_channel="agent-sync", old_topic="AFC Work",
            new_channel="agent-sync", new_topic="Renamed",
        )
        self.assertEqual(moved, [])
        moved = OREG.retarget_routes(
            self.db, seat="MM",
            old_channel="agent-sync", old_topic="Some Other Topic",
            new_channel="agent-sync", new_topic="Renamed",
        )
        self.assertEqual(moved, [])
        entry = OREG.pending_entries(self.db)[0]
        self.assertEqual(entry["route"], {"channel": "agent-sync", "topic": "AFC Work"})

    def test_retarget_routes_revives_a_lost_entry(self):
        """The pass can mark a route lost before the rename event lands;  the event's
        provenance still re-points it, because it proves where the question went."""
        self.begin(
            route={"channel": "agent-sync", "topic": "AFC Work"},
            topics=[{"channel": "agent-sync", "topic": "AFC Work", "expires": 5000.0}],
        )
        out = self.begin(
            route={"channel": "agent-sync", "topic": "Elsewhere"},
            topics=[{"channel": "agent-sync", "topic": "Elsewhere", "expires": 9000.0}],
        )
        self.assertEqual(out.get("route_change"), "lost")
        moved = OREG.retarget_routes(
            self.db, seat="MM",
            old_channel="agent-sync", old_topic="AFC Work",
            new_channel="agent-sync", new_topic="AFC Work Renamed",
        )
        self.assertEqual(len(moved), 1)
        entry = OREG.pending_entries(self.db)[0]
        self.assertEqual(entry["route"], {"channel": "agent-sync", "topic": "AFC Work Renamed"})
        self.assertFalse(entry.get("route_lost"))


if __name__ == "__main__":
    unittest.main()
