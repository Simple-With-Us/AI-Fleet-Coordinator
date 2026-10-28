"""Tests for fleet-ops-control.py:  the allowlist, the ssh gate, the plans and the audit.

Nothing here runs launchctl, systemctl, fleet-secrets or Coolify:  every runner is a fake.

  python3 scripts/test_fleet_ops_control.py
"""

import datetime
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("foc", os.path.join(HERE, "fleet-ops-control.py"))
foc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(foc)

UUID = "abc123def456"
TOKEN = "tok-" + "z" * 40
NOW = 1_800_000_000.0


def stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


CONFIG = {
    "schemaVersion": 1,
    "mac": {
        "agents": [
            {"label": "com.jay.fleet-tools-doctor", "actions": ["start", "stop", "restart"]},
            {"label": "com.jay.only-start", "actions": ["start"]},
        ]
    },
    "server": {
        "units": [
            {"unit": "fleet-server-status.timer", "scope": "user", "actions": ["start", "stop", "restart"]},
            {"unit": "fleet-cli-git-update.timer", "scope": "system", "actions": ["restart"]},
        ],
        "updates": [{"name": "fleet-cli", "unit": "fleet-cli-git-update.service", "scope": "system"}],
        "coolify": {"redeploy": True},
    },
}


class FakeRunner:
    """Records every argv;  answers `launchctl print` with `loaded` and the rest with `status`."""

    def __init__(self, loaded=True, status=0, secret=TOKEN):
        self.calls = []
        self.loaded = loaded
        self.status = status
        self.secret = secret

    def __call__(self, argv, timeout=None, env=None, want_stdout=False):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        self.calls.append((list(argv), dict(env or {})))
        if argv[1:2] == ["print"]:
            return (0 if self.loaded else 113), b""
        if argv[1:2] == ["get"]:
            return (0, self.secret.encode()) if self.secret else (3, b"")
        return self.status, b""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.config_path = self.home / "fleet-ops-control.json"
        self.config_path.write_text(json.dumps(CONFIG))
        self.state = self.home / ".local" / "state" / "fleet-tools"
        self.state.mkdir(parents=True, mode=0o700)
        self.http_calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def http(self, url, headers):
        self.http_calls.append((url, dict(headers)))
        return self.http_status

    def ctx(self, platform="linux", runner=None, environ=None, http_status=200):
        self.runner = runner or FakeRunner()
        self.http_status = http_status
        return foc.Context(
            platform=platform,
            uid=os.getuid(),
            home=self.home,
            environ=environ or {"PATH": "/usr/bin:/bin", "SECRET_THING": "x" * 20},
            runner=self.runner,
            http=self.http,
            now=lambda: NOW,
            config_path=self.config_path,
            fleet_secrets="/fake/bin/fleet-secrets",
        )

    def run_main(self, argv, ctx):
        out = io.StringIO()
        code = foc.main(argv, ctx=ctx, out=out)
        text = out.getvalue()
        return code, (json.loads(text) if text.startswith("{") else text)

    def audit_rows(self):
        path = self.state / "ops-audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def write_status(self, rows, age=60, mode=0o600, tool="fleet-server-status"):
        path = self.state / "server-status.json"
        path.write_text(
            json.dumps({"schemaVersion": 1, "tool": tool, "generatedAt": stamp(NOW - age), "coolify": rows})
        )
        os.chmod(path, mode)
        return path


