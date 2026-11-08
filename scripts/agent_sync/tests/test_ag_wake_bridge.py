"""The Antigravity wake bridge (AFC-665):  the webhook the Operator's http wake reaches, and the
deliverer that turns one wake into a prompt in a specific Antigravity conversation.  Every test
drives the bridge the way the fleet does:  the POST carries the agent-sync-wake/1 contract the
listener sends, the key lives in a 0600 file, the registry is a stub list, and the delivery
command is a fake that leaves its argv in a file, never a real `agy` run."""

from __future__ import annotations

import importlib.util
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from importlib.machinery import SourceFileLoader as _SFL
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[2] / "ag-wake-bridge"

_loader = _SFL("ag_wake_bridge_stub", str(BRIDGE))
_spec = importlib.util.spec_from_loader("ag_wake_bridge_stub", _loader)
assert _spec is not None
B = importlib.util.module_from_spec(_spec)
sys.modules["ag_wake_bridge_stub"] = B
_loader.exec_module(B)

SUMMARIES_DDL = (
    "CREATE TABLE conversation_summaries ("
    " conversation_id text primary key,"
    " title text not null default '',"
    " parent_conversation_id text,"
    " nesting_depth integer default 0,"
    " killed integer default 0)"
)

CID_A = "4aade78e-6a38-40ed-9fc2-11a6eeac6189"
CID_B = "cd6b94bc-8497-4f06-974b-9e247440c923"
CID_SUB = "11111111-2222-4333-8444-555555555555"
CID_MAC = "22222222-3333-4444-8555-666666666666"


def run_bridge(
    argv: list[str], env: dict[str, str], stdin: bytes = b""
) -> subprocess.CompletedProcess:
    full_env = {"PATH": "/usr/bin:/bin", "HOME": env.get("HOME", ""), **env}
    return subprocess.run(
        [sys.executable, "-I", str(BRIDGE), *argv],
        env=full_env,
        input=stdin,
        capture_output=True,
        timeout=30,
    )


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.join(self.tmp.name, "home")
        self.state = os.path.join(self.tmp.name, "ag-wake")
        os.makedirs(self.home)
        self.key = secrets.token_hex(24)
        os.makedirs(self.state, mode=0o700)
        self.key_file = os.path.join(self.state, "routine-key")
        with open(self.key_file, "w", encoding="utf-8") as fh:
            fh.write(self.key + "\n")
        os.chmod(self.key_file, 0o600)
        self.fake_path = os.path.join(self.tmp.name, "fake-out.txt")
        self.config = {
            "delivery_enabled": True,
            "command_template": [
                "/bin/sh",
                "-c",
                "printenv AGENT_LAUNCHER >> '%s'; "
                "printf '%%s %%s\\n' \"$1\" \"$2\" >> '%s'" % (self.fake_path, self.fake_path),
                "bridge",
                "{session_id}",
                "{wake_text}",
            ],
            "timeout_seconds": 30,
            "target_host": "server",
        }
        self.config_file = os.path.join(self.state, "config.json")
        with open(self.config_file, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh)
        self.store = os.path.join(self.home, ".gemini", "antigravity-cli", "conversation_summaries.db")
        os.makedirs(os.path.dirname(self.store))
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(SUMMARIES_DDL)
        conn.close()

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": self.home,
            "AG_WAKE_STATE": self.tmp.name,
            "AG_WAKE_KEY_FILE": self.key_file,
            "AG_WAKE_CONFIG": self.config_file,
            "AG_WAKE_STORE": self.store,
        }
        env.update(extra)
        return env

    def fake_out(self) -> str:
        return self.fake_path

    def rows(self, *rows: dict) -> list[dict]:
        return list(rows)


