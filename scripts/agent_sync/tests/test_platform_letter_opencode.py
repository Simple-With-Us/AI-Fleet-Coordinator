"""OpenCode on `platform-letter` (AFC-564): the adapter's spec, the hook flows the plugin drives, the installer for the
`plugins` array of opencode.json, and the plugin's own node tests (and one run of the plugin against the real hook).
The hook runs in process against a temporary registry in the double (pair) letter space, with the ssh leg faked by the
core tests' FakeSsh.  No test depends on which pair the registry hands out first, only on the letter being a pair that
the Operator, the note and the title agree on; the one adjacency test needs a registry that takes a parent."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_sync import operator_registry as OR
from agent_sync import platform_letter as PL
from agent_sync import platform_letter_opencode as OC
from agent_sync.tests import test_platform_letter as T

SCRIPTS = Path(__file__).resolve().parents[2]
PLUGIN = SCRIPTS / "agent_sync" / OC.PLUGIN_DIRNAME
ENTRY = SCRIPTS / "platform-letter"
PAIR = r"^[A-Z]{2}$"
SID = "ses_test_0001"
HAS_PARENT = PL._accepts(OR.register_session, "parent_session_id")


class SpecTests(unittest.TestCase):
    def test_the_adapter_is_found_by_the_library(self) -> None:
        self.assertIs(PL.load_spec("opencode"), OC.SPEC)
        self.assertEqual((OC.SPEC.platform, OC.SPEC.seat, OC.SPEC.source), ("opencode", "OPENCODE", "opencode-letter-plugin"))
        self.assertEqual(OC.SPEC.off_variable, "OPENCODE_LETTER")
        self.assertEqual((OC.SPEC.output, OC.SPEC.title_in_output), ("json", True))
        self.assertEqual(OC.SPEC.locations, PL.LOCATIONS)  # OpenCode runs on both hosts

    def test_a_chat_with_a_parent_is_a_sub_thread(self) -> None:
        self.assertEqual(OC.SPEC.kind({"session_id": SID}, {}), "chat")
        self.assertEqual(OC.SPEC.kind({"session_id": SID, "parent_session_id": " "}, {}), "chat")
        self.assertEqual(OC.SPEC.kind({"session_id": SID, "parent_session_id": "ses_p"}, {}), "subagent")
        self.assertEqual(OC.SPEC.parent_id({"parent_session_id": "ses_p"}, {}), "ses_p")
        self.assertTrue(PL.in_scope(OC.SPEC, {}, {"parent_session_id": "ses_p"}))

    def test_the_names_opencode_gives_an_unnamed_chat_are_no_title(self) -> None:
        for name in (
            "New session - 2026-10-29T12:34:56.789Z",
            "Child session - 2026-10-29T12:34:56.789Z",
            "New session",
            "",
            "  ",
        ):
            with self.subTest(name=name):
                self.assertEqual(OC.opencode_title({"title": name}), "")
        for name in ("Fix Login", "AB: Fix Login", "New session for billing", "Child session notes"):
            with self.subTest(name=name):
                self.assertEqual(OC.opencode_title({"title": name}), name)
        self.assertEqual(OC.opencode_title({}), "")

    def test_a_launcher_run_and_the_off_switches_take_no_letter(self) -> None:
        for env in ({"AGENT_LAUNCHER": "botfleet"}, {"AGENT_LAUNCH_SEAT": "BF"}, {"SESSION_LETTER": "0"}, {"OPENCODE_LETTER": "0"}):
            with self.subTest(env=env):
                self.assertFalse(PL.in_scope(OC.SPEC, env, {}))
        self.assertTrue(PL.in_scope(OC.SPEC, {"CODEX_LETTER": "0"}, {}))

    def test_a_title_with_the_no_letter_mark_is_a_headless_run_and_takes_no_letter(self) -> None:
        # The spec's `kind` answered chat or subagent only, so the library's rule that a headless run takes no letter
        # was never applied to OpenCode: `opencode run` in the shared service is told only by the title it was given.
        for title in ("[no-letter] pr-sweep", "  [No-Letter] x", "[no-letter]"):
            with self.subTest(title=title):
                self.assertEqual(OC.SPEC.kind({"session_id": SID, "title": title}, {}), "headless")
                self.assertFalse(PL.in_scope(OC.SPEC, {}, {"session_id": SID, "title": title}))
                # A sub-thread of one is as silent as the chat itself.
                self.assertFalse(PL.in_scope(OC.SPEC, {}, {"session_id": SID, "title": title, "parent_session_id": "ses_p"}))
        for title in ("Fix Login", "The [no-letter] mark", "AB: [no-letter] x", "", None):
            with self.subTest(title=title):
                self.assertEqual(OC.SPEC.kind({"session_id": SID, "title": title}, {}), "chat")
        self.assertTrue(PL.in_scope(OC.SPEC, {}, {"session_id": SID, "title": "Fix Login"}))

    def test_the_plugin_and_the_adapter_use_one_mark(self) -> None:
        source = (PLUGIN / "index.js").read_text(encoding="utf-8")
        found = re.search(r'export const NO_LETTER_MARK = "([^"]+)"', source)
        self.assertIsNotNone(found)
        self.assertEqual(found.group(1), OC.NO_LETTER_MARK)

    def test_the_sweeps_opencode_workers_carry_the_mark(self) -> None:
        """pr-sweep runs `opencode run <prompt>` per PR in the shared service, where no environment variable reaches the
        plugin, so the worker's title is its opt-out; without it each worker would take a pair."""
        platforms = json.loads((SCRIPTS / "pr_sweep" / "platforms.json").read_text(encoding="utf-8"))
        argv = platforms["platforms"]["opencode"]["argv"]
        self.assertEqual(argv[:2], ["opencode", "run"])
        self.assertIn("{prompt}", argv)
        title = argv[argv.index("--title") + 1]
        self.assertTrue(OC.no_letter(title), title)
        self.assertEqual(OC.SPEC.kind({"session_id": SID, "title": title}, {}), "headless")


