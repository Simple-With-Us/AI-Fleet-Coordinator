"""GR-MCP:  the MCP wrapper for git-relay.  Seats call tools, never see the token.

A minimal stdio JSON-RPC server implementing the MCP handshake subset seats
need:  initialize, notifications/initialized, tools/list and tools/call.  The
wrapper holds only the seat's own bearer token;  every call is forwarded to
git-relay over HTTP, where policy, rate caps and the audit log apply.

Config:  GIT_RELAY_URL (default http://127.0.0.1:8471), GIT_RELAY_SEAT_TOKEN.
The seat token rides every call, so a non-loopback relay URL must be https -
plaintext http is accepted only for loopback relays.

A seat host reaching the relay's public hostname directly (AFC-672) goes
through Cloudflare, which refuses urllib's default User-Agent (error 1010) and
then Cloudflare Access (403) without a service token.  Every call therefore
carries a named User-Agent, and an https relay also gets the Access service
token headers when GIT_RELAY_CF_ACCESS_CLIENT_ID / GIT_RELAY_CF_ACCESS_CLIENT_SECRET
(or the fleet-wide CF_ACCESS_CLIENT_ID / CF_ACCESS_CLIENT_SECRET pair) are set.
Redirects are never followed, so neither the bearer nor the Access secret can
be carried to another host.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


def _validate_relay_url(relay_url: str) -> None:
    parsed = urllib.parse.urlparse(relay_url)
    host = parsed.hostname or ""
    loopback = host == "localhost"
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if parsed.scheme == "https" and host:
        return
    if parsed.scheme == "http" and loopback:
        return
    raise ValueError(
        "GIT_RELAY_URL must be https (plaintext http is allowed only for loopback relays)"
    )


USER_AGENT = "gr-mcp/1 (+swu-git-relay)"

# Checked in order;  the first pair with either half set wins, and a half pair
# is a startup error rather than a silent Access 403 on every call.
ACCESS_ENV_PAIRS = (
    ("GIT_RELAY_CF_ACCESS_CLIENT_ID", "GIT_RELAY_CF_ACCESS_CLIENT_SECRET"),
    ("CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"),
)


def access_headers_from_env(env) -> dict[str, str]:
    """Cloudflare Access service-token headers from the environment, or {}."""
    for id_name, secret_name in ACCESS_ENV_PAIRS:
        client_id = (env.get(id_name) or "").strip()
        secret = (env.get(secret_name) or "").strip()
        if not client_id and not secret:
            continue
        if not (client_id and secret):
            raise ValueError(f"{id_name} and {secret_name} must be set together")
        return {"CF-Access-Client-Id": client_id, "CF-Access-Client-Secret": secret}
    return {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect:  urllib would otherwise re-send Authorization and
    the Access headers to wherever a 3xx points (an Access login page included)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_opener():
    return urllib.request.build_opener(_NoRedirect()).open


PROTOCOL_VERSION = "2025-06-18"
MAX_LINE = 1024 * 1024
TIMEOUT_SECONDS = 30

TOOLS = [
    {
        "name": "open_pr",
        "description": "Open a pull request as the relay's GitHub identity",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "base": {"type": "string"},
                "head": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "draft": {"type": "boolean"},
            },
            "required": ["repo", "base", "head", "title", "body"],
        },
    },
    {
        "name": "edit_pr",
        "description": "Edit a pull request's title and/or body",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["repo", "number"],
        },
    },
    {
        "name": "reply_thread",
        "description": "Reply inline to a pull request review thread",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "comment_id": {"type": "integer"},
                "body": {"type": "string"},
            },
            "required": ["repo", "number", "comment_id", "body"],
        },
    },
    {
        "name": "comment_pr",
        "description": "Post a top-level comment on a pull request",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "body": {"type": "string"},
                "intent_id": {"type": "string"},
            },
            "required": ["repo", "number", "body"],
        },
    },
]

# mint_token is deliberately NOT exposed over MCP:  it returns a raw App
# installation token, which would land in the MCP client's stdio transcript and
# any logging around it.  Seats that need a token for git-over-HTTPS call the
# relay's HTTP API directly, where the response goes only to that caller.
TOOLS += [
    {
        "name": "merge_pr",
        "description": "Merge a pull request as the caller's own GitHub App",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "method": {"type": "string", "enum": ["squash", "merge", "rebase"]},
            },
            "required": ["repo", "number"],
        },
    },
    {
        "name": "workflow_dispatch",
        "description": "Dispatch a workflow run as the caller's own GitHub App",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "workflow": {"type": "string", "description": "workflow file name or numeric id"},
                "ref": {"type": "string"},
                "inputs": {"type": "object"},
                "intent_id": {"type": "string"},
                "expected_head_sha": {"type": "string"},
            },
            "required": ["repo", "workflow", "ref"],
        },
    },
    {
        "name": "resolve_thread",
        "description": "Resolve a pull request review thread as the caller's own GitHub App",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "thread_id": {"type": "string", "description": "PRRT_ review-thread id"},
            },
            "required": ["repo", "thread_id"],
        },
    },
]


TOOL_NAMES = {t["name"] for t in TOOLS}

MAX_DISPATCH_INPUTS = 32
MAX_DISPATCH_INPUT_VALUE = 1024

# ---------------------------------------------------------------- wf verbs
# The relay's workflow verbs (git_relay.workflow_verbs.SPECS, the same list the
# HTTP relay gates on in SeatPolicy.WORKFLOW_ACTIONS) are exposed here as
# pass-through tools named exactly like the relay actions, so a seat running
# GR-MCP locally reaches the same verbs the hosted front serves as gr_wf_*.
# The wrapper does NOT re-implement their argument shapes or policy:  every
# call is forwarded to the relay's /v1/<action>, where check_shape, the seat
# policy allowlist, rate caps and the audit log apply exactly as for an HTTP
# caller.  Locally the wrapper only checks the envelope (a repo string and a
# bounded argument object) before forwarding.
#
# No verb is excluded (owner ruling, 2026-11-08:  "Nothing should stay
# hosted-only by design, or else what is point of the relay?").  The five
# verbs whose bodies can exceed this stdio transport's MAX_LINE
# (wf_file_put, wf_files_put, wf_git_blob_create, wf_git_tree_create and
# wf_release_asset_upload_binary — the relay itself caps those requests at
# 256 KiB to 6 MiB) are carried by the chunked staging transport below:
# the caller splits the JSON-encoded arguments into stage_chunk messages,
# each comfortably under MAX_LINE, and GR-MCP reassembles them in memory
# and forwards the whole call to the relay over HTTP, where check_shape,
# the seat policy, rate caps and the audit log apply unchanged.  A call
# that fits one line needs no staging;  staging is only the large-body
# carriage, never a second policy path.
MAX_WF_ARGS = 64

# ---------------------------------------------------------------- staging
# Chunked large-body transport for wf calls.  A stage is held only in this
# process's memory, is keyed by a caller-chosen stage_id, accepts chunks
# strictly in order, and is dropped on commit, abort, or any protocol
# error.  Bounds keep a stage under the relay's own largest request cap
# (6 MiB for the file verbs) plus JSON envelope room.
MAX_STAGE_CHUNKS = 1024
MAX_CHUNK_CHARS = 512 * 1024
MAX_STAGED_CHARS = 8 * 1024 * 1024
MAX_STAGES = 4
_STAGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{3,63}")


def stage_messages(name: str, args: dict, chunk_chars: int = MAX_CHUNK_CHARS):
    """Yield the stage_chunk JSON-RPC messages carrying one wf call.

    Client-side helper for seats whose MCP transport caps a line below the
    call's size:  send each yielded message in order;  the reply to the
    final chunk is the wrapped call's own result."""
    if not 1 <= chunk_chars <= MAX_CHUNK_CHARS:
        raise ValueError("chunk_chars out of bounds")
    payload = json.dumps(args)
    chunks = [payload[i : i + chunk_chars] for i in range(0, len(payload), chunk_chars)] or [""]
    if len(chunks) > MAX_STAGE_CHUNKS:
        raise ValueError("call too large to stage")
    stage_id = f"stage-{os.urandom(6).hex()}"
    for seq, data in enumerate(chunks):
        params = {"stage_id": stage_id, "seq": seq, "total": len(chunks), "data": data}
        if seq == 0:
            params["name"] = name
        yield {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": "stage_chunk", "arguments": params},
        }


