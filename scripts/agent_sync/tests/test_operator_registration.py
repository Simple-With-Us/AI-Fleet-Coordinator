from __future__ import annotations

import json
import os
import tempfile
import time
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
            # Existing tests model the pinned owner answering;  pass
            # answerer="seat_bot" to model the seat bot (kody 4366047282).
            "answerer": "owner",
            "now": 2000.0,
        }
        kwargs.update(overrides)
        return OREG.answer(self.db, **kwargs)

    def test_load_rejects_unknown_top_level_sections(self):
        self.begin()
        path = OREG.queue_path(self.db)
        data = OREG.L.read_json(path)
        data["surprise"] = {"x": 1}
        OREG.L.write_json(path, data, durable=True)
        with self.assertRaises(ValueError):
            OREG.load(self.db)

    def test_answer_rejects_a_malformed_entry_id(self):
        out = self.answer("garbage", "mac")
        self.assertEqual(out, {"status": "invalid", "reason": "malformed_entry_id"})
        self.assertEqual(self.registry.writes, [])

    def test_answer_refuses_when_the_registry_writer_refuses(self):
        entry = self.begin()["entry"]

        def refusing_register(**identity):
            return {
                "record_id": "r",
                "label": "",
                "generation": 0,
                "created": False,
                "outcome": "resumed_closed",
            }

        out = self.answer(entry["entry_id"], "mac", register=refusing_register)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["reason"], "resumed_closed")
        # The question stays open:  no false "Registered" confirmation, and no endless
        # re-ask loop over a session the registry will not letter.
        pending = OREG.pending_entries(self.db)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], OREG.STATE_ASKED)
        self.assertEqual(pending[0]["reason"], "register:resumed_closed")

    def test_answer_refuses_a_seat_mismatch_outcome(self):
        entry = self.begin()["entry"]

        def mismatch_register(**identity):
            return {
                "record_id": "r",
                "label": "A",
                "generation": 1,
                "created": False,
                "outcome": "seat_mismatch",
            }

        out = self.answer(entry["entry_id"], "mac", register=mismatch_register)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["reason"], "seat_mismatch")
        self.assertEqual(len(OREG.pending_entries(self.db)), 1)

    def test_answer_reply_words_a_refusal(self):
        text = OREG.answer_reply(
            {"status": "refused", "reason": "resumed_closed", "entry_id": "reg-aaaaaaaaaaaa"}
        )
        self.assertIn("refused", text)
        self.assertIn("reg-aaaaaaaaaaaa", text)

    def test_cli_registrations_read_a_missing_file_as_empty(self):
        registrations, _ = OREG._registry_functions(os.path.join(self.tmp.name, "no-such.db"))
        self.assertEqual(registrations("minimax", _session_id()), [])

    def test_mark_refused_drops_a_live_asked_entry_terminally(self):
        entry = self.begin()["entry"]
        self.assertTrue(OREG.mark_refused(self.db, entry["entry_id"], "the question was withheld: it matched the secret scan", now=3000.0))
        stored = OREG.load(self.db)["entries"][entry["entry_id"]]
        self.assertEqual(stored["state"], OREG.STATE_DROPPED)
        self.assertEqual(stored["reason"], "the question was withheld: it matched the secret scan")
        self.assertEqual(stored["dropped_at"], 3000.0)
        # Terminal stays terminal:  a second refusal finds nothing live, and the
        # answerable set no longer holds the entry.
        self.assertFalse(OREG.mark_refused(self.db, entry["entry_id"], "again", now=3001.0))

    def test_mark_reconciled_stamps_only_a_live_asked_entry(self):
        entry = self.begin()["entry"]
        self.assertTrue(OREG.mark_reconciled(self.db, entry["entry_id"], now=3000.0))
        stamped = OREG.pending_entries(self.db)[0]
        self.assertEqual(stamped["reconciled_at"], 3000.0)
        self.assertFalse(stamped.get("notified_at"))
        self.assertFalse(OREG.mark_reconciled(self.db, "reg-bbbbbbbbbbbb", now=3001.0))

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

    def test_a_settling_answer_registers_a_conflict(self):
        entry = self.begin(evidence_location="mac")["entry"]
        out = self.answer(entry["entry_id"], "cloud")
        self.assertEqual(out["status"], "conflict")
        # The confirmation promises it:  a settling answer re-runs every check and registers.
        settled = self.answer(entry["entry_id"], "mac")
        self.assertEqual(settled["status"], "registered")
        self.assertEqual(self.registry.writes[0]["verified_host"], "mac")
        self.assertEqual(OREG.pending_entries(self.db), [])

    def test_a_mismatched_settling_answer_keeps_the_conflict_open(self):
        entry = self.begin(evidence_location="mac")["entry"]
        self.answer(entry["entry_id"], "cloud")
        again = self.answer(entry["entry_id"], "server")
        self.assertEqual(again["status"], "conflict")
        self.assertEqual(again["reason"], OREG.REASON_EVIDENCE_MISMATCH)
        self.assertEqual(self.registry.writes, [])

    def test_answerable_entries_include_an_unsettled_conflict(self):
        entry = self.begin(evidence_location="mac")["entry"]
        self.answer(entry["entry_id"], "cloud")
        self.assertEqual(OREG.pending_entries(self.db), [])
        ids = [row["entry_id"] for row in OREG.answerable_entries(self.db)]
        self.assertEqual(ids, [entry["entry_id"]])

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

    def test_a_host_id_short_circuits_its_location_word(self):
        # The registry column holds host ids ("mac-1") as well as bare location words:
        # one machine must not disagree with itself, so an already-lettered session is
        # answered from the registry rather than asked again.
        self.registry.rows.append(
            {
                "platform": "minimax",
                "native_session_id": _session_id(),
                "verified_host": "mac-1",
                "label": "C",
                "generation": 1,
            }
        )
        out = self.begin(evidence_location="mac")
        self.assertEqual(out["status"], "already_registered")
        self.assertEqual(out["location"], "mac")
        self.assertEqual(OREG.pending_entries(self.db), [])

    def test_an_answer_matching_the_lettered_hosts_location_registers(self):
        entry = self.begin()["entry"]
        # Lettered at host id "mac-1" between ask and answer;  the answer "mac" is the
        # same location, not a conflict.
        self.registry.rows.append(
            {
                "platform": "minimax",
                "native_session_id": _session_id(),
                "verified_host": "mac-1",
                "label": "C",
                "generation": 1,
            }
        )
        out = self.answer(entry["entry_id"], "mac")
        self.assertEqual(out["status"], "registered")
        self.assertEqual(len(self.registry.writes), 1)

    def test_an_answer_against_a_different_locations_host_id_conflicts(self):
        entry = self.begin()["entry"]
        self.registry.rows.append(
            {
                "platform": "minimax",
                "native_session_id": _session_id(),
                "verified_host": "server-2",
                "label": "B",
                "generation": 1,
            }
        )
        out = self.answer(entry["entry_id"], "mac")
        self.assertEqual(out["status"], "conflict")
        self.assertEqual(out["reason"], OREG.REASON_OTHER_LOCATION)
        self.assertEqual(out["known"], ["server"])
        self.assertEqual(self.registry.writes, [])

    def test_unknown_and_late_answers_are_ignored(self):
        self.assertEqual(self.answer("reg-ffffffffffff", "mac")["status"], "ignored")
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
            self.begin(requester="api_key=sk-" + os.urandom(16).hex())
        self.assertEqual(OREG.pending_entries(self.db), [])

    def test_a_secret_shaped_route_is_refused_and_nothing_is_stored(self):
        with self.assertRaises(OREG.RegistrationRefused):
            self.begin(
                route={"channel": "agent-sync", "topic": "sk-" + os.urandom(16).hex()},
                route_topics=[{"channel": "agent-sync", "topic": "t", "expires": None}],
            )
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




    def test_seat_bot_answer_without_evidence_never_mints(self):
        # kody 4366047282:  an uncorroborated seat-bot claim minted a letter.
        entry = self.begin()["entry"]
        out = self.answer(entry["entry_id"], "mac", answerer="seat_bot")
        self.assertEqual(out["status"], "uncorroborated")
        self.assertEqual(self.registry.writes, [])
        # The question stays open and answerable for the owner.
        reloaded = OREG.load(self.db)["entries"][0]
        self.assertEqual(reloaded["state"], OREG.STATE_ASKED)
        again = self.answer(entry["entry_id"], "mac", answerer="owner")
        self.assertEqual(again["status"], "registered")

    def test_seat_bot_answer_matching_evidence_mints(self):
        # Evidence from the discovering channel corroborates the bot's answer.
        entry = self.begin(evidence_location="cloud")["entry"]
        out = self.answer(entry["entry_id"], "cloud", answerer="seat_bot")
        self.assertEqual(out["status"], "registered")
        self.assertEqual(self.registry.writes[0]["verified_host"], "cloud")

    def test_seat_bot_answer_contradicting_evidence_conflicts(self):
        entry = self.begin(evidence_location="cloud")["entry"]
        out = self.answer(entry["entry_id"], "mac", answerer="seat_bot")
        self.assertEqual(out["status"], "conflict")
        self.assertEqual(out["reason"], OREG.REASON_EVIDENCE_MISMATCH)
        self.assertEqual(self.registry.writes, [])

    def test_dual_discovery_identities_corroborate_independently(self):
        # The same chat discovered locally AND through a cloud adapter is two
        # platform identities;  each entry is corroborated by its own channel's
        # evidence, and an answer borrowed from the other channel conflicts.
        self.begin(platform="cursor", session_id="chat-1", evidence_location="mac")
        cloud = self.begin(platform="cursor-cloud", session_id="chat-1", evidence_location="cloud")["entry"]
        out = self.answer(cloud["entry_id"], "mac", answerer="seat_bot")
        self.assertEqual(out["status"], "conflict")
        out = self.answer(cloud["entry_id"], "cloud", answerer="seat_bot")
        self.assertEqual(out["status"], "registered")
        self.assertEqual(self.registry.writes[0]["verified_host"], "cloud")

    def test_uncorroborated_reply_points_to_the_owner(self):
        entry = self.begin()["entry"]
        out = self.answer(entry["entry_id"], "server", answerer="seat_bot")
        reply = OREG.answer_reply(out)
        self.assertIn("owner", reply)
        self.assertIn(entry["entry_id"], reply)


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

