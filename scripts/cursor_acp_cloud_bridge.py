#!/usr/bin/env python3
"""ACP stdio server that turns Shellular Cursor chats into Cloud Agents.

Shellular speaks Agent Client Protocol to `cursor-agent acp`.  Those local
sessions never appear in the desktop chat list or iOS Cursor.  This process
implements enough ACP to stay a Shellular spawn target, then creates or
follows up a Cloud Agent so desktop Agents Window and iOS share the same
bc- id.

If CURSOR_API_KEY / CURSOR_SYNC_API_KEY is missing, or CURSOR_ACP_LOCAL=1, exec local cursor-agent
acp instead (same as stock Shellular).

Stdout is JSON-RPC only.  Logs go to stderr and ~/.cursor/fleet-chat-surfaces/bridge.log.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, TextIO

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from cursor_chat_surfaces import (  # noqa: E402
    agent_web_url,
    create_cloud_agent,
    extract_prompt_text,
    follow_up_cloud_agent,
    get_conversation,
    get_cloud_agent,
    load_api_key,
    open_url,
    put_session_mapping,
    session_map,
    unwrap_created_agent,
    worker_running,
)

JsonDict = dict[str, Any]
LOG_PATH = Path.home() / ".cursor" / "fleet-chat-surfaces" / "bridge.log"


def log(message: str) -> None:
    line = message.rstrip() + "\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(time.strftime("%Y-%m-%dT%H:%M:%S%z ") + line)
    except OSError:
        pass


def exec_local_acp() -> None:
    binary = shutil.which("cursor-agent") or str(Path.home() / ".local" / "bin" / "cursor-agent")
    log(f"passthrough local ACP via {binary}")
    os.execv(binary, [binary, "acp"])


class AcpBridge:
    def __init__(self, api_key: str) -> None:
        self.api_key = api_key
        self.sessions: dict[str, JsonDict] = {}
        self._out_lock = threading.Lock()
        self.on_mac = os.environ.get("CURSOR_BRIDGE_ON_MAC", "") == "1"
        self.machine_name = os.environ.get("CURSOR_BRIDGE_MACHINE", "jay-mac")
        self.open_desktop = os.environ.get("CURSOR_BRIDGE_OPEN", "1") != "0"

    def write(self, payload: JsonDict) -> None:
        raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        with self._out_lock:
            sys.stdout.write(raw + "\n")
            sys.stdout.flush()

    def reply(self, req_id: Any, result: Any) -> None:
        self.write({"jsonrpc": "2.0", "id": req_id, "result": result})

    def fail(self, req_id: Any, message: str, code: int = -32000) -> None:
        self.write(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            }
        )

    def notify(self, method: str, params: JsonDict) -> None:
        self.write({"jsonrpc": "2.0", "method": method, "params": params})

    def session_update(self, session_id: str, update: JsonDict) -> None:
        self.notify("session/update", {"sessionId": session_id, "update": update})

    def emit_text(self, session_id: str, text: str) -> None:
        if not text:
            return
        self.session_update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": text},
            },
        )

    def handle_initialize(self, req_id: Any, params: JsonDict) -> None:
        version = params.get("protocolVersion", 1)
        self.reply(
            req_id,
            {
                "protocolVersion": version,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {
                        "image": False,
                        "audio": False,
                        "embeddedContext": True,
                    },
                },
                "agentInfo": {
                    "name": "cursor-cloud-bridge",
                    "version": "1.0.0",
                },
                "authMethods": [],
            },
        )

    def handle_authenticate(self, req_id: Any, params: JsonDict) -> None:
        del params
        self.reply(req_id, {})

    def handle_session_new(self, req_id: Any, params: JsonDict) -> None:
        session_id = str(uuid.uuid4())
        cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else os.getcwd()
        self.sessions[session_id] = {"cwd": cwd, "agentId": None, "url": None}
        self.reply(
            req_id,
            {
                "sessionId": session_id,
                "modes": {
                    "currentModeId": "agent",
                    "availableModes": [
                        {"id": "agent", "name": "Agent"},
                        {"id": "plan", "name": "Plan"},
                        {"id": "ask", "name": "Ask"},
                    ],
                },
            },
        )

    def handle_session_load(self, req_id: Any, params: JsonDict) -> None:
        session_id = str(params.get("sessionId") or "")
        mapped = session_map().get(session_id)
        cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else os.getcwd()
        record: JsonDict = {"cwd": cwd, "agentId": None, "url": None}
        if isinstance(mapped, dict):
            record["agentId"] = mapped.get("agentId")
            record["url"] = mapped.get("url")
        self.sessions[session_id] = record
        self.reply(
            req_id,
            {
                "sessionId": session_id,
                "modes": {
                    "currentModeId": "agent",
                    "availableModes": [
                        {"id": "agent", "name": "Agent"},
                        {"id": "plan", "name": "Plan"},
                        {"id": "ask", "name": "Ask"},
                    ],
                },
            },
        )

    def handle_session_cancel(self, params: JsonDict) -> None:
        session_id = str(params.get("sessionId") or "")
        record = self.sessions.get(session_id) or {}
        record["cancel"] = True

    def handle_session_prompt(self, req_id: Any, params: JsonDict) -> None:
        session_id = str(params.get("sessionId") or "")
        record = self.sessions.setdefault(session_id, {"cwd": os.getcwd()})
        record["cancel"] = False
        turn_id = uuid.uuid4().hex
        record["turn_id"] = turn_id
        text = extract_prompt_text(params.get("prompt"))
        if not text:
            self.fail(req_id, "empty prompt")
            return
        on_mac = self.on_mac and worker_running()
        try:
            baseline_ids: set[str] = set()
            baseline_count = 0
            if record.get("agentId"):
                agent_id = str(record["agentId"])
                existing_convo = get_conversation(self.api_key, agent_id)
                baseline_msgs = self._assistant_messages(existing_convo)
                baseline_ids = {m["id"] for m in baseline_msgs}
                baseline_count = len(baseline_msgs)
                payload = follow_up_cloud_agent(self.api_key, agent_id, text)
                url = str(record.get("url") or agent_web_url(agent_id))
            else:
                payload = create_cloud_agent(
                    self.api_key,
                    text,
                    str(record.get("cwd") or os.getcwd()),
                    name="Shellular Cursor",
                    on_mac=on_mac,
                    machine_name=self.machine_name,
                )
                agent = unwrap_created_agent(payload)
                agent_id = str(agent.get("id") or "")
                url = str(agent.get("url") or agent_web_url(agent_id))
                record["agentId"] = agent_id
                record["url"] = url
                put_session_mapping(session_id, agent)
                if self.open_desktop:
                    open_url(url)
            log(f"session={session_id} agent={agent_id} prompt_chars={len(text)}")
            self.emit_text(
                session_id,
                (
                    "This Shellular Cursor chat is a Cloud Agent, so it shows in "
                    "desktop Cursor (Agents Window) and iOS Cursor.\n"
                    f"{url}\n\n"
                ),
            )
            self._mirror_conversation(
                session_id,
                agent_id,
                payload,
                baseline_ids=baseline_ids,
                baseline_count=baseline_count,
                turn_id=turn_id,
            )
            self.reply(req_id, {"stopReason": "end_turn"})
        except Exception as err:
            log(f"prompt failed: {err}")
            self.fail(req_id, str(err))

    def _assistant_messages(self, conversation: JsonDict | None) -> list[JsonDict]:
        if not conversation:
            return []
        messages = conversation.get("messages") or conversation.get("conversation") or []
        items: list[JsonDict] = []
        if not isinstance(messages, list):
            return items
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            role = str(msg.get("role") or msg.get("type") or "").lower()
            if role in {"assistant", "agent", "ai"}:
                content = msg.get("content") or msg.get("text") or ""
                text = ""
                if isinstance(content, str) and content.strip():
                    text = content
                elif isinstance(content, list):
                    joined = extract_prompt_text(content)
                    if joined:
                        text = joined
                if text:
                    msg_id = str(msg.get("id") or f"idx_{i}")
                    items.append({"id": msg_id, "text": text, "index": i})
        return items

    def _assistant_texts(self, conversation: JsonDict | None) -> list[str]:
        return [m["text"] for m in self._assistant_messages(conversation)]

    def _mirror_conversation(
        self,
        session_id: str,
        agent_id: str,
        created: JsonDict,
        baseline_ids: set[str] | None = None,
        baseline_count: int = 0,
        turn_id: str | None = None,
        baseline_texts: set[str] | None = None,
    ) -> None:
        seen_ids: set[str] = set(baseline_ids or ())
        new_emitted = 0
        convo = get_conversation(self.api_key, agent_id)
        if convo is None:
            self.emit_text(
                session_id,
                "The Cloud Agent is running.  Follow and control it in the desktop "
                "Agents Window or the iOS Cursor inbox (same thread).\n",
            )
            return
        deadline = time.time() + 90
        while time.time() < deadline:
            rec = self.sessions.get(session_id) or {}
            if rec.get("cancel") or (turn_id and rec.get("turn_id") != turn_id):
                return
            convo = get_conversation(self.api_key, agent_id)
            msgs = self._assistant_messages(convo)
            for i, m in enumerate(msgs):
                msg_id = m["id"]
                if msg_id in seen_ids or (not baseline_ids and i < baseline_count):
                    continue
                if baseline_texts and m["text"] in baseline_texts:
                    continue
                seen_ids.add(msg_id)
                new_emitted += 1
                text = m["text"]
                self.emit_text(session_id, text if text.endswith("\n") else text + "\n")
            try:
                info = get_cloud_agent(self.api_key, agent_id)
            except RuntimeError:
                info = {}
            status = str(info.get("status") or "").upper()
            run = created.get("run") if isinstance(created.get("run"), dict) else None
            run_status = str((run or {}).get("status") or "").upper()
            if status in {"ARCHIVED", "COMPLETED", "FINISHED"} or run_status in {"FINISHED", "ERROR", "CANCELLED", "EXPIRED", "COMPLETED"}:
                return
            if status == "IDLE" and new_emitted > 0:
                return
            time.sleep(2.5)
        if new_emitted == 0:
            self.emit_text(
                session_id,
                "The Cloud Agent is still running.  Follow it live in the desktop "
                "Agents Window or the iOS Cursor inbox.\n",
            )


def read_messages(stream: TextIO) -> Any:
    for line in stream:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            yield json.loads(stripped)
        except json.JSONDecodeError:
            log(f"bad json-rpc line chars={len(stripped)}")


def serve(api_key: str) -> int:
    bridge = AcpBridge(api_key)
    log("cloud ACP bridge ready")
    for message in read_messages(sys.stdin):
        if not isinstance(message, dict):
            continue
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        try:
            if method == "initialize":
                bridge.handle_initialize(req_id, params)
            elif method == "authenticate":
                bridge.handle_authenticate(req_id, params)
            elif method == "session/new":
                bridge.handle_session_new(req_id, params)
            elif method == "session/load":
                bridge.handle_session_load(req_id, params)
            elif method == "session/prompt":
                threading.Thread(
                    target=bridge.handle_session_prompt,
                    args=(req_id, params),
                    daemon=True,
                ).start()
            elif method == "session/cancel":
                bridge.handle_session_cancel(params)
            elif req_id is not None:
                bridge.fail(req_id, f"method not implemented: {method}", code=-32601)
        except Exception:
            log(traceback.format_exc())
            if req_id is not None:
                bridge.fail(req_id, "internal error")
    return 0


def main() -> int:
    if os.environ.get("CURSOR_ACP_LOCAL", "") == "1":
        exec_local_acp()
    key = load_api_key()
    if not key:
        exec_local_acp()
    return serve(key)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError:
        raise
    except Exception:
        log(traceback.format_exc())
        sys.exit(1)
