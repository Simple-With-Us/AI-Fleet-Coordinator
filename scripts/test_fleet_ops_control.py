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
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("foc", os.path.join(HERE, "fleet-ops-control.py"))
foc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(foc)

UUID = "abc123def456"
TOKEN = "tok-" + "z" * 40
NOW = 1_800_000_000.0


def stamp(epoch):
    return datetime.datetime.fromtimestamp(epoch, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


CONFIG = {
    "schemaVersion": 1,
    "mac": {
        "agents": [
            {"label": "com.jay.fleet-tools-doctor", "actions": ["start", "stop", "restart"]},
            {"label": "com.jay.only-start", "actions": ["start"]},
        ],
        "pm2Apps": [
            {"name": "shellular", "actions": ["start", "stop", "restart"]},
            {"name": "only-start-app", "actions": ["start"]},
        ],
    },
    "server": {
        "units": [
            {
                "unit": "fleet-server-status.timer",
                "scope": "user",
                "actions": ["start", "stop", "restart"],
            },
            {"unit": "fleet-cli-git-update.timer", "scope": "system", "actions": ["restart"]},
        ],
        "updates": [
            {"name": "fleet-cli", "unit": "fleet-cli-git-update.service", "scope": "system"}
        ],
        "coolify": {"redeploy": True},
    },
}


def update_state(**changes):
    state = {
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "Job": "",
        "Type": "oneshot",
        "InvocationID": "a" * 32,
        "Result": "success",
        "ExecMainCode": "1",
        "ExecMainStatus": "0",
        "ExecMainStartTimestampMonotonic": "100",
        "ExecMainExitTimestampMonotonic": "110",
    }
    state.update(changes)
    return "".join(f"{key}={value}\n" for key, value in state.items()).encode()


class FakeRunner:
    """Records every argv;  answers `launchctl print` with `loaded` and the rest with `status`."""

    def __init__(
        self, loaded=True, status=0, secret=TOKEN, log_bytes=b"line one\nline two\n", jlist=None
    ):
        self.calls = []
        self.loaded = loaded
        self.status = status
        self.secret = secret
        self.log_bytes = log_bytes
        self.jlist = jlist
        self.max_bytes_seen = []

    def __call__(
        self,
        argv,
        timeout=None,
        env=None,
        want_stdout=False,
        max_bytes=None,
        combine_stderr=False,
    ):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        self.calls.append((list(argv), dict(env or {})))
        self.max_bytes_seen.append(max_bytes)
        if "show" in argv and "fleet-cli-git-update.service" in argv:
            return 0, update_state()
        if argv[1:2] == ["print"]:
            return (0 if self.loaded else 113), b""
        if argv[1:2] == ["get"]:
            return (0, self.secret.encode()) if self.secret else (3, b"")
        if argv[-1:] == ["jlist"]:
            return self.status, (self.jlist if want_stdout and self.jlist is not None else b"")
        if "journalctl" in argv[0]:
            return self.status, (self.log_bytes if want_stdout else b"")
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
            pm2_node="/fake/bin/node",
            pm2_script="/fake/lib/pm2",
            sleep=lambda _: None,
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
            json.dumps(
                {"schemaVersion": 1, "tool": tool, "generatedAt": stamp(NOW - age), "coolify": rows}
            )
        )
        os.chmod(path, mode)
        return path


class AllowlistTests(Base):
    def test_repo_allowlist_is_valid_and_covers_every_plist(self):
        config = foc.load_config()
        plists = Path(HERE, "launchd").glob("*.plist")
        for plist in plists:
            text = plist.read_text()
            label = (
                text.split("<key>Label</key>", 1)[1]
                .split("<string>", 1)[1]
                .split("</string>", 1)[0]
            )
            self.assertIn(label.strip(), config["agents"], plist.name)
        self.assertIn("com.jay.fleet-worker-health", config["agents"])
        self.assertIn("com.jay.fleet-mac-services", config["agents"])
        self.assertEqual(config["units"]["fleet-server-status.timer"]["scope"], "user")
        self.assertEqual(config["updates"]["fleet-cli"]["unit"], "fleet-cli-git-update.service")
        self.assertTrue(config["redeploy"])

    def test_malformed_entries_refuse_the_whole_file(self):
        bad_docs = [
            {"schemaVersion": 2},
            {
                "schemaVersion": 1,
                "mac": {"agents": [{"label": "com.jay.x; rm -rf /", "actions": ["start"]}]},
            },
            {
                "schemaVersion": 1,
                "mac": {"agents": [{"label": "com.jay.x", "actions": ["delete"]}]},
            },
            {"schemaVersion": 1, "mac": {"agents": [{"label": "com.jay.x", "actions": []}]}},
            {
                "schemaVersion": 1,
                "server": {
                    "units": [{"unit": "../etc.service", "scope": "user", "actions": ["start"]}]
                },
            },
            {
                "schemaVersion": 1,
                "server": {"units": [{"unit": "x.mount", "scope": "user", "actions": ["start"]}]},
            },
            {
                "schemaVersion": 1,
                "server": {
                    "units": [{"unit": "x@y.service", "scope": "user", "actions": ["start"]}]
                },
            },
            {
                "schemaVersion": 1,
                "server": {"units": [{"unit": "x.service", "scope": "root", "actions": ["start"]}]},
            },
            {
                "schemaVersion": 1,
                "server": {"updates": [{"name": "x", "unit": "x.timer", "scope": "system"}]},
            },
            {"schemaVersion": 1, "server": {"coolify": {"redeploy": "yes"}}},
            {
                "schemaVersion": 1,
                "mac": {"agents": [{"label": "com.jay.x\n", "actions": ["start"]}]},
            },
            {
                "schemaVersion": 1,
                "server": {
                    "units": [{"unit": "x.service\n", "scope": "user", "actions": ["start"]}]
                },
            },
            {
                "schemaVersion": 1,
                "server": {"updates": [{"name": "x\n", "unit": "x.service", "scope": "system"}]},
            },
        ]
        for doc in bad_docs:
            with self.assertRaises(foc.Refused, msg=json.dumps(doc)):
                foc.validate_config(doc)


