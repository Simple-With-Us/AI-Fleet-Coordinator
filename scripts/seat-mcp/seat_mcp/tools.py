"""MCP tools:  seat_launch, seat_status, seat_reply, seat_result, grok_session_*, recall_*.

Async jobs only for seats:  seat_launch returns a jobId immediately.  The recall_* tools are
synchronous reads / one small write against the fleet-agents corpus (see recall_bridge).
"""

from __future__ import annotations

from typing import Any

from . import grok_tui, jobs, recall_bridge
from .config import DEFAULT_CWD, IMPLEMENTED_SEATS, TEST_SEATS, WEDGE_SEC
from .runner import launch_async
from .seats import SeatError, stuff_prior, validate_cwd, validate_tui_cwd

JsonDict = dict[str, Any]


def _opts(raw: Any) -> JsonDict:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SeatError("opts must be an object")
    # Never accept a permission / sandbox mode.  v1 DeepSeek is read-only.
    forbidden = {"mode", "permissionMode", "permission_mode", "sandbox", "dshPermissionMode"}
    hit = [k for k in raw if k in forbidden]
    if hit:
        raise SeatError("mode is not a parameter.  DeepSeek v1 is always read-only.")
    return dict(raw)


def seat_launch(arguments: JsonDict) -> JsonDict:
    seat = str(arguments.get("seat") or "").strip()
    prompt = arguments.get("prompt")
    if not seat:
        raise SeatError("seat is required")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SeatError("prompt is required")
    opts = _opts(arguments.get("opts"))
    if seat == "grok-tui":
        cwd = _tui_cwd(arguments.get("cwd"), opts.get("sessionId") or opts.get("session_id"))
    else:
        cwd = validate_cwd(arguments.get("cwd"))

    if seat == "deepseek" and (opts.get("sessionId") or opts.get("session_id")):
        raise SeatError(
            "DSH headless cannot resume a session.  "
            "Official path is one submitted task.  "
            "Pass opts.priorJobId to stuff prior text into a new one-shot, "
            "or omit sessionId."
        )
    if seat == "grok-tui" and not (opts.get("sessionId") or opts.get("session_id")):
        raise SeatError(
            "grok-tui requires opts.sessionId.  Call grok_sessions_list first."
        )

    if seat == "grok":
        busy = jobs.running_grok_job()
        if busy:
            raise SeatError(
                "grok ACP job already running:  %s (state %s).  "
                "One github-only session/prompt at a time.  Poll seat_status on that jobId, "
                "or wait until it finishes.  Do not resume dead 01a050*/01a051* sessions."
                % (busy.get("jobId"), busy.get("state"))
            )

    prior_id = opts.get("priorJobId") or opts.get("prior_job_id")
    launch_prompt = prompt.strip()
    session_carry = opts.get("sessionId") or opts.get("session_id")
    if prior_id:
        prior = jobs.load_job(str(prior_id))
        if seat == "deepseek" or not session_carry:
            launch_prompt = stuff_prior(
                str(prior.get("prompt") or ""),
                str(prior.get("text") or ""),
                prompt.strip(),
            )
        # grok: never auto session/load a prior sessionId.  Fresh session/new
        # unless the caller passed opts.sessionId explicitly (seat_reply).

    rec = jobs.new_record(
        seat=seat,
        prompt=launch_prompt,
        cwd=cwd,
        opts=opts,
        prior_job_id=str(prior_id) if prior_id else None,
    )
    launch_async(rec["jobId"])
    return {"jobId": rec["jobId"]}


def seat_status(arguments: JsonDict) -> JsonDict:
    job_id = str(arguments.get("jobId") or arguments.get("job_id") or "").strip()
    if not job_id:
        raise SeatError("jobId is required")
    rec = jobs.load_job(job_id)
    return jobs.status_view(rec, WEDGE_SEC)


