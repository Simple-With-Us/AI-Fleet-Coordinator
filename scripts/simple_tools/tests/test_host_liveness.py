"""AFC-878: the host's own liveness report, and the sweep that trusts it.

The registry's stale sweep used to end a letter on its own idle test:  a chat
not seen for the window, replaced by a newer chat of the same seat.  That test
is a guess about the chat,  and the chat's own host knows better.  A host now
reports the chats it still holds open,  with its own last-activity time for
each,  and the sweep asks that report before it recycles a pair.

Three cases,  and each has its own answer:

* the host says it still holds the chat and touched it lately:  the chat keeps
  its letter;
* the host says it no longer holds the chat (archived,  or gone):  the letter
  may be recycled;
* the host cannot be reached,  so its report has gone stale:  the letter is
  kept,  because an unreachable host is not positive evidence that its chats
  ended.
"""

from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import UTC, datetime, timedelta

from simple_tools import operator_registry as OR
from simple_tools import platform_letter_minimax as PM
from simple_tools.tests.test_listener_registry import OneWriterTest
from simple_tools.tests.test_platform_letter_one_table import Base


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


class LivenessReportTests(unittest.TestCase):
    """The report itself:  what the registry stores and what it reads back."""

    def setUp(self) -> None:
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        registry = OR.SessionRegistry(self.db)
        registry.close_db()

    def test_a_report_is_stored_and_read_back(self) -> None:
        now = datetime.now(UTC)
        answer = OR.record_host_liveness(
            self.db,
            platform="minimax",
            verified_host="server",
            live={"m-1": _iso(now), "m-2": _iso(now - timedelta(hours=2))},
        )
        self.assertEqual(answer["count"], 2)
        report = OR.host_liveness(self.db, "minimax", "server")
        self.assertEqual(set(report["live"]), {"m-1", "m-2"})
        self.assertEqual(report["verified_host"], "server")

    def test_a_report_with_no_time_says_nothing(self) -> None:
        # A chat the host named without saying when it was last touched says
        # nothing about whether it is still open, so it is left out.
        OR.record_host_liveness(
            self.db, platform="minimax", verified_host="server", live={"m-1": "not a time"}
        )
        report = OR.host_liveness(self.db, "minimax", "server")
        self.assertEqual(report["live"], {})

    def test_a_host_with_no_report_reads_as_none(self) -> None:
        self.assertEqual(OR.host_liveness(self.db, "minimax", "server"), {})


class AttestedRetirementTests(unittest.TestCase):
    """The gate:  what the host's report decides,  and what it does not."""

    class Row(dict):
        pass

    def row(self, record_id: str = "r-1", sid: str = "m-1") -> dict:
        return {
            "record_id": record_id,
            "platform": "minimax",
            "verified_host": "server",
            "native_session_id": sid,
        }

    def gate(self, rows, reports, *, minutes_ago: int = 0):
        now = datetime.now(UTC)
        cutoff = now - timedelta(minutes=120)
        return OR.attested_retirement(
            rows,
            reports,
            cutoff,
            now=now - timedelta(minutes=minutes_ago),
        )

    def test_a_live_chat_keeps_its_letter(self) -> None:
        now = datetime.now(UTC)
        reports = {
            ("minimax", "server"): {
                "reported_at": _iso(now),
                "live": {"m-1": _iso(now)},
            }
        }
        retirable, refused = self.gate([self.row()], reports)
        self.assertEqual(retirable, [])
        self.assertEqual(refused[0]["reason"], "host_confirms_live")

    def test_a_chat_the_host_no_longer_holds_may_go(self) -> None:
        now = datetime.now(UTC)
        reports = {("minimax", "server"): {"reported_at": _iso(now), "live": {}}}
        retirable, refused = self.gate([self.row()], reports)
        self.assertEqual(len(retirable), 1)
        self.assertEqual(refused, [])

    def test_a_chat_the_host_has_not_touched_may_go(self) -> None:
        now = datetime.now(UTC)
        reports = {
            ("minimax", "server"): {
                "reported_at": _iso(now),
                "live": {"m-1": _iso(now - timedelta(hours=40))},
            }
        }
        retirable, _refused = self.gate([self.row()], reports)
        self.assertEqual(len(retirable), 1)

    def test_an_unreachable_host_is_not_evidence_of_retirement(self) -> None:
        # The report has gone stale:  the host may be asleep.  Its silence is not
        # proof that the chat ended, so the letter stays.
        now = datetime.now(UTC)
        reports = {
            ("minimax", "server"): {
                "reported_at": _iso(now - timedelta(hours=3)),
                "live": {"m-1": _iso(now - timedelta(hours=40))},
            }
        }
        retirable, refused = self.gate([self.row()], reports)
        self.assertEqual(retirable, [])
        self.assertEqual(refused[0]["reason"], "host_unverified")

    def test_a_host_that_never_reported_keeps_the_old_rule(self) -> None:
        # A platform that has not adopted the report is not stranded:  the
        # sweep's own idle test still decides there.
        retirable, refused = self.gate([self.row()], {})
        self.assertEqual(len(retirable), 1)
        self.assertEqual(refused, [])