class ServerVerbTests(Base):
    def test_plan_prints_argv_and_runs_nothing(self):
        code, ans = self.run_main(
            ["server", "restart", "fleet-server-status.timer", "--plan", "--json"], self.ctx()
        )
        self.assertEqual(code, 0)
        self.assertEqual(ans["result"], "planned")
        self.assertEqual(ans["commands"][0][1:], ["--user", "restart", "fleet-server-status.timer"])
        self.assertEqual(set(ans), {"ok", "verb", "target", "commands", "result", "detail"})
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.audit_rows(), [])

    def test_yes_runs_the_argv_with_a_trimmed_env_and_audits(self):
        code, ans = self.run_main(
            ["server", "stop", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
        )
        self.assertEqual((code, ans["result"]), (0, "done"))
        ((argv, env),) = self.runner.calls
        self.assertEqual(argv[1:], ["--user", "stop", "fleet-server-status.timer"])
        self.assertNotIn("SECRET_THING", env)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            {k: rows[0][k] for k in ("host", "verb", "target", "result")},
            {
                "host": "server",
                "verb": "server stop",
                "target": "fleet-server-status.timer",
                "result": "done",
            },
        )
        mode = stat.S_IMODE(os.stat(self.state / "ops-audit.jsonl").st_mode)
        self.assertEqual(mode, 0o600)

    def test_system_scope_has_no_user_flag(self):
        code, ans = self.run_main(
            ["server", "restart", "fleet-cli-git-update.timer", "--plan", "--json"], self.ctx()
        )
        self.assertEqual(code, 0)
        self.assertEqual(ans["commands"][0][1:], ["restart", "fleet-cli-git-update.timer"])

    def test_refuses_without_plan_or_yes(self):
        code, ans = self.run_main(
            ["server", "start", "fleet-server-status.timer", "--json"], self.ctx()
        )
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
            ["server", "start", "fleet-server-status.timer", "--yes", "--json"],
            self.ctx(runner=FakeRunner(status=5)),
        )
        self.assertEqual((code, ans["result"]), (1, "failed"))
        self.assertEqual(ans["detail"], "systemctl start exited 5")
        self.assertEqual(self.audit_rows()[0]["result"], "failed")

    def test_update_starts_the_update_unit(self):
        code, ans = self.run_main(["server", "update", "fleet-cli", "--yes", "--json"], self.ctx())
        self.assertEqual(code, 0)
        argv = next(
            argv for argv, _ in self.runner.calls if "--no-block" in argv and "-l" not in argv
        )
        self.assertEqual(argv[-3:], ["start", "--no-block", "fleet-cli-git-update.service"])
        self.assertEqual(ans["result"], "submitted")
        self.assertFalse(ans["verified"])
        code, _ = self.run_main(["server", "update", "everything", "--yes", "--json"], self.ctx())
        self.assertEqual(code, 2)

    def test_server_verbs_refused_on_the_mac(self):
        code, ans = self.run_main(
            ["server", "start", "fleet-server-status.timer", "--plan", "--json"], self.ctx("darwin")
        )
        self.assertEqual(code, 2)

    def test_human_output(self):
        code, text = self.run_main(
            ["server", "start", "fleet-server-status.timer", "--plan"], self.ctx()
        )
        self.assertEqual(code, 0)
        self.assertTrue(text.startswith("planned: server start"))


