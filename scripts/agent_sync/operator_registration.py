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
import re
import secrets
import time
from collections.abc import Callable, Iterable, Mapping
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
    if "notified_at" in entry and not isinstance(entry["notified_at"], (int, float)):
        raise ValueError("operator_registration: entry %s has a malformed notified_at" % key)
    if "route" in entry:
        route = entry["route"]
        if (
            not isinstance(route, dict)
            or not isinstance(route.get("channel"), str)
            or not route["channel"]
            or not isinstance(route.get("topic"), str)
            or not route["topic"]
        ):
            raise ValueError("operator_registration: entry %s has a malformed route" % key)
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
        raise ValueError(
            "operator_registration: the queue file is unreadable or malformed, not an empty queue"
        )
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


def _route(route: Mapping[str, Any] | None) -> dict[str, str] | None:
    """The chat's Zulip channel/topic as a clean pair, or None when no route is known."""
    if not isinstance(route, Mapping):
        return None
    channel = str(route.get("channel") or "").strip()
    topic = str(route.get("topic") or "").strip()
    if not channel or not topic:
        return None
    return {"channel": channel[:MAX_LABEL], "topic": topic[:MAX_LABEL]}


def _topic_expires(topics: Iterable[Mapping[str, Any]] | None, route: Mapping[str, str]) -> Any:
    """The lease expiry of the topic a route names, or None when the lease does not say."""
    key = (route["channel"].casefold(), route["topic"].casefold())
    for entry in topics or ():
        if not isinstance(entry, Mapping):
            continue
        other = (
            str(entry.get("channel") or "").casefold(),
            str(entry.get("topic") or "").casefold(),
        )
        if other == key:
            expires = entry.get("expires")
            return expires if expires is None or isinstance(expires, (int, float)) else None
    return None


def _refresh_route(
    entry: dict[str, Any],
    clean_route: dict[str, str] | None,
    topics: Iterable[Mapping[str, Any]] | None,
) -> str | None:
    """Track the asked topic through renewals and whole-topic renames;  never steal.

    The route a question was asked on is refreshed against the chat's live lease topics on
    every pass:

    - The asked topic still listed (whatever newer activity topics appeared) keeps the
      route;  only its stored expiry follows renewals.  An unrelated new attachment never
      moves a pending question.
    - The asked topic gone from the lease follows ONLY a whole-topic rename or move:  the
      daemon's update_message handler rewrites the lease entry in place and keeps its
      expiry, so exactly one live topic carrying the asked topic's last expiry is the
      continuation.  No unique match means the question cannot be placed honestly (an
      unrelated activity topic must not silently inherit it):  the entry is marked
      route_lost, keeps its stale route, and the sweep's re-ask starts a fresh question.
    """
    topics = [t for t in (topics or ()) if isinstance(t, Mapping)]
    old = entry.get("route")
    if not isinstance(old, dict):
        if clean_route:
            entry["route"] = clean_route
            entry["route_expires"] = _topic_expires(topics, clean_route)
            entry.pop("route_lost", None)
            return "filled"
        return None
    key = (
        str(old.get("channel") or "").casefold(),
        str(old.get("topic") or "").casefold(),
    )
    for topic in topics:
        current = (
            str(topic.get("channel") or "").casefold(),
            str(topic.get("topic") or "").casefold(),
        )
        if current != key:
            continue
        changed = False
        expires = topic.get("expires")
        if expires is None or isinstance(expires, (int, float)):
            if entry.get("route_expires") != expires:
                entry["route_expires"] = expires
                changed = True
        if entry.pop("route_lost", None) is not None:
            changed = True
        return "kept" if changed else None
    stored = entry.get("route_expires", ...)
    candidates = []
    if stored is not ... and (stored is None or isinstance(stored, (int, float))):
        candidates = [t for t in topics if t.get("expires") == stored]
    if len(candidates) == 1:
        picked = candidates[0]
        channel = str(picked.get("channel") or "").strip()
        topic = str(picked.get("topic") or "").strip()
        if channel and topic:
            entry["route"] = {"channel": channel[:MAX_LABEL], "topic": topic[:MAX_LABEL]}
            entry["route_expires"] = picked.get("expires")
            entry.pop("route_lost", None)
            return "followed"
    if not entry.get("route_lost"):
        entry["route_lost"] = True
        return "lost"
    return None


