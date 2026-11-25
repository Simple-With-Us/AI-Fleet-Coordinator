"""Pure event derivation:  review activity in, alert events out.

Two completion signals, either of which completes a review round on a head:
a SUBMITTED REVIEW from the review bot, or the bot's CHECK RUN concluding on
the current head (Kody posts "Kody Code Review" from app 413034;  a head may
carry no such check, so absence is never assumed from another head's state).
Findings are INLINE REVIEW COMMENTS from the findings bots.  A successful
check conclusion means the review FINISHED, not that it found nothing:
findings, when any, are the comment events.

Two states of the PR itself are events too, so the chat that owns the PR
hears what blocks it without polling:

  CI FAILED      a check run on the current head concluded in failure, timed
                 out or failed to start.  One event per cycle for the batch,
                 keyed by the runs it names and never by a high-water mark:  a
                 rerun that fails again is a new run and a new event, and one
                 that passes is none.
  MERGE BLOCKED  the current head cannot merge for a reason the owner must
                 clear:  conflicting files, a closed merge gate (the fleet's
                 guard fails its `merge-gate` check while a merge hold is
                 active or when it cannot vouch for the PR), or changes
                 requested that still stand by anyone but the completion bot:
                 Kody's own changes requested is already its review-completed
                 event, while another review bot's (Sentry's) is otherwise
                 silent and blocks like a person's.  Failed checks are named
                 beside those and never alone:
                 a failed check is the CI event.  Keyed by the head and the
                 blockers, so one head alerts once per blocker set, and a
                 conflict also includes the confirmed base commit in its identity.

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
#: The review bots whose findings are events:  measured over the last 46 PRs, Kody and
#: Sentry are the only bots that review;  every other bot author is a fleet seat replying in a
#: review thread, which must never wake the thread that wrote it.  A bot added here is a
#: findings bot at once;  the completion signal stays Kody's, the one with a check run.
REVIEW_BOTS = (SENTRY_LOGIN, KODY_LOGIN)

#: Check conclusions that mean the check itself failed.  A cancelled run (a newer push or
#: a hand) stopped without a result and one waiting on an approval has not run:  neither is.
CI_FAILURE_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
#: The fleet's own merge guard publishes this check (scripts/hold-guard):  it fails while any
#: merge hold is active and whenever the guard cannot vouch for the PR.
MERGE_GATE_CHECK = "merge-gate"
#: Checks whose failure says something other than "a test failed":  the merge gate is a
#: merge blocker with its own name, and the review bot's own check is the review-completion
#: signal, which has its own event.
CI_QUIET_CHECKS = frozenset({MERGE_GATE_CHECK, KODY_CHECK_NAME})
#: How many failed checks a summary names;  the rest are counted.  With the bound below it keeps
#: the wake's topic, the summary behind a `owner/repo#number: ` prefix, inside the 200
#: characters the router keeps, so no name is cut in the middle.
CI_NAMED = 2
#: A check name is workflow prose:  bounded and stripped to one line before it reaches a topic.
CHECK_NAME_CHARS = 48
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
#: What keeps a PR from merging, and how a summary names it.  `checks_failed` rides beside the
#: others and is never a merge event on its own:  a failed check is the CI event.
BLOCKER_LABELS = {
    "checks_failed": "checks failed",
    "changes_requested": "changes requested",
    "conflicts": "merge conflicts with the base branch",
    "merge_gate": "the merge gate is closed",
}
#: A reviewer's standing decision is the newest of these;  a comment or a pending review
#: never changes it (the branch rules do not dismiss stale reviews on a push, but an
#: explicit DISMISSED review is the reviewer taking the request back).
_DECISION_STATES = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")


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


def check_name(run: dict) -> str:
    """One failed check as a summary may name it:  one line, bounded, never empty."""
    name = _CONTROL_RE.sub(" ", str(run.get("name") or "")).strip()
    if not name:
        return "check #%s" % run.get("id")
    return name[:CHECK_NAME_CHARS]


def in_conflict(pull: dict) -> bool:
    """A confirmed conflicting commit pair, never an unknown/protection hold."""
    if pull.get("state") != "open" or pull.get("mergeable") is not False:
        return False
    if pull.get("mergeable_state") != "dirty":
        return False
    for field in ("head", "base"):
        row = pull.get(field)
        if not isinstance(row, dict) or not isinstance(row.get("sha"), str):
            return False
        if not re.fullmatch(r"[0-9a-f]{40}", row["sha"]):
            return False
    return True


def merge_conflict(repo: str, number: int, pull: dict) -> list[dict]:
    """Legacy inspection contract only. The cycle emits merge_blocked, not both."""
    if not in_conflict(pull):
        return []
    head, base = pull["head"]["sha"], pull["base"]["sha"]
    return [
        dict(
            repo=repo,
            number=number,
            kind="merge_conflict",
            event_id=head + ":" + base,
            actor="github",
            head=head,
            current_head=head,
            head_matches=True,
            summary="merge conflict: update the PR against its base branch",
        )
    ]


def newest_runs(check_runs: list, current_head: str) -> dict[str, dict]:
    """The newest run of each named check on the current head.

    A rerun is a new run beside the one it repeats, so a check is what its newest run says:
    one that failed and then passed has passed."""
    newest: dict[str, dict] = {}
    for run in check_runs:
        rid = run.get("id")
        name = str(run.get("name") or "")
        if not isinstance(rid, int):
            continue
        if str(run.get("head_sha") or "") != current_head:
            continue
        if name not in newest or rid > newest[name]["id"]:
            newest[name] = run
    return newest


def failed_checks(check_runs: list, current_head: str) -> list[dict]:
    """The checks of the current head whose newest run failed, the merge gate and the review
    bot's check left out:  each has its own meaning."""
    newest = newest_runs(check_runs, current_head)
    failed = []
    for name in sorted(newest):
        run = newest[name]
        if name in CI_QUIET_CHECKS or _kody_check(run):
            continue
        if str(run.get("status") or "") == "completed" and (
            str(run.get("conclusion") or "") in CI_FAILURE_CONCLUSIONS
        ):
            failed.append(run)
    return failed


