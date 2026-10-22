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
    """Per-seat allowlists parsed from PR_RELAY_SEATS_JSON.

    Shape:
        {
          "swu-echo": {
            "token": "<bearer>",
            "actions": ["open_pr", "edit_pr", "reply_thread", "comment_pr"],
            "repos": ["Simple-With-Us/AI-Fleet-Coordinator", "Simple-With-Us/*"]
          }
        }
    """

    # PR-write actions are authored as the owner (PR-only token);  everything
    # else runs as the seat's own GitHub App via a relay-minted token.
    VALID_ACTIONS = ("open_pr", "edit_pr", "reply_thread", "comment_pr")
    APP_ACTIONS = ("mint_token", "merge_pr", "workflow_dispatch", "resolve_thread")
    # Known but not implemented:  accepted in seat config, refused at dispatch
    # until their implementation and per-action policy land.
    REGISTERED_ACTIONS = (
        "create_release",
        "rerun_workflow",
        "cancel_workflow",
        "set_labels",
        "create_issue_comment",
        "request_reviewers",
        "set_branch_protection",
        "create_repo",
    )
    ALL_ACTIONS = VALID_ACTIONS + APP_ACTIONS + REGISTERED_ACTIONS

    def __init__(self, seats_json: str, rate_per_hour: int = 120):
        if not seats_json:
            raise PolicyError("PR_RELAY_SEATS_JSON is empty")
        try:
            raw = json.loads(seats_json)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"PR_RELAY_SEATS_JSON is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict) or not raw:
            raise PolicyError("PR_RELAY_SEATS_JSON must be a non-empty object of seats")
        self._seats: dict[str, dict] = {}
        for seat, cfg in raw.items():
            if not isinstance(cfg, dict):
                raise PolicyError(f"seat {seat!r}: config must be an object")
            token = str(cfg.get("token") or "")
            if len(token) < 16:
                raise PolicyError(f"seat {seat!r}: token missing or shorter than 16 characters")
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
            wants_app = any(a in self.APP_ACTIONS for a in actions)
            if wants_app and not app_slug:
                raise PolicyError(f"seat {seat!r}: app actions need an app_slug")
            if app_slug is not None:
                if not isinstance(app_slug, str) or not app_slug.startswith("swu-"):
                    raise PolicyError(f"seat {seat!r}: app_slug must be a swu- app slug")
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
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "actions": set(actions),
                "repos": [str(r) for r in repos],
                "app_slug": app_slug,
                "mint_permissions": dict(mint_perms),
            }
        if rate_per_hour < 1:
            raise PolicyError("rate_per_hour must be at least 1")
        self._rate_per_hour = rate_per_hour
        self._calls: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def authenticate(self, token: str) -> str | None:
        """Return the seat whose token matches, or None.  Constant-time over seats."""
        if not token:
            return None
        candidate = hashlib.sha256(token.encode()).hexdigest()
        match = None
        for seat, cfg in self._seats.items():
            if hmac.compare_digest(candidate, cfg["token_hash"]):
                match = seat
        return match

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
