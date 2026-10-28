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

from .app_tokens import MINT_PERMISSION_CEILING


class PolicyError(ValueError):
    """A seat config or request is invalid."""


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
    # permission in SPECS.  They need the seat's own App, like APP_ACTIONS, but are not
    # wired into the HTTP dispatch: the server refuses them as registered-not-wired.
    WORKFLOW_ACTIONS = (
        "wf_merge_queue_entry",
        "wf_merge_queue_enqueue",
        "wf_merge_queue_dequeue",

        "wf_pr_draft",
        "wf_pr_ready",
        "wf_automerge_enable",
        "wf_automerge_disable",
        "wf_review_threads",
        "wf_workflow_enable",
        "wf_workflow_disable",
        "wf_run_delete",
        "wf_artifact_delete",
        "wf_release_assets",
        "wf_release_asset_get",
        "wf_release_asset_upload",
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
        "wf_merge_guarded",
    )
    ALL_ACTIONS = VALID_ACTIONS + APP_ACTIONS + REGISTERED_ACTIONS + WORKFLOW_ACTIONS

    def __init__(self, seats_json: str, rate_per_hour: int = 120, verifier=None):
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
                outside = [k for k in mint_perms if k not in MINT_PERMISSION_CEILING]
                if outside:
                    # Config load must refuse names the minter always rejects,
                    # so a bad seat entry cannot pass startup and fail every mint.
                    raise PolicyError(
                        f"seat {seat!r}: mint_permissions outside the mint ceiling {outside}"
                    )
            self._seats[seat] = {
                "auth": auth,
                "identity": identity,
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
        self._verifier = verifier
        self._rate_per_hour = rate_per_hour
        self._calls: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def authenticate(self, token: str) -> str | None:
        """Return the seat the presented credential resolves to, or None.

        Static tokens compare constant-time over the seats that have one.
        With no static match and a shared verifier configured, the bearer is
        resolved through ST-MCP;  it must land on a seat in "shared" mode
        whose configured identity the verifier confirmed.
        """
        if not token:
            return None
        candidate = hashlib.sha256(token.encode()).hexdigest()
        match = None
        for seat, cfg in self._seats.items():
            if cfg["token_hash"] and hmac.compare_digest(candidate, cfg["token_hash"]):
                match = seat
        if match is not None:
            return match
        if self._verifier is None:
            return None
        # Refuse locally when no configured shared seat claims the identity the
        # token names:  otherwise every wrong guess - even for a family the
        # relay does not serve - costs a blocking Worker verify before any
        # per-seat rate limit can apply.  A valid bearer always names a
        # configured identity, so this can never false-refuse one.
        named = self._verifier.parse_identity(token)
        if named is None:
            return None
        if not any(
            cfg["auth"] == "shared" and cfg["identity"] == named for cfg in self._seats.values()
        ):
            return None
        identity = self._verifier.resolve(token)
        if identity is None:
            return None
        for seat, cfg in self._seats.items():
            if cfg["auth"] == "shared" and cfg["identity"] == identity:
                return seat
        return None

    def _repo_allowed(self, seat: str, repo: str) -> bool:
        for pattern in self._seats[seat]["repos"]:
            if pattern.endswith("/*"):
                if repo.startswith(pattern[:-1]):
                    return True
            elif repo == pattern:
                return True
        return False

    def _rate_ok(self, seat: str, now: float) -> bool:
        with self._lock:
            window = [t for t in self._calls.get(seat, []) if now - t < 3600]
            self._calls[seat] = window
            if len(window) >= self._rate_per_hour:
                return False
            window.append(now)
            return True

    def authorize(self, seat: str, action: str, repo: str, now: float | None = None) -> str | None:
        """Return None when allowed, else a short denial reason (safe to return to the caller)."""
        if seat not in self._seats:
            return "unknown seat"
        if action not in self._seats[seat]["actions"]:
            return f"action {action!r} not allowed for this seat"
        if not self._repo_allowed(seat, repo):
            return f"repo {repo!r} not allowed for this seat"
        if not self._rate_ok(seat, now if now is not None else time.time()):
            return "hourly rate limit reached for this seat"
        return None

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
