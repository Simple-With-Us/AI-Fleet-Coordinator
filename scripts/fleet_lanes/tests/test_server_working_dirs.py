"""Server working directory tests:  every tool session starts in the lanes root.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_server_working_dirs -v

The script under test writes host files, so every test runs against a throwaway
home, a throwaway lanes root and a throwaway systemd directory.  Nothing here
touches the real home, the real lanes, or /etc.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_lanes import server_working_dirs as swd

TMUX_BOOT = """#!/usr/bin/env bash
if /usr/bin/tmux new-session -d -s "$SESSION" -c /root \\\\
  "$HOME/.local/bin/codex" resume --last; then :; fi
"""

TMUX_AUTOSTART = """#!/usr/bin/env bash
exec tmux new-session -e "AGENT_SESSION=\${AGENT_SESSION-}" codex resume --last
"""

MINIMAX_ACP = '''#!/usr/bin/python3 -S
"""Select MiniMax Full Access through its supported ACP configuration API."""

import json
import signal
import subprocess
import sys
import threading
import uuid


def main():
    child = subprocess.Popen(
        ['/root/.minimax-code/bin/mcode', 'acp', *sys.argv[1:]],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=sys.stderr,
        text=True, bufsize=1,
    )
'''

MUSE_ACP = '''#!/usr/bin/env python3
"""Adapt Muse ACP authentication and missing MCP lists for Shellular."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading


def main():
    child = subprocess.Popen(
        ["/bin/node", "adapter.js", *sys.argv[1:]], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=sys.stderr, text=True, bufsize=1,
    )
'''


class ServerWorkingDirsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.lanes = self.home / "apps" / "lanes"
        self.apps = self.home / "apps"
        self.systemd = self.tmp / "systemd"
        for path in (
            self.lanes,
            self.home / ".local" / "bin",
            self.apps / "muse-acp-runtime",
            self.home / ".config" / "opencode",
            self.systemd,
        ):
            path.mkdir(parents=True, exist_ok=True)
        (self.home / ".local" / "bin" / "codex-tmux-boot").write_text(TMUX_BOOT)
        (self.home / ".local" / "bin" / "codex-tmux-autostart").write_text(TMUX_AUTOSTART)
        (self.apps / "muse-acp-runtime" / "minimax-shellular-acp.py").write_text(MINIMAX_ACP)
        (self.apps / "muse-acp-runtime" / "muse-shellular-acp.py").write_text(MUSE_ACP)
        (self.home / ".bashrc").write_text("# base\n")
        (self.home / ".config" / "opencode" / "opencode.json").write_text(
            '{"server": {"host": "100.69.77.26", "port": 62306}}\n'
        )
        self.patches = [
            mock.patch.object(swd, "HOME", self.home),
            mock.patch.object(swd, "SYSTEMD_DIR", self.systemd),
            mock.patch.dict(os.environ, {"FLEET_LANES_ROOT": str(self.lanes)}),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()

    # -- helpers ----------------------------------------------------------- #

    def apply(self) -> int:
        return swd.main(["--apply"])

    def verify(self) -> int:
        return swd.main(["--verify"])

    def read(self, relative: str) -> str:
        return (self.home / relative).read_text()

    # -- tests ------------------------------------------------------------- #

    def test_verify_reports_the_missing_knobs(self) -> None:
        self.assertEqual(self.verify(), 1)

    def test_apply_points_every_knob_at_the_lanes_root(self) -> None:
        self.apply()
        self.assertIn(f"-c {self.lanes}", self.read(".local/bin/codex-tmux-boot"))
        self.assertIn(f"-c {self.lanes}", self.read(".local/bin/codex-tmux-autostart"))
        self.assertIn("FLEET_LANES_ROOT", self.read("apps/muse-acp-runtime/minimax-shellular-acp.py"))
        self.assertIn("FLEET_LANES_ROOT", self.read("apps/muse-acp-runtime/muse-shellular-acp.py"))
        self.assertIn("fleet: agent working directory", self.read(".bashrc"))
        unit = (self.systemd / "opencode-server.service").read_text()
        self.assertIn(f"WorkingDirectory={self.lanes}", unit)
        self.assertIn("--port 62306", unit)
        claude = (self.systemd / "claude-remote-control.service").read_text()
        self.assertIn(f"WorkingDirectory={self.lanes}", claude)
        self.assertEqual(self.verify(), 0)

    def test_apply_is_idempotent(self) -> None:
        self.apply()
        before = {p: p.read_text() for p in self.home.rglob("*") if p.is_file()}
        self.apply()
        after = {p: p.read_text() for p in self.home.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_apply_does_not_corrupt_a_correct_tmux_path(self) -> None:
        """A path that already names the lanes root must not be rewritten."""
        boot = self.home / ".local" / "bin" / "codex-tmux-boot"
        boot.write_text(TMUX_BOOT.replace("-c /root", f"-c {self.lanes}"))
        self.apply()
        self.assertEqual(boot.read_text().count(str(self.lanes)), 1)
        self.assertNotIn(f"{self.lanes}{self.lanes}", boot.read_text())

    def test_the_minimax_runtime_gains_the_cwd_and_the_import(self) -> None:
        self.apply()
        text = self.read("apps/muse-acp-runtime/minimax-shellular-acp.py")
        self.assertIn("import os", text)
        self.assertIn('cwd=os.environ.get("FLEET_LANES_ROOT"', text)

    def test_a_host_without_systemd_is_skipped_not_broken(self) -> None:
        self.systemd.rmdir()
        self.apply()
        self.assertEqual(self.verify(), 0)


if __name__ == "__main__":
    unittest.main()
