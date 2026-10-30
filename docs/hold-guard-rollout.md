# Hold guard rollout (PR-hold ownership: setter-only release)

Origin: Jay, WhatsApp 2026-10-25 20:45 CT - do-not-merge-type tags on PRs
that others can't break unless they put them on.

This branch is SLICE 1 (source only, no live effects until the workflow's
first event): `scripts/hold-guard/` + `.github/workflows/hold-guard.yml`.

## Ownership model (canonical: scripts/hold-guard/hold_guard.py)

Epoch pairing per label over the PR timeline (fully paginated, sorted by
(created_at, id); state derived from the API at evaluation time, never from
the webhook payload):

- `labeled` with no open hold -> hold opens, owner = applying actor.id
- `labeled` while open -> repair/duplicate; ownership UNCHANGED (the guard's
  own re-apply can never steal or reset ownership)
- `unlabeled` by the owner (actor.id match) -> released
- `unlabeled` by anyone else -> illegitimate: hold stays open under the
  original owner, remover recorded, label re-applied, one comment posted
- label present with no recoverable applying event -> ACTIVE, owner UNKNOWN;
  clearable by no one automatically (escalation path, no silent reassignment)

Logins are display only; matching is by numeric actor.id (rename-safe).
Suffix labels (`do-not-automerge:<login>`) are supported as independent
per-setter holds; the suffix is a display hint, never the ownership source.

## Verified capabilities

- Disarm contract: `gh pr merge --disable-auto <pr>` followed by a state
  read (`gh pr view <pr> --json autoMergeRequest`); success = state "off",
  matching the fleet's own disable-pr-automerge.sh contract (already-off
  is benign; armed/unreadable after the attempt is a critical failure).
  Integration fake enforces the real flag set - the previously drafted
  `gh pr merge --auto --disable` is invalid and is now rejected by the
  test double.
