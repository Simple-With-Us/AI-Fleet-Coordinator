"""Pure event derivation:  review activity in, alert events out.

Two completion signals, either of which completes a review round on a head:
a SUBMITTED REVIEW from the review bot, or the bot's CHECK RUN concluding on
the current head (Kody posts "Kody Code Review" from app 413034;  a head may
carry no such check, so absence is never assumed from another head's state).
Findings are INLINE REVIEW COMMENTS from the findings bots.  A successful
check conclusion means the review FINISHED, not that it found nothing:
findings, when any, are the comment events.

Semantic dedupe:  a head's completion is alerted once - a submitted review
and a completed check on the SAME head are one round, not two.  Head binding
is explicit:  every event records the head it was made against and whether
that is the PR's current head, so an old-head signal is never reported as
current-head completion.
"""

from __future__ import annotations

import re

KODY_LOGIN = "kody-ai[bot]"
SENTRY_LOGIN = "sentry[bot]"
KODY_APP_ID = 413034
KODY_CHECK_NAME = "Kody Code Review"

REVIEW_STATES = ("APPROVED", "CHANGES_REQUESTED", "COMMENTED")


def _login(item: dict) -> str:
    user = item.get("user")
    return str(user.get("login") or "") if isinstance(user, dict) else ""


def _kody_check(run: dict) -> bool:
    app = run.get("app")
    return (
        str(run.get("name") or "") == KODY_CHECK_NAME
        and isinstance(app, dict)
        and (app.get("id") == KODY_APP_ID or str(app.get("slug") or "") == "kody-ai")
    )


def merge_conflict(repo: str, number: int, pr: dict) -> list[dict]:
    """A confirmed conflicting commit pair, never an unknown/protection hold.

    Identity includes both commits, so a base-branch advance is a new conflict
    even when the PR head stays fixed. The event store dedupes the same pair
    across cycles and restarts. Existing conflicts alert on first observation:
    they remain actionable, unlike historical review activity.
    """
    if pr.get("state") != "open" or pr.get("mergeable") is not False:
        return []
    if pr.get("mergeable_state") != "dirty":
        return []
    head_row, base_row = pr.get("head"), pr.get("base")
    if not isinstance(head_row, dict) or not isinstance(base_row, dict):
        return []
    head, base = head_row.get("sha"), base_row.get("sha")
    if any(
        not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) for sha in (head, base)
    ):
        return []
    return [
        {
            "repo": repo,
            "number": number,
            "kind": "merge_conflict",
            "event_id": head + ":" + base,
            "actor": "github",
            "head": head,
            "current_head": head,
            "head_matches": True,
            "summary": "merge conflict: update the PR against its base branch",
        }
    ]


def derive(
    repo: str,
    number: int,
    current_head: str,
    reviews: list,
    comments: list,
    check_runs: list,
    cursor: dict,
    *,
    review_actor: str = KODY_LOGIN,
    finding_actors=(SENTRY_LOGIN, KODY_LOGIN),
) -> list[dict]:
    """The new alert events for one PR this cycle.

    cursor: {"review_id", "comment_id", "check_id", "completed_heads"} - the
    high-water ids and the heads whose completion round was already alerted.
    """
    events: list[dict] = []
    completed_heads = set(cursor.get("completed_heads") or ())

    # 1. A submitted review from the review bot completes a round on its
    #    commit.  Skipped when that head's round was already alerted (by a
    #    check, or an earlier review).
    for review in reviews:
        rid = review.get("id")
        if not isinstance(rid, int) or rid <= int(cursor.get("review_id") or 0):
            continue
        if _login(review) != review_actor:
            continue
        state = str(review.get("state") or "")
        if state not in REVIEW_STATES:
            continue
        commit = str(review.get("commit_id") or "")
        if commit and commit in completed_heads:
            continue
        events.append(
            {
                "repo": repo,
                "number": number,
                "kind": "review_submitted",
                "event_id": str(rid),
                "actor": review_actor,
                "head": commit,
                "current_head": current_head,
                "head_matches": bool(commit) and commit == current_head,
                "summary": "review completed: %s" % state.lower().replace("_", " "),
            }
        )
        if commit:
            completed_heads.add(commit)

    # 2. The review bot's check run concluding on the CURRENT head completes
    #    the round.  Only current-head checks alert:  a check on an older
    #    head says nothing about the code now proposed.  The conclusion is
    #    named;  "success" means the review ran to completion, never that it
    #    found nothing.
    for run in check_runs:
        rid = run.get("id")
        if not isinstance(rid, int) or rid <= int(cursor.get("check_id") or 0):
            continue
        if not _kody_check(run):
            continue
        if str(run.get("status") or "") != "completed":
            continue
        head = str(run.get("head_sha") or "")
        if not head or head != current_head or head in completed_heads:
            continue
        conclusion = str(run.get("conclusion") or "unknown")
        events.append(
            {
                "repo": repo,
                "number": number,
                "kind": "review_completed",
                "event_id": str(rid),
                "actor": review_actor,
                "head": head,
                "current_head": current_head,
                "head_matches": True,
                "summary": "review check completed: %s" % conclusion,
            }
        )
        completed_heads.add(head)

    # 3. New inline findings from the findings bots, one event per batch.
    new_comments = [
        c
        for c in comments
        if isinstance(c.get("id"), int)
        and c["id"] > int(cursor.get("comment_id") or 0)
        and _login(c) in finding_actors
    ]
    if new_comments:
        heads = {str(c.get("original_commit_id") or "") for c in new_comments}
        actors: dict[str, int] = {}
        for c in new_comments:
            actors[_login(c)] = actors.get(_login(c), 0) + 1
        events.append(
            {
                "repo": repo,
                "number": number,
                "kind": "findings_added",
                "event_id": "c%d" % max(c["id"] for c in new_comments),
                "actor": "+".join(sorted(actors)),
                "head": sorted(heads)[-1] if heads else "",
                "current_head": current_head,
                "head_matches": heads == {current_head},
                "summary": "%d new finding comment%s (%s)"
                % (
                    len(new_comments),
                    "" if len(new_comments) == 1 else "s",
                    ", ".join("%s: %d" % kv for kv in sorted(actors.items())),
                ),
            }
        )
    return events
