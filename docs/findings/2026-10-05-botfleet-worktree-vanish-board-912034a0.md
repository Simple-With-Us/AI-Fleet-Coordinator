# Findings: registered BotFleet worktrees vanish under `~/apps` (board `912034a09956404c85ba1715d6b12290`)

**Status:** investigation (docs only — no production delete-behavior change in this PR)  
**Board:** `912034a09956404c85ba1715d6b12290` — [AFC][CLAUDE] Something deletes registered BotFleet worktrees under `~/apps` within minutes; not disk-janitor, not code-main-keeper  
**Claimed by:** GB-HOUSEKEEPER (this cloud run)  
**Scope:** AI-Fleet-Coordinator tracked scripts/docs, fleet recall corpus, and live Mac inventory described in `docs/MAC-LOCAL-PROCESSES.md` (cloud agent cannot `df` or read `~/.claude-disk-janitor/janitor.log` on the Mac).

## Executive summary

Something on the Mac removes or hollows out **suffixed** BotFleet lanes under `~/apps/botfleet-<seat>-*` within **minutes**, while BotFleet’s UI/registry loses the lane at the same time.  The Sep 2025 incident report already ruled out **`com.jay.disk-janitor` full worktree retirement** (no `RETIRED` line in `~/.claude-disk-janitor/janitor.log`) and **`code-main-keeper`** (only scans `~/Code/*`, logs to `~/apps/logs/code-main-keeper.log`).

This pass catalogs **every fleet-documented mechanism** that can remove, prune, or reap git worktrees or large trees under `~/apps`, with **triggers** (launchd, pm2, cron, BotFleet harness, agent routines).  **No single scheduled AFC script matches “minutes + silent + full directory gone”** when the tracked janitor logic and stated logs are taken at face value.  The leading hypotheses are: **(1)** a **non-logging** remover (interactive agent, BotFleet server code not mirrored in AFC, or CleanMyMac / `rm` without going through janitor), **(2)** **janitor live drift** vs tracked `scripts/disk-janitor.sh`, or **(3)** **misread partial deletion** (e.g. `node_modules` reap) followed by **`git worktree prune`** dropping registry entries while the path still exists or is empty.

**Next step on the Mac:** keep **`com.jay.reaper-watch`** running (installed for this board); on the next vanish, read `~/apps/_archive/2026-10-02/reaper-watch.log` for attributed PID/argv.  Until reaper-watch fires, treat root cause as **open**.

## Observed symptom (from board + fleet recall)

| Signal | Detail |
| --- | --- |
| Paths | `~/apps/botfleet-claude-*` (e.g. `botfleet-claude-engines`, branch `claude/engine-hardening`, **0 commits ahead of `origin/main`**) |
| Timeline | Directory emptied and BotFleet registration gone **1–3 minutes** after `git worktree add`; one vanish **~2 h** later even with dirty tree + `.janitor-keep` |
| What survived | Branch ref on remote/local repo; other workflow lanes (`botfleet-claude-eng-*`) intact at check time |
| Ruled out (logged) | No matching `RETIRED` in janitor log; no `STRAY-WORKTREE` / removal from code-main-keeper for `~/apps` |
| Environment | Mac under disk pressure (tens of GB free, load 280+); heavy concurrent agent sessions |

Sources: board row `912034a0`, Claude memory `botfleet-lane-vanish-and-no-timeout`, recall contribution `046b4a02` (runbook: `.janitor-keep`, `pnpm --dir` not `cp -R`).

## What “registered BotFleet worktree” means

BotFleet tracks active lanes in its harness data (conversations, driver config, and **This Computer** job cwd).  When the checkout directory disappears, the UI/registry row goes stale — that is a **symptom**, not proof of which process deleted files.  AFC does not contain BotFleet server source; lane lifecycle APIs likely live in **`jaywedgeworth22/BotFleet`** (not searched successfully from this cloud workspace).  Treat any BotFleet-side `worktree remove` / cleanup routine as a **high-priority follow-up** once reaper-watch names a parent process.

Standing integration lane **`~/apps/botfleet-<seat>`** (no suffix) is protected by janitor `KEEP_RE`; **suffixed** lanes like `~/apps/botfleet-claude-engines` are **not** standing lanes and remain eligible for automated reclaim when merged + clean + idle (see below).

