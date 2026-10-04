# Open-session handoff — 2026-08-21

**Author:** MONET (Mac) · **Scope:** every unarchived Claude Code session on the Mac as of
2026-08-21 ~16:30 CT · **Purpose:** any fleet agent can pick up any lane below without
re-reading the original transcript.

Ten sessions are open.  Their transcripts are hours to days stale, so **every claim below
was re-verified against live state at handoff time** — where live state contradicts what a
session believed, the live number is the one written here and the drift is called out.

Read § Cross-cutting first: three of the ten lanes are the same Mac-health problem seen
from different angles, and two lanes are blocked on the same owner action.

---

## Cross-cutting state — verified 2026-08-21 16:30 CT

| Signal | Live value | Was (session belief) |
|---|---|---|
| Swap | **29.87 GB used of 30.72 GB — 849 MB free** | 34.0/34.8 GB at 08:33 |
| Data volume | 84 GB free, 80% used | 104 GB free at 16:22 |
| pm2 online | **11 of 14** | 14 of 14 at 09:17 |
| `xcode-health` | **errored, 2794 restarts** | errored, 2678 at 08:33 |
| `grok-acp` | **errored, 2069 restarts** | online, 1726 at 08:33 |
| `grok-leader` | errored, 506 restarts | errored, 390 at 08:33 |
| `senate-relay` | online, 705 restarts | errored, 550 at 08:33 |
| `:8791` holder | orphan Python pid **65116** (not pm2) | same orphan, 08:33 |
| `:8899` holder | orphan deno pid **76761** (not pm2) | same orphan, 08:33 |

Open PRs, all repos: **Socratic.Trade has 6; Congress.Trade, Usage-Monitor and
AI-Fleet-Coordinator have zero.**  Session metadata that still shows "PR 61 OPEN" or
"PR 3004 open" is stale — both are closed out.

### The single most valuable mechanism to carry forward

**When pm2's God daemon dies without reaping, its children reparent to launchd and keep
their listening socket.**  The replacement process can then never bind, so pm2 restart-loops
it forever, and nothing inside that loop can clear it.  Worse, the orphan keeps serving —
so every port probe and `/health` check reports green while pm2's managed copy is dead.
This has now been the root cause **four separate times**: `mac-collab` (2026-08-20, fixed
with `_bind_or_reclaim()`), `senate-relay`, `xcode-health`, and the board outage.
`xcode-health` has burned 2,794 restarts against an orphan that answers 200.

Diagnose it with `lsof -nP -iTCP:<port> -sTCP:LISTEN` and check whether the holding pid is
pm2-managed — **not** with a health probe, which will lie to you.

---

## Lane 1 — Congress.Trade App Store submission ⚠️ OWNER-BLOCKED, highest value

**Session:** `local_04d2e609-1a92-4543-bebe-c1176dc1a999` · cwd `~/Code/Congress.Trade` ·
last activity 12:56 CT

Everything code-side is merged; CT has zero open PRs.  A TestFlight build was being produced
from `fa48d842` (then-current `main`) carrying the account-linked entitlement model, sepia
removal, icon and text colours, the web-accessibility fixes, and the iOS test-race fix.

**Blocked on the owner, and nothing ships until it happens:** a physical-device recording of
the account-deletion flow.  Shot list is Section 4 of
`CHECKLIST_FOR_ASC_PUBLIC_RELEASE.md` (present in the CT repo root).

Owner steps, in order:

1. Install the newest TestFlight build — **not** `202608202100`, which predates today's fixes.
2. Record the deletion flow, under two minutes, unedited: home screen → launch → ≡ →
   Sign In → back to ≡ → scroll until Delete Account is visible → tap → let the confirmation
   sit a beat → confirm → show the signed-out state → reopen ≡ to prove it.
3. Optional but recommended — record the no-account purchase flow: launch signed out → ≡ →
   Premium → show both plans tappable with no sign-in wall.  Do not complete a purchase.
   This pre-empts the 5.1.1 argument rather than merely answering it.
