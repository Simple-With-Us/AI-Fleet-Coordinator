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

import http.client
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
    "DEFAULT_USER_AGENT",
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
_API_BATCH_CACHE_KEY = "_api_observer_batch_cache"
DEFAULT_USER_AGENT = "fleet-config-api-collector/1.0 (+ops)"

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
    allow_private_ips: bool = False
    header_provider: Callable[[], Mapping[str, str]] | None = None
    batch_by_host: bool = False


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
    allow_loopback: bool,
    allow_private_ips: bool,
    raw_host: str,
) -> None:
    """Refuse link-local/metadata/multicast targets and unapproved loopback/private addresses."""
    if ip_obj.is_link_local or ip_obj.is_multicast or ip_obj.is_unspecified:
        raise CheckError(
            "the api observer refused link-local or metadata host %s" % raw_host,
            ReasonCode.ERROR,
        )
    if ip_obj.is_loopback and not (allow_loopback or allow_private_ips):
        raise CheckError(
            "the api observer refused unapproved loopback address %s" % raw_host,
            ReasonCode.ERROR,
        )
    if (
        (ip_obj.is_private or ip_obj.is_reserved)
        and not ip_obj.is_loopback
        and not allow_private_ips
    ):
        raise CheckError(
            "the api observer refused unapproved private address %s" % raw_host,
            ReasonCode.ERROR,
        )


