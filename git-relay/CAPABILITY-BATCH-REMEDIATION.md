# Capability batch remediation

This PR is not a completed capability implementation and stays a draft.

## Fixed in this source pass

- Reconciled the old branch with main at 9433a8d48f096714bef4fd9043afd2445a3d2421 without replacing main's evolved relay guards.
- Removed policy grants for verbs absent from the actual typed implementation table. Unsupported action names must not be advertised as available or installed in fleet seat policy.
- Removed the generic administration-write token ceiling expansion. Main's typed repository-creation route and current exact-scope rules remain unchanged.
- Retained the historical capability documents as explicitly unimplemented design proposals. Their presence grants nothing and makes no deployment claim.
- Preserved and tested the response decode fix: invalid UTF-8, invalid JSON, over-limit integer tokens, recursion failures and lone surrogates become transport-unknown outcomes after an upstream write, rather than local refusals that could release an outcome key for duplicate replay.

## Still unfinished

The proposed repository administration, collaborator writes, branch-protection writes, ruleset writes, webhook writes, Actions settings and other missing verbs still need typed implementations, least-permission minting, fixed-input validation, bounded secret-safe projections, action classification, tests and current-head review. Do not restore policy grants ahead of those implementations. Owner review of the relay permission surface and green current-head CI remain readiness criteria. No live policy, permission, credentials or service changed in this pass.

## Verification

On Python 3.12, the exact final reconciled production tree passes all 1760 relay tests. This includes the three added decode-boundary tests, covering seven malformed input classes, valid scalar/array/Unicode bodies and empty completion.
