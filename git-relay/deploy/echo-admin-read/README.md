# ECHO admin-read restoration (AFC-629 / AFC-631 / AFC-639)

Source preparation only. Merging does not activate this package.

The current relay selects GIT_RELAY_SEATS_FILE, not inline JSON. This fixed
transaction appends only five reviewed read actions to the existing swu-echo
action list: wf_repo_list, wf_pr_search, wf_repo_hooks, wf_deploy_keys and
wf_org_installations. All other seats, fields, own-App identity, auth,
repository scopes and permission ceilings are preserved. The handlers already
exist in the reviewed relay source (7c333213) and each requires only a
read-level ceiling the seat already holds at read or write (metadata,
pull_requests, repository_hooks, administration, organization_administration;
git_relay/workflow_verbs.py ACTION_PERMISSIONS). No code/config replacement,
new seat, provider permission, identity or credential audience is introduced.

The observed live baseline hash (30aa..., confirmed by the operator's live
read 2026-11-09 22:52) and the config hash (af09...) are pinned in the
builder. The candidate hash is NOT pinned in source: the qualified operator
observes the live policy, runs the fixed transform over it, then pins the
resulting candidate hash in three places together - AFTER in build.py,
CANDIDATE in runtime_proof.py, and PROOF in build.py as the sha256 of the
re-pinned runtime_proof.py - before activation review, exactly as the
echo-review-read transaction's pins were produced. Until then the builder
refuses every candidate and the runtime proof accepts only the baseline. The
only manifest target is state/seats.json, mode0600 and
existing 65534 ownership. Policy contents stay on the host; candidates are
private0600, never source/log material. Baseline drift refuses rather than
widening scope.

The existing finite supervisor/recovery package is hash-pinned and unchanged
except its fixed unit literal. Its lock, frozen single-task/no-child/no-pending
write checks, stop confirmation, durable before-image, atomic replacement,
failed-exit/boot recovery and rollback runtime proof remain mandatory. A new
transaction directory/unit/manifest is used, never an earlier operation replay.

The fixed runner reads the existing root-private WISE proof credential directly
at its existing purpose path. It neither copies it nor acquires ECHO credentials.
Runtime proof accepts only the two exact policy hashes, verifies the actual
process startup selects the single expected FILE input and that both logical
and physical file paths identify the same file, then performs a WISE own-App
repository read. LinuxHost separately pins process identity and hashes around
proof. This proves service/unchanged-seat health, not ECHO access.

After successful activation ECHO must perform its own read proof of each of the
five verbs through its normal route (and the fleet-tools front advertisement
for the three AFC-631 verbs, which the deployed Worker does not advertise yet).
Those results are required for task closeout, not substituted by WISE reads.
Rollback restores the exact old policy and proves the old process/config
selector and WISE read. No provider write probe is needed.

Before activation qualify source review, tests, current baseline/config parity,
credential purpose/access, maintenance serialization and the finite executor.
No blanket seat expansion, selector migration or replay is allowed. Extending
any of the five verbs to any other seat is a separate owner decision and is
not part of this transaction.
