# Direct Infisical Reader

This source migration is not activated.  The installed Muster binary and its working route must remain in place until this consumer has its own verified binding and the replacement has passed CI and runtime checks.

This native-only draft depends on shared library PR #2167 and the neutral command/payload PR #2176.  Shared bootstrap packaging in PR #2166 is separately coordinated.  These shared sources are not copied into the native change; all dependencies must be incorporated and verified before a final review or merge.

Muster uses its existing engine interpreter to invoke `python3 -I /absolute/reviewed/scripts/fleet_infisical_cli.py`.  Isolated mode excludes the current directory and ambient Python paths; the entry imports the shared `fleet_infisical` REST client from its own reviewed directory.  Swift does not implement a second HTTP reader.  The old `fleet_secrets.cli` module is a compatibility alias for the same command implementation, so existing callers retain one implementation.

The bridge passes only constructor-injected, explicitly named read bindings into the bounded subprocess.  It does not inherit ambient tokens, inspect another consumer's store, persist a token, or add a legacy credential fallback.  Bound named reads request only the selected keys; metadata-only names never exports values.  Shared consumer reads require their own scoped service token in both interactive and noninteractive processes; a TTY never authorizes a tokenless administrative-session fallback.  Constructor injection proves source behavior in tests; it does not configure an app launched from Finder.  No persistent Muster runtime token store or runtime data-reader grant has been verified for this migration.

## Required Bindings And Scope

| Existing Operation | Project And Environment | Exact Folder | Binding And Behavior |
|---|---|---|---|
| `get NAME`, including the default recall admin key name `RECALL_ADMIN_TOKEN` | Fleet-OPS prod | `/` | Own read-only `INFISICAL_TOKEN`, with `INFISICAL_FLEET_OPS_PROJECT_ID`; only the requested name leaves the reader |
| Controls secret-name listing | Fleet-OPS prod | `/` | Same own exact-folder read route; `names` requests metadata only and returns no values |
| Controls knob-name listing | AI Fleet Coordinator prod | `/` or `/wake-router`, selected per call | Own read-only `INFISICAL_AI_FLEET_COORDINATOR_TOKEN`, with its project id; the shared verifier accepts exactly the selected folder, not a multi-folder grant |
| Existing compatibility doctor | Fleet-OPS prod and AI Fleet Coordinator prod | `/` and `/fleetlink` respectively | Existing doctor probes and unresolved-reference counts remain unchanged; this extra AFC probe needs an own verified grant or a separately reviewed narrower doctor contract before activation |

The current knob registry has 53 entries and the folder set `/`, `/wake-router`; the Controls panel derives its folder list from that registry.  A single broad or multi-folder token is not a substitute for the required exact-folder route.  Each `FleetSecrets` instance currently accepts one explicit read token per project; provisioner-approved per-folder selection and application call-site wiring remain prerequisites for using all selected folders.

The shared private-store primitive accepts only a caller-declared absolute directory, filename and binding.  It requires an owner-matched `0700` directory and a regular owner-matched `0600`, single-link token file, refuses symlinks and FIFOs, bounds the read, and preserves any explicit binding including an empty or invalid one.  It discovers no consumer stores.  This migration does not assign Muster a new path or borrow the letter registry's token.

## Readiness And Activation

- Verify the approved purpose, own read-only prod grant and finite expiry for every required exact project/folder route; record them in the existing service-token registry without values.
- Establish and verify Muster's own protected consumer binding contract and Finder-launch wiring; constructor test bindings alone do not satisfy this.
- Resolve the legacy doctor's extra `/fleetlink` probe and the selected knob-folder routing without widening a token grant or borrowing another consumer's token.
- Include the shared CLI module in both release payload manifests and the compatibility installer before publishing a legacy alias that imports it.
- Pass Python tests and the GitHub-hosted macOS build/tests, then verify real metadata-only names, a scoped named read and masked doctor output through Muster's own binding.
- Keep this source PR draft and unmerged until the binding and release decision are explicit.  A main-branch change under `muster/**` triggers `.github/workflows/muster-release.yml`, which publishes a Sparkle update that installed copies pick up automatically; PR builds do not publish.

The neutral command's separate `scripts/**` publication also triggers the existing fleet CLI server-update signal when merged.  PR #2176 therefore owns its complete compatibility payload; the native-only draft does not replace that delivery gate.
