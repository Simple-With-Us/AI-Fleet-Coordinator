"""`open-loops` end to end:  a real python3 -I subprocess, fake gh, board, lane, C1, C6, agent-sync and fleet-facts
programs on PATH, and a temp HOME."""
import json
import stat
import unittest
from datetime import datetime, timezone

from fleet_facts.tests.builders import write_shim as ff_shim

from open_loops import cli

from .builders import FAKE_KEY, Sandbox, ago, install_shim
from .scenes import make_dm, standard_world


class Cli(Sandbox):
    def setUp(self):
        super().setUp()
        # The subprocess reads the real clock, so the scenario is relative to it;  no UNKNOWN PR, because gh's
        # "mergeable is still being computed" retry really sleeps in a subprocess.
        self.now = datetime.now(timezone.utc)
        self.world = standard_world(self.home, self.bin, programs=False, now=self.now, unknown_pr=False)
        ff_shim(self.bin, "gh", "gh")
        ff_shim(self.bin, "board", "board")
        (self.home / "apps").mkdir()
        ff_shim(self.home / "apps", "lane", "lane")
        ff_shim(self.bin, "fleet-autopush", "jsoncmd")
        ff_shim(self.bin, "needs-owner", "jsoncmd")
        install_shim(self.bin, "agent-sync", "agent-sync")
        install_shim(self.bin, "fleet-facts", "fleet-facts")
        w = self.world
        self.scenario = {
            "gh": {"pages": w.pages},
            "board": {"list": {"in_progress": w.in_progress, "needs-owner": w.needs_owner}, "show": w.show, "show_fail": sorted(w.show_fail)},
            "lane": w.lane_report,
            "cmds": {"fleet-autopush": w.docs["fleet-autopush"], "needs-owner": w.docs["needs-owner"]},
            "agent-sync": {"whoami": {"seat": "BF-DEPLOYER", "bot_seat": "BF-DEPLOYER", "verified": True, "user_id": 77},
                           "topics": [], "read": {}, "dm_read": []},
            "fleet-facts": {"verify_rc": 0},
        }

    def facts_via_cli(self, *args):
        proc = self.run_cli("gather", "--json", *args)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout), proc

    def commands(self):
        return [c for c in self.calls() if isinstance(c, list)]


class Help(Cli):
    def test_help_runs_under_python_dash_I(self):
        proc = self.run_cli("--help")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for word in ("gather", "render", "send"):
            self.assertIn(word, proc.stdout)

    def test_no_command_is_a_usage_error(self):
        self.assertEqual(self.run_cli().returncode, 2)


class Gather(Cli):
    def test_a_mac_gather_lists_every_source_ok_or_with_a_reason(self):
        facts, proc = self.facts_via_cli()
        for name in ("github", "linear", "lanes", "autopush", "needs_owner"):
            self.assertEqual(facts["sources"][name]["status"], "ok", name)
        self.assertEqual(facts["sources"]["asks"]["status"], "absent")
        self.assertEqual(facts["sources"]["asks"]["reason"], "not built yet (C3, Phase 2)")
        for name, source in facts["sources"].items():
            self.assertIn(source["status"], ("ok", "absent", "failed"))
            if source["status"] != "ok":
                self.assertTrue(source["reason"], name)                                    # a reason for everything that is not ok
        self.assertEqual(facts["stuck_prs"]["count"], 2)
        self.assertEqual(facts["not_on_github"]["snapshot_only"]["count"], 1)
        self.assertEqual(facts["decisions"]["count"], 2)

    def test_no_mac_never_starts_a_local_program(self):
        facts, proc = self.facts_via_cli("--no-mac")
        self.assertEqual(facts["sources"]["lanes"]["status"], "absent")
        self.assertEqual({m["source"] for m in facts["missing"]}, {"lanes", "autopush"})
        programs = {c[0] for c in self.commands()}
        self.assertTrue({"gh", "board"} <= programs)
        self.assertFalse(programs & {"lane", "fleet-autopush", "needs-owner"})
        self.assertIn("Missing:  lanes", proc.stderr)

    def test_out_writes_a_private_file_and_prints_nothing(self):
        target = self.root / "state" / "open-loops" / "facts-today.json"
        proc = self.run_cli("gather", "--json", "--out", str(target))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(target.parent.stat().st_mode), 0o700)
        facts = json.loads(target.read_text())
        self.assertEqual(facts["schema"], 1)
        self.assertIn("wrote %s" % facts["facts_id"], proc.stderr)
        self.assertEqual([p.name for p in target.parent.iterdir()], ["facts-today.json"])      # no temp file left behind

    def test_out_replaces_an_existing_file_atomically_and_refuses_a_folder(self):
        target = self.root / "facts.json"
        target.write_text("old")
        target.chmod(0o644)
        self.assertEqual(self.run_cli("gather", "--json", "--out", str(target)).returncode, 0)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertEqual(json.loads(target.read_text())["schema"], 1)
        proc = self.run_cli("gather", "--json", "--out", str(self.root))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("is a folder", proc.stderr)

    def test_the_launch_time_comes_from_the_flag_or_the_environment(self):
        facts, _ = self.facts_via_cli("--launch-at", ago(1, self.now))
        self.assertTrue(facts["claims"]["clock_started"])
        proc = self.run_cli("gather", "--json", CLAIM_EXPIRY_LAUNCH_AT=ago(1, self.now))
        self.assertTrue(json.loads(proc.stdout)["claims"]["clock_started"])

    def test_mac_facts_are_verified_with_the_key_in_the_environment_only(self):
        doc = {"schema": 1, "generated_at": ago(1, self.now), "sig": "00",
               "sources": {"lanes": {"status": "ok", "reason": None}, "autopush": {"status": "ok", "reason": None}},
               "lanes": {"attention": []}, "autopush": {"document": {"lanes": [], "refused": []}}}
        path = self.write("gb-mac-facts.json", json.dumps(doc))
        proc = self.run_cli("gather", "--json", "--no-mac", "--mac-facts", str(path), FLEET_FACTS_SIGNING_KEY=FAKE_KEY)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        facts = json.loads(proc.stdout)
        self.assertEqual(facts["sources"]["mac_facts"]["status"], "ok")
        self.assertEqual(facts["missing"], [])
        verify = [c for c in self.calls() if isinstance(c, dict) and c.get("tool") == "fleet-facts"]
        self.assertEqual(len(verify), 1)
        self.assertTrue(verify[0]["key_set"])
        self.assertEqual(verify[0]["file_mode"], "0o600")
        self.assertNotIn(FAKE_KEY, proc.stdout + proc.stderr + self.log.read_text())
        # without the key the same file is ignored
        proc = self.run_cli("gather", "--json", "--no-mac", "--mac-facts", str(path))
        facts = json.loads(proc.stdout)
        self.assertEqual(facts["sources"]["mac_facts"]["status"], "absent")
        self.assertIn("mac_facts", [m["source"] for m in facts["missing"]])


