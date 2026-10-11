"""One GraphQL census of the open pull requests in the org.  Read-only:  no mutation is ever sent.

    gh api graphql -f query=... -f q='org:Simple-With-Us is:pr is:open archived:false' -f endCursor=<cursor>

  * One search, `first: 30` a page, and the cursor variable is named exactly `$endCursor`.  (Under
    `gh api graphql --paginate` any other name makes gh refetch page 1 for ever.  This module pages by hand,
    so it can retry a page, and it fails the source if a cursor or a whole page comes back twice.)
  * A 502, 503 or 504 from GitHub is retried (three attempts, a growing wait).  A PR whose `mergeable` is
    still UNKNOWN (GitHub computes it lazily) is fetched again by node id a few times, then reported as
    UNKNOWN with `mergeable_pending` counted, which is not a failure.
  * A pull request from outside the org (a fork:  `isCrossRepository`, or a head repository whose owner is
    not the org, or none) is reduced to a count by repository.  Its title, branch, author, labels and files
    never leave this module.
  * Every free-text field (title, labels, check names, file paths, author) passes the F3 `render_untrusted`
    in its plain form.  A branch name stays as git spelled it when it is made of plain characters (it is a
    join key for claims and lanes), else it is cleaned and `head_ref_exact` is False.  The PR URL is built
    here from the repository name and number, never taken from the response.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from . import safety
from .runner import Runner, failed, ok, which

ORG = "Simple-With-Us"
SEARCH = "org:%s is:pr is:open archived:false" % ORG
PAGE_SIZE = 30
MAX_PAGES = 40            # 1,200 PRs;  GitHub search itself stops at 1,000 results
RETRY_STATUSES = ("502", "503", "504")
HTTP_ATTEMPTS = 3
MERGEABLE_ROUNDS = 3
TIMEOUT_S = 120

PR_FRAGMENT = """
fragment PrFields on PullRequest {
  id number title isDraft mergeable mergeStateStatus reviewDecision createdAt updatedAt
  headRefName isCrossRepository
  repository { nameWithOwner }
  headRepositoryOwner { login }
  author { login }
  labels(first: 20) { nodes { name } }
  autoMergeRequest { enabledAt mergeMethod }
  commits(last: 1) { nodes { commit { oid committedDate statusCheckRollup { state
    contexts(first: 50) { totalCount nodes { __typename
      ... on CheckRun { name status conclusion }
      ... on StatusContext { context state } } } } } } }
  reviewThreads(first: 100) { totalCount nodes { isResolved } }
  files(first: 100) { totalCount nodes { path } }
}
"""

SEARCH_QUERY = """
query($q: String!, $endCursor: String) {
  search(query: $q, type: ISSUE, first: %d, after: $endCursor) {
    issueCount
    pageInfo { hasNextPage endCursor }
    nodes { ...PrFields }
  }
}
""" % PAGE_SIZE + PR_FRAGMENT

NODES_QUERY = """
query($ids: [ID!]!) { nodes(ids: $ids) { ...PrFields } }
""" + PR_FRAGMENT

_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_OID = re.compile(r"^[0-9a-f]{40,64}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
_FAILING = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}


class GithubError(Exception):
    pass


def _iso(value: Any) -> str | None:
    return value if isinstance(value, str) and _ISO.match(value) else None


def _word(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Z_]{1,40}", value) else None


def _graphql(run: Runner, gh: str, query: str, strings: dict[str, str], arrays: dict[str, list[str]],
             sleep: Callable[[float], None]) -> dict:
    """One `gh api graphql` call with 5xx retries.  Returns the response's `data` object."""
    argv = [gh, "api", "graphql", "-f", "query=" + query]
    for key, value in strings.items():
        argv += ["-f", "%s=%s" % (key, value)]
    for key, values in arrays.items():
        for value in values:
            argv += ["-f", "%s[]=%s" % (key, value)]
    last = "no attempt"
    for attempt in range(HTTP_ATTEMPTS):
        result = run(argv, timeout=TIMEOUT_S)
        if result.reason:
            raise GithubError("gh %s" % result.reason)
        if result.rc != 0:
            text = result.err or result.out
            if any(("HTTP %s" % code) in text for code in RETRY_STATUSES):
                last = "HTTP 5xx"
                if attempt + 1 < HTTP_ATTEMPTS:
                    sleep(2.0 * (attempt + 1))
                continue
            first = (text.strip().splitlines() or ["gh exited %s" % result.rc])[0]
            raise GithubError(safety.clean(first, 160))
        try:
            body = json.loads(result.out)
        except ValueError:
            raise GithubError("gh did not return JSON")
        if not isinstance(body, dict):
            raise GithubError("gh returned an unexpected shape")
        if body.get("errors") and not body.get("data"):
            types = sorted({safety.clean(e.get("type") or e.get("message"), 40) for e in body["errors"] if isinstance(e, dict)})
            raise GithubError("GraphQL errors: " + ", ".join(types)[:160])
        data = body.get("data")
        if not isinstance(data, dict):
            raise GithubError("no data in the response")
        return data
    raise GithubError("gave up after %d attempts (%s)" % (HTTP_ATTEMPTS, last))


