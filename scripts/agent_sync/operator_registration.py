"""Ask-before-letter registration for Operator session letters (AFC-412 follow-up).

Owner direction, Oct 23 2026:  when the Operator receives a session/chat id, it asks the
session's location - mac, server or cloud - BEFORE the session letter is assigned.  A letter
is never issued on a guessed, defaulted or self-evident location, because the letter is what
a wake address resolves, and the location is what decides how that chat can be woken.

This module is the decision core and the durable queue.  It never sends a Zulip message and
never opens the registry itself:  the caller (the Operator's CLI today, the listener daemon
next) delivers the question, brings the reply back, and injects the registry functions, so
the queue can be tested without a database and the registry never contends with the wake
path's read handles.

The flow:

  1.  ``begin`` records the one pending question for a session identity.  A session that is
      already lettered is answered from the registry instead of being asked again, and a
      second ``begin`` for the same identity returns the live question rather than opening
      another one.
  2.  ``answer`` takes the reply.  Only "mac", "server" or "cloud" counts.  A reply that
      contradicts the catalog's discovery evidence, or that would letter a session already
      lettered at another location, is a conflict:  nothing is registered and the owner
      settles it.  Otherwise the session is registered at the answered location and the
      letter comes back.
  3.  ``sweep`` drops a question nobody answered, like the clarifying-question queue
      (clarify.py, owner decision Oct 22 2026):  a late reply never revives a dropped entry.

The queue file lives next to the registry database, so the shared-store override that gives
the fleet one letter namespace (AGENT_SYNC_OPERATOR_REGISTRY) also gives it one pending
queue.  Entries hold ids, labels and timestamps:  never a message body, a title or a key,
and caller-supplied text that looks like a secret is refused rather than stored.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any

from . import live as L
from . import secretscan
from . import session_ids as SI

# Owner ruling (daemon.py, session_labels):  "location means server, mac, cloud".  The CLI
# register command already takes --location with exactly these values.
LOCATION_VALUES = ("mac", "server", "cloud")

STATE_ASKED = "asked"
STATE_REGISTERED = "registered"
STATE_CONFLICT = "conflict"
STATE_DROPPED = "dropped"

REASON_NO_REPLY = "no_reply"
REASON_EVIDENCE_MISMATCH = "evidence_mismatch"
REASON_OTHER_LOCATION = "lettered_at_other_location"

DEFAULT_TIMEOUT_SECONDS = 24 * 3600
MAX_LABEL = 80
MAX_ID = 140


class RegistrationRefused(ValueError):
    """Caller-supplied text looked like a credential;  nothing was stored.

    Carries the field and the secretscan kind, never the value, so a refusal is safe to log.
    """

    def __init__(self, field: str, kind: str) -> None:
        super().__init__("operator_registration: %s refused (%s)" % (field, kind))
        self.field = field
        self.kind = kind


def queue_path(registry_db: str) -> str:
    """The pending queue, next to the registry it gates letters for."""
    return os.path.join(os.path.dirname(os.path.abspath(registry_db)), "operator-registration.json")


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("operator_registration: %s must be a string" % field)
    label = value.strip()[:limit]
    kind = secretscan.scan(label) if label else None
    if kind:
        raise RegistrationRefused(field, kind)
    return label


def _validate_entry(key: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("operator_registration: entry %s is not an object" % key)
    for field in ("entry_id", "platform", "profile_scope", "session_id", "seat", "state"):
        if not isinstance(entry.get(field), str) or not entry[field]:
            raise ValueError("operator_registration: entry %s has no %s" % (key, field))
    if entry["entry_id"] != key:
        raise ValueError("operator_registration: entry %s carries a mismatched id" % key)
    if entry["state"] not in (STATE_ASKED, STATE_REGISTERED, STATE_CONFLICT, STATE_DROPPED):
        raise ValueError("operator_registration: entry %s has an unknown state" % key)
    for field in ("asked_at", "deadline"):
        if not isinstance(entry.get(field), (int, float)):
            raise ValueError("operator_registration: entry %s has no %s" % (key, field))
    return dict(entry)


def load(registry_db: str) -> dict[str, Any]:
    """The queue, with every entry validated.

    An absent file is an empty queue;  a file that exists but cannot be read as a queue
    raises ValueError, because discarding it would silently forget questions already asked.
    """
    path = queue_path(registry_db)
    if not os.path.exists(path):
        return {"entries": {}}
    data = L.read_json(path)
    if not isinstance(data, dict) or not data:
        raise ValueError("operator_registration: the queue file is unreadable or malformed, not an empty queue")
    entries = data.get("entries")
    if not isinstance(entries, Mapping):
        raise ValueError("operator_registration: the queue file has no entries object")
    return {
        "entries": {str(key): _validate_entry(str(key), value) for key, value in entries.items()}
    }


def save(registry_db: str, data: Mapping[str, Any]) -> None:
    """Durable write:  a unique mode-600 temp file, fsynced, then os.replace."""
    L.write_json(queue_path(registry_db), dict(data), durable=True)


def _identity(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(entry.get("platform") or ""),
        str(entry.get("profile_scope") or ""),
        str(entry.get("session_id") or ""),
    )


def question_for(entry: Mapping[str, Any]) -> str:
    """The one question the Operator asks.  Ids and labels only, never a body or a title."""
    return (
        "Session letter request:  the %s chat with session id starting %s (seat %s) has no "
        "session letter yet.  Which location is it on:  mac, server, or cloud?  "
        "Answer with the location and ref %s."
        % (
            entry["platform"],
            entry["session_id"][:8],
            entry["seat"],
            entry["entry_id"],
        )
    )


def begin(
    registry_db: str,
    *,
    platform: str,
    profile_scope: str,
    session_id: str,
    seat: str,
    requester: str,
    evidence_location: str = "",
    registrations: Callable[[str, str], list[dict[str, Any]]],
    now: float | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Start (or resume) the one pending location question for a session identity.

    ``registrations(platform, session_id)`` is the injected read-only registry scan naming
    every location that session id is already lettered at.  A session already lettered at
    one location is answered from the registry - asking again would be noise - and one
    lettered at several is reported ambiguous rather than asked, because the contradiction
    is the owner's to settle.
    """
    now = time.time() if now is None else float(now)
    native = SI.check(session_id, "the native session id")
    platform = _text(platform, "platform", MAX_LABEL).lower()
    scope = _text(profile_scope, "profile_scope", MAX_LABEL)
    seat_label = _text(seat, "seat", MAX_LABEL)
    requester_label = _text(requester, "requester", MAX_LABEL)
    evidence = _text(evidence_location, "evidence_location", MAX_LABEL).lower()
    if evidence and evidence not in LOCATION_VALUES:
        raise ValueError("operator_registration: evidence_location must be one of %s" % (LOCATION_VALUES,))
    if not platform or not scope or not seat_label:
        raise ValueError("operator_registration: platform, profile_scope and seat are required")

    known = registrations(platform, native)
    if len(known) == 1 and (not evidence or known[0].get("verified_host") == evidence):
        return {
            "status": "already_registered",
            "location": known[0].get("verified_host"),
            "label": known[0].get("label"),
            "generation": known[0].get("generation"),
        }
    if len(known) > 1:
        return {
            "status": "ambiguous",
            "registrations": [
                {"verified_host": row.get("verified_host"), "label": row.get("label"), "generation": row.get("generation")}
                for row in known
            ],
        }

    with L.locked(queue_path(registry_db)):
        data = load(registry_db)
        for entry in data["entries"].values():
            if entry["state"] == STATE_ASKED and _identity(entry) == (platform, scope, native):
                return {"status": "pending", "entry": dict(entry), "question": question_for(entry)}
        entry_id = "reg-" + secrets.token_hex(6)
        entry = {
            "entry_id": entry_id,
            "platform": platform,
            "profile_scope": scope,
            "session_id": native,
            "seat": seat_label,
            "requester": requester_label,
            "evidence_location": evidence,
            "state": STATE_ASKED,
            "asked_at": round(now, 3),
            "deadline": round(now + float(timeout_seconds), 3),
        }
        data["entries"][entry_id] = entry
        save(registry_db, data)
    return {"status": "ask", "entry": dict(entry), "question": question_for(entry)}