def _wf_actions() -> dict[str, bool]:
    """{action: writes} from the relay's own SPECS, or {} when unavailable.

    Fail closed:  if the verb list cannot be loaded, GR-MCP simply keeps its
    original seven tools rather than guessing an action list."""
    try:
        from .workflow_verbs import SPECS
    except Exception:
        return {}
    return {action: bool(writes) for action, (_perms, writes) in SPECS.items()}


WF_ACTIONS = _wf_actions()
WF_WRITES = frozenset(action for action, writes in WF_ACTIONS.items() if writes)

TOOLS += [
    {
        "name": "stage_chunk",
        "description": (
            "Carry one chunk of a large wf call's JSON-encoded arguments (see "
            "stage_messages in git_relay.mcp_server).  Chunks arrive in order; "
            "the final chunk performs the wrapped call and returns its result. "
            "Local transport only:  nothing is forwarded until the call is whole."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "stage_id": {"type": "string"},
                "name": {"type": "string", "description": "the wf verb, on seq 0 only"},
                "seq": {"type": "integer"},
                "total": {"type": "integer"},
                "data": {"type": "string"},
            },
            "required": ["stage_id", "seq", "total", "data"],
        },
    },
    {
        "name": "stage_abort",
        "description": "Drop an unfinished staged call without forwarding anything.",
        "inputSchema": {
            "type": "object",
            "properties": {"stage_id": {"type": "string"}},
            "required": ["stage_id"],
        },
    },
]

