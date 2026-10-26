#!/usr/bin/env bash
# hold-guard orchestration: disarm-first, evaluate, repair, report, publish
# the merge-gate check.  Executed by hold-guard.yml AND by the integration
# tests (test_guard_integration.py runs THIS script against a fake gh), so
# the tested code is the shipped code.
#
# Required env: REPO (owner/repo), HEAD_SHA (SHA the gate check targets)
# Optional env: PR (pull number; empty under merge_group), PR_URL, EVENT_NAME
# Exit: 0 evaluated (held or clear); 3 if any evaluation errored (a failure
# check is still attempted - absent check + red run is the loud failure mode).
#
# Quoting rules: every API-derived value is either passed through jq-built
# JSON (--input -) or read line-wise with IFS= read -r - never word-split.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${EVENT_NAME:=pull_request_target}"
: "${PR:=}"

echo "::group::hold-guard run (event=$EVENT_NAME repo=$REPO sha=$HEAD_SHA pr=${PR:-none})"

# 1. Disarm any armed auto-merge FIRST (disarming never merges).
if [ "$EVENT_NAME" = "pull_request_target" ] && [ -n "$PR" ]; then
  gh pr merge --auto --disable "$PR_URL" || true
fi

# 2. Which PRs to evaluate.
prs=()
if [ -n "$PR" ]; then
  prs=("$PR")
else
  # merge_group: map the group head SHA back to its PRs
  mapfile -t prs < <(gh api "repos/$REPO/commits/$HEAD_SHA/pulls" --jq '.[].number')
fi
if [ "${#prs[@]}" -eq 0 ]; then
  echo "::notice::no PRs resolved for $HEAD_SHA - nothing to evaluate"
fi

# 3. Evaluate each PR (fail-closed per PR on error).
any_error=0
any_held=0
combined='[]'
for pr in "${prs[@]}"; do
  if out="$(python3 "$DIR/hold_guard.py" "$REPO" "$pr")"; then
    :
  else
    rc=$?
    any_error=1
    out="$(printf '{"state":"error","error":"hold_guard rc=%s: %s"}' "$rc" "$out")"
  fi
  echo "$out" > "/tmp/hold-guard-$pr.json"
  [ "$(printf '%s' "$out" | jq -r .state)" = "held" ] && any_held=1
  combined="$(jq -s '.[0] + [ .[1].holds[]? | . + {pr: $pr} ]' --arg pr "$pr" <(echo "$combined") <(echo "$out"))"
done
echo "$combined" > /tmp/hold-guard-combined.json

# 4. Repairs + reports (labels via jq-built JSON, never word-split).
printf '%s' "$combined" | jq -c '.[] | select(.needs_reapply)' | while IFS= read -r h; do
  pr=$(printf '%s' "$h" | jq -r .pr)
  printf '%s' "$h" | jq -c '{labels: [.label]}' \
    | gh api "repos/$REPO/issues/$pr/labels" -X POST --input - >/dev/null \
    && echo "::notice::re-applied $(printf '%s' "$h" | jq -r .label) on #$pr" \
    || echo "::error::failed to re-apply label on #$pr"
done
printf '%s' "$combined" | jq -c '.[] | select((.illegitimate_removals // []) | length > 0)' | while IFS= read -r h; do
  pr=$(printf '%s' "$h" | jq -r .pr)
  body=$(printf '%s' "$h" | jq -r '"Hold `\(.label)` was removed by \(.illegitimate_removals | join(", ")). Only its setter (\(.owner_login // "unknown")) can release it - the hold is back on."')
  gh pr comment "$pr" --repo "$REPO" --body "$body" >/dev/null \
    || echo "::error::failed to comment on #$pr"
done

# 5. Publish the merge-gate conclusion (always attempted).
#    GitHub's selection among multiple same-name check runs on one SHA is
#    not doc-verified, so this does NOT rely on "latest run wins": every
#    existing merge-gate run on this SHA created by our identity is PATCHED
#    to the current conclusion, and a new run is only POSTed when none
#    exist.  Runs from a foreign app 403 on PATCH - tolerated and counted;
#    binding the required check to our App (slice 4) resolves that class.
if [ "$any_error" -eq 1 ]; then
  conclusion=failure; title="merge-gate: evaluation error"
  summary="Hold evaluation failed (see workflow log). Failing closed."
elif [ "$any_held" -eq 1 ]; then
  conclusion=failure; title="merge-gate: active merge holds"
  summary="$(jq -r '[.[] | "- `\(.label)` (PR #\(.pr)) set by \(.owner_login // "unknown")\(if .unknown then " - owner unknown, needs escalation" else "" end)"] | join("\n")' /tmp/hold-guard-combined.json)"
else
  conclusion=success; title="merge-gate: no active merge holds"
  summary="All holds were released by their setters."
fi
payload="$(jq -n --arg sha "$HEAD_SHA" --arg c "$conclusion" --arg t "$title" --arg s "$summary" \
  '{name: "merge-gate", head_sha: $sha, status: "completed", conclusion: $c, output: {title: $t, summary: $s}}')"
existing="$(gh api "repos/$REPO/commits/$HEAD_SHA/check-runs?check_name=merge-gate" --jq '.check_runs[].id' 2>/dev/null || true)"
patch_fail=/tmp/hold-guard-patch-fail
: > "$patch_fail"
printf '%s\n' "$existing" | while IFS= read -r run_id; do
  [ -z "$run_id" ] && continue
  if ! printf '%s' "$payload" | gh api "repos/$REPO/check-runs/$run_id" -X PATCH --input - >/dev/null 2>&1; then
    echo fail >> "$patch_fail"
    echo "::notice::merge-gate run $run_id not patchable by this identity (foreign app?) - left as-is"
  fi
done
# POST a fresh run when none existed OR any existing run rejected our PATCH,
# so our identity always carries the current conclusion on this SHA.
if [ -z "$(printf '%s' "$existing" | tr -d '[:space:]')" ] || [ -s "$patch_fail" ]; then
  printf '%s' "$payload" | gh api "repos/$REPO/check-runs" -X POST --input - >/dev/null \
    || echo "::error::failed to create merge-gate check run"
fi

echo "::endgroup::"
[ "$any_error" -eq 1 ] && exit 3
exit 0
