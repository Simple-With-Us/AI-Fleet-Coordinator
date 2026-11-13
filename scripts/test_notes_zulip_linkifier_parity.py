"""AFC-562:  the Notes linkifier and the Zulip TARGET name the same projects and shortcuts.

A Linear project has no URL that can be derived from its reference, so it is coded by hand in two
places:  scripts/zulip_linkifiers.py TARGET (one literal rule per project) and the Notes linkifier's
PROJECT_PATHS in scripts/apple-notes-coding.sh (one reference-to-slug line each).  The fleet
shortcuts are likewise coded twice:  TARGET rules 5118 and 5151, and the Notes FL_RE.  A change to
one side without the other would link a project in Zulip but leave it plain text in Notes, or the
other way round, so this test fails until both sides agree:

  every P-* project rule in TARGET is in PROJECT_PATHS with the slug its template names, and
  PROJECT_PATHS names nothing TARGET does not;
  the fl/ and fleetlink/ rules share one path shape in TARGET and in Notes, and both sides build
  the same https URL from the same host.  The Notes side holds that host as a constant and not as
  an override (owner rule:  no environment overrides), so the test reads the constant.

Run:  cd scripts && python3 -m unittest test_notes_zulip_linkifier_parity
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zulip_linkifiers as L

SHELL = Path(__file__).resolve().parent / "apple-notes-coding.sh"
PROJECT_PATTERN = re.compile(r"P-[A-Z]+-[0-9]+\Z")
FLEETLINK_IDS = (5118, 5151)


def shell_text() -> str:
    return SHELL.read_text(encoding="utf-8")


def notes_project_paths(shell: str) -> dict[str, str]:
    """PROJECT_PATHS from the Notes script, as a reference-to-slug dict."""
    start = shell.index("PROJECT_PATHS = {")
    depth = 0
    end = None
    for offset, char in enumerate(shell[start:]):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = start + offset + 1
                break
    if end is None:  # pragma: no cover - the block is always closed
        raise AssertionError("PROJECT_PATHS in apple-notes-coding.sh is never closed")
    return ast.literal_eval(ast.parse(shell[start:end]).body[0].value)


def notes_env_default(shell: str, name: str) -> str:
    """The default of one _env("NAME", "default") in the Notes script."""
    match = re.search(f'_env\\("{re.escape(name)}", "([^"]+)"\\)', shell)
    if match is None:  # pragma: no cover - the names below always exist
        raise AssertionError(f"{name} has no default in apple-notes-coding.sh")
    return match.group(1)


def notes_constant(shell: str, name: str) -> str:
    """The value of one module-level constant (NAME = "value") in the Notes script."""
    match = re.search(rf'^{re.escape(name)} = "([^"]+)"$', shell, re.MULTILINE)
    if match is None:  # pragma: no cover - the names below always exist
        raise AssertionError(f"{name} is not a constant in apple-notes-coding.sh")
    return match.group(1)


def target_projects() -> dict[str, tuple[int, str]]:
    """Every P-* project rule in TARGET, as reference -> (id, template)."""
    return {
        pattern: (rule_id, url)
        for rule_id, pattern, url in L.TARGET
        if PROJECT_PATTERN.match(pattern)
    }


class ProjectParityTest(unittest.TestCase):
    def test_every_target_project_is_coded_in_notes(self):
        projects = target_projects()
        self.assertTrue(projects, "TARGET names no P-* project rule at all")
        paths = notes_project_paths(shell_text())
        missing = sorted(set(projects) - set(paths))
        self.assertFalse(
            missing,
            "these TARGET project rules are missing from the Notes PROJECT_PATHS:  "
            + ", ".join(missing),
        )

    def test_notes_names_no_project_target_does_not(self):
        paths = notes_project_paths(shell_text())
        extra = sorted(set(paths) - set(target_projects()))
        self.assertFalse(
            extra,
            "these Notes PROJECT_PATHS entries have no TARGET rule:  " + ", ".join(extra),
        )

    def test_both_sides_build_the_same_project_url(self):
        shell = shell_text()
        host = notes_env_default(shell, "NOTES_LINKIFY_LINEAR_HOST")
        workspace = notes_env_default(shell, "NOTES_LINEAR_WORKSPACE")
        paths = notes_project_paths(shell)
        for ref, (rule_id, template) in sorted(target_projects().items()):
            with self.subTest(ref=ref):
                if ref not in paths:
                    self.fail(f"TARGET rule {rule_id} ({ref}) is missing from PROJECT_PATHS")
                self.assertEqual(
                    template,
                    f"https://{host}/{workspace}/project/{paths[ref]}",
                    f"TARGET rule {rule_id} and PROJECT_PATHS disagree on {ref}",
                )


class FleetlinkParityTest(unittest.TestCase):
    def target_rules(self) -> dict[int, tuple[str, str]]:
        rows = {rule_id: (pattern, url) for rule_id, pattern, url in L.TARGET}
        for rule_id in FLEETLINK_IDS:
            self.assertIn(rule_id, rows, f"TARGET has no rule {rule_id}")
        return {rule_id: rows[rule_id] for rule_id in FLEETLINK_IDS}

    def test_both_rules_share_one_path_shape_with_notes(self):
        shell = shell_text()
        match = re.search(r'FL_PATH_RE = r"([^"]+)"', shell)
        self.assertIsNotNone(match, "the Notes script has no FL_PATH_RE")
        notes_path = match.group(1)
        for rule_id, (pattern, _) in sorted(self.target_rules().items()):
            with self.subTest(rule=rule_id):
                found = re.fullmatch(r"(fl|fleetlink)/\(\?P<path>(.+)\)", pattern, re.DOTALL)
                self.assertIsNotNone(found, f"TARGET rule {rule_id} is not a path rule")
                self.assertEqual(
                    found.group(2),
                    notes_path,
                    f"TARGET rule {rule_id} and the Notes FL_PATH_RE disagree",
                )

    def test_notes_covers_both_prefixes(self):
        shell = shell_text()
        match = re.search(r"FL_RE = re\.compile\(r\"([^\"]*)\" \+ FL_PATH_RE", shell)
        self.assertIsNotNone(match, "the Notes script has no FL_RE")
        found = re.search(r"\(\?:([^)]*)\)/", match.group(1))
        self.assertIsNotNone(found, "the Notes FL_RE names no prefixes")
        self.assertEqual(
            set(found.group(1).split("|")),
            {pattern.split("/")[0] for _, (pattern, _) in self.target_rules().items()},
            "the Notes FL_RE and TARGET cover different prefixes",
        )

    def test_both_sides_build_the_same_shortcut_url(self):
        shell = shell_text()
        #  the host is a constant on the Notes side (owner rule:  no environment overrides), so the
        #  two sides are compared through the constant and not through a knob default
        host = notes_constant(shell, "FLEETLINK_HOST")
        self.assertIn(
            'FLEETLINK_HOST + "/" + m.group("path")',
            shell,
            "the Notes script no longer builds a fleet shortcut as https://<host>/<path>",
        )
        for rule_id, (_, template) in sorted(self.target_rules().items()):
            with self.subTest(rule=rule_id):
                self.assertEqual(
                    template,
                    f"https://{host}/{{+path}}",
                    f"TARGET rule {rule_id} and the Notes constant host disagree",
                )


if __name__ == "__main__":
    unittest.main()