4. Check trial length and prices in ASC match the paywall copy — a mismatch there is its own
   rejection.

**Agent steps once the files arrive:** add the recording line to the review notes, cancel the
stale submission `b61e2a4a`, create the submission with **both** subscriptions attached,
submit.

**Agent work still outstanding regardless of the owner:** confirm the build from `fa48d842`
actually reached TestFlight, then update `CHECKLIST_FOR_ASC_PUBLIC_RELEASE.md` in a single
pass — correct build number, the fixes now merged, and the remaining steps — so the document
the owner records against names the right build.

Expect this and don't file it as a bug: the sandbox purchase is probably already claimed by
whichever account first signed in, so on the new build other accounts correctly show *free*.
That is the entitlement fix working.  Clear it under Settings → Developer → Sandbox Apple
Account for a clean slate.

> **Trap already paid for:** that session misdiagnosed a merge block as CI flake and pushed
> three empty commits at it.  The real cause was an unresolved-review-threads **ruleset**
> rule.  Check the rulesets endpoint before concluding "flake".

---

## Lane 2 — Socratic.Trade sign-in button unification ⚠️ STOPPED MID-WORK

**Session:** `local_7a60bb21-441f-4006-bb07-2e5a97610c4a` · last activity 14:29 CT ·
successor PR **[#3008](https://github.com/Simple-With-Us/Socratic-Trade/pull/3008)**
(`claude/login-wordmark-catalyst`, OPEN, **BLOCKED**)

This is the most at-risk lane — the session ends on a bare tool call with no closing summary,
so its findings exist nowhere else.  PR #3004 (`claude/ipad-layout-login`) was **closed**
at 10:23 CT; the work now lives in #3008.

**The owner's ask:** the sides of the non-Apple sign-in boxes look strange in ways branding
and policy don't require.  Research how other sites (Infisical was named) handle multiple
login types, and make Apple / Google / GitHub look like one family while staying branded.

**Already diagnosed and fixed — do not re-investigate:** the grey outer box was **system
chrome**, not custom styling.  There was no `.buttonStyle(.plain)`, so SwiftUI's default
bordered style drew its own background *behind* the custom one.  The Apple button escaped it
because it is UIKit-hosted, not a SwiftUI `Button`.  That fix was applied and visually
confirmed — grey boxes gone.

**Already researched, primary-source verified — do not re-research:**

- **The current teal Google button is an outright compliance violation.**  Google explicitly
  forbids the colour "G" mark on a coloured background.
- **Apple explicitly permits a custom button** for this exact situation — their guidance says
  you may want to align logos across multiple sign-in buttons.  So a unified treatment is
  sanctioned, not a rules-bend.

**What remains:** with the grey chrome gone, the real problem is visible — teal, white and
black are three different treatments.  Apply the unified-but-branded design across the three
providers.  The session stopped while fetching official GitHub artwork; get that from
GitHub's own brand assets rather than guessing a path.  Then unblock and land #3008.

---

## Lane 3 — Mac fleet health: two orphans awaiting a yes ⚠️ OWNER-BLOCKED

**Session:** `local_84ffc5a0-4e5b-43a1-a866-3d5815819202` · cwd `~/Code/AI-Fleet-Coordinator` ·
last activity 08:33 CT

The session ended on a direct question to the owner — *"Want me to do #1 now?"* — that was
never answered.  **The condition has since worsened**, so re-ask and act:

1. **Kill the two orphans** holding `:8791` (Python pid 65116) and `:8899` (deno pid 76761)
   so pm2 can bind and take ownership.  Cheap, brief blip on those ports, immediately fixes
   the restart storm.  `xcode-health` has added ~116 restarts since the offer was made.
2. **Reclaim memory** — needs the owner's call on what closes.  Biggest non-editor wins were
   `studio` (2.3 GB) and two WebKit processes (2.5 GB combined).

**Correction the session made about its own earlier claim, worth keeping:** 338 node
processes total only ~0.5 GB (≈2 MB each).  Agent-session sprawl is a **process-count**
problem, not the memory problem.  The RAM is in GUI apps.  Don't kill agent sessions to
free memory.

**Landed already:** fleet #61 (recovery lessons in `MAC-LOCAL-PROCESSES.md`), #62 (agent
worktrees belong in `~/apps/*`, never `~/Code/*`), #64 (effort-log row + the missing
`## Completed` section).  All merged — session metadata showing #61 OPEN is stale.

