"""Pure exact existing-seat read restoration. No host I/O or activation."""

import copy
import hashlib
import json

ACTION = "wf_review_thread_comments"
SEAT = "swu-echo"


def digest(policy):
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def prepare(policy, expected_digest):
    if not isinstance(policy, dict) or not policy or digest(policy) != expected_digest:
        raise ValueError("policy shape or digest mismatch")
    row = policy.get(SEAT)
    if (
        not isinstance(row, dict)
        or row.get("app_slug") != SEAT
        or row.get("auth") != "shared"
    ):
        raise ValueError("existing own-App seat required")
    actions = row.get("actions")
    if (
        not isinstance(actions, list)
        or not actions
        or any(not isinstance(a, str) for a in actions)
        or len(set(actions)) != len(actions)
        or "wf_review_threads" not in actions
        or ACTION in actions
    ):
        raise ValueError(
            "qualified prior review read required; new action must be absent"
        )
    if row.get("mint_permissions", {}).get("pull_requests") not in ("read", "write"):
        raise ValueError("existing pull_requests read ceiling required")
    out = copy.deepcopy(policy)
    out[SEAT]["actions"].append(ACTION)
    for seat, before in policy.items():
        if seat != SEAT and out[seat] != before:
            raise ValueError("other seat changed")
    if list(out) != list(policy):
        raise ValueError("seat order changed")
    if {k: v for k, v in row.items() if k != "actions"} != {
        k: v for k, v in out[SEAT].items() if k != "actions"
    }:
        raise ValueError("non-action field changed")
    if out[SEAT]["actions"] != actions + [ACTION]:
        raise ValueError("unexpected action delta")
    return out
