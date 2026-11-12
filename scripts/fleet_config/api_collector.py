"""Remote read-only ``api`` collector and trust boundary for ``fleet_config``.

Phase 2 left ``Collector.API`` returning ``UNSUPPORTED`` until a trust boundary and
failure mapping were specified (``AFC-441``).  This module defines:

* The read-only observer transport contract with pinned destinations, disabled HTTP
  redirects, bounded reads, single bounded transient retry, and zero retries on auth
  or 404 failures.
* Strict observation validation:  schema version pinning, request nonce check, bounded
  clock skew and expiry, duplicate-identity refusal, secret canary redaction, and
  unconditional refusal of ``self_report`` evidence as compliance ``PASS`` or L1/L2.
* A versioned ``EvidenceSidecar`` provenance record keyed by observation and result
  identity, keeping ``contract.SCHEMA_VERSION == 1`` unchanged.
* Explicit six-kind cloud-seat rule templates (``build_first_cloud_seat_rules``) for
  registered subjects without inventing unverified platform endpoints or hashes.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

import ipaddress
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .checks import (
    CheckContext,
    CheckError,
    build_result,
    iso_utc,
    parse_bounded_json,
)
from .types import (
    CHECK_STATES,
    CheckResult,
    CheckSpec,
    CheckState,
    CheckType,
    Collector,
    ConfigKind,
    ExpectedState,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
)

__all__ = [
    "API_OBSERVATION_SCHEMA_VERSION",
    "API_OBSERVER_CACHE_KEY",
    "API_SIDECAR_CACHE_KEY",
    "DEFAULT_MAX_RETRIES",
    "MAX_API_JSON_DEPTH",
    "MAX_API_JSON_NODES",
    "MAX_API_RESPONSE_BYTES",
    "MAX_API_ROWS",
    "MAX_CLOCK_SKEW_SECONDS",
    "TRANSIENT_HTTP_STATUSES",
    "TRUSTED_OBSERVER_CLASSES",
    "ApiObserverRuntime",
    "ApiObserverTarget",
    "EvidenceSidecar",
    "ObserverClass",
    "api_collect",
    "build_evidence_sidecar",
    "build_first_cloud_seat_rules",
    "evaluate_api_observation",
    "http_fetch_observer",
    "scrub_secrets",
    "validate_observer_url",
]

API_OBSERVATION_SCHEMA_VERSION = 1
API_OBSERVER_CACHE_KEY = "api_observer"
API_SIDECAR_CACHE_KEY = "api_evidence_sidecars"

MAX_API_RESPONSE_BYTES = 262_144
MAX_API_ROWS = 256
MAX_API_JSON_DEPTH = 16
MAX_API_JSON_NODES = 8192
MAX_CLOCK_SKEW_SECONDS = 300
DEFAULT_MAX_RETRIES = 1
MAX_DIAGNOSTIC_CHARS = 320

TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bear" r"er\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\b(?:s" r"k|gh" r"p|gh" r"o|gh" r"u|gh" r"s|gh" r"r|git" r"hub_pat|lin" r"_api|in" r"f)_[A-Za-z0-9_-]{8,}"),
    re.compile(
        r"(?i)\b(?:tok" r"en|sec" r"ret|pass" r"word|pass" r"wd|api_" r"key|api" r"key|auth" r"orization)\s*[:=]\s*[^\s,;\"']+"
    ),
    re.compile(
        ("-" * 5)
        + "BE" "GIN [A-Z0-9 ]*PRI" "VATE KEY"
        + ("-" * 5)
        + r"[\s\S]*?"
        + ("-" * 5)
        + "E" "ND [A-Z0-9 ]*PRI" "VATE KEY"
        + ("-" * 5)
    ),
)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


class ObserverClass(str, Enum):
    """Who produced an API observation.

    Only owner-controlled observers and independent provider APIs can establish
    compliance.  A ``self_report`` can never prove ``PASS``, ``L0`` match, ``L1`` or ``L2``.
    """

    OWNER_OBSERVER = "owner_observer"
    PROVIDER_API = "provider_api"
    SELF_REPORT = "self_report"


TRUSTED_OBSERVER_CLASSES: frozenset[ObserverClass] = frozenset(
    {ObserverClass.OWNER_OBSERVER, ObserverClass.PROVIDER_API}
)


@dataclass(frozen=True)
class ApiObserverTarget:
    """Pinned read-only observer destination and limits for one host/service."""

    service_id: str
    base_url: str
    allowed_hosts: frozenset[str]
    expected_host_id: str
    max_bytes: int = MAX_API_RESPONSE_BYTES
    max_rows: int = MAX_API_ROWS
    timeout_seconds: int = 10
    allow_insecure_loopback: bool = False
    header_provider: Callable[[], Mapping[str, str]] | None = None


@dataclass(frozen=True)
class EvidenceSidecar:
    """Provenance sidecar for one API observation, keyed by observation and result identity.

    Kept separate from ``CheckResult`` v1 so ``contract.SCHEMA_VERSION == 1`` does not
    move before consumer support lands under ``AFC-416``.
    """

    schema_version: int
    observation_id: str
    rule_id: str
    seat: str
    platform: str
    host_id: str
    subject_session: str
    observer_id: str
    observer_class: str
    source_revision: str
    manifest_digest: str
    observed_at: str
    received_at: str
    expires_at: str
    scope: tuple[str, ...]
    evidence_ref: str

    def to_dict(self) -> dict[str, object]:
        """Render the sidecar as JSON-compatible dict data."""
        return {
            "schemaVersion": self.schema_version,
            "observationId": self.observation_id,
            "ruleId": self.rule_id,
            "seat": self.seat,
            "platform": self.platform,
            "hostId": self.host_id,
            "subjectSession": self.subject_session,
            "observerId": self.observer_id,
            "observerClass": self.observer_class,
            "sourceRevision": self.source_revision,
            "manifestDigest": self.manifest_digest,
            "observedAt": self.observed_at,
            "receivedAt": self.received_at,
            "expiresAt": self.expires_at,
            "scope": list(self.scope),
            "evidenceRef": self.evidence_ref,
        }


FetcherFn = Callable[[str, Mapping[str, str], int, int], bytes]


@dataclass
class ApiObserverRuntime:
    """Injected observer configuration/transport carried via ``CheckContext.cache``."""

    target: ApiObserverTarget
    fetcher: FetcherFn | None = None
    nonce_factory: Callable[[], str] | None = None
    sidecar_sink: list[EvidenceSidecar] = field(default_factory=list)
    secret_canaries: tuple[str, ...] = ()
    manifest_digest: str = ""
    resolve_dns: bool = True


def scrub_secrets(text: str, extra_secrets: Sequence[str] = ()) -> str:
    """Scrub credential-like tokens and explicit secret canaries from diagnostic text."""
    cleaned = text
    for secret in extra_secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    cleaned = " ".join(cleaned.splitlines()).strip()
    if len(cleaned) > MAX_DIAGNOSTIC_CHARS:
        return cleaned[: MAX_DIAGNOSTIC_CHARS - 3] + "..."
    return cleaned


def _check_ip_safety(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address,
    *,
    explicitly_allowed: bool,
    raw_host: str,
) -> None:
    """Refuse link-local/metadata/multicast targets and unapproved private addresses."""
    if ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
        raise CheckError(
            "the api observer refused link-local or metadata host %s" % raw_host,
            ReasonCode.ERROR,
        )
    if (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_reserved) and not explicitly_allowed:
        raise CheckError(
            "the api observer refused unapproved private address %s" % raw_host,
            ReasonCode.ERROR,
        )


def validate_observer_url(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    allow_insecure_loopback: bool = False,
) -> str:
    """Validate an observer URL against pinned scheme, host, and SSRF rules."""
    if not isinstance(url, str) or not url.strip():
        raise CheckError("the api observer url is empty", ReasonCode.ERROR)
    parsed = urllib.parse.urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment:
        raise CheckError("the api observer url carries forbidden credentials or fragment", ReasonCode.ERROR)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise CheckError("the api observer url names no host", ReasonCode.ERROR)
    normalized_allow = {item.lower().rstrip(".") for item in allowed_hosts if item.strip()}
    if host not in normalized_allow:
        raise CheckError(
            "the api observer host %s is not on the approved allowlist" % host,
            ReasonCode.ERROR,
        )
    if scheme == "https":
        pass
    elif scheme == "http" and allow_insecure_loopback and host in _LOOPBACK_HOSTS:
        pass
    else:
        raise CheckError(
            "the api observer refused insecure or unsupported scheme %s" % (scheme or "<empty>"),
            ReasonCode.ERROR,
        )
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None:
        _check_ip_safety(ip_obj, explicitly_allowed=host in normalized_allow, raw_host=host)
    return url


def _verify_dns_targets(url: str, *, allowed_hosts: frozenset[str]) -> None:
    """Ensure DNS resolution for a hostname does not target metadata or unapproved private IPs."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    normalized_allow = {item.lower().rstrip(".") for item in allowed_hosts if item.strip()}
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CheckError("the api observer cannot resolve host %s: %s" % (host, exc), ReasonCode.ERROR) from None
    for family, _socktype, _proto, _canonname, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6) or not sockaddr:
            continue
        addr_text = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(addr_text)
        except ValueError:
            continue
        _check_ip_safety(ip_obj, explicitly_allowed=host in normalized_allow, raw_host=host)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail closed on every HTTP redirect so observers cannot bounce to unapproved targets."""

    def redirect_request(  # noqa: PLR0913 - stdlib HTTPRedirectHandler signature
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise urllib.error.HTTPError(
            req.full_url,
            code,
            "redirects are refused by the api collector",
            headers,
            None,
        )


def _read_bounded_http(response: Any, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` from an HTTP response, rejecting oversized bodies."""
    cap = max(1, int(max_bytes))
    raw = response.read(cap + 1)
    if len(raw) > cap:
        raise CheckError(
            "the api observer answered with more than %d bytes" % cap,
            ReasonCode.UNPARSEABLE,
        )
    return raw