def changes_requested(reviews: list, *, exclude=(KODY_LOGIN,)) -> bool:
    """Whether changes requested still stand from anyone but the completion bot.

    A reviewer's standing decision is their newest approving, changes-requested or
    dismissed review, on any head:  the branch rules do not dismiss stale reviews on a
    push, but an EXPLICIT dismissal is the reviewer taking the request back.  Only
    Kody is excluded here:  its decision is already the review-completed event, while
    another review bot's (Sentry's) would otherwise be silent and blocks like a
    person's."""
    standing: dict[str, tuple[int, str]] = {}
    for review in reviews:
        rid = review.get("id")
        state = str(review.get("state") or "")
        login = _login(review)
        if not isinstance(rid, int) or state not in _DECISION_STATES or not login:
            continue
        if login not in standing or rid > standing[login][0]:
            standing[login] = (rid, state)
    return any(
        state == "CHANGES_REQUESTED"
        for login, (_, state) in standing.items()
        if login not in exclude
    )


def merge_blockers(
    pull: dict | None,
    reviews: list,
    check_runs: list,
    current_head: str,
    *,
    exclude=(KODY_LOGIN,),
) -> list[str]:
    """What keeps the PR's current head from merging, named in a fixed order;  empty when
    nothing does.  Mergeability is read from the PR body:  without it a conflict is neither
    found nor ruled out, and the other blockers are still answered.  `exclude` is the
    completion bot(s) whose decision is already its own event."""
    blockers: set[str] = set()
    if pull is not None and in_conflict(pull) is True:
        blockers.add("conflicts")
    gate = newest_runs(check_runs, current_head).get(MERGE_GATE_CHECK)
    if (
        gate is not None
        and str(gate.get("status") or "") == "completed"
        and str(gate.get("conclusion") or "") in CI_FAILURE_CONCLUSIONS
    ):
        blockers.add("merge_gate")
    if changes_requested(reviews, exclude=exclude):
        blockers.add("changes_requested")
    if failed_checks(check_runs, current_head):
        blockers.add("checks_failed")
    if blockers == {"checks_failed"}:
        # Failed checks ride beside another blocker and never stand alone:  they are the CI
        # event, which has already named them.
        return []
    return [name for name in BLOCKER_LABELS if name in blockers]


