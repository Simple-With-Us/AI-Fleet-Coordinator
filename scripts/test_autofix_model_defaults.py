#!/usr/bin/env python3
"""Tests for the MiniMax model id the autofix bots dispatch.

Owner ruling 2026-10-20: all fleet MiniMax use goes to `MiniMax-M3.1-Flash`.

The autofix bots read their model id from two places.  The trusted policy at
`.github/pr-autofix-trusted.yml` is read from the default branch only and
overrides the reusable workflows' own input defaults, and each reusable
workflow carries a `model` input default for callers that do not supply one.
This test pins all of them to one id, and pins the reference doc to the same
one, so a retired spelling cannot creep back in through any of the four.

Stdlib only, like the rest of this tree: CI runs a bare `setup-python` with
no dependency install, so a test that imported PyYAML would fail there and
pass here.  The parsing below is therefore line-shaped, and it is shaped
around the exact YAML that these four files use.

The rule the retired spelling has to obey: it may appear in a comment, where
it explains why the live id was chosen, and it may never appear as a value.
"""

from __future__ import annotations

import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The one id every fleet MiniMax surface uses.
LIVE_MODEL = "MiniMax-M3.1-Flash"

# Spellings the owner ruling retired.  These may be named in a comment and
# nowhere else.  A comment that names one is a record of why the live id was
# picked; a value that names one is a fleet surface about to dispatch a
# spelling the vendor has already begun removing.
RETIRED_MODEL_IDS = (
    "MiniMax-M3.1-Flash-Preview",
    "MiniMax-M3.1-Flash-Preview-1M",
)

TRUSTED_POLICY = ".github/pr-autofix-trusted.yml"
PR_AUTOFIX_WORKFLOW = ".github/workflows/pr-autofix-reusable.yml"
CLAUDE_AUTOFIX_WORKFLOW = ".github/workflows/claude-code-autofix-reusable.yml"
CODEX_AUTOFIX_WORKFLOW = ".github/workflows/codex-autofix-reusable.yml"
KODY_AUTOFIX_WORKFLOW = ".github/workflows/kody-autofix-reusable.yml"
CLAUDE_CALLER = ".github/workflows/claude-code-autofix.yml"
KODY_CALLER = ".github/workflows/kody-fleet-autofix.yml"
CALLER_WORKFLOWS = (CLAUDE_CALLER, KODY_CALLER)
REFERENCE_DOC = "docs/PR-AUTOFIX.md"

# Every file that carries a model id the autofix bots can dispatch.  The
# caller workflows are policed too (Kody on AFC #805):  a retired id typed
# into a caller's `with: model:` would dispatch it unchanged.
VALUE_CARRYING_FILES = (
    TRUSTED_POLICY,
    PR_AUTOFIX_WORKFLOW,
    CLAUDE_AUTOFIX_WORKFLOW,
    CODEX_AUTOFIX_WORKFLOW,
    KODY_AUTOFIX_WORKFLOW,
    CLAUDE_CALLER,
    KODY_CALLER,
)

# The reusable workflows that carry a `model` input default.
REUSABLE_WORKFLOWS = (
    PR_AUTOFIX_WORKFLOW,
    CLAUDE_AUTOFIX_WORKFLOW,
    CODEX_AUTOFIX_WORKFLOW,
    KODY_AUTOFIX_WORKFLOW,
)


def read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def is_comment(line: str) -> bool:
    return line.lstrip().startswith("#")


def trusted_policy_model(text: str) -> str:
    """The `model:` value in the trusted policy, which is a top-level key."""
    for line in text.splitlines():
        match = re.match(r"^model:\s*(\S+)\s*$", line)
        if match:
            return match.group(1).strip("\"'")
    raise AssertionError("the trusted policy has no top-level model: key")