class AllowlistTests(Base):
    def test_repo_allowlist_is_valid_and_covers_every_plist(self):
        config = foc.load_config()
        plists = Path(HERE, "launchd").glob("*.plist")
        for plist in plists:
            text = plist.read_text()
            label = text.split("<key>Label</key>", 1)[1].split("<string>", 1)[1].split("</string>", 1)[0]
            self.assertIn(label.strip(), config["agents"], plist.name)
        self.assertIn("com.jay.fleet-worker-health", config["agents"])
        self.assertIn("com.jay.fleet-mac-services", config["agents"])
        self.assertEqual(config["units"]["fleet-server-status.timer"]["scope"], "user")
        self.assertEqual(config["updates"]["fleet-cli"]["unit"], "fleet-cli-git-update.service")
        self.assertTrue(config["redeploy"])

    def test_malformed_entries_refuse_the_whole_file(self):
        bad_docs = [
            {"schemaVersion": 2},
            {"schemaVersion": 1, "mac": {"agents": [{"label": "com.jay.x; rm -rf /", "actions": ["start"]}]}},
            {"schemaVersion": 1, "mac": {"agents": [{"label": "com.jay.x", "actions": ["delete"]}]}},
            {"schemaVersion": 1, "mac": {"agents": [{"label": "com.jay.x", "actions": []}]}},
            {"schemaVersion": 1, "server": {"units": [{"unit": "../etc.service", "scope": "user", "actions": ["start"]}]}},
            {"schemaVersion": 1, "server": {"units": [{"unit": "x.mount", "scope": "user", "actions": ["start"]}]}},
            {"schemaVersion": 1, "server": {"units": [{"unit": "x@y.service", "scope": "user", "actions": ["start"]}]}},
            {"schemaVersion": 1, "server": {"units": [{"unit": "x.service", "scope": "root", "actions": ["start"]}]}},
            {"schemaVersion": 1, "server": {"updates": [{"name": "x", "unit": "x.timer", "scope": "system"}]}},
            {"schemaVersion": 1, "server": {"coolify": {"redeploy": "yes"}}},
            {"schemaVersion": 1, "mac": {"agents": [{"label": "com.jay.x\n", "actions": ["start"]}]}},
            {"schemaVersion": 1, "server": {"units": [{"unit": "x.service\n", "scope": "user", "actions": ["start"]}]}},
            {"schemaVersion": 1, "server": {"updates": [{"name": "x\n", "unit": "x.service", "scope": "system"}]}},
        ]
        for doc in bad_docs:
            with self.assertRaises(foc.Refused, msg=json.dumps(doc)):
                foc.validate_config(doc)


class ServerVerbTests(Base):
    def test_plan_prints_argv_and_runs_nothing(self):
        code, ans = self.run_main(["server", "restart", "fleet-server-status.timer", "--plan", "--json"], self.ctx())
        self.assertEqual(code, 0)
        self.assertEqual(ans["result"], "planned")
        self.assertEqual(ans["commands"][0][1:], ["--user", "restart", "fleet-server-status.timer"])
        self.assertEqual(set(ans), {"ok", "verb", "target", "commands", "result", "detail"})
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.audit_rows(), [])

    def test_yes_runs_the_argv_with_a_trimmed_env_and_audits(self):
        code, ans = self.run_main(["server", "stop", "fleet-server-status.timer", "--yes", "--json"], self.ctx())
        self.assertEqual((code, ans["result"]), (0, "done"))
        (argv, env), = self.runner.calls
        self.assertEqual(argv[1:], ["--user", "stop", "fleet-server-status.timer"])
        self.assertNotIn("SECRET_THING", env)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            {k: rows[0][k] for k in ("host", "verb", "target", "result")},
            {"host": "server", "verb": "server stop", "target": "fleet-server-status.timer", "result": "done"},
        )
        mode = stat.S_IMODE(os.stat(self.state / "ops-audit.jsonl").st_mode)
        self.assertEqual(mode, 0o600)

    def test_system_scope_has_no_user_flag(self):
        code, ans = self.run_main(["server", "restart", "fleet-cli-git-update.timer", "--plan", "--json"], self.ctx())
        self.assertEqual(code, 0)
        self.assertEqual(ans["commands"][0][1:], ["restart", "fleet-cli-git-update.timer"])

    def test_refuses_without_plan_or_yes(self):
        code, ans = self.run_main(["server", "start", "fleet-server-status.timer", "--json"], self.ctx())
        self.assertEqual((code, ans["result"]), (2, "refused"))
        self.assertEqual(self.runner.calls, [])

    def test_refuses_unknown_unit_and_disallowed_action(self):
        for argv in (
            ["server", "start", "sshd.service", "--yes", "--json"],
            ["server", "stop", "fleet-cli-git-update.timer", "--yes", "--json"],
            ["server", "start", "../../x.service", "--yes", "--json"],
            ["server", "kill", "fleet-server-status.timer", "--yes", "--json"],
            ["server", "start", "fleet-server-status.timer\n", "--yes", "--json"],
            ["server", "update", "fleet-cli\n", "--yes", "--json"],
        ):
            code, ans = self.run_main(argv, self.ctx())
            self.assertEqual((code, ans["result"]), (2, "refused"), argv)
            self.assertEqual(self.runner.calls, [], argv)

    def test_failed_command_is_failed_and_carries_no_output(self):
        code, ans = self.run_main(
            ["server", "start", "fleet-server-status.timer", "--yes", "--json"], self.ctx(runner=FakeRunner(status=5))
        )
        self.assertEqual((code, ans["result"]), (1, "failed"))
        self.assertEqual(ans["detail"], "systemctl start exited 5")
        self.assertEqual(self.audit_rows()[0]["result"], "failed")

    def test_update_starts_the_update_unit(self):
        code, ans = self.run_main(["server", "update", "fleet-cli", "--yes", "--json"], self.ctx())
        self.assertEqual(code, 0)
        (argv, _env), = self.runner.calls
        self.assertEqual(argv[1:], ["start", "--no-block", "fleet-cli-git-update.service"])
        code, _ = self.run_main(["server", "update", "everything", "--yes", "--json"], self.ctx())
        self.assertEqual(code, 2)

    def test_server_verbs_refused_on_the_mac(self):
        code, ans = self.run_main(["server", "start", "fleet-server-status.timer", "--plan", "--json"], self.ctx("darwin"))
        self.assertEqual(code, 2)

    def test_human_output(self):
        code, text = self.run_main(["server", "start", "fleet-server-status.timer", "--plan"], self.ctx())
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("planned: server start"))


