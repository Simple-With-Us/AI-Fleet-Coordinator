"""AFC-361:  every copy of the session-id contract equals fleet-apps.json `sessionIds`.

The board CLI, apple-notes-coding.sh, agent_sync/session_ids.py and agent-sync-mcp/src/session-ids.js each carry a
copy, because three of them ship without a package.  This test fails when a copy drifts.
Run:  cd scripts && python3 -m unittest test_session_ids_parity
"""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import subprocess
import sys
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
REGISTRY = json.loads((REPO / "fleet-apps.json").read_text(encoding="utf-8"))["sessionIds"]
BOARD = HERE / "linear-board" / "board"
NOTES_SH = HERE / "apple-notes-coding.sh"
JS = HERE / "agent-sync-mcp" / "src" / "session-ids.js"


def _tuples(d):
    return {k: tuple(v) for k, v in d.items()}


class RegistryTests(unittest.TestCase):
    def test_the_block_is_well_formed(self):
        re.compile(REGISTRY["idPattern"])
        self.assertTrue(set(REGISTRY["exemptSeats"]) >= {"ECHO", "WISE", "MA", "JET", "GROK-BOT"})
        self.assertEqual(REGISTRY["exemptPatterns"], ["GB-*"])
        for seat in REGISTRY["envBySeat"]:
            self.assertEqual(seat, seat.upper())
        for seat, names in REGISTRY["envBySeat"].items():
            for name in names:
                self.assertIn(name, REGISTRY["fallbackEnv"], f"{seat}'s {name} is missing from fallbackEnv")
        self.assertFalse(set(REGISTRY["envBySeat"]) & set(REGISTRY["exemptSeats"]),
                         "an exempt seat needs no platform variable entry")

    def test_exempt_seats_are_known_seats(self):
        data = json.loads((REPO / "fleet-apps.json").read_text(encoding="utf-8"))
        known = {s["tag"] for s in data["seats"]} | set(data["seatApps"]["extraSeats"])
        for seat in REGISTRY["exemptSeats"]:
            self.assertIn(seat, known)


class BoardParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        loader = SourceFileLoader("board_cli_for_parity", str(BOARD))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cls.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.mod)

    def test_constants(self):
        m = self.mod
        self.assertEqual(m.SESSION_ID_PATTERN, REGISTRY["idPattern"])
        self.assertEqual(m.SESSION_GENERIC_ENV, REGISTRY["genericEnv"])
        self.assertEqual(sorted(m.SESSION_EXEMPT_SEATS), sorted(REGISTRY["exemptSeats"]))
        self.assertEqual(list(m.SESSION_EXEMPT_PATTERNS), REGISTRY["exemptPatterns"])
        self.assertEqual(_tuples(m.SESSION_ENV_BY_SEAT), _tuples(REGISTRY["envBySeat"]))
        self.assertEqual(list(m.SESSION_FALLBACK_ENV), REGISTRY["fallbackEnv"])


class NotesScriptParity(unittest.TestCase):
    def setUp(self):
        self.text = NOTES_SH.read_text(encoding="utf-8")

    def test_pattern_generic_and_fallback(self):
        self.assertIn(f"SESSION_ID_PATTERN='{REGISTRY['idPattern']}'", self.text)
        self.assertIn(f'SESSION_GENERIC_ENV="{REGISTRY["genericEnv"]}"', self.text)
        self.assertIn(f'SESSION_FALLBACK_ENV="{" ".join(REGISTRY["fallbackEnv"])}"', self.text)

    def test_exempt_case(self):
        m = re.search(r"_session_exempt\(\) \{.*?case \"\$1\" in ([^)]*)\) return 0", self.text, re.S)
        self.assertIsNotNone(m)
        got = sorted(m.group(1).split("|"))
        self.assertEqual(got, sorted(REGISTRY["exemptSeats"] + REGISTRY["exemptPatterns"]))

    def test_env_by_seat(self):
        m = re.search(r"_session_envs\(\) \{.*?esac\n\}", self.text, re.S)
        self.assertIsNotNone(m)
        got = {}
        for seat, names in re.findall(r"^\s*([A-Z]+)\) echo ([A-Z_ ]+) ;;", m.group(0), re.M):
            got[seat] = tuple(names.split())
        self.assertEqual(got, _tuples(REGISTRY["envBySeat"]))


class PythonParity(unittest.TestCase):
    def test_constants(self):
        sys.path.insert(0, str(HERE))
        try:
            from agent_sync import session_ids as S
        finally:
            sys.path.pop(0)
        self.assertEqual(S.ID_PATTERN, REGISTRY["idPattern"])
        self.assertEqual(S.GENERIC_ENV, REGISTRY["genericEnv"])
        self.assertEqual(sorted(S.EXEMPT_SEATS), sorted(REGISTRY["exemptSeats"]))
        self.assertEqual(list(S.EXEMPT_PATTERNS), REGISTRY["exemptPatterns"])
        self.assertEqual(_tuples(S.ENV_BY_SEAT), _tuples(REGISTRY["envBySeat"]))
        self.assertEqual(list(S.FALLBACK_ENV), REGISTRY["fallbackEnv"])


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class JavaScriptParity(unittest.TestCase):
    def test_constants(self):
        code = ("import(process.argv[1]).then(m => console.log(JSON.stringify({p: m.ID_PATTERN, g: m.GENERIC_ENV, "
                "s: m.EXEMPT_SEATS, x: m.EXEMPT_PATTERNS, e: m.ENV_BY_SEAT, f: m.FALLBACK_ENV})))")
        out = subprocess.run(["node", "-e", code, JS.as_uri()], capture_output=True, text=True, timeout=60, check=True)
        got = json.loads(out.stdout)
        self.assertEqual(got["p"], REGISTRY["idPattern"])
        self.assertEqual(got["g"], REGISTRY["genericEnv"])
        self.assertEqual(sorted(got["s"]), sorted(REGISTRY["exemptSeats"]))
        self.assertEqual(got["x"], REGISTRY["exemptPatterns"])
        self.assertEqual(_tuples(got["e"]), _tuples(REGISTRY["envBySeat"]))
        self.assertEqual(got["f"], REGISTRY["fallbackEnv"])


if __name__ == "__main__":
    unittest.main()
