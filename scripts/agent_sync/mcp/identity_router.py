"""Per-call identity for `simple-chat mcp` (AFC-331, docs/protocols/per-call-identity.md sections 2-4).

One stdio connection may act as several bots.  The connection says who may act (the allowed set:
every agent bot whose credential file is on this host, or the launcher's seat alone), and each call
says who is acting, in its `identity` argument:

  - a write (WRITE_TOOLS) with no `identity` is refused with identity_required, whatever the default;
  - a read with no `identity` acts as the default (the launcher's seat, --as, AGENT_SEAT or
    --default-seat), and a connection with no default refuses it with identity_required too;
  - an identity outside the allowed set is refused with identity_not_allowed;
  - a removed tool name (renamed-tools.json) is refused with unknown_tool naming its replacement.

Each identity gets its own Tools over its own Agent, opened on first use with that bot's own
credential file and checked against users/me exactly as the startup bot is (the bot check, the role
check), then kept for the life of the process.  So spacing, idempotency and the post tag are per
identity, and no call ever uses a key that belongs to another bot.  Every write is appended to the
identity audit log (identity_log.py) with the client name from `initialize`.

`tools/list` serves each tool with an `identity` property whose enum is this connection's allowed
set, required on writes, so a client is never offered a name it cannot use.  The inner Tools still
validate against tools.json without it:  the router takes `identity` out first.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Mapping

from .. import identities as X
from .. import identity_log as AUDIT
from . import tools as T

RENAMED_PATH = os.path.join(T.HERE, "renamed-tools.json")
IDENTITY_FREE = frozenset({"identities"})  # connection-level:  takes no identity argument
# Answered by the router with no bot at all:  the guide needs no credential, so a connection with no default
# identity can read it, and a client that adds `identity` to every call is not refused.
IDENTITY_OPTIONAL = frozenset({"guide"})


def renamed_tools() -> dict[str, str]:
    """old name -> new name, for every tool name that was removed (renamed-tools.json)."""
    with open(RENAMED_PATH, encoding="utf-8") as fh:
        data = json.load(fh)
    renamed = data.get("renamed") if isinstance(data, dict) else None
    if not isinstance(renamed, dict):
        raise ValueError("%s: `renamed` must be an object" % RENAMED_PATH)
    return {str(old): str(new) for old, new in renamed.items()}


def identity_property(allowed: list[str], default: str | None) -> dict[str, Any]:
    tail = ("reads without it act as %s" % default) if default else "this connection has no default, so reads need it too"
    return {"type": "string", "enum": list(allowed),
            "description": "The bot this call acts as.  Writes must name it;  %s.  identities lists the set." % tail}


def decorate(spec: Mapping[str, Any], allowed: list[str], default: str | None) -> dict[str, Any]:
    """tools/list for this connection:  every tool but `identities` gains `identity` (required on writes)."""
    tools = []
    for tool in spec["tools"]:
        tool = copy.deepcopy(dict(tool))
        if tool["name"] not in IDENTITY_FREE:
            schema = tool["inputSchema"]
            schema.setdefault("properties", {})["identity"] = identity_property(allowed, default)
            if tool["name"] in T.WRITE_TOOLS:
                schema["required"] = [*(schema.get("required") or []), "identity"]
        tools.append(tool)
    return {"tools": tools}


class OpenFailed(Exception):
    """An identity's credential or bot check failed when it was first used."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class Router:
    """The stdio server's tools object:  `spec`, `call`, `seat` (the default identity)."""

    def __init__(self, *, default: str | None, allowed: Mapping[str, Path], open_tools: Callable[[str], T.Tools],
                 env: Mapping[str, str], home: Path | None, codex_thread_meta: bool = False,
                 launcher_pinned: bool = False, log: Callable[[str], None] = lambda text: None) -> None:
        self.default = default
        self.seat = default
        self.allowed = dict(allowed)
        self.allowed_names = sorted(self.allowed)
        self.open_tools = open_tools
        self.env = env
        self.home = home
        self.codex_thread_meta = codex_thread_meta
        self.launcher_pinned = launcher_pinned
        self.log = log
        self.renamed = renamed_tools()
        served = T.load_served_tools()
        self.by_name = {tool["name"]: tool for tool in served["tools"]}
        self.spec = decorate(served, self.allowed_names, default)
        self.client_info: Mapping[str, Any] | None = None
        self._tools: dict[str, T.Tools] = {}
        self._lock = threading.Lock()

    # ---- the identities this process opened -------------------------------------------------
    def adopt(self, identity: str, tools: T.Tools) -> None:
        """Keep an identity opened elsewhere (the default, verified at startup)."""
        self._tools[identity] = tools

    def scrub(self, text: str) -> str:
        for tools in list(self._tools.values()):
            text = tools.agent.client.scrub(text)
        return text

    def tools_for(self, identity: str) -> T.Tools:
        with self._lock:
            tools = self._tools.get(identity)
            if tools is None:
                tools = self.open_tools(identity)
                self._tools[identity] = tools
            return tools

    # ---- results ----------------------------------------------------------------------------
    def _error(self, error: T.ToolError) -> dict[str, Any]:
        obj = error.as_object(self.scrub)
        return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}], "isError": True,
                "_meta": {T.ERROR_META_KEY: obj}}

    def _shown(self, raw: Any) -> str:
        return raw if isinstance(raw, str) and T._SAFE_NAME_RE.match(raw.replace("-", "_")) else "(not shown)"

    def _guide(self, args: Any) -> dict[str, Any]:
        """The guide tool:  no identity, no credential, nothing written.  `identity` was taken out already."""
        args = {} if args is None else args
        problem = T.schema_problem(self.by_name["guide"]["inputSchema"], args) if isinstance(args, dict) else "arguments must be an object"
        if problem:
            return self._error(T.ToolError("invalid_argument", problem.replace("this tool takes", "guide takes")))
        try:
            return T.guide_result(args, self.env)
        except T.ToolError as error:
            return self._error(error)

    # ---- entry ------------------------------------------------------------------------------
    def call(self, name: str, arguments: Any, *, meta: Any = None) -> dict[str, Any]:
        if name in self.renamed:
            new = self.renamed[name]
            self.log("%s unknown_tool (renamed %s)" % (name, new))
            return self._error(T.ToolError("unknown_tool", "%s was renamed %s (AFC-331):  call %s instead" % (name, new, new),
                                           extra={"replacement": new}))
        if name not in self.by_name:
            raise T.UnknownTool(name)
        args = arguments
        claimed: Any = None
        named = isinstance(args, dict) and "identity" in args
        if named:
            claimed = args["identity"]
            args = {key: value for key, value in args.items() if key != "identity"}
        if name in IDENTITY_OPTIONAL:
            return self._guide(args)
        if name in IDENTITY_FREE:
            if named:
                return self._error(T.ToolError("invalid_argument", "identities takes no arguments;  it lists this "
                                                "connection's identities"))
            return self._identities(args)
        write = name in T.WRITE_TOOLS
        if not named:
            if write:
                return self._audited(name, None, self._error(T.ToolError(
                    "identity_required", "%s is a write:  name the bot it acts as in identity (one of %s;  identities "
                    "lists them)" % (name, ", ".join(self.allowed_names) or "none on this host"),
                    extra={"allowed": self.allowed_names})))
            if self.default is None:
                return self._error(T.ToolError("identity_required", "this connection has no default identity:  name "
                                               "the bot in identity (identities lists them)",
                                               extra={"allowed": self.allowed_names}))
            identity = self.default
        else:
            identity = X.resolve_name(claimed)
            if identity is None or identity not in self.allowed:
                shown = self._shown(claimed)
                why = (" (a launcher pinned this session to %s)" % self.default) if self.launcher_pinned else ""
                result = self._error(T.ToolError(
                    "identity_not_allowed", "identity %s is not in this connection's allowed set%s:  %s"
                    % (shown, why, ", ".join(self.allowed_names) or "none"), extra={"allowed": self.allowed_names}))
                return self._audited(name, shown, result) if write else result
        try:
            tools = self.tools_for(identity)
        except OpenFailed as exc:
            result = self._error(T.ToolError(exc.code, exc.message, retryable=exc.retryable))
            return self._audited(name, identity, result) if write else result
        try:
            result = tools.call(name, args, meta=meta) if self.codex_thread_meta and identity == "CODEX" \
                else tools.call(name, args)
        except (KeyboardInterrupt, SystemExit):
            if write:  # a write that was in flight may have landed:  say so in the audit log, then let the interrupt go on
                AUDIT.append(self.env, self.home, transport=T.TRANSPORT, default_identity=self.default, identity=identity,
                             tool=name, outcome="interrupted", client=self.client_info)
            raise
        return self._audited(name, identity, result) if write else result

    def _audited(self, name: str, identity: str | None, result: Mapping[str, Any]) -> dict[str, Any]:
        """Append the write's audit row (ids only), then return its result."""
        structured = result.get("structuredContent") if isinstance(result, Mapping) else None
        message_id = structured.get("id") if isinstance(structured, Mapping) else None
        error = (result.get("_meta") or {}).get(T.ERROR_META_KEY) if result.get("isError") else None
        outcome = str(error.get("code")) if isinstance(error, Mapping) else "ok"
        AUDIT.append(self.env, self.home, transport=T.TRANSPORT, default_identity=self.default, identity=identity,
                     tool=name, outcome=outcome, message_id=message_id if isinstance(message_id, int) else None,
                     client=self.client_info)
        return dict(result)

    def _identities(self, args: Any) -> dict[str, Any]:
        if args not in (None, {}):
            return self._error(T.ToolError("invalid_argument", "identities takes no arguments"))
        rows = [{"identity": name, "zulip": "configured"} for name in self.allowed_names]
        info = {"default_identity": self.default, "authenticated_seat": None, "transport": T.TRANSPORT,
                "identities": rows}
        text = json.dumps(info, ensure_ascii=False)
        return {"content": [{"type": "text", "text": text}], "structuredContent": info}
