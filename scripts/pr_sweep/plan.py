"""plan: which PRs to work, in which order, in how many chains per repo.

* classify: a draft or a PR labelled `deferred` is `skip`; everything else is `merge` (merge by default, the
  owner-needed test in rules.md rule 10 decides a hold, not the plan).  A PR from a fork is `skip` too: a worker
  cannot push to its head branch.
* tier: small for Renovate, Dependabot and GitHub Actions PRs; frontier for CHANGES_REQUESTED, five or more
  unresolved threads, or a conflict with failing CI; mid otherwise.  Each platform maps a tier to a model.
* chains: PRs of one repo run one after another inside a chain (two workers in one repo at once collide on
  rebases and lockfiles).  A stack (a PR whose base is another open PR's head) stays together, parent first.
  Stacks are spread over at most `chains[repo]` chains (DEFAULT_CHAINS, else 1), easiest first, by load.
  Repos run in parallel.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .fleet import DEFAULT_CHAINS

BOT_AUTHORS = (
    "renovate",
    "dependabot",
    "github-actions",
    "renovate[bot]",
    "dependabot[bot]",
    "github-actions[bot]",
)


def classify(p: Mapping[str, Any]) -> dict[str, str]:
    labels = [str(x).lower() for x in p.get("labels") or []]
    if p.get("draft"):
        return {"mode": "skip", "why": "draft"}
    if "deferred" in labels:
        return {"mode": "skip", "why": "labelled deferred"}
    if p.get("cross_repo"):
        return {"mode": "skip", "why": "head branch is in a fork; workers cannot push to it"}
    return {"mode": "merge", "why": ""}


def tier(p: Mapping[str, Any]) -> str:
    if str(p.get("author") or "").lower() in BOT_AUTHORS:
        return "small"
    if (
        p.get("review") == "CHANGES_REQUESTED"
        or int(p.get("unresolved") or 0) >= 5
        or (p.get("mstate") == "DIRTY" and p.get("ci") == "FAILURE")
    ):
        return "frontier"
    return "mid"


def _ease(group: Sequence[Mapping[str, Any]]) -> int:
    return sum(
        int(p.get("unresolved") or 0)
        + (3 if p.get("mstate") == "DIRTY" else 0)
        + (3 if p.get("ci") == "FAILURE" else 0)
        + (3 if p.get("review") == "CHANGES_REQUESTED" else 0)
        for p in group
    )


def build_chains(
    prs: Sequence[Mapping[str, Any]], chains: Mapping[str, int] | None = None
) -> list[dict[str, Any]]:
    """[{full, repo, items: [pr, ...]}] with stacks kept together and parents first."""
    limits = dict(DEFAULT_CHAINS)
    limits.update(chains or {})
    by_repo: dict[str, list[Mapping[str, Any]]] = {}
    for p in prs:
        by_repo.setdefault(p["full"], []).append(p)
    out: list[dict[str, Any]] = []
    for full in sorted(by_repo, key=str.lower):
        ps = sorted(by_repo[full], key=lambda p: p["n"])
        by_head = {p["head"]: p for p in ps}
        parent: dict[int, int | None] = {}
        for p in ps:
            par = by_head.get(p["base"])
            parent[p["n"]] = par["n"] if par is not None and par["n"] != p["n"] else None

        def root_of(n: int, parent: dict[int, int | None] = parent) -> int:
            seen = set()
            while parent.get(n) is not None and n not in seen:
                seen.add(n)
                n = parent[n]  # type: ignore[assignment]
            return n

        def depth(n: int, parent: dict[int, int | None] = parent) -> int:
            d, seen = 0, set()
            while parent.get(n) is not None and n not in seen:
                seen.add(n)
                n = parent[n]  # type: ignore[assignment]
                d += 1
            return d

        groups: dict[int, list[Mapping[str, Any]]] = {}
        for p in ps:
            groups.setdefault(root_of(p["n"]), []).append(p)
        glist = [sorted(g, key=lambda p: (depth(p["n"]), p["n"])) for g in groups.values()]
        glist.sort(key=lambda g: (_ease(g), g[0]["n"]))
        repo = full.split("/", 1)[1]
        k = max(1, min(int(limits.get(repo, limits.get(full, 1))), len(glist)))
        buckets: list[dict[str, Any]] = [{"load": 0.0, "items": []} for _ in range(k)]
        for g in glist:
            buckets.sort(key=lambda b: b["load"])
            buckets[0]["items"].extend(g)
            buckets[0]["load"] += len(g) + _ease(g) / 4.0
        for b in buckets:
            if b["items"]:
                out.append({"full": full, "repo": repo, "items": list(b["items"])})
    return out


def stack_parent(
    p: Mapping[str, Any], allprs: Sequence[Mapping[str, Any]], default_branch: str = "main"
) -> Mapping[str, Any] | None:
    if p.get("base") in (p.get("default_branch") or default_branch, ""):
        return None
    for q in allprs:
        if q["full"] == p["full"] and q["head"] == p["base"] and q["n"] != p["n"]:
            return q
    return None


def make_plan(
    prs: Sequence[Mapping[str, Any]], chains: Mapping[str, int] | None = None
) -> dict[str, Any]:
    work, skipped = [], []
    for p in prs:
        c = classify(p)
        if c["mode"] == "skip":
            skipped.append(
                {"pr": f"{p['full']}#{p['n']}", "title": p.get("title", ""), "why": c["why"]}
            )
            continue
        item = dict(p)
        item.update(mode=c["mode"], tier=tier(p))
        par = stack_parent(p, prs)
        item["stack_parent"] = par["n"] if par else None
        work.append(item)
    built = build_chains(work, chains)
    per_repo: dict[str, int] = {}
    for ch in built:
        per_repo[ch["full"]] = per_repo.get(ch["full"], 0) + 1
    return {
        "counts": {
            "open": len(prs),
            "work": len(work),
            "skip": len(skipped),
            "chains": len(built),
            "repos": len(per_repo),
        },
        "chains_per_repo": per_repo,
        "chains": [
            {
                "full": ch["full"],
                "items": [
                    {
                        "n": p["n"],
                        "title": p.get("title", ""),
                        "mode": p["mode"],
                        "tier": p["tier"],
                        "head": p["head"],
                        "base": p["base"],
                        "stack_parent": p["stack_parent"],
                        "author": p.get("author", ""),
                    }
                    for p in ch["items"]
                ],
            }
            for ch in built
        ],
        "skipped": skipped,
    }