class HookBase(T.Base):
    def setUp(self) -> None:
        super().setUp()
        self.spec = OC.SPEC

    def fire(self, payload: dict, *, env: dict | None = None, runner=None, now: float | None = None,
             platform: str = "linux", **flags) -> dict | None:
        """One event the plugin would send; the hook's answer line, or None for no output."""
        out = io.StringIO()
        code = PL.hook(
            OC.SPEC, env if env is not None else self.server_env(), io.StringIO(json.dumps(payload)), out,
            runner=runner or T.FakeSsh(self), now=now, platform=platform, **flags,
        )
        self.assertEqual(code, 0)
        text = out.getvalue().strip()
        return json.loads(text)["platform_letter"] if text else None

    def start(self, sid: str = SID, **extra) -> dict | None:
        return self.fire(dict({"event": "start", "session_id": sid, "source": "startup"}, **extra))

    def prompt(self, sid: str, words: str, **extra) -> dict | None:
        return self.fire(dict({"event": "prompt", "session_id": sid, "prompt": words}, **extra))

    def end(self, sid: str, reason: str, **kw) -> dict | None:
        return self.fire({"event": "end", "session_id": sid, "reason": reason}, **kw)

    def note(self, sid: str = SID) -> dict:
        with open(os.path.join(self.state, "OPENCODE", "letters", sid + ".json")) as fh:
            return json.load(fh)

    def row(self, sid: str = SID) -> dict | None:
        return self.lookup(sid, platform="opencode")

    def source_of(self, sid: str = SID) -> str:
        conn = sqlite3.connect(self.db)
        try:
            return conn.execute("SELECT source FROM sessions WHERE native_session_id = ?", (sid,)).fetchone()[0]
        finally:
            conn.close()


