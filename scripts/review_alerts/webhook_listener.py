"""The Echo-owned GitHub webhook listener:  validate, durably record, ack.

This endpoint is ENQUEUE-ONLY.  It never reads the event store, never binds,
never calls the runner or the router:  the single review-alerts worker alone
executes what lands here.  Signature verification and event mapping are the
pure Wise module (scripts/review_alerts/webhook_intake.py, its own isolated
PR), injected here as callables so this file holds no Wise-owned source.

The HTTP ack means exactly one thing:  the delivery's targets are DURABLY
queued or held in the hint inbox (or were already).  It never means the hint
was read, reconciled, woken or delivered.  When the inbox cannot durably take
every target, the answer is a visible 503 refusal and NOTHING is
acknowledged;  GitHub does not automatically redeliver failed deliveries, so
coverage of owned-PR review targets comes from the canonical poll, not from
an assumed source retry.  Issue and issue-comment subjects stay durably HELD
until the reviewed consumer contract;  the poll does not cover them here.

Delivery ids are transport retry identity, deduped per target only after a
durable disposition exists.  Signature failures are answered without trusting
any payload byte.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .hint_inbox import HINT_KINDS_PR, InboxFull

MAX_BODY = 1024 * 1024
PATH = "/github-webhook"
#: The agreed delivery-id grammar (shared with the pure mapper):  retry
#: identity is strict and bounded, never truncated, never guessed.
DELIVERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
#: Advisory heads are accepted only as full 40-hex, else dropped.
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
#: The webhook event names that can carry repo-level discovery holds.
_DISCOVERY_KINDS = {"check_run", "check_suite", "merge_group"}


def _field(result, name, default=None):
    """MappingResult arrives as Wise's frozen dataclass;  a dict-shaped test
    double speaks the same contract.  Read both, never guess past missing."""
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def _hint_dict(hint) -> dict:
    """The Wise Hint dataclass -> the inbox's strict row shape.  Field-name
    translation (action -> source_action) happens at this seam only."""
    get = (lambda name, default=None: hint.get(name, default)) if isinstance(hint, dict) else (
        lambda name, default=None: getattr(hint, name, default)
    )
    head = get("head_sha")
    return {
        "subject": get("subject"),
        "repo": get("repo"),
        "number": get("number"),
        "hint_kind": get("hint_kind"),
        "source_event_id": get("source_event_id") or "",
        "source_time": get("source_time") or "",
        "source_ref": get("source_ref") or "",
        "source_action": get("action") or get("source_action") or "",
        "head_sha": head or "",
    }


def _envelope(payload: dict) -> dict:
    """Bounded evidence from the AUTHENTICATED payload, extracted by the
    listener itself:  a HELD MappingResult omits time/ref/action/subject, so
    the durable held row must not rely on the mapper for them.  Only fixed
    fields, hard bounds, never bodies or prose."""
    out = {"source_time": "", "source_ref": "", "source_action": "", "subject": "pr"}
    if not isinstance(payload, dict):
        return out
    action = payload.get("action")
    if isinstance(action, str):
        out["source_action"] = action[:40]
    for key in ("review", "comment", "check_run", "check_suite", "merge_group", "pull_request", "issue"):
        obj = payload.get(key)
        if not isinstance(obj, dict):
            continue
        for field in ("submitted_at", "completed_at", "updated_at", "created_at"):
            value = obj.get(field)
            if isinstance(value, str) and value and not out["source_time"]:
                out["source_time"] = value[:40]
        ident = obj.get("id")
        if type(ident) is int and not out["source_ref"]:
            out["source_ref"] = str(ident)[:80]
        elif isinstance(obj.get("node_id"), str) and not out["source_ref"]:
            out["source_ref"] = obj["node_id"][:80]
    issue = payload.get("issue")
    if isinstance(issue, dict) and "pull_request" not in issue:
        out["subject"] = "issue"
    return out


def serve(listen: str, secret_provider, *, verify, map_event, allowlist, inbox, log):
    """Start the listener on a daemon thread and return it.

    listen is "host:port" (deployment supplies the address;  this patch
    proposes no address).  secret_provider re-reads the credential file per
    request, matching the other RA_* credential discipline.  The thread is a
    daemon:  the canonical Operator process owns the lifecycle."""
    host, sep, port_text = str(listen).rpartition(":")
    if not sep or not host or not port_text.isdigit():
        raise ValueError("RA_WEBHOOK_LISTEN must be host:port")
    port = int(port_text)
    if not 0 <= port <= 65535:
        raise ValueError("RA_WEBHOOK_LISTEN port out of range")
    repos = frozenset(allowlist)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # access logs carry no outcome value
            pass

        def _answer(self, status: int, body: dict) -> None:
            raw = json.dumps(body, sort_keys=True).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            if self.path != PATH:
                self._answer(404, {"accepted": False, "outcome": "unknown-path"})
                return
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                self._answer(413, {"accepted": False, "outcome": "body-size-refused"})
                return
            body = self.rfile.read(length)
            secret = secret_provider()
            if not secret:
                # Local configuration problem:  fail closed, retryable.
                self._answer(500, {"accepted": False, "outcome": "retry", "reason": "secret-unavailable"})
                return
            if not verify(body, self.headers.get("X-Hub-Signature-256"), secret.encode("utf-8")):
                # Auth failure:  nothing in the payload is trusted or read.
                self._answer(401, {"accepted": False, "outcome": "signature-refused"})
                return
            event_name = str(self.headers.get("X-GitHub-Event") or "")
            delivery_id = str(self.headers.get("X-GitHub-Delivery") or "")
            if not event_name or not DELIVERY_RE.match(delivery_id):
                self._answer(400, {"accepted": False, "outcome": "headers-missing"})
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeError, ValueError):
                self._answer(400, {"accepted": False, "outcome": "invalid", "reason": "json-malformed"})
                return
            if not isinstance(payload, dict):
                self._answer(400, {"accepted": False, "outcome": "invalid", "reason": "json-malformed"})
                return
            try:
                result = map_event(event_name, delivery_id, payload, repos)
            except Exception:
                # A mapper contract breach is a local fault, never payload trust.
                self._answer(500, {"accepted": False, "outcome": "retry", "reason": "mapper-error"})
                return
            outcome = str(_field(result, "outcome", "") or "")
            reason = str(_field(result, "reason", "") or "")[:80]
            if outcome == "invalid":
                self._answer(400, {"accepted": False, "outcome": "invalid", "reason": reason})
                return
            if outcome == "ignored":
                self._answer(200, {"accepted": True, "outcome": "ignored", "reason": reason})
                return
            if outcome == "held":
                hints = [_hint_dict(h) for h in (_field(result, "hints", ()) or ())]
                if not hints:
                    # Repo-level discovery hold:  one bounded evidence row
                    # (number 0 = no enumerated target), visible until a
                    # canonical discovery/ownership path exists.  Time, ref,
                    # action and subject come from the LISTENER's own bounded
                    # read of the authenticated payload (the HELD result
                    # omits them);  an advisory head rides only as validated
                    # 40-hex, never inferred.
                    kind = str(_field(result, "event_name", "") or "")
                    advisory = str(_field(result, "advisory_head_sha", "") or "")
                    envelope = _envelope(payload)
                    hints = [
                        {
                            "subject": envelope["subject"],
                            "repo": str(_field(result, "repo", "") or ""),
                            "number": 0,
                            "hint_kind": (
                                kind
                                if kind in HINT_KINDS_PR or kind in _DISCOVERY_KINDS
                                else ("issue" if envelope["subject"] == "issue" else "pr")
                            ),
                            "source_event_id": delivery_id,
                            "source_time": envelope["source_time"],
                            "source_ref": envelope["source_ref"],
                            "source_action": envelope["source_action"],
                            "head_sha": advisory if HEAD_RE.match(advisory) else "",
                        }
                    ]
                try:
                    targets = inbox.accept(delivery_id, hints, disposition="held", reason=reason)
                except InboxFull:
                    self._answer(503, {"accepted": False, "outcome": "retry", "reason": "inbox-full"})
                    return
                except ValueError:
                    self._answer(500, {"accepted": False, "outcome": "retry", "reason": "hint-invalid"})
                    return
                log("review-webhook-held", delivery=delivery_id, reason=reason)
                self._answer(200, {"accepted": True, "outcome": "held", "reason": reason, "targets": targets})
                return
            if outcome == "mapped":
                hints = [_hint_dict(h) for h in (_field(result, "hints", ()) or ())]
                if not hints:
                    self._answer(400, {"accepted": False, "outcome": "invalid", "reason": "mapped-without-targets"})
                    return
                try:
                    targets = inbox.accept(delivery_id, hints)
                except InboxFull:
                    # No durable per-target disposition:  NOTHING was written,
                    # so no acceptance is acknowledged.  The 503 is a VISIBLE
                    # refusal, not a relied-upon retry:  GitHub does not
                    # automatically redeliver failed deliveries.  The
                    # canonical poll covers refused OWNED-PR review targets
                    # from current source state;  issue and issue-comment
                    # subjects stay durably held until the reviewed consumer
                    # contract.  Already-durable targets dedupe any later
                    # redelivery.
                    self._answer(503, {"accepted": False, "outcome": "retry", "reason": "inbox-full"})
                    return
                except ValueError:
                    self._answer(500, {"accepted": False, "outcome": "retry", "reason": "hint-invalid"})
                    return
                self._answer(200, {"accepted": True, "outcome": "queued", "targets": targets})
                return
            self._answer(500, {"accepted": False, "outcome": "retry", "reason": "mapper-outcome-unknown"})

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(
        target=server.serve_forever, name="review-alerts-webhook", daemon=True
    )
    server.thread = thread
    thread.start()
    return server
