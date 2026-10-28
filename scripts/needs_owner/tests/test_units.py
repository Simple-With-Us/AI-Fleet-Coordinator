"""The pure parts:  text cleaning, the uid and topic, links, the block, the record formats and the refusals."""
from __future__ import annotations

import os
import unittest

from needs_owner import block as B
from needs_owner import f3
from needs_owner import items as I
from needs_owner import links, records, safety
from needs_owner import seat as S
from needs_owner import text as T
from needs_owner.errors import Refusal

from .linear_md import linear_markdown as rewrite


def sample(**kw) -> B.Block:
    base = dict(uid=T.make_uid("CLAUDE", "Revive PR 761 or close it?", ["Revive it", "Close it"]),
                question="Revive PR 761 or close it?", options=["Revive it", "Close it"], low_stakes=[1], recommend=2,
                parent=None, filed_by="CLAUDE", context="PR 761 is stale")
    base.update(kw)
    return B.Block(**base)


class TextTests(unittest.TestCase):
    def test_clean_text(self) -> None:
        self.assertEqual(T.clean_text("  a b\n\tc​  ", "q", 50), "a b c")
        self.assertEqual(T.clean_text("use `x` now", "q", 50), "use 'x' now")
        self.assertEqual(T.clean_text("", "q", 50, required=False), "")
        with self.assertRaises(Refusal):
            T.clean_text(" ​ ", "q", 50)
        with self.assertRaises(Refusal) as ctx:
            T.clean_text("secret words " * 10, "the question", 20)
        self.assertNotIn("secret words", str(ctx.exception))

    def test_normalize_and_hash(self) -> None:
        self.assertEqual(T.normalize("  Revive IT \n"), "revive it")
        a = T.uid_hash("Q", ["a", "b"])
        self.assertEqual(a, T.uid_hash("q", ["A", "B"]))
        self.assertNotEqual(a, T.uid_hash("Q", ["a", "b"], version=2))
        self.assertNotEqual(a, T.uid_hash("Q", ["a", "b"], mirror_msg=5))
        self.assertNotEqual(T.uid_hash("Q", ["a", "b"], mirror_msg=5), T.uid_hash("Q", ["a", "b"], mirror_msg=6))
        self.assertNotEqual(a, T.uid_hash("Q", ["ab"]))

    def test_topic_is_inert_and_short(self) -> None:
        topic = T.topic_for("AFC-282", "@**all** [x](https://y.test) " + "long " * 30)
        self.assertTrue(topic.startswith("OWNER AFC-282 "))
        self.assertLessEqual(len(topic), 60)
        self.assertNotIn("@", topic)
        self.assertNotIn("://", topic)
        self.assertLessEqual(len(T.topic_for("ABCDEFGH-123456789", "x" * 100)), 60)

    def test_keycaps_follow_the_position(self) -> None:
        self.assertEqual([T.option_emoji(n) for n in (1, 2, 3, 4, 5)], ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"])

    def test_index_lists(self) -> None:
        self.assertEqual(T.parse_index_list(["1", "3,2", " 3 "], 3, "--low-stakes"), [1, 2, 3])
        for bad in (["0"], ["4"], ["x"], ["-1"]):
            with self.assertRaises(Refusal):
                T.parse_index_list(bad, 3, "--low-stakes")

    def test_idents(self) -> None:
        self.assertEqual(T.parse_ident(" afc-12 ", "--parent"), "AFC-12")
        with self.assertRaises(Refusal):
            T.parse_ident("12", "--parent")


class LinkTests(unittest.TestCase):
    OK = "https://simplewithus.zulipchat.com/#narrow/channel/12-work-in-progress/topic/OWNER.20AFC-9.20Key.2E.20Now.3F/near/777"

    def test_a_good_link(self) -> None:
        link = links.parse_topic_link(self.OK, {})
        self.assertEqual((link.channel, link.topic, link.message_id), ("work-in-progress", "OWNER AFC-9 Key. Now?", 777))

    def test_the_older_stream_form_and_the_realm_override(self) -> None:
        old = self.OK.replace("channel", "stream")
        self.assertEqual(links.parse_topic_link(old, {}).message_id, 777)
        other = self.OK.replace("simplewithus.zulipchat.com", "chat.example.test")
        with self.assertRaises(Refusal):
            links.parse_topic_link(other, {})
        self.assertEqual(links.parse_topic_link(other, {"AGENT_SYNC_REALM": "https://chat.example.test"}).message_id, 777)

    def test_bad_links(self) -> None:
        # the slug comes from the one canonical channel, so a rename cannot leave a stale no-op replace here
        other_channel = self.OK.replace("%d-%s" % (12, links.CHANNEL), "12-general")
        for bad in ("", "nonsense", self.OK.replace("https", "http"), self.OK.replace("/near/777", ""), self.OK.replace("OWNER.20", "X.20"),
                    other_channel, self.OK + "/more"):
            with self.assertRaises(Refusal, msg=bad):
                links.parse_topic_link(bad, {})


class BlockTests(unittest.TestCase):
    def test_round_trip_with_every_field(self) -> None:
        mirror = "https://simplewithus.zulipchat.com/#narrow/channel/12-work-in-progress/topic/OWNER.20x/near/9"
        b = sample(version=3, again_of="AFC-12 msg 456", mirror_of=mirror, parent="AFC-5", low_stakes=[1, 2])
        text = B.render_block(b)
        self.assertEqual(B.parse_block("intro\n\n" + text + "\n\noutro"), b)
        self.assertTrue(text.startswith("```needs-owner\n") and text.endswith("\n```"))

    def test_the_description_has_no_default_action(self) -> None:
        description = B.render_description(sample())
        self.assertIn("Nothing is decided or done until Jay answers.", description)
        self.assertNotIn("default", description.lower())
        self.assertEqual(B.parse_block(description), sample())

    def test_the_block_survives_linears_rewrite(self) -> None:
        b = sample(question="Close [old] PRs - all ~ of them?", options=["- one [x]", "two ~"])
        text = "[CLAUDE·abc] Filed with the board CLI.\n\n" + B.render_description(b)
        self.assertEqual(B.parse_block(rewrite(text)).question, b.question)
        self.assertEqual(B.parse_block(rewrite(text)).options, b.options)

    def test_the_fence_is_found_by_its_content(self) -> None:
        good = B.render_block(sample())
        bare = good.replace("```needs-owner", "```", 1)
        self.assertEqual(B.parse_block(bare), sample())
        self.assertEqual(B.parse_block(good.replace("```needs-owner", "```text", 1)), sample())
        other_first = "```python\nprint(1)\n```\n\nsome words\n\n" + bare
        self.assertEqual(B.parse_block(other_first), sample())
        self.assertEqual(B.parse_block(bare.replace("\n", "\r\n")), sample())
        for not_ours in ("```\nQuestion: x\n```", "```\nFormat: other/1\nQuestion: x\n```", good[:-4]):
            with self.assertRaises(B.BlockError):
                B.parse_block(not_ours)

    def test_strict_parse(self) -> None:
        good = B.render_block(sample())
        broken = {
            "no fence": "Question: x",
            "unknown field": good.replace("Context:", "Mood: happy\nContext:"),
            "repeated field": good.replace("Parent: none", "Parent: none\nParent: AFC-1"),
            "missing question": good.replace("Question: Revive PR 761 or close it?\n", ""),
            "option gap": good.replace("Option 2:", "Option 3:"),
            "empty option": good.replace("Option 2: Close it", "Option 2: "),
            "bad uid": good.replace("needs-owner:CLAUDE:", "needs-owner::"),
            "bad seat": good.replace("Filed by: CLAUDE", "Filed by: claude bot"),
            "bad recommendation": good.replace("option 2", "option 7"),
            "bad parent": good.replace("Parent: none", "Parent: later"),
            "bad version": good.replace("Context:", "Version: one\nContext:"),
            "bad format": good.replace("needs-owner/1", "needs-owner/9"),
            "a line with no field": good.replace("Context:", "just words\nContext:"),
            "six options": good.replace("Option 2: Close it", "\n".join("Option %d: x%d" % (n, n) for n in range(2, 7))),
        }
        for name, text in broken.items():
            with self.assertRaises(B.BlockError, msg=name):
                B.parse_block(text)

    def test_same_request_and_edited(self) -> None:
        a = sample()
        self.assertTrue(a.same_request(sample(question="revive pr 761 OR close it?", options=["REVIVE IT", "close  it"])))
        for change in ({"low_stakes": []}, {"recommend": 1}, {"context": None}, {"parent": "AFC-1"}, {"options": ["Revive it", "Park it"]}):
            self.assertFalse(a.same_request(sample(**change)), change)
        self.assertTrue(a.hash_matches_uid())
        self.assertFalse(sample(options=["Revive it", "Park it"]).hash_matches_uid())
        self.assertFalse(sample(version=2).hash_matches_uid())
        self.assertEqual(a.scope, "CLAUDE")
        self.assertEqual(sample(uid=T.make_uid("AFC-5", "Revive PR 761 or close it?", ["Revive it", "Close it"]), parent="AFC-5").scope, "AFC-5")

    def test_for_seat(self) -> None:
        self.assertEqual(sample(context="for GB-PLUMBER").for_seat, "GB-PLUMBER")
        self.assertIsNone(sample(context="background for GB-PLUMBER").for_seat)
        self.assertIsNone(sample(context=None).for_seat)


class RecordTests(unittest.TestCase):
    def test_notified(self) -> None:
        self.assertEqual(records.parse_notified(records.notified_text(630742433, "OWNER AFC-1 x")), (630742433, "OWNER AFC-1 x"))
        self.assertEqual(records.parse_notified("[needs-owner] notified msg 55"), (55, None))
        self.assertEqual(records.parse_notified("[needs-owner] notified  msg 55\ntopic:  OWNER AFC-1 y"), (55, "OWNER AFC-1 y"))
        for bad in ("notified msg 5", "[needs-owner] notified msg x", "[needs-owner] notify_pending", "random"):
            self.assertIsNone(records.parse_notified(bad))

    def test_pending(self) -> None:
        self.assertEqual(records.parse_pending(records.pending_text("no_credential")), "no_credential")
        self.assertEqual(records.parse_pending("[needs-owner] notify_pending"), "unsent")
        self.assertIsNone(records.parse_pending("[needs-owner] notified msg 5"))

    def test_replies(self) -> None:
        cases = {
            "owner replied:  msg 123": {"kind": "msg", "id": "123", "option": None},
            "owner replied: msg 123": {"kind": "msg", "id": "123", "option": None},
            "Owner Replied:  reaction 2 on msg 55": {"kind": "reaction", "id": "55", "option": 2},
            "owner replied:  reaction option 3 on msg 55": {"kind": "reaction", "id": "55", "option": 3},
            "owner replied:  comment 9f3a-77": {"kind": "comment", "id": "9f3a-77", "option": None},
            "[needs-owner] owner replied:  msg 1\nand then some words": {"kind": "msg", "id": "1", "option": None},
        }
        for text, want in cases.items():
            self.assertEqual(records.parse_reply(text), want, text)
        for bad in ("owner replied: yes please", "someone replied: msg 5", "owner replied:  reaction 0 on msg 5", ""):
            self.assertIsNone(records.parse_reply(bad), bad)

    def test_block_comment(self) -> None:
        self.assertTrue(records.is_block_comment(records.block_comment_text(B.render_block(sample()))))
        self.assertFalse(records.is_block_comment("see [needs-owner] block"))


class SafetyTests(unittest.TestCase):
    def test_secrets_and_personal_data(self) -> None:
        for text in ("lin_api_" + "A1b2C3d4E5" * 3, "sntrys_" + "abcDEF1234" * 3, "xai-" + "abcDEF1234" * 3, "st." + "abcdefgh.ijklmnop.qrstuvwx",
                     "AKIA" + "ABCDEFGHIJKLMNOP", "ghp_" + "a1B2c3D4e5" * 4):
            with self.assertRaises(Refusal, msg=text):
                safety.check_text("the question", text)
        for text in ("mail a@b.co", "(512) 555-0123", "+1 512 555 0123", "id 1234567", "costs $5", "costs $ 5"):
            with self.assertRaises(Refusal, msg=text):
                safety.check_text("the question", text)
        for text in ("Revive PR 761 or close it?", "thesis tags 0822ffe6", "see AFC-282 and the 236a2fa7e2ab4d24be03b0414750e0d8 issue",
                     "version 1.2.3 on 2026-10-11", "five $ signs? no digits after", "ports 8791 and 8792"):
            safety.check_text("the question", text)

    def test_the_refusal_names_the_field_and_not_the_text(self) -> None:
        with self.assertRaises(Refusal) as ctx:
            safety.check_text("option 2", "call 512-555-0123")
        self.assertIn("option 2", str(ctx.exception))
        self.assertNotIn("0123", str(ctx.exception))


class F3Tests(unittest.TestCase):
    def test_helpers_load_by_path(self) -> None:
        self.assertEqual(f3.identity.canonical_seat("grok-build"), "GROK")
        self.assertEqual(f3.textsafe.render_untrusted("@x", 10, code_span=False), "＠x")
        self.assertIsNone(f3.secretscan.scan("plain words"))

    def test_the_package_never_imports_agent_sync(self) -> None:
        import subprocess
        import sys

        from .world import SCRIPTS

        code = ("import sys; sys.path.insert(0, %r); import needs_owner.cli, needs_owner.verbs; "
                "bad = [m for m in sys.modules if m == 'agent_sync' or m.startswith('agent_sync.')]; "
                "sys.exit(1 if bad else 0)" % str(SCRIPTS))
        self.assertEqual(subprocess.run([sys.executable, "-I", "-c", code]).returncode, 0)


class ItemTests(unittest.TestCase):
    def show(self, **kw) -> dict:
        base = {"id": "u", "identifier": "AFC-1", "url": "https://x/AFC-1", "title": "OWNER DECISION:  Q?", "status": "open", "severity": "P2",
                "app": "AFC", "created_at": "2026-10-11T08:00:00.000Z", "labels": ["Kind/agent-report", "needs-owner"],
                "source_kind": "agent-report", "reported_by": "CLAUDE", "description": B.render_description(sample()), "comments": []}
        base.update(kw)
        return base

    def test_state_order(self) -> None:
        item = I.build_item(self.show())
        self.assertEqual((item.state, item.seat, item.seat_verified), ("notify_pending", "CLAUDE", True))
        notified = I.build_item(self.show(comments=[{"author": "CLAUDE", "text": "[needs-owner] notified msg 5\ntopic: OWNER AFC-1 Q"}]))
        self.assertEqual((notified.state, notified.topic), ("notified", "OWNER AFC-1 Q"))
        replied = I.build_item(self.show(comments=[{"author": "CLAUDE", "text": "[needs-owner] notified msg 5"},
                                                   {"author": "CLAUDE", "text": "owner replied:  msg 9"}]))
        self.assertEqual(replied.state, "replied")
        for status in I.CLOSED_STATUSES:
            self.assertEqual(I.build_item(self.show(status=status)).state, "closed")
        self.assertEqual(I.build_item(self.show(description="plain text")).state, "unadopted")

    def test_an_impostor_reporter_is_not_verified(self) -> None:
        self.assertFalse(I.build_item(self.show(reported_by="CODEX")).seat_verified)
        self.assertFalse(I.build_item(self.show(reported_by=None)).seat_verified)

    def test_a_block_comment_of_another_author_is_not_verified(self) -> None:
        text = records.block_comment_text(B.render_block(sample()))
        mine = I.build_item(self.show(description="by hand", comments=[{"author": "CLAUDE", "text": text}]))
        theirs = I.build_item(self.show(description="by hand", comments=[{"author": "CODEX", "text": text}]))
        self.assertTrue(mine.seat_verified)
        self.assertFalse(theirs.seat_verified)

    def test_age(self) -> None:
        import datetime as dt

        at = dt.datetime(2026, 10, 11, 20, 30, tzinfo=dt.timezone.utc)
        self.assertEqual(I.age_hours("2026-10-11T08:00:00.000Z", at), 12.5)
        self.assertIsNone(I.age_hours(None, at))
        self.assertIsNone(I.age_hours("not a time", at))


class BoardWriteOptInTests(unittest.TestCase):
    def test_board_calls_export_no_write_opt_in(self):
        import os
        import stat
        import tempfile
        from needs_owner.proc import Runner

        with tempfile.TemporaryDirectory() as d:
            tool = os.path.join(d, "simple-board")
            with open(tool, "w") as fh:
                fh.write('#!/bin/sh\nprintf "%s" "$LINEAR_BOARD_WRITES"\n')
            os.chmod(tool, stat.S_IRWXU)
            from needs_owner import seat as seatmod

            self.addCleanup(setattr, seatmod, "MANAGED_BIN_DIR", seatmod.MANAGED_BIN_DIR)
            seatmod.MANAGED_BIN_DIR = d  # the import-only seam:  the managed board is this folder's
            runner = Runner(None, {"PATH": d, "HOME": d})
            result = runner.board("comment", "AFC-1", "x")
            self.assertEqual(result.out, "")
            self.assertNotIn("LINEAR_BOARD_WRITES", runner.env)


class TrustedBinaryTests(unittest.TestCase):
    """A forged `bearer-verified` line cannot come from a binary or folder others can write."""

    def make(self, mode_dir: int, mode_file: int) -> str:
        import tempfile
        root = tempfile.mkdtemp(prefix="nobin-")
        self.addCleanup(__import__("shutil").rmtree, root, True)
        os.chmod(root, mode_dir)
        path = os.path.join(root, "simple-board")
        with open(path, "w") as fh:
            fh.write('#!/bin/sh\necho "seat:        CLAUDE (bearer-verified)"\n')
        os.chmod(path, mode_file)
        return root

    def test_an_owner_only_binary_in_an_owner_only_folder_is_used(self) -> None:
        root = self.make(0o755, 0o755)
        self.assertEqual(S.trusted_binary("simple-board", root), os.path.realpath(os.path.join(root, "simple-board")))

    def test_a_world_writable_folder_or_file_is_refused(self) -> None:
        for mode_dir, mode_file in ((0o777, 0o755), (0o755, 0o775), (0o755, 0o757)):
            root = self.make(mode_dir, mode_file)
            self.assertIsNone(S.trusted_binary("simple-board", root), (oct(mode_dir), oct(mode_file)))

    def test_a_hostile_first_path_entry_is_not_skipped_for_a_later_safe_one(self) -> None:
        bad, good = self.make(0o777, 0o755), self.make(0o755, 0o755)
        self.assertIsNone(S.trusted_binary("simple-board", bad + os.pathsep + good))

    def test_the_real_probe_refuses_a_forged_line_from_an_unsafe_folder(self) -> None:
        bad = self.make(0o777, 0o755)
        self.addCleanup(setattr, S, "MANAGED_BIN_DIR", S.MANAGED_BIN_DIR)
        S.MANAGED_BIN_DIR = bad
        env = {"PATH": bad, S.LOCATOR_ENV: "/fleet-seats:CLAUDE_SEAT_BEARER"}
        with self.assertRaises(Refusal):
            S.verified_seat("CLAUDE", env)

    def test_a_safe_binary_on_the_callers_path_or_home_is_never_the_managed_board(self) -> None:
        planted = self.make(0o755, 0o755)  # user-owned and tidy:  only the caller's PATH and HOME point at it
        self.addCleanup(setattr, S, "MANAGED_BIN_DIR", S.MANAGED_BIN_DIR)
        S.MANAGED_BIN_DIR = None
        env = {"PATH": planted, "HOME": os.path.dirname(planted), S.LOCATOR_ENV: "/fleet-seats:CLAUDE_SEAT_BEARER"}
        from needs_owner import proc

        self.assertNotEqual(proc.find_tool("simple-board", env), os.path.join(planted, "simple-board"))
        self.assertNotEqual(S.managed_board(), os.path.join(planted, "simple-board"))

    def test_a_writable_ancestor_is_refused_even_when_the_parent_and_file_are_tight(self) -> None:
        import tempfile
        grand = tempfile.mkdtemp(prefix="nobin-grand-")
        self.addCleanup(__import__("shutil").rmtree, grand, True)
        parent = os.path.join(grand, "managed")
        os.mkdir(parent)
        os.chmod(parent, 0o755)
        binary = os.path.join(parent, "simple-board")
        with open(binary, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(binary, 0o755)
        self.assertEqual(S.trusted_binary("simple-board", parent), os.path.realpath(binary))
        os.chmod(grand, 0o777)  # another user could now rename `managed` away and put their own in its place
        self.assertIsNone(S.trusted_binary("simple-board", parent))

    def test_a_symlink_chain_is_checked_at_every_hop(self) -> None:
        import tempfile
        good = self.make(0o755, 0o755)  # tight folder and binary
        loose = tempfile.mkdtemp(prefix="nobin-loose-")
        self.addCleanup(__import__("shutil").rmtree, loose, True)
        os.chmod(loose, 0o777)
        # a link kept in a writable folder can be swapped by anyone, even though it points at a tight binary
        os.symlink(os.path.join(good, "simple-board"), os.path.join(loose, "simple-board"))
        self.assertIsNone(S.trusted_binary("simple-board", loose))
        # a link in a tight folder that points into a writable one is refused through its target's folder
        tight = tempfile.mkdtemp(prefix="nobin-tight-")
        self.addCleanup(__import__("shutil").rmtree, tight, True)
        target = os.path.join(loose, "real")
        with open(target, "w") as fh:
            fh.write("#!/bin/sh\n")
        os.chmod(target, 0o755)
        os.symlink(target, os.path.join(tight, "simple-board"))
        self.assertIsNone(S.trusted_binary("simple-board", tight))
        # the ordinary managed shape:  a tight link folder pointing into a tight runtime folder is accepted
        link = tempfile.mkdtemp(prefix="nobin-link-")
        self.addCleanup(__import__("shutil").rmtree, link, True)
        os.symlink(os.path.join(good, "simple-board"), os.path.join(link, "simple-board"))
        self.assertEqual(S.trusted_binary("simple-board", link), os.path.realpath(os.path.join(good, "simple-board")))

    def test_a_middle_symlink_hop_in_a_writable_folder_is_refused(self) -> None:
        import tempfile
        safe = self.make(0o755, 0o755)  # the final binary, tight
        loose = tempfile.mkdtemp(prefix="nobin-mid-")
        self.addCleanup(__import__("shutil").rmtree, loose, True)
        os.chmod(loose, 0o777)
        first = tempfile.mkdtemp(prefix="nobin-first-")  # tight folder holding the entry link
        self.addCleanup(__import__("shutil").rmtree, first, True)
        os.symlink(os.path.join(safe, "simple-board"), os.path.join(loose, "hop"))  # hop 2 lives in a writable folder
        os.symlink(os.path.join(loose, "hop"), os.path.join(first, "simple-board"))  # entry -> hop -> final
        self.assertIsNone(S.trusted_binary("simple-board", first))
        # relative targets and a symlinked folder component are walked too
        rel = tempfile.mkdtemp(prefix="nobin-rel-")
        self.addCleanup(__import__("shutil").rmtree, rel, True)
        os.symlink(loose, os.path.join(rel, "dir"))
        os.symlink(os.path.join("dir", "hop"), os.path.join(rel, "simple-board"))
        self.assertIsNone(S.trusted_binary("simple-board", rel))
        # a two-hop chain wholly inside tight folders is accepted
        t1 = tempfile.mkdtemp(prefix="nobin-t1-")
        self.addCleanup(__import__("shutil").rmtree, t1, True)
        t2 = tempfile.mkdtemp(prefix="nobin-t2-")
        self.addCleanup(__import__("shutil").rmtree, t2, True)
        os.symlink(os.path.join(safe, "simple-board"), os.path.join(t2, "hop"))
        os.symlink(os.path.join(t2, "hop"), os.path.join(t1, "simple-board"))
        self.assertEqual(S.trusted_binary("simple-board", t1), os.path.realpath(os.path.join(safe, "simple-board")))

    def test_a_relative_path_entry_is_never_used(self) -> None:
        self.assertIsNone(S.trusted_binary("simple-board", "bin:."))


class VerifiedSeatTests(unittest.TestCase):
    """The seat is proven by the board CLI's bearer verification, never by AGENT_SEAT or a launcher variable."""

    LOCATOR = {"SIMPLE_BOARD_BEARER_LOCATOR": "/fleet-seats:CLAUDE_SEAT_BEARER"}

    def probe(self, out, code=0):
        calls = []

        def run(env):
            calls.append(dict(env))
            return code, out
        run.calls = calls
        return run

    def test_seat_comes_from_the_verified_whoami_line(self) -> None:
        from needs_owner import seat as S
        run = self.probe("seat:        CLAUDE (bearer-verified)\nidentity:    Claude\n")
        self.assertEqual(S.verified_seat("claude", dict(self.LOCATOR, AGENT_SEAT="CODEX"), probe=run), "CLAUDE")

    def test_no_locator_refuses_even_with_agent_seat(self) -> None:
        from needs_owner import seat as S
        run = self.probe("seat:        CLAUDE (bearer-verified)\n")
        with self.assertRaises(Refusal):
            S.verified_seat("CLAUDE", {"AGENT_SEAT": "CLAUDE", "AGENT_LAUNCH_SEAT": "CLAUDE", "AGENT_LAUNCHER": "x"}, probe=run)
        self.assertEqual(run.calls, [])

    def test_unverified_or_failed_probe_refuses(self) -> None:
        from needs_owner import seat as S
        for out, code in (("seat:        (no bearer: reads run without an identity)\n", 0),
                          ("seat:        CLAUDE (AGENT_SEAT)\n", 0),
                          ("seat:        CLAUDE (bearer-verified)\n", 6), ("", 0)):
            with self.assertRaises(Refusal, msg=out):
                S.verified_seat("CLAUDE", self.LOCATOR, probe=self.probe(out, code))

    def test_by_must_match_the_verified_seat(self) -> None:
        from needs_owner import seat as S
        with self.assertRaises(Refusal):
            S.verified_seat("CODEX", self.LOCATOR, probe=self.probe("seat:        CLAUDE (bearer-verified)\n"))

    def test_children_lose_every_identity_variable_and_keep_the_locator(self) -> None:
        from needs_owner import seat as S
        env = S.child_environment(dict(self.LOCATOR, AGENT_SEAT="CODEX", AGENT_LAUNCH_SEAT="CODEX", PATH="/bin"))
        self.assertNotIn("AGENT_SEAT", env)
        self.assertNotIn("AGENT_LAUNCH_SEAT", env)
        self.assertEqual(env["SIMPLE_BOARD_BEARER_LOCATOR"], self.LOCATOR["SIMPLE_BOARD_BEARER_LOCATOR"])


if __name__ == "__main__":
    unittest.main()
