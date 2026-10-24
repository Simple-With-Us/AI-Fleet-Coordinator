# v9.3 qualified source and pixel review

Preserved October 24, 2026. Proposal only, not activation or snapshot approval.

The branch source tree remains the earlier reviewed-base composition. This directory is append-only evidence for reconstruction on immutable main a0a91169, not a claim that the branch source tree has been rebased.

## Independent checks performed

- Original archive SHA256 c0263187e4823c13f02117495770c7f48c134674f98bc55419c4f7e8b18a52fc verified.
- Both full pages/index diffs apply cleanly to independently fetched immutable a0a91169 source.
- Reconstructed pages SHA256 7bb3ab886d92a6c7c8b04bfc2706d6850f07da32a8bd143950c719e8d6d448e1; index 23d571066a3c06b472fb85326dd525440952948bdc383ea10e6b789c9aaa47eb match source worker.
- Stylesheet CSP hash independently recomputed: sha256-Avk4XEaWQY0Yay3R20IVAj0GWFbfR/Lz43h+if3gbbo=.
- index matches prior reviewed composition plus the recorded ed11 comment correction. The reviewed-to-rebased index diff contains an explanatory comment only, not a source hunk.
- Source delta preserves h3/manual-seat headings, scroll shadows, warning and checkbox wrap rules, single-use refusal copy and acceptance-dependent reveal copy. Artifact claims of owner authority are not authority.

## Actual supplied pixels inspected

- 390px unscrolled: identity/state readable; dates clipped at right edge; actions offscreen. Large row whitespace remains.
- 390px scrolled to Actions: full rotation-consequence warning, checkbox label and both buttons readable; identity offscreen.
- Wide admin: readable identity/state/action layout and warning.
- Unticked reveal: clearly states GR-only and ST-MCP refusal; single-use/no accidental remint explanation readable. Token is a synthetic B fixture, not a live secret.

No new blocker found in this bounded rebase surface. This is not universal mobile clearance: identity and actions cannot be viewed together at390px. Ticked/stale/reveal390 pixels were not supplied to this reviewer. No visual baselines regenerated or approved.

## Executed evidence provenance

Worker-reported composition results:630 total Worker tests,622pass/8skip;61workerd;136Python. Those suites were NOT rerun by this independent reviewer. The stored INTEGRATION.md retains author evidence separately.

## Reconstruction

Start at a0a91169. Apply original v9 archive diffs except pages/index, copy its new source/tests into original package paths. Apply v9.1 seat-state and gb-bearer-test delta. Apply the v9.3 full pages/index diffs instead of the old ones. Do not apply v9.2 or ed11 separately, both are folded into v9.3. Preserve old archives for recovery.

## Holds

No key creation, credential copies, persistent Worker mirror, enrollment, client mapping, provisioning, deployment, snapshot regeneration, merge or cutover. Infisical remains sole secret source of truth; Worker runtime sourcing remains unresolved. Default relay service was not observed in the bounded host read, not proven absent everywhere. Secret-name existence remains unknown after the names query failed. Old Jet untouched.