def seat_reply(arguments: JsonDict) -> JsonDict:
    """Read current text, or launch a follow-up when `prompt` is set.

    Follow-up is async:  returns a new jobId.  DeepSeek follow-up is a new
    one-shot with prior text stuffed.  Grok follow-up uses session/prompt.
    """
    job_id = str(arguments.get("jobId") or arguments.get("job_id") or "").strip()
    if not job_id:
        raise SeatError("jobId is required")
    rec = jobs.load_job(job_id)
    follow = arguments.get("prompt")
    if follow is None or (isinstance(follow, str) and not follow.strip()):
        return {
            "jobId": rec.get("jobId"),
            "state": rec.get("state"),
            "text": rec.get("text") or "",
        }
    if not isinstance(follow, str):
        raise SeatError("prompt must be a string")
    seat = str(rec.get("seat") or "")
    opts: JsonDict = {}
    if seat in {"grok", "grok-tui"} and rec.get("sessionId"):
        opts["sessionId"] = rec.get("sessionId")
    elif seat == "deepseek":
        opts["priorJobId"] = job_id
    else:
        opts["priorJobId"] = job_id
    timeout = (rec.get("opts") or {}).get("timeoutSec")
    if timeout:
        opts["timeoutSec"] = timeout
    effort = (rec.get("opts") or {}).get("effort")
    if effort:
        opts["effort"] = effort
    mcp = (rec.get("opts") or {}).get("mcpServers")
    if mcp and seat == "grok" and not opts.get("sessionId"):
        opts["mcpServers"] = mcp
    return seat_launch(
        {
            "seat": seat,
            "prompt": follow,
            "cwd": rec.get("cwd"),
            "opts": opts,
        }
    )


def _tui_cwd(cwd, session_id) -> str:
    if cwd:
        return validate_tui_cwd(cwd)
    sid = str(session_id or "").strip()
    if sid:
        for row in grok_tui.load_active():
            if row.get("sessionId") == sid and row.get("cwd"):
                return validate_tui_cwd(str(row["cwd"]))
    return validate_tui_cwd(str(DEFAULT_CWD))


def grok_sessions_list(arguments: JsonDict) -> JsonDict:
    cwd = arguments.get("cwd")
    cwd_s = str(cwd).strip() if isinstance(cwd, str) and cwd.strip() else None
    return grok_tui.list_sessions(cwd_s)


def grok_session_peek(arguments: JsonDict) -> JsonDict:
    session_id = str(arguments.get("sessionId") or arguments.get("session_id") or "").strip()
    if not session_id:
        raise SeatError("sessionId is required")
    cwd_s = _tui_cwd(arguments.get("cwd"), session_id)
    return grok_tui.peek_session(session_id, cwd_s)


def grok_session_tail(arguments: JsonDict) -> JsonDict:
    session_id = str(arguments.get("sessionId") or arguments.get("session_id") or "").strip()
    if not session_id:
        raise SeatError("sessionId is required")
    lines = int(arguments.get("lines") or 12)
    cwd_s = _tui_cwd(arguments.get("cwd"), session_id)
    return grok_tui.run_leader(
        ["tail", "--session-id", session_id, "--lines", str(lines)],
        timeout=20.0,
    )


def grok_session_await(arguments: JsonDict) -> JsonDict:
    session_id = str(arguments.get("sessionId") or arguments.get("session_id") or "").strip()
    if not session_id:
        raise SeatError("sessionId is required")
    timeout = int(arguments.get("timeoutSec") or arguments.get("timeout") or 180)
    return grok_tui.run_leader(
        ["await", "--session-id", session_id, "--timeout", str(timeout)],
        timeout=float(timeout) + 10.0,
    )


def grok_session_cancel(arguments: JsonDict) -> JsonDict:
    session_id = str(arguments.get("sessionId") or arguments.get("session_id") or "").strip()
    if not session_id:
        raise SeatError("sessionId is required")
    cwd_s = _tui_cwd(arguments.get("cwd"), session_id)
    return grok_tui.run_leader(
        ["cancel", "--session-id", session_id, "--cwd", cwd_s],
        timeout=25.0,
    )


