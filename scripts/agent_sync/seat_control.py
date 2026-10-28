"""Host-aware seat-control MCP contract and adapter seam.

This module is deliberately not registered by the running MCP server. It is a
code-only foundation for a later, separately approved integration. Callers must
inject the authenticated identity, authorization and approval checks, an audit
sink, the existing catalog/nudge queue/transport, and the existing idempotency
store. No dependency is created implicitly and no shell command is exposed.

The adapter only prompts an already-discovered exact session through
``ChatTransport``. Start/create, separate resume and cancel-turn remain
explicitly unsupported by the currently verified transports.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Protocol

from . import secretscan

# Keep host categories separate from the concrete host ID returned by the
# existing chat catalog. The caller never chooses a title as an identity.
HostScope = Literal["mac", "server", "cloud"]
Operation = Literal["list", "read", "status", "output", "create", "resume", "prompt", "cancel_turn"]
CapabilityState = Literal["implemented", "unsupported", "stale", "runtime_unverified"]

HOST_SCOPES = ("mac", "server", "cloud")
SUPPORTED_PLATFORMS = (
    "grok-leader",
    "codex",
    "cursor-cloud",
    "claude",
    "cursor-local",
    "minimax",
    "muse",
    "fx",
)
OPERATIONS = ("list", "read", "status", "output", "create", "resume", "prompt", "cancel_turn")
MUTATING = frozenset({"create", "resume", "prompt", "cancel_turn"})
MAX_LIST = 24
MAX_OUTPUT_MESSAGES = 8
MAX_PROMPT_CHARS = 12_000
MAX_REQUEST_ID_CHARS = 128
MAX_TOOL_ARGUMENT_BYTES = 65_536
TARGET_FIELDS = frozenset({"host_scope", "host", "platform", "session_id", "owner"})
REQUEST_TTL_SECONDS = 7 * 24 * 60 * 60
# IdemStore prunes rows by this timestamp. Preserve no-replay tombstones beyond
# the normal request TTL so an old retry cannot become a fresh provider send.
TOMBSTONE_EXPIRES_AT = 2**63 - 1
# IdemStore.begin() treats a fresh pending row as an atomic no-replay claim.
# Keep a held/failed receipt pending with a future claim timestamp so begin()
# cannot atomically overwrite it as a new send. The result retains the true
# caller-visible held/failed status.
TOMBSTONE_CLAIMED_AT = TOMBSTONE_EXPIRES_AT


def _states(**overrides: CapabilityState) -> dict[str, CapabilityState]:
    base: dict[str, CapabilityState] = {op: "unsupported" for op in OPERATIONS}
    base.update(overrides)
    return base


# ``implemented`` describes code present in the existing local adapter. It is
# not a claim that a server host or a live deployment has been verified.
# Unknown platform names are always unsupported; catalog discovery alone does
# not grant control capability.
CAPABILITY_MATRIX: dict[tuple[str, str], dict[str, CapabilityState]] = {
    ("mac", "grok-leader"): _states(
        list="implemented",
        read="implemented",
        status="implemented",
        output="implemented",
        prompt="implemented",
    ),
    ("mac", "codex"): _states(
        list="implemented",
        read="implemented",
        status="implemented",
        output="implemented",
        prompt="implemented",
    ),
    ("mac", "claude"): _states(
        list="implemented", read="implemented", status="implemented", output="implemented"
    ),
    ("mac", "cursor-local"): _states(list="implemented", status="implemented"),
    ("mac", "minimax"): _states(
        list="implemented", read="implemented", status="implemented", output="implemented"
    ),
    ("mac", "muse"): _states(list="implemented", status="implemented"),
    ("mac", "fx"): _states(list="implemented", status="implemented"),
    ("cloud", "cursor-cloud"): _states(
        list="implemented",
        read="implemented",
        status="implemented",
        output="implemented",
        prompt="implemented",
    ),
}
for _platform in SUPPORTED_PLATFORMS:
    CAPABILITY_MATRIX[("server", _platform)] = _states(
        **{
            operation: (
                "runtime_unverified"
                if operation in {"list", "read", "status", "output", "prompt"}
                else "unsupported"
            )
            for operation in OPERATIONS
        }
    )


class IdempotencyStore(Protocol):
    """The subset of the existing ``agent_sync.idem.IdemStore`` used here."""

    def begin(
        self, key: str, ttl: float, fields: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any] | None]: ...

    def row(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, **changes: Any) -> None: ...

    def drop(self, key: str) -> None: ...


class Catalog(Protocol):
    def discover(self, **kwargs: Any) -> list[dict[str, Any]]: ...
    def excerpt(self, record_id: str, limit: int = 4) -> list[dict[str, str]]: ...


class Transport(Protocol):
    def preflight(self, binding: dict[str, str]) -> str: ...
    def send(
        self, binding: dict[str, str], text: str, *, permit: Callable[[], bool] | None = None
    ) -> str: ...


class NudgeQueue(Protocol):
    def snapshot(self) -> dict[str, Any]: ...


Authorizer = Callable[[str, str, "SessionTarget | None"], bool]
Approver = Callable[[str, str, "SessionTarget", str], bool]
AuditSink = Callable[[dict[str, Any]], None]


@dataclass(frozen=True)
class SessionTarget:
    host_scope: HostScope
    host: str
    platform: str
    session_id: str
    owner: str

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> SessionTarget:
        extra = set(raw.keys()) - TARGET_FIELDS
        if extra:
            raise ValueError("target carries unsupported identity fields")
        scope = raw.get("host_scope")
        if scope not in HOST_SCOPES:
            raise ValueError("host_scope must be mac, server, or cloud")
        host = _identity(raw.get("host"), "host")
        platform = _identity(raw.get("platform"), "platform")
        session_id = _identity(raw.get("session_id"), "session_id")
        owner = _identity(raw.get("owner"), "owner")
        return cls(scope, host, platform, session_id, owner)  # type: ignore[arg-type]

    def public(self) -> dict[str, str]:
        return {
            "host_scope": self.host_scope,
            "host": self.host,
            "platform": self.platform,
            "session_id": self.session_id,
            "owner": self.owner,
        }


def _identity(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 512:
        raise ValueError(f"{name} must be a non-empty exact identifier")
    return value


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()


def _catalog_session_id(row: Mapping[str, Any]) -> str | None:
    sid = row.get("session_id")
    if isinstance(sid, str) and sid.strip():
        return sid.strip()
    agent_id = row.get("agent_id")
    if isinstance(agent_id, str) and agent_id.strip():
        return agent_id.strip()
    return None


def _catalog_flag(value: Any) -> bool:
    return value is True


def _scope_for(platform: str, local_scope: str) -> str:
    return "cloud" if platform == "cursor-cloud" else local_scope


def capability_matrix() -> list[dict[str, Any]]:
    """Return a JSON-safe, deterministic projection of the contract."""
    rows = []
    for (scope, platform), states in sorted(CAPABILITY_MATRIX.items()):
        rows.append(
            {
                "host_scope": scope,
                "platform": platform,
                "capabilities": dict(states),
            }
        )
    return rows


def _target_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "host_scope": {"type": "string", "enum": list(HOST_SCOPES)},
            "host": {"type": "string", "minLength": 1, "maxLength": 512},
            "platform": {"type": "string", "minLength": 1, "maxLength": 128},
            "session_id": {"type": "string", "minLength": 1, "maxLength": 512},
            "owner": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "required": ["host_scope", "host", "platform", "session_id", "owner"],
        "additionalProperties": False,
    }


_TARGET = _target_schema()


def _tool(
    name: str, description: str, properties: dict[str, Any], required: list[str]
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
    }


# A separate family catalog by design. Neither stdio nor hosted dispatch imports
# this automatically; registration must add handler and identity-policy parity.
SEAT_CONTROL_TOOL_SCHEMAS = [
    _tool(
        "seat_control_capabilities",
        "Read the supported adapter capability matrix. This does not discover or control a session.",
        {},
        [],
    ),
    _tool(
        "seat_control_list",
        "List bounded session metadata for one explicit host scope. Titles are display-only.",
        {
            "host_scope": {"type": "string", "enum": list(HOST_SCOPES)},
            "host": {"type": "string", "minLength": 1, "maxLength": 512},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_LIST},
        },
        ["host_scope", "host"],
    ),
    _tool(
        "seat_control_read",
        "Read bounded metadata for one exact session identity.",
        {"target": _TARGET},
        ["target"],
    ),
    _tool(
        "seat_control_status",
        "Read status for one exact session identity.",
        {"target": _TARGET},
        ["target"],
    ),
    _tool(
        "seat_control_output",
        "Read bounded user and assistant output for one exact session identity.",
        {
            "target": _TARGET,
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_OUTPUT_MESSAGES},
        },
        ["target"],
    ),
    _tool(
        "seat_control_start",
        "Start/create is explicitly unsupported until a host adapter and owner gate are approved.",
        {
            "host_scope": {"type": "string", "enum": list(HOST_SCOPES)},
            "host": {"type": "string"},
            "platform": {"type": "string"},
            "request_id": {"type": "string", "minLength": 1, "maxLength": MAX_REQUEST_ID_CHARS},
        },
        ["host_scope", "host", "platform", "request_id"],
    ),
    _tool(
        "seat_control_resume",
        "Resume is unsupported as a separate operation; name the exact existing session.",
        {
            "target": _TARGET,
            "request_id": {"type": "string", "minLength": 1, "maxLength": MAX_REQUEST_ID_CHARS},
        },
        ["target", "request_id"],
    ),
    _tool(
        "seat_control_prompt",
        "Prompt only an exact discovered existing session. Mutation is disabled unless an integration explicitly enables and approves it.",
        {
            "target": _TARGET,
            "request_id": {"type": "string", "minLength": 1, "maxLength": MAX_REQUEST_ID_CHARS},
            "prompt": {"type": "string", "minLength": 1, "maxLength": MAX_PROMPT_CHARS},
        },
        ["target", "request_id", "prompt"],
    ),
    _tool(
        "seat_control_cancel_turn",
        "Cancel-turn is unsupported. This tool never kills a process.",
        {
            "target": _TARGET,
            "request_id": {"type": "string", "minLength": 1, "maxLength": MAX_REQUEST_ID_CHARS},
        },
        ["target", "request_id"],
    ),
]


class SeatControlMCP:
    """An injectable tool adapter; it never creates credentials or providers.

    ``caller_id`` must come from the existing authenticated MCP context, never
    from tool arguments. ``local_scope`` must come from trusted service
    configuration. Mutations fail closed by default.
    """

    def __init__(
        self,
        *,
        caller_id: str,
        local_scope: Literal["mac", "server"],
        catalog: Catalog,
        transport: Transport,
        nudge_queue: NudgeQueue | None,
        idempotency: IdempotencyStore,
        authorize: Authorizer,
        approve: Approver | None,
        audit: AuditSink,
        feature_enabled: bool = False,
        mutations_enabled: bool = False,
        scan_known: Iterable[str] = (),
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.caller_id = _identity(caller_id, "authenticated caller")
        if local_scope not in ("mac", "server"):
            raise ValueError("local_scope must be mac or server")
        self.local_scope = local_scope
        self.catalog = catalog
        self.transport = transport
        self.nudge_queue = nudge_queue
        self.idempotency = idempotency
        self.authorize = authorize
        self.approve = approve
        self.audit = audit
        self.feature_enabled = bool(feature_enabled)
        self.mutations_enabled = bool(mutations_enabled)
        self.scan_known = tuple(scan_known)
        self.clock = clock

    def list_tools(self) -> list[dict[str, Any]]:
        if not self.feature_enabled:
            return []
        visible = []
        for tool in SEAT_CONTROL_TOOL_SCHEMAS:
            name = tool["name"]
            if name in {"seat_control_start", "seat_control_resume", "seat_control_cancel_turn"}:
                continue  # Not implemented by a verified adapter.
            if name == "seat_control_prompt" and not self.mutations_enabled:
                continue
            visible.append(tool)
        return json.loads(json.dumps(visible))

    def call_tool(self, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        if not self.feature_enabled:
            return self._result("disabled", "seat control is disabled")
        if not self._arguments_within_bounds(arguments):
            self._audit(name, None, "invalid_request")
            return self._result("invalid_request", "arguments exceed the supported bound")
        try:
            if name == "seat_control_capabilities":
                if not self.authorize(self.caller_id, "capabilities", None):
                    self._audit("capabilities", None, "permission_refused", delegation="refused")
                    return self._result("permission_refused", "caller is not authorized")
                self._audit("capabilities", None, "ok", delegation="authorized")
                return self._result("ok", data={"matrix": capability_matrix()})
            if name == "seat_control_list":
                return self._list(arguments)
            if name in {"seat_control_read", "seat_control_status", "seat_control_output"}:
                return self._read(name, arguments)
            operation = {
                "seat_control_start": "create",
                "seat_control_resume": "resume",
                "seat_control_prompt": "prompt",
                "seat_control_cancel_turn": "cancel_turn",
            }.get(name)
            if operation:
                return self._mutate(operation, arguments)
            self._audit(name, None, "unsupported")
            return self._result("unsupported", "unknown seat-control tool")
        except (TypeError, ValueError, KeyError):
            self._audit(name, None, "invalid_request")
            return self._result("invalid_request", "invalid seat-control arguments")
        except Exception:
            self._audit(name, None, "unavailable")
            # Provider or store exceptions may follow a send. The mutating path
            # marks that case unknown before returning; never expose raw errors.
            return self._result("unavailable", "seat-control adapter unavailable")

    def _list(self, args: Mapping[str, Any]) -> dict[str, Any]:
        scope = args.get("host_scope")
        if scope not in HOST_SCOPES:
            self._audit("list", None, "invalid_request")
            return self._result("invalid_request", "host_scope is required")
        try:
            host_filter = _identity(args.get("host"), "host")
        except ValueError:
            self._audit("list", None, "invalid_request")
            return self._result("invalid_request", "an exact host is required")
        target_scope = str(scope)
        if not self.authorize(self.caller_id, "list", None):
            self._audit("list", None, "permission_refused", delegation="refused")
            return self._result("permission_refused", "caller is not authorized")
        limit = args.get("limit", MAX_LIST)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST:
            self._audit("list", None, "invalid_request")
            return self._result("invalid_request", "limit is outside the supported bound")
        try:
            rows = self.catalog.discover()
        except Exception:
            self._audit("list", None, "unavailable")
            return self._result("unavailable", "session catalog unavailable")
        output = []
        for row in rows:
            platform = row.get("platform")
            host = row.get("host")
            sid = _catalog_session_id(row) if isinstance(row, dict) else None
            if not isinstance(platform, str) or not isinstance(host, str) or sid is None:
                continue
            if _scope_for(platform, self.local_scope) != target_scope or host != host_filter:
                continue
            owner = row.get("seat")
            if not isinstance(owner, str) or not owner:
                continue
            target = SessionTarget(target_scope, host, platform, sid, owner)  # type: ignore[arg-type]
            if not self.authorize(self.caller_id, "list", target):
                continue
            caps = self._capabilities(target, row)
            output.append(
                {
                    "target": target.public(),
                    "title": row.get("title"),  # display only; never accepted by _target
                    "updated_at": row.get("updated_at"),
                    "status": row.get("status", "unknown"),
                    "stale": bool(row.get("stale")),
                    "capabilities": caps,
                }
            )
            if len(output) >= limit:
                break
        self._audit("list", None, "ok", delegation="authorized")
        return self._result("ok", data={"sessions": output, "limit": limit})

    def _read(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        target = SessionTarget.parse(
            args.get("target") if isinstance(args.get("target"), Mapping) else {}
        )
        operation = {
            "seat_control_read": "read",
            "seat_control_status": "status",
            "seat_control_output": "output",
        }[name]
        if not self.authorize(self.caller_id, operation, target):
            self._audit(operation, target, "permission_refused", delegation="refused")
            return self._result("permission_refused", "caller is not authorized for this session")
        row, error = self._resolve(target)
        if error:
            self._audit(operation, target, error)
            return self._result(error, "exact session is missing, stale, ambiguous, or unavailable")
        assert row is not None
        state = self._capabilities(target, row).get(operation, "unsupported")
        if state != "implemented":
            self._audit(operation, target, state)
            return self._result(state, "capability is not available for this host and platform")
        if operation == "output":
            limit = args.get("limit", 4)
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or not 1 <= limit <= MAX_OUTPUT_MESSAGES
            ):
                self._audit(operation, target, "invalid_request")
                return self._result(
                    "invalid_request", "output limit is outside the supported bound"
                )
            record_id = row.get("id")
            if not isinstance(record_id, str) or not record_id.strip():
                self._audit(operation, target, "invalid_catalog")
                return self._result("unavailable", "catalog record is missing an opaque id")
            try:
                messages = self.catalog.excerpt(record_id.strip(), limit=limit)
            except Exception:
                messages = []
            safe = []
            total = 0
            for message in messages if isinstance(messages, list) else []:
                if not isinstance(message, dict) or message.get("role") not in {
                    "user",
                    "assistant",
                }:
                    continue
                text = message.get("text")
                if not isinstance(text, str):
                    continue
                text = text[:2400]
                if total + len(text) > 6000:
                    text = text[: max(0, 6000 - total)]
                if text:
                    safe.append({"role": message["role"], "text": text})
                    total += len(text)
                if total >= 6000:
                    break
            self._audit(operation, target, "ok", delegation="authorized")
            return self._result("ok", data={"messages": safe, "bounded": True})
        result = {
            "target": target.public(),
            "title": row.get("title"),
            "workspace_bound": isinstance(row.get("cwd"), str) and bool(row.get("cwd")),
            "status": row.get("status", "unknown"),
            "stale": bool(row.get("stale")),
            "capabilities": self._capabilities(target, row),
        }
        self._audit(operation, target, "ok", delegation="authorized")
        return self._result("ok", data=result)

    def _idempotency_key(self, request_id: str) -> str:
        return _digest(self.caller_id + "\0" + request_id)

    def _drop_idempotency(self, request_id: str) -> None:
        try:
            self.idempotency.drop(self._idempotency_key(request_id))
        except Exception:
            pass

    def _arguments_within_bounds(self, arguments: Mapping[str, Any]) -> bool:
        try:
            payload = json.dumps(dict(arguments), ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return False
        return len(payload.encode("utf-8")) <= MAX_TOOL_ARGUMENT_BYTES

    def _scan_prompt(self, prompt: str) -> bool:
        return secretscan.scan(prompt, self.scan_known) is not None

    def _mutate(self, operation: str, args: Mapping[str, Any]) -> dict[str, Any]:
        request_id = args.get("request_id")
        try:
            request_id = _identity(request_id, "request_id")
        except ValueError:
            return self._result("invalid_request", "request_id is required")
        if len(request_id) > MAX_REQUEST_ID_CHARS:
            return self._result("invalid_request", "request_id exceeds the supported bound")
        raw_target = args.get("target")
        if operation == "create":
            scope, host, platform = args.get("host_scope"), args.get("host"), args.get("platform")
            if scope not in HOST_SCOPES:
                return self._result("invalid_request", "host_scope is required")
            try:
                _identity(host, "host")
                _identity(platform, "platform")
            except ValueError:
                return self._result("invalid_request", "host and platform are required")
            if not self.authorize(self.caller_id, operation, None):
                self._audit(operation, None, "permission_refused", request_id=request_id)
                return self._result("permission_refused", "caller is not authorized")
            self._audit(operation, None, "unsupported", request_id=request_id)
            return self._result(
                "unsupported", "start/create is not implemented for any verified adapter"
            )
        try:
            target = SessionTarget.parse(raw_target if isinstance(raw_target, Mapping) else {})
        except ValueError:
            return self._result("invalid_request", "exact target identity is required")
        if not self.authorize(self.caller_id, operation, target):
            self._audit(
                operation, target, "permission_refused", delegation="refused", request_id=request_id
            )
            self._drop_idempotency(request_id)
            return self._result("permission_refused", "caller is not authorized for this session")
        if operation != "prompt":
            # These operations do not have verified native implementations.
            # In particular cancel-turn never maps to process termination.
            self._audit(operation, target, "unsupported", request_id=request_id)
            return self._result(
                "unsupported", f"{operation} is not implemented for any verified adapter"
            )
        prompt = args.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > MAX_PROMPT_CHARS:
            self._audit(operation, target, "invalid_request", request_id=request_id)
            self._drop_idempotency(request_id)
            return self._result("invalid_request", "prompt is empty or exceeds the supported bound")
        if self._scan_prompt(prompt):
            self._audit(operation, target, "invalid_request", request_id=request_id)
            self._drop_idempotency(request_id)
            return self._result("invalid_request", "prompt contains a refused secret shape")
        if not self.mutations_enabled:
            self._audit(
                operation,
                target,
                "mutation_disabled",
                mutation_gate="closed",
                delegation="refused",
                request_id=request_id,
            )
            self._drop_idempotency(request_id)
            return self._result("permission_refused", "mutations are disabled")
        if self.approve is None or not self.approve(self.caller_id, operation, target, request_id):
            self._audit(
                operation,
                target,
                "approval_required",
                mutation_gate="open",
                delegation="approval_required",
                request_id=request_id,
            )
            self._drop_idempotency(request_id)
            return self._result("permission_refused", "per-request mutation approval is required")
        replay = self._prior_prompt_result(target, request_id, prompt)
        if replay is not None:
            return replay
        row, error = self._resolve(target)
        if error:
            self._audit(operation, target, error, prompt=prompt, request_id=request_id)
            self._drop_idempotency(request_id)
            return self._result(error, "exact session is missing, stale, ambiguous, or unavailable")
        assert row is not None
        row_status = str(row.get("status", "unknown")).strip().lower().replace("-", "_")
        if row_status in {
            "busy",
            "active",
            "unknown",
            "permission_wait",
            "permission_waiting",
            "pending_permission",
            "waiting_permission",
        } or row.get("pending_permission"):
            return self._held(
                operation,
                target,
                request_id,
                prompt,
                "target is busy, unknown, or waiting for permission",
            )
        if self._capabilities(target, row).get("prompt") != "implemented":
            capability = self._capabilities(target, row).get("prompt", "unsupported")
            self._audit(operation, target, capability, prompt=prompt, request_id=request_id)
            self._drop_idempotency(request_id)
            return self._result(capability, "prompt capability is not implemented for this target")
        if self._nudge_pending(target):
            return self._held(
                operation, target, request_id, prompt, "existing queue item for exact session"
            )
        binding = {
            "platform": target.platform,
            "host": target.host,
            "session_id": target.session_id,
            "seat": target.owner,
            "cwd": row.get("cwd") if isinstance(row.get("cwd"), str) else "",
        }
        if not binding["cwd"]:
            self._audit(operation, target, "unsupported", prompt=prompt, request_id=request_id)
            self._drop_idempotency(request_id)
            return self._result("unsupported", "catalog has no exact workspace binding")
        return self._dispatch_prompt(target, request_id, prompt, binding)

    def _prior_prompt_result(
        self, target: SessionTarget, request_id: str, prompt: str
    ) -> dict[str, Any] | None:
        """Resolve a prior request before target discovery can hide its receipt."""
        key = self._idempotency_key(request_id)
        fingerprint = _digest(
            json.dumps(
                {"op": "prompt", "target": target.public(), "prompt": _digest(prompt)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        prior = self.idempotency.row(key)
        if not prior:
            return None
        if prior.get("fingerprint") != fingerprint:
            return self._result(
                "request_conflict", "request_id was already used for different content"
            )
        receipt = prior.get("result")
        if prior.get("state") in {"held", "failed"} or (
            isinstance(receipt, dict) and receipt.get("status") in {"held", "failed"}
        ):
            status = receipt.get("status") if isinstance(receipt, dict) else prior["state"]
            return self._result(
                status,
                receipt.get("reason")
                if isinstance(receipt, dict)
                else "previous request was not dispatched",
                data=receipt,
            )
        if prior.get("state") == "sent":
            self._audit("prompt", target, "duplicate", prompt=prompt, request_id=request_id)
            return self._result(
                "duplicate", "request was already accepted", data=prior.get("result")
            )
        if prior.get("state") == "unknown":
            self._audit("prompt", target, "unknown", prompt=prompt, request_id=request_id)
            return self._result(
                "unknown", "previous outcome is uncertain; automatic replay is blocked"
            )
        if prior.get("state") == "pending":
            self._audit("prompt", target, "pending", prompt=prompt, request_id=request_id)
            return self._result("pending", "request is already in progress")
        return None

    def _dispatch_prompt(
        self, target: SessionTarget, request_id: str, prompt: str, binding: dict[str, str]
    ) -> dict[str, Any]:
        fingerprint = _digest(
            json.dumps(
                {"op": "prompt", "target": target.public(), "prompt": _digest(prompt)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        key = self._idempotency_key(request_id)
        fields = {
            "operation": "prompt",
            "fingerprint": fingerprint,
            "target_fingerprint": _digest(json.dumps(target.public(), sort_keys=True)),
            "prompt_fingerprint": _digest(prompt),
            "caller_fingerprint": _digest(self.caller_id),
        }
        prior = self.idempotency.row(key)
        if prior:
            if prior.get("fingerprint") != fingerprint:
                self._audit(
                    "prompt", target, "request_conflict", prompt=prompt, request_id=request_id
                )
                return self._result(
                    "request_conflict", "request_id was already used for different content"
                )
            receipt = prior.get("result")
            if prior.get("state") in {"held", "failed"} or (
                isinstance(receipt, dict) and receipt.get("status") in {"held", "failed"}
            ):
                status = receipt.get("status") if isinstance(receipt, dict) else prior["state"]
                return self._result(
                    status,
                    receipt.get("reason")
                    if isinstance(receipt, dict)
                    else "previous request was not dispatched",
                    data=receipt,
                )
            if prior.get("state") == "sent":
                self._audit("prompt", target, "duplicate", prompt=prompt, request_id=request_id)
                return self._result(
                    "duplicate", "request was already accepted", data=prior.get("result")
                )
            if prior.get("state") == "unknown":
                self._audit("prompt", target, "unknown", prompt=prompt, request_id=request_id)
                return self._result(
                    "unknown", "previous outcome is uncertain; automatic replay is blocked"
                )
            if prior.get("state") == "pending":
                self._audit("prompt", target, "pending", prompt=prompt, request_id=request_id)
                return self._result("pending", "request is already in progress")
        state, previous = self.idempotency.begin(key, REQUEST_TTL_SECONDS, fields)
        if previous and previous.get("fingerprint") != fingerprint:
            self._audit("prompt", target, "request_conflict", prompt=prompt, request_id=request_id)
            return self._result(
                "request_conflict", "request_id was already used for different content"
            )
        if state == "duplicate":
            if previous is None:
                self._audit("prompt", target, "unknown", prompt=prompt, request_id=request_id)
                return self._result(
                    "unknown", "idempotency receipt is missing; dispatch is blocked"
                )
            return self._result(
                "duplicate", "request was already accepted", data=previous.get("result")
            )
        if state == "busy":
            if previous and isinstance(previous.get("result"), dict):
                receipt = previous["result"]
                if receipt.get("status") in {"held", "failed"}:
                    return self._result(
                        receipt["status"],
                        receipt.get("reason") or "previous request was not dispatched",
                        data=receipt,
                    )
            self._audit("prompt", target, "pending", prompt=prompt, request_id=request_id)
            return self._result("pending", "request is already in progress")
        if state == "reconcile":
            # The existing IdemStore asks callers to reconcile unknown writes.
            # No provider call is made here because prompt replay is unsafe.
            self.idempotency.set(
                key, state="unknown", result={"status": "unknown"}, expires=TOMBSTONE_EXPIRES_AT
            )
            self._audit("prompt", target, "unknown", prompt=prompt, request_id=request_id)
            return self._result(
                "unknown", "previous outcome is uncertain; automatic replay is blocked"
            )
        if state != "send":
            self._audit("prompt", target, "unknown", prompt=prompt, request_id=request_id)
            return self._result("unknown", "idempotency state is unrecognized; dispatch is blocked")

        try:
            # Extend the pending guard before any operation that can dispatch.
            self.idempotency.set(key, state="pending", expires=TOMBSTONE_EXPIRES_AT)
        except Exception:
            self.idempotency.drop(key)
            return self._result(
                "unavailable", "could not persist the no-replay guard; prompt was not sent"
            )

        try:
            preflight = self.transport.preflight(binding)
        except Exception:
            preflight = "busy"
        if preflight == "unsupported":
            self.idempotency.drop(key)
            self._audit("prompt", target, "unsupported", prompt=prompt, request_id=request_id)
            return self._result("unsupported", "native adapter does not support this target")
        if preflight in {"invalid", "invalid_target"}:
            self.idempotency.drop(key)
            self._audit("prompt", target, "invalid_target", prompt=prompt, request_id=request_id)
            return self._result("invalid_target", "native adapter rejected the exact target")
        if preflight != "ready":
            result = {"status": "held", "reason": "target is busy or unavailable"}
            self._pin_receipt(key, result)
            self._audit("prompt", target, "held", prompt=prompt, request_id=request_id)
            return self._result("held", result["reason"], data=result)

        try:
            # Prove the audit path is writable before any provider mutation.
            self._audit(
                "prompt",
                target,
                "dispatching",
                prompt=prompt,
                request_id=request_id,
                mutation_gate="open",
                delegation="approved",
            )
        except Exception:
            self.idempotency.drop(key)
            return self._result("unavailable", "audit sink unavailable; prompt was not sent")

        def finish(status: str, state: str, result: dict[str, Any], message: str) -> dict[str, Any]:
            try:
                if status in {"held", "failed"}:
                    self._pin_receipt(key, result)
                else:
                    self.idempotency.set(
                        key,
                        state=state,
                        result=result,
                        expires=TOMBSTONE_EXPIRES_AT,
                    )
            except Exception:
                # The provider may already have accepted the prompt. Best-effort
                # mark the durable pending row as unknown; even if persistence is
                # unavailable, pending is a no-replay state on the next call.
                try:
                    self.idempotency.set(
                        key,
                        state="unknown",
                        result={"status": "unknown"},
                        expires=TOMBSTONE_EXPIRES_AT,
                    )
                except Exception:
                    pass
                try:
                    self._audit("prompt", target, "unknown", prompt=prompt, request_id=request_id)
                except Exception:
                    pass
                return self._result(
                    "unknown", "delivery receipt persistence failed; automatic replay is blocked"
                )
            try:
                self._audit("prompt", target, status, prompt=prompt, request_id=request_id)
            except Exception:
                # The transport may already have accepted the operation. Keep a
                # terminal no-replay state rather than implying it is safe to retry.
                try:
                    self.idempotency.set(
                        key,
                        state="unknown",
                        result={"status": "unknown"},
                        expires=TOMBSTONE_EXPIRES_AT,
                    )
                except Exception:
                    # The previously persisted sent/pending row is still a
                    # no-replay state; preserve the uncertain caller outcome.
                    pass
                return self._result(
                    "unknown", "audit outcome is uncertain; automatic replay is blocked"
                )
            return self._result(status, message, data=result)

        def permit() -> bool:
            return bool(
                self.mutations_enabled
                and self.authorize(self.caller_id, "prompt", target)
                and self.approve is not None
                and self.approve(self.caller_id, "prompt", target, request_id)
            )

        try:
            outcome = self.transport.send(binding, prompt, permit=permit)
        except Exception:
            # Once send() starts, a raised error is an uncertain delivery.
            result = {"status": "unknown", "request_id": request_id}
            return finish(
                "unknown",
                "unknown",
                result,
                "delivery outcome is uncertain; automatic replay is blocked",
            )

        if outcome == "accepted":
            # Existing native transport acceptance does not prove pickup or completion.
            result = {
                "status": "accepted",
                "request_id": request_id,
                "picked_up": False,
                "completed": False,
            }
            return finish("accepted", "sent", result, "transport accepted the prompt")
        if outcome == "unknown":
            result = {"status": "unknown", "request_id": request_id}
            return finish(
                "unknown",
                "unknown",
                result,
                "delivery outcome is uncertain; automatic replay is blocked",
            )
        if outcome in {"deferred", "held"}:
            result = {"status": "held", "reason": "target is busy or unavailable"}
            return finish("held", "held", result, result["reason"])
        # Existing adapters report a correlated rejection / pre-send failure as failed.
        return finish(
            "failed",
            "failed",
            {"status": "failed"},
            "native adapter rejected the prompt before acceptance",
        )

    def _held(
        self, operation: str, target: SessionTarget, request_id: str, prompt: str, reason: str
    ) -> dict[str, Any]:
        key = self._idempotency_key(request_id)
        fingerprint = _digest(
            json.dumps(
                {"op": operation, "target": target.public(), "prompt": _digest(prompt)},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        prior = self.idempotency.row(key)
        if prior and prior.get("fingerprint") != fingerprint:
            return self._result(
                "request_conflict", "request_id was already used for different content"
            )
        receipt = prior.get("result") if prior else None
        if prior and (
            prior.get("state") in {"held", "failed"}
            or (isinstance(receipt, dict) and receipt.get("status") in {"held", "failed"})
        ):
            status = receipt.get("status") if isinstance(receipt, dict) else prior["state"]
            return self._result(
                status,
                receipt.get("reason")
                if isinstance(receipt, dict)
                else "previous request was not dispatched",
                data=receipt,
            )
        if prior and prior.get("state") == "sent":
            return self._result(
                "duplicate", "request was already accepted", data=prior.get("result")
            )
        if prior and prior.get("state") == "unknown":
            return self._result(
                "unknown", "previous outcome is uncertain; automatic replay is blocked"
            )
        if prior and prior.get("state") == "pending":
            return self._result("pending", "request is already in progress")
        if not prior:
            state, raced = self.idempotency.begin(
                key,
                REQUEST_TTL_SECONDS,
                {
                    "operation": operation,
                    "fingerprint": fingerprint,
                    "target_fingerprint": _digest(json.dumps(target.public(), sort_keys=True)),
                    "prompt_fingerprint": _digest(prompt),
                    "caller_fingerprint": _digest(self.caller_id),
                },
            )
            if raced and raced.get("fingerprint") != fingerprint:
                return self._result(
                    "request_conflict", "request_id was already used for different content"
                )
            if state == "duplicate" and raced:
                self._audit(operation, target, "duplicate", prompt=prompt, request_id=request_id)
                return self._result(
                    "duplicate", "request was already accepted", data=raced.get("result")
                )
            if state == "duplicate":
                self._audit(operation, target, "unknown", prompt=prompt, request_id=request_id)
                return self._result(
                    "unknown", "idempotency receipt is missing; dispatch is blocked"
                )
            if state == "busy":
                if raced and isinstance(raced.get("result"), dict):
                    receipt = raced["result"]
                    if receipt.get("status") in {"held", "failed"}:
                        return self._result(
                            receipt["status"],
                            receipt.get("reason") or "previous request was not dispatched",
                            data=receipt,
                        )
                self._audit(operation, target, "pending", prompt=prompt, request_id=request_id)
                return self._result("pending", "request is already in progress")
            if state == "reconcile":
                self.idempotency.set(
                    key, state="unknown", result={"status": "unknown"}, expires=TOMBSTONE_EXPIRES_AT
                )
                self._audit(operation, target, "unknown", prompt=prompt, request_id=request_id)
                return self._result(
                    "unknown", "previous outcome is uncertain; automatic replay is blocked"
                )
            if state != "send":
                self._audit(operation, target, "unknown", prompt=prompt, request_id=request_id)
                return self._result(
                    "unknown", "idempotency state is unrecognized; dispatch is blocked"
                )
        result = {"status": "held", "reason": reason}
        self._pin_receipt(key, result)
        self._audit(operation, target, "held", prompt=prompt, request_id=request_id)
        return self._result("held", reason, data=result)

    def _pin_receipt(self, key: str, result: dict[str, Any]) -> None:
        """Persist a terminal non-dispatch receipt without racing IdemStore.begin().

        IdemStore.begin() only recognizes `sent` and `pending` as non-new states;
        it overwrites `held` and `failed`. A future-dated pending claim makes
        that existing atomic begin return `busy` indefinitely, while `result`
        preserves the actual held/failed outcome for this adapter's caller.
        """
        self.idempotency.set(
            key,
            state="pending",
            claimed_at=TOMBSTONE_CLAIMED_AT,
            result=result,
            expires=self.clock() + REQUEST_TTL_SECONDS,
        )

    def _nudge_pending(self, target: SessionTarget) -> bool:
        """Do not overtake an existing exact-session wake; never cancel it."""
        if self.nudge_queue is None:
            return False
        try:
            snapshot = self.nudge_queue.snapshot()
            rows = snapshot.get("items", []) if isinstance(snapshot, dict) else []
            for item in rows if isinstance(rows, list) else []:
                binding = item.get("binding") if isinstance(item, dict) else None
                if not isinstance(binding, dict):
                    continue
                if (
                    binding.get("host") == target.host
                    and binding.get("platform") == target.platform
                    and binding.get("session_id") == target.session_id
                    and item.get("status") in {"pending", "dispatching", "unknown"}
                ):
                    return True
        except Exception:
            # An unreadable queue is not proof that the target is free.
            return True
        return False

    def _resolve(self, target: SessionTarget) -> tuple[dict[str, Any] | None, str | None]:
        if target.platform not in SUPPORTED_PLATFORMS:
            return None, "unsupported"
        if _scope_for(target.platform, self.local_scope) != target.host_scope:
            return None, "wrong_host"
        try:
            rows = self.catalog.discover()
        except Exception:
            return None, "unavailable"
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("host") == target.host
            and row.get("platform") == target.platform
            and _catalog_session_id(row) == target.session_id
        ]
        if len(matches) > 1:
            return None, "ambiguous"
        if not matches:
            same_session_elsewhere = [
                row
                for row in rows
                if isinstance(row, dict)
                and row.get("platform") == target.platform
                and _catalog_session_id(row) == target.session_id
            ]
            if same_session_elsewhere:
                return None, "wrong_host"
            return None, "not_found"
        row = matches[0]
        if row.get("seat") != target.owner:
            return None, "wrong_owner"
        if row.get("stale") or row.get("status") == "stale":
            return None, "stale"
        if row.get("status") == "unavailable":
            return None, "unavailable"
        return row, None

    def _capabilities(
        self, target: SessionTarget, row: Mapping[str, Any] | None = None
    ) -> dict[str, CapabilityState]:
        result = dict(CAPABILITY_MATRIX.get((target.host_scope, target.platform), _states()))
        if row and (row.get("stale") or row.get("status") == "stale"):
            for action, state in list(result.items()):
                if state == "implemented":
                    result[action] = "stale"
        elif row:
            catalog_caps = row.get("capabilities")
            if isinstance(catalog_caps, Mapping):
                if not _catalog_flag(catalog_caps.get("read")):
                    for action in ("read", "output"):
                        if result.get(action) == "implemented":
                            result[action] = "unsupported"
                # A discoverable session is not necessarily wake-eligible.
                # Require the catalog's explicit boolean rather than a
                # truthy/missing/malformed value before offering prompt.
                if not _catalog_flag(catalog_caps.get("wake")) and result.get("prompt") == "implemented":
                    result["prompt"] = "unsupported"
            elif result.get("prompt") == "implemented":
                result["prompt"] = "unsupported"
        return result

    def _audit(
        self,
        operation: str,
        target: SessionTarget | None,
        outcome: str,
        *,
        prompt: str | None = None,
        request_id: str | None = None,
        mutation_gate: str | None = None,
        delegation: str | None = None,
    ) -> None:
        record = {
            "event": "seat_control",
            "operation": operation,
            "outcome": outcome,
            "prompt_chars": len(prompt) if prompt is not None else None,
            "at": self.clock(),
        }
        if mutation_gate is not None:
            record["mutation_gate"] = mutation_gate
        if delegation is not None:
            record["delegation"] = delegation
        self.audit(record)

    @staticmethod
    def _result(status: str, message: str | None = None, *, data: Any = None) -> dict[str, Any]:
        result: dict[str, Any] = {"status": status}
        if message:
            result["message"] = message
        if data is not None:
            result["data"] = data
        return result
