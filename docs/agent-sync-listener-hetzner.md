# agent-sync listener on Hetzner (cutover plan)

Owner direction 2026-10-21: move the Zulip listener/poller off the Mac onto the
Hetzner box (`host.jays.services`, tailnet `server.boa-roygbiv.ts.net`), keep the
Cloudflare MCP bridge as the service plane, keep Mac-bound surfaces (Apple
Notes, iMessage boundary, launchd helpers, Muster) on the Mac, and keep Jay's
macOS AppleScript banners for wake events via a thin Mac client.  Jay accepted
5-10 minutes of listener downtime at cutover.

## What moves

- The listener daemon (`agent_sync` daemon: live inbox, wake ledger, held-back
  queue, leases) and its signed read API (`listener_api.py`, port 8788).
- Nothing about the bridge changes; seats keep using the Worker MCP as today.

## What appears

- `agent_sync/wake_alerter.py` (this PR):  the thin Mac client.  It polls the
  read API's `wakes` record once a minute over the tailnet, signed with the
  seat's routine HMAC key, and pops one banner per new wake id.  It holds no
  Zulip key, no Infisical access, no listener state.
- `scripts/launchd/com.jay.agent-sync-wake-alerter.plist`:  launchd unit for
  the alerter.  Env comes from `~/.secrets/agent-sync/wake-alerter.env`
  (0600):  `AGENT_SYNC_SEAT`, `AGENT_SYNC_LISTENER_URL`, `<CODE>_ROUTINE_KEY`.

## Hetzner side (Coolify, "Fleet Infra" project)

- New Coolify app from this repo (main branch, webhook deploy like the other
  Fleet Infra apps):  base directory `scripts/`, start
  `python3 -m agent_sync.listener_cli daemon run` (the daemon's normal entry),
  Python 3.11+ image.
- Bind the read API to the TAILSCALE interface only (no Traefik, no public
  port, no DNS):  `server.boa-roygbiv.ts.net:8788`.
- Runtime env (runtime-only - Coolify bakes "buildtime" vars into build logs;
  secrets must never be buildtime):  the spare Infisical machine identity
  (`INFISICAL_CLIENT_ID` / `INFISICAL_CLIENT_SECRET`, scoped read on `/zulip`
  plus `/ghr` per the toolbox plan) and the listener's normal config env.
- State root on a Docker volume (wake ledger, cursors, notify state).
- Deploy waits on GHR being live (owner sequencing) and on the Infisical
  console steps, which are Jay's.

## Cutover checklist (Mac lane + server lane)

1. Server:  Coolify app deployed, daemon green (`listener_cli status --probe`),
   read API answering on the tailnet address for one seat key.
2. Mac:  install the alerter plist + `~/.secrets/agent-sync/wake-alerter.env`;
   `launchctl bootstrap`, verify one banner arrives from a test wake.
3. Verify the hosted listener's cursors are at or past the Mac listener's
   cursors (or accept a bounded catch-up replay - held-back/dedup keeps it
   quiet).
4. Stop the Mac listener unit (bootout), start of the 5-10 min window.
5. Watch `#alerts` and the wake ledger for 30 min:  mentions/DMs landing,
   banners firing, no duplicate wakes.
6. Only after a clean soak:  remove the old Mac listener unit file and any
   now-unused Mac-side Zulip keys (each deletion is a separate, explicit
   owner step - nothing is deleted during the move itself).
