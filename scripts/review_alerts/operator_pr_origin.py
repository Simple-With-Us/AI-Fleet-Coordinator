"""Bind only an immutable authenticated creation record through Operator custody.

The trusted lookup is the relay's first-confirmed ownership record, not caller JSON.
Origin fields remain seat self-attestation, not per-chat cryptographic proof. The
native transport derives them; the relay commits them with the actual create
receipt. No binding is inferred for older records or from a title/branch.
"""

from __future__ import annotations

import re

IDENTITY = ("platform", "verified_host", "profile_scope", "native_session_id")
ORIGIN_FIELDS = (
    "seat",
    "repo",
    "number",
    "corr",
    "confirmed",
    "source",
    "origin_session_claim",
    "origin_letter_claim",
    "origin_generation_claim",
)
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{3,127}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class Held(Exception):
    pass


def bind_creation(repo, number, *, lookup_creation, registry_history, registry_bind, seat_map):
    """Callbacks are service-owned, never selected by an HTTP request.

    lookup_creation reads protected first-confirmed relay state; registry callbacks
    use the existing Operator's own registry client. This function accepts only a
    PR key, never an origin envelope supplied by a wake/tool caller.
    """
    if (
        not isinstance(repo, str)
        or not REPO_RE.fullmatch(repo)
        or type(number) is not int
        or number < 1
    ):
        raise Held("invalid-pr-key")
    row = lookup_creation(repo, number)
    if (
        not isinstance(row, dict)
        or row.get("confirmed") is not True
        or row.get("source") != "open_pr"
    ):
        raise Held("no-confirmed-live-creation")
    if row.get("repo", "").casefold() != repo.casefold() or row.get("number") != number:
        raise Held("creation-pr-mismatch")
    corr = row.get("corr")
    sid = row.get("origin_session_claim")
    letter = row.get("origin_letter_claim")
    gen = row.get("origin_generation_claim")
    if not isinstance(corr, str) or not corr or len(corr) > 256:
        raise Held("creation-correlation-missing")
    if (
        not isinstance(sid, str)
        or not ID_RE.fullmatch(sid)
        or not isinstance(letter, str)
        or not re.fullmatch(r"[A-Z]{2}", letter)
        or type(gen) is not int
        or gen < 1
    ):
        raise Held("no-native-origin")
    principal = seat_map.get(row.get("seat"))
    if not principal:
        raise Held("unknown-authenticated-seat")
    answer = registry_history(principal)
    if (
        not isinstance(answer, dict)
        or "error" in answer
        or not isinstance(answer.get("items"), list)
    ):
        raise Held("registry-unavailable")
    matches = [
        r
        for r in answer["items"]
        if isinstance(r, dict)
        and r.get("native_session_id") == sid
        and str(r.get("seat", "")).upper() == principal
        and not r.get("closed_at")
        and not r.get("retired_at")
        and r.get("state") in ("active", "open")
        and r.get("generation") == gen
        and (r.get("letter") or r.get("label")) == letter
    ]
    if len(matches) != 1:
        raise Held("origin-not-uniquely-live")
    session = matches[0]
    if not all(isinstance(session.get(k), str) and session[k] for k in IDENTITY):
        raise Held("registry-identity-incomplete")
    # A second trusted read detects replaced/changed creation state before mutation.
    current = lookup_creation(repo, number)
    if not isinstance(current, dict) or any(current.get(k) != row.get(k) for k in ORIGIN_FIELDS):
        raise Held("creation-changed")
    payload = {k: session[k] for k in IDENTITY}
    payload.update(repo=row["repo"], number=number, generation=gen)
    receipt = registry_bind(payload)
    if (
        not isinstance(receipt, dict)
        or receipt.get("outcome") not in ("bound", "unchanged", "renewed")
        or receipt.get("native_session_id") != sid
        or receipt.get("generation") != gen
        or not any(
            isinstance(p, dict)
            and p.get("repo", "").casefold() == repo.casefold()
            and p.get("number") == number
            and p.get("generation") == gen
            for p in receipt.get("bound_prs", [])
        )
    ):
        raise Held("binding-receipt-mismatch")
    return {
        "platform": session["platform"],
        "verified_host": session["verified_host"],
        "profile_scope": session["profile_scope"],
        "seat": principal,
        "repo": row["repo"],
        "number": number,
        "corr": corr,
        "session": sid,
        "generation": gen,
        "outcome": receipt["outcome"],
        "trust": "authenticated-seat-origin-attestation",
    }