class QueueValidationAndPruningTests(unittest.TestCase):
    """Review findings:  full entry shape validation, bounded answer parsing, and
    pruning of settled queue entries."""

    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.dir.name, "registry.db")

    def tearDown(self) -> None:
        self.dir.cleanup()

    def _entry(self, **overrides):
        entry = {
            "entry_id": "reg-0123456789ab",
            "platform": "claude",
            "profile_scope": "default",
            "session_id": "sess-aaaaaaaa",
            "seat": "CLAUDE",
            "state": OREG.STATE_ASKED,
            "asked_at": 1000.0,
            "deadline": 2000.0,
        }
        entry.update(overrides)
        return entry

    def _write_queue(self, entries) -> None:
        path = OREG.queue_path(self.db)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            json.dump({"entries": entries}, handle)

    def test_an_unknown_entry_field_is_rejected(self):
        self._write_queue({"reg-0123456789ab": self._entry(notes="typo")})
        with self.assertRaises(ValueError):
            OREG.load(self.db)

    def test_a_malformed_generation_is_rejected(self):
        self._write_queue({"reg-0123456789ab": self._entry(generation=0)})
        with self.assertRaises(ValueError):
            OREG.load(self.db)

    def test_an_unknown_location_is_rejected(self):
        self._write_queue({"reg-0123456789ab": self._entry(location="moon")})
        with self.assertRaises(ValueError):
            OREG.load(self.db)

    def test_a_malformed_route_shape_is_rejected(self):
        self._write_queue(
            {"reg-0123456789ab": self._entry(route={"channel": "c", "topic": "t", "extra": 1})}
        )
        with self.assertRaises(ValueError):
            OREG.load(self.db)

    def test_an_oversized_answer_is_not_parsed(self):
        content = "pad " * 4000 + "mac reg-0123456789ab"
        self.assertIsNone(OREG.parse_answer(content))
        self.assertGreater(len(content.encode()), OREG.MAX_ANSWER_BYTES)

    def test_two_refs_or_two_locations_early_exit(self):
        self.assertIsNone(OREG.parse_answer("mac reg-0123456789ab reg-ffffffffffff"))
        self.assertIsNone(OREG.parse_answer("mac server reg-0123456789ab"))

    def test_the_sweep_prunes_old_settled_entries_and_keeps_the_rest(self):
        now = time.time()
        old = now - OREG.SETTLED_KEEP_SECONDS - 100
        entries = {
            "reg-0123456789ab": self._entry(
                state=OREG.STATE_REGISTERED, answered_at=old, asked_at=old - 10, deadline=old
            ),
            "reg-aaaaaaaaaaaa": self._entry(
                entry_id="reg-aaaaaaaaaaaa", state=OREG.STATE_REGISTERED, answered_at=now - 100
            ),
            "reg-bbbbbbbbbbbb": self._entry(
                entry_id="reg-bbbbbbbbbbbb", state=OREG.STATE_ASKED, asked_at=old, deadline=now + 100
            ),
            # An old conflict is NOT settled cleanup:  it stays answerable until the
            # owner settles it (kody 4366048276).
            "reg-cccccccccccc": self._entry(
                entry_id="reg-cccccccccccc", state=OREG.STATE_CONFLICT, answered_at=old, asked_at=old - 10, deadline=old
            ),
        }
        self._write_queue(entries)
        OREG.sweep(self.db, now=now)
        kept = OREG.load(self.db)["entries"]
        self.assertNotIn("reg-0123456789ab", kept)
        self.assertIn("reg-aaaaaaaaaaaa", kept)
        self.assertIn("reg-bbbbbbbbbbbb", kept)
        self.assertIn("reg-cccccccccccc", kept)


    def test_a_route_lost_entry_drops_now_and_is_not_answerable(self):
        # kody 4366047591:  a lost route must not sit silently until the 24h deadline -
        # the dropped entry leaves the answerable set so the next pass asks fresh.
        now = time.time()
        entries = {
            "reg-0123456789ab": self._entry(
                state=OREG.STATE_ASKED, asked_at=now - 100, deadline=now + 80000, route_lost=True
            ),
        }
        self._write_queue(entries)
        self.assertTrue(OREG.mark_dropped(self.db, "reg-0123456789ab", "route_lost", now=now))
        kept = OREG.load(self.db)["entries"]["reg-0123456789ab"]
        self.assertEqual(kept["state"], OREG.STATE_DROPPED)
        self.assertEqual(kept["reason"], "route_lost")
        self.assertEqual(kept["dropped_at"], round(now, 3))
        self.assertEqual(OREG.answerable_entries(self.db), [])
        # Not live twice:  a second drop is a no-op.
        self.assertFalse(OREG.mark_dropped(self.db, "reg-0123456789ab", "route_lost", now=now + 1))


if __name__ == "__main__":
    unittest.main()
