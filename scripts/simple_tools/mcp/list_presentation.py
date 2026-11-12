"""Compact default for seat-mcp list/search read tools (AFC-756). Lossless when full=true."""

from __future__ import annotations

import json
from collections.abc import Mapping, MutableMapping
from typing import Any

COMPACT_DEFAULT_LIMIT = 10
COMPACT_BODY_LIMIT = 280
FULL_ZULIP_BODY_LIMIT = 4000

TEXT_FIELD_NAMES = frozenset(
    {
        "body",
        "content",
        "description",
        "summary",
        "text",
        "subtitle",
        "message",
        "markdown",
        "renderedBody",
    }
)

LIST_ROOT_SUFFIXES = ("s", "ies")  # issues, comments, nodes handled explicitly

LINEAR_LIST_SEARCH_TOOLS = frozenset(
    {
        "linear_list",
        "linear_query",
    }
)

LINEAR_LIST_SEARCH_PREFIX = "linear_list_"

ZULIP_MESSAGE_READ_TOOLS = frozenset(
    {
        "zulip_read_topic",
        "zulip_inbox",
        "zulip_dm_read",
        "zulip_get_message",
        "zulip_search",
        "zulip_message_history",
    }
)

NOTES_LIST_TOOLS = frozenset({"notes_list"})


def approximate_tokens(value: object) -> int:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    return max(1, (len(encoded) + 3) // 4)


def split_full_arg(args: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
    work = dict(args)
    full = bool(work.pop("full", False))
    return work, full


def effective_limit(
    args: Mapping[str, Any], *, schema_default: int, full: bool, limit_explicit: bool = False
) -> int:
    if limit_explicit:
        return int(args["limit"])
    if full:
        return schema_default
    return min(schema_default, COMPACT_DEFAULT_LIMIT)


def truncate_text(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    cut = text[:limit].rstrip()
    if not cut.endswith("..."):
        cut += "..."
    return cut, True


def compact_string_fields(obj: Any, *, limit: int, depth: int = 0) -> Any:
    if depth > 24:
        return obj
    if isinstance(obj, str):
        return truncate_text(obj, limit)[0]
    if isinstance(obj, list):
        return [compact_string_fields(item, limit=limit, depth=depth + 1) for item in obj]
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            if key in TEXT_FIELD_NAMES and isinstance(value, str):
                text, truncated = truncate_text(value, limit)
                out[key] = text
                if truncated:
                    out[f"{key}_truncated"] = True
            else:
                out[key] = compact_string_fields(value, limit=limit, depth=depth + 1)
        return out
    return obj


def _slice_nodes_array(nodes: list[Any], top_n: int) -> tuple[list[Any], bool]:
    if len(nodes) <= top_n:
        return nodes, False
    return nodes[:top_n], True


def compact_linear_graphql(data: Any, *, top_n: int) -> tuple[Any, dict[str, Any]]:
    meta: dict[str, Any] = {"compact": True}
    if not isinstance(data, dict):
        return compact_string_fields(data, limit=COMPACT_BODY_LIMIT), meta

    def walk(value: Any) -> Any:
        if isinstance(value, list):
            trimmed, cut = _slice_nodes_array(value, top_n)
            meta["truncated"] = meta.get("truncated", False) or cut
            return [walk(item) for item in trimmed]
        if isinstance(value, dict):
            out: dict[str, Any] = {}
            for key, child in value.items():
                if key == "nodes" and isinstance(child, list):
                    trimmed, cut = _slice_nodes_array(child, top_n)
                    meta["truncated"] = meta.get("truncated", False) or cut
                    out[key] = [walk(item) for item in trimmed]
                    if cut:
                        out["nodes_omitted"] = len(child) - len(trimmed)
                elif key == "edges" and isinstance(child, list):
                    trimmed, cut = _slice_nodes_array(child, top_n)
                    meta["truncated"] = meta.get("truncated", False) or cut
                    out[key] = [walk(item) for item in trimmed]
                    if cut:
                        out["edges_omitted"] = len(child) - len(trimmed)
                else:
                    out[key] = walk(child)
            return compact_string_fields(out, limit=COMPACT_BODY_LIMIT)
        return value

    return walk(data), meta


def compact_linear_list_stdout(text: str, *, top_n: int) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(payload, list):
        trimmed, _ = _slice_nodes_array(payload, top_n)
        trimmed = compact_string_fields(trimmed, limit=COMPACT_BODY_LIMIT)
        return json.dumps(trimmed, ensure_ascii=False, indent=2)
    compacted, _ = compact_linear_graphql(payload, top_n=top_n)
    return json.dumps(compacted, ensure_ascii=False, indent=2)


def compact_zulip_message_item(item: MutableMapping[str, Any], *, body_limit: int) -> None:
    body = item.get("body")
    if isinstance(body, str):
        text, truncated = truncate_text(body, body_limit)
        item["body"] = text
        if truncated:
            item["truncated"] = True


def compact_notes_list_result(
    result: Mapping[str, Any], *, top_n: int = COMPACT_DEFAULT_LIMIT
) -> dict[str, Any]:
    out = dict(result)
    notes = list(out.get("notes") or [])
    trimmed, cut = _slice_nodes_array(notes, top_n)
    out["notes"] = trimmed
    if cut:
        out["compact_truncated"] = True
    return out


def tool_wants_compact(name: str, *, full: bool) -> bool:
    if full:
        return False
    if name in ZULIP_MESSAGE_READ_TOOLS or name in NOTES_LIST_TOOLS:
        return True
    if name in LINEAR_LIST_SEARCH_TOOLS or name.startswith(LINEAR_LIST_SEARCH_PREFIX):
        return True
    if name == "linear_search_documentation":
        return True
    return False


def is_lossless_json_path(name: str, args: Mapping[str, Any]) -> bool:
    """CLI/board --json and explicit full=true stay lossless."""
    return bool(args.get("full"))
