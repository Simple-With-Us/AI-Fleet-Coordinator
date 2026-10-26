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
- Fail-closed applies to evaluation ERRORS only (API/eval failure ->
  armer does not arm; guard posts a failure check and exits 3).
- merge-gate as github-actions[bot] is forgeable by a same-repo workflow
  author with checks: write; binding the required check to a dedicated App
  narrows that to the App's key holders.

## Tests

- scripts/hold-guard/test_hold_guard.py - 16 unit tests (canned runner)
- scripts/hold-guard/test_guard_integration.py - 9 integration tests that
  execute the shipped shell (guard_run.sh, armer_hold_check.sh) against
  fake_gh.py: call ordering (disarm first), request-body exactness, labels
  with spaces, merge_group mapping, pagination, error fail-closed.
- Both suites PASS on this branch.  These are SOURCE fixtures; a later LIVE
  test on a scoped throwaway PR is a separate, not-yet-authorized step.

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