def workflow_input_default(text: str, input_name: str) -> str:
    """The default of one `workflow_call` input, found by its indentation.

    The input key sits at six spaces and its `default:` at eight, which is
    what all three reusable workflows use.  Scanning for the key and then for
    the first `default:` at the deeper indent keeps this from picking up the
    `default:` of a later input.
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s+)%s:\s*$" % re.escape(input_name), line)
        if not match:
            continue
        key_indent = len(match.group(1))
        for follow in lines[index + 1 : index + 12]:
            deeper = re.match(r"^(\s+)default:\s*(\S+)\s*$", follow)
            if deeper and len(deeper.group(1)) > key_indent:
                return deeper.group(2).strip("\"'")
            if (
                re.match(r"^(\s+)\S+:\s*$", follow)
                and len(follow) - len(follow.lstrip()) <= key_indent
            ):
                break
    raise AssertionError("no %s input found" % input_name)


class AutofixModelDefaults(unittest.TestCase):
    """Every autofix surface names one id, and it is the live one."""

    def test_the_trusted_policy_names_the_live_id(self) -> None:
        self.assertEqual(trusted_policy_model(read(TRUSTED_POLICY)), LIVE_MODEL)

    def test_every_caller_model_input_names_the_live_id(self) -> None:
        # A caller's `with: model:` value dispatches verbatim, so it must be
        # the live id and never a retired one.
        for rel in CALLER_WORKFLOWS:
            with self.subTest(file=rel):
                found = None
                for line in read(rel).splitlines():
                    match = re.match(r"^\s+model:\s*(\S+)\s*$", line)
                    if match:
                        found = match.group(1).strip(chr(34) + chr(39))
                        break
                self.assertIsNotNone(found, "%s passes no model: input" % rel)
                self.assertEqual(found, LIVE_MODEL)

    def test_every_reusable_workflow_default_names_the_live_id(self) -> None:
        for rel in REUSABLE_WORKFLOWS:
            with self.subTest(file=rel):
                self.assertEqual(workflow_input_default(read(rel), "model"), LIVE_MODEL)

    def test_a_retired_id_is_never_a_value(self) -> None:
        """A retired spelling may appear in a comment and nowhere else."""
        for rel in VALUE_CARRYING_FILES:
            for number, line in enumerate(read(rel).splitlines(), start=1):
                for retired in RETIRED_MODEL_IDS:
                    if retired not in line:
                        continue
                    with self.subTest(file=rel, line=number, id=retired):
                        self.assertTrue(
                            is_comment(line),
                            "%s line %d names the retired id %s outside a comment: %r"
                            % (rel, number, retired, line),
                        )

    def test_the_reference_doc_names_the_live_id(self) -> None:
        """The doc a seat reads to set up a repo has to agree with the code."""
        text = read(REFERENCE_DOC)
        self.assertIn("| `model` | `%s` |" % LIVE_MODEL, text)
        self.assertIn("model: %s" % LIVE_MODEL, text)

    def test_the_reference_doc_names_no_retired_id(self) -> None:
        text = read(REFERENCE_DOC)
        for retired in RETIRED_MODEL_IDS:
            with self.subTest(id=retired):
                self.assertNotIn(retired, text)

    def test_the_trusted_policy_and_the_workflows_agree(self) -> None:
        """A trusted policy that disagrees with the workflow default is a
        silent divergence: the policy wins on the default branch, so the
        workflow default becomes dead config that nobody notices is wrong."""
        values = {trusted_policy_model(read(TRUSTED_POLICY))}
        for rel in REUSABLE_WORKFLOWS:
            values.add(workflow_input_default(read(rel), "model"))
        self.assertEqual(values, {LIVE_MODEL})

    def test_the_live_id_is_the_one_the_owner_named(self) -> None:
        """Guards against the constant being edited to something else."""
        self.assertEqual(LIVE_MODEL, "MiniMax-M3.1-Flash")
        self.assertNotIn("Preview", LIVE_MODEL)
        self.assertNotIn("M3.1-Flash-Preview", LIVE_MODEL)


if __name__ == "__main__":
    unittest.main()
