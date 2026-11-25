"""The ZCode adapter of `platform-letter` (OPS-1048): Operator session letters and "XX: Title" names for ZCode
chats on the server and the Mac.  ZCode's own store is a temporary SQLite file shaped like the real
`~/.zcode/cli/db/db.sqlite` (`session`), the registry is a temporary one in the double (pair) letter space, and the
hook config is a temporary `~/.zcode/cli/config.json` that already carries another tool's hooks.  No test depends
on which pair the registry hands out first, only on its being a pair, and on the child landing after its parent in
the parent's row."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import shlex
import tempfile
import time
import unittest
from unittest import mock

from simple_tools import listener_api as API
from simple_tools import listener_registry as RG
from simple_tools import operator_registry as OR
from simple_tools import platform_letter as PL
from simple_tools import platform_letter_zcode as Z
from simple_tools.tests.registry_fake import FakeRegistry

PAIR = r"^[A-Z]{2}$"

SESSION_DDL = (
    "CREATE TABLE session ("
    " id text primary key,"
    " project_id text not null default '',"
    " parent_id text,"
    " slug text not null default '',"
    " directory text not null default '',"
    " title text not null default '',"
    " time_created integer not null default 0,"
    " time_updated integer not null default 0,"
    " task_type text not null default 'interactive',"
    " title_source text not null default 'first_input',"
    " time_title_updated integer,"
    " time_archived integer,"
    " time_deleted integer)"
)


def sess(n: int) -> str:
    return "sess_%010d-0000-0000-0000-000000000000" % n


class Base(unittest.TestCase):
    """A home with a ZCode store, and the Operator's registry under the state folder."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(os.path.join(self.home, ".zcode", "cli", "db"))
        self.store = os.path.join(self.home, ".zcode", "cli", "db", "db.sqlite")
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(SESSION_DDL)
        conn.close()
        # The one assigner:  the registry API, served in process over a temporary store.
        self.assigner = FakeRegistry(os.path.join(self.tmp.name, "assigner"))
        if getattr(self, "FAKE", True):
            self.assigner.install()
            self.addCleanup(self.assigner.uninstall)
        self.db = self.assigner.db

    def env(self, **extra: str) -> dict[str, str]:
        env = {"HOME": self.home, "SIMPLE_TOOLS_STATE_DIR": self.state, **(self.assigner.env() if getattr(self, "FAKE", True) else {})}
        env.update(extra)
        return env

    def add_session(self, sid: str, *, parent: str = "", task: str = "interactive",
                    title: str = "", title_source: str = "first_input",
                    age: float = 3600.0) -> None:
        stamp = int((time.time() - age) * 1000)
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO session (id, parent_id, task_type, title, title_source, directory,"
                " time_created, time_updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, parent or None, task, title, title_source, "/tmp/work", stamp, stamp),
            )
        conn.close()

    def run_hook(self, payload: dict, *, location: str = "mac", now: float | None = None,
                 env: dict | None = None) -> tuple[int, str]:
        out = io.StringIO()
        code = PL.hook(
            Z.SPEC,
            env if env is not None else self.env(),
            io.StringIO(json.dumps(payload)),
            out,
            location_flag=location,
            now=time.time() if now is None else now,
        )
        return code, out.getvalue()

    def rows(self) -> list[tuple]:
        conn = sqlite3.connect(self.store)
        rows = conn.execute(
            "SELECT id, title, title_source FROM session ORDER BY id"
        ).fetchall()
        conn.close()
        return rows


class Spec(Base):
    def test_spec_fields(self) -> None:
        spec = PL.load_spec("zcode")
        self.assertIs(spec, Z.SPEC)
        self.assertEqual(spec.platform, "zcode")
        self.assertEqual(spec.seat_for({}, self.env()), "ZC")
        self.assertEqual(spec.display, "ZCode")
        self.assertEqual(spec.locations, ("mac", "server"))
        self.assertEqual(spec.events["SessionStart"], "start")
        self.assertEqual(spec.events["UserPromptSubmit"], "prompt")
        self.assertEqual(spec.events["Stop"], "turn")
        self.assertEqual(spec.close_reasons, ())
        self.assertIn("Stop", spec.quiet_events)
        self.assertIs(spec.unlettered_sessions, Z.zcode_unlettered_sessions)
        self.assertIs(spec.child_sessions, Z.zcode_child_sessions)


class Accessors(Base):
    def test_parent_kind_titles_from_store(self) -> None:
        parent, child = sess(1), sess(2)
        self.add_session(parent, title="Mutex semantics")
        self.add_session(child, parent=parent, task="subagent_child", title="Search breadth")
        payload = {"session_id": child, "hook_event_name": "SessionStart"}
        self.assertEqual(Z.zcode_parent(payload, self.env()), parent)
        self.assertEqual(Z.zcode_kind(payload, self.env()), "subagent")
        self.assertEqual(Z.zcode_stored_title(payload, self.env()), "Search breadth")
        self.assertEqual(
            Z.zcode_kind({"session_id": parent, "hook_event_name": "SessionStart"}, self.env()),
            "chat",
        )

    def test_unknown_session_is_a_chat_without_parent(self) -> None:
        payload = {"session_id": sess(99), "hook_event_name": "SessionStart"}
        self.assertEqual(Z.zcode_kind(payload, self.env()), "chat")
        self.assertEqual(Z.zcode_parent(payload, self.env()), "")

    def test_payload_title_wins_over_store(self) -> None:
        sid = sess(3)
        self.add_session(sid, title="from store")
        payload = {"session_id": sid, "title": "from payload"}
        self.assertEqual(Z.zcode_current_title(payload, self.env()), "from payload")
        self.assertEqual(Z.zcode_current_title({"session_id": sid}, self.env()), "from store")

    def test_chat_payload_carries_start_shape(self) -> None:
        payload = Z.zcode_chat_payload(sess(4))
        self.assertEqual(payload["hook_event_name"], "SessionStart")
        self.assertEqual(payload["session_id"], sess(4))
        self.assertEqual(payload["source"], "startup")


