#!/usr/bin/env python3
"""Grok ACP WebSocket client for Conductor.

Uses the always-on grok-acp adapter on 127.0.0.1:12419 (--no-leader serve).
Empty mcpServers means "use config.toml" unless grok-acp is running from
its stripped GROK_HOME (acp-home), in which case empty means no MCPs.
Pass --mcp-server NAME to expand names from ~/.grok/config.toml.

Answers session/request_permission by selecting an offered allow option
(allow_always, then allow_once).  Implements ACP terminal/* so Grok's
run_terminal_command can finish.  Unknown server methods get JSON-RPC
-32601 instead of an empty {}.

Never prints GROK_AGENT_SECRET or MCP env/header values.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import websockets

from mcp_catalog import CatalogError, expand_names

HOST = "127.0.0.1"
PORT = 12419
WS_PATH = "/ws"
DEFAULT_OUTPUT_LIMIT = 1_048_576
FLEET_SECRETS = os.environ.get("FLEET_SECRETS_BIN") or str(Path.home() / ".local" / "bin" / "fleet-secrets")


def load_secret() -> str:
    """GROK_AGENT_SECRET from the environment, else Infisical Fleet-OPS /grok-acp (`fleet-secrets get`)."""
    secret = os.environ.get("GROK_AGENT_SECRET", "").strip()
    if not secret:
        try:
            proc = subprocess.run(
                [FLEET_SECRETS, "get", "GROK_AGENT_SECRET", "--path", "/grok-acp"],
                capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=60,
            )
            if proc.returncode == 0:
                secret = proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            secret = ""
    if not secret:
        sys.exit("GROK_AGENT_SECRET missing in Infisical /grok-acp")
    return secret


def is_rpc_response(msg: dict) -> bool:
    """JSON-RPC replies have id + result/error and no method."""
    if not isinstance(msg, dict):
        return False
    if "method" in msg:
        return False
    return "id" in msg and ("result" in msg or "error" in msg)


def emit_event(payload: dict) -> None:
    """One NDJSON object per line, flushed.  seat-mcp reads sessionId before prompt ends."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def pick_permission_result(params: dict | None) -> dict:
    """Select an offered allow option.  Prefer allow_always so later tools skip the prompt."""
    options = (params or {}).get("options") or []
    allow_always = None
    allow_once = None
    any_allow = None
    for opt in options:
        if not isinstance(opt, dict):
            continue
        oid = opt.get("optionId")
        if not oid:
            continue
        kind = str(opt.get("kind") or "").lower().replace("-", "_")
        oid_norm = str(oid).lower().replace("-", "_")
        if kind == "allow_always" or oid_norm == "allow_always":
            allow_always = oid
        elif kind == "allow_once" or oid_norm == "allow_once":
            allow_once = oid
        elif kind.startswith("allow") or oid_norm.startswith("allow"):
            any_allow = oid
    chosen = allow_always or allow_once or any_allow
    if chosen:
        return {"outcome": {"outcome": "selected", "optionId": chosen}}
    return {"outcome": {"outcome": "cancelled"}}


class _Term:
    def __init__(self, proc: subprocess.Popen, limit: int):
        self.proc = proc
        self.limit = max(1, int(limit))
        self.buf = bytearray()
        self.truncated = False
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        stdout = self.proc.stdout
        if stdout is None:
            return
        try:
            while True:
                chunk = stdout.read(4096)
                if not chunk:
                    break
                with self.lock:
                    self.buf.extend(chunk)
                    extra = len(self.buf) - self.limit
                    if extra > 0:
                        del self.buf[:extra]
                        self.truncated = True
        except Exception:
            return

    def snapshot(self) -> tuple[bytes, bool]:
        with self.lock:
            return bytes(self.buf), self.truncated


