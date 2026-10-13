"""End to end through a real process: the entry script, a real environment, SIGTERM."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from agent_sync.tests.harness import Harness

ENTRY = Path(__file__).resolve().parents[2] / "agent-sync"


class ProcessTests(Harness):
    def process_environment(self) -> dict[str, str]:
        found = self.env()
        found["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
        return found

    def spawn(self, *argv: str) -> subprocess.Popen[str]:
        return subprocess.Popen([sys.executable, str(ENTRY), *argv], env=self.process_environment(), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def run_entry(self, *argv: str) -> subprocess.CompletedProcess[str]:
        done = subprocess.run([sys.executable, str(ENTRY), *argv], env=self.process_environment(), text=True,
                              capture_output=True, timeout=30)
        self.transcript.extend([done.stdout, done.stderr])
        return done

    def wait_until_registered(self) -> None:
        deadline = time.monotonic() + 30
        while (not self.fake.queues or not self.fake.requests_to("GET", "messages")) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.fake.queues, "the command never registered a queue")

    def wait_until_polling(self) -> None:
        """The client has stored its queue id and opened the long poll: SIGTERM now must delete the queue."""
        deadline = time.monotonic() + 30
        while not self.fake.requests_to("GET", "events") and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.fake.requests_to("GET", "events"), "the command never opened its long poll")

    def test_post_through_the_entry_script(self) -> None:
        done = self.run_entry("post", "--topic", "t", "hello from a process")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("hello from a process", self.fake.messages[-1]["content"])

    def test_missing_topic_exit_code_through_the_entry_script(self) -> None:
        done = self.run_entry("post", "hello")
        self.assertEqual(done.returncode, 2)
        self.assertEqual(self.fake.requests, [])

    def test_listen_prints_a_message_and_a_sigterm_ends_it_cleanly(self) -> None:
        process = self.spawn("listen", "--topic", "t")
        first = out = err = ""
        try:
            self.wait_until_registered()
            self.fake.add_message("Codex", "agent-sync", "t", "live line")
            first = "".join(process.stdout.readline() for _ in range(3))  # head, sender line, body
            self.assertIn("#agent-sync", first)
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.transcript.extend([first, out, err])
        self.assertEqual(process.returncode, 0, err)
        self.assertIn("live line", first + out)
        self.assertEqual(self.fake.deleted_queues, ["q1"])

    def test_wait_gets_sigterm_and_still_deletes_its_queue(self) -> None:
        self.fake.add_message("Codex", "agent-sync", "t", "seed")
        self.assertEqual(self.run_entry("read", "--topic", "t", "--new").returncode, 0)
        process = self.spawn("wait", "--topic", "t", "--timeout", "60")
        out = err = ""
        try:
            self.wait_until_polling()
            process.send_signal(signal.SIGTERM)
            out, err = process.communicate(timeout=30)
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
        self.transcript.extend([out, err])
        self.assertEqual(process.returncode, 130)
        self.assertEqual(self.fake.deleted_queues, ["q1"])


if __name__ == "__main__":
    unittest.main()
