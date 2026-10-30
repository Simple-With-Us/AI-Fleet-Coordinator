"""One contract for Apple Notes on three surfaces (AFC-486, docs/protocols/operator-notes.md, "One Tool Set, Three
Surfaces"):  scripts/agent_sync/mcp/notes-tools.json.  This fails when `simple-notes`, the stdio server or the hosted
Worker drifts from it:  every tool has a verb, every verb but `runner` is a tool, every tool argument is an option of
its verb, the stdio server serves exactly these specs, and the Worker imports the same file.

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_notes_parity
"""

from __future__ import annotations

import argparse
import json
import re
import unittest
from pathlib import Path

from agent_sync import cli
from agent_sync import notes as N
from agent_sync.mcp import tools as T

SCRIPTS = Path(__file__).resolve().parents[2]
NOTES_JSON = SCRIPTS / "agent_sync" / "mcp" / "notes-tools.json"
WORKER_NOTES_JS = SCRIPTS / "agent-sync-mcp" / "src" / "notes.js"
CLI_ONLY_VERBS = {"runner"}  # the Mac runner:  the LaunchAgent runs it, no tool does
# The options of the common parent every verb carries (cli._common_parent), and the CLI's own conveniences.
COMMON_DESTS = {"help", "as_seat", "default_seat", "rc", "session", "json"}
CLI_CONVENIENCES = {"no_wait", "body_file", "fleet_bearer_file"}  # --no-wait is --wait 0;  --body-file is one more way to give the body

# tool argument -> how its verb takes it:  ("positional", DEST) or ("option", "--flag").  `markdown` is the BODY
# positional (or --body-file, or standard input), `session` the global --session every verb inherits.
ARGUMENT_MAP = {
    "title": ("positional", "title"),
    "markdown": ("positional", "body"),
    "html": ("option", "--html"),
    "pin": ("option", "--no-pin"),
    "notify": ("option", "--notify"),
    "needs_owner": ("option", "--needs-owner"),
    "summary": ("option", "--summary"),
    "pr": ("option", "--pr"),
    "request_id": ("option", "--request-id"),
    "session": ("option", "--session"),
    "wait_seconds": ("option", "--wait"),
    "format": ("option", "--format"),
    "query": ("option", "--query"),
    "limit": ("option", "--limit"),
    "since_hours": ("option", "--since-hours"),
    "by_seat": ("option", "--by-seat"),
    "action": ("option", "--action"),
}
# Where a verb takes an argument differently:  status names its request positionally, and log's title is a filter.
OVERRIDES = {
    ("notes_status", "request_id"): ("positional", "request_id"),
    ("notes_log", "title"): ("option", "--title"),
}


def notes_tools() -> list[dict]:
    return json.loads(NOTES_JSON.read_text(encoding="utf-8"))["tools"]


def verb_parsers(prog: str = "simple-notes") -> dict[str, argparse.ArgumentParser]:
    parser = cli.build_parser_for(prog)
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("%s has no verbs" % prog)


def find(parser: argparse.ArgumentParser, how: str, name: str) -> argparse.Action | None:
    for action in parser._actions:
        if how == "positional" and not action.option_strings and action.dest == name:
            return action
        if how == "option" and name in action.option_strings:
            return action
    return None


