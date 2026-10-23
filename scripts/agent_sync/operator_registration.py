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
MAX_ANSWER_BYTES = 8 * 1024
# Settled entries stay long enough to answer "what happened" after a restart, then the
# sweep prunes them so a long-lived daemon's queue file does not grow without bound.
SETTLED_KEEP_SECONDS = 7 * 24 * 3600
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


# Every field an entry may carry, with the shape each must have.  Anything else in the
# file is corruption, not forward compatibility:  an unknown key is rejected rather than
# carried, because a tolerated typo would silently drop a constraint a writer relied on.
_ENTRY_ID_RE = re.compile(r"reg-[0-9a-f]{12}\Z")
_ENTRY_STR_FIELDS = {
    "requester": MAX_LABEL,
    "evidence_location": MAX_LABEL,
    "reason": MAX_LABEL,
    "claimed_location": MAX_LABEL,
    "location": MAX_LABEL,
    "label": MAX_LABEL,
}
_ENTRY_TIME_FIELDS = (
    "asked_at",
    "deadline",
    "notified_at",
    "answered_at",
    "dropped_at",
    "reconciled_at",
)
_ENTRY_KNOWN_KEYS = frozenset(
    (
        "entry_id",
        "platform",
        "profile_scope",
        "session_id",
        "seat",
        "state",
        "route",
        "route_lost",
        "generation",
    )
    + _ENTRY_TIME_FIELDS
    + tuple(_ENTRY_STR_FIELDS)
)