class TerminalHub:
    """In-process ACP terminal/* implementation.  Conductor has no TTY."""

    def __init__(self):
        self._terms: dict[str, _Term] = {}

    def create(self, params: dict | None) -> dict:
        params = params or {}
        command = params.get("command")
        if not command:
            raise ValueError("terminal/create missing command")
        args = list(params.get("args") or [])
        cmd = [str(command)] + [str(a) for a in args]
        if not args:
            cmd = ["/bin/zsh", "-lc", str(command)]
        env = os.environ.copy()
        for item in params.get("env") or []:
            if isinstance(item, dict) and item.get("name"):
                env[str(item["name"])] = str(item.get("value") or "")
        cwd = params.get("cwd") or None
        if cwd and not os.path.isdir(cwd):
            raise ValueError(f"terminal/create cwd missing: {cwd}")
        limit = int(params.get("outputByteLimit") or DEFAULT_OUTPUT_LIMIT)
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        tid = str(uuid.uuid4())
        self._terms[tid] = _Term(proc, limit)
        return {"terminalId": tid}

    def _get(self, params: dict | None) -> _Term:
        tid = (params or {}).get("terminalId")
        term = self._terms.get(tid) if tid else None
        if term is None:
            raise KeyError(f"unknown terminalId: {tid}")
        return term

    def output(self, params: dict | None) -> dict:
        term = self._get(params)
        raw, truncated = term.snapshot()
        result = {
            "output": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }
        if term.proc.poll() is not None:
            result["exitStatus"] = {"exitCode": term.proc.returncode, "signal": None}
        return result

    def wait_sync(self, params: dict | None) -> dict:
        term = self._get(params)
        term.proc.wait()
        if term.reader.is_alive():
            term.reader.join(timeout=2)
        return {"exitCode": term.proc.returncode, "signal": None}

    def kill(self, params: dict | None) -> dict:
        term = self._get(params)
        self._kill_proc(term.proc)
        return {}

    def release(self, params: dict | None) -> dict:
        tid = (params or {}).get("terminalId")
        term = self._terms.pop(tid, None) if tid else None
        if term is not None:
            self._kill_proc(term.proc)
        return {}

    def close_all(self) -> None:
        for tid in list(self._terms):
            term = self._terms.pop(tid, None)
            if term is not None:
                self._kill_proc(term.proc)

    @staticmethod
    def _kill_proc(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
            try:
                proc.wait(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
        if proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:
                pass


class AcpClient:
    def __init__(self, ws):
        self.ws = ws
        self._next_id = 1
        self._pending = {}
        self._send_lock = asyncio.Lock()
        self.terminals = TerminalHub()
        self._bg: set[asyncio.Task] = set()
        self._updates: list[dict] = []
        self._pump_task: asyncio.Task | None = None
        self.last_tool: str | None = None

    def _id(self) -> int:
        n = self._next_id
        self._next_id += 1
        return n

    async def _send(self, payload: dict) -> None:
        async with self._send_lock:
            await self.ws.send(json.dumps(payload))

    async def request(self, method: str, params, timeout: float = 60.0):
        req_id = self._id()
        fut = asyncio.get_event_loop().create_future()
        self._pending[req_id] = fut
        await self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout=timeout)

    async def _handle_server_request(self, msg: dict) -> None:
        method = msg.get("method") or ""
        req_id = msg.get("id")
        params = msg.get("params") or {}
        if method == "session/request_permission":
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": pick_permission_result(params)})
            return
        if method == "terminal/create":
            try:
                result = self.terminals.create(params)
            except Exception as exc:
                await self._send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": str(exc)},
                })
                return
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
            return
        if method == "terminal/output":
            try:
                result = self.terminals.output(params)
            except Exception as exc:
                await self._send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": str(exc)},
                })
                return
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
            return
        if method == "terminal/wait_for_exit":
            task = asyncio.create_task(self._wait_term(req_id, params))
            self._bg.add(task)
            task.add_done_callback(self._bg.discard)
            return
        if method == "terminal/kill":
            try:
                result = self.terminals.kill(params)
            except Exception as exc:
                await self._send({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32602, "message": str(exc)},
                })
                return
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
            return
        if method == "terminal/release":
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": self.terminals.release(params)})
            return
        await self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        })

    async def _wait_term(self, req_id, params: dict) -> None:
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self.terminals.wait_sync, params)
            await self._send({"jsonrpc": "2.0", "id": req_id, "result": result})
        except Exception:
            return

    def _note_update(self, msg: dict) -> None:
        self._updates.append(msg)
        upd = (msg.get("params") or {}).get("update") or {}
        kind = upd.get("sessionUpdate")
        if kind not in {"tool_call", "tool_call_update"}:
            return
        title = upd.get("title") or upd.get("kind") or upd.get("toolCallId")
        if not title:
            return
        self.last_tool = str(title)
        emit_event({"event": "tool", "lastTool": self.last_tool, "sessionUpdate": kind})

    async def start_pump(self) -> None:
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self.pump())

    async def pump(self, collect_updates=False):
        """One reader for the whole WebSocket.  Do not cancel between RPC calls."""
        try:
            await self._pump_loop()
        except Exception:
            return

    async def _pump_loop(self) -> None:
        async for raw in self.ws:
            if not raw:
                continue
            if raw == "ping":
                continue
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if is_rpc_response(msg) and msg["id"] in self._pending:
                fut = self._pending.pop(msg["id"])
                if "error" in msg:
                    fut.set_exception(RuntimeError(json.dumps(msg["error"])))
                else:
                    fut.set_result(msg.get("result", {}))
            elif "method" in msg and "id" in msg:
                await self._handle_server_request(msg)
            elif msg.get("method") == "session/update":
                self._note_update(msg)

    async def initialize(self):
        await self.start_pump()
        return await self.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientInfo": {"name": "conductor-grok-acp", "version": "3"},
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": True,
                },
            },
            timeout=45.0,
        )

    async def session_new(self, cwd: str, mcp_servers=None):
        await self.start_pump()
        return await self.request(
            "session/new",
            {
                "cwd": cwd,
                "mcpServers": mcp_servers or [],
                "_meta": {"yoloMode": True},
            },
            timeout=45.0,
        )

    async def session_list(self, cwd: str | None = None):
        await self.start_pump()
        params = {}
        if cwd:
            params["cwd"] = cwd
        return await self.request("session/list", params, timeout=45.0)

    async def session_load(self, session_id: str, cwd: str, mcp_servers=None):
        await self.start_pump()
        return await self.request(
            "session/load",
            {
                "sessionId": session_id,
                "cwd": cwd,
                "mcpServers": mcp_servers or [],
                "_meta": {"yoloMode": True},
            },
            timeout=45.0,
        )

    async def session_prompt(self, session_id: str, text: str, timeout: float = 900.0):
        await self.start_pump()
        start = len(self._updates)
        try:
            result = await self.request(
                "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]},
                timeout=timeout,
            )
        except Exception:
            self.terminals.close_all()
            raise
        chunks = []
        for msg in self._updates[start:]:
            upd = (msg.get("params") or {}).get("update") or {}
            if upd.get("sessionUpdate") == "agent_message_chunk":
                chunks.append(((upd.get("content") or {}).get("text")) or "")
        return result, "".join(chunks)


