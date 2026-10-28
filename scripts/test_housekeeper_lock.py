#!/usr/bin/env python3
"""Tests for the housekeeper locks.

The locks live in the housekeeper skill, not in code, so the test runs the text
of the skill.  The blocks are taken from the one home under
``docs/fleet-skills/housekeeper/``, and the ssh hop is replaced with a fake that
runs the host side of the call on this machine, so the block that runs is the
block a seat copies and the test never opens a network connection.

The paths each block has to get right:

  A  the lock is free        -> the lock is taken, the work runs, and the trap
                                 releases the lock when the run ends.
  B  a peer holds the lock   -> the run exits 0, no work runs, and the peer's
                                 lock is left alone.
  C  the lock cannot be made -> the run exits nonzero, no work runs, the error
                                 names what went wrong, and the original error
                                 survives in the traceback.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ROOT / "docs" / "fleet-skills" / "housekeeper" / "SKILL.md"
REMOTE_LOCK = "/run/fleet-housekeeper.lock"

FAKE_SSH = '''#!/usr/bin/env python3
"""Stand-in for ssh:  run the host side of the call on this machine."""
import os
import subprocess
import sys

args = sys.argv[1:]
command = args[1] if len(args) > 1 else ""
lock = os.environ["LOCK_PATH"]

if command == "python3 -":
    script = sys.stdin.read().replace("/run/fleet-housekeeper.lock", lock)
    done = subprocess.run([sys.executable, "-"], input=script, text=True)
    sys.exit(done.returncode)

if command.startswith("rmdir "):
    try:
        os.rmdir(lock)
    except OSError:
        pass
    sys.exit(0)

if "df -h" in command:
    sys.stdout.write("SAMPLED:" + command + "\\n")
elif "prune" in command:
    sys.stdout.write("PRUNED:" + command + "\\n")
else:
    sys.stdout.write("OTHER:" + command + "\\n")
sys.exit(0)
'''


def _canonical_text() -> str:
    return CANONICAL.read_text(encoding="utf-8")


def _blocks(text: str) -> list:
    """Every bash block in the skill, in document order."""
    return re.findall(r"```bash\n(.*?)```", text, re.DOTALL)


def _remote_block(text: str) -> str:
    """The block that takes the lock on the host over ssh."""
    for block in _blocks(text):
        if "ssh \"$HOUSEKEEPER_SSH_ALIAS\" 'python3 -'" in block:
            return block
    raise AssertionError("no remote-lock block in the housekeeper skill")


def _mac_block(text: str) -> str:
    """The block that takes the lock on this Mac."""
    for block in _blocks(text):
        if "os.mkdir(sys.argv[1])" in block:
            return block
    raise AssertionError("no Mac-lock block in the housekeeper skill")


def _skill_copies() -> list:
    """Every rendered copy of the housekeeper skill in the tree."""
    found = []
    for path in ROOT.rglob("SKILL.md"):
        if ".git" in path.parts:
            continue
        if path.parent.name == "housekeeper":
            found.append(path)
    return sorted(found)


class LockBlockTest(unittest.TestCase):
    """The one home and every copy carry the same lock blocks."""

    def test_canonical_home_holds_both_blocks(self) -> None:
        self.assertTrue(CANONICAL.is_file(), "%s is missing" % CANONICAL)
        text = _canonical_text()
        self.assertIn(REMOTE_LOCK, _remote_block(text))
        self.assertIn("os.mkdir(sys.argv[1])", _mac_block(text))

    def test_both_playbooks_install_the_same_release_function(self) -> None:
        """Bash keeps one trap per signal, so the two blocks must agree."""
        text = _canonical_text()
        for name, block in (("remote", _remote_block(text)), ("mac", _mac_block(text))):
            with self.subTest(block=name):
                self.assertIn("release_locks() {", block)
                self.assertIn("trap release_locks EXIT", block)
        self.assertEqual(
            _release_function(_remote_block(text)),
            _release_function(_mac_block(text)),
            "the two playbooks install different release functions",
        )

    def test_every_copy_carries_the_same_blocks(self) -> None:
        text = _canonical_text()
        remote = _remote_block(text)
        mac = _mac_block(text)
        copies = _skill_copies()
        self.assertGreater(len(copies), 1, "no rendered copies of the skill")
        for copy in copies:
            with self.subTest(copy=str(copy.relative_to(ROOT))):
                body = copy.read_text(encoding="utf-8")
                self.assertIn(remote, _remote_block(body), "%s drifted" % copy)
                self.assertIn(mac, _mac_block(body), "%s drifted" % copy)


def _release_function(block: str) -> str:
    """The text of the release_locks function in one block."""
    start = block.index("release_locks() {")
    end = block.index("}\n", start) + 2
    return block[start:end]


class _LockRunTest(unittest.TestCase):
    """Shared scaffolding for the two lock blocks."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="housekeeper-lock-"))
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.ssh = self.bin / "ssh"
        self.ssh.write_text(FAKE_SSH, encoding="utf-8")
        self.ssh.chmod(0o755)
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def _run(self, block: str, env: dict):
        full = dict(os.environ)
        full["PATH"] = "%s%s%s" % (self.bin, os.pathsep, full.get("PATH", ""))
        full.update(env)
        return subprocess.run(["bash", "-c", block], capture_output=True, text=True, env=full)