- Hold label families covered: do-not-merge[:*] (Jay's literal ask),
  do-not-automerge[:*], needs-human-merge (fleet-documented).

- Disarm uses repository Actions identity with `contents: write` and
  `pull-requests: write`.  A read-only contents token was rejected by
  `disablePullRequestAutoMerge` on armed PRs1064/1124/1145 on2026-10-29.
  The state readback remains the success condition; no elevated token or
  personal credential fallback is added.
- GITHUB_TOKEN `checks: write` permits creating a check run:
  https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions
- Checks API write is GitHub-Apps-only (OAuth/classic PAT cannot create or
  update check runs):
  https://docs.github.com/en/rest/checks/runs?apiVersion=2022-11-28
- Required checks can be bound to a specific GitHub App (2021-12-01):
  https://github.blog/changelog/2021-11-30-ensure-required-status-checks-provided-by-the-intended-app/
- labeled/unlabeled webhook payloads carry sender:
  https://docs.github.com/en/webhooks/webhook-events-and-payloads?actionType=unlabeled

## Name-collision qualification (2026-10-25, org-wide code search + AFC tree)

- No workflow file named *merge-gate* and no code creating a check run named
  `merge-gate` found.  The string appears only as a concept in
  scripts/pr_sweep (audit rule V7 regex for merge-control workflow
  FILENAMES) and prose docs; ST scripts/automerge-gate-dry-run.sh is a
  filename sibling, not a check producer.
- RESIDUE: only a live head-run check listing proves no third-party App
  posts a `merge-gate` context on an actual head - unverified here.
- pr_sweep interaction: the V7 filename regex
  `(merge-gate|auto-?merge|automerge)` does NOT match `hold-guard.yml`, but
  rules.md (c) makes merge-control changes owner-needed in spirit - this PR
  must not auto-merge; say so in its body.
- Live ruleset read (2026-10-25): AFC/BotFleet/Clutch/FleetLink require
  thread resolution, approvals 0, no merge queue; no `merge-gate` in any
  current required-check list.

## Stated enforcement limits

- ASYNCHRONOUS: a hold placed while a merge is already in GitHub's
  auto-merge pipeline can complete before the guard's disarm lands; a
  previously-green merge-gate on an unchanged head stays green until
  rewritten.  Window = Actions scheduling latency.  Wiring merge-gate as a
  required check narrows the window and gates manual merges at steady state
  but does not remove the window.
- An auto-merge armed AFTER the guard's latest run fires the
  `auto_merge_enabled` activity (Kody r4373585289), so the disarm re-runs
  at the arming moment instead of waiting for an unrelated
  label/synchronize event.  The remaining window is the Actions scheduling
  latency of that triggered run - present, and not claimed away.
- UNKNOWN-owner holds are never re-armed (Kody r4373587152): a re-applied
  unknown-owner hold is releaseable by nobody and becomes a permanent
  block.  They stay open in evaluation (the gate remains red - removal
  alone never turns it green) and escalate via the check summary's
  "owner unknown, needs escalation" line; a human resolves them.
- STALE-REPAIR FENCE (Kody r4373591313): before its repair POSTs each
  distinct PR is re-evaluated once (Kody r4373834824 - per-hold
  re-evaluation would multiply paginated API calls) and any hold the
  fresh state no longer needs is skipped (e.g. the setter re-applied and
  released in the meantime).  A re-check read or parse failure records a
  repair error and turns the job red (Kody r4373834619) - a skipped
  repair is never silent.  The fence SHRINKS the repair race but cannot
  eliminate it:
  a release landing between that re-check and the POST is still
  resurrected as a guard-owned hold, caught only by the release event's
  own queued run, which keeps the gate red and surfaces a guard-owned
  hold in the summary for human escalation.  No stronger guarantee is
  claimed without a merge queue.
- Fail-closed applies to evaluation ERRORS only (API/eval failure ->
  armer does not arm; guard publishes a failure conclusion when the API
  allows and exits 3).
- ERROR PROPAGATION (guard): every critical READ error (evaluation,
  fresh-state fence, check-run listing) is settled BEFORE the check
  payload is constructed and forces a FAILURE conclusion - a degraded
  guard never publishes green.  Every critical failure of any kind (read
  errors, disarm unconfirmed, label re-apply, comment, check
  patch/create) also turns the guard JOB red (exit 3).
  - Disarm success is defined by the post-attempt STATE READ
    (`gh pr view --json autoMergeRequest`): off = benign (covers both
    "was armed, now disarmed" and "was never armed" - the disable mutation
    failing with "not enabled" is not an error); armed or unreadable =
    critical failure.  This mirrors the fleet's own contract (ST
    scripts/disable-pr-automerge.sh, ported inline into the AFC reusable
    workflow's disarm job); no blanket `|| true` anywhere.
  - An unconfirmed disarm also settles the CHECK conclusion as a failure
    (AFC-553): the guard does not know that auto-merge is off, so it cannot
    vouch for a pull request that may still merge itself, whatever the hold
    state says.  The gate publishes red and says why, instead of publishing
    green over a known-unresolved condition.  The seat that armed auto-merge
    can always disarm it with its own token, and the git relay is meant to
    give the workflow a token allowed to make the mutation.
  - Check publication: the check-run listing (GET) is the last read and
    happens before the payload is built, so its failure recomputes the
    payload to failure; the POST is still attempted best-effort.  A
    failed PATCH of an owned run forces a fresh owned POST so our
    identity still carries the current conclusion; a failed POST is
    reported.  All three turn the job red.
- RESIDUAL (stated, not claimed away): when GitHub API WRITES themselves
  fail, the guard cannot force a red check onto the PR - a previously
  green merge-gate on an unchanged head can persist.  The failure signal
  in that case is the RED GUARD JOB plus error notices in the log, not a
  guaranteed red check; the armer-side hold gate re-evaluates
  independently at arm time (slice 2), so the arm path does not depend on
  the guard's writes having landed.
- merge-gate as github-actions[bot] is forgeable by a same-repo workflow
  author with checks: write; binding the required check to a dedicated App
  narrows that to the App's key holders.

## Observed production behavior (first live run, 2026-10-26)

Run 40322235405 (PR 944, pull_request_target, head 63795aed) completed
green on the unheld/off path: event trigger, base-branch checkout of the
merged code, live-API evaluation and merge-gate publication (check
121569862840, SUCCESS on the exact head) are verified working in
production.  The disarm mutation was refused for this run's token
("Resource not accessible by integration" on disablePullRequestAutoMerge);
the independent state read returned off, so the run was benign under the
fleet contract (the state read, not the mutation exit code, defines
success).

Limits of this observation:

- The available filtered timeline for PR 944 showed no hold/automerge
  events.  That is not proof the PR was never held or armed, and not
  contemporaneous event state.
- One refused mutation shows this run's token could not disarm.  It does
  not establish that a given identity can never disarm - no capability
  grounding beyond this single observation exists.
- In the predicted armed path the guard DETECTS and ERRORS (red job,
  error notices, merge-gate still publishing the evaluated hold state).
  It does not prevent an armed merge: with merge-gate not wired as a
  required check (slice 4), an armed auto-merge may still complete.

Unobserved in production: successful armed disarm, hold detection and
repair on a held PR, setter-only release behavior.  Hard enforcement
remains absent until slice 4.

## Privileged-workflow trust boundary (resolved 2026-10-25 review)

- pull_request_target "runs in the context of the **default branch** of the
  base repository" (observed:
  https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows
  - the workflow FILE itself is default-branch code; a stacked-base PR
  cannot inject a doctored hold-guard.yml).
