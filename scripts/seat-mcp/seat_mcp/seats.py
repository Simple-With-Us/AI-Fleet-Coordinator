"""Generic seat registry and spawn plans.

Shellular has no local HTTP API.  Reading ~/.shellular/agents.json for names
is fine.  Spawning is our job.  Do not call Shellular to launch seats.
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any

from .config import (
    AGENTS_JSON,
    ALLOWED_CWD_ROOTS,
    DEFAULT_CWD,
    DEFAULT_TIMEOUT_SEC,
    DSH_SH,
    GROK_ACP_CLIENT,
    GROK_ACP_HOST,
    GROK_ACP_PORT,
    GROK_ACP_PYTHON,
    GROK_BIN,
    GROK_DRIVE,
    GROK_LEADER_CLIENT,
    HOME,
    IMPLEMENTED_SEATS,
    JOBS_DIR,
    MAX_TIMEOUT_SEC,
    TEST_SEATS,
)

JsonDict = dict[str, Any]


class SeatError(ValueError):
    """User-facing seat / cwd / opts error."""


def known_shellular_names() -> list[str]:
    """Names from ~/.shellular/agents.json (informational).  Never spawn from it."""
    names: list[str] = []
    if not AGENTS_JSON.is_file():
        return names
    try:
        data = json.loads(AGENTS_JSON.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return names
    custom = data.get("custom") if isinstance(data, dict) else None
    if not isinstance(custom, list):
        return names
    for item in custom:
        if isinstance(item, dict):
            name = item.get("id") or item.get("name")
            if name:
                names.append(str(name))
    return names


def validate_cwd(cwd: str | None) -> str:
    """cwd must exist and live under /Users/jay/Code or /Users/jay/apps."""
    return _validate_path(cwd, under_code_or_apps=True)


def validate_tui_cwd(cwd: str | None) -> str:
    """Live TUI chats may sit anywhere under Jay's home.

    Attach is not a new spawn.  Still refuse paths outside HOME.
    """
    return _validate_path(cwd, under_code_or_apps=False)


def _validate_path(cwd: str | None, *, under_code_or_apps: bool) -> str:
    raw = (cwd or str(DEFAULT_CWD)).strip() or str(DEFAULT_CWD)
    path = Path(raw).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise SeatError("cwd does not exist:  %s" % raw) from exc
    if not resolved.is_dir():
        raise SeatError("cwd is not a directory:  %s" % raw)
    try:
        resolved.relative_to(HOME)
    except ValueError as exc:
        raise SeatError("cwd must be under %s" % HOME) from exc
    if under_code_or_apps:
        ok = False
        for root in ALLOWED_CWD_ROOTS:
            try:
                resolved.relative_to(root.resolve())
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            raise SeatError("cwd must be under /Users/jay/Code or /Users/jay/apps")
    return str(resolved)


def _timeout_sec(seat: str, opts: JsonDict) -> int:
    raw = opts.get("timeoutSec", opts.get("timeout_sec"))
    if raw is None:
        return int(DEFAULT_TIMEOUT_SEC.get(seat, 300))
    try:
        val = int(raw)
    except (TypeError, ValueError) as exc:
        raise SeatError("opts.timeoutSec must be an integer") from exc
    if val < 1:
        raise SeatError("opts.timeoutSec must be >= 1")
    return min(val, MAX_TIMEOUT_SEC)


def _mcp_server_names(seat: str, opts: JsonDict) -> list[str]:
    """opts.mcpServers is a name allow-list for a new grok ACP session only."""
    raw = opts.get("mcpServers", opts.get("mcp_servers"))
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SeatError("opts.mcpServers must be an array of server names")
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise SeatError("opts.mcpServers entries must be non-empty strings")
        names.append(item.strip())
    if names and seat != "grok":
        raise SeatError(
            "opts.mcpServers is only valid for seat grok (new ACP session).  "
            "grok-tui keeps the TUI MCP set."
        )
    return names


def _effort(opts: JsonDict) -> str | None:
    raw = opts.get("effort")
    if raw is None or raw == "":
        return None
    val = str(raw).strip().lower()
    if val not in {"quick", "deep"}:
        raise SeatError("opts.effort must be quick or deep")
    return val


def grok_acp_listening() -> bool:
    """True when grok-acp is already on 127.0.0.1:12419.  Do not start a second."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.4)
    try:
        sock.connect((GROK_ACP_HOST, GROK_ACP_PORT))
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass
    return True


