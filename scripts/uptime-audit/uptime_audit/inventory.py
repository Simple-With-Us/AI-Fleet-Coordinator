"""Load the reviewed host inventory.

The file is a small YAML subset: a root mapping, lists of flat mappings, and
scalars. Tags, anchors, and flow collections are rejected so the audit does
not take a YAML dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from uptime_audit.errors import AuditError

_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HOST = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_ROOT_KEYS = {"version", "org", "entries", "excluded"}
_ENTRY_KEYS = {"id", "owner", "host", "health_url", "certainty", "source", "note"}
_EXCLUDED_KEYS = {"host", "reason"}


@dataclass(frozen=True)
class ExpectedHost:
    id: str
    owner: str
    host: str
    health_url: str
    certainty: str
    source: str
    note: str

    @property
    def requires_coverage(self) -> bool:
        return self.certainty == "certain" and bool(self.health_url)


@dataclass(frozen=True)
class ExcludedHost:
    host: str
    reason: str


@dataclass(frozen=True)
class Inventory:
    version: int
    org: str
    entries: tuple[ExpectedHost, ...]
    excluded: tuple[ExcludedHost, ...]

    def host_set(self) -> set[str]:
        return {entry.host for entry in self.entries if entry.host}


def load_inventory(path: Path) -> Inventory:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuditError(f"could not read inventory {path}") from exc
    data = _parse(text)
    return _validate(data, path)


def _parse(text: str) -> dict:
    if "\t" in text:
        raise AuditError("inventory uses tabs; use spaces")
    if "\u00a0" in text:
        raise AuditError("inventory contains a no-break space")
    lines = text.splitlines()
    root: dict = {}
    index = 0
    while index < len(lines):
        line, index = _next_content(lines, index)
        if line is None:
            break
        indent, content = _indent(line, index)
        if indent != 0:
            raise AuditError(f"line {index}: expected a root key")
        key, raw_value = _split_key(content, index)
        if key in root:
            raise AuditError(f"line {index}: duplicate key {key}")
        if key not in _ROOT_KEYS:
            raise AuditError(f"line {index}: unknown key {key}")
        if raw_value is None:
            items, index = _parse_list(lines, index)
            root[key] = items
        else:
            root[key] = _scalar(raw_value, index)
    return root


def _parse_list(lines: list[str], index: int) -> tuple[list[dict], int]:
    items: list[dict] = []
    while index < len(lines):
        line, peeked = _next_content(lines, index)
        if line is None:
            return items, peeked
        indent, content = _indent(line, peeked)
        if indent == 0:
            return items, index
        if indent != 2 or not content.startswith("- "):
            raise AuditError(f"line {peeked}: expected a list item")
        index = peeked
        key, raw_value = _split_key(content[2:], index)
        if raw_value is None:
            raise AuditError(f"line {index}: list items are flat mappings")
        item = {key: _scalar(raw_value, index)}
        # `peeked` is the index of the next line. Continuation fields start there.
        cursor = peeked
        while cursor < len(lines):
            nxt, nxt_no = _next_content(lines, cursor)
            if nxt is None:
                cursor = nxt_no
                break
            nxt_indent, nxt_content = _indent(nxt, nxt_no)
            if nxt_indent == 0 or nxt_content.startswith("- "):
                break
            if nxt_indent != 4:
                raise AuditError(f"line {nxt_no}: expected a mapping field")
            field, field_raw = _split_key(nxt_content, nxt_no)
            if field_raw is None:
                raise AuditError(f"line {nxt_no}: nested values are not allowed")
            if field in item:
                raise AuditError(f"line {nxt_no}: duplicate key {field}")
            item[field] = _scalar(field_raw, nxt_no)
            cursor = nxt_no
        items.append(item)
        index = cursor
    return items, index


def _next_content(lines: list[str], start: int) -> tuple[str | None, int]:
    """Return the next non-comment line and its 1-based line number.

    The returned number is also the zero-based index of that line. Callers
    that continue after the line start at that number, which is the next
    index.
    """
    index = start
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        index += 1
        if not stripped or stripped.startswith("#"):
            continue
        return raw, index
    return None, index


def _indent(line: str, number: int) -> tuple[int, str]:
    if "&" in line or "!" in line or "*" in line:
        raise AuditError(f"line {number}: anchors, aliases, and tags are not allowed")
    spaces = len(line) - len(line.lstrip(" "))
    return spaces, line[spaces:]


def _split_key(content: str, number: int) -> tuple[str, str | None]:
    if ":" not in content:
        raise AuditError(f"line {number}: expected key: value")
    key, rest = content.split(":", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", key):
        raise AuditError(f"line {number}: invalid key")
    rest = rest.strip()
    if rest == "":
        return key, None
    if rest.startswith("#"):
        return key, None
    return key, rest


def _scalar(raw: str, number: int):
    if raw.startswith(("'", '"')):
        return _quoted(raw, number)
    if raw in {"null", "~"}:
        return None
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if any(mark in raw for mark in "[]{}"):
        raise AuditError(f"line {number}: flow collections are not allowed")
    if " #" in raw:
        raw = raw.split(" #", 1)[0].rstrip()
    return raw


def _quoted(raw: str, number: int) -> str:
    quote = raw[0]
    if len(raw) < 2 or not raw.endswith(quote):
        raise AuditError(f"line {number}: unterminated string")
    body = raw[1:-1]
    if quote == "'":
        return body.replace("''", "'")
    out = []
    escaped = False
    for char in body:
        if escaped:
            if char == "n":
                out.append("\n")
            elif char in {'"', "\\"}:
                out.append(char)
            else:
                raise AuditError(f"line {number}: unsupported escape")
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    if escaped:
        raise AuditError(f"line {number}: unterminated string")
    return "".join(out)


def _validate(data: dict, path: Path) -> Inventory:
    version = data.get("version")
    org = data.get("org")
    if version != 1:
        raise AuditError(f"{path}: version must be 1")
    if not isinstance(org, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", org):
        raise AuditError(f"{path}: org must be a Sentry org slug")
    entries_raw = data.get("entries")
    excluded_raw = data.get("excluded")
    if not isinstance(entries_raw, list) or not entries_raw:
        raise AuditError(f"{path}: entries must be a non-empty list")
    if not isinstance(excluded_raw, list):
        raise AuditError(f"{path}: excluded must be a list")
    entries = tuple(_entry(item, path) for item in entries_raw)
    excluded = tuple(_excluded(item, path) for item in excluded_raw)
    ids = [entry.id for entry in entries]
    if len(ids) != len(set(ids)):
        raise AuditError(f"{path}: duplicate entry id")
    hosts = {entry.host for entry in entries if entry.host}
    excluded_hosts = [item.host for item in excluded]
    if len(excluded_hosts) != len(set(excluded_hosts)):
        raise AuditError(f"{path}: duplicate excluded host")
    overlap = hosts & set(excluded_hosts)
    if overlap:
        raise AuditError(f"{path}: host is both expected and excluded: {sorted(overlap)[0]}")
    urls = [entry.health_url for entry in entries if entry.health_url]
    if len(urls) != len(set(urls)):
        raise AuditError(f"{path}: duplicate health URL")
    return Inventory(version=1, org=org, entries=entries, excluded=excluded)


def _entry(item: object, path: Path) -> ExpectedHost:
    if not isinstance(item, dict):
        raise AuditError(f"{path}: an entry must be a mapping")
    unknown = set(item) - _ENTRY_KEYS
    if unknown:
        raise AuditError(f"{path}: unknown entry field {sorted(unknown)[0]}")
    entry_id = _text(item.get("id"), path, "id")
    owner = _text(item.get("owner"), path, "owner")
    source = _text(item.get("source"), path, "source")
    certainty = _text(item.get("certainty"), path, "certainty")
    note = item.get("note") or ""
    host = item.get("host") or ""
    health_url = item.get("health_url") or ""
    if not isinstance(note, str) or not isinstance(host, str) or not isinstance(health_url, str):
        raise AuditError(f"{path}: entry {entry_id} has a non-text field")
    if not _ID.fullmatch(entry_id):
        raise AuditError(f"{path}: invalid id {entry_id}")
    if certainty not in {"certain", "uncertain"}:
        raise AuditError(f"{path}: {entry_id} certainty must be certain or uncertain")
    if host and not _HOST.fullmatch(host):
        raise AuditError(f"{path}: {entry_id} has an invalid host")
    if health_url:
        _check_health_url(health_url, host, entry_id, path)
    if certainty == "certain" and (not host or not health_url):
        raise AuditError(f"{path}: certain entry {entry_id} needs a host and a health URL")
    return ExpectedHost(
        id=entry_id,
        owner=owner,
        host=host,
        health_url=health_url,
        certainty=certainty,
        source=source,
        note=note,
    )


def _excluded(item: object, path: Path) -> ExcludedHost:
    if not isinstance(item, dict):
        raise AuditError(f"{path}: an excluded row must be a mapping")
    unknown = set(item) - _EXCLUDED_KEYS
    if unknown:
        raise AuditError(f"{path}: unknown excluded field {sorted(unknown)[0]}")
    host = _text(item.get("host"), path, "excluded host")
    reason = _text(item.get("reason"), path, "excluded reason")
    if host not in {"localhost"} and not _HOST.fullmatch(host) and host != "127.0.0.1":
        raise AuditError(f"{path}: excluded host {host} is invalid")
    return ExcludedHost(host=host, reason=reason)


def _text(value: object, path: Path, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AuditError(f"{path}: missing {label}")
    return value.strip()


def _check_health_url(url: str, host: str, entry_id: str, path: Path) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise AuditError(f"{path}: {entry_id} health URL must be https")
    if parts.username or parts.password:
        raise AuditError(f"{path}: {entry_id} health URL must not carry credentials")
    if parts.hostname.lower().rstrip(".") != host:
        raise AuditError(f"{path}: {entry_id} health URL host does not match host")
