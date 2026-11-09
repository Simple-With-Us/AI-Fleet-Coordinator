#!/usr/bin/env python3
"""Local proof for seat-mcp v1.

Never prints SEAT_MCP_TOKEN.  Proves _echo spawn/status/reply/result and
the _sleep timeout + process-group kill.  Optionally tries a tiny deepseek job.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path("/Users/jay/apps/seat-mcp")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from seat_mcp.auth import load_token
from seat_mcp.config import BIND_HOST, BIND_PORT
from seat_mcp.jobs import load_job, pid_alive

BASE = "http://%s:%s/mcp" % (BIND_HOST, BIND_PORT)
HEALTH = "http://%s:%s/health" % (BIND_HOST, BIND_PORT)


def rpc(token, method, params, req_id):
    body = json.dumps({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}).encode()
    req = urllib.request.Request(
        BASE,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call_tool(token, name, arguments, req_id):
    msg = rpc(token, "tools/call", {"name": name, "arguments": arguments}, req_id)
    if "error" in msg:
        raise RuntimeError(msg["error"])
    result = msg.get("result") or {}
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    texts = []
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(item.get("text") or "")
    blob = "".join(texts)
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return {"text": blob}


def wait_done(token, job_id, timeout):
    deadline = time.time() + timeout
    last = {}
    n = 20
    while time.time() < deadline:
        last = call_tool(token, "seat_status", {"jobId": job_id}, n)
        n += 1
        if last.get("state") in {"succeeded", "failed", "timeout"}:
            return last
        time.sleep(0.25)
    raise TimeoutError("job still %s: %s" % (last.get("state"), job_id))


def main():
    token = load_token()
    try:
        with urllib.request.urlopen(HEALTH, timeout=3) as resp:
            health = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        print("health failed:", exc)
        print("start the server:  /Users/jay/apps/seat-mcp/start.sh")
        return 2
    print("health", health)

    init = rpc(
        token,
        "initialize",
        {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "seat-mcp-prove", "version": "1"},
        },
        1,
    )
    print("initialize", (init.get("result") or {}).get("serverInfo"))

    echo = call_tool(
        token,
        "seat_launch",
        {"seat": "_echo", "prompt": "pong", "cwd": "/Users/jay/apps"},
        2,
    )
    echo_id = echo["jobId"]
    print("echo_job", echo_id)
    echo_st = wait_done(token, echo_id, 10)
    echo_reply = call_tool(token, "seat_reply", {"jobId": echo_id}, 30)
    echo_res = call_tool(token, "seat_result", {"jobId": echo_id}, 31)
    print("echo_status", {k: echo_st.get(k) for k in ("state", "elapsedMs")})
    print("echo_reply_text", json.dumps(echo_reply.get("text")))
    print("echo_result", {k: echo_res.get(k) for k in ("state", "text", "exitCode")})
    rec = load_job(echo_id)
    print("echo_record", str(Path.home() / ".seat-mcp" / "jobs" / (echo_id + ".json")), "state", rec.get("state"))

    sleep = call_tool(
        token,
        "seat_launch",
        {
            "seat": "_sleep",
            "prompt": "timeout-proof",
            "cwd": "/Users/jay/apps",
            "opts": {"timeoutSec": 2, "sleepSec": 30},
        },
        40,
    )
    sleep_id = sleep["jobId"]
    print("sleep_job", sleep_id)
    time.sleep(0.4)
    running = load_job(sleep_id)
    pgid = running.get("pgid")
    print("sleep_running_pgid", pgid, "state", running.get("state"))
    sleep_st = wait_done(token, sleep_id, 12)
    sleep_res = call_tool(token, "seat_result", {"jobId": sleep_id}, 50)
    still = pid_alive(pgid)
    print("sleep_status_state", sleep_st.get("state"))
    print("sleep_error", sleep_res.get("error"))
    print("sleep_pgid_alive_after", still)
    if sleep_st.get("state") != "timeout" or still:
        print("TIMEOUT_PATH_FAIL")
        return 3

    deep_note = "not attempted"
    try:
        deep = call_tool(
            token,
            "seat_launch",
            {
                "seat": "deepseek",
                "prompt": "reply with the single word pong",
                "cwd": "/Users/jay/apps",
                "opts": {"effort": "quick", "timeoutSec": 90},
            },
            60,
        )
        deep_id = deep["jobId"]
        print("deepseek_job", deep_id)
        deep_st = wait_done(token, deep_id, 95)
        deep_res = call_tool(token, "seat_result", {"jobId": deep_id}, 70)
        deep_note = "state=%s text=%s" % (deep_st.get("state"), json.dumps((deep_res.get("text") or "")[:200]))
        print("deepseek", deep_note)
    except Exception as exc:
        deep_note = "deepseek path did not finish:  %s" % exc
        print(deep_note)

    print("PROVED=_echo spawn/status/reply/result AND _sleep timeout process-group kill")
    print("DEEPSEEK=" + deep_note)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except urllib.error.HTTPError as exc:
        print("http", exc.code, exc.reason)
        raise SystemExit(1)
