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
                      "check_run", "check_suite", "merge_group"})
ISSUE_KINDS = frozenset({"issue", "issue_comment_issue"})
MAPPED, IGNORED, HELD, INVALID = "mapped", "ignored", "held", "invalid"
MAX_BODY = 1024 * 1024
MAX_REASON = 80
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SIG_RE = re.compile(r"^sha256=[0-9a-f]{64}$")
MERGE_GROUP_REF_RE = re.compile(r"/pr-(\d{1,10})-")
_KIND = {"pull_request": "pr", "pull_request_review": "pr_review",
         "pull_request_review_comment": "pr_review_comment", "check_run": "check_run",
         "check_suite": "check_suite", "merge_group": "merge_group"}
_DISCOVERY = frozenset({"check_run", "check_suite", "merge_group"})


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
    event_name: str = ""
    delivery_id: str = ""
    repo: str = ""               # subject metadata, "" when unknown
    subject_number: int | None = None


def verify(body_bytes: bytes, signature_header: str | None, secret: bytes) -> bool:
    """True only for a valid strict sha256=<64 hex> signature over the exact body."""
    if not isinstance(secret, bytes) or not secret:
        return False
    if not isinstance(body_bytes, bytes) or len(body_bytes) > MAX_BODY:
        return False
    if not isinstance(signature_header, str) or not SIG_RE.match(signature_header):
        return False
    want = hmac.new(secret, body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, signature_header[7:])


def _s(v) -> str:
    return v if isinstance(v, str) else ""


def _time(p: dict) -> str:
    for key in ("review", "comment", "check_run", "check_suite", "merge_group", "pull_request", "issue"):
        o = p.get(key)
        if isinstance(o, dict):
            for f in ("submitted_at", "completed_at", "updated_at", "created_at"):
                if _s(o.get(f)):
                    return o[f]
    return ""


def _head(p: dict) -> str | None:
    for key in ("pull_request", "check_run", "check_suite", "merge_group"):
        o = p.get(key)
        if not isinstance(o, dict):
            continue
        head = o.get("head") if isinstance(o.get("head"), dict) else {}
        for cand in (o.get("head_sha"), head.get("sha")):
            if isinstance(cand, str) and SHA_RE.match(cand):
                return cand
    return None


def _ref(p: dict) -> str:
    for key in ("review", "comment", "check_run", "check_suite", "merge_group"):
        o = p.get(key)
        if isinstance(o, dict):
            v = o.get("id") if type(o.get("id")) is int else o.get("node_id")
            if v is not None and _s(str(v)):
                return str(v)[:80]
    return ""


def _prs(event: str, p: dict) -> list[int]:
    if event in ("pull_request", "pull_request_review", "pull_request_review_comment"):
        n = (p.get("pull_request") or {}).get("number") or p.get("number")
        return [n] if type(n) is int and n > 0 else []
    if event in ("check_run", "check_suite"):
        prs = (p.get(event) or {}).get("pull_requests") or []
        return sorted({x["number"] for x in prs if type(x) is dict and type(x.get("number")) is int and x["number"] > 0})
    if event == "merge_group":
        m = MERGE_GROUP_REF_RE.search(_s((p.get("merge_group") or {}).get("head_ref")))
        return [int(m.group(1))] if m else []
    return []


def _res(outcome, reason, event, did, repo="", n=None, hints=()):
    return MappingResult(outcome, reason[:MAX_REASON], tuple(hints), _s(event)[:64], _s(did)[:128], repo, n)


def map_event(event_name: str, delivery_id: str, payload: dict, allowlist=frozenset()) -> MappingResult:
    """Pure and stateless.  Never returns an empty result without saying why:
    unsupported events are ignored, malformed ones invalid, denied repos
    ignored (an empty allowlist denies all), and check or merge-group events
    naming no PR are HELD for canonical target discovery."""
    ev, did = event_name, delivery_id
    if not isinstance(ev, str) or not isinstance(payload, dict):
        return _res(INVALID, "malformed-input", _s(ev), did)
    if ev not in EVENTS:
        return _res(IGNORED, "event-not-supported", ev, did)
    repo = _s((payload.get("repository") or {}).get("full_name")) if isinstance(payload.get("repository"), dict) else ""
    if not REPO_RE.match(repo):
        return _res(INVALID, "repo-missing-or-malformed", ev, did)
    if repo not in allowlist:
        return _res(IGNORED, "repo-not-allowlisted", ev, did, repo)
    t, head, ref, action = _time(payload), _head(payload), _ref(payload), _s(payload.get("action"))[:40]
    if ev in ("issues", "issue_comment"):
        issue = payload.get("issue")
        n = issue.get("number") if isinstance(issue, dict) else None
        if type(n) is not int or n < 1:
            return _res(INVALID, "issue-number-missing", ev, did, repo)
        is_pr = "pull_request" in issue
        if ev == "issue_comment":
            kind, subj = ("pr_comment", "pr") if is_pr else ("issue_comment_issue", "issue")
        else:
            kind, subj = ("pr_comment", "pr") if is_pr else ("issue", "issue")
        h = Hint(repo, n, subj, kind, _s(did), t, None, ref, action)
        return _res(MAPPED, "mapped", ev, did, repo, n, (h,))
    nums = _prs(ev, payload)
    if not nums:
        if ev in _DISCOVERY:
            return _res(HELD, "names-no-pr-needs-discovery", ev, did, repo)
        return _res(INVALID, "pr-number-missing", ev, did, repo)
    hs = tuple(Hint(repo, n, "pr", _KIND[ev], _s(did), t, head, ref, action) for n in nums)
    return _res(MAPPED, "mapped", ev, did, repo, nums[0], hs)
