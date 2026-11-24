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
#: Completed-check conclusions reported as check_failure events.  The event
#: states the factual outcome (check name and conclusion on the current
#: head);  it does not assert the check is required or merge-blocking, which
#: the check-runs read cannot establish.  Success, neutral, skipped, stale
#: and cancelled conclusions are not reported.
CHECK_FAILURES = ("failure", "timed_out", "action_required", "startup_failure")


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

    # 4. A reviewer's latest decisive review is CHANGES_REQUESTED.  Decisive
    #    states are APPROVED, CHANGES_REQUESTED and DISMISSED;  COMMENTED and
    #    PENDING never supersede.  A request already superseded by the same
    #    reviewer's approval or dismissal is history and emits nothing.  The
    #    review bot is excluded:  its reviews already alert above.  Repeats
    #    are suppressed by the event store, not the numeric watermark:  review
    #    ids are not assumed to arrive in visibility order.  The event reports
    #    the factual review status;  it asserts no branch-protection rule.
    latest_decisive: dict[str, dict] = {}
    for review in reviews:
        rid = review.get("id")
        if not isinstance(rid, int):
            continue
        actor = _login(review)
        if not actor or str(review.get("state") or "") not in (
            "APPROVED",
            "CHANGES_REQUESTED",
            "DISMISSED",
        ):
            continue
        current = latest_decisive.get(actor)
        if current is None or rid > int(current.get("id") or 0):
            latest_decisive[actor] = review
    for actor, review in latest_decisive.items():
        rid = review["id"]
        if actor == review_actor or str(review.get("state")) != "CHANGES_REQUESTED":
            continue
        if rid <= int(cursor.get("review_seed") or 0):
            continue
        commit = str(review.get("commit_id") or "")
        events.append(
            {
                "repo": repo,
                "number": number,
                "kind": "changes_requested",
                "event_id": str(rid),
                "actor": actor,
                "head": commit,
                "current_head": current_head,
                "head_matches": bool(commit) and commit == current_head,
                "summary": "changes requested by %s" % actor,
            }
        )

    # 5. A completed check run on the CURRENT head with a failure-class
    #    conclusion, other than the review bot's own check (already alerted
    #    above).  Repeats are suppressed by the event store, not the numeric
    #    watermark:  run ids are not completion-ordered, so a higher-id
    #    terminal run must not hide a lower-id run still in progress.  The
    #    summary names the check and conclusion for the wake topic;  the
    #    Operator message body carries only the fixed generic status.  Whether
    #    the check is required is not claimed:  the read does not say.
    for run in check_runs:
        rid = run.get("id")
        if not isinstance(rid, int) or rid <= int(cursor.get("check_seed") or 0):
            continue
        if _kody_check(run):
            continue
        if str(run.get("status") or "") != "completed":
            continue
        head = str(run.get("head_sha") or "")
        if not head or head != current_head:
            continue
        conclusion = str(run.get("conclusion") or "")
        if conclusion not in CHECK_FAILURES:
            continue
        app = run.get("app")
        events.append(
            {
                "repo": repo,
                "number": number,
                "kind": "check_failure",
                "event_id": str(rid),
                "actor": str(app.get("slug") or "github") if isinstance(app, dict) else "github",
                "head": head,
                "current_head": current_head,
                "head_matches": True,
                "summary": "check failed: %s (%s)"
                % (str(run.get("name") or "unnamed"), conclusion),
            }
        )
    return events
