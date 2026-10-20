# Fleet GitOps Migration Priorities

Refs AFC-368.  This public document describes proposed outcomes and sequencing.  The detailed operational inventory and historical drift observations are preserved in a restricted companion under AFC-368.

This plan does not approve deployments, restarts, resource deletion, credential creation, or changes to owner-managed configuration.  Implementation issues must retain the owner gates below and verify current state before acting.

## Goal

Move reviewed configuration toward declared desired state, read-only drift checks, and verified changes from approved commits.  Keep secret values in Infisical and private operational inventory outside public source.

## Ranked Priorities

Benefit, risk, and effort are relative planning estimates: H is high, M is medium, and L is low.  These are proposed work items, not claims that a migration has shipped.

| Rank | Item | Benefit | Risk | Effort | Issue |
|---|---|---|---|---|---|
| 1 | G1: Declare host configuration and introduce check-only reconciliation | H | L | M | AFC-369 |
| 2 | G2: Reconcile agent configuration from reviewed source on each supported host | H | L | M | AFC-370 |
| 3 | G3: Consolidate the operator start page under one maintained source | M | L | L | AFC-371 |
| 4 | G4: Review and retire obsolete artifacts using the restricted inventory | M | L | L | AFC-372 |
| 5 | G5: Introduce an explicit deployment gate for each eligible Worker | H | M | L | AFC-373 |
| 6 | G6: Build and deploy the recall service from a reviewed container definition | M | M | M | AFC-374 |
| 7 | G7: Declare supported Mac background processes and reconcile in check-only mode first | H | M | H | AFC-375 |
| 8 | G8: Propagate repository templates through reviewable sync pull requests | H | L | M | AFC-376 |
| 9 | G9: Declare collaboration configuration with checks for owner-only operations | M | L | L | AFC-377 |
| 10 | G10: Audit the GitHub organization baseline against an approved repository set | M | L | M | AFC-378 |
| 11 | G11: Export non-secret service configuration and detect drift | M | M | M | AFC-379 |
| 12 | G12: Load approved Worker secret bindings from Infisical during deployment | M | M | M | AFC-380 |
| 13 | G13: Declare Linear workflow states and labels | M | L | M | AFC-381 |
| 14 | G14: Declare observability configuration and verify alert delivery | L | L | M | OPS-974 |
| 15 | G15: Audit secret-store structure and access without exporting secret values | M | M | M | OPS-975 |
| 16 | G16: Export DNS and edge configuration, detect drift, then consider reviewed apply | H | H | H | OPS-976 |

## Acceptance Boundaries

- G1 and G7 start with read-only comparison.  Restarting changed processes requires a separate owner decision and verified rollback.
- G2 leaves owner-managed instruction files report-only unless the owner explicitly changes that boundary.
- G3 concerns the operator start page.  The supported daily digest site remains in AFC; this plan does not remove that web surface.  The source decision is settled, while proof that every operator-page deployment follows the approved push path remains open under G3.
- G4 and G7 exclude retired or never-installed jobs from desired state.  A manifest must not schedule an obsolete job merely because it appears in the historical inventory.
- G5 uses a per-Worker gate, a reviewed path scope, deployment provenance, and a health probe.  Do not enable a repository-wide deployment switch as a substitute.  The hosted agent identity service remains excluded until its explicit owner gate and credential-scoping requirements are satisfied.
- G11 and G15 keep resource identities, topology, operational weaknesses, and access details in the restricted inventory.  Public source contains schemas and generic procedures.
- G12 stores secret names and wiring in source; values remain in Infisical.
- G16 begins with export and drift detection.  Tool choice, zone scope, credentials, and apply order remain owner decisions.

## Decisions Reserved for the Owner

1. Whether the hosted agent identity service may deploy from CI after its credential and review boundaries are defined.
2. Which narrowly scoped deployment and DNS credentials may be created and used by approved jobs.
3. Whether reconcilers may restart changed processes after check-only verification, or must remain report-only.
4. Whether owner-managed instruction files must remain report-only.
5. Approval of G3 deployment-path acceptance evidence; the one-source decision does not itself prove that every deployment uses that path.
6. Which obsolete resources may be deleted after current inventory and usage are verified.
7. Authorization and scope for credentials that can change GitHub rulesets.
8. Authorization for a secret-store administrative identity and a choice of structure-management tooling.
9. DNS and edge tooling, zones, and rollout order.
10. The approved authentication approach for background-process reconciliation.

## Outside Automatic Reconciliation

Secret values, owner-only account and role creation, content records, vendor control planes, and store release decisions do not become automatically writable because this plan exists.  Existing deployment and release policies remain binding.

## Verification Before Implementation

Each implementation issue must inspect its current repository and runtime state, name its owner gate, provide a dry-run or focused regression where appropriate, preserve rollback, and distinguish a merged change from a verified deployment.  Historical inventory observations are leads to recheck, not current deployment evidence.
