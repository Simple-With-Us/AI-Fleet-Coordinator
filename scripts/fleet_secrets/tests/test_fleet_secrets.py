"""fleet-secrets tests.  The Infisical CLI and the Keychain are faked at subprocess.run, so no
test touches the network, the real CLI, the real Keychain, the real HOME or any secrets file."""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_secrets import cli, core

FAKE_CLI = "/fake/bin/infisical"
PROJECT = "00000000-0000-4000-8000-000000000001"  # a placeholder, not the real project id
VAL_A = "SENTINEL_VALUE_A_9f3c"
VAL_B = "SENTINEL_VALUE_B_77d1"
CLIENT_ID = "SENTINEL_CLIENT_ID_c0de"
CLIENT_SECRET = "SENTINEL_CLIENT_SECRET_5ec7"
ACCESS_TOKEN = "SENTINEL_ACCESS_TOKEN_a11"
ALL_SENTINELS = (VAL_A, VAL_B, CLIENT_ID, CLIENT_SECRET, ACCESS_TOKEN)
FAKE_LOGIN_KEYCHAIN = "/fake/home/Library/Keychains/login.keychain-db"
KEYCHAIN_FULL = {core.KEYCHAIN_CLIENT_ID: CLIENT_ID, core.KEYCHAIN_CLIENT_SECRET: CLIENT_SECRET}


class World:
    """Stands in for subprocess.run:  the infisical CLI plus /usr/bin/security."""

    def __init__(
        self,
        secrets=None,
        keychain=None,
        export_rc=0,
        export_stdout=None,
        login_rc=0,
        system_keychain=None,
    ):
        self.secrets = {"ALPHA": VAL_A, "BETA": VAL_B} if secrets is None else secrets
        self.keychain = keychain or {}
        self.system_keychain = system_keychain or {}
        self.export_rc = export_rc
        self.export_stdout = export_stdout
        self.login_rc = login_rc
        self.calls = []

    def run(self, argv, **kw):
        argv = list(argv)
        env = dict(kw.get("env") or {})
        self.calls.append((argv, env))
        if argv[0] == core.SECURITY_BIN:
            acct = argv[argv.index("-a") + 1]
            if argv[-1] == core.SYSTEM_KEYCHAIN:
                store = self.system_keychain
            elif argv[-1] == FAKE_LOGIN_KEYCHAIN:
                store = self.keychain
            else:
                raise AssertionError("keychain lookup without an explicit keychain path: %r" % argv)
            if acct in store:
                return subprocess.CompletedProcess(argv, 0, (store[acct] + "\n").encode(), b"")
            return subprocess.CompletedProcess(argv, 44, b"", b"not found")
        if argv[0] != FAKE_CLI:
            raise AssertionError("unexpected program %r" % argv[0])
        if argv[1] == "--version":
            return subprocess.CompletedProcess(argv, 0, b"infisical version 0.43.141\n", b"")
        if argv[1] == "login" and len(argv) > 2 and argv[2] == "status":
            return subprocess.CompletedProcess(
                argv, 0, b"Authenticated as someone@example.com\n", b""
            )
        if argv[1] == "login":
            if self.login_rc:
                return subprocess.CompletedProcess(
                    argv, self.login_rc, b"", ("bad " + CLIENT_SECRET).encode()
                )
            return subprocess.CompletedProcess(argv, 0, (ACCESS_TOKEN + "\n").encode(), b"")
        if argv[1] == "export":
            if self.export_rc:
                leak = ("denied " + VAL_A).encode()
                return subprocess.CompletedProcess(argv, self.export_rc, leak, leak)
            body = (
                self.export_stdout
                if self.export_stdout is not None
                else json.dumps(self.secrets).encode()
            )
            return subprocess.CompletedProcess(argv, 0, body, b"")
        raise AssertionError("unexpected argv %r" % (argv[:2],))

    def exports(self):
        return [(a, e) for a, e in self.calls if len(a) > 1 and a[1] == "export"]

    def logins(self):
        return [
            (a, e)
            for a, e in self.calls
            if a[0] == FAKE_CLI and a[1] == "login" and a[2:3] != ["status"]
        ]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", core.ENV_PROJECT: PROJECT}
        self.exec_calls = []
        patches = [
            mock.patch.object(core, "find_cli", lambda env: FAKE_CLI),
            mock.patch.object(core, "SECURITY_BIN", sys.executable),  # exists on every platform
            mock.patch.object(core, "login_keychain", lambda: FAKE_LOGIN_KEYCHAIN),
            mock.patch.object(
                cli.os, "execvpe", lambda f, a, e: self.exec_calls.append((f, list(a), dict(e)))
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def use(self, world):
        p = mock.patch.object(core.subprocess, "run", world.run)
        p.start()
        self.addCleanup(p.stop)
        return world

    def call(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), env=self.env if env is None else env, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def assertNoSentinel(self, *texts):
        for t in texts:
            for s in ALL_SENTINELS:
                self.assertNotIn(s, t)


class GetTests(Base):
    def test_get_prints_exact_value_without_newline(self):
        self.use(World())
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out, err), (0, VAL_A, ""))

    def test_missing_key_exits_3_and_names_only_the_key(self):
        self.use(World())
        code, out, err = self.call("get", "NOPE")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("NOPE", err)
        self.assertNoSentinel(out, err)

    def test_path_is_passed_and_prod_is_the_only_env(self):
        w = self.use(World())
        self.call("get", "ALPHA", "--path", "/global-api-keys")
        argv, _ = w.exports()[0]
        self.assertIn("--path=/global-api-keys", argv)
        self.assertIn("--env=prod", argv)
        self.assertIn("--projectId=" + PROJECT, argv)
        self.assertNotIn("--token", " ".join(argv))

    def test_path_equals_form(self):
        w = self.use(World())
        self.call("names", "--path=/x")
        self.assertIn("--path=/x", w.exports()[0][0])

    def test_bad_name_and_bad_path_are_usage_errors_before_any_fetch(self):
        w = self.use(World())
        self.assertEqual(self.call("get", "A-B")[0], 2)
        self.assertEqual(self.call("get", "ALPHA", "--path", "../x")[0], 2)
        self.assertEqual(w.calls, [])


