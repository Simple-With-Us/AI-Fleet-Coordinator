"""The ZCode wake bridge (OPS-1048):  the webhook the Operator's http wake reaches, and the
deliverer that turns one wake into a prompt in a specific ZCode thread.  Every test drives the
bridge the way the fleet does:  the POST carries the simple-tools-wake/1 contract the listener
sends, the key lives in a 0600 file, the registry is either a stub list or the real
listener-registry API on a loopback port, and the delivery command is a fake that leaves its
argv in a file, never a real ZCode run."""

from __future__ import annotations

import io
import json
import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

BRIDGE = Path(__file__).resolve().parents[2] / "zc-wake-bridge"

import importlib.util

from importlib.machinery import SourceFileLoader as _SFL

_loader = _SFL("zc_wake_bridge_stub", str(BRIDGE))
_spec = importlib.util.spec_from_loader("zc_wake_bridge_stub", _loader)
assert _spec is not None
B = importlib.util.module_from_spec(_spec)
sys.modules["zc_wake_bridge_stub"] = B
_loader.exec_module(B)


SESSION_DDL = (
    "CREATE TABLE session ("
    " id text primary key,"
    " parent_id text,"
    " title text not null default '',"
    " task_type text not null default 'interactive',"
    " time_archived integer,"
    " time_deleted integer)"
)


def run_bridge(argv: list[str], env: dict[str, str], stdin: bytes = b"") -> subprocess.CompletedProcess:
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
        self.state = os.path.join(self.tmp.name, "zc-wake")
        os.makedirs(self.home)
        self.key = secrets.token_hex(24)
        os.makedirs(self.state, mode=0o700)
        self.key_file = os.path.join(self.state, "routine-key")
        with open(self.key_file, "w", encoding="utf-8") as fh:
            fh.write(self.key + "\n")
        os.chmod(self.key_file, 0o600)
        self.config = {"target_host": "server", "delivery_enabled": False}
        self.config_file = os.path.join(self.state, "config.json")
        with open(self.config_file, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh)
        self.store = os.path.join(self.home, ".zcode", "cli", "db", "db.sqlite")
        os.makedirs(os.path.dirname(self.store))
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(SESSION_DDL)
        conn.close()

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "HOME": self.home,
            "ZC_WAKE_STATE": self.tmp.name,  # state_dir appends "zc-wake"
            "ZC_WAKE_KEY_FILE": self.key_file,
            "ZC_WAKE_CONFIG": self.config_file,
            "ZC_WAKE_STORE": self.store,
        }
        env.update(extra)
        return env


    def rows(self, *rows: dict) -> list[dict]:
        return list(rows)


