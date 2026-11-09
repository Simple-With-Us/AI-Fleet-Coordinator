#!/usr/bin/env python3
"""Claude Code Stop hook: nudge a session that did substantial work to commit a lesson.

Reads the Stop hook JSON on stdin (session_id, transcript_path, stop_hook_active).  Streams the
transcript JSONL once, counting assistant tool_use blocks and looking for evidence that a lesson
was handled: a recall_contribute tool use (any MCP prefix), a Bash command containing
"recall contribute", or an assistant text reply containing "no lesson".  When the session used
at least MIN_TOOLS tools (default 25, env FLEET_RECALL_HOOK_MIN_TOOLS) and nothing was
committed, it writes a per-session marker under $HOME/apps/fleet-rag/state/hook-nudged/ and
prints {"decision": "block", "reason": ...} once; every later stop of that session passes.

Exit 0 immediately when stop_hook_active is true (loop guard), when FLEET_RECALL_HOOKS=0, or
when the marker exists.  Never raises: any exception -> silent exit 0.  Stdlib only; installed
to ~/.claude/hooks/ by scripts/install-fleet-rag.sh --hooks.
"""
from __future__ import annotations

import json
import os
import re
import sys

MIN_TOOLS_DEFAULT = 25
REASON = ("Fleet recall: this session did substantial work and has not committed a lesson.  If you "
          "learned something reusable (a gotcha, a measured number, an owner preference, a runbook "
          "step), call recall_contribute once now (search first).  If there is genuinely nothing "
          "reusable, reply with the words 'no lesson' and finish.")
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]")
_CONTRIB_CMD = re.compile(r"recall\s+contribute\b")


def marker_path(home: str, session_id: str) -> str:
    sid = _SAFE_ID.sub("_", str(session_id))[:128] or "unknown"
    return os.path.join(home, "apps", "fleet-rag", "state", "hook-nudged", sid)


def _texts(content) -> list[str]:
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                out.append(b["text"])
    return out


def scan_transcript(path: str) -> dict:
    """{"tool_uses": int, "contributed": bool, "no_lesson": bool} from one streamed pass."""
    tool_uses = 0
    contributed = False
    no_lesson = False
    with open(path, "rb") as fh:
        for raw in fh:
            # Cheap pre-filter: only assistant lines can carry tool_use blocks or replies.
            if b'"assistant"' not in raw:
                continue
            if b"tool_use" not in raw and b"no lesson" not in raw.lower():
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(rec, dict) or rec.get("type") != "assistant":
                continue
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    tool_uses += 1
                    name = str(b.get("name") or "")
                    if name == "recall_contribute" or name.endswith("__recall_contribute"):
                        contributed = True
                    elif name == "Bash":
                        inp = b.get("input")
                        cmd = inp.get("command") if isinstance(inp, dict) else None
                        if isinstance(cmd, str) and _CONTRIB_CMD.search(cmd):
                            contributed = True
            for t in _texts(content):
                if "no lesson" in t.lower():
                    no_lesson = True
    return {"tool_uses": tool_uses, "contributed": contributed, "no_lesson": no_lesson}


def decide(hook: dict, home: str, min_tools: int) -> dict | None:
    """Return the block decision or None.  Writes the marker when blocking."""
    if hook.get("stop_hook_active"):
        return None
    session_id = str(hook.get("session_id") or "")
    transcript = hook.get("transcript_path")
    if not session_id or not isinstance(transcript, str) or not os.path.isfile(transcript):
        return None
    marker = marker_path(home, session_id)
    if os.path.exists(marker):
        return None
    scan = scan_transcript(transcript)
    if scan["tool_uses"] < min_tools or scan["contributed"] or scan["no_lesson"]:
        return None
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"tool_uses": scan["tool_uses"]}) + "\n")
    return {"decision": "block", "reason": REASON}


def main() -> int:
    try:
        if os.environ.get("FLEET_RECALL_HOOKS", "1") == "0":
            return 0
        raw = sys.stdin.read()
        hook = json.loads(raw) if raw.strip() else {}
        if not isinstance(hook, dict):
            return 0
        try:
            min_tools = int(os.environ.get("FLEET_RECALL_HOOK_MIN_TOOLS", MIN_TOOLS_DEFAULT))
        except ValueError:
            min_tools = MIN_TOOLS_DEFAULT
        out = decide(hook, os.path.expanduser("~"), min_tools)
        if out:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()
    except Exception:  # noqa: BLE001 - a hook must never break the session
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
