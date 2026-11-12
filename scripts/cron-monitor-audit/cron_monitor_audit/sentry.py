"""Read Sentry cron monitors.

Only GET of the org monitors collection is implemented. Creating, editing,
and deleting monitors is out of scope for this tool.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from cron_monitor_audit.errors import AuditError, SentryForbidden

DEFAULT_BASE = "https://us.sentry.io"
_MAX_PAGES = 20
_MAX_BYTES = 2_000_000
_ORG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_DISABLED = {"disabled", "pending_deletion", "deletion_in_progress", "1"}
_FAILING = {"error", "missed_checkin", "missed", "timeout", "timed_out"}
_HEALTHY = {"ok", "in_progress"}


@dataclass(frozen=True)
class Monitor:
    slug: str
    name: str
    status: str
    project: str
    muted: bool
    last_check_in: str
    check_status: str
    health: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def monitors_collection_url(base: str, org: str) -> str:
    if not _ORG.fullmatch(org):
        raise AuditError("org slug is invalid")
    parts = urlsplit(base)
    if parts.scheme != "https" or not parts.hostname:
        raise AuditError("Sentry API base must be https")
    root = urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    return f"{root}/api/0/organizations/{org}/monitors/"


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
            "User-Agent": "fleet-cron-monitor-audit",
        },
    )


def fetch_monitors(org: str, token: str, base: str = DEFAULT_BASE, opener=None) -> list[Monitor]:
    if not token or not token.strip() or any(char in token for char in "\r\n\x00"):
        raise AuditError("SENTRY_AUTH_TOKEN_FULLSCOPE is not set")
    if opener is None:
        opener = build_opener(_NoRedirect)
    collection = monitors_collection_url(base, org)
    url = f"{collection}?per_page=100"
    monitors: list[Monitor] = []
    seen: set[str] = set()
    for _ in range(_MAX_PAGES):
        if url in seen:
            raise AuditError("Sentry monitor list repeated a page")
        seen.add(url)
        payload, link = _get_json(opener, url, token)
        monitors.extend(parse_monitor_page(payload))
        url = _next_link(link, collection)
        if not url:
            return monitors
    raise AuditError("Sentry monitor list did not finish within 20 pages")


def parse_monitor_page(payload: object) -> list[Monitor]:
    rows = _rows(payload)
    monitors: list[Monitor] = []
    for row in rows:
        if not isinstance(row, dict):
            raise AuditError("Sentry monitor list has a row that is not an object")
        slug = row.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            raise AuditError("Sentry monitor list has a monitor without a slug")
        status = row.get("status")
        check_status, last_check_in, health = _environment_health(row.get("environments"))
        muted = row.get("isMuted") is True
        if not muted:
            for env in row.get("environments") or []:
                if isinstance(env, dict) and env.get("isMuted") is True:
                    muted = True
                    break
        monitors.append(
            Monitor(
                slug=slug.strip(),
                name=str(row.get("name") or slug),
                status="" if status is None else str(status).strip().lower(),
                project=_project(row.get("project")),
                muted=muted,
                last_check_in=last_check_in,
                check_status=check_status,
                health=health,
            )
        )
    return monitors


def is_disabled(monitor: Monitor) -> bool:
    return monitor.status in _DISABLED


def _environment_health(environments: object) -> tuple[str, str, str]:
    if environments is None:
        return "", "", "unknown"
    if not isinstance(environments, list):
        raise AuditError("Sentry monitor environments were not a list")
    statuses: list[str] = []
    last = ""
    for env in environments:
        if not isinstance(env, dict):
            raise AuditError("Sentry monitor environment was not an object")
        status = env.get("status")
        if status is not None and str(status).strip():
            statuses.append(str(status).strip().lower())
        stamp = env.get("lastCheckIn")
        if isinstance(stamp, str) and stamp > last:
            last = stamp
    failing = [item for item in statuses if item in _FAILING]
    if failing:
        return failing[0], last, "failing"
    if statuses and all(item in _HEALTHY for item in statuses):
        return statuses[0], last, "healthy"
    if not statuses:
        return "", last, "unknown"
    return statuses[0], last, "unknown"


def _project(value: object) -> str:
    if isinstance(value, dict):
        slug = value.get("slug")
        return str(slug) if slug else ""
    if isinstance(value, str):
        return value
    return ""


def _rows(payload: object) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("monitors", "results", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return nested
    raise AuditError("Sentry monitor list was not a list of monitors")


def _get_json(opener, url: str, token: str) -> tuple[object, str]:
    request = build_request(url, token)
    if request.get_method() != "GET":
        raise AuditError("refusing a Sentry request that is not GET")
    try:
        with opener.open(request, timeout=30) as response:
            status = getattr(response, "status", None)
            if status != 200:
                raise AuditError(f"Sentry monitor list returned HTTP {status}")
            body = response.read(_MAX_BYTES + 1)
            link = response.headers.get("Link", "")
    except AuditError:
        raise
    except HTTPError as exc:
        if exc.code == 403:
            raise SentryForbidden(
                "Sentry monitor list returned HTTP 403. "
                "SENTRY_AUTH_TOKEN_FULLSCOPE with alerts:read or org:read is required. "
                "SENTRY_AUTH_TOKEN is not accepted on this endpoint."
            ) from None
        raise AuditError(f"Sentry monitor list returned HTTP {exc.code}") from None
    except URLError:
        raise AuditError("Sentry monitor list could not be reached") from None
    except TimeoutError:
        raise AuditError("Sentry monitor list timed out") from None
    if len(body) > _MAX_BYTES:
        raise AuditError("Sentry monitor list response was too large")
    try:
        return json.loads(body.decode("utf-8")), link
    except (UnicodeError, json.JSONDecodeError):
        raise AuditError("Sentry monitor list was not JSON") from None


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
            if not url.startswith(collection):
                raise AuditError("Sentry monitor list next page left the monitors collection")
            return url
    return ""