class HookFlow(Base):
    def test_start_registers_a_pair_and_takes_a_letter(self) -> None:
        sid = sess(10)
        self.add_session(sid, title="Fix the wake path")
        code, out = self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"}
        )
        self.assertEqual(code, 0)
        registry = OR.SessionRegistry(self.db)
        record = registry.lookup("zcode", "mac", "default", sid)
        self.assertTrue(record)
        self.assertRegex(record["label"], PAIR)
        self.assertEqual(record["seat"], "ZC")

    def test_child_takes_the_pair_after_its_parent(self) -> None:
        parent, child = sess(11), sess(12)
        self.add_session(parent, title="Parent work")
        self.add_session(child, parent=parent, task="subagent_child", title="Search the repo")
        self.run_hook(
            {"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"}
        )
        self.run_hook(
            {"session_id": child, "hook_event_name": "SessionStart", "source": "startup"}
        )
        registry = OR.SessionRegistry(self.db)
        mom = registry.lookup("zcode", "mac", "default", parent)
        kid = registry.lookup("zcode", "mac", "default", child)
        row = "ABCDEFGHIJKLMNOPQRSTUVWXYZ".index(mom["label"][0])
        self.assertRegex(mom["label"], PAIR)
        self.assertEqual(mom["label"], mom["label"][0] * 2)  # a main chat takes a doubled pair
        self.assertEqual(kid["label"][0], mom["label"][0])  # the child sits in the parent's row
        self.assertNotEqual(kid["label"], mom["label"])
        self.assertEqual(kid["label"], "%s%s" % (mom["label"][0], "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[row + 1]))

    def test_stop_touches_and_never_closes(self) -> None:
        sid = sess(13)
        self.add_session(sid, title="Long chat")
        self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"}
        )
        code, out = self.run_hook({"session_id": sid, "hook_event_name": "Stop"})
        self.assertEqual(code, 0)
        registry = OR.SessionRegistry(self.db)
        record = registry.lookup("zcode", "mac", "default", sid)
        self.assertTrue(record)  # a live label after Stop: the registration was not closed

    def test_off_switch_and_launcher_take_no_letter(self) -> None:
        sid = sess(14)
        self.add_session(sid)
        self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
            env=self.env(SESSION_LETTER="0"),
        )
        registry = OR.SessionRegistry(self.db)
        self.assertIsNone(registry.lookup("zcode", "mac", "default", sid))

    def test_hook_never_fails_the_chat(self) -> None:
        code, out = self.run_hook({"hook_event_name": "SessionStart", "session_id": "not-a-session"})
        self.assertEqual(code, 0)


