#!/usr/bin/env bash
# hold-guard orchestration: disarm-first, evaluate, repair, report, publish
# the merge-gate check.  Executed by hold-guard.yml AND by the integration
# tests (test_guard_integration.py runs THIS script against a fake gh), so
# the tested code is the shipped code.
#
# Required env: REPO (owner/repo), PR (pull number), HEAD_SHA, PR_URL.
# Optional env: GUARD_APP_SLUG (default github-actions).
#
# Error contract: every critical failure (disarm unconfirmed, evaluation,
# repair, comment, check list/patch/create) makes the job RED (exit 3).
# Fail-closed applies to evaluation and write FAILURES.  What we cannot do
# (documented, not claimed): if GitHub API writes themselves fail, no red
# check can be forced onto the PR - a previously green merge-gate may
# persist; the red job + notices are the signal, and the armer-side gate
# re-evaluates independently at arm time.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${EVENT_NAME:=pull_request_target}"
: "${PR:=}"
: "${GUARD_APP_SLUG:=github-actions}"
: "${REPO:?REPO required}" "${HEAD_SHA:?HEAD_SHA required}"

ERR=/tmp/hold-guard-errors; : > "$ERR"
PF=/tmp/hold-guard-patch-fail; : > "$PF"
any_error=0

echo "::group::hold-guard run (event=$EVENT_NAME repo=$REPO sha=$HEAD_SHA pr=${PR:-none})"

if [ -z "$PR" ]; then
  echo "::error::PR is required (merge_group support removed in slice 1)"
  exit 3
fi
prs=("$PR")

# 1. Disarm any armed auto-merge FIRST (disarming never merges).
#    Fleet contract (ST scripts/disable-pr-automerge.sh, ported inline into
#    AFC auto-merge-reusable.yml disarm job): success is defined by the
#    STATE READ (`gh pr view --json autoMergeRequest`), not by the mutation
#    exit code.  off = benign (armed or never armed); armed or unreadable
#    after the attempt = critical failure.
gh pr merge --disable-auto "$PR_URL" 2>&1 | head -3 || true
state="$(gh pr view "$PR_URL" --json autoMergeRequest \
  --jq 'if .autoMergeRequest == null then "off" else "armed" end' 2>/dev/null || true)"
[ -n "$state" ] || state=unreadable
case "$state" in
  off) : ;;
  *) echo "::error::auto-merge state after disarm attempt: $state ($PR_URL)"
     any_error=1 ;;
esac

# 2. Evaluate holds (fail closed per PR).
combined='[]'
for pr in "${prs[@]}"; do
  if out="$(python3 "$DIR/hold_guard.py" "$REPO" "$pr")"; then
    :
  else
    any_error=1
    out="$(printf '{"state":"error","error":"hold_guard rc: %s"}' "$out")"
  fi
  echo "$out" > "/tmp/hold-guard-$pr.json"
  combined="$(jq -s '.[0] + [ .[1].holds[]? | . + {pr: $pr} ]' --arg pr "$pr" <(echo "$combined") <(echo "$out"))"
done

# 3. Repairs + reports (labels via jq-built JSON, never word-split).
#    Pipelines run in subshells; failures are recorded via marker file.
printf '%s' "$combined" | jq -c '.[] | select(.needs_reapply)' | while IFS= read -r h; do
  pr=$(printf '%s' "$h" | jq -r .pr)
  if printf '%s' "$h" | jq -c '{labels: [.label]}' \
    | gh api "repos/$REPO/issues/$pr/labels" -X POST --input - >/dev/null; then
    echo "::notice::re-applied $(printf '%s' "$h" | jq -r .label) on #$pr"
  else
    echo "::error::failed to re-apply label on #$pr"
    echo repair >> "$ERR"
  fi
