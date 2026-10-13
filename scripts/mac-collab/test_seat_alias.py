#!/usr/bin/env python3
"""Tests for the GROK-BUILD alias in the `board` CLI (owner 2026-10-10).

Run:  python3 scripts/mac-collab/test_seat_alias.py

Any spelling of GROK followed by BUILD signs as GROK in the --by payload fields (--mine is a filter and
stays as typed, so legacy rows stored as GROK-BUILD remain findable);  every
other value is sent exactly as typed (this CLI does not upper-case --by).  The CLI is standalone, so it
carries its own copy of the pattern, and the parity test keeps it equal to the GROK row's aliasPattern
in fleet-apps.json.  The CLI is loaded by path (its top level only builds the SECRETS path, so the import
is safe) and `call` is replaced, so no test reaches the server or reads a token.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
CLI = HERE / "board"
FLEET_APPS = HERE.parents[1] / "fleet-apps.json"

ALIAS_SPELLINGS = ("GROK-BUILD", "Grok-Build", "grok-build", "grok_build", "Grok Build", "GROK.BUILD",
                   "Grok (Build)", "grok/build/", "GrokBuild", "GROK--BUILD!!", "  grok-build  ")
NOT_ALIASES = ("GROKBUILDER", "grok-build-bot", "grok2build", "GROK-WEB", "GROK-BOT", "GB-COMPILER",
               "BF-BUILDER", "Grok (Web/iOS)", "GROK", "", "MONET", "RENOIR", "HARNESS", "MINIMAX",
               "DEEPSEEK", "KIMI", "claude", "Codex")


def load_cli():
    loader = importlib.machinery.SourceFileLoader("board_cli_under_test", str(CLI))
    spec = importlib.util.spec_from_loader("board_cli_under_test", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def run_cli(mod, *argv, reply=None):
    """main() with `call` replaced:  returns (the requests it made, its stdout)."""
    seen = []

    def fake_call(method, path, payload=None, *args, **kwargs):
        seen.append((method, path, payload))
        return reply if reply is not None else {"id": "abcdef0123456789", "status": "in_progress",
                                                "addressed_by": (payload or {}).get("addressed_by")}

    out = io.StringIO()
    with mock.patch.object(mod, "call", fake_call), mock.patch.object(sys, "argv", ["board", *argv]), \
            contextlib.redirect_stdout(out):
        code = mod.main()
    assert code == 0, code
    return seen, out.getvalue()


class CanonicalSeatTests(unittest.TestCase):
    def test_every_grok_build_spelling_is_grok(self):
        cli = load_cli()
        for raw in ALIAS_SPELLINGS:
            self.assertEqual(cli.canonical_seat(raw), "GROK", repr(raw))

    def test_everything_else_is_left_untouched(self):
        cli = load_cli()
        for raw in NOT_ALIASES:
            self.assertEqual(cli.canonical_seat(raw), raw, repr(raw))      # not even upper-cased
        self.assertIsNone(cli.canonical_seat(None))

    def test_pattern_is_the_one_in_fleet_apps_json(self):
        rows = json.loads(FLEET_APPS.read_text())["seats"]
        grok = next(r for r in rows if r["tag"] == "GROK")
        self.assertEqual(load_cli().GROK_ALIAS_PATTERN, grok["aliasPattern"])


class VerbsSignGrokTests(unittest.TestCase):
    def setUp(self):
        self.cli = load_cli()

    def test_claim_status_comment_and_file_sign_grok(self):
        cli = self.cli
        (req,), out = run_cli(cli, "claim", "abcdef01", "--by", "Grok-Build", "--env", "Mac")
        self.assertEqual(req[2]["addressed_by"], "GROK")
        self.assertIn("claimed abcdef01 by GROK", out)
        (req,), _ = run_cli(cli, "status", "abcdef01", "completed", "--by", "GROK_BUILD", "--resolution", "done")
        self.assertEqual(req[2]["addressed_by"], "GROK")
        (req,), out = run_cli(cli, "comment", "abcdef01", "--by", "Grok (Build)", "--text", "hi")
        self.assertEqual(req[2]["author"], "GROK")
        self.assertIn("as GROK", out)
        (req,), _ = run_cli(cli, "file", "--title", "t", "--by", "grok build", reply={"id": "x", "app": "a"})
        self.assertEqual(req[2]["reported_by"], "GROK")

    def test_other_values_are_sent_as_typed(self):
        cli = self.cli
        for by in ("claude", "CODEX", "GROKBUILDER", "grok-build-bot", "GROK-WEB"):
            (req,), _ = run_cli(cli, "claim", "abcdef01", "--by", by)
            self.assertEqual(req[2]["addressed_by"], by)
        (req,), _ = run_cli(cli, "status", "abcdef01", "open")                      # no --by: no field
        self.assertNotIn("addressed_by", req[2])

    def test_mine_is_left_as_typed_so_legacy_grok_build_rows_stay_findable(self):
        rows = {"findings": [{"id": "1" * 32, "addressed_by": "GROK", "title": "a", "status": "open", "app": "x"},
                             {"id": "2" * 32, "addressed_by": "GROK-BUILD", "title": "b", "status": "open", "app": "x"},
                             {"id": "3" * 32, "addressed_by": "CODEX", "title": "c", "status": "open", "app": "x"}],
                "total_matching": 3}
        for raw, titles in (("GROK", ["a"]), ("grok", ["a"]), ("GROK-BUILD", ["b"])):
            _, out = run_cli(self.cli, "list", "--json", "--mine", raw, reply=rows)
            self.assertEqual([r["title"] for r in json.loads(out)], titles, raw)
        _, out = run_cli(self.cli, "list", "--json", "--mine", "codex", reply=rows)
        self.assertEqual([r["title"] for r in json.loads(out)], ["c"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
