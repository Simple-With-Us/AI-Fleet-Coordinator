# Direct Infisical Reader

This source migration is not activated.  The installed Muster binary and its working route must remain in place until this consumer has its own verified binding and the replacement has passed CI and runtime checks.

This composed draft incorporates the shared reader, neutral command/payload, registry bootstrap and retained writer sources from PRs #2167, #2176, #2166 and #2188, together with the native reader fixes from #2184.  Those dependencies must land and the resulting source must be verified before merge.  This branch includes both `scripts/**` and `muster/**`; PR builds publish nothing, while a main-branch native change automatically publishes a Sparkle update.

Muster uses its existing engine interpreter to invoke `python3 -I /absolute/reviewed/scripts/fleet_infisical_cli.py`.  Isolated mode excludes the current directory and ambient Python paths; the entry imports the shared `fleet_infisical` REST client from its own reviewed directory.  Swift does not implement a second HTTP reader.  The old `fleet_secrets.cli` module is a compatibility alias for the same command implementation, so existing callers retain one implementation.

The required #2176 entry explicitly prepends its own real script directory to `sys.path` before importing the shared client.  Keep `-I` and omit `PYTHONPATH`: reverting to `-s -m` would let a foreign working directory shadow the reviewed reader.  This composed draft includes that source; its arrival on main, installed-payload verification and metadata-only names acceptance through Muster's own binding remain delivery gates.  When Python reports that this exact entry is absent, all three native reads report that the reader is unavailable, without exposing the child diagnostic.

The production factory selects the protected nonsecret declaration at `~/.config/infisical/muster.json` through the shared command's `--consumer-binding` route.  It declares only the approved `muster-data-reader` profile and Keychain account; the generic custody primitive reads only that account, then the shared reader verifies the grant's live identity, exact Fleet-OPS prod root read scope and finite expiry before values.  The bridge scrubs ambient bindings, rejects constructor-binding conflicts and has no application or another-consumer fallback.  Constructor injection remains an inert test seam.  No actual Muster data-reader grant, registry entry, declaration, Keychain binding or Finder acceptance has been established by these source tests.

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

The generic private-store primitive still accepts only a caller-declared absolute directory, filename and binding, with owner-matched protected directory/file checks, no-follow handling and a bounded read.  The Finder route uses the same custody rules for its nonsecret declaration and the generic named-Keychain primitive; it never borrows the letter registry's store.  The declared source path is a contract awaiting its own verified installation, not an installed grant receipt.

## Readiness And Activation

- Verify the declared nonsecret catalog in hosted native CI; it must perform no Mac `/wake-router` folder listing or claim a provider configuration receipt.
- Verify the approved purpose, own read-only prod grant and finite expiry only for the remaining permitted exact project/folder routes; record them in the existing service-token registry without values.
- Establish and verify Muster's own protected consumer binding contract and Finder-launch wiring; constructor test bindings alone do not satisfy this.
- Verify the selected doctor uses only Muster's own Fleet-OPS root binding and never requests AFC `/fleetlink`; no additional doctor grant is needed.
- Include the shared CLI module in both release payload manifests and the compatibility installer before publishing a legacy alias that imports it.
- Pass Python tests and the GitHub-hosted macOS build/tests, then verify real metadata-only names, a scoped named read and masked doctor output through Muster's own binding.
- Keep this source PR draft and unmerged until the binding and release decision are explicit.  A main-branch change under `muster/**` triggers `.github/workflows/muster-release.yml`, which publishes a Sparkle update that installed copies pick up automatically; PR builds do not publish.

The composed `scripts/**` publication also triggers the existing fleet CLI server-update signal when merged.  The complete compatibility payload and each affected host's own binding acceptance therefore remain delivery gates alongside the native release hold.