def validate_observer_url(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    allow_insecure_loopback: bool = False,
    allow_private_ips: bool = False,
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
    if scheme == "https" or scheme == "http" and allow_insecure_loopback and host in _LOOPBACK_HOSTS:
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
        _check_ip_safety(
            ip_obj,
            allow_loopback=allow_insecure_loopback and host in _LOOPBACK_HOSTS,
            allow_private_ips=allow_private_ips,
            raw_host=host,
        )
    return url


def _verify_dns_targets(
    url: str,
    *,
    allowed_hosts: frozenset[str],
    allow_insecure_loopback: bool = False,
    allow_private_ips: bool = False,
) -> tuple[str, ...]:
    """Resolve and validate target IPs, returning deduplicated numeric IPs to pin connections."""
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return ()
    try:
        ip_literal = ipaddress.ip_address(host)
        return (str(ip_literal),)
    except ValueError:
        pass
    del allowed_hosts
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise CheckError(
            "the api observer cannot resolve host %s: %s" % (host, exc),
            ReasonCode.ERROR,
        ) from None
    verified_ips: list[str] = []
    seen_ips: set[str] = set()
    for family, _socktype, _proto, _canonname, sockaddr in records:
        if family not in (socket.AF_INET, socket.AF_INET6) or not sockaddr:
            continue
        addr_text = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(addr_text)
        except ValueError:
            continue
        _check_ip_safety(
            ip_obj,
            allow_loopback=allow_insecure_loopback and host in _LOOPBACK_HOSTS,
            allow_private_ips=allow_private_ips,
            raw_host=host,
        )
        normalized_ip = str(ip_obj)
        if normalized_ip not in seen_ips:
            seen_ips.add(normalized_ip)
            verified_ips.append(normalized_ip)
    if not verified_ips:
        raise CheckError(
            "the api observer resolved no usable addresses for host %s" % host,
            ReasonCode.ERROR,
        )
    return tuple(verified_ips)


def _connect_pinned_ip(
    ip_str: str,
    port: int,
    timeout: float | None,
    source_address: tuple[str, int] | None,
) -> socket.socket:
    """Open a TCP socket to one pre-verified IP literal without DNS resolution."""
    ip_obj = ipaddress.ip_address(ip_str)
    family = socket.AF_INET6 if ip_obj.version == 6 else socket.AF_INET
    sockaddr: tuple[Any, ...] = (ip_str, port, 0, 0) if ip_obj.version == 6 else (ip_str, port)
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        if timeout is not None:
            sock.settimeout(timeout)
        if source_address is not None:
            sock.bind(source_address)
        sock.connect(sockaddr)
        return sock
    except OSError:
        sock.close()
        raise


def _make_pinned_create_connection(
    pinned_host: str,
    pinned_ips: tuple[str, ...],
) -> Callable[..., socket.socket]:
    """Create a connection factory pinned to pre-verified IPs for one hostname."""

    def _pinned_create_connection(
        address: tuple[str, int],
        timeout: float | None = socket._GLOBAL_DEFAULT_TIMEOUT,  # type: ignore[assignment]
        source_address: tuple[str, int] | None = None,
    ) -> socket.socket:
        host, port = address
        normalized = (host or "").lower().rstrip(".")
        if normalized != pinned_host:
            raise OSError("refused unpinned host connection to %s" % host)
        actual_timeout = None if timeout is socket._GLOBAL_DEFAULT_TIMEOUT else timeout
        last_err: OSError | None = None
        for ip_str in pinned_ips:
            try:
                return _connect_pinned_ip(ip_str, int(port), actual_timeout, source_address)
            except OSError as exc:
                last_err = exc
        if last_err is not None:
            raise last_err
        raise OSError("no pinned addresses succeeded for %s" % host)

    return _pinned_create_connection


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """HTTP handler pinning TCP connections to validated IPs while preserving Host headers."""

    def __init__(self, pinned_host: str, pinned_ips: tuple[str, ...], debuglevel: int = 0) -> None:
        super().__init__(debuglevel=debuglevel)
        self._pinned_host = pinned_host
        self._pinned_ips = pinned_ips

    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        create_conn = _make_pinned_create_connection(self._pinned_host, self._pinned_ips)

        class _PinnedHTTPConnection(http.client.HTTPConnection):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self._create_connection = create_conn

        return self.do_open(_PinnedHTTPConnection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS handler pinning TCP connections to validated IPs while preserving TLS SNI."""

    def __init__(
        self,
        pinned_host: str,
        pinned_ips: tuple[str, ...],
        debuglevel: int = 0,
        context: Any = None,
    ) -> None:
        super().__init__(debuglevel=debuglevel, context=context)
        self._pinned_host = pinned_host
        self._pinned_ips = pinned_ips

    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        create_conn = _make_pinned_create_connection(self._pinned_host, self._pinned_ips)

        class _PinnedHTTPSConnection(http.client.HTTPSConnection):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                self._create_connection = create_conn

        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


def _build_safe_opener(
    opener: urllib.request.OpenerDirector | None,
    *,
    pinned_host: str,
    pinned_ips: tuple[str, ...],
) -> urllib.request.OpenerDirector:
    """Build a fresh OpenerDirector with redirects blocked and DNS rebinding prevented."""
    if opener is not None and type(opener).open is not urllib.request.OpenerDirector.open:
        return opener
    clean = urllib.request.OpenerDirector()
    ssl_context = None
    debuglevel = 0
    if opener is not None:
        for handler in getattr(opener, "handlers", ()):
            if isinstance(handler, urllib.request.HTTPRedirectHandler):
                continue
            if isinstance(handler, urllib.request.HTTPSHandler):
                ssl_context = getattr(handler, "_context", ssl_context)
                if pinned_ips:
                    continue
            elif isinstance(handler, urllib.request.HTTPHandler) and pinned_ips:
                debuglevel = getattr(handler, "_debuglevel", 0)
                continue
            clean.add_handler(handler)
    else:
        for default_cls in (
            urllib.request.ProxyHandler,
            urllib.request.UnknownHandler,
            urllib.request.HTTPDefaultErrorHandler,
            urllib.request.HTTPErrorProcessor,
        ):
            clean.add_handler(default_cls())
        if not pinned_ips:
            clean.add_handler(urllib.request.HTTPHandler())
            clean.add_handler(urllib.request.HTTPSHandler())
    if pinned_ips:
        clean.add_handler(_PinnedHTTPHandler(pinned_host, pinned_ips, debuglevel=debuglevel))
        clean.add_handler(_PinnedHTTPSHandler(pinned_host, pinned_ips, debuglevel=debuglevel, context=ssl_context))
    clean.add_handler(_NoRedirectHandler())
    return clean


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
    allow_private_ips: bool = False,
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
        allow_private_ips=allow_private_ips,
    )
    pinned_host = (urllib.parse.urlsplit(safe_url).hostname or "").lower().rstrip(".")
    pinned_ips: tuple[str, ...] = ()
    if resolve_dns:
        pinned_ips = _verify_dns_targets(
            safe_url,
            allowed_hosts=allowed_hosts,
            allow_insecure_loopback=allow_insecure_loopback,
            allow_private_ips=allow_private_ips,
        )
    limit = max(1, int(timeout))
    transport = _build_safe_opener(opener, pinned_host=pinned_host, pinned_ips=pinned_ips)
    request_headers = dict(headers)
    if not any(key.lower() == "user-agent" for key in request_headers):
        request_headers["User-Agent"] = DEFAULT_USER_AGENT
    attempts = 1 + max(0, int(max_retries))
    for attempt in range(attempts):
        request = urllib.request.Request(safe_url, headers=request_headers, method="GET")
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


def _reject_json_constant(token: str) -> Any:
    """Reject non-standard JSON constants (NaN/Infinity/-Infinity)."""
    raise ValueError("non-standard JSON constant %r is refused" % token)


def _parse_strict_json_bytes(raw: bytes, *, max_bytes: int) -> Any:
    """Parse bounded JSON while refusing non-finite numeric constants."""
    document = parse_bounded_json(
        raw,
        what="api observer",
        max_bytes=max_bytes,
        max_depth=MAX_API_JSON_DEPTH,
        max_nodes=MAX_API_JSON_NODES,
    )
    try:
        json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
    except ValueError as exc:
        raise CheckError("the api observer returned invalid JSON: %s" % exc, ReasonCode.UNPARSEABLE) from None
    return document


def _parse_utc_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 UTC timestamp string (``...Z`` or ``+00:00``)."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _valid_row_identity(row: Mapping[str, Any]) -> tuple[str, str, str, str] | None:
    """Extract validated ``(ruleId, seat, platform, hostId)`` or ``None`` if omitted/malformed.

    Fleet-wide compliance rules carry ``seat == ""`` intentionally; seat-scoped rules carry
    a non-empty seat string.  Missing keys or non-string identity fields fail closed.
    """
    rule_id = row.get("ruleId")
    seat = row.get("seat")
    platform_val = row.get("platform")
    host_id = row.get("hostId")
    if not all(isinstance(v, str) for v in (rule_id, seat, platform_val, host_id)):
        return None
    assert isinstance(rule_id, str)
    assert isinstance(seat, str)
    assert isinstance(platform_val, str)
    assert isinstance(host_id, str)
    if not (rule_id and platform_val and host_id):
        return None
    return (rule_id, seat, platform_val, host_id)


def build_evidence_sidecar(
    row: Mapping[str, Any],
    *,
    rule: Rule,
    platform: str,
    host_id: str,
    observer_id: str,
    observer_class: ObserverClass,
    observed_at: str,
    received_at: str,
    expires_at: str,
    manifest_digest: str = "",
    secret_canaries: Sequence[str] = (),
) -> EvidenceSidecar:
    """Build a scrubbed provenance sidecar record for one API observation."""
    scope_raw = row.get("scope")
    if isinstance(scope_raw, list) and all(isinstance(item, str) for item in scope_raw):
        scope = tuple(scrub_secrets(item, secret_canaries) for item in scope_raw)
    elif rule.scope:
        scope = tuple(scrub_secrets(item, secret_canaries) for item in rule.scope)
    else:
        scope = ("cloud",)
    return EvidenceSidecar(
        schema_version=API_OBSERVATION_SCHEMA_VERSION,
        observation_id=scrub_secrets(str(row.get("observationId") or ""), secret_canaries),
        rule_id=rule.id,
        seat=rule.seat,
        platform=platform,
        host_id=host_id,
        subject_session=scrub_secrets(str(row.get("subjectSession") or ""), secret_canaries),
        observer_id=scrub_secrets(observer_id, secret_canaries),
        observer_class=observer_class.value,
        source_revision=scrub_secrets(str(row.get("sourceRevision") or ""), secret_canaries),
        manifest_digest=scrub_secrets(
            str(manifest_digest or row.get("manifestDigest") or ""), secret_canaries
        ),
        observed_at=observed_at,
        received_at=received_at,
        expires_at=expires_at,
        scope=scope,
        evidence_ref=scrub_secrets(str(row.get("evidenceRef") or ""), secret_canaries),
    )


_SUBSET_NAME_CHECKS: frozenset[CheckType] = frozenset(
    {CheckType.SKILLS_INSTALLED, CheckType.SUBAGENTS_INSTALLED}
)


def _observed_from_row(obs_raw: Any, secret_canaries: Sequence[str]) -> ObservedState:
    """Extract safe ``ObservedState`` fields from a validated row without keeping secrets."""
    if not isinstance(obs_raw, Mapping):
        return ObservedState()
    present = obs_raw.get("present") if isinstance(obs_raw.get("present"), bool) else None
    sha256 = obs_raw.get("sha256")
    sha_val = scrub_secrets(sha256, secret_canaries) if isinstance(sha256, str) else None
    names_raw = obs_raw.get("names")
    names = (
        tuple(sorted(scrub_secrets(str(item), secret_canaries) for item in names_raw))
        if isinstance(names_raw, list)
        else ()
    )
    mode_raw = obs_raw.get("mode")
    mode = scrub_secrets(mode_raw, secret_canaries) if isinstance(mode_raw, str) else None
    size_raw = obs_raw.get("sizeBytes")
    size_val = size_raw if isinstance(size_raw, int) and not isinstance(size_raw, bool) else None
    mtime_raw = obs_raw.get("mtime")
    mtime_val = scrub_secrets(mtime_raw, secret_canaries) if isinstance(mtime_raw, str) else None
    timeout_raw = obs_raw.get("timeoutSeconds")
    timeout_val = timeout_raw if isinstance(timeout_raw, int) and not isinstance(timeout_raw, bool) else None
    return ObservedState(
        present=present,
        sha256=sha_val,
        names=names,
        mode=mode,
        size_bytes=size_val,
        mtime=mtime_val,
        timeout_seconds=timeout_val,
    )


def _matches_expected(rule: Rule, observed: ObservedState) -> tuple[bool, str]:
    """Compare observed cloud facts against the reviewed rule's ``ExpectedState``."""
    exp = rule.expected
    has_constraint = any(
        (
            exp.present is not None,
            exp.sha256 is not None,
            bool(exp.names),
            exp.mode is not None,
            exp.timeout_seconds is not None,
        )
    )
    if not has_constraint:
        return False, "the rule defines no verifiable expected constraints"
    if exp.present is True and observed.present is not True:
        return False, "the expected remote target is absent or unconfirmed"
    if exp.present is False and observed.present is not False:
        return False, "remote target absence could not be confirmed"
    if exp.sha256 is not None and observed.sha256 != exp.sha256:
        return False, "observed digest does not match expected manifest digest"
    if exp.names:
        expected_set = set(exp.names)
        observed_set = set(observed.names)
        if rule.check.type in _SUBSET_NAME_CHECKS:
            if not expected_set.issubset(observed_set):
                return False, "observed names are missing expected manifest entries"
        elif observed_set != expected_set:
            return False, "observed names do not match expected manifest set"
    if exp.mode is not None and observed.mode != exp.mode:
        return False, "observed mode %r does not match expected %r" % (observed.mode, exp.mode)
    if exp.timeout_seconds is not None and observed.timeout_seconds != exp.timeout_seconds:
        return False, "observed timeout does not match expected %d" % exp.timeout_seconds
    return True, "the remote observation matches the reviewed manifest"


def _evaluate_proof(rule: Rule, row: Mapping[str, Any], secret_canaries: Sequence[str]) -> tuple[CheckState, ReasonCode, ProofLevel | None, str]:
    """Evaluate L1/L2 proof status on a matched, trusted observer row."""
    if rule.proof_level is ProofLevel.L0:
        return CheckState.PASS, ReasonCode.OK, ProofLevel.L0, "L0 observation matches the manifest"
    proof_raw = row.get("proof")
    if not isinstance(proof_raw, Mapping):
        return (
            CheckState.UNPROVEN,
            ReasonCode.PROOF_NOT_RUN,
            ProofLevel.L0,
            "L0 matches, but required %s proof receipt is missing" % rule.proof_level.value,
        )
    status = str(proof_raw.get("status") or "").lower()
    if status == "failed":
        detail = scrub_secrets(str(proof_raw.get("reason") or "remote sentinel check did not deny"), secret_canaries)
        return CheckState.DRIFT, ReasonCode.PROOF_FAILED, ProofLevel.L0, detail
    if status == "timeout":
        return (
            CheckState.UNKNOWN,
            ReasonCode.TIMEOUT,
            ProofLevel.L0,
            "the remote proof check timed out",
        )
    if status == "error":
        return (
            CheckState.UNKNOWN,
            ReasonCode.ERROR,
            ProofLevel.L0,
            "the remote proof check errored",
        )
    achieved_raw = proof_raw.get("level")
    try:
        achieved = ProofLevel(str(achieved_raw))
    except ValueError:
        return (
            CheckState.UNPROVEN,
            ReasonCode.PROOF_NOT_RUN,
            ProofLevel.L0,
            "required %s proof was not run" % rule.proof_level.value,
        )
    if status != "passed" or not achieved.at_least(rule.proof_level):
        return (
            CheckState.UNPROVEN,
            ReasonCode.PROOF_NOT_RUN,
            achieved,
            "required %s proof was not achieved" % rule.proof_level.value,
        )
    return CheckState.PASS, ReasonCode.OK, achieved, "remote observation and proof match"


def evaluate_api_observation(
    rule: Rule,
    platform: str,
    payload_bytes: bytes,
    *,
    expected_nonce: str,
    expected_host_id: str,
    ctx: CheckContext,
    max_bytes: int = MAX_API_RESPONSE_BYTES,
    max_rows: int = MAX_API_ROWS,
    manifest_digest: str = "",
    secret_canaries: Sequence[str] = (),
) -> tuple[CheckResult, EvidenceSidecar | None]:
    """Validate an observer payload against the AFC-441 trust boundary and return a result + sidecar."""
    try:
        doc = _parse_strict_json_bytes(payload_bytes, max_bytes=max_bytes)
    except CheckError as exc:
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                scrub_secrets(str(exc), secret_canaries),
                exc.reason_code,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )
    if not isinstance(doc, Mapping):
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "the api observer response is not a JSON object",
                ReasonCode.UNPARSEABLE,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )
    schema_ver = doc.get("schemaVersion")
    if isinstance(schema_ver, bool) or schema_ver != API_OBSERVATION_SCHEMA_VERSION:
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "unsupported api observation schemaVersion %s"
                % scrub_secrets(repr(schema_ver), secret_canaries),
                ReasonCode.UNPARSEABLE,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )
    if not expected_nonce or doc.get("nonce") != expected_nonce:
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "the api observer nonce is missing or does not match (possible replay)",
                ReasonCode.ERROR,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )
    rows = doc.get("rows")
    if not isinstance(rows, list):
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "the api observer response carries no rows list",
                ReasonCode.UNPARSEABLE,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )
    if len(rows) > max_rows:
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "the api observer returned more than %d rows" % max_rows,
                ReasonCode.UNPARSEABLE,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )
    seen_keys: set[tuple[str, str, str, str]] = set()
    matched: Mapping[str, Any] | None = None
    target_key = (rule.id, rule.seat, platform, expected_host_id)
    for item in rows:
        if not isinstance(item, Mapping):
            return (
                build_result(
                    rule,
                    platform,
                    CheckState.UNKNOWN,
                    "the api observer row is not a JSON object",
                    ReasonCode.UNPARSEABLE,
                    ObservedState(),
                    ctx,
                    collector=Collector.API,
                ),
                None,
            )
        key = _valid_row_identity(item)
        if key is None or (key[0] == rule.id and bool(rule.seat) and not key[1]):
            return (
                build_result(
                    rule,
                    platform,
                    CheckState.UNKNOWN,
                    "the api observer row omits required identity fields",
                    ReasonCode.UNPARSEABLE,
                    ObservedState(),
                    ctx,
                    collector=Collector.API,
                ),
                None,
            )
        if key in seen_keys:
            return (
                build_result(
                    rule,
                    platform,
                    CheckState.UNKNOWN,
                    "the api observer returned duplicate identity rows",
                    ReasonCode.UNPARSEABLE,
                    ObservedState(),
                    ctx,
                    collector=Collector.API,
                ),
                None,
            )
        seen_keys.add(key)
        if key == target_key:
            matched = item

    if matched is None:
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "no observation row matched requested seat/platform/host",
                ReasonCode.ERROR,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )

    observer_raw = str(matched.get("observerClass") or doc.get("observerClass") or "")
    try:
        observer_class = ObserverClass(observer_raw)
    except ValueError:
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "unknown observerClass %s is refused"
                % scrub_secrets(repr(observer_raw), secret_canaries),
                ReasonCode.ERROR,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )
    if observer_class not in TRUSTED_OBSERVER_CLASSES:
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "self_report evidence is refused: it cannot establish compliance PASS or L1/L2",
                ReasonCode.ERROR,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )

    observed_dt = _parse_utc_iso(matched.get("observedAt"))
    expires_dt = _parse_utc_iso(matched.get("expiresAt"))
    if observed_dt is None or expires_dt is None:
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "the api observation carries malformed observedAt or expiresAt",
                ReasonCode.UNPARSEABLE,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )
    now_utc = ctx.now if ctx.now.tzinfo is not None else ctx.now.replace(tzinfo=timezone.utc)
    if observed_dt > now_utc + timedelta(seconds=MAX_CLOCK_SKEW_SECONDS):
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "the api observation timestamp is implausibly in the future",
                ReasonCode.ERROR,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )
    if expires_dt <= observed_dt:
        return (
            build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                "the api observation expiresAt is not after observedAt",
                ReasonCode.UNPARSEABLE,
                ObservedState(),
                ctx,
                collector=Collector.API,
            ),
            None,
        )

    local_cap = observed_dt + timedelta(seconds=max(1, int(rule.ttl_seconds)))
    effective_expires = min(expires_dt, local_cap)
    observed_iso = iso_utc(observed_dt)
    expires_iso = iso_utc(effective_expires)
    observer_id = str(matched.get("observerId") or doc.get("observerId") or "unknown-observer")
    sidecar = build_evidence_sidecar(
        matched,
        rule=rule,
        platform=platform,
        host_id=expected_host_id,
        observer_id=observer_id,
        observer_class=observer_class,
        observed_at=observed_iso,
        received_at=iso_utc(now_utc),
        expires_at=expires_iso,
        manifest_digest=manifest_digest,
        secret_canaries=secret_canaries,
    )
    obs_raw = matched.get("observed")
    if not isinstance(obs_raw, Mapping):
        return (
            replace(
                build_result(
                    rule,
                    platform,
                    CheckState.UNKNOWN,
                    "the api observation is missing a valid observed mapping",
                    ReasonCode.UNPARSEABLE,
                    ObservedState(),
                    ctx,
                    collector=Collector.API,
                ),
                checked_at=observed_iso,
                expires_at=expires_iso,
            ),
            sidecar,
        )
    observed_state = _observed_from_row(obs_raw, secret_canaries)

    if effective_expires <= now_utc:
        stale_result = CheckResult(
            rule_id=rule.id,
            seat=rule.seat,
            platform=platform,
            kind=rule.kind,
            state=CheckState.UNKNOWN,
            reason="the api observation expired before this run",
            reason_code=ReasonCode.STALE,
            expected=rule.expected,
            observed=observed_state,
            proof_level_required=rule.proof_level,
            proof_level_achieved=None,
            collector=Collector.API,
            check_type=rule.check.type,
            checked_at=observed_iso,
            expires_at=expires_iso,
            ttl_seconds=rule.ttl_seconds,
            stale=True,
        )
        return stale_result, sidecar

    reported_state_raw = matched.get("state")
    if reported_state_raw is not None:
        if str(reported_state_raw) not in {state.value for state in CHECK_STATES}:
            return (
                build_result(
                    rule,
                    platform,
                    CheckState.UNKNOWN,
                    "the api observation carries an unknown state value",
                    ReasonCode.UNPARSEABLE,
                    observed_state,
                    ctx,
                    collector=Collector.API,
                ),
                sidecar,
            )

    if rule.expected.present is True and observed_state.present is False:
        return (
            replace(
                build_result(
                    rule,
                    platform,
                    CheckState.MISSING,
                    "the expected remote target was authoritatively reported absent",
                    ReasonCode.MISSING,
                    observed_state,
                    ctx,
                    collector=Collector.API,
                ),
                checked_at=observed_iso,
                expires_at=expires_iso,
            ),
            sidecar,
        )

    matched_ok, match_reason = _matches_expected(rule, observed_state)
    if not matched_ok:
        return (
            replace(
                build_result(
                    rule,
                    platform,
                    CheckState.DRIFT,
                    scrub_secrets(match_reason, secret_canaries),
                    ReasonCode.DRIFT,
                    observed_state,
                    ctx,
                    collector=Collector.API,
                ),
                checked_at=observed_iso,
                expires_at=expires_iso,
            ),
            sidecar,
        )

    final_state, final_code, achieved, final_reason = _evaluate_proof(rule, matched, secret_canaries)
    return (
        replace(
            build_result(
                rule,
                platform,
                final_state,
                scrub_secrets(final_reason, secret_canaries),
                final_code,
                observed_state,
                ctx,
                proof_level_achieved=achieved,
                collector=Collector.API,
            ),
            checked_at=observed_iso,
            expires_at=expires_iso,
        ),
        sidecar,
    )


