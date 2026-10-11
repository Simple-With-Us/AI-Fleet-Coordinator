#!/usr/bin/env python3
"""Tests for scripts/check-routine-prompts.py (the routine-text checker, open-loops unit RT0).

    python3 scripts/test_check_routine_prompts.py

The committed fixtures under scripts/tests/fixtures/routines/ are synthetic.  Anything secret-shaped is built here
at run time from pieces, so no scanner (gitleaks, GitHub push protection) ever sees a whole one in the source.
The real routine texts are private and never appear in this repository.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "check-routine-prompts.py"
FIXTURES = SCRIPTS / "tests" / "fixtures" / "routines"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_routine_prompts", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod       # @dataclass looks its module up here
    loader.exec_module(mod)
    return mod


chk = _load()

REMINDER = "I often distract away from things but never intend to undo what I said prior unless explicitly say so"


def run(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = chk.main(list(argv))
    return code, out.getvalue(), err.getvalue()


def rules(name: str, text: str, **kw) -> set[str]:
    return {f.rule for f in chk.check_text(name, text, **kw)}


def clean(extra: str = "") -> str:
    """A text that passes every rule for a non-GB file."""
    return "# Sample\n\nSay it plainly.  Then stop.\n\nReminder:  \"%s.\"\n%s" % (REMINDER, extra)


def gb_text(role: str, extra: str = "") -> str:
    return ("# Block\n\nYou are GB-%s.  Check GET /users/me for %s-grok-bot@example.test first.\n"
            "Start each post with [GB-%s] on its own line.\n\nReminder:  \"%s.\"\n%s"
            % (role, role.lower(), role, REMINDER, extra))


class FixtureTests(unittest.TestCase):
    def test_good_fixtures_pass_with_a_pass_line(self):
        code, out, err = run(str(FIXTURES / "good"))
        self.assertEqual((code, err), (0, ""))
        self.assertIn("routine texts ok: 4 file(s), 2 pasted pair(s)", out)

    def test_bad_directory_fails(self):
        code, out, _ = run(str(FIXTURES / "bad"))
        self.assertEqual(code, 1)
        self.assertNotIn("routine texts ok", out)

    def test_each_bad_fixture_fails_on_exactly_its_own_rule(self):
        expected = {
            "typed-mention.md": {"typed-mention"},
            "code-path.md": {"code-path"},
            "slack-helper.md": {"slack-helper"},
            "single-space-gap.md": {"single-space-gap"},
            "reminder-missing.md": {"reminder-missing"},
            "gb-label-in-core.md": {"gb-label"},
            "gb-plumber.md": {"gb-label"},
        }
        on_disk = {p.name for p in (FIXTURES / "bad").glob("*.md")}
        self.assertEqual(on_disk, set(expected), "a bad fixture was added or removed without a test")
        for name, want in expected.items():
            path = FIXTURES / "bad" / name
            self.assertEqual(rules(str(path), path.read_text(encoding="utf-8")), want, name)

    def test_findings_never_echo_the_text(self):
        _, out, _ = run(str(FIXTURES / "bad"))
        for needle in ("Some Person", "Sample-Repo", "websocket"):
            self.assertNotIn(needle, out.replace("agent-sync-websocket", ""))


class LengthTests(unittest.TestCase):
    def test_limit_is_20000_and_counts_utf16_units(self):
        self.assertEqual(chk.MAX_CHARS, 20000)
        base = clean()
        pad = lambda n: base + "x" * (n - chk.utf16_length(base))
        self.assertNotIn("too-long", rules("a.md", pad(20000)))
        self.assertIn("too-long", rules("a.md", pad(20001)))
        # An emoji is two UTF-16 code units, which is what BotFleet's JavaScript length counts.
        self.assertEqual(chk.utf16_length("\U0001F600"), 2)
        self.assertIn("too-long", rules("a.md", pad(19999) + "\U0001F600"))

    def test_max_chars_flag(self):
        code, out, _ = run("--max-chars", "100", str(FIXTURES / "good"))
        self.assertEqual(code, 1)
        self.assertIn("too-long", out)

    def test_pasted_pair_is_checked_as_one_routine(self):
        # Each file fits under 700 characters;  a block pasted with its core does not.
        sizes = {p.name: len(p.read_text(encoding="utf-8")) for p in (FIXTURES / "good").glob("*.md")}
        limit = max(sizes["gb-deployer.md"], sizes["open-loops-digest.md"]) + 10
        self.assertGreater(sizes["gb-deployer.md"] + sizes["open-loops-digest.md"], limit)
        code, out, _ = run("--max-chars", str(limit), str(FIXTURES / "good"))
        self.assertEqual(code, 1)
        self.assertIn("gb-deployer.md+open-loops-digest.md: too-long", out)
        self.assertNotIn("open-loops-digest.md: too-long", out.replace("gb-deployer.md+open-loops-digest.md", ""))

    def test_join_checks_the_given_files_as_one_routine(self):
        block, core = FIXTURES / "good" / "gb-deployer.md", FIXTURES / "good" / "open-loops-digest.md"
        self.assertEqual(run("--join", str(block), str(core))[0], 0)
        limit = max(len(block.read_text()), len(core.read_text())) + 10
        code, out, _ = run("--join", "--max-chars", str(limit), str(block), str(core))
        self.assertEqual(code, 1)
        self.assertIn("gb-deployer.md + open-loops-digest.md: too-long", out)


class MentionPathSlackTests(unittest.TestCase):
    def test_typed_mentions_including_silent_ones(self):
        at = "@"
        for typed in (at + "**Someone**", at + "_**Someone**", at + "**all**"):
            self.assertIn("typed-mention", rules("a.md", clean("Ping %s now.\n" % typed)), typed)

    def test_describing_how_to_build_a_mention_is_fine(self):
        text = clean("Build it from an at sign, two asterisks, the full name and two asterisks.\n")
        self.assertEqual(rules("a.md", text), set())

    def test_code_paths(self):
        for path in ("~/Code/App/x.py", "/Users/jay/Code/App", "$HOME/Code/App", "${HOME}/Code/App"):
            self.assertIn("code-path", rules("a.md", clean("Open %s.\n" % path)), path)
        for fine in ("~/apps/lanes/App", "/Users/jay/apps/state/x", "the Code tab", "~/Codex/x"):
            self.assertNotIn("code-path", rules("a.md", clean("Open %s.\n" % fine)), fine)

    def test_slack_helper(self):
        for bad in ("agent-sync-websocket.py", "agent-sync-push", "slack-post", "slack_post",
                    "https://hooks.slack.com/services/x", "https://slack.com/api/chat.postMessage"):
            self.assertIn("slack-helper", rules("a.md", clean("Use %s.\n" % bad)), bad)
        self.assertNotIn("slack-helper", rules("a.md", clean("Slack was retired on Oct 7.\n")))


class SentenceGapTests(unittest.TestCase):
    def gaps(self, text: str) -> list[int]:
        return chk.single_space_gaps(text)

    def test_one_space_is_flagged(self):
        self.assertEqual(self.gaps("It ends here. Then more."), [1])
        self.assertEqual(self.gaps("Really? Yes!"), [1])
        self.assertEqual(self.gaps('He said "stop." Then left.'), [1])
        self.assertEqual(self.gaps("Line one is fine.\nLine two ends here! Then more."), [2])

    def test_two_spaces_and_nbsp_pass(self):
        self.assertEqual(self.gaps("It ends here.  Then more."), [])
        self.assertEqual(self.gaps("It ends here.  Then more."), [])
        self.assertEqual(self.gaps("It ends here.   Then more."), [])
        self.assertEqual(self.gaps("It ends here.\nThen more."), [])

    def test_abbreviations_versions_and_list_markers_pass(self):
        for fine in ("Use a tool, e.g. Python.", "That is, i.e. this one.", "Compare vs. the old one.",
                     "See v1.2.3 for details.", "1. Decisions Waiting", "  - 2. Next item", "## 3. Send",
                     "A. Decisions Waiting", "  - b. Next item", "## C. Send"):
            self.assertEqual(self.gaps(fine), [], fine)

    def test_a_boundary_after_no_is_flagged(self):
        # "no" used to sit in the abbreviation list, which hid every sentence that ends with the word
        self.assertEqual(self.gaps("I said no. Then we stop."), [1])
        self.assertEqual(self.gaps("The answer is no! Then we stop."), [1])
        self.assertEqual(self.gaps("No. Then we stop."), [1])
        self.assertEqual(self.gaps("I said no.  Then we stop."), [])

    def test_a_boundary_after_a_one_letter_word_is_flagged(self):
        # a one-letter word used to count as an initial, which hid every sentence that ends with one
        self.assertEqual(self.gaps("Pick option A. Then we stop."), [1])
        self.assertEqual(self.gaps("Take plan B. Then wait."), [1])
        self.assertEqual(self.gaps("You and I. Then we stop."), [1])
        self.assertEqual(self.gaps("Pick option A.  Then we stop."), [])
        # an initial in a name cannot be told from that boundary, so the text is reworded
        self.assertEqual(self.gaps("Written by J. Smith today."), [1])
        # a lone letter that opens a line is a list marker, but only the first boundary on the line is exempt
        self.assertEqual(self.gaps("A. Decisions waiting. Then more."), [1])

    def test_the_bad_fixture_pins_both_shapes(self):
        text = (FIXTURES / "bad" / "single-space-gap.md").read_text(encoding="utf-8")
        lines = text.split("\n")
        want = [number for number, line in enumerate(lines, 1)
                if line in ("The digest is short. It always ends with a link.", "Answer no. Then stop.",
                            "Pick option A. Then wait.")]
        self.assertEqual(len(want), 3)
        self.assertEqual(self.gaps(text), want)

    def test_the_good_fixture_holds_the_same_shapes_spaced_right(self):
        text = (FIXTURES / "good" / "backup-health.md").read_text(encoding="utf-8")
        self.assertIn("no.  Then stop.", text)
        self.assertIn("A.  Then wait.", text)
        self.assertEqual(self.gaps(text), [])

    def test_a_bare_number_is_a_boundary_only_inside_prose(self):
        self.assertEqual(self.gaps("Ends with a number 5. Then"), [1])

    def test_code_urls_and_fences_are_ignored(self):
        self.assertEqual(self.gaps("Run `a. B` now."), [])
        self.assertEqual(self.gaps("Open https://example.test/a. B/c now."), [])
        self.assertEqual(self.gaps("```\nOne. Two.\n```\nDone."), [])
        self.assertEqual(self.gaps("~~~\nOne. Two.\n~~~"), [])
        self.assertEqual(self.gaps("- ```\n  One. Two.\n  ```\n"), [])
        self.assertEqual(self.gaps("```\nOne. Two.\n```\nBack. Again."), [4])


class ReminderTests(unittest.TestCase):
    def test_reminder_forms(self):
        body = "Say it plainly.  Then stop.\n\n"
        self.assertIn("reminder-missing", rules("a.md", body))
        for form in ("%s." % REMINDER, REMINDER, REMINDER.replace(" ", "\n"), REMINDER.replace(" ", " "),
                     "  ".join(REMINDER.split(" "))):
            self.assertNotIn("reminder-missing", rules("a.md", body + '"%s"\n' % form), repr(form))

    def test_a_reworded_reminder_is_missing(self):
        text = "Say it plainly.  Then stop.\n\n\"I often distract away from things but never intend to undo " \
               "what I said unless explicitly told.\"\n"
        self.assertIn("reminder-missing", rules("a.md", text))


class SecretTests(unittest.TestCase):
    def kinds(self, line: str) -> set[str]:
        return {f.message for f in chk.check_text("a.md", clean(line + "\n")) if f.rule == "secret-shaped"}

    def test_each_f3_prefix_is_caught(self):
        samples = {
            "Linear API key": "lin_api_" + "a1B2c3D4" * 5,
            "Sentry token": "sntrys_" + "aB3dE5fG7h" * 4,
            "xAI key": "xai-" + "Zk3Qm9Xa" * 5,
            "Infisical service token": "st." + "ab12cd34" + "." + "ef56ab78" + "." + "cd90ef12",
            "Cloudflare token": "cfut_" + "Ab3dE5fG7h" * 4,
            "Coolify token": "12|" + "aB3dE5fG7h" * 4,
            "Linear client secret": "LINEAR_CLIENT_SECRET=" + "q8w7e6r5" * 4,
            "assigned credential": "GITHUB_READ_TOKEN=" + "q8w7e6r5" * 4,
        }
        for label, secret in samples.items():
            self.assertIn(label, self.kinds("Use " + secret + " now."), label)

    def test_sentry_user_token_and_zulip_shaped_key(self):
        self.assertTrue(self.kinds("Use sntryu_" + "0123abcd" * 8 + " now."))
        key = "Ab3" + "dEf6hIj9lMn2pQr5tUv8xYz1B" + "cD4F"[:4]
        self.assertEqual(len(key), 32)
        self.assertTrue(self.kinds("Use " + key + " now."))

    def test_base_scanner_patterns_still_apply(self):
        self.assertTrue(self.kinds("Use ghp_" + "A1b2C3d4" * 5 + " now."))
        self.assertTrue(self.kinds("Use -----BEGIN RSA " + "PRIVATE KEY-----"))

    def test_names_of_secrets_are_not_secrets(self):
        for fine in ("Use LINEAR_CLIENT_ID and LINEAR_CLIENT_SECRET from your card.",
                     "Set FLEET_FACTS_SIGNING_KEY in the environment.",
                     "Read the secret card and the token you were given.",
                     "A commit abcdef0123456789abcdef0123456789abcdef01 is fine."):
            self.assertEqual(self.kinds(fine), set(), fine)

    def test_finding_never_prints_the_secret(self):
        secret = "lin_api_" + "a1B2c3D4" * 5
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.md"
            path.write_text(clean("Use %s now.\n" % secret), encoding="utf-8")
            code, out, err = run(str(path))
        self.assertEqual(code, 1)
        self.assertIn("secret-shaped", out)
        self.assertNotIn(secret, out + err)
        self.assertNotIn("a1B2c3D4", out + err)


class GbLabelTests(unittest.TestCase):
    def test_a_good_block(self):
        self.assertEqual(rules("gb-deployer.md", gb_text("DEPLOYER")), set())
        self.assertEqual(rules("elsewhere/gb-plumber.txt", gb_text("PLUMBER")), set())

    def test_directed_label_counts_as_the_role_label(self):
        text = gb_text("PLUMBER").replace("[GB-PLUMBER]", "[GB-PLUMBER→BF-PLUMBER]")
        self.assertEqual(rules("gb-plumber.md", text), set())

    def test_missing_label_or_email(self):
        no_label = gb_text("DEPLOYER").replace("[GB-DEPLOYER]", "the label")
        no_email = gb_text("DEPLOYER").replace("deployer-grok-bot@", "the bot address @")
        for text in (no_label, no_email):
            self.assertEqual(rules("gb-deployer.md", text), {"gb-label"})

    def test_another_roles_label_or_email(self):
        other_label = gb_text("DEPLOYER") + "Also post as [GB-HOUSEKEEPER] sometimes.\n"
        other_email = gb_text("DEPLOYER") + "Or check plumber-grok-bot@example.test.\n"
        for text in (other_label, other_email):
            self.assertEqual(rules("gb-deployer.md", text), {"gb-label"})

    def test_a_core_never_carries_a_gb_label(self):
        text = clean("Start posts with [GB-DEPLOYER] here.\n")
        self.assertEqual(rules("open-loops-digest.md", text), {"gb-label"})
        self.assertEqual(rules("open-loops-digest.md", text, gb_role="DEPLOYER") - {"gb-label"}, set())

    def test_bf_names_are_not_gb_labels(self):
        self.assertEqual(rules("backup-health.md", clean("BF-Plumber and GB-Plumber share this core.\n")), set())


class PlaceholderTests(unittest.TestCase):
    def test_placeholders_only_fail_when_asked(self):
        text = clean("Post in topic <C2 ID> open-loops heartbeat.\n")
        self.assertEqual(rules("a.md", text), set())
        self.assertEqual(rules("a.md", text, placeholders=True), {"placeholder"})
        self.assertEqual(rules("a.md", clean("TODO fill in.\n"), placeholders=True), {"placeholder"})


class PrivateFolderTests(unittest.TestCase):
    def make(self, tmp: str, dir_mode: int, file_mode: int) -> Path:
        folder = Path(tmp) / "private"
        folder.mkdir()
        (folder / "open-loops-digest.md").write_text(clean(), encoding="utf-8")
        os.chmod(folder / "open-loops-digest.md", file_mode)
        os.chmod(folder, dir_mode)
        return folder

    def test_private_folder_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.make(tmp, 0o700, 0o600)
            code, out, _ = run("--private", str(folder))
            self.assertEqual(code, 0, out)
            self.assertIn("private folder ok", out)

    def test_group_or_world_access_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.make(tmp, 0o755, 0o600)
            code, out, _ = run("--private", str(folder))
            self.assertEqual(code, 1)
            self.assertIn("private-dir", out)
            os.chmod(folder, 0o700)
            os.chmod(folder / "open-loops-digest.md", 0o644)
            code, out, _ = run("--private", str(folder))
            self.assertEqual(code, 1)
            self.assertIn("expected 0600", out)

    def test_a_folder_inside_a_git_worktree_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".git").mkdir()
            folder = self.make(tmp, 0o700, 0o600)
            code, out, _ = run("--private", str(folder))
            self.assertEqual(code, 1)
            self.assertIn("inside a git worktree", out)

    def test_without_the_flag_modes_are_not_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.make(tmp, 0o755, 0o644)
            self.assertEqual(run(str(folder))[0], 0)
            self.assertTrue(stat.S_IMODE(folder.stat().st_mode) & 0o077)


class UsageTests(unittest.TestCase):
    def test_missing_path_and_empty_folder_are_usage_errors(self):
        self.assertEqual(run("/no/such/folder")[0], 2)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run(tmp)[0], 2)

    def test_a_file_that_is_not_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.md"
            path.write_bytes(b"\xff\xfe not text")
            code, out, _ = run(str(path))
            self.assertEqual(code, 1)
            self.assertIn("unreadable", out)

    def test_a_single_file_argument(self):
        code, out, _ = run(str(FIXTURES / "good" / "gb-deployer.md"))
        self.assertEqual(code, 0)
        self.assertIn("1 file(s), 0 pasted pair(s)", out)

    def test_secretscan_is_loaded_from_the_repo_copy(self):
        self.assertIsNotNone(chk._SCAN, "scripts/agent_sync/secretscan.py should load by path")


if __name__ == "__main__":
    unittest.main()