def begin(
    registry_db: str,
    *,
    platform: str,
    profile_scope: str,
    session_id: str,
    seat: str,
    requester: str,
    evidence_location: str = "",
    route: Mapping[str, Any] | None = None,
    route_topics: Iterable[Mapping[str, Any]] | None = None,
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
        raise ValueError(
            "operator_registration: evidence_location must be one of %s" % (LOCATION_VALUES,)
        )
    if not platform or not scope or not seat_label:
        raise ValueError("operator_registration: platform, profile_scope and seat are required")
    clean_route = _route(route)

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
                {
                    "verified_host": row.get("verified_host"),
                    "label": row.get("label"),
                    "generation": row.get("generation"),
                }
                for row in known
            ],
        }

    with L.locked(queue_path(registry_db)):
        data = load(registry_db)
        for entry in data["entries"].values():
            if entry["state"] == STATE_ASKED and _identity(entry) == (platform, scope, native):
                # The route tracks the chat's live lease:  filled when the lease first
                # names a topic, followed through a whole-topic rename, never stolen by
                # an unrelated new attachment (_refresh_route).
                change = _refresh_route(entry, clean_route, route_topics)
                if change:
                    save(registry_db, data)
                return {
                    "status": "pending",
                    "entry": dict(entry),
                    "question": question_for(entry),
                    "route_change": change,
                }
        for entry in data["entries"].values():
            if entry["state"] == STATE_CONFLICT and _identity(entry) == (platform, scope, native):
                # An unsettled conflict stays terminal until the owner settles it:  opening
                # another question would re-ask every wake and grow the queue without bound.
                return {
                    "status": "conflict",
                    "reason": entry.get("reason") or "",
                    "entry": dict(entry),
                }
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
        if clean_route:
            entry["route"] = clean_route
            entry["route_expires"] = _topic_expires(route_topics, clean_route)
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
            return {
                "status": "invalid",
                "allowed": list(LOCATION_VALUES),
                "entry_id": entry["entry_id"],
            }

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


# ---- the daemon's intake:  parsing replies, stamping sends, wording answers ---------------


_ENTRY_RE = re.compile(r"\breg-[0-9a-f]{12}\b")


def parse_answer(content: Any) -> dict[str, str] | None:
    """The (entry ref, location) an owner reply carries, or None when it is not an answer.

    Both parts are required:  the ref ties the reply to the one open question and the
    location is the answer.  A reply naming no ref, several refs, no location word or two
    different ones is not an answer this flow may act on, so it reads as None and the
    message follows its normal path.  Matching is case-insensitive on whole tokens only,
    so "MacBook" is not "mac" and a server named "cloudbank" is not "cloud".
    """
    if not isinstance(content, str) or not content.strip():
        return None
    refs = _ENTRY_RE.findall(content)
    if len(set(refs)) != 1:
        return None
    words = re.findall(r"[A-Za-z]+", content.casefold())
    locations = [word for word in words if word in LOCATION_VALUES]
    if len(set(locations)) != 1:
        return None
    return {"entry_id": refs[0], "location": locations[0]}


def mark_notified(registry_db: str, entry_id: str, *, now: float | None = None) -> bool:
    """Stamp the question post as sent, so a restart never asks the same question twice.

    The stamp lands only on a live asked entry and only after the send returned, so a
    failed send leaves the entry unstamped and the next wake retries the post.  Answers
    False when there was nothing to stamp.
    """
    now = time.time() if now is None else float(now)
    with L.locked(queue_path(registry_db)):
        data = load(registry_db)
        entry = data["entries"].get(str(entry_id or ""))
        if not isinstance(entry, dict) or entry["state"] != STATE_ASKED:
            return False
        entry["notified_at"] = round(now, 3)
        save(registry_db, data)
        return True


def answer_reply(result: Mapping[str, Any]) -> str:
    """The one-line reply posted where the answer arrived.  Ids and the letter only, never a body."""
    status = str(result.get("status") or "")
    entry_id = str(result.get("entry_id") or "")
    if status == "registered":
        generation = int(result.get("generation") or 1)
        suffix = " (gen %d)" % generation if generation > 1 else ""
        return "Registered:  the chat is on %s, letter %s%s (ref %s)." % (
            result.get("location"),
            result.get("label"),
            suffix,
            entry_id,
        )
    if status == "conflict":
        if result.get("reason") == REASON_OTHER_LOCATION:
            return (
                "Not registered:  that session already has a letter at %s, which disagrees "
                "with %s.  Settle which is right and I will re-register it (ref %s)."
                % (", ".join(result.get("known") or []), result.get("claimed"), entry_id)
            )
        return (
            "Not registered:  the fleet discovered this chat on %s, which disagrees with %s.  "
            "Settle which is right and I will re-register it (ref %s)."
            % (result.get("evidence") or "another location", result.get("claimed"), entry_id)
        )
    if status == "invalid":
        return (
            "That is not a location I can use:  answer mac, server, or cloud (ref %s)." % entry_id
        )
    return "No open question with that ref - it was answered already or expired unanswered."


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
    a.add_argument(
        "--evidence-location", default="", help="the catalog's discovered location, when known"
    )
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