class MacVerbTests(Base):
    def plist(self, label, mode=0o600):
        folder = self.home / "Library" / "LaunchAgents"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{label}.plist"
        path.write_text("<plist/>")
        os.chmod(path, mode)
        return path

    def test_restart_loaded_uses_kickstart_k(self):
        code, ans = self.run_main(["mac", "restart", "com.jay.fleet-tools-doctor", "--yes", "--json"], self.ctx("darwin"))
        self.assertEqual(code, 0)
        uid = os.getuid()
        self.assertEqual(
            [c[0] for c in self.runner.calls],
            [
                ["/bin/launchctl", "print", f"gui/{uid}/com.jay.fleet-tools-doctor"],
                ["/bin/launchctl", "kickstart", "-k", f"gui/{uid}/com.jay.fleet-tools-doctor"],
            ],
        )
        self.assertEqual(self.audit_rows()[0]["host"], "mac")

    def test_start_unloaded_bootstraps_the_installed_plist(self):
        plist = self.plist("com.jay.fleet-tools-doctor")
        code, ans = self.run_main(
            ["mac", "start", "com.jay.fleet-tools-doctor", "--plan", "--json"], self.ctx("darwin", FakeRunner(loaded=False))
        )
        self.assertEqual(code, 0)
        uid = os.getuid()
        self.assertEqual(
            ans["commands"],
            [
                ["/bin/launchctl", "bootstrap", f"gui/{uid}", str(plist)],
                ["/bin/launchctl", "kickstart", f"gui/{uid}/com.jay.fleet-tools-doctor"],
            ],
        )

    def test_start_unloaded_refuses_missing_or_shared_plist(self):
        ctx = self.ctx("darwin", FakeRunner(loaded=False))
        code, ans = self.run_main(["mac", "start", "com.jay.fleet-tools-doctor", "--plan", "--json"], ctx)
        self.assertEqual((code, ans["detail"]), (2, "the agent is not installed"))
        self.plist("com.jay.fleet-tools-doctor", mode=0o666)
        code, _ = self.run_main(["mac", "start", "com.jay.fleet-tools-doctor", "--plan", "--json"], ctx)
        self.assertEqual(code, 2)

    def test_stop_loaded_boots_out_and_unloaded_does_nothing(self):
        code, ans = self.run_main(["mac", "stop", "com.jay.fleet-tools-doctor", "--plan", "--json"], self.ctx("darwin"))
        self.assertEqual(ans["commands"][0][:2], ["/bin/launchctl", "bootout"])
        code, ans = self.run_main(
            ["mac", "stop", "com.jay.fleet-tools-doctor", "--yes", "--json"], self.ctx("darwin", FakeRunner(loaded=False))
        )
        self.assertEqual((code, ans["commands"], ans["detail"]), (0, [], "nothing to do"))

    def test_refuses_unlisted_label_action_and_bad_label(self):
        for argv in (
            ["mac", "start", "com.apple.Finder", "--yes", "--json"],
            ["mac", "stop", "com.jay.only-start", "--yes", "--json"],
            ["mac", "start", "com.jay.x;id", "--yes", "--json"],
            ["mac", "start", "com.jay.fleet-tools-doctor\n", "--yes", "--json"],
            ["mac", "update", "com.jay.fleet-tools-doctor", "--yes", "--json"],
        ):
            code, ans = self.run_main(argv, self.ctx("darwin"))
            self.assertEqual((code, ans["result"]), (2, "refused"), argv)
            self.assertEqual(self.runner.calls, [], argv)

    def test_mac_verbs_refused_on_linux(self):
        code, _ = self.run_main(["mac", "start", "com.jay.fleet-tools-doctor", "--plan", "--json"], self.ctx("linux"))
        self.assertEqual(code, 2)


