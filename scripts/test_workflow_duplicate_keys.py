#!/usr/bin/env python3
"""The test job rejects a workflow file that repeats a mapping key.

GitHub registers an unparseable workflow under its file path and runs a red
suite with zero jobs.  A Hash-backed YAML load hides the repeat, so this
test drives the Psych checker (scripts/check-workflow-duplicate-keys.rb).

Stdlib only.  The test job does not install PyYAML.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKER = os.path.join(ROOT, "scripts", "check-workflow-duplicate-keys.rb")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
CODEX_AUTOFIX = os.path.join(WORKFLOWS, "codex-autofix-reusable.yml")
GUARDED_IF = "if: ${{ always() && steps.request_gate.outputs.skip != 'true' }}"


def run(args):
    return subprocess.run(
        ["ruby", CHECKER, *args],
        capture_output=True,
        text=True,
    )


class WorkflowDuplicateKeyTests(unittest.TestCase):
    def test_checker_exists(self):
        self.assertTrue(os.path.isfile(CHECKER))

    def test_live_workflows_have_unique_keys(self):
        result = run([WORKFLOWS])
        self.assertEqual(
            result.returncode,
            0,
            result.stderr or result.stdout,
        )

    def test_repeated_block_key_fails(self):
        text = "steps:\n  - name: Extract\n    if: one\n    id: usage\n    if: two\n"
        self.assertEqual(self._scan(text), 1)

    def test_same_key_in_different_maps_passes(self):
        text = "a:\n  if: one\nb:\n  if: two\n"
        self.assertEqual(self._scan(text), 0)

    def test_block_scalar_is_not_a_key(self):
        text = "run: |\n  if: one\n  if: two\n"
        self.assertEqual(self._scan(text), 0)

    def test_comment_is_not_a_key(self):
        text = "# if: one\nif: two\n"
        self.assertEqual(self._scan(text), 0)

    def test_flow_duplicate_fails(self):
        text = "{a: 1, a: 2}\n"
        self.assertEqual(self._scan(text), 1)

    def test_quoted_duplicate_fails(self):
        text = "\"if\": one\nif: two\n"
        self.assertEqual(self._scan(text), 1)

    def test_extract_usage_counts_keeps_the_request_gate(self):
        with open(CODEX_AUTOFIX, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        start = next(
            index
            for index, line in enumerate(lines)
            if line.strip() == "- name: Extract Usage Counts"
        )
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("      - "):
                break
            block.append(line)
        ifs = [line.strip() for line in block if line.strip().startswith("if:")]
        self.assertEqual(ifs, [GUARDED_IF])

    def _scan(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sample.yml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            result = run([path])
        return result.returncode


if __name__ == "__main__":
    unittest.main()
