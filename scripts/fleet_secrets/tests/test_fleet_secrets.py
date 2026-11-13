"""fleet-secrets CLI behavior tests with fake REST and explicit manual-session boundaries."""

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
OTHER_PROJECT = (
    "00000000-0000-4000-8000-000000000002"  # placeholder for the AI Fleet Coordinator project
)
VAL_A = "SENTINEL_VALUE_A_9f3c"
VAL_B = "SENTINEL_VALUE_B_77d1"
CLIENT_ID = "SENTINEL_CLIENT_ID_c0de"
CLIENT_SECRET = "SENTINEL_CLIENT_SECRET_5ec7"
ACCESS_TOKEN = "SENTINEL_ACCESS_TOKEN_a11"
ALL_SENTINELS = (VAL_A, VAL_B, CLIENT_ID, CLIENT_SECRET, ACCESS_TOKEN)
KEYCHAIN_FULL = {"infisical-client-id": CLIENT_ID, "infisical-client-secret": CLIENT_SECRET}


class World:
    """Fake REST responses and the explicit interactive CLI-session route."""

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
        self.fetches = []

    def fetch_project(
        self, project, path, env, *, token=None, writer=False, names=None, timeout=None
    ):
        refusal = core.environment_refusal(env)
        if refusal:
            raise core.SecretsError(refusal, 2)
        if self.export_rc:
            message = "timeout" if self.export_rc == 124 else "fetch-failed"
            raise core.SecretsError(message)
        project_name = core.project_name_for_id(project, env)
        token = token or core.resolve_token(env, writer=writer, project_name=project_name)
        self.fetches.append((project, path, dict(env), token, writer, names, timeout))
        if self.export_stdout is not None:
            try:
                return core._pairs(json.loads(self.export_stdout.decode("utf-8")))
            except (ValueError, UnicodeDecodeError):
                raise core.SecretsError("fetch-failed: bad-output") from None
        return {key: value for key, value in self.secrets.items() if core.NAME_RE.fullmatch(key)}

    def list_names(self, project, path, env, *, token=None, timeout=None):
        refusal = core.environment_refusal(env)
        if refusal:
            raise core.SecretsError(refusal, 2)
        if self.export_rc:
            message = "timeout" if self.export_rc == 124 else "fetch-failed"
            raise core.SecretsError(message)
        project_name = core.project_name_for_id(project, env)
        token = token or core.resolve_token(env, project_name=project_name)
        self.fetches.append((project, path, dict(env), token, False, (), timeout))
        if self.export_stdout is not None:
            try:
                json.loads(self.export_stdout.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise core.SecretsError("fetch-failed: bad-output") from None
        return tuple(sorted(key for key in self.secrets if core.NAME_RE.fullmatch(key)))

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
                raise AssertionError(f"keychain lookup without an explicit keychain path: {argv!r}")
            if acct in store:
                return subprocess.CompletedProcess(argv, 0, (store[acct] + "\n").encode(), b"")
            return subprocess.CompletedProcess(argv, 44, b"", b"not found")
        if argv[0] != FAKE_CLI:
            raise AssertionError(f"unexpected program {argv[0]!r}")
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
        raise AssertionError(f"unexpected argv {argv[:2]!r}")

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
        self.env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin",
            core.ENV_PROJECT: PROJECT,
            core.PROJECT_SOURCES["ai-fleet-coordinator"][0]: OTHER_PROJECT,
            core.ENV_TOKEN: "st.test-service-token-fixture",
            core.ENV_AI_FLEET_COORDINATOR_TOKEN: "st.ai-fleet-token-fixture",
            core.ENV_WRITE_TOKEN: "st.test-write-token-fixture",
        }
        self.exec_calls = []
        patches = [
            mock.patch.object(core, "find_cli", lambda env: FAKE_CLI),
            mock.patch.object(core, "SECURITY_BIN", sys.executable),  # exists on every platform
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
        fetch = mock.patch.object(core, "fetch_project", world.fetch_project)
        fetch.start()
        self.addCleanup(fetch.stop)
        names = mock.patch.object(core, "list_project_names", world.list_names)
        names.start()
        self.addCleanup(names.stop)
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

    def test_get_several_names_prints_one_json_object(self):
        w = self.use(World())
        code, out, err = self.call("get", "ALPHA", "BETA")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out), {"ALPHA": VAL_A, "BETA": VAL_B})
        self.assertEqual(len(w.fetches), 1)
        self.assertEqual((w.fetches[0][0], w.fetches[0][1]), (PROJECT, "/"))

    def test_get_several_names_with_one_absent_exits_3_and_prints_nothing(self):
        self.use(World())
        code, out, err = self.call("get", "ALPHA", "NOPE")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("NOPE", err)
        self.assertNoSentinel(out, err)

    def test_missing_key_exits_3_and_names_only_the_key(self):
        self.use(World())
        code, out, err = self.call("get", "NOPE")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("NOPE", err)
        self.assertNoSentinel(out, err)

    def test_path_is_passed_and_prod_is_the_only_env(self):
        w = self.use(World())
        self.call("get", "ALPHA", "--path", "/global-api-keys")
        self.assertEqual((w.fetches[0][0], w.fetches[0][1]), (PROJECT, "/global-api-keys"))

    def test_path_equals_form(self):
        w = self.use(World())
        self.call("names", "--path=/x")
        self.assertEqual(w.fetches[0][1], "/x")

    def test_project_option_reads_the_other_project_for_get_and_names(self):
        other = "00000000-0000-4000-8000-000000000002"
        self.env["INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID"] = other
        w = self.use(World())
        self.assertEqual(
            self.call("get", "ALPHA", "--project", "ai-fleet-coordinator")[:2], (0, VAL_A)
        )
        self.assertEqual(
            self.call("names", "--project=ai-fleet-coordinator", "--path", "/fleetlink")[0], 0
        )
        self.call("get", "ALPHA")
        ids = [item[0] for item in w.fetches]
        self.assertEqual(ids, [other, other, PROJECT])

    def test_unknown_project_is_a_usage_error_before_any_fetch(self):
        w = self.use(World())
        self.assertEqual(self.call("get", "ALPHA", "--project", "other")[0], 2)
        self.assertEqual(self.call("get", "ALPHA", "--project")[0], 2)
        self.assertEqual(w.calls, [])

    def test_bad_name_and_bad_path_are_usage_errors_before_any_fetch(self):
        w = self.use(World())
        self.assertEqual(self.call("get", "A-B")[0], 2)
        self.assertEqual(self.call("get", "ALPHA", "--path", "../x")[0], 2)
        self.assertEqual(w.calls, [])

    def test_a_path_with_a_trailing_newline_is_refused_before_any_fetch(self):
        # The whole path must match:  `$` also matches before a final newline,
        # so a bare re.match would pass "/github-apps\n" on to the export as a
        # folder of its own.
        w = self.use(World())
        self.assertEqual(self.call("get", "ALPHA", "--path", "/github-apps\n")[0], 2)
        self.assertEqual(self.call("names", "--path=/x\n")[0], 2)
        self.assertEqual(w.calls, [])
        with self.assertRaises(core.UsageError):
            core.check_path("/github-apps\n")
        self.assertEqual(core.check_path("/github-apps"), "/github-apps")


