"""Naming cleanup must not expose unknown or unmoved data to pruning."""
import ast
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent

class PreservationTests(unittest.TestCase):
    def test_shell_gate_keeps_unknown_flat_hidden_and_managed_paths(self):
        source = (ROOT / "disk-janitor.sh").read_text()
        start = source.index("janitor_keep_match() {")
        end = source.index("\n}", start) + 2
        function = source[start:end]
        with tempfile.TemporaryDirectory() as home:
            script = 'HOME="$1"; KEEP_RE="^$"; janitor_canon() { printf "%s" "$1"; };\n' + function + '\n'
            for rel in ('apps/unknown-runtime', '.unmoved/listener/state', 'apps/lanes/_managed/fleet/runtime'):
                result = subprocess.run(['bash', '-c', script + '\njanitor_keep_match "$HOME/' + rel + '"', 'test', home])
                self.assertEqual(result.returncode, 0, rel)
            result = subprocess.run(['bash', '-c', script + '\njanitor_keep_match "$HOME/apps/lanes/Repo/feature"', 'test', home])
            self.assertNotEqual(result.returncode, 0)

    def test_python_gate_keeps_unknown_flat_hidden_and_managed_paths(self):
        text = (ROOT / "mac-auto-cleanup.sh").read_text()
        start = text.index('def unproven_app_root_or_hidden_state(')
        end = text.index('def born_recently(', start)
        space = {'os': os, 'HOME_DIR': '/test-home'}
        exec(compile(ast.parse(text[start:end]), '<preservation gate>', 'exec'), space)
        gate = space['unproven_app_root_or_hidden_state']
        for path in ('/test-home/apps/unknown-runtime', '/test-home/.unmoved/listener/state', '/test-home/apps/lanes/_managed/fleet/runtime'):
            self.assertTrue(gate(path), path)
        self.assertFalse(gate('/test-home/apps/lanes/Repo/feature'))

if __name__ == '__main__':
    unittest.main()
