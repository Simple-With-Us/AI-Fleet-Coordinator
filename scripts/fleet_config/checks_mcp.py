"""``mcp`` checks:  the MCP server names a platform's config file holds.

The check type is ``MCP_SERVERS_REGISTERED``.  The target is the platform's MCP config
file and the expected server names are ``rule.expected.names``.  The names are
identifiers only;  nothing here reads a command, an argument or an environment block.

The config shapes that are read, all of them as they are written by the fleet's own
installers:

  * ``mcpServers`` at the top level (Claude, Kimi, Copilot, Muse, MiniMax, Cursor and
    Antigravity);
  * ``mcp.servers`` at the top level (OpenCode);
  * ``mcp`` at the top level, one key per server (Fx);
  * ``mcp_servers`` at the top level (the TOML config of Codex and Grok).

A file that holds none of these shapes is not a file this check can read, so it comes
back as unparseable rather than as a pass.

A rule with no ``expected.names`` is a presence check:  the config file must be there
and must hold an MCP section, and the names it holds are reported.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

import os
from typing import Any

from .checks import CheckContext, CheckError, build_result, expand_target
from .checks_hooks import read_config_document
from .types import (
    CheckResult,
    CheckState,
    CheckType,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
)

__all__ = ["CHECKS"]


def server_names(document: dict[str, Any]) -> tuple[str, ...] | None:
    """The MCP server names a config document holds, sorted, or None when it holds none.

    None means the document is not an MCP config this reader understands, which is not
    the same as a document with no servers:  an empty ``mcpServers`` object is a
    readable config with no servers.
    """
    found: list[str] = []
    for key in ("mcpServers", "mcp_servers"):
        value = document.get(key)
        if isinstance(value, dict):
            found.extend(name for name in value.keys() if isinstance(name, str))
    mcp = document.get("mcp")
    if isinstance(mcp, dict):
        servers = mcp.get("servers")
        if isinstance(servers, dict):
            found.extend(name for name in servers.keys() if isinstance(name, str))
        else:
            found.extend(name for name in mcp.keys() if isinstance(name, str))
    if not found and not any(
        isinstance(document.get(key), dict) for key in ("mcpServers", "mcp_servers", "mcp")
    ):
        return None
    return tuple(sorted(set(found)))


def check_mcp_servers_registered(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Check that the expected MCP servers are registered in a platform's config file."""
    target = rule.check.target
    try:
        path = expand_target(target, ctx.home)
    except CheckError as exc:
        return build_result(
            rule,
            platform,
            CheckState.MISSING,
            str(exc),
            ReasonCode.MISSING,
            ObservedState(present=False),
            ctx,
        )

    if not os.path.exists(path):
        return build_result(
            rule,
            platform,
            CheckState.MISSING,
            "The MCP config file %s is missing." % target,
            ReasonCode.MISSING,
            ObservedState(present=False),
            ctx,
        )

    if not os.path.isfile(path):
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The MCP config target %s is not a regular file." % target,
            ReasonCode.ERROR,
            ObservedState(present=None),
            ctx,
        )

    try:
        document = read_config_document(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The MCP config file %s cannot be read:  %s" % (target, exc),
            ReasonCode.UNPARSEABLE,
            ObservedState(present=None),
            ctx,
        )

    names = server_names(document)
    if names is None:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The MCP config file %s holds no MCP server section this reader understands." % target,
            ReasonCode.UNPARSEABLE,
            ObservedState(present=None),
            ctx,
        )

    expected_names = tuple(rule.expected.names)
    present = {name for name in names}
    matched = sorted(name for name in expected_names if name in present)
    missing = [name for name in expected_names if name not in present]
    others = len([name for name in names if name not in set(expected_names)])

    if rule.expected.present is False:
        if names:
            return build_result(
                rule,
                platform,
                CheckState.DRIFT,
                "The MCP config file %s registers %d server, which the rule says must be absent."
                % (target, len(names)),
                ReasonCode.DRIFT,
                ObservedState(present=True, names=tuple(sorted(names))),
                ctx,
            )
        return build_result(
            rule,
            platform,
            CheckState.PASS,
            "The MCP config file %s registers no server, as the rule expects." % target,
            ReasonCode.OK,
            ObservedState(present=True, names=()),
            ctx,
            proof_level_achieved=ProofLevel.L0,
        )

    observed = ObservedState(present=True, names=tuple(matched))

    if missing:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The MCP server %s is not registered in %s."
            % (", ".join(repr(name) for name in missing), target),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    if not expected_names:
        return build_result(
            rule,
            platform,
            CheckState.PASS,
            "The MCP config file %s is present and holds %d server." % (target, len(names)),
            ReasonCode.OK,
            observed,
            ctx,
            proof_level_achieved=ProofLevel.L0,
        )

    reason = "The MCP server %s is registered in %s." % (
        ", ".join(repr(name) for name in matched),
        target,
    )
    if others:
        if others == 1:
            reason += "  One other server is registered there."
        else:
            reason += "  %d other servers are registered there." % others
    return build_result(
        rule,
        platform,
        CheckState.PASS,
        reason,
        ReasonCode.OK,
        observed,
        ctx,
        proof_level_achieved=ProofLevel.L0,
    )


CHECKS = {
    CheckType.MCP_SERVERS_REGISTERED: check_mcp_servers_registered,
}
