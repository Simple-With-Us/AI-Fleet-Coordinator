"""Pure exact fleet-wide admin-read extension. No host I/O or activation.

Owner decision 2026-11-10: Jay's authenticated WhatsApp answer "for ALL
AGENTS" to the fleet seat question for these five read capabilities
(AFC-629 / AFC-631 / AFC-639; existing repository scopes and write
permissions unchanged). The five reviewed read verbs are granted to every
fleet agent seat, not only swu-echo:

    wf_repo_list          -> metadata:read
    wf_pr_search          -> pull_requests:read
    wf_repo_hooks         -> repository_hooks:read
    wf_deploy_keys        -> administration:read
    wf_org_installations  -> organization_administration:read

The verbs and their read-level mint ceilings are the reviewed relay source
(7c333213, git_relay/workflow_verbs.py ACTION_PERMISSIONS). This transform
extends every seat that already holds each verb's ceiling at read or write
and that matches the reviewed own-App shared-auth seat shape; every other
seat or verb is excluded unchanged and named in the returned report. It
never adds, raises or widens any permission, seat, repository scope,
identity or auth field, and it never removes or reorders an existing action.
A seat that already holds some of the five verbs keeps them; only the
missing ones are appended, so the transform is exact whether or not the
ECHO-only transaction landed first.
"""

import copy
import hashlib
import json

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


def _seat_problem(seat, row):
    """None when the seat matches the reviewed own-App shape, else a reason."""
    if not isinstance(row, dict):
        return "seat row is not an object"
    if row.get("app_slug") != seat:
        return "app_slug is not the seat's own App identity"
    if row.get("auth") != "shared":
        return "auth is not the reviewed shared model"
    actions = row.get("actions")
    if (
        not isinstance(actions, list)
        or not actions
        or any(not isinstance(a, str) for a in actions)
        or len(set(actions)) != len(actions)
    ):
        return "action list missing, empty, typed wrong or duplicated"
    if not isinstance(row.get("mint_permissions"), dict):
        return "mint ceiling missing or not an object"
    return None


def prepare(policy, expected_digest):
    """Return (extended_policy, report). Refuses drift and no-op candidates."""
    if not isinstance(policy, dict) or not policy or digest(policy) != expected_digest:
        raise ValueError("policy shape or digest mismatch")
    out = copy.deepcopy(policy)
    report = {}
    for seat, row in policy.items():
        problem = _seat_problem(seat, row)
        if problem is not None:
            report[seat] = {"skipped": problem}
            continue
        actions = row["actions"]
        permissions = row["mint_permissions"]
        added, already, excluded = [], [], []
        for action in ACTIONS:
            if action in actions:
                already.append(action)
            elif permissions.get(CEILINGS[action]) in ("read", "write"):
                added.append(action)
            else:
                excluded.append(action + " (no " + CEILINGS[action] + " read ceiling)")
        out[seat]["actions"] = actions + added
        report[seat] = {"added": added, "already_present": already, "excluded": excluded}
    if not any(r.get("added") for r in report.values()):
        raise ValueError("no seat extension available; candidate would be a no-op")
    for seat, before in policy.items():
        if "skipped" in report[seat]:
            continue  # excluded unchanged; there is nothing extended to verify
        after = out[seat]
        if {k: v for k, v in after.items() if k != "actions"} != {
            k: v for k, v in before.items() if k != "actions"
        }:
            raise ValueError("non-action field changed for " + seat)
    if list(out) != list(policy):
        raise ValueError("seat order changed")
    return out, report
