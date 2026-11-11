"""Offline checks for the Claude wake router installer and canary (OPS-1048, AFC-694).

No network beyond loopback, no Infisical, no systemd change and no real router.  The installer runs
in its test mode (CLAUDE_WAKE_INSTALL_TEST_ROOT), with every destination under a temporary root and
systemctl and the knob reader as recording stubs.  The canary runs against the router's real HTTP
handler (wake_router.make_handler) on a loopback port, with the router behind it stubbed, so the
body it sends is checked against the contract the router itself enforces.  Fixture keys and tokens
are built in parts at run time, so no credential-shaped literal sits in this file.

Run:  cd scripts && python3 -m unittest simple_tools.tests.test_wake_router_install -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from simple_tools import wake_router as WR

SERVER = Path(__file__).resolve().parents[1] / "server"
INSTALL = SERVER / "install-claude-wake-router.sh"
CANARY = SERVER / "claude-wake-canary.sh"
UNIT = SERVER / "claude-wake-router.service"
UNIT_NAME = "afc-claude-wake-router.service"
FAKE_TOKEN = "st" + "." + "fixture" + "-not-a-real-token-" + "0123456789"
FAKE_KEY = "fixture" + "-routine-" + "key-" + "0123456789abcdef"

SYSTEMCTL_STUB = """#!/usr/bin/env bash
echo "systemctl $*" >> "$STUB_LOG"
case "$1" in
  is-active) [[ "${STUB_ACTIVE:-0}" == 1 ]] && exit 0; exit 3 ;;
  is-enabled) echo disabled; exit 1 ;;
