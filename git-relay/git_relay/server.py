"""HTTP surface for git-relay.

Endpoints (all JSON in, JSON out):
    GET  /healthz
    POST /v1/open_pr           {repo, base, head, title, body, draft?,
                                origin_session_claim?, origin_letter_claim?,
                                origin_generation_claim?}              (owner token; read back)
    POST /v1/service_read      {repo, number, verb, page?, sha?}      (dedicated
                                service bearer; fixed read verbs, repo allowlist;
                                narrowed one-repo App grant, never the token)
    POST /v1/edit_pr           {repo, number, title?, body?}        (seat's App)
    POST /v1/reply_thread      {repo, number, comment_id, body}     (seat's App)
    POST /v1/comment_pr        {repo, number, body}                 (seat's App)
    POST /v1/mint_token        {repo, permissions}                  (seat's App)
    POST /v1/merge_pr          {repo, number, method?}              (seat's App)
    POST /v1/workflow_dispatch {repo, workflow, ref, inputs?}       (seat's App)
    POST /v1/resolve_thread    {repo, thread_id}                    (seat's App)
    POST /v1/create_repo       {repo, visibility?, description?,    (seat's App,
                                license_template?}                    namespace token)

Run it as `git-relay serve` (or `python3 -m git_relay.server`).

Auth: "Authorization: Bearer <seat token>".  Every call, allowed or denied,
writes one audit row.  The GitHub token comes from GITHUB_TOKEN in the
environment at process start; it is never accepted over the wire, logged,
or returned.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import git_read, service_read
from .app_tokens import AppTokenError, missing_app_credential
from .audit import AuditLog, BootstrapDenialAuditor, UnauthorizedAuditor
from .github_client import (
    DispatchPreReadUnavailable,
    GitHubClient,
    RelayGitHubError,
    intent_branch_ref,
)
from .outcomes import MAX_CAPACITY, OutcomeStore, OutcomeStoreError, key_for
from .policy import (
    DEFAULT_RATE_PER_HOUR,
    DEFAULT_WRITE_RATE_PER_HOUR,
    PolicyError,
    SeatPolicy,
    auth_failure_response,
)
from .policy_input import seats_json_from_env
from .pr_owners import PROwnerStore
from .secret_scan import MAX_SCAN_NODES, find_secret, reject_secrets, scan_args

MAX_REQUEST_BYTES = 64 * 1024
# Only the binary release-asset upload may exceed MAX_REQUEST_BYTES (base64 of 180 KiB).
MAX_BINARY_REQUEST_BYTES = 256 * 1024
BINARY_REQUEST_ACTIONS = frozenset({"wf_release_asset_upload_binary"})
# Existing full-file actions get a narrow envelope allowance.  No default,
# permission, scan budget or workflow verb is widened.
MAX_FILE_REQUEST_BYTES = 6 * 1024 * 1024
FILE_REQUEST_ACTIONS = frozenset({"wf_file_put", "wf_files_put"})
# The opt-in large-text git creates carry up to 716,800 bytes of text (JSON escaping can expand it several times); the
# envelope allowance stays narrow and the verb owns the exact text bound.
MAX_LARGE_TEXT_REQUEST_BYTES = 6 * 1024 * 1024
LARGE_TEXT_REQUEST_ACTIONS = frozenset({"wf_git_blob_create", "wf_git_tree_create"})
# Sealed secret values (up to 48 KiB, base64 plus envelope) exceed the default 64 KiB cap; the verb owns the exact bound.
MAX_SECRET_PUT_REQUEST_BYTES = 128 * 1024
SECRET_PUT_REQUEST_ACTIONS = frozenset(
    a
    for a in SeatPolicy.WORKFLOW_ACTIONS
    if a.startswith("wf_secret_put_") or a == "wf_org_variable_set"
)


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


#: The fleet session-id shape (fleet-apps.json `sessionIds`;  session_ids.py parity copy
#: is ^[A-Za-z0-9][A-Za-z0-9._:-]{3,127}$).  A claim is a caller's say-so only:  it is
#: never sent upstream and never routes anything.  The only binding is the
#: session's own registry row (bound_prs), checked live by the wake-router at
#: delivery;  the relay itself verifies nothing about the claims.
_SESSION_CLAIM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{3,127}")
#: A fleet session letter is exactly two capitals (wake-router letter rule).
_LETTER_CLAIM_RE = re.compile(r"[A-Z]{2}")


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

    git_read is a policy name for the /git/ route (git_read.py), so a JSON call to it is refused here.

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
    if action == git_read.ACTION:
        raise ValueError("git_read has no JSON call;  it is served on the /git/ route")
    if action == service_read.ACTION:
        return service_read.validate(args)
    if action == "create_repo":
        return _validate_create_repo(args)
    specs: dict = {"repo": ("text", True)}
    if action == "open_pr":
        specs.update(
            {
                "base": ("text", True),
                "head": ("text", True),
                "title": ("clean", True),
                "body": ("clean", True),
                "draft": ("boolean", False),
                # Untrusted caller claims:  recorded for the ownership index,
                # never sent upstream.  The trusted binding is the session's own
                # registry row (bound_prs), enforced by the wake-router.
                "origin_session_claim": ("session_claim", False),
                "origin_letter_claim": ("letter_claim", False),
                "origin_generation_claim": ("generation_claim", False),
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
        # intent_id is envelope metadata only:  it binds the idempotency key
        # to an explicit operation intent (the same contract pattern as
        # workflow_dispatch's), so a deliberate re-trigger of an identical
        # comment is a NEW operation while an identical no-intent resend
        # stays idempotent-replayed.  It never reaches dispatch or the
        # upstream comment.
        specs.update(
            {"number": ("number", True), "body": ("clean", True), "intent_id": ("text", False)}
        )
    elif action == "mint_token":
        specs["permissions"] = ("permissions", True)
    elif action == "merge_pr":
        specs.update({"number": ("number", True), "method": ("method", False)})
    elif action == "workflow_dispatch":
        specs.update(
            {
                "workflow": ("text", True),
                "ref": ("text", True),
                "inputs": ("inputs", False),
                "intent_id": ("text", False),
                "expected_head_sha": ("text", False),
            }
        )
    elif action == "resolve_thread":
        specs["thread_id"] = ("thread_id", True)
    extra = sorted(set(args) - set(specs))
    if extra:
        raise ValueError(f"{action}: unexpected fields ({len(extra)})")
    if action == "open_pr":
        # The three creation-time native claims are all-or-none:  a caller
        # that offers one claim offers all three, so a partial set can never
        # be read as a complete attestation.  Each claim is shape-checked
        # below, and an absent claim set stays absent.
        offered = {name for name in args if name.startswith("origin_")}
        if offered and offered != set(service_read.CLAIMS):
            raise ValueError("native origin claims must be supplied together")
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
        elif kind == "generation_claim":
            if value is None and not required:
                pass
            else:
                value = _number(value, field)
                if value > 2147483647:
                    raise ValueError(f"{field} must be a generation between 1 and 2147483647")
        elif kind == "number":
            value = _number(value, field)
        elif kind == "boolean":
            if field in args and type(value) is not bool:
                raise ValueError(f"{field} must be a boolean")
            # False is the existing default and must retain the same outcome key.
            if value is False:
                value = None
        elif kind == "session_claim":
            value = _text(value, field, required=required)
            if value is not None and not _SESSION_CLAIM_RE.fullmatch(value):
                raise ValueError(f"{field} is not a session id shape")
        elif kind == "letter_claim":
            value = _text(value, field, required=required)
            if value is not None and not _LETTER_CLAIM_RE.fullmatch(value):
                raise ValueError(f"{field} is not a session letter (two capitals)")
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
    if action == "workflow_dispatch":
        intent = canonical.get("intent_id")
        expected = canonical.get("expected_head_sha")
        if expected is not None and intent is None:
            raise ValueError("expected_head_sha requires intent_id")
        if intent is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}", intent):
            raise ValueError("invalid dispatch intent or expected source")
        if expected is not None and not re.fullmatch(r"[0-9a-f]{40}", expected):
            raise ValueError("invalid dispatch intent or expected source")
        if intent is not None and not intent_branch_ref(canonical.get("ref")):
            raise ValueError("intent dispatch requires a canonical branch name")
    GitHubClient._check_repo(canonical["repo"])
    return canonical


def _validate_create_repo(args: dict) -> dict:
    """Shape for create_repo.  Owner allowlist, name, and private-by-default visibility."""
    extra = sorted(set(args) - {"repo", "visibility", "description", "license_template"})
    if extra:
        raise ValueError(f"create_repo: unexpected fields ({len(extra)})")
    repo = _text(args.get("repo"), "repo")
    if repo.count("/") != 1:
        raise ValueError("invalid repository")
    owner, name = repo.split("/", 1)
    account = SeatPolicy.create_repo_account(owner)
    if account is None:
        raise ValueError("repository owner is not allowed")
    if not SeatPolicy.valid_create_repo_name(name):
        raise ValueError("invalid repository")
    canonical = {"repo": f"{account[0]}/{name}", "visibility": "private"}
    if "visibility" in args:
        visibility = args.get("visibility")
        if visibility not in ("private", "public"):
            raise ValueError("visibility must be private or public")
        canonical["visibility"] = visibility
    if args.get("description") is not None:
        description = _clean_text(args.get("description"), "description")
        if len(description) > 350:
            raise ValueError("description exceeds the 350 character bound")
        canonical["description"] = description
    if args.get("license_template") is not None:
        if args.get("license_template") != "apache-2.0":
            raise ValueError("license_template must be apache-2.0")
        canonical["license_template"] = "apache-2.0"
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
    "git_read": dict(git_read.READ_PERMISSIONS),
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
        pr_owners: PROwnerStore | None = None,
        service_read_token: str | None = None,
        service_read_repos=frozenset(),
    ):
        # Off unless the operator turns it on: with it off every wf_ action is refused
        # as registered-not-wired, exactly as before the wiring existed.
        self.workflow_verbs_enabled = workflow_verbs_enabled
        self.policy = policy
        self.client = client
        self.audit = audit
        self._unauth_auditor = UnauthorizedAuditor()
        self._bootstrap_auditor = BootstrapDenialAuditor()
        self.app_minter = app_minter
        self.outcomes = outcomes if outcomes is not None else OutcomeStore(None)
        self.pr_owners = pr_owners
        # The service-read lane:  a dedicated bearer (not a seat token) and the
        # exact repos it may read.  None/empty keeps the lane closed.
        self.service_read_token = service_read_token
        self.service_read_repos = frozenset(service_read_repos)
        self.typed_gateway = None
        if typed_disarm_enabled:
            from .typed_gateway import TypedGateway

            self.typed_gateway = TypedGateway(policy, client, app_minter, audit, self.outcomes)

    def handle(
        self,
        token: str,
        action: str,
        args: dict,
        proof: dict | None = None,
        *,
        operator_bootstrap: bool = False,
    ) -> tuple[int, dict]:
        if action == "service_read" and self.service_read_token is not None:
            # The alert service's read lane:  its own bearer, no seat policy,
            # fixed verbs and an explicit repo allowlist.  It never mints for
            # a caller-chosen scope and never returns a token.
            #
            # A configured lane owns the action outright:  the lane's own
            # credential is the only one it accepts, and it answers every
            # other bearer with its ordinary unauthenticated refusal.  A
            # relay with no lane configured leaves the action to the normal
            # policy path, where the seat's own grant, repository scope and
            # quota apply.
            return service_read.lane(self, token, args)
        # A separate audited gateway charges policy once and keeps disarm
        # receipts fresh.  Default-off; no generic write-replay path is used.
        if self.typed_gateway is not None:
            from .typed_gateway import ACTIONS

            if action in ACTIONS:
                return self.typed_gateway.handle(token, action, args, proof=proof)
        seat, auth_reason = self.policy.authenticate_with_reason(token, proof)
        if seat is None:
            if auth_reason == "seat_not_in_policy":
                # First-seat bootstrap:  the bearer parses and names an
                # identity no shared seat claims.  Ask the verifier under the
                # bootstrap bounds and record the denial against the
                # verifier-CONFIRMED identity (never the bearer's claim), so
                # an install-seat bootstrap receipt can bind this exact run.
                # The request is refused either way:  the row names who was
                # refused, it does not grant anything, and a verified
                # identity is not attacker-chosen attribution.
                # Only requests arriving on the operator listener may spend
                # the shared verifier and auditor budgets:  the public
                # listener (operator_bootstrap=False) writes the generic
                # bounded unauth row for the same bearer, so an
                # unauthenticated spray from the edge can never reach the
                # verifier or starve first-seat enrollment.  On the operator
                # listener, only actions whose denial row can bind a bootstrap
                # receipt spend those budgets;  anything else falls through to
                # the generic bounded unauth row below.
                identity = (
                    self.policy.bootstrap_resolve(token)
                    if operator_bootstrap and action in ("wf_repo_get", "wf_file_patch")
                    else None
                )
                if identity is not None:
                    repo = str(args.get("repo") or "") if isinstance(args, dict) else ""
                    path = (
                        args.get("path")
                        if action == "wf_file_patch" and isinstance(args, dict)
                        else None
                    )
                    try:
                        recorded = self._bootstrap_auditor.record(
                            self.audit, action, repo, identity, path
                        )
                    except OSError:
                        # An unwritable audit log is not admission evidence:
                        # refuse without the marker rather than let a proof
                        # go PROVEN on a run no receipt could bind.
                        recorded = False
                    status, obj = auth_failure_response(auth_reason)
                    if recorded:
                        # Operator-admission marker:  the bootstrap proof
                        # requires it, so a public-listener run (generic
                        # bounded refusal, no verifier-backed identity row)
                        # cannot produce a proven-but-uninstallable receipt.
                        # Over-budget or unrecorded denials carry no marker:
                        # without the audit row the receipt would not
                        # install.  It names the listener's capability only;
                        # no credential material.
                        return status, dict(obj, bootstrap_admission="operator")
                    return status, obj
            # One bounded, rate-limited row (seat unknown, no token material)
            # so a credential spray is visible without filling the log.
            # The cause is a closed word (seat_not_in_policy, bearer_rejected,
            # verifier_unavailable, unauthenticated), never the credential.
            self._unauth_auditor.record(self.audit, action, auth_reason)
            return auth_failure_response(auth_reason)
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
        attempt_corr: str | None = None
        # Every allowed or denied call writes exactly one audit row, from the
        # finally block below:  no branch (including an unexpected exception
        # from key_for, the outcome store, or dispatch) can skip it or double it.
        outcome = "ok"
        detail: dict = {}
        title_prefixed = False
        key_title = None
        try:
            # Order is deliberate:  secret scan, full shape validation, then the
            # policy check (allowlist + hourly rate) ALL run before the
            # idempotency gate, so malformed or unauthorized input creates no
            # durable outcome row and replayed requests still consume quota.
            try:
                # Credential-shaped input is refused before any durable state
                # or side effect (SecretInputError is a ValueError).
                if action in BINARY_REQUEST_ACTIONS:
                    # The base64 payload gets a larger text budget; the byte scan of the decoded
                    # content happens in the verb.
                    scan_args(args, _budget=[MAX_SCAN_NODES, MAX_BINARY_REQUEST_BYTES])
                elif action in FILE_REQUEST_ACTIONS:
                    # wf_file_put and wf_files_put scan the decoded bytes of each file themselves.
                    scan_args(
                        args,
                        _budget=[MAX_SCAN_NODES, MAX_FILE_REQUEST_BYTES],
                        _decoded_elsewhere=True,
                    )
                elif action in LARGE_TEXT_REQUEST_ACTIONS:
                    scan_args(args, _budget=[MAX_SCAN_NODES, MAX_LARGE_TEXT_REQUEST_BYTES])
                elif action in SECRET_PUT_REQUEST_ACTIONS:
                    scan_args(args, _budget=[MAX_SCAN_NODES, MAX_SECRET_PUT_REQUEST_BYTES])
                else:
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
                if action == "open_pr" or (
                    action == "edit_pr" and canonical.get("title") is not None
                ):
                    # Jay's 2026-11-20 bracketed-heading convention:  the
                    # seat's tag leads the title at creation, and a retitle
                    # through edit_pr cannot strip it.  Applied to the
                    # canonical args so the upstream write and the audit row
                    # agree;  the idempotency key below still binds the seat's
                    # ORIGINAL title, so a pre-convention pending/unknown row
                    # matches its post-deploy retry instead of duplicating.
                    key_title = canonical["title"]
                    canonical["title"], title_prefixed = self.policy.apply_title_tag(
                        seat, canonical["title"]
                    )
                    if len(canonical["title"]) > MAX_TEXT_FIELD:
                        # The prefix itself may not push the field past the
                        # bound _validate_args enforced on the original.
                        raise ValueError("title exceeds the 32 KiB bound")
                denial = self.policy.authorize(seat, action, canonical["repo"])
                if denial:
                    raise PolicyDenied(denial)
                if (
                    action == "create_repo"
                    and canonical.get("visibility") == "public"
                    and not self.policy.public_repo_allowed(seat)
                ):
                    raise PolicyDenied("action 'create_repo_public' not allowed for this seat")
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
                key_args = {k: v for k, v in canonical.items() if not k.startswith("origin_")}
                key = key_for(
                    seat,
                    action,
                    {**key_args, "title": key_title} if key_title is not None else key_args,
                )
                try:
                    attempt_corr = uuid.uuid4().hex
                    prior = self.outcomes.begin(
                        key, reapply_ok=action in _REAPPLY_STATE_ACTIONS, corr=attempt_corr
                    )
                    if prior is not None:
                        # The audit binds the ORIGINAL attempt's id, not a new one.
                        attempt_corr = prior.get("corr") or self.outcomes.corr_of(key)
                except ValueError as exc:
                    # begin() raises ValueError for caller-input errors (e.g. a
                    # malformed corr):  a definite 400, never the capacity 503
                    # below, which a retry could never satisfy.  No outcome row
                    # exists on this path, so the audit row stamps no corr that
                    # could never join.
                    attempt_corr = None
                    outcome = "bad_request"
                    detail = {"reason": str(exc)}
                    return 400, {"error": str(exc)}
                except OutcomeStoreError as exc:
                    # Capacity refusal:  no outcome row was written, so the
                    # audit row stamps no corr either - a stamped one would
                    # dangle with no row to join to.
                    attempt_corr = None
                    outcome = "unavailable"
                    detail = {"reason": type(exc).__name__}
                    return 503, {
                        "error": "outcome store is at capacity; reconcile held outcomes",
                        "retryable": True,
                    }
                if prior is not None and prior["state"] == "ok":
                    detail = {"idempotent_replay": True}
                    replay = dict(prior.get("result") or {})
                    if action == "workflow_dispatch":
                        digest = hashlib.sha256(
                            json.dumps(canonical, sort_keys=True).encode()
                        ).hexdigest()
                        if canonical.get("intent_id") and replay.get("request_digest") != digest:
                            outcome = "digest_mismatch"
                            detail = {"reason": "dispatch_intent_payload_mismatch"}
                            return 409, {
                                "error": "dispatch intent already bound to a different request",
                                "retryable": False,
                            }
                        replay.update(
                            {
                                "replayed": True,
                                "upstream_request_sent": False,
                                "execution_verified": False,
                            }
                        )
                    return 200, replay
                if prior is not None:
                    outcome = "duplicate_refused"
                    return 409, {
                        "error": "an identical request is in flight or its outcome is "
                        "unknown; refusing to replay",
                        "outcome": prior["state"],
                        "retryable": False,
                    }
            try:
                if action == "open_pr" and self.pr_owners is not None:
                    # The creation fence is durable BEFORE the upstream
                    # create, so even total disk failure after a known
                    # create cannot hide a missing row across a restart.
                    # The fence binds to the outcome row the idempotency
                    # gate just wrote, so it uses that row's own
                    # correlation id:  a second key computation could drift
                    # from the key the gate used and would fence nothing.
                    try:
                        self.pr_owners.begin_creation(attempt_corr)
                    except (OSError, ValueError):
                        raise ServerUnavailable("creation index fence unavailable") from None
                result = self._dispatch(
                    seat,
                    action,
                    canonical,
                    canonical["repo"],
                    caller_token=token,
                    corr=attempt_corr,
                )
            except PolicyDenied as exc:
                self._release(key, "refused")
                outcome = "denied"
                detail = _denied_audit_detail(action, canonical, exc)
                return 403, {"error": str(exc)}
            except (PreReadUnavailable, DispatchPreReadUnavailable):
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
                # The mint failed before any GitHub write was attempted: definite
                # refusal, the key is released. A missing or unusable App id or
                # key is a closed reason and the exception text (which names the
                # variable) is not returned. Other mint failures keep the
                # existing 503; that text names a variable and never a value.
                self._release(key, "refused")
                outcome = "unavailable"
                if missing_app_credential(exc):
                    detail = {"error": "no_app_credential"}
                    return 403, {"error": "no_app_credential"}
                detail = {"error": "app_token_error"}
                return 503, {"error": f"app-token minting failed: {exc}"}
            except ValueError as exc:
                self._release(key, "refused")
                outcome = "bad_request"
                detail = _denied_audit_detail(action, canonical, exc)
                return 400, {"error": str(exc)}
            except RelayGitHubError as exc:
                # Audit only allow-listed scalar fields:  an upstream error message
                # can carry response-body content, so it is never logged.
                if exc.code == "repository_moved":
                    # GitHub redirected the slug (renamed or transferred repo).  The
                    # redirect was not followed, so nothing ran upstream:  a definite
                    # refusal that names where the repo went, never an outage (OPS-1036).
                    self._release(key, "refused")
                    outcome = "github_terminal"
                    payload = {
                        "error": "repository moved",
                        "github_status": exc.status,
                        "retryable": False,
                    }
                    detail = {"status": exc.status, "github_code": "repository_moved"}
                    if exc.moved_repository_id is not None:
                        payload["repository_id"] = exc.moved_repository_id
                        detail["repository_id"] = exc.moved_repository_id
                        moved_to = self._moved_repository(seat, exc.moved_repository_id)
                        if moved_to:
                            payload["moved_to"] = moved_to
                            detail["moved_to"] = moved_to
                    return 400, payload
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
                if key is None and exc.code in {"response_too_large", "response_invalid_unicode"}:
                    # A bounded READ changed nothing. Retrying the same body cannot
                    # make it fit. Writes still keep their unknown-outcome fence.
                    outcome = "github_terminal"
                    detail = {"status": exc.status, "github_code": exc.code}
                    return 502, {
                        "error": "github response failed deterministic validation",
                        "github_status": exc.status,
                        "github_code": exc.code,
                        "retryable": False,
                    }
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
                    # open_pr's read-back:  github_status stays 502 (the relay's
                    # classification);  what the read-back GET itself answered is carried
                    # separately.  The number the create named is unconfirmed, so it goes
                    # to the audit row only, labelled, and never into the answer.
                    readback_status = getattr(exc, "readback_status", None)
                    if readback_status is not None:
                        payload["readback_status"] = detail["readback_status"] = readback_status
                        if exc.retry_after is not None:
                            payload["readback_retry_after_seconds"] = exc.retry_after
                            detail["readback_retry_after_seconds"] = exc.retry_after
                    unconfirmed = getattr(exc, "unconfirmed_number", None)
                    if unconfirmed is not None:
                        detail["unconfirmed_number"] = unconfirmed
                        if action == "open_pr":
                            # The create's outcome is unknown:  record the
                            # seat's claim on this number as unconfirmed so
                            # the index can say so honestly.
                            detail["owner_recorded"] = self._record_pr_owner(
                                seat, canonical, unconfirmed, attempt_corr, confirmed=False
                            )
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
            if action == "workflow_dispatch":
                result.update(
                    {
                        "replayed": False,
                        "upstream_request_sent": True,
                        "execution_verified": False,
                        "request_digest": hashlib.sha256(
                            json.dumps(canonical, sort_keys=True).encode()
                        ).hexdigest(),
                    }
                )
            if key is not None:
                self.outcomes.finish(key, "ok", result)
            owner_recorded = None
            if action == "open_pr" and type(result.get("number")) is int:
                # Bind the returned PR to the authenticated seat in the
                # queryable ownership index.
                owner_recorded = self._record_pr_owner(
                    seat, canonical, result["number"], attempt_corr, confirmed=True
                )
            if action == "create_repo":
                # The request description is never an audit field.  Neither is a token.
                detail = {
                    field: result[field]
                    for field in ("repo", "visibility", "result", "id")
                    if field in result
                }
            elif action in SeatPolicy.WORKFLOW_ACTIONS:
                detail = _workflow_audit_detail(canonical, result)
            else:
                detail = {**canonical, **result}
                if title_prefixed:
                    # The seat sent no conforming heading;  the relay added it.
                    detail["title_prefixed"] = True
                if owner_recorded is not None:
                    detail["owner_recorded"] = owner_recorded
            return 200, result
        except Exception as exc:
            # An unexpected relay failure:  whether GitHub was reached is
            # unknowable here, so hold the key as unknown (fail closed) and
            # still record the audit row in finally.
            try:
                self._release(key, "unknown")
            except (OSError, OutcomeStoreError):
                # The release's own durable write failed:  no stored row is
                # known to carry the minted corr, so the audit row in the
                # finally below must not advertise it.  The release failure
                # itself still propagates exactly as before.
                attempt_corr = None
                raise
            # Bind the audit row only to a corr the stored row actually
            # carries:  when begin() itself failed before writing a row, the
            # minted corr never landed anywhere and would dangle.
            attempt_corr = self.outcomes.corr_of(key) if key is not None else None
            outcome = "error"
            detail = {"exception": type(exc).__name__}
            return 500, {"error": "internal relay error"}
        finally:
            # Successful reads leave no row (SeatPolicy.audits);  every denial,
            # error and write still records exactly one.
            if SeatPolicy.audits(action, outcome):
                if key is not None and attempt_corr:
                    # The correlation id captured at begin() binds this audit
                    # entry to this attempt's outcome row exactly, even if a
                    # later request has already replaced the row;  no argument
                    # content is derivable from it.
                    detail = {**detail, "outcome_corr": attempt_corr}
                self.audit.record(seat, action, repo, outcome, detail)

    def _moved_repository(self, seat: str, repo_id: int) -> str | None:
        """Where a moved repository lives now, named only when this seat's policy allows it.

        Read as the seat's own App (src: AppTokenMinter.moved_repository_name).  A slug outside
        the seat's repo allowlist is withheld:  the seat learns the repo moved, not a name its
        policy does not cover.  Any failure answers None.
        """
        if self.app_minter is None or not hasattr(self.app_minter, "moved_repository_name"):
            return None
        try:
            app_slug, _ = self.policy.app_identity(seat)
            if not app_slug:
                return None
            name = self.app_minter.moved_repository_name(app_slug, repo_id)
            if not name or find_secret(name) or not self.policy.repo_allowed(seat, name):
                return None
            return name
        except Exception:
            return None

    def _release(self, key: str | None, state: str) -> None:
        if key is not None:
            self.outcomes.finish(key, state)

    def _record_pr_owner(
        self, seat: str, canonical: dict, number: int, corr: str | None, confirmed: bool
    ) -> bool | None:
        """Best-effort ownership-index write;  the audit row stays the source of truth.

        Returns True or False when a store is wired, None when none is.
        """
        if self.pr_owners is None:
            return None
        try:
            self.pr_owners.record(
                seat=seat,
                repo=canonical["repo"],
                number=number,
                head=canonical["head"],
                base=canonical["base"],
                corr=corr,
                confirmed=confirmed,
                origin_session_claim=canonical.get("origin_session_claim"),
                origin_letter_claim=canonical.get("origin_letter_claim"),
                origin_generation_claim=canonical.get("origin_generation_claim"),
            )
        except Exception:
            # A failed index write never fails or reclassifies the creation:
            # the PR exists at this point, the outcome row already finished
            # "ok", and this attempt's audit row still binds it.  Any
            # unexpected store failure (not just OSError/ValueError) degrades
            # to owner_recorded False;  `python3 -m git_relay.pr_owners
            # rebuild` repairs the index from the audit log.
            return False
        return True

    def _dispatch(
        self,
        seat: str,
        action: str,
        args: dict,
        repo: str,
        caller_token: str | None = None,
        corr: str | None = None,
    ) -> dict:
        # handle() has already scanned, shape-validated, and authorized this
        # call (allowlist + hourly quota, once);  dispatch only executes.
        if action == service_read.ACTION:
            return service_read.dispatch(self, seat, args)
        if action in SeatPolicy.WORKFLOW_ACTIONS:
            return self._dispatch_workflow(seat, action, args, repo, caller_token=caller_token)
        if action in SeatPolicy.APP_ACTIONS:
            return self._dispatch_app(seat, action, args, repo)
        if action == "open_pr":
            # The creation fence was already written by handle(), before
            # this call:  the owner index is fenced durably before the
            # upstream side effect, and no new origin is obtained on retry.
            # The correlation id is the one the idempotency gate wrote, so
            # the refusal below resolves exactly that fence.
            try:
                return self.client.open_pr(
                    repo,
                    base=_text(args.get("base"), "base"),
                    head=_text(args.get("head"), "head"),
                    title=_clean_text(args.get("title"), "title"),
                    body=_clean_text(args.get("body"), "body"),
                    **({"draft": args["draft"]} if "draft" in args else {}),
                )
            except RelayGitHubError as exc:
                # Only the client's validated, un-followed repository move
                # is additional no-create evidence. Generic redirects and
                # post-create readback failures remain fenced as uncertain.
                repository_moved = (
                    exc.code == "repository_moved"
                    and 300 <= exc.status < 400
                    and exc.moved_repository_id is not None
                )
                if self.pr_owners is not None and (
                    repository_moved or 400 <= exc.status < 500 and exc.status != 408
                ):
                    try:
                        self.pr_owners.refuse_creation(corr)
                    except (OSError, ValueError):
                        # Retain the durable fence if cleanup fails. Refusal is
                        # still a refusal; never issue a second upstream write.
                        pass
                raise
        # Every non-open_pr write is an APP_ACTION and never reaches here.
        raise ValueError(f"unknown owner-token action {action!r}")


# Idempotent state setters must reach GitHub again after a verified success: A->B->A
# is a new desired state, not a replay of A.  Keep the same key and all pending/unknown
# holds; only OutcomeStore.begin atomically replaces a known-success row.
_REAPPLY_STATE_ACTIONS = frozenset(
    {
        "wf_repo_variable_update",
        "wf_repo_variable_delete",
        "wf_secret_put_org_actions",
        "wf_secret_delete_org_actions",
        "wf_secret_repos_set_org_actions",
        "wf_secret_put_org_dependabot",
        "wf_secret_delete_org_dependabot",
        "wf_secret_repos_set_org_dependabot",
        "wf_secret_put_org_codespaces",
        "wf_secret_delete_org_codespaces",
        "wf_secret_repos_set_org_codespaces",
        "wf_secret_put_repo_actions",
        "wf_secret_delete_repo_actions",
        "wf_secret_put_repo_dependabot",
        "wf_secret_delete_repo_dependabot",
        "wf_secret_put_repo_codespaces",
        "wf_secret_delete_repo_codespaces",
        "wf_secret_put_env_actions",
        "wf_secret_delete_env_actions",
        "wf_hook_create",
        "wf_hook_update",
        "wf_hook_delete",
        "wf_hook_ping",
        "wf_hook_test",
        "wf_hook_redeliver",
        "wf_org_hook_create",
        "wf_org_hook_update",
        "wf_org_hook_delete",
        "wf_org_hook_ping",
        "wf_deploy_key_add",
        "wf_deploy_key_delete",
        "wf_environment_put",
        "wf_environment_delete",
        "wf_env_branch_policy_create",
        "wf_env_branch_policy_delete",
        "wf_env_variable_set",
        "wf_env_variable_delete",
        "wf_org_variable_set",
        "wf_org_variable_delete",
        "wf_org_variable_repos_set",
        "wf_labels_add",
        "wf_label_update",
        "wf_issue_dependency_add",
        "wf_issue_dependency_remove",
        "wf_issue_milestone_set",
        "wf_issue_update",
        "wf_review_update",
        "wf_commit_comment_update",
        "wf_check_update",
        "wf_milestone_update",
        "wf_labels_set",
        "wf_reviewers_request",
        "wf_reviewers_remove",
        "wf_release_notes_preview",
        "wf_release_update",
        "wf_issue_state_set",
        "wf_issue_comment_update",
        "wf_review_comment_update",
        "wf_assignees_add",
        "wf_assignees_remove",
        "wf_issue_lock",
        "wf_issue_unlock",
        "wf_workflow_enable",
        "wf_workflow_disable",
        "wf_pr_close",
        "wf_pr_draft",
        "wf_pr_edit",
        "wf_pr_ready",
        "wf_pr_reopen",
        "wf_review_thread_resolve",
        "wf_review_thread_unresolve",
        "wf_automerge_enable",
        "wf_automerge_disable",
        "wf_release_asset_update",
    }
)

_WORKFLOW_AUDIT_SCALARS = (
    "blocker_number",
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
    "thread_id",
    "review_id",
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
    "asset_id",
    "artifact_id",
    "release_id",
    "offset",
    "length",
    "method",
    "path",
)


def _denied_audit_detail(action: str, canonical: dict, exc: Exception) -> dict:
    """A denied call's audit detail:  the refusal reason plus, for workflow actions,
    the same bounded scalar allowlist a successful call audits.  The message, edits
    and file content are never logged - they can carry a private payload.  This is
    what lets a guarded transaction bind a denied probe call to its one-time proof
    id through a structured, non-secret field (the probe path)."""
    detail: dict = {"reason": str(exc)}
    if action in SeatPolicy.WORKFLOW_ACTIONS and isinstance(canonical, dict):
        for key in _WORKFLOW_AUDIT_SCALARS:
            value = canonical.get(key)
            if isinstance(value, (int, str)) and not isinstance(value, bool):
                detail[key] = value
    return detail


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
    for kind in ("commit", "tree", "blob"):
        nested = result.get(kind)
        if (
            isinstance(nested, dict)
            and isinstance(nested.get("sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", nested["sha"])
        ):
            detail["result_" + kind + "_sha"] = nested["sha"]
    return detail


def _is_workflow_read(self, action: str) -> bool:
    """A wf_ verb that changes nothing upstream needs no idempotency key and holds no outcome."""
    if action == service_read.ACTION:
        return True
    if action not in SeatPolicy.WORKFLOW_ACTIONS:
        return False
    from .workflow_verbs import SPECS

    spec = SPECS.get(action)
    return spec is not None and not spec[1]


def _dispatch_workflow(
    self, seat: str, action: str, args: dict, repo: str, caller_token: str | None = None
) -> dict:
    # Imported here: workflow_verbs imports this module.
    from .app_tokens import AppTokenError, InstallationReadError
    from .workflow_verbs import CeilingDenied, Unknown, WorkflowVerbs

    if self.app_minter is None:
        raise ServerUnavailable("app-token minting is not configured on this relay")
    if action == "wf_installation_metadata":
        app_slug, ceiling = self.policy.app_identity(seat)
        if _perm_within_ceiling({"metadata": "read"}, ceiling):
            raise PolicyDenied("seat permission ceiling insufficient")
        if app_slug != seat:
            raise PolicyDenied("own App required")
        try:
            return self.app_minter.installation_metadata(app_slug, repo)
        except InstallationReadError as exc:
            raise RelayGitHubError(
                exc.status, "installation metadata read failed", retry_after=exc.retry_after
            ) from None
        except AppTokenError as exc:
            if missing_app_credential(exc):
                raise
            raise ServerUnavailable("own installation metadata unavailable") from None
    try:
        # handle() has already run policy.authorize for this exact seat, action and repo
        # (and counted its quota once); for_action then only checks the App ceiling and
        # mints the least permission in SPECS.
        handle = WorkflowVerbs.for_action(
            self.client,
            self.policy,
            self.app_minter,
            seat,
            action,
            repo,
            preauthorized=True,
            args=args,
            caller_token=caller_token,
        )
        return handle.run(action, args)
    except AppTokenError:
        raise
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
    if action == "create_repo":
        # Not the seat mint ceiling.  organization_administration and administration
        # writes stay rejected by token_for; this path mints the fixed pair and
        # revokes it before returning.
        if app_slug != seat:
            raise PolicyDenied("own App required")
        owner, _name = repo.split("/", 1)
        account = SeatPolicy.create_repo_account(owner)
        if account is None:
            raise ValueError("repository owner is not allowed")
        app_token = None
        try:
            app_token = self.app_minter.token_for_namespace(app_slug, account[0], account[1])
            try:
                return self.client.create_repo(
                    repo,
                    args["visibility"],
                    description=args.get("description"),
                    license_template=args.get("license_template"),
                    bearer=app_token,
                    kind=account[1],
                )
            except RelayGitHubError as exc:
                if exc.code == "create_repo_pre_read":
                    raise PreReadUnavailable() from None
                raise
        finally:
            if app_token is not None:
                self.app_minter.revoke_installation_token(app_token)
    required = dict(ACTION_REQUIRED_PERMISSIONS[action])
    if action == "workflow_dispatch" and args.get("expected_head_sha"):
        required["contents"] = "read"
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
            **(
                {"expected_head_sha": args["expected_head_sha"]}
                if args.get("expected_head_sha")
                else {}
            ),
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


class _OperatorBootstrapRelay:
    """Listener-bound capability facade:  passes operator_bootstrap=True to
    the wrapped Relay so the operator listener alone admits bootstrap
    resolution.  Exists only where make_handler opted in;  the public
    listener and direct callers get Relay.handle's default (off)."""

    __slots__ = ("_relay",)

    def __init__(self, relay: Relay):
        self._relay = relay

    def handle(self, token, action, args, proof=None):
        return self._relay.handle(token, action, args, proof=proof, operator_bootstrap=True)


