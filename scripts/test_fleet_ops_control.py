"""Tests for fleet-ops-control.py:  the allowlist, the ssh gate, the plans and the audit.

Nothing here runs launchctl, systemctl, fleet-secrets, docker or Coolify: every runner is a fake,
except that the bounded reader of `server eavesdropper-status` is also tried against small
Python child processes of its own (and a stand-in `docker` script).

  python3 scripts/test_fleet_ops_control.py
"""

import contextlib
import datetime
import importlib.util
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
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
        for directory in (self.home / ".local", self.home / ".local/state"):
            directory.chmod(0o700)
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


# The Mac's runtime-sync LaunchAgent was renamed on Nov 2, 2026.  The allowlist names the live
# label; the plist template under scripts/launchd still carries the old one until the template
# is renamed too, so the coverage test below matches it through this map.
RENAMED_AGENTS = {"com.jay.agent-sync-runtime-sync": "com.jay.simple-tools-runtime-sync"}


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
            ).strip()
            self.assertIn(RENAMED_AGENTS.get(label, label), config["agents"], plist.name)
        self.assertIn("com.jay.fleet-worker-health", config["agents"])
        self.assertIn("com.jay.fleet-mac-services", config["agents"])
        self.assertEqual(config["units"]["fleet-server-status.timer"]["scope"], "user")
        self.assertEqual(config["updates"]["fleet-cli"]["unit"], "fleet-cli-git-update.service")
        self.assertTrue(config["redeploy"])

    def test_the_allowlist_names_the_renamed_runtime_sync_job_and_not_the_old_one(self):
        config = foc.load_config()
        for old, new in RENAMED_AGENTS.items():
            self.assertNotIn(old, config["agents"])
            self.assertEqual(config["agents"][new], ("start", "stop", "restart"))

    def test_the_repo_allowlist_plans_the_renamed_job_and_refuses_the_old_label(self):
        for label, expected in (
            ("com.jay.simple-tools-runtime-sync", (0, "planned")),
            ("com.jay.agent-sync-runtime-sync", (2, "refused")),
        ):
            with self.subTest(label=label):
                context = self.ctx("darwin")
                context.config_path = foc.CONFIG_PATH
                code, answer = self.run_main(["mac", "restart", label, "--plan", "--json"], context)
                self.assertEqual((code, answer["result"]), expected)
                if expected[1] == "planned":
                    self.assertEqual(
                        answer["commands"],
                        [[foc.LAUNCHCTL, "kickstart", "-k", f"gui/{os.getuid()}/{label}"]],
                    )
                self.assertEqual(self.audit_rows(), [])

    def test_the_allowlist_names_nothing_from_before_the_agent_sync_rename(self):
        config = foc.load_config()
        names = list(config["agents"]) + list(config["pm2"]) + list(config["units"])
        names += [entry["unit"] for entry in config["updates"].values()]
        self.assertEqual([name for name in names if "agent-sync" in name], [])
        # The file as written, so a unit's name, a role or a note cannot carry it either.
        raw = Path(HERE, "fleet-ops-control.json").read_text()
        self.assertNotIn("agent-sync", raw)

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
        self.assertEqual(audit["baselineStartMonotonic"], 100)

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
        with mock.patch.object(foc, "audit", return_value=True) as audit:
            self.update(uid=0)
        self.assertFalse(any(argv[0] == foc.SUDO for argv, _ in self.update_calls))
        self.assertEqual(audit.call_args.kwargs["route"], "root-system-update")

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
    def status(self, state=None, target=None, mode="--plan", config=None, capture=True):
        if config is not None:
            self.config_path.write_text(json.dumps(config))
        self.status_calls = []
        if capture:
            self.write_capture()

        def runner(argv, **kwargs):
            self.status_calls.append(argv)
            self.assertNotIn(foc.SUDO, argv)
            self.assertIn("show", argv)
            return 0, state or update_state(
                InvocationID="b" * 32,
                ExecMainStartTimestampMonotonic="200",
                ExecMainExitTimestampMonotonic="210",
            )

        return self.run_main(
            [
                "server",
                "update-status",
                target or ("fleet-cli." + "b" * 32 + ".100"),
                mode,
                "--json",
            ],
            self.ctx(runner=runner),
        )

    def write_capture(self, **changes):
        row = {
            "host": "server",
            "verb": "server update",
            "target": "fleet-cli",
            "route": "sudo-system-update",
            "result": "submitted",
            "invocationID": "b" * 32,
            "baselineStartMonotonic": 100,
            "verified": False,
        }
        row.update(changes)
        self.capture_text = json.dumps(row) + "\n"
        path = self.state / "ops-audit.jsonl"
        path.write_text(self.capture_text)
        path.chmod(0o600)

    def test_exact_captured_success_is_read_only(self):
        code, answer = self.status()
        self.assertEqual((code, answer["result"], answer["verified"]), (0, "done", True))
        self.assertEqual(answer["invocationID"], "b" * 32)
        self.assertEqual((self.state / "ops-audit.jsonl").read_text(), self.capture_text)
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
        self.assertEqual((self.state / "ops-audit.jsonl").read_text(), self.capture_text)

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
        for baseline, expected in (
            ("100", "submitted"),
            ("110", "refused"),
            ("99", "refused"),
            ("0", "refused"),
        ):
            code, answer = self.status(current, target="fleet-cli." + "b" * 32 + "." + baseline)
            self.assertEqual(answer["result"], expected)
            if expected == "refused":
                self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_mismatch"))
                self.assertEqual(self.status_calls, [])
            else:
                self.assertEqual((code, answer["baselineStartMonotonic"]), (0, 100))

    def test_missing_or_rotated_context_cannot_verify_current_success(self):
        code, answer = self.status(capture=False)
        self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_unavailable"))
        self.assertEqual(self.status_calls, [])
        self.write_capture(invocationID="a" * 32)
        code, answer = self.status(capture=False)
        self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_unavailable"))

    def test_capture_rows_require_exact_action_target_route_and_integer(self):
        for changes in (
            {"host": "mac"},
            {"verb": "server restart"},
            {"target": "other"},
            {"route": "other"},
            {"baselineStartMonotonic": True},
            {"baselineStartMonotonic": "100"},
            {"baselineStartMonotonic": -1},
        ):
            self.write_capture(**changes)
            code, answer = self.status(capture=False)
            self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_unavailable"))
            self.assertEqual(self.status_calls, [])

    def test_symlink_and_nonprivate_audit_are_refused(self):
        self.write_capture()
        path = self.state / "ops-audit.jsonl"
        path.chmod(0o644)
        code, answer = self.status(capture=False)
        self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_unavailable"))
        path.chmod(0o600)
        other = self.state / "capture.jsonl"
        path.rename(other)
        path.symlink_to(other)
        code, answer = self.status(capture=False)
        self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_unavailable"))
        self.assertEqual(self.status_calls, [])

    def test_symlink_audit_parent_is_refused(self):
        self.write_capture()
        renamed = self.state.with_name("saved-fleet-tools")
        self.state.rename(renamed)
        self.state.symlink_to(renamed, target_is_directory=True)
        code, answer = self.status(capture=False)
        self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_unavailable"))

    def test_foreign_owner_and_writable_directory_are_refused(self):
        self.write_capture()
        ctx = self.ctx()
        ctx.uid = os.getuid() + 123
        with self.assertRaises(foc.Refused):
            foc.captured_update_baseline(ctx, "b" * 32)
        self.state.chmod(0o777)
        code, answer = self.status(capture=False)
        self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_unavailable"))

    def test_old_context_outside_bounded_tail_is_unavailable(self):
        self.write_capture()
        with (self.state / "ops-audit.jsonl").open("a") as stream:
            stream.write("x" * (foc.AUDIT_CONTEXT_MAX_BYTES + 1) + "\n")
        code, answer = self.status(capture=False)
        self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_unavailable"))

    def test_recent_context_is_read_from_large_audit_tail(self):
        self.write_capture()
        (self.state / "ops-audit.jsonl").write_text(
            "x" * (foc.AUDIT_CONTEXT_MAX_BYTES + 1) + "\n" + self.capture_text,
        )
        code, answer = self.status(capture=False)
        self.assertEqual((code, answer["result"]), (0, "done"))

    def test_short_reads_still_recover_the_complete_capture(self):
        self.write_capture()
        original_read = os.read
        reads = []

        def short_read(fd, size):
            reads.append(size)
            return original_read(fd, min(size, 7))

        with mock.patch.object(foc.os, "read", side_effect=short_read):
            code, answer = self.status(capture=False)
        self.assertEqual((code, answer["result"]), (0, "done"))
        self.assertGreater(len(reads), 1)

    def test_close_error_preserves_outcome_and_attempts_every_descriptor(self):
        original_open, original_close = os.open, os.close
        for capture, expected in ((True, "done"), (False, "refused")):
            if capture:
                self.write_capture()
            else:
                (self.state / "ops-audit.jsonl").unlink()
            opened, closed = [], []

            def record_open(*args, **kwargs):
                fd = original_open(*args, **kwargs)
                opened.append(fd)
                return fd

            def flaky_close(fd):
                closed.append(fd)
                original_close(fd)
                if len(closed) == 1:
                    raise OSError("simulated close failure")

            with (
                mock.patch.object(foc.os, "open", side_effect=record_open),
                mock.patch.object(foc.os, "close", side_effect=flaky_close),
            ):
                code, answer = self.status(capture=False)
            self.assertEqual(answer["result"], expected)
            if not capture:
                self.assertEqual((code, answer["errorCode"]), (2, "receipt_context_unavailable"))
            self.assertEqual(closed, list(reversed(opened)))
            self.assertGreater(len(closed), 1)


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


