# Housekeeper playbook (BotFleet + GB-HOUSEKEEPER)

Canonical prompt pasted into BotFleet Housekeeper routines, the resource webhook, and first-class resource triggers.  The bot id is private resource inventory and is not in this pack:  it lives in the operator's own inventory on that Mac.  A real U+00A0 plus a normal space between sentences (owner ruling 2026-10-23), with one exception: the chat panes of Claude Code (the Code tab) and MiniMax Code (the MiniMax Code pane) take the `&nbsp;` entity plus a normal space.

**Ownership (prefer BF).**  BF-Housekeeper owns Mac disk reclaim.  GB-HOUSEKEEPER keeps Coolify/Hetzner until BF proves Mac-independent Coolify wake.  Prefer BF for Mac work.  Extra-ship no.  Do not install Tailscale or change Mac networking.  HOLD MiniMax process kills unless Jay says otherwise.

## Ask-first reclaim

These optional reclaim targets are large and non-regenerable (or costly to rebuild).  Report sizes in chat and wait for **explicit Jay OK** before delete/clear.  Never auto-fire under pressure alone.

| Target | Typical size | Path / note |
| --- | --- | --- |
| Monet / Parall Claude VM / `claudevm.bundle` | ~10 GiB | Delete only with explicit Jay OK |
| Google DriveFS local cache | ~7 GiB | Under `~/Library/Application Support/Google/DriveFS` — clear only with explicit Jay OK |

Also ask Jay before: Documents/photos, secrets, live dirty worktrees, CoreSimulator/Devices, in-session `~/.grok/worktrees`, paths with `.janitor-keep`, FileProvider bulk deletes if destructive.

## Auto without asking (regenerable)

Safe under disk pressure (scripts already encode cooldowns):

1. `python3 /Users/jay/apps/mac-resource-watch.py --once --no-webhook`
2. `bash /Users/jay/apps/mac-auto-cleanup.sh --pressure`
3. `bash /Users/jay/.claude-disk-janitor/janitor.sh` (preferred worktree path — follows janitor defaults)
4. `cleanmymac clean --force` when CLI present — never `cleanmymac optimize ram`
5. Stale caches, brew/npm caches
6. Stale git worktrees only when **all** of: age ≥7d (janitor default, not 3d), branch merged (or equivalent), and no `.janitor-keep` — never live mains / dirty / in-session trees
7. Existing Coolify keep-two / B2 keep-two paths when those routines fire (Coolify may still be GB-owned until BF cutover)

Do **not** authorize deleting worktrees solely for age ≥3d.  Sentry HIGH: match janitor’s 7-day default + merged + `.janitor-keep` checks.

The Datadog disk alert is a three-day forecast (≥99%), not a live reading over 90%.  The data volume improved to about 84%;  still reclaim regenerable waste under pressure.

## Alert Tiers

Owner, Oct 29 2026 (AFC-577):  a mild resource issue alerts both Housekeepers and Jay with @ tags, and a filesystem seriously close to full alerts `@all`.

| Tier | When | Tags | Then |
| --- | --- | --- | --- |
| Mild | A filesystem at least 80% used or under 40 GB free;  load, RAM, swap or CPU past a warning line on other monitors (Datadog monitor 22024794 warnings and recoveries stay quiet);  a database growing fast;  Litestream staging ballooning or replication blocked | `@**BF-Housekeeper|1216054** @**GB-Housekeeper|1215533** @**Jay Wedgeworth|1211974**` | The Housekeeper that owns the host reclaims (above), the other stands by |
| Severe | Datadog monitor 22024794: CPU above 90% for 5 minutes | `@**BF-Housekeeper|1216054** @**GB-Housekeeper|1215533** @**Jay Wedgeworth|1211974**` | Alert ingress sends immediately after the monitor threshold; repeat refreshes do not duplicate; recovery is quiet |
| Critical | A filesystem at least 90% used or under 20 GB free (a small tmpfs such as `/tmp` by percent only) | `@**all**` | The sending bot DMs Jay why, as every bot `@all` must (`scripts/simple_tools/broadcast_audit.py`) |

Load is never a space issue, so even the simulator reaper's emergency post stays mild.  The deployed alert-ingress Worker routes Datadog monitor 22024794 as SEVERE when CPU stays above 90% for five minutes and bypasses the former second ten-minute hold.  OPS-1008's isolated Zulip self-test verified one SEVERE page mentioning both Housekeepers and Jay, duplicate suppression on repeat refreshes, and recovery without mentions.  Mild CPU conditions on other monitors use the tags above; warnings and recoveries from Datadog monitor 22024794 stay quiet, while its SEVERE event uses the Severe row.  Datadog's incoming-webhook notification is distinct from this Worker-generated page; this documents the configured ingress route and observed Zulip delivery, not Datadog-console settings or Jay-device push.

The senders today:

