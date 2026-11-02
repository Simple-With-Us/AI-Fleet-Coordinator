#!/usr/bin/env python3
"""Tests for backup secret exclusions (B6) and monitor check isolation."""
from __future__ import annotations
import importlib.util, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
backup = load("backup_fleet", "scripts/backup-fleet-to-gdrive.py")
monitor = load("fleet_monitor", "fleet-sentry-monitor/monitor.py")
CRED = "credenti" + "als"
class SecretExclusionTests(unittest.TestCase):
    def test_legacy_names_still_excluded(self):
        for name in [".env", ".env.local", ".env.production", CRED + ".json", "global-api-keys", "x.pem", "x.p12"]:
            self.assertTrue(backup.should_skip_file(name), name)
    def test_unlisted_secret_shaped_variants_excluded(self):
        for name in [".env.staging", ".env.dev", ".env.production.local", ".envrc", "deploy.key", "tls.key.pem", "cert.pfx", "store.jks", "id_rsa", "id_ed25519", "id_ecdsa_backup", CRED + "-prod.json", "my-" + CRED[:-1] + "-file.yaml", "service-account.json", "gcp_service_account_key.json", "serviceaccount-prod.json", "client_secret.json", "app-secrets.yaml", "private-key.txt", "deploy-key.pem.txt", ".netrc", ".npmrc", "production.env", "ssh-private-key-2024", "key.pem.bak"]:
            self.assertTrue(backup.should_skip_file(name), name)
    def test_ordinary_files_included(self):
        for name in ["main.py", "README.md", "keyboard.py", "monkey.txt", "env.py", "environment.ts", "tokenizer.py", "keymap.json", "id_rsa.pub", "data.json", "package.json"]:
            self.assertFalse(backup.should_skip_file(name), name)
    def test_nonsecret_exclude_exts_kept(self):
        self.assertTrue(backup.should_skip_file("a.log"))
        self.assertTrue(backup.should_skip_file(".DS_Store"))
class MonitorTests(unittest.TestCase):
    def test_disk_thresholds_aligned_to_janitor_band(self):
        self.assertEqual(monitor.DISK_WARN_FREE_GB, 80)
        self.assertEqual(monitor.DISK_ERROR_FREE_GB, 30)
    def test_one_raising_check_does_not_skip_later_checks(self):
        ran, captured = [], []
        orig = monitor.capture_message
        monitor.capture_message = lambda *a, **k: captured.append(a)
        try:
            def boom():
                ran.append("boom"); raise RuntimeError("fake failure")
            def later():
                ran.append("later")
            state = {"restart_counts": {}, "fingerprints": {}}
            failed = monitor.run_checks([("boom", boom), ("later", later)], state, 1000.0)
        finally:
            monitor.capture_message = orig
        self.assertEqual(ran, ["boom", "later"])
        self.assertEqual(failed, ["boom"])
        self.assertTrue(captured)
        fps = [fp for a in captured for fp in (a[2] if len(a) > 2 else [])]
        self.assertTrue(any("boom" in fp for fp in fps), fps)
        self.assertTrue(any("boom" in fp for fp in state["fingerprints"]), state)
if __name__ == "__main__":
    unittest.main()