class Receiver(Base):
    def post(self, payload: dict | bytes, env: dict[str, str], key: str | None = None,
             port: int = 0, config: dict | None = None) -> tuple[int, dict]:
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
            "http://%s:%d/wake/ZC" % (host, bound), data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode())

    def wake(self, **over) -> dict:
        payload = {
            "contract": "simple-tools-wake/1",
            "seat": "ZC",
            "wake_id": "w-0001",
            "topic": "OPS-1048 ZCode letters",
            "channel": "work-in-progress",
            "zulip_link": "https://zulip.example/#/topic/1/OPS-1048",
        }
        payload.update(over)
        return payload

    def registry_env(self, rows: list[dict]) -> dict[str, str]:
        """The registry answer comes from a stub module the bridge's registry_history reads:
        a file the stub loader honours, so no network is involved."""
        path = os.path.join(self.tmp.name, "history.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"items": rows, "count": len(rows)}, fh)
        return {"ZC_WAKE_HISTORY_FILE": path}

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
        self.assertEqual(answer, {"ok": True, "seat": "ZC"})

    def test_good_wake_is_accepted_and_deduped(self) -> None:
        env = self.env(**self.registry_env(self.rows(
            {"native_session_id": "sess_a", "title": "OPS-1048 ZCode letters",
             "verified_host": "server", "state": "open", "assigned_at": 2, "retired_at": None,
             "closed_at": None},
        )))
        status, answer = self.post(self.wake(), env, key=self.key)
        self.assertEqual(status, 200)
        self.assertEqual(answer["session_id"], "sess_a", answer)
        self.assertEqual(answer["picked"], "topic-match")
        # a second identical wake is a no-op
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
        status, _ = self.post(self.wake(seat="MM"), env, key=self.key)
        self.assertEqual(status, 403)
        status, _ = self.post(self.wake(wake_id="bad id!"), env, key=self.key)
        self.assertEqual(status, 400)

    def test_topic_target_never_starts_without_sess_prefix(self) -> None:
        # A crafted registry row whose native id is argv-shaped ("-p") is not a target.
        env = self.env(**self.registry_env(self.rows(
            {"native_session_id": "-p", "title": "Evil", "verified_host": "server",
             "state": "open", "assigned_at": 9, "retired_at": None, "closed_at": None},
        )))
        status, answer = self.post(self.wake(), env, key=self.key)
        self.assertEqual(status, 200, answer)
        self.assertIn(answer.get("reason"), ("no-live-session",))
        self.assertEqual(answer.get("session_id"), "")


class ConfigLoad(Base):
    def test_explicit_missing_config_refuses(self) -> None:
        with self.assertRaises(SystemExit):
            B.load_config(Path(self.tmp.name) / "nope.json", required=True)

    def test_default_missing_config_is_defaults(self) -> None:
        config = B.load_config(Path(self.tmp.name) / "nope.json")
        self.assertEqual(config["target_host"], "server")

    def test_mistyped_fields_refuse(self) -> None:
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text(json.dumps({"target_host": "cloud"}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            B.load_config(bad)


class IsoTimes(Base):
    """The registry writes ISO timestamps for assigned_at; a bare float() on them is the
    crash Claude AA's read-only pass caught (zc-wake-bridge pick on every letterless wake)."""

    def test_iso_assigned_at_ranks_against_numbers(self) -> None:
        rows = self.rows(
            {"native_session_id": "sess_iso", "title": "Letters letters", "verified_host": "server",
             "state": "open", "assigned_at": "2026-11-10T23:00:00Z", "retired_at": None,
             "closed_at": None},
            {"native_session_id": "sess_num", "title": "Letters letters", "verified_host": "server",
             "state": "open", "assigned_at": 5, "retired_at": None, "closed_at": None},
        )
        sid, why = B.pick_target(rows, "Letters letters", Path(self.store), "server")
        self.assertEqual(sid, "sess_iso")  # 2026 epoch dwarfs 5
        self.assertEqual(why, "topic-match")

    def test_a_bad_time_is_zero_not_a_crash(self) -> None:
        rows = self.rows(
            {"native_session_id": "sess_junk", "title": "Letters", "verified_host": "server",
             "state": "open", "assigned_at": "not-a-time", "retired_at": None, "closed_at": None},
        )
        sid, why = B.pick_target(rows, "Letters", Path(self.store), "server")
        self.assertEqual((sid, why), ("sess_junk", "topic-match"))


class UserAgentHeader(Base):
    """The registry host is Cloudflare-fronted and refuses a bare urllib signature (1010);
    every request the bridge sends carries the explicit agent (found live:  the first real
    wake receipt picked nothing because Cloudflare 1010'd the history read)."""

    def test_history_and_resolve_carry_the_agent(self) -> None:
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen: list[str] = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: N802
                pass

            def do_GET(self) -> None:  # noqa: N802
                seen.append(str(self.headers.get("User-Agent")))
                body = b'{"found": true, "native_session_id": "sess_ua_check_0001"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        host, port = server.server_address[:2]
        env = self.env(
            SIMPLE_TOOLS_OPERATOR_REGISTRY_URL="http://%s:%d" % (host, port),
            SIMPLE_TOOLS_OPERATOR_REGISTRY_PRINCIPAL="OPERATOR",
            SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY="k" * 40,
        )
        sid = B.resolve_letter(env, "AA")
        self.assertEqual(sid, "sess_ua_check_0001")
        self.assertEqual(seen, [B.USER_AGENT])


class LetterAddress(Base):
    """A wake that carries a letter addresses one chat:  the registry resolves it, and the
    letter wins the target outright.  A letter that resolves to nothing delivers nowhere."""

    def test_letter_extraction_from_mention_forms(self) -> None:
        cases = {
            "@**ZCode** AA please look": "AA",
            "hey ZCode BB, status?": "BB",
            "no letter here at all": "",
            "ZCode 123 not a letter": "",
            "@**ZCode**  ZZ  double space": "ZZ",
        }
        for text, want in cases.items():
            self.assertEqual(B.letter_of({"excerpt": text}), want, text)
        self.assertEqual(B.letter_of({"letter": "QQ", "excerpt": "ZCode AA"}), "QQ")

    def test_a_letter_beats_topic_and_recency(self) -> None:
        rows = self.rows(
            {"native_session_id": "sess_topic", "title": "Letters letters", "verified_host": "server",
             "state": "open", "assigned_at": 50, "retired_at": None, "closed_at": None},
        )
        sid, why = B.pick_target(
            rows, "Letters letters", Path(self.store), "server", letter_sid="sess_by_letter"
        )
        self.assertEqual((sid, why), ("sess_by_letter", "letter"))

    def test_a_letter_on_a_subagent_session_refuses(self) -> None:
        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO session (id, task_type) VALUES (?, 'subagent_child')", ("sess_sub2",)
            )
        conn.close()
        sid, why = B.pick_target([], "anything", Path(self.store), "server", letter_sid="sess_sub2")
        self.assertEqual((sid, why), ("", "letter-target-is-subagent"))

    def test_a_dead_letter_falls_back_to_deterministic_routing(self) -> None:
        # "ZCode PR" must not dead-end the wake:  an unresolved letter records itself and
        # the topic/recency rules pick instead (Kody:  the mention regex can read a word).
        history = os.path.join(self.tmp.name, "history-fb.json")
        with open(history, "w", encoding="utf-8") as fh:
            json.dump({"items": [{"native_session_id": "sess_fb", "title": "Router contract work",
                                  "verified_host": "server", "state": "open", "assigned_at": 9,
                                  "retired_at": None, "closed_at": None}], "count": 1}, fh)
        env = self.env(ZC_WAKE_HISTORY_FILE=history)
        receipt = B.record(
            env,
            self.config,
            wake_id="w-fb",
            topic="Router contract work",
            channel="Side-Chats",
            link="",
            letter="PR",
        )
        self.assertEqual(receipt["session_id"], "sess_fb")
        self.assertEqual(receipt["picked"], "topic-match")
        self.assertEqual(receipt["reason"], "delivery-disabled")  # the fallback marker rides the receipt's letter field; delivery is off in this fixture

    def test_a_dead_letter_delivers_nowhere(self) -> None:
        env = self.env()  # no registry URL:  resolve_letter answers "" (nothing live)
        receipt = B.record(
            env,
            self.config,
            wake_id="w-dead",
            topic="anything",
            channel="c",
            link="",
            letter="ZZ",
        )
        # a dead letter no longer dead-ends:  nothing else to route on here, so no session
        self.assertEqual(receipt["session_id"], "")
        self.assertEqual(receipt["picked"], "")
        self.assertIn(receipt["reason"], ("no-live-session", "letter-not-live-fell-back"))


class TargetPick(Base):
    def test_topic_match_beats_recency(self) -> None:
        from zc_wake_bridge_stub import pick_target

        rows = self.rows(
            {"native_session_id": "sess_old", "title": "Wake the fleet", "verified_host": "server",
             "state": "open", "assigned_at": 10, "retired_at": None, "closed_at": None},
            {"native_session_id": "sess_new", "title": "OPS-1048 letters", "verified_host": "server",
             "state": "open", "assigned_at": 20, "retired_at": None, "closed_at": None},
        )
        sid, why = pick_target(rows, "OPS-1048 letters", Path(self.store), "server")
        self.assertEqual((sid, why), ("sess_new", "topic-match"))

    def test_falls_back_to_most_recent(self) -> None:
        from zc_wake_bridge_stub import pick_target

        rows = self.rows(
            {"native_session_id": "sess_old", "title": "Unrelated work", "verified_host": "server",
             "state": "open", "assigned_at": 10, "retired_at": None, "closed_at": None},
        )
        sid, why = pick_target(rows, "Totally other topic", Path(self.store), "server")
        self.assertEqual((sid, why), ("sess_old", "most-recent"))

    def test_subagent_and_offhost_rows_are_skipped(self) -> None:
        from zc_wake_bridge_stub import pick_target

        conn = sqlite3.connect(self.store)
        with conn:
            conn.execute(
                "INSERT INTO session (id, task_type) VALUES (?, 'subagent_child')", ("sess_sub",)
            )
        conn.close()
        rows = self.rows(
            {"native_session_id": "sess_sub", "title": "OPS-1048 letters", "verified_host": "server",
             "state": "open", "assigned_at": 30, "retired_at": None, "closed_at": None},
            {"native_session_id": "sess_mac", "title": "OPS-1048 letters", "verified_host": "mac",
             "state": "open", "assigned_at": 40, "retired_at": None, "closed_at": None},
            {"native_session_id": "sess_ok", "title": "Unrelated", "verified_host": "server",
             "state": "open", "assigned_at": 5, "retired_at": None, "closed_at": None},
        )
        sid, why = pick_target(rows, "OPS-1048 letters", Path(self.store), "server")
        self.assertEqual((sid, why), ("sess_ok", "most-recent"))

    def test_no_rows_answers_no_live_session(self) -> None:
        from zc_wake_bridge_stub import pick_target

        sid, why = pick_target([], "anything", Path(self.store), "server")
        self.assertEqual((sid, why), ("", "no-live-session"))


class DeliverCli(Base):
    def test_record_cli_runs_one_routing(self) -> None:
        history = os.path.join(self.tmp.name, "history.json")
        with open(history, "w", encoding="utf-8") as fh:
            json.dump({"items": [{"native_session_id": "sess_cli", "title": "Letters",
                                  "verified_host": "server", "state": "open", "assigned_at": 1,
                                  "retired_at": None, "closed_at": None}], "count": 1}, fh)
        env = self.env(ZC_WAKE_HISTORY_FILE=history)
        result = run_bridge(
            ["record", "--wake-id", "w-cli-1", "--topic", "Letters", "--config", self.config_file],
            env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout.decode().strip().splitlines()[-1])
        log = json.loads(open(os.path.join(self.state, "wakes.jsonl")).read().splitlines()[-1])
        self.assertEqual(log["wake_id"], "w-cli-1")


if __name__ == "__main__":
    unittest.main()