class UnresolvedReferenceTests(Base):
    def test_unresolved_reference_fails_closed_naming_the_key(self):
        self.use(World(secrets={"ALPHA": VAL_A, "BROKEN": "${folder.OTHER}"}))
        code, out, err = self.call("run", "--", "true")
        self.assertEqual(code, 5)
        self.assertIn("BROKEN", err)
        self.assertNotIn("folder.OTHER", err)
        self.assertEqual(self.exec_calls, [])

    def test_get_checks_only_the_requested_key(self):
        self.use(World(secrets={"ALPHA": VAL_A, "BROKEN": "${x}"}))
        self.assertEqual(self.call("get", "ALPHA")[0], 0)
        code, out, err = self.call("get", "BROKEN")
        self.assertEqual((code, out), (5, ""))
        self.assertIn("BROKEN", err)

    def test_names_fails_closed_too(self):
        self.use(World(secrets={"ALPHA": VAL_A, "BROKEN": "${x}"}))
        code, out, err = self.call("names")
        self.assertEqual((code, out), (5, ""))

    def test_a_value_that_merely_contains_a_reference_is_fine(self):
        self.use(World(secrets={"ALPHA": "prefix-${x}-suffix"}))
        self.assertEqual(self.call("get", "ALPHA")[:2], (0, "prefix-${x}-suffix"))


