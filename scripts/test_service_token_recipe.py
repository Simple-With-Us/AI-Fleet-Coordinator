#!/usr/bin/env python3
"""Exercise the canonical registration recipe against an isolated local Sheets stub.

These fixtures never import the real Fleet-OPS client or inherit credentials.  Node
runs with a temporary HOME, an empty command PATH, synthetic bindings, and fetch
disabled.  All apparent API operations are JSON-file reads and writes in the fixture.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "docs/fleet-skills/service-tokens/SKILL.md"
NODE = shutil.which("node")
HEADERS = [
    "Token",
    "Token ID",
    "Project",
    "Project ID",
    "Environment",
    "Folders",
    "Permission",
    "Purpose",
    "Owner",
    "Runtime consumer",
    "Status",
    "Expires (UTC)",
    "Metadata verified (UTC)",
    "Evidence",
    "Audit coverage",
    "Secure storage reference",
    "Screenshot comparison 2026-11-03",
    "Discrepancy / evidence limits",
    "Created By",
]
METADATA = {
    "id": "fixture-token-id",
    "name": "fixture-consumer-ro",
    "project": "Fixture Project",
    "projectId": "fixture-project-id",
    "scopes": [{"environment": "prod", "folder": "/fixture", "permissions": ["read"]}],
    "purpose": "Isolated registration fixture",
    "owner": "Fixture Owner",
    "consumer": "fixture-consumer",
    "status": "active",
    "createdAt": "2026-11-01T00:00:00Z",
    "expiresAt": "2027-01-01T00:00:00Z",
    "verifiedAt": "2026-11-02T00:00:00Z",
    "evidence": "Synthetic positive read",
    "secureStorageReference": "Fixture store reference",
}

STUB = r"""
import { readFile, writeFile, readdir } from 'node:fs/promises';
import { join } from 'node:path';