class ParityTests(unittest.TestCase):
    def setUp(self):
        self.tools = notes_tools()
        self.verbs = verb_parsers()

    def test_every_tool_has_a_simple_notes_verb_and_every_verb_but_the_runner_is_a_tool(self):
        names = [tool["name"] for tool in self.tools]
        self.assertEqual(
            names,
            ["notes_create", "notes_update", "notes_pin", "notes_unpin", "notes_read", "notes_list", "notes_status", "notes_log"],
        )
        tool_verbs = set()
        for name in names:
            with self.subTest(name):
                self.assertIn(name, T.CLI_VERBS, "%s has no CLI_VERBS row" % name)
                (verb,) = T.CLI_VERBS[name]
                self.assertIn(verb, self.verbs, "simple-notes has no %s verb" % verb)
                self.assertEqual("notes_" + verb, name)
                tool_verbs.add(verb)
        self.assertEqual(set(self.verbs) - CLI_ONLY_VERBS, tool_verbs)
        self.assertEqual(set(self.verbs), set(cli.NOTES_VERBS))
        self.assertEqual(sorted(name for name in T.CLI_VERBS if name.startswith("notes_")), sorted(names))

    def test_every_tool_argument_is_an_option_of_its_verb(self):
        for tool in self.tools:
            parser = self.verbs[T.CLI_VERBS[tool["name"]][0]]
            for argument in tool["inputSchema"]["properties"]:
                with self.subTest(tool=tool["name"], argument=argument):
                    mapped = OVERRIDES.get((tool["name"], argument)) or ARGUMENT_MAP.get(argument)
                    self.assertIsNotNone(mapped, "argument %s of %s has no simple-notes option in ARGUMENT_MAP" % (argument, tool["name"]))
                    how, name = mapped
                    self.assertIsNotNone(find(parser, how, name), "simple-notes %s has no %s %s" % (T.CLI_VERBS[tool["name"]][0], how, name))

    def test_every_verb_option_is_a_tool_argument(self):
        """The other way round:  an option a verb grows without a tool argument is drift too."""
        for tool in self.tools:
            verb = T.CLI_VERBS[tool["name"]][0]
            arguments = set(tool["inputSchema"]["properties"])
            known = {
                (OVERRIDES.get((tool["name"], argument)) or ARGUMENT_MAP.get(argument) or ("", ""))[1]
                for argument in arguments
            }
            for action in self.verbs[verb]._actions:
                if action.dest in COMMON_DESTS | CLI_CONVENIENCES or action.dest == "as_seat":
                    continue  # --identity is the tools' `identity`, which the router adds to every tool
                names = set(action.option_strings) or {action.dest}
                with self.subTest(verb=verb, option=sorted(names)[0]):
                    self.assertTrue(names & known, "simple-notes %s %s is not an argument of %s" % (verb, sorted(names)[0], tool["name"]))

    def test_choices_and_bounds_agree(self):
        props = {tool["name"]: tool["inputSchema"]["properties"] for tool in self.tools}
        self.assertEqual(list(find(self.verbs["read"], "option", "--format").choices), props["notes_read"]["format"]["enum"])
        self.assertEqual(list(N.FORMATS), props["notes_read"]["format"]["enum"])
        self.assertEqual(list(find(self.verbs["log"], "option", "--action").choices), props["notes_log"]["action"]["enum"])
        self.assertEqual(list(N.ACTIONS), props["notes_log"]["action"]["enum"])
        self.assertEqual((props["notes_list"]["limit"]["maximum"], props["notes_list"]["limit"]["default"]), (N.LIST_MAX, N.LIST_DEFAULT))
        self.assertEqual(props["notes_list"]["query"]["maxLength"], N.QUERY_MAX)
        self.assertEqual(props["notes_log"]["since_hours"]["maximum"], N.LOG_SINCE_MAX_HOURS)
        for name in ("notes_create", "notes_update"):
            self.assertEqual(props[name]["markdown"]["maxLength"], N.MARKDOWN_MAX)
            self.assertEqual(props[name]["summary"]["maxLength"], N.SUMMARY_MAX)
            self.assertEqual(props[name]["pr"]["pattern"], N._PR_RE.pattern)
        for tool in self.tools:
            if "title" in tool["inputSchema"]["properties"] and tool["name"] != "notes_log":
                self.assertEqual(tool["inputSchema"]["properties"]["title"]["maxLength"], N.TITLE_MAX, tool["name"])
            if "request_id" in tool["inputSchema"]["properties"]:
                self.assertEqual(tool["inputSchema"]["properties"]["request_id"]["pattern"], N.REQUEST_ID_RE.pattern, tool["name"])
            if "wait_seconds" in tool["inputSchema"]["properties"]:
                self.assertEqual(tool["inputSchema"]["properties"]["wait_seconds"]["maximum"], 300)
        # The stdio server answers one call at a time:  a short default, and wait_seconds may raise it to the most.
        self.assertEqual((T.Tools.NOTES_WAIT_DEFAULT, T.Tools.NOTES_WAIT_MAX), (25, 90))
        from agent_sync import notes_cli

        self.assertEqual(notes_cli.CLI_WAIT_DEFAULT, 300)

    def test_the_stdio_server_serves_exactly_these_specs_and_handles_each(self):
        served = [tool for tool in T.load_served_tools()["tools"] if tool["name"].startswith("notes_")]
        self.assertEqual(served, self.tools)
        for tool in self.tools:
            with self.subTest(tool["name"]):
                self.assertTrue(callable(getattr(T.Tools, "_tool_" + tool["name"], None)), "no handler")
                read_only = bool(tool["annotations"].get("readOnlyHint"))
                self.assertEqual(tool["name"] in T.READ_TOOLS, read_only)
                self.assertEqual(tool["name"] in T.WRITE_TOOLS, not read_only)
                self.assertNotIn("transport", tool["inputSchema"]["properties"], "the relay transport is gone")
                self.assertFalse(tool["inputSchema"].get("additionalProperties", True))

    def test_the_hosted_worker_imports_the_same_contract(self):
        source = WORKER_NOTES_JS.read_text(encoding="utf-8")
        self.assertRegex(
            source,
            re.compile(r'^import\s+\w+\s+from\s+"\.\./\.\./agent_sync/mcp/notes-tools\.json"\s+with\s+\{\s*type:\s*"json"\s*\};', re.M),
        )
        self.assertEqual(
            (WORKER_NOTES_JS.parent / "../../agent_sync/mcp/notes-tools.json").resolve(), NOTES_JSON.resolve()
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