class ChildEnrollment(Base):
    """A lettered parent's own turn end enrolls the sub-agent children the store names for it:  ZCode runs no
    hook inside a sub-agent session, so the parent's Stop pass is the only start a child ever gets.  Every
    store this class touches is the fixture's temporary one;  the live store is never opened for writing."""

    def note(self, sid: str) -> dict:
        with open(os.path.join(self.state, "ZC", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def child_labels(self, sids: list[str]) -> dict[str, str | None]:
        registry = OR.SessionRegistry(self.db)
        return {sid: (registry.lookup("zcode", "mac", "default", sid) or {}).get("label") for sid in sids}

    def test_a_parents_stop_enrolls_its_child_in_the_parents_row(self) -> None:
        parent, child = sess(70), sess(71)
        self.add_session(parent, title="Search the store")
        self.add_session(child, parent=parent, task="subagent_child", title="Find the wake path")
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        code, _ = self.run_hook({"session_id": parent, "hook_event_name": "Stop"})
        self.assertEqual(code, 0)
        registry = OR.SessionRegistry(self.db)
        mom = registry.lookup("zcode", "mac", "default", parent)
        kid = registry.lookup("zcode", "mac", "default", child)
        self.assertTrue(kid)
        self.assertEqual(kid["label"][0], mom["label"][0])  # the child sits in the parent's row
        self.assertNotEqual(kid["label"], mom["label"])
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[child], ("%s: Find the wake path" % kid["label"], "custom"))

    def test_a_letterless_parent_enrolls_nothing(self) -> None:
        parent, child = sess(72), sess(73)
        self.add_session(parent, title="Parent work")
        self.add_session(child, parent=parent, task="subagent_child", title="Child work")
        code, _ = self.run_hook({"session_id": parent, "hook_event_name": "Stop"})
        self.assertEqual(code, 0)
        registry = OR.SessionRegistry(self.db)
        self.assertIsNone(registry.lookup("zcode", "mac", "default", parent))
        self.assertIsNone(registry.lookup("zcode", "mac", "default", child))

    def test_a_letter_that_comes_at_the_turn_end_enrolls_the_children_too(self) -> None:
        parent, child = sess(74), sess(75)
        self.add_session(parent, title="Late letter")
        self.add_session(child, parent=parent, task="subagent_child", title="Child work")
        down = self.env()
        down.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL", None)
        t0 = time.time()
        self.run_hook(
            {"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"},
            env=down, now=t0,
        )
        self.assertIsNone(OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", child))
        code, _ = self.run_hook({"session_id": parent, "hook_event_name": "Stop"},
                                now=t0 + PL.RETRY_SECONDS + 1)
        self.assertEqual(code, 0)
        kid = OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", child)
        self.assertTrue(kid)
        self.assertTrue(self.note(child).get("label"))

    def test_a_lettered_child_is_not_replayed_every_turn(self) -> None:
        parent, child = sess(76), sess(77)
        self.add_session(parent, title="Steady work")
        self.add_session(child, parent=parent, task="subagent_child", title="Child work")
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        self.run_hook({"session_id": parent, "hook_event_name": "Stop"}, now=time.time() + 1)
        before = self.note(child)
        self.assertTrue(before.get("label"))
        self.run_hook({"session_id": parent, "hook_event_name": "Stop"}, now=time.time() + 61)
        self.assertEqual(self.note(child), before)  # one note read, no replay, no new write

    def test_a_refused_child_is_logged_and_not_retried_in_a_storm(self) -> None:
        parent = sess(78)
        self.add_session(parent, title="Row fill")
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        for i in range(25):  # the parent's row holds exactly 25 child pairs
            sid = sess(200 + i)
            self.add_session(sid, parent=parent, task="subagent_child",
                             title="Child %02d" % i, age=7200 + i)
            self.run_hook({"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"})
        extra = sess(300)
        self.add_session(extra, parent=parent, task="subagent_child", title="Overflow")
        t0 = time.time()
        code, _ = self.run_hook({"session_id": parent, "hook_event_name": "Stop"}, now=t0)
        self.assertEqual(code, 0)  # the refusal never fails the parent's chat
        note = self.note(extra)
        self.assertEqual(note.get("state"), "failed")
        # The assigner's row-full refusal reaches the client as the shared exhausted-refusal text
        # (the parent's row is full, which the client words as the pairs being live).
        self.assertIn("main-chat pairs", str(note.get("error") or ""))
        self.assertIn(PL.REFUSED_TAIL, str(note.get("error") or ""))
        self.assertIsNone(OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", extra))
        self.run_hook({"session_id": parent, "hook_event_name": "Stop"}, now=t0 + 60)
        self.assertEqual(self.note(extra), note)  # inside the retry window:  not asked again

    def test_the_pass_bounds_itself_per_turn_end(self) -> None:
        parent = sess(79)
        children = [sess(310 + i) for i in range(9)]
        self.add_session(parent, title="Many children")
        for i, sid in enumerate(children):
            self.add_session(sid, parent=parent, task="subagent_child",
                             title="Child %02d" % i, age=7200 + i)
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        self.run_hook({"session_id": parent, "hook_event_name": "Stop"}, now=time.time() + 1)
        first = [sid for sid, label in self.child_labels(children).items() if label]
        self.assertEqual(len(first), PL.CHILD_ENROLL_MAX)
        self.run_hook({"session_id": parent, "hook_event_name": "Stop"}, now=time.time() + 61)
        self.assertEqual(len([sid for sid, label in self.child_labels(children).items() if label]),
                         len(children))

    def test_grandchildren_wait_for_their_own_parents_pass(self) -> None:
        parent, child, grand = sess(80), sess(81), sess(82)
        self.add_session(parent, title="Top work")
        self.add_session(child, parent=parent, task="subagent_child", title="Child work")
        self.add_session(grand, parent=child, task="subagent_child", title="Grandchild work")
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        self.run_hook({"session_id": parent, "hook_event_name": "Stop"}, now=time.time() + 1)
        registry = OR.SessionRegistry(self.db)
        self.assertTrue(registry.lookup("zcode", "mac", "default", child))
        self.assertIsNone(registry.lookup("zcode", "mac", "default", grand))

    def test_an_unreadable_child_note_does_not_stop_the_pass(self) -> None:
        parent, corrupt, healthy = sess(100), sess(101), sess(102)
        self.add_session(parent, title="Two children")
        self.add_session(corrupt, parent=parent, task="subagent_child",
                         title="Corrupt note", age=7200)
        self.add_session(healthy, parent=parent, task="subagent_child",
                         title="Healthy child", age=7100)
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        with open(os.path.join(self.state, "ZC", "letters", corrupt + ".json"), "w") as fh:
            fh.write("{ not json")  # a note no reader can parse:  one child's problem
        code, _ = self.run_hook({"session_id": parent, "hook_event_name": "Stop"})
        self.assertEqual(code, 0)  # the corrupt note neither fails the hook nor starves the sibling
        registry = OR.SessionRegistry(self.db)
        # A note no reader can parse holds nothing, so the child is treated as one that never had
        # a note:  its start is replayed, it takes a letter, and a fresh note replaces the corrupt
        # file.  The failure stays inside that one child's own try, never the pass or the chat.
        self.assertTrue(registry.lookup("zcode", "mac", "default", corrupt))
        self.assertTrue(registry.lookup("zcode", "mac", "default", healthy))
        self.assertTrue(self.note(corrupt).get("label"))

    def test_a_crafted_child_id_is_left_unread(self) -> None:
        parent, child = sess(103), sess(104)
        self.add_session(parent, title="Traversal guard")
        self.add_session("../escape", parent=parent, task="subagent_child",
                         title="Crafted id", age=100)
        self.add_session(child, parent=parent, task="subagent_child", title="Real child", age=101)
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        # The store row's id is not a session id, so the listing never builds a note path from it,
        # and the parent's own Stop pass enrolls only the child it can name safely.
        self.assertEqual(Z.zcode_child_sessions(self.env(), parent), [child])
        code, _ = self.run_hook({"session_id": parent, "hook_event_name": "Stop"})
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "ZC", "letters"))),
                         sorted([parent + ".json", child + ".json"]))
        self.assertTrue(OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", child))

    def test_a_store_of_lettered_children_costs_bounded_reads(self) -> None:
        parent, late = sess(105), sess(106)
        self.add_session(parent, title="Cap work")
        letters = os.path.join(self.state, "ZC", "letters")
        os.makedirs(letters, exist_ok=True)
        for i in range(120):  # newest first:  every lettered row sits ahead of the old unlettered one
            sid = sess(400 + i)
            self.add_session(sid, parent=parent, task="subagent_child", title="L %02d" % i,
                             age=100 + i)
            with open(os.path.join(letters, sid + ".json"), "w") as fh:
                json.dump({"state": "open", "label": "ZZ:"}, fh)
        self.add_session(late, parent=parent, task="subagent_child", title="Late child", age=9000)
        reads = {"n": 0}
        real = Z._note_dict

        def counting(env, sid):
            reads["n"] += 1
            return real(env, sid)

        with mock.patch.object(Z, "_note_dict", counting):
            listed = Z.zcode_child_sessions(self.env(), parent)
        # The examination cap stops the walk at 4 * CHILD_SCAN rows:  the reads are bounded even
        # when the whole table is lettered, and the old unlettered row waits for the repair pass.
        self.assertEqual(listed, [])
        self.assertLessEqual(reads["n"], 4 * Z.CHILD_SCAN)

    def test_a_string_failed_at_child_still_enrolls(self) -> None:
        parent, child = sess(109), sess(110)
        self.add_session(parent, title="String stamp on the enrollment pass")
        self.add_session(child, parent=parent, task="subagent_child", title="Child work")
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        # A retry stamp that is not a number reads as no refusal on the enrollment pass too:
        # the malformed stamp neither crashes the parent's Stop hook nor blocks this child
        # from taking its letter with its siblings.
        with open(os.path.join(self.state, "ZC", "letters", child + ".json"), "w") as fh:
            json.dump({"state": "open-nothing", "failed_at": "not-a-number"}, fh)
        code, _ = self.run_hook({"session_id": parent, "hook_event_name": "Stop"})
        self.assertEqual(code, 0)
        self.assertTrue(self.note(child).get("label"))
        self.assertTrue(OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", child))

    def test_a_string_failed_at_never_aborts_a_scan(self) -> None:
        parent, child = sess(107), sess(108)
        self.add_session(parent, title="String stamp")
        self.add_session(child, parent=parent, task="subagent_child", title="Child work", age=7200)
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        self.run_hook({"session_id": parent, "hook_event_name": "Stop"})
        self.assertTrue(self.note(child).get("label"))  # the pass enrolled it once
        work = self.note(child)
        work.update({"state": "failed", "failed_at": "not-a-number", "error": "row full"})
        work.pop("label", None)
        with open(os.path.join(self.state, "ZC", "letters", child + ".json"), "w") as fh:
            json.dump(work, fh)
        # A retry stamp that is not a number reads as no refusal at all (the shared _seconds helper),
        # so neither scan raises and the child is offered again instead of being stuck forever.
        self.assertIn(child, Z.zcode_unlettered_sessions(self.env()) or [])
        self.assertEqual(Z.zcode_child_sessions(self.env(), parent), [child])


class RepairChildren(Base):
    """The repair pass enrolls a sub-agent child whose parent's note holds a letter, and leaves a child of a
    parent nobody ever lettered alone:  its replay would register the long-gone parent first."""

    def test_a_child_of_a_lettered_parent_is_enrolled_by_the_pass(self) -> None:
        parent, child, orphan = sess(90), sess(91), sess(92)
        gone = sess(93)
        self.add_session(parent, title="Repaired parent")
        self.add_session(child, parent=parent, task="subagent_child", title="Repaired child")
        self.add_session(orphan, parent=gone, task="subagent_child", title="Orphan child")
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        done = PL.repair_unlettered(Z.SPEC, self.env(), "ZC", "mac", now=time.time(), force=True)
        self.assertIn(child, done["asked"], done)
        self.assertNotIn(orphan, done["asked"], done)
        registry = OR.SessionRegistry(self.db)
        mom = registry.lookup("zcode", "mac", "default", parent)
        kid = registry.lookup("zcode", "mac", "default", child)
        self.assertTrue(kid)
        self.assertEqual(kid["label"][0], mom["label"][0])
        self.assertIsNone(registry.lookup("zcode", "mac", "default", orphan))

    def test_a_letterless_parent_and_an_ancient_child_are_left_alone(self) -> None:
        parent, present, ancient = sess(94), sess(95), sess(96)
        quiet = sess(97)
        self.add_session(parent, title="Quiet work")  # in the store, but no letter of its own
        self.add_session(present, parent=parent, task="subagent_child", title="Recent child")
        self.add_session(quiet, title="??: Quiet work")  # an unlettered chat, for the same pass
        self.run_hook({"session_id": sess(98), "hook_event_name": "SessionStart", "source": "startup"})
        self.add_session(ancient, parent=sess(98), task="subagent_child",
                         title="Ancient child", age=30 * 86400)
        done = PL.repair_unlettered(Z.SPEC, self.env(), "ZC", "mac", now=time.time(), force=True)
        self.assertNotIn(present, done["asked"], done)  # its parent holds no letter
        self.assertNotIn(ancient, done["asked"], done)  # older than the pass's own window
        self.assertIn(quiet, done["asked"], done)  # the "??" chat of the same pass still repairs
        registry = OR.SessionRegistry(self.db)
        self.assertIsNone(registry.lookup("zcode", "mac", "default", present))
        self.assertIsNone(registry.lookup("zcode", "mac", "default", ancient))

    def test_a_throttled_child_takes_no_repair_slot(self) -> None:
        parent, refused, quiet = sess(103), sess(104), sess(105)
        self.add_session(parent, title="Throttled parent")
        self.add_session(refused, parent=parent, task="subagent_child", title="Refused child")
        self.add_session(quiet, title="??: Quiet work")  # a healthy candidate for the same slot
        self.run_hook({"session_id": parent, "hook_event_name": "SessionStart", "source": "startup"})
        with open(os.path.join(self.state, "ZC", "letters", refused + ".json"), "w") as fh:
            json.dump({"state": "failed", "failed_at": time.time() - 60}, fh)
        done = PL.repair_unlettered(Z.SPEC, self.env(), "ZC", "mac", now=time.time(), force=True)
        self.assertNotIn(refused, done["asked"], done)  # inside its retry window:  no replay slot
        self.assertIn(quiet, done["asked"], done)  # the slot goes to the healthy candidate
        self.assertIsNone(OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", refused))


class ServerLocation(Base):
    FAKE = False  # its own environment, or a real listener on loopback, answers here
    def test_server_location_without_url_or_remote_is_a_quiet_retry(self) -> None:
        sid = sess(40)
        self.add_session(sid)
        code, out = self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
            location="server",
            env=self.env(),
        )
        self.assertEqual(code, 0)
        self.assertIn("No Operator letter yet", out)
        registry = OR.SessionRegistry(self.db)
        self.assertIsNone(registry.lookup("zcode", "server", "default", sid))


class ApiTransport(Base):
    FAKE = False  # its own environment, or a real listener on loopback, answers here
    """A hook installed --location server on a host off the registry, with the registry API
    URL in its environment:  the hook calls the API itself and the record lands attributed
    to the location it was installed under."""

    def setUp(self) -> None:
        super().setUp()
        import secrets

        self.operator_key = secrets.token_hex(24)
        self.service = RG.RegistryService(
            self.db, {"OPERATOR": self.operator_key}
        )
        self.server = API.ListenerReadApi(
            "127.0.0.1", 0, self.state, {}, registry=self.service
        )
        self.server.start()
        self.addCleanup(self.server.close)
        host, port = self.server.address
        self.api_env = self.env(
            SIMPLE_TOOLS_OPERATOR_REGISTRY_URL="http://%s:%d" % (host, port),
            SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL="OPERATOR",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY=self.operator_key,
        )

    def test_server_hook_registers_through_the_api(self) -> None:
        sid = sess(50)
        self.add_session(sid, title="Wake me over the API")
        code, out = self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
            location="server",
            env=self.api_env,
        )
        self.assertEqual(code, 0)
        record = OR.SessionRegistry(self.db).lookup("zcode", "server", "default", sid)
        self.assertTrue(record, out)
        self.assertRegex(record["label"], PAIR)

    def test_api_unreachable_is_the_quiet_retry(self) -> None:
        import secrets

        sid = sess(51)
        self.add_session(sid)
        dead = self.api_env | {
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL": "http://127.0.0.1:1",
        }
        code, out = self.run_hook(
            {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
            location="server",
            env=dead,
        )
        self.assertEqual(code, 0)
        self.assertIn("No Operator letter yet", out)


class TitleWriter(Base):
    def test_hook_records_thread_link_with_parent_registry_context_after_child_scrub(self) -> None:
        sid = sess(27)
        self.add_session(sid, title="Words")
        env = self.env(SESSION_LETTER_LOCATION="server",
                       SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN="fake.bootstrap",
                       INFISICAL_TOKEN="fake.project", INFISICAL_AI_FLEET_COORDINATOR_TOKEN="fake.afc",
                       FLEET_RECALL_INFISICAL_TOKEN="fake.recall")
        original = dict(env)
        from dataclasses import replace

        seen = []
        original_rename = Z.SPEC.title_writer

        def capture(request):
            seen.append(request)
            return original_rename(request)

        spec = replace(Z.SPEC, title_writer=capture)
        code = PL.hook(spec, env,
                       io.StringIO(json.dumps({"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"})),
                       io.StringIO(), location_flag="server")
        self.assertEqual(code, 0)
        self.assertTrue(seen)
        request = seen[-1]
        self.assertEqual(request.env["HOME"], self.home)
        for name in PL._OPERATOR_CHILD_SECRETS:
            self.assertNotIn(name, request.env)
        self.assertTrue(callable(request.record_thread_link))
        self.assertEqual(env, original)
        with sqlite3.connect(self.db) as connection:
            row = connection.execute("SELECT thread_link FROM sessions WHERE native_session_id = ?", (sid,)).fetchone()
        self.assertEqual(row[0], "zcode://session/%s" % sid)

    def test_rename_writes_generated_title(self) -> None:
        sid = sess(20)
        self.add_session(sid, title="Words the prompt gave")
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sid,
            label="AA",
            title="AA: Words the prompt gave",
            words="Words the prompt gave",
            current="Words the prompt gave",
            reason="start",
            data={"_now": 1700000000},
            env=self.env(),
        )
        self.assertTrue(Z.zcode_rename(request))
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[sid], ("AA: Words the prompt gave", "custom"))

    def test_a_titled_chat_records_its_thread_link(self) -> None:
        self.assigner.uninstall()  # a real listener on loopback answers this one
        import secrets

        from simple_tools import listener_api as API
        from simple_tools import listener_registry as RG

        operator_key = secrets.token_hex(24)
        service = RG.RegistryService(self.db, {"OPERATOR": operator_key})
        server = API.ListenerReadApi("127.0.0.1", 0, self.state, {}, registry=service)
        server.start()
        self.addCleanup(server.close)
        host, port = server.address
        OR.register_session(
            self.db,
            platform="zcode",
            verified_host="server",
            profile_scope="default",
            native_session_id=sess(26),
            seat="ZC",
            source="thread link test",
            issuer=OR.ISSUER_OPERATOR,
        )
        self.add_session(sess(26), title="Words")
        env = self.env(
            SESSION_LETTER_LOCATION="server",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_URL="http://%s:%d" % (host, port),
            SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL="OPERATOR",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY=operator_key,
        )
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sess(26),
            label="AA",
            title="AA: Words",
            words="Words",
            current="Words",
            reason="start",
            data={},
            env=env,
        )
        self.assertTrue(Z.zcode_rename(request))
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT thread_link FROM sessions WHERE native_session_id = ?", (sess(26),)
        ).fetchone()
        conn.close()
        self.assertEqual(row["thread_link"], "zcode://session/%s" % sess(26))

    def test_the_write_is_custom_so_the_app_keeps_its_hands_off(self) -> None:
        # The app model-retitles a `generated` thread and wipes the letter prefix (seen
        # live); a `custom` title is a user rename it leaves alone.  The hook's lettered
        # title must therefore be written AS custom.
        sid = sess(23)
        self.add_session(sid, title="Words", title_source="first_input")
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sid,
            label="AA",
            title="AA: Words",
            words="Words",
            current="Words",
            reason="start",
            data={},
            env=self.env(),
        )
        self.assertTrue(Z.zcode_rename(request))
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[sid], ("AA: Words", "custom"))

    def test_a_hook_written_custom_yields_to_a_later_letter(self) -> None:
        # The hook writes custom titles now; without the note check, its own earlier write
        # would look like a person's rename and block a reissued letter forever (Kody).
        sid = sess(24)
        self.add_session(sid, title="AA: Words", title_source="custom")
        note = {
            "label": "AA",
            "title": "Words",
            "state": "open",
        }
        os.makedirs(os.path.join(self.state, "ZC", "letters"), exist_ok=True)
        with open(os.path.join(self.state, "ZC", "letters", sid + ".json"), "w") as fh:
            json.dump(note, fh)
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sid,
            label="BB",
            title="BB: Words",
            words="Words",
            current="AA: Words",
            reason="rename",
            data={},
            env=self.env(),
            shown="AA: Words",
            held=("BB", "AA"),
        )
        self.assertTrue(Z.zcode_rename(request))
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[sid], ("BB: Words", "custom"))

    def test_a_persons_custom_still_blocks_without_note_proof(self) -> None:
        sid = sess(25)
        self.add_session(sid, title="Jay named this", title_source="custom")
        # no note:  nothing says the hook wrote it
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sid,
            label="AA",
            title="AA: Jay named this",
            words="Jay named this",
            current="Jay named this",
            reason="start",
            data={},
            env=self.env(),
        )
        self.assertTrue(Z.zcode_rename(request))
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[sid], ("Jay named this", "custom"))

    def test_rename_never_writes_over_a_user_rename(self) -> None:
        sid = sess(21)
        self.add_session(sid, title="Jay renamed this", title_source="custom")
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sid,
            label="AA",
            title="AA: Jay renamed this",
            words="Jay renamed this",
            current="Jay renamed this",
            reason="start",
            data={},
            env=self.env(),
        )
        self.assertTrue(Z.zcode_rename(request))
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[sid], ("Jay renamed this", "custom"))

    def test_rename_writes_over_custom_when_the_letter_is_already_there(self) -> None:
        sid = sess(22)
        self.add_session(sid, title="AA: Jay kept the letter", title_source="custom")
        request = PL.TitleRequest(
            platform=Z.PLATFORM,
            session_id=sid,
            label="AA",
            title="AA: Jay kept the letter",
            words="Jay kept the letter",
            current="AA: Jay kept the letter",
            reason="rename",
            data={},
            env=self.env(),
        )
        self.assertTrue(Z.zcode_rename(request))
        rows = dict((r[0], (r[1], r[2])) for r in self.rows())
        self.assertEqual(rows[sid], ("AA: Jay kept the letter", "custom"))


