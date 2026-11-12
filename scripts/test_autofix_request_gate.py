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


def run_gate(block: str, tag: str, body: str, requester: str = "kody-ai") -> tuple[str, str]:
    """Run the extracted block; return (verdict, gate_skip)."""
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
                "requester": requester,
            }
        )
        proc = subprocess.run(
            ["bash", "-c", block + '\necho "verdict=$gate_verdict"\necho "skip=$gate_skip"'],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        out = dict(line.split("=", 1) for line in proc.stdout.strip().splitlines() if "=" in line)
        return out["verdict"], out["skip"]


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
    ("~~~\n{tag} inside a tilde fence\n~~~", "discussion"),
    ("~~~\n{tag} tilde-fenced\n~~~\n{tag} the real ask below", "request"),
    ("~~~\n```\n{tag} backtick fence nested in a tilde fence\n```\n~~~", "discussion"),
    ("```\n{tag} unbalanced fence swallows the rest (fail-closed)", "discussion"),
    ("", "discussion"),
    ("   \n  \n", "discussion"),
    # Fence hygiene: info strings open fences, and a closer must be a pure run
    # of the opener's marker at least as long as the opener.
    ("```python\n{tag} fenced behind an info string\n```", "discussion"),
    ("~~~bash\n{tag} tilde-fenced behind an info string\n~~~", "discussion"),
    (
        "````\n{tag} in a four-backtick fence\n``` shorter closer does not close\n{tag} still hidden\n````",
        "discussion",
    ),
    ("```\n{tag} hidden\n``` not a closer with trailing text\n{tag} still hidden", "discussion"),
    ("````\n{tag} hidden\n````\n{tag} the real ask below the long fence", "request"),
]

# The verdict drives the skip:  request runs, everything else skips - unless
# the requester is Sentry, which the owner exempted from the tag entirely.
SENTRY_FIXTURES = [
    ("{tag} please fix this", "request", "false"),
    ("no tag anywhere in this body", "discussion", "false"),
    ("", "discussion", "false"),
]


class RequestGate(unittest.TestCase):
    def test_every_reusable_carries_the_marked_block(self):
        for path in REUSABLES:
            raw = open(os.path.join(ROOT, path), encoding="utf-8").read()
            self.assertIn(BEGIN, raw, path)
            self.assertIn(END, raw, path)
            self.assertIn("gate_verdict", gate_block(path), path)
            self.assertIn("gate_skip", gate_block(path), path)

    def test_fixtures_against_each_reusable(self):
        for path, tag in REUSABLES.items():
            block = gate_block(path)
            for body, expected in FIXTURES:
                with self.subTest(path=path, body=body[:40]):
                    verdict, skip = run_gate(
                        block, tag, body.replace("{tag}", tag).replace("{TAG}", tag.upper())
                    )
                    self.assertEqual(verdict, expected)
                    # Polarity guard:  a bot requester is skipped exactly when
                    # the verdict is not a request.
                    self.assertEqual(skip, "false" if expected == "request" else "true")

    def test_sentry_is_exempt_from_the_tag(self):
        for path, tag in REUSABLES.items():
            block = gate_block(path)
            for body, expected, expected_skip in SENTRY_FIXTURES:
                with self.subTest(path=path, body=body[:40]):
                    verdict, skip = run_gate(
                        block, tag, body.replace("{tag}", tag), requester="sentry"
                    )
                    self.assertEqual(verdict, expected)
                    self.assertEqual(skip, expected_skip)


if __name__ == "__main__":
    unittest.main()
