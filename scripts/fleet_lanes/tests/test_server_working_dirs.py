"""Server working directory tests:  every tool session starts in the lanes root.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_server_working_dirs -v

The script under test writes host files, so every test runs against a throwaway
home, a throwaway lanes root and a throwaway systemd directory.  Nothing here
touches the real home, the real lanes, or /etc.
"""
from __future__ import annotations

import ast
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
exec tmux new-session -e "AGENT_SESSION=\\${AGENT_SESSION-}" codex resume --last
"""

# A launcher that names a Codex `-c key=value` override before the tmux one.
TMUX_BOOT_WITH_CODEX_FLAG = """#!/usr/bin/env bash
/usr/bin/codex -c sandbox_mode=workspace-write --version >/dev/null || :; fi
if /usr/bin/tmux new-session -d -s "$SESSION" -c /root \\\\
  "$HOME/.local/bin/codex" -c model_reasoning_effort=high resume --last; then :; fi
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

# An ACP runtime whose Popen call is a single line, so the injection has to keep
# a different shape from the two above.
SINGLE_LINE_ACP = '''#!/usr/bin/env python3
"""An ACP runtime with one-line Popen calls."""

import json
import subprocess


def main():
    child = subprocess.Popen(["/bin/node", "adapter.js"])
'''

