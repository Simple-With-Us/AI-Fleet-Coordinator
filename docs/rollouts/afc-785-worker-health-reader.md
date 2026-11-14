# Worker Health Direct Reader

This draft source migration removes the installed fleet-secrets subprocess from the existing worker-health collector.  It preserves the already supported Infisical-synced HEALTH_HMAC_KEY transport first; otherwise it uses the canonical shared fleet_infisical client to request only HEALTH_HMAC_KEY from the currently declared Fleet-OPS prod `/` home.  It adds no reader implementation, service, timer, credential store or live configuration.

The direct route requires this consumer's explicit FLEET_WORKER_HEALTH_INFISICAL_READ_TOKEN and nonsecret FLEET_WORKER_HEALTH_INFISICAL_PROJECT_ID.  Missing either fails closed with the existing no_key health state, without a generic token, writer, other consumer, login or installed-app fallback.  The shared verifier requires exactly that project, prod, root folder, read-only permission and finite future expiry before the named read.  Ambient API origins, extra-folder declarations and credentials never enter its request environment.  Values remain in memory and never enter reports, logs or argv.

## Packaging And Rollout Gates

- Keep the existing working runtime and this PR draft until its own approved binding or existing synced-value transport is verified; no grant or current runtime state was inspected by this source change.
- Land shared-client foundation #2167 and retain scripts/fleet_infisical.py beside the collector in the actual payload.  The Mac LaunchAgent targets the full managed simple-tools-runtime worktree, which the existing sync script checks out from main; this is distinct from the restricted server fleet-CLI bundle.  Foundation already puts the shared client in release EXACT/TOOL_REQUIRED and doctor inventories; no older rollback minimum is widened here.
- The isolated-entry regression runs a copied collector/client pair under Python -I from a hostile current directory/PYTHONPATH and verifies the sibling module is selected.  Removing that module fails the direct route closed while the existing synced-key path still works.
- Before this source reaches the automatically updated managed runtime, verify the consumer's own approved scope/finite expiry and protected bootstrap binding, record metadata in the existing registry, then verify the actual signing/read/report path.  A test binding or provider metadata claim is not an installed LaunchAgent receipt.
- Preserve the existing Worker contract, target, signing scheme, report schema, private atomic file, 15-minute schedule and stale-reading policy.  No secret is hand-set or embedded in the plist by this PR.

The separate Cloudflare watchdog remains unchanged.  Its actual credential config reads AI Fleet Coordinator `/cf-usage-watchdog`, while its README claims the same generic bootstrap also reads root knobs through fleet_knobs.  That root-versus-folder contract needs reconciliation under the exact-folder/read-token policy before replacing its app path; this change neither widens nor splits that consumer's grants.
