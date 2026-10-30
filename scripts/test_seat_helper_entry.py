"""The three entry points run the host's seat helper the same way, only from a trusted file, and never from a hook path."""

import io
import os
import pwd
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ENTRIES = ("simple-chat", "simple-board", "recall")
START, END = (
    "# --- seat remote defaults (host helper, optional) ---",
    "# --- end seat remote defaults ---",
)


def snippet(name):
    text = (HERE / name).read_text()
    return text, text[text.index(START) : text.index(END) + len(END)]


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
        self.assertLess(
            board.index('_seat_defaults("simple-board")'),
            board.index('exec(code, {"__name__": "__main__"'),
        )
        recall, _ = snippet("recall")
        self.assertLess(recall.index('_seat_defaults("recall")'), recall.index("sys.exit(main())"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        os.chmod(self.root, 0o755)
        self.conf = self.root / "home" / ".config" / "fleet-cli"
        self.marker = self.root / "marker"
        self.path = self.conf / "seat-remote-defaults.py"

    def install(self, source=None, mode=0o600):
        self.conf.mkdir(parents=True, exist_ok=True)
        for directory in (self.root / "home", self.root / "home" / ".config", self.conf):
            os.chmod(directory, 0o755)
        if source is None:
            source = f"open({str(self.marker)!r}, 'w').write(fleet_command)\n"
        self.path.write_text(source)
        os.chmod(self.path, mode)

    def run_snippet(self, uid_shift=0):
        """Returns the SystemExit code, or None when the snippet returned normally."""
        ns = {"os": os, "sys": sys}
        exec(snippet("recall")[1], ns)
        ns["_SEAT_HELPER_ANCHOR"] = str(self.root)
        fake = types.SimpleNamespace(pw_dir=str(self.root / "home"))
        real_uid = os.getuid()
        with (
            mock.patch.object(pwd, "getpwuid", return_value=fake),
            mock.patch.object(os, "getuid", return_value=real_uid + uid_shift),
            mock.patch.object(sys, "stderr", new=io.StringIO()) as err,
        ):
            try:
                ns["_seat_defaults"]("recall")
            except SystemExit as exc:
                self.err = err.getvalue()
                return exc.code
        self.err = err.getvalue()
        return None

    def ran(self):
        return self.marker.read_text() if self.marker.exists() else None

    def test_a_trusted_helper_runs_with_the_command_name(self):
        self.install()
        self.assertIsNone(self.run_snippet())
        self.assertEqual(self.ran(), "recall")

    def test_an_absent_helper_or_folder_is_the_optional_case(self):
        self.assertIsNone(self.run_snippet())  # no .config at all
        self.conf.mkdir(parents=True)
        self.assertIsNone(self.run_snippet())  # folder, no file
        self.assertIsNone(self.ran())
        self.assertEqual(self.err, "")

    def test_a_present_but_untrusted_helper_fails_closed_without_running(self):
        for label, prepare, shift in (
            ("group writable", lambda: self.install(mode=0o620), 0),
            ("world writable", lambda: self.install(mode=0o602), 0),
            ("foreign owner", lambda: self.install(), 1),
            ("empty", lambda: self.install(source=""), 0),
            (
                "second hard link",
                lambda: (self.install(), os.link(self.path, self.root / "other")),
                0,
            ),
            (
                "symlinked file",
                lambda: (
                    self.install(),
                    self.path.rename(self.conf / "real.py"),
                    self.path.symlink_to(self.conf / "real.py"),
                ),
                0,
            ),
            (
                "fifo",
                lambda: (self.conf.mkdir(parents=True, exist_ok=True), os.mkfifo(self.path, 0o600)),
                0,
            ),
        ):
            with self.subTest(label):
                if self.root.joinpath("home").exists():
                    shutil.rmtree(self.root / "home")
                (self.root / "other").unlink(missing_ok=True)
                self.marker.unlink(missing_ok=True)
                prepare()
                self.assertEqual(self.run_snippet(shift), 3)
                self.assertIsNone(self.ran())
                self.assertEqual(self.err, "fleet-cli: the seat helper could not be trusted\n")

    def test_an_untrusted_parent_chain_fails_closed(self):
        self.install()
        os.chmod(self.conf, 0o775)
        self.assertEqual(self.run_snippet(), 3)
        os.chmod(self.conf, 0o755)
        os.chmod(self.root / "home", 0o757)
        self.assertEqual(self.run_snippet(), 3)
        os.chmod(self.root / "home", 0o755)
        real = self.root / "home_real"
        (self.root / "home").rename(real)
        (self.root / "home").symlink_to(real)
        self.assertEqual(self.run_snippet(), 3)
        self.assertIsNone(self.ran())

    def test_the_code_that_runs_is_the_bytes_read_from_the_open_file(self):
        self.install()
        real_compile = compile

        def swap_then_compile(source, filename, mode, *args, **kwargs):
            evil = self.conf / "evil"
            evil.write_text(f"open({str(self.marker)!r}, 'w').write('swapped')\n")
            os.chmod(evil, 0o600)
            os.replace(evil, self.path)  # the path now names other code
            return real_compile(source, filename, mode, *args, **kwargs)

        with mock.patch("builtins.compile", side_effect=swap_then_compile):
            self.assertIsNone(self.run_snippet())
        self.assertEqual(self.ran(), "recall")

    def test_a_broken_helper_is_a_refusal_never_a_fallback(self):
        for label, source in (
            ("syntax error", "def broken(:\n"),
            ("runtime error", "raise RuntimeError('secret detail')\n"),
            ("import error", "import module_that_does_not_exist\n"),
        ):
            with self.subTest(label):
                self.install(source=source)
                self.assertEqual(self.run_snippet(), 3)
                self.assertEqual(
                    self.err,
                    "fleet-cli: the seat helper failed\n"
                    if label != "syntax error"
                    else "fleet-cli: the seat helper could not be trusted\n",
                )
                self.assertNotIn("secret detail", self.err)
                self.assertNotIn(str(self.root), self.err)

    def test_the_helpers_own_refusal_propagates_with_its_code(self):
        self.install(source="raise SystemExit(3)\n")
        self.assertEqual(self.run_snippet(), 3)
        self.install(source="raise SystemExit(0)\n")
        self.assertEqual(self.run_snippet(), 0)


if __name__ == "__main__":
    unittest.main()