class Receiver(Base):
    def post(
        self,
        payload: dict | bytes,
        env: dict[str, str],
        key: str | None = None,
        port: int = 0,
        config: dict | None = None,
    ) -> tuple[int, dict]:
        import threading
        from http.server import ThreadingHTTPServer

        received: list[dict] = []
        server = ThreadingHTTPServer(
            ("127.0.0.1", port), B.make_handler(env, config or self.config, received)
        )
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        host, bound = server.server_address[:2]
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Authorization"] = "Bearer %s" % key
        request = urllib.request.Request(
            "http://%s:%d/wake/AG" % (host, bound), data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def wake(self, **over) -> dict:
        payload = {
            "contract": "agent-sync-wake/1",
            "seat": "AG",
            "wake_id": "w-0001",
            "topic": "AFC-665 Antigravity Operator setup",
            "channel": "work-in-progress",
            "zulip_link": "https://zulip.example/#/topic/1/AFC-665",
        }
        payload.update(over)
        return payload

    def registry_env(self, rows: list[dict]) -> dict[str, str]:
        path = os.path.join(self.tmp.name, "history.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"items": rows, "count": len(rows)}, fh)
        return {"AG_WAKE_HISTORY_FILE": path}

    def test_health(self) -> None:
        import threading
        from http.server import ThreadingHTTPServer

        server = ThreadingHTTPServer(("127.0.0.1", 0), B.make_handler(self.env(), self.config, []))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        host, bound = server.server_address[:2]
        with urllib.request.urlopen("http://%s:%d/health" % (host, bound), timeout=5) as response:
            answer = json.loads(response.read().decode())
        self.assertEqual(answer, {"ok": True, "seat": "AG"})

    def test_good_wake_is_accepted_and_deduped(self) -> None:
        env = self.env(
            **self.registry_env(
                self.rows(
                    {
                        "native_session_id": CID_A,
                        "title": "AFC-665 Antigravity Operator setup",
                        "verified_host": "server",
                        "state": "open",
                        "assigned_at": 2,
                        "retired_at": None,
                        "closed_at": None,
                    },
                )
            )
        )
        status, answer = self.post(self.wake(), env, key=self.key)
        self.assertEqual(status, 200)
        self.assertTrue(answer["delivered"], answer)
        self.assertEqual(answer["picked"], "topic-match")
        with open(self.fake_out()) as fh:
            seen = fh.read().split()
        self.assertIn(CID_A, seen)
        self.assertIn("AFC-665", seen)
        status, answer = self.post(self.wake(), env, key=self.key)
        self.assertEqual(status, 200)
        self.assertEqual(answer["reason"], "already-seen")

    def test_bad_key_is_401_and_missing_key_is_503(self) -> None:
        env = self.env()
        status, _ = self.post(self.wake(), env, key="wrong")
        self.assertEqual(status, 401)
        os.chmod(self.key_file, 0o644)
        status, _ = self.post(self.wake(), env, key=self.key)
        self.assertEqual(status, 503)
        os.chmod(self.key_file, 0o600)

    def test_contract_and_seat_are_checked(self) -> None:
        env = self.env()
        status, _ = self.post(self.wake(contract="other/9"), env, key=self.key)
        self.assertEqual(status, 400)
        status, _ = self.post(self.wake(seat="ZC"), env, key=self.key)
        self.assertEqual(status, 403)
        status, _ = self.post(self.wake(wake_id="bad id!"), env, key=self.key)
        self.assertEqual(status, 400)

    def test_non_uuid_target_is_refused(self) -> None:
        env = self.env(
            **self.registry_env(
                self.rows(
                    {
                        "native_session_id": "-p",
                        "title": "Evil",
                        "verified_host": "server",
                        "state": "open",
                        "assigned_at": 9,
                        "retired_at": None,
                        "closed_at": None,
                    },
                )
            )
        )
        status, answer = self.post(self.wake(), env, key=self.key)
        self.assertEqual(status, 503, answer)
        self.assertEqual(answer.get("reason"), "no-live-session")
        self.assertFalse(os.path.exists(self.fake_path))


class TargetPick(Base):
    def test_topic_match_beats_recency_and_skips_subagents_and_offhost(self) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO conversation_summaries (conversation_id, parent_conversation_id, nesting_depth, killed)"
                " VALUES (?, ?, 1, 0)",
                (CID_SUB, CID_A),
            )
        conn.close()
        rows = self.rows(
            {
                "native_session_id": CID_SUB,
                "title": "AFC-665 Antigravity Operator setup",
                "verified_host": "server",
                "state": "open",
                "assigned_at": 30,
                "retired_at": None,
                "closed_at": None,
            },
            {
                "native_session_id": CID_MAC,
                "title": "AFC-665 Antigravity Operator setup",
                "verified_host": "mac",
                "state": "open",
                "assigned_at": 40,
                "retired_at": None,
                "closed_at": None,
            },
            {
                "native_session_id": CID_B,
                "title": "Unrelated work",
                "verified_host": "server",
                "state": "open",
                "assigned_at": 25,
                "retired_at": None,
                "closed_at": None,
            },
            {
                "native_session_id": CID_A,
                "title": "AFC-665Operator setup",
                "verified_host": "server",
                "state": "open",
                "assigned_at": 10,
                "retired_at": None,
                "closed_at": None,
            },
        )
        sid, why = B.pick_target(rows, "AFC-665 Operator setup", Path(self.store), "server")
        self.assertEqual((sid, why), (CID_A, "topic-match"))
        # On the Mac (target_host="mac"), the Mac session wins!
        sid_mac, why_mac = B.pick_target(rows, "AFC-665 Operator setup", Path(self.store), "mac")
        self.assertEqual((sid_mac, why_mac), (CID_MAC, "topic-match"))


class DeliverCli(Base):
    def test_deliver_cli_runs_one_delivery(self) -> None:
        history = os.path.join(self.tmp.name, "history.json")
        with open(history, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "items": [
                        {
                            "native_session_id": CID_A,
                            "title": "Operator setup",
                            "verified_host": "server",
                            "state": "open",
                            "assigned_at": 1,
                            "retired_at": None,
                            "closed_at": None,
                        }
                    ],
                    "count": 1,
                },
                fh,
            )
        env = self.env(AG_WAKE_HISTORY_FILE=history)
        result = run_bridge(
            [
                "deliver",
                "--wake-id",
                "w-cli-1",
                "--topic",
                "Operator setup",
                "--config",
                self.config_file,
            ],
            env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout.decode().strip().splitlines()[-1])
        self.assertTrue(receipt["delivered"])
        with open(self.fake_out()) as fh:
            out_text = fh.read()
        self.assertIn(CID_A, out_text)
        self.assertIn("agent-sync-wake", out_text)


if __name__ == "__main__":
    unittest.main()