class Render(Cli):
    def test_render_prints_the_full_list(self):
        facts, _ = self.facts_via_cli()
        path = self.write("facts.json", json.dumps(facts))
        proc = self.run_cli("render", "--facts", str(path), "--markdown")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("## 3. Work Not on GitHub Yet", proc.stdout)
        self.assertIn(" ", proc.stdout)                                # UTF-8 out, even under LANG=C

    def test_render_brief_goes_through_the_entry_script(self):
        facts, _ = self.facts_via_cli()
        path = self.write("facts.json", json.dumps(facts))
        proc = self.run_cli("render", "--facts", str(path), "--markdown", "--brief")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("## 1. Decisions Waiting on You", proc.stdout)

    def test_render_out_is_private_and_a_loose_facts_file_is_refused(self):
        facts, _ = self.facts_via_cli()
        path = self.write("facts.json", json.dumps(facts))
        out = self.root / "full-list.md"
        self.assertEqual(self.run_cli("render", "--facts", str(path), "--markdown", "--out", str(out)).returncode, 0)
        self.assertEqual(stat.S_IMODE(out.stat().st_mode), 0o600)
        path.chmod(0o666)
        proc = self.run_cli("render", "--facts", str(path), "--markdown")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("written by others", proc.stderr)


class Send(Cli):
    def prepare(self):
        facts, _ = self.facts_via_cli()
        self.facts_file = self.write("facts.json", json.dumps(facts))
        self.dm_file = self.write("dm.md", make_dm(facts))
        return facts

    def test_send_through_the_entry_script(self):
        facts = self.prepare()
        proc = self.run_cli("send", "--dm-file", str(self.dm_file), "--facts", str(self.facts_file), OPEN_LOOPS_C2_ID="FFX-100")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        verbs = [c["argv"][0] for c in self.calls() if isinstance(c, dict) and "argv" in c and c.get("tool") != "fleet-facts"]
        self.assertEqual(verbs[-4:], ["whoami", "dm-read", "dm", "post"])

    def test_the_wrong_seat_is_refused_through_the_entry_script(self):
        self.prepare()
        self.scenario["agent-sync"]["whoami"]["seat"] = "CLAUDE"
        proc = self.run_cli("send", "--dm-file", str(self.dm_file), "--facts", str(self.facts_file), "--c2-id", "FFX-100")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("only BF-Deployer", proc.stderr)

    def test_no_c2_id_anywhere_refuses(self):
        self.prepare()
        proc = self.run_cli("send", "--dm-file", str(self.dm_file), "--facts", str(self.facts_file))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no C2 id", proc.stderr)

    def test_the_c2_id_resolution_order(self):
        file = self.root / "c2-id"
        file.write_text("FFX-9\n")
        original = cli._c2_file
        cli._c2_file = lambda: file
        try:
            self.assertEqual(cli.resolve_c2_id("FFX-1", {cli.C2_ENV: "FFX-2"}), "FFX-1")
            self.assertEqual(cli.resolve_c2_id(None, {cli.C2_ENV: "FFX-2"}), "FFX-2")
            self.assertEqual(cli.resolve_c2_id(None, {}), "FFX-9")
            file.write_text("\n")
            self.assertIsNone(cli.resolve_c2_id(None, {}))
            file.unlink()
            self.assertIsNone(cli.resolve_c2_id("", {}))
        finally:
            cli._c2_file = original


if __name__ == "__main__":
    unittest.main()
