#!/usr/bin/env python3
"""Tests for scripts/check-generated-mirror.py (the GENERATED header, the body hash, and who may change a mirror).

    python3 scripts/test_check_generated_mirror.py

Every case builds a throwaway git repository in a temp directory:  a base commit, then the "pull request" as
working-tree edits, which is exactly what the CI step sees (a checkout of the merge commit against the base sha).
Nothing reads the network, ~/apps or a real checkout.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-generated-mirror.py"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_generated_mirror", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod  # @dataclass looks its module up here
    loader.exec_module(mod)
    return mod


gm = _load()

# The exact line scripts/handbook/build.mjs headerLine() writes in Fleet-OPS (copied from its source, with the hash
# slot filled by header()).  If the generator's wording changes, this fixture and HEADER_RE change together.
GENERATOR_LINE = (
    "<!-- GENERATED from Simple-With-Us/Fleet-OPS handbook/.  Do not edit here; open a PR to handbook/.  "
    "body-sha256:{sha} -->"
)

BODY = "# AGENT-SYNC\n\nOne rule.  Another rule.\n"
OPS_BODY = "# AGENT-SYNC\n\nOPS and Fleet-OPS are listed here.\n"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def header(body: str, claimed: str | None = None) -> str:
    return GENERATOR_LINE.format(sha=claimed or sha(body)) + "\n"


def mirror(body: str = BODY) -> str:
    return header(body) + body


class RepoCase(unittest.TestCase):
    PATH = "AGENT-SYNC.md"

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.base_sha: str | None = None

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def git(self, *args: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
        }
        try:
            done = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                env=env,
                check=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            sys.exit(124)
        except FileNotFoundError:
            sys.exit(127)
        return done.stdout.strip()

    def write(self, text: str, path: str | None = None) -> None:
        target = self.root / (path or self.PATH)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())
        self.git(
            "add", "-A"
        )  # the check diffs the base against the working tree, which sees staged new files

    def commit_base(self, text: str | None) -> str:
        if text is not None:
            self.write(text)
        else:
            (self.root / "README.md").write_text("x\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base_sha = self.git("rev-parse", "HEAD")
        return self.base_sha

    def spec(self, pin: str | None = None) -> gm.GeneratedPath:
        return gm.GeneratedPath(self.PATH, pin)

    def run_check(self, pin: str | None = None, *, base: bool = False, branch: str | None = None):
        problems, notes = gm.check(
            self.root, (self.spec(pin),), self.base_sha if base else None, branch
        )
        return [m for _, m in problems], notes


class ContentRules(RepoCase):
    def test_valid_header_and_body_pass(self) -> None:
        self.commit_base(None)
        self.write(mirror())
        problems, notes = self.run_check()
        self.assertEqual(problems, [])
        self.assertIn("GENERATED header valid", notes[0])

    def test_hand_edited_body_fails_on_the_stale_hash(self) -> None:
        self.commit_base(None)
        self.write(header(BODY) + BODY + "A hand-edited line.\n")
        problems, _ = self.run_check()
        self.assertEqual(len(problems), 1)
        self.assertIn("does not match its header", problems[0])
        self.assertIn(sha(BODY), problems[0])

    def test_header_removed_fails_unless_it_is_the_pinned_text(self) -> None:
        self.commit_base(None)
        self.write(BODY + "A hand-edited line.\n")
        problems, _ = self.run_check(pin=sha(BODY))
        self.assertEqual(len(problems), 1)
        self.assertIn("not the pinned pre-flip text", problems[0])

    def test_pinned_pre_flip_text_passes_without_a_header(self) -> None:
        self.commit_base(None)
        self.write(BODY)
        problems, notes = self.run_check(pin=sha(BODY))
        self.assertEqual(problems, [])
        self.assertIn("pre-flip text", notes[0])

    def test_no_pin_means_the_header_is_required(self) -> None:
        self.commit_base(None)
        self.write(BODY)
        problems, _ = self.run_check(pin=None)
        self.assertEqual(len(problems), 1)
        self.assertIn("no GENERATED header", problems[0])

    def test_once_the_header_is_present_the_pin_does_not_excuse_a_bad_hash(self) -> None:
        self.commit_base(None)
        self.write(header(BODY, claimed="0" * 64) + BODY)
        problems, _ = self.run_check(pin=sha(BODY))
        self.assertEqual(len(problems), 1)
        self.assertIn("does not match its header", problems[0])

    def test_header_must_be_the_generators_line(self) -> None:
        self.commit_base(None)
        good = GENERATOR_LINE.format(sha=sha(BODY))
        bad_lines = [
            good.replace("Fleet-OPS", "Other-Repo"),
            good.replace(".  Do not", ". Do not"),  # one space, not two
            good.replace(sha(BODY), sha(BODY).upper()),  # uppercase hex
            good.replace(sha(BODY), sha(BODY)[:63]),  # 63 digits
            good + " ",  # trailing space
            good.replace("<!-- GENERATED", "<!--  GENERATED"),
            good.replace(" -->", " -->  trailing"),
        ]
        for line in bad_lines:
            with self.subTest(line=line):
                self.write(line + "\n" + BODY)
                problems, _ = self.run_check()
                self.assertEqual(len(problems), 1, line)
                self.assertIn("not the generator's line", problems[0])
        self.write(good + "\n" + BODY)
        self.assertEqual(self.run_check()[0], [])

    def test_header_not_on_line_one_is_no_header(self) -> None:
        self.commit_base(None)
        self.write("\n" + mirror())
        problems, _ = self.run_check(pin=sha(BODY))
        self.assertEqual(len(problems), 1)
        self.assertIn("not the pinned pre-flip text", problems[0])

    def test_crlf_after_the_header_fails(self) -> None:
        self.commit_base(None)
        self.root.joinpath(self.PATH).write_bytes(
            GENERATOR_LINE.format(sha=sha(BODY)).encode() + b"\r\n" + BODY.encode()
        )
        problems, _ = self.run_check()
        self.assertEqual(len(problems), 1)
        self.assertIn("not the generator's line", problems[0])

    def test_hash_is_over_raw_bytes_after_the_first_newline(self) -> None:
        self.commit_base(None)
        body = "café — line\r\nsecond\r\n"
        raw = body.encode()
        self.root.joinpath(self.PATH).write_bytes(
            GENERATOR_LINE.format(sha=hashlib.sha256(raw).hexdigest()).encode() + b"\n" + raw
        )
        self.assertEqual(self.run_check()[0], [])

    def test_header_with_no_body_fails(self) -> None:
        self.commit_base(None)
        self.root.joinpath(self.PATH).write_bytes(
            GENERATOR_LINE.format(sha=sha("")).encode() + b"\n"
        )
        self.assertIn("no body", self.run_check()[0][0])
        self.root.joinpath(self.PATH).write_bytes(GENERATOR_LINE.format(sha=sha("")).encode())
        self.assertIn("no body", self.run_check()[0][0])

    def test_missing_file_fails(self) -> None:
        self.commit_base(None)
        problems, _ = self.run_check()
        self.assertEqual(len(problems), 1)
        self.assertIn("is missing", problems[0])

    def test_the_shipped_entry_is_the_current_document_with_no_stale_pin(self) -> None:
        spec = gm.GENERATED_PATHS[0]
        self.assertEqual(spec.path, "FLEET-PROTOCOL.md")
        self.assertIsNone(spec.pre_flip_sha256)
        self.assertIsNone(spec.alternate)


class BranchRule(RepoCase):
    def test_hand_edit_with_a_recomputed_hash_fails_off_the_mirror_branch(self) -> None:
        self.commit_base(mirror())
        edited = BODY + "Slipped in.\n"
        self.write(mirror(edited))
        problems, _ = self.run_check(base=True, branch="claude/some-edit")
        self.assertEqual(len(problems), 1)
        self.assertIn("'claude/some-edit', not 'handbook-sync/mirror'", problems[0])

    def test_the_same_change_on_the_mirror_branch_passes(self) -> None:
        self.commit_base(mirror())
        self.write(mirror(BODY + "Landed from Fleet-OPS.\n"))
        problems, _ = self.run_check(base=True, branch="handbook-sync/mirror")
        self.assertEqual(problems, [])

    def test_mirror_branch_still_needs_a_valid_hash(self) -> None:
        self.commit_base(mirror())
        self.write(header(BODY) + BODY + "Edited after the hash.\n")
        problems, _ = self.run_check(base=True, branch="handbook-sync/mirror")
        self.assertEqual(len(problems), 1)
        self.assertIn("does not match its header", problems[0])

    def test_a_fork_cannot_borrow_the_mirror_branch_name(self) -> None:
        self.commit_base(mirror())
        self.write(mirror(BODY + "From a fork.\n"))
        problems, _ = self.run_check(
            base=True, branch="fork:someone/AI-Fleet-Coordinator:handbook-sync/mirror"
        )
        self.assertEqual(len(problems), 1)

    def test_a_similar_branch_name_is_not_the_mirror_branch(self) -> None:
        self.commit_base(mirror())
        self.write(mirror(BODY + "Near miss.\n"))
        for name in (
            "handbook-sync/mirror2",
            "handbook-sync",
            "Handbook-Sync/Mirror",
            "x/handbook-sync/mirror",
        ):
            with self.subTest(branch=name):
                self.assertEqual(len(self.run_check(base=True, branch=name)[0]), 1)

    def test_an_unchanged_mirror_passes_on_any_branch(self) -> None:
        self.commit_base(mirror())
        self.assertEqual(self.run_check(base=True, branch="claude/unrelated")[0], [])

    def test_first_header_from_a_feature_branch_fails(self) -> None:
        self.commit_base(BODY)
        self.write(mirror(BODY))  # strips nothing but adds a valid header by hand
        problems, _ = self.run_check(pin=sha(BODY), base=True, branch="claude/flip-it-myself")
        self.assertEqual(len(problems), 1)
        self.assertIn("only the sync job's mirror pull request", problems[0])

    def test_first_header_from_the_mirror_branch_passes(self) -> None:
        self.commit_base(BODY)
        self.write(mirror(BODY + "Rewritten by the handbook.\n"))
        problems, _ = self.run_check(pin=sha(BODY), base=True, branch="handbook-sync/mirror")
        self.assertEqual(problems, [])

    def test_stripping_the_header_fails_on_both_rules(self) -> None:
        self.commit_base(mirror())
        self.write(BODY)
        problems, _ = self.run_check(pin=sha(BODY), base=True, branch="claude/strip")
        # the stripped text is the pinned pre-flip text, so rule 1 allows it;  rule 3 is what refuses the revert
        self.assertEqual(len(problems), 1)
        self.assertIn("only the sync job's mirror pull request", problems[0])

    def test_mirror_branch_stripping_back_to_the_pinned_text_fails(self) -> None:
        # Once the base already has a generated mirror, the pin exemption in rule 1 would accept the exact pre-flip
        # bytes, and rule 3's mirror-branch exemption would let the change through.  Together that would let a
        # later mirror PR drop the GENERATED header from main while CI reports success.  The mirror-branch strip
        # check fires regardless of those exemptions.
        self.commit_base(mirror())
        self.write(BODY)  # the pinned pre-flip text, no header
        problems, _ = self.run_check(pin=sha(BODY), base=True, branch="handbook-sync/mirror")
        # the only problem is the strip:  rule 1 accepts the pinned bytes, rule 3 exempts the mirror branch
        self.assertEqual(len(problems), 1)
        self.assertIn("strips the GENERATED header", problems[0])

    def test_mirror_branch_stripping_to_non_pinned_text_fails(self) -> None:
        # Same shape, but the unheadered head is not the pinned text.  Both rule 1 and the strip rule fire, and
        # the strip rule's message is in the output.
        self.commit_base(mirror())
        self.write(BODY + "An extra line.\n")  # not the pin, so rule 1 also fires
        problems, _ = self.run_check(pin=sha(BODY), base=True, branch="handbook-sync/mirror")
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("strips the GENERATED header" in m for m in problems))
        self.assertTrue(any("not the pinned pre-flip text" in m for m in problems))

    def test_mirror_branch_deleting_a_mirror_fails(self) -> None:
        # Deleting the file on the mirror branch removes the GENERATED header too.  The headerless head combined
        # with a headered base is the same shape as a strip, so the mirror-branch check fires.
        self.commit_base(mirror())
        (self.root / self.PATH).unlink()
        self.git("add", "-A")
        problems, _ = self.run_check(base=True, branch="handbook-sync/mirror")
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("is missing" in m for m in problems))
        self.assertTrue(any("strips the GENERATED header" in m for m in problems))

    def test_mirror_branch_first_header_still_passes(self) -> None:
        # The strip check must not block the legitimate first-mirror-PR transition:  a headered head on a
        # headerless base is the exact shape that lands the GENERATED header on main.
        self.commit_base(BODY)
        self.write(mirror(BODY + "Rewritten by the handbook.\n"))
        problems, _ = self.run_check(pin=sha(BODY), base=True, branch="handbook-sync/mirror")
        self.assertEqual(problems, [])

    def test_pre_flip_pull_request_that_leaves_the_file_alone_passes(self) -> None:
        self.commit_base(BODY)
        problems, notes = self.run_check(pin=sha(BODY), base=True, branch="claude/other-work")
        self.assertEqual(problems, [])
        self.assertIn("pre-flip text", notes[0])

    def test_pre_flip_hand_edit_is_refused_by_the_pin(self) -> None:
        self.commit_base(BODY)
        self.write(BODY + "A freeze-time hand edit.\n")
        problems, _ = self.run_check(pin=sha(BODY), base=True, branch="claude/other-work")
        self.assertEqual(len(problems), 1)
        self.assertIn("not the pinned pre-flip text", problems[0])

    def test_a_path_the_base_lacks_is_a_change(self) -> None:
        self.commit_base(None)
        self.write(mirror())
        problems, _ = self.run_check(base=True, branch="claude/adds-it")
        self.assertEqual(len(problems), 1)

    def test_push_or_local_run_skips_the_branch_rule_and_says_so(self) -> None:
        self.commit_base(mirror())
        self.write(mirror(BODY + "On main after the merge.\n"))
        problems, notes = self.run_check(base=True, branch=None)
        self.assertEqual(problems, [])
        self.assertTrue(any("branch rule skipped" in n for n in notes))

    def test_unknown_base_is_an_error_not_a_pass(self) -> None:
        self.commit_base(mirror())
        with self.assertRaises(gm.GitError):
            gm.check(self.root, (self.spec(),), "0" * 40, "claude/x")

    def test_mirror_branch_may_change_generated_paths_only(self) -> None:
        self.commit_base(mirror())
        self.write(mirror(BODY + "Landed from Fleet-OPS.\n"))
        self.write("print('smuggled')\n", "scripts/other.py")
        problems, _ = self.run_check(base=True, branch="handbook-sync/mirror")
        self.assertEqual(len(problems), 1)
        self.assertIn("generated paths only", problems[0])
        self.assertIn("scripts/other.py", problems[0])

    def test_other_branches_are_not_held_to_generated_paths_only(self) -> None:
        self.commit_base(mirror())
        self.write("print('fine')\n", "scripts/other.py")
        self.assertEqual(self.run_check(base=True, branch="claude/ordinary")[0], [])

    def test_deleting_a_mirror_off_the_mirror_branch_fails(self) -> None:
        self.commit_base(mirror())
        (self.root / self.PATH).unlink()
        self.git("add", "-A")
        problems, _ = self.run_check(base=True, branch="claude/delete-it")
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("is missing" in m for m in problems))
        self.assertTrue(any("not 'handbook-sync/mirror'" in m for m in problems))

    def test_two_paths_are_checked_independently(self) -> None:
        self.write(mirror(), "AGENT-SYNC.md")
        self.write(mirror(OPS_BODY), "docs/guide.md")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")
        self.base_sha = self.git("rev-parse", "HEAD")
        self.write(mirror(OPS_BODY + "Changed.\n"), "docs/guide.md")
        specs = (gm.GeneratedPath("AGENT-SYNC.md", None), gm.GeneratedPath("docs/guide.md", None))
        problems, _ = gm.check(self.root, specs, self.base_sha, "claude/x")
        self.assertEqual([p for p, _ in problems], ["docs/guide.md"])


class RenameInFlight(RepoCase):
    """A mirror being renamed:  exactly one of the two names exists, and only the mirror branch may move it."""

    OLD = "OLD-NAME.md"
    NEW = "NEW-NAME.md"

    def pair(self):
        return (
            gm.GeneratedPath(self.OLD, None, alternate=self.NEW),
            gm.GeneratedPath(self.NEW, None, alternate=self.OLD),
        )

    def run_pair(self, *, base: bool = False, branch: str | None = None):
        problems, notes = gm.check(self.root, self.pair(), self.base_sha if base else None, branch)
        return [m for _, m in problems], notes

    def remove(self, path: str) -> None:
        (self.root / path).unlink()
        self.git("add", "-A")

    def test_the_old_name_alone_is_fine_and_the_new_name_is_noted_as_absent(self) -> None:
        self.write(mirror(), self.OLD)
        self.commit_base(None)
        problems, notes = self.run_pair(base=True, branch="claude/anything")
        self.assertEqual(problems, [])
        self.assertTrue(any(self.NEW in n and "rename in flight" in n for n in notes), notes)

    def test_the_new_name_alone_is_fine_after_the_move(self) -> None:
        self.write(mirror(), self.NEW)
        self.commit_base(None)
        problems, _ = self.run_pair(base=True, branch="claude/anything")
        self.assertEqual(problems, [])

    def test_neither_name_fails(self) -> None:
        self.commit_base(None)
        problems, _ = self.run_pair()
        self.assertEqual(len(problems), 2)
        self.assertTrue(all("a generated path must exist" in m for m in problems))

    def test_both_names_fail(self) -> None:
        self.write(mirror(), self.OLD)
        self.write(mirror(), self.NEW)
        self.commit_base(None)
        problems, _ = self.run_pair()
        self.assertTrue(problems)
        self.assertTrue(all("exactly one of the two may exist" in m for m in problems), problems)

    def test_the_mirror_branch_moves_the_document(self) -> None:
        self.write(mirror(), self.OLD)
        self.commit_base(None)
        self.remove(self.OLD)
        self.write(mirror(BODY + "Moved.\n"), self.NEW)
        problems, _ = self.run_pair(base=True, branch="handbook-sync/mirror")
        self.assertEqual(problems, [])

    def test_the_mirror_branch_adding_the_new_name_without_deleting_the_old_fails(self) -> None:
        self.write(mirror(), self.OLD)
        self.commit_base(None)
        self.write(mirror(BODY + "Moved.\n"), self.NEW)
        problems, _ = self.run_pair(base=True, branch="handbook-sync/mirror")
        self.assertTrue(problems)
        self.assertTrue(all("exactly one of the two may exist" in m for m in problems), problems)

    def test_an_ordinary_branch_cannot_put_a_headered_copy_under_the_new_name(self) -> None:
        self.write(mirror(), self.OLD)
        self.commit_base(None)
        self.remove(self.OLD)
        self.write(mirror(), self.NEW)
        problems, _ = self.run_pair(base=True, branch="claude/hand-move")
        self.assertTrue(problems)
        self.assertTrue(any("not 'handbook-sync/mirror'" in m for m in problems), problems)

    def test_an_ordinary_branch_cannot_delete_the_old_name(self) -> None:
        self.write(mirror(), self.OLD)
        self.write(
            mirror(), self.NEW
        )  # (cannot exist together on main;  the base here only sets up the deletion)
        self.commit_base(None)
        self.remove(self.OLD)
        problems, _ = self.run_pair(base=True, branch="claude/delete")
        self.assertTrue(any("not 'handbook-sync/mirror'" in m for m in problems), problems)

    def test_the_mirror_branch_may_not_touch_other_paths_during_the_move(self) -> None:
        self.write(mirror(), self.OLD)
        self.commit_base(None)
        self.remove(self.OLD)
        self.write(mirror(), self.NEW)
        self.write("other\n", "README.md")
        problems, _ = self.run_pair(base=True, branch="handbook-sync/mirror")
        self.assertTrue(any("may change generated paths only" in m for m in problems), problems)

    def test_the_shipped_list_names_only_the_current_document(self) -> None:
        shipped = {s.path: s.alternate for s in gm.GENERATED_PATHS}
        self.assertEqual(shipped.get("FLEET-PROTOCOL.md"), None)
        self.assertNotIn("AGENT-SYNC.md", shipped)


class CommandLine(RepoCase):
    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        saved = gm.GENERATED_PATHS
        gm.GENERATED_PATHS = (self.spec(None),)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    code = gm.main(["--root", str(self.root), *argv])
                except SystemExit as exc:
                    code = int(exc.code)
        finally:
            gm.GENERATED_PATHS = saved
        return code, out.getvalue(), err.getvalue()

    def test_exit_codes_and_output(self) -> None:
        self.commit_base(None)
        self.write(mirror())
        code, out, _ = self.run_main()
        self.assertEqual(
            (code, out.startswith("ok: AGENT-SYNC.md: GENERATED header valid")), (0, True)
        )
        self.write(header(BODY) + BODY + "Hand edit.\n")
        code, out, _ = self.run_main()
        self.assertEqual(code, 1)
        self.assertTrue(
            out.startswith("error: AGENT-SYNC.md: the body does not match its header"), out
        )

    def test_github_annotation_only_in_actions(self) -> None:
        self.commit_base(None)
        self.write(BODY)
        saved = os.environ.get("GITHUB_ACTIONS")
        try:
            os.environ["GITHUB_ACTIONS"] = "true"
            _, out, _ = self.run_main()
            self.assertIn("::error file=AGENT-SYNC.md::", out)
            os.environ.pop("GITHUB_ACTIONS")
            _, out, _ = self.run_main()
            self.assertNotIn("::error", out)
        finally:
            if saved is None:
                os.environ.pop("GITHUB_ACTIONS", None)
            else:
                os.environ["GITHUB_ACTIONS"] = saved

    def test_bad_base_exits_two(self) -> None:
        self.commit_base(None)
        self.write(mirror())
        code, _, err = self.run_main("--base", "0" * 40, "--head-branch", "claude/x")
        self.assertEqual(code, 2)
        self.assertIn("not a commit", err)

    def test_head_branch_needs_base(self) -> None:
        self.commit_base(None)
        code, _, err = self.run_main("--head-branch", "claude/x")
        self.assertEqual(code, 2)
        self.assertIn("--head-branch needs --base", err)

    def test_end_to_end_pull_request_flags(self) -> None:
        base = self.commit_base(mirror())
        self.write(mirror(BODY + "Edit.\n"))
        code, out, _ = self.run_main("--base", base, "--head-branch", "claude/x")
        self.assertEqual(code, 1)
        self.assertIn("not 'handbook-sync/mirror'", out)
        code, out, _ = self.run_main("--base", base, "--head-branch", "handbook-sync/mirror")
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
