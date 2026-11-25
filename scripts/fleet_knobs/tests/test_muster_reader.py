"""Muster status exercises declared bindings without a real Keychain or provider."""

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fleet_knobs import knobs, muster_reader, source
from fleet_knobs.__main__ import main
from fleet_knobs.registry import REGISTRY


class FakeReaderError(Exception):
    def __init__(self, message):
        self.message = message


class MusterStatusTests(unittest.TestCase):
    def test_cli_isolated_folder_fetch_and_secret_redaction(self):
        calls = []
        secret = next(spec for spec in REGISTRY if spec.secret and spec.folder == "/")
        value = "fixture-only-value-never-in-the-output" * 2

        def fetch(names, *, project_id, path):
            self.assertEqual(project_id, source.PROJECT_ID)
            calls.append(path)
            if path == "/wake-router":
                raise source.FetchFailed("muster-binding-store-unavailable")
            return {secret.name: value} if path == "/" else {}

        with (
            tempfile.TemporaryDirectory() as home,
            patch.dict("os.environ", {"HOME": home}, clear=True),
        ):
            out = StringIO()
            with (
                patch.object(muster_reader, "fetch_named", fetch),
                patch.object(source, "fetch_named") as ordinary,
                patch("fleet_knobs.setter.read_token") as writer,
            ):
                self.assertEqual(main(["status", "--json", "--values", "--muster"], out), 3)
                ordinary.assert_not_called()
                writer.assert_not_called()
            data = json.loads(out.getvalue())
            self.assertCountEqual(calls, ["/", "/code-mirrors", "/wake-router"])
            self.assertEqual(data["folders"]["/"]["fetch"], "ok")
            self.assertEqual(data["folders"]["/wake-router"]["fetch"], "failed")
            self.assertEqual(data["write_token"], "missing")
            row = next(r for r in data["knobs"] if r["name"] == secret.name)
            self.assertNotIn("value", row)
            self.assertNotIn(value, out.getvalue())
            self.assertFalse(
                Path(home, ".local/state/fleet-knobs/fetch-failed-wake-router.json").exists()
            )
            self.assertEqual(knobs.folder_status("/")["fetch"], "not-attempted")

    def test_exact_existing_account_sent_to_strict_bounded_reader(self):
        binding = Mock(
            return_value=(
                source.PROJECT_ID,
                "opaque-fixture",
                {"INFISICAL_ENV": "prod"},
                {"id": "fixture"},
            )
        )
        read = Mock(return_value={"FIXTURE": "42"})
        fi = SimpleNamespace(
            home_dir=lambda _: Path("/fixture"),
            bounded_fetch_project=read,
            SecretsError=ValueError,
            UsageError=TypeError,
        )
        configured = {
            "ACCOUNTS": {(muster_reader._PROJECT, "/wake-router"): "muster-afc-wake-router-ro"},
            "_declared_context": binding,
            "FI": fi,
            "ReaderError": FakeReaderError,
        }
        with patch.object(muster_reader, "_reader", return_value=configured):
            self.assertEqual(
                muster_reader.fetch_named(
                    ["FIXTURE"], project_id=source.PROJECT_ID, path="/wake-router"
                ),
                {"FIXTURE": "42"},
            )
            with self.assertRaisesRegex(source.FetchFailed, "muster-binding-refused"):
                muster_reader.fetch_named(
                    ["FIXTURE"], project_id=source.PROJECT_ID, path="/unapproved"
                )
        args, folder = binding.call_args.args
        self.assertEqual(folder, "/wake-router")
        self.assertEqual(args.consumer, "muster-data-reader")
        self.assertEqual(args.consumer_account, "muster-afc-wake-router-ro")
        self.assertTrue(
            args.consumer_binding.endswith(
                "/.config/infisical/muster/muster-afc-wake-router-ro.json"
            )
        )
        self.assertTrue(read.call_args.kwargs["strict_records"])
        self.assertTrue(read.call_args.kwargs["require_finite_expiry"])
        self.assertEqual(read.call_args.kwargs["expected_identity"], {"id": "fixture"})
        read.assert_called_once()


if __name__ == "__main__":
    unittest.main()
