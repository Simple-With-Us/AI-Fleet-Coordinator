#!/usr/bin/env python3
"""The scoped reader Muster runs:  the verbs, the exit codes and the refusals.

The reader is the only command the app starts,  so the tests cover it the way the app
uses it:  the same three verbs, the same exit codes,  and the same rule that a failure
never shows a value.  Nothing here reaches the network or the keychain:  the shared
client's own calls are replaced,  and the reader is asked the questions the app asks.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_infisical as fi

SCRIPTS = Path(__file__).resolve().parent
# A value that must never show up in any output.  It is not a credential:  it is the
# marker a fake read would have returned.
SENTINEL = "VALUE-SHOULD-NEVER-APPEAR"
# Construct a deterministic synthetic ID at test runtime; no project ID is committed.
PROJECT_ID = str(uuid.UUID(int=1, version=4))


def synthetic_token() -> str:
    """A service-token-shaped value, made up and assembled here so no token-shaped literal sits."""
    return ".".join(["st", "synthetic", "notarealvalue", "notarealvalue"])


def _load_reader():
    """Load `infisical-scoped.py`,  whose name is not an importable module name."""
    path = SCRIPTS / "infisical-scoped.py"
    spec = importlib.util.spec_from_file_location("infisical_scoped", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reader = _load_reader()


class ReaderTestCase(unittest.TestCase):
    """The shared client's own calls are replaced,  so no test leaves the Mac."""

    def setUp(self) -> None:
        home_patch = mock.patch.dict("os.environ", {"HOME": "/Users/example"}, clear=False)
        home_patch.start()
        self.addCleanup(home_patch.stop)
        patch = mock.patch.multiple(
            fi,
            token_from_store=mock.Mock(return_value=(synthetic_token(), "ok")),
            infisical_project_id=mock.Mock(return_value=PROJECT_ID),
            fetch_verified=mock.Mock(return_value=SENTINEL),
            list_project_names=mock.Mock(return_value=("A_KEY", "B_KEY")),
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.fetch_verified = fi.fetch_verified
        self.list_project_names = fi.list_project_names

    def run_reader(self, *arguments: str) -> tuple[int, str, str]:
        """Run one verb and return its exit code,  its stdout and its stderr."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = reader.main(["infisical-scoped.py", *arguments])
        return code, out.getvalue(), err.getvalue()


class GetVerbTests(ReaderTestCase):
    def test_declared_selection_requires_both_flags_before_store_access(self) -> None:
        for args in (("--consumer", "fixture-reader"), ("--consumer-binding", "/not-read")):
            code, out, err = self.run_reader("get", "A_KEY", "--folder", "/", *args)
            self.assertEqual((code, out), (2, ""))
            self.assertNotEqual(err, "")
        fi.token_from_store.assert_not_called()

    def test_explicit_empty_account_never_uses_table_account(self) -> None:
        fi.token_from_store.side_effect = lambda account, *_: (
            ("", "no-token") if account == "" else (synthetic_token(), "ok")
        )
        code, out, _err = self.run_reader("get", "A_KEY", "--folder", "/", "--account", "")
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertEqual(fi.token_from_store.call_args.args[0], "")
        self.fetch_verified.assert_not_called()

    def test_hung_store_is_exit_124_without_provider_text(self) -> None:
        fi.token_from_store.side_effect = fi.SecretsError("private-provider-error", 124)
        code, out, err = self.run_reader("get", "A_KEY", "--folder", "/")
        self.assertEqual(code, 124)
        self.assertEqual(out, "")
        self.assertNotIn("private-provider-error", err)
        self.fetch_verified.assert_not_called()

    def test_project_id_error_uses_allowlisted_reason(self) -> None:
        fi.infisical_project_id.side_effect = fi.SecretsError("private-provider-error", 4)
        code, out, err = self.run_reader("get", "A_KEY", "--folder", "/")
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertNotIn("private-provider-error", err)
        self.fetch_verified.assert_not_called()

    def test_a_value_is_the_only_thing_on_stdout(self) -> None:
        code, out, err = self.run_reader("get", "A_KEY", "--folder", "/")
        self.assertEqual(code, 0)
        self.assertEqual(out, SENTINEL + "\n")
        self.assertEqual(err, "")

    def test_an_absent_name_is_exit_3_and_prints_nothing(self) -> None:
        self.fetch_verified.side_effect = fi.SecretsError("key A_KEY is not in Infisical at /", 3)
        code, out, _err = self.run_reader("get", "A_KEY", "--folder", "/")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")

    def test_a_bad_name_is_a_usage_error(self) -> None:
        code, out, err = self.run_reader("get", "not-a-name", "--folder", "/")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertNotEqual(err, "")
        self.fetch_verified.assert_not_called()

    def test_a_bad_folder_is_a_usage_error(self) -> None:
        code, out, _err = self.run_reader("get", "A_KEY", "--folder", "no-slash")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.fetch_verified.assert_not_called()

    def test_a_missing_folder_argument_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_reader("get", "A_KEY")
        self.assertEqual(raised.exception.code, 2)

    def test_a_refusal_never_prints_a_value(self) -> None:
        self.fetch_verified.side_effect = fi.SecretsError("fetch-failed:  status-500", 4)
        code, out, err = self.run_reader("get", "A_KEY", "--folder", "/")
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertNotIn(SENTINEL, out + err)

    def test_a_refused_token_never_prints_a_value(self) -> None:
        fi.token_from_store.return_value = ("", "keychain-locked")
        code, out, err = self.run_reader("get", "A_KEY", "--folder", "/")
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertNotIn(SENTINEL, out + err)
        self.fetch_verified.assert_not_called()

    def test_a_folder_with_no_account_is_refused(self) -> None:
        code, out, err = self.run_reader("get", "A_KEY", "--folder", "/not-a-folder")
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertNotIn(SENTINEL, out + err)
        self.fetch_verified.assert_not_called()

    def test_the_read_declares_exactly_the_folder_the_token_covers(self) -> None:
        self.run_reader("get", "A_KEY", "--folder", "/")
        scoped = self.fetch_verified.call_args.args[2]
        self.assertEqual(scoped[fi.ENV_READ_PATHS], "/")

    def test_the_value_is_never_in_the_arguments(self) -> None:
        self.run_reader("get", "A_KEY", "--folder", "/")
        arguments = self.fetch_verified.call_args.args
        self.assertNotIn(SENTINEL, " ".join(str(part) for part in arguments))


class NamesVerbTests(ReaderTestCase):
    def test_names_lists_names_only(self) -> None:
        code, out, err = self.run_reader("names", "--folder", "/")
        self.assertEqual(code, 0)
        self.assertEqual(out, "A_KEY\nB_KEY\n")
        self.assertEqual(err, "")
        self.assertNotIn(SENTINEL, out)

    def test_a_folder_with_no_name_is_exit_3(self) -> None:
        self.list_project_names.return_value = ()
        code, out, _err = self.run_reader("names", "--folder", "/")
        self.assertEqual(code, 3)
        self.assertEqual(out, "")

    def test_a_refusal_never_prints_a_value(self) -> None:
        self.list_project_names.side_effect = fi.SecretsError("token-scope-mismatch", 4)
        code, out, err = self.run_reader("names", "--folder", "/")
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertNotIn(SENTINEL, out + err)

    def test_a_bad_folder_is_a_usage_error(self) -> None:
        code, out, _err = self.run_reader("names", "--folder", "no-slash")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.list_project_names.assert_not_called()

    def test_the_project_is_named_when_one_is_asked_for(self) -> None:
        self.run_reader("names", "--folder", "/wake-router", "--project", "ai-fleet-coordinator")
        self.assertEqual(self.list_project_names.call_args.args[0], PROJECT_ID)
        self.assertEqual(self.list_project_names.call_args.args[1], "/wake-router")


class DoctorTests(ReaderTestCase):
    def test_the_report_is_one_object(self) -> None:
        code, out, _err = self.run_reader("doctor")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertIsInstance(report, dict)

    def test_the_report_holds_no_value_and_no_token(self) -> None:
        _code, out, _err = self.run_reader("doctor")
        self.assertNotIn(SENTINEL, out)
        self.assertNotIn(synthetic_token(), out)
        report = json.loads(out)
        self.assertEqual(
            set(report),
            {
                "ok",
                "environment_ok",
                "project_id_resolvable",
                "reader_configured",
                "fetch_names_ok",
                "names_count",
                "failure_reason",
            },
        )

    def test_a_working_mac_reports_ok(self) -> None:
        _code, out, _err = self.run_reader("doctor")
        report = json.loads(out)
        self.assertEqual(report["ok"], True)
        self.assertEqual(report["reader_configured"], True)
        self.assertEqual(report["fetch_names_ok"], True)
        self.assertEqual(report["names_count"], 2)
        self.assertEqual(report["failure_reason"], "")

    def test_a_missing_token_is_reported_and_never_prints_the_token(self) -> None:
        fi.token_from_store.return_value = ("", "no-token")
        code, out, _err = self.run_reader("doctor")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["ok"], False)
        self.assertEqual(report["reader_configured"], False)
        self.assertEqual(report["failure_reason"], "no-token")
        self.assertNotIn(synthetic_token(), out)

    def test_hung_store_keeps_the_diagnostic_json_contract(self) -> None:
        fi.token_from_store.side_effect = fi.SecretsError("private-provider-error", 124)
        code, out, err = self.run_reader("doctor")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        report = json.loads(out)
        self.assertEqual(report["failure_reason"], "keychain-timeout")
        self.assertEqual(report["reader_configured"], False)
        self.assertEqual(report["ok"], False)
        self.assertNotIn("private-provider-error", out)


class AccountTableTests(unittest.TestCase):
    """The account is picked by (project, folder):  a token covers one folder only."""

    def test_the_table_is_the_one_the_app_reads_through(self) -> None:
        self.assertEqual(
            reader.ACCOUNTS,
            {
                ("fleet-ops", "/"): "muster-fleetops-ro",
                ("ai-fleet-coordinator", "/"): "muster-afc-ro",
                ("ai-fleet-coordinator", "/code-mirrors"): "muster-afc-code-mirrors-ro",
                ("ai-fleet-coordinator", "/wake-router"): "muster-afc-wake-router-ro",
            },
        )

    def test_every_account_covers_one_folder_only(self) -> None:
        self.assertEqual(len(set(reader.ACCOUNTS.values())), len(reader.ACCOUNTS))

    def test_the_table_maps_every_folder_the_app_uses(self) -> None:
        registry = json.loads(
            (SCRIPTS / "fleet_knobs" / "registry.json").read_text(encoding="utf-8")
        )
        # The shape is checked before the data is used:  a document that is not a
        # dict,  whose knobs are not a list,  or whose entries are not dicts with a
        # string folder,  is a broken registry and never a set of folders.
        self.assertIsInstance(registry, dict)
        knobs = registry.get("knobs")
        self.assertIsInstance(knobs, list)
        for knob in knobs:
            self.assertIsInstance(knob, dict)
            self.assertIsInstance(knob.get("folder"), str)
        folders = {knob["folder"] for knob in knobs}
        self.assertIn("/", folders)
        for folder in folders:
            self.assertIn(("ai-fleet-coordinator", folder), reader.ACCOUNTS, folder)
        # The panel's own first listing is the fleet-ops root.
        self.assertIn(("fleet-ops", "/"), reader.ACCOUNTS)

    def test_a_folder_the_table_does_not_name_is_refused(self) -> None:
        self.assertIsNone(reader.ACCOUNTS.get(("ai-fleet-coordinator", "/not-a-folder")))


if __name__ == "__main__":
    unittest.main()
