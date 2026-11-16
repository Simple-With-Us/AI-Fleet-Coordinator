"""The harness itself:  the temp-file leak check only sees this world's own files."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from .world import World


class WorldTests(unittest.TestCase):
    def setUp(self) -> None:
        self.real_tempdir = tempfile.gettempdir()  # captured before the world points tempfile at its own directory
        self.w = World(self)

    def test_leak_check_ignores_the_system_temp_dir_but_sees_its_own(self) -> None:
        fd, stray = tempfile.mkstemp(prefix=".needs-owner-", dir=self.real_tempdir)  # another run's file, in the shared directory
        os.close(fd)
        self.addCleanup(Path(stray).unlink, missing_ok=True)
        self.assertNotIn(Path(stray).name, self.w.leftover_temp_files())
        fd, mine = tempfile.mkstemp(prefix=".needs-owner-")  # inside the world, as the CLI creates it in process
        os.close(fd)
        self.addCleanup(Path(mine).unlink, missing_ok=True)
        self.assertIn(Path(mine).name, self.w.leftover_temp_files())
        self.assertNotIn(Path(stray).name, self.w.leftover_temp_files())


if __name__ == "__main__":
    unittest.main()