class ReportLiveOpTests(OneWriterTest):
    """The route:  one host's own view of its chats,  stored by the registry."""

    def report(self, live: dict[str, str], **extra) -> tuple[int, dict]:
        return self.call(
            "OPERATOR",
            "POST",
            "report-live",
            {"platform": "testbed", "verified_host": "mac", "live": live, **extra},
        )

    def test_the_report_is_stored(self) -> None:
        self.create_store()
        now = datetime.now(UTC)
        status, answer = self.report({"m-1": _iso(now)})
        self.assertEqual(status, 200, answer)
        self.assertEqual(answer["count"], 1)
        report = OR.host_liveness(self.db, "testbed", "mac")
        self.assertEqual(set(report["live"]), {"m-1"})

    def test_only_the_operator_may_report(self) -> None:
        self.create_store()
        status, answer = self.call(
            "CLAUDE", "POST", "report-live", {"platform": "testbed", "verified_host": "mac", "live": {}}
        )
        self.assertEqual(status, 403, answer)

    def test_a_report_without_a_host_is_refused(self) -> None:
        self.create_store()
        status, answer = self.call(
            "OPERATOR", "POST", "report-live", {"platform": "testbed", "live": {}}
        )
        self.assertEqual(status, 400, answer)

    def test_a_report_that_is_not_an_object_is_refused(self) -> None:
        self.create_store()
        status, answer = self.report(["m-1"])  # type: ignore[arg-type]
        self.assertEqual(status, 400, answer)


class SweeperLivenessTests(OneWriterTest):
    """The sweeper:  the host's answer decides,  and the reason is named."""

    def setUp(self) -> None:
        super().setUp()
        self.service.gone_report_live_minutes = 0

    def report(self, live: dict[str, str], *, minutes_ago: int = 0) -> None:
        now = datetime.now(UTC) - timedelta(minutes=minutes_ago)
        OR.record_host_liveness(
            self.db,
            platform="testbed",
            verified_host="mac",
            live=live,
            now=now,
        )

    def test_a_live_chat_keeps_its_letter(self) -> None:
        self.create_store()
        self.register("CLAUDE", "old-1")
        self.age("old-1", 40)
        self.register("CLAUDE", "new-1")
        now = datetime.now(UTC)
        self.report({"old-1": _iso(now)})
        result = self.sweeper().maybe_run(self.clock.now)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ended"], 0)
        self.assertIn("old-1", self.open_sids())
        self.assertIn(
            "host_confirms_live", {row["reason"] for row in result["kept"]}
        )
        self.assertEqual(result["refused"], [])

    def test_a_chat_the_host_no_longer_holds_is_recycled(self) -> None:
        self.create_store()
        self.register("CLAUDE", "old-1")
        self.age("old-1", 40)
        self.register("CLAUDE", "new-1")
        self.report({})
        result = self.sweeper().maybe_run(self.clock.now)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ended"], 1)
        self.assertNotIn("old-1", self.open_sids())

    def test_an_unreachable_host_keeps_the_letter(self) -> None:
        self.create_store()
        self.register("CLAUDE", "old-1")
        self.age("old-1", 40)
        self.register("CLAUDE", "new-1")
        now = datetime.now(UTC)
        self.report({"old-1": _iso(now - timedelta(hours=40))}, minutes_ago=180)
        result = self.sweeper().maybe_run(self.clock.now)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ended"], 0)
        self.assertIn("old-1", self.open_sids())
        self.assertIn("host_unverified", {row["reason"] for row in result["kept"]})
        self.assertEqual(result["refused"], [])

    def test_a_host_that_never_reported_keeps_the_old_rule(self) -> None:
        self.create_store()
        self.register("CLAUDE", "old-1")
        self.age("old-1", 40)
        self.register("CLAUDE", "new-1")
        result = self.sweeper().maybe_run(self.clock.now)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["ended"], 1)
        self.assertNotIn("old-1", self.open_sids())


#: The runtime's own table,  with the columns this row reads.
STORE_DDL = (
    "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT, session_kind TEXT,"
    " parent_session_id TEXT, archived INTEGER NOT NULL DEFAULT 0, updated_at_ms INTEGER)"
)


class MiniMaxLiveChatsTests(Base):
    """The MiniMax row's `live_chats`:  the chats this host's store still holds open.

    The store is the truth about this host's chats:  it holds every chat the
    runtime made,  it says which ones are archived,  and it carries the time
    each chat was last touched.
    """

    def setUp(self) -> None:
        super().setUp()
        self.store = PM.store_file(self.env())
        os.makedirs(os.path.dirname(self.store), exist_ok=True)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(STORE_DDL)
        conn.close()

    def add(self, sid: str, *, archived: bool = False, millis: int = 1_700_000_000_000) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO local_runtime_sessions"
                " (session_id, title, session_kind, parent_session_id, archived, updated_at_ms)"
                " VALUES (?, '', 'conversation', NULL, ?, ?)",
                (sid, 1 if archived else 0, millis),
            )
        conn.close()

    def test_the_row_names_its_live_chats_with_their_own_times(self) -> None:
        self.assertIs(PM.SPEC.live_chats, PM.minimax_live)
        self.add("m-0001", millis=1_700_000_000_000)
        self.add("m-0002", archived=True)
        live = PM.minimax_live(self.env())
        self.assertEqual(set(live), {"m-0001"})
        self.assertTrue(live["m-0001"].endswith("Z"))
        self.assertTrue(live["m-0001"].startswith("2023-11-14T"))

    def test_a_store_that_cannot_be_read_names_no_live_chats(self) -> None:
        with open(self.store, "wb") as fh:
            fh.write(b"this is not a session store" * 8)
        self.assertEqual(PM.minimax_live(self.env()), {})

    def test_a_store_without_the_time_column_names_no_live_chats(self) -> None:
        os.remove(self.store)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "CREATE TABLE local_runtime_sessions (session_id TEXT PRIMARY KEY, title TEXT,"
                " session_kind TEXT, parent_session_id TEXT, archived INTEGER NOT NULL DEFAULT 0)"
            )
        conn.close()
        self.assertEqual(PM.minimax_live(self.env()), {})


if __name__ == "__main__":
    unittest.main()