**Shellular is fixed; keep the mechanism.**  Root cause was neither a duplicate install nor
launchd.  **pm2 replays the env cached at an app's first start** — that cached PATH had no
`/usr/sbin`, so it crash-looped on `ioreg: command not found`, while
`pm2-ecosystem.config.cjs` had it right the whole time.  Every plain `pm2 start` faithfully
reproduced the crash and editing the config would have changed nothing.  The fix:

```bash
pm2 restart shellular --update-env
```

Live now: online, 4 restarts.

**Still open, needs verification:** `mac-process-watch.sh` runs `pm2 resurrect` only on the
3+-missing path and never falls through to an ecosystem start, so a poisoned dump cannot
self-heal.  Fleet **#58** ("do not resurrect a poisoned dump.pm2") has since merged —
confirm whether it actually closed this before re-filing.

**Do not touch:** `scripts/mac-process-watch.sh` holds another lane's in-flight uncommitted
work (jlist timeout, stray-CLI kill, shellular relay liveness) on branch
`deepseek/seat-onboard`.  Two separate sessions deliberately left it alone.

> **Correction on the record:** `code-main-keeper` did **not** clobber anyone's worktree.  It
> does exactly what it documents — returns top-level `~/Code/*` checkouts to `main` — and it
> never discards commits absent from `origin/main`.  The lesson is worktree *placement*,
> which is what #62 records.

---

## Lane 4 — Socratic.Trade strategy runs: two live diagnoses that disagree

**Session:** `local_95a951ee-89ec-4403-b911-392bbd3ca370` · cwd `~/Code/Socratic.Trade` ·
last activity 16:28 CT · all 11 of its PRs merged

Three owner tasks were answered; two need hands this session did not have.

**a) Why `strategy_runs` never moves — answered from container logs:**

```
[scheduler] Skipping account 4dba67b5-…: Account equity (0) is too low to trade
[scheduler] Skipping account 717a4e52-…: Account equity (0) is too low to trade
```

Both accounts are skipped at an **equity-floor gate reading 0**.  That is a *pre-run* gate
which writes **no `strategy_runs` row at all** — which is exactly why the completed-run
counter never moved and nothing ever reported as failed.  Not the budget ceiling, not broker
health.  The remaining question is narrow: **is equity genuinely 0, or is the broker read
returning 0?**

⚠️ **Reconcile before acting.**  Board item `06df80cf` (P0, in_progress, @CLAUDE) describes
the same symptom with a *different* cause — "gather has no internal time budget, 8-min
deadline kills the run" — and ST **#3018** (`cursor/gather-timeout-abort-0388`, open) is a
fix for that second theory.  Two agents are working the same P0 from incompatible diagnoses.
Someone should establish which gate actually fires first rather than landing both.

**b) Sentry monitors — owner-only, and bigger than ST.**  15 of 16 monitors across the whole
org are disabled, all with final check-ins on 12–13 July; only `watcher-cron` survived.  That
points at Sentry quota or billing, not an ST setting.  The Sentry MCP exposes no
monitor-update tool, so this needs the owner in the Sentry UI.

**c) Embed credential — correctly declined, still outstanding.**  It requires the key's
value, which no agent should enter.  The owner pastes it at `/console/connections#api-keys`;
an agent can verify it resolved afterward without ever seeing it.