class OneActionAndAuditLinkTests(Base):
    """One `--yes` at a time on a machine, the row id the caller can store, and how far a
    chain got before it stopped."""

    def hold_lock(self):
        fd = os.open(self.state / "ops-control.lock", os.O_RDWR | os.O_CREAT, 0o600)
        foc.fcntl.flock(fd, foc.fcntl.LOCK_EX | foc.fcntl.LOCK_NB)
        self.addCleanup(os.close, fd)

    def test_a_second_run_while_one_holds_the_lock_is_refused_and_runs_nothing(self):
        self.hold_lock()
        code, ans = self.run_main(
            ["server", "restart", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
        )
        self.assertEqual((code, ans["result"], ans["errorCode"]), (2, "refused", "action_busy"))
        self.assertEqual(ans["detail"], "another action is still running on this machine")
        self.assertEqual(ans["commands"], [])
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.audit_rows()[0]["result"], "refused")

    def test_a_journal_read_takes_no_lock_and_is_still_audited(self):
        self.hold_lock()
        code, ans = self.run_main(
            ["server", "logs", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
        )
        self.assertEqual((code, ans["result"]), (0, "done"))
        self.assertEqual(self.audit_rows()[0]["verb"], "server logs")

    def test_a_resource_policy_write_holds_the_same_lock(self):
        config = json.loads(self.config_path.read_text())
        config["server"]["resourcePolicy"] = True
        self.config_path.write_text(json.dumps(config))
        target = "cpu-weight.50." + "a" * 64
        reply = {"ok": True, "result": "done", "commands": []}
        with mock.patch("fleet_resource_control.handle", return_value=(reply, 0)) as handler:
            self.hold_lock()
            code, ans = self.run_main(
                ["server", "resource-policy", target, "--yes", "--json"], self.ctx()
            )
            self.assertEqual((code, ans["errorCode"]), (2, "action_busy"))
            handler.assert_not_called()

    def test_a_plan_takes_no_lock(self):
        self.hold_lock()
        code, ans = self.run_main(
            ["server", "restart", "fleet-server-status.timer", "--plan", "--json"], self.ctx()
        )
        self.assertEqual((code, ans["result"]), (0, "planned"))

    def test_the_lock_is_released_after_a_run(self):
        for _ in range(2):
            code, ans = self.run_main(
                ["server", "restart", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
            )
            self.assertEqual((code, ans["result"]), (0, "done"))

    def test_a_shared_lock_directory_is_refused(self):
        self.state.chmod(0o777)
        try:
            code, ans = self.run_main(
                ["server", "restart", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
            )
        finally:
            self.state.chmod(0o700)
        self.assertEqual((code, ans["errorCode"]), (2, "action_lock_unavailable"))
        self.assertEqual(self.runner.calls, [])

    def test_a_run_names_the_row_it_wrote(self):
        code, ans = self.run_main(
            ["server", "stop", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
        )
        self.assertEqual(code, 0)
        row = self.audit_rows()[0]
        self.assertRegex(ans["auditID"], r"^[0-9a-f]{12}$")
        self.assertEqual(row["id"], ans["auditID"])
        self.assertEqual(ans["auditTarget"], row["target"])
        self.assertEqual(ans["completedCommands"], 1)

    def test_a_refused_redeploy_names_the_target_the_row_holds(self):
        code, ans = self.run_main(["server", "redeploy", UUID, "--yes", "--json"], self.ctx())
        self.assertEqual(code, 2)
        self.assertEqual(ans["auditTarget"], "coolify resource")
        self.assertEqual(self.audit_rows()[0]["id"], ans["auditID"])

    def test_a_plan_writes_no_row_and_names_none(self):
        _, ans = self.run_main(
            ["server", "stop", "fleet-server-status.timer", "--plan", "--json"], self.ctx()
        )
        self.assertNotIn("auditID", ans)
        self.assertNotIn("completedCommands", ans)

    def test_a_row_that_could_not_be_written_is_not_named(self):
        with mock.patch.object(foc, "audit", return_value=False):
            _, ans = self.run_main(
                ["server", "stop", "fleet-server-status.timer", "--yes", "--json"], self.ctx()
            )
        self.assertNotIn("auditID", ans)
        self.assertNotIn("auditTarget", ans)

    def test_a_chain_that_stops_says_how_many_commands_finished(self):
        folder = self.home / "Library" / "LaunchAgents"
        folder.mkdir(parents=True, exist_ok=True)
        plist = folder / "com.jay.fleet-tools-doctor.plist"
        plist.write_text("<plist/>")
        os.chmod(plist, 0o600)

        class KickstartFails(FakeRunner):
            def __call__(self, argv, **kwargs):
                if argv[1:2] == ["kickstart"]:
                    self.calls.append((list(argv), dict(kwargs.get("env") or {})))
                    return 5, b""
                return super().__call__(argv, **kwargs)

        code, ans = self.run_main(
            ["mac", "start", "com.jay.fleet-tools-doctor", "--yes", "--json"],
            self.ctx("darwin", KickstartFails(loaded=False)),
        )
        self.assertEqual((code, ans["result"]), (1, "failed"))
        self.assertEqual(len(ans["commands"]), 2)
        self.assertEqual(ans["completedCommands"], 1)


GOLDEN = Path(HERE).parent / "muster" / "MusterTests" / "Fixtures" / "ops_wire_golden.json"
GOLDEN_ACTIONS = {
    "serverUnit": None,
    "logs": "logs",
    "serverUpdate": "update",
    "coolifyRedeploy": "redeploy",
    "resourceStatus": "resource-status",
    "eavesdropperStatus": "eavesdropper-status",
}


class WireContractTests(Base):
    """The lines Muster sends and the answers it reads, from the file its own tests read."""

    def setUp(self):
        super().setUp()
        self.golden = json.loads(GOLDEN.read_text())

    def from_ssh(self, line):
        ctx = self.ctx(environ={"PATH": "/usr/bin:/bin", "SSH_ORIGINAL_COMMAND": line})
        return self.run_main(["--from-ssh"], ctx)

    @staticmethod
    def normalized(answer):
        """The answer with what differs by machine or by run put in the golden file's form."""
        answer = json.loads(json.dumps(answer))
        for argv in answer.get("commands", []):
            if argv and os.path.basename(argv[0]) == "systemctl":
                argv[0] = "/usr/bin/systemctl"
        if "auditID" in answer:
            answer["auditID"] = "0123456789ab"
        return answer

    def test_every_line_muster_sends_passes_the_gate_as_the_verb_it_names(self):
        self.assertTrue(self.golden["accepted"])
        for entry in self.golden["accepted"]:
            line = entry["line"]
            words = foc.parse_ssh_command(line)
            self.assertEqual(words, line.split(), line)
            scope, action, target, mode, as_json = foc.parse_request(words)
            expected = GOLDEN_ACTIONS[entry["action"]] or entry["verb"]
            self.assertEqual(
                (scope, action, target, mode, as_json),
                ("server", expected, entry["target"], entry["mode"], True),
                line,
            )

    def test_every_line_the_gate_refuses_runs_nothing(self):
        for line in self.golden["refusedAtGate"]:
            with self.assertRaises(foc.Refused, msg=line):
                foc.parse_ssh_command(line)
            code, ans = self.from_ssh(line)
            self.assertEqual((code, ans["result"]), (2, "refused"), line)
            self.assertEqual(self.runner.calls, [], line)

    def test_every_line_the_helper_refuses_passes_the_gate_and_runs_nothing(self):
        for line in self.golden["refusedByHelper"]:
            foc.parse_ssh_command(line)
            code, ans = self.from_ssh(line)
            self.assertEqual((code, ans["result"]), (2, "refused"), line)
            self.assertEqual(self.runner.calls, [], line)

    def test_the_server_still_gives_the_answers_muster_reads(self):
        answers = self.golden["answers"]
        _, planned = self.from_ssh("server restart fleet-server-status.timer --plan --json")
        self.assertEqual(self.normalized(planned), answers["planned"])
        _, not_listed = self.from_ssh("server restart sshd.service --plan --json")
        self.assertEqual(not_listed, answers["notListed"])
        _, done = self.from_ssh("server stop fleet-server-status.timer --yes --json")
        self.assertEqual(self.normalized(done), answers["done"])
        fd = os.open(self.state / "ops-control.lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            foc.fcntl.flock(fd, foc.fcntl.LOCK_EX | foc.fcntl.LOCK_NB)
            _, busy = self.from_ssh("server stop fleet-server-status.timer --yes --json")
        finally:
            os.close(fd)
        self.assertEqual(self.normalized(busy), answers["busy"])


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

    def test_refuses_an_application_that_is_not_on_the_host_now(self):
        for status in ("absent", "unknown"):
            rows = [
                {
                    "uuid": UUID,
                    "name": "fleet-recall",
                    "type": "application",
                    "uuidSource": "label",
                    "status": status,
                }
            ]
            self.write_status(rows)
            code, ans = self.run_main(["server", "redeploy", UUID, "--yes", "--json"], self.ctx())
            self.assertEqual((code, ans["result"]), (2, "refused"), status)
        self.assertEqual(self.http_calls, [])

    def test_a_live_row_ahead_of_a_leftover_with_its_uuid_is_redeployed(self):
        rows = [
            {
                "uuid": UUID,
                "name": "fleet-recall",
                "type": "application",
                "uuidSource": "label",
                "status": "exited",
            },
            {
                "uuid": UUID,
                "name": "fleet-recall (old)",
                "type": "application",
                "uuidSource": "label",
                "status": "absent",
            },
        ]
        self.write_status(rows)
        code, ans = self.run_main(["server", "redeploy", UUID, "--plan", "--json"], self.ctx())
        self.assertEqual(code, 0, ans)

    def test_the_audit_names_the_application_and_not_one_service(self):
        rows = [
            {
                "uuid": UUID,
                "name": "fleet-recall (worker)",
                "service": "worker",
                "type": "application",
                "uuidSource": "label",
                "status": "running",
            }
        ]
        self.write_status(rows)
        code, ans = self.run_main(["server", "redeploy", UUID, "--yes", "--json"], self.ctx())
        self.assertEqual((code, ans["result"]), (0, "submitted"), ans)
        self.assertEqual(self.audit_rows()[0]["target"], "fleet-recall")

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
    """A caller-scoped service token reaches only the fleet-secrets child."""

    ROWS = RedeployTests.ROWS
    TOKEN = "FLEET_OPS_CONTROL_INFISICAL_TOKEN"
    VALUE = "st." + "scoped-fleet-ops-control-token"
    SHARED_AUTH = {
        name: "synthetic-unrelated-credential"
        for name in (
            "INFISICAL_TOKEN",
            "INFISICAL_AI_FLEET_COORDINATOR_TOKEN",
            "INFISICAL_WRITE_TOKEN",
            "INFISICAL_CLIENT_ID",
            "INFISICAL_CLIENT_SECRET",
            "INFISICAL_AUTOMATION_CLIENT_ID",
            "INFISICAL_AUTOMATION_CLIENT_SECRET",
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
            "INFISICAL_SHARED_CLIENT_ID",
            "INFISICAL_SHARED_CLIENT_SECRET",
            "WISE_RELAY_INFISICAL_TOKEN",
        )
    }
    COORDINATES = {"INFISICAL_PROJECT_ID": "preserved-project", "INFISICAL_READ_PATHS": "/coolify"}

    def credentials(self, text, mode=0o600):
        folder = self.home / ".config" / "fleet-tools"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = folder / "ops-control.env"
        path.write_text(text)
        os.chmod(path, mode)
        return path

    def pair_text(self):
        return "# fleet-ops-control\n" + f"{self.TOKEN}={self.VALUE}\n"

    def redeploy(self, environ=None):
        self.write_status(self.ROWS)
        environ = environ or {"PATH": "/usr/bin", "INFISICAL_TOKEN": "stale-token-from-env"}
        return self.run_main(
            ["server", "redeploy", UUID, "--yes", "--json"], self.ctx(environ=environ)
        )

    def secrets_env(self):
        ((argv, env),) = [c for c in self.runner.calls if c[0][1:2] == ["get"]]
        return env

    def test_file_scoped_token_reaches_only_fleet_secrets(self):
        self.credentials(self.pair_text())
        inherited = {
            "PATH": "/usr/bin",
            **self.SHARED_AUTH,
            **self.COORDINATES,
            self.TOKEN: "st.different-process-reader-token",
        }
        code, ans = self.redeploy(inherited)
        # A server redeploy is submitted rather than run inline:  the merged code answers
        # "submitted" for that action, so the expectation follows the code.
        self.assertEqual((code, ans["result"]), (0, "submitted"), ans)
        env = self.secrets_env()
        self.assertEqual(env["INFISICAL_TOKEN"], self.VALUE)
        self.assertNotIn(self.TOKEN, env)
        self.assertFalse((self.SHARED_AUTH.keys() - {"INFISICAL_TOKEN"}) & env.keys())
        self.assertEqual({key: env[key] for key in self.COORDINATES}, self.COORDINATES)
        text = json.dumps(ans) + (self.state / "ops-audit.jsonl").read_text()
        self.assertNotIn(self.VALUE, text)
        for argv, _env in self.runner.calls:
            self.assertNotIn(self.VALUE, " ".join(argv))
        # A systemctl child never sees it.
        self.runner.calls.clear()
        self.run_main(
            ["server", "stop", "fleet-server-status.timer", "--yes", "--json"],
            self.ctx(environ=inherited),
        )
        ((argv, env),) = self.runner.calls
        self.assertNotIn("INFISICAL_TOKEN", env)
        self.assertNotIn(self.TOKEN, env)
        self.assertFalse(self.SHARED_AUTH.keys() & env.keys())

    def test_process_scoped_token_is_mapped_for_the_child(self):
        inherited = {
            "PATH": "/usr/bin",
            **self.SHARED_AUTH,
            **self.COORDINATES,
            self.TOKEN: self.VALUE,
        }
        code, _ = self.redeploy(inherited)
        self.assertEqual(code, 0)
        env = self.secrets_env()
        self.assertEqual(env["INFISICAL_TOKEN"], self.VALUE)
        self.assertNotIn(self.TOKEN, env)
        self.assertFalse((self.SHARED_AUTH.keys() - {"INFISICAL_TOKEN"}) & env.keys())
        self.assertEqual({key: env[key] for key in self.COORDINATES}, self.COORDINATES)
        self.runner.calls.clear()
        self.run_main(
            ["server", "stop", "fleet-server-status.timer", "--yes", "--json"],
            self.ctx(environ=inherited),
        )
        ((argv, env),) = self.runner.calls
        self.assertNotIn(self.TOKEN, env)
        self.assertFalse(self.SHARED_AUTH.keys() & env.keys())

    def test_no_file_discards_broad_token_and_machine_credentials(self):
        code, _ = self.redeploy({"PATH": "/usr/bin", **self.SHARED_AUTH})
        self.assertEqual(code, 0)
        env = self.secrets_env()
        self.assertNotIn("INFISICAL_TOKEN", env)
        self.assertFalse(self.SHARED_AUTH.keys() & env.keys())

    def test_a_shared_or_malformed_file_fails_before_fleet_secrets_runs(self):
        cases = [
            (self.pair_text(), 0o640),
            (self.pair_text(), 0o604),
            ("INFISICAL_AUTOMATION_CLIENT_ID=only-half\n", 0o600),
            ('FLEET_OPS_CONTROL_INFISICAL_TOKEN="quoted"\n', 0o600),
            ("PATH=/tmp/evil\n", 0o600),
            ("FLEET_OPS_CONTROL_INFISICAL_TOKEN=a b\n", 0o600),
            (
                "FLEET_OPS_CONTROL_INFISICAL_TOKEN=st."
                + "x" * 20
                + "\nFLEET_OPS_CONTROL_INFISICAL_TOKEN=st."
                + "y" * 20
                + "\n",
                0o600,
            ),
            ("export FLEET_OPS_CONTROL_INFISICAL_TOKEN=st." + "x" * 25 + "\n", 0o600),
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
        self.assertEqual(self.secrets_env()["INFISICAL_TOKEN"], self.VALUE)

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


class SeatInventoryDispatchTests(Base):
    """`server seat-inventory PLATFORM --plan`:  a read of what a platform has installed."""

    # Made up and assembled here so no token-shaped literal sits in the file.
    VALUE = "-".join(["not", "a", "real", "value", "must", "not", "appear"])

    def write_cursor_mcp(self):
        path = self.home / ".cursor" / "mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "fleet-tools": {"command": "/bin/run", "env": {"T": self.VALUE}},
                        "hosted": {"url": "https://example.invalid/" + self.VALUE},
                    }
                }
            )
        )

    def test_the_plan_answers_with_names_and_counts_from_this_accounts_home(self):
        self.write_cursor_mcp()
        code, answer = self.run_main(
            ["server", "seat-inventory", "cursor", "--plan", "--json"], self.ctx()
        )
        self.assertEqual((code, answer["ok"], answer["result"]), (0, True, "planned"))
        self.assertEqual(answer["inventory"]["platform"], "cursor")
        mcp = [s for s in answer["inventory"]["sections"] if s["title"] == "MCP Servers"][0]
        self.assertEqual(
            [(i["name"], i["detail"]) for i in mcp["items"]],
            [("fleet-tools", "local command"), ("hosted", "remote")],
        )

    def test_no_value_from_a_config_file_reaches_the_answer(self):
        self.write_cursor_mcp()
        out = io.StringIO()
        foc.main(
            ["server", "seat-inventory", "cursor", "--plan", "--json"], ctx=self.ctx(), out=out
        )
        self.assertNotIn(self.VALUE, out.getvalue())

    def test_it_runs_no_command_takes_no_lock_and_writes_no_audit_row(self):
        code, _ = self.run_main(
            ["server", "seat-inventory", "claude", "--plan", "--json"], self.ctx()
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.runner.calls, [])
        self.assertEqual(self.audit_rows(), [])

    def test_it_needs_no_allowlist_entry(self):
        config = json.loads(self.config_path.read_text())
        config["server"] = {key: type(value)() for key, value in config["server"].items()}
        self.config_path.write_text(json.dumps(config))
        code, answer = self.run_main(
            ["server", "seat-inventory", "fx", "--plan", "--json"], self.ctx()
        )
        self.assertEqual((code, answer["result"]), (0, "planned"))

    def test_yes_is_refused_because_there_is_nothing_to_run(self):
        code, answer = self.run_main(
            ["server", "seat-inventory", "claude", "--yes", "--json"], self.ctx()
        )
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertEqual(self.runner.calls, [])
        # Like every refused `--yes`, the attempt is audited, and nothing else is.
        rows = self.audit_rows()
        self.assertEqual(
            [(r["verb"], r["result"]) for r in rows], [("server seat-inventory", "refused")]
        )

    def test_it_runs_on_the_server_only_like_every_server_verb(self):
        self.write_cursor_mcp()
        code, answer = self.run_main(
            ["server", "seat-inventory", "cursor", "--plan", "--json"], self.ctx(platform="darwin")
        )
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertNotIn("inventory", answer)

    def test_a_platform_that_is_not_known_is_refused(self):
        for target in ("nope", "CLAUDE", "..", "claude.x", "a" * 40):
            with self.subTest(target=target):
                code, answer = self.run_main(
                    ["server", "seat-inventory", target, "--plan", "--json"], self.ctx()
                )
                self.assertEqual((code, answer["result"]), (2, "refused"))

    def test_ssh_accepts_the_verb_and_rejects_shell_or_extra_arguments(self):
        words = foc.parse_ssh_command("server seat-inventory claude --plan --json")
        self.assertEqual(words[:3], ["server", "seat-inventory", "claude"])
        for suffix in (" --plan;id", " --plan --json extra", " --plan --yes", " --plan --home=/x"):
            with self.subTest(suffix=suffix), self.assertRaises(foc.Refused):
                foc.parse_ssh_command("server seat-inventory claude" + suffix)

    def test_over_ssh_the_answer_is_json_and_reads_the_account_home(self):
        self.write_cursor_mcp()
        context = self.ctx(
            environ={"SSH_ORIGINAL_COMMAND": "server seat-inventory cursor --plan --json"}
        )
        code, answer = self.run_main(["--from-ssh"], context)
        self.assertEqual((code, answer["result"]), (0, "planned"))
        self.assertEqual(answer["inventory"]["host"], "server")


class StatusReader:
    """Stands in for `run_bounded_read`: answers the one `docker exec` read of
    `server eavesdropper-status` the way the real reader would, and records what it was asked."""

    def __init__(self, status=0, out=b"", err=b""):
        self.status = status
        self.out = out
        self.err = err
        self.calls = []

    def __call__(self, argv, timeout=None, env=None, max_bytes=None):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        config = (env or {}).get("DOCKER_CONFIG", "")
        self.calls.append(
            {
                "argv": list(argv),
                "env": dict(env or {}),
                "timeout": timeout,
                "max_bytes": max_bytes,
                "config_dir": config,
                "config_is_empty_and_private": (
                    os.path.isdir(config)
                    and os.listdir(config) == []
                    and stat.S_IMODE(os.stat(config).st_mode) == 0o700
                ),
            }
        )
        over = max_bytes is not None and len(self.out) > max_bytes
        return self.status, (b"" if over else self.out), self.err, over


GOOD_MODULE = (
    "FROM = 'good'\n"
    "\n"
    "\n"
    "def reduce_status(status, now, instance):\n"
    "    return {'schema': 1, 'from': FROM}\n"
)


class EavesdropperStatusDispatchTests(Base):
    """`server eavesdropper-status server --plan`: a read-only reading of the eavesdropper."""

    # Made up and assembled here so no token-shaped literal sits in the file.
    VALUE = "".join(["aB3dE6g", "H9jK2mN", "5pQ8sT1", "vW4yZ7b", "C0eF"])
    PATH = "/" + "/".join(["home", "owner", "private", "place"])
    EMAIL = "owner" + "@" + "example.invalid"
    USER_ID = 918273645
    SESSION = "-".join(["0123abcd", "4567", "89ab", "cdef", "0123456789ab"])
    ARGV = [
        "docker",
        "exec",
        "--user",
        "agentsync",
        "eavesdropper",
        "/bin/cat",
        "/data/listener/status.json",
    ]
    VERB = ["server", "eavesdropper-status", "server", "--plan", "--json"]

    def setUp(self):
        super().setUp()
        # A copy of the real reducer beside a pretend helper, with modes the loader trusts,
        # whatever the modes of this working tree are (a lane on the server is group-writable).
        scripts = self.home / "scripts"
        scripts.mkdir()
        real = Path(HERE) / "reduced_status.py"
        (scripts / "reduced_status.py").write_bytes(real.read_bytes())
        (scripts / "reduced_status.py").chmod(0o644)
        scripts.chmod(0o755)
        self.enterContext(mock.patch.object(foc, "HELPER_DIR", scripts))

    def document(self, **changes):
        """What Daemon.status() emits, in the fields the reducer reads and some it must not."""
        seat = {
            "bot": "MA",
            "creds": "env",
            "connected": True,
            "cursor": self.USER_ID,
            "last_event": NOW - 30,
            "wake": "inbox",
            "routine": None,
            "pin": None,
            "claude": None,
            "provider": None,
            "quota": None,
            "wakes_24h": 3,
            "budget": {"wakes_per_day": 40},
            "jobs": 1,
            "running": None,
            "paused": False,
            "wakes_paused": False,
            "held_back": {"count": 2, "reasons": {"wakes_per_hour": 2}},
            "leases": [],
            "red": [],
        }
        document = {
            "pid": 7,
            "version": "1.2.3",
            "updated": NOW - 2,
            "instance": "server",
            "disabled": ["GROK"],
            "skipped_seats": {},
            "config_errors": [],
            "partition_file": self.PATH,
            "owner_user_id": self.USER_ID,
            "loop_guard": [],
            "pause": {},
            "seats": {"MA": seat},
        }
        document.update(changes)
        return document

    def reader(self, **kwargs):
        kwargs.setdefault("out", json.dumps(self.document()).encode())
        return StatusReader(**kwargs)

    def plan(self, reader=None, **ctx_args):
        context = self.ctx(**ctx_args)
        context.reader = reader or self.reader()
        return self.run_main(self.VERB, context)

    def test_the_plan_answers_with_the_reduced_status_after_one_fixed_read(self):
        reader = self.reader()
        code, answer = self.plan(reader)
        self.assertEqual(
            (code, answer["ok"], answer["result"], answer["verb"]),
            (0, True, "planned", "server eavesdropper-status"),
        )
        status = answer["status"]
        self.assertEqual((status["schema"], status["instance"]), (1, "server"))
        self.assertEqual(status["generated_at"], stamp(NOW))
        self.assertEqual(status["status_updated"], stamp(NOW - 2))
        self.assertEqual([seat["seat"] for seat in status["seats"]], ["MA"])
        self.assertEqual(status["seats"][0]["running"], "running")
        self.assertEqual(status["disabled"], ["GROK"])
        (call,) = reader.calls
        self.assertTrue(call["argv"][0].endswith("/docker"), call["argv"])
        self.assertEqual(call["argv"][1:], self.ARGV[1:])
        # The capture and the wait are bounded.
        self.assertEqual(call["max_bytes"], foc.EAVESDROPPER_STATUS_MAX_BYTES)
        self.assertEqual(call["timeout"], foc.EAVESDROPPER_READ_TIMEOUT)

    def test_the_docker_environment_is_pinned_with_an_empty_private_config(self):
        reader = self.reader()
        self.plan(reader)
        (call,) = reader.calls
        config = call["config_dir"]
        self.assertEqual(
            call["env"],
            {
                "PATH": "/usr/bin:/bin",
                "DOCKER_HOST": "unix:///var/run/docker.sock",
                "DOCKER_CONFIG": config,
                "HOME": config,
            },
        )
        self.assertTrue(call["config_is_empty_and_private"])
        self.assertFalse(os.path.exists(config), "the temporary config is removed afterwards")

    def test_nothing_from_the_callers_environment_reaches_docker(self):
        hostile = {
            "PATH": "/evil/bin",
            "DOCKER_HOST": "tcp://evil.invalid:2375",
            "DOCKER_CONFIG": "/evil/config",
            "DOCKER_CONTEXT": "evil",
            "DOCKER_TLS_VERIFY": "1",
            "DOCKER_CERT_PATH": "/evil/certs",
            "HOME": "/home/evil",
            "HTTP_PROXY": "http://evil.invalid",
            "SECRET_THING": "x" * 20,
        }
        reader = self.reader()
        code, answer = self.plan(reader, environ=hostile)
        self.assertEqual((code, answer["result"]), (0, "planned"))
        (call,) = reader.calls
        self.assertEqual(
            set(call["env"]), {"PATH", "DOCKER_HOST", "DOCKER_CONFIG", "HOME"}, call["env"]
        )
        self.assertEqual(call["env"]["PATH"], "/usr/bin:/bin")
        self.assertEqual(call["env"]["DOCKER_HOST"], "unix:///var/run/docker.sock")
        self.assertNotIn("evil", json.dumps(call["env"]))

    def test_the_answer_has_only_the_known_keys_and_no_sudo(self):
        reader = self.reader()
        _, answer = self.plan(reader)
        self.assertEqual(
            set(answer), {"ok", "verb", "target", "commands", "result", "detail", "status"}
        )
        self.assertEqual(answer["commands"], [])
        self.assertNotIn(foc.SUDO, [word for call in reader.calls for word in call["argv"]])

    def test_no_value_from_the_document_outside_the_reduced_fields_reaches_the_answer(self):
        seat = self.document()["seats"]["MA"]
        seat.update(
            bot=self.VALUE,
            creds=self.PATH,
            quota={"known": True, "exhausted": True, "reason": self.PATH + " " + self.EMAIL},
            leases=[
                {
                    "lease_id": self.SESSION,
                    "session_id": self.SESSION,
                    "platform": "claude-code",
                    "alive": True,
                    "why": self.PATH,
                    "letter": "A",
                    "title": "Letters Work: " + self.PATH + " for " + self.EMAIL + " " + self.VALUE,
                }
            ],
            red=[self.PATH, self.EMAIL, self.VALUE],
        )
        document = self.document(
            seats={"MA": seat},
            config_errors=[self.PATH, self.VALUE],
            skipped_seats={"X": [self.PATH + " " + self.VALUE]},
            pause={"*": {"by": self.EMAIL, "ts": NOW - 10, "wakes_only": False, "message_id": 5}},
        )
        out = io.StringIO()
        context = self.ctx()
        context.reader = StatusReader(out=json.dumps(document).encode())
        foc.main(self.VERB, ctx=context, out=out)
        text = out.getvalue()
        self.assertIn('"result": "planned"', text)
        for hidden in (self.VALUE, self.PATH, self.EMAIL, self.SESSION, str(self.USER_ID)):
            self.assertNotIn(hidden, text)
        self.assertNotIn("/data/listener", text)
        # Lease titles are never emitted, a plain one included, and there is no title key.
        self.assertNotIn("Letters Work", text)
        self.assertNotIn('"title"', text)
        (lease,) = json.loads(text)["status"]["seats"][0]["leases"]
        self.assertEqual(lease["letter"], "A")
        self.assertNotIn("title", lease)

    def test_the_operator_voice_block_is_read_and_a_bad_one_is_null(self):
        block = {
            "available": False,
            "code": "operator_credential_rejected",
            "since": "2026-11-07T20:00:00Z",
        }
        reader = self.reader(out=json.dumps(self.document(operator_voice=block)).encode())
        code, answer = self.plan(reader)
        self.assertEqual((code, answer["status"]["operator_voice"]), (0, block))
        hostile = {"available": self.PATH, "code": self.PATH, "since": self.EMAIL}
        reader = self.reader(out=json.dumps(self.document(operator_voice=hostile)).encode())
        code, answer = self.plan(reader)
        self.assertEqual(
            (code, answer["status"]["operator_voice"]),
            (0, {"available": None, "code": None, "since": None}),
        )
        self.assertNotIn(self.PATH, json.dumps(answer))
        # An older listener has no block at all, which reads as all null.
        code, answer = self.plan()
        self.assertEqual(
            answer["status"]["operator_voice"], {"available": None, "code": None, "since": None}
        )

    def test_it_takes_no_lock_and_writes_no_audit_row_for_a_plan(self):
        with mock.patch.object(foc, "action_lock", side_effect=AssertionError("lock taken")):
            code, answer = self.plan()
        self.assertEqual((code, answer["result"]), (0, "planned"))
        self.assertEqual(self.audit_rows(), [])
        self.assertFalse((self.state / "ops-control.lock").exists())

    def test_it_needs_no_allowlist_entry(self):
        config = json.loads(self.config_path.read_text())
        config["server"] = {key: type(value)() for key, value in config["server"].items()}
        self.config_path.write_text(json.dumps(config))
        code, answer = self.plan()
        self.assertEqual((code, answer["result"]), (0, "planned"))

    def test_a_missing_allowlist_refuses_it_like_every_other_verb(self):
        # The helper loads the allowlist before it dispatches any verb, so the file itself is
        # needed even though this verb reads no entry from it.
        self.config_path.unlink()
        reader = self.reader()
        code, answer = self.plan(reader)
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertNotIn("status", answer)
        self.assertEqual(reader.calls, [])

    def test_yes_is_refused_and_audited_and_runs_nothing(self):
        reader = self.reader()
        context = self.ctx()
        context.reader = reader
        code, answer = self.run_main(
            ["server", "eavesdropper-status", "server", "--yes", "--json"], context
        )
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertNotIn("status", answer)
        self.assertEqual(reader.calls, [])
        rows = self.audit_rows()
        self.assertEqual(
            [(r["verb"], r["result"]) for r in rows], [("server eavesdropper-status", "refused")]
        )

    def test_it_runs_on_the_server_only_like_every_server_verb(self):
        reader = self.reader()
        code, answer = self.plan(reader, platform="darwin")
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertNotIn("status", answer)
        self.assertEqual(reader.calls, [])

    def test_a_target_other_than_server_is_refused_and_runs_nothing(self):
        for target in ("nope", "mac", "SERVER", "..", "server.x", "a" * 40, "eavesdropper"):
            with self.subTest(target=target):
                reader = self.reader()
                context = self.ctx()
                context.reader = reader
                code, answer = self.run_main(
                    ["server", "eavesdropper-status", target, "--plan", "--json"], context
                )
                self.assertEqual((code, answer["result"]), (2, "refused"))
                self.assertEqual(reader.calls, [])

    def test_a_status_that_cannot_be_read_is_unavailable_with_a_reason_word_only(self):
        limit = foc.EAVESDROPPER_STATUS_MAX_BYTES
        daemon = b"Error response from daemon: "
        cases = (
            ("docker_unavailable", StatusReader(status=127)),
            ("timeout", StatusReader(status=124)),
            (
                "container_not_running",
                StatusReader(status=1, err=daemon + b"No such container: eavesdropper"),
            ),
            (
                "container_not_running",
                StatusReader(status=1, err=daemon + b"container 0123abcd is not running"),
            ),
            (
                "container_not_running",
                StatusReader(
                    status=1,
                    err=daemon + b"Container 0123abcd is restarting, wait until it is running",
                ),
            ),
            (
                "container_not_running",
                StatusReader(
                    status=1, err=b"ERROR RESPONSE FROM DAEMON: CONTAINER X IS NOT RUNNING"
                ),
            ),
            (
                "read_failed",
                StatusReader(status=1, err=b"cat: /data/listener/status.json: No such file"),
            ),
            (
                "read_failed",
                StatusReader(status=1, err=b"permission denied connecting to the docker socket"),
            ),
            ("read_failed", StatusReader(status=1, out=self.PATH.encode())),
            # A phrase after the bounded start of stderr is not looked for.
            ("read_failed", StatusReader(status=1, err=b"x" * 5000 + b" is not running")),
            ("too_large", StatusReader(out=b" " * (limit + 1))),
            ("not_json", StatusReader(out=b"not json " + self.PATH.encode())),
            ("not_json", StatusReader(out=b"[1, 2]")),
            ("not_json", StatusReader(out=b"")),
            ("not_json", StatusReader(out=b"\xff\xfe")),
            ("not_json", StatusReader(out=b"[" * 100000)),
        )
        for reason, reader in cases:
            with self.subTest(reason=reason, out=reader.out[:12], err=reader.err[:24]):
                code, answer = self.plan(reader)
                self.assertEqual(
                    (code, answer["ok"], answer["result"], answer["errorCode"]),
                    (1, False, "unavailable", reason),
                )
                self.assertEqual(answer["detail"], foc.EAVESDROPPER_UNAVAILABLE[reason])
                self.assertNotIn("status", answer)
                text = json.dumps(answer)
                for hidden in ("/data", self.PATH, "daemon", "0123abcd", "container: "):
                    self.assertNotIn(hidden, text)
                self.assertEqual(self.audit_rows(), [])

    def test_docker_failure_is_classified_from_known_phrases_only(self):
        for stderr, expected in (
            (b"Error response from daemon: No such container: x", "container_not_running"),
            (b"error response from daemon: container abc is not running", "container_not_running"),
            (
                b"Container abc is restarting, wait until the container is running",
                "container_not_running",
            ),
            (b"", "read_failed"),
            (None, "read_failed"),
            (b"\xff\xfe is not running", "container_not_running"),
            (b"something else entirely", "read_failed"),
            (b"x" * foc.DOCKER_STDERR_MAX + b"is not running", "read_failed"),
        ):
            with self.subTest(stderr=(stderr or b"")[:30]):
                self.assertEqual(foc._docker_failure(stderr), expected)

    def test_a_file_of_exactly_the_limit_is_read_and_one_byte_more_is_not(self):
        limit = foc.EAVESDROPPER_STATUS_MAX_BYTES
        base = json.dumps({"updated": NOW, "seats": {}, "pad": ""})
        exact = json.dumps({"updated": NOW, "seats": {}, "pad": "x" * (limit - len(base))})
        self.assertEqual(len(exact), limit)
        code, answer = self.plan(StatusReader(out=exact.encode()))
        self.assertEqual((code, answer["result"]), (0, "planned"))
        code, answer = self.plan(StatusReader(out=exact.encode() + b" "))
        self.assertEqual((code, answer["errorCode"]), (1, "too_large"))

    def with_module(self, text, mode=0o644, dir_mode=0o755):
        """The verb's plan with HELPER_DIR a folder holding `text` as the installed module."""
        folder = self.home / "installed"
        folder.mkdir(exist_ok=True)
        (folder / "reduced_status.py").write_text(text)
        (folder / "reduced_status.py").chmod(mode)
        folder.chmod(dir_mode)
        reader = self.reader()
        with (
            mock.patch.object(foc, "HELPER_DIR", folder),
            mock.patch.object(foc, "TRUSTED_MODULE_UID", os.getuid()),
        ):
            code, answer = self.plan(reader)
        return code, answer, reader

    def test_the_trusted_installed_module_is_the_one_that_reduces(self):
        code, answer, reader = self.with_module(GOOD_MODULE)
        self.assertEqual((code, answer["status"]), (0, {"schema": 1, "from": "good"}))
        self.assertEqual(len(reader.calls), 1)

    def test_a_missing_reduction_module_is_unavailable_and_runs_no_docker(self):
        empty = self.home / "empty"
        empty.mkdir()
        reader = self.reader()
        with mock.patch.object(foc, "HELPER_DIR", empty):
            code, answer = self.plan(reader)
        self.assertEqual(
            (code, answer["result"], answer["errorCode"]),
            (1, "unavailable", "module_unavailable"),
        )
        self.assertEqual(reader.calls, [])

    def test_an_untrusted_module_is_unavailable_and_runs_no_docker(self):
        for kind, kwargs in (
            ("file writable by others", {"mode": 0o666}),
            ("file writable by group", {"mode": 0o664}),
            ("folder writable by others", {"dir_mode": 0o757}),
            ("folder writable by group", {"dir_mode": 0o775}),
        ):
            with self.subTest(kind=kind):
                code, answer, reader = self.with_module(GOOD_MODULE, **kwargs)
                self.assertEqual(
                    (code, answer["result"], answer["errorCode"]),
                    (1, "unavailable", "module_unavailable"),
                )
                self.assertEqual(reader.calls, [])

    def test_a_module_that_cannot_load_or_raises_is_unavailable_not_an_exception(self):
        for text in (
            "def reduce_status(status, now, instance):\n    raise RuntimeError('boom')\n",
            "raise RuntimeError('does not load')\n",
            "raise SystemExit(3)\n",
            "x = 1\n",
            "def (:\n",
            "reduce_status = 5\n",
        ):
            with self.subTest(text=text):
                code, answer, _ = self.with_module(text)
                self.assertEqual(
                    (code, answer["result"], answer["errorCode"]),
                    (1, "unavailable", "module_unavailable"),
                )

    def test_ssh_accepts_the_verb_and_rejects_shell_or_extra_arguments(self):
        words = foc.parse_ssh_command("server eavesdropper-status server --plan --json")
        self.assertEqual(words[:3], ["server", "eavesdropper-status", "server"])
        for suffix in (" --plan;id", " --plan --json extra", " --plan --yes", " --path=/data"):
            with self.subTest(suffix=suffix), self.assertRaises(foc.Refused):
                foc.parse_ssh_command("server eavesdropper-status server" + suffix)
        for line in (
            "server eavesdropper-status server extra --plan --json",
            "server eavesdropper-status --plan --json",
            "mac eavesdropper-status server --plan --json",
        ):
            with self.subTest(line=line), self.assertRaises(foc.Refused):
                foc.parse_ssh_command(line)

    def test_over_ssh_the_answer_is_json_and_reads_the_servers_status(self):
        reader = self.reader()
        context = self.ctx(
            environ={"SSH_ORIGINAL_COMMAND": "server eavesdropper-status server --plan --json"},
        )
        context.reader = reader
        code, answer = self.run_main(["--from-ssh"], context)
        self.assertEqual((code, answer["result"]), (0, "planned"))
        self.assertEqual(answer["status"]["instance"], "server")
        self.assertEqual(len(reader.calls), 1)

    def test_over_ssh_a_yes_is_refused_before_anything_runs(self):
        reader = self.reader()
        context = self.ctx(
            environ={"SSH_ORIGINAL_COMMAND": "server eavesdropper-status server --yes --json"},
        )
        context.reader = reader
        code, answer = self.run_main(["--from-ssh"], context)
        self.assertEqual((code, answer["result"]), (2, "refused"))
        self.assertEqual(reader.calls, [])

    def test_the_default_reader_is_the_bounded_one(self):
        self.assertIs(foc.Context().reader, foc.run_bounded_read)

    def fake_docker(self, body):
        """An executable stand-in for docker, written as a Python script."""
        script = self.home / "docker"
        script.write_text("#!" + sys.executable + "\n" + body)
        script.chmod(0o755)
        return script

    def run_real(self, script, **patches):
        """The verb with the REAL reader, `script` as the docker binary, and the caller's
        environment full of things docker must not see."""
        environ = {"PATH": "/evil/bin", "DOCKER_HOST": "tcp://evil.invalid:1", "SECRET_THING": "s"}
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(foc, "DOCKER_CANDIDATES", (str(script),)))
            for name, value in patches.items():
                stack.enter_context(mock.patch.object(foc, name, value))
            return self.run_main(self.VERB, self.ctx(environ=environ))

    def test_with_the_real_reader_the_fake_docker_sees_the_pinned_argv_and_environment(self):
        record = self.home / "record.json"
        script = self.fake_docker(
            "import json, os, sys\n"
            "with open(%r, 'w') as handle:\n"
            "    json.dump({'argv': sys.argv[1:], 'env': dict(os.environ), 'cwd': os.getcwd(),\n"
            "               'config_entries': os.listdir(os.environ['DOCKER_CONFIG'])}, handle)\n"
            "sys.stdout.write(%r)\n" % (str(record), json.dumps(self.document()))
        )
        code, answer = self.run_real(script)
        self.assertEqual((code, answer["result"]), (0, "planned"), answer)
        seen = json.loads(record.read_text())
        self.assertEqual(seen["argv"], self.ARGV[1:])
        self.assertEqual(seen["cwd"], "/")
        self.assertEqual(seen["config_entries"], [])
        env = seen["env"]
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["DOCKER_HOST"], "unix:///var/run/docker.sock")
        self.assertEqual(env["HOME"], env["DOCKER_CONFIG"])
        self.assertNotIn("SECRET_THING", env)
        self.assertNotIn("evil", json.dumps(env))
        self.assertFalse(os.path.exists(env["DOCKER_CONFIG"]))

    def test_with_the_real_reader_a_huge_file_is_too_large_at_once_and_the_child_is_gone(self):
        pid_file = self.home / "pid"
        script = self.fake_docker(
            "import os, sys, time\n"
            "open(%r, 'w').write(str(os.getpid()))\n"
            "time.sleep(0.3)\n"
            "sys.stdout.buffer.write(b' ' * 3000000)\n"
            "sys.stdout.flush()\n"
            "time.sleep(60)\n" % str(pid_file)
        )
        started = time.monotonic()
        code, answer = self.run_real(script)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual((code, answer["errorCode"]), (1, "too_large"))
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_with_the_real_reader_a_hung_docker_times_out_and_the_child_is_gone(self):
        pid_file = self.home / "pid"
        script = self.fake_docker(
            "import os, time\nopen(%r, 'w').write(str(os.getpid()))\ntime.sleep(60)\n"
            % str(pid_file)
        )
        started = time.monotonic()
        code, answer = self.run_real(script, EAVESDROPPER_READ_TIMEOUT=1)
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual((code, answer["errorCode"]), (1, "timeout"))
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_with_the_real_reader_a_stopped_container_is_told_apart_from_a_failed_read(self):
        for stderr, reason in (
            ("Error response from daemon: container abc is not running", "container_not_running"),
            (
                "Error response from daemon: No such container: eavesdropper",
                "container_not_running",
            ),
            ("cat: /data/listener/status.json: No such file or directory", "read_failed"),
        ):
            with self.subTest(reason=reason, stderr=stderr[:30]):
                script = self.fake_docker(
                    "import sys\nsys.stderr.write(%r)\nsys.exit(1)\n" % stderr
                )
                code, answer = self.run_real(script)
                self.assertEqual((code, answer["errorCode"]), (1, reason))
                self.assertNotIn("abc", json.dumps(answer))


