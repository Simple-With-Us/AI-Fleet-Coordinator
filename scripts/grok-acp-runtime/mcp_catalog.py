#!/usr/bin/env python3
"""Expand named MCP servers from ~/.grok/config.toml into ACP mcpServers objects.

ACP session/new takes full server objects (command/url), not names.  An empty
list is not an allow-list: Grok then loads every enabled server from config.
This helper is the name -> object step so seat-mcp can pass opts.mcpServers.

Never prints env/header values.  Does not spawn servers.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

USER_CONFIG = Path.home() / ".grok" / "config.toml"

JsonDict = dict[str, Any]


class CatalogError(ValueError):
    """Unknown, disabled, or unusable MCP server name."""


_TABLE_RE = re.compile(
    r"^\[mcp_servers\.(?P<name>[A-Za-z0-9_-]+)(?P<suffix>\.(?P<sub>env|headers))?\]\s*$"
)
_KEY_RE = re.compile(r"^(?P<key>[A-Za-z0-9_]+)\s*=\s*(?P<val>.+?)\s*$")


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if text in ("true", "false"):
        return text == "true"
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return bytes(text[1:-1], "utf-8").decode("unicode_escape")
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    try:
        if "." in text:
            return float(text)
        return int(text)
    except ValueError:
        return text


def _parse_array(first: str, lines: list[str], idx: int) -> tuple[list[Any], int]:
    buf = first.strip()
    while "]" not in buf:
        idx += 1
        if idx >= len(lines):
            break
        buf += " " + lines[idx].strip()
    inner = buf[buf.find("[") + 1 : buf.rfind("]")]
    items: list[Any] = []
    for part in inner.split(","):
        part = part.strip()
        if part:
            items.append(_parse_scalar(part))
    return items, idx


def parse_mcp_servers(text: str) -> dict[str, JsonDict]:
    """Parse [mcp_servers.NAME] plus .env / .headers tables from grok config.toml."""
    servers: dict[str, JsonDict] = {}
    name: str | None = None
    sub: str | None = None
    lines = text.splitlines()
    idx = 0
    while idx < len(lines):
        raw = lines[idx].strip()
        if not raw or raw.startswith("#"):
            idx += 1
            continue
        table = _TABLE_RE.match(raw)
        if table:
            name = table.group("name")
            sub = table.group("sub")
            servers.setdefault(name, {"enabled": True, "env": {}, "headers": {}, "args": []})
            idx += 1
            continue
        if raw.startswith("["):
            name = None
            sub = None
            idx += 1
            continue
        if name is None:
            idx += 1
            continue
        key_m = _KEY_RE.match(raw)
        if not key_m:
            idx += 1
            continue
        key = key_m.group("key")
        val_raw = key_m.group("val")
        if val_raw.startswith("["):
            parsed, idx = _parse_array(val_raw, lines, idx)
            value: Any = parsed
        else:
            value = _parse_scalar(val_raw)
        target = servers[name]
        if sub in ("env", "headers"):
            target.setdefault(sub, {})[key] = value if isinstance(value, str) else str(value)
        elif key == "args":
            target["args"] = value if isinstance(value, list) else [value]
        else:
            target[key] = value
        idx += 1
    return servers


def load_catalog(path: Path | None = None) -> dict[str, JsonDict]:
    cfg = path or USER_CONFIG
    if not cfg.is_file():
        raise CatalogError("missing %s" % cfg)
    return parse_mcp_servers(cfg.read_text(encoding="utf-8"))


def to_acp(name: str, cfg: JsonDict) -> JsonDict:
    """ACP McpServer object.  Values stay in the object; callers must not log them."""
    if cfg.get("enabled") is False:
        raise CatalogError("%s is disabled in config.toml" % name)
    url = cfg.get("url")
    command = cfg.get("command")
    if isinstance(url, str) and url.strip():
        headers = cfg.get("headers") or {}
        header_list = [
            {"name": str(k), "value": str(v)}
            for k, v in headers.items()
        ]
        transport = str(cfg.get("type") or "http")
        return {
            "type": transport,
            "name": name,
            "url": url,
            "headers": header_list,
        }
    if isinstance(command, str) and command.strip():
        env = cfg.get("env") or {}
        env_list = [{"name": str(k), "value": str(v)} for k, v in env.items()]
        args = cfg.get("args") or []
        if not isinstance(args, list):
            args = [args]
        return {
            "name": name,
            "command": command,
            "args": [str(a) for a in args],
            "env": env_list,
        }
    raise CatalogError("%s has no command or url" % name)


def expand_names(names: list[str], catalog: dict[str, JsonDict] | None = None) -> list[JsonDict]:
    """Fail closed on unknown / disabled names.  Empty input -> empty ACP list."""
    if not names:
        return []
    cat = catalog if catalog is not None else load_catalog()
    out: list[JsonDict] = []
    seen: set[str] = set()
    for raw in names:
        name = str(raw).strip()
        if not name:
            raise CatalogError("empty MCP server name")
        if name in seen:
            continue
        seen.add(name)
        if name not in cat:
            known = ", ".join(sorted(cat)) or "(none)"
            raise CatalogError(
                "unknown MCP server %r (not in ~/.grok/config.toml).  Known: %s"
                % (name, known)
            )
        out.append(to_acp(name, cat[name]))
    return out
