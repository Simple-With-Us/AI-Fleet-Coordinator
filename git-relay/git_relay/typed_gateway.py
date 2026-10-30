"""Inactive authenticated gateway. No server/Worker startup registers it.

Injected only in offline contract tests until review and host qualification.
"""

from .outcomes import OutcomeStoreError, key_for
from .typed_disarm import PreflightUnavailable, RateLimited, Refused, TypedDisarm

ACTIONS = ("pr_state", "pr_hold_state", "pr_automerge_disable")


class TypedGateway:
    def __init__(self, policy, client, minter, audit, outcomes):
        if not getattr(outcomes, "_path", None):
            raise ValueError("durable outcomes required")
        self.policy, self.client, self.minter = policy, client, minter
        self.audit, self.outcomes = audit, outcomes

    def _release_prewrite(self, key):
        if key is None:
            return True
        try:
            self.outcomes.finish(key, "refused")
            return True
        except Exception:
            # Reservation stays held when durability cannot be confirmed.
            return False

    def handle(self, token, action, args, proof=None):
        seat = self.policy.authenticate(token, proof)
        if seat is None:
            return 401, {"error": "unauthenticated"}
        key = None
        repo = ""
        result = "refused"
        try:
            if action not in ACTIONS:
                raise Refused("unsupported typed action")

            # Validate with no auth side effects, prior to policy/rate or state.
            class ShapeOnly:
                def authorize(self, *args):
                    return None

            validator = object.__new__(TypedDisarm)
            validator.policy, validator.seat = ShapeOnly(), seat
            repo, _, _ = validator._validate(action, args)
            # for_action is the single policy/rate charge. Scoped service
            # validates args again but never charges that authorization twice.
            service = TypedDisarm.for_action(
                self.client, self.policy, self.minter, seat, action, repo
            )
            self.audit.record(seat, action, repo, "intent", {})
            if action == "pr_automerge_disable":
                candidate = key_for(seat, action, args)
                # Only a successful reservation belongs to this request.  A
                # full or failing store must never acquire a row via finish().
                try:
                    prior = self.outcomes.begin(candidate)
                except OutcomeStoreError:
                    result = "unavailable"
                    return 503, {"error": "outcome store unavailable", "retryable": True}
                key = candidate
                if prior is not None:
                    # Never replay an old "disabled" receipt: autoMerge may
                    # have been re-enabled. Caller must freshly read/reconcile.
                    key = None
                    result = "duplicate_refused"
                    return 409, {
                        "error": "request already recorded; reconcile state",
                        "outcome": "unknown",
                        "retryable": False,
                    }
                payload = service.disable(args)
                self.outcomes.finish(key, "ok", {"outcome": "confirmed_disabled"})
            elif action == "pr_state":
                payload = service.state(args)
            else:
                payload = service.hold_state(args)
            self.audit.record(
                seat,
                action,
                repo,
                "confirmed",
                {"principal": payload["principal"], "head": payload["head"]},
            )
            result = "ok"
            return 200, payload
        except RateLimited:
            if not self._release_prewrite(key):
                result = "unknown"
                return 502, {
                    "error": "reservation release unavailable",
                    "outcome": "unknown",
                    "retryable": False,
                }
            result = "rate_limited"
            return 429, {"error": "typed quota exhausted", "retryable": True}
        except PreflightUnavailable:
            if not self._release_prewrite(key):
                result = "unknown"
                return 502, {
                    "error": "reservation release unavailable",
                    "outcome": "unknown",
                    "retryable": False,
                }
            key = None
            result = "unavailable"
            return 503, {"error": "preflight unavailable", "retryable": True}
        except Refused:
            if not self._release_prewrite(key):
                result = "unknown"
                return 502, {
                    "error": "reservation release unavailable",
                    "outcome": "unknown",
                    "retryable": False,
                }
            return 403, {"error": "typed request refused", "retryable": False}
        except Exception:
            if key is not None:
                try:
                    self.outcomes.finish(key, "unknown")
                except Exception:
                    pass  # Existing pending durable row still blocks replay.
            result = "unknown"
            return 502, {
                "error": "typed outcome unavailable",
                "outcome": "unknown",
                "retryable": False,
            }
        finally:
            if result != "ok":
                try:
                    self.audit.record(
                        seat, action if action in ACTIONS else "invalid", repo, result, {}
                    )
                except Exception:
                    pass  # Intent audit failure prevented the write above.