def _validate_entry(key: str, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, dict):
        raise ValueError("operator_registration: entry %s is not an object" % key)
    unknown = sorted(str(field) for field in entry if field not in _ENTRY_KNOWN_KEYS)
    if unknown:
        raise ValueError(
            "operator_registration: entry %s has unknown fields %s" % (key, ",".join(unknown))
        )
    for field in ("entry_id", "platform", "profile_scope", "session_id", "seat", "state"):
        if not isinstance(entry.get(field), str) or not entry[field]:
            raise ValueError("operator_registration: entry %s has no %s" % (key, field))
    for field in ("entry_id", "platform", "profile_scope", "session_id", "seat"):
        if len(entry[field]) > MAX_LABEL and field != "session_id":
            raise ValueError("operator_registration: entry %s has an overlong %s" % (key, field))
    if not _ENTRY_ID_RE.fullmatch(entry["entry_id"]):
        raise ValueError("operator_registration: entry %s has a malformed id" % key)
    if entry["entry_id"] != key:
        raise ValueError("operator_registration: entry %s carries a mismatched id" % key)
    if entry["state"] not in (STATE_ASKED, STATE_REGISTERED, STATE_CONFLICT, STATE_DROPPED):
        raise ValueError("operator_registration: entry %s has an unknown state" % key)
    for field in _ENTRY_TIME_FIELDS:
        if field in entry:
            value = entry[field]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(
                    "operator_registration: entry %s has a malformed %s" % (key, field)
                )
    for field in ("asked_at", "deadline"):
        if field not in entry:
            raise ValueError("operator_registration: entry %s has no %s" % (key, field))
    for field, limit in _ENTRY_STR_FIELDS.items():
        if field in entry and (not isinstance(entry[field], str) or len(entry[field]) > limit):
            raise ValueError("operator_registration: entry %s has a malformed %s" % (key, field))
    if entry.get("evidence_location") and entry["evidence_location"] not in LOCATION_VALUES:
        raise ValueError("operator_registration: entry %s has an unknown evidence_location" % key)
    if "location" in entry and entry["location"] not in LOCATION_VALUES:
        raise ValueError("operator_registration: entry %s has an unknown location" % key)
    if "generation" in entry and (
        isinstance(entry["generation"], bool)
        or not isinstance(entry["generation"], int)
        or entry["generation"] < 1
    ):
        raise ValueError("operator_registration: entry %s has a malformed generation" % key)
    if "route_lost" in entry and not isinstance(entry["route_lost"], bool):
        raise ValueError("operator_registration: entry %s has a malformed route_lost" % key)
    if "route" in entry:
        route = entry["route"]
        if (
            not isinstance(route, dict)
            or sorted(str(k) for k in route) != ["channel", "topic"]
            or not isinstance(route.get("channel"), str)
            or not route["channel"]
            or len(route["channel"]) > MAX_LABEL
            or not isinstance(route.get("topic"), str)
            or not route["topic"]
            or len(route["topic"]) > MAX_LABEL
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
    extra = sorted(str(key) for key in data if key != "entries")
    if extra:
        # No forward compatibility:  a section this build does not know is a corrupted or
        # foreign file, and failing closed beats silently discarding it.
        raise ValueError(
            "operator_registration: the queue file has unknown sections %s" % ",".join(extra)
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
    """The chat's Zulip channel/topic as a clean pair, or None when no route is known.

    Channel and topic are user-controlled text (topic names are), so they are scanned
    like every other caller-supplied field before one is stored.
    """
    if not isinstance(route, Mapping):
        return None
    channel = _text(str(route.get("channel") or ""), "route.channel", MAX_LABEL)
    topic = _text(str(route.get("topic") or ""), "route.topic", MAX_LABEL)
    if not channel or not topic:
        return None
    return {"channel": channel, "topic": topic}


def _refresh_route(
    entry: dict[str, Any],
    clean_route: dict[str, str] | None,
    topics: Iterable[Mapping[str, Any]] | None,
) -> str | None:
    """Keep or lose the asked route against the chat's live lease topics;  never guess.

    - The asked topic still listed (whatever newer activity topics appeared) keeps the
      route:  an unrelated new attachment never moves a pending question.
    - The asked topic gone from the lease marks the entry route_lost.  A whole-topic
      rename is NOT recovered here - lease shape cannot tell a rename from an unrelated
      replacement (independent topics can share an expiry, permanent ones most of all).
      Renames arrive with real provenance through the daemon's update_message handler,
      which calls retarget_routes with the event's own old and new names.
    """
    topics = [t for t in (topics or ()) if isinstance(t, Mapping)]
    old = entry.get("route")
    if not isinstance(old, dict):
        if clean_route:
            entry["route"] = clean_route
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
        if current == key:
            if entry.pop("route_lost", None) is not None:
                return "kept"
            return None
    if not entry.get("route_lost"):
        entry["route_lost"] = True
        return "lost"
    return None


def retarget_routes(
    registry_db: str,
    *,
    seat: str,
    old_channel: str,
    old_topic: str,
    new_channel: str,
    new_topic: str,
) -> list[dict[str, Any]]:
    """Re-point pending questions at a whole-topic rename;  the retargeted entries.

    The rename event itself (update_message, propagate_mode change_all) is the only
    provenance accepted:  it carries the old and new names, so no heuristic ever guesses
    a continuation from lease shape.  Only asked-state entries of the renamed seat whose
    route matches the old channel/topic (casefolded) move;  a route_lost entry whose
    stale route matches is revived, because the event proves where its question went.
    """
    # The renamed-to names arrive from a Zulip event, but topic names are still
    # user-controlled text:  scan before one is stored, same as every other field.
    new_channel = _text(new_channel, "route.channel", MAX_LABEL)
    new_topic = _text(new_topic, "route.topic", MAX_LABEL)
    old_key = (old_channel.casefold(), old_topic.casefold())
    seat_key = seat.casefold()
    moved: list[dict[str, Any]] = []
    with L.locked(queue_path(registry_db)):
        data = load(registry_db)
        for entry in data["entries"].values():
            if entry["state"] != STATE_ASKED:
                continue
            if str(entry.get("seat") or "").casefold() != seat_key:
                continue
            route = entry.get("route")
            if not isinstance(route, dict):
                continue
            route_key = (
                str(route.get("channel") or "").casefold(),
                str(route.get("topic") or "").casefold(),
            )
            if route_key != old_key:
                continue
            entry["route"] = {"channel": new_channel, "topic": new_topic}
            entry.pop("route_lost", None)
            moved.append(dict(entry))
        if moved:
            save(registry_db, data)
    return moved


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
    # A session id is caller-supplied text:  it is persisted in the entry and relayed in
    # the question, so it gets the same secret scan as every other text field.
    kind = secretscan.scan(native)
    if kind:
        raise RegistrationRefused("session_id", kind)
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
                # names a topic, kept while the asked topic stays attached, lost when it
                # vanishes.  A whole-topic rename is re-pointed by the daemon's
                # update_message handler through retarget_routes - event provenance,
                # never a lease-shape guess (_refresh_route).
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
        eid = str(entry_id or "")
        if not _ENTRY_ID_RE.fullmatch(eid):
            # A malformed token is rejected by name, never confused with an unknown or
            # expired entry:  each failure shape gets its own explicit status.
            return {"status": "invalid", "reason": "malformed_entry_id"}
        entry = data["entries"].get(eid)
        # An asked entry answers directly;  a conflicted one settles through the same
        # path - the owner names the true location and every cross-check runs again, so
        # the confirmation's "settle which is right and I will re-register it" is real.
        if not isinstance(entry, dict) or entry["state"] not in (STATE_ASKED, STATE_CONFLICT):
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
        outcome = str(out.get("outcome") or "")
        label = str(out.get("label") or "")
        if outcome not in ("registered", "resumed") or not label:
            # The registry writer refused without raising (seat_mismatch, resumed_closed)
            # or returned no live letter.  Closing the entry here would post a false
            # "Registered" confirmation and, with the session still unlettered, the next
            # wake would open the same question again forever.  Refuse, keep the entry
            # answerable, and record only the outcome word - never identity content.
            entry["answered_at"] = round(now, 3)
            entry["reason"] = "register:" + (outcome or "no_letter")
            save(registry_db, data)
            return {
                "status": "refused",
                "reason": outcome or "no_letter",
                "entry_id": entry["entry_id"],
            }
        entry["state"] = STATE_REGISTERED
        entry["answered_at"] = round(now, 3)
        entry["location"] = word
        entry["label"] = label
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
        settled = []
        for entry_id, entry in data["entries"].items():
            if entry["state"] == STATE_ASKED:
                continue
            touched = max(
                float(entry.get(field) or 0)
                for field in ("answered_at", "dropped_at", "notified_at", "asked_at")
            )
            if now - touched > SETTLED_KEEP_SECONDS:
                settled.append(entry_id)
        for entry_id in settled:
            del data["entries"][entry_id]
        if dropped or settled:
            save(registry_db, data)
    return dropped


def pending_entries(registry_db: str) -> list[dict[str, Any]]:
    return [
        dict(entry)
        for entry in load(registry_db)["entries"].values()
        if entry["state"] == STATE_ASKED
    ]


def answerable_entries(registry_db: str) -> list[dict[str, Any]]:
    """The entries an answer may still act on:  asked ones, and conflicts awaiting settlement."""
    return [
        dict(entry)
        for entry in load(registry_db)["entries"].values()
        if entry["state"] in (STATE_ASKED, STATE_CONFLICT)
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
    if not isinstance(content, str):
        return None
    # Bound the untrusted input before any regex touches it:  an answer is a short
    # human reply, so anything past this is not one, and no matcher may materialize it.
    if len(content.encode("utf-8", "ignore")) > MAX_ANSWER_BYTES:
        return None
    if not content.strip():
        return None
    refs: list[str] = []
    for match in _ENTRY_RE.finditer(content):
        if match.group(0) not in refs:
            refs.append(match.group(0))
        if len(refs) > 1:
            return None
    if len(refs) != 1:
        return None
    locations: list[str] = []
    for match in re.finditer(r"[A-Za-z]+", content.casefold()):
        word = match.group(0)
        if word in LOCATION_VALUES and word not in locations:
            locations.append(word)
        if len(locations) > 1:
            return None
    if len(locations) != 1:
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


def mark_reconciled(registry_db: str, entry_id: str, *, now: float | None = None) -> bool:
    """Stamp the last delivery-reconciliation attempt on a live asked entry.

    An unknown delivery must not re-run the whole topic read-back on every wake:  the
    stamp lets the daemon reconcile at most once per reconcile interval.  The entry is
    NOT stamped notified - the question's delivery is still unproven, so the interval
    retry (and the sweep's deadline) still apply.
    """
    now = time.time() if now is None else float(now)
    with L.locked(queue_path(registry_db)):
        data = load(registry_db)
        entry = data["entries"].get(str(entry_id or ""))
        if not isinstance(entry, dict) or entry.get("state") != STATE_ASKED:
            return False
        entry["reconciled_at"] = round(now, 3)
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
    if status == "refused":
        return (
            "Not registered:  the registry refused this answer (%s); the question stays "
            "open (ref %s)." % (result.get("reason") or "refused", entry_id)
        )
    if status == "invalid":
        if result.get("reason") == "malformed_entry_id":
            return "No open question with that ref - it was answered already or expired unanswered."
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
        # First run:  nothing has ever registered, so the file does not exist yet, and a
        # missing file is the truthful "no letters yet" (the daemon reads the boundary
        # the same way).  An existing but unreadable file still raises RegistryUnavailable.
        if not os.path.isfile(str(db_path)):
            return []
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
    except (ValueError, RegistrationRefused, OR.RegistryUnavailable) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(out))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
