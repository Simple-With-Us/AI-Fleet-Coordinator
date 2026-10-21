"""Seat authentication and per-seat policy for the PR relay.

Seats never hold the GitHub token.  Each seat authenticates with its own
bearer token (provisioned out of band) and may call only the actions and
repositories its policy entry names.  Everything not named is denied.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time


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

    VALID_ACTIONS = ("open_pr", "edit_pr", "reply_thread", "comment_pr")

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
            unknown = [a for a in actions if a not in self.VALID_ACTIONS]
            if unknown:
                raise PolicyError(f"seat {seat!r}: unknown actions {unknown}")
            if not actions or not repos:
                raise PolicyError(f"seat {seat!r}: actions and repos must both be non-empty")
            self._seats[seat] = {
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "actions": set(actions),
                "repos": [str(r) for r in repos],
            }
        if rate_per_hour < 1:
            raise PolicyError("rate_per_hour must be at least 1")
        self._rate_per_hour = rate_per_hour
        self._calls: dict[str, list[float]] = {}

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
