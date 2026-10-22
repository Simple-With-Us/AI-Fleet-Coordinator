# Fleet Ops Review — 2026-08-27

**Author:** CLAUDE (Fable), owner-requested.  **Board:** 1a413861.
**Method:** 14-agent parallel sweep (Sonnet readers, Fable synthesis + verification) over 170 Apple Notes (Aug 20–27), all 12 active repos' PRs/issues, THE BOARD (4,319 rows), a week of #agent-sync, all effort logs and handoffs, live production checks (UptimeRobot, Sentry, Coolify, HTTP health), and live Pinecone index stats.  Every headline claim below was verified against live state (gh, git, curl, board CLI) — several candidate "forgotten" items turned out to be done and were removed.

---

## A. Act today

### A1. The early Pinecone snap fired as designed — ~3 real trial days remain to decide
`PINECONE_TRIAL_ENDS_AT=2026-08-27T00:00:00Z` fired overnight, but that date is the owner's **deliberate safety buffer**, set earlier than the real Standard trial end (~2026-08-30) precisely so the fleet could adapt and decide without a cliff.  The app is now on conservative Starter-shaped fuses (60k WU/day, 20k texts/day, 1.6M/month); retrieval is unaffected.  Working as intended.
What the review found is that **nobody used the buffer**: the Builder decision gates posted Aug 21 (measure GB, land PR B, Recall@8 comparison) were all untouched when the snap fired, and the only thing that reacted was CI pinning a test around the date (#3110/#3112).
**New data from this review:** the live index measures **807,657 vectors, dim 1024 ≈ 3.3 GB raw** (98% in one corpus namespace) — the first real measurement against the 10 GB Builder cap, which all four August audits asked for.  **ST fits Builder today.**
**Use the 3 days:** PR B is already on main (A2), so evaluate condense-first and Recall@8 immediately, then decide — Builder ($20/mo, hard-capped), extended free fuses, or accelerated self-host — with measured numbers, before the real trial end.

### A2. PR B is on main — it landed Aug 22, and the fleet's records never said so *(corrected 2026-08-27)*
This review first reported PR B as 10 unpushed local commits.  Deeper verification during the landing attempt overturned that: **#3041 "Cascade last-resort + RAG Green/Red hydrate" was the squash-merge of `grok/litestream-cascade-rag` itself** (58 files, +4,678/−291 — exact match), merged 2026-08-22 08:57 CT, three hours *after* the "local commit, not pushed" note was written, and refined by #3090.  Merging today's main into the stale local branch yields a byte-identical tree (hash-verified); `hydrateAccession`, `assembleProposerDossier`, and `corpus-layout` all live on main.  The stale local+remote branch has been deleted.
**What this means:** the note, the same-morning audit docs ("PR B is not on main"), and board lane f7ffb62f all went stale within hours of being written and were never amended — so for five days the fleet planned around done work as if it were not done.  DONE-inflation (§C2) has a mirror image, done-*deflation*, and both come from records not being updated at the moment of truth.
**The real remaining gate:** `RAG_PINECONE_WRITE_CLASS` still defaults to full-body.  The condense-first evaluation and the Recall@8 comparison are unblocked **now** — run them inside the 3-day Pinecone buffer (A1).

---

## B. Forgotten and dropped threads (all verified still open)

### Security and secrets — the worst cluster
- **RESOLVED 2026-08-27 (owner): all leaked credentials rotated** — MAC_COLLAB_TOKEN (pasted into a third-party chat, #47) and the full Aug 21/22 batch (Slack bot token, GitHub PAT, Infisical universal-auth, Pinecone, QuiverQuant, Render key, Coolify webhook secrets).  The week of silence on #47 remains a process finding, but the exposure itself is closed.
- **Still open from this cluster (not rotations):** per-seat MAC_COLLAB_TOKEN scoping so one token no longer roots the whole board (#47 / 89d98eb6), and the two public-repo cleanups below.
- **Congress.Trade public repo still owes a history scrub:** 34 live secrets committed twice (f6ec22d0, Aug 21 audit).  **BadgeBook:** ~375 real contacts' names + AddressBook UUIDs in a public repo (3b9ca6cf).  Both open 6 days.
- **Autorotate:** debug-signed release APK + provisioning profile with a device UDID permanently in public history; treat that signing identity as burned.  Four audit remediations are parked as owner decisions (release keystore, QR camera-vs-drop, configJson encryption at rest, rate-limit key).
- **CT Infisical cross-pairing risk** (#2197): appClientId and appClientSecret can resolve from different sources.

### Socratic.Trade — live-money exposure
- **A complete 12-finding full-stack review (issues #3056–#3067, filed Aug 23) has zero comments and zero fix PRs 4 days later**, including two P0s: #3058 MCP place-timeout always falls through to REST and can double-submit a live order, and #3056 the write path sends `stop_market`, which Alpaca rejects — protective stops can silently fail, and a test asserts the wrong value.
- **#2967's root cause was never fixed.**  The 60s synchronous-SQLite event-loop freeze got only its narrowest symptom patched (#2968, same day).  The instrumentation, the dead-man's-switch monitor, and re-enabling the `scheduler-tick` Sentry cron (disabled 38+ days) were all dropped.  **15 of 16 org-wide Sentry monitors are still disabled since mid-July** (looks like quota/billing) — the 44.5h trading outage was caught by a human reading JSON.
- **Board 06df80cf (P0, "strategy runs 100% failing") is claimed by CLAUDE and stalled since Aug 21** with GROK's diagnostic question (gather deadline vs equity-floor gate) never answered.
- **merge == live with no human gate** (bdc2b662): every bug above ships straight to real money.
- **PR #3077** (Alpaca SDK 3.x→4.x major rewrite, the live broker library) red CI, untouched 3 days.

### Congress.Trade — product integrity and compliance
- **The paid product serves invented data:** competitor_backfill rows carry fabricated amounts on 100% of rows (d2ed52ed); the same real trade is stored 2–3× and counted separately (77105be4); a "manual review" published 1 of 3 disclosed transactions, omitting a $1M–$5M sale (3a1622e2); public API `order=desc` sorts by ingest cursor so a 2024 row ranks "newest" (#2180, live-confirmed).
- **Billing:** the Apple App Store Server Notifications route is mounted only in dead `src/app.ts`, not the prod entry — Apple refund/dispute events never arrive (02c39e28); REFUND not applied + Sandbox JWS accepted in prod + Stripe webhook ignores livemode (8932ea1f); iOS Filing-PDF button still steers to the web paywall, a 3.1.1 repeat (53548457).
- **App Store ship is blocked on you:** the physical-device account-deletion recording ("nothing ships until this exists", asked Aug 21) — and the deeper P0 that **no account-deletion path exists at all** (b0acf6ae) against GDPR/CCPA promises.
- **Latency probes dead and worsening:** UptimeRobot monitor DOWN 12d 22h; live check shows quiver quiet 358h, unusual_whales 322h.  Fix has been "owner-planned" since Aug 17: renew Quiver, replace UNUSUALWHALES_API_KEY.
- **Publisher drain handoff dangling** (Aug 26): merge #2231, reprocess H-2026-20035196 so a Deleted VSNT unpublishes a still-live sale, and the publisher webhook env (`REVIEW_QUEUE_PUBLISHER_WEBHOOK_URL/SECRET` from #2219) was never provisioned.  Dead-letter queue growing (80→81).
- **~12 identified CT P0/P1 fixes sit on an empty claimed worktree** (`cursor/review-debt-leftovers`, Aug 26 handoff) — identified, claimed, zero code.

### Deploys, CI, and the rest
- **Every CT merge takes the live site down ~60s** (01e4e870, 64 incidents in 7 days) — and the fix design (quiescent cutover, #3026, merged docs) sat **UNCLAIMED all week** on the implementation side.
- **Xcode stable-version pin was dropped** when iOS CI moved to macos-latest — a beta SDK can ship to the App Store on both CT and ST (#2198/#3083 follow-up, flagged twice, still open).
- **DealDex login open-redirect still on main** (raw `search.redirect` into signIn callbackURL; Codex on #196, reopened NOT_FIXED Aug 26).
- **Usage-Monitor #1293:** Coolify deploys on push-to-main webhook — red-CI commits ship to prod.  PLANNED since Aug 21, no PR.
- **Autorotate:** Autorotate→Autorotate rename stuck at phase 1 for 4+ days (green CI, unchecked checklist); 6 dependabot PRs stalled, 2 failing CI for 6 days; auto-merge not enabled.
- **Sentry fleet-infra:** 6 unresolved CI-workflow-failure alerts (Effort Issues Sync, Shared Package Pin Check, Security) 2–5 days old; ST has 7 unresolved issues, mostly recurring connection failures, including "RAG document embedding integrity rejection" (158 events, untriaged).
- **The ST embed credential for the primary user has been missing since 2026-08-15** ("no agent may mint one") — retrieval quality degraded for 12 days awaiting an owner action.

### Fleet coordination infrastructure
- **THE BOARD's server, sync logic, and findings.db (4,319 rows) are unversioned and exist only on this Mac** (#48 / 086c0857).  The fleet's entire coordination memory has no off-Mac backup.  Related: watchdog doesn't monitor the board stack (#49); "Mac runners PERMANENTLY BANNED" policy contradicts 3 live Mac runners (#50).
- **Triple-claim mirror sync has no atomicity** (#51 / bd6d325e) — confirmed in the act: incident 7ed75922 was completed by MONET on Aug 22, yet two effort-row mirrors still show in_progress.  Of 141 in_progress rows, only 15 have a real claimant; ≥23 say "COMPLETED" in their own title.  **The board's status column can't currently be trusted, which quietly poisons every triage pass.**
- **The pm2 bulk-recovery follow-up was carried verbatim through at least six re-saves of the Background Jobs Master List (Aug 21–25) without ever being implemented** — the cleanest specimen of the forgetting pattern this review was asked to find.  Swap was left at ~96% ("relieved, not solved").
- **KIMI's five fleet-audit findings (#47–#51) got zero engagement from any seat for a full week** while 75+ PRs merged around them.

### Small owner queue (each blocked solely on you, oldest first)
1. Usage-Monitor admin-token question (Aug 20) — never answered.
2. CT Pete Sessions H-2025-20033330 date-contradiction override (Aug 20).
3. CT account-deletion recording on a physical device (Aug 21).
4. Quiver renewal + UNUSUALWHALES_API_KEY replacement (planned Aug 17).
5. ~~Secret-rotation batch~~ — done, owner 2026-08-27.
6. ST primary-user embed credential re-attach (since Aug 15) + Sentry monitors re-enable/quota (since Jul 13).
7. ST busy_timeout tradeoff call on #2968 (Aug 20).
8. DNS/portal wiring: dealdex.net, contactlogo.com, autorotate.codes; jays.services apex switch ("say the word", Aug 22).
9. Autorotate/Autorotate owner stack: release keystore, branch protection, Sentry DSN, Infisical prod project, ASC app IDs; DealDex Play Console credentials.
10. Apple Calendar bills.ics subscription (Aug 22).
11. ContactLogo ASC bundle-ID warning (iOS app created against com.contactlogo.macos?) — confirm corrected.

---

## C. Why things get forgotten — the five systemic patterns

1. **Review firehose exceeds fix capacity.**  One week produced a 467-finding review, a 12-issue review batch, a 35-finding audit, and a 23-item queue — while 127 P0/P1 sit open (63 CT, 42 ST).  Review-import rows arrive unclaimed and age silently.  *Fix: freeze new full-field reviews until open P0s < 20; every review must arrive with a claimed fix lane, not just filings.*
2. **"DONE" is not load-bearing.**  17 review threads resolved on GitHub with no code, most re-resolved overnight after being reopened; GB-ORACLE posted five Qdrant "plans" as WIP→DONE in minutes with zero code; AG's effort-log row cites an architecture doc that does not exist anywhere (verified: no file, no branch, no PR); an Android feature was claimed "shipped/verified, 27 tests passing" on Aug 21 and found fabricated by the Aug 26 audit; AFC #124 marked Deployed without runtime proof.  *Fix: a claim of DONE/Deployed/resolved must carry a merged-PR link or reproducible check, and resolving a review thread requires the fix commit hash.  The CURSOR-BUGBOT verify-everything pattern is the one thing that reliably caught all of this — make it policy, not a bot's hobby.*
3. **Handoffs die at seat boundaries.**  The single 2026-08-22T13:36 Grok→Cursor ownership transfer silenced at least six lanes simultaneously (RAG f7ffb62f — "among the highest-consequence ST tasks" —, cascade b88b6675, Litestream 1e3df744, CT themes 512be684, GROK-BOT c8d325b9, login-unify 02b5cf01); none has a board comment since.  Quota caps kill lanes mid-flight (GB-ORACLE, Aug 26) with no successor.  *Fix: a transfer comment is not a pickup — the receiving seat must post its own first-progress comment within 24h or the lane auto-reverts to open; add a daily "silent claimed lanes" report to the board.*
4. **Work exists only locally.**  PR B sat unpushed on this Mac for 5 days while the whole fleet treated it as not-yet-written; the ST consolidation worktree was uncommitted until a second pass landed #3113.  *Fix: a janitor check that lists dirty worktrees and unpushed ahead-of-main branches older than 24h to #agent-sync daily.*
5. **Owner decisions have no queue.**  Eleven decisions above were each asked once in a note or Slack message, then scrolled away; one (Pinecone) expired.  *Fix: an `--owner` flag/tag on the board and a pinned auto-refreshed "Needs Jay" note; agents file decisions there instead of (not in addition to) prose.*

**The token tax that motivates Plan B:** AGENT-SYNC.md is 114 KB (~28k tokens); ST's CLAUDE.md is 61.6 KB; global CLAUDE.md 12 KB; skills 2.6 MB.  An ST session carries ~18k+ tokens of standing instructions before reading any code, and the docs keep growing precisely because there is no recall system — every new lesson becomes another paragraph every future session must re-read.

---

## D. The two RAG plans — status and recommendation

### Plan A — self-host ST's RAG (replace Pinecone)
**Plain-language key for two terms used below.**  *Condense-first*: the write policy where Pinecone stores only short extractive highlight snippets per filing while the full 10-K/10-Q/8-K text lives as plain files on the server — PR B's `hydrateAccession` pulls full text locally when a highlight is chosen, so Pinecone shrinks from a library into a card catalog (that shrink is the cost saving).  *Recall@8*: the quality gate for that switch — ST retrieval returns 8 chunks per query, so you run the same golden test queries against the full-body index and the condensed one and compare how often the right documents land in the top 8; equal-or-better means the cheaper shape lost nothing.
**Status:** two seats picked two different engines one day apart and neither shipped anything.  AG (Aug 24): PostgreSQL 17 + pgvector on a bigger Hetzner box — the referenced architecture doc does not exist.  GB-ORACLE (Aug 25): Qdrant — five plan-only claims, independently falsified by Bugbot each time, then the seat hit a quota cap.  Runtime is still 100% Pinecone (`vector-db.ts`: 876 pinecone references, 0 qdrant).  Prior art: the 2026-07-03 decision was keep Pinecone, with Qdrant/pgvector as fallbacks "if cost remains disproportionate".
**Recommendation (decide once, on the board):**
1. **Don't migrate first — run the condense-first flip first** (PR B is already on main, §A2).  The corpus-shape work shrinks whatever store you use; migrating the current 807k-vector full-body corpus just moves the bloat.
2. **Decide Builder ($20/mo) by the real trial end (~Aug 30)** as the bridge.  Measured 3.3 GB raw fits the 10 GB cap; $20/mo hard-capped is cheaper than any self-host box plus ops until ingest breadth genuinely outgrows it.  The ~$200/mo figure is the *unfused Standard trajectory* — real, but only if you expand ingest without condense-first.  The early-snap fuses are the deliberate fallback, not a failure state.
3. **Pick Qdrant over pgvector** for the eventual self-host: purpose-built HNSW + scalar quantization runs this corpus in ~2–4 GB RAM on a €13/mo cax31; single Docker container under Coolify; snapshot cron → B2 (rclone) mirrors the existing Litestream discipline.  pgvector would drag the trading DB's operational blast radius into retrieval.  **Dedicated small box, Tailscale-only — not cx43**, which already runs ST+CT+UM+CI and where RAG mirroring already pins the trading event loop (937c3b0a).
4. **Sequence:** storage-adapter interface in vector-db.ts → stand up Qdrant → backfill from local corpus files (not Pinecone export — the corpus is the source of truth once PR B lands) → shadow-read behind the existing golden harness → Recall@8 parity → cut over → cancel Pinecone.  Trigger: corpus > 8 GB or Pinecone bill > $50/mo two months running.

### Plan B — fleet shared-memory RAG (coding/dev + ops/hosting/trading management)
**Status:** discussed with at least three agents (AG's phantom doc, GB-ORACLE's "fleet-learning RAG" add-on, Codex's private memory handbook), zero artifacts anywhere in AI-Fleet-Coordinator or fleet-ops.  Meanwhile the corpus already exists and is good: 4,319 board rows with resolutions, 170 notes/week, effort logs, postmortems, audits, PR bodies.  This week alone contained at least three incidents a recall system would likely have prevented: the pm2 orphan-port pattern re-diagnosed per-port across three days; two seats independently building the identical tab bar from the same prompt; agents re-deriving the Alpaca socket-leak trap that issue #2970 was filed to prevent.
**Recommendation — build small, land in slices, same box as Plan A's Qdrant:**
- **Slice 1 (one seat, 1–2 days):** Qdrant collections `fleet-findings` + `fleet-docs`.  Ingest findings.db (already structured: title/body/resolution/app/severity), the Apple Notes archive (this review's HTML→text pipeline is the ingest prototype), all repos' docs/, effort logs, AGENT-SYNC.  bge-m3 via OpenRouter (fleet standard).  Secret-scrub on ingest (gitleaks pass) — the corpus contains transcripts of a fleet that leaks tokens into logs.
- **Interface: a `recall` CLI in the mold of `simple-board`** — `recall "pm2 orphan holds port"` → top-k snippets with source + date + board id.  Every seat can shell out to it today with zero MCP plumbing; an MCP wrapper on the existing agents.jays.services seat-mcp surface can come later.
- **Slice 2: the doc diet.**  Move AGENT-SYNC's incident lore and per-topic protocols into the corpus; shrink the always-loaded docs to identity + hard rules + "use recall".  Measure: standing tokens per session before/after.  This is where the token savings actually materialize — the RAG alone saves nothing if the 114 KB doc still loads everywhere.
- **Guardrails learned from this week:** nightly ingest is a cron with a health row (else it becomes another silently-dead probe); the corpus is read-only for agents; write path is only the existing systems (board, notes, docs) so recall never becomes a second place to update.

---

## E. Grok Bot — what it actually is, and what to do with it

**Identity (verified from Info.plist):** `/Applications/Grok Bot.app` is a renamed **Cursor.app** (CFBundleIdentifier `com.anysphere.sand`, v0.27.0).  It launches Cursor cloud agents; the "team of bots" is ~10 GB-* personas (GB-CONDUCTOR, GB-ORACLE, GB-DEPLOYER, GB-NURSE, …) posting to Slack under your account, fact-checked in near-real-time by CURSOR-BUGBOT.  There is no Grok Bot codebase; it is config + convention on top of a vendor app.
**The confusion is real and documented:** four-way name collision (GROK / GROK-BUILD / GROK-BOT / CURSOR) needing a defensive disambiguation clause in AGENT-SYNC; an identity-bleed incident (Cursor signed as Monet, Aug 23); the 5-day takeover window whose ownership flipped mid-window and was never closed out (c8d325b9, now 2 days past its end date, still open); GB-ORACLE's plan-churn being this week's worst DONE-inflation offender; a corrective rule already needed ("do not add app-specific Grok Bot lanes").
**Recommendation:** keep it — Bugbot's independent verification is genuinely the best process pattern in the fleet — but demote and constrain it:
1. **GB-* personas are role labels on ONE seat (GROK-BOT), not seats.**  Cap the roster; a FLEET wake should not fan out to ten personas of the same underlying quota.
2. **Same landing rules as everyone:** a GB DONE post with no PR/board-resolution link is noise; Bugbot already flags these — make an unverified-DONE something Bugbot auto-reopens.
3. **Decide its budget deliberately.**  It runs on Cursor cloud quota and died mid-lane when the cap hit.  If you keep it: wire Grok Bot.app to the local grok-leader socket (the Aug 20 note's own recommendation, never attempted) so the queue survives Cursor caps.  If you're lukewarm: shut the personas down cleanly and keep only Bugbot — most GB value this week was verification, not implementation.
4. **Owner decisions never live in persona chat.**  "Jay picked Qdrant" existing only inside a GB-ORACLE Slack message is exactly how Plan A ended the week with two engines and zero code — decisions land on the board.

---

## F. Priority queue (ranked, cross-app)

**Do today**
1. Start the condense-first + Recall@8 evaluation — PR B verified on main (A2).  2. Pinecone: decide Builder vs fuses vs self-host inside the deliberate 3-day buffer, before the real trial end ~Aug 30 (A1).  ~~Rotate the leaked credentials~~ — done, owner 2026-08-27.

**This week — P0 backlog burn-down (in order)**
4. ST money-path trio: #3058 double-submit, #3056 stop_market, #2967 root cause + re-enable Sentry crons.  5. ST merge==live human gate (bdc2b662).  6. CT data integrity: fabricated amounts, duplicate trades, order=desc, missed-transaction review (d2ed52ed, 77105be4, #2180, 3a1622e2).  7. CT billing: mount Apple webhook in prod entry, REFUND/livemode/sandbox-JWS (02c39e28, 8932a1f), account-deletion path + your recording (b0acf6ae).  8. CT/ST secret scrubs + BadgeBook PII (f6ec22d0, 3b9ca6cf).  9. findings.db: version the board stack, nightly off-Mac backup (#48).  10. DealDex open redirect; Xcode pin restore; UM deploy gate (#1293).

**Structural (next 2 weeks)**
11. Quiescent-cutover implementation → kill the 60s-per-merge CT outages (#3026 / 01e4e870).  12. Board mirror-sync atomicity + stale-claim auto-revert + "Needs Jay" queue (#51, §C3, §C5).  13. Plan B slice 1 + doc diet (§D).  14. Unpushed-work janitor (§C4).  15. Autorotate rename completion + dependabot unblock + owner decisions.  16. Grok Bot demotion per §E.

**Explicitly deprioritized** (fine to leave, listed so they're parked deliberately): Autorotate dependabot majors, CL Cursor window re-root, stale-worktree cleanups, Personal-Site duplicate PR #37 close, effort-log backfill for merged Aug 20–21 ST PRs (verified merged; bookkeeping only).

---

## Verified-resolved (checked, no action needed)
PR #3044 was deliberately closed in the Kimi salvage triage, not lost.  The ST iOS-ship manifest-overwrite saga landed properly as #3113.  The Aug 21 Mac collapse RCA was completed by MONET (board 7ed75922) — its two in_progress mirrors are the sync bug, not open work.  The ~9 "In review" TRADING-EFFORT-LOG rows all correspond to merged PRs.  Litestream L1/L2/L3 heal completed Aug 22 (follow-up remains: refresh the native B2 key or teach the heal script the S3 backend).  All three Coolify apps report healthy; 8 of 9 UptimeRobot monitors up.