- The scripts it EXECUTES must come from the same trusted ref: the checkout
  step is pinned to `github.event.repository.default_branch` - never the
  PR's base branch, which any repo writer can create with doctored content.
  actions/checkout is SHA-pinned (3d3c42e5... = v7, observed
  https://api.github.com/repos/actions/checkout/git/refs/tags/v7).
- merge_group REMOVED from slice 1 (trigger and script path): GitHub
  builds queue refs from base + PR heads, so the workflow file on a queue
  ref is not default-branch-only trusted code.  "No queue exists on the
  four target repos" is not a justification for shipping an active unsafe
  trigger; a safe queue design (default-branch-pinned execution on
  merge_group, PR mapping, group-SHA check targeting) is a future slice.
  Enforced by test_workflow_static.py::test_no_merge_group_trigger.
- Enforced by test_workflow_static.py: SHA-pinned checkout, default-branch
  ref, no pull_request.base/head refs anywhere (head.sha allowed only as
  check-target data), minimal permissions, serialized concurrency,
  run-step executes only the checked-out script.

## Concurrency and stale-result handling

- concurrency group per PR, cancel-in-progress: false, DEFAULT queue mode
  (single pending): "any existing pending job or workflow run in the same
  group is canceled and replaced" by the newest queued run, and runs in a
  group are serialized one at a time (observed:
  https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency).
  The same doc states ordering is NOT guaranteed (FIFO by wait-start, not
  dispatch time), so no newest-last claim is made.
- FRESH-STATE FENCE: the guard re-evaluates holds from the API immediately
  before writing the check, so the published conclusion reflects state at
  write time (seconds), not run start.  Tested:
  test_fresh_fence_reevaluates_before_publish.  A change landing after the
  fence is covered by the next queued run; this is eventual consistency
  within Actions latency - the honest asynchronous residue.
- GitHub's selection among multiple same-NAME completed check runs on one
  SHA is NOT doc-verified, so the design does not rely on it: the guard
  PATCHes every merge-gate run on the SHA OWNED BY ITS OWN APP IDENTITY
  (app.slug == GUARD_APP_SLUG, default github-actions) and POSTs a new run
  only when it owns none.  Foreign-app runs are never probed or mutated -
  they are filtered out of the GET response before any write.  Tested:
  test_existing_runs_patched_no_duplicate_no_stale,
  test_foreign_run_never_probed_or_patched.
- RESIDUE (unchanged): the async placement window from the limits section;
  a foreign identity posting its own merge-gate run is only resolvable by
  the slice-4 App binding.

## Tests

- scripts/hold-guard/test_hold_guard.py - 17 unit tests (canned runner)
- scripts/hold-guard/test_guard_integration.py - 23 integration tests that
  execute the shipped shell (guard_run.sh, armer_hold_check.sh) against
  fake_gh.py: call ordering (disarm first), request-body exactness, labels
  with spaces, pagination, evaluation fail-closed, and every realistic
  HTTP/gh failure path (disarm armed-after-attempt, disarm state
  unreadable, not-armed benign, owned-PATCH failure forcing fresh POST,
  check-run GET/POST failure, clear-initial-then-fence-read-error,
  clear-initial-then-listing-error - both asserting red job AND failure
  payload - label repair failure, comment failure).
- All suites PASS on this branch (17 unit + 23 integration + 7 static =
  47).  These are SOURCE fixtures; a later LIVE test on a scoped
  throwaway PR is a separate, not-yet-authorized step.

### Slice 0.5: merge_group support - REMOVED from slice 1, needs default-branch-pinned execution design for queue refs before any repo with a queue adopts the guard.

## Next slices (SPEC ONLY - nothing below is executable in this branch)

### Slice 2: AFC reusable armer hold gate

Diff vs current main (file observed 2026-10-25 ~20:51 CT, sha256
715aec55b5923babbd16e6d22560a1d3679c2151e7354fbfa959a4a59c4ced41).
HOLD_GUARD_REF must be the merged AFC SHA that lands slice 1 - no
placeholder may ship in an executable path.  This file has an active lane
(961 release); edit only inside the coordinated window.

```diff
--- armer-orig.yml	2026-10-25 20:53:35.248536223 -0500
+++ armer-patched.yml	2026-10-25 20:56:44.710119384 -0500
@@ -121,6 +121,7 @@
           RESTRICT: ${{ inputs.restrict_to_pattern }}
           SELF_GUARD: ${{ inputs.self_guard_paths }}
           HOLD_LABELS: ${{ inputs.hold_labels }}
+          HOLD_GUARD_REF: 838b7640PINNED-SHA-PLACEHOLDER
           LABELS_JSON: ${{ toJSON(github.event.pull_request.labels.*.name) }}
           REQUIRE_GATE: ${{ inputs.require_codeowners_gate }}
           GATE_SCRIPT: ${{ inputs.gate_script }}
@@ -129,13 +130,23 @@
           skip() { echo "skip=true" >> "$GITHUB_OUTPUT"; echo "reason=$1" >> "$GITHUB_OUTPUT"; echo "::notice::not arming auto-merge: $1"; exit 0; }
           trap 'skip "classifier failed - fail closed"' ERR
 
-          # Hold labels
-          IFS=',' read -ra holds <<< "$HOLD_LABELS"
-          for h in "${holds[@]}"; do
-            if printf '%s' "$LABELS_JSON" | jq -e --arg h "$h" 'index($h) != null' >/dev/null; then
-              skip "hold label '$h' present"
-            fi
-          done
+          # Hold labels: timeline ownership rule (setter-only release).
+          # Canonical implementation scripts/hold-guard/, fetched at a pinned
+          # ref like every other shared fleet asset.  A hold is ACTIVE unless
+          # GitHub's own event log shows the actor who applied it (matched by
+          # numeric actor id) removed it; removal by anyone else leaves it
+          # ACTIVE.  Unknown-owner holds never release automatically.
+          # armer_hold_check.sh exits nonzero on held AND on evaluation
+          # error - both fail closed (skip).  The legacy hold_labels input is
+          # retired: the evaluator's pattern (do-not-automerge,
+          # do-not-automerge:*, needs-human-merge) is a superset of every
+          # documented hold name.
+          base="https://raw.githubusercontent.com/Simple-With-Us/AI-Fleet-Coordinator/${HOLD_GUARD_REF}/scripts/hold-guard"
+          curl -fsSL "$base/hold_guard.py" -o /tmp/hold_guard.py
+          curl -fsSL "$base/armer_hold_check.sh" -o /tmp/armer_hold_check.sh
+          if ! reason="$(PR_NUMBER="$PR_NUMBER" bash /tmp/armer_hold_check.sh)"; then
+            skip "merge hold active or unevaluable: $reason"
+          fi
 
           # Owner rule 2026-10-21: never merge while a Kodus review is running on
           # the head commit or an autofix run involving this PR is queued or in
```

### Slice 3: standalone armers

Usage-Monitor, Congress.Trade, congress-trading-shared each carry a
job-level `if: !contains(labels, 'do-not-automerge')`.  Each needs the same
wrapper call (armer_hold_check.sh) moved into a classify step.  Not started.

### Slice 4: required-check wiring (SETTINGS CHANGE - user decision)

Add context `merge-gate` (optionally bound to the guard's App per the 2021
changelog) to each repo's existing repo-level ruleset required_status_checks.
No new ruleset, no approvals, no bypass change.  Requires an admin identity
not currently delegated.  BotFleet/Clutch/FleetLink have no in-repo armer
(Tailnet enumeration 2026-10-25), so this settings change is their ONLY
hold coverage; their merge route is otherwise manual/external (unconfirmed).
