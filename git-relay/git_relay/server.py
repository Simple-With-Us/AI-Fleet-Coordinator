"""HTTP surface for git-relay.

Endpoints (all JSON in, JSON out):
    GET  /healthz
    POST /v1/open_pr           {repo, base, head, title, body}      (owner token)
    POST /v1/edit_pr           {repo, number, title?, body?}        (seat's App)
    POST /v1/reply_thread      {repo, number, comment_id, body}     (seat's App)
    POST /v1/comment_pr        {repo, number, body}                 (seat's App)
    POST /v1/mint_token        {repo, permissions}                  (seat's App)
    POST /v1/merge_pr          {repo, number, method?}              (seat's App)
    POST /v1/workflow_dispatch {repo, workflow, ref, inputs?}       (seat's App)
    POST /v1/resolve_thread    {repo, thread_id}                    (seat's App)

Run it as `git-relay serve` (or `python3 -m git_relay.server`).

Auth: "Authorization: Bearer <seat token>".  Every call, allowed or denied,
writes one audit row.  The GitHub token comes from GITHUB_TOKEN in the
environment at process start; it is never accepted over the wire, logged,
or returned.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .app_tokens import AppTokenError
from .audit import AuditLog
from .github_client import GitHubClient, RelayGitHubError
from .outcomes import OutcomeStore, OutcomeStoreError, key_for
from .policy import PolicyError, SeatPolicy
from .secret_scan import find_secret, reject_secrets, scan_args

MAX_REQUEST_BYTES = 64 * 1024


def _positive_int(raw: str, default: int) -> int:
    """An operator-tunable bound:  garbage, zero and negatives fall back."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


# Bounded inbound concurrency (Kody discussion_r4372744451):  the default one
# thread per connection, with each authenticate able to block on a Worker
# verify up to REQUEST_TIMEOUT_S, would let an unauthenticated spray of
# unique wrong secrets fan out into unbounded threads AND unbounded
# concurrent Worker calls.  A semaphore acquired BEFORE the handler thread is
# spawned caps both;  excess connections wait in the TCP accept backlog and
# are served in turn.  This bounds relay resources without an
# application-level refusal - but it is not magic:  under a flood that
# overruns the finite backlog the kernel turns new connections away, and a
# queued client may give up waiting.  That is degraded availability under an
# active flood, never an auth verdict on a bearer;  per-source fairness under
# such a flood is an edge property (the relay listens on loopback behind
# cloudflared).  The per-connection socket timeout below frees slots held by
# clients that connect and then stall, so a slow-incomplete-client set cannot
# pin every slot forever - and shutdown() returns within about one timeout.
MAX_CONCURRENT_REQUESTS = _positive_int(os.environ.get("GIT_RELAY_MAX_CONCURRENT", ""), 8)
REQUEST_SOCKET_TIMEOUT_S = _positive_int(os.environ.get("GIT_RELAY_SOCKET_TIMEOUT_S", ""), 30)
MAX_TEXT_FIELD = 32 * 1024

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8471


def _text(value, field: str, required: bool = True) -> str | None:
    if value is None:
        if required:
            raise ValueError(f"missing {field}")
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    if len(value) > MAX_TEXT_FIELD:
        raise ValueError(f"{field} exceeds the 32 KiB bound")
    return value


def _clean_text(value, field: str, required: bool = True) -> str:
    """_text plus a credential-shape refusal for seat-supplied outbound text."""
    text = _text(value, field, required=required)
    if text is not None:
        reject_secrets(field, text)
    return text


