#!/usr/bin/env python3
"""Tests for the FLEET-PROTOCOL.md checks in scripts/check-fleet-registry.py.

    python3 scripts/test_check_fleet_registry.py

Since the handbook flip, FLEET-PROTOCOL.md starts with a GENERATED comment that names Fleet-OPS.  These tests pin that
the repo and acronym checks read the text under that line, so the header can never stand in for a missing row.
Nothing here reads ~/apps or the network.
"""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "check-fleet-registry.py"


def _load():
    sys.path.insert(0, str(SCRIPTS))
    loader = importlib.machinery.SourceFileLoader("check_fleet_registry", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


reg = _load()

HEADER = (
    "<!-- GENERATED from Simple-With-Us/Fleet-OPS handbook/.  Do not edit here; open a PR to handbook/.  "
    "body-sha256:{sha} -->\n"
)
APPS = [{"repo": "fleet-ops", "acronym": "OPS"}, {"repo": "DealDex", "acronym": "DD"}]
FULL_BODY = "# Protocol\n\nThe fleet-ops repo, OPS, and DealDex (DD) are listed.\n"


class StripHeader(unittest.TestCase):
    def test_strips_only_a_leading_generated_line(self) -> None:
        header = HEADER.format(sha="a" * 64)
        self.assertEqual(reg.strip_generated_header(header + FULL_BODY), FULL_BODY)
        self.assertEqual(reg.strip_generated_header(FULL_BODY), FULL_BODY)
        later = FULL_BODY + header
        self.assertEqual(reg.strip_generated_header(later), later)
        self.assertEqual(
            reg.strip_generated_header("\n" + header + FULL_BODY), "\n" + header + FULL_BODY
        )


class SimpleToolsRows(unittest.TestCase):
    def test_a_headered_copy_with_every_row_passes(self) -> None:
        text = HEADER.format(sha="b" * 64) + FULL_BODY
        self.assertEqual(reg.simple_tools_errors(APPS, text, "coordinator FLEET-PROTOCOL.md"), [])

    def test_the_header_cannot_stand_in_for_a_missing_row(self) -> None:
        # The header holds "Fleet-OPS", so "OPS" and (case-folded) "fleet-ops" are both substrings of it.
        body = "# Protocol\n\nDealDex (DD) only.\n"
        text = HEADER.format(sha="c" * 64) + body
        self.assertIn("OPS", text)
        self.assertEqual(
            reg.simple_tools_errors(APPS, text, "coordinator FLEET-PROTOCOL.md"),
            [
                "coordinator FLEET-PROTOCOL.md missing repo fleet-ops",
                "coordinator FLEET-PROTOCOL.md missing acronym OPS",
            ],
        )
        self.assertEqual(
            reg.simple_tools_errors(APPS, text, "~/apps/FLEET-PROTOCOL.md", fold_repo=True),
            [
                "~/apps/FLEET-PROTOCOL.md missing repo fleet-ops",
                "~/apps/FLEET-PROTOCOL.md missing acronym OPS",
            ],
        )

    def test_live_copy_ignores_repo_case_but_not_acronym_case(self) -> None:
        text = "fleet-ops and ops, DEALDEX and DD\n"
        self.assertEqual(
            reg.simple_tools_errors(APPS, text, "live", fold_repo=True), ["live missing acronym OPS"]
        )
        self.assertEqual(
            reg.simple_tools_errors(APPS, text, "coordinator"),
            ["coordinator missing acronym OPS", "coordinator missing repo DealDex"],
        )

    def test_engine_only_row_check_reads_under_the_header(self) -> None:
        data = {"seats": [{"tag": "OPS"}]}
        row = "**Engine-only CLIs** (Droid)\n"
        self.assertEqual(
            reg.engine_only_errors(data, HEADER.format(sha="d" * 64) + row),
            [],
        )

    def test_active_kimi_passes_only_with_the_exact_legacy_mirror(self) -> None:
        mirror = (SCRIPTS.parent / "FLEET-PROTOCOL.md").read_text()
        data = {"seats": [{"tag": "KIMI", "notesName": "Kimi"}]}
        self.assertEqual(reg.engine_only_errors(data, mirror), [])

    def test_active_kimi_is_rejected_when_a_changed_mirror_still_calls_it_engine_only(self) -> None:
        mirror = (SCRIPTS.parent / "FLEET-PROTOCOL.md").read_text()
        header = reg._GENERATED_BODY_SHA256.match(mirror)
        self.assertIsNotNone(header)
        body = mirror[header.end() :]
        row = re.search(r"\*\*Engine-only CLIs\*\*\s*\(([^)]*)\)", body)
        self.assertIsNotNone(row)
        updated_row = row.group(1) + ", Kimi"
        body = body[: row.start(1)] + updated_row + body[row.end(1) :]
        changed_mirror = HEADER.format(sha=hashlib.sha256(body.encode("utf-8")).hexdigest()) + body

        errors = reg.engine_only_errors(
            {"seats": [{"tag": "KIMI", "notesName": "Kimi"}]}, changed_mirror
        )
        self.assertEqual(len(errors), 1)
        self.assertIn("live seat KIMI (", errors[0])


if __name__ == "__main__":
    unittest.main()
