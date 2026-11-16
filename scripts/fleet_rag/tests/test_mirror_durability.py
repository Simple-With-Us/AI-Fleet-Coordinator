"""Regression tests for the AFC-697 durability fixes in mirror.py."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_rag import mirror


def _tree(root: Path, marker: str) -> None:
    (root / "tree").mkdir(parents=True, exist_ok=True)
    (root / "tree" / f"{marker}.txt").write_text(marker, encoding="utf-8")


class WriteStampTests(unittest.TestCase):
    def test_stamp_contents_are_fsynced_before_the_rename(self) -> None:
        events = []
        real_replace = os.replace

        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            with (
                mock.patch.object(mirror.os, "fsync", lambda fd: events.append(("fsync", fd))),
                mock.patch.object(
                    mirror.os,
                    "replace",
                    lambda a, b: (events.append(("replace", (str(a), str(b)))), real_replace(a, b)),
                ),
            ):
                mirror.write_stamp(directory, 1000.0, {"tree": 1})
            kinds = [e[0] for e in events]
            self.assertEqual(kinds[:2], ["fsync", "replace"])  # content fsync precedes rename
            self.assertIn("replace", kinds)
            self.assertTrue((directory / mirror.COMPLETE_NAME).is_file())


class PromoteDurabilityTests(unittest.TestCase):
    def _layout(self, root: Path) -> tuple[Path, Path]:
        live = root / "live"
        incoming = root / "incoming"
        _tree(live, "old")
        _tree(incoming, "new")
        return live, incoming

    def test_fsync_failure_after_the_swap_rolls_everything_back(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            live, incoming = self._layout(Path(d))
            calls = []

            def flaky(directory: Path) -> None:
                calls.append(directory)
                if len(calls) == 2:  # write_stamp's fsync passes; the post-swap one fails
                    raise OSError("simulated fsync failure")

            with mock.patch.object(mirror, "_fsync_dir", side_effect=flaky):
                with self.assertRaises(OSError):
                    mirror.promote(incoming, live, {"tree": 1}, 1000.0)
            # live is exactly as it was; incoming is restored; no .previous is left behind
            self.assertEqual((live / "tree" / "old.txt").read_text(encoding="utf-8"), "old")
            self.assertEqual((incoming / "tree" / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertFalse((live.parent / "live.previous").exists())

    def test_successful_promote_still_swaps(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            live, incoming = self._layout(Path(d))
            mirror.promote(incoming, live, {"tree": 1}, 1000.0)
            self.assertEqual((live / "tree" / "new.txt").read_text(encoding="utf-8"), "new")
            self.assertTrue((live / mirror.COMPLETE_NAME).is_file())
            self.assertFalse((live.parent / "live.previous").exists())


if __name__ == "__main__":
    unittest.main()
