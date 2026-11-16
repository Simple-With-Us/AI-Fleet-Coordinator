"""Exact additive bootstrap for wf_file_patch; no I/O or credential access.

The installed policy may use GIT_RELAY_SEATS_FILE rather than the older env
installer. The deployment transaction owns quiescence, CAS, durable backups,
atomic replacement and recovery. This module never performs activation.
"""

import copy
import hashlib
import json

ACTION = "wf_file_patch"


def digest(policy):
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def prepare(policy, expected_digest):
    """Return only an action-appended copy, refusing malformed or stale input."""
    if not isinstance(policy, dict) or not policy or digest(policy) != expected_digest:
        raise ValueError("policy shape or digest mismatch")
    result = copy.deepcopy(policy)
    for name, row in result.items():
        if not isinstance(name, str) or not isinstance(row, dict):
            raise TypeError("invalid seat")
        actions = row.get("actions")
        if (
            not isinstance(actions, list)
            or not actions
            or any(not isinstance(a, str) for a in actions)
            or len(set(actions)) != len(actions)
            or "wf_file_put" not in actions
        ):
            raise ValueError("seat has no qualified file-write action list")
        if ACTION not in actions:
            actions.append(ACTION)
    # Independently prove every non-action value and original ordered action
    # sequence unchanged, including private fields never returned separately.
    for name in policy:
        before, after = policy[name], result[name]
        if {k: v for k, v in before.items() if k != "actions"} != {
            k: v for k, v in after.items() if k != "actions"
        }:
            raise ValueError("unexpected policy change")
        expected = before["actions"] + ([] if ACTION in before["actions"] else [ACTION])
        if after["actions"] != expected:
            raise ValueError("unexpected action change")
    return result