class ReducerModuleTests(Base):
    """The module the verb runs is read through descriptors and judged by fstat, never imported.

    It sits beside the helper, and the layout is read from who owns that folder.  A folder owned
    by TRUSTED_MODULE_UID (root, in production) is the installed layout.  These tests stand in for
    it by naming this account the trusted one (`installed`); a folder owned by the calling
    account while root is the trusted one is a checkout (`checkout`)."""

    def build(self, dir_mode=0o755, file_mode=0o644, text=GOOD_MODULE):
        folder = self.home / "helper"
        folder.mkdir()
        path = folder / "reduced_status.py"
        path.write_text(text, encoding="utf-8")
        path.chmod(file_mode)
        folder.chmod(dir_mode)
        return folder, path

    def load(self, folder, uid=None, trusted=0):
        context = self.ctx()
        if uid is not None:
            context.uid = uid
        with (
            mock.patch.object(foc, "HELPER_DIR", folder),
            mock.patch.object(foc, "TRUSTED_MODULE_UID", trusted),
        ):
            return foc._load_reduced_status(context)

    def installed(self, folder, **kwargs):
        return self.load(folder, trusted=os.getuid(), **kwargs)

    def checkout(self, folder, **kwargs):
        return self.load(folder, trusted=0, **kwargs)

    def not_root(self):
        if os.getuid() == 0:
            self.skipTest("running as root, so every file here is the trusted owner's")

    def test_a_trusted_module_is_compiled_in_memory(self):
        folder, _ = self.build()
        module = self.installed(folder)
        self.assertEqual(module.reduce_status(None, 0, "server"), {"schema": 1, "from": "good"})
        self.assertEqual(module.__name__, "reduced_status")
        self.assertNotIn("reduced_status", sys.modules)
        self.assertFalse((folder / "__pycache__").exists())
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["reduced_status.py"])

    def test_the_installed_layout_refuses_a_file_that_group_or_others_can_write(self):
        for mode in (0o666, 0o664, 0o646, 0o626, 0o622):
            with self.subTest(mode=oct(mode)):
                folder, _ = self.build(file_mode=mode)
                self.assertIsNone(self.installed(folder))
                shutil.rmtree(folder)

    def test_the_installed_layout_refuses_a_folder_that_group_or_others_can_write(self):
        for mode in (0o777, 0o775, 0o757, 0o722, 0o702):
            with self.subTest(mode=oct(mode)):
                folder, _ = self.build(dir_mode=mode)
                self.assertIsNone(self.installed(folder))
                shutil.rmtree(folder)

    def fake_file_owner(self, uid):
        """os.fstat answering `uid` as the owner of every regular file (folders unchanged)."""
        real = os.fstat

        def fstat(fd):
            info = real(fd)
            if not stat.S_ISREG(info.st_mode):
                return info
            fields = tuple(info)
            return os.stat_result(fields[:4] + (uid,) + fields[5:])

        return mock.patch.object(foc.os, "fstat", fstat)

    def test_the_installed_layout_needs_the_trusted_owner_not_the_calling_account(self):
        folder, _ = self.build()
        other = os.getuid() + 1000
        # The folder is the trusted owner's, so this is the installed layout, and the file is
        # the calling account's: refused, although the account may own a checkout's file.
        with self.fake_file_owner(other):
            self.assertIsNone(self.installed(folder, uid=other))
            self.assertIsNone(self.installed(folder))
        self.assertIsNotNone(self.installed(folder))

    def test_a_folder_owned_by_neither_the_trusted_owner_nor_the_calling_account_is_refused(self):
        self.not_root()
        folder, _ = self.build()
        self.assertIsNone(self.checkout(folder, uid=os.getuid() + 1000))
        self.assertIsNone(self.load(folder, uid=os.getuid() + 1000, trusted=os.getuid() + 1001))
        self.assertIsNotNone(self.checkout(folder))

    def test_a_symlinked_module_is_refused(self):
        folder = self.home / "helper"
        folder.mkdir()
        elsewhere = self.home / "elsewhere.py"
        elsewhere.write_text(GOOD_MODULE)
        elsewhere.chmod(0o644)
        (folder / "reduced_status.py").symlink_to(elsewhere)
        folder.chmod(0o755)
        self.assertIsNone(self.installed(folder))
        self.not_root()
        self.assertIsNone(self.checkout(folder))

    def test_a_symlinked_folder_is_refused(self):
        folder, _ = self.build()
        link = self.home / "link"
        link.symlink_to(folder, target_is_directory=True)
        self.assertIsNotNone(self.installed(folder))
        self.assertIsNone(self.installed(link))
        self.not_root()
        self.assertIsNone(self.checkout(link))

    def test_a_pipe_in_place_of_the_module_is_refused_without_waiting(self):
        folder = self.home / "helper"
        folder.mkdir()
        os.mkfifo(folder / "reduced_status.py", 0o644)
        folder.chmod(0o755)
        self.assertIsNone(self.installed(folder))

    def test_a_missing_module_and_an_oversized_module_are_refused(self):
        empty = self.home / "empty"
        empty.mkdir()
        empty.chmod(0o755)
        self.assertIsNone(self.installed(empty))
        folder, _ = self.build(text=GOOD_MODULE + "#" * (foc.REDUCED_STATUS_MAX_BYTES + 1) + "\n")
        self.assertIsNone(self.installed(folder))

    def swap(self, folder, path, before, text):
        """Run the loader while the module file is replaced by an evil one `before` the open
        of it or after it.  Returns (module, whether the evil code ran)."""
        marker = self.home / "evil-ran"
        evil = self.home / "evil.py"
        evil.write_text("open(%r, 'w').write('ran')\n%s" % (str(marker), text))
        evil.chmod(0o666 if before else 0o644)
        real_open = os.open

        def hook(name, flags, *args, **kwargs):
            module_open = name == "reduced_status.py" and kwargs.get("dir_fd") is not None
            if module_open and before:
                os.replace(evil, path)
            descriptor = real_open(name, flags, *args, **kwargs)
            if module_open and not before:
                os.replace(evil, path)
            return descriptor

        with mock.patch.object(foc.os, "open", hook):
            module = self.installed(folder)
        return module, marker.exists()

    def test_a_file_swapped_in_before_the_open_is_judged_by_what_was_opened(self):
        folder, path = self.build()
        module, ran = self.swap(folder, path, before=True, text=GOOD_MODULE)
        self.assertIsNone(module)
        self.assertFalse(ran)

    def test_a_file_swapped_in_after_the_open_is_never_read(self):
        folder, path = self.build()
        module, ran = self.swap(
            folder, path, before=False, text=GOOD_MODULE.replace("good", "evil")
        )
        self.assertEqual(module.reduce_status(None, 0, "x")["from"], "good")
        self.assertFalse(ran)
        self.assertIn("evil", path.read_text(), "the swap did happen")

    def test_a_checkout_accepts_the_calling_accounts_files_and_still_no_group_or_other_write(self):
        self.not_root()
        folder, path = self.build()
        self.assertIsNotNone(self.checkout(folder))
        for file_mode, dir_mode in ((0o664, 0o755), (0o646, 0o755), (0o644, 0o775), (0o644, 0o757)):
            with self.subTest(file=oct(file_mode), folder=oct(dir_mode)):
                path.chmod(file_mode)
                folder.chmod(dir_mode)
                self.assertIsNone(self.checkout(folder))
        path.chmod(0o644)
        folder.chmod(0o755)
        self.assertIsNotNone(self.checkout(folder))

    def test_the_real_module_loads_through_the_loader(self):
        real = Path(HERE) / "reduced_status.py"
        folder, path = self.build(text=real.read_text(encoding="utf-8"))
        module = self.installed(folder)
        out = module.reduce_status(
            {"updated": NOW, "instance": "server", "seats": {}}, NOW, "server"
        )
        self.assertEqual((out["schema"], out["instance"], out["seats"]), (1, "server", []))
        self.assertFalse((folder / "__pycache__").exists())

    def test_the_reducer_sits_beside_the_helper_and_there_is_one_place(self):
        self.assertTrue((foc.HELPER_DIR / foc.REDUCED_STATUS_NAME).is_file())
        self.assertFalse((foc.HELPER_DIR / "agent_sync" / foc.REDUCED_STATUS_NAME).exists())
        self.assertFalse(hasattr(foc, "REDUCED_STATUS_LAYOUTS"))
        self.assertEqual(foc.TRUSTED_MODULE_UID, 0)