def write_effort_patch(job_id: str, effort: str) -> Path:
    """Temp --patch YAML only.  Do not edit ~/.dsh/settings-headless.yaml.

    quick:  deepseek-v4-flash / low
    deep:  deepseek-v4-pro / high
    """
    if effort == "deep":
        model = "deepseek-v4-pro"
        reasoning = "high"
    else:
        model = "deepseek-v4-flash"
        reasoning = "low"
    path = JOBS_DIR / ("%s.patch.yml" % job_id)
    text = (
        "# seat-mcp temp effort overlay.  Delete after the job.\n"
        "# Do not copy this into ~/.dsh.\n"
        "- id: agent-default-model\n"
        "  config:\n"
        "    provider: deepseek-official\n"
        "    model: %s\n"
        "    reasoningEffort: %s\n"
        "- id: llm-deepseek\n"
        "  config:\n"
        "    thinking: enabled\n"
        "    reasoningEffort: %s\n"
    ) % (model, reasoning, reasoning)
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def stuff_prior(prior_prompt: str, prior_text: str, follow_up: str) -> str:
    """DSH headless cannot resume.  Stuff prior text into a new one-shot."""
    return (
        "Prior task:\n%s\n\nPrior reply:\n%s\n\nFollow-up:\n%s\n"
        % (prior_prompt.strip(), (prior_text or "").strip(), follow_up.strip())
    )


def plan_spawn(rec: JsonDict) -> JsonDict:
    """Return {argv, env, timeoutSec, parse} without launching.

    Never expose a permission mode parameter.  DeepSeek is always read-only.
    Never default danger-full-access.  Do not use npx @deepseek-ai/dsh.
    """
    seat = str(rec.get("seat") or "")
    prompt = str(rec.get("prompt") or "")
    cwd = str(rec.get("cwd") or DEFAULT_CWD)
    opts = rec.get("opts") if isinstance(rec.get("opts"), dict) else {}
    job_id = str(rec.get("jobId") or "")
    timeout = _timeout_sec(seat, opts)
    _mcp_server_names(seat, opts)
    env = os.environ.copy()
    env.pop("DSH_SESSION_ID", None)
    env.pop("DSH_RESUME", None)

    if seat == "deepseek":
        if not DSH_SH.is_file():
            raise SeatError("missing %s" % DSH_SH)
        argv = [str(DSH_SH), "--profile", "headless"]
        effort = _effort(opts)
        if effort:
            patch = write_effort_patch(job_id, effort)
            argv.extend(["--patch", str(patch)])
        argv.append(prompt)
        env["DSH_HOME"] = str(HOME / ".dsh")
        env["DSH_PERMISSION_MODE"] = "read-only"
        return {
            "argv": argv,
            "env": env,
            "timeoutSec": timeout,
            "parse": "plain",
            "merge_stderr": True,
        }

    if seat == "grok":
        session_id = opts.get("sessionId") or opts.get("session_id") or rec.get("sessionId")
        if not grok_acp_listening():
            raise SeatError(
                "grok-acp is not listening on %s:%s.  "
                "Do not start a second serve.  Use the existing pm2 grok-acp."
                % (GROK_ACP_HOST, GROK_ACP_PORT)
            )
        if not GROK_ACP_CLIENT.is_file():
            raise SeatError("missing %s" % GROK_ACP_CLIENT)
        py = str(GROK_ACP_PYTHON if GROK_ACP_PYTHON.is_file() else "python3")
        env["PYTHONUNBUFFERED"] = "1"
        if session_id:
            argv = [
                py,
                "-u",
                str(GROK_ACP_CLIENT),
                "prompt",
                "--session-id",
                str(session_id),
                "--prompt",
                prompt,
                "--timeout",
                str(timeout),
            ]
        else:
            argv = [
                py,
                "-u",
                str(GROK_ACP_CLIENT),
                "new",
                "--cwd",
                cwd,
                "--prompt",
                prompt,
                "--timeout",
                str(timeout),
            ]
            for name in _mcp_server_names(seat, opts):
                argv.extend(["--mcp-server", name])
        return {
            "argv": argv,
            "env": env,
            "timeoutSec": timeout,
            "parse": "grok-json",
            "merge_stderr": False,
        }

    if seat == "grok-tui":
        session_id = opts.get("sessionId") or opts.get("session_id") or rec.get("sessionId")
        if not session_id:
            raise SeatError(
                "grok-tui requires opts.sessionId of a live TUI chat.  "
                "Call grok_sessions_list first."
            )
        helper = GROK_DRIVE if GROK_DRIVE.is_file() else GROK_LEADER_CLIENT
        if not helper.is_file():
            raise SeatError("missing %s" % helper)
        py = str(GROK_ACP_PYTHON if GROK_ACP_PYTHON.is_file() else "python3")
        argv = [
            py,
            str(helper),
            "prompt",
            "--session-id",
            str(session_id),
            "--cwd",
            cwd,
            "--prompt",
            prompt,
            "--timeout",
            "12",
        ]
        from_name = opts.get("from") or opts.get("fromName") or opts.get("from_name")
        if from_name:
            argv.extend(["--from-name", str(from_name)])
        if opts.get("queue") or opts.get("force"):
            argv.append("--queue")
        if opts.get("self"):
            argv.append("--self")
        await_sec = opts.get("awaitReply") or opts.get("await_reply") or opts.get("awaitSec")
        if await_sec:
            argv.extend(["--await-reply", str(int(await_sec))])
            timeout = max(timeout, int(await_sec) + 20)
        return {
            "argv": argv,
            "env": env,
            "timeoutSec": timeout,
            "parse": "grok-json",
            "merge_stderr": False,
        }

    if seat == "_echo":
        argv = ["/bin/echo", prompt if prompt else "pong"]
        return {
            "argv": argv,
            "env": env,
            "timeoutSec": timeout,
            "parse": "plain",
            "merge_stderr": True,
        }

    if seat == "_sleep":
        seconds = str(opts.get("sleepSec") or opts.get("sleep_sec") or "30")
        argv = ["/bin/sleep", str(int(seconds))]
        return {
            "argv": argv,
            "env": env,
            "timeoutSec": timeout,
            "parse": "plain",
            "merge_stderr": True,
        }

    extra = known_shellular_names()
    hint = ""
    if extra:
        hint = "  Shellular names (not spawned here):  %s." % ", ".join(extra)
    raise SeatError(
        "unknown seat %r.  v1 implements:  %s.  Test adapters:  %s.%s"
        % (seat, ", ".join(IMPLEMENTED_SEATS), ", ".join(TEST_SEATS), hint)
    )