@dataclass
class _CachedHostBatch:
    """Per-run cache of validated multi-row host payloads or terminal host fetch failure."""

    entries: list[tuple[str, bytes, frozenset[tuple[str, str, str, str]]]] = field(
        default_factory=list
    )
    error: tuple[str, ReasonCode] | None = None


def _query_url(
    base_url: str,
    *,
    nonce: str,
    rule: Rule,
    platform: str,
    host_id: str,
    include_rule_id: bool = True,
) -> str:
    """Build the read-only GET query string for a single rule or host-batch fetch."""
    params: dict[str, str] = {
        "platform": platform,
        "hostId": host_id,
        "nonce": nonce,
    }
    if include_rule_id:
        params["ruleId"] = rule.id
        params["seat"] = rule.seat
    query = urllib.parse.urlencode(params)
    separator = "&" if "?" in base_url else "?"
    return base_url + separator + query


def _index_batch_keys(
    raw: bytes,
    *,
    expected_nonce: str,
    max_bytes: int,
    max_rows: int,
) -> frozenset[tuple[str, str, str, str]]:
    """Index verified identity keys from a valid envelope so matching rules skip extra GETs."""
    try:
        doc = _parse_strict_json_bytes(raw, max_bytes=max_bytes)
    except CheckError:
        return frozenset()
    if not isinstance(doc, Mapping):
        return frozenset()
    schema_ver = doc.get("schemaVersion")
    if isinstance(schema_ver, bool) or schema_ver != API_OBSERVATION_SCHEMA_VERSION:
        return frozenset()
    if not expected_nonce or doc.get("nonce") != expected_nonce:
        return frozenset()
    rows = doc.get("rows")
    if not isinstance(rows, list) or len(rows) > max_rows:
        return frozenset()
    keys: set[tuple[str, str, str, str]] = set()
    for item in rows:
        if not isinstance(item, Mapping):
            return frozenset()
        key = _valid_row_identity(item)
        if key is None or key in keys:
            return frozenset()
        keys.add(key)
    return frozenset(keys)