const statePath = process.env.FIXTURE_STATE;
const save = state => writeFile(statePath, JSON.stringify(state));
const privateError = () => new Error('fixture-provider-private-body-do-not-print');
export async function api(path, init = {}) {
  const state = JSON.parse(await readFile(statePath, 'utf8'));
  const method = init.method || 'GET';
  state.calls.push({ method, path });
  await save(state);
  if (!path.startsWith('/values/')) throw new Error('unexpected fixture path');
  if (method === 'POST') {
    if (!path.includes(':append?valueInputOption=RAW&insertDataOption=INSERT_ROWS')) {
      throw new Error('unexpected fixture mutation');
    }
    const markerDir = join(process.env.HOME, '.config/infisical/registry-attempts');
    const markers = (await readdir(markerDir)).filter(name => name.endsWith('.json'));
    if (markers.length !== 1) throw new Error('missing pre-mutation attempt marker');
    const marker = JSON.parse(await readFile(join(markerDir, markers[0]), 'utf8'));
    if (marker.outcome !== 'unknown') throw new Error('mutation without unknown marker');
    state.appendCount += 1;
    if (state.mode === 'throw_without_commit') {
      await save(state);
      throw privateError();
    }
    const body = JSON.parse(init.body);
    if (body.majorDimension !== 'ROWS' || body.values.length !== 1) {
      throw new Error('unexpected fixture body');
    }
    state.rows.push(body.values[0]);
    await save(state);
    if (state.mode === 'commit_then_throw') throw privateError();
    const row = state.rows.length + 1;
    return { updates: { updatedRows: 1, updatedRange: `'Infisical Service Tokens'!A${row}:S${row}` } };
  }
  if (method !== 'GET') throw new Error('unexpected fixture method');
  if (state.mode === 'read_failure') throw privateError();
  const range = decodeURIComponent(path.slice('/values/'.length).split('?')[0]);
  if (range === 'Infisical Service Tokens') return { values: [state.headers, ...state.rows] };
  const match = /^'Infisical Service Tokens'!A([0-9]+):S\1$/.exec(range);
  if (!match) throw new Error('unexpected fixture read range');
  const row = [...state.rows[Number(match[1]) - 2]];
  if (state.mode === 'readback_mismatch') row[0] = 'different fixture row';
  if (state.mode === 'trim_readback') {
    while (row[row.length - 1] === '') row.pop();
  }
  return { values: [row] };
}
"""


def recipe_source() -> str:
    text = SKILL.read_text(encoding="utf-8")
    start = "node --input-type=module - \"$metadata_file\" <<'JS'\n"
    if text.count(start) != 1:
        raise AssertionError("canonical registration heredoc must occur exactly once")
    tail = text.split(start, 1)[1]
    if "\nJS\n" not in tail:
        raise AssertionError("canonical registration heredoc is not terminated")
    # This guard is deliberately outside the recipe, and never makes a request.
    return (
        "globalThis.fetch = () => { throw new Error('fixture network forbidden'); };\n"
        + tail.split("\nJS\n", 1)[0]
    )


def expected_row(metadata: dict) -> list[str]:
    """Independent fixture expectation for the existing 19-column sheet contract."""
    scope = metadata["scopes"][0]
    permission = "/".join(value for value in ("read", "write") if value in scope["permissions"])
    return [
        metadata["name"],
        metadata["id"],
        metadata["project"],
        metadata["projectId"],
        scope["environment"],
        scope["folder"],
        permission,
        metadata["purpose"],
        metadata["owner"],
        metadata["consumer"],
        metadata["status"],
        metadata["expiresAt"],
        metadata["verifiedAt"],
        metadata["evidence"],
        metadata.get("auditCoverage", ""),
        metadata["secureStorageReference"],
        metadata.get("screenshotComparison", ""),
        metadata.get("discrepancyEvidenceLimits", ""),
        metadata.get("createdBy", ""),
    ]


class Fixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="service-token-recipe-")
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.private = self.home / ".config/infisical"
        self.private.mkdir(parents=True, mode=0o700)
        self.private.chmod(0o700)
        self.empty_bin = self.root / "empty-bin"
        self.empty_bin.mkdir()
        self.ops = self.root / "fixture-ops"
        self.client = self.ops / "scripts/fleet-sheet/sa.mjs"
        self.client.parent.mkdir(parents=True)
        self.client.write_text(STUB, encoding="utf-8")
        self.metadata = copy.deepcopy(METADATA)
        self.metadata_path = self.private / "metadata.json"
        self.write_metadata()
        self.state_path = self.root / "sheet-state.json"
        self.write_state(
            {"headers": HEADERS, "rows": [], "appendCount": 0, "calls": [], "mode": "normal"}
        )
        self.source = recipe_source()

    def close(self) -> None:
        self.temp.cleanup()

    @property
    def marker(self) -> Path:
        digest = hashlib.sha256(self.metadata["id"].encode()).hexdigest()
        return self.private / "registry-attempts" / (digest + ".json")

    @property
    def lock(self) -> Path:
        return self.marker.with_suffix(".lock")

    def write_metadata(self, raw: str | None = None) -> None:
        self.metadata_path.write_text(
            raw if raw is not None else json.dumps(self.metadata), encoding="utf-8"
        )
        self.metadata_path.chmod(0o600)

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def write_state(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def change_state(self, **changes: object) -> None:
        state = self.state()
        state.update(changes)
        self.write_state(state)

    def run(self, *, omit: str | None = None) -> subprocess.CompletedProcess[str]:
        # Do not copy os.environ: no real auth, preload, proxy or CLI setting reaches Node.
        env = {
            "HOME": str(self.home),
            "PATH": str(self.empty_bin),
            "FLEET_OPS_ROOT": str(self.ops),
            "INFISICAL_TOKEN": "fixture",
            "INFISICAL_FLEET_OPS_PROJECT_ID": "fixture-sheet-project",
            "FIXTURE_STATE": str(self.state_path),
        }
        if omit:
            del env[omit]
        return subprocess.run(
            [NODE, "--input-type=module", "-", str(self.metadata_path)],
            input=self.source,
            text=True,
            capture_output=True,
            env=env,
            cwd=self.root,
            timeout=15,
            check=False,
        )


@unittest.skipUnless(NODE, "requires node for the isolated canonical recipe fixture")
class ServiceTokenRecipeTests(unittest.TestCase):
    def fixture(self) -> Fixture:
        fixture = Fixture()
        self.addCleanup(fixture.close)
        return fixture

    def assert_no_mutation(self, fixture: Fixture) -> None:
        self.assertEqual(fixture.state()["appendCount"], 0)
        self.assertFalse(any(call["method"] != "GET" for call in fixture.state()["calls"]))
        self.assertFalse(fixture.marker.exists())

    def test_append_exact_readback_and_rerun_adds_only_one_row(self) -> None:
        fixture = self.fixture()
        first = fixture.run()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("appended and read back exactly", first.stdout)
        self.assertEqual(fixture.state()["rows"], [expected_row(fixture.metadata)])
        self.assertEqual(json.loads(fixture.marker.read_text())["outcome"], "ok")
        self.assertEqual(fixture.marker.stat().st_mode & 0o777, 0o600)
        self.assertFalse(fixture.lock.exists())
        second = fixture.run()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already registered", second.stdout)
        self.assertEqual(fixture.state()["appendCount"], 1)
        self.assertEqual(len(fixture.state()["rows"]), 1)
        self.assertEqual(
            json.loads(fixture.metadata_path.read_text())["createdAt"], METADATA["createdAt"]
        )

    def test_committed_but_thrown_append_is_unknown_and_never_replayed(self) -> None:
        fixture = self.fixture()
        fixture.change_state(mode="commit_then_throw")
        first = fixture.run()
        self.assertEqual(first.returncode, 3)
        self.assertIn("outcome_unknown phase=mutation-started", first.stderr)
        self.assertNotIn("fixture-provider-private-body", first.stdout + first.stderr)
        self.assertEqual(json.loads(fixture.marker.read_text())["outcome"], "unknown")
        committed_rows = fixture.state()["rows"]
        fixture.change_state(mode="normal", rows=[])
        invisible = fixture.run()
        self.assertEqual(invisible.returncode, 3)
        self.assertIn("reconcile without appending", invisible.stdout)
        self.assertEqual(fixture.state()["appendCount"], 1)
        fixture.change_state(rows=committed_rows)
        reconciled = fixture.run()
        self.assertEqual(reconciled.returncode, 0, reconciled.stderr)
        self.assertEqual(fixture.state()["appendCount"], 1)
        self.assertEqual(json.loads(fixture.marker.read_text())["outcome"], "ok")

    def test_sheets_trailing_empty_cells_are_normalized_during_readback(self) -> None:
        fixture = self.fixture()
        fixture.change_state(mode="trim_readback")
        result = fixture.run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(fixture.marker.read_text())["outcome"], "ok")
        self.assertEqual(fixture.state()["rows"], [expected_row(fixture.metadata)])

    def test_thrown_append_without_visible_commit_also_blocks_replay(self) -> None:
        fixture = self.fixture()
        fixture.change_state(mode="throw_without_commit")
        self.assertEqual(fixture.run().returncode, 3)
        fixture.change_state(mode="normal")
        self.assertEqual(fixture.run().returncode, 3)
        self.assertEqual(fixture.state()["rows"], [])
        self.assertEqual(fixture.state()["appendCount"], 1)

    def test_readback_mismatch_remains_unknown_until_exact_reconciliation(self) -> None:
        fixture = self.fixture()
        fixture.change_state(mode="readback_mismatch")
        first = fixture.run()
        self.assertEqual(first.returncode, 3)
        self.assertIn("phase=post-append", first.stderr)
        self.assertEqual(json.loads(fixture.marker.read_text())["outcome"], "unknown")
        fixture.change_state(mode="normal")
        self.assertEqual(fixture.run().returncode, 0)
        self.assertEqual(fixture.state()["appendCount"], 1)

    def test_missing_bindings_name_only_the_missing_source(self) -> None:
        for name in ("FLEET_OPS_ROOT", "INFISICAL_TOKEN", "INFISICAL_FLEET_OPS_PROJECT_ID"):
            with self.subTest(name=name):
                fixture = self.fixture()
                result = fixture.run(omit=name)
                self.assertEqual(result.returncode, 1)
                self.assertIn("missing_configuration", result.stderr)
                self.assertIn("source=" + name, result.stderr)
                self.assertEqual(fixture.state()["calls"], [])
                self.assert_no_mutation(fixture)

    def test_missing_client_names_shared_source_without_import_error_details(self) -> None:
        fixture = self.fixture()
        fixture.client.unlink()
        result = fixture.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("source=Fleet-OPS/scripts/fleet-sheet/sa.mjs", result.stderr)
        self.assertNotIn(str(fixture.root), result.stderr)
        self.assert_no_mutation(fixture)

    def test_failed_registry_read_does_not_print_provider_body(self) -> None:
        fixture = self.fixture()
        fixture.change_state(mode="read_failure")
        result = fixture.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("source=Infisical Service Tokens/read", result.stderr)
        self.assertNotIn("fixture-provider-private-body", result.stdout + result.stderr)
        self.assert_no_mutation(fixture)

    def test_exact_byte_limit_is_accepted_and_sentinel_byte_is_rejected(self) -> None:
        for byte_count, expected_status in ((64 * 1024, 0), (64 * 1024 + 1, 1)):
            with self.subTest(byte_count=byte_count):
                fixture = self.fixture()
                raw = json.dumps(fixture.metadata)
                fixture.write_metadata(raw + " " * (byte_count - len(raw.encode())))
                result = fixture.run()
                self.assertEqual(result.returncode, expected_status, result.stderr)
                if expected_status:
                    self.assertIn("metadata_too_large", result.stderr)
                    self.assertEqual(fixture.state()["calls"], [])
                    self.assert_no_mutation(fixture)

    def test_depth_and_node_limits_refuse_before_client_import(self) -> None:
        deep: object = None
        for _ in range(18):
            deep = [deep]
        for raw in (json.dumps({"nested": deep}), json.dumps([None] * 1001)):
            with self.subTest(kind="depth" if "nested" in raw else "nodes"):
                fixture = self.fixture()
                fixture.write_metadata(raw)
                result = fixture.run()
                self.assertEqual(result.returncode, 1)
                self.assertIn("metadata_complexity", result.stderr)
                self.assertEqual(fixture.state()["calls"], [])
                self.assert_no_mutation(fixture)

    def test_provider_secret_forms_report_column_without_echoing_value(self) -> None:
        # Explicitly synthetic credential-shaped strings, never live credentials.
        examples = [
            "ghp_" + "fixture" * 6,
            "github_pat_" + "fixture" * 6,
            "sk-proj-" + "fixture" * 6,
            "sk-ant-" + "fixture" * 6,
            "sk_live_" + "fixture" * 6,
            "xoxb-" + "fixture-" * 6,
            ".".join(("st", "fixture", "fixture")),
            '"private_key": "fixture"',
        ]
        for value in examples:
            with self.subTest(provider=value.split("fixture", 1)[0]):
                fixture = self.fixture()
                fixture.metadata["purpose"] = value
                fixture.write_metadata()
                result = fixture.run()
                self.assertEqual(result.returncode, 1)
                self.assertIn("secret_like_metadata", result.stderr)
                self.assertIn("source=Purpose", result.stderr)
                self.assertNotIn(value, result.stdout + result.stderr)
                self.assert_no_mutation(fixture)

    def test_only_one_prod_scope_and_unique_read_write_permissions_are_accepted(self) -> None:
        invalid_scopes = [
            [],
            [METADATA["scopes"][0], METADATA["scopes"][0]],
            [{"environment": "dev", "folder": "/fixture", "permissions": ["read"]}],
            [{"environment": "prod", "folder": "/a, /b", "permissions": ["read"]}],
        ]
        invalid_scopes.extend(
            [{"environment": "prod", "folder": "/fixture", "permissions": permissions}]
            for permissions in ([], ["admin"], ["read", "read"], ["read-write"])
        )
        for scopes in invalid_scopes:
            with self.subTest(scopes=scopes):
                fixture = self.fixture()
                fixture.metadata["scopes"] = copy.deepcopy(scopes)
                fixture.write_metadata()
                result = fixture.run()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(fixture.state()["calls"], [])
                self.assert_no_mutation(fixture)

    def test_accepted_permissions_use_mirror_compatible_labels(self) -> None:
        for permissions, cell in (
            (["read"], "read"),
            (["write"], "write"),
            (["write", "read"], "read/write"),
        ):
            with self.subTest(permissions=permissions):
                fixture = self.fixture()
                fixture.metadata["scopes"][0]["permissions"] = permissions
                fixture.write_metadata()
                result = fixture.run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(fixture.state()["rows"][0][HEADERS.index("Permission")], cell)
                self.assertEqual(fixture.state()["rows"][0][HEADERS.index("Folders")], "/fixture")

    def test_timezone_offsets_are_accepted_for_all_metadata_timestamps(self) -> None:
        for suffix in ("+00:00", "+05:30", "-05:00"):
            with self.subTest(suffix=suffix):
                fixture = self.fixture()
                for field in ("createdAt", "expiresAt", "verifiedAt"):
                    fixture.metadata[field] = fixture.metadata[field].removesuffix("Z") + suffix
                fixture.write_metadata()
                result = fixture.run()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(fixture.state()["rows"], [expected_row(fixture.metadata)])

    def test_invalid_or_absent_timezone_remains_a_timestamp_refusal(self) -> None:
        for suffix in ("", "+99:00"):
            with self.subTest(suffix=suffix):
                fixture = self.fixture()
                fixture.metadata["verifiedAt"] = "2026-11-02T00:00:00" + suffix
                fixture.write_metadata()
                result = fixture.run()
                self.assertEqual(result.returncode, 1)
                self.assertIn("invalid_timestamp", result.stderr)
                self.assertEqual(fixture.state()["calls"], [])
                self.assert_no_mutation(fixture)

    def test_stable_content_collision_never_appends_a_different_id(self) -> None:
        fixture = self.fixture()
        other = copy.deepcopy(fixture.metadata)
        other["id"] = "different-fixture-token-id"
        fixture.change_state(rows=[expected_row(other)])
        result = fixture.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("content_match_requires_reconciliation", result.stderr)
        self.assert_no_mutation(fixture)

    def test_another_local_lock_is_never_removed_or_bypassed(self) -> None:
        fixture = self.fixture()
        fixture.lock.parent.mkdir(mode=0o700)
        fixture.lock.mkdir(mode=0o700)
        result = fixture.run()
        self.assertEqual(result.returncode, 1)
        self.assertIn("local_lock_unavailable", result.stderr)
        self.assertTrue(fixture.lock.is_dir())
        self.assertEqual(fixture.state()["calls"], [])
        self.assert_no_mutation(fixture)


if __name__ == "__main__":
    unittest.main()