**d) Production was serving stale code since 12:07 AM CT** — found by accident, nothing
alerted.  Every build failed on a top-level `await` that only breaks inside the image,
because `package.json` is copied one line too late for `tsx` to read `"type": "module"`.
Fixed in ST **#3010**, and verified by the failure *changing*: exit 1 `TransformError` →
exit 2, the RTH latch's deliberate block.

**Follow-up someone must actually do:** after 3:00 PM CT the evening drain retries.  Confirm
the live sha advances.  If a build still fails, **exit 2 is the latch (correct, not a fault)
and exit 1 is a real error.**

---

## Lane 5 — Mac disk: second sweep pass never ran

**Session:** `local_78bc1820-11d5-4293-bd64-caab6b2c7137` · cwd `~/Code/Usage-Monitor` ·
last activity 16:22 CT

The owner's question was answered: the command is **`ms`** → `bash ~/apps/mac-status.sh`,
defined at `.zshrc:29`.  It prints pm2, launchd-not-in-pm2, brew services, listeners, and the
last 8 down-watch lines.

**Unfinished:**

- **The second sweep pass never ran** — DerivedData, DeviceSupport, brew and npm caches.
  The first pass completed: 42.4 GB from `~/apps` `.next/cache` plus 1.5 GB from `~/Code`.
- **Skip `simctl`.**  `simctl delete all` hung ~20 minutes and wedged CoreSimulator under 262
  concurrent builds; the diagnostic hung too.  Clear `XCTestDevices` **by path** instead.
- **Discoverability gap:** `MAC-LOCAL-PROCESSES.md:209` records the command of record as
  `bash ~/apps/mac-status.sh` but never mentions the `ms` alias, which lives only in
  `.zshrc` — which is why it was hard to find.  Add the alias to that line.

Live disk now: **84 GB free, 80% used** — down from the 104 GB the session last reported, so
the pressure is returning.

---

## Lane 6 — Fleet docs: unlanded work + a real drift problem

**Session:** `local_01bfeec0-2bcb-4714-8e2c-2020ebc96336` · cwd `~/Code/AI-Fleet-Coordinator` ·
last activity 16:25 CT

Landed: agent logo fixes (the Grok chip was showing the **xAI company mark** — `grok.svg` was
byte-identical to `XAI.svg`; Cursor was a 206-byte generic pointer triangle), plus a
`logo_file()` resolver because the marks are now a mix of SVG and PNG while the fleet daily
digest hardcoded `agent-logos/<slug>.svg`.  The portable sentence-gap skill landed on `main`
(`05988d3`).

**Three things left open:**

1. **The disk-forensics workflow died with a session restart** and never reported, so the
   "where did 10+ GB go" question is still unanswered.  It can be relaunched with
   `resumeFromRunId`.  What *was* established: the volume recovered on its own (91% → 85%,
   38 GB → 61 GB free), and a `com.apple.os.update-MSUPrepareUpdate` snapshot plus a
   `ProductMetadata.plist` touched at 03:42 are consistent with a macOS update download and
   prepare that expanded then released — **a hypothesis, not a conclusion.**  The one
   confirmed *standing* item is the **22 GB CoreSimulator runtime volume (iOS 23F77)**,
   which sat at 97.56% full all session.
2. **`AGENT-SYNC.md` has genuinely diverged in both directions** — `/Users/jay/apps/AGENT-SYNC.md`
   has Autorotate content the repo copy lacks; the repo copy has the THE BOARD section the live
   copy lacks.  The session added the cross-link to each independently but deliberately did
   **not** pick a winner.  This needs someone to reconcile it on purpose.