class Install(Base):
    def setUp(self) -> None:
        super().setUp()
        self.config = os.path.join(self.home, ".zcode", "cli", "config.json")
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "hooks": {
                        "events": {
                            "PreToolUse": [
                                {
                                    "matcher": "Bash",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "/opt/lane-tools/lane-guard-hook --format claude",
                                            "timeout": 5,
                                        }
                                    ],
                                }
                            ]
                        },
                        "enabled": True,
                    }
                },
                fh,
            )

    def install(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        code = PL.install_main(["--platform", "zcode", *argv], self.env(), out)
        return code, out.getvalue()

    def test_install_adds_entries_that_run_the_shared_command(self) -> None:
        code, out = self.install("--location", "server")
        self.assertEqual(code, 0)
        answer = json.loads(out.splitlines()[-1])
        self.assertEqual(answer["changed"], ["SessionStart", "UserPromptSubmit", "Stop"])
        doc = json.load(open(self.config))
        events = doc["hooks"]["events"]
        self.assertTrue(doc["hooks"]["enabled"])  # the user's flag survives
        names = []
        for entry in events["SessionStart"]:
            for hook in entry["hooks"]:
                words = shlex.split(hook["command"])
                self.assertEqual(words[0], os.path.join(self.home, ".local/bin/platform-letter"))
                self.assertEqual(words[1:], ["hook", "--platform", "zcode", "--location", "server"])
                names.append(hook["command"])
        self.assertEqual(len(names), 1)
        # the other tool's hook keeps its matcher and its command
        self.assertEqual(events["PreToolUse"][0]["matcher"], "Bash")
        self.assertIn("lane-guard-hook", events["PreToolUse"][0]["hooks"][0]["command"])

    def test_install_replaces_an_entry_that_ran_the_retired_wrapper(self) -> None:
        old = os.path.join(self.home, ".local/bin/zc-letter/platform-letter") + " hook --platform zcode --location server"
        doc = json.load(open(self.config))
        doc["hooks"]["events"]["SessionStart"] = [{"hooks": [{"type": "command", "command": old, "timeout": 20}]}]
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.install()
        events = json.load(open(self.config))["hooks"]["events"]
        commands = [h["command"] for e in events["SessionStart"] for h in e["hooks"]]
        self.assertEqual(len(commands), 1)
        self.assertEqual(shlex.split(commands[0])[0], os.path.join(self.home, ".local/bin/platform-letter"))
        self.assertIn("--location", commands[0])  # the location the old entry carried is kept

    def test_install_is_idempotent_and_check_passes(self) -> None:
        self.install("--location", "server")
        code, out = self.install("--location", "server")
        answer = json.loads(out.splitlines()[-1])
        self.assertEqual(answer["changed"], [])
        code, out = self.install("--location", "server", "--check")
        self.assertEqual(code, 0)

    def test_remove_takes_only_ours(self) -> None:
        self.install("--location", "server")
        code, out = self.install("--remove")
        self.assertEqual(code, 0)
        doc = json.load(open(self.config))
        events = doc["hooks"]["events"]
        self.assertNotIn("SessionStart", events)
        self.assertNotIn("UserPromptSubmit", events)
        self.assertNotIn("Stop", events)
        self.assertIn("PreToolUse", events)


class NoLetterTitles(Base):
    """The "??" title on ZCode, through the store writer (PR #1838 review):  the hook's own "??: Words" is replaced by
    the letter when it comes, and a lettered chat keeps its letter when one call fails."""

    def down(self) -> dict[str, str]:
        env = self.env()
        env.pop("SIMPLE_TOOLS_OPERATOR_REGISTRY_URL", None)
        return env

    def title(self, sid: str) -> tuple[str, str]:
        return dict((r[0], (r[1], r[2])) for r in self.rows())[sid]

    def test_an_unlettered_chat_takes_its_letter_over_the_mark(self) -> None:
        sid, t0 = sess(60), time.time()
        self.add_session(sid)
        self.run_hook({"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
                      env=self.down(), now=t0)
        self.assertEqual(self.title(sid), ("??: New Chat", "custom"))
        self.run_hook({"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": "fix the login bug"},
                      now=t0 + PL.RETRY_SECONDS + 1)
        label = OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", sid)["label"]
        self.assertEqual(self.title(sid), ("%s: Fix the Login Bug" % label, "custom"))

    def test_a_turn_end_asks_again_and_letters_the_chat(self) -> None:
        sid, t0 = sess(61), time.time()
        self.add_session(sid, title="Mutex semantics")
        self.run_hook({"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"},
                      env=self.down(), now=t0)
        self.assertEqual(self.title(sid), ("??: Mutex semantics", "custom"))
        self.run_hook({"session_id": sid, "hook_event_name": "Stop"}, now=t0 + PL.RETRY_SECONDS + 1)
        label = OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", sid)["label"]
        self.assertEqual(self.title(sid), ("%s: Mutex semantics" % label, "custom"))

    def test_a_lettered_chat_keeps_its_letter_through_one_failed_resume(self) -> None:
        sid, t0 = sess(62), time.time()
        self.add_session(sid, title="Fix the login bug")
        self.run_hook({"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"}, now=t0)
        label = OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", sid)["label"]
        self.assertEqual(self.title(sid)[0], "%s: Fix the login bug" % label)
        _, out = self.run_hook({"session_id": sid, "hook_event_name": "SessionStart", "source": "resume"},
                               env=self.down(), now=t0 + 60)
        self.assertEqual(self.title(sid)[0], "%s: Fix the login bug" % label)
        self.assertIn("Operator session letter: ZCode %s" % label, out)
        self.run_hook({"session_id": sid, "hook_event_name": "UserPromptSubmit", "prompt": "more"}, now=t0 + 120)
        self.assertEqual(self.title(sid)[0], "%s: Fix the login bug" % label)

    def test_a_turn_end_puts_the_letter_back_on_a_title_the_app_regenerated(self) -> None:
        sid, t0 = sess(63), time.time()
        self.add_session(sid, title="Fix the login bug")
        self.run_hook({"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"}, now=t0)
        label = OR.SessionRegistry(self.db).lookup("zcode", "mac", "default", sid)["label"]
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute("UPDATE session SET title = 'Merge Draft PRs', title_source = 'generated' WHERE id = ?",
                         (sid,))
        conn.close()
        self.run_hook({"session_id": sid, "hook_event_name": "Stop"}, now=t0 + 60)
        self.assertEqual(self.title(sid), ("%s: Merge Draft PRs" % label, "custom"))

    def test_a_persons_name_is_never_overwritten(self) -> None:
        sid = sess(64)
        self.add_session(sid, title="Jay named this", title_source="custom")
        request = PL.TitleRequest(platform=Z.PLATFORM, session_id=sid, label="CC", title="CC: Jay named this",
                                  words="Jay named this", current="Jay named this", reason="prompt", data={},
                                  env=self.env(), shown="??: New Chat", held=("CC",))
        self.assertTrue(Z.zcode_rename(request))
        self.assertEqual(self.title(sid), ("Jay named this", "custom"))


class ServerShapedHome(Base):
    """A server's home:  the managed `platform-letter` wrapper (the one `fleet-cli-git-update` writes), a `zc-letter`
    wrapper from before, and its own scoped bootstrap.  The current wrapper reads that protected store
    and never sources the superseded registry env script."""

    FAKE = False

    def test_after_the_install_the_hook_goes_through_the_managed_wrapper_and_registers(self) -> None:
        import importlib.util
        import subprocess
        from pathlib import Path

        # The managed wrapper runs the system interpreter by its fixed path, isolated:  it must be one this checkout runs on.
        try:
            probe = subprocess.run(
                ["/usr/bin/python3", "-c", "import sys; print(sys.version_info >= (3, 11))"],
                capture_output=True, text=True, timeout=30,
            )
            usable = probe.stdout.strip() == "True"
        except OSError:
            usable = False
        if not usable:
            self.skipTest("the managed wrapper's fixed interpreter /usr/bin/python3 is missing or older than 3.11 here")
        scripts = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location("_rel", scripts / "fleet-cli-release.py")
        rel = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rel)
        home = Path(self.home)
        bin_dir = home / ".local" / "bin"
        (bin_dir / "zc-letter").mkdir(parents=True)
        managed = bin_dir / "platform-letter"
        managed.write_bytes(rel.wrapper(home, "scripts/platform-letter"))
        managed.chmod(0o755)
        # The release the wrapper runs:  this checkout.
        (home / ".local" / "share" / "fleet-cli").mkdir(parents=True)
        (home / ".local" / "share" / "fleet-cli" / "current").symlink_to(scripts.parent)
        # The host's own narrow bootstrap reaches the registry served on loopback.
        self.assigner.install()
        self.addCleanup(self.assigner.uninstall)
        url = self.assigner.serve(os.path.join(self.tmp.name, "listener-state"))
        bootstrap = self.assigner.bootstrap_env(url, home=self.home)
        (bin_dir / "operator-registry-env").write_text(
            "echo obsolete-registry-helper-sourced >&2\nexit 92\n"
        )
        # An entry an earlier install pointed at the zc-letter wrapper:  install replaces it in place.
        old = "%s hook --platform zcode --location server" % (bin_dir / "zc-letter" / "platform-letter")
        config = home / ".zcode" / "cli" / "config.json"
        config.write_text(
            json.dumps({"hooks": {"events": {"SessionStart": [{"hooks": [{"type": "command", "command": old}]}]}}})
        )
        out = io.StringIO()
        code = PL.install_main(["--platform", "zcode", "--location", "server"], {"HOME": self.home}, out)
        self.assertEqual(code, 0, out.getvalue())
        written = json.loads(config.read_text())["hooks"]["events"]["SessionStart"]
        commands = [h["command"] for group in written for h in group["hooks"]]
        self.assertEqual(commands, ["%s hook --platform zcode --location server" % managed])
        # No profile, signing-key env or old helper is required; the real bootstrap reads its store.
        sid = sess(31)
        self.add_session(sid, title="Fix the wake path")
        done = subprocess.run(
            shlex.split(commands[0]),
            input=json.dumps({"session_id": sid, "hook_event_name": "SessionStart", "source": "startup"}),
            capture_output=True,
            text=True,
            timeout=120,
            env={"HOME": self.home, "PATH": "/usr/bin:/bin", "SIMPLE_TOOLS_STATE_DIR": self.state, **bootstrap},
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        record = OR.SessionRegistry(self.db).lookup("zcode", "server", "default", sid)
        self.assertTrue(record)
        self.assertRegex(record["label"], PAIR)
