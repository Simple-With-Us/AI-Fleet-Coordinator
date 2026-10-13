#!/usr/bin/env python3
"""ACP stdio client of the shared Grok leader.

Lists, loads, peeks, and prompts local chats (the same store the TUI uses).
Does not print secrets.  Requires a process holding ~/.grok/leader.sock
(pm2 grok-leader, or a TUI that spawned the leader).

  python3 /Users/jay/apps/grok-acp-runtime/leader-client.py list
  python3 /Users/jay/apps/grok-acp-runtime/leader-client.py peek --session-id ID --cwd DIR
  python3 /Users/jay/apps/grok-acp-runtime/leader-client.py prompt --session-id ID --cwd DIR --prompt "..."

Timeouts.  Each RPC keeps its historical default (initialize 25s,
session/list 20s, session/close 15s) so callers with a short subprocess
timeout (grok-drive.py, seat-mcp) still get a clean JSON error first.  A fresh
leader client is slow on a loaded Mac (session/list measured 14-22s on
2026-10-08), so a patient caller passes --rpc-timeout SECONDS (or sets
GROK_LEADER_RPC_TIMEOUT): ONE total budget for the whole command that replaces
the per-RPC defaults, ending in a clean JSON "no ACP result" error before the
caller has to kill this process.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

GROK = "/Users/jay/.grok/bin/grok"
CMD = [GROK, "agent", "--always-approve", "--leader", "stdio"]
SESSIONS_ROOT = Path.home() / ".grok" / "sessions"

# Total-budget deadline (epoch seconds) when --rpc-timeout / the env var is set.
_BUDGET_DEADLINE = None


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _timeout(default):
    """Per-RPC timeout: the remaining total budget when one is set."""
    if _BUDGET_DEADLINE is None:
        return default
    return max(1.0, _BUDGET_DEADLINE - time.time())


sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    from session_disk import peek_summary as disk_peek
except Exception:
    disk_peek = None


class LeaderStdio:
    def __init__(self):
        self.p = subprocess.Popen(
            CMD,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._id = 0
        self._buf = b""

    def close(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(timeout=2)
            except Exception:
                self.p.kill()

    def _next(self):
        self._id += 1
        return self._id

    def _read_msg(self, deadline):
        while time.time() < deadline:
            if self.p.poll() is not None:
                err = self.p.stderr.read().decode(errors="replace")[:800]
                raise RuntimeError("leader stdio exited %s: %s" % (self.p.returncode, err))
            r, _, _ = select.select([self.p.stdout], [], [], 0.2)
            if not r:
                continue
            chunk = os.read(self.p.stdout.fileno(), 8192)
            if not chunk:
                continue
            self._buf += chunk
            while b"\n" in self._buf:
                raw, self._buf = self._buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    continue
        return None

    def request(self, method, params, timeout=30.0, collect_text=False):
        req_id = self._next()
        line = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}) + "\n"
        self.p.stdin.write(line.encode())
        self.p.stdin.flush()
        deadline = time.time() + timeout
        chunks = []
        while time.time() < deadline:
            msg = self._read_msg(deadline)
            if msg is None:
                break
            if collect_text:
                text = extract_agent_text(msg)
                if text:
                    chunks.append(text)
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                raise RuntimeError(json.dumps(msg["error"]))
            result = msg.get("result") or {}
            if collect_text:
                return result, "".join(chunks)
            return result
        raise TimeoutError("no ACP result for %s within %.0fs" % (method, timeout))

    def notify(self, method, params):
        line = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        self.p.stdin.write(line.encode())
        self.p.stdin.flush()

    def prompt_live(self, session_id, text, timeout=8.0, wait=False):
        """Inject into a live TUI.  Default: return once queued, do not wait the turn.

        Waiting for session/prompt's result waits until the TUI finishes that
        turn (can be minutes).  Closing this stdio client does not cancel it.
        """
        req_id = self._next()
        params = {
            "sessionId": session_id,
            "prompt": [{"type": "text", "text": text}],
        }
        line = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": "session/prompt", "params": params}) + "\n"
        self.p.stdin.write(line.encode())
        self.p.stdin.flush()
        deadline = time.time() + (timeout if wait else min(timeout, 8.0))
        chunks = []
        queued = False
        notes = []
        while time.time() < deadline:
            msg = self._read_msg(deadline)
            if msg is None:
                break
            method = msg.get("method") or ""
            if method:
                notes.append(method)
                if method in {"_x.ai/queue/changed", "_x.ai/session_notification", "session/update", "_x.ai/sessions/changed"}:
                    queued = True
                piece = extract_agent_text(msg)
                if piece:
                    chunks.append(piece)
                if queued and not wait:
                    return {"queued": True, "text": "".join(chunks), "notes": notes[:12]}
                continue
            if msg.get("id") != req_id:
                continue
            if "error" in msg:
                raise RuntimeError(json.dumps(msg["error"]))
            result = msg.get("result") or {}
            return {"queued": False, "text": "".join(chunks), "result": result, "notes": notes[:12]}
        if queued or not wait:
            return {"queued": True, "text": "".join(chunks), "notes": notes[:12]}
        raise TimeoutError("no ACP result for session/prompt")


def extract_agent_text(msg):
    """Pull assistant text out of a session/update notification."""
    if not isinstance(msg, dict):
        return ""
    if msg.get("method") not in {"session/update", "x.ai/session/update"}:
        return ""
    params = msg.get("params") or {}
    upd = params.get("update") if isinstance(params.get("update"), dict) else params
    kind = upd.get("sessionUpdate") or upd.get("session_update") or ""
    if kind != "agent_message_chunk":
        return ""
    content = upd.get("content") or {}
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, str):
        return content
    return ""


def slim_session(s):
    return {
        "sessionId": s.get("sessionId"),
        "cwd": s.get("cwd"),
        "title": s.get("title"),
        "updatedAt": s.get("updatedAt"),
    }


def _initialize(client, timeout=25.0):
    timeout = _timeout(timeout)
    return client.request(
        "initialize",
        {
            "protocolVersion": 1,
            "clientInfo": {"name": "grok-leader-client", "version": "1.1"},
            "clientCapabilities": {
                "fs": {"readTextFile": True, "writeTextFile": True},
                "terminal": True,
            },
        },
        timeout=timeout,
    )


def find_session_dir(session_id: str):
    if not SESSIONS_ROOT.is_dir() or "/" in session_id or "\\" in session_id:
        return None
    matches = list(SESSIONS_ROOT.glob("*/" + session_id))
    dirs = [p for p in matches if p.is_dir()]
    return dirs[0] if dirs else None


def peek_from_disk(session_id: str) -> dict:
    """Read summary.json.  Does not session/load a live TUI (that hangs ~45s)."""
    path = find_session_dir(session_id)
    if path is None:
        return {"ok": False, "error": "session dir not found", "sessionId": session_id}
    summary_path = path / "summary.json"
    if not summary_path.is_file():
        return {"ok": False, "error": "summary.json missing", "sessionId": session_id, "dir": str(path)}
    try:
        data = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": "summary.json parse failed: %s" % exc, "sessionId": session_id}
    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    return {
        "ok": True,
        "sessionId": session_id,
        "cwd": info.get("cwd"),
        "title": data.get("generated_title") or data.get("session_summary"),
        "text": data.get("last_turn_summary") or "",
        "recap": data.get("last_recap") or "",
        "updatedAt": data.get("updated_at") or data.get("last_active_at"),
        "source": "disk",
    }


def _resume(client, session_id, cwd, timeout=20.0):
    timeout = _timeout(timeout)
    """Attach to a session the TUI already has open.  session/load hangs on live chats."""
    return client.request(
        "session/resume",
        {"sessionId": session_id, "cwd": cwd},
        timeout=timeout,
    )


def main():
    global _BUDGET_DEADLINE
    p = argparse.ArgumentParser(description="List/load/prompt local Grok chats via the shared leader")
    p.add_argument(
        "--rpc-timeout",
        type=float,
        default=None,
        help="total seconds for the whole command (default: per-RPC timeouts; env GROK_LEADER_RPC_TIMEOUT)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("handshake")
    ls = sub.add_parser("list")
    ls.add_argument("--cwd", default="")
    ld = sub.add_parser("load")
    ld.add_argument("--session-id", required=True)
    ld.add_argument("--cwd", default="/Users/jay")
    pk = sub.add_parser("peek", help="disk summary.json; no session/load")
    pk.add_argument("--session-id", required=True)
    pk.add_argument("--cwd", default="/Users/jay")
    ca = sub.add_parser("cancel", help="session/cancel notification after resume")
    ca.add_argument("--session-id", required=True)
    ca.add_argument("--cwd", default="/Users/jay")
    cl = sub.add_parser("close", help="session/close: unload MCP, keep disk history")
    cl.add_argument("--session-id", required=True)
    cl.add_argument("--cwd", default="/Users/jay")
    pr = sub.add_parser("prompt", help="session/resume then session/prompt on a live TUI chat")
    pr.add_argument("--session-id", required=True)
    pr.add_argument("--prompt", required=True)
    pr.add_argument("--cwd", default="/Users/jay")
    pr.add_argument("--timeout", type=float, default=8.0, help="seconds to wait for queued ack (or full reply with --wait)")
    pr.add_argument("--wait", action="store_true", help="wait for the TUI turn to finish and return its text")
    args = p.parse_args()

    budget = args.rpc_timeout
    if budget is None:
        try:
            budget = float(os.environ.get("GROK_LEADER_RPC_TIMEOUT", "") or 0.0) or None
        except ValueError:
            budget = None
    if budget is not None and budget > 0:
        _BUDGET_DEADLINE = time.time() + budget

    if args.cmd == "peek":
        fn = disk_peek or peek_from_disk
        print(json.dumps(fn(args.session_id), indent=2))
        return

    client = LeaderStdio()
    try:
        init = _initialize(client)
        if args.cmd == "handshake":
            caps = init.get("agentCapabilities") or {}
            print(json.dumps({
                "ok": True,
                "protocolVersion": init.get("protocolVersion"),
                "sessionCapabilities": caps.get("sessionCapabilities"),
                "loadSession": caps.get("loadSession"),
            }, indent=2))
            return
        if args.cmd == "list":
            params = {}
            if args.cwd:
                params["cwd"] = args.cwd
            listed = client.request("session/list", params, timeout=_timeout(20.0))
            sessions = [slim_session(s) for s in (listed.get("sessions") or [])]
            print(json.dumps({"ok": True, "count": len(sessions), "sessions": sessions}, indent=2))
            return
        if args.cmd == "load":
            # Idle / disk sessions only.  Live TUI chats hang on session/load — use prompt.
            loaded, text = client.request(
                "session/load",
                {
                    "sessionId": args.session_id,
                    "cwd": args.cwd,
                    "mcpServers": [],
                },
                timeout=_timeout(45.0),
                collect_text=True,
            )
            print(json.dumps({
                "ok": True,
                "sessionId": args.session_id,
                "cwd": args.cwd,
                "keys": sorted(loaded.keys()),
                "text": text,
            }, indent=2))
            return
        if args.cmd == "cancel":
            _resume(client, args.session_id, args.cwd)
            # Notification, not a request — session/cancel as request is -32601.
            client.notify("session/cancel", {"sessionId": args.session_id})
            time.sleep(0.3)
            print(json.dumps({
                "ok": True,
                "sessionId": args.session_id,
                "method": "session/cancel",
                "note": "notification sent after resume; TUI may ignore if no turn is running",
            }, indent=2))
            return
        if args.cmd == "close":
            # Unload a live chat without deleting ~/.grok/sessions/... history.
            result = client.request(
                "session/close",
                {"sessionId": args.session_id},
                timeout=_timeout(15.0),
            )
            kept = find_session_dir(args.session_id) is not None
            meta = result.get("_meta") if isinstance(result, dict) else None
            outcome = None
            if isinstance(meta, dict):
                outcome = meta.get("x.ai/closeOutcome") or meta.get("closeOutcome")
            print(json.dumps({
                "ok": True,
                "sessionId": args.session_id,
                "method": "session/close",
                "closeOutcome": outcome,
                "diskKept": kept,
                "result": result,
            }, indent=2))
            return
        if args.cmd == "prompt":
            _resume(client, args.session_id, args.cwd)
            wait = bool(args.wait)
            timeout = float(args.timeout) if wait else min(float(args.timeout), 8.0)
            out = client.prompt_live(args.session_id, args.prompt, timeout=timeout, wait=wait)
            print(json.dumps({
                "ok": True,
                "queued": bool(out.get("queued")),
                "sessionId": args.session_id,
                "cwd": args.cwd,
                "text": out.get("text") or "",
                "result": out.get("result"),
            }, indent=2))
            return
    finally:
        client.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"ok": False, "at": _now_iso(), "error": str(e)}), file=sys.stderr)
        sys.exit(1)
