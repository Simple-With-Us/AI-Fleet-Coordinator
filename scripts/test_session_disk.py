#!/usr/bin/env python3
"""Tests for generic Grok TUI drive helpers.  No live inject into this TUI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

REPO = Path(__file__).resolve().parents[1]
DISK = REPO / "scripts" / "grok-acp-runtime"
sys.path.insert(0, str(DISK))

from session_disk import (
    DEFAULT_IDLE_UNLOAD_SEC,
    enrich_sessions,
    is_self_session,
    load_active,
    peek_summary,
    peek_tail,
    poll_after_inject,
    poll_until_idle,
    prefix_prompt,
    prompt_inject_receipt,
    select_idle_unload,
    turn_state,
    unload_skip_reason,
    user_prompt_receipt,
)

LIVE_ID = "01a04521-e2e0-7403-9c44-cb8ee340330b"
DRIVE = DISK / "grok-drive.py"


def _any_live_id() -> str:
    rows = load_active()
    if rows:
        return str(rows[0]["sessionId"])
    return LIVE_ID


def _iso_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class PrefixTests(unittest.TestCase):
    def test_default_remote(self) -> None:
        os.environ.pop("AGENT_TAG", None)
        os.environ.pop("AGENT_SEAT", None)
        self.assertTrue(prefix_prompt("hello", None).startswith("[from: remote]"))

    def test_named(self) -> None:
        self.assertEqual(prefix_prompt("hello", "CLAUDE"), "[from: CLAUDE] hello")

    def test_idempotent(self) -> None:
        once = prefix_prompt("hello", "CURSOR")
        self.assertEqual(prefix_prompt(once, "CURSOR"), once)


class DiskTests(unittest.TestCase):
    def test_peek_live(self) -> None:
        sid = _any_live_id()
        out = peek_summary(sid)
        self.assertTrue(out.get("ok"), out)
        self.assertIn(out.get("turnState"), {"idle", "working", "needs-input", "unknown"})

    def test_tail_live(self) -> None:
        sid = _any_live_id()
        out = peek_tail(sid, lines=3)
        self.assertTrue(out.get("ok"), out)
        self.assertIsInstance(out.get("tail"), list)

    def test_turn_state_shape(self) -> None:
        sid = _any_live_id()
        st = turn_state(sid)
        self.assertEqual(st.get("sessionId"), sid)
        self.assertIn(st.get("turnState"), {"idle", "working", "needs-input", "unknown"})
        if load_active():
            self.assertTrue(st.get("live"))
        self.assertIn("pendingTool", st)

    def test_enrich_adds_turn_state(self) -> None:
        sid = _any_live_id()
        rows = enrich_sessions([{"sessionId": sid, "cwd": "/Users/jay/Code"}])
        self.assertTrue(rows)
        self.assertIn("turnState", rows[0])
        if load_active():
            self.assertTrue(rows[0].get("live"))


class DriveCliTests(unittest.TestCase):
    def test_grok_drive_has_generic_commands(self) -> None:
        src = DRIVE.read_text(encoding="utf-8")
        for needle in (
            "await",
            "tail",
            "cancel",
            "--queue",
            "--from-name",
            "--await-reply",
            "--self",
            "poll_after_inject",
            "Not Grok-Bot-only",
            "cmd_close",
            "session/close",
        ):
            self.assertIn(needle, src)

    def test_self_guard_refuses_own_session(self) -> None:
        env = os.environ.copy()
        env["GROK_SESSION_ID"] = "self-session-id"
        proc = subprocess.run(
            [
                "/usr/bin/python3",
                str(DRIVE),
                "prompt",
                "--session-id",
                "self-session-id",
                "--cwd",
                "/Users/jay/apps",
                "--prompt",
                "should-not-inject",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
        )
        data = json.loads(proc.stdout or "{}")
        self.assertEqual(proc.returncode, 3)
        self.assertFalse(data.get("ok"))


class IdleUnloadSelectTests(unittest.TestCase):
    def _row(self, **kwargs):
        now = 1_800_000_000.0
        base = {
            "sessionId": "idle-old",
            "live": True,
            "loaded": True,
            "turnState": "idle",
            "updatedAt": now - (14 * 3600),
        }
        base.update(kwargs)
        return base, now

    def test_default_long_clock_is_four_hours(self):
        self.assertEqual(DEFAULT_IDLE_UNLOAD_SEC, 4 * 3600)

    def test_unloads_live_idle_over_four_hours(self):
        row, now = self._row()
        self.assertIsNone(
            unload_skip_reason(row, now=now, max_idle_sec=DEFAULT_IDLE_UNLOAD_SEC, self_id="me")
        )
        picked = select_idle_unload(
            [row], now=now, max_idle_sec=DEFAULT_IDLE_UNLOAD_SEC, self_id="me"
        )
        self.assertEqual(len(picked), 1)

    def test_skips_fresh_live_idle(self):
        row, now = self._row()
        row["updatedAt"] = now - 3600
        self.assertEqual(
            unload_skip_reason(row, now=now, max_idle_sec=DEFAULT_IDLE_UNLOAD_SEC, self_id="me"),
            "fresh",
        )

    def test_three_hours_still_fresh_at_four_hours(self):
        row, now = self._row()
        row["updatedAt"] = now - (3 * 3600)
        self.assertEqual(
            unload_skip_reason(row, now=now, max_idle_sec=DEFAULT_IDLE_UNLOAD_SEC, self_id="me"),
            "fresh",
        )

    def test_eight_hours_is_idle_at_four_hours(self):
        row, now = self._row()
        row["updatedAt"] = now - (8 * 3600)
        self.assertIsNone(
            unload_skip_reason(row, now=now, max_idle_sec=DEFAULT_IDLE_UNLOAD_SEC, self_id="me")
        )

    def test_skips_working_and_needs_input(self):
        row, now = self._row(turnState="working")
        self.assertEqual(
            unload_skip_reason(row, now=now, self_id="me"),
            "busy",
        )
        row2, now = self._row(sessionId="n", turnState="needs-input")
        self.assertEqual(unload_skip_reason(row2, now=now, self_id="me"), "busy")

    def test_skips_self_and_not_live(self):
        row, now = self._row(sessionId="me")
        self.assertEqual(unload_skip_reason(row, now=now, self_id="me"), "self")
        row2, now = self._row(live=False, loaded=False)
        self.assertEqual(unload_skip_reason(row2, now=now, self_id="me"), "not_loaded")
        # A TUI with nothing loaded frees nothing: attached is a skip input, not a candidate.
        row3, now = self._row(loaded=False)
        self.assertEqual(unload_skip_reason(row3, now=now, self_id="me"), "not_loaded")

    def test_skips_pending_tool_and_missing_ts(self):
        row, now = self._row(pendingTool="bash")
        self.assertEqual(unload_skip_reason(row, now=now, self_id="me"), "pending_tool")
        row2, now = self._row()
        row2.pop("updatedAt")
        self.assertEqual(unload_skip_reason(row2, now=now, self_id="me"), "no_timestamp")

    def test_close_self_guard_cli(self):
        env = os.environ.copy()
        env["GROK_SESSION_ID"] = "self-session-id"
        proc = subprocess.run(
            [
                "/usr/bin/python3",
                str(DRIVE),
                "close",
                "--session-id",
                "self-session-id",
                "--cwd",
                "/Users/jay/apps",
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
            check=False,
        )
        data = json.loads(proc.stdout or "{}")
        self.assertEqual(proc.returncode, 3)
        self.assertFalse(data.get("ok"))
        self.assertTrue(data.get("self"))
        self.assertIn("GROK_SESSION_ID", data.get("error") or "")

    def test_self_guard_off_when_env_unset(self) -> None:
        self.assertFalse(is_self_session("self-session-id"))


class AfterInjectTests(unittest.TestCase):
    def test_skips_preexisting_idle(self) -> None:
        states = [
            {
                "turnState": "idle",
                "turnStartedAt": 1.0,
                "turnEndedAt": 1.5,
                "pendingTool": None,
                "phase": None,
            },
            {
                "turnState": "working",
                "turnStartedAt": 2.0,
                "turnEndedAt": 1.5,
                "pendingTool": None,
                "phase": None,
            },
            {
                "turnState": "idle",
                "turnStartedAt": 2.0,
                "turnEndedAt": 3.0,
                "pendingTool": None,
                "phase": None,
            },
        ]
        it = iter(states)

        def fake_state(_sid: str):
            try:
                return next(it)
            except StopIteration:
                return states[-1]

        with (
            patch("session_disk.turn_state", side_effect=fake_state),
            patch(
                "session_disk.peek_summary",
                return_value={"ok": True, "text": "done"},
            ),
        ):
            out = poll_after_inject("x", before_started=1.0, timeout=2.0, interval=0.0)
        self.assertEqual(out.get("await"), "idle")
        self.assertTrue(out.get("sawNewTurn"))
        self.assertEqual(out.get("turnStartedAt"), 2.0)

    def test_timeout_if_no_new_turn(self) -> None:
        idle = {
            "turnState": "idle",
            "turnStartedAt": 1.0,
            "turnEndedAt": 1.5,
            "pendingTool": None,
            "phase": None,
        }
        with (
            patch("session_disk.turn_state", return_value=idle),
            patch(
                "session_disk.peek_summary",
                return_value={"ok": True, "text": "old"},
            ),
        ):
            out = poll_after_inject("x", before_started=1.0, timeout=0.05, interval=0.01)
        self.assertEqual(out.get("await"), "timeout")
        self.assertFalse(out.get("sawNewTurn"))

    def test_poll_until_idle_returns_immediately_when_idle(self) -> None:
        idle = {
            "turnState": "idle",
            "turnStartedAt": 1.0,
            "turnEndedAt": 1.5,
            "pendingTool": None,
            "phase": None,
        }
        with (
            patch("session_disk.turn_state", return_value=idle),
            patch(
                "session_disk.peek_summary",
                return_value={"ok": True, "text": "now"},
            ),
        ):
            out = poll_until_idle("x", timeout=2.0, interval=0.0)
        self.assertEqual(out.get("await"), "idle")


class PendingToolTests(unittest.TestCase):
    def test_unresolved_permission(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = "sess-perm"
            d = root / "cwd" / sid
            d.mkdir(parents=True)
            now = _iso_now()
            events = [
                {"ts": now, "type": "turn_started"},
                {"ts": now, "type": "permission_requested", "tool_name": "run_terminal_command"},
                {"ts": now, "type": "phase_changed", "phase": "permission_prompt"},
            ]
            (d / "events.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
            )
            (d / "summary.json").write_text(
                json.dumps(
                    {
                        "info": {"cwd": "/Users/jay/apps"},
                        "last_turn_summary": "waiting",
                        "generated_title": "perm",
                    }
                ),
                encoding="utf-8",
            )
            (root / "active.json").write_text(
                json.dumps([{"session_id": sid, "cwd": "/Users/jay/apps", "pid": 1}]),
                encoding="utf-8",
            )
            with (
                patch("session_disk.SESSIONS_ROOT", root),
                patch("session_disk.ACTIVE_SESSIONS", root / "active.json"),
            ):
                st = turn_state(sid)
                peek = peek_summary(sid)
            self.assertEqual(st.get("turnState"), "needs-input")
            self.assertEqual(st.get("pendingTool"), "run_terminal_command")
            self.assertEqual(peek.get("pendingTool"), "run_terminal_command")

    def test_resolved_permission_clears(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = "sess-ok"
            d = root / "cwd" / sid
            d.mkdir(parents=True)
            now = _iso_now()
            events = [
                {"ts": now, "type": "turn_started"},
                {"ts": now, "type": "permission_requested", "tool_name": "read_file"},
                {
                    "ts": now,
                    "type": "permission_resolved",
                    "tool_name": "read_file",
                    "decision": "allow",
                },
                {"ts": now, "type": "turn_ended"},
            ]
            (d / "events.jsonl").write_text(
                "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
            )
            (d / "summary.json").write_text(
                json.dumps({"info": {"cwd": "/Users/jay/apps"}}),
                encoding="utf-8",
            )
            with (
                patch("session_disk.SESSIONS_ROOT", root),
                patch("session_disk.ACTIVE_SESSIONS", root / "none.json"),
            ):
                st = turn_state(sid)
            self.assertIsNone(st.get("pendingTool"))
            self.assertEqual(st.get("turnState"), "idle")


class InstallScriptTests(unittest.TestCase):
    def test_bash_syntax(self) -> None:
        script = REPO / "scripts" / "install-grok-tui-drive.sh"
        self.assertTrue(script.is_file())
        proc = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_dry_run_mentions_dest(self) -> None:
        script = REPO / "scripts" / "install-grok-tui-drive.sh"
        proc = subprocess.run(
            ["bash", str(script), "--dry-run"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("grok-drive.py", proc.stdout)
        self.assertIn("seat-mcp-launch.sh", proc.stdout)
        src = script.read_text(encoding="utf-8")
        self.assertIn("refusing pm2 restart", src)
        self.assertIn("never a second bind", src)

    def test_launchers_tracked(self) -> None:
        launch = REPO / "scripts" / "mcp-servers" / "seat-mcp-launch.sh"
        proxy = REPO / "scripts" / "mcp-servers" / "seat-mcp-stdio-proxy.py"
        self.assertTrue(launch.is_file())
        self.assertTrue(proxy.is_file())
        src = proxy.read_text(encoding="utf-8")
        self.assertIn("SEAT_MCP_TOKEN", src)
        self.assertIn("127.0.0.1:8793", src)
        self.assertNotIn("print(token", src)

    def test_seat_launcher_classifies_child_failures_without_echoing_stderr(self) -> None:
        source = REPO / "scripts" / "seat-mcp" / "start.sh"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            runtime = root / "seat-mcp"
            runtime.mkdir()
            launcher = runtime / "start.sh"
            shutil.copyfile(source, launcher)
            child = root / "fleet_infisical_cli.py"
            child.write_text(
                "import os, sys\n"
                "sys.stderr.write('test-only-private-diagnostic')\n"
                "sys.exit(int(os.environ['CHILD_STATUS']))\n",
                encoding="utf-8",
            )
            for status, reason in (
                (2, "configuration refused"),
                (3, "missing"),
                (4, "fetch or authentication failed"),
                (5, "unresolved reference"),
                (7, "reader failed (exit 7)"),
            ):
                with self.subTest(status=status):
                    result = subprocess.run(
                        ["/bin/bash", str(launcher)],
                        env={
                            "HOME": str(root),
                            "PATH": os.defpath,
                            "CHILD_STATUS": str(status),
                        },
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertEqual(
                        result.stderr.strip(),
                        f"SEAT_MCP_TOKEN {reason} in Infisical /seat-mcp",
                    )
                    self.assertNotIn("test-only-private-diagnostic", result.stderr)

    def test_installed_runtime_closure_and_loaders_ignore_hostile_python_path(self) -> None:
        repo_scripts = REPO / "scripts"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            hostile = root / "hostile"
            home.mkdir()
            hostile.mkdir()
            (hostile / "fleet_infisical_cli.py").write_text(
                "raise AssertionError('hostile CLI executed')\n", encoding="utf-8"
            )
            (hostile / "fleet_infisical.py").write_text(
                "raise AssertionError('hostile library executed')\n", encoding="utf-8"
            )
            (hostile / "fleet-secrets").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
            (hostile / "fleet-secrets").chmod(0o700)
            install_env = {"HOME": str(home), "PATH": os.defpath}
            installed = subprocess.run(
                ["bash", str(repo_scripts / "install-grok-tui-drive.sh")],
                env=install_env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            apps = home / "apps"
            for name in ("fleet_infisical.py", "fleet_infisical_cli.py"):
                self.assertEqual((apps / name).read_bytes(), (repo_scripts / name).read_bytes())
            for source, destination in (
                (
                    repo_scripts / "grok-acp-runtime" / "acp-client.py",
                    apps / "grok-acp-runtime" / "acp-client.py",
                ),
                (
                    repo_scripts / "seat-mcp" / "seat_mcp" / "auth.py",
                    apps / "seat-mcp" / "seat_mcp" / "auth.py",
                ),
                (
                    repo_scripts / "mcp-servers" / "seat-mcp-stdio-proxy.py",
                    apps / "mcp-servers" / "seat-mcp-stdio-proxy.py",
                ),
            ):
                self.assertEqual(destination.read_bytes(), source.read_bytes())
            self.assertFalse((apps / "fleet-secrets").exists())

            with RuntimeInfisicalFixture() as provider:
                script = r"""