class RunTests(Base):
    def test_run_injects_env_and_execs(self):
        self.use(World())
        self.env["KEEP"] = "kept"
        code, _, err = self.call("run", "--", "mycmd", "--flag", "x")
        self.assertEqual(err, "")
        ((prog, argv, env),) = self.exec_calls
        self.assertEqual((prog, argv), ("mycmd", ["mycmd", "--flag", "x"]))
        self.assertEqual((env["ALPHA"], env["BETA"], env["KEEP"]), (VAL_A, VAL_B, "kept"))

    def test_run_replaces_an_existing_variable(self):
        self.use(World())
        self.env["ALPHA"] = "old"
        self.call("run", "--", "mycmd")
        self.assertEqual(self.exec_calls[0][2]["ALPHA"], VAL_A)

    def test_only_filters_to_the_named_keys(self):
        self.use(World())
        self.call("run", "--only", "ALPHA", "--", "mycmd")
        env = self.exec_calls[0][2]
        self.assertEqual(env["ALPHA"], VAL_A)
        self.assertNotIn("BETA", env)

    def test_only_with_an_absent_key_exits_3_and_starts_nothing(self):
        self.use(World())
        code, _, err = self.call("run", "--only", "ALPHA,NOPE", "--", "mycmd")
        self.assertEqual(code, 3)
        self.assertIn("NOPE", err)
        self.assertEqual(self.exec_calls, [])

    def test_only_ignores_an_unresolved_reference_in_an_unrequested_key(self):
        self.use(World(secrets={"ALPHA": VAL_A, "BROKEN": "${x}"}))
        self.call("run", "--only", "ALPHA", "--", "mycmd")
        self.assertEqual(self.exec_calls[0][2]["ALPHA"], VAL_A)

    def test_loader_and_path_names_are_never_injected(self):
        self.use(
            World(
                secrets={
                    "ALPHA": VAL_A,
                    "PATH": "/evil",
                    "LD_PRELOAD": "x",
                    "DYLD_INSERT_LIBRARIES": "y",
                    "HOME": "/evil",
                }
            )
        )
        self.call("run", "--", "mycmd")
        env = self.exec_calls[0][2]
        self.assertEqual(env["PATH"], self.env["PATH"])
        self.assertEqual(env["HOME"], self.env["HOME"])
        self.assertNotIn("LD_PRELOAD", env)
        self.assertNotIn("DYLD_INSERT_LIBRARIES", env)

    def test_only_refuses_reserved_names_with_a_usage_error(self):
        w = self.use(
            World(
                secrets={
                    "ALPHA": VAL_A,
                    "LD_PRELOAD": "x",
                    "DYLD_INSERT_LIBRARIES": "y",
                    "PATH": "/evil",
                    "NODE_OPTIONS": "z",
                    "BASH_ENV": "q",
                }
            )
        )
        for name in ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PATH", "NODE_OPTIONS", "BASH_ENV"):
            code, _, err = self.call("run", "--only", "ALPHA," + name, "--", "mycmd")
            self.assertEqual(code, 2, name)
            self.assertIn(name, err)
        self.assertEqual(self.exec_calls, [])
        self.assertEqual(w.calls, [])

    def test_infisical_auth_keys_held_in_infisical_are_never_injected(self):
        secrets = {
            "ALPHA": VAL_A,
            "INFISICAL_TOKEN": "STORED_TOKEN_SENTINEL",
            "INFISICAL_CLIENT_ID": "STORED_ID_SENTINEL",
            "INFISICAL_CLIENT_SECRET": "STORED_SECRET_SENTINEL",
            core.ENV_CLIENT_SECRET: "STORED_UA_SENTINEL",
        }
        w = self.use(World(secrets=secrets))
        self.call("run", "--", "mycmd")
        env = self.exec_calls[0][2]
        self.assertEqual(env["ALPHA"], VAL_A)
        self.assertEqual(
            {k for k in env if k.startswith("INFISICAL_")}, {core.ENV_PROJECT}
        )  # only the parent's own
        for var in ("INFISICAL_TOKEN", "INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET"):
            code, _, err = self.call("run", "--only", "ALPHA," + var, "--", "mycmd")
            self.assertEqual(code, 2, var)
        self.assertEqual(len(self.exec_calls), 1)
        self.assertEqual(
            self.call("get", "INFISICAL_TOKEN")[:2], (0, "STORED_TOKEN_SENTINEL")
        )  # get still reads it

    def test_the_parents_auth_variables_never_reach_the_child(self):
        self.use(World())
        self.env[core.ENV_TOKEN] = "PARENT_TOKEN_SENTINEL"
        self.env[core.ENV_CLIENT_ID] = "PARENT_ID_SENTINEL"
        self.env[core.ENV_CLIENT_SECRET] = "PARENT_SECRET_SENTINEL"
        self.call("run", "--", "mycmd")
        self.call("run", "--only", "ALPHA", "--", "mycmd")
        self.assertEqual(len(self.exec_calls), 2)
        for _, _, env in self.exec_calls:
            for var in (core.ENV_TOKEN, core.ENV_CLIENT_ID, core.ENV_CLIENT_SECRET):
                self.assertNotIn(var, env)
            self.assertEqual(env["ALPHA"], VAL_A)

    def test_names_that_are_not_env_names_are_dropped(self):
        self.use(World(secrets={"ALPHA": VAL_A, "bad-name": "x", "9LIVES": "y"}))
        self.call("run", "--", "mycmd")
        env = self.exec_calls[0][2]
        self.assertNotIn("bad-name", env)
        self.assertNotIn("9LIVES", env)

    def test_run_requires_dashes_and_a_command(self):
        w = self.use(World())
        self.assertEqual(self.call("run", "mycmd")[0], 2)
        self.assertEqual(self.call("run", "--")[0], 2)
        self.assertEqual(w.calls, [])

    def test_command_not_found_is_127(self):
        self.use(World())
        with mock.patch.object(cli.os, "execvpe", side_effect=FileNotFoundError):
            code, _, err = self.call("run", "--", "nosuchcmd")
        self.assertEqual(code, 127)
        self.assertIn("nosuchcmd", err)
        self.assertNoSentinel(err)


