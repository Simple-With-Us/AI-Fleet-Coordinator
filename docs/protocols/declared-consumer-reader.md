# Declared Consumer Reader

`scripts/infisical-scoped.py` optionally accepts `--consumer-binding ABS --consumer EXPECTED` on `get`, `names`, and `doctor`.  Both flags are required together.  Existing callers retain their account table, explicit account selection, and read-only null-expiry compatibility when these flags are omitted.

The declaration selects one existing grant; it does not create a grant, credential, registry row, installation, or release authorization.  The held native Muster migration and its own grant, registry, custody, Finder acceptance, and explicit hold-release gates remain separate.

Normal fleet CLI bundles carry `scripts/infisical-scoped.py` beside the canonical shared client.  The release manifest requires the reader for new bundles; retained older rollback bundles remain valid without it.  The staged-payload regression runs the actual reader's help under isolated Python without store or provider access.  The legacy compatibility installer is not this deployment route.

## Declaration Schema

The JSON object has exactly these keys; duplicate and unknown keys at every level are refused.

| Key | Required Value |
|---|---|
| `schemaVersion` | Integer `1` |
| `consumer` | Exact `--consumer` identifier |
| `project` | Exact requested project slug, `fleet-ops` or `ai-fleet-coordinator` |
| `projectId` | UUID equal to the existing project resolver's result |
| `environment` | `prod` |
| `secretPath` | Exact requested `--folder`; selected doctor defaults to `/` |
| `permissions` | Exactly `["read"]` |
| `expiresAt` | Finite future RFC 3339 timestamp with seconds and an explicit offset or `Z` |
| `tokenName` | Approved registry token name, also the exact storage account |
| `tokenId` | Approved registry token identifier |
| `storage` | Exactly `{"kind":"mac-login-keychain","service":"infisical-service-token","account":tokenName}` |

Consumer and token-name identifiers contain 1–128 ASCII letters, digits, dots, underscores, or hyphens, start with a letter or digit, and cannot start with `st.`.  Token IDs contain 1–128 ASCII letters, digits, underscores, or hyphens.  Inline tokens, values, and extra fields are not supported.

## Custody And Verification

The absolute declaration path is walked through held descriptors without following symlink components.  Its immediate directory must belong to the current user with mode `0700`; the regular file must have the same owner, mode `0600`, one link, and a size of at most 16 KiB.  The shared reader rechecks directory and file identity after capture.

The declared route refuses `--account`, ambient authentication bindings, folder-binding markers, and API overrides, including empty markers.  Existing project-ID metadata may resolve the requested project, but its result must equal the declaration; the declaration never replaces the resolver.  The provider environment contains only prod and the exact folder, and uses the canonical provider origin.

The shared `token_from_store(..., bounded_keychain=True)` route reads only the named service/account in the caller home's `Library/Keychains/login.keychain-db`, passed explicitly to `security` without consulting the keychain search list.  It has a five-second maximum and 8 KiB stdout capture limit.  Stderr is discarded.  It refuses other platforms and never falls back to a file, another keychain or account, a session, or an environment token.  The unselected store API keeps its existing behavior.

The existing token-metadata request verifies exact live ID, name, project, prod folder, read permission, and finite future expiry before any value request.  A conflicting `_id` is refused.  Expected and live expiry must represent the same instant.  There is no second token-metadata request for identity verification.

Declared `get` uses the canonical `bounded_fetch_project` worker with strict row provenance, one requested name, a 30-second worker budget, and an 8 KiB private-response cap.  Token-store capture has its separate five-second budget.  The token enters the worker only through private stdin, never argv, files, inherited environment, or diagnostics.

Declared `names` and masked `doctor` use the canonical metadata-only listing (`viewSecretValue=false`) and strict workspace, prod, folder, and shared-type provenance.  They perform no value read.  Doctor retains the existing JSON fields and exposes only a fixed failure reason on refusal.  This interface does not claim the held native source's older total-operation timing or value-counting doctor contract.

## Source Verification

Run from an owned lane:

```sh
PYTHONPATH=scripts python3 -m unittest scripts.test_infisical_scoped scripts.test_scoped_token_store fleet_secrets.tests.test_bounded_reads
```

The tests use protected temporary declarations and stateful synthetic stores, the actual project resolver, disposable canonical workers, and a loopback verifier.  They establish source behavior only; they neither access real host custody nor establish runtime acceptance.