class HookTests(HookBase):
    def test_a_new_chat_gets_a_letter_and_the_title_the_plugin_writes(self) -> None:
        out = self.start()
        label = out["label"]
        self.assertRegex(label, PAIR)
        self.assertEqual((out["event"], out["platform"], out["title"]), ("start", "opencode", "%s: New Chat" % label))
        self.assertIn("OpenCode %s" % label, out["context"])
        row = self.row()
        self.assertEqual((row["label"], row["seat"], row["state"]), (label, "OPENCODE", "active"))
        self.assertEqual(self.source_of(), "opencode-letter-plugin")
        self.assertEqual(self.note()["label"], label)

    def test_the_first_prompt_names_the_chat_and_a_later_one_costs_no_hop(self) -> None:
        label = self.start()["label"]
        ssh = T.FakeSsh(self)
        out = self.fire({"event": "prompt", "session_id": SID, "title": "%s: New Chat" % label,
                         "prompt": "fix the login page for the billing app"}, runner=ssh)
        self.assertEqual(out["title"], "%s: Fix the Login Page for the Billing" % label)
        self.assertEqual(ssh.ops(), ["title"])
        self.assertEqual(self.row()["title"], "Fix the Login Page for the Billing")
        again = self.fire({"event": "prompt", "session_id": SID, "title": out["title"], "prompt": "and the logout"}, runner=ssh)
        self.assertIsNone(again)
        self.assertEqual(ssh.ops(), ["title"])

    def test_a_chat_that_already_has_a_name_keeps_its_words_under_the_letter(self) -> None:
        out = self.start(title="Billing Audit")
        self.assertEqual(out["title"], "%s: Billing Audit" % out["label"])
        self.assertEqual(self.row()["title"], "Billing Audit")

    def test_opencodes_default_title_is_not_a_name(self) -> None:
        out = self.start(title="New session - 2026-10-29T12:34:56.789Z")
        self.assertEqual(out["title"], "%s: New Chat" % out["label"])
        self.assertEqual(self.row()["title"] or "", "")

    def test_a_rename_that_dropped_the_letter_gets_it_back_and_the_operator_the_words(self) -> None:
        label = self.start()["label"]
        out = self.fire({"event": "rename", "session_id": SID, "title": "Billing Audit"})
        self.assertEqual((out["event"], out["title"]), ("rename", "%s: Billing Audit" % label))
        self.assertEqual(self.row()["title"], "Billing Audit")
        # The plugin sends the title it wrote back as a rename too (the echo); nothing is said twice.
        self.assertIsNone(self.fire({"event": "rename", "session_id": SID, "title": "%s: Billing Audit" % label}))

    def test_archive_closes_the_registration_and_unarchive_registers_the_chat_again(self) -> None:
        label = self.start()["label"]
        self.assertIsNone(self.end(SID, "archived"))
        self.assertEqual(self.row()["state"], "closed")
        self.assertEqual(self.note()["state"], "closed")
        out = self.start(source="resume")
        self.assertRegex(out["label"], PAIR)
        self.assertEqual(self.row()["state"], "active")
        self.assertEqual(self.note()["state"], "open")
        self.assertEqual(out["label"], label)  # nothing took the pair while the chat was archived

    def test_a_deleted_chat_is_closed_and_an_end_the_plugin_does_not_send_is_not(self) -> None:
        self.start()
        self.end(SID, "something-else")
        self.assertEqual(self.row()["state"], "active")
        self.end(SID, "deleted")
        self.assertEqual(self.row()["state"], "closed")
        self.assertIsNone(self.end("ses_never_seen", "deleted"))  # a chat the hook never saw has nothing to close

    @unittest.skipUnless(HAS_PARENT, "this registry has no parent")
    def test_a_sub_chat_takes_the_pair_after_its_parent(self) -> None:
        parent = self.start("ses_parent_01")["label"]
        child = self.start("ses_child_001", parent_session_id="ses_parent_01")["label"]
        self.assertEqual(parent[0], child[0])
        self.assertNotEqual(parent, child)
        self.assertEqual(self.note("ses_child_001")["parent"], "ses_parent_01")

    def test_a_sub_chat_without_a_registered_parent_still_gets_a_pair(self) -> None:
        out = self.start("ses_child_002", parent_session_id="ses_nobody_01")
        self.assertRegex(out["label"], PAIR)

    def test_a_launcher_run_takes_no_letter(self) -> None:
        ssh = T.FakeSsh(self)
        self.assertIsNone(self.fire({"event": "start", "session_id": SID}, env=self.server_env(AGENT_LAUNCHER="botfleet"), runner=ssh))
        self.assertEqual(ssh.calls, [])
        self.assertIsNone(self.row())

    def test_a_headless_run_takes_no_letter_whichever_event_the_plugin_sends(self) -> None:
        ssh = T.FakeSsh(self)
        mark = "[no-letter] pr-sweep"
        for payload in (
            {"event": "start", "session_id": SID, "source": "startup", "title": mark},
            {"event": "prompt", "session_id": SID, "prompt": "review the pr", "title": mark},
            {"event": "rename", "session_id": SID, "title": mark},
        ):
            with self.subTest(event=payload["event"]):
                self.assertIsNone(self.fire(payload, runner=ssh))
        self.assertEqual(ssh.calls, [])
        self.assertIsNone(self.row())

    def test_the_other_platforms_keep_their_own_namespace(self) -> None:
        self.register("s-codex-0001")  # a Codex chat holds the first pair of its own platform
        out = self.start()
        self.assertRegex(out["label"], PAIR)
        self.assertEqual(self.lookup("s-codex-0001")["label"], out["label"])  # AA and AA, two chats
        self.assertEqual(self.row()["seat"], "OPENCODE")

    def test_on_the_mac_the_registry_is_reached_in_process(self) -> None:
        def no_ssh(*a, **k):
            raise AssertionError("the Mac does not ssh")

        out = self.fire({"event": "start", "session_id": SID}, env=self.env(), runner=no_ssh, platform="darwin")
        self.assertRegex(out["label"], PAIR)
        self.assertEqual(self.note()["location"], "mac")

    def test_a_host_that_is_not_the_mac_and_names_no_location_takes_no_letter(self) -> None:
        out = self.fire({"event": "start", "session_id": SID}, env=self.env(), platform="linux")
        self.assertNotIn("label", out)
        self.assertIn("No Operator letter", out["context"])

    def test_a_secret_in_a_title_never_reaches_the_operator(self) -> None:
        """The title a chat already carries stays the chat's own (the plugin only puts the letter before it); what is
        relayed to the Operator is scanned first, and so is a title made from a prompt."""
        token = T.secret_word()
        self.start(title="Rotate %s Today" % token)
        self.assertNotIn(token, json.dumps(self.row()))
        self.start("ses_test_0002")
        out = self.prompt("ses_test_0002", "export it as %s please" % token)
        self.assertNotIn(token, json.dumps(out))
        self.assertNotIn(token, json.dumps(self.lookup("ses_test_0002", platform="opencode")))

    def test_the_hook_through_the_command_line_entry(self) -> None:
        """`platform-letter hook --platform opencode` as the plugin runs it: stdin in, one JSON line out."""
        out = io.StringIO()
        code = PL.main(
            ["hook", "--platform", "opencode", "--location", "server", "--remote", "mac"],
            env=self.env(), stdin=io.StringIO(json.dumps({"event": "start", "session_id": SID})), stdout=out,
            runner=T.FakeSsh(self),
        )
        self.assertEqual(code, 0)
        self.assertRegex(json.loads(out.getvalue())["platform_letter"]["label"], PAIR)


class InstallBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.cfg_dir = os.path.join(self.home, ".config", "opencode")
        os.makedirs(self.cfg_dir)
        self.cfg = os.path.join(self.cfg_dir, "opencode.json")
        # A checkout's plugin directory, outside any lane, for the installer to name.
        self.plugin = os.path.join(self.tmp.name, "runtime", "scripts", "agent_sync", OC.PLUGIN_DIRNAME)
        os.makedirs(self.plugin)
        Path(self.plugin, "index.js").write_text("export default {};\n")
        self.env = {"HOME": self.home, OC.ENV_PLUGIN_DIR: self.plugin}

    def write(self, doc, raw: str | None = None, mode: int = 0o600) -> None:
        text = raw if raw is not None else json.dumps(doc, indent=2) + "\n"
        with open(self.cfg, "w") as fh:
            fh.write(text)
        os.chmod(self.cfg, mode)

    def read(self) -> dict:
        with open(self.cfg) as fh:
            return json.load(fh)

    def run_install(self, *argv: str, env: dict | None = None) -> tuple[int, dict]:
        out = io.StringIO()
        code = PL.main(["install", "--platform", "opencode", *argv], env=env or self.env, stdout=out)
        return code, json.loads(out.getvalue())

    def backups(self) -> list[str]:
        return sorted(n for n in os.listdir(self.cfg_dir) if ".bak-letter-" in n)