class NamesTests(Base):
    def test_names_sorted_one_per_line_and_no_values(self):
        self.use(World(secrets={"ZED": VAL_B, "ALPHA": VAL_A}))
        code, out, err = self.call("names")
        self.assertEqual((code, out, err), (0, "ALPHA\nZED\n", ""))
        self.assertNoSentinel(out)


class AuthSelectionTests(Base):
    def test_session_is_used_without_a_keychain_item(self):
        w = self.use(World())
        self.call("get", "ALPHA")
        self.assertEqual(w.logins(), [])
        self.assertNotIn(core.ENV_TOKEN, w.exports()[0][1])

    def test_keychain_identity_logs_in_and_exports_with_the_token_in_env(self):
        w = self.use(World(keychain=KEYCHAIN_FULL))
        code, out, _ = self.call("get", "ALPHA")
        self.assertEqual((code, out), (0, VAL_A))
        ((largv, lenv),) = w.logins()
        self.assertIn("--method=universal-auth", largv)
        self.assertEqual(lenv[core.ENV_CLIENT_SECRET], CLIENT_SECRET)
        eargv, eenv = w.exports()[0]
        self.assertEqual(eenv[core.ENV_TOKEN], ACCESS_TOKEN)
        self.assertNotIn(core.ENV_CLIENT_SECRET, eenv)

    def test_system_keychain_identity_is_used_when_the_login_keychain_is_empty(self):
        w = self.use(World(system_keychain=KEYCHAIN_FULL))
        code, out, _ = self.call("get", "ALPHA")
        self.assertEqual((code, out), (0, VAL_A))
        ((largv, lenv),) = w.logins()
        self.assertIn("--method=universal-auth", largv)
        self.assertEqual(lenv[core.ENV_CLIENT_ID], CLIENT_ID)
        self.assertEqual(lenv[core.ENV_CLIENT_SECRET], CLIENT_SECRET)
        eargv, eenv = w.exports()[0]
        self.assertEqual(eenv[core.ENV_TOKEN], ACCESS_TOKEN)
        self.assertNotIn(core.ENV_CLIENT_SECRET, eenv)
        lookups = [a for a, _ in w.calls if a[0] == core.SECURITY_BIN]
        self.assertIn(
            [
                core.SECURITY_BIN,
                "find-generic-password",
                "-s",
                "fleet-secrets",
                "-a",
                core.KEYCHAIN_CLIENT_SECRET,
                "-w",
                core.SYSTEM_KEYCHAIN,
            ],
            lookups,
        )

    def test_only_system_has_the_items_so_the_mode_is_system_keychain_not_keychain(self):
        # Every lookup names its keychain, so System items are never found through the login lookup.
        w = self.use(World(system_keychain=KEYCHAIN_FULL))
        self.assertEqual(core.auth_mode(self.env), "system-keychain")
        lookups = [a for a, _ in w.calls if a[0] == core.SECURITY_BIN]
        self.assertTrue(lookups)
        self.assertTrue(all(a[-1] in (FAKE_LOGIN_KEYCHAIN, core.SYSTEM_KEYCHAIN) for a in lookups))

    def test_no_usable_login_keychain_falls_cleanly_to_system(self):
        self.use(World(system_keychain=KEYCHAIN_FULL))
        with mock.patch.object(core, "login_keychain", lambda: None):
            self.assertEqual(core.auth_mode(self.env), "system-keychain")
            code, out, _ = self.call("get", "ALPHA")
        self.assertEqual((code, out), (0, VAL_A))

    def test_one_item_in_each_keychain_is_not_mixed(self):
        w = self.use(
            World(
                keychain={core.KEYCHAIN_CLIENT_ID: CLIENT_ID},
                system_keychain={core.KEYCHAIN_CLIENT_SECRET: CLIENT_SECRET},
            )
        )
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (4, ""))
        self.assertIn("keychain-incomplete", err)
        self.assertEqual(w.logins(), [])

    def test_login_keychain_beats_the_system_keychain(self):
        w = self.use(
            World(
                keychain=KEYCHAIN_FULL,
                system_keychain={
                    core.KEYCHAIN_CLIENT_ID: "OTHER_ID",
                    core.KEYCHAIN_CLIENT_SECRET: "OTHER_SECRET",
                },
            )
        )
        self.call("get", "ALPHA")
        ((_, lenv),) = w.logins()
        self.assertEqual(lenv[core.ENV_CLIENT_ID], CLIENT_ID)

    def test_env_token_beats_both_keychains(self):
        w = self.use(World(keychain=KEYCHAIN_FULL, system_keychain=KEYCHAIN_FULL))
        self.env[core.ENV_TOKEN] = "ENV_TOKEN_SENTINEL"
        self.call("get", "ALPHA")
        self.assertEqual(w.logins(), [])
        self.assertEqual(w.exports()[0][1][core.ENV_TOKEN], "ENV_TOKEN_SENTINEL")

    def test_half_filled_system_keychain_fails_instead_of_downgrading(self):
        w = self.use(World(system_keychain={core.KEYCHAIN_CLIENT_SECRET: CLIENT_SECRET}))
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (4, ""))
        self.assertIn("keychain-incomplete", err)
        self.assertIn("System keychain", err)
        self.assertEqual(w.exports(), [])
        self.assertNoSentinel(err)

    def test_system_keychain_secret_and_token_never_appear_in_argv_or_the_run_child(self):
        w = self.use(World(system_keychain=KEYCHAIN_FULL))
        self.call("get", "ALPHA")
        self.call("run", "--", "mycmd")
        for argv, _ in w.calls:
            joined = "\x00".join(argv)
            for s in (CLIENT_ID, CLIENT_SECRET, ACCESS_TOKEN):
                self.assertNotIn(s, joined)
        env = self.exec_calls[0][2]
        for var in (core.ENV_TOKEN, core.ENV_CLIENT_ID, core.ENV_CLIENT_SECRET):
            self.assertNotIn(var, env)

    def test_failed_system_keychain_login_error_holds_no_secret(self):
        self.use(World(system_keychain=KEYCHAIN_FULL, login_rc=1))
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (4, ""))
        self.assertIn("login-failed", err)
        self.assertNoSentinel(err)

    def test_get_returns_the_automation_identity_keys_that_run_refuses_to_inject(self):
        # `get` is how the seeding script reads the pair;  only `run` withholds INFISICAL_* names.
        self.use(
            World(
                secrets={
                    "INFISICAL_AUTOMATION_CLIENT_ID": VAL_A,
                    "INFISICAL_AUTOMATION_CLIENT_SECRET": VAL_B,
                }
            )
        )
        code, out, _ = self.call("get", "INFISICAL_AUTOMATION_CLIENT_ID")
        self.assertEqual((code, out), (0, VAL_A))
        code, out, _ = self.call("get", "INFISICAL_AUTOMATION_CLIENT_SECRET")
        self.assertEqual((code, out), (0, VAL_B))
        self.assertEqual(self.call("run", "--", "mycmd")[0], 0)
        self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_ID", self.exec_calls[0][2])

    def test_client_secret_and_token_never_appear_in_any_argv(self):
        w = self.use(World(keychain=KEYCHAIN_FULL))
        self.call("get", "ALPHA")
        self.call("run", "--", "mycmd")
        self.assertTrue(w.calls)
        for argv, _ in w.calls:
            joined = "\x00".join(argv)
            for s in (CLIENT_ID, CLIENT_SECRET, ACCESS_TOKEN):
                self.assertNotIn(s, joined)
            self.assertNotIn("--client-secret", joined)
            self.assertNotIn("--token", joined)

    def test_the_machine_token_does_not_reach_the_command_run_by_run(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.call("run", "--", "mycmd")
        env = self.exec_calls[0][2]
        for var in (core.ENV_TOKEN, core.ENV_CLIENT_ID, core.ENV_CLIENT_SECRET):
            self.assertNotIn(var, env)

    def test_half_filled_keychain_fails_instead_of_downgrading(self):
        w = self.use(World(keychain={core.KEYCHAIN_CLIENT_ID: CLIENT_ID}))
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (4, ""))
        self.assertIn("keychain-incomplete", err)
        self.assertEqual(w.exports(), [])
        self.assertNoSentinel(err)

    def test_token_in_env_is_used_as_is(self):
        w = self.use(World(keychain=KEYCHAIN_FULL))
        self.env[core.ENV_TOKEN] = "ENV_TOKEN_SENTINEL"
        self.call("get", "ALPHA")
        self.assertEqual(w.exports()[0][1][core.ENV_TOKEN], "ENV_TOKEN_SENTINEL")
        self.assertEqual(w.logins(), [])

    def test_failed_login_error_holds_no_secret(self):
        self.use(World(keychain=KEYCHAIN_FULL, login_rc=1))
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (4, ""))
        self.assertIn("login-failed", err)
        self.assertNoSentinel(err)