NO_POPEN_ACP = '''#!/usr/bin/env python3
"""An ACP runtime with no subprocess call at all."""

import json


def main():
    return 0
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
        # The generated units run the tools out of ~/.local/bin, which a root
        # operator's PATH does not hold, so the test home carries stand-ins.
        for tool in ("opencode", "claude"):
            binary = self.home / ".local" / "bin" / tool
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)
        self.write_config('{"server": {"host": "example.test", "port": 62306}}\n')
        self.patches = [
            mock.patch.object(swd, "HOME", self.home),
            mock.patch.object(swd, "SYSTEMD_DIR", self.systemd),
            mock.patch.dict(os.environ, {"FLEET_LANES_ROOT": str(self.lanes)}),
            mock.patch.dict(os.environ, {"PATH": str(self.home / "empty-bin")}),
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

    def write_config(self, text: str) -> None:
        (self.home / ".config" / "opencode" / "opencode.json").write_text(text)

    def acp(self, name: str = "minimax-shellular-acp.py") -> Path:
        return self.apps / "muse-acp-runtime" / name

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
        self.assertIn('cwd=(os.environ.get("FLEET_LANES_ROOT") or', text)

    def test_a_host_without_systemd_is_skipped_not_broken(self) -> None:
        self.systemd.rmdir()
        self.apply()
        self.assertEqual(self.verify(), 0)

    # -- ExecStart names a binary that exists (kody: critical) ------------- #

    def test_which_searches_the_directories_the_units_declare(self) -> None:
        self.assertEqual(swd.which("opencode"), str(self.home / ".local" / "bin" / "opencode"))

    def test_apply_refuses_to_invent_an_execstart_path(self) -> None:
        (self.home / ".local" / "bin" / "claude").unlink()
        with self.assertRaises(SystemExit) as caught:
            self.apply()
        self.assertIn("claude", str(caught.exception))
        self.assertFalse((self.systemd / "claude-remote-control.service").exists())

    def test_the_generated_execstart_exists_on_disk(self) -> None:
        self.apply()
        for name in ("opencode-server.service", "claude-remote-control.service"):
            with self.subTest(unit=name):
                unit = (self.systemd / name).read_text()
                binary = swd.unit_execstart(unit).split()[0]
                self.assertTrue(Path(binary).is_file(), f"{binary} does not exist")

    # -- tmux -c is not Codex -c (kody: high, sentry) --------------------- #

    def test_a_codex_override_is_not_mistaken_for_the_tmux_cwd(self) -> None:
        boot = self.home / ".local" / "bin" / "codex-tmux-boot"
        boot.write_text(TMUX_BOOT_WITH_CODEX_FLAG)
        self.apply()
        text = boot.read_text()
        self.assertIn("-c sandbox_mode=workspace-write", text)
        self.assertIn("-c model_reasoning_effort=high", text)
        self.assertIn(f"-c {self.lanes}", text)
        self.assertEqual(self.verify(), 0)

    def test_tmux_cwd_ignores_a_key_value_token(self) -> None:
        self.assertIsNone(swd.tmux_cwd("tmux new-session codex -c sandbox_mode=workspace-write\n"))
        self.assertEqual(swd.tmux_cwd("tmux new-session -d -c /root\n"), "/root")

    # -- the ACP runtime edits are structural (kody: high, sentry) --------- #

    def test_the_rewritten_acp_runtime_is_valid_python(self) -> None:
        for name in ("minimax-shellular-acp.py", "muse-shellular-acp.py"):
            with self.subTest(name=name):
                self.apply()
                tree = ast.parse(self.acp(name).read_text())
                self.assertTrue(swd._binds_os(tree))

    def test_the_acp_runtime_falls_back_when_the_root_is_blank(self) -> None:
        """A blank FLEET_LANES_ROOT must not reach Popen as an empty cwd."""
        with mock.patch.dict(os.environ, {"FLEET_LANES_ROOT": "   "}):
            self.assertEqual(eval(swd.cwd_expr(self.lanes), {"os": os}), str(self.lanes))
        with mock.patch.dict(os.environ, {"FLEET_LANES_ROOT": str(self.lanes)}):
            self.assertEqual(eval(swd.cwd_expr(self.lanes), {"os": os}), str(self.lanes))

    def test_a_stale_acp_fallback_is_rewritten_to_the_current_root(self) -> None:
        self.acp().write_text(MINIMAX_ACP.replace("import json", "import json\nimport os").replace(
            "        text=True, bufsize=1,\n    )",
            '        text=True, bufsize=1,\n        cwd=os.environ.get("FLEET_LANES_ROOT", "/srv/old-lanes"),\n    )',
        ))
        ok, note = swd.knob_acp_runtime(self.acp(), self.lanes)
        self.assertFalse(ok, "a stale fallback must not verify as PASS")
        self.apply()
        self.assertIn(str(self.lanes), self.acp().read_text())
        self.assertNotIn("/srv/old-lanes", self.acp().read_text())
        self.assertEqual(self.verify(), 0)

    def test_a_single_line_acp_call_gains_a_working_cwd(self) -> None:
        self.acp().write_text(SINGLE_LINE_ACP)
        self.apply()
        text = self.acp().read_text()
        ast.parse(text)
        self.assertIn("cwd=", text)
        self.assertIn("import os", text)
        self.assertEqual(self.verify(), 0)

    def test_an_acp_runtime_with_no_popen_is_reported_not_silently_ignored(self) -> None:
        self.acp().write_text(NO_POPEN_ACP)
        self.assertEqual(self.apply(), 1)
        self.assertEqual(self.verify(), 1)

    # -- the OpenCode flags (kody: high) ----------------------------------- #

    def test_the_unit_keeps_its_flags_when_the_config_names_none(self) -> None:
        self.apply()
        self.write_config("{}\n")
        unit = self.systemd / "opencode-server.service"
        unit.write_text(unit.read_text().replace(str(self.home), "/mnt/fleet-extra/home"))
        before = unit.read_text()
        self.apply()
        self.assertIn("--hostname example.test", before)
        self.assertIn("--hostname example.test", unit.read_text())
        self.assertIn("--port 62306", unit.read_text())
        self.assertEqual(self.verify(), 0)

    def test_a_host_that_is_not_a_hostname_is_refused(self) -> None:
        self.write_config('{"server": {"host": "--user root\\nExecStart=/bin/false", "port": 62306}}\n')
        with self.assertRaises(SystemExit):
            self.apply()
        verdicts = [ok for label, ok, _ in swd.check(self.lanes) if label.startswith("opencode")]
        self.assertEqual(verdicts, [False])

    def test_a_port_that_is_not_a_port_is_refused(self) -> None:
        self.write_config('{"server": {"host": "example.test", "port": "62306 --other"}}\n')
        with self.assertRaises(SystemExit):
            self.apply()

    def test_a_valid_host_and_port_are_written_and_verified(self) -> None:
        self.apply()
        unit = (self.systemd / "opencode-server.service").read_text()
        self.assertIn("ExecStart=", unit)
        self.assertIn("--hostname example.test --port 62306", unit)

    # -- operator-owned unit directives survive the rewrite (sentry) ------- #

    def test_an_environment_file_the_operator_added_survives(self) -> None:
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            "EnvironmentFile=/etc/opencode/secrets.env\nEnvironment=EXTRA=1\n"
            "ExecStart=/root/.opencode/bin/opencode serve --service --hostname keepme --port 62306\n"
        )
        self.apply()
        text = unit.read_text()
        self.assertIn("EnvironmentFile=/etc/opencode/secrets.env", text)
        self.assertIn("Environment=EXTRA=1", text)
        self.assertIn(f"WorkingDirectory={self.lanes}", text)
        self.assertEqual(self.verify(), 0)

    # -- the bashrc knob follows the resolved root (kody: medium) ---------- #

    def test_the_bashrc_block_names_the_resolved_root(self) -> None:
        self.apply()
        text = self.read(".bashrc")
        self.assertIn(f'cd "{self.lanes}"', text)
        self.assertNotIn('cd "$HOME/apps/lanes"', text)

    def test_a_bashrc_block_pointing_elsewhere_fails_the_knob(self) -> None:
        self.apply()
        path = self.home / ".bashrc"
        path.write_text(path.read_text().replace(str(self.lanes), "/srv/other"))
        ok, note = swd.knob_bashrc(self.lanes)
        self.assertFalse(ok, note)

    def test_a_marker_without_the_fleet_block_is_not_a_pass(self) -> None:
        (self.home / ".bashrc").write_text("# fleet: agent working directory\n# nothing else\n")
        ok, _ = swd.knob_bashrc(self.lanes)
        self.assertFalse(ok)

    # -- the lanes root resolves like layout.py (kody: medium) ------------- #

    def test_a_tilde_root_is_expanded_against_the_home(self) -> None:
        other = self.home / "other" / "lanes"
        other.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"FLEET_LANES_ROOT": "~/other/lanes", "HOME": str(self.home)}):
            self.assertEqual(swd.lanes_root(), other)

    def test_a_relative_root_is_joined_onto_the_home(self) -> None:
        other = self.home / "rel" / "lanes"
        other.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"FLEET_LANES_ROOT": "rel/lanes"}):
            self.assertEqual(swd.lanes_root(), other)

    # -- the restart hint and the exit status (kody: medium, high) --------- #

    def test_the_restart_hint_names_every_service_the_apply_rewrote(self) -> None:
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.apply()
        printed = buffer.getvalue()
        self.assertIn("claude-remote-control.service", printed)
        self.assertIn("opencode-server.service", printed)

    def test_apply_returns_nonzero_when_a_knob_cannot_be_applied(self) -> None:
        self.acp().write_text(NO_POPEN_ACP)
        self.assertEqual(self.apply(), 1)
        self.assertEqual(self.apply(), 1, "a knob that stays off keeps the apply failing")

    def test_apply_exits_zero_when_every_knob_takes(self) -> None:
        self.assertEqual(self.apply(), 0)


if __name__ == "__main__":
    unittest.main()