def parse_progress_line(line: str) -> JsonDict:
    """Fields from one acp-client NDJSON line.  Empty dict if not progress."""
    raw = (line or "").strip()
    if not raw.startswith("{"):
        return {}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(obj, dict):
        return {}
    fields: JsonDict = {}
    if obj.get("sessionId"):
        fields["sessionId"] = str(obj["sessionId"])
    last_tool = obj.get("lastTool")
    if last_tool:
        fields["lastTool"] = str(last_tool)
    return fields


def parse_output(parse: str, stdout: str, fallback_session: str | None = None) -> tuple[str, str | None]:
    """Return (text, sessionId).  Prefers NDJSON event=done, then a trailing JSON object."""
    if parse != "grok-json":
        return stdout.strip(), fallback_session
    blob = stdout.strip()
    if not blob:
        return "", fallback_session
    session = fallback_session
    done = None
    for line in blob.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("sessionId"):
            session = str(obj["sessionId"])
        if obj.get("event") == "done" or "text" in obj:
            done = obj
    if done is not None:
        text = done.get("text")
        if text is None:
            text = blob
        return str(text), (str(session) if session else None)
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        last = blob.rfind("{")
        if last < 0:
            return blob, session
        try:
            data = json.loads(blob[last:])
        except json.JSONDecodeError:
            return blob, session
    if not isinstance(data, dict):
        return blob, session
    text = data.get("text")
    if text is None:
        text = blob
    session = data.get("sessionId") or session
    return str(text), (str(session) if session else None)


def grok_stdio_fallback_argv(prompt: str, cwd: str) -> list[str]:
    """Last-resort spawn if helpers are missing.  Flag before stdio.

    Do not use this to start grok-acp serve.  Only `grok agent --always-approve stdio`.
    """
    return [str(GROK_BIN), "agent", "--always-approve", "stdio"]
