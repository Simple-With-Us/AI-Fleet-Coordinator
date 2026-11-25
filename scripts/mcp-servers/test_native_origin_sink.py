"""Optional private one-shot diagnostic sink for the call-shape probe.

The probe's bounded shape line already goes to stderr; these tests pin the
optional origin-call-shape.json sink that carries the same reviewed
shape-only payload for clients whose stderr is not retained.  The admitted
credential-shaped fixture in test_native_origin_capture.py is untouched:
these tests use only clearly non-secret prose markers.

Capture-reader contract pinned here:  an absent, partial or invalid sink is
a failed capture.  Success is never inferred from marker disappearance or
file presence; only complete bounded JSON with the fixed schema counts.
"""

import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    "capture", Path(__file__).with_name("assigned-hosted-stdio.py")
)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)

SINK = "origin-call-shape.json"
FLAG = "origin-call-shape.sink"
MARKER = "origin-call-shape.once"


def fd_count():
    return len(os.listdir("/proc/self/fd"))


class ShapeSink(unittest.TestCase):
    def arm(self, d, identity, *, flag=True, flag_mode=0o600, flag_bytes=b""):
        folder = Path(d) / ".simple-tools" / identity
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o700)
        (Path(d) / ".simple-tools").chmod(0o700)
        marker = folder / MARKER
        marker.write_bytes(b"")
        marker.chmod(0o600)
        if flag:
            planted = folder / FLAG
            planted.write_bytes(flag_bytes)
            planted.chmod(flag_mode)
        return folder

    def emit(self, d, identity, params):
        out = io.StringIO()
        with redirect_stderr(out):
            M.call_shape_once({"HOME": d}, identity, params)
        return out.getvalue()

    def params(self, **meta):
        return {"name": "gr_open_pr", "_meta": meta}

    def test_sink_matches_stderr_and_emits_once(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            line = self.emit(d, "AG", self.params(threadId="t"))
            sink = folder / SINK
            self.assertTrue(sink.is_file())
            self.assertEqual(stat.S_IMODE(sink.stat().st_mode), 0o600)
            self.assertEqual(sink.read_text(), line)
            self.assertEqual(json.loads(sink.read_text()), json.loads(line))
            again = self.emit(d, "AG", self.params(threadId="t2"))
            self.assertEqual(again, "")
            self.assertEqual(sink.read_text(), line)
            self.assertEqual(list(folder.glob("*.tmp-*")), [])

    def test_no_flag_no_sink_but_stderr_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG", flag=False)
            line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertTrue(line)
            self.assertFalse((folder / SINK).exists())

    def test_existing_sink_never_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            sink = folder / SINK
            sink.write_text("prior generation\n")
            sink.chmod(0o600)
            line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertTrue(line)
            self.assertEqual(sink.read_text(), "prior generation\n")
            self.assertEqual(list(folder.glob("*.tmp-*")), [])

    def test_symlink_sink_target_untouched(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            outside = Path(d) / "elsewhere.txt"
            outside.write_text("untouched\n")
            os.symlink(outside, folder / SINK)
            line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertTrue(line)
            self.assertEqual(outside.read_text(), "untouched\n")

    def test_symlink_flag_is_not_honored(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG", flag=False)
            os.symlink(Path(d) / "anything", folder / FLAG)
            line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertTrue(line)
            self.assertFalse((folder / SINK).exists())

    def test_flag_custody_refusals(self):
        for name, plant in (
            ("directory", lambda p: p.mkdir()),
            ("fifo", lambda p: os.mkfifo(p)),
            ("wrong-mode", lambda p: (p.write_bytes(b""), p.chmod(0o644))),
            ("non-empty", lambda p: (p.write_bytes(b"x"), p.chmod(0o600))),
        ):
            with self.subTest(kind=name), tempfile.TemporaryDirectory() as d:
                folder = self.arm(d, "AG", flag=False)
                plant(folder / FLAG)
                line = self.emit(d, "AG", self.params(threadId="t"))
                self.assertTrue(line)
                self.assertFalse((folder / SINK).exists())

    def test_hardlinked_flag_is_not_honored(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG", flag=False)
            real = Path(d) / "real-flag"
            real.write_bytes(b"")
            real.chmod(0o600)
            os.link(real, folder / FLAG)
            line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertTrue(line)
            self.assertFalse((folder / SINK).exists())

    def test_sink_name_taken_by_directory_still_emits_stderr(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            (folder / SINK).mkdir()
            line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertTrue(line)
            self.assertTrue((folder / SINK).is_dir())
            self.assertEqual(list(folder.glob("*.tmp-*")), [])

    def test_short_write_completes_full_line(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            real_write = os.write
            calls = []

            def half_write(fd, data):
                calls.append(len(data))
                if len(calls) == 1 and len(data) > 1:
                    return real_write(fd, data[: len(data) // 2])
                return real_write(fd, data)

            with mock.patch.object(os, "write", half_write):
                line = self.emit(d, "AG", self.params(threadId="t"))
            sink = folder / SINK
            self.assertEqual(sink.read_text(), line)
            self.assertEqual(json.loads(sink.read_text()), json.loads(line))
            self.assertGreater(len(calls), 1)

    def test_write_failure_leaves_no_sink_no_temp_and_stable_fds(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            before = fd_count()

            def enospc(fd, data):
                raise OSError(28, "No space left on device")

            with mock.patch.object(os, "write", enospc):
                line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertTrue(line)
            self.assertFalse((folder / SINK).exists())
            self.assertEqual(list(folder.glob("*.tmp-*")), [])
            self.assertEqual(fd_count(), before)

    def test_descriptor_stability_across_sink_paths(self):
        with tempfile.TemporaryDirectory() as d:
            before = fd_count()
            for round_ in range(3):
                folder = self.arm(d, "AG")
                self.emit(d, "AG", self.params(threadId="t"))
                (folder / SINK).unlink()
                (folder / FLAG).unlink()
                marker = folder / MARKER
                marker.write_bytes(b"")
                marker.chmod(0o600)
                (folder / FLAG).write_bytes(b"")
                (folder / FLAG).chmod(0o600)
                self.emit(d, "AG", self.params(threadId="t"))
            self.assertEqual(fd_count(), before)

    def test_preexisting_temp_is_never_deleted(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            # Force the random temp suffix so the exclusive open collides
            # with a file this invocation never created.
            foreign = folder / (SINK + ".tmp-%d-deadbeef" % os.getpid())
            foreign.write_text("not ours\n")
            foreign.chmod(0o600)
            before = fd_count()
            with mock.patch.object(os, "urandom", lambda n: bytes.fromhex("deadbeef")):
                line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertTrue(line)
            self.assertEqual(foreign.read_text(), "not ours\n")
            self.assertFalse((folder / SINK).exists())
            self.assertEqual(fd_count(), before)

    def test_entropy_failure_preserves_stderr_and_descriptors(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            before = fd_count()
            with mock.patch.object(os, "urandom", side_effect=OSError("entropy unavailable")):
                line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertEqual(json.loads(line)["diagnostic"], "origin-call-shape/1")
            self.assertFalse((folder / SINK).exists())
            self.assertFalse(list(folder.glob(SINK + ".tmp-*")))
            self.assertEqual(fd_count(), before)

    def test_close_error_preserves_stderr_and_temp_cleanup(self):
        for fail_write in (False, True):
            with self.subTest(fail_write=fail_write), tempfile.TemporaryDirectory() as d:
                folder = self.arm(d, "AG")
                before = fd_count()
                real_close = os.close
                closed = []

                def close_with_report(fd):
                    real_close(fd)
                    closed.append(fd)
                    raise OSError("simulated delayed close failure")

                original_sink = M._write_shape_sink

                def sink_with_close_error(dir_fd, line):
                    with mock.patch.object(os, "close", side_effect=close_with_report):
                        if fail_write:
                            with mock.patch.object(os, "write", side_effect=OSError("full")):
                                original_sink(dir_fd, line)
                        else:
                            original_sink(dir_fd, line)

                with mock.patch.object(M, "_write_shape_sink", side_effect=sink_with_close_error):
                    line = self.emit(d, "AG", self.params(threadId="t"))
                self.assertEqual(json.loads(line)["diagnostic"], "origin-call-shape/1")
                self.assertEqual(len(closed), 1)
                self.assertFalse((folder / SINK).exists())
                self.assertFalse(list(folder.glob(SINK + ".tmp-*")))
                self.assertEqual(fd_count(), before)

    def test_sink_never_carries_values_or_unknown_names(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            sensitive = "private prose marker 9f8e7d6c5b4a3f2e1d"
            huge = "x" * (1024 * 1024)
            params = {
                "name": "prefix " + sensitive + " " + huge,
                "_meta": {"threadId": "t", "caller-supplied-key": sensitive},
            }
            line = self.emit(d, "AG", params)
            sink_text = (folder / SINK).read_text()
            for payload in (line, sink_text):
                self.assertNotIn(sensitive, payload)
                self.assertNotIn("caller-supplied-key", payload)
                self.assertLess(len(payload), 1024)

    def test_unprivate_identity_dir_never_writes_sink(self):
        with tempfile.TemporaryDirectory() as d:
            folder = self.arm(d, "AG")
            # Group-writable fails the probe's custody check (st_mode & 0o022).
            folder.chmod(0o775)
            line = self.emit(d, "AG", self.params(threadId="t"))
            self.assertEqual(line, "")
            self.assertFalse((folder / SINK).exists())
            self.assertTrue((folder / MARKER).exists())

    def test_flag_alone_without_marker_emits_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            folder = Path(d) / ".simple-tools" / "AG"
            folder.mkdir(parents=True)
            folder.chmod(0o700)
            (Path(d) / ".simple-tools").chmod(0o700)
            flag = folder / FLAG
            flag.write_bytes(b"")
            flag.chmod(0o600)
            self.assertEqual(self.emit(d, "AG", self.params(threadId="t")), "")
            self.assertFalse((folder / SINK).exists())


if __name__ == "__main__":
    unittest.main()
