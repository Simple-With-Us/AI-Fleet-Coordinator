# Direct Recall Credential Readers

Refs [AFC-785](https://linear.app/simple-with-us/issue/AFC-785).  Requires [#2167](https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/2167), including its mandatory finite-expiry verifier and standalone runtime payload.

`fleet_rag.core` now uses the canonical `fleet_infisical` module for both existing reader purposes.  Optional public-fallback credentials and the ingest heartbeat retain Fleet-OPS, prod, exact root-folder semantics, named reads, the existing 60-second budget and absent-on-failure behavior.  Their callers still prefer a configured value in the environment.  The compatibility names `fleet_secret`, `fleet_secrets_many` and `fleet_secret_get` start no application or child process.

The separate recall configuration reader retains its selected project, API override, prod-only rule, own `FLEET_RECALL_INFISICAL_TOKEN` binding and existing protected consumer-file route.  Its direct named read has the existing 30-second budget.  Both purposes pin the exact root folder before metadata verification, so an ambient multi-folder setting cannot widen a grant.  A missing recall binding fails before the shared reader can consider another project's token.

| Consumer Purpose | Project And Folder | Own Binding | Activation Evidence |
| --- | --- | --- | --- |
| Public fallback and ingest heartbeat optional credentials | Fleet-OPS, prod, `/` | Consumer-owned `INFISICAL_TOKEN`; Fleet-OPS project ID through its existing binding | Not verified by this source change; verify exact read-only scope and finite expiry before replacing the active route |
| Recall's local Qdrant/TEI configuration | Existing selected recall project, prod, `/` | `FLEET_RECALL_INFISICAL_TOKEN`, directly or from its existing protected consumer file | Existing route retained; verify its own exact grant and finite expiry independently |

The recall-project token, the letter registry token and another consumer's Fleet-OPS token are not substitutes for the optional reader's own binding.  This change creates no token, reads no live consumer store and activates no service.  Preserve the working deployed route until the replacement access and shipped shared module are verified.

Tests exercise real loopback metadata and named reads, reject wider grants before values, prove cross-project token isolation, preserve optional and environment behavior, and run the installed recall reader under isolated Python while rejecting all legacy application imports.  The shared hermetic fixture now disables the direct optional-read seam rather than relying on the absence of an executable.
