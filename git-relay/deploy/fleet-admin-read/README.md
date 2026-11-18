# Fleet admin-read extension (AFC-629 / AFC-631 / AFC-639)

Source preparation only. Merging does not activate this package.

Owner decision 2026-11-10: Jay's authenticated WhatsApp answer "for ALL
AGENTS" to the fleet seat question for the five read capabilities
wf_repo_list, wf_pr_search, wf_repo_hooks, wf_deploy_keys and
wf_org_installations, with existing repository scopes and write permissions
unchanged. This fixed transaction appends those five reviewed read verbs to
EVERY existing seat that matches the reviewed own-App shared-auth shape and
already holds each verb's read-level mint ceiling (metadata, pull_requests,
repository_hooks, administration, organization_administration;
git_relay/workflow_verbs.py ACTION_PERMISSIONS). The echo-admin-read
transaction's own README records that extending any of the five verbs to any
other seat is a separate owner decision; this package is that separate
decision's transaction. All other fields, own-App identities, auth, seats,
repository scopes and permission ceilings are preserved. The handlers already
exist in the reviewed relay source (7c333213). No code/config replacement,
new seat, provider permission, identity or credential audience is introduced.

Per-seat selectivity (git_relay/fleet_admin_read.py): a verb is appended to a
seat only when the seat's existing mint ceiling covers it at read or write
and the seat does not already hold it; a seat missing a ceiling keeps that
verb excluded, and a seat whose row does not match the reviewed own-App
shared-auth shape is skipped entirely. Every exclusion and skip is named in
the build report so real conflicts surface at review instead of being
silently dropped. The transform refuses a no-op candidate.

The observed live baseline hash (30aa..., confirmed by the operator's live
read 2026-11-09 22:52) and the config hash (af09...) are pinned in the
builder. If the live baseline has drifted by activation time - for example
because the echo-admin-read transaction landed first - the build refuses and
the qualified operator re-observes the live policy and re-pins BEFORE
together with the other constants; baseline drift never widens scope. The
transform is exact on either baseline: a seat already holding the five verbs
receives no duplicates. The logical baseline digest and the candidate hash
are NOT pinned in source: the qualified operator observes the live policy,
computes its canonical digest, runs the fixed transform over it, then pins
four constants together - LOGICAL_BEFORE and AFTER in build.py, CANDIDATE in
runtime_proof.py, and PROOF in build.py as the sha256 of the re-pinned
runtime_proof.py - before activation review, exactly as the echo-admin-read
transaction's pins were produced. Until then the builder refuses every
candidate and the runtime proof accepts only the baseline (a non-hex
candidate placeholder is never an accepted gate state). The only manifest
target is state/seats.json, mode0600 and
existing 65534 ownership. Policy contents stay on the host; candidates are
private0600, never source/log material.

The existing finite supervisor/recovery package is hash-pinned and unchanged
except its fixed unit literal. Its lock, frozen single-task/no-child/no-pending
write checks, stop confirmation, durable before-image, atomic replacement,
failed-exit/boot recovery and rollback runtime proof remain mandatory. A new
transaction directory/unit/manifest is used, never an earlier operation replay.

The fixed runner reads the existing root-private WISE proof credential directly
at its existing purpose path. It neither copies it nor acquires any other
seat's credentials. Runtime proof accepts only the two exact policy hashes,
verifies the actual process startup selects the single expected FILE input
and that both logical and physical file paths identify the same file, then
performs a WISE own-App repository read. LinuxHost separately pins process
identity and hashes around proof. This proves service/unchanged-seat health,
not any extended seat's access.

After successful activation EVERY extended seat must perform its own read
proof of the five verbs through its normal route (and the fleet-tools front
advertisement for the three AFC-631 verbs, which the deployed Worker does not
advertise yet). Those per-seat results are required for task closeout, not
substituted by WISE reads. Rollback restores the exact old policy and proves
the old process/config selector and WISE read. No provider write probe is
needed.

Before activation qualify source review, tests, current baseline/config
parity, credential purpose/access, maintenance serialization and the finite
executor. No blanket seat expansion beyond this owner decision, selector
migration or replay is allowed.
