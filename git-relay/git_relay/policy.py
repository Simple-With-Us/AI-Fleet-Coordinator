"""Seat authentication and per-seat policy for the PR relay.

Seats never hold the GitHub token.  Each seat authenticates with its own
bearer token (provisioned out of band) and may call only the actions and
repositories its policy entry names.  Everything not named is denied.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
import time

from .app_tokens import mint_permission_allowed
from .shared_bearer import SharedBearerVerifier


class PolicyError(ValueError):
    """A seat config or request is invalid."""


# Closed, non-secret causes for a credential the relay will not treat as a seat.
# unauthenticated: empty, malformed, or not a credential this relay checks.
# seat_not_in_policy: the bearer or assertion names an identity no shared seat claims.
# bearer_rejected: the identity is configured and the checker refused the credential.
# verifier_unavailable: the identity is configured and the checker could not be asked.
AUTH_FAILURE_REASONS = frozenset(
    {
        "unauthenticated",
        "seat_not_in_policy",
        "bearer_rejected",
        "verifier_unavailable",
    }
)

# First-seat bootstrap probe bounds.  A seat_not_in_policy answer may be
# probed for proof purposes, but every probe costs a blocking Worker verify,
# so three bounds apply per window:  a GLOBAL verify count, a distinct
# IDENTITY CARDINALITY, and a per-identity count.  The global and cardinality
# bounds come first:  an unbounded set of parsed identities must not multiply
# the budget by rotating names.  A refused probe behaves exactly like an
# unverifiable bearer - the ordinary bounded denial row, no verifier call.
BOOTSTRAP_VERIFY_GLOBAL = 6
BOOTSTRAP_VERIFY_IDENTITIES = 8
BOOTSTRAP_VERIFY_PER_IDENTITY = 2
BOOTSTRAP_VERIFY_WINDOW_S = 60.0


def auth_failure_response(reason: str) -> tuple[int, dict]:
    """HTTP answer for authenticate_with_reason's failure cause. No credential material."""
    if reason not in AUTH_FAILURE_REASONS:
        reason = "unauthenticated"
    if reason == "verifier_unavailable":
        return 503, {"error": "verifier_unavailable", "retryable": True}
    return 401, {"error": reason}


# Per-seat calls an hour the gateway admits before it refuses.  This is a seat-token blast-radius bound, not a
# throttle for routine work:  the repo and action allowlists are the access control, and GitHub's own
# 429/Retry-After is already returned to the caller as a retryable refusal (server.py), so GitHub's limits remain
# the real ceiling.  3600 is one call a second on average for a full hour.  GIT_RELAY_RATE_PER_HOUR overrides it.
DEFAULT_RATE_PER_HOUR = 3600
# Per-seat state-changing calls an hour, a second rolling bound on top of the one above.  A stolen seat token
# can read a lot but must not be able to write without limit, so every authorized state-changing dispatch also
# draws from this bucket.  It counts LOGICAL writes (one authorized dispatch each), not GitHub requests:  one
# write verb can issue more than one mutating request upstream, and GitHub's own content-creation guideline
# (about 80 a minute and 500 an hour, lower on some endpoints, see docs.github.com/en/rest/using-the-rest-api/
# rate-limits-for-the-rest-api) is a provider guideline, not what this number measures.  500 is a blast-radius
# choice, generous for routine work (one write every 7 seconds for a full hour).  GIT_RELAY_WRITE_RATE_PER_HOUR
# overrides it.  Reads, and mint_token (a credential issue, not a repository change), draw only from the
# all-calls bucket.
DEFAULT_WRITE_RATE_PER_HOUR = 500
RATE_LIMIT_DENIAL = "hourly rate limit reached for this seat"
WRITE_RATE_LIMIT_DENIAL = "hourly write limit reached for this seat"
RATE_LIMIT_DENIALS = (RATE_LIMIT_DENIAL, WRITE_RATE_LIMIT_DENIAL)


