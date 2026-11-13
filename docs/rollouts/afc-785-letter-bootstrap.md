# Shared Letter Client Bootstrap

An interactive letter client needs its own scoped Infisical service token to fetch the registry address and signing key.  The shared `platform-letter` and `simple-chat letter` entry points bind that token once per process, independent of seat and platform adapter.  The signing key remains an Infisical value held in memory.

The host's consumer store is `~/.config/platform-letter/infisical.token`: an owner-only regular file with mode `0600` in a directory with mode `0700`.  The bootstrap reader refuses symlinks, hard links, public files or directories, non-regular files, malformed values and oversized values.  It never changes an explicit `FLEET_KNOBS_INFISICAL_TOKEN` binding and never reads another consumer's token, an old registry helper or a signing-key cache.

Provision the shared host consumer a read-only service token for AI Fleet Coordinator, `prod`, exact folder `/wake-router`, with an expiry.  Verify the token's own metadata, the intended named reads and a refused outside-folder read before use.  Record its metadata in the Infisical Service Tokens registry and the Fleet-OPS mirror.  Keep the value solely in the protected consumer store; never pass it in arguments or messages.

Deploy the reviewed source through the normal fleet CLI release path.  Run the shared letter request in the intended interactive session and verify the registry's assignment and persisted title.  A loader test or a successful health check alone does not prove assignment.

Rollback the source through the fleet CLI release path if needed.  Preserve the consumer token until the replacement path is live and verified; remove obsolete helpers and signing-key caches only after auditing their remaining callers.
