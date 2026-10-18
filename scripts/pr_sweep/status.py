"""status and needs-work: the CURRENT facts of each PR, computed in code, and the repair decision.

Lesson from the 2026-10-16 sweep: its status step was an LLM that returned repo names of its own, the lookup by
name silently missed, every PR read as "needs work", and 203 agents ran.  Here:

  * one GraphQL call per repo (chunks of CHUNK PRs), each PR under the alias `p<number>`, and the answer is
    mapped back by the numbers that were REQUESTED, never by anything the response names;
  * a PR the response does not hold, or a repo whose call failed, gets `{"error": ...}` and needs_work raises
    StatusMissing for it.  A missing status never means "work on it";
  * `fresh` (activity in the last FRESH_MINUTES) is computed here from updatedAt and the head commit date, so a
    sweep can skip a PR a seat is editing right now before it launches anything.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .gh import Gh, GhError
from .inventory import ci_state

CHUNK = 20
FRESH_MINUTES = 15

PR_STATUS = """
  number state isDraft mergeStateStatus mergeable reviewDecision baseRefName headRefName headRefOid updatedAt
  mergedAt url title
  author { login }
  autoMergeRequest { enabledAt }
  labels(first: 30) { nodes { name } }
  commits(last: 1) { nodes { commit { oid committedDate statusCheckRollup { state } } } }
  reviewThreads(first: 100) { totalCount nodes { isResolved } }
  reviewRequests(first: 20) { nodes { requestedReviewer { __typename ... on User { login } ... on Team { slug } } } }
  latestReviews(first: 30) { nodes { state author { login } } }
