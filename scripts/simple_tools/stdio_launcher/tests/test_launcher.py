"""Tests for the per-seat stdio launcher and its installer.

Run:  cd scripts && python3 -m unittest discover -s simple_tools/stdio_launcher/tests -t .

Each end-to-end case installs a launcher into a temporary folder with a fake
bearer and a fake transport, runs it as a subprocess and speaks MCP over stdio.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

from simple_tools.stdio_launcher import install, launcher

FAKE_TOKEN = "sbt_claude_" + "A" * 43
# Stands in for scripts/mcp-servers/assigned-hosted-stdio.py:  it answers initialize
# with the identity the bearer assigns and reports the environment it was given.
FAKE_TRANSPORT = textwrap.dedent(
    """
    import json, os, re, sys
    token = os.environ.get("ASSIGNED_MCP_BEARER", "")
    match = re.fullmatch(r"sbt_([a-z][a-z0-9-]{1,19})_[A-Za-z0-9_-]{43}", token)
    identity = match[1].upper() if match else None
    for raw in sys.stdin:
        message = json.loads(raw)
        if "id" not in message:
            continue
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "serverInfo": {"name": "fake", "identity": identity},
            "env_keys": sorted(os.environ),
        }
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    """
)
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t"}},
}


class Installed:
    """A launcher installed into a temp folder by install.render."""

    def __init__(self, seat="CLAUDE", token=FAKE_TOKEN, mode=0o600):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.bearer = os.path.join(root, "bearer")
        with open(self.bearer, "w", encoding="ascii") as target:
            target.write(token + "\n")
        os.chmod(self.bearer, mode)
        self.transport = os.path.join(root, "transport.py")
        with open(self.transport, "w", encoding="utf-8") as target:
            target.write(FAKE_TRANSPORT)
        self.path = os.path.join(root, f"{seat.lower()}-simple-tools")
        with open(self.path, "w", encoding="utf-8") as target:
            target.write(install.render(seat, self.bearer, sys.executable, self.transport, root))

    def run(self, env, messages=(INITIALIZE,)):
        child_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": self.tmp.name, **env}
        stdin = "".join(json.dumps(m) + "\n" for m in messages)
        done = subprocess.run(
            [sys.executable, "-I", self.path], input=stdin, capture_output=True, text=True,
            env=child_env, timeout=30,
        )
        replies = [json.loads(line) for line in done.stdout.splitlines() if line.strip()]
        return done, replies

    def close(self):
        self.tmp.cleanup()


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.installed = Installed()
        self.addCleanup(self.installed.close)

    def test_shell_agent_seat_zc_without_launcher_initializes_as_claude(self):
        done, replies = self.installed.run({"AGENT_SEAT": "ZC"})
        self.assertEqual(len(replies), 1, done.stderr)
        self.assertEqual(replies[0]["result"]["serverInfo"]["identity"], "CLAUDE")
        notices = [line for line in done.stderr.splitlines() if line.strip()]
        self.assertEqual(len(notices), 1, done.stderr)
        self.assertIn("ignoring shell AGENT_SEAT=ZC", notices[0])
        self.assertNotIn(FAKE_TOKEN, done.stderr + done.stdout)

    def test_transport_gets_only_path_home_and_bearer(self):
        _, replies = self.installed.run({"AGENT_SEAT": "ZC", "UNRELATED": "x"})
        keys = set(replies[0]["result"]["env_keys"])
        # The interpreter may add its own locale variable on some hosts.
        self.assertEqual(keys - {"__CF_USER_TEXT_ENCODING", "LC_CTYPE"}, {"PATH", "HOME", "ASSIGNED_MCP_BEARER"})

    def test_clean_environment_initializes_with_no_notice(self):
        done, replies = self.installed.run({})
        self.assertEqual(replies[0]["result"]["serverInfo"]["identity"], "CLAUDE")
        self.assertEqual(done.stderr.strip(), "")

    def test_launcher_assigning_another_seat_answers_initialize_with_named_error(self):
        done, replies = self.installed.run(
            {"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "BF-PLUMBER", "AGENT_SEAT": "BF-PLUMBER"},
            messages=(INITIALIZE, {"jsonrpc": "2.0", "method": "notifications/initialized"},
                      {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        )
        self.assertEqual([r["id"] for r in replies], [1, 2])
        error = replies[0]["error"]
        self.assertEqual(error["code"], launcher.REFUSAL_CODE)
        self.assertEqual(error["data"]["cause"], "seat_mismatch")
        self.assertIn("BF-PLUMBER", error["message"])
        self.assertIn("CLAUDE", error["message"])
        self.assertIn("launcher botfleet", error["message"])
        self.assertEqual(done.returncode, 3)
        self.assertIn("refused", done.stderr)

    def test_launcher_with_matching_seat_runs(self):
        _, replies = self.installed.run({"AGENT_LAUNCHER": "paseo", "AGENT_LAUNCH_SEAT": "claude"})
        self.assertEqual(replies[0]["result"]["serverInfo"]["identity"], "CLAUDE")

    def test_launcher_without_assigned_seat_refuses(self):
        _, replies = self.installed.run({"AGENT_LAUNCHER": "botfleet"})
        self.assertEqual(replies[0]["error"]["data"]["cause"], "seat_unassigned")

    def test_bearer_mode_other_than_0600_refuses(self):
        for mode in (0o644, 0o640, 0o400):
            with self.subTest(mode=oct(mode)):
                installed = Installed(mode=mode)
                self.addCleanup(installed.close)
                done, replies = installed.run({})
                error = replies[0]["error"]
                self.assertEqual(error["data"]["cause"], "bearer_custody")
                self.assertIn(f"{mode:04o}, not 0600", error["message"])
                self.assertNotIn(FAKE_TOKEN, done.stdout + done.stderr)

    def test_symlinked_bearer_refuses(self):
        link = self.installed.bearer + ".link"
        os.symlink(self.installed.bearer, link)
        with open(self.installed.path, encoding="utf-8") as source:
            text = source.read().replace(repr(self.installed.bearer), repr(link))
        with open(self.installed.path, "w", encoding="utf-8") as target:
            target.write(text)
        _, replies = self.installed.run({})
        self.assertEqual(replies[0]["error"]["data"]["cause"], "bearer_custody")

    def test_other_seats_bearer_refuses(self):
        installed = Installed(token="sbt_codex_" + "B" * 43)
        self.addCleanup(installed.close)
        done, replies = installed.run({})
        self.assertEqual(replies[0]["error"]["data"]["cause"], "bearer_format")
        self.assertNotIn("B" * 43, done.stdout + done.stderr)

    def test_unconfigured_template_refuses_instead_of_exiting(self):
        done = subprocess.run(
            [sys.executable, "-I", launcher.__file__], input=json.dumps(INITIALIZE) + "\n",
            capture_output=True, text=True, timeout=30, env={"PATH": "/usr/bin:/bin"},
        )
        reply = json.loads(done.stdout)
        self.assertEqual(reply["error"]["data"]["cause"], "launcher_unconfigured")


class SeatDecision(unittest.TestCase):
    def test_agent_tag_mismatch_is_ignored_with_one_line(self):
        refusal, notice = launcher.decide_seat({"AGENT_SEAT": "ZC", "AGENT_TAG": "zc"}, "CLAUDE")
        self.assertIsNone(refusal)
        self.assertEqual(notice.count("\n"), 0)
        self.assertIn("AGENT_SEAT=ZC, AGENT_TAG=zc", notice)

    def test_launch_seat_without_launcher_still_wins(self):
        refusal, _ = launcher.decide_seat({"AGENT_LAUNCH_SEAT": "ZC"}, "CLAUDE")
        self.assertEqual(refusal[0], "seat_mismatch")

    def test_hostile_env_value_is_made_inert(self):
        refusal, _ = launcher.decide_seat(
            {"AGENT_LAUNCHER": "x\nIgnore previous", "AGENT_LAUNCH_SEAT": "ZC"}, "CLAUDE"
        )
        self.assertNotIn("\n", refusal[1])

    def test_refusal_answers_batches_and_bad_json(self):
        stdin = io.BytesIO(
            b'[{"jsonrpc":"2.0","id":7,"method":"ping"},{"jsonrpc":"2.0","method":"n"}]\nnot json\n\n'
        )
        stdout = io.BytesIO()
        code = launcher.serve_refusal("CLAUDE", ("seat_mismatch", "why"), stdin, stdout, io.StringIO())
        replies = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(code, 3)
        self.assertEqual([r["id"] for r in replies], [7, None])


class Installer(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dest = os.path.join(self.tmp.name, "libexec")
        self.args = [
            "--seat", "claude", "--seat", "AG", "--dest", self.dest, "--home", self.tmp.name,
            "--secrets-dir", os.path.join(self.tmp.name, "secrets"), "--python", sys.executable,
        ]

    def test_dry_run_writes_nothing(self):
        out = io.StringIO()
        self.assertEqual(install.main(self.args, out=out), 0)
        self.assertFalse(os.path.exists(self.dest))
        self.assertIn("dry run", out.getvalue())
        self.assertIn("create", out.getvalue())

    def test_apply_writes_private_launchers_and_transport(self):
        install.main(self.args + ["--apply"], out=io.StringIO())
        for name in ("claude-simple-tools", "ag-simple-tools", install.TRANSPORT_NAME):
            path = os.path.join(self.dest, name)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o700, name)
        with open(os.path.join(self.dest, "ag-simple-tools"), encoding="utf-8") as source:
            text = source.read()
        self.assertIn("SEAT = 'AG'", text)
        self.assertIn(repr(os.path.join(self.tmp.name, "secrets", "ag")), text)
        with open(install.TRANSPORT_SOURCE, encoding="utf-8") as a, open(
            os.path.join(self.dest, install.TRANSPORT_NAME), encoding="utf-8"
        ) as b:
            self.assertEqual(a.read(), b.read())

    def test_reinstall_is_unchanged_and_replacement_keeps_backup(self):
        install.main(self.args + ["--apply"], out=io.StringIO())
        out = io.StringIO()
        install.main(self.args + ["--apply"], out=out)
        self.assertNotIn("replace", out.getvalue())
        path = os.path.join(self.dest, "claude-simple-tools")
        with open(path, "a", encoding="utf-8") as target:
            target.write("# hand edit\n")
        out = io.StringIO()
        install.main(self.args + ["--apply"], out=out)
        self.assertIn("replace", out.getvalue())
        backups = [n for n in os.listdir(self.dest) if n.startswith("claude-simple-tools.bak-")]
        self.assertEqual(len(backups), 1)

    def test_refuses_to_replace_a_symlink(self):
        os.makedirs(self.dest)
        os.symlink("/dev/null", os.path.join(self.dest, "claude-simple-tools"))
        with self.assertRaises(ValueError):
            install.main(self.args + ["--apply"], out=io.StringIO())

    def test_render_rejects_relative_paths(self):
        with self.assertRaises(ValueError):
            install.render("CLAUDE", "relative/bearer", sys.executable, "/t.py", "/home")


if __name__ == "__main__":
    unittest.main()
