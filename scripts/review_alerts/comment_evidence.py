"""Canonical comment evidence and revision identity for the consumer.

Byte-adopted from the independently qualified writer contracts:  the v4.1
revision digest (three byte-exact vectors qualified by the reviewer) and the
v7 canonical_comment_evidence validator.  The semantics, bounds, regexes and
preimage bytes are identical to simple_tools.issue_verify;  only the
exception type and module home differ (this package cannot import
simple_tools).  Nothing here authenticates input by itself:  the caller
feeds it ONLY answers from the authenticated dedicated service_read lane
(the lane that carries the whole GitHub comment, body included;  the
seat-dispatch projection carries no body and cannot feed this).

A failed validation raises CommentUnverified and the caller HOLDS for
uncertainty:  a 404 alone, a malformed answer or a foreign object never
proves deletion, missing ownership or target membership.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
from collections.abc import Mapping
from typing import Any

#: The one number bound shared by the relay lane, the registry, the router
#: pin and the consumers:  1..99999999 (8 digits).
MAX_ISSUE_NUMBER = 99_999_999
#: Canonical comment ids:  the exact safe integer range, no truncation.
MAX_COMMENT_ID = 2**53 - 1
#: Login shape and body bound, byte-identical to the qualified validator.
_LOGIN_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")
MAX_COMMENT_BODY = 1_000_000

#: Strict RFC3339 timestamp, byte-adopted from the corrected v7.1 helper:
#: calendar-valid date (leap years included), 24h time, seconds 00-59,
#: offset hh<=23 mm<=59, fraction 1-9 digits, ASCII only, uppercase T and
#: mandatory Z/+hh:mm offset (lowercase t/z refused).  Validation only:
#: callers keep and hash the ORIGINAL string, never a normalized form.
_STAMP_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,9})?"
    r"(?:Z|[+-]([0-9]{2}):([0-9]{2}))"
)


def _valid_stamp(value) -> bool:
    """Strict RFC 3339 date-time, semantics identical to the corrected
    writer helper (validation only;  the exact string is what gets hashed)."""
    if not isinstance(value, str) or not value.isascii():
        return False
    found = _STAMP_RE.fullmatch(value)
    if not found:
        return False
    year, month, day, hour, minute, second = (int(found.group(i)) for i in range(1, 7))
    try:
        datetime.date(year, month, day)
    except ValueError:
        return False
    if year < 1 or hour > 23 or minute > 59 or second > 59:
        return False
    if found.group(7) is not None and (int(found.group(7)) > 23 or int(found.group(8)) > 59):
        return False
    return True


class CommentUnverified(ValueError):
    """Canonical comment evidence could not be proven:  the caller holds."""


def comment_revision_digest(comment: Mapping[str, Any]) -> str:
    """The canonical revision identity of one comment, for edit event ids
    `i<id>r<digest>`.

    Computed ONLY from an authenticated canonical read of the comment
    (never a webhook body):  the numeric id, the author login, the exact
    `updated_at` string (native precision kept) and the exact body text.
    Two distinct edits in the same second differ whenever their text
    differs;  an identical re-save does not mint a new revision.
    """
    user = comment.get("user")
    login = user.get("login") if isinstance(user, Mapping) else None
    cid, updated, body = comment.get("id"), comment.get("updated_at"), comment.get("body")
    if (
        type(cid) is not int
        or not 1 <= cid <= MAX_COMMENT_ID
        or not isinstance(login, str)
        or not isinstance(updated, str)
        or not updated
        or not isinstance(body, str)
    ):
        raise CommentUnverified("comment lacks canonical revision evidence")
    canonical = json.dumps(
        {"id": cid, "login": login, "updated_at": updated, "body": body},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def canonical_comment_evidence(
    repo: str, comment_id: int, answer: Any, expected_number: int | None = None
) -> dict[str, Any]:
    """Validate one narrow-lane `issue_comment` answer and return its canonical evidence.

    Refuses, as CommentUnverified, unless:  the envelope is exactly this
    verb, repo and comment id;  the comment id, author login, exact
    updated_at and exact body text are well formed;  and its issue_url is
    exactly https://api.github.com/repos/<repo>/issues/<n>.  That <n> is the
    comment's target (issue or pull request, which the `issue` read
    decides);  when `expected_number` is given, a different target refuses,
    so a comment id is never taken to belong to a hinted number on its own
    say-so.  Returns the evidence plus the revision digest.  Nothing here
    comes from a webhook payload.
    """
    if type(comment_id) is not int or not 1 <= comment_id <= MAX_COMMENT_ID:
        raise CommentUnverified("comment id out of range")
    if expected_number is not None and (
        type(expected_number) is not int or not 1 <= expected_number <= MAX_ISSUE_NUMBER
    ):
        raise CommentUnverified("expected target number is not a bounded integer")
    if not isinstance(answer, Mapping):
        raise CommentUnverified("malformed comment answer")
    echoed = answer.get("repo")
    if (
        answer.get("verb") != "issue_comment"
        or not isinstance(echoed, str)
        or echoed.casefold() != repo.casefold()
        or answer.get("comment_id") != comment_id
        or type(answer.get("comment_id")) is not int
    ):
        raise CommentUnverified("answer is for another object")
    body = answer.get("body")
    if not isinstance(body, Mapping) or body.get("id") != comment_id or type(body.get("id")) is not int:
        raise CommentUnverified("comment id mismatch")
    user = body.get("user")
    login = user.get("login") if isinstance(user, Mapping) else None
    updated = body.get("updated_at")
    text = body.get("body")
    if (
        not isinstance(login, str)
        or not _LOGIN_RE.fullmatch(login)
        or not _valid_stamp(updated)
        or not isinstance(text, str)
        or len(text) > MAX_COMMENT_BODY
    ):
        raise CommentUnverified("comment lacks canonical revision evidence")
    url = body.get("issue_url")
    prefix = "https://api.github.com/repos/%s/issues/" % repo
    tail = url[len(prefix):] if isinstance(url, str) and url.casefold().startswith(prefix.casefold()) else ""
    if not tail.isascii() or not tail.isdigit() or tail != str(int(tail)):
        raise CommentUnverified("comment target is not provable")
    target = int(tail)
    if not 1 <= target <= MAX_ISSUE_NUMBER or (expected_number is not None and target != expected_number):
        raise CommentUnverified("comment belongs to another target")
    return {
        "id": comment_id,
        "login": login,
        "updated_at": updated,
        "body": text,
        "target_number": target,
        "revision": comment_revision_digest(body),
    }