def answer(
    registry_db: str,
    *,
    entry_id: str,
    location: str,
    registrations: Callable[[str, str], list[dict[str, Any]]],
    register: Callable[..., dict[str, Any]],
    now: float | None = None,
) -> dict[str, Any]:
    """Record the location reply and letter the session, or refuse.

    ``register`` is the injected registry writer (operator_registry.register_session);  it
    is called only after the location is validated and every cross-check passes, so this
    queue can never mint a letter on a guessed or contradicted location.  An unknown entry,
    a dropped one and an already-answered one all return ``ignored``:  a late reply never
    revives a dropped question (the clarifying-queue rule).
    """
    now = time.time() if now is None else float(now)
    word = str(location or "").strip().lower()
    with L.locked(queue_path(registry_db)):
        data = load(registry_db)
        entry = data["entries"].get(str(entry_id or ""))
        if not isinstance(entry, dict) or entry["state"] != STATE_ASKED:
            return {"status": "ignored"}
        if word not in LOCATION_VALUES:
            return {"status": "invalid", "allowed": list(LOCATION_VALUES), "entry_id": entry["entry_id"]}

        known = registrations(entry["platform"], entry["session_id"])
        other = [row for row in known if row.get("verified_host") != word]
        conflict_reason = ""
        if other:
            conflict_reason = REASON_OTHER_LOCATION
        elif entry.get("evidence_location") and entry["evidence_location"] != word:
            conflict_reason = REASON_EVIDENCE_MISMATCH
        if conflict_reason:
            entry["state"] = STATE_CONFLICT
            entry["answered_at"] = round(now, 3)
            entry["claimed_location"] = word
            entry["reason"] = conflict_reason
            save(registry_db, data)
            return {
                "status": "conflict",
                "reason": conflict_reason,
                "claimed": word,
                "evidence": entry.get("evidence_location") or "",
                "known": [row.get("verified_host") for row in known],
                "entry_id": entry["entry_id"],
            }

        out = register(
            platform=entry["platform"],
            verified_host=word,
            profile_scope=entry["profile_scope"],
            native_session_id=entry["session_id"],
            seat=entry["seat"],
        )
        entry["state"] = STATE_REGISTERED
        entry["answered_at"] = round(now, 3)
        entry["location"] = word
        entry["label"] = str(out.get("label") or "")
        entry["generation"] = int(out.get("generation") or 1)
        save(registry_db, data)
        return {
            "status": "registered",
            "entry_id": entry["entry_id"],
            "location": word,
            "label": entry["label"],
            "generation": entry["generation"],
            "outcome": out.get("outcome"),
        }


