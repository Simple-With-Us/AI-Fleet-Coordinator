"""The runtime sync transport:  a host without the operator's approval file still updates.

The fixed public-source retrieval is the operator-approved transport for
``scripts/simple-tools-runtime-sync.sh``.  Before the approval file exists the job keeps the
transport it used before (the runtime's own ``origin``), so a host without the file never
stands still behind main with nothing but a log line (AFC-906).

The behaviour is checked against a scratch runtime, a scratch published repository and a
scratch ``HOME``:  the real runtime worktree, the real state directory and the real log are
never touched, and no test run reaches the network.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "simple-tools-runtime-sync.sh"
PUBLIC_URL = "https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git"


def git(*arguments: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *arguments],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=60,
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(arguments)} failed: {result.stderr}")
    return result.stdout.strip()


class RuntimeSyncTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="runtime-sync-transport-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)], check=False)

        self.home = self.tmp / "home"
        self.home.mkdir()
        self.state = self.home / ".simple-tools"
        self.state.mkdir()
        self.log = self.tmp / "sync.log"

        # The published repository the runtime follows, as a local bare remote.
        self.origin = self.tmp / "origin.git"
        git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=self.tmp)
        self.seed = self.tmp / "seed"
        git("init", "-q", "-b", "main", str(self.seed), cwd=self.tmp)
        git("config", "user.email", "seat@example.invalid", cwd=self.seed)
        git("config", "user.name", "seat", cwd=self.seed)
        (self.seed / "README.md").write_text("one\n", encoding="utf-8")
        git("add", "README.md", cwd=self.seed)
        git("commit", "-q", "-m", "one", cwd=self.seed)
        git("remote", "add", "origin", str(self.origin), cwd=self.seed)
        git("push", "-q", "origin", "main", cwd=self.seed)

        # The runtime worktree the job updates.
        self.runtime = self.tmp / "runtime"
        git("clone", "-q", str(self.origin), str(self.runtime), cwd=self.tmp)
        git("config", "user.email", "seat@example.invalid", cwd=self.runtime)
        git("config", "user.name", "seat", cwd=self.runtime)

        # A commit the published repository moved to, which the runtime has not seen.
        (self.seed / "README.md").write_text("two\n", encoding="utf-8")
        git("commit", "-q", "-am", "two", cwd=self.seed)
        git("push", "-q", "origin", "main", cwd=self.seed)
        self.second = git("rev-parse", "HEAD", cwd=self.seed)

    def run_sync(self) -> subprocess.CompletedProcess[str]:
        environment = {
            "HOME": str(self.home),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SIMPLE_TOOLS_RUNTIME": str(self.runtime),
            "SIMPLE_TOOLS_RUNTIME_SYNC_LOG": str(self.log),
        }
        return subprocess.run(
            ["bash", str(SCRIPT)],
            capture_output=True,
            text=True,
            env=environment,
            timeout=180,
        )

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def test_without_the_approval_file_the_job_still_retrieves(self) -> None:
        """The finding this fixes:  a host with no approval file must not stand still."""
        self.assertFalse((self.state / "runtime-public-source.approved").exists())
        self.run_sync()

        # The retrieval really happened:  the runtime's origin/main moved to the new commit.
        # Under the old gate the run exited before any fetch and this stayed at the first commit.
        self.assertEqual(
            git("rev-parse", "refs/remotes/origin/main", cwd=self.runtime),
            self.second,
        )
        self.assertIn("PUBLIC-SOURCE-NOT-APPROVED", self.log_text())
        self.assertNotIn("adoption is held", self.log_text())

    def test_the_approval_file_is_what_selects_the_public_source_transport(self) -> None:
        """The public-source transport is the approved default;  the origin one is the fallback."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('local retrieval="public-source"', text)
        not_approved = text.split('if [ ! -f "$STATE_DIR/runtime-public-source.approved" ]; then')
        self.assertEqual(len(not_approved), 2)
        branch = not_approved[1].split("fi")[0]
        self.assertIn('retrieval="origin"', branch)
        self.assertIn("fetch -q origin", branch)
        # The origin transport exists nowhere else:  never as a fallback after a failure.
        self.assertNotIn("fetch -q origin", not_approved[0])

    def test_the_public_source_transport_is_fixed_and_never_falls_back(self) -> None:
        """The approved transport is one fixed public source, and its failure does not fall back."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(text.count(PUBLIC_URL), 1)
        self.assertIn(
            'log "FETCH-FAILED  fixed public-source retrieval failed; no credential fallback"',
            text,
        )

    def test_the_fallback_transport_never_waits_on_a_prompt(self) -> None:
        """A launchd run has no terminal, so every interactive path must be closed.

        Git's own prompt is one of several:  an SSH passphrase, an SSH host key and a
        credential helper each ask on their own, and each one would hold the whole alarm
        on every tick.  The fallback closes all of them at once.
        """
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never "
            "GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes' "
            "rgit -c credential.helper= fetch -q origin;",
            text,
        )
        # Each guard is present on its own, so a later edit cannot drop one silently.
        for guard in (
            "GIT_TERMINAL_PROMPT=0",
            "GCM_INTERACTIVE=never",
            "GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes'",
            "rgit -c credential.helper= fetch -q origin",
        ):
            self.assertIn(guard, text)

    def test_the_fallback_guards_exist_only_inside_the_not_approved_branch(self) -> None:
        """The approved transport is fixed and never borrows the fallback's guards."""
        text = SCRIPT.read_text(encoding="utf-8")
        parts = text.split('if [ ! -f "$STATE_DIR/runtime-public-source.approved" ]; then')
        self.assertEqual(len(parts), 2)
        self.assertIn("GIT_SSH_COMMAND", parts[1].split("fi")[0])
        self.assertNotIn("GIT_SSH_COMMAND", parts[0])

    def test_the_script_parses(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