class UnresolvedReferenceTests(Base):
    def test_unresolved_reference_fails_closed_naming_the_key(self):
        self.use(World(secrets={"ALPHA": VAL_A, "BROKEN": "${folder.OTHER}"}))
        code, _out, err = self.call("run", "--", "true")
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

    def test_names_lists_keys_despite_an_unresolved_value(self):
        # Listing keys uses no value, so one key whose value is an unexpanded
        # reference must not take the whole listing down (AFC-592).  `get` and
        # `run` still fail closed on that key (covered above).
        self.use(World(secrets={"ALPHA": VAL_A, "BROKEN": "${x}"}))
        code, out, _err = self.call("names")
        self.assertEqual(code, 0)
        self.assertEqual(out.split(), ["ALPHA", "BROKEN"])
        self.assertNotIn(VAL_A, out)
        self.assertNotIn("${x}", out)

    def test_a_value_that_merely_contains_a_reference_is_fine(self):
        self.use(World(secrets={"ALPHA": "prefix-${x}-suffix"}))
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (5, ""))
        self.assertIn("ALPHA", err)
        self.assertNotIn("${x}", err)


class RunTests(Base):
    def test_run_injects_env_and_execs(self):
        self.use(World())
        self.env["KEEP"] = "kept"
        _code, _, err = self.call("run", "--", "mycmd", "--flag", "x")
        self.assertEqual(err, "")
        ((prog, argv, env),) = self.exec_calls
        self.assertEqual((prog, argv), ("mycmd", ["mycmd", "--flag", "x"]))
        self.assertEqual((env["ALPHA"], env["BETA"], env["KEEP"]), (VAL_A, VAL_B, "kept"))

    def test_run_replaces_an_existing_variable(self):
        self.use(World())
        self.env["ALPHA"] = "old"
        self.call("run", "--", "mycmd")
        self.assertEqual(self.exec_calls[0][2]["ALPHA"], VAL_A)

    def test_noninteractive_get_names_and_run_require_a_scoped_token(self):
        for args in (("get", "ALPHA"), ("names",), ("run", "--", "mycmd")):
            with self.subTest(args=args):
                world = self.use(World())
                env = dict(self.env)
                env.pop(core.ENV_TOKEN)
                env.pop(core.ENV_AI_FLEET_COORDINATOR_TOKEN)
                code, _out, err = self.call(*args, env=env)
                self.assertEqual(code, 4)
                self.assertIn(core.ENV_TOKEN, err)
                self.assertEqual(world.fetches, [])
                self.assertEqual(self.exec_calls, [])

    def test_interactive_get_keeps_the_manual_cli_session_route(self):
        self.use(World())
        env = dict(self.env)
        env.pop(core.ENV_TOKEN)
        env.pop(core.ENV_AI_FLEET_COORDINATOR_TOKEN)

        class TTY:
            def isatty(self):
                return True

        with (
            mock.patch.object(cli.sys, "stdin", TTY()),
            mock.patch.object(cli.sys, "stderr", TTY()),
        ):
            code, out, err = self.call("get", "ALPHA", env=env)
        self.assertEqual((code, out, err), (0, VAL_A, ""))

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
        self.use(World(secrets=secrets))
        self.call("run", "--", "mycmd")
        env = self.exec_calls[0][2]
        self.assertEqual(env["ALPHA"], VAL_A)
        self.assertEqual(
            {k for k in env if k.startswith("INFISICAL_")},
            {k for k in self.env if k.startswith("INFISICAL_")}
            - {
                core.ENV_TOKEN,
                core.ENV_AI_FLEET_COORDINATOR_TOKEN,
                core.ENV_WRITE_TOKEN,
            },
        )
        for var in ("INFISICAL_TOKEN", "INFISICAL_CLIENT_ID", "INFISICAL_CLIENT_SECRET"):
            code, _, _err = self.call("run", "--only", "ALPHA," + var, "--", "mycmd")
            self.assertEqual(code, 2, var)
        self.assertEqual(len(self.exec_calls), 1)
        self.assertEqual(
            self.call("get", "INFISICAL_TOKEN")[:2], (0, "STORED_TOKEN_SENTINEL")
        )  # get still reads it

    def test_the_parents_auth_variables_never_reach_the_child(self):
        self.use(World())
        self.env[core.ENV_TOKEN] = "st.parent-token-fixture"
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
    def test_names_never_exports_secret_values(self):
        world = self.use(World())
        with mock.patch.object(core, "fetch_project", side_effect=AssertionError("value export")):
            code, out, err = self.call("names", "--path", "/metadata")
        self.assertEqual((code, out, err), (0, "ALPHA\nBETA\n", ""))
        self.assertEqual((world.fetches[0][1], world.fetches[0][5]), ("/metadata", ()))

    def test_legacy_import_uses_the_shared_command_implementation(self):
        import fleet_infisical_cli

        self.assertIs(cli, fleet_infisical_cli)

    def test_names_sorted_one_per_line_and_no_values(self):
        self.use(World(secrets={"ZED": VAL_B, "ALPHA": VAL_A}))
        code, out, err = self.call("names")
        self.assertEqual((code, out, err), (0, "ALPHA\nZED\n", ""))
        self.assertNoSentinel(out)


