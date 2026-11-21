# Direct Infisical Reader

This native source is held and is not activated.  Preserve the installed Muster binary and its working route until the own-consumer grant, custody, current CI/review, actual Finder acceptance and explicit owner release gates are satisfied.  A merge changing `muster/**` automatically publishes a Sparkle update through `.github/workflows/muster-release.yml`; source fixtures and a draft PR do not authorize that publication.

AFC-868 composes only the finite native adapter, catalog, matching tests and documentation from held PR #2187 onto current main.  It preserves the later native UI and autosave implementation.  The shared declared-consumer prerequisite is AFC-867, documented in [Declared Consumer Reader](../../docs/protocols/declared-consumer-reader.md); this native source adds no Python reader, REST client, credential implementation or deployment route.

Every `FleetSecrets` initializer selects the same own-consumer declaration.  The production call sites use `FleetSecrets.managed()`.  Construction reads no credential store.  The existing engine locator selects the engine checkout, while the reader uses fixed system Python and an absolute reviewed entry:

```text
/usr/bin/python3 -I <engine>/scripts/infisical-scoped.py get NAME --folder / --project fleet-ops --consumer-binding <home>/.config/infisical/muster.json --consumer muster-data-reader
```

`names` and `doctor` use the same folder, project and consumer selectors.  Doctor emits JSON by default; the native adapter passes neither `--json` nor the retired `--path` spelling.  Isolated Python excludes ambient Python paths.  Its child environment contains only `HOME` and the system `PATH`, so ambient authentication, folder and API markers are absent even when the parent marker is empty.  The shared reader uses its existing approved project metadata resolver and compares that result with the declaration.

| Operation | Source And Scope | Receipt |
|---|---|---|
| `get NAME`, including the recall admin key name | Own Fleet-OPS prod `/` read-only declaration | Only the requested value returns to its consumer |
| Controls Secret Names | Same exact declared scope | Metadata-only names; no value or configured-value receipt |
| Controls Declared Knob Catalog | Existing local nonsecret KnobRegistry definitions | Declared names and folders only; no provider access |
| Controls Doctor | Same exact declared scope | Metadata-only names checks and fixed masked refusal reason |

The knob catalog never lists the AFC `/wake-router` folder.  Its `/wake-router` label is existing declaration metadata; it does not require a grant or prove provider configuration.  The knob table retains its current values, configuration status and stale-copy behavior.

The native runner's timeout is 45 seconds, followed by its existing termination grace of up to two seconds when needed.  A selected named read has the shared reader's 30-second worker budget plus its separate five-second store budget; this document makes no older 15-second total-operation claim.  Names and doctor use the metadata-only shared path.  Exit 3 means a missing named key, exit 124 means unavailable, other nonzero key reads mean refusal, and cancellation errors propagate.  An exact Python missing-entry diagnostic maps to a missing reader without displaying raw stderr.  The doctor displays only its fixed booleans, names count and `consumer-binding-refused`, never raw diagnostics, value counts or unresolved-reference counts.

The normal reviewed engine/release payload must include `scripts/infisical-scoped.py` and its existing shared `fleet_infisical.py` dependency before this source can ship.  Native source does not install a helper or re-enable a retired command.  The shared prerequisite owns payload closure.

## Readiness And Release Hold

- Establish Muster's own finite Fleet-OPS prod root read-only grant and record names-only metadata in the existing service-token registry.
- Establish the exact own login-Keychain item and protected nonsecret declaration described in [Finder Infisical Reader Binding](FINDER-INFISICAL-BINDING.md).
- Complete current-head review, CI and native code review; Linux source checks do not establish a Mac build or UI acceptance.
- Demonstrate actual Finder-launched `get`, metadata-only `names` and masked `doctor` through this own declaration, without shell-token injection or another consumer's account.
- Obtain the owner's explicit release of the existing source and automatic Sparkle publication hold before merge or activation.

No grant, registry entry, Keychain item, protected declaration, installed replacement, Finder acceptance or hold release is claimed by this source.
