"""Explicit source-only builder for one exact-target qualification, not auto-loaded."""

import dataclasses
import time

from .native_custody import fresh
from .native_integration import Driver
from .native_qualification import Qualification
from .zcode_native import digest


def notification(head, turn_id):
    # Same existing head-only turn instruction, no external text or inferred link note.
    return (
        "[ZC wake turn]  You were tagged in Zulip 1 times (turn " + turn_id + ").  "
        "The message text is not quoted here on purpose.  Read each message in Zulip "
        "before you act on it, one at a time:  zulip_get_message with its message_id, "
        "at the default length first and with full: true only when you need the rest.  "
        "Answer each in its own thread under your seat's rules.\n"
        "- wake id " + head.wake_id + ":  message_id " + str(head.message_id) + "."
    )


def build(adapter, env, config, spec, *, runner=None, remote_runner=None):
    # Called only by separately reviewed installed builder selection. Never mutate
    # live global config or make all platform delivery native.
    expected = {"target", "workspace", "epoch", "expires"}
    if not isinstance(spec, dict) or set(spec) not in (
        expected,
        expected | {"qualification_generation", "source_boundary"},
    ):
        raise ValueError("qualification-spec")
    local_config = {**config, "native_delivery_review_only": False}
    kwargs = {}
    if runner is not None:
        kwargs["runner"] = runner
    if remote_runner is not None:
        kwargs["remote_runner"] = remote_runner
    base = adapter.router_platform(env, local_config, **kwargs)
    state = adapter.state_dir(env)
    node = "/home/jay/.zcode/server/releases/3.19.0-linux-x64-60f71efd6a01/runtime/node"
    driver = Driver(state, spec["epoch"], node, str(env.get("HOME") or ""))

    def registry_context():
        # WakeSettings owns the scoped registry context. Keep its live mapping,
        # never a dict snapshot: refreshed or revoked settings apply at dispatch.
        overlay = getattr(config, "registry_env", None)
        return overlay(env) if callable(overlay) else env

    def custody(target, workspace):
        if adapter._recheck(registry_context(), target) is not None:
            return False
        status, row = adapter._session_binding(env, target.session_id)
        substatus, subs = adapter._subagent_ids(env, [target.session_id])
        return (
            status == "ok"
            and row.get("id") == target.session_id
            and row.get("directory") == workspace
            and substatus == "ok"
            and target.session_id not in subs
        )

    def native(target, head, text, turn_id):
        def guarded_driver(*args, **kwargs):
            original_verify = kwargs["verify"]

            def verify(binding):
                return (
                    spec["epoch"] <= time.time()
                    and (spec["expires"] is None or time.time() < spec["expires"])
                    and original_verify(binding)
                    and spec["epoch"] <= time.time()
                    and (spec["expires"] is None or time.time() < spec["expires"])
                )

            kwargs["verify"] = verify
            return driver(*args, **kwargs)

        return adapter._deliver_prompt(
            registry_context(),
            {**local_config, "native_delivery_review_only": True},
            target.session_id,
            text,
            int(config.get("resume_timeout_seconds") or 900),
            evidence_ids=(head.wake_id,),
            target=target,
            recheck_registry=True,
            native_driver=guarded_driver,
            native_fresh=fresh(state, head.wake_id, target, spec["epoch"]),
        )

    def fallback(target, head, riders, turn_id):
        return base.deliver(target, head)

    def turn_fallback(target, head, riders, turn_id):
        return base.deliver_turn(target, head, riders, turn_id)

    def unknown(code):
        return adapter.WR.Delivery("unknown", "native-qualification", code=code)

    qualifier = Qualification(
        state,
        spec["target"],
        spec["workspace"],
        spec["epoch"],
        spec["expires"],
        native,
        fallback,
        notification,
        unknown,
        custody,
        **(
            {
                "qualification_generation": spec["qualification_generation"],
                "source_boundary": spec["source_boundary"],
            }
            if "qualification_generation" in spec
            else {}
        ),
    )

    def deliver(target, head):
        # Single-wake path has no turn ID. This deterministic notification-only ID
        # is derived from the authoritative wake, never creates a router turn/rider.
        turn = "t-" + digest({"wake_key": head.wake_id})[:16]
        return qualifier(target, head, (), turn)

    def deliver_turn(target, head, riders, turn_id):
        if riders:
            return base.deliver_turn(target, head, riders, turn_id)
        result = qualifier(target, head, (), turn_id, fallback=turn_fallback)
        if isinstance(result, adapter.WR.Turn):
            return result
        return adapter.WR.Turn(result, unreadable=result.outcome in ("unknown", "refused"))

    platform = dataclasses.replace(base, deliver=deliver, deliver_turn=deliver_turn)
    # Caller owns bounded query-only follow-up and cleanup; no permanent schedule.
    return platform, qualifier, driver, custody