class BoundedReadTests(unittest.TestCase):
    """`run_bounded_read` against real child processes (small Python scripts of their own)."""

    def child(self, body):
        return [sys.executable, "-c", body]

    ENV = {"PATH": "/usr/bin:/bin"}

    def run_child(self, body, timeout=20, max_bytes=1024, **kwargs):
        return foc.run_bounded_read(self.child(body), timeout, self.ENV, max_bytes, **kwargs)

    def test_it_returns_status_stdout_and_stderr(self):
        body = "import sys; sys.stdout.write('ok'); sys.stderr.write('warn'); sys.exit(3)"
        self.assertEqual(self.run_child(body), (3, b"ok", b"warn", False))

    def test_stdout_of_exactly_the_limit_is_kept_and_one_byte_more_is_over(self):
        body = "import sys; sys.stdout.buffer.write(b'x' * %d)"
        status, out, err, over = self.run_child(body % 1024, max_bytes=1024)
        self.assertEqual((status, len(out), over), (0, 1024, False))
        status, out, err, over = self.run_child(body % 1025, max_bytes=1024)
        self.assertEqual((out, over), (b"", True))

    def test_going_over_the_limit_kills_the_child_at_once(self):
        body = (
            "import os, sys, time\n"
            "sys.stderr.write(str(os.getpid())); sys.stderr.flush()\n"
            "time.sleep(0.3)\n"
            "sys.stdout.buffer.write(b'x' * 3000000); sys.stdout.flush()\n"
            "time.sleep(60)\n"
        )
        started = time.monotonic()
        status, out, err, over = self.run_child(body, timeout=30)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual((out, over), (b"", True))
        with self.assertRaises(ProcessLookupError):
            os.kill(int(err), 0)

    def test_a_timeout_is_124_and_kills_the_child(self):
        body = (
            "import os, sys, time\n"
            "sys.stderr.write(str(os.getpid())); sys.stderr.flush()\n"
            "time.sleep(60)\n"
        )
        started = time.monotonic()
        status, out, err, over = self.run_child(body, timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual((status, out, over), (124, b"", False))
        with self.assertRaises(ProcessLookupError):
            os.kill(int(err), 0)

    def test_a_child_that_closes_its_pipes_but_keeps_running_still_times_out(self):
        body = "import os, time; os.close(1); os.close(2); time.sleep(60)"
        started = time.monotonic()
        status, out, err, over = self.run_child(body, timeout=1)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual((status, over), (124, False))

    def test_stderr_is_read_alongside_and_only_its_start_is_kept(self):
        body = "import sys; sys.stderr.write('e' * 1000000); sys.exit(1)"
        started = time.monotonic()
        status, out, err, over = self.run_child(body)
        self.assertLess(time.monotonic() - started, 15)
        self.assertEqual((status, out, over), (1, b"", False))
        self.assertEqual(err, b"e" * foc.DOCKER_STDERR_MAX)

    def test_a_program_that_cannot_start_is_127(self):
        argv = [str(Path(tempfile.gettempdir()) / "no-such-program-for-this-test")]
        self.assertEqual(foc.run_bounded_read(argv, 5, self.ENV, 10), (127, b"", b"", False))

    def test_the_environment_the_directory_and_the_arguments_are_exact(self):
        body = (
            "import json, os, sys\n"
            "print(json.dumps({'a': os.environ.get('A_ONE'), 'secret': os.environ.get('SECRET_X'),\n"
            "                  'cwd': os.getcwd(), 'argv': sys.argv[1:]}))\n"
        )
        argv = self.child(body) + ["a; echo injected", "$(id)"]
        with mock.patch.dict(os.environ, {"SECRET_X": "from-the-parent"}):
            status, out, err, over = foc.run_bounded_read(argv, 20, {"A_ONE": "1"}, 4096)
        seen = json.loads(out)
        self.assertEqual(
            seen,
            {"a": "1", "secret": None, "cwd": "/", "argv": ["a; echo injected", "$(id)"]},
        )

    def test_a_non_argv_is_refused(self):
        for argv in ("echo hi", [], [1], None):
            with self.assertRaises(foc.Refused):
                foc.run_bounded_read(argv, 1, {}, 1)

    def stubborn(self):
        """A child that never answers and never dies: wait() with no timeout would hang."""
        read_out, write_out = os.pipe()
        read_err, write_err = os.pipe()

        class Stubborn:
            returncode = None
            stdout = os.fdopen(read_out, "rb", buffering=0)
            stderr = os.fdopen(read_err, "rb", buffering=0)
            killed = False
            waits = []

            def kill(self):
                self.killed = True

            def poll(self):
                return None

            def wait(self, timeout=None):
                self.waits.append(timeout)
                if timeout is None:
                    raise AssertionError("wait() without a timeout")
                raise foc.subprocess.TimeoutExpired("stubborn", timeout)

        proc = Stubborn()
        self.addCleanup(os.close, write_out)
        self.addCleanup(os.close, write_err)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.stderr.close)
        return proc

    def test_the_wait_after_a_kill_is_bounded_in_the_bounded_reader(self):
        proc = self.stubborn()
        with mock.patch.object(foc.subprocess, "Popen", lambda *a, **k: proc):
            result = foc.run_bounded_read(["x"], 0.2, {}, 10)
        self.assertEqual(result, (124, b"", b"", False))
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waits and all(t is not None for t in proc.waits), proc.waits)

    def test_the_wait_after_a_kill_is_bounded_in_the_capped_runner_too(self):
        proc = self.stubborn()
        with mock.patch.object(foc.subprocess, "Popen", lambda *a, **k: proc):
            result = foc._run_capped(["x"], 0.2, {}, 10)
        self.assertEqual(result, (124, b""))
        self.assertTrue(proc.killed)
        self.assertTrue(proc.waits and all(t is not None for t in proc.waits), proc.waits)

    def test_an_interrupt_kills_the_child_with_a_bounded_wait_and_propagates(self):
        class Interrupting:
            def register(self, *args, **kwargs):
                pass

            def get_map(self):
                return {"open": True}

            def select(self, timeout=None):
                raise KeyboardInterrupt

            def close(self):
                pass

        for call in (
            lambda: foc.run_bounded_read(["x"], 5, {}, 10),
            lambda: foc._run_capped(["x"], 5, {}, 10),
        ):
            proc = self.stubborn()
            with (
                mock.patch.object(foc.subprocess, "Popen", lambda *a, **k: proc),
                mock.patch.object(foc.selectors, "DefaultSelector", Interrupting),
                self.assertRaises(KeyboardInterrupt),
            ):
                call()
            self.assertTrue(proc.killed)
            self.assertTrue(proc.waits and all(t is not None for t in proc.waits), proc.waits)


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