TOOLS += [
    {
        "name": action,
        "description": (
            f"Relay workflow verb {action} ({'write' if WF_ACTIONS[action] else 'read'}); "
            "the relay enforces seat policy and the exact argument shape."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string", "description": "owner/name"}},
            "required": ["repo"],
        },
    }
    for action in sorted(WF_ACTIONS)
]
TOOL_NAMES = {t["name"] for t in TOOLS}


def _validate_args(name: str, args: dict) -> str | None:
    """Enforce the advertised tool schema server-side before anything is forwarded."""
    if name in WF_ACTIONS:
        # Envelope only:  the relay owns the exact per-verb shape (check_shape)
        # and the seat policy;  duplicating either here would drift.
        if not isinstance(args.get("repo"), str) or not args["repo"]:
            return "argument 'repo' is required"
        if len(args) > MAX_WF_ARGS:
            return f"too many arguments (capped at {MAX_WF_ARGS})"
        return None
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
    props = schema.get("properties", {})
    for required in schema.get("required", []):
        if required not in args:
            return f"missing argument {required!r}"
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            return f"unexpected argument {key!r}"
        kind = spec.get("type")
        if kind == "string" and not isinstance(value, str):
            return f"argument {key!r} must be a string"
        if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"argument {key!r} must be an integer"
        if kind == "boolean" and type(value) is not bool:
            return f"argument {key!r} must be a boolean"
        if kind == "object" and not isinstance(value, dict):
            return f"argument {key!r} must be an object"
    thread_id = args.get("thread_id")
    if name == "resolve_thread" and not (
        isinstance(thread_id, str) and re.fullmatch(r"PRRT_[A-Za-z0-9_-]{6,128}", thread_id)
    ):
        return "thread_id must be a PRRT_ review-thread id"
    if name == "workflow_dispatch":
        intent, expected = args.get("intent_id"), args.get("expected_head_sha")
        if (intent is None) != (expected is None):
            return "intent_id and expected_head_sha must be supplied together"
        if intent is not None and (
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}", intent)
            or not re.fullmatch(r"[0-9a-f]{40}", expected)
        ):
            return "invalid dispatch intent or expected source"
        inputs = args.get("inputs")
        if inputs is not None:
            if len(inputs) > MAX_DISPATCH_INPUTS:
                return f"inputs is capped at {MAX_DISPATCH_INPUTS} keys"
            for key, value in inputs.items():
                if not isinstance(key, str) or value is None or isinstance(value, (dict, list)):
                    return "inputs must be a flat object of scalar values"
                if isinstance(value, str) and len(value) > MAX_DISPATCH_INPUT_VALUE:
                    return (
                        f"inputs string values are capped at {MAX_DISPATCH_INPUT_VALUE} characters"
                    )
    return None


