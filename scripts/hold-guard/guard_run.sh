#!/usr/bin/env bash
# hold-guard orchestration: evaluate, conditionally disarm, repair, report, publish
# the merge-gate check.  Executed by hold-guard.yml AND by the integration
# tests (test_guard_integration.py runs THIS script against a fake gh), so
# the tested code is the shipped code.
#
# Required env: REPO (owner/repo), PR (pull number), HEAD_SHA, PR_URL.
# Optional env: GUARD_APP_SLUG (default github-actions).
#
# Error contract: every critical READ error (evaluation, fresh-state fence,
# check-run listing) is settled BEFORE the check payload is built and forces
# a FAILURE conclusion - a degraded guard never publishes green.  An
# unconfirmed disarm settles the conclusion the same way: the guard does not
# know that auto-merge is off, so it cannot vouch for a pull request that may
# still merge itself, whatever the hold state says.  Every critical failure
# of any kind (read errors, disarm unconfirmed, label repair, comment, check
# patch/create) also makes the JOB red (exit 3).
# What we cannot do (documented, not claimed): if GitHub API writes
# themselves fail, no red check can be forced onto the PR - a previously
# green merge-gate may persist; the red job + notices are the signal, and
# the armer-side gate re-evaluates independently at arm time.
set -uo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
: "${EVENT_NAME:=pull_request_target}"
: "${PR:=}"
: "${GUARD_APP_SLUG:=github-actions}"
: "${REPO:?REPO required}" "${HEAD_SHA:?HEAD_SHA required}"

# Per-run scratch dir (Kody r4374207226): any deterministic path under
# shared /tmp is a cross-run namespace.  A startup glob scoped per PR still
# collides when the SAME PR is evaluated by two concurrent runs on a reused
# runner (queued synchronize/labeled events): run B's startup can delete
# run A's snapshot between its write and jq parse, turning a valid repair
# into a spurious parse/repair error and red job.  A unique mktemp dir per
# run removes the shared namespace entirely - no startup cleanup is needed
# (no stale file can ever be trusted, which also keeps the r4374043272
# property), and the trap removes only our own files.  mktemp failure is a
# critical startup failure (red, fail closed); trap cleanup failure is
# benign temp residue.  HOLD_GUARD_SCRATCH overrides the dir for tests;
# caller-provided dirs are validated writable below (red exit otherwise)
# and are caller-cleaned.
SCRATCH="${HOLD_GUARD_SCRATCH:-}"
if [ -z "$SCRATCH" ]; then
  if ! SCRATCH="$(mktemp -d /tmp/hold-guard-run.XXXXXX)"; then
    echo "::error::could not create per-run scratch dir"
    exit 3
  fi
  trap 'rm -rf -- "$SCRATCH"' EXIT
else
  # Caller-provided override must be validated BEFORE any marker relies on
  # it (Kody r4374404410): a missing or unwritable override made every
  # marker truncation and append fail silently - with no set -e, the failed
  # writes leave no file, [ -s "$ERR" ] stays false, and a failed repair
  # exits 0 instead of red (fail-open).
  if [ ! -d "$SCRATCH" ] || [ ! -w "$SCRATCH" ]; then
    echo "::error::HOLD_GUARD_SCRATCH is not a writable directory: $SCRATCH"
    exit 3
  fi
fi
# The initial marker truncations are the first fail-closed writability
# check.  Post-init write failures (disk full mid-run) cannot be recorded
# by writing another marker: every append below is instead guarded by a
# subshell exit, and each pipeline is checked in the MAIN shell where
# any_error=1 turns the job red without touching the filesystem.
ERR="$SCRATCH/errors"; : > "$ERR"   || { echo "::error::cannot initialize marker file in $SCRATCH"; exit 3; }
PF="$SCRATCH/patch-fail"; : > "$PF"   || { echo "::error::cannot initialize marker file in $SCRATCH"; exit 3; }
REPAIRED="$SCRATCH/repaired"; : > "$REPAIRED"   || { echo "::error::cannot initialize marker file in $SCRATCH"; exit 3; }
any_error=0
read_error=""

echo "::group::hold-guard run (event=$EVENT_NAME repo=$REPO sha=$HEAD_SHA pr=${PR:-none})"

if [ -z "$PR" ]; then
  echo "::error::PR is required (merge_group support removed in slice 1)"
  exit 3