def _number(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _validate_args(action: str, args: dict) -> dict:
    """Full shape validation for one action;  returns the canonical arguments.

    Runs before any durable state or side effect:  every field the action
    accepts is checked here, and any field it does NOT accept is rejected, so
    ignored envelope fields cannot defeat the idempotency key and malformed
    input never reaches the outcome store or GitHub.
    """
    if action not in SeatPolicy.ALL_ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    if action in SeatPolicy.REGISTERED_ACTIONS:
        raise ValueError(f"action {action!r} is registered but not implemented yet")
    if action in SeatPolicy.WORKFLOW_ACTIONS:
        raise ValueError(f"action {action!r} is registered but not wired into the relay yet")
    specs: dict = {"repo": ("text", True)}
    if action == "open_pr":
        specs.update(
            {
                "base": ("text", True),
                "head": ("text", True),
                "title": ("clean", True),
                "body": ("clean", True),
            }
        )
    elif action == "edit_pr":
        # An edit must change something:  with neither title nor body GitHub
        # answers 422 after the call already left the fleet, which audits as a
        # terminal refusal instead of the local 400 it should be.
        if args.get("title") is None and args.get("body") is None:
            raise ValueError("edit_pr: title or body is required")
        specs.update(
            {"number": ("number", True), "title": ("clean", False), "body": ("clean", False)}
        )
    elif action == "reply_thread":
        specs.update(
            {"number": ("number", True), "comment_id": ("number", True), "body": ("clean", True)}
        )
    elif action == "comment_pr":
        specs.update({"number": ("number", True), "body": ("clean", True)})
    elif action == "mint_token":
        specs["permissions"] = ("permissions", True)
    elif action == "merge_pr":
        specs.update({"number": ("number", True), "method": ("method", False)})
    elif action == "workflow_dispatch":
        specs.update(
            {"workflow": ("text", True), "ref": ("text", True), "inputs": ("inputs", False)}
        )
    elif action == "resolve_thread":
        specs["thread_id"] = ("thread_id", True)
    extra = sorted(set(args) - set(specs))
    if extra:
        raise ValueError(f"{action}: unexpected fields ({len(extra)})")
    canonical: dict = {}
    for field, (kind, required) in specs.items():
        value = args.get(field)
        if kind == "text":
            value = _text(value, field, required=required)
        elif kind == "clean":
            value = _clean_text(value, field, required=required)
        elif kind == "thread_id":
            value = _text(value, field, required=required)
            if not re.fullmatch(r"PRRT_[A-Za-z0-9_-]{6,128}", value):
                raise ValueError("thread_id must be a pull request review thread id")
        elif kind == "number":
            value = _number(value, field)
        elif kind == "method":
            if value is None:
                value = "squash"
            if value not in ("merge", "squash", "rebase"):
                raise ValueError("method must be merge, squash, or rebase")
        elif kind == "permissions":
            if not isinstance(value, dict) or not value:
                raise ValueError("permissions must be a non-empty object")
            if any(not isinstance(k, str) or v not in ("read", "write") for k, v in value.items()):
                raise ValueError("permissions values must be read or write")
        elif kind == "inputs":
            if value is not None and not isinstance(value, dict):
                raise ValueError("inputs must be an object")
            if isinstance(value, dict):
                if len(value) > 32:
                    raise ValueError("inputs is capped at 32 keys")
                for key, item in value.items():
                    if not isinstance(key, str):
                        raise ValueError("inputs keys must be strings")
                    if not isinstance(item, (str, int, float, bool)):
                        raise ValueError("inputs values must be scalars")
                    if isinstance(item, str) and len(item) > 1024:
                        raise ValueError("inputs values are capped at 1024 characters")
        if value is not None:
            canonical[field] = value
    GitHubClient._check_repo(canonical["repo"])
    return canonical


class PolicyDenied(Exception):
    """The seat authenticated but its policy refused the call."""


class PreReadUnavailable(Exception):
    """A failed pre-read proves that no write was attempted."""


class ServerUnavailable(Exception):
    """A configured-on-paper backend (app-token minting) is not wired."""


# Minimum installation permissions each App action needs;  a seat whose
# mint_permissions ceiling does not cover these gets a policy denial.
ACTION_REQUIRED_PERMISSIONS = {
    "merge_pr": {"contents": "write", "pull_requests": "write"},
    "workflow_dispatch": {"actions": "write"},
    # GitHub gates resolveReviewThread at merge level for App tokens: pull_requests
    # write alone returns FORBIDDEN, contents write with it succeeds (GitHub's PR team
    # calls this intentional; reported at github/gh-aw#35726 and reproduced downstream).
    # A seat whose ceiling lacks contents write is denied here, before any mint.
    "resolve_thread": {"contents": "write", "pull_requests": "write"},
    # Use the PR-specific permission for these PR-only actions, including
    # conversation comments whose GitHub endpoint uses /issues/:number.
    "edit_pr": {"pull_requests": "write"},
    "reply_thread": {"pull_requests": "write"},
    "comment_pr": {"pull_requests": "write"},
}


class Relay:
    """Wires policy, client and audit together; easy to drive without HTTP."""

    def __init__(
        self,
        policy: SeatPolicy,
        client: GitHubClient,
        audit: AuditLog,
        app_minter=None,
        outcomes: OutcomeStore | None = None,
        workflow_verbs_enabled: bool = False,
        typed_disarm_enabled: bool = False,
    ):
        # Off unless the operator turns it on: with it off every wf_ action is refused
        # as registered-not-wired, exactly as before the wiring existed.
        self.workflow_verbs_enabled = workflow_verbs_enabled
        self.policy = policy
        self.client = client
        self.audit = audit
        self.app_minter = app_minter
        self.outcomes = outcomes if outcomes is not None else OutcomeStore(None)
        self.typed_gateway = None
        if typed_disarm_enabled:
            from .typed_gateway import TypedGateway

            self.typed_gateway = TypedGateway(policy, client, app_minter, audit, self.outcomes)

    def handle(self, token: str, action: str, args: dict) -> tuple[int, dict]:
        # A separate audited gateway charges policy once and keeps disarm
        # receipts fresh.  Default-off; no generic write-replay path is used.
        if self.typed_gateway is not None:
            from .typed_gateway import ACTIONS

            if action in ACTIONS:
                return self.typed_gateway.handle(token, action, args)
        seat = self.policy.authenticate(token)
        if seat is None:
            return 401, {"error": "unauthenticated"}
        # The action string comes from the URL path:  a credential-shaped one
        # must never be persisted in the audit row or echoed in an error.
        if find_secret(action):
            self.audit.record(seat, "<credential-shaped>", "", "bad_request", {"field": "action"})
            return 400, {"error": "action is not acceptable"}
        repo = str(args.get("repo") or "")
        if find_secret(repo):
            # A credential-shaped repo is refused by the scan below;  it must
            # never reach the audit log verbatim.
            repo = ""
        key = None
        # Every allowed or denied call writes exactly one audit row, from the
        # finally block below:  no branch (including an unexpected exception
        # from key_for, the outcome store, or dispatch) can skip it or double it.
        outcome = "ok"
        detail: dict = {}
        try:
            # Order is deliberate:  secret scan, full shape validation, then the
            # policy check (allowlist + hourly rate) ALL run before the
            # idempotency gate, so malformed or unauthorized input creates no
            # durable outcome row and replayed requests still consume quota.
            try:
                # Credential-shaped input is refused before any durable state
                # or side effect (SecretInputError is a ValueError).
                scan_args(args)
                if action in SeatPolicy.WORKFLOW_ACTIONS and self.workflow_verbs_enabled:
                    # The verb layer owns per-field shape validation and rejects any field it
                    # does not accept; here only the envelope (a repo string) is checked.
                    if not isinstance(args.get("repo"), str) or not args["repo"]:
                        raise ValueError("repo is required")
                    canonical = dict(args)
                    # Shape-check before any policy quota, outcome row or upstream call.
                    from .workflow_verbs import WorkflowVerbs

                    WorkflowVerbs.check_shape(action, canonical, canonical["repo"])
                else:
                    canonical = _validate_args(action, args)
                denial = self.policy.authorize(seat, action, canonical["repo"])
                if denial:
                    raise PolicyDenied(denial)
            except PolicyDenied as exc:
                outcome = "denied"
                detail = {"reason": str(exc)}
                return 403, {"error": str(exc)}
            except ValueError as exc:
                outcome = "bad_request"
                detail = {"reason": str(exc)}
                return 400, {"error": str(exc)}
            if action != "mint_token" and not self._is_workflow_read(action):
                # Idempotency gate for non-idempotent writes:  mint_token is exempt
                # because it returns a credential (a replay could only be served
                # by persisting that credential in the store, which is worse) and
                # a duplicate mint is benign - installation tokens expire within
                # the hour and the minter's per-key lock dedups concurrent calls.
                key = key_for(seat, action, canonical)
                try:
                    prior = self.outcomes.begin(key, reapply_ok=action in _REAPPLY_STATE_ACTIONS)
                except OutcomeStoreError as exc:
                    outcome = "unavailable"
                    detail = {"reason": type(exc).__name__}
                    return 503, {
                        "error": "outcome store is at capacity; reconcile held outcomes",
                        "retryable": True,
                    }
                if prior is not None and prior["state"] == "ok":
                    detail = {"idempotent_replay": True}
                    return 200, dict(prior.get("result") or {})
                if prior is not None:
                    outcome = "duplicate_refused"
                    return 409, {
                        "error": "an identical request is in flight or its outcome is "
                        "unknown; refusing to replay",
                        "outcome": prior["state"],
                        "retryable": False,
                    }
            try:
                result = self._dispatch(seat, action, canonical, canonical["repo"])
            except PolicyDenied as exc:
                self._release(key, "refused")
                outcome = "denied"
                detail = {"reason": str(exc)}
                return 403, {"error": str(exc)}
            except PreReadUnavailable:
                self._release(key, "refused")
                outcome = "unavailable"
                detail = {"reason": "pre_read_unavailable"}
                return 503, {
                    "error": "upstream pre-read unavailable; no write attempted",
                    "retryable": True,
                }
            except ServerUnavailable as exc:
                self._release(key, "refused")
                outcome = "unavailable"
                detail = {"reason": str(exc)}
                return 503, {"error": str(exc)}
            except AppTokenError as exc:
                # The mint failed before any GitHub write was attempted:  definite
                # refusal, the key is released, the seat can retry as-is.  The
                # message names the missing variable but never carries a value
                # (AppTokenError's contract).
                self._release(key, "refused")
                outcome = "unavailable"
                detail = {"error": "app_token_error"}
                return 503, {"error": f"app-token minting failed: {exc}"}
            except ValueError as exc:
                self._release(key, "refused")
                outcome = "bad_request"
                detail = {"reason": str(exc)}
                return 400, {"error": str(exc)}
            except RelayGitHubError as exc:
                # Audit only allow-listed scalar fields:  an upstream error message
                # can carry response-body content, so it is never logged.
                if exc.code == "invalid_grant":
                    # A definite credential-grant refusal:  nothing was written,
                    # the key is released, and a fixed credential may retry.  The
                    # masked preview identifies the rejected key without exposing it.
                    self._release(key, "refused")
                    outcome = "github_auth"
                    detail = {"status": exc.status}
                    payload = {
                        "error": "github refused the credential grant",
                        "github_status": exc.status,
                        "retryable": True,
                    }
                    if exc.code:
                        payload["github_code"] = detail["github_code"] = exc.code
                    if exc.credential_preview is not None:
                        payload["credential_preview"] = exc.credential_preview
                        detail["credential_preview"] = exc.credential_preview
                    payload["github_diagnostics"] = dict(exc.diagnostics)
                    detail["github_diagnostics"] = dict(exc.diagnostics)
                    return 503, payload
                if (
                    key is None
                    and exc.status in (0, 408, 429)
                    or (key is None and exc.status >= 500)
                ):
                    # A workflow READ changed nothing upstream: a transient failure is a
                    # plain retryable error, not an unknown outcome.
                    outcome = "github_retryable"
                    detail = {"status": exc.status}
                    payload = {
                        "error": "github call failed; safe to retry",
                        "github_status": exc.status,
                        "retryable": True,
                    }
                    if exc.retry_after is not None:
                        payload["retry_after_seconds"] = detail["retry_after_seconds"] = (
                            exc.retry_after
                        )
                    payload["github_diagnostics"] = dict(exc.diagnostics)
                    detail["github_diagnostics"] = dict(exc.diagnostics)
                    return (429 if exc.status == 429 else 502), payload
                if exc.status in (0, 408) or exc.status >= 500:
                    # Transport failure, timeout, or 5xx:  GitHub may have accepted
                    # the write.  The outcome is unknown;  the key is held and
                    # replay is refused until the outcome is resolved.
                    self._release(key, "unknown")
                    outcome = "github_unknown"
                    detail = {"status": exc.status}
                    payload = {
                        "error": "github call outcome unknown",
                        "github_status": exc.status,
                        "retryable": False,
                        "outcome": "unknown",
                    }
                    if exc.code is not None:
                        payload["github_code"] = exc.code
                        detail["github_code"] = exc.code
                    payload["github_diagnostics"] = dict(exc.diagnostics)
                    detail["github_diagnostics"] = dict(exc.diagnostics)
                    return 502, payload
                if exc.status == 429:
                    # Rate limited before the write ran:  a definite refusal.
                    # The key is released and the seat may retry, honoring any
                    # Retry-After GitHub asked for.
                    self._release(key, "refused")
                    outcome = "github_retryable"
                    detail = {"status": exc.status}
                    payload = {
                        "error": "github rate limited the request",
                        "github_status": exc.status,
                        "retryable": True,
                    }
                    if exc.retry_after is not None:
                        payload["retry_after_seconds"] = exc.retry_after
                        detail["retry_after_seconds"] = exc.retry_after
                    payload["github_diagnostics"] = dict(exc.diagnostics)
                    detail["github_diagnostics"] = dict(exc.diagnostics)
                    return 429, payload
                self._release(key, "refused")
                outcome = "github_terminal"
                detail = {"status": exc.status}
                payload = {
                    "error": "github refused the request",
                    "github_status": exc.status,
                    "retryable": False,
                }
                if exc.code:
                    payload["github_code"] = detail["github_code"] = exc.code
                if exc.credential_preview is not None:
                    payload["credential_preview"] = exc.credential_preview
                    detail["credential_preview"] = exc.credential_preview
                payload["github_diagnostics"] = dict(exc.diagnostics)
                detail["github_diagnostics"] = dict(exc.diagnostics)
                return 400, payload
            if key is not None:
                self.outcomes.finish(key, "ok", result)
            if action in SeatPolicy.WORKFLOW_ACTIONS:
                detail = _workflow_audit_detail(canonical, result)
            else:
                detail = {**canonical, **result}
            return 200, result
        except Exception as exc:
            # An unexpected relay failure:  whether GitHub was reached is
            # unknowable here, so hold the key as unknown (fail closed) and
            # still record the audit row in finally.
            self._release(key, "unknown")
            outcome = "error"
            detail = {"exception": type(exc).__name__}
            return 500, {"error": "internal relay error"}
        finally:
            self.audit.record(seat, action, repo, outcome, detail)

    def _release(self, key: str | None, state: str) -> None:
        if key is not None:
            self.outcomes.finish(key, state)

    def _dispatch(self, seat: str, action: str, args: dict, repo: str) -> dict:
        # handle() has already scanned, shape-validated, and authorized this
        # call (allowlist + hourly quota, once);  dispatch only executes.
        if action in SeatPolicy.WORKFLOW_ACTIONS:
            return self._dispatch_workflow(seat, action, args, repo)
        if action in SeatPolicy.APP_ACTIONS:
            return self._dispatch_app(seat, action, args, repo)
        if action == "open_pr":
            return self.client.open_pr(
                repo,
                base=_text(args.get("base"), "base"),
                head=_text(args.get("head"), "head"),
                title=_clean_text(args.get("title"), "title"),
                body=_clean_text(args.get("body"), "body"),
            )
        # Every non-open_pr write is an APP_ACTION and never reaches here.
        raise ValueError(f"unknown owner-token action {action!r}")


# Idempotent state setters must reach GitHub again after a verified success: A->B->A
# is a new desired state, not a replay of A.  Keep the same key and all pending/unknown
# holds; only OutcomeStore.begin atomically replaces a known-success row.
_REAPPLY_STATE_ACTIONS = frozenset(
    {
        "wf_issue_update",
        "wf_check_update",
        "wf_milestone_update",
        "wf_labels_set",
        "wf_reviewers_request",
        "wf_reviewers_remove",
        "wf_release_update",
        "wf_issue_comment_update",
        "wf_review_comment_update",
        "wf_assignees_add",
        "wf_assignees_remove",
        "wf_issue_lock",
        "wf_issue_unlock",
        "wf_workflow_enable",
        "wf_workflow_disable",
        "wf_pr_draft",
        "wf_pr_ready",
        "wf_automerge_enable",
        "wf_automerge_disable",
        "wf_release_asset_update",
    }
)

_WORKFLOW_AUDIT_SCALARS = (
    "job_id",
    "run_id",
    "number",
    "commit_id",
    "event",
    "side",
    "line",
    "ref",
    "branch",
    "base",
    "head",
    "expected_head_sha",
    "prior_sha",
    "tag",
    "sha",
    "expected_sha",
    "comment_id",
    "check_id",
    "deployment_id",
    "milestone",
    "release_id",
    "asset_id",
    "workflow_id",
    "artifact_id",
    "per_page",
    "page",
    "state",
    "tail_bytes",
    "method",
    "path",
)


def _workflow_audit_detail(canonical: dict, result: dict) -> dict:
    """What a wf_ call may leave in the audit log: bounded identifiers and counts only.

    Never file bytes, log text, commit messages, search text or any result body: a read
    verb returns repository content and a write verb is given it.
    """
    detail: dict = {
        k: canonical[k]
        for k in _WORKFLOW_AUDIT_SCALARS
        if isinstance(canonical.get(k), (int, str)) and not isinstance(canonical.get(k), bool)
    }
    if isinstance(canonical.get("files"), list):
        detail["files"] = len(canonical["files"])
    for key in ("commit", "sha", "merged", "files"):
        value = result.get(key)
        if isinstance(value, (int, bool)) or (isinstance(value, str) and len(value) <= 64):
            detail[f"result_{key}"] = value
    # Review and inline writes nest their new ids one level down; keep the ids
    # (bounded ints) so the audit row says which review or comment was created.
    for key, result_key in (("review", "result_review_id"), ("comment", "result_comment_id")):
        nested = result.get(key)
        if isinstance(nested, dict):
            value = nested.get("id")
            if isinstance(value, int) and not isinstance(value, bool):
                detail[result_key] = value
    return detail


def _is_workflow_read(self, action: str) -> bool:
    """A wf_ verb that changes nothing upstream needs no idempotency key and holds no outcome."""
    if action not in SeatPolicy.WORKFLOW_ACTIONS:
        return False
    from .workflow_verbs import SPECS

    spec = SPECS.get(action)
    return spec is not None and not spec[1]


def _dispatch_workflow(self, seat: str, action: str, args: dict, repo: str) -> dict:
    # Imported here: workflow_verbs imports this module.
    from .workflow_verbs import CeilingDenied, Unknown, WorkflowVerbs

    if self.app_minter is None:
        raise ServerUnavailable("app-token minting is not configured on this relay")
    try:
        # handle() has already run policy.authorize for this exact seat, action and repo
        # (and counted its quota once); for_action then only checks the App ceiling and
        # mints the least permission in SPECS.
        handle = WorkflowVerbs.for_action(
            self.client, self.policy, self.app_minter, seat, action, repo, preauthorized=True
        )
        return handle.run(action, args)
    except Unknown:
        raise ServerUnavailable("the seat's own App credential is unavailable") from None
    except CeilingDenied as exc:
        raise PolicyDenied(str(exc)) from None


Relay._is_workflow_read = _is_workflow_read
Relay._dispatch_workflow = _dispatch_workflow


def _perm_within_ceiling(requested: dict, ceiling: dict) -> str | None:
    ranks = {"read": 1, "write": 2}
    for name, level in requested.items():
        allowed = ceiling.get(name)
        if allowed is None or ranks.get(allowed, 0) < ranks.get(level, 99):
            return name
    return None


def _app_dispatch(self, seat: str, action: str, args: dict, repo: str) -> dict:
    if self.app_minter is None:
        raise ServerUnavailable("app-token minting is not configured on this relay")
    app_slug, ceiling = self.policy.app_identity(seat)
    if action == "mint_token":
        requested = args.get("permissions")
        if not isinstance(requested, dict) or not requested:
            raise ValueError("permissions must be a non-empty object")
        if any(not isinstance(k, str) or v not in ("read", "write") for k, v in requested.items()):
            raise ValueError("permissions values must be read or write")
        missing = _perm_within_ceiling(requested, ceiling)
        if missing:
            raise PolicyDenied(f"seat ceiling does not cover permission {missing!r}")
        token, expiry = self.app_minter.token_for(app_slug, repo, dict(requested))
        return {
            "token": token,
            "app_slug": app_slug,
            "expires_in_seconds": max(0, int(expiry - time.time())),
        }
    required = ACTION_REQUIRED_PERMISSIONS[action]
    missing = _perm_within_ceiling(required, ceiling)
    if missing:
        raise PolicyDenied(f"seat ceiling does not cover permission {missing!r}")
    token, _ = self.app_minter.token_for(app_slug, repo, dict(required))
    if action == "edit_pr":
        return self.client.edit_pr(
            repo,
            _number(args.get("number"), "number"),
            title=_clean_text(args.get("title"), "title", required=False),
            body=_clean_text(args.get("body"), "body", required=False),
            bearer=token,
        )
    if action == "reply_thread":
        return self.client.reply_thread(
            repo,
            _number(args.get("number"), "number"),
            _number(args.get("comment_id"), "comment_id"),
            _clean_text(args.get("body"), "body"),
            bearer=token,
        )
    if action == "comment_pr":
        return self.client.comment_pr(
            repo,
            _number(args.get("number"), "number"),
            _clean_text(args.get("body"), "body"),
            bearer=token,
        )
    if action == "merge_pr":
        return self.client.merge_pr(
            repo,
            _number(args.get("number"), "number"),
            str(args.get("method") or "squash"),
            bearer=token,
        )
    if action == "workflow_dispatch":
        return self.client.workflow_dispatch(
            repo,
            _text(args.get("workflow"), "workflow"),
            _text(args.get("ref"), "ref"),
            args.get("inputs"),
            bearer=token,
        )
    return self.client.resolve_thread(_text(args.get("thread_id"), "thread_id"), bearer=token)


Relay._dispatch_app = _app_dispatch


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a hard cap on in-flight requests.

    The semaphore is acquired in the accept loop (process_request), so once
    the slots are taken the kernel backlog - not a growing thread pile -
    absorbs the burst.  request_queue_size is raised so the backlog actually
    holds it.  Slots release when the request's thread finishes.
    """

    daemon_threads = True
    request_queue_size = 128

    def __init__(
        self,
        *args,
        max_concurrent: int = MAX_CONCURRENT_REQUESTS,
        socket_timeout_s: int = REQUEST_SOCKET_TIMEOUT_S,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(max(1, max_concurrent))
        self._socket_timeout_s = max(1, socket_timeout_s)

    def process_request(self, request, client_address):
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        # A stalled or half-sent request must not hold its slot forever.
        request.settimeout(self._socket_timeout_s)
        try:
            super().process_request_thread(request, client_address)
        except (TimeoutError, OSError):
            # socket.timeout subclasses OSError:  a stalled client is dropped.
            pass
        finally:
            self._slots.release()


def make_handler(relay: Relay):
    class Handler(BaseHTTPRequestHandler):
        server_version = "swu-git-relay/1"

        def log_message(self, fmt, *fmt_args):  # keep default stderr log out of the audit path
            return

        def _send(self, status: int, obj: dict) -> None:
            raw = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "v1":
                self._send(404, {"error": "not found"})
                return
            action = parts[1]
            auth = self.headers.get("Authorization") or ""
            token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send(400, {"error": "bad Content-Length"})
                return
            if length < 1 or length > MAX_REQUEST_BYTES:
                self._send(400, {"error": "request body missing or over the 64 KiB bound"})
                return
            raw = self.rfile.read(length)
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                self._send(400, {"error": "body is not valid JSON"})
                return
            if not isinstance(args, dict):
                self._send(400, {"error": "body must be a JSON object"})
                return
            status, obj = relay.handle(token, action, args)
            self._send(status, obj)

    return Handler


DEFAULT_AUDIT_PATH = "git-relay-audit.jsonl"
DEFAULT_OUTCOMES_PATH = "git-relay-outcomes.json"
# The state files were renamed along with the CLI (GHR -> git-relay).  The
# outcome store is a write-idempotency ledger:  its pending/unknown rows are
# the only thing that stops a replay of a write whose GitHub outcome was never
# known.  A renamed relay that ignored the ledger left behind under the old
# name would start empty and re-arm those side effects, so the old file is
# adopted rather than abandoned.  This moves a file once, on first start;  it
# adds no alias, no fallback name, and no second accepted configuration.
LEGACY_STATE_FILES = {
    DEFAULT_AUDIT_PATH: "pr-relay-audit.jsonl",
    DEFAULT_OUTCOMES_PATH: "pr-relay-outcomes.json",
}
# How long a starting relay waits for another one to finish adopting the same
# legacy file before it refuses.  Adoption is a copy of one small jsonl file, so
# this is generous; it exists to fail loudly rather than silently start with an
# empty ledger.
ADOPT_LOCK_WAIT_SECONDS = 5.0


def _fsync_dir(directory: str) -> None:
    """Make a rename or unlink durable, not just visible to this process."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _acquire_adopt_lock(lock_path: str) -> int | None:
    """Try once to hold the adoption lock;  return its fd, or None if it is held.

    The lock is an exclusive ``flock`` on the lock file, not the file's mere
    existence.  The kernel drops it when its owner exits, however it exits, so a
    crash mid-adoption cannot wedge later startups and no staleness guess (an
    age bound, a PID that may be reused) is ever needed.  Nothing ever deletes a
    lock another process holds.  The holder unlinks the file before it lets go,
    so after taking the lock the path must still name the file this fd has
    open;  if it does not, a previous holder finished in between and the caller
    tries again on the fresh file.  Only "held by someone else" counts as
    contention:  every other OSError propagates.
    """
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        try:
            on_path = os.stat(lock_path)
        except FileNotFoundError:
            os.close(fd)
            return -1
        held = os.fstat(fd)
        if (on_path.st_dev, on_path.st_ino) != (held.st_dev, held.st_ino):
            os.close(fd)
            return -1
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.fsync(fd)
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def adopt_legacy_state(default_path: str) -> str:
    """Adopt a pre-rename state file when the new default does not exist yet."""
    default_name = os.path.basename(default_path)
    legacy_name = LEGACY_STATE_FILES.get(default_name)
    if legacy_name is None:
        return default_path
    directory = os.path.dirname(default_path) or "."
    legacy_path = os.path.join(directory, legacy_name)
    current_path = os.path.join(directory, default_name)
    # An existing current file always wins:  it is the live ledger.
    if os.path.exists(current_path) or not os.path.exists(legacy_path):
        return default_path
    # A check-then-act is not enough here.  Two relays starting in one directory
    # both see "no current file", and the loser of the race would either crash in
    # os.replace or overwrite a live ledger that appeared in between - exactly the
    # orphaning this migration exists to prevent.  The lock makes the adoption
    # exclusive and the re-check under it makes the winner's file authoritative.
    lock_path = os.path.join(directory, f".{default_name}.adopt.lock")
    deadline = time.monotonic() + ADOPT_LOCK_WAIT_SECONDS
    while True:
        lock_fd = _acquire_adopt_lock(lock_path)
        if lock_fd is not None and lock_fd >= 0:
            break
        if lock_fd == -1:
            # The previous holder finished and removed the file we locked;  the
            # next attempt locks the current one.
            continue
        # Someone else is adopting, or already did.  Either way the answer is
        # the same once their file is in place.
        if os.path.exists(current_path) or not os.path.exists(legacy_path):
            return default_path
        if time.monotonic() > deadline:
            raise SystemExit(
                f"error: {lock_path} is held; another git-relay is adopting "
                f"{legacy_name} into {default_name}.  Retry once it finishes."
            )
        time.sleep(0.05)
    try:
        # Re-check under the lock:  the current file always wins.
        if os.path.exists(current_path) or not os.path.exists(legacy_path):
            return default_path
        # Stage the contents in the same directory, fsync them, then replace the
        # final path, so a crash cannot leave the ledger half-written or lose the
        # rename that gives it its new name.
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=f".{default_name}.", suffix=".adopt")
        try:
            with os.fdopen(fd, "wb") as target, open(legacy_path, "rb") as source:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            os.replace(tmp_path, current_path)
            _fsync_dir(directory)
            os.unlink(legacy_path)
            _fsync_dir(directory)
        except Exception:
            # The legacy file is untouched unless the unlink was reached, so a
            # retry is safe; only the staged copy goes.
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
    finally:
        # Unlink while still holding the lock, then release it by closing.  A
        # waiter that locks the unlinked file sees the path no longer names it
        # and retries, so two adopters never run at once.
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass
        finally:
            os.close(lock_fd)
    print(
        f"git-relay: adopted the pre-rename {legacy_name} as {default_name}; "
        "its held write outcomes still apply",
        flush=True,
    )
    return default_path


