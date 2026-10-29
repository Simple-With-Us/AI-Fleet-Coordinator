"""The Operator's Linear project webhook (AFC-565):  Project create/update in, linkifiers out.

Linear posts here when a project is created or updated;  the hook verifies the
Linear-Signature, ignores anything that is not a Project create/update, and otherwise
runs one reconcile (scripts/linear_project_linkifiers.py) on a single background worker
thread:  events that arrive while a run is in progress cause exactly one more run
afterwards.  Every accepted or ignored event answers 200 at once (Linear treats any
non-200 as a failed delivery and retries).  Only project codes and slugs are logged,
never bodies or secrets.

Served by the listener API (listener_api.py) under /linear/v1/ when the daemon gives
it one ([linear_projects] enabled, with OPERATOR_LINKIFIERS_INFISICAL_TOKEN set).
Python 3.11+, standard library only.
"""

from __future__ import annotations

import inspect
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import linear_project_linkifiers as LPL
except ImportError:  # the daemon's sys.path holds scripts/, tests run with cwd=scripts/
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import linear_project_linkifiers as LPL

PREFIX = "/linear/v1/"
PATH = "/linear/v1/projects"
MAX_BODY = 256 * 1024
SIGNATURE_HEADER = "Linear-Signature"
ACTIONS = frozenset({"create", "update"})


def _header(headers: Mapping[str, str], name: str) -> str | None:
    try:
        value = headers.get(name)  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        value = None
    if value is not None:
        return value
    try:
        items = headers.items()  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        return None
    lowered = name.lower()
    for key, val in items:
        if str(key).lower() == lowered:
            return val
    return None


def _classify(exc: BaseException) -> str:
    """One failed reconcile as retryable, permanent or outcome_unknown, for the log and
    last_error only.  No auto-retry here:  the next webhook event re-runs the full
    idempotent reconcile, while an immediate requeue on a permanent error would loop."""
    try:
        text = str(exc).lower()
    except Exception:  # noqa: BLE001 - a bad __str__ must not break the worker
        text = ""
    if isinstance(exc, LPL.SecretsRefused):
        return "permanent"
    if "not configured" in text or "not_configured" in text or "missing" in text:
        return "permanent"
    padded = " " + text + " "
    if " 401 " in padded or " 403 " in padded or "http 401" in text or "http 403" in text:
        return "permanent"
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "retryable"
    if isinstance(exc, OSError) and not isinstance(exc, (FileNotFoundError, PermissionError)):
        return "retryable"
    for token in (
        "429",
        "500",
        "502",
        "503",
        "504",
        "timeout",
        "timed out",
        "connection",
        "network",
        "temporary",
        "try again",
        "rate limit",
        "rate limited",
    ):
        if token in text:
            return "retryable"
    return "outcome_unknown"


def _safe_detail(exc: BaseException, secrets: dict[str, str] | None) -> str:
    """One-line truncated failure detail with any cached secret value redacted."""
    try:
        detail = str(exc)
    except Exception:  # noqa: BLE001 - a bad __str__ must not break the worker
        return ""
    detail = " ".join(detail.split())[:200]
    if detail and secrets:
        for value in secrets.values():
            if isinstance(value, str) and len(value) >= 4 and value in detail:
                detail = detail.replace(value, "[redacted]")
    return detail


def _event_identity(payload: Mapping[str, Any]) -> tuple[str | None, str | None]:
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    code = data.get("identifier")
    slug = data.get("slugId")
    if not slug:
        slug = LPL.slug_from_url(data.get("url")) or LPL.slug_from_url(payload.get("url"))
    # Payload text is untrusted:  only a well-formed code or slug ever reaches a log line.
    code = code if isinstance(code, str) and LPL.CODE_RE.fullmatch(code) else None
    slug = slug if isinstance(slug, str) and LPL.SLUG_RE.fullmatch(slug) else None
    return code, slug


