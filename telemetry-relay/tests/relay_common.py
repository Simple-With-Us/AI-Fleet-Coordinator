"""Shared helpers for the fleet-telemetry-relay tests (stdlib only, no PyYAML)."""
from __future__ import annotations

import json
import re
from pathlib import Path

RELAY_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = RELAY_ROOT / "otelcol.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPTS = RELAY_ROOT.parent / "scripts"
COOLIFY = RELAY_ROOT / "COOLIFY.md"
CONFIGURE = SCRIPTS / "telemetry" / "configure-agents.sh"

# producer -> relay contract.  Ports, platforms and bearer names are pinned here
# so the config, the fixtures and the Coolify doc cannot drift apart.
PRODUCERS = {
    "claude": {
        "port": 4318,
        "platform": "claude-code",
        "agent": "claude-code",
        "bearer": "RELAY_BEARER_CLAUDE_CODE",
        "fixtures": [
            "claude-code-api-request.json",
            "claude-code-user-prompt.json",
            "claude-code-tool-result.json",
        ],
    },
    "codex": {
        "port": 4319,
        "platform": "codex",
        "agent": "codex",
        "bearer": "RELAY_BEARER_CODEX",
        "fixtures": ["codex-turn.json"],
    },
    "grok": {
        "port": 4320,
        "platform": "grok-cli",
        "agent": "grok-cli",
        "bearer": "RELAY_BEARER_GROK",
        "fixtures": ["grok-cli-turn.json"],
    },
    "shim": {
        "port": 4321,
        "platform": "hook-shim",
        "agent": None,  # shim keeps the producer-set platform as gen_ai.agent.name
        "bearer": "RELAY_BEARER_SHIM",
        "fixtures": ["hook-shim-after-response.json"],
    },
}

# Vendor attr -> gen_ai target, per producer.  session.id and session_id both
# map to conversation.id (session_id is the fallback).
MAPPINGS = {
    "claude": [
        ("model", "gen_ai.request.model"),
        ("session.id", "gen_ai.conversation.id"),
        ("session_id", "gen_ai.conversation.id"),
        ("input_tokens", "gen_ai.usage.input_tokens"),
        ("output_tokens", "gen_ai.usage.output_tokens"),
        ("cost_usd", "gen_ai.usage.cost"),
        ("duration_ms", "turn.duration_ms"),
        ("tool_name", "gen_ai.tool.name"),
    ],
    "codex": [
        ("model", "gen_ai.request.model"),
        ("session.id", "gen_ai.conversation.id"),
        ("session_id", "gen_ai.conversation.id"),
        ("input_tokens", "gen_ai.usage.input_tokens"),
        ("output_tokens", "gen_ai.usage.output_tokens"),
        ("cost_usd", "gen_ai.usage.cost"),
        ("duration_ms", "turn.duration_ms"),
        ("tool_name", "gen_ai.tool.name"),
    ],
    "grok": [
        ("model", "gen_ai.request.model"),
        ("session.id", "gen_ai.conversation.id"),
        ("session_id", "gen_ai.conversation.id"),
        ("input_tokens", "gen_ai.usage.input_tokens"),
        ("output_tokens", "gen_ai.usage.output_tokens"),
        ("cost_usd", "gen_ai.usage.cost"),
        ("duration_ms", "turn.duration_ms"),
        ("tool_name", "gen_ai.tool.name"),
    ],
    "shim": [
        ("model", "gen_ai.request.model"),
        ("conversation_id", "gen_ai.conversation.id"),
        ("session.id", "gen_ai.conversation.id"),
        ("session_id", "gen_ai.conversation.id"),
        ("input_tokens", "gen_ai.usage.input_tokens"),
        ("output_tokens", "gen_ai.usage.output_tokens"),
        ("cost_usd", "gen_ai.usage.cost"),
        ("duration_ms", "turn.duration_ms"),
        ("tool_name", "gen_ai.tool.name"),
        ("generation_id", "turn.generation_id"),
    ],
}

ENV_NAMES = (
    "RELAY_BEARER_CLAUDE_CODE",
    "RELAY_BEARER_CODEX",
    "RELAY_BEARER_GROK",
    "RELAY_BEARER_SHIM",
    "SENTRY_FLEET_OTLP_ENDPOINT",
    "SENTRY_FLEET_OTLP_KEY",
    "USAGE_MONITOR_OTLP_METRICS_ENDPOINT",
    "USAGE_MONITOR_OTLP_TRACES_ENDPOINT",
    "USAGE_MONITOR_INGEST_TOKEN",
)

_SET_RE = re.compile(
    r'set\(attributes\["([^"]+)"\],\s*(?:attributes\["([^"]+)"\]|"([^"]*)")\)'
    r"(?:\s+where\s+(.+))?"
)
_ATTR_CMP = re.compile(r'attributes\["([^"]+)"\]\s*(==|!=)\s*(nil|"[^"]*")')


def config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def block(text: str, header: str) -> str:
    """Slice of `text` from `header:` to the next same-or-less indented key."""
    match = re.search(r"(?m)^(\s*)%s:\s*$" % re.escape(header), text)
    if match is None:
        raise KeyError("missing block %s" % header)
    indent = match.group(1)
    rest = text[match.end() :]
    next_key = re.search(r"(?m)^%s\S" % re.escape(indent), rest)
    return rest if next_key is None else rest[: next_key.start()]


def statements(text: str, producer: str, kind: str) -> list[str]:
    """OTTL `set(...)` lines under transform/<producer> <kind>_statements."""
    body = block(text, "transform/%s" % producer)
    section = block(body, "%s_statements" % kind)
    found = []
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("- set(") or stripped.startswith("set("):
            found.append(stripped.lstrip("- ").rstrip())
    return found


def _where_ok(clause: str | None, attrs: dict) -> bool:
    if not clause:
        return True
    parts = re.split(r"\s+and\s+", clause)
    for part in parts:
        hit = _ATTR_CMP.fullmatch(part.strip())
        if hit is None:
            return False
        key, op, rhs = hit.group(1), hit.group(2), hit.group(3)
        present = key in attrs and attrs[key] is not None
        if rhs == "nil":
            ok = (not present) if op == "==" else present
        else:
            want = rhs.strip('"')
            actual = attrs.get(key)
            ok = (actual == want) if op == "==" else (actual != want)
        if not ok:
            return False
    return True


def apply_sets(stmts: list[str], attrs: dict) -> dict:
    """Apply a subset of OTTL `set` statements to a shallow copy of `attrs`."""
    out = dict(attrs)
    for stmt in stmts:
        hit = _SET_RE.search(stmt)
        if hit is None:
            continue
        dest, src_key, literal, where = hit.group(1), hit.group(2), hit.group(3), hit.group(4)
        if not _where_ok(where, out):
            continue
        if src_key is not None:
            if src_key in out and out[src_key] is not None:
                out[dest] = out[src_key]
        else:
            out[dest] = literal
    return out


def env_refs(text: str) -> set[str]:
    """Env interpolations in collector YAML, ignoring comments."""
    names: set[str] = set()
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        names.update(re.findall(r"\$\{env:([A-Z0-9_]+)\}", line))
    return names
