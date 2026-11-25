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


def shell_branch(text: str, opener: str) -> tuple[str, str]:
    """The shell branch that `opener` opens, and the text before it.

    The branch closes on the shell's own `fi` keyword, which the script writes on a
    line of its own.  Cutting on a bare `fi` substring would end the branch at any
    word that happens to hold those two letters, and the assertions would then
    enforce only a fragment of the branch.
    """
    assert text.count(opener) == 1, "the opener must appear exactly once"
    before, after = text.split(opener)
    lines = after.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "fi" or stripped.startswith("fi ") or stripped.startswith("fi;"):
            return before, "\n".join(lines[:index])
    raise AssertionError("the branch that %r opens never closes" % opener)


def function_body(text: str, name: str) -> str:
    """The body of a shell function, up to its closing brace or paren on a line of its own.

    A shell function body is not a branch, so it closes on `}` or `)` rather than on
    `fi`;  a subshell function carries `(` in place of `{`.
    """
    start = text.index(f"{name}() ")
    lines = text[start:].splitlines()
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in ("}", ")"):
            return "\n".join(lines[1:index])
    raise AssertionError("the function %s never closes" % name)


class ShellBranchTests(unittest.TestCase):
    """The branch parser, on its own:  the shell's `fi` is the only boundary."""

    OPENER = 'if [ ! -f "$STATE_DIR/runtime-public-source.approved" ]; then'

    def test_a_word_holding_fi_inside_the_branch_does_not_cut_it(self) -> None:
        script = (
            "#!/bin/bash\n"
            "main() {\n"
            '  if [ ! -f "$STATE_DIR/runtime-public-source.approved" ]; then\n'
            '    retrieval="origin"   # the verified file name\n'
            "    fetch_public_runtime() { rgit fetch -q origin; }\n"
            "  fi\n"
            "}\n"
            "main \"$@\"\n"
        )
        before, branch = shell_branch(script, self.OPENER)
        self.assertIn('retrieval="origin"', branch)
        self.assertIn("fetch -q origin", branch)
        # The text before the opener keeps the rest of the script, never the branch.
        self.assertNotIn("fetch -q origin", before)

    def test_the_branch_ends_at_the_shells_own_fi(self) -> None:
        script = (
            "main() {\n"
            '  if [ ! -f "$STATE_DIR/runtime-public-source.approved" ]; then\n'
            "    true\n"
            "  fi\n"
            "  after_the_branch\n"
            "}\n"
        )
        before, branch = shell_branch(script, self.OPENER)
        self.assertIn("true", branch)
        # The branch stops at the closing fi, so what follows it is not part of it.
        self.assertNotIn("after_the_branch", branch)
        self.assertIn("main() {", before)

    def test_a_branch_that_never_closes_is_refused(self) -> None:
        script = (
            "main() {\n"
            '  if [ ! -f "$STATE_DIR/runtime-public-source.approved" ]; then\n'
            "    true\n"
            "}\n"
        )
        with self.assertRaises(AssertionError):
            shell_branch(script, self.OPENER)

    def test_a_function_that_never_closes_is_refused(self) -> None:
        with self.assertRaises(AssertionError):
            function_body("rgit() {\n  true\n", "rgit")


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
        before, branch = shell_branch(
            text, 'if [ ! -f "$STATE_DIR/runtime-public-source.approved" ]; then'
        )
        self.assertIn('retrieval="origin"', branch)
        # The transport is chosen before the run lock is taken, so the fetch itself stays
        # inside the lock and the branch itself carries no fetch.
        self.assertIn('if [ "$retrieval" = "origin" ]; then', text)
        self.assertIn("if ! rgit fetch -q origin", text)
        self.assertEqual(text.count("fetch -q origin"), 1)
        self.assertNotIn("fetch -q origin", before)

    def test_the_public_source_transport_is_fixed_and_never_falls_back(self) -> None:
        """The approved transport is one fixed public source, and its failure does not fall back."""
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(text.count(PUBLIC_URL), 1)
        self.assertIn(
            'log "FETCH-FAILED  fixed public-source retrieval failed; no credential fallback"',
            text,
        )
        # Nothing after the approved branch reaches the origin transport.
        approved = text.index("elif ! fetch_public_runtime; then")
        self.assertNotIn("fetch -q origin", text[approved:])

    def test_the_origin_transport_closes_every_prompt_path(self) -> None:
        """A launchd run has no terminal, so every prompt must be closed.

        Git's own prompt is one of several:  an SSH passphrase, an SSH host key and a GCM
        interaction each ask on their own, and each one would hold the whole alarm on
        every tick.  The guards live in the git wrapper, so every call is closed and a
        later call site cannot drop one silently.
        """
        text = SCRIPT.read_text(encoding="utf-8")
        wrapper = function_body(text, "rgit")
        for guard in (
            "GIT_TERMINAL_PROMPT=0",
            "GCM_INTERACTIVE=never",
            "GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes'",
        ):
            self.assertIn(guard, wrapper)

    def test_the_origin_transport_keeps_the_host_credential_helper(self) -> None:
        """A credential helper is how an origin that needs one authenticates.

        Clearing it in the wrapper would make that fetch fail instead of updating, so the
        helper is cleared in the isolated public-source path instead, where no host
        credential is wanted at all.
        """
        text = SCRIPT.read_text(encoding="utf-8")
        wrapper = function_body(text, "rgit")
        self.assertNotIn("credential.helper", wrapper)
        isolated = function_body(text, "fetch_public_runtime")
        self.assertIn("-c credential.helper=", isolated)

    def test_the_approved_transport_reads_no_host_credential(self) -> None:
        """The approved transport sees no host Git config, no credential and no agent."""
        text = SCRIPT.read_text(encoding="utf-8")
        body = function_body(text, "fetch_public_runtime")
        for guard in (
            "env -i HOME=",
            "GIT_CONFIG_NOSYSTEM=1",
            "GIT_CONFIG_GLOBAL=/dev/null",
            "GIT_TERMINAL_PROMPT=0",
            "-c credential.helper=",
            "-c core.hooksPath=/dev/null",
        ):
            self.assertIn(guard, body)

    def test_the_script_parses(self) -> None:
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