class LinearProjectsHook:
    """POST /linear/v1/projects.  `secrets_loader` is called once at construction
    and cached;  `runner` runs one reconcile (called with the cached secrets when
    it takes an argument, else with none)."""

    def __init__(
        self,
        secrets_loader: Callable[[], dict[str, str]],
        runner: Callable[..., Any],
        *,
        clock: Callable[[], float] = time.time,
        log: Callable[..., None] | None = None,
    ) -> None:
        self.clock = clock
        self.log = log or (lambda *_args, **_kwargs: None)
        self._runner = runner
        try:
            takes = any(
                p.kind
                in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.VAR_POSITIONAL,
                )
                for p in inspect.signature(runner).parameters.values()
            )
        except (TypeError, ValueError):
            takes = True
        self._runner_takes_secrets = takes
        try:
            self.secrets: dict[str, str] | None = dict(secrets_loader() or {})
            self._secrets_reason: str | None = None
        except LPL.SecretsRefused as exc:
            self.secrets = None
            self._secrets_reason = exc.reason
            self.log("linear-projects-secrets", reason=exc.reason)
        except Exception as exc:  # noqa: BLE001 - a loader failure must not stop the server
            self.secrets = None
            self._secrets_reason = "load_failed"
            self.log("linear-projects-secrets", reason=type(exc).__name__)
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._pending = False
        self._running = False
        self._last_error: dict[str, Any] | None = None
        self._thread = threading.Thread(target=self._work, name="linear-projects", daemon=True)
        self._thread.start()

    def _invoke(self) -> None:
        assert self.secrets is not None
        if self._runner_takes_secrets:
            self._runner(self.secrets)
        else:
            self._runner()

    def _work(self) -> None:
        while True:
            with self._wake:
                while not self._pending:
                    self._wake.wait()
                self._pending = False
                self._running = True
            try:
                self._invoke()
                with self._wake:
                    self._last_error = None
            except BaseException as exc:  # noqa: BLE001 - one run must never kill the worker
                # zulip_linkifiers.Zulip.call raises SystemExit on an HTTP error, so Exception
                # alone would end this thread and silently stop every later run.  The failure is
                # surfaced in the log and last_error;  the next event re-runs the full reconcile.
                outcome = _classify(exc)
                detail = _safe_detail(exc, self.secrets)
                self.log(
                    "linear-projects-error",
                    error=type(exc).__name__,
                    outcome=outcome,
                    detail=detail,
                )
                with self._wake:
                    self._last_error = {
                        "error": type(exc).__name__,
                        "outcome": outcome,
                        "detail": detail,
                        "at": self.clock(),
                    }
            finally:
                with self._wake:
                    self._running = False

    def _enqueue(self) -> None:
        with self._wake:
            self._pending = True  # coalesced:  at most one run waits behind the current one
            self._wake.notify()

    @property
    def queued(self) -> bool:
        with self._lock:
            return self._pending or self._running

    @property
    def last_error(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._last_error) if self._last_error is not None else None

    def handle(
        self, method: str, raw_path: str, headers: Mapping[str, str], body: bytes
    ) -> tuple[int, dict[str, Any] | None]:
        """(status, JSON body or None) for one request.  Pure apart from the worker."""
        if self.secrets is None:
            return 503, {"error": "not_configured"}
        if str(method).upper() != "POST":
            return 405, {"error": "method_not_allowed"}
        parts = urlsplit(raw_path)
        if parts.path != PATH or parts.fragment:
            return 404, {"error": "not_found"}
        if len(body or b"") > MAX_BODY:
            return 413, {"error": "too_large"}
        try:
            payload = LPL.verify_linear_webhook(
                body or b"",
                _header(headers, SIGNATURE_HEADER),
                self.secrets.get("LINEAR_WEBHOOK_SECRET", ""),
                int(self.clock() * 1000),
            )
        except LPL.WebhookRefused as exc:
            self.log("linear-projects-refused", reason=exc.reason)
            return exc.status, {"error": exc.reason}
        event = payload.get("type")
        action = payload.get("action")
        code, slug = _event_identity(payload)
        if event != "Project" or action not in ACTIONS:
            self.log(
                "linear-projects-ignored",
                linear_type=str(event)[:40],
                action=str(action)[:40],
                code=code,
                slug=slug,
            )
            return 200, {"ok": True, "ignored": True}
        self.log("linear-projects-queued", code=code, slug=slug, action=str(action)[:40])
        self._enqueue()
        return 200, {"ok": True, "queued": True}