class NoValueInErrorsTests(Base):
    def test_failed_export_is_a_reason_token_only(self):
        self.use(World(export_rc=1))
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (4, ""))
        self.assertIn("exit-1", err)
        self.assertNoSentinel(out, err)

    def test_bad_json_is_a_reason_token_only(self):
        self.use(World(export_stdout=("not json " + VAL_A).encode()))
        code, _, err = self.call("names")
        self.assertEqual(code, 4)
        self.assertIn("bad-output", err)
        self.assertNoSentinel(err)

    def test_timeout_and_missing_cli(self):
        def boom(argv, **kw):
            if argv[0] == core.SECURITY_BIN:
                return subprocess.CompletedProcess(argv, 44, b"", b"")
            raise subprocess.TimeoutExpired(argv, 1)

        with mock.patch.object(core.subprocess, "run", boom):
            code, _, err = self.call("get", "ALPHA")
        self.assertEqual((code, err.strip()), (4, "fleet-secrets: timeout"))
        with mock.patch.object(core, "find_cli", lambda env: None):
            code, _, err = self.call("get", "ALPHA")
        self.assertEqual(code, 4)
        self.assertIn("cli-missing", err)

    def test_list_shaped_export_is_accepted(self):
        body = json.dumps(
            [{"key": "ALPHA", "value": VAL_A}, {"secretKey": "BETA", "secretValue": VAL_B}]
        )
        self.use(World(export_stdout=body.encode()))
        self.assertEqual(self.call("get", "BETA")[:2], (0, VAL_B))