def _build_shared_verifier():
    """The ST-MCP shared-bearer verifier, or None when not configured.

    Set GIT_RELAY_VERIFY_URL to resolve admin-minted per-identity bearers
    through the Worker's verify route.  The route key is a fleet-secrets read
    like every other credential here;  a configured URL with an unreadable or
    short key is a startup error, never silent unauthenticated operation.

    The verify key lives in Fleet-OPS /tools (the same folder the Worker
    resolves it from), NOT in /github-apps with the App credentials:
    GIT_RELAY_VERIFY_KEY_PATH overrides the folder, default "/tools".
    """
    verify_url = os.environ.get("GIT_RELAY_VERIFY_URL", "").strip()
    if not verify_url:
        return None
    if not shutil.which("fleet-secrets"):
        raise SystemExit("git-relay config: fleet-secrets executable not found on PATH")
    from .app_tokens import AppTokenError, FleetSecretsSource
    from .shared_bearer import SharedBearerVerifier

    raw_path = os.environ.get("GIT_RELAY_VERIFY_KEY_PATH")
    try:
        return SharedBearerVerifier(
            url=verify_url,
            key_name=os.environ.get("GIT_RELAY_VERIFY_KEY_NAME", "GR_BEARER_VERIFY_KEY"),
            # The host plan provisions TWO scoped service tokens:  the
            # owner-only /git-relay-owner token, and the existing App
            # reader whose scope already covers /github-apps and /tools.
            # The verify-key source therefore binds the App credential
            # file by default;  GIT_RELAY_TOOLS_CREDENTIAL_FILE exists
            # only for a future split.
            key_source=FleetSecretsSource(
                path=(raw_path or "/tools").strip(),
                credential_file=(
                    _credential_file_for(
                        "the /tools verify-key source",
                        "GIT_RELAY_TOOLS_CREDENTIAL_FILE",
                        "",
                    )
                    if os.environ.get("GIT_RELAY_TOOLS_CREDENTIAL_FILE") is not None
                    else _credential_file_for(
                        "the App-credentials source",
                        "GIT_RELAY_APPS_CREDENTIAL_FILE",
                        "git-relay-apps-token",
                    )
                ),
            ),
        )
    except (AppTokenError, ValueError) as exc:
        raise SystemExit(f"git-relay config: {exc}") from exc


