"""inventory: every open PR across the fleet owners, as plain records.

One GraphQL search per owner (`is:pr is:open archived:false user:<owner>`, 50 a page, paged by hand with the
cursor variable named `$endCursor`), so the fleet repos of fleet-apps.json and every other repo of
Simple-With-Us and jaywedgeworth22 come back in a handful of calls.  A PR from a fork is kept but marked
`cross_repo` (its head branch is not in the repo; workers cannot push to it).

Record fields (also what `plan` reads):  owner, repo, full, n, title, url, author, head, base, head_oid, draft,
labels, created, updated, committed, mstate, review, threads, unresolved, threads_partial, ci, auto, cross_repo,
fleet.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .gh import Gh, GhError

PAGE = 50
MAX_PAGES = 25  # GitHub search stops at 1,000 results anyway

PR_FIELDS = """
  number title url isDraft createdAt updatedAt headRefName baseRefName headRefOid isCrossRepository
  mergeStateStatus reviewDecision
  repository { name owner { login } isArchived defaultBranchRef { name } }
  author { login }
  labels(first: 30) { nodes { name } }
  autoMergeRequest { enabledAt }
  commits(last: 1) { nodes { commit { oid committedDate statusCheckRollup { state } } } }
  reviewThreads(first: 100) { totalCount nodes { isResolved } }
"""

SEARCH = """
query($q: String!, $endCursor: String) {
  search(query: $q, type: ISSUE, first: %d, after: $endCursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ... on PullRequest { %s } }
  }
}
""" % (PAGE, PR_FIELDS)


def ci_state(rollup: Any) -> str:
    """SUCCESS, FAILURE, PENDING or NONE from a statusCheckRollup (GitHub's own summary of every check)."""
    state = (rollup or {}).get("state") if isinstance(rollup, dict) else None
    if state == "SUCCESS":
        return "SUCCESS"
    if state in ("FAILURE", "ERROR"):
        return "FAILURE"
    if state in ("PENDING", "EXPECTED"):
        return "PENDING"
    return "NONE"


def record(node: dict[str, Any], fleet_repos: Sequence[str] = ()) -> dict[str, Any] | None:
    """One PR node as a flat record, or None when the node is not a PR (search can return issues)."""
    if not isinstance(node, dict) or "number" not in node or not node.get("repository"):
        return None
    repo = node["repository"]
    owner = (repo.get("owner") or {}).get("login") or ""
    full = f"{owner}/{repo.get('name')}"
    commit = (((node.get("commits") or {}).get("nodes") or [{}])[-1] or {}).get("commit") or {}
    threads = node.get("reviewThreads") or {}
    nodes = threads.get("nodes") or []
    total = int(threads.get("totalCount") or 0)
    return {
        "owner": owner,
        "repo": repo.get("name"),
        "full": full,
        "n": int(node["number"]),
        "title": node.get("title") or "",
        "url": node.get("url") or f"https://github.com/{full}/pull/{node['number']}",
        "author": ((node.get("author") or {}).get("login") or "").replace("app/", ""),
        "head": node.get("headRefName") or "",
        "base": node.get("baseRefName") or "",
        "head_oid": node.get("headRefOid") or commit.get("oid") or "",
        "draft": bool(node.get("isDraft")),
        "labels": [
            lb.get("name")
            for lb in (node.get("labels") or {}).get("nodes") or []
            if isinstance(lb, dict) and lb.get("name")
        ],
        "created": node.get("createdAt"),
        "updated": node.get("updatedAt"),
        "committed": commit.get("committedDate"),
        "mstate": node.get("mergeStateStatus") or "UNKNOWN",
        "review": node.get("reviewDecision") or "",
        "threads": total,
        "unresolved": sum(1 for t in nodes if isinstance(t, dict) and not t.get("isResolved")),
        "threads_partial": total > len(nodes),
        "ci": ci_state(commit.get("statusCheckRollup")),
        "auto": node.get("autoMergeRequest") is not None,
        "cross_repo": bool(node.get("isCrossRepository")),
        "archived": bool(repo.get("isArchived")),
        "default_branch": ((repo.get("defaultBranchRef") or {}).get("name")) or "main",
        "fleet": full.lower() in {r.lower() for r in fleet_repos},
    }


def search_owner(gh: Gh, owner: str, fleet_repos: Sequence[str] = ()) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors = set()
    for _ in range(MAX_PAGES):
        body = gh.graphql(
            SEARCH, {"q": f"is:pr is:open archived:false user:{owner}", "endCursor": cursor}
        )
        search = ((body.get("data") or {}).get("search")) or {}
        for node in search.get("nodes") or []:
            rec = record(node, fleet_repos)
            if rec is not None and not rec["archived"]:
                out.append(rec)
        info = search.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        if not cursor or cursor in seen_cursors:
            raise GhError(
                f"search for {owner} returned a repeated or empty cursor; refusing to loop"
            )
        seen_cursors.add(cursor)
    return out


def inventory(
    gh: Gh, owners: Sequence[str], fleet_repos: Sequence[str] = ()
) -> list[dict[str, Any]]:
    """Every open PR of every owner, deduplicated by owner/repo#n and sorted."""
    found: dict[str, dict[str, Any]] = {}
    for owner in owners:
        for rec in search_owner(gh, owner, fleet_repos):
            found[f"{rec['full']}#{rec['n']}"] = rec
    return sorted(found.values(), key=lambda r: (r["full"].lower(), r["n"]))


def key(rec: dict[str, Any]) -> str:
    return f"{rec['full']}#{rec['n']}"