class ConfigTests(Base):
    def test_project_id_env_beats_file(self):
        (self.home / ".agent-sync").mkdir()
        other = "00000000-0000-4000-8000-0000000000ff"
        (self.home / ".agent-sync" / "fleet-ops-project-id").write_text(other + "\n")
        self.assertEqual(core.infisical_project_id({core.ENV_PROJECT: PROJECT}, self.home), PROJECT)
        self.assertEqual(core.infisical_project_id({}, self.home), other)

    def test_missing_project_id_fails_closed_with_the_shared_text(self):
        self.env.pop(core.ENV_PROJECT)
        w = self.use(World())
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (4, ""))
        self.assertIn(
            "the Fleet-OPS Infisical project id is not set:  set INFISICAL_FLEET_OPS_PROJECT_ID "
            "or put it in ~/.agent-sync/fleet-ops-project-id "
            "(it lives in the private fleet-ops inventory)",
            err,
        )
        self.assertEqual(w.calls, [])

    def test_malformed_project_id_is_not_echoed(self):
        self.env[core.ENV_PROJECT] = "SENTINEL_NOT_A_UUID"
        self.use(World())
        code, _, err = self.call("get", "ALPHA")
        self.assertEqual(code, 4)
        self.assertIn("is not a UUID", err)
        self.assertNotIn("SENTINEL_NOT_A_UUID", err)

    def test_non_prod_environment_is_refused_before_any_call(self):
        w = self.use(World())
        self.env["INFISICAL_ENVIRONMENT"] = "dev"
        code, _, err = self.call("get", "ALPHA")
        self.assertEqual(code, 2)
        self.assertIn("prod is the only environment", err)
        self.assertEqual(w.calls, [])
        self.env["INFISICAL_ENVIRONMENT"] = "prod"
        self.assertEqual(self.call("get", "ALPHA")[0], 0)


