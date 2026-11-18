# Shared Infisical Subprocess Consumers

Refs AFC-785.  This source slice depends on the shared direct reader from PR #2167 and does not perform a live copy, provision a token, or activate another consumer.

## Scoped Folder Population

`scripts/simple-tools-mcp/scripts/populate-scoped-folders.py` now calls `fleet_infisical.fetch_project` directly for one requested name at a time.  It passes only the fixed API origin and the existing exact folder declaration; the selected project token is an explicit function argument.  Ambient reader, writer, letter, API-origin and folder bindings cannot substitute for that route.  No child process receives credentials and no installed `fleet-secrets` application is discovered.

The existing 72-entry source/destination mapping, two project-specific reader bindings, separate Fleet-OPS writer, dry-run scope gate and collision/reconciliation rules remain.  Each direct value read additionally uses the shared verifier and provenance checks.  Missing source is code3; missing destination is absent; unresolved references stop with code5.  Caught read timeouts remain code124; other provider failures retain only their fixed code.  The shared HTTP transport may report a socket timeout as a generic provider failure, as the previous service-token CLI did.  A missing application is no longer a code127 condition because no application is invoked.

The shared query disables reference expansion and refuses unresolved references.  The former comment promising expansion was stale against the service-token CLI's current shared-reader route; this change adds no cross-folder reference reader.

`--apply` retains its existing explicit owner-approval requirement.  This change neither grants that approval nor runs it.  The three existing `AFC_SCOPED_FOLDERS_*` bindings and their recorded exact folder sets must be checked by the authorized provisioner before an actual copy; no live grant or expiry was inspected here.

## Remaining Callers

| Source | Verified Current Route | Remaining Boundary |
| --- | --- | --- |
| `scripts/fleet-worker-health.py:read_key` | `HEALTH_HMAC_KEY` process binding first, otherwise installed `fleet-secrets get HEALTH_HMAC_KEY`, whose default project/folder is Fleet-OPS `/` | This caller declares no own-purpose Infisical reader binding.  Its scheduled managed-runtime entry must preserve the working route until a replacement grant and launch binding are verified. |
| `scripts/simple-tools-mcp/infra_phase0.py:_load` | Process binding first, then installed `fleet-secrets get`; only callers request `CLOUDFLARE_JAY_ACCOUNT_EMAIL` and `CLOUDFLARE_JAY_API_KEY` | It declares no own-purpose reader binding and uses a Global Cloudflare key.  Retirement requires an independently reviewed scoped provider route, not copying that key into a new binding. |
| `scripts/simple-tools-mcp/install_seat_key.py:handoff` | Same legacy environment/application fallback for those two Cloudflare fields and `ZULIP_OWNER_USER_ID` | Its `infisical_pair` already uses the shared direct reader with explicit project-specific seat-key bindings for prod `/zulip`.  Those seat-key grants do not authorize unrelated Global Cloudflare or owner-pin reads. |

These are verified source boundaries, not claims about the callers' current host configuration.  No credential store was scanned and no consumer token was borrowed.  The held native Muster migration and the legacy doctor's value-validation contract remain separate.

## Validation

The population suite covers all72 copies, reconciliation after partial writes, wrong/missing grants, named-result absence, value preservation, redacted failures, no subprocess, and exclusion of ambient/letter/writer bindings.  Additional tests exercise the actual shared reader for grant validation, response provenance, unresolved-reference refusal and exact-name absence.  These tests use synthetic in-memory data only.
