"""The three entry points run the host's seat helper the same way, only from a trusted file, and never from a hook path."""
import os
import pwd
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ENTRIES = ("simple-chat", "simple-board", "recall")
START, END = "# --- seat remote defaults (host helper, optional) ---", "# --- end seat remote defaults ---"


def snippet(name):
    text = (HERE / name).read_text()
    return text, text[text.index(START): text.index(END) + len(END)]


class EntryTests(unittest.TestCase):
    def test_the_snippet_is_identical_in_all_three_entry_points(self):
        self.assertEqual(len({snippet(n)[1] for n in ENTRIES}), 1)

    def test_it_runs_before_the_tool_and_after_the_hook_fast_paths(self):
        text, _ = snippet("simple-chat")
        call = text.index('_seat_defaults("simple-chat")')
        for hook in ('"attach", "detach"', '== "letter"', '"handbook-service"'):
            self.assertLess(text.index(hook), call, hook)
        self.assertLess(call, text.index("sys.exit(main(sys.argv[1:]"))
        board, _ = snippet("simple-board")
        self.assertLess(board.index('_seat_defaults("simple-board")'), board.index("exec(code"))
        recall, _ = snippet("recall")
        self.assertLess(recall.index('_seat_defaults("recall")'), recall.index("sys.exit(main())"))

    def run_snippet(self, mode, uid=None, helper=True):
        calls = []
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home, ".config/fleet-cli")
            conf.mkdir(parents=True)
            path = conf / "seat-remote-defaults.py"
            if helper:
                path.write_text("")
                os.chmod(path, mode)
            ns = {"os": os, "sys": sys}
            exec(snippet("recall")[1], ns)
            fake = types.SimpleNamespace(pw_dir=home)
            run = types.SimpleNamespace(run_path=lambda p, init_globals: calls.append((p, init_globals)))
            real_uid = os.getuid()
            with mock.patch.object(pwd, "getpwuid", return_value=fake), mock.patch.dict(sys.modules, {"runpy": run}):
                if uid is not None:
                    with mock.patch.object(os, "getuid", return_value=real_uid + uid):
                        ns["_seat_defaults"]("recall")
                else:
                    ns["_seat_defaults"]("recall")
            return calls, str(path)

    def test_a_trusted_helper_runs_with_the_command_name(self):
        calls, path = self.run_snippet(0o600)
        self.assertEqual(calls, [(path, {"fleet_command": "recall"})])

    def test_an_absent_group_writable_or_foreign_helper_is_skipped(self):
        self.assertEqual(self.run_snippet(0o600, helper=False)[0], [])
        self.assertEqual(self.run_snippet(0o620)[0], [])
        self.assertEqual(self.run_snippet(0o602)[0], [])
        self.assertEqual(self.run_snippet(0o600, uid=1)[0], [])

    def test_a_symlinked_helper_is_skipped(self):
        with tempfile.TemporaryDirectory() as home:
            conf = Path(home, ".config/fleet-cli")
            conf.mkdir(parents=True)
            (conf / "real.py").write_text("")
            (conf / "seat-remote-defaults.py").symlink_to(conf / "real.py")
            ns = {"os": os, "sys": sys}
            exec(snippet("recall")[1], ns)
            calls = []
            run = types.SimpleNamespace(run_path=lambda p, init_globals: calls.append(p))
            with mock.patch.object(pwd, "getpwuid", return_value=types.SimpleNamespace(pw_dir=home)), mock.patch.dict(sys.modules, {"runpy": run}):
                ns["_seat_defaults"]("recall")
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
