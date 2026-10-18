"""audit: the DETERMINISTIC part of the sweep audit, read from GitHub (GraphQL plus one REST call per merge).

Checks (each a violation with evidence; `--since` limits the event checks to the sweep window):
  V1  a head_ref_force_pushed event                      (rule 5 and 8: never force-push)
  V2  a review dismissal of anyone's review but kody-ai[bot]'s, or by an actor not on the allowed list (rule 7)
  V3  a resolved thread with no reply from anyone but its first author and no PR commit after its first comment
      (rule 6: a real reply before resolving).  GitHub keeps who resolved a thread but not when, so "later commit"
      is any commit on the PR after the thread opened; this errs toward NOT flagging.
  V4  a PR merged while a human CHANGES_REQUESTED review, a pending human review request or an unresolved
      thread is still on it
  V5  conflict markers (<<<<<<< , >>>>>>> , a bare =======) on lines the merge commit added to the default branch
  V6  a lint or test baseline file changed, or a test file deleted, by the merge commit (warning)
  V7  the merge commit changed CODEOWNERS or a merge-gate or auto-merge workflow (warning: those need the owner)
  V8  a PR comment by a person asking Codex or an autofix bot to FIX something (an `@codex review` is fine)

What it cannot judge (a fix that is wrong, a reply that does not hold up) stays with a reviewer.
"""

from __future__ import annotations

import datetime as _dt
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .gh import Gh, GhError
from .status import is_bot, parse_time, split_key

DEFAULT_ALLOWED_DISMISSERS = ("jaywedgeworth22",)
KODY = ("kody-ai", "kody-ai[bot]")
_MARKER = re.compile(r"^\+(?:<<<<<<< |>>>>>>> |<<<<<<<$|>>>>>>>$|=======$)")
_BASELINE = re.compile(r"(?i)(baseline|\.oxlint|eslint-baseline|lint-baseline|\.snap$)")
_TEST = re.compile(r"(?i)(^|/)(tests?|__tests__|spec)(/|$)|[._-](test|spec)\.[a-z]+$")
_SAFETY = re.compile(
    r"(?i)(^|/)CODEOWNERS$|^\.github/workflows/[^/]*(merge-gate|auto-?merge|automerge)[^/]*\.ya?ml$"
)
_FIX_ASK = re.compile(r"(?i)@codex\b(?!\s+review\b)|@\w*autofix\w*|/autofix\b|\bautofix\.ci\b")

AUDIT_QUERY = """
query($owner: String!, $repo: String!, $n: Int!) { repository(owner: $owner, name: $repo) { pullRequest(number: $n) {
  number state mergedAt baseRefName mergeCommit { oid }
  commits(last: 100) { nodes { commit { oid committedDate } } }
  reviewThreads(first: 100) { nodes { isResolved path resolvedBy { login }
    comments(first: 50) { nodes { author { login } createdAt } } } }
  reviewRequests(first: 20) { nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug } } } }
  latestReviews(first: 30) { nodes { state author { login } } }
  timelineItems(first: 100, itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT, REVIEW_DISMISSED_EVENT, MERGED_EVENT]) {
    nodes { __typename
      ... on HeadRefForcePushedEvent { createdAt actor { login } beforeCommit { oid } afterCommit { oid } }
      ... on ReviewDismissedEvent { createdAt actor { login } dismissalMessage previousReviewState
                                     review { author { login } state } }
      ... on MergedEvent { createdAt actor { login } } } }
  comments(last: 100) { nodes { author { login } createdAt body url } }
} } }
"""


def _login(obj: Any) -> str:
    return ((obj or {}).get("login") or "") if isinstance(obj, dict) else ""


def _after(when: str | None, since: _dt.datetime | None) -> bool:
    if since is None:
        return True
    t = parse_time(when)
    return t is None or t >= since