class UpdateReceiptTests(Base):
    def update(self, states=None, submit=(0, b""), permission=0, uid=1001):
        sequence = list(states or [update_state()])
        self.update_calls = []

        def runner(argv, **kwargs):
            self.update_calls.append((argv, kwargs))
            if "show" in argv:
                return 0, sequence.pop(0) if len(sequence) > 1 else sequence[0]
            if argv[:4] == [foc.SUDO, "-n", "-l", "--"]:
                return permission, b""
            return submit

        ctx = self.ctx(runner=runner)
        ctx.uid = uid
        return self.run_main(["server", "update", "fleet-cli", "--yes", "--json"], ctx)

    def test_old_success_is_never_completion(self):
        code, answer = self.update()
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "submitted", False))
        self.assertNotIn("invocationID", answer)
        audit = self.audit_rows()[0]
        self.assertEqual(audit["route"], "sudo-system-update")
        self.assertFalse(audit["verified"])

    def test_new_success_is_verified(self):
        old = update_state()
        new = update_state(
            InvocationID="b" * 32,
            ExecMainStartTimestampMonotonic="200",
            ExecMainExitTimestampMonotonic="210",
        )
        code, answer = self.update([old, old, new])
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "done", True))
        self.assertEqual(answer["invocationID"], "b" * 32)
        self.assertEqual(answer["executionState"], "completed")
        audit = self.audit_rows()[0]
        self.assertEqual(audit["invocationID"], "b" * 32)
        self.assertTrue(audit["verified"])

    def test_new_invocation_with_old_process_times_is_not_completion(self):
        code, answer = self.update(
            [update_state(), update_state(), update_state(InvocationID="b" * 32)]
        )
        self.assertEqual((code, answer["result"]), (0, "submitted"))
        self.assertEqual(answer["baselineStartMonotonic"], 100)
        original_id = answer["invocationID"]

        def status_runner(argv, **kwargs):
            self.assertIn("show", argv)
            return 0, update_state(InvocationID=original_id)

        later, later_code = foc.update_status(
            self.ctx(runner=status_runner),
            foc.validate_config(CONFIG),
            "fleet-cli." + original_id + "." + str(answer["baselineStartMonotonic"]),
            "plan",
        )
        self.assertEqual((later_code, later["result"], later["verified"]), (0, "submitted", False))
        self.assertEqual(later["baselineStartMonotonic"], answer["baselineStartMonotonic"])

    def test_busy_update_is_refused_without_submission(self):
        for changes in ({"ActiveState": "activating"}, {"Job": "7"}):
            code, answer = self.update([update_state(**changes)])
            self.assertEqual((code, answer["errorCode"]), (2, "update_busy"))
            self.assertTrue(all("show" in argv for argv, _ in self.update_calls))

    def test_busy_recheck_refuses_race_after_plan(self):
        code, answer = self.update([update_state(), update_state(ActiveState="activating")])
        self.assertEqual((code, answer["errorCode"]), (2, "update_busy"))
        self.assertFalse(
            any("--no-block" in argv and "-l" not in argv for argv, _ in self.update_calls)
        )

    def test_missing_unit_is_refused_without_sudo(self):
        code, answer = self.update([b"LoadState=not-found\n"])
        self.assertEqual((code, answer["errorCode"]), (2, "unit_unavailable"))
        self.assertEqual(len(self.update_calls), 1)

    def test_missing_authorization_is_refused_without_submission(self):
        code, answer = self.update(permission=1)
        self.assertEqual((code, answer["errorCode"]), (2, "authorization_unavailable"))
        self.assertEqual(len(self.update_calls), 2)

    def test_execution_rechecks_permission_and_refuses_revocation(self):
        count = 0

        def runner(argv, **kwargs):
            nonlocal count
            if "show" in argv:
                return 0, update_state()
            if "-l" in argv:
                count += 1
                return (0 if count == 1 else 1), b""
            self.fail("no mutation is allowed after permission revocation")

        code, answer = self.run_main(
            ["server", "update", "fleet-cli", "--yes", "--json"],
            self.ctx(runner=runner),
        )
        self.assertEqual((code, answer["errorCode"], count), (2, "authorization_unavailable", 2))

    def test_sudo_wraps_only_exact_systemctl_argv(self):
        self.update()
        commands = [argv for argv, _ in self.update_calls if "--no-block" in argv]
        self.assertEqual(
            commands,
            [
                [
                    foc.SUDO,
                    "-n",
                    "-l",
                    "--",
                    foc._systemctl(),
                    "start",
                    "--no-block",
                    "fleet-cli-git-update.service",
                ],
                [
                    foc.SUDO,
                    "-n",
                    "-l",
                    "--",
                    foc._systemctl(),
                    "start",
                    "--no-block",
                    "fleet-cli-git-update.service",
                ],
                [
                    foc.SUDO,
                    "-n",
                    foc._systemctl(),
                    "start",
                    "--no-block",
                    "fleet-cli-git-update.service",
                ],
            ],
        )

    def test_config_cannot_select_another_privileged_update(self):
        for name, unit in (
            ("other", "fleet-cli-git-update.service"),
            ("fleet-cli", "sshd.service"),
        ):
            ctx = self.ctx()
            with self.assertRaises(foc.Refused) as raised:
                foc.update_command(ctx, name, {"scope": "system", "unit": unit})
            self.assertEqual(raised.exception.error_code, "update_route_unavailable")
            self.assertEqual(self.runner.calls, [])

    def test_other_system_controls_never_gain_sudo_fallback(self):
        ctx = self.ctx()
        config = foc.validate_config(CONFIG)
        commands, _ = foc.server_unit_commands(ctx, config, "restart", "fleet-cli-git-update.timer")
        self.assertEqual(commands, [[foc._systemctl(), "restart", "fleet-cli-git-update.timer"]])
        self.assertEqual(self.runner.calls, [])

    def test_privileged_executable_is_not_selected_from_candidates(self):
        with mock.patch.object(foc, "_systemctl", return_value="/untrusted/systemctl"):
            command = foc.update_command(
                self.ctx(), "fleet-cli", {"scope": "system", "unit": foc.FLEET_CLI_UPDATE_UNIT}
            )
        self.assertEqual(
            command,
            [
                foc.SUDO,
                "-n",
                "/usr/bin/systemctl",
                "start",
                "--no-block",
                foc.FLEET_CLI_UPDATE_UNIT,
            ],
        )

    def test_missing_pinned_executables_refuse_before_sudo_query(self):
        for missing, expected in (
            (foc.UPDATE_SYSTEMCTL, "update_executable_unavailable"),
            (foc.SUDO, "authorization_unavailable"),
        ):
            ctx = self.ctx()
            with mock.patch.object(foc.os.path, "isfile", side_effect=lambda path: path != missing):
                with self.assertRaises(foc.Refused) as raised:
                    foc.update_command(
                        ctx, "fleet-cli", {"scope": "system", "unit": foc.FLEET_CLI_UPDATE_UNIT}
                    )
            self.assertEqual(raised.exception.error_code, expected)
            self.assertEqual(self.runner.calls, [])

    def test_invalid_probe_cannot_verify_completion(self):
        code, answer = self.update([update_state(), update_state(), b"LoadState=loaded\n"])
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "submitted", False))

    def test_plan_checks_permission_without_mutation(self):
        code, answer = self.run_main(
            ["server", "update", "fleet-cli", "--plan", "--json"],
            self.ctx(),
        )
        self.assertEqual((code, answer["result"]), (0, "planned"))
        self.assertEqual(answer["commands"][0][:3], [foc.SUDO, "-n", "/usr/bin/systemctl"])
        self.assertFalse(
            any("--no-block" in argv and "-l" not in argv for argv, _ in self.runner.calls)
        )
        self.assertEqual(self.audit_rows(), [])

    def test_root_uses_direct_systemctl(self):
        self.update(uid=0)
        self.assertFalse(any(argv[0] == foc.SUDO for argv, _ in self.update_calls))

    def test_private_stderr_is_classified_and_never_returned(self):
        marker = b"Access denied SECRET_HOST_AND_TOKEN"
        code, answer = self.update(submit=(1, marker))
        self.assertEqual((code, answer["errorCode"]), (1, "authorization_unavailable"))
        self.assertNotIn("SECRET", json.dumps(answer))
        self.assertNotIn("SECRET", (self.state / "ops-audit.jsonl").read_text())

    def test_submission_timeout_and_missing_executable_have_fixed_codes(self):
        for status, expected in (
            (124, "update_submit_timeout"),
            (127, "update_executable_unavailable"),
        ):
            code, answer = self.update(submit=(status, b"private detail"))
            self.assertEqual((code, answer["errorCode"]), (1, expected))

    def test_new_failed_invocation_is_reported(self):
        old = update_state()
        failed = update_state(
            InvocationID="b" * 32, ActiveState="failed", Result="exit-code", ExecMainStatus="1"
        )
        code, answer = self.update([old, old, failed])
        self.assertEqual((code, answer["result"], answer["verified"]), (1, "failed", True))
        self.assertEqual(answer["errorCode"], "update_invocation_failed")

    def test_running_invocation_remains_submitted(self):
        old = update_state()
        code, answer = self.update(
            [old, old, update_state(InvocationID="b" * 32, ActiveState="activating")]
        )
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "submitted", False))

    def test_replaced_invocation_is_not_credited(self):
        old = update_state()
        running = update_state(InvocationID="b" * 32, ActiveState="activating")
        replaced = update_state(
            InvocationID="c" * 32,
            ExecMainStartTimestampMonotonic="200",
            ExecMainExitTimestampMonotonic="210",
        )
        code, answer = self.update([old, old, running, replaced])
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "submitted", False))
        self.assertNotIn("invocationID", answer)
        self.assertEqual(answer["executionState"], "expired")
        self.assertEqual(answer["errorCode"], "invocation_replaced")

    def test_verification_has_a_monotonic_deadline(self):
        clock = [0.0]
        waits = []

        def runner(argv, **kwargs):
            if "show" in argv:
                clock[0] += kwargs["timeout"]
                return 0, update_state()
            return 0, b""

        def sleep(seconds):
            waits.append(seconds)
            clock[0] += seconds

        ctx = self.ctx(runner=runner)
        ctx.monotonic = lambda: clock[0]
        ctx.sleep = sleep
        code, answer = self.run_main(["server", "update", "fleet-cli", "--yes", "--json"], ctx)
        self.assertEqual((code, answer["result"]), (0, "submitted"))
        # Two pre-submission probes consume six seconds; observation has its own 12s bound.
        self.assertLessEqual(clock[0], 2 * foc.UPDATE_PROBE_TIMEOUT + foc.UPDATE_VERIFY_SECONDS)
        self.assertTrue(waits)

    def test_update_longer_than_old_window_can_complete(self):
        clock = [0.0]
        probes = [0]

        def runner(argv, **kwargs):
            if "show" not in argv:
                return 0, b""
            probes[0] += 1
            if probes[0] <= 2:
                return 0, update_state()
            return 0, update_state(
                InvocationID="b" * 32,
                ActiveState="inactive" if clock[0] >= 5 else "activating",
                ExecMainStartTimestampMonotonic="200",
                ExecMainExitTimestampMonotonic="210",
            )

        ctx = self.ctx(runner=runner)
        ctx.monotonic = lambda: clock[0]
        ctx.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        code, answer = self.run_main(["server", "update", "fleet-cli", "--yes", "--json"], ctx)
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "done", True))
        self.assertGreater(clock[0], 3.5)