def from_env() -> Relay:
    try:
        policy = SeatPolicy(
            os.environ.get("GIT_RELAY_SEATS_JSON", ""),
            rate_per_hour=int(os.environ.get("GIT_RELAY_RATE_PER_HOUR", "120")),
            verifier=_build_shared_verifier(),
        )
    except PolicyError as exc:
        raise SystemExit(f"git-relay config: {exc}") from exc
    client = GitHubClient(_owner_token_from_cli())
    # An explicitly configured path is the operator's business:  adoption only
    # covers the built-in defaults.
    audit_path = (
        os.environ["GIT_RELAY_AUDIT_PATH"]
        if "GIT_RELAY_AUDIT_PATH" in os.environ
        else adopt_legacy_state(DEFAULT_AUDIT_PATH)
    )
    outcomes_path = (
        os.environ["GIT_RELAY_OUTCOMES_PATH"]
        if "GIT_RELAY_OUTCOMES_PATH" in os.environ
        else adopt_legacy_state(DEFAULT_OUTCOMES_PATH)
    )
    audit = AuditLog(audit_path)
    outcomes = OutcomeStore(outcomes_path)
    app_minter = None
    if os.environ.get("GIT_RELAY_APP_MINT", "1") != "0":
        # App-credential reads go through the fleet-secrets CLI only (owner
        # decision 2026-10-21:  no env secrets, no direct Infisical).  The CLI
        # must be installed on this host or App actions stay unavailable.
        if not shutil.which("fleet-secrets"):
            raise SystemExit("git-relay config: fleet-secrets executable not found on PATH")
        from .app_tokens import AppTokenError, AppTokenMinter, FleetSecretsSource

        # The Fleet-OPS prod folder the App keys are read from.  Defaults to
        # /github-apps (their verified live location);  GIT_RELAY_SECRETS_PATH
        # overrides it for a future folder move without a code change.  A
        # set-but-empty override is a config error like any other, not a
        # silently different default.
        raw_secrets_path = os.environ.get("GIT_RELAY_SECRETS_PATH")
        if raw_secrets_path is not None and not raw_secrets_path.strip():
            raise SystemExit(
                "git-relay config: GIT_RELAY_SECRETS_PATH is set but empty; "
                "unset it to use /github-apps"
            )
        try:
            app_minter = AppTokenMinter(
                FleetSecretsSource(
                    path=(raw_secrets_path or "/github-apps").strip(),
                    credential_file=_credential_file_for(
                        "the App-credentials source",
                        "GIT_RELAY_APPS_CREDENTIAL_FILE",
                        "git-relay-apps-token",
                    ),
                ),
                lambda method, path, bearer, payload=None: client._call(
                    method, path, payload, bearer=bearer
                ),
            )
        except AppTokenError as exc:
            raise SystemExit(f"git-relay config: {exc}") from exc
    return Relay(
        policy,
        client,
        audit,
        app_minter=app_minter,
        outcomes=outcomes,
        workflow_verbs_enabled=os.environ.get("GIT_RELAY_ENABLE_WORKFLOW_VERBS") == "1",
        typed_disarm_enabled=os.environ.get("GIT_RELAY_ENABLE_TYPED_DISARM") == "1",
    )


