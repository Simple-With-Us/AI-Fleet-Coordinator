"""toml_hooks: finding, adopting and replacing a `[[hooks]]` entry of ours by its content, with the markers gone.

cd scripts && python3 -m unittest fleet_lanes.tests.test_toml_hooks -v
"""
from __future__ import annotations

import re
import sys
import unittest

from fleet_lanes import marked_block as MB
from fleet_lanes import toml_hooks as H

NAME = "hooks-fleet-guards"
CMD = "/h/apps/lane-tools/fleet-guard-hook --format kimi"
DESIRED = {"event": "PreToolUse", "matcher": "Bash", "command": CMD, "timeout": 5}
BODY = ["[[hooks]]", 'event = "PreToolUse"', 'matcher = "Bash"', 'command = "%s"' % CMD, "timeout = 5"]
OURS = re.compile(r"(?:^|[\s/'\"=])fleet-guard-hook(?=$|[\s'\"])")


def is_ours(text: object) -> bool:
    return isinstance(text, str) and bool(OURS.search(text))


OWNER = '''default_model = "k2"

[providers.kimi]
type = "kimi"

[[hooks]]
event = "Notification"
command = "terminal-notifier -message done"
'''
ENTRY = "\n".join(BODY) + "\n"


def plan(text, **kw):
    return H.plan(text, NAME, "test", BODY, DESIRED, is_ours)


def strip_comments(text: str) -> str:
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("#"))


class ScanTests(unittest.TestCase):
    def test_entries_are_read_by_header_and_end_at_the_next_table(self) -> None:
        text = OWNER + "\n[[hooks]]\nevent = \"PreToolUse\"\nmatcher = 'Bash'\ncommand = \"a b\"\ntimeout = 5   # seconds\n\n[later]\nx = 1\n"
        entries = H.scan_hooks(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[1].fields, {"event": "PreToolUse", "matcher": "Bash", "command": "a b", "timeout": 5})
        self.assertFalse(entries[1].complex)
        lines = text.split("\n")
        self.assertEqual(lines[entries[1].end], "timeout = 5   # seconds", "trailing blank lines are not part of the entry")
        self.assertEqual(lines[entries[1].start], "[[hooks]]")

    def test_escapes_quotes_and_comments_inside_strings(self) -> None:
        text = '[[hooks]]\ncommand = "\'/h x/fleet-guard-hook\' --format kimi # not a comment" # a comment\nevent = "a\\"b"\n'
        (e,) = H.scan_hooks(text)
        self.assertEqual(e.command, "'/h x/fleet-guard-hook' --format kimi # not a comment")
        self.assertEqual(e.fields["event"], 'a"b')
        self.assertFalse(e.complex)

    def test_a_value_that_runs_over_several_lines_marks_the_entry_complex(self) -> None:
        for body in ('command = """\nx\n"""\n', "command = '''\nx\n'''\n", "command = [\n  'x',\n]\n", "command = {a = 1}\n"):
            with self.subTest(body=body):
                (e,) = H.scan_hooks("[[hooks]]\n" + body)
                self.assertTrue(e.complex)
                self.assertIsNone(e.command)

    def test_a_marker_line_ends_an_entry(self) -> None:
        text = "# fleet:begin %s (managed by t)\n%s# fleet:end %s\ntail = 1\n" % (NAME, ENTRY, NAME)
        (e,) = H.scan_hooks(text)
        self.assertEqual(text.split("\n")[e.end], "timeout = 5")
        self.assertNotIn("tail", e.fields)

    def test_other_tables_named_hooks_are_not_entries(self) -> None:
        self.assertEqual(H.scan_hooks("[hooks]\nx = 1\n[[hooks.extra]]\ny = 1\n[[other]]\n"), [])

    def test_crlf_files_are_read(self) -> None:
        (e,) = H.scan_hooks(ENTRY.replace("\n", "\r\n"))
        self.assertEqual(e.fields, DESIRED)


