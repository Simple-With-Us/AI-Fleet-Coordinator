"""Normalize LaunchAgent plists for comparison."""

from __future__ import annotations

import json
import plistlib
import re
from pathlib import Path
from typing import Any
from xml.parsers.expat import ExpatError

# Keys that define job shape for drift detection (not log paths).
COMPARE_KEYS = (
    "Label",
    "ProgramArguments",
    "Program",
    "StartInterval",
    "RunAtLoad",
    "KeepAlive",
    "ProcessType",
    "StartCalendarInterval",
    "ThrottleInterval",
    "WorkingDirectory",
    "EnvironmentVariables",
    "LaunchOnlyOnce",
    "StandardOutPath",
    "StandardErrorPath",
)

OWNER_HOME_PREFIX = "/Users/jay"
COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _load_plist_bytes(raw: bytes) -> dict[str, Any] | None:
    def _parse(blob: bytes) -> dict[str, Any] | None:
        try:
            data = plistlib.loads(blob)
        except (plistlib.InvalidFileException, ValueError, OSError, ExpatError):
            return None
        return data if isinstance(data, dict) else None

    parsed = _parse(raw)
    if parsed is not None:
        return parsed
    try:
        stripped = COMMENT_RE.sub("", raw.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    return _parse(stripped.encode("utf-8"))


def expand_home(value: str, home: str) -> str:
    if value == OWNER_HOME_PREFIX:
        return home
    if value.startswith(OWNER_HOME_PREFIX + "/"):
        return home + value[len(OWNER_HOME_PREFIX) :]
    return value


def normalize_value(value: Any, home: str) -> Any:
    if isinstance(value, str):
        return expand_home(value, home)
    if isinstance(value, list):
        return [normalize_value(v, home) for v in value]
    if isinstance(value, dict):
        return {k: normalize_value(v, home) for k, v in sorted(value.items())}
    return value


def plist_view(path: Path, home: str) -> dict[str, Any] | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    data = _load_plist_bytes(raw)
    if data is None:
        return None
    view: dict[str, Any] = {}
    for key in COMPARE_KEYS:
        if key in data:
            view[key] = normalize_value(data[key], home)
    return view


def plist_views_equal(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def sha256_file(path: Path) -> str | None:
    import hashlib

    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None