def _fetch_all(run: Runner, gh: str, sleep: Callable[[float], None]) -> tuple[list[dict], dict]:
    nodes: list[dict] = []
    seen_cursors: set[str] = set()
    seen_ids: set[str] = set()
    cursor: str | None = None
    meta = {"pages": 0, "issue_count": None, "complete": True, "incomplete_reason": None}
    while True:
        strings = {"q": SEARCH}
        if cursor is not None:
            strings["endCursor"] = cursor
        data = _graphql(run, gh, SEARCH_QUERY, strings, {}, sleep)
        search = data.get("search")
        if not isinstance(search, dict):
            raise GithubError("no search result in the response")
        meta["pages"] += 1
        if isinstance(search.get("issueCount"), int):
            meta["issue_count"] = search["issueCount"]
        page = [n for n in (search.get("nodes") or []) if isinstance(n, dict) and "number" in n]
        fresh = [n for n in page if n.get("id") not in seen_ids]
        if page and not fresh:
            raise GithubError("a page repeated (the cursor did not advance)")
        for n in fresh:
            seen_ids.add(n.get("id"))
        nodes.extend(fresh)
        info = search.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            return nodes, meta
        nxt = info.get("endCursor")
        if not isinstance(nxt, str) or not nxt or nxt == cursor or nxt in seen_cursors:
            raise GithubError("the cursor repeated")
        seen_cursors.add(nxt)
        cursor = nxt
        if meta["pages"] >= MAX_PAGES:
            meta["complete"] = False
            meta["incomplete_reason"] = "stopped after %d pages" % MAX_PAGES
            return nodes, meta


def _refetch_unknown(run: Runner, gh: str, nodes: list[dict], sleep: Callable[[float], None]) -> None:
    """Replace each node whose mergeable is UNKNOWN with a fresh copy, up to MERGEABLE_ROUNDS times."""
    for attempt in range(MERGEABLE_ROUNDS):
        pending = [n for n in nodes if n.get("mergeable") == "UNKNOWN" and isinstance(n.get("id"), str)]
        if not pending:
            return
        sleep(3.0 * (attempt + 1))
        for start in range(0, len(pending), 30):
            chunk = pending[start:start + 30]
            try:
                data = _graphql(run, gh, NODES_QUERY, {}, {"ids": [n["id"] for n in chunk]}, sleep)
            except GithubError:
                return  # leave them UNKNOWN;  that is reported, not a failure
            fresh = {n.get("id"): n for n in (data.get("nodes") or []) if isinstance(n, dict) and n.get("id")}
            for i, node in enumerate(nodes):
                if node.get("id") in fresh:
                    nodes[i] = fresh[node["id"]]


def _check_contexts(rollup: Any) -> tuple[str | None, list[dict], int, list[str]]:
    if not isinstance(rollup, dict):
        return None, [], 0, []
    contexts = rollup.get("contexts") or {}
    out: list[dict] = []
    failing: list[str] = []
    for item in (contexts.get("nodes") or []):
        if not isinstance(item, dict):
            continue
        if item.get("__typename") == "CheckRun":
            name = item.get("name")
            state = item.get("conclusion") if item.get("status") == "COMPLETED" else item.get("status")
        else:
            name = item.get("context")
            state = item.get("state")
        state = _word(state) or "UNKNOWN"
        label = safety.clean(name, 80)
        out.append({"name": label, "state": state})
        if state in _FAILING:
            failing.append(label)
    total = contexts.get("totalCount") if isinstance(contexts.get("totalCount"), int) else len(out)
    return _word(rollup.get("state")), out, total, failing[:20]


