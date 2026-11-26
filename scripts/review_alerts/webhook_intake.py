"""Pure GitHub webhook helpers for the review-alert listener.

No I/O, no env reads, no GitHub credential, no callbacks, no dedupe state.
The listener (owned by the review-alert service) calls verify() then
map_event() and enqueues the returned advisory hints in process.  A hint only
says WHERE to reconcile; it is never an alert, a snapshot or derive input.
The delivery id is transport retry identity only, never alert identity.

Events: pull_request, pull_request_review, pull_request_review_comment,
check_run, check_suite, merge_group, issues, issue_comment.  Standalone issue
hints carry subject "issue"; the listener must not route them until the
issue-owner contract exists.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass

EVENTS = (
    "pull_request", "pull_request_review", "pull_request_review_comment",
    "check_run", "check_suite", "merge_group", "issues", "issue_comment",
)
PR_KINDS = frozenset({"pr", "pr_review", "pr_review_comment", "pr_comment",
                      "check_run", "check_suite"})
ISSUE_KINDS = frozenset({"issue", "issue_comment_issue"})
MAPPED, IGNORED, HELD, INVALID = "mapped", "ignored", "held", "invalid"
MAX_BODY = 1024 * 1024
MAX_REASON = 80
MAX_NUMBER = 10**9           # PR and issue numbers
MAX_REPO = 140
MAX_TIME = 40
MAX_REF = 80
MAX_ACTION = 40
MAX_EVENT = 64
MAX_TARGETS = 50             # PRs one payload may name
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SIG_RE = re.compile(r"^sha256=[0-9a-f]{64}$")
DELIVERY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_KIND = {"pull_request": "pr", "pull_request_review": "pr_review",
         "pull_request_review_comment": "pr_review_comment", "check_run": "check_run",
         "check_suite": "check_suite"}
_DISCOVERY = frozenset({"check_run", "check_suite"})


class _Bad(Exception):
    """Internal: malformed payload shape; the reason is a fixed short code."""


@dataclass(frozen=True)
class Hint:
    repo: str
    number: int
    subject: str                 # "pr" or "issue"
    hint_kind: str
    source_event_id: str         # transport delivery id, retry identity only
    source_time: str = ""        # payload timestamp, "" when absent
    head_sha: str | None = None  # 40-hex or None, advisory
    source_ref: str = ""         # observed source object id (review/comment/check), "" if none
    action: str = ""             # payload action, "" if none


@dataclass(frozen=True)
class MappingResult:
    outcome: str                 # mapped | ignored | held | invalid
    reason: str                  # bounded
    hints: tuple = ()
    event_name: str = ""         # "" when the supplied name was not a valid short string
    delivery_id: str = ""        # the exact valid id, "" when missing or invalid (never truncated)
    repo: str = ""               # subject metadata, "" when unknown
    subject_number: int | None = None
    advisory_head_sha: str | None = None  # head named by a held event, advisory only


def verify(body_bytes: bytes, signature_header: str | None, secret: bytes) -> bool:
    """True only for a valid strict sha256=<64 hex> signature over the exact body."""
    if not isinstance(secret, bytes) or not secret:
        return False
    if not isinstance(body_bytes, bytes) or len(body_bytes) > MAX_BODY:
        return False
    if not isinstance(signature_header, str) or not SIG_RE.fullmatch(signature_header):
        return False
    want = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature_header[7:])


def _obj(v, code="bad-shape"):
    """A dict, or absent (None).  Any other type is malformed."""
    if v is None:
        return None
    if isinstance(v, dict):
        return v
    raise _Bad(code)


def _num(v, code="number-invalid") -> int:
    if type(v) is not int or v < 1 or v > MAX_NUMBER:
        raise _Bad(code)
    return v


def _str(v, limit, code) -> str:
    if v is None:
        return ""
    if not isinstance(v, str) or len(v) > limit:
        raise _Bad(code)
    return v


def _time(p: dict) -> str:
    for key in ("review", "comment", "check_run", "check_suite", "merge_group", "pull_request", "issue"):
        o = _obj(p.get(key), "bad-shape")
        if o is None:
            continue
        for f in ("submitted_at", "completed_at", "updated_at", "created_at"):
            t = _str(o.get(f), MAX_TIME, "time-oversized")
            if t:
                return t
    return ""


def _head(p: dict) -> str | None:
    for key in ("pull_request", "check_run", "check_suite", "merge_group"):
        o = _obj(p.get(key))
        if o is None:
            continue
        head = _obj(o.get("head")) or {}
        for cand in (o.get("head_sha"), head.get("sha")):
            if isinstance(cand, str) and SHA_RE.fullmatch(cand):
                return cand
    return None


def _ref(p: dict) -> str:
    for key in ("review", "comment", "check_run", "check_suite"):
        o = _obj(p.get(key))
        if o is None:
            continue
        v = o.get("id") if type(o.get("id")) is int else o.get("node_id")
        if v is not None:
            return _str(str(v), MAX_REF, "ref-oversized")
    return ""


def _prs(event: str, p: dict) -> list[int]:
    """PR numbers named, validated whole:  one bad entry fails the payload."""
    if event in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        pr = _obj(p.get("pull_request"))
        n = pr.get("number") if pr is not None else p.get("number")
        if n is None:
            return []
        return [_num(n)]
    if event in ("check_run", "check_suite"):
        o = _obj(p.get(event))
        prs = o.get("pull_requests") if o is not None else None
        if prs is None:
            return []
        if not isinstance(prs, list):
            raise _Bad("pull-requests-not-list")
        if len(prs) > MAX_TARGETS:
            raise _Bad("too-many-targets")
        out = set()
        for x in prs:
            if not isinstance(x, dict):
                raise _Bad("pull-request-entry-invalid")
            out.add(_num(x.get("number"), "pull-request-entry-invalid"))
        return sorted(out)
    return []


def _res(outcome, reason, event="", did="", repo="", n=None, hints=(), head=None):
    return MappingResult(outcome, reason[:MAX_REASON], tuple(hints), event, did, repo, n, head)


def map_event(event_name: str, delivery_id: str, payload: dict, allowlist=frozenset()) -> MappingResult:
    """Pure and stateless.  Never returns an empty result without saying why.

    Invalid: non-string or overlong event name, missing/invalid/overlong
    delivery id (never truncated), non-object payload, malformed or oversized
    nested fields, bad numbers.  Ignored: unsupported event, repo not on the
    allowlist (empty allowlist denies all).  Held: check_run, check_suite and
    merge_group that name no PR, and every merge_group (a queue ref is never
    parsed for a PR), with the head as advisory only.  A payload is mapped
    whole or not at all:  one malformed target fails every target."""
    ev = event_name if isinstance(event_name, str) and 0 < len(event_name) <= MAX_EVENT else ""
    did = delivery_id if isinstance(delivery_id, str) and DELIVERY_RE.fullmatch(delivery_id) else ""
    if not ev:
        return _res(INVALID, "event-name-invalid", "", did)
    if not did:
        return _res(INVALID, "delivery-id-invalid", ev, "")
    if not isinstance(payload, dict):
        return _res(INVALID, "payload-not-object", ev, did)
    if ev not in EVENTS:
        return _res(IGNORED, "event-not-supported", ev, did)
    try:
        r = payload.get("repository")
        if not isinstance(r, dict):
            raise _Bad("repo-missing-or-malformed")
        repo = r.get("full_name")
        if not isinstance(repo, str) or len(repo) > MAX_REPO or not REPO_RE.fullmatch(repo):
            raise _Bad("repo-missing-or-malformed")
        if repo not in allowlist:
            return _res(IGNORED, "repo-not-allowlisted", ev, did, repo)
        t, head, ref = _time(payload), _head(payload), _ref(payload)
        action = _str(payload.get("action"), MAX_ACTION, "action-oversized")
        if ev in ("issues", "issue_comment"):
            issue = _obj(payload.get("issue"))
            if issue is None:
                raise _Bad("issue-missing")
            n = _num(issue.get("number"), "issue-number-invalid")
            is_pr = "pull_request" in issue
            if ev == "issue_comment":
                kind, subj = ("pr_comment", "pr") if is_pr else ("issue_comment_issue", "issue")
            else:
                kind, subj = ("pr_comment", "pr") if is_pr else ("issue", "issue")
            return _res(MAPPED, "mapped", ev, did, repo, n, (Hint(repo, n, subj, kind, did, t, None, ref, action),))
        if ev == "merge_group":
            _obj(payload.get("merge_group"))
            return _res(HELD, "merge-group-needs-discovery", ev, did, repo, None, (), head)
        nums = _prs(ev, payload)
        if not nums:
            if ev in _DISCOVERY:
                return _res(HELD, "names-no-pr-needs-discovery", ev, did, repo, None, (), head)
            raise _Bad("pr-number-missing")
        hs = tuple(Hint(repo, n, "pr", _KIND[ev], did, t, head, ref, action) for n in nums)
        return _res(MAPPED, "mapped", ev, did, repo, nums[0], hs)
    except _Bad as e:
        return _res(INVALID, str(e), ev, did)
