"""The dispatcher's allowed route shapes against the REAL claude/platform registry parsers.

These tests bind operator_registry_dispatch's allowlists to the installed CLIs:  every op the
dispatcher routes must parse in the real registry main, and every op it refuses must not.
A temp registry stands in for the canonical store;  no docker, no network.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest

from simple_tools import claude_letter, operator_registry as OR, platform_letter
from simple_tools.server import operator_registry_dispatch as D


class RouteContractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.db = os.path.join(self.home, ".local", "state", "simple-tools", "operator", "registry.sqlite3")
        os.makedirs(os.path.dirname(self.db))
        OR.SessionRegistry(self.db).close_db()

    def _run_claude(self, argv):
        out = io.StringIO()
        code = claude_letter.registry_main(argv, env={"HOME": self.home}, stdout=out)
        return code, json.loads(out.getvalue())

    def _run_platform(self, argv):
        out = io.StringIO()
        code = platform_letter.registry_main(argv, env={"HOME": self.home}, stdout=out)
        return code, json.loads(out.getvalue())

    def test_every_dispatched_claude_op_parses_in_the_real_parser(self):
        for op in sorted(D.ALLOWED_REGISTRY_OPS):
            _code, answer = self._run_claude([op, "--location", "server",
                                              "--session-id", "sess-0001"])
            self.assertNotIn("bad arguments", answer.get("error", ""), op)

    def test_show_is_not_a_claude_registry_op(self):
        _code, answer = self._run_claude(["show", "--location", "server", "--session-id", "s-1"])
        self.assertIn("bad arguments", answer.get("error", ""),
                      "the dispatcher must not route 'show' to the claude registry parser")

    def test_claude_show_is_a_letter_subcommand_not_a_registry_op(self):
        out = io.StringIO()
        # 'letter show' routes to show(), not registry_main - matching the dispatcher's route.
        code = claude_letter.main(["show", "--session-id", "sess-0001"],
                                  env={"HOME": self.home}, stdout=out)
        self.assertNotIn("bad arguments", out.getvalue())
        self.assertNotEqual(code, 2)

    def test_every_dispatched_platform_op_parses_in_the_real_parser(self):
        for op in sorted(D.ALLOWED_PLATFORM_OPS):
            _code, answer = self._run_platform([op, "--platform", "codex", "--location",
                                                "server", "--session-id", "sess-0001"])
            self.assertNotIn("bad arguments", answer.get("error", ""), op)

    def test_the_platform_parser_requires_platform(self):
        _code, answer = self._run_platform(["touch", "--location", "server",
                                            "--session-id", "sess-0001"])
        self.assertIn("bad arguments", answer.get("error", ""),
                      "the dispatcher's --platform requirement mirrors the real parser")

    def test_platform_show_is_a_real_registry_op(self):
        _code, answer = self._run_platform(["show", "--platform", "codex", "--location",
                                            "server", "--session-id", "sess-0001"])
        self.assertNotIn("bad arguments", answer.get("error", ""))

    def test_every_module_verb_exists_in_the_real_cli(self):
        import inspect
        import re
        source = inspect.getsource(OR.main)
        for verb in sorted(D.ALLOWED_MODULE_VERBS):
            self.assertRegex(source, r'add_parser\(\s*"%s"' % verb, verb)

    def test_no_extra_module_verbs_are_routed(self):
        # Every routed verb must exist. New local export-attempt commands stay
        # deliberately unrouted until their remote argument/retry contract is reviewed.
        import inspect
        import re
        source = inspect.getsource(OR.main)
        real = set(re.findall(r'add_parser\(\s*"([a-z-]+)"', source))
        local_only = {"export-attempts", "record-export-attempt", "record-export-attempts"}
        self.assertEqual(set(D.ALLOWED_MODULE_VERBS), real - local_only)
        self.assertTrue(local_only <= real)
        for verb in local_only:
            self.assertIsNone(D._container_command({}, "isolated", ["operator-registry", verb]))


if __name__ == "__main__":
    unittest.main()
