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

- `agent_sync/wake_alerter.py` (this PR):  the thin wake client, two modes.
  Default (the Mac):  one macOS banner per new wake id, bodies never reach a
  banner.  Hook mode (`AGENT_SYNC_ALERTER_HOOK` set):  run one configured
  command per new wake instead - this is the headless-wake path for
  server-side seats.  Both modes hold no Zulip key, no Infisical access, no
  listener state.
- `scripts/launchd/com.jay.agent-sync-wake-alerter.plist`:  launchd unit for
  the Mac alerter.  Env comes from `~/.secrets/agent-sync/wake-alerter.env`
  (0600):  `AGENT_SYNC_SEAT`, `AGENT_SYNC_LISTENER_URL`, `<CODE>_ROUTINE_KEY`.

## Server-side seats are the main clients

Owner direction 2026-10-21:  every agent platform (Claude, Codex, Muse Code,
and the rest) runs on the Hetzner server, and they all communicate with the
Operator.  The Operator's decision-making LLM agent also runs on the server.
So the layout is:  Operator decides, GHR and agent-sync execute, and the
server-side seats are the main clients of both.  The Mac is a minor client -
its role narrows to human-facing alerts (the AppleScript banners) and the
Mac-bound surfaces listed above.  (Working assumption until Jay names
otherwise:  "the Operator" is the OPS-966 Operator component - the AFC
repo-token routing/dispatch plane - not a new piece.)

## Headless wake:  launching Muse Code from a wake event

For a seat hosted on the server (Muse Code is installed there, per Jay), a
wake event should be able to start real work without a human screen.  The
wake alerter's hook mode provides it:

- A supervisor on the server runs
  `python3 -m agent_sync.wake_alerter --loop 60` with
  `AGENT_SYNC_ALERTER_HOOK=/opt/fleet/muse-wake.sh {seat} {wake_id} {channel} {topic}`.
  The hook receives wake METADATA only ({seat}/{channel}/{topic}/{wake_id}/
  {state} argv placeholders plus AGENT_SYNC_WAKE_* env vars) - never message
  bodies, same rule as the banners.  A wake id is marked seen only when the
  hook exits 0, so a failed launch retries on the next poll (bounded by the
  since_hours window and the state file cap).
- The hook wrapper launches Muse Code headlessly
  (https://ai.developer.meta.com/docs/muse-code.md):  non-interactive auth is
  `META_API_KEY` (runtime-only env, never buildtime - Coolify bakes buildtime
  vars into build logs), and one prompt runs to completion:

      #!/bin/sh
      # /opt/fleet/muse-wake.sh SEAT WAKE_ID CHANNEL TOPIC
      exec muse exec "You are the $1 seat on the fleet.  A wake event arrived
      for you in $3 / $4 (wake $2).  Read the event through your normal
      agent-sync MCP bridge on the tailnet and run your wake routine.  Take
      no action outside the seat's lane;  the Operator decides, you execute."

  The prompt points the seat at the event through its normal authenticated
  bridge rather than pasting content into the prompt, which keeps chat text
  out of argv and out of logs.
- Dedupe, state and retry come from the alerter itself;  Muse Code needs no
  wake ledger access.  Multiple seats on the box run one alerter process per
  seat (each with its own seat key and state file).

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
