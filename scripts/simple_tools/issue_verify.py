"""Canonical issue verification and the explicit self-association client.

The verifier trusts nothing but the relay's own narrow-lane `issue` answer, and only after
the requested repository, issue number and object type are re-checked here.  It carries no
credential, no callback URL and no caller-chosen path:  `read` is the service-owned
ServiceReadClient call, injected.  Anything ambiguous (a redirect or missing object, a
malformed pull_request marker, a wrong repo or number, a foreign URL) refuses, so no
association is ever written for an object this module could not prove is an existing
issue.  Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any

#: The one number bound shared by the relay lane, the registry, the router pin and the
#: consumers:  1..99999999 (8 digits).  Anything larger is refused before any read.
MAX_ISSUE_NUMBER = 99_999_999
ISSUE_STATES = ("open", "closed")


class IssueUnverified(ValueError):
    """The canonical read could not prove an existing issue:  nothing is associated."""


def canonical_issue_proof(repo: str, number: int, answer: Any) -> dict[str, Any]:
    """Validate one narrow-lane `issue` answer and return the proof, or raise."""
    if type(number) is not int or not 1 <= number <= MAX_ISSUE_NUMBER:
        raise IssueUnverified("issue number out of range")
    if not isinstance(answer, Mapping):
        raise IssueUnverified("no answer")
    if (
        answer.get("verb") != "issue"
        or not isinstance(answer.get("repo"), str)
        or answer["repo"].casefold() != repo.casefold()
        or answer.get("number") != number
        or type(answer.get("number")) is not int
    ):
        raise IssueUnverified("answer is not for the requested issue")
    body = answer.get("body")
    if (
        not isinstance(body, Mapping)
        or type(body.get("number")) is not int
        or body["number"] != number
        or body.get("state") not in ISSUE_STATES
    ):
        raise IssueUnverified("canonical body mismatch")
    # The type comes only from the canonical marker:  absent means an issue, an object
    # means a pull request, anything else (null, string, list) is ambiguous and refuses.
    if "pull_request" in body:
        if not isinstance(body["pull_request"], Mapping):
            raise IssueUnverified("malformed pull_request marker")
        kind = "pull_request"
    else:
        kind = "issue"
    # The observed URL is exactly the canonical object path:  /issues/<n> for an issue,
    # /pull/<n> for a pull request.  No query, fragment, control character or other object.
    url = body.get("html_url")
    leaf = "pull" if kind == "pull_request" else "issues"
    expected = "https://github.com/%s/%s/%d" % (repo, leaf, number)
    if not isinstance(url, str) or url.casefold() != expected.casefold():
        raise IssueUnverified("observed URL is not the canonical object URL")
    return {"number": number, "kind": kind, "state": body["state"], "html_url": url}


def make_issue_verifier(
    read: Callable[[str, str, Mapping[str, Any]], tuple[int, Any]],
) -> Callable[[str, int], dict[str, Any]]:
    """`read(repo, verb, args) -> (status, answer)` is the service-owned relay read.

    A non-200 (404, 301, 410, permission failure, timeout) refuses:  a failed read holds
    the association and never creates it.
    """

    def verify(repo: str, number: int) -> dict[str, Any]:
        if type(number) is not int or not 1 <= number <= MAX_ISSUE_NUMBER:
            raise IssueUnverified("issue number out of range")
        try:
            status, answer = read(repo, "issue", {"number": number})
        except Exception:
            # A failed, timed-out or refused read (including a missing grant) holds.
            raise IssueUnverified("canonical read failed") from None
        if status != 200:
            raise IssueUnverified("canonical read refused")
        return canonical_issue_proof(repo, number, answer)

    return verify


def bind_issue_call(
    call: Callable[..., Mapping[str, Any]],
    env: Mapping[str, str] | None,
    *,
    platform: str,
    location: str,
    scope: str,
    session_id: str,
    repo: str,
    number: int,
    generation: int,
) -> Mapping[str, Any]:
    """The session's own explicit self-association over the signed registry API.

    The registry refuses a seat other than the session's own, a stale generation, another
    live owner, and anything the canonical verifier could not prove.  Returns the API's
    answer unchanged; never retries a write whose outcome is unknown.
    """
    if type(number) is not int or not 1 <= number <= MAX_ISSUE_NUMBER:
        raise ValueError("issue number out of range")
    if type(generation) is not int or generation < 1:
        raise ValueError("generation required")
    return call(
        env,
        "bind-issue",
        {
            "platform": platform,
            "location": location,
            "scope": scope,
            "session_id": session_id,
            "repo": repo,
            "number": number,
            "generation": generation,
        },
    )


def comment_revision_digest(comment: Mapping[str, Any]) -> str:
    """The canonical revision identity of one comment, for edit event ids `i<id>r<digest>`.

    Computed ONLY from an authenticated canonical read of the comment (never a webhook
    body):  the numeric id, the author login, the exact `updated_at` string (native
    precision kept) and the exact body text.  Two distinct edits in the same second differ
    whenever their text differs;  an identical re-save does not mint a new revision.
    """
    user = comment.get("user")
    login = user.get("login") if isinstance(user, Mapping) else None
    cid, updated, body = comment.get("id"), comment.get("updated_at"), comment.get("body")
    if (
        type(cid) is not int
        or not 1 <= cid <= 2**53 - 1
        or not isinstance(login, str)
        or not isinstance(updated, str)
        or not updated
        or not isinstance(body, str)
    ):
        raise IssueUnverified("comment lacks canonical revision evidence")
    canonical = json.dumps(
        {"id": cid, "login": login, "updated_at": updated, "body": body},
        sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def old_router_would_fail_capability(test_answer: Any) -> bool:
    """True when a router's `test:true` answer for a nonexistent issue and an exact
    session did NOT hold with the typed refusal:  an old router that ignores require_issue
    picks the session, so the gate refuses to enable issue wakes."""
    if not isinstance(test_answer, Mapping):
        return True
    return not (
        test_answer.get("test") is True
        and test_answer.get("reason") == "required-issue-unbound"
        and not test_answer.get("picked")
        and test_answer.get("delivered") is False
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def service_issue_reader(
    env: Mapping[str, str], opener: Any = None, should_stop: Callable[[], bool] | None = None
) -> Callable | None:
    """The service-owned read client: POSTs the fixed `issue` verb to the relay's
    /v1/service_read with the review service's own bearer, read from the process
    environment (RA_SERVICE_TOKEN, or the file named by RA_SERVICE_TOKEN_FILE) at CALL time.
    Nothing a caller or payload sends reaches it.  No credential returns None (held).
    Redirects are never followed.  Errors raise and become IssueUnverified.  `should_stop` (the
    daemon's stop predicate) is checked before the request and again before the answer is
    used:  a pending stop refuses, so shutdown never completes an association."""
    base = (env.get("RA_RELAY_URL") or "http://127.0.0.1:8471").rstrip("/")

    def token() -> str:
        value = (env.get("RA_SERVICE_TOKEN") or "").strip()
        path = (env.get("RA_SERVICE_TOKEN_FILE") or "").strip()
        if not value and path:
            with open(path, encoding="utf-8") as handle:
                value = handle.read().strip()
        return value

    try:
        if not token():
            return None
    except OSError:
        return None
    send = opener or urllib.request.build_opener(_NoRedirect).open

    def read(repo: str, verb: str, args: Mapping[str, Any]):
        if should_stop is not None and should_stop():
            raise IssueUnverified("stopping")
        bearer = token()
        if not bearer:
            raise IssueUnverified("no service credential")
        request = urllib.request.Request(
            base + "/v1/service_read",
            data=json.dumps({"repo": repo, "verb": verb, **args}).encode("utf-8"),
            method="POST",
        )
        request.add_header("Authorization", "Bearer " + bearer)
        request.add_header("Content-Type", "application/json")
        try:
            with send(request, timeout=10) as response:
                answer = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            raise IssueUnverified("canonical read failed") from None
        if should_stop is not None and should_stop():
            raise IssueUnverified("stopping")
        if not isinstance(answer, dict) or "body" not in answer:
            raise IssueUnverified("malformed read answer")
        # The relay's own envelope is kept and checked, never rebuilt from the request:  a
        # mismatched or missing verb, repo or number refuses.
        echoed = answer.get("repo")
        if (
            answer.get("verb") != verb
            or not isinstance(echoed, str)
            or echoed.casefold() != repo.casefold()
            or type(answer.get("number")) is not int
            or answer.get("number") != args.get("number")
        ):
            raise IssueUnverified("read answer is for another object")
        return 200, answer

    return read


_STAMP_RE = __import__("re").compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]{1,9})?"
    r"(?:Z|[+-]([0-9]{2}):([0-9]{2}))"
)


def valid_rfc3339(value: Any) -> bool:
    """Strict RFC 3339 date-time (calendar-valid, 24h time, seconds 00-59, offset hh<=23 mm<=59).
    Validation only:  callers keep and hash the ORIGINAL string, never a normalized form."""
    if not isinstance(value, str) or not value.isascii():
        return False
    found = _STAMP_RE.fullmatch(value)
    if not found:
        return False
    year, month, day, hour, minute, second = (int(found.group(i)) for i in range(1, 7))
    try:
        __import__("datetime").date(year, month, day)
    except ValueError:
        return False
    if year < 1 or hour > 23 or minute > 59 or second > 59:
        return False
    if found.group(7) is not None and (int(found.group(7)) > 23 or int(found.group(8)) > 59):
        return False
    return True


_LOGIN_RE = __import__("re").compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})(?:\[bot\])?")
MAX_COMMENT_BODY = 1_000_000


def canonical_comment_evidence(
    repo: str, comment_id: int, answer: Any, expected_number: int | None = None
) -> dict[str, Any]:
    """Validate one narrow-lane `issue_comment` answer and return its canonical evidence.

    `answer` is the authenticated service_read lane answer (the lane that carries the whole
    GitHub comment, body included;  the seat-dispatch projection carries no body and cannot
    feed this).  Refuses, as IssueUnverified, unless:  the envelope is exactly this verb, repo
    and comment id;  the comment id, author login, exact updated_at and exact body text are
    well formed;  and its issue_url is exactly https://api.github.com/repos/<repo>/issues/<n>.
    That <n> is the comment's target (issue or pull request, which the `issue` read decides);
    when `expected_number` is given, a different target refuses, so a comment id is never
    taken to belong to a hinted number on its own say-so.  Returns the evidence plus the
    revision digest.  Nothing here comes from a webhook payload."""
    if type(comment_id) is not int or not 1 <= comment_id <= 2**53 - 1:
        raise IssueUnverified("comment id out of range")
    if expected_number is not None and (
        type(expected_number) is not int or not 1 <= expected_number <= MAX_ISSUE_NUMBER
    ):
        raise IssueUnverified("expected target number is not a bounded integer")
    if not isinstance(answer, Mapping):
        raise IssueUnverified("malformed comment answer")
    echoed = answer.get("repo")
    if (
        answer.get("verb") != "issue_comment"
        or not isinstance(echoed, str)
        or echoed.casefold() != repo.casefold()
        or answer.get("comment_id") != comment_id
        or type(answer.get("comment_id")) is not int
    ):
        raise IssueUnverified("answer is for another object")
    body = answer.get("body")
    if not isinstance(body, Mapping) or body.get("id") != comment_id or type(body.get("id")) is not int:
        raise IssueUnverified("comment id mismatch")
    user = body.get("user")
    login = user.get("login") if isinstance(user, Mapping) else None
    updated = body.get("updated_at")
    text = body.get("body")
    if (
        not isinstance(login, str)
        or not _LOGIN_RE.fullmatch(login)
        or not isinstance(updated, str)
        or not valid_rfc3339(updated)
        or not isinstance(text, str)
        or len(text) > MAX_COMMENT_BODY
    ):
        raise IssueUnverified("comment lacks canonical revision evidence")
    url = body.get("issue_url")
    prefix = "https://api.github.com/repos/%s/issues/" % repo
    tail = url[len(prefix):] if isinstance(url, str) and url.casefold().startswith(prefix.casefold()) else ""
    if not tail.isascii() or not tail.isdigit() or tail != str(int(tail)):
        raise IssueUnverified("comment target is not provable")
    target = int(tail)
    if not 1 <= target <= MAX_ISSUE_NUMBER or (expected_number is not None and target != expected_number):
        raise IssueUnverified("comment belongs to another target")
    return {
        "id": comment_id,
        "login": login,
        "updated_at": updated,
        "body": text,
        "target_number": target,
        "revision": comment_revision_digest(body),
    }