done
printf '%s' "$combined" | jq -c '.[] | select((.illegitimate_removals // []) | length > 0)' | while IFS= read -r h; do
  pr=$(printf '%s' "$h" | jq -r .pr)
  body=$(printf '%s' "$h" | jq -r '"Hold `\(.label)` was removed by \(.illegitimate_removals | join(", ")). Only its setter (\(.owner_login // "unknown")) can release it - the hold is back on."')
  gh pr comment "$pr" --repo "$REPO" --body "$body" >/dev/null \
    || { echo "::error::failed to comment on #$pr"; echo comment >> "$ERR"; }
done
[ -s "$ERR" ] && any_error=1

# 4. Fresh-state fence: re-evaluate IMMEDIATELY before publishing so the
#    written conclusion reflects state at write time (seconds), not the
#    start of this run.  A still-later change is covered by the next queued
#    run; per-run freshness is the strongest asynchronous guarantee here.
fresh='[]'
fresh_error=0
for pr in "${prs[@]}"; do
  if fout="$(python3 "$DIR/hold_guard.py" "$REPO" "$pr")"; then
    fresh="$(jq -s '.[0] + [ .[1].holds[]? | . + {pr: $pr} ]' --arg pr "$pr" <(echo "$fresh") <(echo "$fout"))"
  else
    fresh_error=1
  fi
done
echo "$fresh" > /tmp/hold-guard-combined.json

# 5. Publish the merge-gate conclusion (always attempted).
if [ "$any_error" -eq 1 ] || [ "$fresh_error" -eq 1 ]; then
  conclusion=failure; title="merge-gate: evaluation error"
  summary="Hold evaluation or a guard step failed (see workflow log). Failing closed."
elif [ "$(printf '%s' "$fresh" | jq 'length')" -eq 0 ]; then
  conclusion=success; title="merge-gate: no active merge holds"
  summary="All holds were released by their setters."
else
  conclusion=failure; title="merge-gate: active merge holds"
  summary="$(jq -r '[.[] | "- `\(.label)` (PR #\(.pr)) set by \(.owner_login // "unknown")\(if .unknown then " - owner unknown, needs escalation" else "" end)"] | join("\n")' /tmp/hold-guard-combined.json)"
fi
payload="$(jq -n --arg sha "$HEAD_SHA" --arg c "$conclusion" --arg t "$title" --arg s "$summary" \
  '{name: "merge-gate", head_sha: $sha, status: "completed", conclusion: $c, output: {title: $t, summary: $s}}')"

# Only runs owned by OUR app identity are eligible to PATCH (no foreign
# probing: a 403 we never trigger is a mutation we never attempt).
if runs_json="$(gh api "repos/$REPO/commits/$HEAD_SHA/check-runs?check_name=merge-gate")"; then
  owned="$(printf '%s' "$runs_json" | jq -r --arg s "$GUARD_APP_SLUG" '.check_runs[] | select(.app.slug == $s) | .id')"
else
  echo "::error::could not list merge-gate runs for $HEAD_SHA"
  any_error=1
  owned=""
fi
printf '%s\n' "$owned" | while IFS= read -r run_id; do
  [ -z "$run_id" ] && continue
  if ! printf '%s' "$payload" | gh api "repos/$REPO/check-runs/$run_id" -X PATCH --input - >/dev/null; then
    echo fail >> "$PF"
    echo "::error::failed to patch owned merge-gate run $run_id"
  fi
done
[ -s "$PF" ] && any_error=1
# POST when we own no run on this SHA OR any owned PATCH failed, so our
# identity carries the current conclusion whenever the API allows it.
if [ -z "$(printf '%s' "$owned" | tr -d '[:space:]')" ] || [ -s "$PF" ]; then
  if ! printf '%s' "$payload" | gh api "repos/$REPO/check-runs" -X POST --input - >/dev/null; then
    echo "::error::failed to create merge-gate check run"
    any_error=1
  fi
fi

echo "::endgroup::"
[ "$any_error" -eq 1 ] && exit 3
exit 0