fi
prs=("$PR")

# Disarm only when a hold or critical error requires it. A clear PR may
# keep legitimate auto-merge armed; the guard must not create a hold itself.
# State readback remains authoritative when disarming is required.
disarm_error=""
disarm_attempted=0
ensure_disarmed() {
  [ "$disarm_attempted" -eq 1 ] && return
  disarm_attempted=1
  gh pr merge --disable-auto "$PR_URL" 2>&1 | head -3 || true
  state="$(gh pr view "$PR_URL" --json autoMergeRequest \
    --jq 'if .autoMergeRequest == null then "off" else "armed" end' 2>/dev/null || true)"
  [ -n "$state" ] || state=unreadable
  case "$state" in
    off) : ;;
    *) echo "::error::auto-merge state after disarm attempt: $state ($PR_URL)"
       any_error=1
       disarm_error="auto-merge is $state after the disarm attempt" ;;
  esac
}

# Fast current-label signal: disarm visible holds before expensive paginated
# timeline ownership evaluation. Errors also disarm, never a false-clear.
# Removed-but-unreleased historical holds still require timeline recovery.
if signal="$(python3 "$DIR/hold_guard.py" "$REPO" "$PR" --hold-signal)" &&
   signal_state="$(printf '%s' "$signal" | jq -er '.held | if type == "boolean" then tostring else error("invalid hold signal") end')"; then
  [ "$signal_state" = true ] && ensure_disarmed
else
  any_error=1; read_error="current hold signal error"
  ensure_disarmed
fi

# 2. Evaluate holds (fail closed per PR).
combined='[]'
for pr in "${prs[@]}"; do
  if out="$(python3 "$DIR/hold_guard.py" "$REPO" "$pr")"; then
    :
  else
    rc=$?
    any_error=1; read_error="evaluation error"
    # hold_guard.py already prints a valid {"state":"error",...} object on
    # failure - keep it intact (r4373586516/r4373591826: interpolating it
    # into a JSON string produced invalid JSON, emptying `combined` and
    # dropping every collected hold).  Wrap via jq (proper escaping) only
    # when stdout was not JSON at all.
    if ! printf '%s' "$out" | jq -e . >/dev/null 2>&1; then
      out="$(jq -cn --arg rc "$rc" --arg e "$out" '{state:"error",error:("hold_guard rc=" + $rc + ": " + $e)}')"
    fi
  fi
  echo "$out" > "$SCRATCH/hold-guard-$pr.json"
  combined="$(jq -s --arg pr "$pr" '.[0] + [ .[1].holds[]? | . + {pr: $pr} ]' <(echo "$combined") <(echo "$out"))"
done

# Evaluate before mutation: active holds and evaluation errors disarm
# before repair. A later hold/read failure is caught again before publish.
if ! hold_count="$(printf '%s' "$combined" | jq -er 'if type == "array" then length else error("invalid hold list") end')"; then
  any_error=1; read_error="invalid hold evaluation"; hold_count=0
fi
if [ "$any_error" -eq 1 ] || [ "$hold_count" -gt 0 ]; then
  ensure_disarmed
fi