class SeatPolicy:
    """Per-seat allowlists parsed from GIT_RELAY_SEATS_JSON.

    Shape:
        {
          "swu-echo": {
            "token": "<bearer>",
            "actions": ["open_pr", "edit_pr", "reply_thread", "comment_pr"],
            "repos": ["Simple-With-Us/AI-Fleet-Coordinator", "Simple-With-Us/*"]
          }
        }

    Auth modes (per seat):
        "token"  (default) the seat presents its static provisioned bearer;
                 "token" is required.
        "shared" the seat presents its admin-minted ST-MCP bearer, resolved
                 to its identity through the shared verifier
                 (shared_bearer.SharedBearerVerifier);  "token" must be
                 absent, and "identity" may name the identity the verifier
                 must return (default: the seat name itself).  A shared seat
                 is unreachable when no verifier is configured.
    """

    # Only open_pr is authored as the owner (owner direction 2026-10-21:  the
    # PR-only token makes PRs and does what the bot tokens cannot).  Every
    # other write - edits, replies and comments included - runs as the seat's
    # own GitHub App via a relay-minted token.
    VALID_ACTIONS = ("open_pr",)
    APP_ACTIONS = (
        "mint_token",
        # Read-only fetch and clone over the relay's /git/ route (git_read.py).  The token stays in the relay.
        "git_read",
        "merge_pr",
        "workflow_dispatch",
        "resolve_thread",
        "edit_pr",
        "reply_thread",
        "comment_pr",
    )
    # Known but not implemented:  accepted in seat config, refused at dispatch
    # until their implementation and per-action policy land.
    REGISTERED_ACTIONS = (
        "pr_state",
        "pr_hold_state",
        "pr_automerge_disable",
        "create_release",
        "rerun_workflow",
        "cancel_workflow",
        "set_labels",
        "create_issue_comment",
        "request_reviewers",
        "set_branch_protection",
        "create_repo",
    )
    # Workflow verbs (workflow_verbs.SPECS): policy names a seat config may list, each
    # authorized once per call by WorkflowVerbs.for_action and minted at the least
    # permission in SPECS.  They need the seat's own App, like APP_ACTIONS.  HTTP
    # dispatch is gated by GIT_RELAY_ENABLE_WORKFLOW_VERBS=1 (server.py; default off):
    # with it unset or any other value the server refuses them as registered-not-wired.
    WORKFLOW_ACTIONS = (
        "wf_git_blob_binary_get",
        "wf_issue_events",
        "wf_issue_timeline",
        "wf_pr_commits_page",
        "wf_pr_review_get",
        "wf_release_latest",
        "wf_release_by_tag",
        "wf_commit_comment_create",
        "wf_commit_comment_update",
        "wf_commit_comment_delete",
        "wf_repo_variables",
        "wf_repo_variable_get",
        "wf_repo_variable_create",
        "wf_repo_variable_update",
        "wf_repo_variable_delete",
        "wf_workflow_get",
        "wf_artifact_get",
        "wf_artifacts_page",
        "wf_caches_page",
        "wf_cache_usage",
        "wf_run_attempt_get",
        "wf_run_attempt_jobs",
        "wf_run_pending_deployments",
        "wf_job_get",
        "wf_job_rerun",
        "wf_run_rerun_all",
        "wf_run_force_cancel",
        "wf_run_logs_delete",
        "wf_review_requests_add",
        "wf_review_requests_remove",
        "wf_review_requests_rerequest",
        "wf_commit_comments_page",
        "wf_commit_comment_get",
        "wf_pr_requested_reviewers",
        "wf_repo_tags",
        "wf_repo_languages",
        "wf_git_tree_create",
        "wf_git_commit_create",
        "wf_pr_list_page",
        "wf_compare_commits_page",
        "wf_pr_reviews_page",
        "wf_pr_comments_page",
        "wf_labels_add",
        "wf_label_update",
        "wf_issue_dependency_add",
        "wf_issue_dependency_remove",
        "wf_issue_parent",
        "wf_issue_subissues",
        "wf_issue_blocked_by",
        "wf_issue_blocking",
        "wf_check_runs_page",
        "wf_check_run_get",
        "wf_file_history",
        "wf_runs_page",
        "wf_workflows_page",
        "wf_pr_files_page",
        "wf_merge_queue_entry",
        "wf_merge_queue_enqueue",
        "wf_merge_queue_dequeue",
        "wf_git_blob_get",
        "wf_git_blob_create",
        "wf_pr_create",
        "wf_pr_edit",
        "wf_pr_close",
        "wf_git_commit_get",
        "wf_git_tree_get",
        "wf_commit_statuses",
        "wf_commit_status_create",
        "wf_review_thread_resolve",
        "wf_review_thread_unresolve",
        "wf_review_update",
        "wf_review_dismiss",
        "wf_review_pending_delete",
        "wf_review_comment_reply",
        "wf_check_create",
        "wf_check_update",
        "wf_deployments",
        "wf_deployment_get",
        "wf_deployment_statuses",
        "wf_deployment_create",
        "wf_deployment_status_create",
        "wf_deployment_delete",
        "wf_milestones",
        "wf_milestone_get",
        "wf_milestone_create",
        "wf_milestone_update",
        "wf_milestone_delete",
        "wf_labels_set",
        "wf_reviewers_request",
        "wf_reviewers_remove",
        "wf_release_create",
        "wf_release_update",
        "wf_environments",
        "wf_environment_get",
        "wf_file_rename",
        "wf_file_delete",
        "wf_issue_list",
        "wf_issue_get",
        "wf_issue_create",
        "wf_issue_update",
        "wf_issue_comment",
        "wf_issue_comment_update",
        "wf_issue_comment_delete",
        "wf_review_comment_update",
        "wf_review_comment_delete",
        "wf_release_list",
        "wf_release_get",
        "wf_release_delete",
        "wf_branch_update",
        "wf_branch_delete",
        "wf_tag_create",
        "wf_tag_get",
        "wf_tag_delete",
        "wf_pr_draft",
        "wf_pr_ready",
        "wf_pr_reopen",
        "wf_automerge_enable",
        "wf_automerge_disable",
        "wf_review_thread_comments",
        "wf_review_threads",
        "wf_repo_hooks",
        "wf_deploy_keys",
        "wf_org_installations",
        "wf_repo_list",
        "wf_pr_search",
        "wf_branch_rules",
        "wf_rulesets",
        "wf_assignees_add",
        "wf_assignees_remove",
        "wf_issue_lock",
        "wf_issue_unlock",
        "wf_label_create",
        "wf_label_delete",
        "wf_pr_get",
        "wf_pr_reviews",
        "wf_pr_comments",
        "wf_issue_comments",
        "wf_commit_get",
        "wf_compare",
        "wf_branch_get",
        "wf_repo_get",
        "wf_installation_metadata",
        "wf_labels",
        "wf_workflows",
        "wf_run_get",
        "wf_run_artifacts",
        "wf_workflow_enable",
        "wf_workflow_disable",
        "wf_run_delete",
        "wf_artifact_delete",
        "wf_release_assets",
        "wf_release_asset_get",
        "wf_release_asset_upload",
        "wf_release_asset_download",
        "wf_release_asset_upload_binary",
        "wf_release_asset_update",
        "wf_release_asset_delete",
        "wf_job_log",
        "wf_run_logs",
        "wf_artifact_download",
        "wf_review_submit",
        "wf_review_inline",
        "wf_branch_create",
        "wf_pr_list",
        "wf_pr_files",
        "wf_file_get",
        "wf_tree",
        "wf_branches",
        "wf_commits",
        "wf_code_search",
        "wf_runs",
        "wf_run_jobs",
        "wf_checks",
        "wf_update_branch",
        "wf_run_rerun",
        "wf_run_cancel",
        "wf_run_approve",
        "wf_file_put",
        "wf_files_put",
        "wf_file_patch",
        "wf_merge_guarded",
    )
    ALL_ACTIONS = VALID_ACTIONS + APP_ACTIONS + REGISTERED_ACTIONS + WORKFLOW_ACTIONS

    def __init__(
        self,
        seats_json: str,
        rate_per_hour: int = DEFAULT_RATE_PER_HOUR,
        verifier=None,
        write_rate_per_hour: int = DEFAULT_WRITE_RATE_PER_HOUR,
        assertion_verifier=None,
    ):
        if not seats_json:
            raise PolicyError("GIT_RELAY_SEATS_JSON is empty")
        try:
            raw = json.loads(seats_json)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"GIT_RELAY_SEATS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or not raw:
            raise PolicyError("GIT_RELAY_SEATS_JSON must be a non-empty object of seats")
        self._seats: dict[str, dict] = {}
        for seat, cfg in raw.items():
            if not isinstance(cfg, dict):
                raise PolicyError(f"seat {seat!r}: config must be an object")
            auth = str(cfg.get("auth") or "token")
            if auth not in ("token", "shared"):
                raise PolicyError(f'seat {seat!r}: auth must be "token" or "shared"')
            token = str(cfg.get("token") or "")
            if auth == "shared" and token:
                raise PolicyError(
                    f"seat {seat!r}: a shared-auth seat must not carry a static token"
                )
            if auth == "token" and len(token) < 16:
                raise PolicyError(f"seat {seat!r}: token missing or shorter than 16 characters")
            identity = str(cfg.get("identity") or seat)
            if auth == "shared" and not identity:
                raise PolicyError(f"seat {seat!r}: shared auth needs an identity")
            # oauth_assert lets the Worker's hosted-OAuth path in for this seat:
            # a short-lived HMAC assertion the Worker mints only after its own
            # grant checks (git_relay/assertion.py).  Default off:  a seat
            # without the flag refuses assertions exactly as before.
            oauth_assert = cfg.get("oauth_assert", False)
            if not isinstance(oauth_assert, bool):
                raise PolicyError(f"seat {seat!r}: oauth_assert must be true or absent")
            if oauth_assert and auth != "shared":
                raise PolicyError(f"seat {seat!r}: oauth_assert needs shared auth")
            actions = cfg.get("actions") or []
            repos = cfg.get("repos") or []
            unknown = [a for a in actions if a not in self.ALL_ACTIONS]
            if unknown:
                raise PolicyError(f"seat {seat!r}: unknown actions {unknown}")
            if not actions or not repos:
                raise PolicyError(f"seat {seat!r}: actions and repos must both be non-empty")
            app_slug = cfg.get("app_slug")
            mint_perms = cfg.get("mint_permissions") or {}
            if not isinstance(mint_perms, dict):
                raise PolicyError(f"seat {seat!r}: mint_permissions must be an object")
            # Inactive typed actions require the same own-App startup
            # contract, without adding them to live APP dispatch.
            typed_app_actions = ("pr_state", "pr_hold_state", "pr_automerge_disable")
            wants_app = any(
                a in self.APP_ACTIONS or a in self.WORKFLOW_ACTIONS or a in typed_app_actions
                for a in actions
            )
            if wants_app and not app_slug:
                raise PolicyError(f"seat {seat!r}: app actions need an app_slug")
            if app_slug is not None:
                if not isinstance(app_slug, str) or not app_slug.startswith("swu-"):
                    raise PolicyError(f"seat {seat!r}: app_slug must be a swu- app slug")
                # Q7: one bearer per bot identity, and a bot's Zulip bot and its
                # GitHub App are the SAME identity.  The seat key IS the bot, so
                # an App that is not the seat's own name is another bot's App:
                # refuse it at config load, before any bearer can authenticate,
                # so no App action can ever run as another bot.
                if app_slug != seat:
                    raise PolicyError(
                        f"seat {seat!r}: app_slug must be the seat's own bot identity "
                        f"(one bearer = one Zulip bot = one GitHub App); got {app_slug!r}"
                    )
                if not mint_perms or any(
                    not isinstance(k, str) or v not in ("read", "write")
                    for k, v in mint_perms.items()
                ):
                    raise PolicyError(f"seat {seat!r}: mint_permissions must be name: read|write")
                outside = [k for k, v in mint_perms.items() if not mint_permission_allowed(k, v)]
                if outside:
                    # Config load must refuse names the minter always rejects,
                    # so a bad seat entry cannot pass startup and fail every mint.
                    raise PolicyError(
                        f"seat {seat!r}: mint_permissions outside the mint ceiling {outside}"
                    )
            self._seats[seat] = {
                "auth": auth,
                "identity": identity,
                "oauth_assert": oauth_assert,
                "token_hash": hashlib.sha256(token.encode()).hexdigest() if token else None,
                "actions": set(actions),
                "repos": [str(r) for r in repos],
                "app_slug": app_slug,
                "mint_permissions": dict(mint_perms),
            }
        # Two seats may never claim the same verified identity:  resolution
        # would be order-dependent, and attribution is the point of the design.
        claimed = {}
        for seat, cfg in self._seats.items():
            if cfg["auth"] == "shared":
                other = claimed.get(cfg["identity"])
                if other is not None:
                    raise PolicyError(
                        f"seats {other!r} and {seat!r} both claim verified identity {cfg['identity']!r}"
                    )
                claimed[cfg["identity"]] = seat
        if rate_per_hour < 1:
            raise PolicyError("rate_per_hour must be at least 1")
        if write_rate_per_hour < 1:
            raise PolicyError("write_rate_per_hour must be at least 1")
        self._verifier = verifier
        self._assertion_verifier = assertion_verifier
        self._rate_per_hour = rate_per_hour
        self._write_rate_per_hour = write_rate_per_hour
        self._calls: dict[str, list[float]] = {}
        self._writes: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._bootstrap_calls: list[float] = []
        self._bootstrap_idents: dict[str, list[float]] = {}

    def authenticate(self, token: str, proof: dict | None = None) -> str | None:
        """Return the seat the presented credential resolves to, or None."""
        seat, _reason = self.authenticate_with_reason(token, proof)
        return seat

    def bootstrap_resolve(self, token: str, now: float | None = None) -> str | None:
        """The verifier-CONFIRMED identity of a bearer no configured seat claims, or None.

        Runs only after authenticate_with_reason answered seat_not_in_policy:
        the token already parsed as a recognizable bearer naming an identity
        no shared seat claims.  The returned identity is the verifier's own
        answer, never the bearer's claim;  an unverifiable bearer is None.
        Every call costs a blocking Worker verify, so the bootstrap bounds
        (global count, distinct identity cardinality, per-identity count)
        reserve their slot under the lock BEFORE the call and keep the
        reservation on failure:  a spray of one identity or of many names is
        self-limiting either way, and concurrent probes cannot overrun the
        budget.  Over budget returns None;  the caller then writes the
        ordinary bounded denial row and never reaches the verifier.
        """
        if self._verifier is None:
            return None
        named = self._verifier.parse_identity(token)
        if named is None:
            return None
        now = time.monotonic() if now is None else now
        with self._lock:
            recent = [t for t in self._bootstrap_calls if now - t < BOOTSTRAP_VERIFY_WINDOW_S]
            idents = {
                i: [t for t in ts if now - t < BOOTSTRAP_VERIFY_WINDOW_S]
                for i, ts in self._bootstrap_idents.items()
            }
            idents = {i: ts for i, ts in idents.items() if ts}
            if (
                len(recent) >= BOOTSTRAP_VERIFY_GLOBAL
                or (named not in idents and len(idents) >= BOOTSTRAP_VERIFY_IDENTITIES)
                or len(idents.get(named, [])) >= BOOTSTRAP_VERIFY_PER_IDENTITY
            ):
                self._bootstrap_calls, self._bootstrap_idents = recent, idents
                return None
            recent.append(now)
            idents.setdefault(named, []).append(now)
            self._bootstrap_calls, self._bootstrap_idents = recent, idents
        return self._verifier.resolve(token)

    def authenticate_with_reason(
        self, token: str, proof: dict | None = None
    ) -> tuple[str | None, str]:
        """(seat, "") or (None, a closed AUTH_FAILURE_REASONS cause).

        Static tokens compare constant-time over the seats that have one.
        With no static match, a shared bearer is resolved through ST-MCP and
        must land on a seat in "shared" mode whose configured identity the
        verifier confirmed. A recognizable bearer whose identity is not in
        the loaded policy is seat_not_in_policy and is not sent to the Worker.
        That is a real miss when the loaded file lags the reviewed source:
        ST-MCP accepts the bearer from its own catalog.

        `proof` carries {"action", "body_sha256"} of the exact request the
        token rides on; only the assertion family binds to it. Bearers and
        static tokens ignore it.
        """
        if not token:
            return None, "unauthenticated"
        candidate = hashlib.sha256(token.encode()).hexdigest()
        for seat, cfg in self._seats.items():
            if cfg["token_hash"] and hmac.compare_digest(candidate, cfg["token_hash"]):
                return seat, ""
        # Worker-minted OAuth assertions: the Worker verifies the HMAC, the
        # action/body binding, the live grant state and the one-use jti per
        # call (git_relay/assertion.py); the relay holds no mint-capable key.
        # An assertion-shaped token never falls through to another family.
        named_assertion = (
            self._assertion_verifier.parse_identity(token)
            if self._assertion_verifier is not None
            else None
        )
        if named_assertion is not None:
            # Refuse locally when no configured oauth_assert seat claims the
            # identity the token names, BEFORE the blocking remote verify.
            if not any(
                cfg["auth"] == "shared"
                and cfg["oauth_assert"]
                and cfg["identity"] == named_assertion
                for cfg in self._seats.values()
            ):
                return None, "seat_not_in_policy"
            resolved = self._assertion_verifier.resolve(token, proof)
            if resolved is None:
                return None, "bearer_rejected"
            identity, claims = resolved
            # Scope authorization is inside the signature: a grant approved
            # without zulip:write can mint assertions, but never for a write.
            if self.is_write(proof["action"]) and "zulip:write" not in claims.get("scopes", []):
                return None, "bearer_rejected"
            for seat, cfg in self._seats.items():
                if cfg["auth"] == "shared" and cfg["oauth_assert"] and cfg["identity"] == identity:
                    return seat, ""
            return None, "seat_not_in_policy"
        # Refuse locally when no configured shared seat claims the identity the
        # token names: otherwise every wrong guess costs a blocking Worker
        # verify before any per-seat rate limit can apply. A bearer ST-MCP
        # accepts can still miss here when the loaded policy file is behind
        # the reviewed source. That miss is seat_not_in_policy, not a bare
        # unauthenticated, and the Worker is not asked.
        named = self._named_bearer(token)
        if named is None:
            return None, "unauthenticated"
        if not any(
            cfg["auth"] == "shared" and cfg["identity"] == named for cfg in self._seats.values()
        ):
            return None, "seat_not_in_policy"
        if self._verifier is None:
            return None, "verifier_unavailable"
        identity, verdict = self._verified_identity(token)
        if identity is None:
            return None, verdict
        for seat, cfg in self._seats.items():
            if cfg["auth"] == "shared" and cfg["identity"] == identity:
                return seat, ""
        return None, "seat_not_in_policy"

    def _named_bearer(self, token: str) -> str | None:
        if self._verifier is not None:
            return self._verifier.parse_identity(token)
        return SharedBearerVerifier.parse_identity(token)

    def _verified_identity(self, token: str) -> tuple[str | None, str]:
        detail = getattr(self._verifier, "resolve_detail", None)
        if callable(detail):
            identity, reason = detail(token)
        else:
            identity = self._verifier.resolve(token)
            reason = "bearer_rejected"
        if identity:
            return identity, ""
        if reason == "verifier_unavailable":
            return None, "verifier_unavailable"
        return None, "bearer_rejected"

    def _repo_allowed(self, seat: str, repo: str) -> bool:
        for pattern in self._seats[seat]["repos"]:
            if pattern.endswith("/*"):
                if repo.startswith(pattern[:-1]):
                    return True
            elif repo == pattern:
                return True
        return False

    def repo_allowed(self, seat: str, repo: str) -> bool:
        """Public read of the seat allowlist: org-wide verbs filter results to it."""
        return self._repo_allowed(seat, repo)

    def repo_patterns(self, seat: str) -> tuple:
        """The seat's repository allowlist patterns (read-only copy)."""
        return tuple(self._seats[seat]["repos"])

    @classmethod
    def is_write(cls, action: str) -> bool:
        """Whether an authorized dispatch of this action can change something upstream.

        Read from structured metadata, never a name pattern:  wf_ verbs carry their own writes flag in
        workflow_verbs.SPECS, native actions are listed here.  mint_token issues a credential and changes no
        repository state.  Registered-but-unwired actions and any unclassified name count as writes, so the
        write bucket can only err toward counting.
        """
        if action in ("mint_token", "git_read"):
            return False
        if action in cls.WORKFLOW_ACTIONS:
            # Imported here:  workflow_verbs imports server, which imports this module.
            from .workflow_verbs import SPECS

            spec = SPECS.get(action)
            return True if spec is None else bool(spec[1])
        return True

    @classmethod
    def audits(cls, action: str, outcome: str) -> bool:
        """Whether a finished, authenticated call writes an audit row.

        Owner ruling 2026-11-06:  successful reads leave the audit log.  A call is skipped only when
        it ended "ok" and its action is a known read (is_write False).  Every failure, denial and
        write still records, and so does mint_token:  it changes no repository state but issues a
        credential, which the log must show.
        """
        if outcome != "ok" or action == "mint_token":
            return True
        return action not in cls.ALL_ACTIONS or cls.is_write(action)

    def _rate_ok(self, seat: str, now: float, write: bool = False) -> str | None:
        """Charge one call, and one write when `write`, or None and nothing charged when either bucket is full."""
        with self._lock:
            window = [t for t in self._calls.get(seat, []) if now - t < 3600]
            self._calls[seat] = window
            writes = [t for t in self._writes.get(seat, []) if now - t < 3600]
            self._writes[seat] = writes
            if len(window) >= self._rate_per_hour:
                return RATE_LIMIT_DENIAL
            if write and len(writes) >= self._write_rate_per_hour:
                return WRITE_RATE_LIMIT_DENIAL
            window.append(now)
            if write:
                writes.append(now)
            return None

    def authorize(self, seat: str, action: str, repo: str, now: float | None = None) -> str | None:
        """Return None when allowed, else a short denial reason (safe to return to the caller)."""
        if seat not in self._seats:
            return "unknown seat"
        if action not in self._seats[seat]["actions"]:
            return f"action {action!r} not allowed for this seat"
        if not self._repo_allowed(seat, repo):
            return f"repo {repo!r} not allowed for this seat"
        # One authorize is one dispatch:  the quota is charged here, once, before the idempotency gate, exactly as
        # the all-calls bucket always was, so a replayed request still consumes quota.
        return self._rate_ok(
            seat, now if now is not None else time.time(), write=self.is_write(action)
        )

    def app_identity(self, seat: str) -> tuple[str, dict]:
        """(app_slug, mintable permission ceiling) for a seat's App actions."""
        cfg = self._seats[seat]
        return cfg["app_slug"], dict(cfg["mint_permissions"])

    def bot_identities(self) -> dict[str, str]:
        """Every seat's GitHub App, for the one-bearer-one-bot audit.

        Config load refuses any App that is not its own seat's name, so this
        mapping can only ever pair a bearer with that bot's own App.
        """
        return {seat: cfg["app_slug"] for seat, cfg in self._seats.items() if cfg["app_slug"]}