def grok_session_prompt(arguments: JsonDict) -> JsonDict:
    """Async follow-up into a live TUI session.  Returns jobId immediately."""
    session_id = str(arguments.get("sessionId") or arguments.get("session_id") or "").strip()
    prompt = arguments.get("prompt")
    if not session_id:
        raise SeatError("sessionId is required")
    if not isinstance(prompt, str) or not prompt.strip():
        raise SeatError("prompt is required")
    cwd = arguments.get("cwd")
    opts = _opts(arguments.get("opts"))
    opts["sessionId"] = session_id
    if arguments.get("from") or arguments.get("fromName"):
        opts["from"] = arguments.get("from") or arguments.get("fromName")
    if arguments.get("queue"):
        opts["queue"] = True
    if arguments.get("self"):
        opts["self"] = True
    if arguments.get("awaitReply") or arguments.get("awaitSec"):
        opts["awaitReply"] = arguments.get("awaitReply") or arguments.get("awaitSec")
    return seat_launch(
        {
            "seat": "grok-tui",
            "prompt": prompt.strip(),
            "cwd": cwd,
            "opts": opts,
        }
    )


def seat_result(arguments: JsonDict) -> JsonDict:
    job_id = str(arguments.get("jobId") or arguments.get("job_id") or "").strip()
    if not job_id:
        raise SeatError("jobId is required")
    rec = jobs.load_job(job_id)
    return jobs.result_view(rec)


TOOL_IMPL = {
    "seat_launch": seat_launch,
    "seat_status": seat_status,
    "seat_reply": seat_reply,
    "seat_result": seat_result,
    "grok_sessions_list": grok_sessions_list,
    "grok_session_peek": grok_session_peek,
    "grok_session_tail": grok_session_tail,
    "grok_session_prompt": grok_session_prompt,
    "grok_session_await": grok_session_await,
    "grok_session_cancel": grok_session_cancel,
    "recall_search": recall_bridge.recall_search,
    "recall_stats": recall_bridge.recall_stats,
    "recall_contribute": recall_bridge.recall_contribute,
    "recall_correct": recall_bridge.recall_correct,
    "recall_retract": recall_bridge.recall_retract,
}