def make_handler(relay: Relay, operator_bootstrap: bool = False, service_read_only: bool = False):
    # Bootstrap admission is bound to the LISTENER, never to the peer
    # address:  the tunnel and any local client reach the public listener
    # alike, so remote_addr cannot distinguish an operator from the edge.
    # The operator listener is a separate loopback-only socket the tunnel
    # ingress never targets (main()).  Only that listener opts the relay
    # into bootstrap admission;  everywhere else Relay.handle's default
    # keeps it off.
    if operator_bootstrap:
        relay = _OperatorBootstrapRelay(relay)

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
            elif git_read.is_git_path(self.path):
                if operator_bootstrap or service_read_only:
                    # The operator listener serves the v1 API only:  Git
                    # smart-HTTP needs the full Relay interface and is not
                    # part of bootstrap admission, so it is refused outright
                    # rather than delegated through the capability facade.
                    self._send(404, {"error": "not found"})
                    return
                git_read.handle(relay, self, "GET")
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if service_read_only and self.path != "/v1/service_read":
                self._send(404, {"error": "not found"})
                return
            if git_read.is_git_path(self.path):
                if operator_bootstrap:
                    self._send(404, {"error": "not found"})
                    return
                git_read.handle(relay, self, "POST")
                return
            parts = self.path.strip("/").split("/")
            if len(parts) != 2 or parts[0] != "v1":
                self._send(404, {"error": "not found"})
                return
            action = parts[1]
            auth = self.headers.get("Authorization") or ""
            bearer_prefix = "Bearer "
            token = auth[len(bearer_prefix) :].strip() if auth.startswith(bearer_prefix) else ""
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                self._send(400, {"error": "bad Content-Length"})
                return
            cap = (
                MAX_BINARY_REQUEST_BYTES
                if action in BINARY_REQUEST_ACTIONS
                else MAX_FILE_REQUEST_BYTES
                if action in FILE_REQUEST_ACTIONS
                else MAX_LARGE_TEXT_REQUEST_BYTES
                if action in LARGE_TEXT_REQUEST_ACTIONS
                else MAX_SECRET_PUT_REQUEST_BYTES
                if action in SECRET_PUT_REQUEST_ACTIONS
                else MAX_REQUEST_BYTES
            )
            if length < 1 or length > cap:
                self._send(400, {"error": "request body missing or over the action byte bound"})
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
            # The request binding for assertion credentials:  the action and
            # the SHA-256 of the exact raw body bytes (git_relay/assertion.py).
            # Bearer and static tokens ignore the proof.
            proof = {"action": action, "body_sha256": hashlib.sha256(raw).hexdigest()}
            status, obj = relay.handle(token, action, args, proof=proof)
            self._send(status, obj)

    return Handler