3. **Uncommitted work is sitting in the `AI-Fleet-Coordinator` worktree right now** on branch
   `deepseek/seat-onboard`: modified `AGENT-SYNC.md`, `TEMPLATE-AGENTS.md`,
   `docs/fleet-skills/README-add-in-app.md`, `docs/fleet-skills/owner-copy/SKILL.md`, plus
   untracked `docs/fleet-skills/sentence-gap/`, `docs/fleet-skills/sentence-gap.zip`,
   `.claude/skills/sentence-gap/`, `.grok/skills/sentence-gap/` and `.cursor/`.  Per standing
   policy, unlanded finished work is invisible to peers and gets re-done — someone should
   land or explicitly park it.

---

## Lane 7 — Socratic.Trade login: one small verification gap

**Session:** `local_27c4566e-af2d-4e4e-b0fd-84b3c2a2d712` · last activity 09:17 CT

ST **#3002** ("hero wordmark, equal-size provider buttons, expanded legal copy") **merged at
16:13 CT** — that thread is closed.

**The one honest gap the session flagged about its own work:** the **iOS screen is
screenshot-verified; the website login is not.**  It is compile- and test-verified only —
the agent declined to add a dev server while the box was thrashing.  Load has since
recovered, so capture it.  Small, concrete, unfinished.

Gate at the time was fully green: lint 0 errors, `tsc` clean, 664 files / 7,400 tests passed,
build exit 0, and `xcodebuild` BUILD SUCCEEDED — mandatory here, because AGENTS.md is explicit
that a merge touching `ios/**` is the one case where a green JS gate proves nothing.

---

## Lane 8 — Usage-Monitor: an unanswered question and six tangled PRs

**Session:** `local_946986c8-97bd-425c-b07d-b24a322e2dc2` · last activity 2026-08-20 03:30 CT

**Ended blocked on a question the owner never answered.**  The owner asked for "the usage
admin token for Usage Monitor, since the one I have isn't working."  There is **no literal
`USAGE_ADMIN_TOKEN`** in that app, and several "admin"-named candidates exist in the handoff
store — the session put an `AskUserQuestion` up to disambiguate and stopped there.  Whoever
picks this up: get the owner to name the credential first; do not guess, and follow
`secret-safety` (names-only greps, never a whole-file read of `~/.secrets/global-api-keys`).

**Landed:** the `deepmerge-ts` advisory (GHSA-ggr8-5vv4-36mx, stack exhaustion via
`@prisma/config`).  `npm audit fix` could not clear it — every prisma release from
`6.13.0-dev.1` through `7.9.1` pins an exact vulnerable version, and prisma 8 is still an
`-rc` with a breaking CLI rewrite.  Fixed with a `"deepmerge-ts": "^8.0.0"` override in the
existing `overrides` block.  UM #1244 merged, then #1243.

**Left deliberately unresolved:** that advisory was blocking all 10 open UM PRs.  Four
merged; the remaining six passed `verify` but had **real merge conflicts among themselves on
`docs/EFFORT-LOG.md`** — unrelated docs PRs stepping on each other.  The session flagged it
to the fleet rather than arbitrating peers' in-flight docs.  **Usage-Monitor now shows zero
open PRs**, so verify whether that resolved itself before spending time on it.

---

## Lane 9 — Congress.Trade APNs: unstarted feature work, cleanly scoped

**Session:** `local_7d3e6653-7ff0-48b1-b2d2-fa6a5ab3ec06` · last activity 2026-08-12

CT **#1815** (`*.p8` gitignore) is **merged**, and the exposure is closed — verified at
handoff: `.gitignore:54` carries `*.p8` on `main`, and no loose `.p8` remains in the CT repo
root.

The audit answered all three questions and none of it needs redoing:

- **SIWA already works in production** (since 2026-08-10) and **never needed a `.p8` key.**
  It is the native-client flow: the app gets a signed identity token from Apple's on-device
  provider, and the backend only *verifies* that signature against Apple's **public** JWKS
  (`app/src/auth/appleIdentity.ts:157`, route `app/src/auth/routes.ts:293`).  No private key
  anywhere in that path.
