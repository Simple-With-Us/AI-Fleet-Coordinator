#!/usr/bin/env python3
"""Unit tests for hetzner_gitops diff logic. No systemd, no host, no network."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from hetzner_gitops.diff import DriftKind, LiveLayout, diff_file, diff_manifest, resolve_under
from hetzner_gitops.manifest import FileKind, FileSpec, ManifestError, load_manifest
from hetzner_gitops.reconcile import EXIT_DRIFT, EXIT_OK, EXIT_REFUSED, main

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "deploy" / "hetzner" / "manifest.toml"

EXCLUDED_IDS = {
    "afc-wake-router.service",
    "ag-wake-bridge.service",
    "zc-wake-bridge.service",
    "operator-sheets-drain.service",
    "operator-sheets-drain.timer",
    "fleet-server-status.service",
    "fleet-server-status.timer",
    "fleet-vm-status.service",
    "fleet-vm-status.timer",
}


def _layout(repo: Path, live: Path) -> LiveLayout:
    return LiveLayout(live_root=live, repo_root=repo)


def _plant(manifest_files, repo: Path, live: Path) -> None:
    for spec in manifest_files:
        dest = live / spec.installed
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes((repo / spec.source).read_bytes())


class ResolveTests(unittest.TestCase):
    def test_parent_segment_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(Exception):
            resolve_under(Path(tmp), "../outside")


class ManifestTests(unittest.TestCase):
    def test_repo_manifest_sources_exist_and_exclusions_stay_out(self):
        manifest = load_manifest(MANIFEST)
        self.assertEqual(manifest.issue, "AFC-369")
        self.assertGreaterEqual(len(manifest.files), 14)
        self.assertGreaterEqual(len(manifest.gaps), 20)
        self.assertTrue(EXCLUDED_IDS <= manifest.excluded_ids)
        self.assertTrue(EXCLUDED_IDS.isdisjoint(manifest.desired_ids))
        desired_sources = {item.source for item in manifest.files}
        self.assertNotIn("scripts/simple_tools/server/afc-wake-router.service", desired_sources)
        self.assertNotIn("scripts/systemd/fleet-server-status.service", desired_sources)
        self.assertNotIn("scripts/whatsapp_bridge/wa-bridge.service.example", desired_sources)
        for item in manifest.files:
            self.assertTrue((REPO_ROOT / item.source).is_file(), item.source)
            if item.script_source:
                self.assertTrue((REPO_ROOT / item.script_source).is_file(), item.script_source)
        for item in manifest.excluded_units:
            self.assertTrue((REPO_ROOT / item.source).is_file(), item.source)
        gap_ids = {gap.id for gap in manifest.gaps}
        self.assertIn("fleet-deploy-guard@.service", gap_ids)
        self.assertIn("wireguard-template", gap_ids)
        self.assertIn("usage-monitor-replica-status.service.d", gap_ids)
        self.assertEqual(manifest.forbids[0].id, "fleet-backups-bak")

    def test_parent_segment_in_installed_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.toml"
            path.write_text(
                "\n".join(
                    [
                        "schema_version = 1",
                        'issue = "AFC-369"',
                        "[[file]]",
                        'id = "bad"',
                        'kind = "unit"',
                        'source = "scripts/host/fleet-housekeep.service"',
                        'installed = "../etc/passwd"',
                        'inventory = "x"',
                    ]
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ManifestError):
                load_manifest(path)


class DiffFileTests(unittest.TestCase):
    def test_missing_live_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            live = root / "live"
            live.mkdir()
            (repo / "units").mkdir(parents=True)
            (repo / "units" / "demo.service").write_text(
                "[Unit]\nDescription=demo\n", encoding="utf-8"
            )
            spec = FileSpec(
                "demo.service",
                FileKind.UNIT,
                "units/demo.service",
                "etc/systemd/system/demo.service",
                "SRV",
            )
            drifts: list = []
            diff_file(spec, _layout(repo, live), drifts)
            self.assertEqual(drifts[0].kind, DriftKind.MISSING_LIVE)

    def test_byte_mismatch_and_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            live = root / "live"
            (repo / "units").mkdir(parents=True)
            source = repo / "units" / "demo.service"
            source.write_text("[Unit]\nDescription=demo\n", encoding="utf-8")
            spec = FileSpec(
                "demo.service",
                FileKind.UNIT,
                "units/demo.service",
                "etc/systemd/system/demo.service",
                "SRV",
            )
            dest = live / spec.installed
            dest.parent.mkdir(parents=True)
            dest.write_text("[Unit]\nDescription=other\n", encoding="utf-8")
            drifts: list = []
            diff_file(spec, _layout(repo, live), drifts)
            self.assertEqual(drifts[0].kind, DriftKind.CONTENT_DRIFT)
            dest.write_bytes(source.read_bytes())
            drifts.clear()
            diff_file(spec, _layout(repo, live), drifts)
            self.assertEqual(drifts, [])

    def test_live_directory_is_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            live = root / "live"
            (repo / "units").mkdir(parents=True)
            (repo / "units" / "demo.service").write_text("x\n", encoding="utf-8")
            spec = FileSpec(
                "demo.service",
                FileKind.UNIT,
                "units/demo.service",
                "etc/systemd/system/demo.service",
                "",
            )
            (live / spec.installed).mkdir(parents=True)
            drifts: list = []
            diff_file(spec, _layout(repo, live), drifts)
            self.assertEqual(drifts[0].kind, DriftKind.LIVE_IS_DIRECTORY)


class RepoTreeTests(unittest.TestCase):
    def test_copied_tree_is_clean_until_a_byte_or_bak_file_changes(self):
        manifest = load_manifest(MANIFEST)
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp)
            _plant(manifest.files, REPO_ROOT, live)
            result = diff_manifest(manifest, _layout(REPO_ROOT, live))
            self.assertTrue(result.ok, result.to_json())
            target = live / "etc/systemd/system/fleet-housekeep.service"
            target.write_bytes(target.read_bytes() + b"\n")
            drifted = diff_manifest(manifest, _layout(REPO_ROOT, live))
            self.assertFalse(drifted.ok)
            self.assertTrue(any(item.kind == DriftKind.CONTENT_DRIFT for item in drifted.drifts))
            target.write_bytes((REPO_ROOT / "scripts/host/fleet-housekeep.service").read_bytes())
            bak = live / "etc/cron.d/fleet-backups.bak-20260904"
            bak.parent.mkdir(parents=True, exist_ok=True)
            bak.write_text("# duplicate\n", encoding="utf-8")
            forbidden = diff_manifest(manifest, _layout(REPO_ROOT, live))
            self.assertTrue(
                any(item.kind == DriftKind.FORBIDDEN_PRESENT for item in forbidden.drifts)
            )
            self.assertTrue(all(item.target not in EXCLUDED_IDS for item in forbidden.drifts))


class CliTests(unittest.TestCase):
    def test_check_does_not_write_and_apply_is_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            live = Path(tmp) / "live"
            live.mkdir()
            check_argv = [
                "--manifest",
                str(MANIFEST),
                "--repo-root",
                str(REPO_ROOT),
                "--live-root",
                str(live),
                "--json",
            ]
            self.assertEqual(_code(check_argv), EXIT_DRIFT)
            self.assertEqual(list(live.iterdir()), [])
            self.assertEqual(_code(["--manifest", str(MANIFEST), "--apply"]), EXIT_REFUSED)
            self.assertEqual(list(live.iterdir()), [])
            bak = live / "etc/cron.d/fleet-backups.bak-old"
            bak.parent.mkdir(parents=True)
            bak.write_text("# duplicate\n", encoding="utf-8")
            self.assertEqual(
                _code(
                    [
                        "--manifest",
                        str(MANIFEST),
                        "--repo-root",
                        str(REPO_ROOT),
                        "--live-root",
                        str(live),
                        "--apply",
                    ]
                ),
                EXIT_OK,
            )
            self.assertTrue(bak.is_file())
            planted = live / "etc/systemd/system/fleet-housekeep.service"
            self.assertTrue(planted.is_file())
            self.assertFalse((live / "usr/local/bin/fleet-housekeep.sh").exists())
            self.assertFalse((live / "usr/local/sbin/fleet-qdrant-snapshot.sh").exists())
            self.assertEqual(planted.stat().st_mode & 0o777, 0o644)
            held = json.loads(_capture(check_argv))
            self.assertFalse(held["ok"])
            self.assertTrue(any(item["kind"] == "forbidden_present" for item in held["drifts"]))
            bak.unlink()
            payload = json.loads(_capture(check_argv))
            self.assertGreater(payload["gapCount"], 0)
            self.assertTrue(payload["ok"])
            gap_ids = {item["id"] for item in payload["gaps"]}
            self.assertTrue(EXCLUDED_IDS.isdisjoint(gap_ids))

    def test_apply_writes_nothing_when_a_source_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            live = root / "live"
            (repo / "units").mkdir(parents=True)
            live.mkdir()
            (repo / "units" / "present.service").write_text("[Unit]\n", encoding="utf-8")
            manifest_path = repo / "manifest.toml"
            manifest_path.write_text(
                "\n".join(
                    [
                        "schema_version = 1",
                        'issue = "AFC-369"',
                        "[[file]]",
                        'id = "present.service"',
                        'kind = "unit"',
                        'source = "units/present.service"',
                        'installed = "etc/systemd/system/present.service"',
                        'inventory = "test"',
                        "[[file]]",
                        'id = "gone.service"',
                        'kind = "unit"',
                        'source = "units/gone.service"',
                        'installed = "etc/systemd/system/gone.service"',
                        'inventory = "test"',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            code = _code(
                [
                    "--manifest",
                    str(manifest_path),
                    "--repo-root",
                    str(repo),
                    "--live-root",
                    str(live),
                    "--apply",
                ]
            )
            self.assertEqual(code, EXIT_DRIFT)
            self.assertEqual(list(live.iterdir()), [])

    def test_reconcile_source_does_not_call_the_host(self):
        text = (REPO_ROOT / "scripts/hetzner_gitops/reconcile.py").read_text(encoding="utf-8")
        for banned in ("systemctl", "daemon-reload", "ssh "):
            self.assertNotIn(banned, text)


def _run(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = main(argv)
    return code, buffer.getvalue()


def _code(argv: list[str]) -> int:
    code, _output = _run(argv)
    return code


def _capture(argv: list[str]) -> str:
    code, output = _run(argv)
    if code != EXIT_OK and "--json" not in argv:
        raise AssertionError(code)
    return output


if __name__ == "__main__":
    unittest.main()
