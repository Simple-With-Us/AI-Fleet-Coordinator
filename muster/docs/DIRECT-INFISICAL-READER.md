# Direct Infisical Reader

This source migration is not activated.  The installed Muster binary and its working route must remain in place until this consumer has its own verified binding and the replacement has passed CI and runtime checks.

This native-only draft depends on shared library PR #2167 and the neutral command/payload PR #2176.  Shared bootstrap packaging in PR #2166 is separately coordinated.  These shared sources are not copied into the native change; all dependencies must be incorporated and verified before a final review or merge.

Muster uses its existing engine interpreter to invoke `python3 -I /absolute/reviewed/scripts/fleet_infisical_cli.py`.  Isolated mode excludes the current directory and ambient Python paths; the entry imports the shared `fleet_infisical` REST client from its own reviewed directory.  Swift does not implement a second HTTP reader.  The old `fleet_secrets.cli` module is a compatibility alias for the same command implementation, so existing callers retain one implementation.

The required #2176 entry explicitly prepends its own real script directory to `sys.path` before importing the shared client.  Keep `-I` and omit `PYTHONPATH`: reverting to `-s -m` would let a foreign working directory shadow the reviewed reader.  This native draft does not yet include that dependency; the composed-source import and metadata-only names checks remain delivery gates.  When Python reports that this exact entry is absent, all three native reads report that the reader is unavailable, without exposing the child diagnostic.

The bridge passes only constructor-injected, explicitly named read bindings into the bounded subprocess.  It does not inherit ambient tokens, inspect another consumer's store, persist a token, or add a legacy credential fallback.  Bound named reads request only the selected keys; metadata-only names never exports values.  Shared consumer reads require their own scoped service token in both interactive and noninteractive processes; a TTY never authorizes a tokenless administrative-session fallback.  Constructor injection proves source behavior in tests; it does not configure an app launched from Finder.  No persistent Muster runtime token store or runtime data-reader grant has been verified for this migration.

An explicit API override uses the shared client's actual binding, `FLEET_SECRETS_API`.  The unused `INFISICAL_API_URL` spelling is not passed, and neither spelling is inherited from the ambient process.  Without an explicit override, the shared client uses its canonical API origin.

## Required Bindings And Scope

| Existing Operation | Project And Environment | Exact Folder | Binding And Behavior |
|---|---|---|---|
| `get NAME`, including the default recall admin key name `RECALL_ADMIN_TOKEN` | Fleet-OPS prod | `/` | Own read-only `INFISICAL_TOKEN`, with `INFISICAL_FLEET_OPS_PROJECT_ID`; only the requested name leaves the reader |
| Controls secret-name listing | Fleet-OPS prod | `/` | Same own exact-folder read route; `names` requests metadata only and returns no values |
| Controls declared knob catalog | No provider access | Declared folders only | Uses existing KnobRegistry nonsecret definitions; no Infisical listing, credential or configuration receipt |
| Muster selected doctor | Fleet-OPS prod | `/` | Explicit `doctor --project fleet-ops --path /` uses the same own read binding, preserves masked status/unresolved-reference counts and never probes AFC `/fleetlink` |

The Controls catalog takes only nonsecret names and declared folders from the existing KnobRegistry definitions.  A `/wake-router` label is catalog metadata, not a Mac folder read: no provider access or token is used for the catalog.  Its copy explicitly says declarations do not prove a name or value exists in Infisical.  Current values, stale-copy warnings and secret configuration status remain in the separate knob table, unchanged by the catalog.  `INFISICAL.md` prohibits Mac reads of `/wake-router` because a folder token could also read the router bearer; do not mint, broaden or borrow one.

Muster selects only Fleet-OPS prod `/` for the shared doctor, using the same own binding as its named read and secret-name listing.  It does not require an AFC `/fleetlink` grant.  The shared command's unqualified legacy doctor still checks both projects for existing callers; explicit selection narrows this consumer without adding another implementation.  Doctor validation reads values to count unresolved references but returns only masked status/counts; it is not a metadata-only catalog.

The shared private-store primitive accepts only a caller-declared absolute directory, filename and binding.  It requires an owner-matched `0700` directory and a regular owner-matched `0600`, single-link token file, refuses symlinks and FIFOs, bounds the read, and preserves any explicit binding including an empty or invalid one.  It discovers no consumer stores.  This migration does not assign Muster a new path or borrow the letter registry's token.

## Readiness And Activation

- Verify the declared nonsecret catalog in hosted native CI; it must perform no Mac `/wake-router` folder listing or claim a provider configuration receipt.
- Verify the approved purpose, own read-only prod grant and finite expiry only for the remaining permitted exact project/folder routes; record them in the existing service-token registry without values.
- Establish and verify Muster's own protected consumer binding contract and Finder-launch wiring; constructor test bindings alone do not satisfy this.
- Verify the selected doctor uses only Muster's own Fleet-OPS root binding and never requests AFC `/fleetlink`; no additional doctor grant is needed.
- Include the shared CLI module in both release payload manifests and the compatibility installer before publishing a legacy alias that imports it.
- Pass Python tests and the GitHub-hosted macOS build/tests, then verify real metadata-only names, a scoped named read and masked doctor output through Muster's own binding.
- Keep this source PR draft and unmerged until the binding and release decision are explicit.  A main-branch change under `muster/**` triggers `.github/workflows/muster-release.yml`, which publishes a Sparkle update that installed copies pick up automatically; PR builds do not publish.

The neutral command's separate `scripts/**` publication also triggers the existing fleet CLI server-update signal when merged.  PR #2176 therefore owns its complete compatibility payload; the native-only draft does not replace that delivery gate.