def http_fetch_observer(
    url: str,
    headers: Mapping[str, str],
    timeout: int,
    max_bytes: int,
    *,
    allowed_hosts: frozenset[str],
    allow_insecure_loopback: bool = False,
    max_retries: int = DEFAULT_MAX_RETRIES,
    opener: urllib.request.OpenerDirector | None = None,
    resolve_dns: bool = True,
) -> bytes:
    """Fetch an observer observation over read-only GET with bounded size and retries.

    Redirects are refused.  401/403/404 are never retried; 404 maps to ``ERROR`` (never
    ``MISSING``).  Transient 429/5xx or connection errors retry at most ``max_retries``
    times.  Error bodies are never echoed into failure diagnostics.
    """
    safe_url = validate_observer_url(
        url,
        allowed_hosts=allowed_hosts,
        allow_insecure_loopback=allow_insecure_loopback,
    )
    if resolve_dns:
        _verify_dns_targets(safe_url, allowed_hosts=allowed_hosts)
    limit = max(1, int(timeout))
    transport = opener if opener is not None else urllib.request.build_opener(_NoRedirectHandler())
    attempts = 1 + max(0, int(max_retries))
    for attempt in range(attempts):
        request = urllib.request.Request(safe_url, headers=dict(headers), method="GET")
        try:
            with transport.open(request, timeout=limit) as response:
                return _read_bounded_http(response, max_bytes)
        except CheckError:
            raise
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            try:
                exc.close()
            except Exception:  # noqa: BLE001 - best-effort cleanup of upstream error stream
                pass
            if status in TRANSIENT_HTTP_STATUSES and attempt + 1 < attempts:
                continue
            if 300 <= status < 400:
                raise CheckError(
                    "the api observer returned HTTP %d redirect, which is refused" % status,
                    ReasonCode.ERROR,
                ) from None
            raise CheckError(
                "the api observer returned HTTP %d (no error body retained)" % status,
                ReasonCode.ERROR,
            ) from None
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (TimeoutError, socket.timeout)):
                if attempt + 1 < attempts:
                    continue
                raise CheckError(
                    "the api observer timed out after %d seconds" % limit,
                    ReasonCode.TIMEOUT,
                ) from None
            if attempt + 1 < attempts and isinstance(reason, OSError):
                continue
            raise CheckError("the api observer request failed: network error", ReasonCode.ERROR) from None
        except (TimeoutError, socket.timeout):
            if attempt + 1 < attempts:
                continue
            raise CheckError(
                "the api observer timed out after %d seconds" % limit,
                ReasonCode.TIMEOUT,
            ) from None
    raise CheckError("the api observer exhausted retry budget", ReasonCode.ERROR)
