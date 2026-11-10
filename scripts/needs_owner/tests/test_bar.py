"""The owner bar:  `--why-owner`, the `NEEDS_OWNER_BAR` mode and the `NEEDS_OWNER_SEAT_DAILY_MAX` cap on `needs-owner file`.

Every test runs `file` in process against the fake board and the fake simple-chat with fake K1 knobs, as the rest of this
folder does.  The default mode, `off`, is held to today's behaviour:  no new key in the JSON, no new line in the block, no
warning, no file written, and only the mode knob is read.  The real `fleet_knobs` reading both knobs is in
`test_knobs_adapter.py`."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from needs_owner import bar, verbs
from needs_owner.errors import Refusal

from .test_mirror import MirrorCase, request_text
from .world import REALM_LINK, World

Q = "Rotate the signing key?"
CLASSES = ("identity", "credential", "money", "legal", "irreversible", "policy")
DOC = Path(__file__).resolve().parents[3] / "docs" / "protocols" / "needs-owner.md"
HOME_FILINGS = ("apps", "needs-owner", "filings", "filings.json")


class BarCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.w = self.world()

    def tearDown(self) -> None:
        self.assertEqual(self.w.leftover_temp_files(), [], "a private temp file was left behind")

    def world(self, mode: str = "off", cap: int = 0) -> World:
        w = World(self)
        w.knobs.values["NEEDS_OWNER_BAR"] = mode
        w.knobs.values["NEEDS_OWNER_SEAT_DAILY_MAX"] = cap
        return w

    def file(
        self,
        question: str = Q,
        *,
        why: str | None = None,
        w: World | None = None,
        extra: tuple = (),
        seat: str = "CLAUDE",
        options: tuple = ("Yes", "No"),
    ):
        w = w or self.w
        flags = tuple(extra) + (("--why-owner", why) if why is not None else ())
        return w.file(question, *options, seat=seat, extra=flags, env=w.env(AGENT_SEAT=seat))

    def filed(self, w: World | None = None) -> list[dict]:
        return [c for c in (w or self.w).calls("simple-board") if c["argv"][0] == "file"]

    def block_text(self, w: World | None = None) -> str:
        return self.filed(w)[-1]["files"]["--desc-file"]["text"]

    def filings_file(self, w: World | None = None) -> Path:
        return (w or self.w).home.joinpath(*HOME_FILINGS)

    def refused_before_anything(self, w: World) -> None:
        """No issue, no comment, no label, no post:  the refusal came before the first write."""
        self.assertEqual(w.board_state()["issues"], {})
        self.assertEqual(
            [c for c in w.calls("simple-board") if c["argv"][0] in ("file", "comment", "label")], []
        )
        self.assertEqual(w.calls("simple-chat"), [])


# ---- off:  today's behaviour exactly ---------------------------------------------------------------


class OffTests(BarCase):
    def test_off_is_today_s_behaviour_exactly(self) -> None:
        r = self.file()
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(r.err, "", "no warning")
        got = r.json()
        self.assertNotIn("bar", got)
        self.assertNotIn("why_owner", got)
        self.assertNotIn("Why owner", self.block_text())
        self.assertFalse(self.filings_file().exists(), "nothing is counted in off")
        self.assertEqual(self.w.knobs.reads, ["NEEDS_OWNER_BAR"], "only the mode is read, and the cap is not")

    def test_off_neither_reads_nor_records_the_flag(self) -> None:
        for n, why in enumerate(("credential", "not-a-class", "")):
            r = self.file("Question number %d?" % n, why=why)
            self.assertEqual(r.code, 0, (why, r.err))
            self.assertEqual(r.err, "", why)
            self.assertNotIn("why_owner", r.json())
            self.assertNotIn("Why owner", self.block_text())

    def test_off_never_applies_the_cap(self) -> None:
        w = self.world("off", 1)
        for n in range(3):
            r = self.file("Question number %d?" % n, w=w)
            self.assertEqual(r.code, 0, r.err)
        self.assertFalse(self.filings_file(w).exists())
        self.assertEqual(w.knobs.reads, ["NEEDS_OWNER_BAR"] * 3)

    def test_a_dry_run_in_off_prints_the_old_plan(self) -> None:
        r = self.w.run(
            "file", "--by", "CLAUDE", "--app", "AFC", "--question", Q, "--option", "Yes", "--option", "No", "--dry-run", "--json"
        )
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(sorted(r.json()), ["dry_run", "existing", "options", "severity", "title", "uid"])

    def test_a_value_that_is_not_a_mode_is_off(self) -> None:
        for n, bad in enumerate(("bogus", "", None, "ENFORCE", " enforce", "warning", "on", 3, True, ["enforce"], {})):
            self.w.knobs.values["NEEDS_OWNER_BAR"] = bad
            r = self.file("Question number %d?" % n)
            self.assertEqual(r.code, 0, (bad, r.err))
            self.assertNotIn("why-owner", r.err, repr(bad))  # (the sixth open item prints the unrelated over-five warning)
            self.assertNotIn("bar", r.json(), repr(bad))
        self.assertFalse(self.filings_file().exists())

    def test_a_knob_that_cannot_be_read_is_off(self) -> None:
        self.w.knobs.fail = RuntimeError("no reader token")
        r = self.file()
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(r.err, "")
        self.assertNotIn("bar", r.json())

    def test_a_missing_cap_reads_as_no_cap_and_keeps_the_mode(self) -> None:
        w = self.world("enforce")
        del w.knobs.values["NEEDS_OWNER_SEAT_DAILY_MAX"]
        for n in range(2):
            self.assertEqual(self.file("Question number %d?" % n, why="money", w=w).code, 0)
        refused = self.file("No class here?", w=w)
        self.assertEqual(refused.code, 2, "the mode still holds when only the cap cannot be read")

    def test_a_cap_that_is_not_a_whole_number_above_zero_is_no_cap(self) -> None:
        for bad in (-1, 0, True, "3", None, 2.5):
            w = self.world("enforce", bad)
            for n in range(2):
                r = self.file("Question number %d?" % n, why="money", w=w)
                self.assertEqual(r.code, 0, (bad, r.err))


# ---- warn:  file it, record the class, say so ------------------------------------------------------


class WarnTests(BarCase):
    def setUp(self) -> None:
        self.w = self.world("warn")

    def test_a_missing_class_is_filed_and_warned_about_once(self) -> None:
        r = self.file()
        self.assertEqual(r.code, 0, r.err)
        (line,) = r.err.splitlines()
        self.assertIn("no --why-owner class was given", line)
        for word in ("Side-Chats", "Pending-for-Jay", *CLASSES):
            self.assertIn(word, line)
        got = r.json()
        self.assertEqual((got["bar"], got["why_owner"]), ("warn", None))
        self.assertNotIn("Why owner", self.block_text())
        self.assertEqual(len(self.w.board_state()["issues"]), 1, "it was filed")
        self.assertEqual(len(self.w.calls("simple-chat")), 1, "and announced")

    def test_every_class_is_recorded_and_read_back(self) -> None:
        for n, cls in enumerate(CLASSES):
            r = self.file("Question number %d?" % n, why=cls)
            self.assertEqual(r.code, 0, (cls, r.err))
            self.assertNotIn("why-owner", r.err, cls)  # (the sixth open item prints the unrelated over-five warning)
            got = r.json()
            self.assertEqual((got["bar"], got["why_owner"]), ("warn", cls))
            self.assertIn("\nWhy owner: %s\n" % cls, self.block_text())
            ident = got["identifier"]
            self.assertEqual(self.w.run("status", ident, "--json").json()["why_owner"], cls)
        listed = {r["identifier"]: r["why_owner"] for r in self.w.run("list", "--json").json()}
        self.assertEqual(sorted(listed.values()), sorted(CLASSES))

    def test_a_class_in_capitals_is_stored_in_lower_case(self) -> None:
        r = self.file(why=" Credential ")
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(r.json()["why_owner"], "credential")
        self.assertIn("\nWhy owner: credential\n", self.block_text())

    def test_an_unknown_class_is_filed_without_it_and_never_echoed(self) -> None:
        r = self.file(why="my-bogus-class")
        self.assertEqual(r.code, 0, r.err)
        (line,) = r.err.splitlines()
        self.assertIn("not one of the six classes", line)
        self.assertNotIn("my-bogus-class", r.err)
        self.assertIsNone(r.json()["why_owner"])
        self.assertNotIn("Why owner", self.block_text())

    def test_an_item_with_no_class_has_no_why_owner_key_in_the_listing(self) -> None:
        self.file()
        (listed,) = self.w.run("list", "--json").json()
        self.assertNotIn("why_owner", listed)

    def test_the_class_is_not_part_of_the_request_so_a_rerun_stays_idempotent(self) -> None:
        first = self.file(why="credential")
        again = self.file()  # the same question, no class this time
        self.assertEqual(again.code, 0, again.err)
        self.assertTrue(again.json()["already_filed"])
        self.assertEqual(again.json()["identifier"], first.json()["identifier"])
        other = self.file(why="money")
        self.assertEqual(other.code, 0, other.err)
        self.assertTrue(other.json()["already_filed"])
        self.assertEqual(len(self.w.board_state()["issues"]), 1)
        self.assertEqual(len(self.w.calls("simple-chat")), 1, "and nothing was posted twice")

    def test_a_dry_run_shows_the_bar_and_changes_nothing(self) -> None:
        r = self.file(why="legal", extra=("--dry-run",))
        self.assertEqual(r.code, 0, r.err)
        plan = r.json()
        self.assertEqual((plan["dry_run"], plan["bar"], plan["why_owner"]), (True, "warn", "legal"))
        self.assertEqual(self.w.board_state()["issues"], {})
        self.assertFalse(self.filings_file().exists())

    def test_the_cap_warns_and_the_item_is_still_filed(self) -> None:
        w = self.world("warn", 2)
        for n in range(2):
            r = self.file("Question number %d?" % n, why="money", w=w)
            self.assertEqual((r.code, r.err), (0, ""))
        third = self.file("Question number 2?", why="money", w=w)
        self.assertEqual(third.code, 0, third.err)
        (line,) = third.err.splitlines()
        self.assertIn("CLAUDE has filed 2 needs-owner items in the last 24 hours (the daily cap is 2)", line)
        self.assertIn("Pending-for-Jay", line)
        self.assertEqual(len(w.board_state()["issues"]), 3, "warn files it anyway")
        self.assertEqual(len(w.calls("simple-chat")), 3)


# ---- enforce:  refuse what is not a reason ---------------------------------------------------------


class EnforceTests(BarCase):
    def setUp(self) -> None:
        self.w = self.world("enforce")

    def test_a_missing_class_is_refused_before_anything_is_filed(self) -> None:
        r = self.file()
        self.assertEqual(r.code, 2)
        self.assertEqual(r.out, "")
        (line,) = r.err.splitlines()
        self.assertTrue(line.startswith("needs-owner: no --why-owner class was given"), line)
        for word in ("Side-Chats", "Pending-for-Jay", *CLASSES):
            self.assertIn(word, line)
        self.refused_before_anything(self.w)

    def test_a_blank_class_is_a_missing_one(self) -> None:
        r = self.file(why="   ")
        self.assertEqual(r.code, 2)
        self.assertIn("no --why-owner class was given", r.err)
        self.refused_before_anything(self.w)

    def test_an_unknown_class_is_refused_and_never_echoed(self) -> None:
        r = self.file(why="my-bogus-class")
        self.assertEqual(r.code, 2)
        self.assertIn("not one of the six classes", r.err)
        self.assertIn("Pending-for-Jay", r.err)
        self.assertNotIn("my-bogus-class", r.err)
        self.refused_before_anything(self.w)

    def test_a_good_class_is_filed_and_recorded(self) -> None:
        for n, cls in enumerate(CLASSES):
            r = self.file("Question number %d?" % n, why=cls)
            self.assertEqual(r.code, 0, (cls, r.err))
            self.assertNotIn("why-owner", r.err, cls)
            self.assertEqual(r.json()["why_owner"], cls)
            self.assertIn("\nWhy owner: %s\n" % cls, self.block_text())
        self.assertEqual(len(self.w.board_state()["issues"]), len(CLASSES))

    def test_urgent_and_again_obey_the_same_rule(self) -> None:
        self.assertEqual(self.file(why="money", extra=("--urgent",)).code, 0)
        refused = self.file(options=("Maybe", "No"), extra=("--again",))
        self.assertEqual(refused.code, 2)
        self.assertEqual(len(self.w.board_state()["issues"]), 1)
        again = self.file(options=("Maybe", "No"), extra=("--again",), why="money")
        self.assertEqual(again.code, 0, again.err)
        self.assertEqual(len(self.w.board_state()["issues"]), 2)

    def test_a_new_version_that_had_to_be_retried_keeps_its_class(self) -> None:
        """Both earlier versions are closed, so the board says `already filed` for the second version's uid and the block
        is built again for a third:  the class must come through that second build too."""
        w = self.world("enforce")
        first = self.file(why="money", w=w)
        w.close(first.json()["identifier"])
        second = self.file(why="money", w=w, extra=("--again",))
        w.close(second.json()["identifier"])
        third = self.file(why="legal", w=w, extra=("--again",))
        self.assertEqual(third.code, 0, third.err)
        text = self.block_text(w)
        self.assertIn("Version: 3", text)
        self.assertIn("\nWhy owner: legal\n", text)

    def test_a_dry_run_is_held_to_the_bar_too(self) -> None:
        refused = self.file(extra=("--dry-run",))
        self.assertEqual(refused.code, 2)
        ok = self.file(why="irreversible", extra=("--dry-run",))
        self.assertEqual(ok.code, 0, ok.err)
        self.assertEqual((ok.json()["bar"], ok.json()["why_owner"]), ("enforce", "irreversible"))
        self.refused_before_anything(self.w)

    def test_the_cap_refuses_the_next_item_and_names_where_else_to_go(self) -> None:
        w = self.world("enforce", 2)
        for n in range(2):
            self.assertEqual(self.file("Question number %d?" % n, why="policy", w=w).code, 0)
        third = self.file("Question number 2?", why="policy", w=w)
        self.assertEqual(third.code, 2)
        (line,) = third.err.splitlines()
        self.assertIn("CLAUDE has filed 2 needs-owner items in the last 24 hours (the daily cap is 2)", line)
        self.assertIn("Pending-for-Jay", line)
        self.assertEqual(len(w.board_state()["issues"]), 2)
        self.assertEqual(len(w.calls("simple-chat")), 2, "the refused one posted nothing")

    def test_no_cap_means_no_limit_even_in_enforce(self) -> None:
        w = self.world("enforce", 0)
        for n in range(7):
            self.assertEqual(self.file("Question number %d?" % n, why="policy", w=w).code, 0)


# ---- the cap, in detail ---------------------------------------------------------------------------


class CapTests(BarCase):
    def test_the_cap_is_per_seat(self) -> None:
        w = self.world("enforce", 1)
        self.assertEqual(self.file("Mine?", why="money", w=w).code, 0)
        self.assertEqual(self.file("Theirs?", why="money", w=w, seat="CODEX").code, 0)
        self.assertEqual(self.file("Mine again?", why="money", w=w).code, 2)
        self.assertEqual(self.file("Theirs again?", why="money", w=w, seat="CODEX").code, 2)

    def test_a_rerun_that_files_nothing_is_not_counted_or_refused(self) -> None:
        w = self.world("enforce", 1)
        first = self.file(why="money", w=w)
        self.assertEqual(first.code, 0, first.err)
        again = self.file(why="money", w=w)
        self.assertEqual(again.code, 0, "the cap is full and the rerun still finishes:  " + again.err)
        self.assertTrue(again.json()["already_filed"])
        rows = json.loads(self.filings_file(w).read_text(encoding="utf-8"))["rows"]
        self.assertEqual(len(rows), 1, "the rerun was not counted")
        self.assertEqual(self.file("A different question?", why="money", w=w).code, 2)

    def test_a_closed_item_still_counts(self) -> None:
        w = self.world("enforce", 1)
        first = self.file(why="money", w=w).json()
        w.close(first["identifier"])  # closing removes the label, so Linear alone would forget it
        self.assertEqual(w.run("list", "--all", "--json").json(), [])
        self.assertEqual(self.file("Another one?", why="money", w=w).code, 2)

    def test_the_window_is_twenty_four_hours(self) -> None:
        w = self.world("enforce", 1)
        self.assertEqual(self.file(why="money", w=w).code, 0)
        start = w.now
        w.now = start + dt.timedelta(hours=24) - dt.timedelta(seconds=1)
        self.assertEqual(self.file("Not yet?", why="money", w=w).code, 2)
        w.now = start + dt.timedelta(hours=24)
        later = self.file("Now?", why="money", w=w)
        self.assertEqual(later.code, 0, later.err)

    def test_an_again_is_a_new_filing_and_counts(self) -> None:
        w = self.world("enforce", 1)
        self.assertEqual(self.file(why="money", w=w).code, 0)
        r = self.file(why="money", w=w, options=("Maybe", "No"), extra=("--again",))
        self.assertEqual(r.code, 2, r.err)

    def test_a_dry_run_checks_the_cap_and_counts_nothing(self) -> None:
        w = self.world("enforce", 1)
        for _ in range(3):
            self.assertEqual(self.file(why="money", w=w, extra=("--dry-run",)).code, 0)
        self.assertFalse(self.filings_file(w).exists())
        self.assertEqual(self.file(why="money", w=w).code, 0)
        self.assertEqual(self.file("Other?", why="money", w=w, extra=("--dry-run",)).code, 2)

    def test_the_count_holds_only_the_seat_a_time_and_an_identifier(self) -> None:
        w = self.world("warn", 5)
        got = self.file("Do not store these words?", why="money", w=w).json()
        path = self.filings_file(w)
        text = path.read_text(encoding="utf-8")
        self.assertNotIn("words", text)
        self.assertNotIn("Yes", text)
        doc = json.loads(text)
        self.assertEqual(doc["schema"], 1)
        (row,) = doc["rows"]
        self.assertEqual(sorted(row), ["id", "seat", "t"])
        self.assertEqual((row["seat"], row["id"]), ("CLAUDE", got["identifier"]))
        self.assertEqual(row["t"], w.now.timestamp())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_count_that_cannot_be_written_never_blocks_a_filing(self) -> None:
        w = self.world("enforce", 5)
        self.filings_file(w).mkdir(parents=True)  # the ledger path is a folder:  it can be neither read nor replaced
        r = self.file(why="money", w=w)
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(r.err, "", "the lock was fine, so there is nothing to warn about")
        self.assertEqual(len(w.board_state()["issues"]), 1)


# ---- an item that already exists is not a new filing -------------------------------------------------


class ExistingItemTests(BarCase):
    """The bar meets only a NEW filing:  a rerun finds its item first and behaves exactly as in off."""

    PLAIN_PLAN = ["dry_run", "existing", "options", "severity", "title", "uid"]

    def test_an_existing_item_is_refiled_in_enforce_without_a_class(self) -> None:
        w = self.world("enforce", 1)
        first = self.file(why="money", w=w)
        self.assertEqual(first.code, 0, first.err)
        reads = list(w.knobs.reads)
        again = self.file(w=w)  # no class, and the cap is full
        self.assertEqual(again.code, 0, again.err)
        self.assertEqual(again.err, "", "no warning either")
        got = again.json()
        self.assertTrue(got["already_filed"])
        self.assertEqual(got["identifier"], first.json()["identifier"])
        self.assertNotIn("bar", got)
        self.assertNotIn("why_owner", got)
        self.assertEqual(w.knobs.reads, reads, "an existing item never reads the bar knobs, as in off")
        self.assertEqual(len(bar._rows(w.env())), 1, "and is not counted")
        self.assertEqual(len(w.board_state()["issues"]), 1)
        self.assertEqual(len(w.calls("simple-chat")), 1, "nothing was posted twice")

    def test_a_new_filing_without_a_class_still_refuses(self) -> None:
        w = self.world("enforce", 1)
        self.assertEqual(self.file(why="money", w=w).code, 0)
        self.assertEqual(self.file(w=w).code, 0, "the rerun")
        new = self.file("A new question?", w=w)
        self.assertEqual(new.code, 2)
        self.assertIn("no --why-owner class was given", new.err)
        self.assertEqual(len(w.board_state()["issues"]), 1)

    def test_an_item_filed_before_the_bar_is_not_counted_when_it_is_refiled(self) -> None:
        w = self.world("off")
        before = self.file(w=w)
        self.assertEqual(before.code, 0, before.err)
        w.knobs.values["NEEDS_OWNER_BAR"] = "enforce"
        w.knobs.values["NEEDS_OWNER_SEAT_DAILY_MAX"] = 1
        rerun = self.file(w=w)
        self.assertEqual(rerun.code, 0, rerun.err)
        self.assertTrue(rerun.json()["already_filed"])
        self.assertFalse(self.filings_file(w).exists(), "a rerun is never counted")
        self.assertEqual(self.file("Another question?", why="money", w=w).code, 0, "the one slot was still free")
        self.assertEqual(self.file("A third question?", why="money", w=w).code, 2)

    def test_warn_says_nothing_about_an_existing_item(self) -> None:
        w = self.world("warn", 1)
        self.assertEqual(self.file(why="money", w=w).code, 0)
        again = self.file(w=w)  # no class, and the cap is full
        self.assertEqual((again.code, again.err), (0, ""))

    def test_a_dry_run_of_an_existing_item_is_the_old_plan(self) -> None:
        w = self.world("enforce", 1)
        self.assertEqual(self.file(why="money", w=w).code, 0)
        r = self.file(w=w, extra=("--dry-run",))
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(sorted(r.json()), self.PLAIN_PLAN)
        self.assertEqual(r.json()["existing"], "AFC-300")

    def test_the_same_re_ask_run_twice_is_an_existing_item(self) -> None:
        w = self.world("enforce")
        first = self.file(why="money", w=w, options=("Maybe", "No"), extra=("--again",))
        self.assertEqual(first.code, 0, first.err)
        second = self.file(w=w, options=("Maybe", "No"), extra=("--again",))
        self.assertEqual(second.code, 0, second.err)
        self.assertTrue(second.json()["already_filed"])
        self.assertEqual(len(w.board_state()["issues"]), 1)


# ---- the lock ----------------------------------------------------------------------------------------------


class LockTests(BarCase):
    """The check, the Linear filing and the record are one critical section under an exclusive advisory lock."""

    def lock_file(self, w: World) -> Path:
        return w.home / "apps" / "needs-owner" / "filings" / "filings.lock"

    def can_take(self, w: World) -> bool:
        lock = bar.FilingsLock(w.env())
        with mock.patch.object(bar, "LOCK_WAIT_SECONDS", 0.2):
            reason = lock.acquire()
        lock.release()
        return reason is None

    def test_the_lock_file_is_private_and_beside_the_ledger(self) -> None:
        w = self.world("warn", 5)
        self.assertEqual(self.file(why="money", w=w).code, 0)
        lock = self.lock_file(w)
        self.assertTrue(lock.is_file())
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)
        self.assertEqual(lock.parent, self.filings_file(w).parent)
        self.assertEqual(lock.parent.stat().st_mode & 0o777, 0o700)

    def test_a_lock_file_made_earlier_with_a_wider_mode_is_brought_to_0600(self) -> None:
        w = self.world("warn", 5)
        lock = self.lock_file(w)
        lock.parent.mkdir(parents=True)
        lock.write_text("", encoding="utf-8")
        lock.chmod(0o666)
        self.assertEqual(self.file(why="money", w=w).code, 0)
        self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_the_lock_is_held_across_the_linear_filing_and_freed_after(self) -> None:
        w = self.world("enforce", 5)
        seen: dict = {}
        real = verbs.file_new

        def spy(*args, **kwargs):
            seen["free_during"] = self.can_take(w)
            return real(*args, **kwargs)

        with mock.patch.object(verbs, "file_new", spy):
            r = self.file(why="money", w=w)
        self.assertEqual(r.code, 0, r.err)
        self.assertIs(seen["free_during"], False, "another filing cannot slip in while this one is filed")
        self.assertTrue(self.can_take(w), "and it is freed once the filing is recorded")

    def test_a_failed_filing_records_nothing_and_frees_the_slot_and_the_lock(self) -> None:
        w = self.world("enforce", 1)
        failed = w.file(Q, "Yes", "No", extra=("--why-owner", "money"), env=w.env(FAKE_BOARD_FAIL="file"))
        self.assertEqual(failed.code, 1, failed.err)
        self.assertFalse(self.filings_file(w).exists(), "nothing was reserved")
        self.assertTrue(self.can_take(w))
        ok = self.file(why="money", w=w)
        self.assertEqual(ok.code, 0, "the one slot was not used up by the failure:  " + ok.err)
        refused = self.file("A second question?", why="money", w=w)
        self.assertEqual(refused.code, 2)
        self.assertTrue(self.can_take(w), "a refusal frees it too")

    def test_a_refusal_for_a_missing_class_never_takes_the_lock(self) -> None:
        w = self.world("enforce", 1)
        self.assertEqual(self.file(w=w).code, 2)
        self.assertFalse(self.lock_file(w).exists())

    def test_a_holder_on_this_host_is_waited_for(self) -> None:
        w = self.world("warn", 5)
        holder = bar.FilingsLock(w.env())
        self.assertIsNone(holder.acquire())
        timer = threading.Timer(0.6, holder.release)
        timer.start()
        started = time.monotonic()
        with mock.patch.object(bar, "LOCK_WAIT_SECONDS", 30.0):
            r = self.file(why="money", w=w)
        waited = time.monotonic() - started
        timer.join()
        self.assertEqual(r.code, 0, r.err)
        self.assertNotIn("filings lock", r.err)
        self.assertGreaterEqual(waited, 0.5, "the filing waited for the holder")

    def test_off_never_takes_the_lock(self) -> None:
        w = self.world("off", 1)
        holder = bar.FilingsLock(w.env())
        self.assertIsNone(holder.acquire())
        self.addCleanup(holder.release)
        with mock.patch.object(bar, "LOCK_WAIT_SECONDS", 0.2):
            r = self.file(w=w)
        self.assertEqual((r.code, r.err), (0, ""), "off does not even look at the lock")
        w2 = self.world("off", 1)
        self.assertEqual(self.file(w=w2).code, 0)
        self.assertFalse(self.lock_file(w2).exists())

    def broken(self, w: World, kind: str):
        """A context in which the lock cannot be taken, and the reason word `acquire` gives for it."""
        if kind == "timeout":
            holder = bar.FilingsLock(w.env())
            self.assertIsNone(holder.acquire())
            self.addCleanup(holder.release)
            patch = mock.patch.object(bar, "LOCK_WAIT_SECONDS", 0.2)
        elif kind == "cannot_open":
            patch = mock.patch.object(bar, "_lock_path", return_value="/proc/self/no-such-folder/filings.lock")
        else:
            patch = mock.patch.object(bar, "fcntl", None)
        return patch

    KINDS = ("timeout", "cannot_open", "no_flock")

    def test_a_lock_that_cannot_be_taken_fails_closed_in_enforce_with_a_cap(self) -> None:
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                w = self.world("enforce", 1)
                with self.broken(w, kind):
                    r = self.file(why="money", w=w)
                self.assertEqual(r.code, 2, r.err)
                (line,) = r.err.splitlines()
                self.assertIn("could not take the filings lock: %s" % kind, line)
                self.assertIn("nothing was filed", line)
                self.assertIn("Pending-for-Jay", line)
                self.refused_before_anything(w)

    def test_a_lock_that_cannot_be_taken_fails_open_in_warn(self) -> None:
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                w = self.world("warn", 1)
                with self.broken(w, kind):
                    r = self.file(why="money", w=w)
                self.assertEqual(r.code, 0, r.err)
                (line,) = r.err.splitlines()
                self.assertIn("could not take the filings lock (%s)" % kind, line)
                self.assertEqual(len(w.board_state()["issues"]), 1, "it was filed")

    def test_with_no_cap_there_is_nothing_to_protect_so_enforce_fails_open_too(self) -> None:
        for kind in self.KINDS:
            with self.subTest(kind=kind):
                w = self.world("enforce", 0)
                with self.broken(w, kind):
                    r = self.file(why="money", w=w)
                self.assertEqual(r.code, 0, r.err)
                self.assertIn("could not take the filings lock (%s)" % kind, r.err)

    def test_a_lock_failure_does_not_touch_an_existing_item(self) -> None:
        w = self.world("enforce", 1)
        self.assertEqual(self.file(why="money", w=w).code, 0)
        with self.broken(w, "cannot_open"):
            r = self.file(w=w)
        self.assertEqual((r.code, r.err), (0, ""))


class SlotTests(unittest.TestCase):
    """Two filings racing for the last slot of a cap:  exactly one wins, in threads and in separate processes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = {"HOME": tmp.name}
        self.policy = bar.Policy("enforce", 1)

    def attempt(self, results: dict, index: int, delay: float) -> None:
        try:
            with bar.Slot(self.policy, self.env, "CLAUDE", None, lambda line: None) as slot:
                slot.check()
                time.sleep(delay)  # the Linear filing
                slot.record("AFC-%d" % index)
            results[index] = "won"
        except Refusal:
            results[index] = "refused"

    def test_threads_racing_for_the_last_slot_have_one_winner(self) -> None:
        results: dict = {}
        start = threading.Barrier(6)

        def run(index: int) -> None:
            start.wait()
            self.attempt(results, index, 0.1)

        threads = [threading.Thread(target=run, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        self.assertEqual(sorted(results.values()), ["refused"] * 5 + ["won"])
        self.assertEqual(len(bar._rows(self.env)), 1)

    def test_processes_racing_for_the_last_slot_have_one_winner(self) -> None:
        scripts = str(Path(__file__).resolve().parents[2])
        code = (
            "import sys, time\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from needs_owner import bar\n"
            "from needs_owner.errors import Refusal\n"
            "try:\n"
            "    with bar.Slot(bar.Policy('enforce', 1), {'HOME': sys.argv[2]}, 'CLAUDE', None, lambda line: None) as slot:\n"
            "        slot.check()\n"
            "        time.sleep(0.8)\n"
            "        slot.record('AFC-' + sys.argv[3])\n"
            "    print('won')\n"
            "except Refusal:\n"
            "    print('refused')\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-I", "-c", code, scripts, self.env["HOME"], str(n)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for n in range(4)
        ]
        outputs = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, err)
            outputs.append(out.strip())
        self.assertEqual(sorted(outputs), ["refused"] * 3 + ["won"])
        self.assertEqual(len(bar._rows(self.env)), 1)

    def test_a_slot_that_fails_inside_records_nothing_and_frees_the_lock(self) -> None:
        with self.assertRaises(RuntimeError):
            with bar.Slot(self.policy, self.env, "CLAUDE", None, lambda line: None) as slot:
                slot.check()
                raise RuntimeError("the filing failed")
        self.assertEqual(bar._rows(self.env), [])
        results: dict = {}
        self.attempt(results, 1, 0)
        self.assertEqual(results, {1: "won"}, "the reservation did not survive the failure")

    def test_a_dead_holder_does_not_keep_the_lock(self) -> None:
        scripts = str(Path(__file__).resolve().parents[2])
        code = (
            "import os, sys\n"
            "sys.path.insert(0, sys.argv[1])\n"
            "from needs_owner import bar\n"
            "lock = bar.FilingsLock({'HOME': sys.argv[2]})\n"
            "assert lock.acquire() is None\n"
            "os._exit(0)\n"  # dies holding the lock
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", code, scripts, self.env["HOME"]], capture_output=True, text=True, timeout=60
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        results: dict = {}
        with mock.patch.object(bar, "LOCK_WAIT_SECONDS", 5.0):
            self.attempt(results, 1, 0)
        self.assertEqual(results, {1: "won"})

    def test_off_holds_nothing_and_records_nothing(self) -> None:
        with bar.Slot(bar.Policy(), self.env, "CLAUDE", None, lambda line: None) as slot:
            self.assertIsNone(slot.check())
            slot.record("AFC-1")
        self.assertEqual(bar._rows(self.env), [])
        self.assertFalse(Path(bar._lock_path(self.env)).exists())


# ---- what the bar leaves alone ---------------------------------------------------------------------


class ExemptTests(BarCase):
    def mirror(self, w: World, message_id: int):
        topic = "OWNER AFC-9 Plumber needs a key"
        w.set_zulip(topics_by_id={**w.zulip().get("topics_by_id", {}), str(message_id): topic})
        link = REALM_LINK % ("OWNER.20AFC-9.20Plumber.20needs.20a.20key", message_id)
        return w.run(
            "file", "--by", "CLAUDE", "--app", "AFC", "--question", "Plumber needs a key %d" % message_id,
            "--option", "Approve", "--option", "Decline", "--context", "for GB-PLUMBER", "--mirror-of", link, "--json",
        )  # fmt: skip

    def test_a_grok_bot_mirror_is_never_held_to_the_bar(self) -> None:
        w = self.world("enforce", 1)
        for message_id in (777, 888):
            r = self.mirror(w, message_id)
            self.assertEqual(r.code, 0, r.err)
            self.assertNotIn("--why-owner", r.err)
            self.assertNotIn("bar", r.json())
            self.assertNotIn("why_owner", r.json())
        self.assertEqual(len(w.board_state()["issues"]), 2, "the cap does not apply either")
        self.assertEqual(w.knobs.reads, [], "a mirror never even reads the bar knobs")
        self.assertFalse(self.filings_file(w).exists())

    def test_a_mirror_does_not_use_up_a_seat_s_cap(self) -> None:
        w = self.world("enforce", 1)
        self.assertEqual(self.mirror(w, 777).code, 0)
        self.assertEqual(self.file(why="money", w=w).code, 0)

    def test_adopt_is_not_gated(self) -> None:
        w = self.world("enforce", 1)
        ident = w.seed_issue("Tonight: rotate the key?")
        r = w.run("adopt", ident, "--by", "CLAUDE", "--option", "Yes", "--option", "No", "--json")
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(w.knobs.reads, [])


class NotifierUntouchedTests(MirrorCase):
    def test_the_fallback_notifier_still_mirrors_in_enforce_and_reads_only_its_own_knobs(self) -> None:
        self.w.knobs.values["NEEDS_OWNER_BAR"] = "enforce"
        self.w.knobs.values["NEEDS_OWNER_SEAT_DAILY_MAX"] = 1
        message = self.persona(content=request_text())
        result = self.run_notify()
        (mirrored,) = self.events(result, "mirrored")
        self.assertEqual(mirrored["msg"], message)
        (item,) = self.mirrored_items()
        self.assertEqual(item["for_seat"], "GB-PLUMBER")
        self.assertNotIn("NEEDS_OWNER_BAR", self.w.knobs.reads)
        self.assertNotIn("NEEDS_OWNER_SEAT_DAILY_MAX", self.w.knobs.reads)
        self.assertEqual(
            sorted(set(self.w.knobs.reads)), ["NEEDS_OWNER_CAPS", "NEEDS_OWNER_NOTIFY_PAUSED", "OWNER_LINEAR_USER_ID"]
        )


# ---- the pure parts ----------------------------------------------------------------------------------


class Reader:
    def __init__(self, **values) -> None:
        self.values = values

    def get(self, name: str):
        return self.values[name]


class PolicyTests(unittest.TestCase):
    def test_the_two_good_modes_and_the_cap(self) -> None:
        self.assertEqual(bar.read_policy(Reader(NEEDS_OWNER_BAR="warn", NEEDS_OWNER_SEAT_DAILY_MAX=4)), bar.Policy("warn", 4))
        self.assertEqual(bar.read_policy(Reader(NEEDS_OWNER_BAR="enforce", NEEDS_OWNER_SEAT_DAILY_MAX=0)), bar.Policy("enforce", 0))

    def test_anything_else_is_off_with_no_cap(self) -> None:
        for mode in ("off", "bogus", "", None, 1, True, "Warn"):
            got = bar.read_policy(Reader(NEEDS_OWNER_BAR=mode, NEEDS_OWNER_SEAT_DAILY_MAX=9))
            self.assertEqual(got, bar.Policy("off", 0), repr(mode))
            self.assertFalse(got.on)
        self.assertEqual(bar.read_policy(Reader()), bar.Policy())

    def test_a_cap_that_is_not_a_positive_whole_number_is_no_cap(self) -> None:
        for cap in (-3, 0, True, False, "5", None, 1.5):
            self.assertEqual(bar.read_policy(Reader(NEEDS_OWNER_BAR="warn", NEEDS_OWNER_SEAT_DAILY_MAX=cap)).cap, 0, repr(cap))

    def test_the_module_defaults_are_off(self) -> None:
        self.assertEqual(bar.Policy(), bar.Policy("off", 0))
        self.assertEqual(bar.MODES, ("off", "warn", "enforce"))


class ClassTests(unittest.TestCase):
    def test_off_looks_at_nothing(self) -> None:
        for given in (None, "", "credential", "nonsense"):
            self.assertEqual(bar.check_class("off", given), (None, None))
        self.assertEqual(bar.check_class("anything else", "credential"), (None, None))

    def test_warn_keeps_a_good_class_and_warns_about_the_rest(self) -> None:
        for cls in CLASSES:
            self.assertEqual(bar.check_class("warn", cls), (cls, None))
        stored, warning = bar.check_class("warn", None)
        self.assertIsNone(stored)
        self.assertIn("no --why-owner class was given", warning)
        stored, warning = bar.check_class("warn", "nonsense")
        self.assertIsNone(stored)
        self.assertIn("not one of the six classes", warning)

    def test_enforce_refuses_a_missing_or_unknown_class(self) -> None:
        for cls in CLASSES:
            self.assertEqual(bar.check_class("enforce", cls), (cls, None))
        for given in (None, "", "  ", "nonsense", "money credential"):
            with self.assertRaises(Refusal):
                bar.check_class("enforce", given)

    def test_there_are_six_classes_and_the_doc_and_the_registry_agree(self) -> None:
        self.assertEqual(tuple(bar.CLASSES), CLASSES)
        self.assertLessEqual(len(bar.CLASSES), 6)
        text = DOC.read_text(encoding="utf-8")
        for cls in bar.CLASSES:
            self.assertIn("| `%s` |" % cls, text, "the doc lists the class %s" % cls)
            self.assertRegex(cls, bar.CLASS_RE.pattern)
        for knob in (bar.MODE_KNOB, bar.CAP_KNOB, "Pending-for-Jay", "Side-Chats", "--why-owner"):
            self.assertIn(knob, text)
        import fleet_knobs

        mode = fleet_knobs.BY_NAME[bar.MODE_KNOB]
        self.assertEqual((mode.kind, mode.choices, mode.default, mode.local), ("enum", bar.MODES, "off", "deny"))
        cap = fleet_knobs.BY_NAME[bar.CAP_KNOB]
        self.assertEqual((cap.kind, cap.default, cap.minimum, cap.local), ("int", 0, 0, "deny"))
        for spec in (mode, cap):
            self.assertEqual(spec.consumers, (("the needs-owner filing command", "scripts/needs_owner/bar.py"),))


class FilingsTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.env = {"HOME": tmp.name}

    def test_the_window_edges(self) -> None:
        bar.record_filing(self.env, "CLAUDE", "AFC-1", 1000.0)
        self.assertEqual(bar.filings_in_window(self.env, "CLAUDE", 1000.0), 1)
        self.assertEqual(bar.filings_in_window(self.env, "CLAUDE", 1000.0 + 86399.0), 1)
        self.assertEqual(bar.filings_in_window(self.env, "CLAUDE", 1000.0 + 86400.0), 0)
        self.assertEqual(bar.filings_in_window(self.env, "CODEX", 1000.0), 0, "another seat's count")
        self.assertEqual(bar.filings_in_window(self.env, "CLAUDE", 999.0), 0, "a row from the future is not counted")

    def test_old_rows_go_and_the_file_stays_small(self) -> None:
        bar.record_filing(self.env, "CLAUDE", "AFC-1", 1000.0)
        bar.record_filing(self.env, "CLAUDE", "AFC-2", 1000.0 + bar.KEEP_SECONDS + 1)
        rows = bar._rows(self.env)
        self.assertEqual([r["id"] for r in rows], ["AFC-2"])
        for n in range(bar.MAX_ROWS + 20):
            bar.record_filing(self.env, "CLAUDE", "AFC-%d" % (n + 10), 5_000_000.0)
        self.assertEqual(len(bar._rows(self.env)), bar.MAX_ROWS)

    def test_a_damaged_file_counts_nothing_and_is_replaced(self) -> None:
        path = Path(bar._path(self.env))
        path.parent.mkdir(parents=True)
        for junk in ("not json", '{"schema": 9, "rows": []}', '{"schema": 1, "rows": "x"}', '[1, 2]', '{"schema": 1, "rows": [1, "a", null]}'):
            path.write_text(junk, encoding="utf-8")
            self.assertEqual(bar.filings_in_window(self.env, "CLAUDE", 1.0), 0, junk)
        bar.record_filing(self.env, "CLAUDE", "AFC-1", 10.0)
        self.assertEqual(bar.filings_in_window(self.env, "CLAUDE", 11.0), 1)

    def test_rows_with_odd_times_are_ignored(self) -> None:
        path = Path(bar._path(self.env))
        path.parent.mkdir(parents=True)
        rows = [{"seat": "CLAUDE", "t": "10"}, {"seat": "CLAUDE", "t": True}, {"seat": "CLAUDE", "t": None}, {"seat": "CLAUDE"}]
        path.write_text(json.dumps({"schema": 1, "rows": rows}), encoding="utf-8")
        self.assertEqual(bar.filings_in_window(self.env, "CLAUDE", 11.0), 0)

    def test_the_cap_check(self) -> None:
        warn, enforce = bar.Policy("warn", 2), bar.Policy("enforce", 2)
        for n in range(2):
            self.assertIsNone(bar.check_cap(enforce, self.env, "CLAUDE", 100.0))
            bar.record_filing(self.env, "CLAUDE", "AFC-%d" % n, 100.0)
        self.assertIn("daily cap is 2", bar.check_cap(warn, self.env, "CLAUDE", 101.0))
        with self.assertRaises(Refusal):
            bar.check_cap(enforce, self.env, "CLAUDE", 101.0)
        self.assertIsNone(bar.check_cap(bar.Policy("enforce", 0), self.env, "CLAUDE", 101.0), "0 is no cap")
        self.assertIsNone(bar.check_cap(bar.Policy("off", 2), self.env, "CLAUDE", 101.0), "off never applies it")
        self.assertIsNone(bar.check_cap(enforce, self.env, "CODEX", 101.0))

    def test_the_wall_clock(self) -> None:
        fixed = dt.datetime(2026, 11, 10, 12, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(bar.wall(lambda: fixed), fixed.timestamp())
        self.assertAlmostEqual(bar.wall(None), dt.datetime.now(dt.timezone.utc).timestamp(), delta=5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
