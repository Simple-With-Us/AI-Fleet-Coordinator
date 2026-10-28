"""`fleet-facts gather` end to end:  a real subprocess, fake gh and board on PATH, a temp HOME."""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from .builders import (
    board_row,
    board_show,
    comment,
    doctor_checkout,
    doctor_report,
    gql_node,
    gql_page,
    read_log,
    write_scenario,
    write_shim,
)

SCRIPTS = Path(__file__).resolve().parents[2]
ENTRY = SCRIPTS / "fleet-facts"
SIGNER = "fixture-signing-key-0123456789abcdefABCDEF"


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.state = self.root / "state"
        for d in (self.home, self.bin, self.state):
            d.mkdir()
        self.log = self.root / "calls.log"
        self.scenario = {}

    def install(self, name, source):
        return write_shim(self.bin, name, source)

    def install_fake_infisical(self, key=None, fail=False):
        """A fake `infisical` first on PATH:  K1 finds it before any real CLI (and so never reaches the real Infisical)."""
        body = (
            "#!/bin/sh\nexit 1\n"
            if fail
            else '#!/bin/sh\nprintf \'{"FLEET_FACTS_SIGNING_KEY": "%s", "FLEET_FACTS_PUBLISH_FULL_COPY": "%s"}\' '
            '"$FAKE_SIGNING_KEY" "${FAKE_FULL_COPY:-false}"\n'
        )
        path = self.bin / "infisical"
        path.write_text(body)
        path.chmod(0o755)
        self.fake_key = key

    def install_fake_gh_404(self):
        """A `gh` that answers every call with a 404:  a publish run (not a dry run) always looks for a left-over full copy first,
        and a test must never reach the real GitHub."""
        path = self.bin / "gh"
        path.write_text("#!/bin/sh\necho 'gh: Not Found (HTTP 404)' >&2\nexit 1\n")
        path.chmod(0o755)

    def run_cli(self, *args, scenario=None, entry=ENTRY, env_extra=None):
        write_scenario(
            self.root / "scenario.json", scenario if scenario is not None else self.scenario
        )
        env = {
            "HOME": str(self.home),
            "PATH": "%s:/usr/bin:/bin" % self.bin,
            "FF_SCENARIO": str(self.root / "scenario.json"),
            "FF_LOG": str(self.log),
            "FF_STATE": str(self.state),
            "LANG": "C",
            # The fake infisical cannot log in a machine identity:  never let the real System keychain one be tried.
            "FLEET_KNOBS_AUTH": "session",
        }
        if getattr(self, "fake_key", None):
            env["FAKE_SIGNING_KEY"] = self.fake_key
        env.update(env_extra or {})
        return subprocess.run(
            [sys.executable, "-I", str(entry), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

    def github_and_linear(self, title="Example change"):
        self.install("gh", "gh")
        self.install("simple-board", "board")
        self.scenario["gh"] = {
            "pages": {
                "": gql_page(
                    [
                        gql_node(1, title=title),
                        gql_node(2, cross=True, owner="outside-person", title="fork text"),
                    ],
                    "c1",
                    3,
                ),
                "c1": gql_page([gql_node(3)], None, 3),
            }
        }
        self.scenario["board"] = {
            "list": {
                "in_progress": [board_row("FFX-1"), board_row("FFX-2", seats=(), n=2)],
                "needs-owner": [],
            },
            "show": {
                "FFX-1": board_show(
                    "FFX-1", comments=[comment("CLAUDE", "CLAIMED", "2026-10-10T10:00:00.000Z")]
                ),
                "FFX-2": board_show("FFX-2"),
            },
        }


class NoMac(Sandbox):
    def test_cloud_run_marks_mac_parts_absent_and_never_touches_the_lane_tool(self):
        self.github_and_linear()
        lane = self.home / "apps" / "lane"
        lane.parent.mkdir()
        write_shim(lane.parent, "lane", "lane")
        self.scenario["lane"] = {}
        proc = self.run_cli("gather", "--no-mac", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["sources"]["github"]["status"], "ok")
        self.assertEqual(doc["sources"]["linear"]["status"], "ok")
        for part in ("lanes", "autopush", "backup", "asks", "report", "needs_owner", "consult"):
            self.assertEqual(doc["sources"][part], {"status": "absent", "reason": "--no-mac"}, part)
            self.assertNotIn(part, doc)
        self.assertEqual([p["number"] for p in doc["github"]["prs"]], [1, 3])
        self.assertEqual(doc["github"]["outside_fork_prs"]["count"], 1)
        self.assertNotIn("fork text", proc.stdout)
        self.assertEqual([c["identifier"] for c in doc["linear"]["claims"]], ["FFX-1"])
        self.assertEqual([c["identifier"] for c in doc["linear"]["orphans"]], ["FFX-2"])
        calls = read_log(self.log)
        self.assertEqual([c for c in calls if c[0] == "lane"], [])
        self.assertTrue(
            [c for c in calls if c[0] == "gh"] and [c for c in calls if c[0] == "simple-board"]
        )

    def test_gh_is_called_with_the_cursor_variable_and_board_only_reads(self):
        self.github_and_linear()
        self.run_cli("gather", "--no-mac", "--json")
        calls = read_log(self.log)
        gh = [c for c in calls if c[0] == "gh"]
        self.assertEqual(len(gh), 2)
        self.assertIn("endCursor=c1", gh[1])
        board = [c for c in calls if c[0] == "simple-board"]
        self.assertEqual({c[2] for c in board}, {"list", "show"})
        self.assertEqual(
            {c[1] for c in board}, {"unset"}
        )  # no write switch is set for any call:  the bearer is the gate

    def test_linear_updated_at_is_nowhere_in_the_output(self):
        self.github_and_linear()
        doc = json.loads(self.run_cli("gather", "--no-mac", "--part", "linear", "--json").stdout)

        def keys(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield k
                    yield from keys(v)
            elif isinstance(node, list):
                for v in node:
                    yield from keys(v)

        self.assertFalse({k for k in keys(doc["linear"]) if "updated" in k.lower()})

    def test_a_secret_shaped_title_is_withheld(self):
        token = "ghp_" + "Qw3Er5Ty" * 5
        self.github_and_linear(title="oops " + token)
        proc = self.run_cli("gather", "--no-mac", "--part", "github", "--json")
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn(token, proc.stdout)
        doc = json.loads(proc.stdout)
        self.assertEqual(doc["redacted"], ["$.github.prs[0].title"])

    def test_failed_sources_are_reported_on_stderr_and_exit_zero(self):
        self.install("gh", "gh")
        self.scenario["gh"] = {"pages": {}}
        proc = self.run_cli("gather", "--no-mac", "--part", "github", "--json")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("github failed", proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["sources"]["github"]["status"], "failed")

    def test_without_json_a_summary_line_per_part(self):
        proc = self.run_cli("gather", "--no-mac", "--part", "lanes", "--part", "backup")
        self.assertEqual(
            proc.stdout.splitlines(), ["lanes: absent  (--no-mac)", "backup: absent  (--no-mac)"]
        )


class MacParts(Sandbox):
    def test_lanes_backup_and_autopush_on_the_mac(self):
        now = datetime.now(timezone.utc)  # noqa: UP017 (3.9 has no datetime.UTC)
        report = doctor_report(
            [doctor_checkout("/home/example/apps/lanes/Example/claude-x", unpushed=2)],
            generated_at=now.isoformat(timespec="seconds"),
        )
        apps = self.home / "apps"
        (apps / "fleet-work-backup").mkdir(parents=True)
        write_shim(apps, "lane", "lane")
        write_shim(apps / "fleet-work-backup", "fleet_work_backup.py", "fleet_work_backup.py")
        self.install("fleet-autopush", "jsoncmd")
        self.scenario.update(
            {
                "lane": report,
                "backup": {"verdict": "ok", "reason": "fine"},
                "cmds": {"fleet-autopush": {"lanes": [{"n": 1}]}},
            }
        )
        proc = self.run_cli(
            "gather", "--part", "lanes", "--part", "backup", "--part", "autopush", "--json"
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout)
        self.assertEqual(
            {k: v["status"] for k, v in doc["sources"].items()},
            {"lanes": "ok", "backup": "ok", "autopush": "ok"},
        )
        self.assertEqual(doc["lanes"]["attention_count"], 1)
        self.assertEqual(doc["backup"]["verdict"], "ok")
        self.assertEqual(doc["autopush"]["document"], {"lanes": [{"n": 1}]})
        # The second run reuses the lane report (under 900 s old) and does not start the doctor again.
        self.run_cli("gather", "--part", "lanes", "--json")
        self.assertEqual(len([c for c in read_log(self.log) if c[0] == "lane"]), 1)


class Errors(Sandbox):
    def test_unknown_part_is_a_usage_error(self):
        proc = self.run_cli("gather", "--part", "everything", "--json")
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, "")

    def test_no_command_prints_usage(self):
        proc = self.run_cli()
        self.assertEqual(proc.returncode, 2)

    def test_publish_mac_without_a_signing_key_publishes_nothing(self):
        self.install_fake_infisical(fail=True)
        self.install_fake_gh_404()
        proc = self.run_cli("publish-mac")
        self.assertEqual(proc.returncode, 3, proc.stderr)
        self.assertIn("no-key", proc.stderr)
        self.assertIn("nothing was published", proc.stderr)
        self.assertFalse(
            (self.root / "calls.log").exists(), "no gh, board or lane program was started"
        )
        status = json.loads(
            (self.home / ".local" / "state" / "fleet-facts" / "mac-facts-publish.json").read_text()
        )
        self.assertEqual(status["result"], "no-key")
        self.assertEqual(
            status["files"],
            {"mac-facts.json": "skipped:  FLEET_FACTS_PUBLISH_FULL_COPY is off"},
            "the left-over check ran first and found none",
        )

    def test_publish_mac_dry_run_signs_the_path_free_copy_and_sends_nothing(self):
        self.install_fake_infisical(key=SIGNER)
        out = self.root / "dry"
        proc = self.run_cli("publish-mac", "--dry-run", "--out", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        document = json.loads((out / "gb" / "mac-facts.json").read_text())
        self.assertEqual(document["signed_for"], "gb/mac-facts.json")
        self.assertRegex(document["sig"], r"^[0-9a-f]{64}$")
        self.assertTrue(
            (out / "gb" / "full-list.md").is_file(),
            "the full list, or the note that says why there is none",
        )
        self.assertFalse(
            (out / "mac-facts.json").exists(),
            "the path-bearing copy is off unless Infisical turns it on",
        )
        self.assertIn("FLEET_FACTS_PUBLISH_FULL_COPY is off", proc.stdout)
        self.assertNotIn(SIGNER, proc.stdout + proc.stderr)
        self.assertNotIn(SIGNER, (out / "gb" / "mac-facts.json").read_text())

    def test_publish_mac_dry_run_writes_the_full_copy_when_infisical_says_so(self):
        self.install_fake_infisical(key=SIGNER)
        out = self.root / "dry"
        proc = self.run_cli(
            "publish-mac", "--dry-run", "--out", str(out), env_extra={"FAKE_FULL_COPY": "true"}
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        document = json.loads((out / "mac-facts.json").read_text())
        self.assertEqual(document["signed_for"], "mac-facts.json")

    def test_out_without_dry_run_is_a_usage_error(self):
        proc = self.run_cli("publish-mac", "--out", str(self.root / "x"))
        self.assertEqual(proc.returncode, 2)

    def test_publish_one_file_dry_run(self):
        self.install_fake_infisical(key=SIGNER)
        local = self.root / "a.json"
        local.write_text('{"n": 1}')
        proc = self.run_cli("publish", "--path", "gb/a.json", "--file", str(local), "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gb/a.json: checked", proc.stdout)
        refused = self.run_cli(
            "publish", "--path", ".github/workflows/x.yml", "--file", str(local), "--dry-run"
        )
        self.assertEqual(refused.returncode, 3)
        self.assertIn("not one this job writes", refused.stderr)

    def test_verify_exit_codes_through_the_real_entry_point(self):
        from fleet_facts import signing

        signed = self.root / "signed.json"
        signed.write_text(
            json.dumps(
                signing.sign(
                    {"signed_for": "mac-facts.json", "generated_at": "2026-10-11T12:00:00Z"}, SIGNER
                )
            )
        )
        tampered = self.root / "tampered.json"
        tampered.write_text(signed.read_text().replace("2026-10-11T12", "2026-10-11T13"))
        good = self.run_cli("verify", str(signed), env_extra={"FLEET_FACTS_SIGNING_KEY": SIGNER})
        self.assertEqual(
            (good.returncode, good.stdout.startswith("fleet-facts: verified:")),
            (0, True),
            good.stderr,
        )
        bad = self.run_cli("verify", str(tampered), env_extra={"FLEET_FACTS_SIGNING_KEY": SIGNER})
        self.assertEqual(bad.returncode, 1)
        self.assertIn("not verified", bad.stderr)
        self.assertEqual(
            self.run_cli(
                "verify",
                str(signed),
                "--path",
                "gb/mac-facts.json",
                env_extra={"FLEET_FACTS_SIGNING_KEY": SIGNER},
            ).returncode,
            1,
        )
        self.assertEqual(
            self.run_cli("verify", str(signed)).returncode, 3, "no key in the environment"
        )
        self.assertEqual(self.run_cli("verify").returncode, 2, "usage")
        for proc in (good, bad):
            self.assertNotIn(SIGNER, proc.stdout + proc.stderr)

    def test_help_and_version(self):
        self.assertEqual(self.run_cli("--help").returncode, 0)
        proc = self.run_cli("--version")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("fleet-facts", proc.stdout)

    def test_runs_isolated_from_the_repository_checkout(self):
        proc = self.run_cli("gather", "--no-mac", "--part", "lanes", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