# 3. Repairs + reports (labels via jq-built JSON, never word-split).
#    Pipelines run in subshells; failures are recorded via marker file and
#    turn the job red; the evaluated hold state still decides the payload.
# Stale-repair fence (Kody r4373591313): candidates come from the
# pre-disarm evaluation; a legitimate release landing since then must not
# be resurrected as a guard-owned hold (releaseable by nobody).  Each
# distinct PR is re-evaluated ONCE before its repairs (hoisted per Kody
# r4373834824 - one evaluation per hold would multiply paginated API
# calls; per-PR is sufficient because epoch pairing allows at most one
# open hold per label name, so repairing hold A cannot change hold B's
# needs_reapply).  A re-check read error, and a jq failure reading the
# re-check (Kody r4373834619 - an empty result used to fall into the
# stale-skip branch and skip the repair with NO red signal), both record a
# repair error and turn the job red (fail closed - the next
# event-triggered run retries).  The fence re-check can predate a later
# POST in the same pass by seconds; that and the re-check-to-POST window
# are the documented asynchronous residue (docs/hold-guard-rollout.md) -
# shrunk, never eliminated.
declare -A fresh_repair_ok=()
printf '%s' "$combined" | jq -c '.[] | select(.needs_reapply)' | while IFS= read -r h; do
  pr=$(printf '%s' "$h" | jq -r .pr)
  label=$(printf '%s' "$h" | jq -r .label)
  if [ -z "${fresh_repair_ok[$pr]:-}" ]; then
    if fresh1="$(python3 "$DIR/hold_guard.py" "$REPO" "$pr")"; then
      # Kody r4374043272: mark fresh-ok ONLY after the cache write itself
      # succeeds; a failed write with a stale leftover would make the later
      # jq parse trust old state (silent skip or resurrected hold, no red).
      if printf '%s' "$fresh1" > "$SCRATCH/fresh-repair-$pr.json"; then
        fresh_repair_ok[$pr]=1
      else
        echo "::error::pre-repair re-check cache write failed on #$pr - skipping repair"
        echo repair >> "$ERR" || exit 4
        fresh_repair_ok[$pr]=0
      fi
    else
      echo "::error::pre-repair re-check failed on #$pr - skipping repair"
      echo repair >> "$ERR" || exit 4
      fresh_repair_ok[$pr]=0
    fi
  fi
  [ "${fresh_repair_ok[$pr]}" = "1" ] || continue
  if ! still="$(jq --arg l "$label" '[.holds[]? | select(.label == $l and .needs_reapply)] | length' < "$SCRATCH/fresh-repair-$pr.json")"; then
    echo "::error::pre-repair re-check parse failed on #$pr - skipping repair"
    echo repair >> "$ERR" || exit 4
    continue
  fi
  if [ "$still" != "1" ]; then
    echo "::notice::skipping stale repair of $label on #$pr (no longer needed at re-check)"
    continue
  fi
  if printf '%s' "$h" | jq -c '{labels: [.label]}' \
    | gh api "repos/$REPO/issues/$pr/labels" -X POST --input - >/dev/null; then
    echo "::notice::re-applied $(printf '%s' "$h" | jq -r .label) on #$pr"
    # a lost narration entry means the successful repair is never commented
    # and later marker writes are broken too - fail the loop, not silently
    printf '%s\n' "$h" >> "$REPAIRED" || exit 4
  else
    echo "::error::failed to re-apply label on #$pr"
    echo repair >> "$ERR" || exit 4
  fi
done || {
  # subshell exit 4 = marker write failed post-init; a jq/selection failure
  # lands here too.  Either way error accounting is degraded: red job,
  # signalled in the main shell without writing to the broken marker.
  echo "::error::repair loop or its error accounting failed"
  any_error=1
}
# Comments narrate ACTUAL repairs only: the entry lands in $REPAIRED after
# a successful re-apply POST, so a failed repair never produces a "the
# hold is back on" comment, and a hold already repaired in an earlier run
# (label present, needs_reapply false) is not re-narrated on every run
# (Sentry finding 4373451447 on #964).
jq -c 'select((.illegitimate_removals // []) | length > 0)' "$REPAIRED" | while IFS= read -r h; do
  pr=$(printf '%s' "$h" | jq -r .pr)
  body=$(printf '%s' "$h" | jq -r '"Hold `\(.label)` was removed by \([.illegitimate_removals[]? | . // "unknown"] | join(", ")). Only its setter (\(.owner_login // "unknown")) can release it - the hold is back on."')
  gh pr comment "$pr" --repo "$REPO" --body "$body" >/dev/null \
    || { echo "::error::failed to comment on #$pr"; echo comment >> "$ERR" || exit 4; }
done || {
  echo "::error::comment loop or its error accounting failed"
  any_error=1
}
[ -s "$ERR" ] && any_error=1

# 4. Fresh-state fence: re-evaluate IMMEDIATELY before publishing so the
#    written conclusion reflects state at write time (seconds), not the
#    start of this run.  A still-later change is covered by the next queued
#    run; per-run freshness is the strongest asynchronous guarantee here.
fresh='[]'
for pr in "${prs[@]}"; do
  if fout="$(python3 "$DIR/hold_guard.py" "$REPO" "$pr")"; then
    fresh="$(jq -s --arg pr "$pr" '.[0] + [ .[1].holds[]? | . + {pr: $pr} ]' <(echo "$fresh") <(echo "$fout"))"
  else
    any_error=1; read_error="evaluation error"
  fi