## Inventory: mechanisms that touch `~/apps` worktrees

Paths are **live Mac** unless noted **tracked** (`AI-Fleet-Coordinator/scripts/…`).  “Worktree remove” = deletes checkout dir via git; “dep reap” = `rm -rf` on `node_modules`/`.next`/`.turbo` only.

| Actor | Trigger | Worktree remove? | Dep reap? | Log / evidence | Notes |
| --- | --- | --- | --- | --- | --- |
| **`com.jay.disk-janitor`** | launchd **every 30 min** → `~/.claude-disk-janitor/janitor.sh` (**tracked:** `scripts/disk-janitor.sh`) | **Yes**, if `REAP_WORKTREES=1`: `git worktree remove` after merged/gone + clean + **≥7d idle** (`STALE_DAYS`, floor documented line 389) | **Yes**, if free `< PRESSURE_FREE` (~65G): `rm -rf` deps on clean worktrees idle **`IDLE_HRS` (4h)** | `~/.claude-disk-janitor/janitor.log` — lines `RETIRED worktree …` / `wt-retire` | **Always** runs `git worktree prune` (registry only).  `KEEP_RE` protects `~/apps/<prefix>-<seat>` **without extra suffix** + runtimes + `~/apps/botfleet-server`.  **Not** a match for **minutes** if log + script match production. |
| **`com.jay.mac-cleanup`** | launchd **4 h** → `~/apps/mac-auto-cleanup.sh` (**tracked:** `scripts/mac-auto-cleanup.sh`) | **Explicitly no** (comment §4) | **Yes** — Python block §3b: suffixed `~/apps/*` git worktrees, clean, **4h idle**, no `.janitor-keep` | Script stdout / mac-cleanup launchd logs | Does not call `git worktree remove`.  Can delete **`node_modules`** only — can look like a “dead lane” if misread as full vanish. |
| **`com.jay.mac-resource-watch`** | launchd **5 min** → `~/apps/mac-resource-watch.py` (**tracked:** `scripts/mac-resource-watch.py`) | No (delegates) | Via **`mac-auto-cleanup.sh --pressure`** on disk hits (cooldown 3600s / 900s critical) | `~/Library/Logs/…` + watch stderr | Also POSTs BotFleet Housekeeper webhook; may wake bot to run same scripts. |
| **BotFleet Housekeeper** (bot `d43849b8…`) | Routines **09:00 / 15:00 / 21:00** + resource webhook | No direct — prompt runs janitor / mac-auto-cleanup | Same as above | BotFleet chat + webhook capture | Playbook: `docs/HOUSEKEEPER.md` — worktrees only **≥7d + merged + no `.janitor-keep`**. |
| **`code-main-keeper`** | pm2 always-on ~**60s** → `~/apps/code-main-keeper-daemon.sh` | **No** — only **`~/Code/*`** integration trees (ff-only to `origin/main`) | No | `~/apps/logs/code-main-keeper.log` | **Cannot** delete `~/apps` worktrees; wrong path by design (`docs/MAC-LOCAL-PROCESSES.md`, `AGENT-SYNC.md`). |
| **`com.jay.merge-shepherd`** | launchd **30 min** | No | No | merge-shepherd log | `gh pr update-branch` only. |
| **CleanMyMac CLI** | From janitor (low free) + **every** mac-auto-cleanup run: `clean dev/ai/trash --force` | No git API | Can delete **dev caches** under many roots (21 cache classes) | janitor / cleanup logs; CLI review output | **`junk`** module banned on janitor path (2026-09-13 log unlink bug).  mac-auto-cleanup still calls scoped dev/ai/trash.  Theoretically could touch project-adjacent trees if misclassified — **reaper-watch** target. |
| **Hog Hunter reclaim** | Janitor **lowfree** → `~/Code/HogHunter/scripts/hoghunter-clean` | No | Cache/log/APFS band | Hog Hunter / janitor log | Documented cache-only; not worktree-specific. |
| **BotFleet `update-botfleet.sh`** | On-demand / `com.jay.botfleet-update` launchd submit | No documented worktree remove | Can replace **`~/apps/botfleet-server`** tree during update | `~/Library/Caches/BotFleet/update-control/` | Requires **clean** harness checkout; not suffixed agent lanes. |
| **BotFleet background jobs** | Harness `job_start` | No | No arbitrary `~/apps` delete | `~/.botfleet/jobs/` | Stops jobs on harness restart; does not reap agent worktrees (`docs/MAC-LOCAL-PROCESSES.md`). |
| **`com.jay.grok-idle-unload`** | Hourly | No | No | unload log | Closes idle Grok sessions; **`~/.grok/worktrees`** policy separate from `~/apps`. |
| **Disk janitor — retired KIMI / scratch** | Same 30 min tick | **Yes** for `*/.claude/worktrees/*`, `*/.grok/worktrees/*`, `/tmp/*`, `*-kimi` lanes (not `botfleet-claude-*`) | Same pressure path | janitor.log | **`botfleet-claude-engines`** does not match scratch patterns (`scripts/disk-janitor.sh` `janitor_is_retired_kimi_or_scratch`). |
| **Manual / agent cleanup** | Human or agent session | **`git worktree remove`** (agents instructed never `--force` in fleet scripts) | `rm -rf` | Transcripts / reaper-watch | Oct 2026 **`wave1.py`** manifest cleanup (`~/apps/_archive/2026-10-02/`) — **DELETE** tier worktrees, **slow**, owner-driven — not minute-scale automation. |
| **`com.jay.reaper-watch`** | launchd **KeepAlive** → `~/apps/_archive/2026-10-02/reaper-watch.py` | N/A (observer) | N/A | `reaper-watch.log` | **Detection only** — board `912034a0` follow-up; do not disable while open. |
| **Hetzner / GB-HOUSEKEEPER** | ssh / cron on Coolify host | No | No | remote logs | **Out of scope** for `~/apps` on Mac. |

