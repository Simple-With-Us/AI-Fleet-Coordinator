"""The minimized copy of the Mac facts for Grok Bot (`gb/mac-facts.json`).

Grok Bot reads the repository with a read-only token and runs in the cloud, so its copy carries no local
paths and no long free text.  Starting from the (already redacted) full document:

  * every `path` key (and `path_exact`) is dropped and the lane is named by its folder name instead (`lane`, unless the row has
    one), which is what `open_loops.views.lane_name` falls back to;
  * any other string that still holds a Mac path (`~/...`, `/Users/...`, `/Volumes/...`) has the path replaced by
    `[path]`;
  * every string over 80 characters is cut to 80 (a reason, a question, a quote), except a few join keys
    (a branch, a repository, a lane name) that a consumer matches exactly;
  * a last check on the serialized text refuses the copy if a Mac path is still in it.

Pure functions, no I/O.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

__all__ = ["QUOTE_CAP", "PathLeak", "minimize", "mac_path_in"]

QUOTE_CAP = 80
ELLIPSIS = "…"
# Keys whose strings are matched exactly by a consumer:  never cut.
EXACT_KEYS = frozenset({"branch", "repo", "owner_repo", "lane", "name", "identifier", "topic", "head_ref", "signed_for", "sig", "sig_alg",
                       "kind", "schema", "tool", "version", "host", "sha256", "path_in_repo"})
_PATH = re.compile(r"(?:~|/Users|/Volumes)/[^\s\"',;)\]}]*|/Users\b|/Volumes\b")
_LEAK = re.compile(r"/Users/|/Volumes/|~/")
DROPPED_KEYS = ("path", "path_exact")       # the path, and the flag that says whether it was exact
MAX_DEPTH = 64


class PathLeak(Exception):
    """The minimized copy still holds a Mac path;  it must not be published."""


def mac_path_in(text: str) -> bool:
    return bool(_LEAK.search(text))


def _scrub(text: str) -> str:
    return _PATH.sub("[path]", text)


def _cut(text: str) -> str:
    return text if len(text) <= QUOTE_CAP else text[: QUOTE_CAP - 1] + ELLIPSIS


def _lane_from(path: str) -> str | None:
    base = os.path.basename(path.rstrip("/"))
    return base[:QUOTE_CAP] if base and not _LEAK.search(base) else None


def _walk(node: Any, key: str | None, depth: int) -> Any:
    if depth > MAX_DEPTH:
        return None
    if isinstance(node, str):
        text = _scrub(node)
        return text if key in EXACT_KEYS else _cut(text)
    if isinstance(node, list):
        return [_walk(item, key, depth + 1) for item in node]
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        path = node.get("path")
        for child_key, value in node.items():
            if child_key in DROPPED_KEYS:
                continue
            out[child_key] = _walk(value, child_key, depth + 1)
        if isinstance(path, str):
            lane = _lane_from(path)
            if lane and not out.get("lane"):
                out["lane"] = lane
        return out
    return node


def minimize(document: dict) -> dict:
    """The Grok Bot copy of `document` (which is not changed).  Raises PathLeak if a Mac path survives."""
    copy = _walk(document, None, 0)
    text = json.dumps(copy, ensure_ascii=True)
    if mac_path_in(text):
        raise PathLeak("a Mac path is still in the minimized copy")
    return copy
