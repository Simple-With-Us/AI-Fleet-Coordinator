# Grok idle MCP unload + Hetzner swap for RAM oversubscribe

Board `8247aa02`.  Branch `grok/idle-chat-unload`.  Worktree `~/apps/fleet-grok-idle-unload`.

## Why

Live Grok TUI chats each spawn a full MCP set (GitHub, Sentry, Coolify, Chrome DevTools, …).  One leader tree was ~2.6 GB / 306 procs with 10 live chats.  Idle chats kept those tools resident.

The Hetzner box is 16 vCPU / 30 GiB.  Container RAM caps (TEI 12 GiB, Qdrant 12 GiB, plus ST/CT/UM) are ~40–50% above physical on purpose.  Swap was a 4 GiB file, 3.8 GiB already used, while MemAvailable was still ~22 GiB.  That use was Linux swappiness=60 paging TEI (~2.4 GiB) and Qdrant (~1 GiB) out to keep file cache, not true oversubscribe.

## What landed

### Mac — unload tools, do not delete chats

- ACP `session/close` unloads a live chat's MCP processes and **keeps** `~/.grok/sessions/<cwd>/<id>/`.
- `grok-drive.py close` and `leader-client.py close` wrap that call.
- Hourly launchd `com.jay.grok-idle-unload` closes **live idle** chats whose `updatedAt` is older than **36 hours**.
- Skips working, needs-input, pendingTool, and `$GROK_SESSION_ID`.
- Optional orphan reap: SIGTERM leader-child MCP processes whose `GROK_SESSION_ID` is no longer live and whose session is missing or also idle >36h.  Never SIGKILL.  Never deletes session dirs.
- `/resume` on a closed chat reloads tools.

On-demand:

```bash
python3 ~/apps/grok-acp-runtime/grok-idle-unload.py --dry-run
python3 ~/apps/grok-acp-runtime/grok-drive.py close --session-id ID --cwd DIR
```

### Hetzner — 16 GiB swap, swappiness 20

- Idempotent `scripts/host/ensure-swap.sh`: extra file `/swapfile.extra` so the in-use 4 GiB `/swapfile` is not swapoff'd.  Target 16 GiB total.  `vm.swappiness=20` in `/etc/sysctl.d/99-fleet-swap.conf`.
- Swap is the overflow for cgroup caps above 30 GiB.  It is not a reason to keep paging TEI while 20 GiB of RAM is cache.

## Verification

```bash
python3 scripts/test_session_disk.py
bash scripts/host/test-ensure-swap.sh
python3 ~/apps/grok-acp-runtime/grok-idle-unload.py --dry-run
# host (root):
bash scripts/host/ensure-swap.sh
free -h
cat /proc/sys/vm/swappiness
swapon --show
```

First Mac dry-run is expected to close **zero** live chats (none were 36h idle yet).  Fake `session/close` on a nonexistent id returns `_meta.x.ai/closeOutcome=notResident` and does not create or delete a session dir.

## Follow-ups

- Sharing one MCP set across live TUI chats (Grok still forks per session).
- Do not put agent threads on this same 30 GiB box.
