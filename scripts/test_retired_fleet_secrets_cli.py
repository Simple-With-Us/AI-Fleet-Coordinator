#!/usr/bin/env python3
"""The retired fleet-secrets CLI stays retired, and every consumer names its own grant.

The CLI was deleted from the fleet.  The five files that used to spawn it read Infisical
through the shared client (scripts/fleet_infisical.py) now, each under its own scoped
read-only service token:  an account named `<consumer>-ro`, held in that consumer's own
store, and granted exactly the folder that consumer reads.  No ambient token stands in
for that grant.

This test pins that contract:

  * none of the consumer files names the retired CLI or the deleted package any more;
  * each reader names the account it reads with, and the secret names it reads, so a
    consumer with no reviewed grant fails here rather than at run time;
  * the CI workflow runs the shared client's own tests, and names neither the deleted
    package nor the deleted installer.

The account names below are the names the owner mints the tokens under, and the folders
are the grants those tokens must carry:  a drift in either is a failure here, not a
surprise on a host.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The consumer files that used to spawn the retired CLI.
CONSUMER_FILES = (
    "scripts/simple_tools/notes_client.py",
    "scripts/sheet-read.mjs",
    "scripts/zulip_linkifiers.py",
    "scripts/alert-ingress/selftest.py",
)

# The retired CLI's binary name, and the deleted package's module name.
RETIRED_BINARY = "fleet-secrets"
RETIRED_MODULE = "fleet_secrets"

# Each reader:  the scoped token accounts it names, the secret names it reads, and the
# Infisical folders those accounts' grants must cover.
READERS = {
    "scripts/simple_tools/notes_client.py": {
        "accounts": ("notes-client-ro", "notes-operator-ro"),
        "names": ("OPERATOR_API_URL",),
        "folders": ("/operator",),
    },
    "scripts/zulip_linkifiers.py": {
        "accounts": (
            "zulip-linkifiers-operator-ro",
            "zulip-linkifiers-zulip-ro",
            "zulip-linkifiers-root-ro",
        ),
        "names": ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY", "ZULIP_OPERATOR_SITE"),
        "folders": ("/zulip_operator", "/zulip"),
    },
    "scripts/alert-ingress/selftest.py": {
        "accounts": ("alert-ingress-selftest-ro",),
        "names": (
            "DATADOG_WEBHOOK_SECRET",
            "BETTERSTACK_WEBHOOK_SECRET",
            "ZULIP_DATADOG_BOT_EMAIL",
            "ZULIP_DATADOG_BOT_API_KEY",
            "ZULIP_BETTERSTACK_BOT_EMAIL",
            "ZULIP_BETTERSTACK_BOT_API_KEY",
        ),
        "folders": ("/zulip",),
    },
    "scripts/sheet-read.mjs": {
        "accounts": ("sheet-read-ro",),
        "names": ("GOOGLE_SHEETS_SA",),
        "folders": ("/google-sheets-sa",),
    },
}

# The notes consumer's principal keys are declared beside it, in the shared wire contract.
NOTES_KEY_NAMES = (
    "OPERATOR_NOTES_WORKER_KEY",
    "OPERATOR_NOTES_SERVER_KEY",
    "OPERATOR_NOTES_RUNNER_KEY",
)
NOTES_WIRE = "scripts/simple_tools/notes_wire.py"

# The workflow paths the deleted package and installer owned.
DELETED_PATHS = (
    "fleet_secrets/tests",
    "install-fleet-secrets.sh",
    "fleet_secrets/seed-system-keychain.sh",
    "fleet_secrets/switch-pm2-jobs.sh",
)
WORKFLOW = ".github/workflows/ci.yml"


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class RetiredCliTests(unittest.TestCase):
    """No consumer spawns the retired CLI any more."""

    def test_no_consumer_file_names_the_retired_cli(self) -> None:
        for relative in CONSUMER_FILES:
            text = read(relative)
            with self.subTest(file=relative):
                self.assertNotIn(RETIRED_BINARY, text)
                self.assertNotIn(RETIRED_MODULE, text)

    def test_no_consumer_file_shells_out_to_a_reader_binary(self) -> None:
        for relative in CONSUMER_FILES:
            text = read(relative)
            # The shared client's own API-origin variable is FLEET_SECRETS_API and stays;
            # the retired CLI's binary path variable does not.
            for marker in (r"\bFLEET_SECRETS\b", r"\bFLEET_SECRETS_BIN\b"):
                with self.subTest(file=relative, marker=marker):
                    self.assertIsNone(re.search(marker, text))


class ScopedReaderTests(unittest.TestCase):
    """Every reader names its own scoped token account and the names it reads."""

    def test_each_reader_names_its_accounts(self) -> None:
        for relative, expected in READERS.items():
            text = read(relative)
            for account in expected["accounts"]:
                with self.subTest(file=relative, account=account):
                    self.assertIn(account, text)
                    self.assertTrue(account.endswith("-ro"), account)

    def test_each_reader_names_the_secrets_it_reads(self) -> None:
        for relative, expected in READERS.items():
            text = read(relative)
            for name in expected["names"]:
                with self.subTest(file=relative, name=name):
                    self.assertIn(name, text)

    def test_each_reader_names_the_folder_it_reads(self) -> None:
        for relative, expected in READERS.items():
            text = read(relative)
            for folder in expected["folders"]:
                with self.subTest(file=relative, folder=folder):
                    self.assertIn(folder, text)

    def test_the_notes_principal_keys_are_declared(self) -> None:
        text = read(NOTES_WIRE)
        for name in NOTES_KEY_NAMES:
            with self.subTest(name=name):
                self.assertIn(name, text)


class WorkflowTests(unittest.TestCase):
    """The workflow tests the shared client and names nothing that is gone."""

    def test_the_workflow_runs_the_shared_client_tests(self) -> None:
        text = read(WORKFLOW)
        self.assertIn("test_fleet_infisical_store", text)

    def test_the_workflow_names_no_deleted_path(self) -> None:
        text = read(WORKFLOW)
        for path in DELETED_PATHS:
            with self.subTest(path=path):
                self.assertNotIn(path, text)


if __name__ == "__main__":
    unittest.main()