class ScopedServiceTokenTests(Base):
    def test_fleet_ops_token_is_passed_directly_without_login(self):
        world = self.use(World())
        code, out, _ = self.call("get", "ALPHA")
        self.assertEqual((code, out), (0, VAL_A))
        self.assertEqual(world.logins(), [])
        self.assertEqual(world.fetches[0][3], self.env[core.ENV_TOKEN])
        self.assertFalse(world.fetches[0][4])

    def test_invalid_service_token_fails_before_export(self):
        world = self.use(World())
        env = dict(self.env, **{core.ENV_TOKEN: "st.short"})
        code, _, err = self.call("get", "ALPHA", env=env)
        self.assertEqual(code, 4)
        self.assertIn("service-token-required", err)
        self.assertEqual(world.fetches, [])

    def test_second_project_uses_separate_token_binding(self):
        world = self.use(World())
        code, _, _ = self.call("get", "ALPHA", "--project", "ai-fleet-coordinator")
        self.assertEqual(code, 0)
        self.assertEqual(world.fetches[0][3], self.env[core.ENV_AI_FLEET_COORDINATOR_TOKEN])


class NoValueInErrorsTests(Base):
    def test_failed_export_is_a_reason_token_only(self):
        self.use(World(export_rc=1))
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (4, ""))
        self.assertIn("fetch-failed", err)
        self.assertNoSentinel(out, err)

    def test_bad_json_is_a_reason_token_only(self):
        self.use(World(export_stdout=("not json " + VAL_A).encode()))
        code, _, err = self.call("names")
        self.assertEqual(code, 4)
        self.assertIn("bad-output", err)
        self.assertNoSentinel(err)

    def test_timeout_and_missing_cli(self):
        world = self.use(World(export_rc=124))
        code, _, err = self.call("get", "ALPHA")
        self.assertEqual((code, err.strip()), (4, "fleet-secrets: timeout"))
        world.export_rc = 0
        with mock.patch.object(core, "find_cli", lambda env: None):
            code, _, err = self.call("get", "ALPHA")
        self.assertEqual((code, err), (0, ""))

    def test_list_shaped_export_is_accepted(self):
        body = json.dumps(
            [{"key": "ALPHA", "value": VAL_A}, {"secretKey": "BETA", "secretValue": VAL_B}]
        )
        self.use(World(export_stdout=body.encode()))
        self.assertEqual(self.call("get", "BETA")[:2], (0, VAL_B))


