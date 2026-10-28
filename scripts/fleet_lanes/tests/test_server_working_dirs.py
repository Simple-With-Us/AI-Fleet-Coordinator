"""Server working directory tests:  every tool session starts in the lanes root.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_server_working_dirs -v

The script under test writes host files, so every test runs against a throwaway
home, a throwaway lanes root and a throwaway systemd directory.  Nothing here
touches the real home, the real lanes, or /etc.
"""
from __future__ import annotations

import ast
import json
import os
import shlex
import subprocess
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


class _FailingHandle:
    """A temporary file whose write fails after it has written part of the text."""

    def __init__(self, handle) -> None:
        self._handle = handle

    @property
    def name(self) -> str:
        return self._handle.name

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._handle.__exit__(*exc_info)

    def write(self, text: str) -> int:
        self._handle.write(text[: len(text) // 2])
        self._handle.flush()
        raise OSError(28, "No space left on device")

    def flush(self) -> None:
        self._handle.flush()

    def fileno(self) -> int:
        return self._handle.fileno()


# The real factory, taken before any test replaces it.
NAMED_TEMPORARY_FILE = tempfile.NamedTemporaryFile


def failing_tempfile(*args, **kwargs):
    """A stand-in for tempfile.NamedTemporaryFile whose write always fails."""
    return _FailingHandle(NAMED_TEMPORARY_FILE(*args, **kwargs))


class _OSProxy:
    """The os module with one call replaced, so only this module sees it."""

    def __init__(self, real, name: str, error: OSError) -> None:
        self._real = real
        self._name = name
        self._error = error

    def __getattr__(self, attribute):
        if attribute == self._name:
            def refuse(*args, **kwargs):
                raise self._error
            return refuse
        return getattr(self._real, attribute)


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
            self.home / ".claude",
            self.systemd,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text('{\n  "env": {"AGENT_SEAT": "CLAUDE"},\n  "theme": "dark"\n}\n')
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
        self.assertEqual(json.loads(self.settings.read_text())["worktree"]["location"], str(self.lanes.resolve()))
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
        unit.write_text(unit.read_text().replace(str(self.home), "/srv/checkout-volume/home"))
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
            "ExecStart=/root/.opencode/bin/opencode serve --service --hostname example.test --port 62306\n"
        )
        self.apply()
        text = unit.read_text()
        self.assertIn("EnvironmentFile=/etc/opencode/secrets.env", text)
        self.assertIn("Environment=EXTRA=1", text)
        self.assertIn(f"WorkingDirectory={self.lanes}", text)
        self.assertEqual(self.verify(), 0)

    def test_a_stale_root_in_the_unit_is_not_carried_over(self) -> None:
        """A unit that already names an old root must not keep it after the rewrite.

        systemd takes the last assignment for a variable, and the carried line
        lands after the template's, so carrying the old value would run the
        unit with a root that is no longer there.
        """
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            'Environment="FLEET_CODE_ROOT=/srv/old-volume/Code"\n'
            "ExecStart=/root/.opencode/bin/opencode serve --service --hostname example.test --port 62306\n"
        )
        self.apply()
        text = unit.read_text()
        self.assertNotIn("/srv/old-volume/Code", text)
        self.assertIn(f'Environment="FLEET_CODE_ROOT={self.home / "Code"}"', text)
        self.assertEqual(text.count("FLEET_CODE_ROOT="), 1)
        self.assertEqual(self.verify(), 0)

    def test_a_carried_line_keeps_the_variables_the_template_does_not_own(self) -> None:
        """One line can set both:  the template's root and the operator's own."""
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            'Environment="FLEET_CODE_ROOT=/srv/old-volume/Code" EXTRA=1\n'
            "ExecStart=/root/.opencode/bin/opencode serve --service --hostname example.test --port 62306\n"
        )
        self.apply()
        text = unit.read_text()
        self.assertNotIn("/srv/old-volume/Code", text)
        self.assertIn("EXTRA=1", text)
        self.assertIn(f'Environment="FLEET_CODE_ROOT={self.home / "Code"}"', text)
        self.assertEqual(self.verify(), 0)

    def test_a_quoted_value_with_a_space_is_read_as_one_variable(self) -> None:
        """systemd splits on the whitespace that is outside quotes, and so do we."""
        assignments = swd.environment_assignments(
            'Environment="FLEET_CODE_ROOT=/mnt/a volume/Code" EXTRA=1'
        )
        self.assertEqual(
            assignments,
            [("FLEET_CODE_ROOT", '"FLEET_CODE_ROOT=/mnt/a volume/Code"'), ("EXTRA", "EXTRA=1")],
        )

    # -- the operator's own identity survives the rewrite (AFC-541) -------- #

    OPERATOR_UNIT = (
        "[Unit]\nDescription=mine\n\n[Service]\n"
        "User=jay\nGroup=jay\nEnvironment=HOME=/srv/example/home\n"
        "ExecStart=/srv/example/home/.opencode/bin/opencode serve --service"
        " --hostname example.test --port 62306\n"
    )

    def test_the_operator_s_identity_survives_the_apply(self) -> None:
        """A unit the operator set up keeps the account, the group and the command.

        The template carries a default for each of them and the default used to win,
        so the apply rewrote the unit as a root-owned one that ran another binary and
        the host had to be repaired by hand.
        """
        unit = self.systemd / "opencode-server.service"
        unit.write_text(self.OPERATOR_UNIT)
        self.apply()
        text = unit.read_text()
        self.assertIn("User=jay", text)
        self.assertIn("Group=jay", text)
        self.assertIn("Environment=HOME=/srv/example/home", text)
        self.assertIn(
            "ExecStart=/srv/example/home/.opencode/bin/opencode serve --service"
            " --hostname example.test --port 62306",
            text,
        )
        self.assertNotIn("User=root", text)
        self.assertEqual(text.count("ExecStart="), 1)
        self.assertEqual(self.verify(), 0)

    def test_two_execstart_lines_stay_two(self) -> None:
        """systemd runs every ExecStart line, so the operator's replace the template's."""
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            "ExecStart=/srv/example/home/.opencode/bin/opencode serve --service"
            " --hostname example.test --port 62306\n"
            "ExecStart=/srv/example/home/.opencode/bin/opencode worker"
            " --hostname example.test --port 62306\n"
        )
        self.apply()
        text = unit.read_text()
        self.assertEqual(text.count("ExecStart="), 2)
        self.assertNotIn(str(self.home / ".local" / "bin" / "opencode"), text)
        self.assertEqual(self.verify(), 0)

    def test_a_continued_execstart_stays_a_whole_command(self) -> None:
        """systemd continues a line that ends with a backslash, so the command is one line.

        A directive that keeps its backslash would read the line after it as part of
        the command, and the apply would write a unit that runs the wrong thing.
        """
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            "ExecStart=/srv/example/home/.opencode/bin/opencode serve \\\n"
            "    --service --hostname example.test --port 62306\n"
        )
        self.apply()
        text = unit.read_text()
        self.assertNotIn("\\\n", text)
        self.assertIn("--service --hostname example.test --port 62306", text)
        self.assertEqual(
            swd.unit_execstart_flags(text), " --hostname example.test --port 62306"
        )
        self.assertEqual(self.verify(), 0)

    def test_a_continued_environment_line_survives_the_merge(self) -> None:
        """An Environment= the unit continues is one value, and it is carried once."""
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            'Environment="EXTRA=one \\\n'
            'two"\n'
        )
        self.apply()
        text = unit.read_text()
        self.assertEqual(text.count("EXTRA=one"), 1)
        self.assertNotIn("\\\n", text)
        self.assertIn("EXTRA=one two", text)

    def test_a_stale_pair_in_the_operator_s_command_becomes_the_configured_one(self) -> None:
        """The host and the port are the knob's policy, so the command carries the configured pair."""
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            "ExecStart=/srv/example/home/.opencode/bin/opencode serve --service"
            " --hostname old.example --port 1234\n"
        )
        self.apply()
        text = unit.read_text()
        self.assertIn("--hostname example.test --port 62306", text)
        self.assertNotIn("old.example", text)
        self.assertEqual(self.verify(), 0)

    def test_a_command_with_no_pair_gains_the_configured_one(self) -> None:
        """A command that names no host and no port gains the configured pair."""
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            "ExecStart=/srv/example/home/.opencode/bin/opencode serve --service\n"
        )
        self.apply()
        text = unit.read_text()
        self.assertIn("--hostname example.test --port 62306", text)
        self.assertEqual(self.verify(), 0)

    def test_a_config_that_names_no_server_block_leaves_the_command_alone(self) -> None:
        """No configured pair means no pair is invented and none is removed."""
        self.write_config("{}\n")
        command = (
            "ExecStart=/srv/example/home/.opencode/bin/opencode serve --service"
            " --hostname keep.example --port 1234\n"
        )
        unit = self.systemd / "opencode-server.service"
        unit.write_text("[Unit]\nDescription=mine\n\n[Service]\n" + command)
        self.apply()
        text = unit.read_text()
        self.assertIn(command, text)
        self.assertEqual(text.count("ExecStart="), 1)

    def test_a_unit_that_sets_no_identity_gets_the_template_values(self) -> None:
        """A directive the operator did not set keeps the template's value."""
        unit = self.systemd / "opencode-server.service"
        unit.write_text("[Unit]\nDescription=mine\n\n[Service]\nType=simple\n")
        self.apply()
        text = unit.read_text()
        self.assertIn("User=root", text)
        self.assertIn(
            f"ExecStart={self.home}/.local/bin/opencode serve --service"
            " --hostname example.test --port 62306",
            text,
        )
        self.assertEqual(self.verify(), 0)

    def test_a_unit_that_keeps_its_own_execstart_needs_no_template_binary(self) -> None:
        """The template's binary is resolved only when the template's own line survives."""
        (self.home / ".local" / "bin" / "opencode").unlink()
        unit = self.systemd / "opencode-server.service"
        unit.write_text(self.OPERATOR_UNIT)
        self.apply()
        self.assertIn("ExecStart=/srv/example/home/.opencode/bin/opencode", unit.read_text())
        self.assertEqual(self.verify(), 0)

    def test_the_knob_passes_for_a_unit_with_the_operator_s_own_identity(self) -> None:
        """The knob reads the working directory and the roots, never the identity."""
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            "User=jay\nGroup=jay\n"
            f"WorkingDirectory={self.lanes}\n"
            f'Environment="FLEET_CODE_ROOT={self.home / "Code"}"\n'
            f'Environment="FLEET_APPS_ROOT={self.apps}"\n'
            f'Environment="FLEET_LANES_ROOT={self.lanes}"\n'
            "ExecStart=/srv/example/home/.opencode/bin/opencode serve --service"
            " --hostname example.test --port 62306\n"
        )
        ok, note = swd.knob_unit(unit, self.lanes, swd.opencode_server_flags())
        self.assertTrue(ok, note)

    # -- the bashrc knob follows the resolved root (kody: medium) ---------- #

    def test_the_bashrc_block_names_the_resolved_root(self) -> None:
        self.apply()
        text = self.read(".bashrc")
        self.assertIn(f"cd {shlex.quote(str(self.lanes))}", text)
        self.assertNotIn('cd "$HOME/apps/lanes"', text)

    def test_a_bashrc_block_pointing_elsewhere_fails_the_knob(self) -> None:
        self.apply()
        path = self.home / ".bashrc"
        path.write_text(path.read_text().replace(str(self.lanes), "/srv/other"))
        ok, note = swd.knob_bashrc(self.lanes)
        self.assertFalse(ok, note)

    def test_the_bashrc_block_exports_the_roots(self) -> None:
        """A host that keeps its trees on a volume needs the roots in the environment."""
        self.apply()
        text = self.read(".bashrc")
        self.assertIn(f"export FLEET_LANES_ROOT={shlex.quote(str(self.lanes))}", text)
        self.assertIn(f"export FLEET_APPS_ROOT={shlex.quote(str(self.apps))}", text)
        self.assertIn(f"export FLEET_CODE_ROOT={shlex.quote(str(self.home / 'Code'))}", text)

    def test_the_bashrc_block_quotes_a_root_that_carries_a_metacharacter(self) -> None:
        """A root read from the environment is written into a file every shell sources.

        A path that carried a metacharacter would otherwise run a command substitution
        in every interactive shell on the host (kody: critical, on AFC-541).
        """
        nasty = self.home / "a$(id)" / "b"
        nasty.mkdir(parents=True)
        with mock.patch.dict(os.environ, {"FLEET_APPS_ROOT": str(nasty)}):
            self.apply()
        text = self.read(".bashrc")
        self.assertIn(f"export FLEET_APPS_ROOT={shlex.quote(str(nasty))}", text)
        self.assertNotIn(f'export FLEET_APPS_ROOT="{nasty}"', text)
        # The real proof: a shell that sources the block reads the path as it was
        # written, and the substitution in it never runs.
        bash = Path("/bin/bash")
        if not bash.exists():
            self.skipTest("this host has no /bin/bash")
        result = subprocess.run(
            [str(bash), "-c",
             f'source {shlex.quote(str(self.home / ".bashrc"))}; printf %s "$FLEET_APPS_ROOT"'],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, str(nasty))

    def test_a_bashrc_block_without_the_exports_fails_the_knob(self) -> None:
        self.apply()
        path = self.home / ".bashrc"
        path.write_text(path.read_text().replace("export FLEET_CODE_ROOT=", "export UNUSED_CODE_ROOT="))
        ok, note = swd.knob_bashrc(self.lanes)
        self.assertFalse(ok, note)
        self.assertIn("FLEET_CODE_ROOT", note)

    def test_apply_upgrades_a_block_that_lacks_the_exports(self) -> None:
        self.apply()
        path = self.home / ".bashrc"
        path.write_text(path.read_text().replace(
            f"export FLEET_CODE_ROOT={shlex.quote(str(self.home / 'Code'))}\n", ""))
        ok, _ = swd.knob_bashrc(self.lanes)
        self.assertFalse(ok)
        self.apply()
        self.assertTrue(swd.knob_bashrc(self.lanes)[0])
        self.assertEqual(self.verify(), 0)

    def test_apply_does_not_duplicate_the_closing_marker(self) -> None:
        """The block renders its own closing line, so the old one's tail must not survive."""
        self.apply()
        first = self.read(".bashrc")
        self.apply()
        second = self.read(".bashrc")
        self.assertEqual(first.count(swd.BASHRC_END), 1)
        self.assertEqual(second.count(swd.BASHRC_END), 1)
        self.assertEqual(first, second)

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

    # -- the Claude desktop app's SSH worktrees ---------------------------- #

    def test_the_desktop_worktree_location_keeps_the_other_settings(self) -> None:
        self.apply()
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["env"], {"AGENT_SEAT": "CLAUDE"})
        self.assertEqual(data["theme"], "dark")
        self.assertTrue(list(self.settings.parent.glob("settings.json.bak-*")), "the apply leaves a backup")

    def test_a_location_spelled_with_a_tilde_already_passes(self) -> None:
        self.settings.write_text('{"worktree": {"location": "~/apps/lanes"}}\n')
        before = self.settings.read_text()
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            ok, note = swd.knob_claude_worktrees(self.settings, self.lanes)
            self.assertTrue(ok, note)
            self.apply()
        self.assertEqual(self.settings.read_text(), before, "a location that is already right is not rewritten")

    def test_a_location_elsewhere_fails_and_is_moved_to_the_lanes_root(self) -> None:
        self.settings.write_text('{"worktree": {"location": "/srv/elsewhere", "bgIsolation": "none"}}\n')
        ok, _ = swd.knob_claude_worktrees(self.settings, self.lanes)
        self.assertFalse(ok)
        self.apply()
        section = json.loads(self.settings.read_text())["worktree"]
        self.assertEqual(section, {"location": str(self.lanes.resolve()), "bgIsolation": "none"})

    def test_settings_that_are_not_a_json_object_are_left_alone(self) -> None:
        self.settings.write_text("not json\n")
        self.assertEqual(self.apply(), 1)
        self.assertEqual(self.settings.read_text(), "not json\n")

    def test_a_host_without_claude_settings_gets_none_written(self) -> None:
        self.settings.unlink()
        self.assertEqual(self.apply(), 0)
        self.assertFalse(self.settings.exists())

    # -- a knob the account cannot write is skipped, not fatal (Oct 28) ---- #
    #
    # Found live on the Hetzner host (Oct 28, 2026):  the apply died with a
    # PermissionError while backing up a systemd unit, so the knobs after it and
    # the final verify never ran.

    def apply_output(self) -> tuple[int, str]:
        """Run the apply and return its exit code and everything it printed."""
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = self.apply()
        return code, buffer.getvalue()

    def stale_opencode_unit(self) -> Path:
        """A unit that exists and is off, so the apply wants to rewrite it."""
        unit = self.systemd / "opencode-server.service"
        unit.write_text(
            "[Unit]\nDescription=mine\n\n[Service]\n"
            "WorkingDirectory=/srv/old-volume/lanes\n"
            "ExecStart=/srv/example/home/.opencode/bin/opencode serve --service\n"
        )
        return unit

    def test_an_unwritable_knob_is_skipped_and_the_apply_continues(self) -> None:
        """A knob the account cannot write must not abort the whole apply."""
        unit = self.stale_opencode_unit()
        original = unit.read_text()
        real_write = swd.write

        def refuse(path, text):
            if path == unit:
                raise PermissionError(13, "Permission denied")
            real_write(path, text)

        with mock.patch.object(swd, "write", refuse):
            code, printed = self.apply_output()

        self.assertIn("skipped  opencode-server.service", printed)
        self.assertIn("not writable by this account", printed)
        # The knob after the skipped one is still applied.
        claude = self.systemd / "claude-remote-control.service"
        self.assertTrue(claude.exists(), "the knob after the skipped one was not applied")
        self.assertIn(f"WorkingDirectory={self.lanes}", claude.read_text())
        # The unit that could not be written is untouched.
        self.assertEqual(unit.read_text(), original)
        self.assertEqual(code, 1, "a knob that is off keeps the apply failing")

    def test_the_final_verify_still_runs_after_a_skipped_knob(self) -> None:
        """The apply still ends with the verify, so it reports the real state."""
        unit = self.stale_opencode_unit()
        real_write = swd.write

        def refuse(path, text):
            if path == unit:
                raise PermissionError(13, "Permission denied")
            real_write(path, text)

        with mock.patch.object(swd, "write", refuse):
            code, printed = self.apply_output()

        self.assertIn("FAIL  opencode-server.service", printed)
        self.assertIn("PASS  claude-remote-control.service", printed)
        # The exit code is the verify's, not a guess.
        self.assertEqual(code, self.verify())

    def test_a_knob_that_cannot_be_written_is_reported_off_by_the_verify(self) -> None:
        """A knob that could not be applied is off, and the verify says so."""
        unit = self.stale_opencode_unit()
        real_write = swd.write

        def refuse(path, text):
            if path == unit:
                raise PermissionError(13, "Permission denied")
            real_write(path, text)

        with mock.patch.object(swd, "write", refuse):
            code, _ = self.apply_output()

        self.assertEqual(code, 1)
        verdicts = {label: ok for label, ok, _ in swd.check(self.lanes)}
        self.assertFalse(verdicts["opencode-server.service"])
        self.assertTrue(verdicts["claude-remote-control.service"])

    def test_an_unwritable_knob_of_any_kind_is_skipped_and_the_apply_continues(self) -> None:
        """Every knob kind gets the same treatment, not only the systemd units."""
        original = self.settings.read_text()
        real_backup = swd.backup

        def refuse(path):
            if path == self.settings:
                raise PermissionError(13, "Permission denied")
            real_backup(path)

        with mock.patch.object(swd, "backup", refuse):
            code, printed = self.apply_output()

        self.assertIn("skipped  claude desktop worktrees", printed)
        self.assertIn("not writable by this account", printed)
        # The knob after it is still applied, and the one before it too.
        self.assertIn("changed  opencode-server.service", printed)
        self.assertIn(str(self.lanes), self.read("apps/muse-acp-runtime/minimax-shellular-acp.py"))
        # The file that could not be written is untouched.
        self.assertEqual(self.settings.read_text(), original)
        self.assertEqual(code, 1, "a knob that is off keeps the apply failing")

    def test_a_write_that_fails_leaves_the_file_it_had_intact(self) -> None:
        """A write that fails at the last step leaves the old file whole."""
        staged = self.tmp / "staged"
        staged.mkdir()
        path = staged / "unit.service"
        path.write_text("original\n")
        proxy = _OSProxy(os, "replace", OSError(28, "No space left on device"))
        with mock.patch.object(swd, "os", proxy):
            with self.assertRaises(OSError):
                swd.write(path, "replacement\n")
        self.assertEqual(path.read_text(), "original\n")
        self.assertEqual([p.name for p in staged.iterdir()], ["unit.service"])

    def test_a_write_that_fails_partway_leaves_no_half_written_file(self) -> None:
        """A write that fails while staging never touches the file it had."""
        staged = self.tmp / "staged"
        staged.mkdir()
        path = staged / "unit.service"
        path.write_text("original\n")
        with mock.patch.object(swd.tempfile, "NamedTemporaryFile", failing_tempfile):
            with self.assertRaises(OSError):
                swd.write(path, "replacement\n")
        self.assertEqual(path.read_text(), "original\n")
        self.assertEqual([p.name for p in staged.iterdir()], ["unit.service"])

    def test_a_unit_that_cannot_be_written_is_never_left_half_written(self) -> None:
        """A unit whose write fails partway keeps the content it had."""
        unit = self.stale_opencode_unit()
        original = unit.read_text()
        with mock.patch.object(swd.tempfile, "NamedTemporaryFile", failing_tempfile):
            code, printed = self.apply_output()
        self.assertEqual(unit.read_text(), original)
        self.assertIn("skipped  opencode-server.service", printed)
        self.assertEqual(code, 1)
        # No staged file is left beside the unit:  a staged write is named with a
        # leading dot, and the unit that failed keeps the content it had.
        staged = [p.name for p in self.systemd.iterdir() if p.name.startswith(".")]
        self.assertEqual(staged, [])


if __name__ == "__main__":
    unittest.main()