class UpdateStatusTests(Base):
    def status(self, state=None, target=None, mode="--plan", config=None):
        if config is not None:
            self.config_path.write_text(json.dumps(config))
        self.status_calls = []

        def runner(argv, **kwargs):
            self.status_calls.append(argv)
            self.assertNotIn(foc.SUDO, argv)
            self.assertIn("show", argv)
            return 0, state or update_state(InvocationID="b" * 32)

        return self.run_main(
            ["server", "update-status", target or ("fleet-cli." + "b" * 32 + ".0"), mode, "--json"],
            self.ctx(runner=runner),
        )

    def test_exact_captured_success_is_read_only(self):
        code, answer = self.status()
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "done", True))
        self.assertEqual(answer["invocationID"], "b" * 32)
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(len(self.status_calls), 1)

    def test_running_invocation_remains_submitted(self):
        code, answer = self.status(update_state(InvocationID="b" * 32, ActiveState="activating"))
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "submitted", False))

    def test_failed_invocation_reports_verified_failure(self):
        code, answer = self.status(
            update_state(InvocationID="b" * 32, ActiveState="failed", Result="exit-code")
        )
        self.assertEqual((code, answer["result"], answer["verified"]), (1, "failed", True))

    def test_old_or_replaced_success_never_verifies_requested_id(self):
        for other in ("a" * 32, "c" * 32, ""):
            code, answer = self.status(update_state(InvocationID=other))
            self.assertEqual((code, answer["result"], answer["verified"]), (0, "submitted", False))
            self.assertEqual(answer["invocationID"], "b" * 32)
            self.assertEqual(answer["errorCode"], "invocation_not_current")
            self.assertEqual(answer["executionState"], "expired")

    def test_no_captured_or_malformed_id_refused_before_probe(self):
        for target in (
            "fleet-cli",
            "fleet-cli.",
            "fleet-cli." + "B" * 32 + ".0",
            "other." + "b" * 32 + ".0",
            "fleet-cli." + "b" * 32 + ".extra",
            "fleet-cli." + "b" * 32,
            "fleet-cli." + "b" * 32 + ".00",
            "fleet-cli." + "b" * 32 + ".-1",
            "fleet-cli." + "b" * 32 + ".18446744073709551616",
        ):
            code, answer = self.status(target=target)
            self.assertEqual((code, answer["errorCode"]), (2, "invocation_id_invalid"))
            self.assertEqual(self.status_calls, [])

    def test_yes_is_refused_without_mutation_or_audit(self):
        code, answer = self.status(mode="--yes")
        self.assertEqual((code, answer["errorCode"]), (2, "read_only_required"))
        self.assertEqual(self.status_calls, [])
        self.assertEqual(self.audit_rows(), [])

    def test_config_cannot_retarget_the_status_unit(self):
        config = json.loads(json.dumps(CONFIG))
        config["server"]["updates"][0]["unit"] = "sshd.service"
        code, answer = self.status(config=config)
        self.assertEqual((code, answer["errorCode"]), (2, "update_route_unavailable"))
        self.assertEqual(self.status_calls, [])

    def test_ssh_gate_accepts_typed_read_only_receipt(self):
        words = foc.parse_ssh_command(
            "server update-status fleet-cli." + "b" * 32 + ".0 --plan --json"
        )
        self.assertEqual(words[1], "update-status")

    def test_invalid_process_status_cannot_verify(self):
        code, answer = self.status(update_state(InvocationID="b" * 32, ExecMainCode="0"))
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "submitted", False))

    def test_recheck_preserves_the_captured_freshness_baseline(self):
        current = update_state(InvocationID="b" * 32)
        for baseline, expected in (("100", "submitted"), ("110", "submitted"), ("99", "done")):
            code, answer = self.status(current, target="fleet-cli." + "b" * 32 + "." + baseline)
            self.assertEqual((code, answer["result"]), (0, expected))
            self.assertEqual(answer["baselineStartMonotonic"], int(baseline))


