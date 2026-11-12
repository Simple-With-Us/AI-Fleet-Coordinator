"""Read Sentry uptime monitors.

Only GET of the org uptime collection is implemented. Creating, editing, and
deleting monitors is out of scope for this tool.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from uptime_audit.errors import AuditError

DEFAULT_BASE = "https://us.sentry.io"
_MAX_PAGES = 20
_MAX_BYTES = 2_000_000
_ORG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_ACTIVE = {"0", "active", "ok", "enabled"}
_INACTIVE = {"1", "disabled", "muted", "inactive", "paused"}


@dataclass(frozen=True)
class Monitor:
    id: str
    name: str
    url: str
    status: str
    project: str
    interval_seconds: int | None
    covering: bool


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def uptime_collection_url(base: str, org: str) -> str:
    if not _ORG.fullmatch(org):
        raise AuditError("org slug is invalid")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise AuditError("Sentry API base must be https")
    root = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return f"{root}/api/0/organizations/{org}/uptime/"


def build_request(url: str, token: str) -> Request:
    if not url.startswith("https://") or not token:
        raise AuditError("refusing a Sentry request without https and a token")
    if token in url:
        raise AuditError("refusing a Sentry request that puts the token in the URL")
    # method is fixed. There is no argument that can turn this into a write.
    return Request(
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "fleet-uptime-audit",
        },
    )


def is_covering(item: dict) -> bool:
    """An active, unmuted monitor counts as coverage. Anything else does not."""
    if _flag(item, "muted", "isMuted", "is_muted"):
        return False
    enabled = item.get("enabled")
    if enabled is False:
        return False
    status = item.get("status")
    if isinstance(status, bool):
        return status
    if isinstance(status, int) and not isinstance(status, bool):
        return status == 0
    if status is None:
        return enabled is True
    text = str(status).strip().lower()
    if text in _INACTIVE:
        return False
    return text in _ACTIVE


def fetch_monitors(org: str, token: str, base: str = DEFAULT_BASE, opener=None) -> list[Monitor]:
    if not token or not token.strip() or any(char in token for char in "\r\n\x00"):
        raise AuditError("SENTRY_AUTH_TOKEN is not set")
    if opener is None:
        opener = build_opener(_NoRedirect)
    collection = uptime_collection_url(base, org)
    url = collection
    monitors: list[Monitor] = []
    seen: set[str] = set()
    for _ in range(_MAX_PAGES):
        if url in seen:
            raise AuditError("Sentry uptime list repeated a page")
        seen.add(url)
        payload, link = _get_json(opener, url, token)
        monitors.extend(parse_monitor_page(payload))
        url = _next_link(link, collection)
        if not url:
            return monitors
    raise AuditError("Sentry uptime list did not finish within 20 pages")


def parse_monitor_page(payload: object) -> list[Monitor]:
    rows = _rows(payload)
    monitors: list[Monitor] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AuditError("Sentry uptime list has a row that is not an object")
        url = _pick(row, "url", "uri")
        if not isinstance(url, str) or not url.strip():
            raise AuditError("Sentry uptime list has a monitor without a URL")
        interval = _pick(row, "intervalSeconds", "interval_seconds")
        interval_seconds = interval if isinstance(interval, int) else None
        status = _pick(row, "status")
        monitors.append(
            Monitor(
                id=str(_pick(row, "id") or ""),
                name=str(_pick(row, "name") or ""),
                url=url.strip(),
                status="" if status is None else str(status),
                project=str(_pick(row, "projectSlug", "project_slug") or ""),
                interval_seconds=interval_seconds,
                covering=is_covering(row),
            )
        )
    return monitors


def _rows(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("monitors", "results", "data"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    raise AuditError("Sentry uptime list was not a list of monitors")


def _pick(row: dict, *keys: str):
    for key in keys:
        if key in row and row[key] is not None:
            return row[key]
    return None


def _flag(row: dict, *keys: str) -> bool:
    for key in keys:
        if row.get(key) is True:
            return True
    return False


def _get_json(opener, url: str, token: str) -> tuple[object, str]:
    request = build_request(url, token)
    if request.get_method() != "GET":
        raise AuditError("refusing a Sentry request that is not GET")
    try:
        with opener.open(request, timeout=30) as response:
            status = getattr(response, "status", None)
            if status != 200:
                raise AuditError(f"Sentry uptime list returned HTTP {status}")
            body = response.read(_MAX_BYTES + 1)
            link = response.headers.get("Link", "")
    except AuditError:
        raise
    except HTTPError as exc:
        raise AuditError(f"Sentry uptime list returned HTTP {exc.code}") from None
    except URLError:
        raise AuditError("Sentry uptime list could not be reached") from None
    except TimeoutError:
        raise AuditError("Sentry uptime list timed out") from None
    if len(body) > _MAX_BYTES:
        raise AuditError("Sentry uptime list response was too large")
    try:
        return json.loads(body.decode("utf-8")), link
    except (UnicodeError, json.JSONDecodeError):
        raise AuditError("Sentry uptime list was not JSON") from None


def _next_link(header: str, collection: str) -> str:
    if not header:
        return ""
    for match in re.finditer(r"<([^>]+)>\s*;\s*([^,]+)", header):
        url, params = match.group(1), match.group(2)
        rel = ""
        results = ""
        for part in params.split(";"):
            piece = part.strip()
            if piece.startswith("rel="):
                rel = piece.split("=", 1)[1].strip('"')
            elif piece.startswith("results="):
                results = piece.split("=", 1)[1].strip('"')
        if rel == "next" and results != "false":
            # The bearer must not follow a link off the uptime collection.
            if not url.startswith(collection):
                raise AuditError("Sentry uptime list next page left the uptime collection")
            return url
    return ""