"""


class StatusMissing(RuntimeError):
    """A PR has no status (the call failed or the PR was not in the answer).  Never treated as needing work."""


def parse_time(text: Any) -> _dt.datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        when = _dt.datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=_dt.timezone.utc)  # noqa: UP017 (3.9 has no datetime.UTC)


def is_bot(login: str) -> bool:
    low = (login or "").lower()
    return (
        low.endswith("[bot]")
        or low.endswith("-bot")
        or low in ("renovate", "dependabot", "github-actions", "kody-ai", "copilot", "sentry")
    )


def query_for(owner: str, repo: str, numbers: Sequence[int]) -> str:
    aliases = "\n".join(f"  p{n}: pullRequest(number: {int(n)}) {{ {PR_STATUS} }}" for n in numbers)
    # owner and repo come from GitHub's own search results or the registry; quote them as GraphQL strings
    return "query { repository(owner: %s, name: %s) {\n%s\n} }" % (_q(owner), _q(repo), aliases)


def _q(text: str) -> str:
    return '"' + str(text).replace("\\", "\\\\").replace('"', '\\"') + '"'


def facts(node: Mapping[str, Any], now: _dt.datetime) -> dict[str, Any]:
    commit = (((node.get("commits") or {}).get("nodes") or [{}])[-1] or {}).get("commit") or {}
    threads = node.get("reviewThreads") or {}
    tnodes = threads.get("nodes") or []
    total = int(threads.get("totalCount") or 0)
    updated, committed = parse_time(node.get("updatedAt")), parse_time(commit.get("committedDate"))
    newest = max([t for t in (updated, committed) if t is not None], default=None)
    age = (now - newest).total_seconds() / 60.0 if newest else None
    requests = []
    for r in (node.get("reviewRequests") or {}).get("nodes") or []:
        who = (r or {}).get("requestedReviewer") or {}
        requests.append(who.get("login") or ("team:" + str(who.get("slug"))))
    human_changes = sorted(
        {
            (rv.get("author") or {}).get("login") or ""
            for rv in (node.get("latestReviews") or {}).get("nodes") or []
            if isinstance(rv, dict)
            and rv.get("state") == "CHANGES_REQUESTED"
            and not is_bot((rv.get("author") or {}).get("login") or "")
        }
    )
    return {
        "n": int(node.get("number") or 0),
        "state": node.get("state") or "",
        "draft": bool(node.get("isDraft")),
        "mstate": node.get("mergeStateStatus") or "UNKNOWN",
        "mergeable": node.get("mergeable") or "UNKNOWN",
        "review": node.get("reviewDecision") or "",
        "base": node.get("baseRefName") or "",
        "head": node.get("headRefName") or "",
        "head_oid": node.get("headRefOid") or commit.get("oid") or "",
        "unresolved": sum(1 for t in tnodes if isinstance(t, dict) and not t.get("isResolved")),
        "threads": total,
        "threads_partial": total > len(tnodes),
        "ci": ci_state(commit.get("statusCheckRollup")),
        "auto": node.get("autoMergeRequest") is not None,
        "updated": node.get("updatedAt"),
        "committed": commit.get("committedDate"),
        "merged_at": node.get("mergedAt"),
        "minutes_since_activity": round(age, 1) if age is not None else None,
        "fresh": age is not None and age < FRESH_MINUTES,
        "author": ((node.get("author") or {}).get("login") or ""),
        "title": node.get("title") or "",
        "labels": [
            lb.get("name")
            for lb in (node.get("labels") or {}).get("nodes") or []
            if isinstance(lb, dict)
        ],
        "review_requests": requests,
        "human_changes_requested": [h for h in human_changes if h],
    }


def status_repo(
    gh: Gh, owner: str, repo: str, numbers: Sequence[int], now: _dt.datetime
) -> dict[int, dict[str, Any]]:
    """{number: facts} for exactly the requested numbers.  A failure becomes {"error": ...} per number."""
    out: dict[int, dict[str, Any]] = {}
    wanted = [int(n) for n in numbers]
    for i in range(0, len(wanted), CHUNK):
        chunk = wanted[i : i + CHUNK]
        try:
            body = gh.graphql(query_for(owner, repo, chunk))
        except GhError as exc:
            for n in chunk:
                out[n] = {"n": n, "error": str(exc)}
            continue
        repo_node = ((body.get("data") or {}).get("repository")) or None
        for n in chunk:
            node = (repo_node or {}).get(f"p{n}")
            if not isinstance(node, dict):
                why = (
                    "repository not found or not readable"
                    if repo_node is None
                    else "PR not in the answer"
                )
                out[n] = {"n": n, "error": why}
            elif int(node.get("number") or -1) != n:
                out[n] = {"n": n, "error": f"answer for p{n} carried number {node.get('number')}"}
            else:
                out[n] = facts(node, now)
    return out


def status_many(gh: Gh, keys: Iterable[str], now: _dt.datetime) -> dict[str, dict[str, Any]]:
    """{"owner/repo#n": facts} for every requested key, grouped into one call per repo."""
    by_repo: dict[tuple[str, str], list[int]] = {}
    order: list[str] = []
    for k in keys:
        full, n = split_key(k)
        owner, repo = full.split("/", 1)
        by_repo.setdefault((owner, repo), []).append(n)
        order.append(f"{full}#{n}")
    result: dict[str, dict[str, Any]] = {}
    for (owner, repo), numbers in by_repo.items():
        for n, f in status_repo(gh, owner, repo, numbers, now).items():
            result[f"{owner}/{repo}#{n}"] = f
    return {k: result.get(k, {"error": "not requested"}) for k in order}


def split_key(text: str) -> tuple[str, int]:
    """'owner/repo#123' -> ('owner/repo', 123).  Raises ValueError on anything else."""
    full, sep, num = text.strip().rpartition("#")
    if not sep or full.count("/") != 1 or not num.isdigit() or not all(full.split("/")):
        raise ValueError(f"expected owner/repo#number, got {text!r}")
    return full, int(num)


def needs_work(
    mode: str,
    s: Mapping[str, Any] | None,
    *,
    default_branch: str = "main",
    parent_state: str | None = None,
) -> tuple[bool, str]:
    """(needs a worker, why).  Raises StatusMissing for a PR without status: the caller stops, it never guesses.

    `parent_state` is the state of the PR whose head is this PR's base (a stack), when there is one."""
    if s is None or s.get("error"):
        raise StatusMissing(str((s or {}).get("error") or "no status"))
    if s.get("state") != "OPEN":
        return False, f"{str(s.get('state')).lower()}"
    if mode == "skip":
        return False, "skipped by plan (draft or deferred)"
    base = s.get("base") or default_branch
    stacked = base != default_branch
    if (
        stacked
        and parent_state == "OPEN"
        and not (s.get("unresolved") or s.get("mstate") == "DIRTY")
    ):
        return False, "stacked on an open parent PR: waits for it"
    if s.get("unresolved"):
        return True, f"{s['unresolved']} unresolved thread(s)"
    if s.get("mstate") in ("DIRTY", "BEHIND"):
        return True, f"mergeStateStatus {s['mstate']}"
    if s.get("ci") == "FAILURE":
        return True, "CI failing"
    if s.get("review") == "CHANGES_REQUESTED":
        return True, "changes requested"
    if stacked and parent_state != "OPEN":
        return (
            True,
            f"stacked on {base}, whose PR is {(parent_state or 'gone').lower()}: retarget to {default_branch}",
        )
    if (
        mode == "merge"
        and not s.get("auto")
        and s.get("mstate") in ("CLEAN", "UNSTABLE", "HAS_HOOKS")
    ):
        return True, "clean but not merged or armed"
    if s.get("mstate") == "BLOCKED" and s.get("ci") == "SUCCESS":
        return True, "blocked with green CI (review, label or protection)"
    if s.get("ci") == "PENDING":
        return False, "CI running"
    if s.get("auto"):
        return False, "auto-merge armed"
    return False, f"mergeStateStatus {s.get('mstate')}"