import importlib.util, runpy, sys, types
sys.modules["websockets"] = types.SimpleNamespace()
apps, mode = sys.argv[1:]
acp_dir = apps + "/grok-acp-runtime"
if mode == "grok":
    sys.path.insert(0, acp_dir)
    spec = importlib.util.spec_from_file_location("copied_acp", acp_dir + "/acp-client.py")
    acp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(acp)
    assert acp.load_secret() == "fixture-grok-secret"
else:
    sys.path.insert(0, apps + "/seat-mcp")
    from seat_mcp.auth import load_token
    proxy = runpy.run_path(apps + "/mcp-servers/seat-mcp-stdio-proxy.py")
    assert load_token() == "fixture-seat-token"
    assert proxy["load_token"]() == "fixture-seat-token"
print("copied-loaders-ok")
"""
                for mode, token in (
                    ("grok", "st.test-only-grok-key"),
                    ("seat", "st.test-only-seat-key"),
                ):
                    env = {
                        "HOME": str(home),
                        "PATH": os.defpath,
                        "PYTHONPATH": str(hostile),
                        "FLEET_SECRETS_API": provider.url,
                        "INFISICAL_TOKEN": token,
                        "INFISICAL_FLEET_OPS_PROJECT_ID": provider.project,
                        "INFISICAL_ENVIRONMENT": "prod",
                    }
                    result = subprocess.run(
                        [sys.executable, "-I", "-c", script, str(apps), mode],
                        cwd=hostile,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "copied-loaders-ok")
                    self.assertNotIn("fixture-grok-secret", result.stdout + result.stderr)
                    self.assertNotIn("fixture-seat-token", result.stdout + result.stderr)
                self.assertTrue(any("%2Fgrok-acp" in call for call in provider.calls))
                self.assertTrue(any("%2Fseat-mcp" in call for call in provider.calls))
                self.assertTrue(any("GROK_AGENT_SECRET" in call for call in provider.calls))
                self.assertTrue(any("SEAT_MCP_TOKEN" in call for call in provider.calls))
                self.assertTrue(all("fleet-secrets" not in call for call in provider.calls))
                value_reads = [
                    (
                        unquote(urlsplit(call).path.rsplit("/", 1)[-1]),
                        unquote(parse_qs(urlsplit(call).query)["secretPath"][0]),
                    )
                    for call in provider.calls
                    if "/v3/secrets/raw/" in call
                ]
                self.assertEqual(
                    sorted(value_reads),
                    sorted(
                        [
                            ("GROK_AGENT_SECRET", "/grok-acp"),
                            ("SEAT_MCP_TOKEN", "/seat-mcp"),
                            ("SEAT_MCP_TOKEN", "/seat-mcp"),
                        ]
                    ),
                )

    def test_copied_loader_refuses_bad_grants_before_secret_values(self) -> None:
        repo_scripts = REPO / "scripts"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            apps = root / "apps"
            hostile = root / "hostile"
            hostile.mkdir()
            apps.mkdir(exist_ok=True)
            for name in ("fleet_infisical.py", "fleet_infisical_cli.py"):
                shutil.copyfile(repo_scripts / name, apps / name)
            acp_dir = apps / "grok-acp-runtime"
            acp_dir.mkdir()
            shutil.copyfile(
                repo_scripts / "grok-acp-runtime" / "acp-client.py", acp_dir / "acp-client.py"
            )
            shutil.copyfile(
                repo_scripts / "grok-acp-runtime" / "mcp_catalog.py", acp_dir / "mcp_catalog.py"
            )
            (hostile / "fleet_infisical_cli.py").write_text(
                "raise AssertionError('hostile CLI executed')\n", encoding="utf-8"
            )
            code = r"""