def _inside(node: dict) -> bool:
    if node.get("isCrossRepository") is not False:
        return False
    owner = ((node.get("headRepositoryOwner") or {}).get("login") or "")
    return owner.lower() == ORG.lower()


def _record(node: dict) -> dict | None:
    repo = ((node.get("repository") or {}).get("nameWithOwner") or "")
    number = node.get("number")
    if not _REPO.match(repo) or not isinstance(number, int) or isinstance(number, bool):
        return None
    ref, exact = safety.safe_ref(node.get("headRefName"))
    commit = (((node.get("commits") or {}).get("nodes") or [{}])[0] or {}).get("commit") or {}
    state, contexts, contexts_total, failing = _check_contexts(commit.get("statusCheckRollup"))
    threads = node.get("reviewThreads") or {}
    thread_nodes = [t for t in (threads.get("nodes") or []) if isinstance(t, dict)]
    threads_total = threads.get("totalCount") if isinstance(threads.get("totalCount"), int) else len(thread_nodes)
    files = node.get("files") or {}
    paths = [safety.clean((f or {}).get("path"), 200) for f in (files.get("nodes") or []) if isinstance(f, dict)]
    auto = node.get("autoMergeRequest")
    return {
        "repo": repo,
        "number": number,
        "url": "https://github.com/%s/pull/%d" % (repo, number),
        "title": safety.clean(node.get("title"), 200),
        "is_draft": node.get("isDraft") is True,
        "mergeable": _word(node.get("mergeable")) or "UNKNOWN",
        "merge_state": _word(node.get("mergeStateStatus")),
        "review_decision": _word(node.get("reviewDecision")),
        "created_at": _iso(node.get("createdAt")),
        "updated_at": _iso(node.get("updatedAt")),
        "head_ref": ref,
        "head_ref_exact": exact,
        "same_repo": True,
        "author": safety.safe_token((node.get("author") or {}).get("login")),
        "labels": [safety.clean(l.get("name"), 60) for l in ((node.get("labels") or {}).get("nodes") or []) if isinstance(l, dict)],
        "auto_merge": isinstance(auto, dict),
        "auto_merge_method": _word(auto.get("mergeMethod")) if isinstance(auto, dict) else None,
        "head_commit": {
            "oid": commit.get("oid") if isinstance(commit.get("oid"), str) and _OID.match(commit.get("oid")) else None,
            "committed_at": _iso(commit.get("committedDate")),
        },
        "checks": {"state": state, "total": contexts_total, "contexts": contexts, "failing": failing},
        "review_threads": {
            "total": threads_total,
            "unresolved": sum(1 for t in thread_nodes if t.get("isResolved") is False),
            "partial": threads_total > len(thread_nodes),
        },
        "files": paths,
        "files_total": files.get("totalCount") if isinstance(files.get("totalCount"), int) else len(paths),
    }


def build(nodes: list[dict], meta: dict) -> dict:
    """The data block from raw nodes.  Pure."""
    prs: list[dict] = []
    outside = {"count": 0, "by_repo": {}}
    for node in nodes:
        if not _inside(node):
            repo = ((node.get("repository") or {}).get("nameWithOwner") or "")
            key = repo if _REPO.match(repo) else "(other)"
            outside["count"] += 1
            outside["by_repo"][key] = outside["by_repo"].get(key, 0) + 1
            continue
        record = _record(node)
        if record is not None:
            prs.append(record)
    prs.sort(key=lambda p: (p["repo"], p["number"]))
    return {
        "prs": prs,
        "outside_fork_prs": {"count": outside["count"], "by_repo": dict(sorted(outside["by_repo"].items()))},
        "pages": meta["pages"],
        "issue_count": meta["issue_count"],
        "mergeable_pending": sum(1 for p in prs if p["mergeable"] == "UNKNOWN"),
    }


def gather(run: Runner, *, sleep: Callable[[float], None] = time.sleep) -> dict:
    """The `github` source."""
    gh = which("gh")
    if gh is None:
        return failed("gh is not installed")
    try:
        nodes, meta = _fetch_all(run, gh, sleep)
        _refetch_unknown(run, gh, nodes, sleep)
    except GithubError as exc:
        return failed(str(exc))
    data = build(nodes, meta)
    if not meta["complete"]:
        return failed("incomplete:  " + meta["incomplete_reason"], data)
    return ok(data)