class RedeployTests(Base):
    ROWS = [
        {"uuid": UUID, "name": "fleet-recall", "type": "application", "status": "running"},
        {"uuid": "standalone-box", "name": "standalone-box", "type": "standalone", "status": "running"},
    ]

    def test_plan_checks_the_live_report(self):
        self.write_status(self.ROWS)
        code, ans = self.run_main(["server", "redeploy", UUID, "--plan", "--json"], self.ctx())
        self.assertEqual(code, 0)
        self.assertEqual(ans["commands"], [["coolify", "GET", f"/api/v1/deploy?uuid={UUID}"]])
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.runner.calls, [])

    def test_yes_sends_the_token_only_in_a_header_and_audits_the_name(self):
        self.write_status(self.ROWS)
        code, ans = self.run_main(["server", "redeploy", UUID, "--yes", "--json"], self.ctx())
        self.assertEqual((code, ans["result"]), (0, "done"), ans)
        (url, headers), = self.http_calls
        self.assertEqual(url, f"http://127.0.0.1:8000/api/v1/deploy?uuid={UUID}")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        for argv, _env in self.runner.calls:
            self.assertNotIn(TOKEN, " ".join(argv))
        self.assertNotIn(TOKEN, json.dumps(ans))
        audit_text = (self.state / "ops-audit.jsonl").read_text()
        self.assertNotIn(TOKEN, audit_text)
        self.assertNotIn(UUID, audit_text)
        self.assertEqual(self.audit_rows()[0]["target"], "fleet-recall")

    def test_refuses_uuid_not_in_report_standalone_stale_or_unsafe_report(self):
        cases = [
            (self.ROWS, 60, 0o600, "fleet-server-status", "zzzzzzzzzz"),
            (self.ROWS, 60, 0o600, "fleet-server-status", "standalone-box"),
            (self.ROWS, 60, 0o600, "fleet-server-status", "standalonebox"),
            (self.ROWS, 3 * 3600, 0o600, "fleet-server-status", UUID),
            (self.ROWS, 60, 0o666, "fleet-server-status", UUID),
            (self.ROWS, 60, 0o600, "something-else", UUID),
        ]
        for rows, age, mode, tool, uuid in cases:
            self.write_status(rows, age=age, mode=mode, tool=tool)
            code, ans = self.run_main(["server", "redeploy", uuid, "--yes", "--json"], self.ctx())
            self.assertEqual((code, ans["result"]), (2, "refused"), (age, mode, tool, uuid))
            self.assertEqual(self.http_calls, [])
        self.assertNotIn(UUID, (self.state / "ops-audit.jsonl").read_text())

    def test_refuses_rows_whose_uuid_is_not_a_coolify_uuid(self):
        # A service, a database, or an application without a uuid label:  the collector put
        # a name in `uuid`, and a name must never reach Coolify.
        rows = [
            {"uuid": "qdrantst", "name": "qdrantst", "type": "service", "status": "running"},
            {"uuid": "svcuuid123456", "name": "recall-api", "type": "service", "status": "running"},
            {"uuid": "postgres16", "name": "postgres16", "type": "database", "status": "running"},
            {"uuid": "echomail", "name": "echomail", "type": "application", "status": "running"},
            {"uuid": "echomail2", "name": "echomail2 (relay)", "type": "application", "status": "running"},
            {"uuid": "labelled123", "name": "x", "type": "application", "uuidSource": "name"},
        ]
        self.write_status(rows)
        for row in rows:
            code, ans = self.run_main(["server", "redeploy", row["uuid"], "--yes", "--json"], self.ctx())
            self.assertEqual((code, ans["result"]), (2, "refused"), row)
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(all(r["target"] == "coolify resource" for r in self.audit_rows()))

    def test_uuid_from_label(self):
        self.assertTrue(foc.uuid_from_label({"uuid": UUID, "name": "fleet-recall"}))
        self.assertTrue(foc.uuid_from_label({"uuid": UUID, "name": UUID + "-123456"}))
        self.assertTrue(foc.uuid_from_label({"uuid": "same", "name": "same", "uuidSource": "label"}))
        self.assertFalse(foc.uuid_from_label({"uuid": "same", "name": "same"}))
        self.assertFalse(foc.uuid_from_label({"uuid": "same", "name": "same (web)"}))
        self.assertFalse(foc.uuid_from_label({"uuid": UUID, "name": "x", "uuidSource": "resource"}))
        self.assertFalse(foc.uuid_from_label({"uuid": UUID}))

    def test_refuses_a_uuid_with_a_trailing_newline(self):
        self.write_status(self.ROWS)
        code, ans = self.run_main(["server", "redeploy", UUID + "\n", "--plan", "--json"], self.ctx())
        self.assertEqual((code, ans["detail"]), (2, "the uuid is not valid"))

    def test_refuses_without_a_report(self):
        code, ans = self.run_main(["server", "redeploy", UUID, "--plan", "--json"], self.ctx())
        self.assertEqual(ans["detail"], "there is no server status report")

    def test_missing_token_and_bad_answer_fail(self):
        self.write_status(self.ROWS)
        code, ans = self.run_main(["server", "redeploy", UUID, "--yes", "--json"], self.ctx(runner=FakeRunner(secret="")))
        self.assertEqual((code, ans["result"]), (1, "failed"))
        self.assertEqual(self.http_calls, [])

    def test_base_url_must_be_https_or_loopback(self):
        for base, ok in (
            ("https://coolify.example.test", True),
            ("http://localhost:8000", True),
            ("http://coolify.example.test", False),
            ("https://coolify.example.test/api", False),
            ("https://user@coolify.example.test", False),
            ("file:///etc/passwd", False),
        ):
            ctx = self.ctx(environ={"COOLIFY_BASE_URL": base})
            if ok:
                self.assertEqual(foc.coolify_base(ctx), base)
            else:
                with self.assertRaises(foc.Refused, msg=base):
                    foc.coolify_base(ctx)