def _fetch_runtime_payload(
    runtime: ApiObserverRuntime,
    rule: Rule,
    platform: str,
    timeout: int,
    *,
    include_rule_id: bool,
) -> tuple[str, bytes]:
    """Perform one read-only observer GET for a rule or host batch."""
    target = runtime.target
    nonce = runtime.nonce_factory() if runtime.nonce_factory is not None else uuid.uuid4().hex
    headers = dict(target.header_provider()) if target.header_provider is not None else {}
    request_url = validate_observer_url(
        _query_url(
            target.base_url,
            nonce=nonce,
            rule=rule,
            platform=platform,
            host_id=target.expected_host_id,
            include_rule_id=include_rule_id,
        ),
        allowed_hosts=target.allowed_hosts,
        allow_insecure_loopback=target.allow_insecure_loopback,
        allow_private_ips=target.allow_private_ips,
    )
    if runtime.fetcher is not None:
        raw = runtime.fetcher(request_url, headers, timeout, target.max_bytes)
    else:
        raw = http_fetch_observer(
            request_url,
            headers,
            timeout,
            target.max_bytes,
            allowed_hosts=target.allowed_hosts,
            allow_insecure_loopback=target.allow_insecure_loopback,
            allow_private_ips=target.allow_private_ips,
            resolve_dns=runtime.resolve_dns,
        )
    return nonce, raw