class MacVerbTests(Base):
    def plist(self, label, mode=0o600):
        folder = self.home / "Library" / "LaunchAgents"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{label}.plist"
        path.write_text("<plist/>")
        os.chmod(path, mode)
        return path

    def test_restart_loaded_uses_kickstart_k(self):
        code, ans = self.run_main(
            ["mac", "restart", "com.jay.fleet-tools-doctor", "--yes", "--json"], self.ctx("darwin")
        )
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
            ["mac", "start", "com.jay.fleet-tools-doctor", "--plan", "--json"],
            self.ctx("darwin", FakeRunner(loaded=False)),
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
        code, ans = self.run_main(
            ["mac", "start", "com.jay.fleet-tools-doctor", "--plan", "--json"], ctx
        )
        self.assertEqual((code, ans["detail"]), (2, "the agent is not installed"))
        self.plist("com.jay.fleet-tools-doctor", mode=0o666)
        code, _ = self.run_main(
            ["mac", "start", "com.jay.fleet-tools-doctor", "--plan", "--json"], ctx
        )
        self.assertEqual(code, 2)

    def test_stop_loaded_boots_out_and_unloaded_does_nothing(self):
        code, ans = self.run_main(
            ["mac", "stop", "com.jay.fleet-tools-doctor", "--plan", "--json"], self.ctx("darwin")
        )
        self.assertEqual(ans["commands"][0][:2], ["/bin/launchctl", "bootout"])
        code, ans = self.run_main(
            ["mac", "stop", "com.jay.fleet-tools-doctor", "--yes", "--json"],
            self.ctx("darwin", FakeRunner(loaded=False)),
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
        code, _ = self.run_main(
            ["mac", "start", "com.jay.fleet-tools-doctor", "--plan", "--json"], self.ctx("linux")
        )
        self.assertEqual(code, 2)


class RedeployTests(Base):
    ROWS = [
        {"uuid": UUID, "name": "fleet-recall", "type": "application", "status": "running"},
        {
            "uuid": "standalone-box",
            "name": "standalone-box",
            "type": "standalone",
            "status": "running",
        },
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
        self.assertEqual((code, ans["result"]), (0, "submitted"), ans)
        self.assertFalse(ans["verified"])
        ((url, headers),) = self.http_calls
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
            {
                "uuid": "echomail2",
                "name": "echomail2 (relay)",
                "type": "application",
                "status": "running",
            },
            {"uuid": "labelled123", "name": "x", "type": "application", "uuidSource": "name"},
        ]
        self.write_status(rows)
        for row in rows:
            code, ans = self.run_main(
                ["server", "redeploy", row["uuid"], "--yes", "--json"], self.ctx()
            )
            self.assertEqual((code, ans["result"]), (2, "refused"), row)
        self.assertEqual(self.http_calls, [])
        self.assertEqual(self.runner.calls, [])
        self.assertTrue(all(r["target"] == "coolify resource" for r in self.audit_rows()))

    def test_uuid_from_label(self):
        self.assertTrue(foc.uuid_from_label({"uuid": UUID, "name": "fleet-recall"}))
        self.assertTrue(foc.uuid_from_label({"uuid": UUID, "name": UUID + "-123456"}))
        self.assertTrue(
            foc.uuid_from_label({"uuid": "same", "name": "same", "uuidSource": "label"})
        )
        self.assertFalse(foc.uuid_from_label({"uuid": "same", "name": "same"}))
        self.assertFalse(foc.uuid_from_label({"uuid": "same", "name": "same (web)"}))
        self.assertFalse(foc.uuid_from_label({"uuid": UUID, "name": "x", "uuidSource": "resource"}))
        self.assertFalse(foc.uuid_from_label({"uuid": UUID}))

    def test_refuses_a_uuid_with_a_trailing_newline(self):
        self.write_status(self.ROWS)
        code, ans = self.run_main(
            ["server", "redeploy", UUID + "\n", "--plan", "--json"], self.ctx()
        )
        self.assertEqual((code, ans["detail"]), (2, "the uuid is not valid"))

    def test_refuses_without_a_report(self):
        code, ans = self.run_main(["server", "redeploy", UUID, "--plan", "--json"], self.ctx())
        self.assertEqual(ans["detail"], "there is no server status report")

    def test_missing_token_and_bad_answer_fail(self):
        self.write_status(self.ROWS)
        code, ans = self.run_main(
            ["server", "redeploy", UUID, "--yes", "--json"], self.ctx(runner=FakeRunner(secret=""))
        )
        self.assertEqual((code, ans["result"]), (1, "failed"))
        self.assertEqual(self.http_calls, [])

    def test_base_url_must_be_a_loopback_origin(self):
        # The token never leaves this host,  whatever the environment names.
        for base, ok in (
            ("http://localhost:8000", True),
            ("http://127.0.0.1:8000", True),
            ("https://[::1]:8443", True),
            ("https://coolify.example.test", False),
            ("http://coolify.example.test", False),
            ("https://127.0.0.1.example.test", False),
            ("http://localhost:8000/api", False),
            ("http://user@localhost:8000", False),
            ("http://@127.0.0.1:8000", False),
            ("http://127.0.0.1:8000#", False),
            ("http://127.0.0.1:8000?", False),
            ("http://[::1:8000", False),
            ("http://localhost:notaport", False),
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
    PAIR = {
        "INFISICAL_AUTOMATION_CLIENT_ID": "id-" + "a" * 30,
        "INFISICAL_AUTOMATION_CLIENT_SECRET": "sec-" + "b" * 40,
    }

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
        return self.run_main(
            ["server", "redeploy", UUID, "--yes", "--json"], self.ctx(environ=environ)
        )

    def secrets_env(self):
        ((argv, env),) = [c for c in self.runner.calls if c[0][1:2] == ["get"]]
        return env

    def test_file_is_the_one_identity_and_reaches_only_fleet_secrets(self):
        self.credentials(self.pair_text())
        code, ans = self.redeploy()
        self.assertEqual((code, ans["result"]), (0, "submitted"), ans)
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
        self.run_main(
            ["server", "stop", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
        )
        ((argv, env),) = self.runner.calls
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
            ('INFISICAL_TOKEN="quoted"\n', 0o600),
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
                (
                    "the Infisical credential file is not private",
                    "the Infisical credential file is malformed",
                ),
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
        ((url, headers),) = self.http_calls
        self.assertEqual(url, "http://127.0.0.1:8000/api/v1/version")
        self.assertEqual(headers["Authorization"], "Bearer " + TOKEN)
        self.assertNotIn(TOKEN, json.dumps(ans))
        self.assertEqual(self.audit_rows(), [])
        self.assertEqual(
            self.secrets_env()["INFISICAL_AUTOMATION_CLIENT_ID"],
            self.PAIR["INFISICAL_AUTOMATION_CLIENT_ID"],
        )

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
        self.assertEqual((code, ans["result"]), (0, "submitted"))

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
        code, ans = self.ssh(
            "server update fleet-cli --plan", argv=("server", "update", "fleet-cli", "--from-ssh")
        )
        self.assertEqual(code, 2)


class ResourceDispatchTests(Base):
    def enable(self):
        config = json.loads(self.config_path.read_text())
        config["server"]["resourcePolicy"] = True
        self.config_path.write_text(json.dumps(config))

    def test_resource_controls_require_explicit_allowlist(self):
        code, answer = self.run_main(
            ["server", "resource-status", "coding", "--plan", "--json"], self.ctx()
        )
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertEqual(self.runner.calls, [])

    def test_resource_allowlist_is_a_boolean(self):
        for value in (1, "true", [], None):
            config = json.loads(self.config_path.read_text())
            config["server"]["resourcePolicy"] = value
            with self.subTest(value=value), self.assertRaises(foc.Refused):
                foc.validate_config(config)

    def test_ssh_resource_dispatch_uses_the_bounded_handler(self):
        self.enable()
        target = "cpu-weight.50." + "a" * 64
        command = "server resource-policy " + target + " --plan --json"
        context = self.ctx(environ={"SSH_ORIGINAL_COMMAND": command})
        reply = {
            "ok": True,
            "verb": "server resource-policy",
            "target": target,
            "commands": [],
            "result": "planned",
            "detail": "bounded test",
        }
        with mock.patch("fleet_resource_control.handle", return_value=(reply, 0)) as handler:
            code, answer = self.run_main(["--from-ssh"], context)
        self.assertEqual((code, answer["result"]), (0, "planned"))
        handler.assert_called_once_with(context, "resource-policy", target, "plan")
        self.assertEqual(self.runner.calls, [])

    def test_resource_status_yes_is_refused_and_status_needs_no_writer(self):
        self.enable()
        context = self.ctx()
        context.resource_snapshot = dict
        with mock.patch(
            "fleet_resource_control.InfisicalStore",
            side_effect=__import__("fleet_resource_control").Refused("no writer"),
        ):
            code, answer = self.run_main(
                ["server", "resource-status", "coding", "--plan", "--json"], context
            )
            self.assertEqual(
                (code, answer["result"], answer["resourcePolicy"]["status"]),
                (0, "planned", "unavailable"),
            )
        code, answer = self.run_main(
            ["server", "resource-status", "coding", "--yes", "--json"], context
        )
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertEqual(self.runner.calls, [])

    def test_resource_ssh_rejects_shell_or_extra_arguments(self):
        target = "cpu-weight.50." + "a" * 64
        for suffix in (
            " --plan;id",
            " --plan --json extra",
            " --plan\n",
            " --plan --yes",
            " --plan --config=x",
        ):
            with self.subTest(suffix=suffix), self.assertRaises(foc.Refused):
                foc.parse_ssh_command("server resource-policy " + target + suffix)


class LogsAndPm2Tests(Base):
    def test_logs_plan_refuses_non_allowlisted_unit_and_carries_no_log_text(self):
        code, ans = self.run_main(
            ["server", "logs", "sshd.service", "--plan", "--json"], self.ctx()
        )
        self.assertEqual((code, ans["result"]), (2, "refused"))
        self.assertEqual(self.runner.calls, [])
        code, ans = self.run_main(
            ["server", "logs", "fleet-server-status.timer", "--plan", "--json"], self.ctx()
        )
        self.assertEqual((code, ans["result"]), (0, "planned"))
        self.assertNotIn("output", ans)
        self.assertNotIn("line one", json.dumps(ans))
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.audit_rows(), [])

    def test_logs_yes_argv_is_bounded_journalctl_and_audit_has_no_log_text(self):
        code, ans = self.run_main(
            ["server", "logs", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
        )
        self.assertEqual((code, ans["result"]), (0, "done"), ans)
        ((argv, _env),) = self.runner.calls
        self.assertTrue(argv[0].endswith("journalctl"), argv)
        self.assertEqual(
            argv[1:],
            ["--user", "-u", "fleet-server-status.timer", "-n", "200", "--no-pager"],
        )
        self.assertEqual(ans["output"], "line one\nline two\n")
        self.assertEqual(ans["lines"], 2)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verb"], "server logs")
        self.assertEqual(rows[0]["lines"], 2)
        audit_text = (self.state / "ops-audit.jsonl").read_text()
        self.assertNotIn("line one", audit_text)
        self.assertNotIn("line two", audit_text)

    def test_logs_system_scope_has_no_user_flag_and_truncates(self):
        many = b"".join(b"row %d\n" % i for i in range(250))
        code, ans = self.run_main(
            ["server", "logs", "fleet-cli-git-update.timer", "--yes", "--json"],
            self.ctx(runner=FakeRunner(log_bytes=many)),
        )
        self.assertEqual(code, 0, ans)
        ((argv, _env),) = self.runner.calls
        self.assertEqual(argv[1:], ["-u", "fleet-cli-git-update.timer", "-n", "200", "--no-pager"])
        self.assertIn("truncated", ans["output"])
        self.assertLessEqual(ans["lines"], 200)
        self.assertNotIn("row 0\n", ans["output"])

    def test_logs_refused_for_mac_scope_and_bad_unit(self):
        for argv in (
            ["mac", "logs", "com.jay.fleet-tools-doctor", "--plan", "--json"],
            ["server", "logs", "../x.service", "--yes", "--json"],
            ["server", "logs", "fleet-server-status.timer\n", "--yes", "--json"],
        ):
            platform = "darwin" if argv[0] == "mac" else "linux"
            code, ans = self.run_main(argv, self.ctx(platform))
            self.assertEqual((code, ans["result"]), (2, "refused"), argv)

    def test_pm2_plan_and_yes_argv_for_allowlisted_app(self):
        code, ans = self.run_main(
            ["pm2", "restart", "shellular", "--plan", "--json"], self.ctx("darwin")
        )
        self.assertEqual((code, ans["result"]), (0, "planned"), ans)
        self.assertEqual(
            ans["commands"], [["/fake/bin/node", "/fake/lib/pm2", "restart", "shellular"]]
        )
        self.assertEqual(self.runner.calls, [])
        code, ans = self.run_main(
            ["pm2", "stop", "shellular", "--yes", "--json"], self.ctx("darwin")
        )
        self.assertEqual((code, ans["result"]), (0, "done"), ans)
        ((argv, env),) = self.runner.calls
        self.assertEqual(argv, ["/fake/bin/node", "/fake/lib/pm2", "stop", "shellular"])
        self.assertIn("PM2_HOME", env)
        self.assertNotIn("SECRET_THING", env)
        self.assertEqual(self.audit_rows()[0]["verb"], "pm2 stop")

    def test_pm2_bad_token_unlisted_and_disallowed_action_refused(self):
        for argv in (
            ["pm2", "start", "not-listed", "--yes", "--json"],
            ["pm2", "stop", "only-start-app", "--yes", "--json"],
            ["pm2", "start", "shellular;id", "--yes", "--json"],
            ["pm2", "start", "-oProxyCommand=x", "--yes", "--json"],
            ["pm2", "start", "shellular\n", "--yes", "--json"],
            ["pm2", "delete", "shellular", "--yes", "--json"],
        ):
            code, ans = self.run_main(argv, self.ctx("darwin"))
            self.assertEqual((code, ans["result"]), (2, "refused"), argv)
            self.assertEqual(self.runner.calls, [], argv)

    def test_pm2_refused_on_linux_and_malformed_allowlist_entry(self):
        code, _ = self.run_main(
            ["pm2", "start", "shellular", "--plan", "--json"], self.ctx("linux")
        )
        self.assertEqual(code, 2)
        with self.assertRaises(foc.Refused):
            foc.validate_config(
                {
                    "schemaVersion": 1,
                    "mac": {"pm2Apps": [{"name": "Bad Name", "actions": ["start"]}]},
                }
            )

    def test_repo_allowlist_pm2_names_match_the_swift_registry(self):
        config = foc.load_config()
        swift = Path(HERE, "..", "muster", "Muster", "FleetServices", "FleetServices.swift")
        mcp = Path(HERE, "..", "muster", "Muster", "FleetServices", "McpServerControl.swift")
        text = swift.read_text() + mcp.read_text()
        for name in config["pm2"]:
            self.assertIn('"%s"' % name, text, name)

    def pm2_listing(self, status="stopped", pid=0, name="shellular"):
        return json.dumps(
            [{"name": name, "pid": pid, "pm2_env": {"status": status, "pid": pid}}]
        ).encode()

    def test_pm2_start_on_a_stale_record_plans_a_restart(self):
        runner = FakeRunner(jlist=self.pm2_listing(status="online", pid=0))
        code, ans = self.run_main(
            ["pm2", "start", "shellular", "--plan", "--json"], self.ctx("darwin", runner=runner)
        )
        self.assertEqual((code, ans["result"]), (0, "planned"), ans)
        self.assertEqual(
            ans["commands"], [["/fake/bin/node", "/fake/lib/pm2", "restart", "shellular"]]
        )
        self.assertIn("stale", ans["detail"])
        ((argv, env),) = self.runner.calls
        self.assertEqual(argv, ["/fake/bin/node", "/fake/lib/pm2", "jlist"])
        self.assertIn("PM2_HOME", env)

    def test_pm2_start_on_stopped_and_healthy_records_plans_a_plain_start(self):
        for listing in (
            self.pm2_listing(status="stopped", pid=0),
            self.pm2_listing(status="online", pid=4242),
            self.pm2_listing(status="errored", pid=0),
            b"not json at all",
        ):
            runner = FakeRunner(jlist=listing)
            code, ans = self.run_main(
                ["pm2", "start", "shellular", "--plan", "--json"],
                self.ctx("darwin", runner=runner),
            )
            self.assertEqual((code, ans["result"]), (0, "planned"), listing)
            self.assertEqual(
                ans["commands"],
                [["/fake/bin/node", "/fake/lib/pm2", "start", "shellular"]],
                listing,
            )

    def test_pm2_start_refuses_when_pm2_no_longer_remembers_the_app(self):
        runner = FakeRunner(jlist=b"[]")
        code, ans = self.run_main(
            ["pm2", "start", "shellular", "--yes", "--json"], self.ctx("darwin", runner=runner)
        )
        self.assertEqual((code, ans["result"]), (2, "refused"), ans)
        self.assertIn("no longer remembers", ans["detail"])
        ((argv, _env),) = self.runner.calls
        self.assertEqual(argv[-1], "jlist")

    def test_pm2_failure_names_the_verb_and_not_the_script_path(self):
        code, ans = self.run_main(
            ["pm2", "stop", "shellular", "--yes", "--json"],
            self.ctx("darwin", runner=FakeRunner(status=3)),
        )
        self.assertEqual((code, ans["result"]), (1, "failed"), ans)
        self.assertEqual(ans["detail"], "node stop exited 3")

    def test_logs_yes_asks_the_runner_to_cap_what_it_captures(self):
        code, ans = self.run_main(
            ["server", "logs", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
        )
        self.assertEqual(code, 0, ans)
        self.assertEqual(self.runner.max_bytes_seen, [foc.LOG_MAX_BYTES * 4])

    def test_capped_capture_keeps_only_the_tail(self):
        import sys

        status, out = foc.run_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write('x' * 300000 + 'TAILMARK')",
            ],
            timeout=30,
            want_stdout=True,
            max_bytes=1000,
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(out), 1000)
        self.assertTrue(out.endswith(b"TAILMARK"))

    def test_private_stderr_capture_is_bounded(self):
        import sys

        status, out = foc.run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('x' * 300000 + 'TAILMARK')"],
            timeout=30,
            want_stdout=True,
            max_bytes=1000,
            combine_stderr=True,
        )
        self.assertEqual(status, 0)
        self.assertEqual(len(out), 1000)
        self.assertTrue(out.endswith(b"TAILMARK"))

    def _assert_capped_capture_interrupt_cleans_up(self, stage):
        class Pipe:
            def __init__(self):
                self.closed = False

            def fileno(self):
                return 42

            def close(self):
                self.closed = True

        class Process:
            def __init__(self):
                self.stdout = Pipe()
                self.killed = False
                self.wait_calls = 0

            def poll(self):
                return None if not self.killed else -9

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                self.wait_calls += 1
                if stage == "wait" and not self.killed:
                    raise KeyboardInterrupt
                return -9

        class Selector:
            def __init__(self):
                self.closed = False

            def register(self, fd, events):
                return None

            def select(self, timeout):
                if stage == "selector":
                    raise KeyboardInterrupt
                return [object()]

            def close(self):
                self.closed = True

        process = Process()
        selector = Selector()
        with (
            mock.patch.object(foc.subprocess, "Popen", return_value=process),
            mock.patch.object(foc.selectors, "DefaultSelector", return_value=selector),
            mock.patch.object(
                foc.os,
                "read",
                side_effect=KeyboardInterrupt if stage == "read" else None,
                return_value=b"",
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            foc._run_capped(["journalctl"], 30, None, 100)

        self.assertTrue(process.killed)
        self.assertEqual(process.wait_calls, 2 if stage == "wait" else 1)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(selector.closed)

    def test_capped_capture_selector_interrupt_kills_and_reaps_child(self):
        self._assert_capped_capture_interrupt_cleans_up("selector")

    def test_capped_capture_read_interrupt_kills_and_reaps_child(self):
        self._assert_capped_capture_interrupt_cleans_up("read")

    def test_capped_capture_wait_interrupt_kills_and_reaps_child(self):
        self._assert_capped_capture_interrupt_cleans_up("wait")

    def test_text_output_strips_terminal_escapes_but_json_keeps_the_field(self):
        dirty = "a\x1b[31mred\x1b[0m\x07b\x1b]0;title\x07c\nd\te\x7f\x85f"
        answer = {
            "ok": True,
            "verb": "server logs",
            "target": "x.service",
            "commands": [],
            "result": "done",
            "detail": "1 journal lines",
            "output": dirty,
        }
        text_out = io.StringIO()
        foc.emit(answer, False, text_out)
        shown = text_out.getvalue()
        self.assertNotIn("\x1b", shown)
        self.assertNotIn("\x07", shown)
        self.assertNotIn("\x7f", shown)
        self.assertNotIn("\x85", shown)
        self.assertIn("aredbc\nd\tef", shown)
        json_out = io.StringIO()
        foc.emit(answer, True, json_out)
        self.assertEqual(json.loads(json_out.getvalue())["output"], dirty)


class SourceTests(unittest.TestCase):
    def test_no_shell_true_anywhere(self):
        text = Path(HERE, "fleet-ops-control.py").read_text()
        self.assertNotIn("shell=True", text)
        self.assertNotIn("os.system", text)
        self.assertNotIn("os.popen", text)

    def test_mac_run_scripts_import_on_python_3_9(self):
        # The Mac runs these under /usr/bin/python3 (3.9):  `datetime.UTC` is 3.11+, and an
        # automated fix once rewrote `timezone.utc` to it.
        paths = [
            Path(HERE, name)
            for name in (
                "fleet-ops-control.py",
                "fleet-mac-services.py",
                "fleet-worker-health.py",
                "fleet-tools-doctor.py",
                "fleet-server-status.py",
                "fleet-vm-status.py",
            )
        ] + sorted(Path(HERE, "fleet_knobs").glob("*.py"))
        for path in paths:
            with self.subTest(path=path.name):
                self.assertNotIn("datetime.UTC", path.read_text())


if __name__ == "__main__":
    unittest.main()