DEFAULT_AUDIT_PATH = "git-relay-audit.jsonl"
DEFAULT_OUTCOMES_PATH = "git-relay-outcomes.json"
DEFAULT_PR_OWNERS_PATH = "git-relay-pr-owners.jsonl"
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
    through the Worker's verify route.  The route key is a scoped Infisical read
    like every other credential here;  a configured URL with an unreadable or
    short key is a startup error, never silent unauthenticated operation.

    The verify key lives in Fleet-OPS /tools (the same folder the Worker
    resolves it from), NOT in /github-apps with the App credentials:
    GIT_RELAY_VERIFY_KEY_PATH overrides the folder, default "/tools".
    """
    verify_url = os.environ.get("GIT_RELAY_VERIFY_URL", "").strip()
    if not verify_url:
        return None
    from .app_tokens import AppTokenError, InfisicalSource
    from .shared_bearer import SharedBearerVerifier

    raw_path = os.environ.get("GIT_RELAY_VERIFY_KEY_PATH")
    try:
        return SharedBearerVerifier(
            url=verify_url,
            key_name=os.environ.get("GIT_RELAY_VERIFY_KEY_NAME", "GR_BEARER_VERIFY_KEY"),
            # Preserve the existing file selector, without claiming its grant covers /tools.
            # The canonical verifier requires exactly this one folder; the legacy two-folder
            # grant cannot satisfy it.  Activation needs the verified dedicated tools binding.
            key_source=InfisicalSource(
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


def _build_assertion_verifier():
    """The hosted-OAuth assertion verifier, or None when not configured.

    Set GIT_RELAY_OAUTH_ASSERT=1 to accept Worker-minted per-call assertions
    (git_relay/assertion.py) for seats flagged oauth_assert in the seat
    policy.  GIT_RELAY_ASSERT_VERIFY_URL is the Worker's signed
    /internal/gr-assert-verify route (https, required when the switch is on).
    The channel key is the SAME bearer verify key as the shared-bearer route
    (GIT_RELAY_VERIFY_KEY_NAME, default GR_BEARER_VERIFY_KEY, from the same
    /tools source):  the relay holds no mint-capable material - the mint key
    lives in the dedicated runtime /gr-assert-signing folder.  A configured switch with
    an unreadable or short key is a startup error, never silent
    unauthenticated operation.
    """
    if os.environ.get("GIT_RELAY_OAUTH_ASSERT", "").strip() != "1":
        return None
    verify_url = os.environ.get("GIT_RELAY_ASSERT_VERIFY_URL", "").strip()
    if not verify_url:
        raise SystemExit(
            "git-relay config: GIT_RELAY_OAUTH_ASSERT=1 needs GIT_RELAY_ASSERT_VERIFY_URL"
        )
    from .app_tokens import AppTokenError, InfisicalSource
    from .assertion import GrAssertionVerifier

    raw_path = os.environ.get("GIT_RELAY_VERIFY_KEY_PATH")
    try:
        return GrAssertionVerifier(
            url=verify_url,
            key_name=os.environ.get("GIT_RELAY_VERIFY_KEY_NAME", "GR_BEARER_VERIFY_KEY"),
            # Same existing file selector and dedicated-binding activation gate as the bearer
            # verify key above; this source change neither widens nor proves a grant.
            key_source=InfisicalSource(
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


OUTCOMES_CAPACITY_KEY = "GIT_RELAY_OUTCOMES_MAX_ENTRIES"
MIN_PRODUCTION_OUTCOMES = 5_000


def _outcome_capacity_from_source(source) -> int:
    """One folder-proven startup setting; no environment override or fallback."""
    value = source.verified_secret(OUTCOMES_CAPACITY_KEY)
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{3,4}", value):
        raise ValueError(
            f"{OUTCOMES_CAPACITY_KEY} must be an integer from "
            f"{MIN_PRODUCTION_OUTCOMES} to {MAX_CAPACITY}"
        )
    capacity = int(value)
    if not MIN_PRODUCTION_OUTCOMES <= capacity <= MAX_CAPACITY:
        raise ValueError(
            f"{OUTCOMES_CAPACITY_KEY} must be an integer from "
            f"{MIN_PRODUCTION_OUTCOMES} to {MAX_CAPACITY}"
        )
    return capacity


def from_env() -> Relay:
    try:
        policy = SeatPolicy(
            seats_json_from_env(),
            rate_per_hour=int(
                os.environ.get("GIT_RELAY_RATE_PER_HOUR", str(DEFAULT_RATE_PER_HOUR))
            ),
            verifier=_build_shared_verifier(),
            assertion_verifier=_build_assertion_verifier(),
            write_rate_per_hour=int(
                os.environ.get("GIT_RELAY_WRITE_RATE_PER_HOUR", str(DEFAULT_WRITE_RATE_PER_HOUR))
            ),
        )
    except (PolicyError, ValueError) as exc:
        if isinstance(exc, PolicyError) and (
            "GIT_RELAY_SEATS_FILE" in os.environ or "GIT_RELAY_SEATS_CREDENTIAL" in os.environ
        ):
            raise SystemExit("git-relay config: file seat policy invalid or unreadable") from None
        raise SystemExit(f"git-relay config: {exc}") from exc
    app_source = None
    outcome_capacity = None
    if os.environ.get("GIT_RELAY_APP_MINT", "1") != "0":
        # The existing source uses the canonical bounded reader with its own token file.
        from .app_tokens import AppTokenError, AppTokenMinter, InfisicalSource

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
            app_source = InfisicalSource(
                path=(raw_secrets_path or "/github-apps").strip(),
                credential_file=_credential_file_for(
                    "the App-credentials source",
                    "GIT_RELAY_APPS_CREDENTIAL_FILE",
                    "git-relay-apps-token",
                ),
            )
            # Read before any state adoption/load.  A missing or invalid policy
            # never activates a guessed capacity or alters the existing ledger.
            outcome_capacity = _outcome_capacity_from_source(app_source)
        except (AppTokenError, ValueError) as exc:
            raise SystemExit(f"git-relay config: {exc}") from exc
    client = GitHubClient(_owner_token_from_cli())
    app_minter = None
    if app_source is not None:
        app_minter = AppTokenMinter(
            app_source,
            lambda method, path, bearer, payload=None: client._call(
                method, path, payload, bearer=bearer
            ),
        )
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
    owners_path = os.environ.get("GIT_RELAY_PR_OWNERS_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(audit_path)), DEFAULT_PR_OWNERS_PATH
    )
    audit = AuditLog(audit_path)
    # App-disabled legacy startup keeps the auth-free library default.
    outcomes = OutcomeStore(outcomes_path, max_entries=outcome_capacity)
    return Relay(
        policy,
        client,
        audit,
        app_minter=app_minter,
        service_read_token=_service_read_token_from_file(),
        service_read_repos=_service_read_repos_from_env(),
        outcomes=outcomes,
        workflow_verbs_enabled=os.environ.get("GIT_RELAY_ENABLE_WORKFLOW_VERBS") == "1",
        typed_disarm_enabled=os.environ.get("GIT_RELAY_ENABLE_TYPED_DISARM") == "1",
        pr_owners=PROwnerStore(owners_path),
    )


def _credential_file_for(scope: str, env_var: str, default_name: str) -> str | None:
    """The protected Infisical-token file one scoped source reads.

    Production resolves it from systemd's CREDENTIALS_DIRECTORY (the unit's
    LoadCredential keys are the default file names) or an explicit
    GIT_RELAY_*_CREDENTIAL_FILE override;  a set-but-empty override is a
    config error like any other.  With neither, startup REFUSES - the
    process environment is never a secret source.  No co-located secret
    (seat tokens or another scope's token) supplies a missing binding.  Tests and local
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
    """Compatibility-named owner PAT factory, read once through the shared named reader
    at its fixed folder (/git-relay-owner): the canonical reader proves the
    value came from that exact folder before answering.  Never the process
    environment - an ambient GITHUB_TOKEN is IGNORED, and there is no other
    fallback source (owner direction 2026-10-21 + the /git-relay-owner
    custody move 2026-10-25).  The relay refuses to start without it,
    exactly as it refused to start with an empty GITHUB_TOKEN before,
    and it refuses to start without a credential file binding for the
    folder scope (_credential_file_for):  no ambient inheritance in
    production.
    """
    from .app_tokens import AppTokenError, InfisicalSource

    source = InfisicalSource(
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


def _report_pr_owner_binding(relay):
    # Startup-only, nonsecret activation receipt. Bound by journald to this
    # service invocation/PID; not an HTTP endpoint or an ownership backfill.
    store = relay.pr_owners
    owners_parent = os.path.dirname(os.path.abspath(store._path))
    print(
        json.dumps(
            {
                "event": "pr_owners_bound_v1",
                "pid": os.getpid(),
                "module": type(store).__module__,
                "type": type(store).__name__,
                "path": os.path.realpath(store._path),
                "absolute": os.path.isabs(store._path),
                "parent_writable": os.path.isdir(owners_parent)
                and os.access(owners_parent, os.W_OK),
                "load_error": store.load_error,
            }
        ),
        flush=True,
    )


def _service_read_repos_from_env() -> frozenset:
    """The service-read repo allowlist:  GIT_RELAY_SERVICE_READ_REPOS as a
    comma list of owner/name;  empty means the lane answers 503 to all."""
    out = set()
    for part in os.environ.get("GIT_RELAY_SERVICE_READ_REPOS", "").split(","):
        part = part.strip()
        if part:
            if not service_read.LANE_REPO.fullmatch(part):
                raise SystemExit("git-relay config: bad repo in GIT_RELAY_SERVICE_READ_REPOS")
            out.add(part)
    return frozenset(out)


def _service_read_token_from_file() -> str | None:
    """The service lane's dedicated bearer, from its own credential file
    (LoadCredential git-relay-service-read or GIT_RELAY_SERVICE_READ_CREDENTIAL_FILE);
    never the process environment.  None when the lane is not configured."""
    if not _service_read_repos_from_env():
        return None
    path = _credential_file_for(
        "service-read", "GIT_RELAY_SERVICE_READ_CREDENTIAL_FILE", "git-relay-service-read"
    )
    try:
        value = open(path, "r", encoding="utf-8").read().strip()
    except OSError as exc:
        raise SystemExit("git-relay config: service-read credential file unreadable") from exc
    if len(value) < 32:
        raise SystemExit("git-relay config: service-read credential is too short")
    return value


def _service_read_listener_address(env):
    """Optional private bridge socket. Never wildcard, public or bootstrap-capable."""
    import ipaddress

    raw = env.get("GIT_RELAY_SERVICE_READ_BIND", "")
    if not raw:
        return None
    try:
        address = ipaddress.ip_address(raw)
        allowed = address.is_loopback or any(
            address in ipaddress.ip_network(net)
            for net in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
        port = int(env.get("GIT_RELAY_SERVICE_READ_PORT", "8473"))
        if not allowed or not 1024 <= port <= 65535:
            raise ValueError()
    except (ValueError, TypeError):
        raise SystemExit("git-relay config: invalid private service-read listener") from None
    return str(address), port


def main() -> None:
    relay = from_env()
    _report_pr_owner_binding(relay)
    host = os.environ.get("GIT_RELAY_HOST", DEFAULT_HOST)
    port = int(os.environ.get("GIT_RELAY_PORT", str(DEFAULT_PORT)))
    server = BoundedThreadingHTTPServer((host, port), make_handler(relay))
    print(f"git-relay listening on {host}:{port}")
    operator_port = os.environ.get("GIT_RELAY_OPERATOR_PORT")
    operator_server = None
    if operator_port:
        # The operator listener carries the bootstrap capability on its own
        # socket, bound to 127.0.0.1 explicitly - never to GIT_RELAY_HOST.
        # The tunnel ingress never targets it (config-grounded at design
        # time; re-verify at activation), so the public forwarded path
        # cannot reach bootstrap admission.  Unset, the relay serves the
        # public listener only and bootstrap admission is unavailable
        # everywhere:  first-seat enrollment then has no admitted path.
        # The operator listener reduces public-edge exposure only - any
        # local process can still spend its budgets, so it is not per-user
        # authentication.
        operator_server = BoundedThreadingHTTPServer(
            ("127.0.0.1", int(operator_port)),
            make_handler(relay, operator_bootstrap=True),
        )
        threading.Thread(target=operator_server.serve_forever, daemon=True).start()
        print(f"git-relay operator listener on 127.0.0.1:{operator_port}")
    service_server = None
    service_address = _service_read_listener_address(os.environ)
    if service_address:
        if not relay.service_read_token or not relay.service_read_repos:
            raise SystemExit(
                "git-relay config: service-read listener needs dedicated auth and repos"
            )
        service_server = BoundedThreadingHTTPServer(
            service_address, make_handler(relay, service_read_only=True)
        )
        threading.Thread(target=service_server.serve_forever, daemon=True).start()
        print("git-relay private service-read listener active")
    try:
        server.serve_forever()
    finally:
        if service_server is not None:
            service_server.shutdown()
            service_server.server_close()
        if operator_server is not None:
            operator_server.shutdown()
            operator_server.server_close()


if __name__ == "__main__":
    main()
