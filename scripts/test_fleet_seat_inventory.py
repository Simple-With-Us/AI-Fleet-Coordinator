"""Tests for fleet_seat_inventory:  what each platform has installed, as names and counts."""

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet_seat_inventory as inv

# A made-up value that must never reach an answer, assembled at runtime so no token-shaped
# literal sits in the file.
SENTINEL = "-".join(["not", "a", "real", "value", "must", "not", "appear"])


def write(home, relative, text):
    path = os.path.join(home, relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def mkdirs(home, *relatives):
    for relative in relatives:
        os.makedirs(os.path.join(home, relative), exist_ok=True)


def section(sections, title, source=None):
    for item in sections:
        if item["title"] == title and (source is None or item["source"] == source):
            return item
    raise AssertionError("no section %r in %r" % (title, [s["title"] for s in sections]))


def names(sec):
    return [item["name"] for item in sec["items"]]


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    # MARK: platforms with JSON

    def test_claude_hooks_plugins_skills_agents_and_permission_counts(self):
        write(
            self.home,
            ".claude/settings.json",
            json.dumps(
                {
                    "env": {"API_TOKEN": SENTINEL},
                    "permissions": {"allow": ["Bash(ls:*)", "Read"], "deny": ["Bash(rm:*)"]},
                    "hooks": {
                        "PreToolUse": [
                            {"matcher": "Bash", "hooks": [{"command": "/x/" + SENTINEL}]}
                        ],
                        "Stop": [{}, {}],
                    },
                }
            ),
        )
        write(
            self.home,
            ".claude/plugins/installed_plugins.json",
            json.dumps(
                {"version": 2, "plugins": {"stripe@official": [{}], "vercel@official": [{}]}}
            ),
        )
        mkdirs(self.home, ".claude/skills/board-ops", ".claude/skills/closeout")
        write(self.home, ".claude/agents/code-scout.md", "x")
        write(self.home, ".claude/commands/imark-notes.md", "x")
        sections = inv.read_platform("claude", self.home)

        hooks = section(sections, "Hooks")
        self.assertEqual(names(hooks), ["PreToolUse", "Stop"])
        self.assertEqual([i["detail"] for i in hooks["items"]], ["1 entry", "2 entries"])
        permissions = section(sections, "Permissions")
        self.assertEqual(
            [(i["name"], i["detail"]) for i in permissions["items"]],
            [("allow rules", "2"), ("deny rules", "1")],
        )
        self.assertEqual(
            names(section(sections, "Plugins")), ["stripe@official", "vercel@official"]
        )
        self.assertEqual(names(section(sections, "Skills")), ["board-ops", "closeout"])
        self.assertEqual(names(section(sections, "Sub-Agents")), ["code-scout"])
        self.assertEqual(names(section(sections, "Commands")), ["imark-notes"])

    def test_no_value_from_a_config_file_is_ever_in_the_answer(self):
        write(
            self.home,
            ".claude/settings.json",
            json.dumps(
                {
                    "env": {"API_TOKEN": SENTINEL},
                    "hooks": {
                        "PreToolUse": [
                            {"hooks": [{"command": "curl -H 'Authorization: " + SENTINEL + "'"}]}
                        ]
                    },
                }
            ),
        )
        write(
            self.home,
            ".cursor/mcp.json",
            json.dumps(
                {
                    "mcpServers": {
                        "fleet-tools": {
                            "command": "/bin/run",
                            "args": [SENTINEL],
                            "env": {"T": SENTINEL},
                        },
                        "remote-one": {
                            "url": "https://example.invalid/" + SENTINEL,
                            "headers": {"A": SENTINEL},
                        },
                    }
                }
            ),
        )
        write(self.home, ".grok/config.toml", '[mcp_servers.pagerduty]\ntoken = "%s"\n' % SENTINEL)
        for platform in ("claude", "cursor", "grok"):
            text = json.dumps(inv.inventory(platform, self.home))
            self.assertNotIn(SENTINEL, text, platform)

    def test_cursor_mcp_servers_say_how_they_run_and_nothing_more(self):
        write(
            self.home,
            ".cursor/mcp.json",
            json.dumps(
                {
                    "mcpServers": {
                        "fleet-tools": {"command": "/bin/run", "args": ["a"]},
                        "hosted": {"url": "https://example.invalid/mcp"},
                        "odd": {"note": "no transport keys"},
                    }
                }
            ),
        )
        mcp = section(inv.read_platform("cursor", self.home), "MCP Servers")
        self.assertEqual(
            [(i["name"], i["detail"]) for i in mcp["items"]],
            [("fleet-tools", "local command"), ("hosted", "remote"), ("odd", None)],
        )

    def test_antigravity_hook_groups_show_events_and_whether_each_is_on(self):
        write(
            self.home,
            ".gemini/config/hooks.json",
            json.dumps(
                {
                    "lane-guard": {"PreToolUse": [{}], "enabled": True},
                    "session-letter": {
                        "SessionStart": [{}],
                        "PreInvocation": [{}],
                        "enabled": False,
                    },
                }
            ),
        )
        groups = section(inv.read_platform("antigravity", self.home), "Hook Groups")
        self.assertEqual(
            [(i["name"], i["detail"]) for i in groups["items"]],
            [
                ("lane-guard", "PreToolUse, on"),
                ("session-letter", "PreInvocation, SessionStart, off"),
            ],
        )

    def test_antigravity_reads_mcp_from_both_files_it_keeps_them_in(self):
        write(
            self.home, ".gemini/config/mcp_config.json", json.dumps({"mcpServers": {"coolify": {}}})
        )
        write(self.home, ".gemini/settings.json", json.dumps({"mcpServers": {"composio": {}}}))
        sections = inv.read_platform("antigravity", self.home)
        self.assertEqual(
            names(section(sections, "MCP Servers", "~/.gemini/config/mcp_config.json")), ["coolify"]
        )
        self.assertEqual(
            names(section(sections, "MCP Servers", "~/.gemini/settings.json")), ["composio"]
        )

    def test_zcode_reads_one_config_for_servers_hooks_and_plugins(self):
        write(
            self.home,
            ".zcode/cli/config.json",
            json.dumps(
                {
                    "mcpServers": {"fleet-recall": {"command": "x"}},
                    "hooks": {"events": {"PreToolUse": [{}]}},
                    "plugins": {"enabledPlugins": {"guard": True, "other": False}},
                }
            ),
        )
        sections = inv.read_platform("zcode", self.home)
        self.assertEqual(names(section(sections, "MCP Servers")), ["fleet-recall"])
        self.assertEqual(names(section(sections, "Hooks")), ["PreToolUse"])
        plugins = section(sections, "Plugins")
        self.assertEqual(
            [(i["name"], i["detail"]) for i in plugins["items"]],
            [("guard", "on"), ("other", "off")],
        )

    def test_fx_and_opencode_name_their_mcp_key_differently(self):
        write(self.home, ".fx/mcp.json", json.dumps({"mcp": {"fleet-recall": {"command": "x"}}}))
        write(
            self.home,
            ".config/opencode/opencode.json",
            json.dumps({"mcp": {"simple-tools": {"url": "u"}}}),
        )
        self.assertEqual(
            names(section(inv.read_platform("fx", self.home), "MCP Servers")), ["fleet-recall"]
        )
        self.assertEqual(
            names(section(inv.read_platform("opencode", self.home), "MCP Servers")),
            ["simple-tools"],
        )

    def test_muse_code_reads_the_same_files_as_muse(self):
        write(
            self.home,
            ".config/muse/settings.json",
            json.dumps(
                {
                    "mcpServers": {"coolify": {}},
                    "runtime_capabilities": {"plugin:guard:hook:lane-guard": {}},
                }
            ),
        )
        self.assertEqual(
            inv.read_platform("muse-code", self.home), inv.read_platform("muse", self.home)
        )
        self.assertEqual(
            names(section(inv.read_platform("muse", self.home), "Plugin Hooks")),
            ["plugin:guard:hook:lane-guard"],
        )

    def test_kimi_plugins_are_named_by_their_name_or_id(self):
        write(
            self.home,
            ".kimi-code/plugins/installed.json",
            json.dumps(
                {"version": 1, "plugins": [{"name": "fleet-guard"}, {"id": "other"}, "bare"]}
            ),
        )
        plugins = section(inv.read_platform("kimi", self.home), "Plugins")
        self.assertEqual(names(plugins), ["fleet-guard", "other", "entry 3"])

    def test_grok_hooks_come_from_every_json_file_in_its_hooks_folder(self):
        write(self.home, ".grok/hooks/lane-guard.json", json.dumps({"hooks": {"PreToolUse": [{}]}}))
        write(self.home, ".grok/hooks/lane-guard.json.bak-1", json.dumps({"hooks": {"Stop": [{}]}}))
        write(self.home, ".grok/hooks/broken.json", "{not json")
        hooks = section(inv.read_platform("grok", self.home), "Hooks")
        self.assertEqual(names(hooks), ["PreToolUse (lane-guard.json)"])
        self.assertIn("broken.json:  not valid JSON", hooks["note"])

    # MARK: TOML

    def test_codex_mcp_servers_and_plugins_come_from_toml_table_names(self):
        write(
            self.home,
            ".codex/config.toml",
            "\n".join(
                [
                    'model = "x"',
                    "[mcp_servers.fleet-tools]",
                    'command = "x"',
                    "[mcp_servers.fleet-tools.env]",
                    'T = "%s"' % SENTINEL,
                    '[plugins."documents@openai"]',
                    '[plugins."cloudflare@curated".mcp_servers.cloudflare-api.tools.search]',
                    '[plugins."cloudflare@curated"]',
                    "",
                ]
            ),
        )
        sections = inv.read_platform("codex", self.home)
        self.assertEqual(names(section(sections, "MCP Servers")), ["fleet-tools"])
        self.assertEqual(
            names(section(sections, "Plugins")), ["cloudflare@curated", "documents@openai"]
        )

    def test_toml_headers_split_on_dots_outside_quotes(self):
        self.assertEqual(inv.toml_headers('[a.b."c.d"]\n[e]\n[[f]]\n'), [["a", "b", "c.d"], ["e"]])
        self.assertEqual(inv.toml_headers("[a]\n[[f]]\n[[f]]\n", arrays=True), [["f"], ["f"]])

    def test_kimi_hooks_are_counted_from_array_tables(self):
        write(
            self.home,
            ".kimi-code/config.toml",
            "[[hooks]]\nevent = 'x'\n[[hooks]]\nevent = 'y'\n[thinking]\n",
        )
        hooks = section(inv.read_platform("kimi", self.home), "Hooks")
        self.assertEqual(
            [(i["name"], i["detail"]) for i in hooks["items"]], [("registrations", "2")]
        )

    # MARK: honesty about what could not be read

    def test_an_absent_file_and_folder_say_so_and_are_never_empty_lists(self):
        sections = inv.read_platform("claude", self.home)
        for title in ("Hooks", "Plugins"):
            sec = section(sections, title)
            self.assertEqual(sec["items"], [])
            self.assertEqual(sec["note"], "The file is not on this host.")
        self.assertEqual(section(sections, "Skills")["note"], "The folder is not on this host.")

    def test_a_file_that_is_not_json_says_nothing_was_read(self):
        write(self.home, ".cursor/mcp.json", "{oops")
        sec = section(inv.read_platform("cursor", self.home), "MCP Servers")
        self.assertEqual(sec["note"], "The file is not valid JSON, so nothing was read.")

    def test_a_file_that_lists_none_says_so(self):
        write(self.home, ".cursor/mcp.json", json.dumps({"mcpServers": {}}))
        sec = section(inv.read_platform("cursor", self.home), "MCP Servers")
        self.assertEqual((sec["items"], sec["note"]), ([], "The file lists none."))

    def test_a_file_over_the_size_cap_is_not_read(self):
        write(self.home, ".cursor/mcp.json", " " * (inv.MAX_FILE_BYTES + 1))
        sec = section(inv.read_platform("cursor", self.home), "MCP Servers")
        self.assertIn("larger than this reader reads", sec["note"])

    def test_backup_copies_are_left_out_and_counted(self):
        mkdirs(self.home, ".cursor/rules")
        for name in ("fleet-policy.mdc", "fleet-lane-map.mdc", "fleet-lane-map.mdc.bak-20261102"):
            write(self.home, ".cursor/rules/" + name, "x")
        rules = section(inv.read_platform("cursor", self.home), "Rules")
        self.assertEqual(names(rules), ["fleet-lane-map.mdc", "fleet-policy.mdc"])
        self.assertEqual(rules["note"], "1 backup copy is not listed.")

    def test_real_backup_names_are_backups_and_names_that_only_contain_the_letters_are_not(self):
        # Names seen beside live files on the owner's Mac.
        for backup in (
            "settings.json.bak",
            "config.toml.bak-fleet-rag-20260901203618",
            "hooks.json.bak-lane-guard-20261007-223357",
            "opencode.json.before-own-tools-20261029-2012",
            "worktree-create.sh.disabled-20260705",
            "fleet-lane-map.mdc.bak-lane-map-20261102-033041",
            "x.orig",
            "download.tmp",
        ):
            with self.subTest(backup=backup):
                self.assertTrue(inv.is_backup(backup))
        for kept in (
            "template.tmpl",
            "foo.bakery",
            "origin.md",
            "board-ops",
            "disabled-notes",
            "bak",
        ):
            with self.subTest(kept=kept):
                self.assertFalse(inv.is_backup(kept))

    def test_a_long_list_is_cut_and_says_how_many_there_were(self):
        mkdirs(self.home, ".fx/skills")
        for number in range(inv.MAX_NAMES + 5):
            os.makedirs(os.path.join(self.home, ".fx/skills", "s%04d" % number))
        skills = section(inv.read_platform("fx", self.home), "Skills")
        self.assertEqual(len(skills["items"]), inv.MAX_NAMES)
        self.assertIn(
            "Only the first %d of %d" % (inv.MAX_NAMES, inv.MAX_NAMES + 5), skills["note"]
        )

    def test_a_name_with_control_characters_is_cleaned(self):
        self.assertEqual(inv.clean("a\x1b[31mb\nc"), "a[31mbc")
        self.assertEqual(len(inv.clean("x" * 500)), inv.MAX_NAME_LENGTH)

    def test_clutch_says_its_yaml_is_not_read_instead_of_listing_nothing(self):
        sec = section(inv.read_platform("clutch", self.home), "MCP Servers And Hooks")
        self.assertEqual(sec["items"], [])
        self.assertIn("YAML", sec["note"])

    # MARK: the answer

    def test_an_unknown_platform_is_refused_not_answered_empty(self):
        answer = inv.inventory("nope", self.home)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["sections"], [])

    def test_the_answer_names_the_host_it_was_asked_for_and_otherwise_the_one_it_runs_on(self):
        self.assertEqual(inv.inventory("fx", self.home, host="server")["host"], "server")
        self.assertEqual(inv.inventory("nope", self.home, host="server")["host"], "server")
        self.assertEqual(inv.inventory("fx", self.home)["host"], inv.host_label())

    def test_the_module_stays_readable_by_the_macs_python_3_9(self):
        # `datetime.UTC` is Python 3.11, and the Mac runs this file with its system Python 3.9,
        # so a formatter rewriting the older spelling would break the Mac's reader.
        with open(inv.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("datetime.UTC", source)

    def test_every_known_platform_answers_and_the_answer_is_json(self):
        for platform in inv.PLATFORMS:
            answer = inv.inventory(platform, self.home)
            self.assertTrue(answer["ok"], platform)
            self.assertEqual(json.loads(json.dumps(answer))["platform"], platform)
            self.assertEqual(answer["schemaVersion"], inv.SCHEMA_VERSION)

    def test_the_command_line_takes_one_platform_and_prints_one_json_line(self):
        out = io.StringIO()
        code = inv.main(["--platform", "fx", "--home", self.home, "--json"], out)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["platform"], "fx")
        for bad in (
            [],
            ["--platform", "../etc"],
            ["--platform", "NOPE"],
            ["--platform", "fx", "--x"],
        ):
            out = io.StringIO()
            self.assertEqual(inv.main(bad, out), 2, bad)
            self.assertFalse(json.loads(out.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