def _credential_file_for(scope: str, env_var: str, default_name: str) -> str | None:
    """The protected Infisical-token file one fleet-secrets source runs under.

    Production resolves it from systemd's CREDENTIALS_DIRECTORY (the unit's
    LoadCredential keys are the default file names) or an explicit
    GIT_RELAY_*_CREDENTIAL_FILE override;  a set-but-empty override is a
    config error like any other.  With neither, startup REFUSES - the
    process environment is never a secret source, and a CLI child that
    inherits it would pick up co-located secrets (seat tokens, another
    scope's token).  There is no inheritance escape:  tests and local
    runs point the variables at their own files.  Names scopes and
    variables only, never file contents.
    """
    explicit = os.environ.get(env_var)
    if explicit is not None:
        if not explicit.strip():
            raise SystemExit(f"git-relay config: {env_var} is set but empty")
        return explicit.strip()
    creds = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if creds:
        return os.path.join(creds, default_name)
    raise SystemExit(
        f"git-relay config: no credential file binding for {scope}:  "
        f"set {env_var} or run under systemd LoadCredential "
        f"(CREDENTIALS_DIRECTORY/{default_name})"
    )


def _owner_token_from_cli() -> str:
    """The owner PAT for open_pr, read once at startup through `fleet-secrets
    get-verified` at its fixed folder (/git-relay-owner):  the CLI proves the
    value came from that exact folder before answering.  Never the process
    environment - an ambient GITHUB_TOKEN is IGNORED, and there is no other
    fallback source (owner direction 2026-10-21 + the /git-relay-owner
    custody move 2026-10-25).  The relay refuses to start without it,
    exactly as it refused to start with an empty GITHUB_TOKEN before,
    and it refuses to start without a credential file binding for the
    folder scope (_credential_file_for):  no ambient inheritance in
    production.
    """
    from .app_tokens import AppTokenError, FleetSecretsSource

    source = FleetSecretsSource(
        path="/git-relay-owner",
        credential_file=_credential_file_for(
            "the /git-relay-owner source",
            "GIT_RELAY_OWNER_CREDENTIAL_FILE",
            "git-relay-owner-token",
        ),
    )
    try:
        return source.verified_secret("GITHUB_TOKEN")
    except AppTokenError as exc:
        raise RuntimeError(f"git-relay config: owner token:  {exc}") from None


def main() -> None:
    relay = from_env()
    host = os.environ.get("GIT_RELAY_HOST", DEFAULT_HOST)
    port = int(os.environ.get("GIT_RELAY_PORT", str(DEFAULT_PORT)))
    server = BoundedThreadingHTTPServer((host, port), make_handler(relay))
    print(f"git-relay listening on {host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
