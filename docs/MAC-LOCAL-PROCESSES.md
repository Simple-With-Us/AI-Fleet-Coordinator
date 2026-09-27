# Mac local processes (owner machine)

**This is the master list.**  Every always-on job, scheduled job, login item,
and installed helper script that agents create on Jay's Mac lives here.
Not Coolify.  Not GitHub-hosted runners.  Not a one-shot `npm exec` from an
interactive session.

- Live: `/Users/jay/apps/MAC-LOCAL-PROCESSES.md`
- GitHub: https://github.com/jaywedgeworth22/AI-Fleet-Coordinator/blob/main/docs/MAC-LOCAL-PROCESSES.md
- Apple Note: `⭐️ Background Jobs Master List` (Coding, pinned; owner-renamed 2026-08-16)
- Binding rule: `/Users/jay/apps/AGENT-SYNC.md` § Mac local processes

Last inventory: Sun, Sep 20, 2026 22:47 CDT (MM).  Top-to-bottom fleet review found five pm2 entries (`agy-acp` 4640 restarts, `grok-acp` 117, `seat-mcp` 118, `vision-worker` 117, `xcode-health` was OK via dup-detect) errored in restart loops because non-pm2 orphan processes held the same ports or lock files pm2-managed services should own: node pid 89684 holding 127.0.0.1:8765 (agy-acp stdio-to-ws), bash pid 89542 + grok-1.0 pid 89673 holding :12419 (grok-acp), Python pid 89532 holding :8793 (seat-mcp), Python pid 89565 holding `/Users/jay/vision-worker/attempt-state.json.lock`.  New `scripts/pm2-orphan-cleanup.sh` (worktree `~/apps/AI-Fleet-Coordinator-mm @ mm/start-consolidation`, PR inbound) finds the orphans by command pattern + port + lock-file, pre-checks `:port/health`, kills them, then `pm2 reset && pm2 restart <entry>`.  All four now online with restarts=0 (agy-acp pid 90113, seat-mcp pid 90818, grok-acp pid 91671, vision-worker pid 88161).  xcode-health was already using dup-detect correctly; left alone.  Log spam under `/Users/jay/.pm2/logs/{agy-acp,grok-acp,seat-mcp,vision-worker}-error.log` can be archived+deleted by `pm2 flush` once the operator is sure the restart counters stay at 0.  Prior: Sat, Sep 19, 2026 01:50 EEST (MM).  The always-on `~/apps/botfleet-server` was reset to `origin/main` (`bb283161`, PR #480) and bounced manually because (a) it had been a stale detached HEAD at `4716a96b` (#468), six PRs behind main, with two uncommitted edits on `server/index.ts` and `server/vps-computer.ts` (a 30s→90s `vpsDockerArgs` timeout tweak), and (b) the `cursor/triage-2026-09-18` branch had deleted `server/computer-capability.ts` while `server/computer-grants.ts:20` keeps statically importing it, so the harness was crash-looping on `ERR_MODULE_NOT_FOUND` and `/api/health` was returning curl-level timeouts.  Diff of the two discarded edits preserved at `~/.minimax/agents/mavis/memory/botfleet-harness-pre-ubf-2026-09-19/`.  Owner picked option (1) from the four-card questionnaire; the long-path `~/apps/update-botfleet.sh` was abandoned at ~10 min because the bash background task's watchdog killed it during the `pnpm install` + `build` phases, and the manual reset (`fetch && reset --hard origin/main && pnpm install --frozen-lockfile && kill -9 24924`) was driven from the parent shell so the harness came back under launchd on the first try.  New running PID is `16998` (`launchctl list  com.jay.botfleet-server` exit `0`); `/api/health` returns `{"app":"botfleet","pid":16998,"static":false}`, `/api/bots?messages=20` serves the `BotFleet.app` group with all three user-attempt messages visible at the transcript tail, and the renderer can re-hydrate them on next chat toggle.  No plist, env var, or `update-botfleet.sh` change was needed — this was a working-tree corruption; the binding rule for MM seats (never edit `~/apps/botfleet-server` directly; route everything through a seat worktree) is the fix for next time and should be the next paragraph in `AGENT-SYNC.md`.  Prior: Fri, Sep 18, 2026 8:50pm CT (CURSOR).  Added the missing always-on `pm2 mac-collab-litestream` row (B2 replica of THE BOARD `findings.db`; already in `expect_pm2` and the live ecosystem).  Tracked `scripts/pm2-ecosystem.config.cjs` on origin/main still listed retired scout/senate jobs and omitted `seat-mcp` + `mac-collab-litestream` — synced from live.  Prior: Fri, Sep 18, 2026 10:04pm EEST (GROK).  Installed scheduled `com.jays.subscription-quota-collector` (every 15 min, Node 26) for Claude/Codex/Grok/MiniMax remaining % → Usage Monitor.  Antigravity stays on `com.jays.antigravity-usage-collector`.  UM #1444/#1446 already on main; collector grok nested-auth fix is UM #1493.  Prior: Thu, Sep 17, 2026 5:50pm CT (GROK).  `com.jay.botfleet-server` launchd was crash-looping (~10k exits) on missing `~/apps/botfleet-server/node_modules` while `/Applications/BotFleet.app` still served `:8799`/`:8800`.  Wrapper `~/apps/botfleet-server-start.sh` now exits 0 when the harness is already healthy; KeepAlive is `SuccessfulExit=false`; watch treats `:8799 /health` as UP.  `com.jay.fleet-gdrive-backup` zip step was succeeding; agent-config rsync against Google Drive File Provider failed under launchd (`open: Operation not permitted`) — mirror is now in-process Python `copy_mirror`.  `com.jay.ios-ship-now` last exit 1 is the 2026-09-17 01:34 EEST login run (`rcs st=1 ct=1 um=1 ul=1`) after missing `*-grok-tf-runner` checkouts; live `ship-now-gui.sh` uses `~/Code/*` and skips a missing root.  Do not re-fire that job.
**pm2 `grok-leader`** is the shared Grok backend (`~/.grok/leader.sock`).  **pm2 `grok-acp`** is
`127.0.0.1:12419` for Conductor new sessions (`--no-leader serve` — `--leader serve`
does not bind).  Shellular Grok + new TUI join the leader.  List/load:
`python3 ~/apps/grok-acp-runtime/leader-client.py list`.
Slack inbox is multi-seat (`com.jay.slack-agent-inbox`).
Scout uses local `http://127.0.0.1:8899/fetch-ptr` (same session as
production).  `com.PM2` re-bootstrapped.  Claude remote-control up.
Ecosystem: `~/apps/pm2-ecosystem.config.cjs`.  Status: `bash ~/apps/mac-status.sh`.
Down/restart log: `~/Library/Logs/mac-process-watch.log`.  Watch times out `pm2 jlist` at 8s (wedged RPC used to hang the whole 120s loop), resurrects God if needed, and kills Shellular if the process is up but the cloud relay is dead (1006 / handshake).  Also expects `grok-leader` + `mac-collab` + `mac-collab-sync` + `mac-collab-writeback` + `mac-collab-litestream`.  When 3+ jobs are missing, watch **resurrects only if the dump still lists the expected names**; a one-job leftover dump is treated as poison and watch runs `pm2 start ~/apps/pm2-ecosystem.config.cjs` then `pm2 save`.  Watch restarts
always-on jobs (pm2 resurrect / ecosystem start, launchd kickstart/bootstrap)
and keeps scheduled timers **loaded** (does not kickstart idle).  Steals
stale disk-janitor / merge-shepherd run-locks older than 2h.  4-per-hour
backoff.  `MAC_PROCESS_WATCH_RESTART=0` is log-only.

**How to read the Kind column**

- **Always-on** — supposed to stay up across logout/reboot (KeepAlive or a
  login resurrect).  If it is down, that is a bug unless the row says Disabled.
- **Scheduled** — launchd/cron fires it; it is not a long-lived daemon.
- **On-demand** — installed helper.  Agents run it by hand.  It must not linger.

---

## Binding rule (every agent, every platform, forever)

If you **create, change, load, bootout, or retire** any of the following, you
**must** update this file **and** the Apple Note in the same change
(`~/apps/apple-notes-coding.sh --update "⭐️ Background Jobs Master List"`
— prefer `--update` on that exact title; do not mint a second note):

- `~/Library/LaunchAgents/*.plist` (and `/Library/LaunchAgents` /
  `LaunchDaemons` if you touch those)
- `crontab` / login items
- `pm2` KeepAlive jobs (`pm2 start` / `delete` / `save`)
- Any helper script under `~/apps`, `~/.claude-*`, `~/vision-worker`,
  `~/actions-runner`, or similar that other agents are expected to run

Do **not** add a process "just for this session" and leave it.  If it survives
logout or reboot, it belongs here.  If you retire one, mark it **Retired** —
do not delete the row.

Cloud / no-Mac agents: do not invent LaunchAgents.  Describe the job in the
PR and leave a Mac seat to install + list it.

Never register a daemon from `~/.npm/_npx/…`.  `mac-auto-cleanup.sh` wipes
that tree and the job crash-loops (Shellular did this 42,703× on 2026-08-13).
Install to `~/apps/<tool>-runtime` instead.

---

## Fleet recovery lessons (read before touching pm2)

Written after the **2026-08-21 total-degradation incident** (Grok restore, Claude
root-cause).  The Mac did **not** run out of disk (~108GB free) — it ran out of CPU
and RAM on a 16GB box, and then a poisoned pm2 dump kept the fleet from coming back.

**Memory, not disk, is what takes this box down.**  Load hit ~800 with swap at
12.4GB/14.3GB and ~400k pageouts.  `JetsamEvent-2026-08-21-021055` shows the memory
killer firing with Cursor Helper Renderer as the largest process, alongside
`shutdown_stall_2026-08-21-020046` and two dirty reboots (01:58, 02:04) with no
`.panic` file.  Under that pressure every stdio MCP server times out at 30s, the pm2
God RPC wedges, and jobs crash-loop — none of which is the individual job's fault.
Check `sysctl vm.swapusage` and `uptime` **before** debugging any single service.

**`pm2 kill` and `pm2 save` write the LIVE list over `~/.pm2/dump.pm2`.**  If the live
list is a subset, the dump is poisoned and `pm2 resurrect` can never restore the rest.
That is what happened: a DeepSeek `dsh`/`npx` session in `~/Code/Congress.Trade` started
a fresh God holding only `vision-worker` at 00:35, and the 01:05 and 01:40 kills wrote
that one-job list over the dump.  **Never `pm2 save` unless `pm2 list` shows the full 14
jobs**, and never save from a one-off `npx`/`dsh` session.  The previous good list
survives in `~/.pm2/dump.pm2.bak` — copy it aside before any pm2 surgery
(`~/.pm2/dump.pm2.golden-14apps.20260821` is the 2026-08-21 snapshot).

**`pm2 start` does NOT re-read `env` from the ecosystem file.**  pm2 caches an app's
environment on first start and replays it forever.  On 2026-08-21 Shellular crash-looped
on `/bin/sh: ioreg: command not found` — `node-machine-id` shells out to
`ioreg -rd1 -c IOPlatformExpertDevice` — because the cached PATH had no `/usr/sbin`,
even though `pm2-ecosystem.config.cjs` sets `/usr/sbin:/sbin` correctly.  Editing the
config alone changes nothing.  Use `pm2 restart <app> --update-env`, or
`pm2 delete <app> && pm2 start "$ECOSYSTEM" --only <app>`.  Verify against the LIVE env,
never the file: `pm2 jlist` → `pm2_env.env.PATH`.

**Two pm2 installs shadow each other.**  `/usr/local/bin/pm2` (7.0.1, node 24) precedes
`/opt/homebrew/bin/pm2` (7.0.3, node 26) on PATH, which yields "In-memory PM2 is
out-of-date" and a CLI that cannot drive the running God.  Prefer the explicit
`/opt/homebrew/bin/pm2`, or run `pm2 update` — but note `pm2 update` restarts every job
from the CURRENT dump, so confirm the dump is not poisoned first.

**`mac-process-watch.sh` cannot recover a poisoned dump.**  When 3+ jobs are missing it
runs `pm2 resurrect` only, and never falls through to
`pm2 start "$ECOSYSTEM" --only <name>` the way the one/two-missing path does.  Combined
with the 4-restarts/hour backoff, the watch then stops even attempting the useless
resurrect — which is why nothing self-healed overnight.  **Open follow-up:** make the
bulk path verify the expected names actually came back and start any stragglers from the
ecosystem file, and skip dump-on-kill when the live list is a subset.

**All 15 jobs are already in `~/apps/pm2-ecosystem.config.cjs`**, `mac-collab-sync`,
`mac-collab-writeback`, and `mac-collab-litestream` included — so recovery never needs a hand-rolled
`pm2 start`.  Restore with `pm2 start ~/apps/pm2-ecosystem.config.cjs` and only
then `pm2 save`.

**`grok-leader` vs a live Grok TUI:** if `/usr/sbin/lsof ~/.grok/leader.sock` shows a `grok-*` TUI holding the socket, **`pm2 stop grok-leader`** (not `pm2 kill`).  `mac-process-watch` skips restart while that socket is bound (`SKIP pm2:grok-leader lock-held`) and `pm2 stop`s an `errored` storm.  Bare `lsof` is a no-op when PATH lacks `/usr/sbin` (LaunchAgent 2026-08-21: 355 restarts).  `leader.sh` exits 75 when the lock is held; ecosystem `stop_exit_codes: [75]`.  Leaving the pm2 job `errored` burns CPU on a restart storm.  Start pm2 `grok-leader` again only after the TUI exits and the socket is free.  Do not `pm2 kill` the whole daemon to "fix" a lock conflict.

**`~/.pm2/pm2.log` grows unbounded** — it reached **230MB** by 2026-08-21, which makes
every read of it slow.  Read it with `grep -a … | tail`, and rotate it when it gets big.

**Agent worktrees belong in `~/apps/*`, never `~/Code/*`.**  `code-main-keeper` (pm2,
every ~60s) deliberately returns every top-level `~/Code/*` checkout to `origin/main` —
those trees are the human review / integration base, so "open in Xcode" always builds
the landed product.  On a non-main branch with a clean tree it soft-checkouts main and
fast-forwards.  It is safe by design (never hard-resets, never force-checkouts, never
discards commits absent from `origin/main`), but a worktree created at
`~/Code/<repo>-<seat>` jumps back to main the moment your tree goes clean, which reads
exactly like lost work if you have not pushed yet.  Push early, and put the worktree
under `~/apps/`.

---

## Always-on (supposed to stay up)

Live-checked Fri, Aug 21, 2026 ~2:25am CT.

| Name | Kind | What it is | Live now |
|---|---|---|---|
| `com.jay.claude-remote-control` | Always-on | Phone / claude.ai steering.  Monet / Renoir / Claude all look like `claude`.  Do not SIGKILL because you see no TTY.  `WorkingDirectory` in `~/Library/LaunchAgents/com.jay.claude-remote-control.plist` is the default directory new remote/mobile chats start in — changed 2026-09-01 from `/Users/jay/Code/Socratic.Trade` to `/Users/jay/Code` (bare `/Users/jay` throws "Workspace not trusted"; `/Users/jay/Code` is trust-accepted).  Edit that key + `launchctl bootout gui/501/com.jay.claude-remote-control` + `launchctl bootstrap gui/501 <plist>` to repoint it. | **Up** (reloaded 2026-09-01, now serving from `/Users/jay/Code`) |
| **pm2 `agy-acp`** | Always-on | Antigravity ACP on `127.0.0.1:8765` (loopback bind 2026-08-21).  Pinned `~/apps/agy-acp-runtime` + `start.sh`.  Public `acp.jays.services` is Cloudflare Access on Usage.Jays.Services (org `silent-frost-37e0.cloudflareaccess.com`, app `e76661ba-1d68-4688-a7ff-9fca47e41939`).  Allow `mail@jays.services` / `jaywedgeworth22@gmail.com` (policy `bd579c65-5b28-4840-b159-172b62ffd772`) plus service token `907f9543-f298-4f86-b6c2-917b1eb9dcdc` (policy `da67c29d-6d8d-4dfb-8d2b-2d71f3bf54c1`, secret `~/.secrets/acp-jays-services-access-service-token.env`).  Custom WAF block `b2b9c804020c48c6b95a6b41fc243050` (ruleset `12a588f22c6f434fa21692a824d698aa`) is disabled, not deleted.  Unauth `/health` is Access 302, not WAF 403.  Origin has no app token (stdio-to-ws returns 426).  Phone/Conductor still uses Shellular. | **Up** |
| **pm2 `seat-mcp`** | Always-on | `127.0.0.1:8793` only (`python3 -m seat_mcp` via `~/apps/seat-mcp/start.sh`).  Public `agents.jays.services` on named tunnel `Jay's Tunnel` (ingress v9 → `http://127.0.0.1:8793`).  Cloudflare Access on Usage.Jays.Services (org `silent-frost-37e0.cloudflareaccess.com`, app `1e1a5fc4-0f7d-44ac-bb0d-5fa6d5d73ddf`).  Allow `mail@jays.services` / `jaywedgeworth22@gmail.com` (policy `1c04ab21-257c-42aa-ab9c-6593917a7c2e`, OTP IdP `76030d10-ec5b-46ff-ba9d-a4412cd755ce`) plus service token `215fc9ae-4a1d-4b0e-8ec3-9bd0c49ee1fd` (policy `f0e75dca-6223-4953-b133-42439e9bdb53`, secret `~/.secrets/agents-jays-services-access-service-token.env`).  GET `/health` bypass app `99c0aacc-9e84-49b4-89b0-51bdb4b6bf36` (policy `4a451833-0bab-410f-85a7-93b1d66b733f`).  POST `/mcp` needs Access AND Bearer `SEAT_MCP_TOKEN` (`~/.secrets/seat-mcp.env`, never print).  DNS CNAME `08e22f0be6e68f208c2ecea141297243` proxied to the tunnel.  Owner approved the public hostname 2026-08-26.  Do not mint TryCloudflare.  Do not start a second listener on 8793.  In the live ecosystem (inventory 2026-09-18). | **Up** (pm2, loopback) |
| **pm2 `grok-leader`** | Always-on when socket free | Shared Grok backend.  `~/.grok/bin/grok agent --always-approve leader --no-exit-on-disconnect`.  Socket `~/.grok/leader.sock`.  Shellular and new TUI (`[cli] use_leader = true`) attach here so a phone/bot can `session/list` + `session/load` local chats.  Added 2026-08-18.  `leader.sh` exits 75 when the socket is already bound; ecosystem `stop_exit_codes: [75]`. | **Stopped 2026-08-21 18:11 CT** — this TUI (pid 12360) spawned leader pid 76260 at 15:43.  355 restarts then `errored` because watch lock-held used bare `lsof` and launchd PATH had no `/usr/sbin`.  `pm2 stop grok-leader`.  Watch now uses `/usr/sbin/lsof`.  `pm2 start grok-leader` after TUI exits. |
| **pm2 `harness-web`** | Always-on | Harness browser UI on `127.0.0.1:3080` (DSH engine; MMH is the Shellular HTTP adapter).  Tailscale Serve `https://macbook.boa-roygbiv.ts.net:3080` (tailnet only, no Funnel).  Start: `~/apps/harness-runtime/scripts/start-web.sh`.  Serve: `~/apps/harness-runtime/scripts/serve-tailscale.sh`.  Canonical repo `jaywedgeworth22/Harness`.  CLI wrapper `scripts/dsh.sh` execs `node_modules/.bin/dsh` only — never this file, never `npx`.  Dock `pingHarness` / `ensure-web.sh` treat HTTP 401 as up (auth-walled `/`).  Restore deps with `npm ci` in `~/Code/Harness`.  **Never `npx @deepseek-ai/dsh`.**  Renamed from `dsh-web` 2026-09-19. | **Up** |
| **pm2 `grok-acp`** | Always-on | Grok ACP WebSocket on `127.0.0.1:12419` (`/ws`).  Pinned `~/apps/grok-acp-runtime`.  Native `~/.grok/bin/grok agent --always-approve --no-leader serve`.  `start.sh` sets `GROK_HOME` to `acp-home/` (stripped config, no plugins, Claude/Cursor MCPs off, `[ui] permission_mode = always-approve`) so Conductor sessions do not load the TUI kitchen sink.  `acp-client.py --mcp-server NAME` expands names from user `~/.grok/config.toml`.  Empty list = no MCPs.  Client auto-approves `session/request_permission` from offered allow options and implements ACP `terminal/*`.  TUI / grok-leader keep `~/.grok`.  **Never bind `:2419`**.  `--leader serve` does not listen.  Token in `~/.secrets/grok-acp.env` (never print).  Added 2026-08-18.  Per-session MCP pick 2026-08-29.  Permission + terminals 2026-08-30. | **Up** |
| **pm2 `xcode-health`** | Always-on | `127.0.0.1:8791`.  Public `xcode.jays.services`.  Moved off launchd 2026-08-16.  **Orphan-holds-port self-heal (2026-08-21, Cursor):** `_bind_or_reclaim()` matches mac-collab — on `EADDRINUSE` SIGTERM a holder only when it is another `xcode-health-server.py` that fails an 8s `/health` probe (SIGKILL after 15s); a healthy sibling or any other process is left alone and the start exits 3.  The 2026-08-21 degradation had this job at 2678 restarts against an orphan that was still serving 200. | **Up** — `/health` 200 (pm2-owned) |
| **pm2 `mac-collab`** | Always-on | `127.0.0.1:8792`.  Public `mac.jays.services` (THE BOARD + markdown `/files`).  Token `~/.secrets/mac-collab.env`.  **Does not serve `global-api-keys`.** Names-only: `GET /files/key-names`. Timing-safe compare + 401 rate limit. `board show/status` accepts unique 8-char id prefixes. Live `~/apps/MAC-LOCAL-PROCESSES.md` on the allowlist. Autorotate board synced. | **Up** — `/health` 200 (orphan held `:8792` twice on 2026-08-20: 1:09am, and 19:50-22:06 which crash-looped pm2 ~24k times into a 28MB error log; rotated to `mac-collab-error.log.20260820-eaddrinuse-loop.gz`) |
| **pm2 `mac-collab-sync`** | Always-on | Board reconciler (effort logs + GitHub issues → mac-collab).  Not the HTTP server.  Also snapshots `~/apps/mac-collab/findings.db` to `~/apps/mac-collab/backups/findings-YYYYMMDD.db` (14-day keep).  Runs can overlap: seen 2 concurrent on 2026-08-20 because `gh issue list` calls were hitting their 60s timeout (`network is unreachable`) and a run outlived the interval.  Not the cause of that day's outage, but it doubles the POST load on the board server. | **Up** |
| **pm2 `mac-collab-writeback`** | Always-on | **Reverse-sync: board writes → live effort-log files + GitHub Issues.**  Surgical bullet moves in `~/apps/*-EFFORT-LOG.md`; REST close/reopen of Issues only when GH state differs.  Does **not** git-commit `~/Code/<repo>` (branch protection; Code trees are the integration base).  Trigger is an applied-status map plus `updated_at` (server no longer bumps `updated_at` on no-op sync POSTs).  First pass bootstraps the map and writes nothing.  Grace: stamps `writeback_at`; sync omits `status` for 15 min (effort-row **and** github-issue).  Cursor: `~/apps/mac-collab/writeback_cursor.json`.  Script: `~/apps/mac-collab/write_back.py --loop`.  In ecosystem (Grok 2026-08-22).  Added 2026-08-22 (AG); hardened 2026-08-22 (Grok) after a first-run loop closed/reopened ~1700 issues per cycle. | **Up** after harden restart |
| **pm2 `mac-collab-litestream`** | Always-on | Litestream replicate of THE BOARD sqlite `~/apps/mac-collab/findings.db` to Backblaze B2 `jays-fleet-shared-eu` prefix `mac-collab/findings.db`.  Entry `~/apps/mac-collab/start_litestream.sh` + `litestream.yml`.  In `pm2-ecosystem.config.cjs` and `mac-process-watch.sh` `expect_pm2` (watch since 2026-09-15, DSH).  **Not** Socratic.Trade's Coolify Litestream (live `app.db` on B2 `jays-socratic-trade-eu`).  Inventory row added 2026-09-18 (Grok) after a health-check found the owner-facing table skipped it. | **Up** — pm2 online 2026-09-18 |
| **pm2 `vision-worker`** | Always-on | CT scanned-PTR worker.  Moved off launchd 2026-08-16.  Grok CLI solo pass first (`--cwd grok-cwd`, medium effort, JSON schema, turns scaled 4+2*pages); on miss cascade Qwen3-VL 8B/30B (page images) then Gemini 3.7 Flash then grok-4.5.  Env `OPENROUTER_CASCADE_MODELS`.  Scripts `~/vision-worker/{worker.py,run-vision-worker.sh,grok-cwd/}`.  Hand-copied from #2165 (`70855495`) 2026-08-21 11:05pm CT.  `pm2 start ecosystem --only vision-worker`; exec cwd `~/vision-worker` (not the repo). | **Up** |
| **pm2 `cursor-slack-sync`** | Always-on | Cursor #agent-sync inbox.  Moved off launchd 2026-08-16. | **Up** — connected |
| `homebrew.mxcl.moshi-hook` | Always-on | Vendor/local hook server (`moshi-hook serve`). | **Up** (pid 1900) |
| `com.cloudflare.cloudflared` | Always-on | System LaunchDaemon.  Named tunnel `Jay's Tunnel`.  Hosts scout / agent-sync / xcode / mac / acp / agents.jays.services. | **Up** (pid 835, root).  Do not mint a TryCloudflare hostname.  Do not change `SENATE_RELAY_URL`. |
| `com.PM2` | Login one-shot | `pm2 resurrect` at login (`pm2.jay.plist`, `LaunchOnlyOnce`).  Logs: `~/Library/Logs/com.PM2.*.log`. | Re-bootstrapped Fri, Aug 21 2026.  `LaunchOnlyOnce` exits after resurrect and can drop out of `launchctl print` — that is expected.  Plist stays in LaunchAgents.  Do not invent a KeepAlive PM2 daemon. |
| **pm2 `shellular`** | Always-on | Phone → this Mac (Shellular).  Pinned install: `~/apps/shellular-runtime` (v0.0.52).  **Not** launchd — `com.jay.shellular` is **disabled**.  Grok Build spawn (`~/.shellular/agents.json`): `~/.grok/bin/grok agent --always-approve --leader stdio`.  `--always-approve` / `--leader` are `agent` flags — **never** after `stdio`. | **Up** |
| **pm2 `residential-proxy`** | Retired | Retired 2026-09-09 (MINIMAX).  Replaced by the physical-device residential-IP proxy on the WireGuard mesh (the "little proxy server" Coolify bounces through).  CT server now calls the device directly; no Mac-side HTTP CONNECT needed.  Source: `~/Code/Congress.Trade/scout/run-residential-proxy.sh` and `scout/residential-proxy.mjs` (deleted in the same change).  `pm2 delete residential-proxy` already run; the doc row stays so the retirement is auditable. | **Retired** |
| **pm2 `scout`** | Retired | Retired 2026-08-28 (AG) in favor of server-directed scraping.  Reinforced 2026-09-09 (MINIMAX) — the source files in `~/Code/Congress.Trade/scout/` are deleted; the runtime no longer has any scout-side state on this Mac. | **Retired** |
| **pm2 `senate-relay`** | Retired | Retired 2026-09-09 (MINIMAX).  Senate eFD relay moved to the Coolify server; the local Mac no longer runs the relay process.  Source: `~/apps/senate-relay-runtime/senate-relay.ts` (pinned runtime) and `~/Code/Congress.Trade/scout/senate-relay.ts` (deleted in the same change).  `pm2 delete senate-relay` already run. | **Retired** |
| **pm2 `senate-tunnel`** | Retired | Retired 2026-09-09 (MINIMAX).  Watcher was a no-op once the named tunnel `Jay's Tunnel` is the only ingress (`com.cloudflare.cloudflared` system LaunchDaemon, see below).  The `scout.jays.services` hostname is being retired alongside this; no other job depends on it. | **Retired** |
| **pm2 `agent-sync-push`** | Always-on | Slack Socket Mode fan-out + `POST /post` + authenticated WebSocket (same `AGENT_SYNC_POST_TOKEN`). | **Up** — `http://127.0.0.1:8787/health` 200 |
| **pm2 `code-main-keeper`** | Always-on | Keeps `~/Code/*` integration trees on `origin/main` (ff-only). | **Up** |
| `com.jay.slack-agent-inbox` | Always-on | Slack DM inbox for **any** agent seat (Grok / Claude / Codex / Cursor / Antigravity / Kimi).  Prefix or `/use`.  Start script sources `~/.secrets/agent-sync.env` (token not in the plist).  Replaces the Grok-only `slack-grok-listen.py` orphan. | **Up** (launchd KeepAlive, 2026-08-17) |
| `com.jay.botfleet-server` | Always-on | BotFleet harness on loopback `127.0.0.1:8799`, webhook `8800`.  Live UI is `/Applications/BotFleet.app` (Electron forks the packaged server).  launchd runs `~/apps/botfleet-server-start.sh` from detached checkout `~/apps/botfleet-server` only when `:8799 /health` is down.  KeepAlive `SuccessfulExit=false` so a clean "already healthy" exit does not restart-storm.  Tracked `scripts/botfleet-server-start.sh` + `scripts/launchd/com.jay.botfleet-server.plist`.  **2026-09-19 MM:** the always-on checkout was treated like a feature branch by a different seat (`cursor/triage-2026-09-18` deletion of `server/computer-capability.ts`) and silently fell six PRs behind `origin/main`; the renderer kept showing an "already healthy" 200 from its own `/health` while the harness itself crash-looped on `ERR_MODULE_NOT_FOUND` and the user's outbound messages routed to sqlite but never broadcast back.  Reset to `origin/main` (`bb283161`, PR #480) via `git -C ~/apps/botfleet-server fetch origin main && reset --hard origin/main && pnpm install --frozen-lockfile` after the long-form `~/apps/update-botflight.sh` (sic; `update-botfleet.sh`) hung past its bash background-task watchdog at the build step.  Restart was `kill -9 24924` so launchd's wrapper bounced the new tree; running PID `16998`.  Two uncommitted edits were discarded; backup at `~/.minimax/agents/mavis/memory/botfleet-harness-pre-ubf-2026-09-19/`.  **Binding follow-up to add to AGENT-SYNC.md:** a seat worktree that deletes files used by the always-on harness must `git -C ~/apps/botfleet-server reset --hard origin/main` (or `ubf`) before any next-message send from the desktop app, otherwise the message lands in sqlite but never gets the SSE frame back.  **2026-09-17 GROK:** launchd had been crash-looping on missing `node_modules` (`yaml` import from `server/drivers/acp/hermes.ts`) while the app still served 200.  Watch now short-circuits on `:8799` health.  Restore deps with `pnpm install --frozen-lockfile` in that checkout; `KEEP_RE` already protects it from janitor reaps.  Supervised signed Mac adoption on Sat, Sep 12, 2026 (Codex): app and matching dependencies/source installed at `ae8abe7d5d595b427164ddf37fecebcf7da65c05`.  Authenticated runtime identity verified PID 11581, clean exact source/API 1, one SQLite owner; installed UI attached on 18799 and retained the unsent draft.  Rollback app/dependencies and data/user-state snapshot retained in `~/Library/Caches/BotFleet/updates/audit-ae8abe7d-v2-20260912/previous/receipt.json`.  The old on-demand updater was replaced on Sat, Sep 12, 2026 6:50pm CT (Claude) — `~/apps/update-botflight.sh` (sic; `update-botfleet.sh`) is now the transactional wrapper from `3b30294e` (PR #325, with the PR #371 rollback-placement fix), backup at `update-botfleet.sh.bak-20260912-safe-updater`.  Before the next `ubf`, this checkout must be clean: `apply` refuses on any `git status --porcelain` output, and it carried a modified `electron/vendor/electron-updater.cjs (restored with `git checkout` on Sat, Sep 12, 2026 7:10pm CT by Claude; tree clean)` at install time.  Sat, Sep 12, 2026 1:20pm CT (Claude): restarted after the routines.json prompt-snapshot wedge and a node_modules reap (see inventory line); the checkout is now on the janitor keep list, and `server.log` is a fresh file again (the old one had been unlinked while launchd still held it, so nothing was logged from 12:31 until the restart). | **Up; verified** |
| `com.jay.botfleet-update` | On-demand (one-shot) | **Added Sun, Sep 13, 2026 (CLAUDE), BotFleet branch `claude/remote-update`.**  Not a plist and not always-on: the BotFleet harness submits it with `launchctl submit -l com.jay.botfleet-update -o <log> -e <log> -- /bin/bash -c 'export PATH=<node dir>:"$PATH"; exec /bin/bash ~/apps/update-botfleet.sh --progress <file> --run-id <id>'` when someone presses **Install Update** in the desktop app or taps it on the paired phone (`POST /api/update/run`).  It runs the same transactional updater `ubf` runs, and it has to be a launchd job rather than a child of anything: the updater stops `com.jay.botfleet-server` and quits `/Applications/BotFleet.app`, so a child of either would die mid-install.  launchd owns it instead, in the same GUI domain.  The job writes step-by-step progress and a final outcome to `~/Library/Caches/BotFleet/update-control/runs/<runId>.progress.json` with its stdout and stderr beside it in `<runId>.log`; the harness reads those back, which is also how a run survives the harness restart it performs.  `current-run.json` / `last-run.json` / `available.json` in that same directory are the harness's own bookkeeping.  The label is removed (`launchctl remove`) before each submit, and a submit failure falls back to a detached `spawn` of the same script.  Nothing loads it at boot and nothing keeps it alive; after a run it stays registered until the next submit removes it. | **Not yet in use** — lands with BotFleet PR; no run has been started from this Mac. |
| `com.jay.botfleet-imessage-relay` | Retired / Forbidden | Retired 2026-09-02 (Antigravity).  Violated the strict owner account boundary: automated outbound messages must NEVER be sent from user `jay`.  Caused an infinite echo loop with `imessage-botfleet.py` on `agents`.  All outbound bot messages must ONLY be sent via `agents` (`agentchat@icloud.com`).  Never re-enable under `jay`. | **Retired / Forbidden** |
| `com.jay.imessage-accountant-forward` | Always-on | Forwards accountant-relevant iMessages.  `KeepAlive=true`.  A **fleet** job that was missing from this list until 2026-09-01 (Claude). |
| `com.jay.imessage-grok` | Always-on (intended) | Grok inbox for iMessage group Grok - Socratic Trade.  Reads `chat.db`.  Plist now calls Xcode `Python.app` (not `/usr/bin/python3`). | launchd **disabled** (FDA).  Aqua orphan **Up** (pid 81696, `ppid=1`, this is the intended listener — do not kill as a leak).  launchd-spawned `Python.app` still gets `authorization denied` on `chat.db`.  Owner: System Settings → Privacy & Security → Full Disk Access → allow that `Python.app` for background, then `launchctl enable gui/501/com.jay.imessage-grok` and bootstrap.  Do not load until then (KeepAlive would crash-loop every 10s). |
| `com.jay.shellular` | Disabled | Old launchd wrapper (`npx shellular start`).  Disabled 2026-08-12.  Do not re-enable while pm2 owns it. | **Disabled** |
| `com.jay.agy-acp` / `com.jay.xcode-health` / `com.congress.trade.vision-worker` / `com.cursor.slack-sync` | Retired launchd | Replaced by the pm2 jobs of the same name.  Labels **disabled**.  Plists left on disk. | **Disabled** |

---

## Scheduled (fires, then exits)

| Name | Cadence | What it is |
|---|---|---|
| `com.jay.disk-janitor` | every 30 min | Regenerable-cache + CleanMyMac CLI deep sweep + idle-worktree cleanup when disk is tight (< 80G free; pressure at 65G).  Walks fleet Code repos including BotFleet/fleet-ops.  Kimi-named / nested `.claude/worktrees` / `/tmp` scratch reaped when clean.  `STALE_DAYS=2` under 80G free.  Steal a run-lock older than 2h.  Sat, Sep 12, 2026 1:20pm CT (Claude): `KEEP_RE` now also protects `~/apps/botfleet-server` (the always-on harness checkout is clean and idle by construction, so the pressure reap deleted its dependencies twice this week).  Sat, Sep 12, 2026 4:50pm CT (Claude): the low-free CleanMyMac step no longer runs bare `cleanmymac clean --force` (which ran its `junk` module and deleted the live, launchd-held-open `~/Library/Logs/botfleet/server.log` out from under `com.jay.botfleet-server` on 2026-09-12 ~12:31) — it now runs only `cleanmymac clean dev --force`, `cleanmymac clean ai --force`, and `cleanmymac clean trash --force` (never system junk), plus `cleanmymac optimize ram` as before.  Also added `~/Library/Logs`, `~/Library/Logs/botfleet`, and `~/.botfleet` to CleanMyMac's own ignore list (`cleanmymac ignore add`, now 20 entries) as a second layer against any other `clean junk` caller — including `~/apps/mac-auto-cleanup.sh`'s own unconditional `cleanmymac clean --force`, which still runs it and is flagged separately, unfixed here. |
| `com.jay.merge-shepherd` | every 30 min | `gh pr update-branch` so bot merges still retrigger verify.  Same 2h stale-lock steal (wedged 2026-07-14..16). |
| `com.jay.mac-process-watch` | every 120 s | Always-on restarter + scheduled-timer keeper.  pm2 via `~/.pm2/pm2.pid` + `kill -0` (do not pgrep -f).  launchd always-on: bootstrap / kickstart, except `com.jay.botfleet-server` which is UP when `127.0.0.1:8799/health` is 200 (do not steal the port from BotFleet.app).  Scheduled: bootstrap if not-loaded; **idle is OK**.  Never bootstrap `com.jay.ios-ship-now` or `com.PM2`.  Steals janitor/shepherd locks >2h.  Checks trigger script paths exist.  Backoff 4/hour.  `MAC_PROCESS_WATCH_RESTART=0` = log-only. |
| `com.jays.mac-server-watchdog` | every 120 s | Mac heartbeat → Usage Monitor + local self-heal. |
| `com.jays.antigravity-usage-collector` | every 4 h | Antigravity quota → Usage Monitor ingest.  Direct Node execution reading `USAGE_INGEST_TOKEN` from `~/.secrets/global-api-keys` (repaired 2026-09-12 to avoid headless Infisical login errors). |
| `com.jays.codex-usage-collector` | every 15 min | Codex CLI session JSONL tokens → Usage Monitor ingest (estimated API-equivalent).  Default lookback 180 days.  Direct Node execution with secrets fallback. |
| `com.jays.grok-usage-collector` | every 15 min | Grok Build `updates.jsonl` turn_completed tokens + costUsdTicks → Usage Monitor ingest (estimated API-equivalent).  Default lookback 180 days.  Direct Node execution with secrets fallback. |
| `com.jays.copilot-usage-collector` | every 15 min | Copilot CLI `session-state/*/events.jsonl` shutdown modelMetrics tokens → Usage Monitor ingest (estimated API-equivalent).  Direct Node execution with secrets fallback. |
| `com.jays.subscription-quota-collector` | every 15 min | Claude, Codex, Grok, MiniMax subscription remaining % → Usage Monitor ingest (`GET /api/quota-windows`).  Antigravity is NOT collected here.  Direct Node 26 (`/opt/homebrew/bin/node`) with `USAGE_INGEST_TOKEN` fallback from `~/.secrets/global-api-keys`.  Node 24 `fetch` ETIMEDOUTs on Grok/MiniMax.  Script `~/Code/Usage-Monitor/scripts/subscription-quota-collector.mjs` (UM #1493 nested Grok `key` + `config.creditUsagePercent`).  `--redacted` in launchd logs.  Example plist in the repo.  Installed 2026-09-18 (GROK).  **Scheduled, not KeepAlive.** |
| `com.jay.mac-cleanup` | every 4 h (14400s) | **2026-09-01 (Claude):** now takes a **re-entrant owner-token lock** (`~/.claude-disk-janitor/.housekeeper.lock`, 2h stale-steal) — it previously had none while two independent invokers reached it.  Callers already holding it export `HOUSEKEEPER_LOCK_OWNER=1` so the parent does not deadlock its own children.  `cleanmymac optimize ram` is **removed** (gated behind `RESOURCE_ALLOW_RAM_OPTIMIZE=1`): it purges resident pages into swapfiles on the same APFS container as user data, measured **+3.6-7.3G swap per run**.  Plist gained `EnvironmentVariables:PATH` — launchd's default PATH excludes `/opt/homebrew/bin`, so the `npm`/`pnpm`/`brew`/`cleanmymac` blocks had been silently no-opping under launchd.  Safe developer cache, CleanMyMac CLI (system junk, dev junk, AI cache, trash), Xcode iOS DeviceSupport/DerivedData, unavailable simulators only (`simctl delete unavailable`), agent session prune, and remote Hetzner Coolify docker prune.  **Wipes `~/.npm/_npx`.**  `--pressure` adds CleanMyMac purge + 3-day Grok session prune.  Does **not** reap git worktrees or wipe `~/.grok/worktrees` — that is `com.jay.disk-janitor`.  **2026-09-08 (Claude):** `npm cache clean --force` and `pnpm store prune` (in `~/apps/mac-auto-cleanup.sh`) now skip via `pgrep` while an `npm`/`pnpm install` is running anywhere, and the worktree dep-reap refuses to delete a `node_modules`/`.next`/`.turbo` dir that `git ls-files --error-unmatch` still shows as tracked (a sweep once deleted a tracked vendored `node_modules` under `app/vendor` in a Congress.Trade worktree).  **Sat, Sep 12, 2026 4:50pm CT (Claude):** the unconditional (every tick, no free-space gate) `cleanmymac clean --force` call no longer runs `junk` — it ran EVERY clean module including system junk, and deleted the live, launchd-held-open `~/Library/Logs/botfleet/server.log` out from under `com.jay.botfleet-server` on 2026-09-12 ~12:31 (same bug as `com.jay.disk-janitor`, PR #207).  Now runs only `cleanmymac clean dev --force`, `cleanmymac clean ai --force`, and `cleanmymac clean trash --force`; `cleanmymac purge --force` under `--pressure` and `cleanmymac optimize ram` under `RESOURCE_ALLOW_RAM_OPTIMIZE=1` are unchanged.  `~/Library/Logs`, `~/Library/Logs/botfleet`, and `~/.botfleet` are also on CleanMyMac's own ignore list now (added in PR #207, shared across every caller of the CLI on this Mac). |
| `com.jay.mac-resource-watch` | every 5 min (300s) | **2026-09-01 (Claude) — loop fix.  Cleanup previously had NO cooldown (only the webhook did), so with disk hovering under the 80G warn line it ran `mac-auto-cleanup.sh --pressure` every 5 min, back to back.  Measured: each run grew swap 3.6-7.3G and 2 of 3 runs ended with LESS free disk.**  Now: `CLEAN_COOLDOWN_SEC=3600` (`900` when critical), a single-flight lock, 3x backoff when a run frees nothing, and cache cleanup only on a **disk** hit (swap/load-only pressure no longer triggers it — deleting caches cannot reduce swap).  `--clean` no longer bypasses the cooldown (all four BF-Housekeeper surfaces pass it); `--force` is the only bypass.  The `swap_total_gb >= 8` disjunct is **removed** — macOS grows the swap store on demand, so once past 8G it was true forever and every tick reported a hit; the gate is now `swap_used_pct >= 80 AND swap_used_gb >= 8`.  Webhook now sends **both** `X-BotFleet-Event` and `X-Webhook-Event` (the launchd receiver on :8800 runs older code that only reads the latter) and parses the response body — a 202 is returned for accepted/ignored/captured/duplicate alike, and only a payload carrying `runId` actually started a bot.  Samples disk free, swap, and 1-minute load.  On warn/crit (disk ≤80G / drop ≥6G / swap ≥80% and ≥8G total / load ≥16) runs `mac-auto-cleanup.sh --pressure` and POSTs BotFleet Housekeeper webhook (`resource.disk` / `resource.ram` / `resource.cpu`).  Cooldown 45 min.  Secret `~/.secrets/botfleet-housekeeper-webhook.env` (never print).  Live `~/apps/mac-resource-watch.py`.  Tracked `scripts/mac-resource-watch.py` + `scripts/launchd/com.jay.mac-resource-watch.plist`. |
| `com.jay.fleet-gdrive-backup` | daily 06:00 local | Zip `~/Code` git repos to Google Drive `Website & App Source Backups - YYYY-MM-DD` **and** mirror fleet agent skills/rules into `My Drive/fleet-agent-config/` plus refresh `My Drive/fleet-skills/`.  List = `fleet-apps.json` plus extra Code checkouts (code-main-keeper skip list).  Live `~/apps/fleet-gdrive-backup/run.sh`.  Tracked `scripts/backup-fleet-to-gdrive.py` + `scripts/sync-fleet-agent-config-to-gdrive.py` + `scripts/launchd/com.jay.fleet-gdrive-backup.plist`.  GitHub 90-day artifacts: coordinator `.github/workflows/backup-repos.yml`.  **Scheduled, not always-on.**  **2026-09-17 GROK:** repo zips were succeeding; the job still exited 1 because `rsync -a --delete` against the Drive File Provider mount returned `open: Operation not permitted` on `fleet-agent-config/gemini/skills`.  Agent-config + fleet-skills mirror now uses in-process `copy_mirror` (no rsync). |
| `com.jay.grok-idle-unload` | hourly (3600s) | **Scheduled, not always-on.**  `session/close` on **live** Grok TUI chats idle >12h so their MCP tool processes exit.  Does **not** `/delete` transcripts.  `/resume` or `grok --resume ID` reloads tools.  Skips working / needs-input / pendingTool / `$GROK_SESSION_ID`.  Also SIGTERMs orphan MCP children whose session is no longer live and is missing or also idle >12h.  Override: `GROK_IDLE_UNLOAD_HOURS` in the plist.  Live `~/apps/grok-acp-runtime/grok-idle-unload.py`.  Tracked `scripts/grok-acp-runtime/grok-idle-unload.py` + `scripts/launchd/com.jay.grok-idle-unload.plist`.  On-demand: `python3 ~/apps/grok-acp-runtime/grok-idle-unload.py --dry-run`. |
| `com.jay.github-outbox-bridge` | every 120 s | **Not yet installed** — lands with AFC PR #251 (Claude, cloud); a Mac seat installs it and refreshes the pinned Note.  `#agent-sync` hands for a browser-only seat (Instinct: form-filling vault, no API headers, no MCP client).  Posts new comments on that seat's private outbox GitHub issue (`jaywedgeworth22/fleet-ops`) to `#agent-sync` through the loopback relay `POST http://127.0.0.1:8787/post` as `username=<SEAT>` after checking the `[SEAT]` / `[SEAT->PEER]` + `repo:` header shape (rocket reaction = posted; confused reaction + reply = rejected), and mirrors skim matches (`->SEAT`, `@SEAT`, `->FLEET`, HALT / PROD DOWN / URGENT / OBJECTION / HEADS-UP / DEPLOY CLAIM) back onto the same issue as `<!-- outbox-bridge:slack -->` comments marked as data.  Tokens from `~/.secrets/agent-sync.env` only (`SLACK_BOT_TOKEN`, `AGENT_SYNC_POST_TOKEN`; never in the plist).  Config `~/apps/github-outbox-bridge.json` (`{"seats":[{"seat":"INSTINCT","repo":"jaywedgeworth22/fleet-ops","issue":N}]}`).  State `~/.agent-sync/outbox-bridge-<SEAT>.json`; single-flight `flock`; after 3 relay failures it comments once that the relay is down and keeps retrying.  Live `~/apps/github-outbox-bridge.py`.  Tracked `scripts/github-outbox-bridge.py` + `scripts/launchd/com.jay.github-outbox-bridge.plist` + `scripts/test_github_outbox_bridge.py`.  On-demand: `python3 ~/apps/github-outbox-bridge.py --once --dry-run`. |
| `com.github.domt4.homebrew-autoupdate` | daily | Homebrew autoupdate.  Vendor. |
| `com.google.GoogleUpdater.wake` | hourly | Google updater wake.  Vendor. |
| `services.jays.hetzner-watcher` | **Retired** (2026-08-28) | Was hourly watch for scarce server models (`cx53`, `cx43`, `ccx53`). Retired after successful server upgrades. |
| cron `41 9 * * *` | **Retired** (2026-08-28) | Was daily watch `~/apps/check-hetzner-cx43.sh`. Crontab entry removed. |

---

## On-login / one-shot launchd

| Name | Kind | What it is |
|---|---|---|
| `com.jay.ios-ship-now` | One-shot at login | GUI-session TestFlight ship (needs login keychain).  KeepAlive false.  Last exit **1** from the 2026-09-17 01:34 EEST login run: `rcs st=1 ct=1 um=1 ul=1` after `ship-testflight.sh` `cd` into missing `~/apps/{trading,congress,usage}-grok-tf-runner`.  Live `ship-now-gui.sh` now ships from `~/Code/*` and skips a missing root (does not fail the whole job).  Watch never bootstraps this label (`RunAtLoad` would ship TestFlight).  Do not re-fire from this job. |
| `com.jay.provider-knob-sync` | Scheduled (30 min) | `~/apps/provider-knob-sync-start.sh` → `scripts/sync-provider-knobs.sh --apply`.  Slack token lives in `~/.secrets/provider-knob-sync.env` (not the LaunchAgent env).  **Not** always-on — `StartInterval=1800`, `RunAtLoad=false`.  Do not invent a KeepAlive job. |

Login items (Aqua, user apps — not agent-owned): Wallspace, GeminiAppLauncher, Devly, Kimi, Google Drive, Dockspace.

---

## On-demand helpers (able to be run; must not stay up)

These are the installed scripts agents actually invoke.  They are **not**
daemons.  If you add another one that other seats should use, list it here.
Worktree one-offs (`~/apps/congress-grok-*`, `trading-grok-*`) are **not**
listed — those die with the branch.

| Path | What it is |
|---|---|
| `~/Code/ai-fleet-coordinator/scripts/{build-fleet-daily-digest.py,build-agent-calendar.py}` | On-demand locally and scheduled in hosted GitHub Actions every 6 hours.  Rebuilds the public activity HTML, Markdown, and ICS feeds from repositories explicitly allowlisted in `scripts/public_activity_repos.py` and verified currently public through GitHub API.  Internal effort boards are never read into public feeds.  No Mac daemon or local schedule is installed.  Updated 2026-09-26 (CODEX). |
| `~/apps/otel-lane-tag.sh` | On-demand.  Tags a Claude Code lane's untracked `.claude/settings.local.json` with `OTEL_RESOURCE_ATTRIBUTES=seat=<seat>,lane=<dir-name>,project=<repo>,repo=<repo>` so its sessions carry seat/lane/app attribution into Sentry `agent-sessions` (`project=` also drives Usage Monitor's per-project cost attribution — see UM `AGENTS.md` "Per-project cost attribution").  Reads ai-fleet-coordinator's `fleet-apps.json` from `origin/main` (never `~/Code`) for the prefix → repo and seat → worktreeSuffix mapping.  Run it after creating a new lane, or `--all` to sweep every lane under `~/apps` once (safe to re-run; skips anything under `~/Code`, anything not a git worktree, and any lane whose name does not parse as `<prefix>-<seat>[-suffix]`, and leaves an existing `.envrc` project value alone).  Only ever touches that one untracked file and, inside it, only the `env.OTEL_RESOURCE_ATTRIBUTES` key.  **Verified 2026-09-24 (Claude):** Sentry's OTLP log ingest reserves the top-level `project` key and silently overwrites an incoming resource attribute of the same name with its own project slug — the same collision already seen for `organization.id`.  `repo=` is the Sentry-safe duplicate of the same value; `project=` stays because that is the exact key Usage Monitor's OTLP-metrics project-resolver reads, and metrics do not go through this collision.  Added 2026-09-24 (Claude). |
| `~/.claude/hooks/command-class-pretooluse.py` | Always-on (Claude Code PreToolUse hook, matcher `Bash`, registered in `~/.claude/settings.json` alongside `board-check.sh` and `secret-guard-pretooluse.py`).  Idle-poll detection signal: classifies each Bash command into a fixed enum (`poll-wait` / `git` / `test` / `build` / `deploy` / `read` / `other`) in-process from the command text only, then emits exactly one OTLP log record `{class, session.id, seat}` to the existing Sentry `agent-sessions` logs pipeline — fire-and-forget, ~1.2 s socket timeout, never the command text or any other field.  Never blocks the tool call or changes the permission decision: always exits 0 with no stdout, and any network/parse failure is silently swallowed.  `seat` is read from `OTEL_RESOURCE_ATTRIBUTES` (see `otel-lane-tag.sh` above), falling back to `AGENT_SEAT`.  The OTLP destination (`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` / `_HEADERS`) is read directly from `~/.claude/settings.json` rather than assumed to be inherited from the parent process's env — **verified 2026-09-24 (Claude):** a live `claude -p` run showed the parent session's own `claude_code.*` logs reaching Sentry fine while this hook's log did not arrive, confirming a PreToolUse hook subprocess is not guaranteed to inherit the parent's `OTEL_EXPORTER_OTLP_LOGS_*` env; reading the settings file directly fixed it, re-verified live in-session afterward (`poll-wait`, `git`, `build`, `read`, `other` all confirmed arriving with correct `class`/`seat`/`session.id`).  Added 2026-09-24 (Claude). |
| `~/Code/Socratic.Trade/scripts/infisical-secrets-safe.sh` (run from the owned agent lane) | On-demand.  Safe Infisical operations helper; rejects runtime LLM key writes, including `MINIMAX_API_KEY`, which belongs in Settings API Keys.  Updated 2026-09-09 (Codex). |
| `~/.local/bin/sharing` (`handoff`, `sharinghandoff`) | On-demand.  Restarts `cloudd` and `sharingd` to recover stuck Universal Clipboard, AirDrop, and Handoff on macOS.  Also available via macOS Shortcuts ("sharing", "handoff", "sharinghandoff"). |
| `~/apps/send-sms` / `~/apps/local-sms-gateway/send-sms.py` | Local Twilio SMS gateway & CLI for one-way SMS alerts to owner. Symlinked to `/usr/local/bin/send-sms`. Supports CLI (`send-sms "msg"`) and localhost HTTP server (`--serve --port 8787`). |
| `~/apps/fleet-mode` | On-demand.  Prints the portable Fleet mode briefing for pasting at any platform's agent. |
| `~/Code/Usage-Monitor/scripts/namecheap.py` | Namecheap XML API CLI for querying balances, listing active domains, checking domain availability, and inspecting DNS records. |
| `~/apps/hetzner-watcher/check-hetzner-availability.py` | On-demand & scheduled. Checks Hetzner Cloud API in Nuremberg (`nbg1`) for scarce server models (`cx53`, `cx43`, `ccx53`, `ccx63`) and sends immediate SMS alerts via `send-sms`. |
| `~/apps/apple-notes-coding.sh` | Create/update/pin Coding notes.  Fixed Thu, Sep 17, 2026: non-ASCII titles no longer duplicate on --update. |
| `~/Code/AI-Fleet-Coordinator/scripts/safari-start/install.sh` (run from the owned `~/apps/fleet-*-*` lane) | On-demand.  Copies the Safari start page to `~/Sites/safari-start/index.html` and sets homepage / new tab / new window in both the global Safari domain and the sandboxed container plist.  Quits Safari so the keys stick.  `--revert` restores Apple's Start Page.  Hosted aliases `https://jays.services/start/` and `https://start.jays.services/` are being consolidated onto protected home.  The installer now copies a redirect page; existing installed copies and Safari preferences are unchanged until explicitly run.  Updated 2026-09-26 (CODEX); originally added 2026-09-14 (FX). |
| `Simple-With-Us/scripts/build_catalog.py` (run from an owned checkout) | On-demand.  Generates the public catalog and per-product availability regions from `apps/index.json`; `--check` verifies link policy and generated freshness.  No background process.  Updated 2026-09-26 (CODEX). |
| `Personal-Site/scripts/verify-public-boundary.mjs` (run from an owned checkout) | On-demand.  Checks the portfolio's public project/link boundary and catalog projection.  No background process.  Added 2026-09-26 (CODEX). |
| `AI-Fleet-Coordinator/scripts/safari-start/worker.js` (deploy from an owned checkout) | On-demand deployment helper/source for the hosted Worker.  Page aliases forward to protected `home.jays.services`; `/suggest` remains for installed local copies.  No new Mac background process.  Updated 2026-09-26 (CODEX). |

| `~/apps/icloud-mail.py` / `~/apps/icloud-mail.sh` | On-demand. Fleet-wide iCloud Mail CLI & library for IMAP/SMTP message listing, search, reading, triage, and sending. MCP launcher: `~/apps/mcp-servers/icloud-mail-launch.sh`. Doc: `~/apps/ICLOUD-MAIL.md`. |
| `~/apps/agent-sync-poll.py` | One-pass #agent-sync read (session start). |
| `~/apps/agent-sync-websocket.py` | Local Slack post helper (needs slack_sdk). |
| `~/apps/agent-sync/consumer.mjs` | Slack Socket Mode consumer (session attach). |
| `~/apps/slack-sync.sh` | Bot-token Slack read/post without MCP. |
| `~/apps/mac-status.sh` | One-screen pm2 + launchd + down-watch.  **This is the command.**  Run it as the alias **`ms`** (`.zshrc:29`) — the alias is how people actually find it.  Annotates TUI-held `leader.sock` and the `ios-ship-now` login leftover. |
| `~/Code/AI-Fleet-Coordinator/scripts/fleet-rag.py` | On-demand.  Thin CLI over `scripts/fleet_rag/` for the `fleet-agents` knowledge corpus.  `stats` / `search "..."` / `ingest file.jsonl` / `ingest-all`.  Talks to the **Hetzner** services `qdrant-st` (`100.69.77.26:6333`) and `tei-bge-m3` (`100.69.77.26:8081`) over Tailscale — nothing runs locally, so there is no daemon to keep up.  Loads creds from Infisical shared/prod by itself.  Ingest is idempotent by content hash.  Canonical: `docs/RAG-FLEET-INFRA.md`.  Do **not** point Socratic.Trade's embed provider at that endpoint — see the doc's parity section. |
| `~/apps/fleet-rag/recall` | On-demand.  Installed copy of `scripts/recall` (search / stats / contribute / eval / doctor / ingest).  Symlinked at `~/apps/mac-collab/recall` and `~/.local/bin/recall` (same PATH pattern as `board`).  MCP stdio server is `~/apps/fleet-rag/fleet-recall-mcp.py`.  Installer: `scripts/install-fleet-rag.sh`. |
| `~/apps/fleet-rag/recall-tunnel` | On-demand.  Opens/checks/closes an SSH tunnel to the Hetzner box (`recall-tunnel up` / `status` / `down`) forwarding Qdrant 6333, TEI embed 8081, and TEI rerank 8082 to `127.0.0.1:16333/18081/18082`, for when Tailscale is down on this Mac; `eval "$(recall-tunnel env)"` exports `QDRANT_URL` / `TEI_URL` / `TEI_RERANK_URL` so `recall` tries the direct path first.  Symlinked next to `recall` at `~/apps/mac-collab/recall-tunnel` and `~/.local/bin/recall-tunnel`.  Installer: `scripts/install-fleet-rag.sh`. |
| `~/.claude/hooks/fleet-recall-session-start.sh` + `~/.claude/hooks/fleet-recall-stop.py` | Always-on (Claude Code hooks, registered in `~/.claude/settings.json` SessionStart + Stop).  SessionStart adds one context line (corpus size; search before re-deriving, contribute at closeout).  Stop nudges once per substantial session (25+ tool uses, no `recall_contribute` yet) to commit a lesson or reply "no lesson"; honours `stop_hook_active`, marker under `~/apps/fleet-rag/state/hook-nudged/`.  `FLEET_RECALL_HOOKS=0` disables.  Installed by `install-fleet-rag.sh --hooks`; tracked `scripts/hooks/`.  Added 2026-09-02 (Claude). |
| `~/.claude/hooks/subagent-economy-pretooluse.py` | Always-on (Claude Code PreToolUse hook, matcher `Agent|Task|Workflow`, registered in `~/.claude/settings.json`).  Hard-denies four delegation mistakes prose never stopped: a subagent spawned with no `model` (it silently inherits the session tier, so a frontier session makes every worker frontier); an unknown tier string; `run_in_background: false` (blocks the owner out of their own conversation while a worker runs); and a doubled worktree (`isolation: "worktree"` alongside a prompt that also runs `git worktree add`).  For workflows it additionally denies a script that assigns no model anywhere, and an all-`opus` assignment across 3+ agent() sites.  Deny messages carry the tier guidance and explicitly encourage MORE delegation at the right tier.  Fails open on malformed input.  Backup of prior settings at `~/.claude/settings.json.bak-subagent-economy`.  Added 2026-09-04 (Claude). |
| `~/apps/fleet-rag/fleet-recall-mcp.py` | On-demand (stdio MCP `fleet-recall`, spawned by Claude Code / Cursor / Codex / Grok / Antigravity / Grok ACP per session).  Tools `recall_search` / `recall_contribute` / `recall_stats`.  No daemon.  Since 2026-09-08 (Claude, `fleet_rag/public_fallback.py`), both this server and the `recall` CLI fall back to the public `https://recall.jays.services` REST twin when Tailscale is down on this Mac (checked via `tailscale status`) or the Tailscale-only Qdrant/TEI path returns a connection-level error — needs `RECALL_API_TOKEN` (`~/.secrets/global-api-keys`) and `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` (`~/.secrets/agents-jays-services-access-service-token.env`); missing/rejected credentials print one actionable line instead of a `RemoteDisconnected` traceback.  Canonical: `docs/RAG-FLEET-INFRA.md`. |
| BotFleet routines `Fleet RAG nightly ingest` (02:30 daily) + `Fleet RAG weekly health + recall eval` (Sun 06:30) | Always-on (BotFleet bot Oracle, `~/.botfleet/routines.json`, loopback API `127.0.0.1:8799/api/routines`).  Nightly: `recall ingest --all --prune` + sentinel; the Hetzner `fleet-health-verify.sh` pages when the sentinel is >30 h old.  Weekly: `recall doctor --platforms --box`, eval, digest → Apple Note "[FLEET, Oracle] Weekly recall digest".  Added 2026-09-01, updated 2026-09-02 (Claude).  Re-created Tue, Sep 8, 2026 (Claude, same names and schedules, new ids `6d74f498` / `5a4202d4`) with a preflight step: run `recall doctor --platforms` first and, when the sentinel row says the direct path is skipped because Tailscale is logged out, open `recall-tunnel up` and prefix the recall commands with the three tunnel URLs, then `recall-tunnel down`.  The nightly had failed every night since Sep 3 for exactly that reason. |
| `~/Code/AI-Fleet-Coordinator/scripts/install-fleet-rag.sh` | On-demand.  Copies `fleet_rag/` + `recall` + MCP server into `~/apps/fleet-rag/`, registers `fleet-recall` in Claude / Cursor / Gemini / Codex / Grok MCP configs (no tokens), optionally copies seat-mcp recall tools.  Does **not** restart `qdrant-st`.  `--with-seat-mcp` prints `pm2 restart seat-mcp` but does not run it. |
| `~/Code/AI-Fleet-Coordinator/scripts/install-fleet-skills.py` | On-demand.  Installs fleet skills into `~/.gemini/skills`, `~/.cursor/skills`, `~/.claude/skills`, `~/.codex/skills`, and `~/.grok/skills`, specializing Slack tag / Notes name / branch prefix / worktree suffix per seat. |
| `~/apps/fleet-gdrive-backup/run.sh` | Also on-demand.  Same Drive backup as launchd `com.jay.fleet-gdrive-backup` (repo zips + agent-config mirror).  `--list` prints the repo set.  `--only Repo` zips one app.  Tracked `scripts/backup-fleet-to-gdrive.py` + `scripts/sync-fleet-agent-config-to-gdrive.py`. |
| `~/apps/botfleet-server-start.sh` | Always-on launchd entry for `com.jay.botfleet-server`.  Exits 0 if `127.0.0.1:8799/health` is already 200.  Tracked `scripts/botfleet-server-start.sh`. |
| `~/apps/cursor-mac-process-hook.sh` | On-demand.  Fast pm2 always-on check (8s cap, fail open, no secrets).  Cursor `sessionStart` user hook (`~/.cursor/hooks/mac-process-check.sh`) calls this and injects `additional_context` when jobs are down. |
| `~/apps/cursor-chat-surfaces/` | On-demand.  Grok Bot / Shellular Cursor → desktop Agents Window + iOS.  Grok Bot is already Cloud Agents (open Agents Window; Filter → Source → SDK if hidden).  Shellular id `cursor` is the cloud bridge (`session/list` = Cloud Agents, never execs `cursor-agent`).  id `cursor-local` is `cursor_acp_noauth_shim.py` wrapping `cursor-agent acp` (strips `authMethods` so iOS 0.0.43 does not show Authentication required).  Command: `~/apps/cursor-chat-surfaces/cursor-chat-surfaces status`.  Canonical: `docs/CURSOR-CHAT-SURFACES.md`.  Key: `CURSOR_SYNC_API_KEY` in `~/.secrets/global-api-keys`.  Do **not** add this to the 15 always-on pm2 jobs. |
| `~/apps/harness-runtime/` | Symlink → `~/Code/Harness/` (`jaywedgeworth22/Harness`).  **Never `npx @deepseek-ai/dsh`.**  Web UI is always-on pm2 `harness-web` on **:3080**.  Dock app: `~/Applications/Harness.app` (bundle id `com.jays.dsh-harness-web`).  Rebuild: `~/apps/harness-runtime/scripts/install-dock-app.sh`.  Root shims `dsh-acp.sh` / `mmh-acp.sh` are Shellular ids `deepseek` / `minimax`.  `~/apps/dsh-runtime/` is the 2026-09-19 rollback copy. |
| `~/apps/cursor-chat-surfaces/cursor-machine-worker.sh` | On-demand.  `agent worker start --name jay-mac` so Cloud Agents can run tools on this Mac.  Only when `CURSOR_BRIDGE_ON_MAC=1`. |
| `~/apps/pm2-ecosystem.config.cjs` | pm2 definitions for the 15 always-on fleet jobs (incl. `mac-collab-sync`, `mac-collab-writeback`, `mac-collab-litestream`, `seat-mcp`).  Tracked `scripts/pm2-ecosystem.config.cjs`. |
| `~/apps/mac-collab/start_litestream.sh` | Always-on pm2 entry for `mac-collab-litestream`.  Loads B2 replica credentials from `~/.secrets/global-api-keys` into env (never print) and execs `litestream replicate -config litestream.yml`.  Tracked `scripts/mac-collab/start_litestream.sh`. |
| `~/apps/mac-collab/litestream.yml` | Litestream config for THE BOARD sqlite: local `~/apps/mac-collab/findings.db` → B2 `jays-fleet-shared-eu` prefix `mac-collab/findings.db`.  **Not** ST's Coolify Litestream.  Tracked `scripts/mac-collab/litestream.yml`. |
| `~/apps/mac-process-watch.sh` | Scheduled down-watch + always-on restarter (also launchd).  Tracked copy: `AI-Fleet-Coordinator/scripts/mac-process-watch.sh`. |
| `~/apps/agy-acp-runtime` | Pinned `@rebornix/stdio-to-ws` for `agy-acp`. |
| `~/apps/seat-mcp/start.sh` | On-demand / already-running.  seat-mcp on `127.0.0.1:8793`.  Token `~/.secrets/seat-mcp.env`.  Public hostname `agents.jays.services` (Access).  v1.1 tools: `grok_sessions_list` / `grok_session_peek` / `grok_session_prompt` for live TUI attach.  Do not start a second copy if 8793 is listening.  `ecosystem.snippet.cjs` exists; do not `pm2 start` / `pm2 save` from a one-off. |
| `~/apps/mcp-servers/seat-mcp-launch.sh` | On-demand.  stdio proxy into local seat-mcp.  Grok TUI `~/.grok/config.toml` + Cursor `~/.cursor/mcp.json` point here.  Token never in those files. |
| `~/apps/agy-acp-runtime/agy-acp-turbo.sh` | Shellular Antigravity ACP wrapper (`--dangerously-skip-permissions --mode accept-edits --skip-naration`). Symlinked to `~/.local/bin/agy-acp-turbo`. |
| `~/apps/grok-acp-runtime` | Pinned Grok ACP adapter.  localhost `127.0.0.1:12419` only.  Never `:2419`. |
| `~/apps/grok-acp-runtime/leader.sh` | pm2 `grok-leader` entry.  Shared backend on `~/.grok/leader.sock`.  Exits 75 when the socket is already bound.  Tracked copy: `AI-Fleet-Coordinator/scripts/grok-leader.sh`. |
| `~/apps/grok-acp-runtime/leader-client.py` | `handshake` / `list` / `load` / `peek` / `prompt` / `close` over leader stdio (no extra packages).  Tracked `scripts/grok-acp-runtime/leader-client.py`. |
| `~/apps/grok-acp-runtime/grok-drive.py` | On-demand.  Any-seat CLI: `list` / `peek` / `tail` / `prompt` / `await` / `cancel` / `close` / `new`.  `list` merges `active_sessions.json` `live=true` plus `turnState` and `pendingTool`.  Prompt uses `session/resume`.  `close` is `session/close` (unload MCP, keep disk).  Refuses `$GROK_SESSION_ID` unless `--self`.  Tracked `scripts/grok-acp-runtime/grok-drive.py`. |
| `~/apps/grok-acp-runtime/grok-idle-unload.py` | On-demand / hourly launchd.  Close live chats idle >12h.  `--dry-run` lists only.  `GROK_IDLE_UNLOAD_HOURS` overrides.  Tracked `scripts/grok-acp-runtime/grok-idle-unload.py`. |
| `~/apps/grok-acp-runtime/session_disk.py` | On-demand.  Disk peek / turnState / `poll_after_inject`.  Tracked `scripts/grok-acp-runtime/session_disk.py`. |
| `~/apps/mcp-servers/seat-mcp-stdio-proxy.py` | On-demand.  stdio → `127.0.0.1:8793/mcp`.  Reads `SEAT_MCP_TOKEN` from `~/.secrets/seat-mcp.env`.  Never on argv.  Tracked `scripts/mcp-servers/seat-mcp-stdio-proxy.py`. |
| `scripts/install-grok-tui-drive.sh` | On-demand.  After an AFC merge, copy tracked `scripts/grok-acp-runtime`, `scripts/seat-mcp`, and `scripts/mcp-servers/seat-mcp-*` into `~/apps/`.  `--restart-seat-mcp` only if pm2 already owns the job.  Do not bind a second `:8793`. |
| `~/apps/grok-acp-runtime/start.sh` | pm2 `grok-acp` entry: `--no-leader serve --bind 127.0.0.1:12419`.  Sources `~/.secrets/grok-acp.env`. |
| `~/apps/grok-acp-runtime/acp-client.py` | Conductor WS client for **new** sessions on `:12419`.  `--mcp-server NAME` from user `~/.grok/config.toml`.  Auto-approves `session/request_permission`, implements ACP `terminal/*`, one WS pump, NDJSON `event=session` as soon as `session/new` returns. |
| `~/apps/grok-acp-runtime/README.md` | Leader + Conductor + Shellular attach notes. |
| `~/apps/cursor-slack-ws-sync.py` | Cursor #agent-sync Socket Mode inbox (also launchd). |
| `~/apps/botfleet-imessage-relay.py` | BotFleet 12-bot iMessage bidirectional relay (helper).  Inbound POST `{text, source:"imessage"}`.  Outbound only if the bot text starts with `[to iMessage]`; the tag is stripped.  launchd `com.jay.botfleet-imessage-relay` is Retired/Forbidden under jay — never load it.  On-demand / agents-account only. |
| `~/apps/update-botfleet.sh` | On-demand.  **Replaced Sat, Sep 12, 2026 6:50pm CT (Claude)** with the repo's transactional updater: a copy of `scripts/update-botfleet.sh` from jaywedgeworth22/BotFleet at `3b30294e`, which execs the tracked `~/apps/botfleet-server/scripts/update-botfleet-mac.mjs` so the installed helper and its tests cannot drift apart.  The old non-transactional script (move checkout, `rm -rf /Applications/BotFleet.app`, `cp -a`, `kickstart`) is kept at `update-botfleet.sh.bak-20260912-safe-updater`.  No update was run as part of that install. |  Launchers are unchanged: terminal alias `ubf` (`~/.zshrc`) and the double-click wrapper `~/apps/Update BotFleet.command` both still call this path, and a bare run is still the `update` command.  What changed for the owner: an update now **prepares** (fetch, pin a commit reachable from `origin/main`, `pnpm install --frozen-lockfile` and package inside a throwaway worktree under `~/Library/Caches/BotFleet/updates/<stage>/`, then verify signature, Team ID, bundle ID, build identity, and dependency fingerprint) and only then **applies** under an authenticated quiesce — it refuses while work is in flight, fences API mutations and webhook deliveries, and sends `SIGTERM` only to BotFleet PIDs it verified first, never `SIGKILL`, `pkill`/`killall`, or the old Finder / Dock / System Settings / icon-cache cleanup.  Any install or startup failure **rolls back** the prior app bundle, dependency tree, and checkout commit.  Receipts and exactly one verified rollback generation live under `~/Library/Caches/BotFleet/updates/<audit>/`; `~/apps/update-botfleet.sh unquiesce` releases the admission fence if an interrupted run left it held.  Sub-commands: `update` (default), `prepare --target/--source/--stage [--bundle --dependencies]`, `apply --stage`, `unquiesce`; also `--no-open` and `--help`.  Carry-overs from the old script, checked at install: the Sentry DSN is no longer exported by the wrapper because the tracked updater packages through `scripts/with-sentry-dsn.sh`, which resolves `SENTRY_DSN` / `VITE_SENTRY_DSN` from the environment, then Infisical, then the same chmod 600 handoff file, disables xtrace first and never prints a value; the codesign timestamp retry is unnecessary because the new build always passes `-c.mac.timestamp=none`; and the retired `ubf --force` flag is swallowed by a small compat shim in the installed copy (the transaction has no "already at `origin/main`, skip" early exit — every run prepares and applies — and the flag would otherwise abort the run as an unknown option).  Three behaviour changes worth knowing: packaging now demands the exact `Developer ID Application: Jay Wedgeworth, LLC (CC8UTF7ATG)` identity with no Apple Development or ad-hoc fallback (present on this Mac, verified Sep 12); `apply` refuses while `~/apps/botfleet-server` shows **any** `git status --porcelain` output, untracked files included, whereas the old script first restored its own three generated paths and ignored untracked files — that checkout carried a modified `electron/vendor/electron-updater.cjs` at install time, so it must be cleaned before the first run or the update will build for minutes and then refuse; and the wrapper runs whichever implementation the live checkout holds, which is currently older than `3b30294e`, so the first run uses the pre-PR-#371 rollback placement and every run after that uses the merged one.  Rollout doc: `docs/rollouts/2026-09-12-safe-mac-updater.md` (PR #325; rollback placement and pruning fixed in PR #371 = `3b30294e`).  Tests green at that commit: `scripts/mac-update-transaction.node-test.mjs`, `scripts/update-botfleet-mac.node-test.mjs`, `scripts/mac-update-rollback-placement.node-test.mjs` (50 passing).
| `~/apps/imessage-grok-listen.py` | Grok iMessage group listener.  launchd disabled until FDA; Aqua orphan is the live process. |
| `~/apps/slack-agent-listen.py` | Slack DM multi-seat inbox.  Also launchd `com.jay.slack-agent-inbox`. |
| `~/apps/slack-agent-listen-start.sh` | launchd entry: sources agent-sync.env, execs the inbox. |
| `~/apps/slack-grok-listen.py` | Wrapper that execs `slack-agent-listen.py` (old Grok-only name). |
| `~/apps/slack-agent-inbox.md` | How to DM any seat from Slack. |
| `~/apps/xcode-health/xcode-health-server.py` | xcode.jays.services health (also launchd). |
| `~/vision-worker/run-vision-worker.sh` | CT scanned-PTR vision worker (also launchd). |
| `~/vision-worker/worker.py` | Vision worker body.  **Hand-copied deploy** of `Congress.Trade/services/vision-worker/worker.py` — merging a PR does **not** ship it.  After any vision-worker PR merges: `cp` the repo file here, `python3 -m py_compile`, run `python3 -m unittest discover -s ~/vision-worker -p 'test_worker.py'`, then `pm2 restart vision-worker --update-env`.  Drifted 31 commits behind `main` on 2026-08-21 because this was undocumented. |
| `~/apps/ios-fleet/ios-debug.sh` | On-demand.  Xcode-console equivalent without opening Xcode: simulator `simctl launch --console` + `log stream`, or device `devicectl` launch / screenshot + `log collect`.  `--target device --install-debug` replaces TestFlight for that bundle.  `--logs-only` does not.  Tracked copy: `AI-Fleet-Coordinator/scripts/ios-debug.sh`.  Canonical: AGENT-SYNC § iOS agent build loop (2026-08-21). |
| `~/apps/ios-fleet/ship-testflight.sh` | Archive + upload one iOS app to TestFlight.  Consults `build-window.sh` and re-execs itself under `nice -n 10` when the answer is `background`.  Never blocked, only deprioritised.  `IOS_SHIP_RENICED` guards against a re-exec loop; `IOS_SHIP_PRIORITY=normal` forces full speed.  **On-demand.**  After a successful archive: `sentry-cli debug-files upload` + `sentry-cli build upload` (Size Analysis) using `SENTRY_AUTH_TOKEN`; injects `SENTRY_DSN` into xcodebuild when set.  Sentry failures warn, they do not fail the ship.  GitHub-hosted ships use each repo's vendored `scripts/ios-fleet/` pin, not this path. |
| `~/apps/ios-fleet/build-window.sh` | On-demand.  Decides iOS ship PRIORITY on this shared Mac; prints `normal` or `background`, always exit 0.  **socratic**: prefers X:15-X:45 (strategy runs own X:00-X:15, X:45-X:00 is buffer), unrestricted 20:00-06:00 local.  **congress**: any time unless CT has a non-zero `ingestion_backlog` on its public health route.  Others unconstrained.  FAILS OPEN to `normal` on any error - a broken check must never degrade a ship.  Inherited by `ship-all.sh` and `ship-now-gui.sh` since both call `ship-testflight.sh`.  Tune with `IOS_SHIP_WINDOW_START_MIN` / `_END_MIN` / `IOS_SHIP_QUIET_START_HOUR` / `_END_HOUR`. |
| `~/apps/ios-fleet/ship-all.sh` | Sequential TestFlight ship for the fleet. |
| `~/apps/ios-fleet/ship-now-gui.sh` | GUI-session ship (same as the login LaunchAgent). |
| `~/apps/ios-fleet/asc-api.mjs` | App Store Connect API helper (JWT, no secret print). |
| `~/apps/ios-fleet/block-xcode-project-writes.py` | Guard against hand-editing `.pbxproj`. |
| `~/apps/ios-fleet/fix-runner-aqua-session.sh` | Re-attach Mac Xcode runners to Aqua. |
| `~/apps/code-main-keeper.sh` | One-shot ff-only `~/Code/*` → origin/main. |
| `~/apps/code-main-keeper-daemon.sh` | Loop wrapper (meant to be the pm2 job). |
| `~/apps/check-hetzner-cx43.sh` | Daily cheaper-8-vCPU watch for live host `159792099` (also cron). |
| `~/apps/mac-auto-cleanup.sh` | One-shot cleanup with CleanMyMac CLI & Xcode/developer cache pruning (also 4h LaunchAgent).  `--pressure` for resource-watch wakes. |
| `~/apps/mac-resource-watch.py` | 5-minute disk/RAM/CPU sampler + Housekeeper webhook.  Also `~/apps/mac-resource-watch.sh`.  Tracked `scripts/mac-resource-watch.py`. |
| `~/.claude-disk-janitor/janitor.sh` | Disk janitor body with low-disk CleanMyMac sweep (also launchd every 30 min).  Tracked copy `AI-Fleet-Coordinator/scripts/disk-janitor.sh`.  **2026-09-08 (Claude):** the low-free cache sweep no longer `rm -rf`s `~/.cache/uv` (was stranding running `uvx`-launched MCP servers — Hetzner, alpaca, fmp, sentry, pinecone, uptimerobot, coolify); both `uv cache prune` and the more destructive `uv cache clean` (only below a new `CRIT_FREE` 30G floor) are now skipped and logged whenever a bounded (8s, no hang) `pgrep`/`lsof` check shows the cache still in use — a bot review caught that `prune` alone could still evict a live archive-v0 env. |
| `~/.claude-merge-shepherd/run.sh` | Merge-shepherd body (also launchd every 30 min). |
| `~/apps/cloud-runner-director.sh` | On-demand | Fleet GitHub Actions cloud runner monitor & traffic director. Tracks 5-slot macOS cloud runner capacity, detects queue congestion, flags and auto-cancels stalled/superseded runs, and alerts Slack `#agent-sync`. Tracked copy: `Socratic.Trade/scripts/cloud-runner-director.py`. |
| `~/Code/Usage-Monitor/scripts/ops/mac-server-watchdog.sh` | Mac heartbeat (also launchd every 120 s). |
| `~/Code/Usage-Monitor/script/build_and_run.sh` | **On-demand.**  Builds, stages, ad hoc signs, runs, and optionally installs the native Usage Monitor Mac app into `~/Applications/Usage Monitor.app`.  App reads local agent subscription quotas and an optional Usage Monitor server every 5 minutes only while open; menu bar, Dock, or both.  Publishes a private quota-only snapshot for BotFleet; readers expire it after 10 minutes.  No LaunchAgent or login item.  CODEX 2026-09-13. |
| `~/Code/Usage-Monitor/scripts/antigravity-usage-collector.mjs` | AG quota collector (also launchd every 4 h). |
| `~/Code/Usage-Monitor/scripts/codex-usage-collector.mjs` | Codex session JSONL collector (also launchd every 15 min once bootstrapped). |
| `~/Code/Usage-Monitor/scripts/grok-usage-collector.mjs` | Grok Build updates.jsonl collector (also launchd every 15 min once bootstrapped). |
| `~/Code/Usage-Monitor/scripts/copilot-usage-collector.mjs` | Copilot CLI session JSONL collector (also launchd every 15 min once bootstrapped). |
| `~/Code/Socratic.Trade/scripts/sync-provider-knobs.sh` | Provider-knob sync.  launchd job is scheduled every 30 min (template comment still breaks `plistlib`). |
| `~/Code/Congress.Trade/scout/run-residential-proxy.sh` | Live Mac residential proxy runner (`0.0.0.0:3128` / Tailscale `100.113.106.39:3128`). Owned by pm2 `residential-proxy`. |
| `~/apps/scout-runtime/run-scout.sh` | **Retired** — Mac scout replaced by server-directed scraping via Tailscale `residential-proxy`. |
| `~/Code/Congress.Trade/scout/run-scout.sh` | **Retired** — Git copy of old scout runner. |
| `~/Code/Congress.Trade/scout/run-senate-relay.sh` | Git copy. Live Mac origin is `~/apps/senate-relay-runtime/run.sh`. |
| `~/Code/Congress.Trade/scout/run-senate-tunnel.sh` | Senate tunnel (also pm2). |
| `~/apps/agent-sync-push/start.sh` | #agent-sync relay (also pm2). |
| `~/apps/mcp-servers/*-launch.sh` | Per-session MCP launchers.  Die with the parent.  Codex `mcp_servers.github` / `mcp_servers.render` must use `github-mcp-launch.sh` / `render-mcp-launch.sh` — never inline `mcp-remote --header` in `~/.codex/config.toml` (JSON-style `\\"` is a TOML parse error; Codex then cannot load any config).  2026-09-17. |
| `~/apps/congress-publish.sh` | **Stale** — still SSHes the decommissioned Oracle box.  Do not run.  Use Coolify / `app/scripts/ship.sh`. |
| `~/apps/socratic-publish.sh` | **Stale** — Oracle.  Do not run. |
| `~/apps/trading-publish.sh` | **Stale** — Oracle.  Do not run. |
| `~/apps/usage-publish.sh` | **Stale** — Oracle.  Do not run. |
| `~/apps/sync-post-pr-626.py` | One-off leftover.  Do not run. |

## Vendor / login-item (not agent-owned)

Always-on or scheduled, but agents did not install them.  Do not kill or
"clean up" these.

| Name | Kind | What it is |
|---|---|---|
| Wallspace, GeminiAppLauncher, Devly, Kimi, Google Drive, Dockspace | Login item | Aqua apps. |
| `io.tailscale.ipn.macsys.login-item-helper` | Always-on | Tailscale. |
| `com.macpaw.CleanMyMac5.{Menu,HealthMonitor,Updater}` | Always-on / scheduled | CleanMyMac. |
| `com.microsoft.update.agent` | Scheduled | Microsoft updater. |
| `com.anthropic.claudefordesktop.ShipIt` | Scheduled | Claude Desktop updater. |
| `com.fiplab.mc3loginhelper` | Always-on | Memory Clean login helper.  (Its launchd **GUI instance label** is `application.com.fiplab.memoryclean3`, which is why label-based audits report it as a false gap — match on bundle name too.) |
| `com.adobe.ccxprocess` | Always-on (vendor) | Adobe Creative Cloud Experience process.  Agents did not install it — do not kill or clean up.  Added to the list 2026-09-01 (Claude) after a label cross-reference found it undocumented. |
| `com.github.domt4.homebrew-autoupdate` | Scheduled | Homebrew autoupdate (also listed above). |
| `com.google.GoogleUpdater.wake` | Scheduled | Google updater (also listed above). |

---

## Retired / not loaded

| Item | State |
|---|---|
| `~/apps/com.jay.code-main-keeper.plist` | Template on disk; **not** in LaunchAgents.  Sibling `*.retired-launchd`. |
| `~/Library/LaunchAgents/com.botfleet.imessage-listener.plist` | Always-on | iMessage listener running on the `agents` macOS user account (triggers `/Users/agents/apps/imessage-agent-listen.py --agent grok`). |
| `com.jay.shellular` | launchd **disabled** 2026-08-12.  Replaced by pm2 + `~/apps/shellular-runtime`. |
| `actions.runner…trading-live-mac` | Old Mac CI label.  Still in disabled-list as enabled-but-unused.  **Banned** for GitHub Actions jobs. |
| `actions.runner…mac-xcode26-socratic` | Retired 2026-08-24.  LaunchAgent uninstalled, plists removed, de-registered from GitHub. |
| `actions.runner…mac-xcode26-congress` | Retired 2026-08-24.  LaunchAgent uninstalled, plists removed, de-registered from GitHub. |
| `actions.runner…mac-xcode26-usage` | Retired 2026-08-24.  LaunchAgent uninstalled, plists removed, de-registered from GitHub. |

---

## Not in this list (on purpose)

- Interactive `grok` / `claude` / `codex` / `cursor` terminals
- Per-session MCP children (`npm exec` MCP servers).  Those die with the parent.
  Orphans with `ppid=1` are leaks — kill those, not KeepAlive jobs.
- Coolify / Hetzner app containers
- iOS Simulator
- Apple system LaunchAgents

---

## How to add a row

1. Prefer `~/Library/LaunchAgents/com.jay.<name>.plist` or a `~/apps/<tool>-runtime` + pm2 job.
2. Label, program path, KeepAlive / interval, log path, one-line purpose, Kind.
3. Never put secrets in the plist.  Inject via Infisical or `~/.secrets/` at runtime, value-blind.
4. Update **this file first**, then `--update` the Apple Note, then the coordinator repo mirror.
5. Do not duplicate the full table into every app's AGENTS.md — point here.

```bash
launchctl print gui/$(id -u)/<Label>
launchctl kickstart -k gui/$(id -u)/<Label>
launchctl bootout gui/$(id -u)/<Label>
pm2 status
bash ~/apps/mac-status.sh
```

### pm2 orphan-holds-port recovery (2026-08-21, CLAUDE)

**Symptom.** One or more pm2 apps sit `errored` with a large `restart_time`, their
error log repeats `Address already in use` / `EADDRINUSE` / Deno `AddrInUse (os error 48)`,
and `pm2 jlist` / `pm2 restart` time out.

**Cause.** When the pm2 God daemon dies without reaping its children (memory pressure,
jetsam, a crash), the children are reparented to launchd and keep their listening socket.
The pm2-managed replacement can then never bind, so pm2 restart-loops it forever.  Nothing
inside that loop can clear the orphan.

**Recovery** (script: `~/apps/mac-collab/`-independent, see the CLAUDE rollout note):

1. `launchctl bootout gui/$(id -u)/com.jay.mac-process-watch` -- pause the watchdog so its
   own `pm2 resurrect` cannot race the repair.  **Re-arm it afterwards with `launchctl
   bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jay.mac-process-watch.plist`.**
2. Record God's pid (`~/.pm2/pm2.pid`) and `pgrep -P <god>` BEFORE signalling anything --
   once the parent dies the survivors reparent and you lose the list.
3. `pm2 kill`, then SIGTERM (SIGKILL after ~6s) every recorded child plus every listener on
   a fleet port.  **Fleet ports: 8765 agy-acp, 8791 xcode-health, 8792 mac-collab, 8793 seat-mcp,
   8899 senate-relay, 12419 grok-acp.**  Missing a port here is how senate-relay stayed
   broken through the first pass of this repair (238 restarts against an orphan deno on 8899).
4. `rm -f ~/.pm2/{rpc.sock,pub.sock,pm2.pid}` -- stale IPC is why a freshly started God
   still answers nothing.
5. `pm2 resurrect` (from `~/.pm2/dump.pm2`), then `pm2 jlist` to confirm 14/14 online.
6. `pm2 reset all` so the next crash-loop is visible against a zero baseline, then `pm2 save`.

**Do not** SIGKILL God first and sort it out afterwards -- that is precisely how the orphans
are created.

| BotFleet release validation helpers | On-demand / hosted CI | Repository `scripts/regenerate-mac-feed.mjs`, `scripts/verify-release-assets.mjs`, `scripts/verify-release-tag.mjs`, and `scripts/verify-release-blockmap.py` (PR #334; Linux feed follow-up #285).  Validate Mac/Windows installers and both Linux feed formats, asset bytes, BLAKE2b-144 blockmap chunks, and exact release tags.  No LaunchAgent or recurring local job; hosted dual-architecture Mac signing/notarization passed run 34686539553 attempt 2. | Source validation; no published release or new installed Mac receipt | <!-- botfleet-release-validation-helpers -->

| BotFleet board audit | On-demand | Repository `scripts/audit-effort-board.mjs` reads explicit board/GitHub snapshot files and reports duplicate candidates, stale statuses, missing issue links, and missing surface rollout receipts.  It has no network client or writeback mode.  Added under BotFleet #286; no LaunchAgent or recurring job. | Source validation | <!-- botfleet-board-audit-helper -->

| Simple With Us public catalog link audit | On-demand / hosted CI | Repository `Simple-With-Us/scripts/audit_catalog_links.py` checks the public `apps/index.json` manifest, generated homepage/detail links, support/privacy routes, local fragments, and anonymous HTTP destinations.  The GitHub workflow runs weekly or by manual dispatch, uploads a report and job summary, and has no local daemon, credentials, alerts, or production writeback. | Source validation; review `ok`, `unverified`, and confirmed `broken` results before changing a public CTA | <!-- simple-with-us-catalog-link-audit -->
