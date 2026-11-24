import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zcode_native import NativeDelivery, _save, digest

B = dict(
    host="server",
    local=True,
    session_id="sess-fixture",
    workspace="/fixture",
    generation=1,
)


class Failures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.folder = Path(self.tmp.name)
        self.calls = []

        def transport(req):
            self.calls.append(req["action"])
            return {}

        self.d = NativeDelivery(self.folder, transport, lambda _: True)

    def tearDown(self):
        self.d.close()
        self.tmp.cleanup()

    def send(self):
        return self.d.deliver(B, "new", "fixture", fresh=True)

    def test_file_fsync_failure_no_send(self):
        with patch("zcode_native.os.fsync", side_effect=OSError("fsync")):
            with self.assertRaises(OSError):
                self.send()
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.folder.glob("*.tmp")), [])
        self.send()
        self.assertEqual(self.calls, ["dispatch"])

    def test_directory_fsync_failure_query_after_restart(self):
        actual = os.fsync

        def fail_directory(fd):
            if os.path.isdir("/proc/self/fd/" + str(fd)):
                raise OSError("directory-fsync")
            return actual(fd)

        with patch("zcode_native.os.fsync", side_effect=fail_directory):
            with self.assertRaises(OSError):
                self.send()
        self.assertEqual(self.calls, [])
        self.d.close()
        self.d = NativeDelivery(
            self.folder,
            lambda req: self.calls.append(req["action"]) or {},
            lambda _: True,
        )
        self.send()
        self.assertEqual(self.calls, ["query"])

    def test_replace_failure_no_send(self):
        with patch("zcode_native.os.replace", side_effect=OSError("replace")):
            with self.assertRaises(OSError):
                self.send()
        self.assertEqual(self.calls, [])

    def test_save_after_send_failure_query_only(self):
        actual = _save
        count = 0

        def save(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("post-send-save")
            return actual(*args)

        with patch("zcode_native._save", side_effect=save):
            with self.assertRaises(OSError):
                self.send()
        self.assertEqual(self.calls, ["dispatch"])
        self.send()
        self.assertEqual(self.calls, ["dispatch", "query"])

    def test_process_crash_after_durable_intent(self):
        import multiprocessing

        def child():
            d = NativeDelivery(self.folder, lambda _: os._exit(23), lambda _: True)
            d.deliver(B, "new", "fixture", fresh=True)

        p = multiprocessing.get_context("fork").Process(target=child)
        p.start()
        p.join(5)
        self.assertEqual(p.exitcode, 23)
        self.send()
        self.assertEqual(self.calls, ["query"])

    def test_orphan_temp_never_blocks_existing_intent_query(self):
        self.send()
        self.assertEqual(self.calls, ["dispatch"])
        orphan = self.folder / (digest({"wake_key": "new"}) + ".orphan.tmp")
        orphan.write_text("incomplete")
        self.send()
        self.assertEqual(self.calls, ["dispatch", "query"])
        self.assertTrue(orphan.exists())

    def test_replace_failure_cleaned_then_safe_first_dispatch(self):
        with patch("zcode_native.os.replace", side_effect=OSError("replace")):
            with self.assertRaises(OSError):
                self.send()
        self.assertEqual(list(self.folder.glob("*.tmp")), [])
        self.send()
        self.assertEqual(self.calls, ["dispatch"])

    def test_temp_symlink_not_touched(self):
        target = self.folder / "keep"
        target.write_text("keep")
        orphan = self.folder / (digest({"wake_key": "new"}) + ".tmp")
        orphan.symlink_to(target)
        self.send()
        self.assertEqual(self.calls, ["dispatch"])
        self.assertTrue(orphan.is_symlink())
        self.assertEqual(target.read_text(), "keep")

    def test_crash_before_replace_orphan_does_not_authorize_replay(self):
        import multiprocessing

        def child():
            d = NativeDelivery(self.folder, lambda _: os._exit(99), lambda _: True)
            with patch(
                "zcode_native.os.replace", side_effect=lambda *a, **kw: os._exit(24)
            ):
                d.deliver(B, "new", "fixture", fresh=True)

        p = multiprocessing.get_context("fork").Process(target=child)
        p.start()
        p.join(5)
        self.assertEqual(p.exitcode, 24)
        self.assertTrue(list(self.folder.glob("*.tmp")))
        self.send()
        self.send()
        self.assertEqual(self.calls, ["dispatch", "query"])


if __name__ == "__main__":
    unittest.main()