import importlib.util, sys, types
sys.modules["websockets"] = types.SimpleNamespace()
sys.path.insert(0, str(__import__("pathlib").Path(sys.argv[1]).parent))
spec = importlib.util.spec_from_file_location("copied_acp", sys.argv[1])
acp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acp)
try:
    acp.load_secret()
except SystemExit as exc:
    print(str(exc))
else:
    raise AssertionError("bad grant accepted")
"""
            for mode in ("wrong", "empty", "malformed"):
                with self.subTest(mode=mode), RuntimeInfisicalFixture(mode=mode) as provider:
                    env = {
                        "HOME": str(root),
                        "PATH": os.defpath,
                        "PYTHONPATH": str(hostile),
                        "FLEET_SECRETS_API": provider.url,
                        "INFISICAL_TOKEN": "st.test-only-grok-key",
                        "INFISICAL_FLEET_OPS_PROJECT_ID": provider.project,
                        "INFISICAL_ENVIRONMENT": "prod",
                    }
                    result = subprocess.run(
                        [sys.executable, "-I", "-c", code, str(acp_dir / "acp-client.py")],
                        cwd=hostile,
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("GROK_AGENT_SECRET missing in Infisical /grok-acp", result.stdout)
                    self.assertNotIn("st.test-only-grok-key", result.stdout + result.stderr)
                    self.assertFalse(any("/v3/secrets/raw/" in call for call in provider.calls))


class RuntimeInfisicalFixture:
    """Loopback-only provider for the production CLI's real token and folder checks."""

    def __init__(self, mode="normal"):
        import uuid

        self.mode = mode
        self.project = str(uuid.uuid4())
        self.calls = []
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                fixture.calls.append(self.path)
                received = self.headers.get("Authorization", "").removeprefix("Bearer ")
                folder_for_token = {
                    "st.test-only-grok-key": "/grok-acp",
                    "st.test-only-seat-key": "/seat-mcp",
                }
                if received not in folder_for_token:
                    self.send_error(403)
                    return
                if self.path.endswith("/v2/service-token"):
                    metadata = {
                        "workspace": fixture.project,
                        "permissions": ["read"],
                        "scopes": [
                            {"environment": "prod", "secretPath": folder_for_token[received]}
                        ],
                        "expiresAt": "2099-01-01T00:00:00Z",
                    }
                    if fixture.mode == "wrong":
                        metadata["scopes"] = [{"environment": "prod", "secretPath": "/other"}]
                    elif fixture.mode == "empty":
                        metadata["scopes"] = []
                    elif fixture.mode == "malformed":
                        metadata.pop("expiresAt")
                    body = json.dumps(metadata).encode()
                else:
                    query = parse_qs(urlsplit(self.path).query)
                    folder = unquote(query.get("secretPath", [""])[0])
                    name = unquote(urlsplit(self.path).path.rsplit("/", 1)[-1])
                    value = "fixture-grok-secret" if folder == "/grok-acp" else "fixture-seat-token"
                    body = json.dumps(
                        {
                            "secret": {
                                "workspace": fixture.project,
                                "environment": "prod",
                                "secretPath": folder,
                                "type": "shared",
                                "secretKey": name,
                                "secretValue": value,
                                "secretValueHidden": False,
                            }
                        }
                    ).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = "http://127.0.0.1:%s/api" % self.server.server_address[1]

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class PromptInjectReceiptTests(unittest.TestCase):
    def test_user_prompt_receipt_matches_tail_history(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sid = "sess-receipt"
            d = root / "cwd" / sid
            d.mkdir(parents=True)
            hist = d / "chat_history.jsonl"
            hist.write_text(
                json.dumps({"type": "user", "content": [{"type": "text", "text": "nudge"}]}) + "\n",
                encoding="utf-8",
            )
            with patch("session_disk.SESSIONS_ROOT", root):
                self.assertTrue(user_prompt_receipt(sid, "nudge"))
                self.assertFalse(user_prompt_receipt(sid, "other"))

    def test_prompt_inject_receipt_accepts_new_turn(self) -> None:
        with (
            patch("session_disk.turn_state", return_value={"turnStartedAt": 20.0}),
            patch("session_disk.user_prompt_receipt", return_value=False),
        ):
            self.assertTrue(prompt_inject_receipt("s", "x", before_started=10.0))


if __name__ == "__main__":
    unittest.main()