def _mcp_from_args(args) -> list:
    names = list(args.mcp_server or [])
    joined = getattr(args, "mcp_servers", None)
    if joined:
        names.extend(part.strip() for part in str(joined).split(",") if part.strip())
    try:
        return expand_names(names)
    except CatalogError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        sys.exit(2)


async def run(args):
    secret = load_secret()
    mcp_servers = _mcp_from_args(args) if args.cmd in {"new", "load"} else []
    url = f"ws://{HOST}:{PORT}{WS_PATH}?server-key={secret}"
    async with websockets.connect(
        url,
        additional_headers={"Authorization": f"Bearer {secret}"},
        open_timeout=10,
        ping_interval=None,
        ping_timeout=None,
    ) as ws:
        client = AcpClient(ws)
        try:
            init = await client.initialize()
            if args.cmd == "handshake":
                print(json.dumps({
                    "ok": True,
                    "listen": f"127.0.0.1:{PORT}",
                    "path": WS_PATH,
                    "protocolVersion": init.get("protocolVersion"),
                    "agent": (init.get("agentInfo") or init.get("agent") or {}),
                }, indent=2))
                return
            cwd = args.cwd or os.getcwd()
            timeout = float(getattr(args, "timeout", 900) or 900)
            if args.cmd == "list":
                listed = await client.session_list(cwd if args.cwd else None)
                sessions = listed.get("sessions") or []
                slim = []
                for s in sessions:
                    slim.append({
                        "sessionId": s.get("sessionId"),
                        "cwd": s.get("cwd"),
                        "title": s.get("title"),
                        "updatedAt": s.get("updatedAt"),
                    })
                print(json.dumps({"ok": True, "count": len(slim), "sessions": slim}, indent=2))
                return
            if args.cmd == "new":
                sess = await client.session_new(cwd, mcp_servers)
                session_id = sess.get("sessionId")
                emit_event({
                    "event": "session",
                    "sessionId": session_id,
                    "cwd": cwd,
                    "mcpCount": len(mcp_servers),
                    "mcpNames": [s.get("name") for s in mcp_servers],
                })
                text = ""
                if args.prompt:
                    _, text = await client.session_prompt(session_id, args.prompt, timeout=timeout)
                emit_event({
                    "event": "done",
                    "sessionId": session_id,
                    "cwd": cwd,
                    "mcpCount": len(mcp_servers),
                    "mcpNames": [s.get("name") for s in mcp_servers],
                    "text": text,
                })
                return
            if args.cmd == "load":
                if not args.session_id:
                    sys.exit("--session-id required")
                loaded = await client.session_load(args.session_id, cwd, mcp_servers)
                emit_event({
                    "event": "session",
                    "ok": True,
                    "sessionId": args.session_id,
                    "cwd": cwd,
                    "mcpCount": len(mcp_servers),
                    "mcpNames": [s.get("name") for s in mcp_servers],
                    "loaded": {k: loaded.get(k) for k in ("sessionId", "cwd") if k in loaded},
                })
                text = ""
                if args.prompt:
                    _, text = await client.session_prompt(args.session_id, args.prompt, timeout=timeout)
                emit_event({
                    "event": "done",
                    "ok": True,
                    "sessionId": args.session_id,
                    "cwd": cwd,
                    "mcpCount": len(mcp_servers),
                    "mcpNames": [s.get("name") for s in mcp_servers],
                    "text": text,
                })
                return
            if args.cmd == "prompt":
                if not args.session_id:
                    sys.exit("--session-id required")
                if not args.prompt:
                    sys.exit("--prompt required")
                emit_event({"event": "session", "sessionId": args.session_id})
                result, text = await client.session_prompt(args.session_id, args.prompt, timeout=timeout)
                emit_event({"event": "done", "sessionId": args.session_id, "text": text, "result": result})
                return
        finally:
            client.terminals.close_all()