class RemoteLockBehaviourTest(_LockRunTest):
    """The remote block takes the lock, reads the host's answer, and releases it."""

    def setUp(self) -> None:
        super().setUp()
        self.block = _remote_block(_canonical_text())

    def _run_remote(self, lock: Path, mac_lock: Path = None):
        env = {
            "HOUSEKEEPER_SSH_ALIAS": "housekeeper-test-host",
            "LOCK_PATH": str(lock),
        }
        if mac_lock is not None:
            env["LOCK_DIR"] = str(mac_lock)
        return self._run(self.block, env)

    def test_free_lock_is_taken_and_released(self) -> None:
        lock = self.root / "free.lock"
        mac_lock = self.root / "mac.lock"
        mac_lock.mkdir()
        done = self._run_remote(lock, mac_lock)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("SAMPLED:", done.stdout, "the block did not sample the host")
        self.assertIn("PRUNED:", done.stdout, "the destructive commands did not run")
        self.assertFalse(lock.exists(), "the trap did not release the remote lock")
        self.assertFalse(mac_lock.exists(), "the trap did not release the Mac lock")

    def test_peer_holding_the_lock_stops_the_run(self) -> None:
        lock = self.root / "peer.lock"
        lock.mkdir()
        mac_lock = self.root / "mac.lock"
        mac_lock.mkdir()
        done = self._run_remote(lock, mac_lock)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("skipped, a peer holds the remote lock", done.stdout)
        self.assertNotIn("SAMPLED:", done.stdout, "the block sampled the host")
        self.assertNotIn("PRUNED:", done.stdout, "the destructive commands ran")
        self.assertTrue(lock.is_dir(), "the peer's lock was removed")
        self.assertTrue(mac_lock.is_dir(), "the Mac lock was removed")

    def test_lock_that_cannot_be_made_fails_closed(self) -> None:
        parent = self.root / "not-a-directory"
        parent.write_text("x", encoding="utf-8")
        lock = parent / "lock"
        done = self._run_remote(lock)
        self.assertNotEqual(done.returncode, 0, "the block exited 0 on a broken lock")
        self.assertNotIn("SAMPLED:", done.stdout, "the block sampled the host")
        self.assertNotIn("PRUNED:", done.stdout, "the destructive commands ran")
        self.assertIn(
            "cannot create the remote lock directory",
            done.stderr,
            "the failure did not name what went wrong",
        )
        self.assertIn(
            "Errno",
            done.stderr,
            "the original error did not survive the re-raise",
        )


class MacLockBehaviourTest(_LockRunTest):
    """The Mac block takes the local lock and releases it."""

    def setUp(self) -> None:
        super().setUp()
        self.block = _mac_block(_canonical_text())

    def _run_mac(self, lock: Path):
        return self._run(self.block, {"HOUSEKEEPER_LOCK": str(lock)})

    def test_free_lock_is_taken_and_released(self) -> None:
        lock = self.root / "free.lock"
        done = self._run_mac(lock)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertFalse(lock.exists(), "the trap did not release the lock")

    def test_peer_holding_the_lock_stops_the_run(self) -> None:
        lock = self.root / "peer.lock"
        lock.mkdir()
        done = self._run_mac(lock)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("skipped, peer holds lock", done.stdout)
        self.assertTrue(lock.is_dir(), "the peer's lock was removed")

    def test_lock_that_cannot_be_made_fails_closed(self) -> None:
        parent = self.root / "not-a-directory"
        parent.write_text("x", encoding="utf-8")
        lock = parent / "lock"
        done = self._run_mac(lock)
        self.assertNotEqual(done.returncode, 0, "the block exited 0 on a broken lock")
        self.assertIn(
            "cannot create the lock directory",
            done.stderr,
            "the failure did not name what went wrong",
        )
        self.assertIn(
            "Errno",
            done.stderr,
            "the original error did not survive the re-raise",
        )


if __name__ == "__main__":
    unittest.main()
