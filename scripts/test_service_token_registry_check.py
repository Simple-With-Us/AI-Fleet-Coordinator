#!/usr/bin/env python3
"""Exercise the service-token registry check against local fixtures.

The fixtures never reach the sheet, the network or a credential:  the sidecars
are synthetic files in a temporary directory and the registry rows are a
temporary JSON-lines file.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "service_token_registry_check.mjs"
NODE = shutil.which("node")

MAC_SIDECAR = {
    "id": "11111111-1111-4111-8111-111111111111",
    "name": "fixture-mac-ro",
    "project": "Fixture Project",
    "projectId": "fixture-project-id",
    "environment": "prod",
    "folder": "/fixture",
    "scope": "prod:/fixture",
    "expiresAt": "2027-01-01T00:00:00Z",
    "storedAt": "login keychain: infisical-service-token / fixture-mac-ro",
    "mode": "keychain",
}
SERVER_SIDECAR = {
    "id": "22222222-2222-4222-8222-222222222222",
    "name": "fixture-server-ro",
    "projectId": "fixture-project-id",
    "scopes": [{"environment": "prod", "folder": "/fixture", "permissions": ["read"]}],
    "expiresAt": "2027-05-10T22:09:38.511Z",
    "consumer": "the shared fixture consumer on the server",
}
UNREGISTERED_SIDECAR = {
    "id": "33333333-3333-4333-8333-333333333333",
    "name": "fixture-unregistered-ro",
    "projectId": "fixture-project-id",
    "scopes": [{"environment": "prod", "folder": "/fixture", "permissions": ["read"]}],
    "expiresAt": "2027-02-16T06:07:38.735Z",
}


def registry_row(token_id: str, name: str) -> str:
    return json.dumps(
        {
            "Token": name,
            "Token ID": token_id,
            "Project": "Fixture Project",
            "Environment": "prod",
            "Folders": "/fixture",
            "Permission": "read",
            "Status": "active",
            "Expires (UTC)": "2027-01-01T00:00:00Z",
        }
    )


class RegistryCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="mm-registry-check-"))
        self.sidecars = self.tmp / "sidecars"
        self.sidecars.mkdir()
        (self.sidecars / "mac.json").write_text(json.dumps(MAC_SIDECAR), encoding="utf-8")
        (self.sidecars / "server.json").write_text(json.dumps(SERVER_SIDECAR), encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_rows(self, rows: list[str]) -> Path:
        path = self.tmp / "rows.jsonl"
        path.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return path

    def run_check(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [NODE, str(SCRIPT), *args],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def test_every_installed_token_has_a_row(self) -> None:
        rows = self.write_rows(
            [
                registry_row(MAC_SIDECAR["id"], MAC_SIDECAR["name"]),
                registry_row(SERVER_SIDECAR["id"], SERVER_SIDECAR["name"]),
            ]
        )
        result = self.run_check("--sidecar-dir", str(self.sidecars), "--sheet-rows", str(rows))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("every installed service token has a registry row", result.stdout)

    def test_a_token_in_use_without_a_row_is_reported(self) -> None:
        (self.sidecars / "unregistered.json").write_text(
            json.dumps(UNREGISTERED_SIDECAR), encoding="utf-8"
        )
        rows = self.write_rows(
            [
                registry_row(MAC_SIDECAR["id"], MAC_SIDECAR["name"]),
                registry_row(SERVER_SIDECAR["id"], SERVER_SIDECAR["name"]),
            ]
        )
        result = self.run_check("--sidecar-dir", str(self.sidecars), "--sheet-rows", str(rows))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("MISSING ROW", result.stdout)
        self.assertIn(UNREGISTERED_SIDECAR["id"], result.stdout)
        self.assertNotIn("MISSING ROW", result.stderr)

    def test_a_peer_inventory_is_checked_too(self) -> None:
        peer = self.tmp / "peer.json"
        peer.write_text(
            json.dumps(
                {
                    "host": "peer",
                    "tokens": [
                        {
                            "id": UNREGISTERED_SIDECAR["id"],
                            "name": UNREGISTERED_SIDECAR["name"],
                            "file": "peer.json",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        rows = self.write_rows(
            [
                registry_row(MAC_SIDECAR["id"], MAC_SIDECAR["name"]),
                registry_row(SERVER_SIDECAR["id"], SERVER_SIDECAR["name"]),
            ]
        )
        result = self.run_check(
            "--sidecar-dir",
            str(self.sidecars),
            "--sheet-rows",
            str(rows),
            "--inventory",
            str(peer),
        )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(UNREGISTERED_SIDECAR["id"], result.stdout)

    def test_the_inventory_print_carries_ids_and_names_only(self) -> None:
        result = self.run_check("--sidecar-dir", str(self.sidecars), "--print-inventory")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        ids = sorted(token["id"] for token in payload["tokens"])
        self.assertEqual(ids, sorted([MAC_SIDECAR["id"], SERVER_SIDECAR["id"]]))
        for token in payload["tokens"]:
            self.assertNotIn("value", token)
            self.assertNotIn("secret", token)

    def test_a_token_on_two_hosts_is_not_a_duplicate(self) -> None:
        peer = self.tmp / "peer.json"
        peer.write_text(
            json.dumps(
                {
                    "host": "peer",
                    "tokens": [
                        {"id": MAC_SIDECAR["id"], "name": MAC_SIDECAR["name"], "file": "mac.json"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        rows = self.write_rows(
            [
                registry_row(MAC_SIDECAR["id"], MAC_SIDECAR["name"]),
                registry_row(SERVER_SIDECAR["id"], SERVER_SIDECAR["name"]),
            ]
        )
        result = self.run_check(
            "--sidecar-dir",
            str(self.sidecars),
            "--sheet-rows",
            str(rows),
            "--inventory",
            str(peer),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("DUPLICATE SIDECAR", result.stdout)

    def test_two_sidecars_on_one_host_naming_one_token_are_reported(self) -> None:
        (self.sidecars / "staging.json").write_text(json.dumps(MAC_SIDECAR), encoding="utf-8")
        rows = self.write_rows(
            [
                registry_row(MAC_SIDECAR["id"], MAC_SIDECAR["name"]),
                registry_row(SERVER_SIDECAR["id"], SERVER_SIDECAR["name"]),
            ]
        )
        result = self.run_check("--sidecar-dir", str(self.sidecars), "--sheet-rows", str(rows))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("DUPLICATE SIDECAR", result.stdout)
        self.assertIn(MAC_SIDECAR["id"], result.stdout)

    def test_a_read_failure_concludes_nothing(self) -> None:
        result = self.run_check("--sidecar-dir", str(self.sidecars))
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("needs --sheet-rows or --fleet-ops-root", result.stderr)

    def test_a_sidecar_without_a_token_id_is_not_counted(self) -> None:
        (self.sidecars / "no-id.json").write_text(
            json.dumps({"name": "fixture-without-an-id"}), encoding="utf-8"
        )
        rows = self.write_rows(
            [
                registry_row(MAC_SIDECAR["id"], MAC_SIDECAR["name"]),
                registry_row(SERVER_SIDECAR["id"], SERVER_SIDECAR["name"]),
            ]
        )
        result = self.run_check("--sidecar-dir", str(self.sidecars), "--sheet-rows", str(rows))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("installed service tokens: 2", result.stdout)


if __name__ == "__main__":
    unittest.main()
