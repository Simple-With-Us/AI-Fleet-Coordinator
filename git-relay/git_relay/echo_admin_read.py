"""Pure exact existing-seat admin-read restoration. No host I/O or activation.

Appends only the five reviewed read verbs to the existing swu-echo seat so the
fleet-tools deficiencies AFC-629, AFC-631 and AFC-639 can be verified and used
through the sanctioned route. The verbs and their read-level mint ceilings are
the reviewed relay source (7c333213, git_relay/workflow_verbs.py
ACTION_PERMISSIONS):

    wf_repo_list          -> metadata:read
    wf_pr_search          -> pull_requests:read
    wf_repo_hooks         -> repository_hooks:read
    wf_deploy_keys        -> administration:read
    wf_org_installations  -> organization_administration:read

Every required ceiling key must already exist on the seat at read or write;
this transform never adds, raises or widens any permission, seat, repository
scope, identity or auth field.
"""

import copy
import hashlib
import json

SEAT = "swu-echo"
ACTIONS = [
    "wf_repo_list",
    "wf_pr_search",
    "wf_repo_hooks",
    "wf_deploy_keys",
    "wf_org_installations",
]
CEILINGS = {
    "wf_repo_list": "metadata",
    "wf_pr_search": "pull_requests",
    "wf_repo_hooks": "repository_hooks",
    "wf_deploy_keys": "administration",
    "wf_org_installations": "organization_administration",
}


def digest(policy):
    return hashlib.sha256(
        json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def prepare(policy, expected_digest):
    if not isinstance(policy, dict) or not policy or digest(policy) != expected_digest:
        raise ValueError("policy shape or digest mismatch")
    row = policy.get(SEAT)
    if not isinstance(row, dict) or row.get("app_slug") != SEAT or row.get("auth") != "shared":
        raise ValueError("existing own-App seat required")
    actions = row.get("actions")
    if (
        not isinstance(actions, list)
        or not actions
        or any(not isinstance(a, str) for a in actions)
        or len(set(actions)) != len(actions)
        or any(a in actions for a in ACTIONS)
    ):
        raise ValueError("qualified seat action list required; new actions must be absent")
    permissions = row.get("mint_permissions")
    if not isinstance(permissions, dict):
        raise ValueError("existing mint ceiling required")
    for action in ACTIONS:
        if permissions.get(CEILINGS[action]) not in ("read", "write"):
            raise ValueError("existing read ceiling required for " + action)
    out = copy.deepcopy(policy)
    out[SEAT]["actions"].extend(ACTIONS)
    for seat, before in policy.items():
        if seat != SEAT and out[seat] != before:
            raise ValueError("other seat changed")
    if list(out) != list(policy):
        raise ValueError("seat order changed")
    if {k: v for k, v in row.items() if k != "actions"} != {
        k: v for k, v in out[SEAT].items() if k != "actions"
    }:
        raise ValueError("non-action field changed")
    if out[SEAT]["actions"] != actions + ACTIONS:
        raise ValueError("unexpected action delta")
    return out
