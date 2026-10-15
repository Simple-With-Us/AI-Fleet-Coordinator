#!/usr/bin/env python3
"""Drive a live Mac Grok TUI session from any local agent.

list / peek / tail / prompt / await / cancel / close  → shared leader + disk
new                                                   → grok-acp :12419 (not the TUI)

Not Grok-Bot-only.  Claude, Cursor, this TUI, Shellular, … all use the same CLI.
Never prints GROK_AGENT_SECRET or SEAT_MCP_TOKEN.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from session_disk import (  # noqa: E402
    enrich_sessions,
    is_self_session,
    peek_summary,
    peek_tail,
    poll_after_inject,
    poll_until_idle,
    prefix_prompt,
    turn_state,
)

LEADER = HERE / "leader-client.py"
ACP = HERE / "acp-client.py"
PY = "/usr/bin/python3"


def run_json(argv, timeout):
    proc = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    blob = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    data = None
    src = blob or err
    if src:
        try:
            data = json.loads(src)
        except json.JSONDecodeError:
            start = src.find("{")
            try:
                data = json.loads(src[start:] if start >= 0 else src)
            except json.JSONDecodeError:
                data = {"ok": False, "error": src[:800]}
    if data is None:
        data = {"ok": False, "error": "empty helper output", "exitCode": proc.returncode}
    if proc.returncode != 0 and data.get("ok") is not False:
        data = dict(data)
        data["ok"] = False
        data.setdefault("exitCode", proc.returncode)
    return data, proc.returncode


def cmd_list(args):
    argv = [PY, str(LEADER), "list"]
    if args.cwd:
        argv.extend(["--cwd", args.cwd])
    data, code = run_json(argv, timeout=25)
    sessions = enrich_sessions(data.get("sessions") or [])
    out = {
        "ok": bool(data.get("ok", code == 0)) or bool(sessions),
        "count": len(sessions),
        "sessions": sessions,
    }
    if data.get("error"):
        out["leaderError"] = data.get("error")
        if sessions:
            out["ok"] = True
            out["partial"] = True
    print(json.dumps(out, indent=2))
    return 0 if out.get("ok") else 1


def cmd_peek(args):
    data = peek_summary(args.session_id)
    if args.tail:
        tail = peek_tail(args.session_id, lines=int(args.tail))
        data["tail"] = tail.get("tail") or []
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_tail(args):
    data = peek_tail(args.session_id, lines=int(args.lines))
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_await(args):
    data = poll_until_idle(args.session_id, timeout=float(args.timeout))
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_prompt(args):
    if is_self_session(args.session_id) and not args.self:
        print(json.dumps({
            "ok": False,
            "error": (
                "refusing to prompt this process's own TUI "
                "($GROK_SESSION_ID).  Pass --self if you really mean it."
            ),
            "sessionId": args.session_id,
            "self": True,
        }, indent=2))
        return 3
    st = turn_state(args.session_id)
    if st.get("turnState") in {"working", "needs-input"} and not args.queue:
        extra = {
            "ok": False,
            "error": "session is %s (phase %s).  Pass --queue to inject anyway."
            % (st.get("turnState"), st.get("phase")),
            "sessionId": args.session_id,
            "turnState": st.get("turnState"),
            "phase": st.get("phase"),
        }
        if st.get("pendingTool"):
            extra["pendingTool"] = st.get("pendingTool")
        print(json.dumps(extra, indent=2))
        return 2
    before_started = float(st.get("turnStartedAt") or 0.0)
    text = prefix_prompt(args.prompt, args.from_name)
    argv = [
        PY, str(LEADER), "prompt",
        "--session-id", args.session_id,
        "--cwd", args.cwd,
        "--prompt", text,
        "--timeout", str(args.timeout),
    ]
    if args.wait:
        argv.append("--wait")
    data, code = run_json(argv, timeout=float(args.timeout) + 20)
    data = dict(data or {})
    if code != 0 and not data.get("receipt"):
        data.setdefault(
            "error",
            data.get("error") or "no correlated receipt for session/prompt (timeout or leader error)",
        )
    data["turnState"] = st.get("turnState")
    data["from"] = args.from_name or os.environ.get("AGENT_TAG") or os.environ.get("AGENT_SEAT") or "remote"
    if args.await_reply and data.get("ok"):
        waited = poll_after_inject(
            args.session_id,
            before_started=before_started,
            timeout=float(args.await_reply),
        )
        data["reply"] = waited
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_cancel(args):
    argv = [
        PY, str(LEADER), "cancel",
        "--session-id", args.session_id,
        "--cwd", args.cwd,
    ]
    data, code = run_json(argv, timeout=20)
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_close(args):
    """Unload MCP for a live chat.  Does not delete disk history."""
    if is_self_session(args.session_id) and not args.self:
        print(json.dumps({
            "ok": False,
            "error": (
                "refusing to close this process's own TUI "
                "($GROK_SESSION_ID).  Pass --self if you really mean it."
            ),
            "sessionId": args.session_id,
            "self": True,
        }, indent=2))
        return 3
    st = turn_state(args.session_id)
    if st.get("turnState") in {"working", "needs-input"} and not args.force:
        extra = {
            "ok": False,
            "error": "session is %s (phase %s).  Pass --force to close anyway."
            % (st.get("turnState"), st.get("phase")),
            "sessionId": args.session_id,
            "turnState": st.get("turnState"),
            "phase": st.get("phase"),
        }
        if st.get("pendingTool"):
            extra["pendingTool"] = st.get("pendingTool")
        print(json.dumps(extra, indent=2))
        return 2
    argv = [
        PY, str(LEADER), "close",
        "--session-id", args.session_id,
        "--cwd", args.cwd,
    ]
    data, code = run_json(argv, timeout=20)
    print(json.dumps(data, indent=2))
    return 0 if data.get("ok") else 1


def cmd_new(args):
    argv = [PY, str(ACP), "new", "--cwd", args.cwd, "--prompt", args.prompt]
    data, code = run_json(argv, timeout=float(args.timeout) + 20)
    print(json.dumps(data, indent=2))
    return code


def main():
    p = argparse.ArgumentParser(description="Drive a live Mac Grok TUI from any local agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list", help="TUI chats + turnState (idle/working/needs-input)")
    ls.add_argument("--cwd", default="")
    pk = sub.add_parser("peek", help="disk summary; optional --tail N of updates.jsonl")
    pk.add_argument("--session-id", required=True)
    pk.add_argument("--cwd", default="/Users/jay")
    pk.add_argument("--tail", type=int, default=0)
    tl = sub.add_parser("tail", help="last N live transcript chunks")
    tl.add_argument("--session-id", required=True)
    tl.add_argument("--lines", type=int, default=12)
    aw = sub.add_parser("await", help="poll disk until the open turn ends")
    aw.add_argument("--session-id", required=True)
    aw.add_argument("--timeout", type=float, default=180.0)
    pr = sub.add_parser("prompt", help="inject a follow-up; returns queued unless --wait/--await-reply")
    pr.add_argument("--session-id", required=True)
    pr.add_argument("--prompt", required=True)
    pr.add_argument("--cwd", default="/Users/jay")
    pr.add_argument("--timeout", type=float, default=12.0)
    pr.add_argument("--wait", action="store_true", help="wait on ACP for the TUI turn (usually the wrong tool)")
    pr.add_argument("--await-reply", type=float, default=0.0, help="after queue, poll disk this many seconds")
    pr.add_argument("--queue", action="store_true", help="inject even if the TUI is working / needs-input")
    pr.add_argument("--from-name", "--from", dest="from_name", default="", help="prefix [from: NAME]; default AGENT_TAG / remote")
    pr.add_argument(
        "--self",
        action="store_true",
        help="allow prompt when sessionId equals $GROK_SESSION_ID (this TUI)",
    )
    ca = sub.add_parser("cancel", help="best-effort session/cancel on the live TUI")
    ca.add_argument("--session-id", required=True)
    ca.add_argument("--cwd", default="/Users/jay")
    cl = sub.add_parser("close", help="session/close: unload MCP tools, keep the transcript")
    cl.add_argument("--session-id", required=True)
    cl.add_argument("--cwd", default="/Users/jay")
    cl.add_argument("--force", action="store_true", help="close even if working / needs-input")
    cl.add_argument(
        "--self",
        action="store_true",
        help="allow close when sessionId equals $GROK_SESSION_ID (this TUI)",
    )
    nw = sub.add_parser("new", help="new grok-acp session on :12419 (not the TUI)")
    nw.add_argument("--prompt", required=True)
    nw.add_argument("--cwd", default="/Users/jay/apps")
    nw.add_argument("--timeout", type=float, default=180.0)
    args = p.parse_args()
    if args.cmd == "list":
        raise SystemExit(cmd_list(args))
    if args.cmd == "peek":
        raise SystemExit(cmd_peek(args))
    if args.cmd == "tail":
        raise SystemExit(cmd_tail(args))
    if args.cmd == "await":
        raise SystemExit(cmd_await(args))
    if args.cmd == "prompt":
        raise SystemExit(cmd_prompt(args))
    if args.cmd == "cancel":
        raise SystemExit(cmd_cancel(args))
    if args.cmd == "close":
        raise SystemExit(cmd_close(args))
    if args.cmd == "new":
        raise SystemExit(cmd_new(args))


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired:
        print(json.dumps({"ok": False, "error": "helper timed out"}), file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        sys.exit(1)