def sweep(registry_db: str, *, now: float | None = None) -> list[dict[str, Any]]:
    """Drop every question nobody answered by its deadline;  return what was dropped.

    The caller logs one drop per returned entry.  The check, the mutation and the write run
    under one lock, so a reply landing in the same window is either answered or dropped,
    never overwritten.
    """
    now = time.time() if now is None else float(now)
    dropped: list[dict[str, Any]] = []
    with L.locked(queue_path(registry_db)):
        data = load(registry_db)
        for entry in data["entries"].values():
            if entry["state"] == STATE_ASKED and float(entry.get("deadline") or 0) <= now:
                entry["state"] = STATE_DROPPED
                entry["dropped_at"] = round(now, 3)
                entry["reason"] = REASON_NO_REPLY
                dropped.append(dict(entry))
        if dropped:
            save(registry_db, data)
    return dropped


def pending_entries(registry_db: str) -> list[dict[str, Any]]:
    return [
        dict(entry)
        for entry in load(registry_db)["entries"].values()
        if entry["state"] == STATE_ASKED
    ]


# ---- the Operator's CLI:  ask, answer, sweep, pending -----------------------------------


def _registry_functions(db_path: str):
    # Imported lazily so the queue module and its tests never need the registry's 3.11+
    # import surface, and the read handle stays read-only by behaviour (operator_registry).
    from . import operator_registry as OR

    def registrations(platform: str, session_id: str) -> list[dict[str, Any]]:
        return OR.registrations_for_native_id(db_path, platform, session_id)

    def register(**identity: Any) -> dict[str, Any]:
        return OR.register_session(
            db_path,
            source="operator-registration",
            issuer=OR.ISSUER_OPERATOR,
            **identity,
        )

    return registrations, register


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="operator-registration")
    parser.add_argument("--db", default="", help="the registry file (default: the shared path)")
    sub = parser.add_subparsers(dest="command", required=True)

    a = sub.add_parser("ask", help="record the location question for one session and print it")
    a.add_argument("--platform", required=True)
    a.add_argument("--scope", required=True)
    a.add_argument("--session-id", required=True)
    a.add_argument("--seat", required=True)
    a.add_argument("--requester", required=True, help="who presented the session id")
    a.add_argument("--evidence-location", default="", help="the catalog's discovered location, when known")
    a.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)

    n = sub.add_parser("answer", help="register with the answered location and print the letter")
    n.add_argument("--entry", required=True)
    n.add_argument("--location", required=True, help="one of:  mac, server, cloud")

    sub.add_parser("sweep", help="drop unanswered questions past their deadline")
    sub.add_parser("pending", help="list the questions still waiting for an answer")

    args = parser.parse_args(argv)
    from . import operator_registry as OR  # path resolution only;  no handle is opened

    db_path = args.db or OR._default_db()

    if args.command == "sweep":
        print(json.dumps({"dropped": sweep(db_path)}))
        return 0
    if args.command == "pending":
        print(json.dumps({"pending": pending_entries(db_path)}))
        return 0

    registrations, register = _registry_functions(db_path)
    try:
        if args.command == "ask":
            out = begin(
                db_path,
                platform=args.platform,
                profile_scope=args.scope,
                session_id=args.session_id,
                seat=args.seat,
                requester=args.requester,
                evidence_location=args.evidence_location,
                registrations=registrations,
                timeout_seconds=args.timeout,
            )
        else:
            out = answer(
                db_path,
                entry_id=args.entry,
                location=args.location,
                registrations=registrations,
                register=register,
            )
    except (ValueError, RegistrationRefused) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
