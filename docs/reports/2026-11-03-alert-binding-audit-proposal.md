# Alert Binding Self-Metadata Audit Proposal

Refs AFC-600.  This is a source proposal for parent and independent review before publication or credential use.  It has not run against GitHub secret bindings or Infisical.

The separate manual-only workflow is `.github/workflows/alert-infisical-binding-audit.yml`.  Its standalone Python auditor is `scripts/audit-alert-infisical-binding.py`; the reproducible offline suite is `scripts/test-audit-alert-infisical-binding.py`.  The checked-in JSON fixtures contain synthetic identifiers and no credential.

## Source And Contract

The inspected trusted main was `738b92fcd975866c9a64bdc89aea285a3465c97b`.  Its `scripts/infisical-fetch.mjs` still implements universal-auth login and raw-secret listing, so this proposal does not import it.  The service-token verifier inspected at unmerged candidate `74930debdad043ac60a952d287479adfa94bdc5d` supplied the bounded self-metadata validation pattern, not approved credential execution authority.

The auditor accepts only the existing `AFC_ALERT_WORKERS_INFISICAL_TOKEN` and separate `INFISICAL_FLEET_OPS_PROJECT_ID` bindings.  No inventory identifier or expected token name is embedded.  The project binding is validated independently before the request; token metadata cannot choose the expected project.  There is no workflow environment selection, matching the existing consumer's repository/inherited-organization delivery boundary.

At most one request is made: `GET https://app.infisical.com/api/v2/service-token`.  The transport uses normal TLS verification, ignores ambient proxy configuration, refuses redirects, has a ten-second socket timeout and reads at most 128 KiB plus one byte.  The workflow has a three-minute overall job timeout.  It does not retry, log in, list secret names or values, export into `GITHUB_ENV`, invoke a provider, deploy, call a model, write a registry or change a grant.

Required metadata is an object with UUID `id`, a nonempty bounded ASCII token name, UUID `workspace`, exact singleton `scopes: [{environment: "prod", secretPath: "/"}]` and exact singleton `permissions: ["read"]`.  Present `_id` and `projectId` aliases must agree.  A null or absent `expiresAt` is the verified legacy contract for a nonexpiring grant and is accepted with `nonexpiring: true`.  A non-null expiry must be well-formed, timezone-aware and future.  Extra metadata fields are ignored and never emitted.  Duplicate JSON keys, nonfinite JSON constants, malformed UTF-8, unsupported identity shapes and reflected bearer names are refused.

This pins the observed legacy self-metadata contract, not every future API shape.  Missing required identity/grant fields, name syntax drift, invalid non-null expiry syntax or an unsupported schema produce a fixed refusal rather than a compatibility fallback.  A successful self-metadata response verifies the bearer grant, not its human purpose, issuer, registry state, original GitHub secret location or runtime activation.  The actual expected expiry and purpose must be matched privately against the canonical registry; accepting nonexpiring metadata changes no issuance or token policy.

## Output Boundary

The JSON receipt contains only fixed enums/booleans and SHA256 digests of fully validated token ID, token name and project ID.  No bearer digest is computed.  Hash fields stay empty on every refusal.  `expiry_verified: true` means expiry metadata conforms to the API contract; `nonexpiring` distinguishes null/absent expiry from a validated future date.  It does not claim the expiry matches the registry.  A success always retains `purpose_verified: false`; matching the hashes against the canonical private registry and authorized consumer purpose is a separate private review.

`missing_delivery` means an effective process binding was missing or empty.  It does not assert that a GitHub secret object is absent, and cannot distinguish missing repository objects, organization inheritance, policy withholding or empty configured values.  Transport/provider failures emit only local enums, never exception text, response bodies, URLs or raw metadata.

## Review And Release Sequence

1. Parent and an independent reviewer inspect the exact local commit and reproduce the offline suite.  No publication, PR or credential read is authorized by this proposal alone.
2. Parent separately releases publication through the existing own Git Relay route.  Read every review surface, preserve Draft until the parent's decision, and check effects of existing CI followers before any default-branch merge.  This proposal changes none of those existing workflows or their admission policy.
3. After reviewed default-branch publication, read back the exact merged source, workflow registration/state and current `main` head.  GitHub requires default-branch registration for `workflow_dispatch`; a new branch file alone is not runnable.  Do not enable a disabled workflow or change admission/permissions to satisfy this check.
4. Parent explicitly releases one metadata read for the exact merged SHA.  Dispatch `alert-infisical-binding-audit.yml` with `ref: "main"`, `inputs: {expected_source_sha: "<released exact 40-character SHA>"}`, a new meaningful `intent_id` for this approved operation and the same `expected_head_sha`.  Reuse that intent and identical payload for outcome reconciliation; an unknown outcome is not permission to retry with a new intent.
5. The relay checks branch head before dispatch, but that GET/POST fence is not atomic.  The workflow rejects a differing `GITHUB_SHA` before checkout or credential delivery, checks out the immutable event SHA, verifies checkout HEAD and runs offline tests before exposing the two bindings to the auditor.  The auditor independently requires the manual event, exact repository, main ref and matching expected SHA.
6. Read the actual run ID/head SHA, event, workflow path, step disposition and sanitized receipt.  Reject source mismatch or unexpected execution before accepting evidence.  Acceptance alone is not a completed audit.  Privately compare successful metadata hashes and grant purpose with the canonical registry; any write, activation or registry update needs separate task authority.

The inspected relay source supports the intent fields and requires a branch name for its expected-head path.  Its reviewed seat-policy source includes CODEX workflow dispatch with the necessary permission ceiling, but source and exposed tools do not prove this new workflow is admitted on the deployed service.  No dispatch was used to test admission.  If registration or admission is refused, stop and report the exact boundary; do not reuse another workflow's name, activate a typed lane, broaden a permission or alter an allowance.

The checkout action is pinned to `3d3c42e5aac5ba805825da76410c181273ba90b1`, observed from the official `actions/checkout` v7 tag through the existing connected GitHub read route.  The auditor has no package installation or third-party Python dependency.

## Offline Validation

Run from the owned lane:

```sh
python3 scripts/test-audit-alert-infisical-binding.py
ruby scripts/check-workflow-duplicate-keys.rb .github/workflows/alert-infisical-binding-audit.yml
git diff --check
```

The suite injects synthetic bindings, metadata responses and a fake opener; it does not read process credential bindings or make network calls.  It covers missing delivery, invocation/source guards, project/identity/scope/permission failures, valid null/absent expiry, invalid non-null expiry, redirects, bounded reads, socket/read timeouts, provider errors, proxy isolation, one exact GET and hostile-field redaction.  Workflow assertions keep the trigger manual-only and the two fixed secret references after source and offline checks.

Official contracts: [manual workflow registration](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#onworkflow_dispatch), [dispatch branch/tag refs](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event).  Local relay source: `git-relay/git_relay/github_client.py` and `docs/github-capabilities/dispatch-intents.md`.