class InstallTests(InstallBase):
    def test_the_mac_file_with_no_plugins_array_gets_one_and_keeps_its_other_key(self) -> None:
        original = {"server": {"hostname": "127.0.0.1", "port": 4096}}
        self.write(original, mode=0o640)
        code, answer = self.run_install()
        self.assertEqual(code, 0)
        self.assertEqual(answer["changed"], ["plugins"])
        self.assertEqual(self.read(), dict(original, plugins=[self.plugin]))  # a bare path: the Mac needs no options
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o640)  # the file keeps its mode
        (name,) = self.backups()
        backup = os.path.join(self.cfg_dir, name)
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o600)
        with open(backup) as fh:
            self.assertEqual(json.load(fh), original)
        self.assertNotIn("hostname", json.dumps(answer))  # no other key is ever printed

    def test_a_second_run_changes_nothing_and_a_check_says_so(self) -> None:
        self.write({"server": {}})
        self.run_install()
        before = Path(self.cfg).read_bytes()
        code, answer = self.run_install()
        self.assertEqual((code, answer["changed"]), (0, []))
        self.assertEqual(Path(self.cfg).read_bytes(), before)
        self.assertEqual(len(self.backups()), 1)
        code, answer = self.run_install("--check")
        self.assertEqual(code, 0)
        self.assertEqual((answer["installed"], answer["current"]), ({"plugins": True}, {"plugins": True}))

    def test_a_check_before_the_install_says_it_is_not_there_and_writes_nothing(self) -> None:
        self.write({"server": {}})
        before = Path(self.cfg).read_bytes()
        code, answer = self.run_install("--check")
        self.assertEqual((code, answer["installed"]), (1, {"plugins": False}))
        self.assertEqual(Path(self.cfg).read_bytes(), before)
        self.assertEqual(self.backups(), [])

    def test_the_server_entry_carries_its_location_and_registry_host_and_keeps_them(self) -> None:
        self.write({})
        code, answer = self.run_install("--location", "server", "--remote", "mac")
        self.assertEqual(code, 0)
        self.assertEqual(self.read()["plugins"], [{"package": self.plugin, "options": {"location": "server", "remote": "mac"}}])
        # Run again without them: the ones installed stay.
        self.assertEqual(self.run_install()[1]["changed"], [])
        self.assertEqual(self.read()["plugins"][0]["options"], {"location": "server", "remote": "mac"})
        # A new registry host replaces the old one, in place.
        self.run_install("--remote", "macbook")
        self.assertEqual(self.read()["plugins"][0]["options"], {"location": "server", "remote": "macbook"})
        self.assertEqual(len(self.read()["plugins"]), 1)

    def test_options_set_by_hand_survive_a_refresh_and_a_check_calls_the_entry_current(self) -> None:
        # The plugin's `log` and `command` have no flag; a refresh used to rebuild the entry from location and remote only,
        # which dropped them with no warning, and `--check` called such an entry not current.
        hand = {"location": "server", "remote": "mac", "log": "/tmp/letters.log", "command": "/x/platform-letter"}
        self.write({"plugins": [{"package": self.plugin, "options": dict(hand)}]})
        code, answer = self.run_install("--check")
        self.assertEqual((code, answer["installed"], answer["current"]), (0, {"plugins": True}, {"plugins": True}))
        before = Path(self.cfg).read_bytes()
        code, answer = self.run_install()
        self.assertEqual((code, answer["changed"]), (0, []))
        self.assertEqual(Path(self.cfg).read_bytes(), before)
        self.assertEqual(self.backups(), [])
        # A check against another registry host still says the entry is not what was asked for.
        self.assertEqual(self.run_install("--check", "--remote", "other")[0], 1)
        # An outdated entry is renewed where it stands with the hand-set options, and a new registry host replaces the old.
        stale = "/old/home/.local/share/fleet-cli/current/scripts/agent_sync/" + OC.PLUGIN_DIRNAME
        self.write({"plugins": ["/a", {"package": stale, "options": dict(hand)}]})
        code, answer = self.run_install("--remote", "macbook")
        self.assertEqual((code, answer["action"]), (0, "updated"))
        self.assertEqual(
            self.read()["plugins"],
            ["/a", {"package": self.plugin, "options": dict(hand, remote="macbook")}],
        )
        # The `[path, options]` form is renewed as the object form with its options.
        self.write({"plugins": [[stale, {"log": "/tmp/l2"}]]})
        self.run_install("--location", "server", "--remote", "mac")
        self.assertEqual(
            self.read()["plugins"],
            [{"package": self.plugin, "options": {"log": "/tmp/l2", "location": "server", "remote": "mac"}}],
        )

    def test_other_plugins_stay_where_they_are_and_ours_goes_last(self) -> None:
        lane = "/mnt/x/lane-tools/opencode-plugin"
        other = {"package": "@someone/plugin", "options": {"a": 1}}
        self.write({"plugins": [lane, other, ["/p/q", {"b": 2}]], "model": "m"})
        self.run_install()
        self.assertEqual(self.read()["plugins"], [lane, other, ["/p/q", {"b": 2}], self.plugin])
        self.assertEqual(list(self.read()), ["plugins", "model"])  # key order is the owner's

    def test_a_plugin_of_ours_from_another_release_or_home_is_replaced_where_it_stands(self) -> None:
        stale = "/old/home/.local/share/fleet-cli/current/scripts/agent_sync/" + OC.PLUGIN_DIRNAME
        self.write({"plugins": ["/a", stale, "/b", {"package": stale + "/", "options": {"remote": "mac"}}]})
        code, answer = self.run_install()
        self.assertEqual((code, answer["action"]), (0, "updated"))
        # The first of ours is renewed where it stands; the duplicate further down is taken out.
        self.assertEqual(self.read()["plugins"], ["/a", self.plugin, "/b"])

    def test_a_plugin_with_a_similar_name_elsewhere_is_not_ours(self) -> None:
        notours = "/home/x/" + OC.PLUGIN_DIRNAME
        self.write({"plugins": [notours]})
        self.run_install()
        self.assertEqual(self.read()["plugins"], [notours, self.plugin])
        self.run_install("--remove")
        self.assertEqual(self.read()["plugins"], [notours])

    def test_remove_takes_only_ours_and_the_array_goes_when_nothing_is_left(self) -> None:
        self.write({"plugins": ["/lane", self.plugin, {"package": "p"}]})
        code, answer = self.run_install("--remove")
        self.assertEqual((code, answer["changed"]), (0, ["plugins"]))
        self.assertEqual(self.read()["plugins"], ["/lane", {"package": "p"}])
        self.write({"server": {}, "plugins": [self.plugin]})
        self.run_install("--remove")
        self.assertEqual(self.read(), {"server": {}})
        self.assertEqual(self.run_install("--remove")[1]["changed"], [])  # already gone

    def test_remove_works_from_a_lane_too(self) -> None:
        """Taking the plugin out never needs the checkout to be a good one."""
        self.write({"plugins": [self.plugin]})
        env = {"HOME": self.home}  # no override: this checkout is a lane's
        code, answer = self.run_install("--remove", env=env)
        self.assertEqual((code, answer["changed"]), (0, ["plugins"]))

    def test_a_file_that_cannot_be_edited_safely_is_left_alone(self) -> None:
        cases = {
            "not json": ('{"plugins": [', None),
            "a comment": ('{"a": 1} // note\n', None),
            "not an object": ("[1, 2]", None),
            "duplicate key": ('{"plugins": [], "plugins": ["x"]}', None),
            "plugins not a list": ('{"plugins": "x"}', None),
        }
        for name, (raw, _) in cases.items():
            with self.subTest(name):
                self.write(None, raw=raw)
                code, answer = self.run_install()
                self.assertEqual(code, 2)
                self.assertIn("error", answer)
                self.assertEqual(Path(self.cfg).read_text(), raw)
                self.assertEqual(self.backups(), [])

    def test_a_missing_file_is_created_private_but_a_missing_directory_is_not(self) -> None:
        code, _ = self.run_install()
        self.assertEqual(code, 0)
        self.assertEqual(self.read(), {"plugins": [self.plugin]})
        self.assertEqual(stat.S_IMODE(os.stat(self.cfg).st_mode), 0o600)
        self.assertEqual(self.backups(), [])  # nothing to back up
        shutil.rmtree(self.cfg_dir)
        code, answer = self.run_install()
        self.assertEqual(code, 2)
        self.assertFalse(os.path.exists(self.cfg_dir))

    def test_xdg_config_home_is_followed(self) -> None:
        xdg = os.path.join(self.tmp.name, "xdg")
        os.makedirs(os.path.join(xdg, "opencode"))
        code, _ = self.run_install(env=dict(self.env, XDG_CONFIG_HOME=xdg))
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(os.path.join(xdg, "opencode", "opencode.json")))
        self.assertFalse(os.path.exists(self.cfg))

    def test_bad_flags_are_refused(self) -> None:
        code, answer = self.run_install("--remote=-oProxyCommand=x")
        self.assertEqual(code, 2)
        self.assertIn("ssh destination", answer["error"])
        self.assertFalse(os.path.exists(self.cfg))

    def test_the_indent_of_the_file_is_kept(self) -> None:
        self.write(None, raw='{\n    "server": {\n        "port": 1\n    }\n}\n')
        self.run_install()
        text = Path(self.cfg).read_text()
        self.assertIn('\n    "server": {', text)
        self.assertTrue(text.endswith("}\n"))

    def test_the_answer_says_what_a_host_without_the_hook_command_needs(self) -> None:
        self.write({})
        _, answer = self.run_install()
        self.assertTrue(any("platform-letter" in n for n in answer["notes"]))
        os.makedirs(os.path.join(self.home, ".local", "bin"))
        Path(self.home, ".local", "bin", "platform-letter").write_text("#!/bin/sh\n")
        self.write({})
        _, answer = self.run_install("--location", "server", "--remote", "mac")
        self.assertNotIn("notes", answer)
        self.assertTrue(self.run_install("--check")[1]["hook_present"])


class PluginDirTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        os.makedirs(self.home)

    def make(self, *parts: str) -> str:
        path = os.path.join(self.tmp.name, *parts, "scripts", "agent_sync", OC.PLUGIN_DIRNAME)
        os.makedirs(path)
        Path(path, "index.js").write_text("export default {};\n")
        return path

    def test_a_task_lane_is_refused(self) -> None:
        lane = self.make("apps", "lanes", "AI-Fleet-Coordinator", "claude-x")
        with self.assertRaises(OC.PluginDirError):
            OC.plugin_dir({}, self.home, source=lane)

    def test_the_managed_runtime_is_named_as_it_is(self) -> None:
        managed = self.make("apps", "lanes", "_managed", "fleet", "runtime")
        self.assertEqual(OC.plugin_dir({}, self.home, source=managed), managed)

    def test_the_servers_checkout_is_named_through_its_current_link(self) -> None:
        cli = os.path.join(self.home, ".local", "share", "fleet-cli")
        repo = os.path.join(cli, "repo", "scripts", "agent_sync", OC.PLUGIN_DIRNAME)
        os.makedirs(repo)
        Path(repo, "index.js").write_text("export default {};\n")
        os.symlink("repo", os.path.join(cli, "current"))
        got = OC.plugin_dir({}, self.home, source=repo)
        self.assertEqual(got, os.path.join(cli, "current", "scripts", "agent_sync", OC.PLUGIN_DIRNAME))
        self.assertTrue(os.path.isfile(os.path.join(got, "index.js")))  # the link resolves

    def test_the_override_must_be_an_absolute_directory_with_the_plugin(self) -> None:
        good = self.make("somewhere")
        self.assertEqual(OC.plugin_dir({OC.ENV_PLUGIN_DIR: good}, self.home), good)
        for bad in ("relative/dir", os.path.join(self.tmp.name, "empty")):
            with self.subTest(bad=bad), self.assertRaises(OC.PluginDirError):
                OC.plugin_dir({OC.ENV_PLUGIN_DIR: bad}, self.home)

    def test_this_checkout_ships_the_plugin(self) -> None:
        self.assertTrue((PLUGIN / "index.js").is_file())
        self.assertTrue(OC.is_ours(str(PLUGIN)))


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class NodeTests(HookBase):
    def test_the_plugins_own_node_tests_pass(self) -> None:
        # The file, never the directory: `node --test <dir>` can take the plugin's own index.js for a test file and pass
        # without running a single test.
        proc = subprocess.run(
            ["node", "--test", str(SCRIPTS / "agent_sync" / "tests" / "opencode_letter_plugin.test.mjs")],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            cwd=str(PLUGIN),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout[-3000:] + proc.stderr[-1000:])
        # The summary line is "ℹ tests 22" (spec reporter) or "# tests 22" (TAP): it ran tests, and none failed.
        self.assertRegex(proc.stdout, r"(?m)^\S+ tests [1-9]\d*\s*$")
        self.assertRegex(proc.stdout, r"(?m)^\S+ fail 0\s*$")

    def test_the_plugin_drives_the_real_hook_end_to_end(self) -> None:
        """The plugin's engine, its real spawn, `scripts/platform-letter hook --platform opencode`, a temporary
        registry as the Mac's, and a fake OpenCode that records the titles written."""
        driver = os.path.join(self.tmp.name, "driver.mjs")
        Path(driver).write_text(
            "import { createLetters, runHook } from %s;\n" % json.dumps((PLUGIN / "index.js").as_uri())
            + "const settings = { command: process.argv[2], args: [process.argv[3], 'hook', '--platform', 'opencode'], timeoutMs: 30000 };\n"
            "const writes = [];\n"
            "const db = {};\n"  # the chats' titles, as OpenCode would hold them
            "const api = { get: async (sid) => ({ id: sid, title: db[sid] }), update: async (sid, title) => { db[sid] = title; writes.push([sid, title]); } };\n"
            "const letters = createLetters({ api, run: (payload) => runHook(settings, payload), settleMs: 0 });\n"
            "const idle = async () => { for (let i = 0; i < 6; i++) { await Promise.all([...letters.sessions.values()].map((s) => s.chain)); await new Promise((r) => setImmediate(r)); } };\n"
            "const say = (type, data) => letters.onEvent({ type, data });\n"
            "say('session.created', { sessionID: 'ses_e2e_0001', parentID: '' });\n"
            "await idle();\n"
            "say('session.inbox.enqueued', { sessionID: 'ses_e2e_0001', item: { type: 'user', payload: { text: 'fix the login page' } } });\n"
            "await idle();\n"
            "db['ses_e2e_0001'] = 'Billing Audit';\n"
            "say('session.renamed', { sessionID: 'ses_e2e_0001', title: 'Billing Audit' });\n"
            "await idle();\n"
            "say('session.archived', { sessionID: 'ses_e2e_0001' });\n"
            "await idle();\n"
            "console.log(JSON.stringify(writes));\n"
        )
        env = dict(
            os.environ,
            AGENT_SYNC_STATE_DIR=self.state,
            SESSION_LETTER_LOCATION="mac",  # the registry in process, as on the Mac
            HOME=self.home,
        )
        for name in ("AGENT_LAUNCHER", "AGENT_LAUNCH_SEAT", "SESSION_LETTER", "OPENCODE_LETTER"):
            env.pop(name, None)
        proc = subprocess.run(
            ["node", driver, sys.executable, str(ENTRY)], capture_output=True, text=True, timeout=180, env=env
        )
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        writes = json.loads(proc.stdout.strip().splitlines()[-1])
        label = self.note("ses_e2e_0001")["label"]
        self.assertRegex(label, PAIR)
        self.assertEqual(
            writes,
            [
                ["ses_e2e_0001", "%s: New Chat" % label],
                ["ses_e2e_0001", "%s: Fix the Login Page" % label],
                ["ses_e2e_0001", "%s: Billing Audit" % label],
            ],
        )
        self.assertEqual(self.lookup("ses_e2e_0001", location="mac", platform="opencode")["state"], "closed")


if __name__ == "__main__":
    unittest.main()
