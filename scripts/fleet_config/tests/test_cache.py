"""The last-known-good copy:  written atomically,  private,  and trusted only while fresh.

The trust rules are the ones ``fleet_knobs/lkg.py`` uses,  because the same reasoning
applies:  a file another process wrote is only trusted when it is ours,  private,  regular
and young.  Every test here runs in a temporary home,  so the real state directory is
never touched.
"""

from __future__ import annotations

import json
import os
import stat
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fleet_config import cache
from fleet_config.checks import build_result
from fleet_config.types import (
    CheckState,
    CheckType,
    ConfigKind,
    ExpectedState,
    ObservedState,
    ReasonCode,
)

from ._fake import FleetTestCase, make_manifest, make_rule

MOMENT = datetime(2026, 10, 19, 14, 30, 0, tzinfo=timezone.utc)


def _ctx():
    """A context with a fixed clock."""
    from fleet_config.checks import CheckContext

    return CheckContext(home="/tmp/fleet-config-home", now=MOMENT)


def _report(manifest, ctx):
    """A report built by hand,  so the test does not depend on a check module."""
    from fleet_config import contract
    from fleet_config.types import StatusReport

    results = []
    for seat in manifest.seats:
        for rule in seat.rules:
            results.append(
                build_result(
                    rule,
                    rule.platforms[0],
                    CheckState.PASS,
                    "the fake check says so",
                    ReasonCode.OK,
                    ObservedState(present=True),
                    ctx,
                )
            )
    frozen = tuple(results)
    return StatusReport(
        schema_version=contract.SCHEMA_VERSION,
        generated_at="2026-10-19T14:30:00Z",
        results=frozen,
        summary=contract.summarize(frozen),
        manifest_source="<fake manifest>",
        notes=("a note",),
    )


def a_report(rules=None):
    """A small report with one result per rule."""
    rules = rules if rules is not None else [make_rule("mm-instructions", ConfigKind.INSTRUCTIONS)]
    return _report(make_manifest(rules), _ctx())


class CacheTests(FleetTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_saved_report_comes_back_unchanged(self):
        report = a_report()
        cache.save_report(report)
        loaded, note = cache.load_report()
        self.assertEqual(note, "ok")
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.results), len(report.results))
        self.assertEqual(loaded.results[0].rule_id, report.results[0].rule_id)
        self.assertIs(loaded.results[0].state, CheckState.PASS)
        self.assertEqual(loaded.notes, report.notes)
        self.assertEqual(loaded.summary.total, report.summary.total)

    def test_the_state_directory_is_private_and_the_file_is_too(self):
        cache.save_report(a_report())
        directory = cache.state_dir()
        info = os.lstat(directory)
        self.assertTrue(stat.S_ISDIR(info.st_mode))
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o700)
        file_info = os.lstat(cache.cache_path())
        self.assertTrue(stat.S_ISREG(file_info.st_mode))
        self.assertEqual(stat.S_IMODE(file_info.st_mode), 0o600)

    def test_the_write_is_atomic_and_leaves_no_temp_file(self):
        cache.save_report(a_report())
        leftovers = [name for name in os.listdir(cache.state_dir()) if name.startswith(".report.")]
        self.assertEqual(leftovers, [])

    def test_a_missing_copy_is_absent_and_not_an_error(self):
        loaded, note = cache.load_report()
        self.assertIsNone(loaded)
        self.assertEqual(note, "absent")

    def test_a_symlinked_copy_is_refused(self):
        cache.save_report(a_report())
        real = cache.cache_path()
        moved = os.path.join(str(self.home), "elsewhere.json")
        os.rename(real, moved)
        os.symlink(moved, real)
        loaded, note = cache.load_report()
        self.assertIsNone(loaded)
        self.assertEqual(note, "unsafe")

    def test_a_readable_by_other_copy_is_refused(self):
        cache.save_report(a_report())
        os.chmod(cache.cache_path(), 0o644)
        loaded, note = cache.load_report()
        self.assertIsNone(loaded)
        self.assertEqual(note, "unsafe")

    def test_an_old_copy_is_refused(self):
        cache.save_report(a_report())
        loaded, note = cache.load_report(now=time_now() + cache.MAX_AGE_SECONDS + 60)
        self.assertIsNone(loaded)
        self.assertEqual(note, "expired")

    def test_a_copy_in_another_schema_is_refused(self):
        cache.save_report(a_report())
        path = cache.cache_path()
        document = json.loads(_read(path).decode("utf-8"))
        document["schema"] = 99
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(document))
        os.chmod(path, 0o600)
        loaded, note = cache.load_report()
        self.assertIsNone(loaded)
        self.assertEqual(note, "wrong-schema")

    def test_a_corrupt_copy_is_refused(self):
        cache.save_report(a_report())
        path = cache.cache_path()
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("this is not json")
        os.chmod(path, 0o600)
        loaded, note = cache.load_report()
        self.assertIsNone(loaded)
        self.assertEqual(note, "unreadable")

    def test_a_fresh_report_is_not_stale_and_an_old_one_is(self):
        report = a_report()
        self.assertFalse(cache.report_is_stale(report, MOMENT))
        self.assertTrue(cache.report_is_stale(report, MOMENT + timedelta(seconds=901)))

    def test_the_cached_bytes_hold_no_secret(self):
        sentinel = "sentinelSecretValue0123456789"
        rule = make_rule(
            "mm-credential",
            ConfigKind.PERMISSIONS,
            check_type=CheckType.CREDENTIAL_STAT,
            expected=ExpectedState(description="the credential file", present=True, mode="0600"),
        )
        report = _report(make_manifest([rule]), _ctx())
        cache.save_report(report)
        raw = _read(cache.cache_path())
        self.assertNotIn(sentinel.encode("utf-8"), raw)
        self.assertNotIn(b"contents", raw)

    def test_the_cache_lives_under_the_state_directory(self):
        self.assertEqual(
            cache.cache_path(),
            os.path.join(str(self.home), ".local", "state", "fleet-config", "report.json"),
        )


def time_now() -> float:
    """The current epoch seconds."""
    import time

    return time.time()


def _read(path: str) -> bytes:
    """The bytes of a file,  read through a context manager."""
    with open(path, "rb") as handle:
        return handle.read()


def _ctx():
    from fleet_config.checks import CheckContext

    return CheckContext(home="/tmp/fleet-config-home", now=MOMENT)


if __name__ == "__main__":
    unittest.main()