def derive(
    repo: str,
    number: int,
    current_head: str,
    reviews: list,
    comments: list,
    check_runs: list,
    cursor: dict,
    *,
    pull: dict | None = None,
    review_actor: str = KODY_LOGIN,
    finding_actors=REVIEW_BOTS,
) -> list[dict]:
    """The new alert events for one PR this cycle.

    cursor: {"review_id", "comment_id", "check_id", "ci_ids", "ci_floor", "completed_heads"} -
    the high-water marks, the failed check runs already alerted, and the heads whose
    completion round was already alerted.
    `pull` is the PR body the cycle read;  its mergeability is what the conflict event
    reads, so without it (or without the fields) no conflict is derived either way.
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
                "source_time": review.get("submitted_at"),
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
                "source_time": run.get("completed_at"),
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

    # 4. Check runs on the CURRENT head that failed.  A run already named in an event is
    #    never named again, and a rerun is a new run.  The runs are tracked one by one and
    #    not by a high-water mark:  a slow job created before a fast one may fail after it,
    #    with the lower id.  An old head's failure says nothing about the code now proposed.
    floor = int(cursor.get("ci_floor") or 0)
    named = {int(i) for i in cursor.get("ci_ids") or []}
    newest = newest_runs(check_runs, current_head)
    failed = []
    for run in check_runs:
        rid = run.get("id")
        if not isinstance(rid, int) or rid <= floor or rid in named:
            continue
        head = str(run.get("head_sha") or "")
        if not head or head != current_head:
            continue
        # A rerun that failed and then passed has passed:  only a check's newest
        # run on this head is its verdict (the same rule the blockers read).
        if newest.get(str(run.get("name") or ""), {}).get("id") != rid:
            continue
        if str(run.get("status") or "") != "completed":
            continue
        if str(run.get("conclusion") or "") not in CI_FAILURE_CONCLUSIONS:
            continue
        if str(run.get("name") or "") in CI_QUIET_CHECKS or _kody_check(run):
            continue
        failed.append(run)
    if failed:
        names = sorted({check_name(run) for run in failed})
        shown = ", ".join(names[:CI_NAMED])
        more = len(names) - CI_NAMED
        if more > 0:
            shown += " (+%d more)" % more
        events.append(
            {
                "repo": repo,
                "number": number,
                "kind": "ci_failed",
                "event_id": "ci" + "+".join(str(i) for i in sorted(run["id"] for run in failed)),
                "actor": "",
                "head": current_head,
                "current_head": current_head,
                "head_matches": True,
                "summary": "CI failed on the current head: %s" % shown,
            }
        )

    # 5. What keeps the current head from merging.  Keyed by the head and the blockers:  one
    #    head alerts once per blocker set, and the store keeps a set from alerting twice.
    if current_head:
        known_requests = set(cursor.get("legacy_review_ids", []))
        blocker_reviews = [
            dict(row, state="DISMISSED")
            if row.get("id") in known_requests and row.get("state") == "CHANGES_REQUESTED"
            else row
            for row in reviews
        ]
        blockers = merge_blockers(
            pull, blocker_reviews, check_runs, current_head, exclude=(review_actor,)
        )
        base = pull["base"]["sha"] if "conflicts" in blockers else ""
        # Legacy outcomes remain immutable, including unknown. Remove only facts
        # already recorded, never a new gate, changed base, or newly failed run.
        if current_head + ":" + base in cursor.get(
            "legacy_conflict_pairs", []
        ) or current_head in cursor.get("legacy_conflict_heads", []):
            # Head-only #2510 records cannot establish a historical base. Keep
            # their conflict outcome bounded to that head without inventing it.
            blockers = [name for name in blockers if name != "conflicts"]
        known_components = cursor.get("legacy_blocker_components", {}).get(current_head, [])
        blockers = [name for name in blockers if name not in known_components]
        if blockers == ["checks_failed"]:
            blockers = []
        if blockers:
            identity = "mb%s:%s%s" % (
                current_head,
                base + ":" if "conflicts" in blockers else "",
                "+".join(blockers),
            )
            events.append(
                {
                    "repo": repo,
                    "number": number,
                    "kind": "merge_blocked",
                    "event_id": identity,
                    "actor": "",
                    "head": current_head,
                    "current_head": current_head,
                    "head_matches": True,
                    "summary": "blocked from merging: %s"
                    % ", ".join(BLOCKER_LABELS[name] for name in blockers),
                }
            )
    return events