done
# diagnostic snapshot (checked: a post-init scratch failure must turn the
# job red, not pass silently - same accounting contract as the markers)
echo "$fresh" > "$SCRATCH/combined.json" \
  || { echo "::error::could not write combined fence snapshot"; any_error=1; }

# 5. Publish the merge-gate conclusion (always attempted).
#    The check-run listing is the LAST read; it happens BEFORE the payload
#    is built so its failure also forces a failure conclusion.
owned=""
# --paginate --slurp (r4373586178/r4373591470): the first REST page holds
# 30 runs; owned merge-gate runs beyond it were invisible, producing
# duplicate POSTs beside a stale green.  Pages arrive as one slurped array
# of page objects and are merged before the GUARD_APP_SLUG filter.
if runs_json="$(gh api --paginate --slurp "repos/$REPO/commits/$HEAD_SHA/check-runs?check_name=merge-gate")"; then
  owned="$(printf '%s' "$runs_json" | jq -r --arg s "$GUARD_APP_SLUG" '[.[].check_runs[]?] | .[] | select(.app.slug == $s) | .id')"
else
  echo "::error::could not list merge-gate runs for $HEAD_SHA"
  any_error=1
  [ -n "$read_error" ] || read_error="check-run listing error"
fi

# The fence can discover a newly placed hold, and the final check listing
# can fail after an initially clear evaluation. Both still require disarm.
if ! hold_count="$(printf '%s' "$fresh" | jq -er 'if type == "array" then length else error("invalid hold list") end')"; then
  any_error=1; read_error="invalid fresh hold evaluation"; hold_count=0
fi
if [ "$any_error" -eq 1 ] || [ "$hold_count" -gt 0 ]; then
  ensure_disarmed
fi

if [ -n "$read_error" ]; then
  conclusion=failure; title="merge-gate: $read_error"
  summary="A guard read step failed (see workflow log). Failing closed - no green check from a degraded guard."
elif [ -n "$disarm_error" ]; then
  # The hold state may be clean, but the guard does not know that auto-merge
  # is off, so it cannot say the pull request is safe to merge.  A green gate
  # over a pull request that may still merge itself is the one answer this
  # gate must never publish.
  conclusion=failure; title="merge-gate: the disarm failed"
  holds="$(printf '%s' "$fresh" | jq -r '[.[] | "- `\(.label)` (PR #\(.pr)) set by \(.owner_login // "unknown")\(if .unknown then " - owner unknown, needs escalation" else "" end)"] | join("\n")')"
  summary="The guard could not disarm auto-merge on PR #$PR: $disarm_error.  The hold state was evaluated, but a green gate over a pull request that may still merge itself is not a safe answer, so this check stays red.  Holds evaluated: ${holds:-none}."
elif [ "$(printf '%s' "$fresh" | jq 'length')" -eq 0 ]; then
  conclusion=success; title="merge-gate: no active merge holds"
  summary="All holds were released by their setters."
else
  conclusion=failure; title="merge-gate: active merge holds"
  # summary is built from the in-memory fence result, not the snapshot
  # file, so a snapshot write failure cannot degrade the payload
  summary="$(printf '%s' "$fresh" | jq -r '[.[] | "- `\(.label)` (PR #\(.pr)) set by \(.owner_login // "unknown")\(if .unknown then " - owner unknown, needs escalation" else "" end)"] | join("\n")')"
fi
payload="$(jq -n --arg sha "$HEAD_SHA" --arg c "$conclusion" --arg t "$title" --arg s "$summary" \
  '{name: "merge-gate", head_sha: $sha, status: "completed", conclusion: $c, output: {title: $t, summary: $s}}')"

# Only runs owned by OUR app identity are eligible to PATCH (no foreign
# probing: a 403 we never trigger is a mutation we never attempt).
printf '%s\n' "$owned" | while IFS= read -r run_id; do
  [ -z "$run_id" ] && continue
  if ! printf '%s' "$payload" | gh api "repos/$REPO/check-runs/$run_id" -X PATCH --input - >/dev/null; then
    echo fail >> "$PF" || exit 4
    echo "::error::failed to patch owned merge-gate run $run_id"
  fi
done || {
  echo "::error::patch loop or its error accounting failed"
  any_error=1
}
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