- **DeviceCheck — skip it.**  Zero references in the codebase, no docs anticipating it, and
  the fraud pattern it targets is already reasonably covered by SIWA plus Stripe/StoreKit
  per-account trial dedup for a $5/mo product.
- **APNs is the real gap.**  Device registration is built and live on `main`
  (`PushNotificationManager.swift`, `AppDelegate.swift`, `app/src/client/pushDevices.ts`,
  `app/src/client/commands.ts:231`).  **The send path does not exist** — an APNs HTTP/2
  client that signs a JWT with the `.p8` and POSTs to `api.push.apple.com`, plus the trigger
  deciding when a trade or filing event fans out.  Both were explicitly scoped out of CT
  #1446 pending these credentials.

**Sequence if this gets picked up:** the key lands in Infisical as `APNS_KEY` / `APNS_KEY_ID`
/ `APNS_TEAM_ID` **first**, then the send path is built, gated behind Premium per #1446's
plan.  Nothing has started.

---

## Lane 10 — Congress.Trade billing alerts: closed, but two corrections must survive

**Session:** `local_dc925f77-bc1a-4e4b-a8b7-dd2d5ebe3df5` · last activity 14:27 CT

Complete and verified in the production database, not merely by an HTTP 200.  CT #2080,
#2086, #2082 and ST #2953 merged.  All eight Codex findings fixed, several real bugs — a
`sendPushover` call with no abort signal awaited from the Stripe webhook (a stalled socket
never *settles*, so Stripe retries an event it thinks timed out — now bounded at 5s in the
shared helper), an activation claim consumed *before* delivery (so any failure lost the alert
permanently), and `customer.subscription.updated` excluded entirely so card-confirmation
subscriptions became Premium with no alert ever produced.

**Nothing is outstanding here — but three corrections are worth more than the fixes:**

1. **The migrate runbook was wrong.**  `POST /api/admin/migrate` applies the statement list
   compiled into the **running container**, so it can only apply migrations already deployed.
   Migrating before the deploy lands returns a clean **200 and creates nothing.**  The order
   is **deploy → migrate → verify in the database.**  This fails silently, which is why it
   burned a cycle.
2. **CT and ST expose the deploy sha at different paths** — CT uses `build.sha`, ST uses
   `checks.release.sha`.  A watcher written for one reports a **false negative** on the
   other, which it did.
3. **An unresolved review thread is not a merge safeguard.**  The session held a thread open
   so auto-merge could not fire before CI finished; the autofix bot resolved it externally.
   No harm that time — CI was already green — but the safeguard was weaker than intended.

A trailing background command shows `stopped` with no completion record; check its output
file before assuming it finished.

---

## Suggested pick-up order

| Priority | Lane | Why |
|---|---|---|
| 1 | **3** — kill the two orphans | Degrading now; one owner "yes" fixes a 2,794-restart storm |
| 2 | **1** — CT App Store recording | Owner-blocked, blocks the entire release |
| 3 | **4** — reconcile the two strategy-run diagnoses | Two agents working one P0 from incompatible theories |
| 4 | **2** — ST sign-in buttons | Stopped mid-work; findings exist nowhere else; #3008 is BLOCKED |
| 5 | **5**, **6** | Disk pressure returning; unlanded work goes stale |
| 6 | **7**, **8**, **9** | Small, well-scoped, not urgent |

## Rules that apply to whoever picks these up

- Claim on THE BOARD before substantial work (`board list` → `board claim <id> --by <SEAT>
  --env Mac --where "..."`), land the `docs/EFFORT-LOG.md` row, post start and closeout to
  `#agent-sync`.
- Branch under your own seat prefix and work in `~/apps/*`, never a top-level `~/Code/*`
  checkout (fleet #62).  `code-main-keeper` returns those to `main`.
- Two spaces between sentences everywhere — two literal ASCII spaces in files like this one,
  the literal `&nbsp;` entity plus a space in chat replies.
- Don't trust a health probe to tell you a pm2 job is healthy.  See § Cross-cutting.