### Schedulers that invoke the same playbook (lock: `~/.claude-disk-janitor/.housekeeper.lock`)

From `skills/housekeeper/SKILL.md`: **`mac-cleanup` (4h)**, **`disk-janitor` (30m)**, **`mac-resource-watch` (5m)**, **three BotFleet Housekeeper routines**, **resource webhook**, plus occasional live bot thread — all can reach **`mac-auto-cleanup.sh`** and/or **`janitor.sh`**.

## Why disk-janitor is a weak fit for “minutes” (but not fully excludable)

Tracked retirement logic (`scripts/disk-janitor.sh`):

1. **`git worktree prune`** — every tick; removes **stale registry entries** if directory already gone — does not delete files by itself.
2. **`git worktree remove`** — only when branch is **merged/gone**, tree **clean** (ignoring generated junk), **no `.janitor-keep`**, and **no file mtime within `STALE_DAYS` (default 7 days)**.
3. A **fresh** lane at **`origin/main` with 0 commits ahead is **“merged”** immediately (`merge-base --is-ancestor`), but **should fail the idle gate** on first minutes after checkout.

So a **logged** janitor retirement in **minutes** would imply **live `janitor.sh` ≠ tracked script**, **`STALE_DAYS` overridden in plist**, or **clock/mtime anomaly** — verify on Mac:

```bash
grep -E 'STALE_DAYS|REAP_WORKTREES' ~/Library/LaunchAgents/com.jay.disk-janitor.plist
tail -50 ~/.claude-disk-janitor/janitor.log
shasum -a 256 ~/.claude-disk-janitor/janitor.sh scripts/disk-janitor.sh
```

## Most likely deleters (ranked for minute-scale, silent full-path loss)