class CredentialTests(Base):
    """The Infisical identity fleet-secrets logs in with, from the root-only file."""

    ROWS = RedeployTests.ROWS
    PAIR = {"INFISICAL_AUTOMATION_CLIENT_ID": "id-" + "a" * 30, "INFISICAL_AUTOMATION_CLIENT_SECRET": "sec-" + "b" * 40}

    def credentials(self, text, mode=0o600):
        folder = self.home / ".config" / "fleet-tools"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / "ops-control.env"
        path.write_text(text)
        os.chmod(path, mode)
        return path

    def pair_text(self):
        return "# fleet-ops-control\n" + "".join(f"{k}={v}\n" for k, v in self.PAIR.items())

    def redeploy(self, environ=None):
        self.write_status(self.ROWS)
        environ = environ or {"PATH": "/usr/bin", "INFISICAL_TOKEN": "stale-token-from-env"}
        return self.run_main(["server", "redeploy", UUID, "--yes", "--json"], self.ctx(environ=environ))

    def secrets_env(self):
        (argv, env), = [c for c in self.runner.calls if c[0][1:2] == ["get"]]
        return env

    def test_file_is_the_one_identity_and_reaches_only_fleet_secrets(self):
        self.credentials(self.pair_text())
        code, ans = self.redeploy()
        self.assertEqual((code, ans["result"]), (0, "done"), ans)
        env = self.secrets_env()
        for key, value in self.PAIR.items():
            self.assertEqual(env[key], value)
        self.assertNotIn("INFISICAL_TOKEN", env)
        text = json.dumps(ans) + (self.state / "ops-audit.jsonl").read_text()
        for value in self.PAIR.values():
            self.assertNotIn(value, text)
            for argv, _env in self.runner.calls:
                self.assertNotIn(value, " ".join(argv))
        # A systemctl child never sees it.
        self.runner.calls.clear()
        self.run_main(["server", "stop", "fleet-server-status.timer", "--yes", "--json"], self.ctx())
        (argv, env), = self.runner.calls
        self.assertFalse(set(self.PAIR) & set(env))

    def test_a_token_line_alone_is_enough(self):
        self.credentials("INFISICAL_TOKEN=st." + "c" * 40 + "\n")
        code, _ = self.redeploy({"PATH": "/usr/bin", **self.PAIR})
        self.assertEqual(code, 0)
        env = self.secrets_env()
        self.assertEqual(env["INFISICAL_TOKEN"], "st." + "c" * 40)
        self.assertFalse(set(self.PAIR) & set(env))

    def test_no_file_keeps_the_process_environment(self):
        code, _ = self.redeploy({"PATH": "/usr/bin", **self.PAIR})
        self.assertEqual(code, 0)
        env = self.secrets_env()
        self.assertEqual({k: env[k] for k in self.PAIR}, self.PAIR)

    def test_a_shared_or_malformed_file_fails_before_fleet_secrets_runs(self):
        cases = [
            (self.pair_text(), 0o640),
            (self.pair_text(), 0o604),
            ("INFISICAL_AUTOMATION_CLIENT_ID=only-half\n", 0o600),
            ("INFISICAL_TOKEN=\"quoted\"\n", 0o600),
            ("PATH=/tmp/evil\n", 0o600),
            ("INFISICAL_TOKEN=a b\n", 0o600),
            ("INFISICAL_TOKEN=x\nINFISICAL_TOKEN=y\n", 0o600),
            ("export INFISICAL_TOKEN=x\n", 0o600),
            ("# nothing\n", 0o600),
        ]
        for text, mode in cases:
            self.credentials(text, mode)
            code, ans = self.redeploy()
            self.assertEqual((code, ans["result"]), (1, "failed"), (text, oct(mode)))
            self.assertIn(
                ans["detail"],
                ("the Infisical credential file is not private", "the Infisical credential file is malformed"),
            )
            self.assertEqual([c for c in self.runner.calls if c[0][1:2] == ["get"]], [])
            self.assertEqual(self.http_calls, [])

    def test_a_symlinked_file_is_refused(self):
        real = self.home / "elsewhere.env"
        real.write_text(self.pair_text())
        os.chmod(real, 0o600)
        folder = self.home / ".config" / "fleet-tools"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "ops-control.env").symlink_to(real)
        code, ans = self.redeploy()
        self.assertEqual((code, ans["detail"]), (1, "the Infisical credential file cannot be read"))

    def test_check_token_reads_the_token_and_asks_coolify_for_its_version(self):
        self.credentials(self.pair_text())
        code, ans = self.run_main(["--check-token"], self.ctx())
        self.assertEqual((code, ans["result"]), (0, "done"), ans)
        (url, headers), = self.http_calls
        self.assertEqual(url, "http://127.0.0.1:8000/api/v1/version")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, json.dumps(ans))
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(self.secrets_env()["INFISICAL_AUTOMATION_CLIENT_ID"], self.PAIR["INFISICAL_AUTOMATION_CLIENT_ID"])

    def test_check_token_failures(self):
        code, ans = self.run_main(["--check-token"], self.ctx(runner=FakeRunner(secret="")))
        self.assertEqual((code, ans["detail"]), (1, "the Coolify token is not available"))
        self.assertEqual(self.http_calls, [])
        code, ans = self.run_main(["--check-token"], self.ctx(http_status=401))
        self.assertEqual((code, ans["detail"]), (1, "Coolify answered HTTP 401"))
        code, ans = self.run_main(["--check-token"], self.ctx("darwin"))
        self.assertEqual((code, ans["result"]), (2, "refused"))
        code, ans = self.run_main(["--check-token", "--json"], self.ctx())
        self.assertEqual((code, ans["result"]), (2, "refused"))


