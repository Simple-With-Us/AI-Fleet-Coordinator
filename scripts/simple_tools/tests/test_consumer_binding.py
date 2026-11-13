"""Explicit reader selection uses synthetic metadata, processes and provider replies only."""

import copy
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import fleet_infisical as core
import fleet_infisical_bootstrap as bootstrap
import fleet_infisical_cli as cli


class DeclarationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "consumer"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "binding.json"
        self.path.write_bytes(b"{}")
        self.path.chmod(0o600)

    def test_reads_only_the_checked_file_and_refuses_unsafe_custody(self):
        self.assertEqual(bootstrap.read_private_declaration(str(self.path)), b"{}")
        for mode in (0o644, 0o660, 0o400):
            with self.subTest(mode=mode):
                self.path.chmod(mode)
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.read_private_declaration(str(self.path))
        self.path.chmod(0o600)
        self.directory.chmod(0o755)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.read_private_declaration(str(self.path))
        self.directory.chmod(0o700)
        with mock.patch.object(bootstrap.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(bootstrap.BootstrapError):
                bootstrap.read_private_declaration(str(self.path))

    def test_links_nonregular_and_oversized_declarations_are_refused(self):
        original = self.directory / "original"
        self.path.rename(original)
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            with self.subTest(kind=kind):
                if kind == "symlink":
                    self.path.symlink_to(original)
                elif kind == "hardlink":
                    os.link(original, self.path)
                elif kind == "fifo":
                    os.mkfifo(self.path, 0o600)
                else:
                    self.path.mkdir(mode=0o700)
                with self.assertRaises(bootstrap.BootstrapError):
                    bootstrap.read_private_declaration(str(self.path))
                self.path.rmdir() if kind == "directory" else self.path.unlink()
        self.path.write_bytes(b"x" * (16 * 1024 + 1))
        self.path.chmod(0o600)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.read_private_declaration(str(self.path))

    def test_ancestor_symlinks_and_relative_paths_never_select_a_store(self):
        link = Path(self.temp.name) / "linked"
        link.symlink_to(self.directory, target_is_directory=True)
        for path in ("binding.json", "", str(link / self.path.name), str(self.directory / "../consumer/binding.json")):
            with self.subTest(path=path), self.assertRaises(bootstrap.BootstrapError):
                bootstrap.read_private_declaration(path)

    def test_path_replacement_during_read_does_not_reopen_an_unchecked_file(self):
        real_read = os.read
        reads = []

        def replace(fd, count):
            if not reads:
                self.path.unlink()
                self.path.symlink_to("/not-a-declared-store")
            reads.append(fd)
            return real_read(fd, count)

        with mock.patch.object(bootstrap.os, "read", side_effect=replace):
            with self.assertRaisesRegex(bootstrap.BootstrapError, "changed"):
                bootstrap.read_private_declaration(str(self.path))

    def test_missing_declaration_creates_no_replacement(self):
        self.path.unlink()
        with self.assertRaisesRegex(bootstrap.BootstrapError, "missing"):
            bootstrap.read_private_declaration(str(self.path))
        self.assertFalse(self.path.exists())


class KeychainTests(unittest.TestCase):
    def setUp(self):
        self.token = "st." + secrets.token_urlsafe(32)
        self.children = []
        self.calls = []
        self.real_popen = subprocess.Popen

    def bind(self, raw, status=0, delay=0, budget=2):
        def start(argv, **options):
            self.calls.append((argv, options))
            code = (
                "import sys,time;"
                f"sys.stderr.buffer.write({raw!r});sys.stderr.flush();"
                f"sys.stdout.buffer.write({raw!r});sys.stdout.flush();"
                f"time.sleep({delay!r});sys.exit({status!r})"
            )
            child = self.real_popen([sys.executable, "-I", "-c", code], **options)
            self.children.append(child)
            return child

        env = {"HOME": "/synthetic-home", "PYTHONPATH": "hostile", "OTHER_TOKEN": "ambient"}
        with mock.patch.object(bootstrap.sys, "platform", "darwin"), mock.patch.object(
            bootstrap.subprocess, "Popen", side_effect=start
        ):
            reason = bootstrap.bind_keychain_token(
                env, token_env=core.ENV_TOKEN, service=bootstrap.KEYCHAIN_SERVICE,
                account="muster-test-reader", deadline=time.monotonic() + budget,
            )
        self.assertTrue(all(child.poll() is not None for child in self.children))
        argv, options = self.calls[-1]
        self.assertEqual(argv, ["/usr/bin/security", "find-generic-password", "-s", bootstrap.KEYCHAIN_SERVICE, "-a", "muster-test-reader", "-w"])
        self.assertEqual(options["env"], {"HOME": "/synthetic-home", "PATH": "/usr/bin:/bin"})
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertNotIn(self.token, repr(argv) + repr(options))
        return reason, env

    def test_one_exact_item_is_bound_with_no_ambient_child_credentials(self):
        reason, env = self.bind((self.token + "\n").encode())
        self.assertEqual(reason, "bootstrap-bound")
        self.assertEqual(env[core.ENV_TOKEN], self.token)

    def test_refusals_never_reflect_output_or_try_another_account(self):
        for raw, status, expected in (
            (self.token.encode(), 44, "bootstrap-missing"),
            (self.token.encode(), 36, "bootstrap-keychain-locked"),
            (self.token.encode(), 1, "bootstrap-unreadable"),
            (b"\xff", 0, "bootstrap-unreadable"),
            (b"", 0, "bootstrap-shape-refused"),
            (b"st." + b"x" * 8192, 0, "bootstrap-shape-refused"),
        ):
            with self.subTest(status=status, size=len(raw)):
                before = len(self.calls)
                reason, env = self.bind(raw, status)
                self.assertEqual(reason, expected)
                self.assertEqual(env[core.ENV_TOKEN], "")
                self.assertEqual(len(self.calls), before + 1)
                self.assertNotIn(self.token, reason)

    def test_timeout_terminates_and_reaps_the_helper(self):
        reason, env = self.bind(self.token.encode(), delay=1, budget=0.05)
        self.assertEqual(reason, "bootstrap-timeout")
        self.assertEqual(env[core.ENV_TOKEN], "")

    def test_conflicting_empty_binding_and_wrong_platform_never_start_a_process(self):
        with mock.patch.object(bootstrap.subprocess, "Popen") as start:
            env = {core.ENV_TOKEN: ""}
            self.assertEqual(bootstrap.bind_keychain_token(env, token_env=core.ENV_TOKEN, service=bootstrap.KEYCHAIN_SERVICE, account="own-reader", deadline=time.monotonic()+1), "bootstrap-binding-conflict")
            with mock.patch.object(bootstrap.sys, "platform", "linux"):
                self.assertEqual(bootstrap.bind_keychain_token({}, token_env=core.ENV_TOKEN, service=bootstrap.KEYCHAIN_SERVICE, account="own-reader", deadline=time.monotonic()+1), "bootstrap-platform-unavailable")
        start.assert_not_called()


class SelectedReaderTests(unittest.TestCase):
    def setUp(self):
        DeclarationTests.setUp(self)
        self.token = "st." + secrets.token_urlsafe(32)
        self.project = str(uuid.uuid4())
        self.declaration = {
            "schemaVersion": 1, "consumer": "muster-data-reader",
            "tokenName": "muster-test-reader", "tokenId": str(uuid.uuid4()),
            "project": "fleet-ops", "projectId": self.project,
            "environment": "prod", "secretPath": "/", "permissions": ["read"],
            "expiresAt": "2099-01-01T00:00:00Z",
            "storage": {"kind": "mac-login-keychain", "service": bootstrap.KEYCHAIN_SERVICE, "account": "muster-test-reader"},
        }
        self.live = {
            "id": self.declaration["tokenId"], "name": self.declaration["tokenName"],
            "workspace": self.project, "permissions": ["read"],
            "scopes": [{"environment": "prod", "secretPath": "/"}],
            "expiresAt": self.declaration["expiresAt"],
        }
        self.calls = []

    def run_cli(self, args, env=None):
        self.path.write_text(json.dumps(self.declaration))
        self.path.chmod(0o600)
        out, err = io.StringIO(), io.StringIO()

        def bind(environ, **options):
            self.assertEqual(options["account"], self.declaration["tokenName"])
            self.assertEqual(set(environ), {"HOME", "PATH", core.ENV_PROJECT, "INFISICAL_ENVIRONMENT", core.ENV_READ_PATHS})
            environ[core.ENV_TOKEN] = self.token
            return "bootstrap-bound"

        def api(method, path, token, body, environ, **options):
            self.calls.append(path)
            self.assertEqual(token, self.token)
            self.assertIsNone(body)
            self.assertEqual(environ[core.ENV_PROJECT], self.project)
            self.assertEqual(environ[core.ENV_READ_PATHS], "/")
            if path == "/v2/service-token":
                return 200, copy.deepcopy(self.live)
            row = {"workspace": self.project, "environment": "prod", "secretPath": "/", "type": "shared", "secretKey": "WANTED", "secretValue": "fixture-value", "secretValueHidden": False}
            if "viewSecretValue=false" in path:
                row.update(secretValue=core.HIDDEN_VALUE_MASK, secretValueHidden=True)
            return 200, {"secret": row} if "/raw/" in path else {"secrets": [row]}

        with mock.patch.object(bootstrap, "bind_keychain_token", side_effect=bind) as bound, mock.patch.object(core, "_api_json", side_effect=api), mock.patch.object(core, "find_cli", side_effect=AssertionError("no optional CLI probe")):
            code = cli.main([*args, "--consumer-binding", str(self.path)], env={} if env is None else env, out=out, err=err)
        self.assertNotIn(self.token, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue(), bound.call_count

    def test_get_names_and_selected_doctor_use_one_own_binding(self):
        for args in (["get", "WANTED"], ["names"], ["doctor", "--json", "--project", "fleet-ops", "--path", "/"]):
            with self.subTest(args=args):
                self.calls.clear()
                code, out, err, count = self.run_cli(args)
                self.assertEqual(code, 0, err)
                self.assertEqual(count, 1)
                if args[0] == "get":
                    self.assertEqual(out, "fixture-value")
                elif args[0] == "names":
                    self.assertEqual(out, "WANTED\n")
                    self.assertTrue(any("viewSecretValue=false" in path for path in self.calls))
                else:
                    self.assertTrue(json.loads(out)["ok"])
                    self.assertIsNone(json.loads(out)["cli_present"])
                    self.assertNotIn("fixture-value", out)

    def test_invalid_routes_and_conflicting_bindings_refuse_before_store_or_provider(self):
        bad = (["set", "CACHE_X"], ["run", "--", "true"], ["get-verified", "WANTED"], ["doctor"], ["names", "--project", "ai-fleet-coordinator"], ["names", "--path", "/wake-router"], ["names", "--path", "/", "--path", "/"], ["get", "WANTED", "--service", "foreign"])
        for args in bad:
            with self.subTest(args=args):
                code, out, err, count = self.run_cli(args)
                self.assertNotEqual(code, 0)
                self.assertEqual((out, count, self.calls), ("", 0, []))
        for key in (core.ENV_TOKEN, core.ENV_WRITE_TOKEN, core.ENV_API, "SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN"):
            with self.subTest(key=key):
                code, out, err, count = self.run_cli(["names"], {key: ""})
                self.assertEqual(code, 4)
                self.assertIn("auth-conflict", err)
                self.assertEqual((out, count, self.calls), ("", 0, []))

    def test_invalid_declarations_refuse_before_keychain_and_provider(self):
        original = copy.deepcopy(self.declaration)
        for field, value in (("schemaVersion", True), ("consumer", "another-consumer"), ("environment", "dev"), ("secretPath", "/wake-router"), ("permissions", ["read", "write"]), ("expiresAt", None), ("expiresAt", "2000-01-01T00:00:00Z"), ("inlineToken", self.token)):
            with self.subTest(field=field):
                self.declaration = {**copy.deepcopy(original), field: value}
                code, out, err, count = self.run_cli(["names"])
                self.assertEqual(code, 4)
                self.assertEqual((out, count, self.calls), ("", 0, []))
        self.declaration = original

    def test_live_identity_or_scope_mismatch_never_reads_values(self):
        original = copy.deepcopy(self.live)
        for field, value in (("id", str(uuid.uuid4())), ("name", "other-reader"), ("workspace", str(uuid.uuid4())), ("permissions", ["read", "write"]), ("expiresAt", None), ("scopes", [{"environment": "prod", "secretPath": "/wake-router"}])):
            with self.subTest(field=field):
                self.calls.clear()
                self.live = {**copy.deepcopy(original), field: value}
                code, out, err, count = self.run_cli(["get", "WANTED"])
                self.assertEqual(code, 4)
                self.assertEqual(out, "")
                self.assertEqual(self.calls, ["/v2/service-token"])

    def test_malformed_duplicate_and_value_bearing_metadata_never_reaches_a_keychain(self):
        for raw in (b"{", b"\xff", b'{"schemaVersion":1,"schemaVersion":1}', b"[" * 1500 + b"]" * 1500):
            with self.subTest(size=len(raw)):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(bootstrap, "read_private_declaration", return_value=raw), mock.patch.object(bootstrap, "bind_keychain_token") as bind, mock.patch.object(core, "_api_json") as api:
                    self.assertEqual(cli.main(["names", "--consumer-binding", str(self.path)], env={}, out=out, err=err), 4)
                bind.assert_not_called()
                api.assert_not_called()
                self.assertEqual(out.getvalue(), "")
                self.assertIn("metadata-refused", err.getvalue())

    def test_bootstrap_value_cannot_escape_through_an_unexpected_provider_value_or_error(self):
        self.path.write_text(json.dumps(self.declaration))
        self.path.chmod(0o600)
        prepared = {core.ENV_TOKEN: self.token, core.ENV_PROJECT: self.project}
        for failure in (False, True):
            with self.subTest(failure=failure):
                out, err = io.StringIO(), io.StringIO()
                result = {"side_effect": core.SecretsError(self.token)} if failure else {"return_value": {"WANTED": self.token}}
                with mock.patch.object(cli, "_consumer_environment", return_value=prepared), mock.patch.object(core, "fetch_project", **result):
                    self.assertEqual(cli.main(["get", "WANTED", "--consumer-binding", str(self.path)], env={}, out=out, err=err), 4)
                self.assertEqual(out.getvalue(), "")
                self.assertNotIn(self.token, err.getvalue())

    def test_selected_doctor_never_truncates_a_bootstrap_value_into_a_reason(self):
        marker = "st." + secrets.token_urlsafe(100)
        prepared = {core.ENV_TOKEN: marker, core.ENV_PROJECT: self.project}
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_consumer_environment", return_value=prepared), mock.patch.object(core, "fetch_project", side_effect=core.SecretsError(marker)), mock.patch.object(core, "find_cli") as probe:
            code = cli.main(["doctor", "--json", "--project", "fleet-ops", "--path", "/", "--consumer-binding", str(self.path)], env={}, out=out, err=err)
        probe.assert_not_called()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue())["failure_reason"], "consumer-binding-operation-refused")
        self.assertNotIn(marker[:30], out.getvalue() + err.getvalue())

    def test_keychain_time_is_subtracted_from_the_same_provider_budget(self):
        prepared = {core.ENV_TOKEN: self.token, core.ENV_PROJECT: self.project}

        def prepare(*args):
            self.assertAlmostEqual(args[2], 100.1)
            return prepared

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "CONSUMER_BINDING_BUDGET", 0.1), mock.patch.object(cli.time, "monotonic", side_effect=[100.0, 100.02]), mock.patch.object(cli, "_consumer_environment", side_effect=prepare), mock.patch.object(core, "list_project_names", return_value=("WANTED",)) as names:
            code = cli.main(["names", "--consumer-binding", str(self.path)], env={}, out=out, err=err)
        self.assertEqual(code, 0, err.getvalue())
        remaining = names.call_args.kwargs["timeout"]
        self.assertAlmostEqual(remaining, 0.08)

    def test_each_invocation_uses_the_current_coherent_declared_account(self):
        for _ in range(2):
            self.calls.clear()
            self.token = "st." + secrets.token_urlsafe(32)
            self.declaration["tokenName"] = "muster-fixture-" + uuid.uuid4().hex
            self.declaration["tokenId"] = str(uuid.uuid4())
            self.declaration["storage"]["account"] = self.declaration["tokenName"]
            self.live.update(id=self.declaration["tokenId"], name=self.declaration["tokenName"])
            code, out, err, count = self.run_cli(["names"])
            self.assertEqual(code, 0, err)
            self.assertEqual(count, 1)
            self.assertEqual(out, "WANTED\n")

    def test_run_child_selector_is_opaque_after_the_separator(self):
        args = ["--", "child-tool", "--consumer-binding", "/child/only.json"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "cmd_run", return_value=0) as run, mock.patch.object(bootstrap, "read_private_declaration") as read, mock.patch.object(core, "_api_json") as api:
            self.assertEqual(cli.main(["run", *args], env={}, out=out, err=err), 0)
        self.assertEqual(run.call_args.args[0], args)
        read.assert_not_called()
        api.assert_not_called()

    def test_selected_doctor_bootstrap_refusals_return_safe_nonzero_json(self):
        marker = "st." + secrets.token_urlsafe(64)
        for reason in ("consumer-binding-missing", "bootstrap-missing", "bootstrap-keychain-locked", marker):
            with self.subTest(reason=reason):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(cli, "_consumer_environment", side_effect=core.SecretsError(reason)):
                    code = cli.main(["doctor", "--json", "--project", "fleet-ops", "--path", "/", "--consumer-binding", str(self.path)], env={}, out=out, err=err)
                self.assertEqual(code, 4)
                report = json.loads(out.getvalue())
                self.assertFalse(report["ok"])
                self.assertEqual(report["failure_reason"], reason if reason != marker else "consumer-binding-operation-refused")
                self.assertEqual(report["requested_project"], "fleet-ops")
                self.assertEqual(report["requested_path"], "/")
                self.assertNotIn(marker, out.getvalue() + err.getvalue())


if __name__ == "__main__":
    unittest.main()