class PlanTests(unittest.TestCase):
    def test_a_file_without_ours_gets_the_block_appended(self) -> None:
        new, action, notes = plan(OWNER)
        self.assertEqual(action, "append")
        self.assertTrue(new.startswith(OWNER + "\n# fleet:begin " + NAME))
        self.assertEqual(notes, [])

    def test_a_missing_or_empty_file_is_created(self) -> None:
        self.assertEqual(plan(None)[1], "create")
        self.assertEqual(plan("  \n")[1], "append")

    def test_the_block_replaces_itself_and_a_second_plan_changes_nothing(self) -> None:
        new, _, _ = plan(OWNER)
        again, action, _ = plan(new)
        self.assertEqual((again, action), (new, "none"))
        changed = new.replace("timeout = 5", "timeout = 9")
        fixed, action, _ = plan(changed)
        self.assertEqual((fixed, action), (new, "replace"))

    def test_stripped_markers_with_current_fields_change_nothing(self) -> None:
        new, _, _ = plan(OWNER)
        stripped = strip_comments(new)
        again, action, notes = plan(stripped)
        self.assertEqual((again, action), (stripped, "none"))
        self.assertIn("lost its markers", notes[0])

    def test_stripped_markers_with_other_fields_are_replaced_where_they_stand(self) -> None:
        new, _, _ = plan(OWNER)
        stripped = strip_comments(new).replace('matcher = "Bash"', 'matcher = "Other"') + "\n[later]\nx = 1\n"
        fixed, action, notes = plan(stripped)
        self.assertEqual(action, "replace")
        self.assertEqual(fixed.count("[[hooks]]"), 2)
        self.assertEqual(fixed.count("# fleet:begin"), 1)
        self.assertTrue(fixed.endswith("[later]\nx = 1\n"), "what follows the entry is kept")
        self.assertLess(fixed.index("# fleet:begin"), fixed.index("[later]"))
        self.assertIn("replaced in place", notes[-1])
        self.assertEqual(plan(fixed)[1], "none")

    def test_several_unmarked_entries_collapse_into_the_first_place(self) -> None:
        a = ENTRY.replace('"Bash"', '"Old"')
        text = OWNER + "\n" + a + "\n[mid]\nx = 1\n\n" + ENTRY
        fixed, action, notes = plan(text)
        self.assertEqual(action, "replace")
        self.assertEqual(fixed.count("[[hooks]]"), 2, "the owner's entry and one of ours")
        self.assertTrue(fixed.index("# fleet:begin") < fixed.index("[mid]"))
        self.assertIn("removed 1 duplicate", notes[0])
        self.assertNotIn("Old", fixed)

    def test_a_duplicate_outside_an_intact_block_is_removed(self) -> None:
        new, _, _ = plan(OWNER)
        text = new + "\n" + ENTRY
        fixed, action, notes = plan(text)
        self.assertEqual((fixed, action), (new, "replace"))
        self.assertIn("duplicate", notes[0])

    def test_a_lane_guard_entry_is_ours_too_and_is_replaced(self) -> None:
        old = OWNER + "\n[[hooks]]\nevent = \"PreToolUse\"\nmatcher = \"Bash\"\ncommand = \"/h/lane-tools/fleet-guard-hook --format claude\"\ntimeout = 5\n"
        fixed, action, _ = plan(old)
        self.assertEqual(action, "replace")
        self.assertIn("--format kimi", fixed)
        self.assertNotIn("--format claude", fixed)

    def test_forms_that_cannot_be_edited_raise(self) -> None:
        for text in ('hooks = [{ command = "%s" }]\n' % CMD,
                     '[x]\nnote = "%s"\n' % CMD,
                     '[[hooks]]\ncommand = """\n%s\n"""\n' % CMD,
                     '[[hooks]]\ncommand = "%s"\nenv = {a = 1}\n' % CMD):
            with self.subTest(text=text):
                with self.assertRaises(MB.BlockError):
                    plan(text)

    def test_a_comment_that_mentions_the_hook_is_not_a_mention(self) -> None:
        new, action, _ = plan(OWNER + "# fleet-guard-hook is described here\n")
        self.assertEqual(action, "append")

    def test_crlf_files_keep_their_line_endings(self) -> None:
        crlf = OWNER.replace("\n", "\r\n")
        new, action, _ = plan(crlf)
        self.assertEqual(action, "append")
        self.assertNotRegex(new, r"(?<!\r)\n")
        stripped = strip_comments(new.replace("\r\n", "\n")).replace('matcher = "Bash"', 'matcher = "X"').replace("\n", "\r\n")
        fixed, action, _ = plan(stripped)
        self.assertEqual(action, "replace")
        self.assertNotRegex(fixed, r"(?<!\r)\n")

    def test_inspect_reports_the_state(self) -> None:
        new, _, _ = plan(OWNER)
        self.assertEqual(H.inspect(OWNER, NAME, DESIRED, is_ours).state, "none")
        info = H.inspect(new, NAME, DESIRED, is_ours)
        self.assertEqual((info.state, info.current, len(info.inside), len(info.outside)), ("block", True, 1, 0))
        info = H.inspect(strip_comments(new), NAME, DESIRED, is_ours)
        self.assertEqual((info.state, info.current, len(info.outside)), ("stripped", True, 1))
        with self.assertRaises(MB.BlockError):
            H.inspect("# fleet:begin %s (managed by t)\n" % NAME, NAME, DESIRED, is_ours)

    def test_the_result_parses_as_toml_where_tomllib_exists(self) -> None:
        try:
            import tomllib
        except ImportError:
            self.skipTest("Python %d.%d has no tomllib" % sys.version_info[:2])
        new, _, _ = plan(OWNER)
        for text in (new, strip_comments(new), plan(strip_comments(new).replace("Bash", "X"))[0]):
            hooks = tomllib.loads(text)["hooks"]
            self.assertEqual([h["event"] for h in hooks], ["Notification", "PreToolUse"])


if __name__ == "__main__":
    unittest.main()