class SshGateTests(Base):
    def ssh(self, command, argv=("--from-ssh",)):
        environ = {"PATH": "/usr/bin"}
        if command is not None:
            environ["SSH_ORIGINAL_COMMAND"] = command
        return self.run_main(list(argv), self.ctx(environ=environ))

    def test_accepts_a_server_verb(self):
        code, ans = self.ssh("server restart fleet-server-status.timer --plan --json")
        self.assertEqual((code, ans["result"]), (0, "planned"))
        code, ans = self.ssh("server update fleet-cli --yes")
        self.assertEqual((code, ans["result"]), (0, "done"))

    def test_rejects_everything_else(self):
        for command in (
            None,
            "",
            "server restart fleet-server-status.timer --plan; id",
            "server restart fleet-server-status.timer --plan && id",
            "server restart fleet-server-status.timer --plan | sh",
            "server restart $(id).timer --plan",
            "server restart `id` --plan",
            "server restart 'fleet-server-status.timer' --plan",
            "server restart fleet-server-status.timer\n--plan",
            "server restart fleet-server-status.timer --plan\n",
            "server restart fleet-server-status.timer\n --plan",
            "--check-token",
            "server redeploy abc123def456 --check-token",
            "server restart fleet-server-status.timer",
            "server restart fleet-server-status.timer --plan --yes",
            "server restart fleet-server-status.timer --plan --plan",
            "server restart fleet-server-status.timer extra --plan",
            "server restart fleet-server-status.timer --plan --config=/tmp/x",
            "--plan server restart fleet-server-status.timer",
            "mac restart com.jay.fleet-tools-doctor --plan",
            "server restart sshd.service --plan",
            "server shell bash --plan",
            "x" * 300,
        ):
            code, ans = self.ssh(command)
            self.assertEqual((code, ans["result"]), (2, "refused"), command)
        self.assertEqual(self.runner.calls, [])

    def test_from_ssh_takes_no_other_arguments(self):
        code, ans = self.ssh("server update fleet-cli --plan", argv=("--from-ssh", "--json"))
        self.assertEqual(code, 2)
        code, ans = self.ssh("server update fleet-cli --plan", argv=("server", "update", "fleet-cli", "--from-ssh"))
        self.assertEqual(code, 2)


class SourceTests(unittest.TestCase):
    def test_no_shell_true_anywhere(self):
        text = Path(HERE, "fleet-ops-control.py").read_text()
        self.assertNotIn("shell=True", text)
        self.assertNotIn("os.system", text)
        self.assertNotIn("os.popen", text)


if __name__ == "__main__":
    unittest.main()
