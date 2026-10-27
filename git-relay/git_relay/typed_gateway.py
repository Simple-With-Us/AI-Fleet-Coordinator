"""Inactive authenticated gateway. No server/Worker startup registers it.

Injected only in offline contract tests until review and host qualification.
"""

from .outcomes import key_for
from .typed_disarm import Refused, TypedDisarm

ACTIONS = ("pr_state", "pr_hold_state", "pr_automerge_disable")


class TypedGateway:
    def __init__(self, policy, client, minter, audit, outcomes):
        if not getattr(outcomes, "_path", None):
            raise ValueError("durable outcomes required")
        self.policy, self.client, self.minter = policy, client, minter
        self.audit, self.outcomes = audit, outcomes

    def handle(self, token, action, args):
        seat = self.policy.authenticate(token)
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
                key = key_for(seat, action, args)
                prior = self.outcomes.begin(key)
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
        except Refused:
            if key is not None:
                self.outcomes.finish(key, "refused")
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