def check_pr(
    pr: Mapping[str, Any],
    files: Sequence[Mapping[str, Any]] | None,
    *,
    since: _dt.datetime | None = None,
    allowed_dismissers: Sequence[str] = DEFAULT_ALLOWED_DISMISSERS,
) -> dict[str, list[dict[str, str]]]:
    """Pure: the violations and warnings for one PR node (AUDIT_QUERY's pullRequest) and its merge-commit files."""
    v: list[dict[str, str]] = []
    w: list[dict[str, str]] = []
    allowed = {a.lower() for a in allowed_dismissers}
    for ev in (pr.get("timelineItems") or {}).get("nodes") or []:
        kind = ev.get("__typename")
        if not _after(ev.get("createdAt"), since):
            continue
        if kind == "HeadRefForcePushedEvent":
            v.append(
                {
                    "rule": "V1",
                    "evidence": f"force push by {_login(ev.get('actor')) or '?'} at {ev.get('createdAt')}: "
                    f"{((ev.get('beforeCommit') or {}).get('oid') or '?')[:8]} -> "
                    f"{((ev.get('afterCommit') or {}).get('oid') or '?')[:8]}",
                }
            )
        elif kind == "ReviewDismissedEvent":
            author = _login((ev.get("review") or {}).get("author"))
            actor = _login(ev.get("actor"))
            if author.lower() not in KODY:
                v.append(
                    {
                        "rule": "V2",
                        "evidence": f"{actor or '?'} dismissed a review by {author or '?'} "
                        f"({ev.get('previousReviewState')}) at {ev.get('createdAt')}; only stale Kody reviews "
                        "(kody-ai or kody-ai[bot]) may be dismissed",
                    }
                )
            elif actor.lower() not in allowed:
                v.append(
                    {
                        "rule": "V2",
                        "evidence": f"kody-ai review dismissed by {actor or '?'}, not on the allowed list "
                        f"({', '.join(sorted(allowed))}) at {ev.get('createdAt')}",
                    }
                )
    commit_times = [
        parse_time(((c or {}).get("commit") or {}).get("committedDate"))
        for c in (pr.get("commits") or {}).get("nodes") or []
    ]
    commit_times = [t for t in commit_times if t is not None]
    unresolved = 0
    for t in (pr.get("reviewThreads") or {}).get("nodes") or []:
        comments = (t.get("comments") or {}).get("nodes") or []
        if not t.get("isResolved"):
            unresolved += 1
            continue
        if not comments:
            continue
        first_author = _login(comments[0].get("author"))
        opened = parse_time(comments[0].get("createdAt"))
        replied = any(_login(c.get("author")) != first_author for c in comments[1:])
        later_commit = opened is not None and any(ct > opened for ct in commit_times)
        if not replied and not later_commit:
            v.append(
                {
                    "rule": "V3",
                    "evidence": f"thread on {t.get('path')} by {first_author or '?'} resolved by "
                    f"{_login(t.get('resolvedBy')) or '?'} with no reply and no later commit",
                }
            )
    if pr.get("state") == "MERGED":
        human_cr = sorted(
            {
                _login(r.get("author"))
                for r in (pr.get("latestReviews") or {}).get("nodes") or []
                if r.get("state") == "CHANGES_REQUESTED" and not is_bot(_login(r.get("author")))
            }
        )
        if human_cr:
            v.append(
                {
                    "rule": "V4",
                    "evidence": f"merged with CHANGES_REQUESTED from {', '.join(human_cr)}",
                }
            )
        pending = []
        for r in (pr.get("reviewRequests") or {}).get("nodes") or []:
            who = (r or {}).get("requestedReviewer") or {}
            name = who.get("login") or (("team:" + who["slug"]) if who.get("slug") else "")
            if name and not is_bot(name):
                pending.append(name)
        if pending:
            v.append(
                {
                    "rule": "V4",
                    "evidence": f"merged with a pending review request to {', '.join(pending)}",
                }
            )
        if unresolved:
            v.append({"rule": "V4", "evidence": f"merged with {unresolved} unresolved thread(s)"})
    for f in files or []:
        name = str(f.get("filename") or "")
        patch = str(f.get("patch") or "")
        hits = [ln for ln in patch.splitlines() if _MARKER.match(ln)]
        if hits:
            v.append(
                {
                    "rule": "V5",
                    "evidence": f"{name}: the merge added {len(hits)} conflict-marker line(s), first "
                    f"{hits[0][:60]!r}",
                }
            )
        if _BASELINE.search(name) and f.get("status") in ("modified", "added"):
            w.append({"rule": "V6", "evidence": f"baseline file changed: {name}"})
        if f.get("status") == "removed" and _TEST.search(name):
            w.append({"rule": "V6", "evidence": f"test file deleted: {name}"})
        if _SAFETY.search(name):
            w.append(
                {
                    "rule": "V7",
                    "evidence": f"merge-safety control changed: {name} (needs the owner)",
                }
            )
    for c in (pr.get("comments") or {}).get("nodes") or []:
        author = _login(c.get("author"))
        if is_bot(author) or not _after(c.get("createdAt"), since):
            continue
        body = str(c.get("body") or "")
        if _FIX_ASK.search(body):
            v.append(
                {
                    "rule": "V8",
                    "evidence": f"comment by {author or '?'} asks a bot to fix: {c.get('url') or ''}",
                }
            )
    return {"violations": v, "warnings": w}


def audit(
    gh: Gh,
    keys: Sequence[str],
    *,
    since: _dt.datetime | None = None,
    allowed_dismissers: Sequence[str] = DEFAULT_ALLOWED_DISMISSERS,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k in keys:
        full, n = split_key(k)
        owner, repo = full.split("/", 1)
        try:
            body = gh.graphql(AUDIT_QUERY, {"owner": owner, "repo": repo, "n": n})
            pr = (((body.get("data") or {}).get("repository") or {}).get("pullRequest")) or None
            if pr is None:
                raise GhError("PR not found")
            files = None
            oid = (pr.get("mergeCommit") or {}).get("oid")
            if pr.get("state") == "MERGED" and oid:
                files = (gh.rest(f"repos/{owner}/{repo}/commits/{oid}") or {}).get("files") or []
            res = check_pr(pr, files, since=since, allowed_dismissers=allowed_dismissers)
            out.append(
                {"pr": f"{full}#{n}", "state": pr.get("state"), "ok": not res["violations"], **res}
            )
        except GhError as exc:
            out.append(
                {
                    "pr": f"{full}#{n}",
                    "ok": False,
                    "error": str(exc),
                    "violations": [],
                    "warnings": [],
                }
            )
    return out
