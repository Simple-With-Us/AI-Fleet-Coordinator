"""Exact additive seat insertion; no I/O or credential access.

The installed policy may use GIT_RELAY_SEATS_FILE rather than the older env
installer. The deployment transaction (deploy/seat_policy.py) owns quiescence,
CAS, durable backups, atomic replacement and recovery. This module never
performs activation.
"""

import copy
import hashlib
import json

REQUIRED_KEYS = {"actions", "app_slug", "auth", "identity", "mint_permissions", "repos"}
OPTIONAL_KEYS = REQUIRED_KEYS | {"oauth_assert"}


def digest(policy):
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _checked_strings(value, what):
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(v, str) or not v for v in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("seat row has no qualified %s list" % what)
    return list(value)


def _checked_row(seat_name, row):
    """A deep copy of the seat row, refused unless it is exactly a rendered seat."""
    if not isinstance(row, dict):
        raise TypeError("invalid seat row")
    unknown = sorted(set(row) - OPTIONAL_KEYS)
    if unknown or not REQUIRED_KEYS <= set(row):
        raise ValueError("seat row keys are not a rendered seat's")
    out = {
        "actions": _checked_strings(row["actions"], "actions"),
        "app_slug": row["app_slug"],
        "auth": row["auth"],
        "identity": row["identity"],
        "mint_permissions": row["mint_permissions"],
        "repos": _checked_strings(row["repos"], "repos"),
    }
    if out["app_slug"] != seat_name:
        raise ValueError("seat row app_slug does not name its key")
    if out["auth"] != "shared":
        raise ValueError("seat row auth is not shared")
    if not isinstance(out["identity"], str) or not out["identity"]:
        raise ValueError("seat row identity is not a name")
    if (
        not isinstance(out["mint_permissions"], dict)
        or not out["mint_permissions"]
        or any(
            not isinstance(k, str) or not isinstance(v, str) or not k or not v
            for k, v in out["mint_permissions"].items()
        )
    ):
        raise ValueError("seat row mint_permissions is not a permission map")
    out["mint_permissions"] = dict(out["mint_permissions"])
    if "oauth_assert" in row:
        # The per-seat opt-in renders only as literal true (deploy/seat_policy.py).
        if row["oauth_assert"] is not True:
            raise ValueError("seat row oauth_assert must be true when present")
        out["oauth_assert"] = True
    return out


def prepare(policy, seat_name, seat_row, expected_digest):
    """Return a copy of policy with exactly seat_name appended, refusing malformed or stale input."""
    if not isinstance(policy, dict) or not policy or digest(policy) != expected_digest:
        raise ValueError("policy shape or digest mismatch")
    if not isinstance(seat_name, str) or not seat_name:
        raise TypeError("invalid seat name")
    if seat_name in policy:
        raise ValueError("seat already present")
    row = _checked_row(seat_name, seat_row)
    result = copy.deepcopy(policy)
    result[seat_name] = row
    # Independently prove every original seat and its position unchanged: the
    # only difference is the one appended seat.
    for name in policy:
        if result[name] != policy[name]:
            raise ValueError("unexpected policy change")
    if list(result) != list(policy) + [seat_name]:
        raise ValueError("unexpected seat order change")
    return result
