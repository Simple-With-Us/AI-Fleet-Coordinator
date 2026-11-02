# Muster fleet overhaul sweep

Owner-directed fleet-mode sweep: seven tiered auditors produced about 95 findings across Muster and the fleet scripts, synthesized into ten file-disjoint fix batches plus one follow-up.  Cheaper models took mechanical batches; the frontier model took only git-relay secret handling and synthesis.  Gates below ran in this lane after integration, not only in worker worktrees.

## What landed

* Board hosted writes work again: remote writes inject the validated hosted identity inside `remote_call`, with local single-value validation, label mapping and static error hints.
* Recall and Infisical correctness: refreshed settings reset the recall config cache, non-prod Infisical slugs are refused in `fleet_rag`, refresh keeps the env backfill and last-known-good, revoked keys leave `os.environ`, and the recall bootstrap aborts by default without gitleaks.
* Backups fail closed on secrets by meaning (any `.env` basename, key and service-account shapes), the sentry monitor isolates each check and warns at the janitor disk band, and the Drive backup legs fail independently.
* Muster performance: process deadlines with concurrent capped pipe drains, single-flight refresh, artifacts off the view body, streamed config reads, detached MiniMax reads, capped engine output, batched drift saves with failure backoff.
* Muster breadth: a real Settings scene for repo root, scripts dir, Zulip seat and theme; wake Apply, Test and Pin confirm first; empty states with Clear Filters; a knob unset engine verb wired to a Touch ID Reset To Default; a Secret Names panel that can only ever show names and booleans; a masked Recall Settings panel storing a base URL and a token key name, never a token.
* GitHub relay hardening: 401s are audited without token material, credential-shaped values are hashed by key meaning at any depth, the secret scanner covers common credential families, malformed MCP lines get protocol errors, and plaintext relay URLs are refused off loopback.
* CI and site honesty: the autofix marker lookup and repo backups fail closed, gitleaks scans provable history, CI gains concurrency, least-privilege permissions, caches, a pinned PyYAML and the ECHO custody tests.  The digest site gains unmasked deterministic visual coverage at phone and desktop widths, an axe serious-zero gate, failed-request assertions, dark-mode variables, and a 6.9KB app icon replacing a 797KB one.
* Hosted parity: hosted Linear list ANDs its filters and honours env, remote chat serves guide locally plus search, dm-read and edit, per-seat locks free concurrent reads, gave-up inbox delivery notifies, Zulip linkifiers use the caller's own seat credential, fleet knobs fetch only named keys, and three lints that could silently pass now fail loudly.

## Gates actually run in this lane

* `xcodebuild test -scheme Muster -destination platform=macOS CODE_SIGNING_ALLOWED=NO` — 617 tests, 1 skipped, 0 failures.
* `python3 scripts/linear-board/test_board_remote.py` — 30 OK.  `cd git-relay` — 199 OK (hardening, relay, typed gateway).  `cd scripts && python3 -m unittest discover -s fleet_knobs/tests -t .` — 113 OK plus inventory check exit 0. `fleet_rag` infisical suites — 67 OK, wider recall set 143 OK.  `scripts.test_backup_exclusions` — 6 OK.
* `cd scripts/agent-sync-mcp && npm ci && npm test` — 1080 pass.  `cd visual-tests && npm ci && npx playwright test tests/smoke.spec.ts tests/visual.spec.ts tests/a11y.spec.ts` — 17 passed.  All 25 workflow YAML files parse.

## Follow-ups and honest gaps

* Wave 1's first run died on subscription quota; two batches were salvaged from retained worktrees and re-gated here before commit.
* The audit's adversarial critic pass failed on spilled inputs, so workers re-verified each citation before fixing; remaining unaudited depth includes BotFleet VM internals inside Muster FleetServices and per-verb git-relay workflow bodies.
* Pre-existing failures reproduced on untouched bases and left alone: relay `test_policy_credential` and ECHO custody (Mac Python lacks `os.getxattr`), one daemon entry-script test, 22 agent-sync macOS snapshot mismatches, and Muster app signing locally (suite runs with signing disabled).
* Deferred large Muster builds needing owner decisions: Registry editing for fleet-apps and fleet-seats, Linear actions inside Muster, held-back queue inspection with editable daemon settings, Coolify start/stop/env/logs allowlist growth, and a unified Mac Processes view.
* The Linear issue for this sweep could not be filed at claim time because hosted board writes were broken (batch one of this sweep).  File and claim it as soon as the merged runtime sync makes the fixed CLI live, then close it from the claim topic.
