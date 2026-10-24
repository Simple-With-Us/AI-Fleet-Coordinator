# v9.3 delta — rebase of the reviewed shared-bearer chain onto main a0a91169

Base for application: main a0a91169. Prior chain (v9 archive 0e121456, v9.1
delta b018b472, ed11c004 comment fix, v9.2 delta a7ab29d7) was reviewed against
f4f89972..2ca64d28-era main. Main then advanced; #891's naming-accuracy merge
touched the proposal's two hottest files, so v9's pages/index diffs no longer
applied (6/7 and 5/12 hunks failed). This delta is the rebased result for
independent review and durable storage only — not activation.

## What changed vs the reviewed head (review surface)

reviewed-to-rebased/pages.js.diff is the ENTIRE review surface for pages.js —
two hunks:
1. STYLE/STYLE_HASH: merged stylesheet keeps ALL of (a) main's new h3 rule,
   (b) main's new narrow-table scroll shadows, (c) v9's .bearer-warn/.bearer-form
   rules, (d) v9.2's media-query wrap override (white-space:normal;max-width:22rem).
   New STYLE_HASH sha256-Avk4XEaWQY0Yay3R20IVAj0GWFbfR/Lz43h+if3gbbo=,
   recomputed; the suite's pages.test.mjs recomputation passes on it.
2. #891's manual-seats restructure preserved: the manual-seats table is now
   conditional with an h3, followed by the "Hand-registered clients" h3.

index.js: reviewed-to-rebased diff is EMPTY, verified intentional. v9's index
rewrite had already renamed the same six "Agent-Sync Admin" strings to
"ST-MCP Admin" (canonical rename map) that #891 renamed; both sides produced
identical lines, so the three-way merge result equals the reviewed head
byte-for-byte (base had 6 "Agent-Sync Admin", result has the same 6 lines as
"ST-MCP Admin", zero old-name remnants anywhere in pages.js/index.js).

## Manual conflict resolutions (explicit)

Three-way merge: base f4f89972, side A main a0a91169, side B reviewed head.
- pages.js STYLE line: both sides rewrote it -> hand-merged all four rule sets
  (listed above), hash recomputed. Nothing dropped.
- pages.js reveal send-line: kept the reviewed acceptance-branched `send`
  copy (main's side was the pre-feature static line); adjacent #891 rename
  ("Back to ST-MCP Admin") preserved.
- index.js: merged cleanly, no hunks needed hand work.

## Package contents

- worker-pages.js.diff, worker-index.js.diff: FULL diffs a0a91169 -> rebased
  (apply with the other v9/v9.1 diffs minus their pages/index members to
  reconstruct; ed11c004 is folded into the index merge, superseded here).
- reviewed-to-rebased/pages.js.diff: the review surface described above.

## Executed evidence (this composition, main a0a91169 + full chain)

- Worker module suite: 630 tests, 622 pass, 8 skip (pre-existing environment
  skips, identical set), 0 fail.
- workerd integration (test/integration.workerd.mjs): 61 passed.
- Python (git_relay/tests incl. test_shared_bearer.py): 136/136 OK.
- pages.js/index.js carry zero "Agent-Sync Admin" remnants; #891 renames,
  h3 style, scroll shadows and manual-seats restructure all verified present.

## Pixel verification (no baseline regeneration)

Rendered from THIS rebased tree and visually inspected (attached):
- Admin bearer section at true 390px (iframe-viewport technique; headless
  Chrome enforces a 500px minimum window width): unscrolled and
  scrolled-to-Actions — full rotation-consequence warning and checkbox label
  readable after v9.2's wrap; #891's scroll shadows now visible on the
  horizontally scrolled table.
- Admin bearer section at 1280px wide.
- Reveal (ticked/unticked) and stale-refusal pages.
Intentional differences from the pre-v9 visual-tests snapshots on main: the
generalized bearer section (all identities), the warning/checkbox controls,
the reveal send-line branching, h3 headings, scroll shadows. No rendering
regression observed. The Playwright snapshot specs added on main after the
review base WILL mismatch by design; regeneration is a later reviewed step,
not done here.

## Out of scope / held

No key provisioning, no CF persistence decision (Jay's SSOT architecture call),
no deployment, no cutover, no snapshot regeneration. Host readiness wording:
default git-relay service NOT OBSERVED on the Hetzner host (Wise 20:15Z,
scoped to default names/locations); secret-name existence UNKNOWN
(fleet-secrets fetch failed, exit 1) — not "clear-but-unprovisioned".