esac
exit 0
"""
KNOB_STUB = """#!/usr/bin/env bash
echo "knob-read" >> "$STUB_LOG"
printf '%s\\n' "$STUB_KNOBS"
"""


def knobs(enabled="false", seats="CLAUDE", source="infisical", stale=False):
    def row(value, src=source):
        return {"source": src, "stale": stale, "value": value}

    return json.dumps(
        {
            "WAKE_ROUTER_DELIVERY_ENABLED": row(enabled),
            "WAKE_ROUTER_DELIVERY_SEATS": row(seats),
            "WAKE_ROUTER_MAC_SSH": row("mac-wake"),
        }
    )


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="claude-wake-install-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        (self.root / "bin").mkdir()
        for name, body in (("systemctl", SYSTEMCTL_STUB), ("knob-read", KNOB_STUB)):
            path = self.root / "bin" / name
            path.write_text(body)
            path.chmod(0o755)
        runtime = self.root / "home/jay/.local/share/fleet-cli/current/scripts/simple-chat"
        runtime.parent.mkdir(parents=True)
        runtime.write_text('if sys.argv[1] == "wake-router": pass\n')
        self.env_file = self.root / "etc/claude-wake-router.env"
        self.env_file.parent.mkdir(parents=True)
        self.write_env("FLEET_KNOBS_INFISICAL_TOKEN=%s\n" % FAKE_TOKEN)
        self.key = self.root / "home/jay/.local/state/claude-wake/routine-key"
        self.key.parent.mkdir(parents=True)
        self.key.write_text(FAKE_KEY + "\n")
        self.key.chmod(0o600)
        self.log = self.root / "stub.log"
        self.unit = self.root / "etc/systemd/system" / UNIT_NAME

    def write_env(self, text, mode=0o600):
        self.env_file.write_text(text)
        self.env_file.chmod(mode)

    def run_install(self, *args, stub_knobs=None, active=False, test_root=True):
        env = dict(os.environ)
        env.pop("CLAUDE_WAKE_INSTALL_TEST_ROOT", None)
        if test_root:
            env["CLAUDE_WAKE_INSTALL_TEST_ROOT"] = str(self.root)
        env["STUB_LOG"] = str(self.log)
        env["STUB_KNOBS"] = knobs() if stub_knobs is None else stub_knobs
        env["STUB_ACTIVE"] = "1" if active else "0"
        done = subprocess.run(
            ["bash", str(INSTALL), *args], env=env, capture_output=True, text=True, timeout=60
        )
        out = done.stdout + done.stderr
        self.assertNotIn(FAKE_TOKEN, out)
        self.assertNotIn(FAKE_KEY, out)
        return done.returncode, out

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    # ---- the plan ---------------------------------------------------------------------------

    def test_syntax(self):
        for script in (INSTALL, CANARY):
            done = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_dry_run_on_this_checkout_exits_zero_and_writes_nothing(self):
        code, out = self.run_install("--dry-run", test_root=False)
        self.assertEqual(code, 0, out)
        self.assertIn("dry run: nothing will be changed", out)
        self.assertIn("afc-claude-wake-router.service", out)
        self.assertIn("dry run done:", out)

    def test_dry_run_in_test_root_writes_nothing(self):
        code, out = self.run_install("--dry-run", "--start")
        self.assertEqual(code, 0, out)
        self.assertFalse(self.unit.exists())
        self.assertIn("would install", out)
        self.assertIn("would run systemctl enable --now", out)
        self.assertFalse(any("daemon-reload" in c or "enable" in c for c in self.calls()))

    # ---- a real run -------------------------------------------------------------------------

    def test_install_copies_the_unit_and_never_starts_it(self):
        code, out = self.run_install()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.unit.read_bytes(), UNIT.read_bytes())
        self.assertEqual(self.unit.stat().st_mode & 0o777, 0o644)
        self.assertIn("systemctl daemon-reload", self.calls())
        self.assertFalse(any(c.startswith("systemctl enable") for c in self.calls()))
        self.assertIn("delivery for CLAUDE would be held", out)
        self.assertIn("not started by this script", out)

    def test_second_run_is_unchanged(self):
        self.assertEqual(self.run_install()[0], 0)
        self.log.unlink()
        code, out = self.run_install()
        self.assertEqual(code, 0, out)
        self.assertIn("unchanged", out)
        self.assertNotIn("systemctl daemon-reload", self.calls())

    def test_start_enables_it(self):
        code, out = self.run_install("--start", stub_knobs=knobs(enabled="true"))
        self.assertEqual(code, 0, out)
        self.assertIn("systemctl enable --now " + UNIT_NAME, self.calls())
        self.assertIn("would be ARMED", out)

    def test_start_restarts_a_running_older_unit(self):
        code, out = self.run_install("--start", active=True)
        self.assertEqual(code, 0, out)
        self.assertIn("systemctl restart " + UNIT_NAME, self.calls())

    def test_armed_needs_claude_in_the_seats(self):
        code, out = self.run_install(stub_knobs=knobs(enabled="true", seats="CODEX"))
        self.assertEqual(code, 0, out)
        self.assertIn("would be held", out)

    # ---- refusals:  nothing is installed ----------------------------------------------------

    def assert_refused(self, fragment, **kwargs):
        code, out = self.run_install("--start", **kwargs)
        self.assertNotEqual(code, 0, out)
        self.assertIn(fragment, out)
        self.assertFalse(self.unit.exists())
        self.assertFalse(any("daemon-reload" in c or "enable" in c for c in self.calls()))

    def test_refuses_unset_delivery_knobs(self):
        self.assert_refused(
            "WAKE_ROUTER_DELIVERY_ENABLED is not set", stub_knobs=knobs(source="default")
        )

    def test_refuses_stale_knobs(self):
        self.assert_refused("could not be read", stub_knobs=knobs(stale=True))

    def test_refuses_unreadable_knobs(self):
        self.assert_refused("could not be read", stub_knobs="not json")

    def test_refuses_an_open_env_file(self):
        self.write_env("FLEET_KNOBS_INFISICAL_TOKEN=%s\n" % FAKE_TOKEN, mode=0o640)
        self.assert_refused("no group or other access")

    def test_refuses_an_env_file_without_the_token(self):
        self.write_env("CLAUDE_WAKE_BIND=127.0.0.1\n")
        self.assert_refused("has no FLEET_KNOBS_INFISICAL_TOKEN value")

    def test_refuses_a_missing_env_file(self):
        self.env_file.unlink()
        self.assert_refused("is missing")

    def test_refuses_a_wildcard_bind(self):
        self.write_env("FLEET_KNOBS_INFISICAL_TOKEN=%s\nCLAUDE_WAKE_BIND=0.0.0.0\n" % FAKE_TOKEN)
        self.assert_refused("never a wildcard")

    def test_refuses_the_zc_tls_port(self):
        self.write_env("FLEET_KNOBS_INFISICAL_TOKEN=%s\nCLAUDE_WAKE_PORT=8793\n" % FAKE_TOKEN)
        self.assert_refused("zc-wake-tls.service")

    def test_refuses_an_open_key(self):
        self.key.chmod(0o644)
        self.assert_refused("mode 0600 or 0400")

    def test_refuses_a_short_key(self):
        self.key.write_text("short\n")
        self.assert_refused("shorter than 16")

    def test_key_file_from_the_env_file(self):
        other = self.root / "srv/key"
        other.parent.mkdir()
        other.write_text(FAKE_KEY)
        other.chmod(0o600)
        self.key.unlink()
        self.write_env(
            "FLEET_KNOBS_INFISICAL_TOKEN=%s\nCLAUDE_WAKE_KEY_FILE=/srv/key\n" % FAKE_TOKEN
        )
        code, out = self.run_install()
        self.assertEqual(code, 0, out)
        self.assertIn("routine key ok: /srv/key", out)

    def test_refuses_a_missing_runtime(self):
        (self.root / "home/jay/.local/share/fleet-cli/current/scripts/simple-chat").unlink()
        self.assert_refused("install the fleet-cli runtime")


# ---- the canary ---------------------------------------------------------------------------


class StubRouter:
    """What make_handler reads from a Router:  the platform, the key file, and three answers."""

    def __init__(self, key_file, receipts):
        self.platform = SimpleNamespace(seat="CLAUDE", mention="Claude")
        self.key_file = key_file
        self.receipts = list(receipts)
        self.accepted = []

    def accept(self, wake):
        self.accepted.append(wake)
        return 202, {"wake_id": wake.wake_id, "receipt_id": "r1", "status": "accepted"}

    def receipt(self, wake_id):
        status = self.receipts.pop(0) if len(self.receipts) > 1 else self.receipts[0]
        return {
            "wake_id": wake_id,
            "receipt_id": "r1",
            "status": status,
            "picked": "letter" if status != "accepted" else "",
            "picked_letter": "AA",
            "session_id": "4984a346-0000-0000-0000-000000000000",
            "delivered": status == "delivered",
        }

    def test_answer(self, wake):
        self.accepted.append(wake)
        return {"wake_id": wake.wake_id, "test": True, "status": "test", "picked": "letter",
                "picked_letter": wake.letter, "delivered": False}


class CanaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="claude-wake-canary-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.key = self.tmp / "routine-key"
        self.key.write_text(FAKE_KEY + "\n")
        self.key.chmod(0o600)

    def serve(self, receipts=("accepted", "delivered")):
        router = StubRouter(self.key, receipts)
        server = ThreadingHTTPServer(("127.0.0.1", 0), WR.make_handler(router))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return router, "http://127.0.0.1:%d" % server.server_address[1]

    def canary(self, *args, url=None, key=None):
        argv = ["bash", str(CANARY), "--letter", "AA", "--message-id", "639559626", *args]
        if url:
            argv += ["--url", url]
        argv += ["--key-file", str(key or self.key)]
        env = dict(os.environ, CLAUDE_WAKE_CANARY_POLL_SECONDS="0.05")
        done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=60)
        out = done.stdout + done.stderr
        self.assertNotIn(FAKE_KEY, out)
        return done.returncode, out

    def wake_id(self, out):
        line = next(l for l in out.splitlines() if l.startswith("wake_id: "))
        return line.split(": ", 1)[1]

    def test_dry_run_sends_nothing(self):
        code, out = self.canary("--dry-run", url="http://127.0.0.1:9")
        self.assertEqual(code, 0, out)
        self.assertIn("dry run: nothing sent", out)
        self.assertIn('"contract": "simple-tools-wake/1"', out)

    def test_one_correlated_wake_to_one_letter(self):
        router, url = self.serve()
        code, out = self.canary(url=url)
        self.assertEqual(code, 0, out)
        wake_id = self.wake_id(out)
        self.assertEqual(len(router.accepted), 1)
        wake = router.accepted[0]
        self.assertEqual((wake.wake_id, wake.letter, wake.seat), (wake_id, "AA", "CLAUDE"))
        self.assertEqual(wake.message_id, 639559626)
        receipt = json.loads(next(l for l in out.splitlines() if l.startswith("receipt: "))[9:])
        self.assertEqual((receipt["wake_id"], receipt["status"]), (wake_id, "delivered"))
        self.assertIn("pick: ", out)

    def test_test_flag_delivers_nothing(self):
        router, url = self.serve()
        code, out = self.canary("--test", url=url)
        self.assertEqual(code, 0, out)
        self.assertIn('"test": true', out)
        self.assertEqual(router.accepted[0].letter, "AA")

    def test_held_and_dead_exit_codes(self):
        for status, expected in (("held", 3), ("dead", 4)):
            _, url = self.serve(receipts=(status,))
            self.assertEqual(self.canary(url=url)[0], expected, status)

    def test_wait_runs_out(self):
        _, url = self.serve(receipts=("accepted",))
        code, out = self.canary("--wait", "0", url=url)
        self.assertEqual(code, 5, out)
        self.assertIn("no final receipt", out)

    def test_wrong_key_is_refused(self):
        _, url = self.serve()
        other = self.tmp / "other"
        other.write_text("another" + "-fixture-" + "key-0123456789")
        other.chmod(0o600)
        code, out = self.canary(url=url, key=other)
        self.assertEqual(code, 1, out)
        self.assertIn('"http": 401', out)

    def test_open_key_file_is_refused(self):
        self.key.chmod(0o640)
        code, out = self.canary(url="http://127.0.0.1:9")
        self.assertEqual(code, 1, out)
        self.assertIn("no group or other access", out)

    def test_bad_arguments(self):
        done = subprocess.run(
            ["bash", str(CANARY), "--letter", "aa", "--message-id", "1", "--dry-run"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(done.returncode, 2, done.stderr)
        done = subprocess.run(["bash", str(CANARY), "--dry-run"], capture_output=True, text=True)
        self.assertEqual(done.returncode, 2, done.stderr)


if __name__ == "__main__":
    unittest.main()