def tool_schemas() -> list[JsonDict]:
    seat_desc = (
        "Seat to run.  deepseek (dsh --profile headless, read-only), "
        "grok (new session on grok-acp 127.0.0.1:12419), "
        "or grok-tui (inject into a live Mac Grok TUI via the shared leader; "
        "requires opts.sessionId).  Test adapters:  _echo, _sleep."
    )
    return [
        {
            "name": "seat_launch",
            "description": (
                "Start an async seat job.  Returns jobId immediately.  "
                "Poll seat_status, then seat_reply / seat_result.  "
                "DeepSeek is always DSH_PERMISSION_MODE=read-only.  "
                "There is no mode parameter."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "seat": {
                        "type": "string",
                        "description": seat_desc,
                    },
                    "prompt": {"type": "string", "description": "Task text."},
                    "cwd": {
                        "type": "string",
                        "description": "Must exist under /Users/jay/Code or /Users/jay/apps.",
                    },
                    "opts": {
                        "type": "object",
                        "description": (
                            "Optional.  effort:  quick|deep (deepseek temp --patch only).  "
                            "timeoutSec.  sessionId (grok follow-up or grok-tui attach).  "
                            "priorJobId (stuff prior text; required for deepseek follow-up).  "
                            "mcpServers:  array of ~/.grok/config.toml MCP names for a new "
                            "grok ACP session only (example [\"github\"]).  Empty or omitted "
                            "loads none on grok-acp.  grok-tui keeps the TUI set."
                        ),
                        "properties": {
                            "effort": {"type": "string", "enum": ["quick", "deep"]},
                            "timeoutSec": {"type": "integer", "minimum": 1, "maximum": 900},
                            "sessionId": {"type": "string"},
                            "priorJobId": {"type": "string"},
                            "mcpServers": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": (
                                    "MCP names from ~/.grok/config.toml for seat grok.  "
                                    "Not a TUI picker."
                                ),
                            },
                        },
                    },
                },
                "required": ["seat", "prompt"],
            },
        },
        {
            "name": "seat_status",
            "description": "Job state, elapsedMs, heartbeat (working vs wedged), partialTail.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "jobId": {"type": "string"},
                },
                "required": ["jobId"],
            },
        },
        {
            "name": "seat_reply",
            "description": (
                "Without prompt:  current assistant text.  "
                "With prompt:  async follow-up, returns new jobId.  "
                "DeepSeek follow-up is a new one-shot with prior text stuffed "
                "(headless cannot resume)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "jobId": {"type": "string"},
                    "prompt": {"type": "string", "description": "Optional follow-up.  Starts a new job."},
                },
                "required": ["jobId"],
            },
        },
        {
            "name": "seat_result",
            "description": "Snapshot:  text, exitCode, sessionId, stats, artifacts.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "jobId": {"type": "string"},
                },
                "required": ["jobId"],
            },
        },
        {
            "name": "grok_sessions_list",
            "description": (
                "List Mac Grok TUI chats.  Each row has live, turnState "
                "(idle|working|needs-input), title, lastTurnSummary, and "
                "pendingTool when needs-input is a permission prompt.  "
                "Cloud agents: same tools on https://agents.jays.services/mcp "
                "(Access + Bearer).  Do not start a second grok-acp."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "cwd": {
                        "type": "string",
                        "description": "Optional filter cwd for session/list.",
                    },
                },
            },
        },
        {
            "name": "grok_session_peek",
            "description": (
                "Disk peek of a TUI chat (summary.json).  Instant.  "
                "Does not session/load.  Does not send a prompt."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                "required": ["sessionId"],
            },
        },
        {
            "name": "grok_session_prompt",
            "description": (
                "Inject a follow-up into a live Grok TUI.  Any local or cloud "
                "agent via seat-mcp.  Refuses if turnState is working/needs-input "
                "unless queue=true.  Prefixes [from: NAME] (from / AGENT_TAG / remote).  "
                "Refuses sessionId == $GROK_SESSION_ID unless self=true.  "
                "Returns jobId; the job should succeed once queued.  "
                "Use grok_session_await for the TUI reply via disk peek "
                "(waits for the NEXT turn after inject, not a pre-existing idle)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string"},
                    "prompt": {"type": "string"},
                    "cwd": {"type": "string"},
                    "from": {"type": "string", "description": "Label in [from: NAME]."},
                    "queue": {"type": "boolean"},
                    "self": {
                        "type": "boolean",
                        "description": "Allow prompt when sessionId is this TUI's GROK_SESSION_ID.",
                    },
                    "awaitReply": {"type": "integer"},
                    "opts": {
                        "type": "object",
                        "properties": {
                            "timeoutSec": {"type": "integer", "minimum": 1, "maximum": 900},
                            "from": {"type": "string"},
                            "queue": {"type": "boolean"},
                            "self": {"type": "boolean"},
                            "awaitReply": {"type": "integer"},
                        },
                    },
                },
                "required": ["sessionId", "prompt"],
            },
        },
        {
            "name": "grok_session_tail",
            "description": "Last N chunks from the live TUI updates.jsonl (thought/tool/text).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string"},
                    "lines": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                "required": ["sessionId"],
            },
        },
        {
            "name": "grok_session_await",
            "description": (
                "Poll disk until the TUI turn is idle or needs-input.  "
                "Use after grok_session_prompt instead of waiting on ACP.  "
                "If the session is already idle this returns immediately; "
                "prompt --await-reply waits for the next turn after inject."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string"},
                    "timeoutSec": {"type": "integer", "minimum": 1, "maximum": 900},
                },
                "required": ["sessionId"],
            },
        },
        {
            "name": "grok_session_cancel",
            "description": "Best-effort session/cancel notification on a live TUI turn.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sessionId": {"type": "string"},
                    "cwd": {"type": "string"},
                },
                "required": ["sessionId"],
            },
        },
        *recall_bridge.recall_tool_schemas(),
    ]


# Keep the tuples imported so a future list tool can mention them.
_ = (IMPLEMENTED_SEATS, TEST_SEATS)
