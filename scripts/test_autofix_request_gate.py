#!/usr/bin/env python3
"""Tests for the autofix request gate in the three autofix reusables.

Owner ruling 2026-10-21: a bot comment or review is an autofix request only
when it is a deliberate request - the tag opening the comment - never a tag
mentioned inside review text, quotes, or code blocks.  Kody's verification
replies quoted the trigger phrase and started real runs nobody requested
(AFC run 39608718486).

Each reusable carries the identical gate between REQUEST-GATE-BEGIN and
REQUEST-GATE-END markers.  This test extracts that exact block from each
workflow file and runs it under bash against fixture bodies, so the shipped
text is what is exercised.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REUSABLES = {
    ".github/workflows/pr-autofix-reusable.yml": "@pr-autofix",
    ".github/workflows/claude-code-autofix-reusable.yml": "@claude-code-autofix",
    ".github/workflows/codex-autofix-reusable.yml": "@codex-autofix",
}

BEGIN = "# REQUEST-GATE-BEGIN"
END = "# REQUEST-GATE-END"


def gate_block(path: str) -> str:
    lines = open(os.path.join(ROOT, path), encoding="utf-8").read().splitlines()
    start = next(i for i, line in enumerate(lines) if BEGIN in line)
    end = next(i for i, line in enumerate(lines) if END in line)
    return textwrap.dedent("\n".join(lines[start + 1 : end]))


def run_gate(block: str, tag: str, body: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        body_file = os.path.join(tmp, "body.txt")
        with open(body_file, "w", encoding="utf-8") as fh:
            fh.write(body)
        env = dict(os.environ)
        env.update(
            {
                "GATE_BODY_FILE": body_file,
                "GATE_TAG": tag,
                "RUNNER_TEMP": tmp,
            }
        )
        proc = subprocess.run(
            ["bash", "-c", block + '\necho "verdict=$gate_verdict"'],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip().split("verdict=")[-1]


# (body, expected) - expected applies with each reusable's own tag.
FIXTURES = [
    # Deliberate requests: the tag opens the comment.
    ("{tag} please fix this", "request"),
    ("   {tag} indented opener", "request"),
    ("{TAG} uppercase", "request"),
    ("\n\n  \n{tag} after blank lines", "request"),
    ("> quoting the earlier comment\n{tag} do it", "request"),
    ("```\n{tag} inside a fence\n```\n{tag} the real ask below the fence", "request"),
    # Discussion: the tag is mentioned, not requested.
    ("Verified: the marker strips the suffix and {tag} works now", "discussion"),
    ("Looks good!\n\n{tag}", "discussion"),
    ("```\n{tag} only inside a fence\n```", "discussion"),
    ("> {tag} only inside a quote", "discussion"),
    ("{tag}-2 a different handle", "discussion"),
    ("{tag}er a longer handle", "discussion"),
    ("`{tag}` in inline code mid-sentence", "discussion"),
    ("", "discussion"),
    ("   \n  \n", "discussion"),
]


class RequestGate(unittest.TestCase):
    def test_every_reusable_carries_the_marked_block(self):
        for path in REUSABLES:
            raw = open(os.path.join(ROOT, path), encoding="utf-8").read()
            self.assertIn(BEGIN, raw, path)
            self.assertIn(END, raw, path)
            self.assertIn("gate_verdict", gate_block(path), path)

    def test_fixtures_against_each_reusable(self):
        for path, tag in REUSABLES.items():
            block = gate_block(path)
            for body, expected in FIXTURES:
                with self.subTest(path=path, body=body[:40]):
                    actual = run_gate(
                        block, tag, body.replace("{tag}", tag).replace("{TAG}", tag.upper())
                    )
                    self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