class RelayMcp:
    """Transport-free core, so tests drive it without stdio."""

    def __init__(self, relay_url: str, seat_token: str, opener=None, access_headers=None):
        if not seat_token:
            raise ValueError("GIT_RELAY_SEAT_TOKEN is required")
        _validate_relay_url(relay_url)
        self._url = relay_url.rstrip("/")
        self._token = seat_token
        self._opener = opener or _default_opener()
        # The Access secret only ever rides https;  a loopback relay is not
        # behind Cloudflare and must not receive it over plaintext http.
        https = urllib.parse.urlparse(relay_url).scheme == "https"
        self._access_headers = dict(access_headers or {}) if https else {}
        self._stages: dict[str, dict] = {}

    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return _result(
                message,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "GR-MCP", "version": "1"},
                },
            )
        if method == "tools/list":
            return _result(message, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if name not in TOOL_NAMES:
                return _error(message, -32602, f"unknown tool {name!r}")
            args = params.get("arguments")
            if not isinstance(args, dict):
                return _error(message, -32602, "arguments must be an object")
            if name == "stage_chunk":
                return self._stage_chunk(message, args)
            if name == "stage_abort":
                stage_id = args.get("stage_id")
                if not isinstance(stage_id, str) or not _STAGE_ID.fullmatch(stage_id):
                    return _error(message, -32602, "invalid stage_id")
                self._stages.pop(stage_id, None)
                return _result(message, {"content": [{"type": "text", "text": "stage dropped"}]})
            invalid = _validate_args(name, args)
            if invalid:
                return _error(message, -32602, invalid)
            return self._call_tool(message, name, args)
        if method == "ping":
            return _result(message, {})
        return _error(message, -32601, f"unknown method {method!r}")

    def _stage_chunk(self, message: dict, args: dict) -> dict:
        """Accumulate one chunk;  on the final chunk, run the wrapped call.

        Any protocol error drops the stage:  a half-carried write must never
        be completable by a later, unrelated chunk stream."""
        stage_id, seq, total, data = (
            args.get("stage_id"),
            args.get("seq"),
            args.get("total"),
            args.get("data"),
        )
        if not isinstance(stage_id, str) or not _STAGE_ID.fullmatch(stage_id):
            return _error(message, -32602, "invalid stage_id")
        if (
            isinstance(seq, bool)
            or not isinstance(seq, int)
            or isinstance(total, bool)
            or not isinstance(total, int)
            or not 1 <= total <= MAX_STAGE_CHUNKS
            or not 0 <= seq < total
            or not isinstance(data, str)
            or len(data) > MAX_CHUNK_CHARS
        ):
            self._stages.pop(stage_id, None)
            return _error(message, -32602, "invalid chunk")
        stage = self._stages.get(stage_id)
        if stage is None:
            if seq != 0:
                return _error(message, -32602, "first chunk must have seq 0")
            name = args.get("name")
            if name not in WF_ACTIONS:
                return _error(message, -32602, "only wf verbs can be staged")
            if len(self._stages) >= MAX_STAGES:
                return _error(message, -32602, "too many open stages")
            stage = {"name": name, "total": total, "parts": []}
            self._stages[stage_id] = stage
        if seq != len(stage["parts"]) or total != stage["total"] or (seq > 0 and "name" in args):
            self._stages.pop(stage_id, None)
            return _error(message, -32602, "chunk out of order or stage mismatch")
        stage["parts"].append(data)
        if sum(len(part) for part in stage["parts"]) > MAX_STAGED_CHARS:
            self._stages.pop(stage_id, None)
            return _error(message, -32602, "staged call too large")
        if seq < total - 1:
            return _result(
                message,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"staged": seq + 1, "total": total, "complete": False}
                            ),
                        }
                    ]
                },
            )
        self._stages.pop(stage_id, None)
        try:
            wrapped = json.loads("".join(stage["parts"]))
        except ValueError:
            return _error(message, -32602, "staged arguments are not valid JSON")
        if not isinstance(wrapped, dict):
            return _error(message, -32602, "staged arguments must be an object")
        invalid = _validate_args(stage["name"], wrapped)
        if invalid:
            return _error(message, -32602, invalid)
        return self._call_tool(message, stage["name"], wrapped)

    def _call_tool(self, message: dict, name: str, args: dict) -> dict:
        if name == "open_pr" and "draft" in args:
            if type(args["draft"]) is not bool:
                return _error(message, -32602, "argument 'draft' must be a boolean")
            if args["draft"] is False:
                args = {key: value for key, value in args.items() if key != "draft"}
        req = urllib.request.Request(
            f"{self._url}/v1/{name}",
            method="POST",
            data=json.dumps(args).encode(),
            headers={
                **self._access_headers,
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        # A wf read verb writes nothing whatever happens to its answer, so none of its
        # failures is an unknown write outcome (Seer finding on PR #1515):  reads get the
        # retryable "unavailable" guidance in every branch below, writes keep the guard.
        is_read = name in WF_ACTIONS and name not in WF_WRITES
        try:
            with self._opener(req, timeout=TIMEOUT_SECONDS) as resp:
                raw = resp.read(MAX_LINE + 1)
            if len(raw) > MAX_LINE:
                if is_read:
                    return _tool_failure(message, "unavailable", retryable=True)
                return _tool_failure(message, "outcome_unknown")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                if is_read:
                    return _tool_failure(message, "unavailable", retryable=True)
                return _tool_failure(message, "outcome_unknown")
            if payload.get("outcome") in ("unknown", "pending"):
                if is_read:
                    return _tool_failure(message, "unavailable", retryable=True)
                return _tool_failure(message, "outcome_unknown")
        except urllib.error.HTTPError as exc:
            # The original seven tools all write, and so do the wf write
            # verbs:  a server error or replay hold can follow an accepted
            # write, so never call it a definite refusal.  A wf read verb is
            # the exception:  its failure wrote nothing.
            try:
                raw = exc.read(MAX_LINE + 1)
                error = json.loads(raw) if len(raw) <= MAX_LINE else None
            except (OSError, ValueError):
                error = None
            if is_read:
                if isinstance(error, dict):
                    # The relay marks a rate-limited or transiently failed read (429, 502)
                    # retryable:  say so, with its retry-after, before calling it a refusal.
                    if type(error.get("retryable")) is bool and error["retryable"]:
                        return _tool_failure(
                            message,
                            "unavailable",
                            exc.code,
                            retryable=True,
                            retry_after=error.get("retry_after_seconds"),
                        )
                    return _tool_failure(message, "relay_refused", exc.code)
                return _tool_failure(message, "unavailable", exc.code)
            if isinstance(error, dict) and error.get("outcome") in ("unknown", "pending"):
                return _tool_failure(message, "outcome_unknown", exc.code)
            if (
                isinstance(error, dict)
                and type(error.get("retryable")) is bool
                and error["retryable"]
            ):
                return _tool_failure(
                    message,
                    "unavailable",
                    exc.code,
                    retryable=True,
                    retry_after=error.get("retry_after_seconds"),
                )
            if not isinstance(error, dict) or exc.code >= 500:
                return _tool_failure(message, "outcome_unknown", exc.code)
            return _tool_failure(message, "relay_refused", exc.code)
        except (OSError, ValueError):
            if is_read:
                return _tool_failure(message, "unavailable", retryable=True)
            return _tool_failure(message, "outcome_unknown")
        return _result(message, {"content": [{"type": "text", "text": json.dumps(payload)}]})


def _tool_failure(message, code, status=None, retryable=False, retry_after=None):
    # Provider prose can contain private details.  Return only stable local
    # guidance and the HTTP status, never the response body or credentials.
    if code == "outcome_unknown":
        detail = (
            "The write outcome is unknown.  Do not retry blindly; check the target state first."
        )
    elif code == "unavailable":
        detail = "Relay reports a retryable pre-write failure.  Nothing was written."
    else:
        detail = "Relay refused the request."
    error = {"code": code, "message": detail, "retryable": retryable}
    if (
        retryable
        and isinstance(retry_after, (int, float))
        and not isinstance(retry_after, bool)
        and 0 <= retry_after <= 86400
    ):
        error["retry_after_seconds"] = retry_after
    if status is not None:
        error["http_status"] = status
    return _result(
        message,
        {"isError": True, "content": [{"type": "text", "text": json.dumps(error)}]},
    )


def _result(message: dict, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


def _error(message: dict, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": code, "message": text}}


_NO_ID = object()
_ID_PREFIX = re.compile(r'"id"\s*:\s*("(?:[^"\\]|\\.){0,128}"|-?\d{1,19})')


def _recover_id(text: str):
    """Best-effort JSON-RPC id from a line too broken to parse, prefix only."""
    match = _ID_PREFIX.search(text[:8192])
    if not match:
        return _NO_ID
    try:
        return json.loads(match.group(1))
    except ValueError:
        return _NO_ID


def process_line(line: str, server: RelayMcp, log) -> dict | None:
    """Handle one raw stdin line; malformed input is never silently dropped.

    A recoverable id gets a JSON-RPC error (-32700 parse error, -32600
    oversized or invalid request);  otherwise a bounded stderr note via
    `log`, carrying only a kind and a byte count - never line content,
    which may hold a credential pasted into a tool argument.
    """
    if len(line) > MAX_LINE:
        recovered = _recover_id(line)
        if recovered is not _NO_ID:
            return {
                "jsonrpc": "2.0",
                "id": recovered,
                "error": {"code": -32600, "message": "request too large"},
            }
        log(f"GR-MCP: dropped oversized line ({len(line)} bytes)")
        return None
    stripped = line.strip()
    if not stripped:
        return None
    try:
        message = json.loads(stripped)
    except json.JSONDecodeError:
        recovered = _recover_id(stripped)
        if recovered is not _NO_ID:
            return {
                "jsonrpc": "2.0",
                "id": recovered,
                "error": {"code": -32700, "message": "parse error"},
            }
        log(f"GR-MCP: dropped malformed line ({len(stripped)} bytes)")
        return None
    if not isinstance(message, dict):
        log("GR-MCP: dropped non-object line")
        return None
    return server.handle(message)


def main() -> None:
    import sys

    server = RelayMcp(
        os.environ.get("GIT_RELAY_URL", "http://127.0.0.1:8471"),
        os.environ.get("GIT_RELAY_SEAT_TOKEN", ""),
        access_headers=access_headers_from_env(os.environ),
    )

    def log(text: str) -> None:
        sys.stderr.write(text + "\n")
        sys.stderr.flush()

    for line in sys.stdin:
        reply = process_line(line, server, log)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