def api_collect(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Execute a rule via an explicitly configured read-only ``api`` observer runtime.

    When no observer runtime is configured in ``ctx.cache[API_OBSERVER_CACHE_KEY]``,
    return ``UNSUPPORTED`` honestly instead of inventing compliance outcomes.

    Multi-row payloads returned for ``(host_id, platform)`` and terminal host transport
    failures are cached in ``ctx.cache`` for the duration of the run so N rules against
    one host do not exhaust ``RUN_DEADLINE_SECONDS`` with redundant sequential GETs.
    """
    runtime = ctx.cache.get(API_OBSERVER_CACHE_KEY)
    if not isinstance(runtime, ApiObserverRuntime):
        return build_result(
            rule,
            platform,
            CheckState.UNSUPPORTED,
            "the api collector has no configured observer endpoint (AFC-398), so this rule is not checked here",
            ReasonCode.UNSUPPORTED,
            ObservedState(),
            ctx,
            collector=Collector.API,
        )

    target = runtime.target
    target_key = (rule.id, rule.seat, platform, target.expected_host_id)
    run_cache = ctx.cache.setdefault(_API_BATCH_CACHE_KEY, {})
    batch_key = (
        id(runtime),
        target.service_id,
        target.base_url,
        platform,
        target.expected_host_id,
    )
    batch: _CachedHostBatch | None = None
    if isinstance(run_cache, dict):
        cached = run_cache.get(batch_key)
        if isinstance(cached, _CachedHostBatch):
            batch = cached
        else:
            batch = _CachedHostBatch()
            run_cache[batch_key] = batch

    if batch is not None and batch.error is not None:
        err_msg, err_code = batch.error
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            err_msg,
            err_code,
            ObservedState(),
            ctx,
            collector=Collector.API,
        )

    nonce: str | None = None
    raw: bytes | None = None
    if batch is not None:
        for cached_nonce, cached_raw, keys in batch.entries:
            if target_key in keys:
                nonce, raw = cached_nonce, cached_raw
                break
        if raw is None and target.batch_by_host and batch.entries:
            nonce, raw, _ = batch.entries[0]

    if raw is None or nonce is None:
        timeout = max(1, min(int(ctx.timeout), int(target.timeout_seconds)))
        try:
            nonce, raw = _fetch_runtime_payload(
                runtime,
                rule,
                platform,
                timeout,
                include_rule_id=not target.batch_by_host,
            )
        except CheckError as exc:
            msg = scrub_secrets(str(exc), runtime.secret_canaries)
            if batch is not None:
                batch.error = (msg, exc.reason_code)
            return build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                msg,
                exc.reason_code,
                ObservedState(),
                ctx,
                collector=Collector.API,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed on unexpected transport error
            msg = scrub_secrets("the api observer transport failed: %s" % exc, runtime.secret_canaries)
            if batch is not None:
                batch.error = (msg, ReasonCode.ERROR)
            return build_result(
                rule,
                platform,
                CheckState.UNKNOWN,
                msg,
                ReasonCode.ERROR,
                ObservedState(),
                ctx,
                collector=Collector.API,
            )
        if batch is not None:
            keys = _index_batch_keys(
                raw,
                expected_nonce=nonce,
                max_bytes=target.max_bytes,
                max_rows=target.max_rows,
            )
            batch.entries.append((nonce, raw, keys))

    result, sidecar = evaluate_api_observation(
        rule,
        platform,
        raw,
        expected_nonce=nonce,
        expected_host_id=target.expected_host_id,
        ctx=ctx,
        max_bytes=target.max_bytes,
        max_rows=target.max_rows,
        manifest_digest=runtime.manifest_digest,
        secret_canaries=runtime.secret_canaries,
    )
    if sidecar is not None:
        runtime.sidecar_sink.append(sidecar)
        sink = ctx.cache.setdefault(API_SIDECAR_CACHE_KEY, [])
        if isinstance(sink, list):
            sink.append(sidecar)
    return result


def build_first_cloud_seat_rules(
    *,
    seat: str,
    platform: str,
    host_id: str,
    owner_seat: str,
    rules_target: str,
    rules_sha256: str,
    hooks_target: str,
    hook_names: Sequence[str],
    mcp_target: str,
    mcp_names: Sequence[str],
    skills_target: str,
    skill_names: Sequence[str],
    subagents_target: str,
    subagent_names: Sequence[str],
    permissions_target: str,
    permission_names: Sequence[str],
    hook_proof_level: ProofLevel = ProofLevel.L0,
    ttl_seconds: int = 900,
    source: str = "AFC-441 Jet Oct 21 section 6",
) -> tuple[Rule, ...]:
    """Build the first six-kind cloud-seat rules for one registered cloud host/subject.

    All cloud rules carry ``collector=Collector.API`` and ``autofix_allowed=False``.
    Callers must supply real registered targets and digests; never invent endpoints.
    """
    prefix = "%s-%s" % (seat.lower(), host_id.lower())
    hooks_ttl = max(int(ttl_seconds), 86400) if hook_proof_level is ProofLevel.L2 else int(ttl_seconds)
    return (
        Rule(
            id="%s-instructions" % prefix,
            kind=ConfigKind.INSTRUCTIONS,
            platforms=(platform,),
            check=CheckSpec(
                type=CheckType.MARKED_BLOCK_HASH,
                target=rules_target,
                detail="fleet-lane-map",
            ),
            expected=ExpectedState(
                description="Approved public fleet instruction-block digest observed remotely.",
                present=True,
                sha256=rules_sha256,
            ),
            proof_level=ProofLevel.L0,
            owner_seat=owner_seat,
            autofix_allowed=False,
            collector=Collector.API,
            ttl_seconds=ttl_seconds,
            seat=seat,
            source=source,
        ),
        Rule(
            id="%s-hooks" % prefix,
            kind=ConfigKind.HOOKS,
            platforms=(platform,),
            check=CheckSpec(
                type=CheckType.HOOK_REGISTERED,
                target=hooks_target,
                detail="PreToolUse",
            ),
            expected=ExpectedState(
                description="Registered hook names and timeouts on cloud subject (L1 only with real sentinel).",
                present=True,
                names=tuple(hook_names),
            ),
            proof_level=hook_proof_level,
            owner_seat=owner_seat,
            autofix_allowed=False,
            collector=Collector.API,
            ttl_seconds=hooks_ttl,
            seat=seat,
            source=source,
        ),
        Rule(
            id="%s-mcp" % prefix,
            kind=ConfigKind.MCP,
            platforms=(platform,),
            check=CheckSpec(
                type=CheckType.MCP_SERVERS_REGISTERED,
                target=mcp_target,
            ),
            expected=ExpectedState(
                description="Allowlisted configured MCP server names observed on cloud host.",
                names=tuple(mcp_names),
            ),
            proof_level=ProofLevel.L0,
            owner_seat=owner_seat,
            autofix_allowed=False,
            collector=Collector.API,
            ttl_seconds=ttl_seconds,
            seat=seat,
            source=source,
        ),
        Rule(
            id="%s-skills" % prefix,
            kind=ConfigKind.SKILLS,
            platforms=(platform,),
            check=CheckSpec(
                type=CheckType.SKILLS_INSTALLED,
                target=skills_target,
            ),
            expected=ExpectedState(
                description="Expected authorized fleet skill IDs present on cloud host.",
                names=tuple(skill_names),
            ),
            proof_level=ProofLevel.L0,
            owner_seat=owner_seat,
            autofix_allowed=False,
            collector=Collector.API,
            ttl_seconds=ttl_seconds,
            seat=seat,
            source=source,
        ),
        Rule(
            id="%s-subagents" % prefix,
            kind=ConfigKind.SUBAGENTS,
            platforms=(platform,),
            check=CheckSpec(
                type=CheckType.SUBAGENTS_INSTALLED,
                target=subagents_target,
            ),
            expected=ExpectedState(
                description="Expected subagent definition IDs present on cloud host.",
                names=tuple(subagent_names),
            ),
            proof_level=ProofLevel.L0,
            owner_seat=owner_seat,
            autofix_allowed=False,
            collector=Collector.API,
            ttl_seconds=ttl_seconds,
            seat=seat,
            source=source,
        ),
        Rule(
            id="%s-permissions" % prefix,
            kind=ConfigKind.PERMISSIONS,
            platforms=(platform,),
            check=CheckSpec(
                type=CheckType.PERMISSIONS_MATCH,
                target=permissions_target,
            ),
            expected=ExpectedState(
                description="Exact approved permission identifiers (credential stat/key-name only).",
                names=tuple(permission_names),
            ),
            proof_level=ProofLevel.L0,
            owner_seat=owner_seat,
            autofix_allowed=False,
            collector=Collector.API,
            ttl_seconds=ttl_seconds,
            seat=seat,
            source=source,
        ),
    )