class DoctorTests(Base):
    def test_doctor_reports_booleans_and_counts_only(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        code, out, err = self.call("doctor", "--json")
        rep = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(
            rep["ok"]
            and rep["cli_present"]
            and rep["project_id_resolvable"]
            and rep["fetch_names_ok"]
        )
        self.assertEqual(
            (rep["auth_mode"], rep["names_count"], rep["cli_version"]), ("keychain", 2, "0.43.141")
        )
        for v in rep.values():
            self.assertIsInstance(v, (bool, int, str, type(None)))
        self.assertNoSentinel(out, err)
        self.assertNotIn(PROJECT, out)

    def test_doctor_reports_the_system_keychain_as_booleans_and_its_auth_mode(self):
        self.use(World(system_keychain=KEYCHAIN_FULL))
        code, out, err = self.call("doctor", "--json")
        rep = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(rep["auth_mode"], "system-keychain")
        self.assertIs(rep["system_keychain_client_id_present"], True)
        self.assertIs(rep["system_keychain_client_secret_present"], True)
        self.assertIs(rep["keychain_client_id_present"], False)
        self.assertIs(rep["keychain_client_secret_present"], False)
        self.assertNoSentinel(out, err)

    def test_doctor_session_mode_text_output(self):
        self.use(World())
        code, out, _ = self.call("doctor")
        self.assertEqual(code, 0)
        self.assertIn("auth_mode: session\n", out)
        self.assertIn("session_login_valid: true\n", out)
        self.assertNoSentinel(out)

    def test_doctor_exits_1_and_gives_a_reason_when_the_fetch_fails(self):
        self.use(World(export_rc=1))
        code, out, _ = self.call("doctor")
        self.assertEqual(code, 1)
        self.assertIn("fetch_names_ok: false\n", out)
        self.assertIn("failure_reason: fetch-failed\n", out)
        self.assertNoSentinel(out)


class UsageTests(Base):
    def test_no_arguments_and_unknown_command(self):
        self.assertEqual(self.call()[0], 2)
        self.assertEqual(self.call("frobnicate")[0], 2)
        self.assertEqual(self.call("help")[0], 0)



class SetTests(Base):
    """`set` writes one secret over REST with the machine identity's token.  _api is faked, so no network."""

    def setUp(self):
        super().setUp()
        self.api = []
        self.statuses = {}

    def fake_api(self, method, path, token, body, env):
        self.api.append((method, path, token, dict(body or {})))
        return self.statuses.get((method, path.split("/v3/secrets/raw/")[0] if "/v3/" in path else path), self.statuses.get(method, 200))

    def run_set(self, *argv, stdin="VAL_TO_WRITE_1\n", env=None):
        with mock.patch.object(core, "_api", self.fake_api), mock.patch.object(cli.sys, "stdin", io.StringIO(stdin)):
            return self.call("set", *argv, env=env)

    def test_update_path_sends_the_value_in_the_body_only_and_strips_one_newline(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        code, out, err = self.run_set("MY_KEY", "--path", "/linear")
        self.assertEqual((code, out, err), (0, "updated MY_KEY at /linear\n", ""))
        method, path, token, body = self.api[-1]
        self.assertEqual((method, path, token), ("PATCH", "/v3/secrets/raw/MY_KEY", ACCESS_TOKEN))
        self.assertEqual(body["secretValue"], "VAL_TO_WRITE_1")
        self.assertEqual((body["environment"], body["secretPath"], body["type"]), ("prod", "/linear", "shared"))
        self.assertEqual(body["workspaceId"], PROJECT)

    def test_a_missing_secret_is_created_after_the_patch_404s(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.statuses["PATCH"] = 404
        code, out, _ = self.run_set("NEW_KEY", "--path", "/linear")
        self.assertEqual((code, out), (0, "created NEW_KEY at /linear\n"))
        self.assertEqual([a[0] for a in self.api if "/v3/" in a[1]], ["PATCH", "POST"])

    def test_the_folder_chain_is_created_first(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.run_set("K", "--path", "/a/b")
        folders = [(a[3]["name"], a[3]["path"]) for a in self.api if a[1] == "/v1/folders"]
        self.assertEqual(folders, [("a", "/"), ("b", "/a")])

    def test_a_failed_write_exits_4_and_never_echoes_the_value_or_token(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.statuses["PATCH"] = 403
        code, out, err = self.run_set("K", "--path", "/linear", stdin=VAL_A + "\n")
        self.assertEqual(code, 4)
        self.assertIn("http-403", err)
        self.assertNoSentinel(out, err)

    def test_the_cli_login_session_is_refused_because_it_cannot_hand_over_a_token(self):
        self.use(World())  # no keychain items:  session mode
        code, out, err = self.run_set("K", "--path", "/linear")
        self.assertEqual(code, 4)
        self.assertIn("machine identity", err)
        self.assertEqual(self.api, [])

    def test_an_empty_value_and_a_bad_name_are_usage_errors_before_any_call(self):
        w = self.use(World(keychain=KEYCHAIN_FULL))
        self.assertEqual(self.run_set("K", "--path", "/linear", stdin="")[0], 2)
        self.assertEqual(self.run_set("A-B", "--path", "/linear")[0], 2)
        self.assertEqual(self.api, [])
        self.assertEqual(w.logins(), [])

    def test_the_value_is_never_an_argument_and_never_reaches_a_subprocess(self):
        w = self.use(World(keychain=KEYCHAIN_FULL))
        self.run_set("K", "--path", "/linear", stdin=VAL_B)
        for argv, env in w.calls:
            self.assertNotIn(VAL_B, " ".join(argv))
            self.assertNotIn(VAL_B, " ".join(env.values()))


if __name__ == "__main__":
    unittest.main()