- **The server disk guardrail** (`/usr/local/sbin/fleet-disk-guardrail.sh`, cron every 2 minutes, posts as the Operator to `#alerts` › `host-disk-guardrail`):  root, the fleet volume (the one the host names in `FLEET_SERVER_VOLUME`) and `/tmp`, plus Litestream staging and replication errors.  Mild posts tag Jay beside the Housekeepers.  The script is a host file in no repo yet;  its backup from before AFC-577 is `fleet-disk-guardrail.sh.bak-20261030-afc577` beside it.
- **The server storage watch** (`/root/vps-storage-watch.sh`, cron every 15 minutes, `#alerts` › `vps-storage`):  database growth only, always mild, tagging both Housekeepers and Jay.  It records free space, and the guardrail alerts on it.
- **The simulator reaper** (`scripts/sim-idle-reaper.py`, Mac LaunchAgent, `#Alerts` › `sim-idle-reaper` as MA):  its load-guard post tags both Housekeepers and Jay on a second line, so the first line still starts with its severity.

Repeated mild posts in one topic stop waking BF-Housekeeper after four peer wakes without Jay posting there (BotFleet's loop guard), and GB-Housekeeper takes at most two peer wakes per topic per hour.

## Canonical bot prompt

```
You are Housekeeper.  Prefer BF-Housekeeper for this Mac.  Keep this Mac from running out of disk, RAM, or inodes.  Coolify/Hetzner stays GB until BF non-maus prove.

Act on regenerable waste without asking:
1. python3 /Users/jay/apps/mac-resource-watch.py --once --no-webhook
2. bash /Users/jay/apps/mac-auto-cleanup.sh --pressure
3. bash /Users/jay/.claude-disk-janitor/janitor.sh
4. cleanmymac clean --force (when the CLI exists).  Never `cleanmymac optimize ram`.
5. Stale caches and brew/npm caches.
6. Stale git worktrees only when age≥7d AND merged (or equivalent) AND no .janitor-keep — never age≥3d alone, never live mains, never dirty/in-session trees.  Prefer janitor.sh for worktree retirement.
7. On Hetzner hosts you can reach, run the existing Coolify keep-two / maintenance path only when that routine owns it.  Do not persist TCPMSS.  Do not change sysctl or network settings.  Do not install Tailscale.

Ask-first reclaim (explicit Jay OK required before any delete/clear):
- Monet / Parall Claude VM / claudevm.bundle (~10 GiB)
- Google DriveFS local cache (~7 GiB under Application Support/Google/DriveFS)
- Also ask before: Documents/photos, secrets, live dirty worktrees, CoreSimulator/Devices, in-session ~/.grok/worktrees, paths with .janitor-keep, FileProvider bulk deletes if destructive

HOLD MiniMax kills unless Jay says otherwise.  Playbook: /Users/jay/Code/AI-Fleet-Coordinator/docs/HOUSEKEEPER.md

Report in this Housekeeper chat: disk free before/after, swap, load 1/5/15, what you deleted, and the largest remaining dirs.  Do not extra-ship.  Do not post in Zulip unless kicking grok at a repo or after a completed app update.

If this wake is a resource trigger or webhook, the payload names the metric and sample.  Start with that pressure.  Do not only report.
```

Live Mac jobs:

- `com.jay.mac-resource-watch` — every 5 min; samples disk/RAM/CPU; runs safe cleanup on a disk hit (own cooldown, `--force` is the only bypass); POSTs BotFleet Housekeeper webhook and, if configured, Grok Bot Housekeeper (45 min webhook cooldown)
- `com.jay.mac-cleanup` — every 4 h (`mac-auto-cleanup.sh`)
- `com.jay.disk-janitor` — every 30 min; cache + idle worktree retirement (warn at 80G free, pressure at 65G; worktrees age≥7d + merged + no `.janitor-keep`)  **2026-09-30 (Claude):** a memory/load gate runs first — load1 above `JANITOR_MAX_LOAD` (40) or swap at or above `JANITOR_MAX_SWAP_PCT` (90) logs `PRESSURE-SKIP`, does only the pm2-log and leftover test-DB truncations, and skips every git/gh/worktree phase and all CleanMyMac calls until the Mac settles.

Webhook secrets (never print) resolve from the process environment first, and only a name the environment does not set falls back to Infisical, as `mac-resource-watch.py` does: `BOTFLEET_HOUSEKEEPER_WEBHOOK_URL` and `_SECRET` (required) fall back to Fleet-OPS `/botfleet-housekeeper-webhook`, read with the scoped read-only service token in the owner's login keychain (the generic-password item `infisical-service-token` / `mac-resource-watch-ro`) and with no other credential source; the optional second wake takes `GROK_BOT_HOUSEKEEPER_WEBHOOK_URL` / `_SECRET` from the environment alone, because the token is scoped to the BotFleet folder.  **AFC-609, Wed, Nov 4, 2026 10:50pm CT:** the watcher's log says where each wake's credentials came from and, when they are missing, why, so a skipped delivery is never reported as the one vague line that said "no valid urls/secrets found" about every different failure: `ok`, `env`, `env+infisical`, `no-token`, `keychain-locked`, `token-unreadable`, `absent`, `forbidden`, `http-<code>`, `unreachable`, `timeout`, `truncated` or `bad-output`.  A timeout is retried once, because that is the one failure that is the machine being slow rather than the credentials being wrong.  **AFC-645, Thu, Nov 5, 2026:** the token value moved out of the 0600 file and into the owner's login keychain, so no service-token value is a plaintext file on disk any more.  The keychain is reachable only from the owner's GUI session, so the watcher runs there, and a consumer outside it fails closed with `keychain-locked` rather than silently reaching for another credential.