class ConfigTests(Base):
    def test_project_id_env_beats_file(self):
        (self.home / ".simple-tools").mkdir()
        other = "00000000-0000-4000-8000-0000000000ff"
        (self.home / ".simple-tools" / "fleet-ops-project-id").write_text(other + "\n")
        self.assertEqual(core.infisical_project_id({core.ENV_PROJECT: PROJECT}, self.home), PROJECT)
        self.assertEqual(core.infisical_project_id({}, self.home), other)

    def test_missing_project_id_fails_closed_with_the_shared_text(self):
        self.env.pop(core.ENV_PROJECT)
        w = self.use(World())
        code, out, err = self.call("get", "ALPHA")
        self.assertEqual((code, out), (4, ""))
        self.assertIn(
            "the Fleet-OPS Infisical project id is not set:  set INFISICAL_FLEET_OPS_PROJECT_ID "
            "or put it in ~/.simple-tools/fleet-ops-project-id "
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
    def test_doctor_checks_the_second_project_with_its_scoped_token(self):
        self.use(World())
        code, out, _ = self.call("doctor", "--json")
        rep = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(rep["ai_fleet_coordinator_project_id_resolvable"])
        self.assertTrue(rep["ai_fleet_coordinator_fetch_ok"])
        self.env.pop(core.PROJECT_SOURCES["ai-fleet-coordinator"][0])
        code, out, _ = self.call("doctor", "--json")
        rep = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(rep["ai_fleet_coordinator_project_id_resolvable"])
        self.assertFalse(rep["ok"])

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
            (rep["auth_mode"], rep["names_count"], rep["cli_version"]), ("token", 2, "0.43.141")
        )
        self.assertTrue(rep["reader_configured"])
        self.assertTrue(rep["writer_configured"])
        for v in rep.values():
            self.assertIsInstance(v, (bool, int, str, type(None)))
        self.assertNoSentinel(out, err)
        self.assertNotIn(PROJECT, out)

    def test_doctor_reports_write_token_presence_without_values(self):
        self.use(World())
        code, out, err = self.call("doctor", "--json")
        rep = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(rep["auth_mode"], "token")
        self.assertTrue(rep["writer_configured"])
        self.assertNoSentinel(out, err)

    def test_doctor_session_mode_text_output(self):
        self.use(World())
        self.env.pop(core.ENV_TOKEN)

        class TTY:
            def isatty(self):
                return True

        with (
            mock.patch.object(cli.sys, "stdin", TTY()),
            mock.patch.object(cli.sys, "stderr", TTY()),
        ):
            code, out, _ = self.call("doctor")
        self.assertEqual(code, 1)
        self.assertIn("auth_mode: missing\n", out)
        self.assertIn("reader_configured: false\n", out)
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
        self.bodies = {}

    def fake_api(self, method, path, token, body, env):
        self.api.append((method, path, token, dict(body or {})))
        return self.statuses.get(
            (method, path.split("/v3/secrets/raw/")[0] if "/v3/" in path else path),
            self.statuses.get(method, 200),
        )

    def fake_api_json(self, method, path, token, body, env, **kwargs):
        status = self.fake_api(method, path, token, body, env)
        return status, self.bodies.get((method, path), None)

    def run_set(self, *argv, stdin="VAL_TO_WRITE_1\n", env=None):
        with (
            mock.patch.object(core, "_api", self.fake_api),
            mock.patch.object(core, "_api_json", self.fake_api_json),
            mock.patch.object(core, "verify_service_token", return_value=("/linear",)),
            mock.patch.object(cli.sys, "stdin", io.StringIO(stdin)),
        ):
            return self.call("set", *argv, env=env)

    def test_update_path_sends_the_value_in_the_body_only_and_strips_one_newline(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        code, out, err = self.run_set("CACHE_MY_APP", "--path", "/linear")
        self.assertEqual((code, out, err), (0, "updated CACHE_MY_APP at /linear\n", ""))
        method, path, token, body = self.api[-1]
        self.assertEqual(
            (method, path, token),
            ("PATCH", "/v3/secrets/raw/CACHE_MY_APP", self.env[core.ENV_WRITE_TOKEN]),
        )
        self.assertEqual(body["secretValue"], "VAL_TO_WRITE_1")
        self.assertEqual(
            (body["environment"], body["secretPath"], body["type"]), ("prod", "/linear", "shared")
        )
        self.assertEqual(body["workspaceId"], PROJECT)

    def test_a_missing_secret_is_created_after_the_patch_404s(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.statuses["PATCH"] = 404
        code, out, _ = self.run_set("CACHE_NEW_APP", "--path", "/linear")
        self.assertEqual((code, out), (0, "created CACHE_NEW_APP at /linear\n"))
        self.assertEqual([a[0] for a in self.api if "/v3/secrets/raw/" in a[1]], ["PATCH", "POST"])

    def test_set_never_creates_a_folder_and_checks_it_exists_first(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.run_set("CACHE_K", "--path", "/linear")
        self.assertEqual([a for a in self.api if a[1] == "/v1/folders"], [])
        self.assertEqual(self.api[0][0], "GET")
        self.assertIn("secretPath=/linear", self.api[0][1])

    def test_a_missing_folder_fails_with_folder_missing_and_writes_nothing(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.statuses["GET"] = 404
        code, out, err = self.run_set("CACHE_K", "--path", "/linear", stdin=VAL_A)
        self.assertEqual((code, out), (4, ""))
        self.assertIn("folder-missing", err)
        self.assertEqual([a[0] for a in self.api], ["GET"])
        self.assertNoSentinel(err)

    def test_a_failed_folder_check_other_than_404_also_writes_nothing(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.statuses["GET"] = 500
        code, _, err = self.run_set("CACHE_K", "--path", "/linear")
        self.assertEqual(code, 4)
        self.assertIn("folder-check http-500", err)
        self.assertEqual([a[0] for a in self.api], ["GET"])

    def test_names_and_folders_off_the_allowlist_are_refused_before_any_login_or_call(self):
        w = self.use(World(keychain=KEYCHAIN_FULL))
        for name, path in (
            ("ALPHA", "/linear"),  # right folder, wrong name
            ("CACHE_X", "/"),  # right name shape, root folder
            ("CACHE_X", "/zulip"),  # right name shape, wrong folder
            ("CACHE_x", "/linear"),  # lower case
            ("CACHE_", "/linear"),  # nothing after the prefix
            ("REALM_ADMIN_ZULIP_KEY", "/zulipadmin"),
            ("CACHE_X", "/linear/sub"),
        ):
            code, out, err = self.run_set(name, "--path", path, stdin=VAL_A)
            self.assertEqual((code, out), (2, ""), (name, path))
            self.assertIn("allowlist", err)
            self.assertNoSentinel(err)
        self.assertEqual(self.api, [])
        self.assertEqual(w.logins(), [])

    def test_no_environment_variable_widens_the_allowlist(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        env = dict(
            self.env,
            FLEET_SECRETS_WRITE_ALLOW=".*",
            FLEET_SECRETS_ALLOW_ALL="1",
            FLEET_SECRETS_WRITE_PATHS="/zulip",
        )
        code, _, _ = self.run_set("ALPHA", "--path", "/zulip", env=env)
        self.assertEqual(code, 2)
        self.assertEqual(self.api, [])

    def test_the_allowlist_is_exactly_the_reviewed_one(self):
        self.assertEqual(
            core.WRITE_ALLOWLIST,
            (
                ("/linear", r"^CACHE_[A-Z0-9_]+$"),
                (
                    r"/cli-logins/[A-Z][A-Z0-9-]{0,15}",
                    r"^(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|COPILOT_GITHUB_TOKEN)$",
                ),
                (
                    r"/cli-logins",
                    r"^(GH_TOKEN|GITHUB_TOKEN|GH_ENTERPRISE_TOKEN|COPILOT_GITHUB_TOKEN)_[A-Z][A-Z0-9-]{0,15}$",
                ),
            ),
        )

    def test_folder_probe_accepts_existing_path_without_creating_anything(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        with (
            mock.patch.object(core, "_api_json", self.fake_api_json),
            mock.patch.object(core, "verify_service_token", return_value=("/cli-logins/OPENCODE",)),
        ):
            core.ensure_folder("/cli-logins/OPENCODE", self.env, "SENTINEL_TOKEN_x1")
        self.assertEqual([item[0] for item in self.api], ["GET"])
        self.assertFalse(any("/v1/folders" in item[1] for item in self.api))

    def test_a_rejected_folder_that_is_not_an_existing_folder_is_a_failure(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.statuses["GET"] = 500
        with (
            mock.patch.object(core, "_api_json", self.fake_api_json),
            mock.patch.object(core, "verify_service_token", return_value=("/cli-logins/OPENCODE",)),
        ):
            with self.assertRaises(core.SecretsError) as caught:
                core.ensure_folder("/cli-logins/OPENCODE", self.env, "SENTINEL_TOKEN_x1")
        self.assertIn("folder-missing-or-inaccessible", caught.exception.message)

    def test_the_folder_is_never_created_outside_prod(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        env = dict(self.env)
        env["INFISICAL_ENV"] = "staging"
        with self.assertRaises(core.SecretsError) as caught:
            core.ensure_folder("/cli-logins/OPENCODE", env, "SENTINEL_TOKEN_x1")
        self.assertEqual(self.api, [])
        self.assertIn("prod", caught.exception.message)

    def test_a_failed_write_exits_4_and_never_echoes_the_value_or_token(self):
        self.use(World(keychain=KEYCHAIN_FULL))
        self.statuses["PATCH"] = 403
        code, out, err = self.run_set("CACHE_K", "--path", "/linear", stdin=VAL_A + "\n")
        self.assertEqual(code, 4)
        self.assertIn("http-403", err)
        self.assertNoSentinel(out, err)

    def test_set_requires_separate_write_token(self):
        self.use(World())
        env = dict(self.env)
        env.pop(core.ENV_WRITE_TOKEN)
        code, _out, err = self.run_set("CACHE_K", "--path", "/linear", env=env)
        self.assertEqual(code, 4)
        self.assertIn(core.ENV_WRITE_TOKEN, err)
        self.assertEqual(self.api, [])

    def test_an_empty_value_and_a_bad_name_are_usage_errors_before_any_call(self):
        w = self.use(World(keychain=KEYCHAIN_FULL))
        self.assertEqual(self.run_set("CACHE_K", "--path", "/linear", stdin="")[0], 2)
        self.assertEqual(self.run_set("A-B", "--path", "/linear")[0], 2)
        self.assertEqual(self.api, [])
        self.assertEqual(w.logins(), [])

    def test_the_value_is_never_an_argument_and_never_reaches_a_subprocess(self):
        w = self.use(World(keychain=KEYCHAIN_FULL))
        self.run_set("CACHE_K", "--path", "/linear", stdin=VAL_B)
        self.assertTrue(any(VAL_B == body.get("secretValue") for _, _, _, body in self.api))
        self.assertFalse(w.calls)
        for _, path, token, _body in self.api:
            self.assertNotIn(VAL_B, path)
            self.assertNotIn(VAL_B, token)


if __name__ == "__main__":
    unittest.main()


class TestFetchVerified(unittest.TestCase):
    """fetch_verified:  the REST row's own fields prove the folder."""

    ENV = {
        "INFISICAL_FLEET_OPS_PROJECT_ID": "00000000-0000-4000-8000-000000000042",
        core.ENV_TOKEN: "st.test-service-token-fixture",
    }
    PROJECT = "00000000-0000-4000-8000-000000000042"
    PATH = "/git-relay-owner"

    def _secret(self, **overrides):
        row = {
            "workspace": self.PROJECT,
            "environment": "prod",
            "type": "shared",
            "secretKey": "GITHUB_TOKEN",
            "secretValue": "the-value",
            "secretValueHidden": False,
            "secretPath": self.PATH,
            "id": "row-id",
            "version": 3,
        }
        row.update(overrides)
        return row

    def _run(self, status=200, answer=None, row_overrides=None):
        if answer is None:
            answer = {"secret": self._secret(**(row_overrides or {}))}
        calls = []

        def fake_api_json(method, path, token, body, env, max_bytes=None, deadline=None):
            calls.append(
                {
                    "method": method,
                    "path": path,
                    "token": token,
                    "max_bytes": max_bytes,
                }
            )
            if path == "/v2/service-token":
                return 200, {
                    "workspace": self.PROJECT,
                    "permissions": ["read"],
                    "scopes": [{"environment": "prod", "secretPath": self.PATH}],
                    "expiresAt": "2099-01-01T00:00:00Z",
                }
            return status, answer

        with mock.patch.object(core, "_api_json", fake_api_json):
            try:
                value = core.fetch_verified(
                    "GITHUB_TOKEN", self.PATH, self.ENV, token=self.ENV[core.ENV_TOKEN]
                )
                return value, calls, None
            except core.SecretsError as exc:
                return None, calls, exc

    def test_happy_path_returns_the_value(self):
        value, calls, err = self._run()
        self.assertIsNone(err)
        self.assertEqual(value, "the-value")
        call = next(item for item in calls if item["path"].startswith("/v3/"))
        self.assertEqual(call["method"], "GET")
        self.assertIn("/v3/secrets/raw/GITHUB_TOKEN?", call["path"])
        self.assertIn("workspaceId=" + self.PROJECT, call["path"])
        self.assertIn("environment=prod", call["path"])
        self.assertIn("secretPath=%2Fgit-relay-owner", call["path"])
        self.assertEqual(call["token"], self.ENV[core.ENV_TOKEN])
        self.assertEqual(call["max_bytes"], core.MAX_VERIFIED_BYTES)

    def test_wrong_secret_path_fails_closed(self):
        _, _, err = self._run(row_overrides={"secretPath": "/"})
        self.assertIsNotNone(err)
        self.assertEqual(err.code, 4)
        self.assertIn("secretPath", str(err))
        self.assertNotIn("the-value", str(err))

    def test_wrong_workspace_fails_closed(self):
        _, _, err = self._run(row_overrides={"workspace": "other-uuid"})
        self.assertEqual(err.code, 4)
        self.assertNotIn("the-value", str(err))

    def test_wrong_environment_fails_closed(self):
        _, _, err = self._run(row_overrides={"environment": "dev"})
        self.assertEqual(err.code, 4)

    def test_personal_type_fails_closed(self):
        _, _, err = self._run(row_overrides={"type": "personal"})
        self.assertEqual(err.code, 4)

    def test_wrong_key_fails_closed(self):
        _, _, err = self._run(row_overrides={"secretKey": "OTHER_KEY"})
        self.assertEqual(err.code, 4)

    def test_hidden_value_fails_closed(self):
        _, _, err = self._run(row_overrides={"secretValueHidden": True})
        self.assertEqual(err.code, 4)

    def test_missing_hidden_field_fails_closed(self):
        row = self._secret()
        del row["secretValueHidden"]
        _, _, err = self._run(answer={"secret": row})
        self.assertEqual(err.code, 4)

    def test_non_string_value_fails_closed(self):
        _, _, err = self._run(row_overrides={"secretValue": 42})
        self.assertEqual(err.code, 4)

    def test_empty_value_fails_closed(self):
        _, _, err = self._run(row_overrides={"secretValue": ""})
        self.assertEqual(err.code, 4)

    def test_unresolved_reference_fails_closed(self):
        _, _, err = self._run(row_overrides={"secretValue": "${OTHER_KEY}"})
        self.assertIsNotNone(err)

    def test_missing_secret_row_fails_closed(self):
        _, _, err = self._run(answer={"unexpected": {}})
        self.assertEqual(err.code, 4)

    def test_absent_key_is_exit_3_naming_folder_and_key_only(self):
        for status in (400, 404):
            _, _, err = self._run(status=status, answer=None)
            self.assertEqual(err.code, 3)
            self.assertIn("GITHUB_TOKEN", str(err))
            self.assertIn("/git-relay-owner", str(err))
            self.assertNotIn("the-value", str(err))

    def test_other_status_is_a_fetch_failure(self):
        _, _, err = self._run(status=500, answer={"secret": self._secret()})
        self.assertEqual(err.code, 4)
        self.assertIn("status-500", str(err))

    def test_unreachable_is_a_fetch_failure(self):
        _, _, err = self._run(status=0, answer=None)
        self.assertEqual(err.code, 4)
        self.assertIn("unreachable", str(err))

    def test_non_json_answer_fails_closed(self):
        # The service-token metadata response is valid, while the raw-secret response is not.
        def fake_api(method, path, token, body, env, **kwargs):
            if path == "/v2/service-token":
                return 200, {
                    "workspace": self.PROJECT,
                    "permissions": ["read"],
                    "scopes": [{"environment": "prod", "secretPath": self.PATH}],
                    "expiresAt": "2099-01-01T00:00:00Z",
                }
            return 200, None

        with mock.patch.object(core, "_api_json", fake_api):
            with self.assertRaises(core.SecretsError) as ctx:
                core.fetch_verified(
                    "GITHUB_TOKEN", self.PATH, self.ENV, token=self.ENV[core.ENV_TOKEN]
                )
        self.assertEqual(ctx.exception.code, 4)
        self.assertIn("bad-output", str(ctx.exception))

    def test_missing_service_token_is_refused(self):
        with self.assertRaises(core.SecretsError) as ctx:
            core.fetch_verified(
                "GITHUB_TOKEN", self.PATH, {"INFISICAL_FLEET_OPS_PROJECT_ID": self.PROJECT}
            )
        self.assertIn("INFISICAL_TOKEN", str(ctx.exception))

    def test_valid_service_token_is_used_when_not_passed_explicitly(self):
        def fake_api(method, path, token, body, env, **kwargs):
            if path == "/v2/service-token":
                return 200, {
                    "workspace": self.PROJECT,
                    "permissions": ["read"],
                    "scopes": [{"environment": "prod", "secretPath": self.PATH}],
                    "expiresAt": "2099-01-01T00:00:00Z",
                }
            return 200, {"secret": self._secret()}

        with mock.patch.object(core, "_api_json", fake_api):
            self.assertEqual(core.fetch_verified("GITHUB_TOKEN", self.PATH, self.ENV), "the-value")


class TestApiJsonBoundedRead(unittest.TestCase):
    def test_oversized_answer_fails_closed_without_logging(self):
        class _Resp:
            status = 200

            def read(self, limit=None):
                data = b"x" * (core.MAX_VERIFIED_BYTES + 10)
                return data if limit is None else data[:limit]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with mock.patch.object(core, "_opener", lambda: mock.Mock(open=lambda *a, **k: _Resp())):
            status, answer = core._api_json(
                "GET", "/v3/secrets/raw/X", "tok", None, {}, max_bytes=core.MAX_VERIFIED_BYTES
            )
        self.assertEqual((status, answer), (0, None))

    def test_unbounded_default_is_unchanged(self):
        class _Resp:
            status = 200

            def read(self, limit=None):
                self.saw_limit = limit
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        resp = _Resp()
        with mock.patch.object(core, "_opener", lambda: mock.Mock(open=lambda *a, **k: resp)):
            status, answer = core._api_json("GET", "/v1/x", "tok", None, {})
        self.assertEqual(status, 200)
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(resp.saw_limit, core.MAX_OUTPUT_BYTES + 1)


class TestCmdGetVerified(unittest.TestCase):
    def test_prints_the_value_only(self):
        import io

        from fleet_secrets import cli

        out = io.StringIO()
        with mock.patch.object(core, "fetch_verified", lambda n, p, env: "v"):
            rc = cli.cmd_get_verified(["NAME", "--path", "/folder"], {}, out)
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "v")

    def test_root_path_is_a_usage_error(self):
        import io

        from fleet_secrets import cli

        with self.assertRaises(core.UsageError):
            cli.cmd_get_verified(["NAME"], {}, io.StringIO())

    def test_dispatched_from_main(self):
        import io

        from fleet_secrets import cli

        out = io.StringIO()
        with mock.patch.object(core, "fetch_verified", lambda n, p, env: "v"):
            rc = cli.main(
                ["get-verified", "NAME", "--path", "/folder"],
                {core.ENV_TOKEN: "st.test-service-token-fixture"},
                out,
                io.StringIO(),
            )
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), "v")