| Rank | Hypothesis | Rationale | Confirm / falsify |
| --- | --- | --- | --- |
| **1** | **Short-lived process not using janitor logging** (agent `rm`/`git worktree remove`, BotFleet server cleanup, IDE/tooling) | Board already ruled janitor + keeper logs; reaper-watch exists because deleter leaves no janitor line | **`reaper-watch.log`** on next event; correlate PID with BotFleet/Electron/Claude/Cursor parent chain |
| **2** | **CleanMyMac `dev` / mac-auto-cleanup cache sweep** mis-targeting lane tree | Multiple callers; dev module walks broad cache paths; MAC-LOCAL-PROCESSES notes simultaneous **`node_modules`** loss on always-on dirs | Time-correlate vanish with `com.jay.mac-cleanup` / resource-watch / janitor `cleanmymac` lines; check whether path empty vs fully unlinked |
| **3** | **Janitor pressure dep-reap + prune** (partial) mistaken for full vanish | 4h idle gate usually blocks **minutes**, but heavy I/O can confuse operators; **`git worktree prune`** drops registration when git thinks worktree is broken | Immediately after recreate: `git -C ~/Code/BotFleet worktree list`; compare dir size; janitor log for `idle-dep-reap` / `wtprune` without `RETIRED` |
| **4** | **Live janitor drift** (old `STALE_DAYS=2` folklore vs current 7d floor) | Rollout doc `2026-08-22-disk-prune-kimi.md` mentions `STALE_DAYS=2` when free `<50G`; **tracked script line 389 says floor 7d** — drift caused past incidents | Diff live vs tracked; plist env |
| **5** | **`cp -R` / broken `pnpm` with cwd inside lane** (Sep 25 runbook) | Vanished lanes had background **`cp`/`pnpm`**; symlink failures under load — may trigger separate cleanup or human abort | Never seed deps with `cp -R`; use `pnpm --dir <lane> install` from outside lane |

**Ruled out for typical `~/apps/botfleet-*` path (unless misconfigured):**

- **`code-main-keeper`** — `~/Code/*` only.
- **Merge-shepherd, gdrive backup, Hetzner prune jobs** — no worktree semantics on Mac lanes.

## Mitigations (documented — not enforced by this PR)

From fleet recall runbook `046b4a02` / Claude memory:

1. After `git worktree add`, immediately **`touch <lane>/.janitor-keep`** and **push the branch** (note: one vanish still occurred **with** `.janitor-keep` — do not treat as airtight until reaper-watch explains it).
2. Use **`~/apps/<prefix>-<seat>-<suffix>`** naming; only **unsuffixed** `~/apps/botfleet-claude` is `KEEP_RE`-protected.
3. Install deps with **`pnpm --dir <lane> install --frozen-lockfile --prefer-offline`** from a cwd **outside** the lane.
4. Keep **`com.jay.reaper-watch`** enabled until board closes.

## Recommended next steps

| Priority | Action | Owner surface |
| --- | --- | --- |
| P0 | On next vanish, capture **`reaper-watch.log`** slice + **`janitor.log`** ±30 min | Mac operator |
| P1 | Audit **BotFleet** repo for `worktree remove`, lane cleanup, or registry GC on missing path | BotFleet PR (separate from this docs PR) |
| P1 | **`shasum`** live `janitor.sh` vs `origin/main` `scripts/disk-janitor.sh`; read plist **`EnvironmentVariables`** | Mac / AFC |
| P2 | If reaper-watch implicates **CleanMyMac** or **`mac-auto-cleanup`**, add **`~/apps/botfleet-*`** to ignore list or extend `KEEP_RE` for BotFleet-registered paths (product decision) | AFC + owner OK |
| P2 | Consider tracking **`reaper-watch.py`** + plist in AFC repo (currently only under `~/apps/_archive/2026-10-02/` on Mac) | AFC infra |

## References (AFC repo)

- `scripts/disk-janitor.sh` — worktree prune, retire, pressure dep-reap, `KEEP_RE`
- `scripts/mac-auto-cleanup.sh` — §3b suffixed worktree dep reap; §4 janitor ownership
- `scripts/mac-resource-watch.py` — disk-triggered cleanup + Housekeeper webhook
- `docs/MAC-LOCAL-PROCESSES.md` — launchd/pm2 inventory, **`com.jay.reaper-watch`**, janitor graded pressure gate
- `docs/HOUSEKEEPER.md` — BotFleet Housekeeper prompt (7d + merged + `.janitor-keep`)
- `scripts/pm2-ecosystem.config.cjs` — `code-main-keeper` daemon
- `AGENT-SYNC.md` — stray worktree policy under `~/Code/*` only
- Contract tests: `scripts/test-disk-janitor-match.sh`, `scripts/test-mac-auto-cleanup.sh`, `scripts/test-disk-janitor-keeplist.sh`

## Change log

- **2026-10-05** — GB-HOUSEKEEPER (Cursor cloud): initial inventory + ranked hypotheses for board `912034a0` (docs-only PR).
