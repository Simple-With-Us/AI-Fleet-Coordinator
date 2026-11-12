#!/usr/bin/env python3
"""Tests for the skill token budget lint (AFC-763)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import skill_token_lint as lint

ROOT = SCRIPTS.parent

SHARED = (
    "The fleet keeps one shared memory of board findings effort logs owner notes "
    "and skill text so a later seat can search it before deriving the same lesson "
    "from scratch during a long turn that would otherwise reread every file in the tree"
)
SHARED_NEAR = SHARED.replace("effort logs", "status logs")
OTHER = (
    "Deploy the worker through the coolify pipeline and confirm the health check "
    "returns the build stamp before traffic moves to the new container on the "
    "public host that serves the daily digest site for the operator dashboard"
)


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _quiet_main(argv: list[str]) -> int:
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        return lint.main(argv)


class EstimateTests(unittest.TestCase):
    def test_chars_divided_by_four(self) -> None:
        self.assertEqual(lint.estimate_tokens(""), 0)
        self.assertEqual(lint.estimate_tokens("abc"), 0)
        self.assertEqual(lint.estimate_tokens("abcd"), 1)
        self.assertEqual(lint.estimate_tokens("a" * 8), 2)
        self.assertEqual(lint.estimate_tokens("a" * 2500 * 4), 2500)

    def test_over_budget_is_strictly_greater(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/exact/SKILL.md", "a" * (lint.DEFAULT_BUDGET * 4))
            _write(root, "skills/over/SKILL.md", "b" * (lint.DEFAULT_BUDGET * 4 + 4))
            report = lint.scan(root)
        by_skill = {row.skill: row for row in report.skills}
        self.assertFalse(by_skill["exact"].over)
        self.assertEqual(by_skill["exact"].max_tokens, lint.DEFAULT_BUDGET)
        self.assertTrue(by_skill["over"].over)
        self.assertEqual(by_skill["over"].max_tokens, lint.DEFAULT_BUDGET + 1)

    def test_budget_argument_changes_the_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/small/SKILL.md", "a" * 40)
            under = lint.scan(root, budget=10)
            over = lint.scan(root, budget=9)
        self.assertFalse(under.skills[0].over)
        self.assertEqual(under.skills[0].max_tokens, 10)
        self.assertTrue(over.skills[0].over)


class DiscoveryTests(unittest.TestCase):
    def test_finds_skill_md_and_other_markdown_under_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/alpha/SKILL.md", "alpha")
            _write(root, "skills/alpha/notes.md", "notes")
            _write(root, "pkg/SKILL.md", "nested skill")
            _write(root, "docs/readme.md", "not a skill")
            _write(root, "docs/fleet-skills/alpha/SKILL.md", "catalog")
            found = {path.relative_to(root).as_posix() for path in lint.skill_markdown_files(root)}
        self.assertEqual(
            found,
            {
                "skills/alpha/SKILL.md",
                "skills/alpha/notes.md",
                "pkg/SKILL.md",
                "docs/fleet-skills/alpha/SKILL.md",
            },
        )

    def test_repo_discovery_includes_canonical_skills_and_skips_agents(self) -> None:
        found = {path.relative_to(ROOT).as_posix() for path in lint.skill_markdown_files(ROOT)}
        self.assertIn("skills/token-thrift/SKILL.md", found)
        self.assertIn("skills/session-start/SKILL.md", found)
        self.assertNotIn("AGENTS.md", found)
        self.assertNotIn("README.md", found)

    def test_invalid_utf8_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "skills" / "bad" / "SKILL.md"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"\xff\xfe not utf-8")
            report = lint.scan(root)
            text = lint.render_text(report, strict=True)
        self.assertEqual(report.unreadable, ("skills/bad/SKILL.md",))
        self.assertTrue(report.has_findings)
        self.assertIn("unreadable file", text)
        self.assertIn("skills/bad/SKILL.md", text)

    def test_seat_copies_collapse_to_one_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = SHARED + "\n"
            _write(root, "skills/alpha/SKILL.md", body)
            _write(root, ".cursor/skills/alpha/SKILL.md", body)
            _write(root, "docs/fleet-skills/by-seat/cursor/alpha/SKILL.md", body)
            report = lint.scan(root, budget=10_000)
        self.assertEqual(len(report.skills), 1)
        self.assertEqual(report.skills[0].skill, "alpha")
        self.assertEqual(report.skills[0].copies, 3)
        self.assertEqual(report.shared, ())


class ParagraphTests(unittest.TestCase):
    def test_exact_paragraph_shared_across_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for skill in ("alpha", "beta", "gamma"):
                _write(root, f"skills/{skill}/SKILL.md", SHARED + "\n")
            report = lint.scan(root, budget=10_000)
        self.assertEqual(len(report.shared), 1)
        self.assertEqual(report.shared[0].skills, ("alpha", "beta", "gamma"))
        self.assertEqual(report.shared[0].similarity, 1.0)
        self.assertIn("shared memory", report.shared[0].sample)

    def test_near_duplicate_is_reported_and_unrelated_text_is_not(self) -> None:
        self.assertGreaterEqual(lint.similarity_ratio(SHARED, SHARED_NEAR), lint.DEFAULT_SIMILARITY)
        self.assertLess(lint.similarity_ratio(SHARED, OTHER), lint.DEFAULT_SIMILARITY)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/alpha/SKILL.md", SHARED + "\n\n" + OTHER + "\n")
            _write(root, "skills/beta/SKILL.md", SHARED_NEAR + "\n")
            report = lint.scan(root, budget=10_000)
        self.assertEqual(len(report.shared), 1)
        self.assertEqual(report.shared[0].skills, ("alpha", "beta"))
        self.assertGreaterEqual(report.shared[0].similarity, lint.DEFAULT_SIMILARITY)
        self.assertLess(report.shared[0].similarity, 1.0)
        self.assertIn("shared memory", report.shared[0].sample)

    def test_similarity_one_keeps_exact_matches_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/alpha/SKILL.md", SHARED + "\n")
            _write(root, "skills/beta/SKILL.md", SHARED_NEAR + "\n")
            _write(root, "skills/gamma/SKILL.md", SHARED + "\n")
            report = lint.scan(root, budget=10_000, similarity=1.0)
        self.assertEqual(len(report.shared), 1)
        self.assertEqual(report.shared[0].skills, ("alpha", "gamma"))
        self.assertNotIn("beta", report.shared[0].skills)

    def test_short_paragraphs_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/alpha/SKILL.md", "Same short line.\n")
            _write(root, "skills/beta/SKILL.md", "Same short line.\n")
            report = lint.scan(root, budget=10_000)
        self.assertEqual(report.shared, ())

    def test_blank_lines_keep_a_shared_paragraph_apart_from_unique_ones(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/alpha/SKILL.md", SHARED + "\n\n" + OTHER + "\n")
            _write(root, "skills/beta/SKILL.md", SHARED + "\n\n" + SHARED_NEAR + " extra unique ending words here now\n")
            report = lint.scan(root, budget=10_000, similarity=1.0)
        self.assertEqual(len(report.shared), 1)
        self.assertIn("shared memory", report.shared[0].sample)
        self.assertNotIn("coolify", report.shared[0].sample)

    def test_fenced_code_is_its_own_paragraph(self) -> None:
        code = "python3 scripts/skill_token_lint.py --budget 2500 --strict --format json\n" * 6
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = SHARED + "\n\n```\n" + code + "```\n"
            _write(root, "skills/alpha/SKILL.md", body)
            _write(root, "skills/beta/SKILL.md", "```\n" + code + "```\n")
            report = lint.scan(root, budget=10_000)
        samples = [group.sample for group in report.shared]
        self.assertTrue(any("skill_token_lint" in sample for sample in samples))
        self.assertTrue(any("shared memory" not in sample for sample in samples))

    def test_same_skill_does_not_count_as_shared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/alpha/SKILL.md", SHARED + "\n")
            _write(root, "skills/alpha/notes.md", SHARED + "\n")
            report = lint.scan(root, budget=10_000)
        self.assertEqual(report.shared, ())
        self.assertEqual(report.skills[0].copies, 2)


class RenderTests(unittest.TestCase):
    def test_summary_table_names_tokens_and_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/big/SKILL.md", "a" * 40)
            _write(root, "skills/small/SKILL.md", "b" * 4)
            _write(root, "skills/big-peer/SKILL.md", SHARED + "\n")
            _write(root, "skills/small-peer/SKILL.md", SHARED + "\n")
            report = lint.scan(root, budget=9)
            text = lint.render_text(report)
        self.assertIn("WARNING:", text)
        self.assertIn("does not block", text)
        self.assertIn("chars/4", text)
        self.assertIn("LARGEST SKILLS", text)
        self.assertIn("max_tokens", text)
        self.assertIn("big", text)
        self.assertIn("OVER", text)
        self.assertIn("ok", text)
        self.assertIn("SHARED PARAGRAPHS", text)
        self.assertIn("big-peer", text)
        self.assertIn("small-peer", text)
        self.assertLess(text.index("big"), text.index("\nsmall"))

    def test_clean_report_says_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/tiny/SKILL.md", "hello\n")
            report = lint.scan(root)
            text = lint.render_text(report)
        self.assertTrue(text.startswith("OK:"))
        self.assertIn("OVER BUDGET\nnone", text)
        self.assertIn("SHARED PARAGRAPHS\nnone", text)
        self.assertFalse(report.has_findings)

    def test_annotations_escape_and_name_the_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/big/SKILL.md", "a" * 40)
            report = lint.scan(root, budget=1)
        lines = lint.annotation_lines(report)
        self.assertEqual(len(lines), 1)
        self.assertIn("::warning file=skills/big/SKILL.md::", lines[0])
        self.assertIn("over the 1 budget", lines[0])

    def test_json_lists_skills_by_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/big/SKILL.md", "a" * 80)
            _write(root, "skills/small/SKILL.md", "b")
            report = lint.scan(root, budget=10)
        payload = report.to_dict()
        self.assertEqual(payload["skills"][0]["skill"], "big")
        self.assertEqual(payload["skills"][0]["max_tokens"], 20)
        self.assertEqual(payload["chars_per_token"], 4)
        self.assertTrue(payload["has_findings"])


class CliTests(unittest.TestCase):
    def test_warn_mode_exits_zero_and_strict_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/big/SKILL.md", "a" * 40)
            warn = _quiet_main(["--root", str(root), "--budget", "1"])
            strict = _quiet_main(["--root", str(root), "--budget", "1", "--strict"])
            clean = _quiet_main(["--root", str(root), "--budget", "100", "--strict"])
        self.assertEqual(warn, 0)
        self.assertEqual(strict, 1)
        self.assertEqual(clean, 0)

    def test_flag_overrides_env_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/big/SKILL.md", "a" * 40)
            previous = os.environ.get("SKILL_TOKEN_BUDGET")
            os.environ["SKILL_TOKEN_BUDGET"] = "1"
            try:
                from_env = lint.scan(root, budget=lint.resolve_budget(None))
                from_flag = lint.resolve_budget(100)
            finally:
                if previous is None:
                    os.environ.pop("SKILL_TOKEN_BUDGET", None)
                else:
                    os.environ["SKILL_TOKEN_BUDGET"] = previous
        self.assertTrue(from_env.skills[0].over)
        self.assertEqual(from_flag, 100)

    def test_bad_env_budget_exits_two(self) -> None:
        previous = os.environ.get("SKILL_TOKEN_BUDGET")
        os.environ["SKILL_TOKEN_BUDGET"] = "many"
        try:
            code = _quiet_main(["--root", str(ROOT)])
        finally:
            if previous is None:
                os.environ.pop("SKILL_TOKEN_BUDGET", None)
            else:
                os.environ["SKILL_TOKEN_BUDGET"] = previous
        self.assertEqual(code, 2)

    def test_missing_root_exits_two(self) -> None:
        self.assertEqual(_quiet_main(["--root", "/tmp/skill-token-lint-missing-root"]), 2)

    def test_cli_json_and_annotations(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root, "skills/big/SKILL.md", "a" * 40 + " percent % sign\n")
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPTS / "skill_token_lint.py"),
                    "--root",
                    str(root),
                    "--budget",
                    "1",
                    "--format",
                    "json",
                    "--annotations",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload, _, annotations = completed.stdout.partition("\n::warning ")
        data = json.loads(payload)
        self.assertEqual(data["over_budget_count"], 1)
        self.assertIn("::warning ", completed.stdout)
        self.assertIn("skills/big/SKILL.md", annotations)

    def test_script_module_help_mentions_budget(self) -> None:
        completed = subprocess.run(
            [sys.executable, str(SCRIPTS / "skill_token_lint.py"), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--budget", completed.stdout)
        self.assertIn("2500", completed.stdout)


class RepoScanTests(unittest.TestCase):
    def test_real_tree_estimate_matches_chars_over_four(self) -> None:
        report = lint.scan(ROOT)
        self.assertGreaterEqual(len(report.files), 20)
        token_thrift = ROOT / "skills" / "token-thrift" / "SKILL.md"
        expected = len(token_thrift.read_text(encoding="utf-8")) // 4
        match = next(row for row in report.files if row.path == "skills/token-thrift/SKILL.md")
        self.assertEqual(match.tokens, expected)
        text = lint.render_text(report)
        self.assertIn("LARGEST SKILLS", text)
        self.assertIn("max_tokens", text)
        self.assertIn("SHARED PARAGRAPHS", text)


if __name__ == "__main__":
    unittest.main()