def main():
    p = argparse.ArgumentParser(description="Conductor client for local grok-acp")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("handshake", help="initialize only — no session, no model turn")

    def _add_mcp_flags(parser):
        parser.add_argument(
            "--mcp-server",
            action="append",
            default=[],
            help="MCP server name from ~/.grok/config.toml (repeatable)",
        )
        parser.add_argument(
            "--mcp-servers",
            default="",
            help="Comma-separated MCP server names from ~/.grok/config.toml",
        )

    n = sub.add_parser("new", help="session/new, optional first prompt")
    n.add_argument("--cwd", default="/Users/jay")
    n.add_argument("--prompt")
    _add_mcp_flags(n)
    n.add_argument("--timeout", type=float, default=900.0)
    ls = sub.add_parser("list", help="session/list — local chats on the shared leader")
    ls.add_argument("--cwd", default="")
    ld = sub.add_parser("load", help="session/load an existing local chat, optional prompt")
    ld.add_argument("--session-id", required=True)
    ld.add_argument("--cwd", default="/Users/jay")
    ld.add_argument("--prompt")
    _add_mcp_flags(ld)
    ld.add_argument("--timeout", type=float, default=900.0)
    pr = sub.add_parser("prompt", help="session/prompt follow-up")
    pr.add_argument("--session-id", required=True)
    pr.add_argument("--prompt", required=True)
    pr.add_argument("--cwd", default="/Users/jay")
    pr.add_argument("--timeout", type=float, default=900.0)
    args = p.parse_args()
    try:
        asyncio.get_event_loop().run_until_complete(run(args))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
