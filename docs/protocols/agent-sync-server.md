# Agent-Sync Listener:  Server Instance

CLAUDE seat, Thu, Oct 8.  Deployed on Thu, Oct 8 as the Coolify application "agent-sync listener (server)" (project Fleet Infra, environment production, uuid `l40rxd4rbj1pnogmbtetzmsf`), at commit 4761f44.  It does not deploy itself:  see Coolify Setup, step 1.  The listener design is [agent-sync-listener.md](agent-sync-listener.md); this page covers only what the server instance adds.  Fri, Oct 9:  the five cloud seats (MA, JET, GROK-WEB, INSTINCT and ECHO) now have sections, with inbox capture (see [Cloud Seats](#cloud-seats)).

## Summary

The listener runs as two instances of the same package (`scripts/agent_sync`), by owner decision on Thu, Oct 8.

- **mac** is the LaunchAgent on the owner's Mac.  It holds the nine Mac seats (AG, CLAUDE, CLUTCH, CODEX, CURSOR, FX, GROK, MC and MM):  live delivery into Claude sessions and the tool-less `claude -p` wake for CLAUDE, and inbox capture for the other eight.
- **server** is a container on the Coolify box (Hetzner).  It holds the Grok Bot (GB) personas and the five cloud seats (MA, JET, GROK-WEB, INSTINCT and ECHO).  A cloud seat has a queue and inbox capture and no wake, except JET, which is woken through the hosted MCP Worker (see [Cloud Seats](#cloud-seats)).  A GB persona is woken on an @-mention or a DM by calling its Grok Bot routine webhook.  The routine then replies in Zulip with the persona's own key.

BotFleet (BF) bots are handled natively by BotFleet, not by either instance.

The server instance reuses everything in the listener:  the router, dedupe, the budgets, the loop guard, the owner rule, coalescing, the ledger and the kill switch.  It changes four things.  Credentials come from environment variables.  The wake adapter is `http`, a remote routine.  It posts nothing to Zulip itself.  Its notify-owner writes only the owner queue, because there is no display.

## Seat Partition

[`agent-sync-partition.toml`](agent-sync-partition.toml) gives each seat to one instance:  `mac`, `server`, or `none` (no listener holds it).  Today the nine Mac seats (AG, CLAUDE, CLUTCH, CODEX, CURSOR, FX, GROK, MC and MM) are `mac`, and the eleven GB personas plus MA, JET, GROK-WEB, INSTINCT and ECHO are `server`.  The BF role bots and GROK-BUILD (the older code of the GROK seat) are `none`.  The server sample enables the five cloud seats (since Fri, Oct 9), MA, GROK-WEB, INSTINCT and ECHO with `wake = "inbox"` and JET with `wake = "http"` into the hosted MCP Worker.

**A seat must never be configured in both instances.**  If it were, two listeners would each hold a queue for the same bot and each wake it, so one message could be answered twice.  The partition fails closed, and it is enforced at start, at connect and on reload.

1. `[daemon] instance` in `listener.toml` is `"mac"` (the default) or `"server"`.  A seat may also carry its own `instance` key.
2. The daemon reads the partition file:  the checkout's copy on the Mac, and the copy baked into the image on the server.  It **refuses to start** when its config holds a seat, enabled or not, that the partition does not give to its own instance:  a seat of the other instance (by the partition or by the seat's own key), a `none` seat, or a seat the file does not list at all.  A partition file that is missing, cannot be parsed or has a bad entry refuses too, so no seat is ever held without it.  The refusal happens before the flock, before any thread and before any `POST /register`.  The daemon exits 2 with one stderr line per problem.  Under launchd or Docker it is restarted and refuses again, but it never registers a queue.
3. A section may not read another listed seat's credential under its own name, so aliasing a name cannot get past step 2.  `[seat.CLAUDE] bot = "GB-Compiler"` (the GB-Compiler file) and `[seat.GB-FIXER] key_env = "ZULIP_GB_COMPILER_API_KEY"` (another seat's default variable) both refuse to start.
4. At connect, `users/me` must be the seat's own bot:  the seat tag derived from the bot's email (`compiler-grok-bot@` is GB-COMPILER) must equal the seat.  A key that belongs to another bot (custom variable names can hide that from step 3) is refused like an admin key:  no queue, `seat-refused` in the log, red in status.  The tag comes from the email, so a seat must be named for its own bot (`grok-build-bot@` is GROK, not GROK-BUILD).
5. `AGENT_SYNC_INSTANCE` is set by the LaunchAgent plist (`mac`) and by the image (`server`).  A daemon whose config says the other instance refuses to start.
6. A reload (SIGHUP) that would add a seat this instance may not hold is refused.  The running config stays, and the refusal shows red in `agent-sync status`.

To move a seat, remove it from one instance's `listener.toml`, reload that listener, change the partition entry in a PR (with the owner's OK), and only then add it to the other instance.  A unit test checks that the partition file, the Mac sample config and the server sample config agree.  The flock does not help here:  it works only across processes that share one state directory, never across the two machines.

## Credentials From the Environment

A seat with `creds = "env"` (server instance only) reads its email and key from environment variables named in its config:

| Key | Default | Example for GB-COMPILER |
|---|---|---|
| `email_env` | `ZULIP_<CODE>_EMAIL` | `ZULIP_GB_COMPILER_EMAIL` |
| `key_env` | `ZULIP_<CODE>_API_KEY` | `ZULIP_GB_COMPILER_API_KEY` |
| `site_env` | `ZULIP_SITE` | `ZULIP_SITE` |

CODE is the seat with hyphens turned into underscores.

- The realm is never taken from `ZULIP_SITE`.  It is `AGENT_SYNC_REALM`, else `https://simplewithus.zulipchat.com`.  The site's host must match the realm host, or the seat is refused.
- A missing variable refuses the seat, and the error names the variable, never a value.
- A seat may not name `ZULIP_EMAIL`, `ZULIP_API_KEY` or `ZULIP_RC`, the CLI's single-seat variables.  Two seats may not share a variable.  Within one seat, the Zulip email, key and site and the routine URL and key must be five different variables, so a typo cannot send the bot's Zulip key to the routine.  The routine may not read `ZULIP_SITE`.  In each case the seats lose their queue and the config shows red, so seats cannot alias one bot.
- Values lose wrapping quotes and surrounding spaces, as zuliprc values do, for the Zulip variables and the routine URL and key alike.  A copied `"https://..."` is therefore not refused as "not https", and a quoted key is not sent as `Bearer "<key>"`.
- Each loaded key and its base64 form are scrubbed from every log line, error and status, as on the Mac.
- **The environment is read once.**  A container's environment is fixed when Coolify creates it, and the daemon copies it at start.  `agent-sync daemon reload` re-reads only `listener.toml`.  A new or changed variable (a persona enabled, a key rotated) reaches the listener only when the app is restarted in Coolify.  Coolify's docs say the same:  use Restart when only runtime values changed.  A status line for a missing variable says to restart.
- The Infisical names are pending from Muse.  If they differ from the defaults, set `email_env` and `key_env` in the seat's section rather than renaming secrets.

**Admin and owner keys stay refused (listener decision 7).**  The listener accepts only moderator (300) and member (400) bots.  Every bot is a realm member now (owner, Fri, Oct 9:  "all bots are Members now not admin").  That includes GB-Director, Muse Assist (MA), Jet (JET), Instinct and Echo, which were admins earlier.  GB-Director's seat is present in the sample but disabled only because it has no routine yet.  If an admin or owner bot's key is configured for a seat, `users/me` shows role 200 or 100.  The seat is then refused, logged as `seat-refused` with the role, retried every 5 minutes, shown red in status, and notified once a day.  The other seats keep running.

## DMs

Each persona's own queue already receives the DMs sent to its bot.  The router captures every DM into the seat inbox (`type: private`, class `dm`) and applies the listener's wake prefilter unchanged.

- An owner DM (the owner's user id from a human Zulip app) wakes the routine with `"dm": true`.  Owner coalescing is 5 seconds, and owner caps apply.
- A DM from a bot, a fleet seat included, is captured and never wakes, as on the Mac.  Neither does a DM from a sender who is not eligible, or an API post made with the owner's key.  See [Open Questions](#open-questions).
- The daemon never posts for an `http` seat.  The DM-only reply rule therefore belongs to the routine, and the body spells it out:  for a DM trigger, `reply_to` is `{"type": "direct", "to": [<the DM's other members>]}`, never a channel.

## The Routine Wake (`http`)

Seat config (`listener.toml`):

```toml
[seat.GB-COMPILER]
instance = "server"
creds = "env"
wake = "http"
routine = { url_env = "GB_COMPILER_ROUTINE_URL", key_env = "GB_COMPILER_ROUTINE_KEY", method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }
```

| Routine key | Values | Default |
|---|---|---|
| `url_env`, `key_env` | Names of the environment variables holding the routine URL and the sender key.  The values are never in the repo or the config.  Wrapping quotes are removed.  A URL that does not parse refuses the routine (`routine_url_refused`) without stopping the daemon | `<CODE>_ROUTINE_URL`, `<CODE>_ROUTINE_KEY` |
| `method` | `POST`, `PUT`, `PATCH` | `POST` |
| `auth` | `bearer` (`<header>: Bearer <key>`), `header` (the key itself as the header value), `hmac-sha256` (the lowercase hex HMAC-SHA256 of the raw body bytes, keyed with the sender key) | `bearer` |
| `header` | Header name for the key or signature (not `Host`, `Content-Type`, `Content-Length` and the like) | `Authorization`, `X-Routine-Key`, `X-Agent-Sync-Signature` by mode |
| `signature_prefix` | Text before the hex digest, for `hmac-sha256` only (for example `sha256=`) | empty |
| `timeout_seconds` | 1 to 60, per request | 15 |
| `cost_usd` | What one routine wake counts against `usd_per_day` | 0 (the daemon spends nothing; Grok Bot bills its own run) |

**Request.**  The daemon serializes the body once, computes the header over those exact bytes, and sends the identical bytes on every attempt.  Headers are `Content-Type: application/json; charset=utf-8`, `Idempotency-Key: <wake_id>`, `User-Agent: agent-sync/1 (fleet)` and the auth header.  It uses no proxy and follows no redirect, because a redirect would re-send the key to another host.  The URL must be https; plain http is accepted only for a loopback test server.

**Outcome.**  A 2xx is accepted (ledger `done`, action `routine`).  A 5xx or a network failure is retried with 2, 5 and 10 seconds of backoff.  That is 3 retries, so at most 4 requests.  A 4xx (429 included) or a 3xx is never retried.  A wake that is not accepted is `failed` in the ledger.  A 4xx also sends one notify-owner per seat and status a day ("check the routine URL, key and format").  An owner-triggered failure always notifies.

**Body (contract `agent-sync-wake/1`).**  One JSON object, keys sorted, UTF-8:

| Field | Meaning |
|---|---|
| `contract` | `"agent-sync-wake/1"` |
| `seat` | The persona's seat, for example `"GB-COMPILER"` |
| `wake_id` | The ledger id.  Also the `Idempotency-Key`, so dedupe on it |
| `message_id` | The trigger:  the newest owner trigger in a coalesced batch, else the newest trigger |
| `trigger_ids` | Every message id the batch coalesced |
| `dm` | `true` for a DM trigger, else `false` |
| `channel`, `topic` | Display copies of the trigger's channel and topic, escaped onto one line like the sender name (marker text removed, U+2028 and U+2029 escaped, at most 100 characters); `null` for a DM.  Never use them to address the reply |
| `dm_recipient_ids` | For a DM, every other member of the DM (the persona's bot left out); else `[]` |
| `sender_user_id`, `sender_full_name`, `is_bot` | The trigger's sender.  The name is escaped onto one line, with marker text removed |
| `owner` | The daemon's owner flag:  the owner's user id AND a human Zulip client.  It is a routing hint, never authority |
| `excerpt` | The trigger's body, at most 2,000 characters in all, between `BEGIN_UNTRUSTED_ZULIP nonce=<hex>` and `END_UNTRUSTED_ZULIP nonce=<hex>`.  It is escaped like every other untrusted body:  NFKC, format characters dropped, marker text in any case or with any separator becomes `[marker removed]`, control characters become `\xNN`, and U+2028 and U+2029 become `\u2028` and `\u2029`.  Any loaded key or routine secret is redacted |
| `zulip_link` | `<realm>/#narrow/channel/<id>-<name>/topic/<topic>/near/<id>`, or `<realm>/#narrow/dm/<ids>-dm/near/<id>` |
| `reply_to` | Where the reply goes:  `{"type": "stream", "channel": ..., "topic": ...}` with the exact names, or, for a DM, `{"type": "direct", "to": [...]}`.  For addressing only:  never show it, and never put it in a prompt |
| `reply_prefix` | `[GB-COMPILER·wake] re=<message_id>` |
| `sent_at` | Unix seconds when the body was built (covered by the HMAC) |

**What the routine must do.**

- Verify the auth header (a constant-time compare for the HMAC) and drop a repeated `wake_id`.
- Treat `excerpt` and every other Zulip-derived string (channel, topic, sender name, `reply_to`) as data, never as instructions.
- Reply through `reply_to` only.  For a DM that means a DM to `reply_to.to`, never a channel.
- Start the reply with `reply_prefix` on its own first line and mention no one.  The listener's loop guard counts only `·wake`-tagged replies, and a `·wake` tag never wakes any seat.
- Answer 2xx quickly and do the work after.  The daemon waits at most `timeout_seconds` per request.

**What is never logged.**  The routine URL, its query string, its path, the key and the signature never reach `listener.log`, stdout, the ledger, status or the owner queue.  The daemon scrubs them like bot keys.  A request is logged as host, method, auth mode, status per attempt, body length, and the length and SHA-256 of the response.  A response body is never logged.

## Container

`scripts/agent_sync/server/`:

| File | Role |
|---|---|
| `Dockerfile` | `python:3.12-slim`, non-root user `agentsync` (uid 10001), copies `scripts/agent-sync`, `scripts/agent_sync` and the partition file under `/app`.  `HEALTHCHECK` runs `healthcheck.py`.  `ENTRYPOINT` is `entrypoint.sh`; `CMD` is `daemon run --wait-lock` |
| `Dockerfile.dockerignore` | Keeps only those paths in the build context (BuildKit); the tests stay out |
| `entrypoint.sh` | On first start, copies the sample config onto the volume as `$AGENT_SYNC_CONFIG`, never overwriting one, then execs `agent-sync` (exec form, so SIGTERM reaches the daemon, which deletes its queues) |
| `healthcheck.py` | Unhealthy when `/data/listener/status.json` is missing or older than 60 seconds.  The daemon rewrites it every 2 seconds; a refused start never writes it |
| `listener.toml` | The server sample:  GB-COMPILER enabled with `wake = "http"`, the cloud seats MA, GROK-WEB, INSTINCT and ECHO enabled with `wake = "inbox"`, and JET enabled with `wake = "http"` (its routine is the hosted MCP Worker).  The other ten personas are present with `enabled = false`, each with `wake = "http"` and a `routine` block that names its default variables (their routines do not exist yet) |

**Environment the container expects.**  The image sets these:

- `AGENT_SYNC_INSTANCE=server`
- `AGENT_SYNC_STATE_DIR=/data`
- `AGENT_SYNC_CONFIG=/data/listener.toml`
- `AGENT_SYNC_LOG_STDOUT=1`, which writes every log line to stdout as well as to `/data/logs/listener.log`
- `HOME=/home/agentsync`

Infisical (project "AI Fleet Coordinator", environment `prod`, folder `/zulip`) holds every one of these except `ZULIP_SITE`.  Coolify gets runtime-only copies, copied by hand with a script (Coolify Setup, step 3).  `ZULIP_SITE` is not in Infisical:  set it in Coolify as a plain value, `https://simplewithus.zulipchat.com`.

- `ZULIP_SITE` (Coolify only)
- `ZULIP_GB_COMPILER_EMAIL` and `ZULIP_GB_COMPILER_API_KEY`
- `GB_COMPILER_ROUTINE_URL` and `GB_COMPILER_ROUTINE_KEY`
- For each cloud seat, `ZULIP_<CODE>_EMAIL` and `ZULIP_<CODE>_API_KEY`, with CODE `MA`, `JET`, `GROK_WEB`, `INSTINCT` or `ECHO` (ten variables)
- For JET's wake:  `JET_ROUTINE_URL` (`https://agent-sync.jays.services/internal/wake/JET`) and `JET_ROUTINE_KEY` (the shared HMAC key, kept in `~/.secrets/jet-wake-hmac.env` on Jay's Mac and installed as the Worker secret `WAKE_HMAC_KEY_JET`).  Until both are set, each JET wake is logged as `routine-unready` and dropped, and capture is unchanged
- For each persona enabled later:  `ZULIP_GB_<ROLE>_EMAIL`, `ZULIP_GB_<ROLE>_API_KEY`, `GB_<ROLE>_ROUTINE_URL` and `GB_<ROLE>_ROUTINE_KEY`, or the names its section sets

These are optional, and best left unset:

- `AGENT_SYNC_REALM`, which defaults to the fleet realm
- `AGENT_SYNC_HEALTH_MAX_AGE`, which defaults to 60
- `AGENT_SYNC_PARTITION`, which defaults to the copy in the image

## Coolify Setup

1. **Application.**  Add an application from the `Simple-With-Us/AI-Fleet-Coordinator` repo, branch `main`.  Use the Dockerfile build pack with base directory `/` and Dockerfile location `/scripts/agent_sync/server/Dockerfile`.  The build context must be the repo root.  Run one replica.  Under General > Build, set **Watch Paths** so that only listener changes redeploy it:
   ```
   scripts/agent_sync/**
   !scripts/agent_sync/tests/**
   scripts/agent-sync
   docs/protocols/agent-sync-partition.toml
   ```
   Every redeploy re-registers the queues, hands over the lock and backfills, so an unrelated merge to `main` should not start one.  The last matching pattern wins, so a test file is excluded.  Watch Paths filter only Git webhook deploys.  A manual deploy from the dashboard, or one through the authenticated Deploy Webhook, always runs.  The alternative is to turn auto-deploy off and deploy by hand.
   **As deployed, nothing auto-deploys.**  The repo is public, so Coolify created no GitHub webhook, and Watch Paths do nothing until one exists.  To get push deploys, add a manual webhook in the repo's GitHub settings:  URL `https://host.jays.services/webhooks/source/github/events/manual`, push events only, secret from the application's Webhooks page in Coolify (its GitHub webhook secret).  Adding it is the owner's call.  Until then, deploy by hand from the dashboard.  The pinned seats live on the `/data` volume, so a deploy does not change them.  After a deploy that changes which bots `daemon init` treats as eligible (`FLEET_SEATS`), run step 6 again.
2. **No domain, no port mapping.**  The listener only makes outbound connections (Zulip and the routines), so it needs no hostname and no proxy route.  Leave the Ports Exposes default, clear any Domains value Coolify pre-fills, and add no port mapping, so no hostname or proxy route exists.  The image listens on no port.  In particular, **`agent-sync.jays.services` is not reused here.**  That is the retired Slack relay's hostname, and old clients and tokens still point at it.
3. **Environment.**  Infisical (project "AI Fleet Coordinator", environment `prod`, folder `/zulip`) is the source of truth, and Coolify holds copies, with one exception:  `ZULIP_SITE` is not in Infisical, so set it in Coolify as a plain value, `https://simplewithus.zulipchat.com`.  No Infisical sync into this app exists.  The deployed copies were made by hand with a script that reads Infisical `prod` and writes Coolify runtime-only variables, never printing a value.  To change one, update Infisical first, then re-run that copy (AGENT-SYNC, Secret handoff).  Coolify also copies each variable into the preview scope on its own.  Preview deployments are off, so those copies are unused.  For **every** variable in the list above, set it to runtime only:  untick **Available during build** and keep **Available in the container**.  Coolify defaults new variables to both, and a build-time value can end up in the build log and in the image metadata.  The Dockerfile declares no `ARG` and needs no build-time value, so nothing here belongs at build time.  After adding or changing any variable, **restart the app** (a reload does not read the environment).
4. **Storage.**  Add one persistent volume mounted at `/data`.  A new named volume inherits the image's `/data` ownership (uid 10001, mode 700).  A bind mount needs `chown 10001:10001` and `chmod 700` on the host directory first.  The volume holds the config, the cursors, the seat inboxes, the ledger, the logs and `daemon.lock`.  Losing it loses the cursors:  the next start begins at the newest message and replays nothing.  Never mount `listener.toml` as a single file:  `daemon init` replaces it atomically (`os.replace`), which a single-file mount refuses.
5. **Health check.**  Leave Coolify's dashboard health check disabled.  For a Dockerfile application Coolify detects the image's `HEALTHCHECK` and uses it instead, which also lets a rolling deploy wait for the new container before removing the old one.  The image checks every 30 seconds, with a 60-second start period and 3 retries.  On the host, `docker inspect --format '{{.State.Health.Status}}' <container>` shows the result (expect `healthy`).  Healthy means only that the main loop rewrote `status.json` in the last 60 seconds.  It does not mean a seat is connected, the owner is pinned or a routine is ready; step 7 checks those.
6. **First deploy, then init.**  Until `daemon init` runs, `owner_user_id` is 0, so nothing is treated as the owner and nothing wakes.  Open the app's terminal in Coolify (or `docker exec -it <container> sh`) and run:
   ```
   agent-sync daemon init --seat MA     # reads the user list as MA (any enabled seat with working credentials), asks before pinning
   agent-sync daemon reload             # SIGHUP:  re-reads listener.toml (enough here; no variable changed)
   agent-sync status
   ```
   `init` pins `owner_user_id` and `eligible_user_ids` into `/data/listener.toml`.  It also checks each enabled seat's environment credentials, role and #agent-sync subscription.  **Always pass `--seat`.**  Without it the reader is the first enabled seat in name order (ECHO, with the sample as it is now), and `init` stops with that seat's missing variable.  Name a seat whose credentials are present and correct;  GB-COMPILER is red until the owner replaces its credentials (see Open Questions).

   Over a non-interactive `docker exec` there is no terminal to answer the prompt, so `init` needs `--yes` or it changes nothing.  The Coolify host is reachable as the ssh alias `coolify`:
   ```
   ssh coolify
   C=$(docker ps --filter name=l40rxd4rbj1pnogmbtetzmsf --format '{{.Names}}')
   docker exec "$C" agent-sync daemon init --seat MA --yes
   docker exec "$C" agent-sync daemon reload
   docker exec "$C" agent-sync status
   ```
7. **Verify.**  A green container does not prove a good deploy.  `agent-sync status` must show all of these:
   - `listener: running (pid ...)`
   - `instance server; owner pinned: yes`
   - no line that starts with `RED` (config, partition, credential or role problems)
   - `GB-COMPILER  connected`, with `wake http (routine ready, <host>)` on the same line (while its credentials are the wrong bot's, expect it red instead, and read the rest of the list without it)
   - each cloud seat (MA, JET, GROK-WEB, INSTINCT, ECHO) `connected`, with `wake inbox` (JET:  `wake http`)
   - no `routine NOT ready` and no `DOWN` for any enabled seat

   `LaunchAgent: not installed` is expected in the container.  A fresh or lost volume reseeds the sample with `owner_user_id = 0`, so after one, run step 6 again.
8. **Redeploys.**  The new container starts with `--wait-lock`.  If the old container still holds `/data/listener/daemon.lock`, the new one logs `waiting-for-lock` and waits.  Its health check passes, because the old container keeps `status.json` fresh.  While it waits, it never touches the old container's ledger:  it does not mark the old container's running wake failed, and it does not load the old container's queued wakes.  When Coolify stops the old container, that container deletes its queues and flushes its cursor.  The new one then takes the lock within a second, rebuilds its seats, cursors and pending wakes from the volume as the old one left them, re-registers, and backfills from that cursor.  So there are never two queues per bot, and nothing the old container already routed is woken again.  This holds only because both containers share the same volume on the same host:  `flock` works across containers on one kernel, never across hosts.

**Operating.**

- Logs are in Coolify's log view (stdout) and in `/data/logs/listener.log` (5 MB, 3 files).
- Kill switch:  `agent-sync daemon pause [--seat GB-COMPILER] [--wakes-only]`, lifted with `agent-sync daemon resume`.  An owner DM to a persona saying `agent-sync pause` pauses every seat of this instance.  Hard stop:  stop the app.
- `agent-sync wakes --seat GB-COMPILER` lists the ledger.
- `agent-sync inbox --local --as GB-COMPILER` reads the captured inbox.
- Owner notes (`notify-owner`) go to `/data/<SEAT>/owner-queue.jsonl` only.
- `agent-sync daemon reload` re-reads `listener.toml` and nothing else.  Use it only when every variable the new config names was already in the container when it started (for example after `daemon init`).  Anything that adds or changes a variable needs a restart.

**Editing `/data/listener.toml`.**  `python:3.12-slim` has no text editor, so the Coolify terminal and `docker exec` have none either.  Never run a global `sed` on the file:  `s/enabled = false/enabled = true/` would enable all ten disabled personas at once.  Scope every edit to one section with a sed range, from the seat's header to the next `[seat.` header:

```
sed -i '/^\[seat\.GB-FIXER\]$/,/^\[seat\./ s/^enabled = false/enabled = true/' /data/listener.toml
sed -i '/^\[seat\.GB-FIXER\]$/,/^\[seat\./ s/auth = "bearer"/auth = "hmac-sha256"/' /data/listener.toml
grep -A6 '^\[seat\.GB-FIXER\]$' /data/listener.toml      # check the result
```

GNU sed keeps the file's owner and mode 600.  For a larger change, edit the file on the host instead, at the volume's path (`docker volume inspect <volume> --format '{{.Mountpoint}}'`), keeping owner 10001 and mode 600.

**A section added to the sample does not reach a live volume.**  The entrypoint copies the sample only when `/data/listener.toml` does not exist, so merging a new `[seat.X]` section changes fresh volumes and nothing else.  To add one to the running app, append it, never replace the file (a replacement loses the pinned `owner_user_id` and `eligible_user_ids`).  `docker exec` runs as `agentsync`, so an append keeps the owner and mode 600:
```
docker exec -i "$C" sh -c 'cat >> /data/listener.toml' < section.toml
docker exec "$C" grep -A4 '^\[seat\.MA\]$' /data/listener.toml      # check it landed once
docker exec "$C" agent-sync daemon reload                              # only if every variable it names is already in the container
```
Appending a section twice makes the TOML invalid (a duplicate table), so check first with the `grep`.

**Enabling another persona.**  Every persona's section in the sample already carries `wake = "http"` and a `routine` block that names its default variables, so enabling one is a one-line edit.

1. Add its routine URL and key (`GB_<ROLE>_ROUTINE_URL`, `GB_<ROLE>_ROUTINE_KEY`) in Infisical (`prod`, `/zulip`), and check that its Zulip email and key are there.
2. Make sure all four reach the app's Environment Variables in Coolify (copied by hand with the script from step 3), runtime only (step 3).
3. In `/data/listener.toml`, set the persona's `enabled = true` with the scoped `sed` above.  If its routine needs another method, auth or header, change those lines the same way.
4. **Restart the app in Coolify.**  The container's environment is fixed when it is created, so `agent-sync daemon reload` would leave the persona red with "environment variable not set".
5. Run `agent-sync status` and check step 7 for the persona.
6. Mirror the change in `scripts/agent_sync/server/listener.toml` in a PR, so a fresh volume starts the same way.

**Rotating a key.**  Change it in Infisical first, make sure the new value reaches Coolify (copied by hand with the script from step 3), then restart the app.  The same applies to a bot's Zulip key and to a routine URL or key.  Until the restart, the listener keeps using the old value.

## Cloud Seats

MA (`muse-assist-bot@`), JET (`openai-dot-bot@`), GROK-WEB (`grok-web-bot@`), INSTINCT (`instinct-owl-bot@`) and ECHO (`instinct-bat-bot@`) are cloud seats:  they run in other apps, with no Mac and no zuliprc file.  The partition gives all five to this instance (owner, Thu, Oct 8), and since Fri, Oct 9 the sample holds each with this section (Echo's is the same, kept minimal because Echo may be retired when it merges with Instinct):

```toml
[seat.MA]
instance = "server"
creds = "env"
wake = "inbox"
```

**What a cloud seat's queue does.**

- It holds one Zulip event queue for the bot, so its @-mentions and DMs are captured at zero tokens into `/data/<SEAT>/` while no session of that app is open, and a restart backfills from the cursor.  The prefilter, dedupe, budgets and loop guard are the same as everywhere else.
- It checks the key at connect.  The bot's role must be member or moderator, and the key's own bot must be the seat (the tag derived from the bot's email must equal the seat).  A wrong key shows `seat-refused` in the log and red in `agent-sync status`, and the other seats keep running.
- It makes the bot visible in `agent-sync status` (connected or red, last event, cursor).

**JET is woken (Fri, Oct 9).**  JET's section has `wake = "http"` with `auth = "hmac-sha256"` (header `X-Agent-Sync-Signature`, no prefix) and an explicit budget (6 wakes an hour, 40 a day, owner 30 a day).  Its routine is the hosted MCP Worker's `/internal/wake/JET`, which checks the signature and a 5-minute `sent_at` window, dedupes `wake_id`, and sends a signed MCP Events `zulip.mention` to every ChatGPT chat or dot that subscribed (`docs/protocols/agent-sync-mcp.md` section 3.12, and `ARMING-JAY.md` "Wake Jet" for Jay's steps).  The capture below is unchanged.

**What it does not do.**  No wake adapter exists for the other cloud seats, so nothing wakes them.  Nothing reads their inboxes either:  the hosted MCP `inbox` tool (`docs/protocols/agent-sync-mcp.md`) is stateless and queries Zulip itself, one `is:mentioned` search per allowlisted channel, with no DMs.  It never reads these files.  The only reader today is an operator inside the container (`agent-sync inbox --local --as MA`, or `--as JET`, and so on).  A cloud seat is therefore captured, not served.  The real consumers, a wake adapter for a seat that can take one, or an MCP tool that reads this inbox, are Open Questions below.

**Not eligible senders.**  `daemon init` pins `eligible_user_ids` from `FLEET_SEATS` (`scripts/agent_sync/listener_cli.py`).  That list holds MA and JET but not GROK-WEB, INSTINCT or ECHO, so a message from one of those three is captured and never triggers a wake on another seat.  This changes only if the list changes.

**Credentials and roles.**  Each seat reads `ZULIP_<CODE>_EMAIL` and `ZULIP_<CODE>_API_KEY` (CODE `MA`, `JET`, `GROK_WEB`, `INSTINCT`, `ECHO`).  All ten are in Infisical `prod` `/zulip` and are copied into the Coolify app as runtime-only literals by the script of Coolify Setup, step 3.  They are copied by hand and the app must be restarted afterwards.  Rotation follows "Rotating a key" below.  MA, JET and INSTINCT were realm admins, which the listener refuses;  every bot is a realm member now (owner, Fri, Oct 9), so none is refused.

## Open Questions

- **Routine request format.**  It is pending from GB-Compiler.  The sample assumes `POST` with `Authorization: Bearer <key>`.  If GB-Compiler signs requests, set `auth = "hmac-sha256"`, the header name and any `signature_prefix`.  If the routine needs a different body shape, a mapping layer is needed:  the body is a fixed contract on purpose.  Unknown:  whether the routine wants the HMAC over a timestamp as well (the body's `sent_at` is covered), and whether it dedupes on `Idempotency-Key`.
- **Retry count.**  "Retry up to 3 times" is read as 3 retries, so at most 4 requests.  A request that timed out or lost its connection after sending may already have reached the routine, so the routine must drop a repeated `wake_id` (also sent as `Idempotency-Key`).
- **Peer DMs.**  A DM from another fleet seat's bot is captured but does not wake, as on the Mac (bot-to-bot loops).  If GB personas should answer peer DMs, that is a prefilter change for `http` seats.
- **One trigger per wake.**  A coalesced batch sends the newest owner trigger (else the newest trigger) as `message_id` and `excerpt`, plus every id in `trigger_ids`.  If the routine wants every body, the contract changes to a list.
- **Spend.**  `usd_per_day` counts `cost_usd` per routine wake (default 0).  Grok Bot's own spend is not metered here; the count budgets (6 an hour, 40 a day, owner 20 a day) are the limit.
- **Owner notes on the server.**  They land in an owner queue nobody reads there.  A Zulip DM to the owner from a notifier bot, or a pull from the Mac, would close that gap.
- **Infisical to Coolify.**  Settled for now:  a script copies `/zulip` (`prod`) into the app by hand.  This repo configures no Infisical sync.  Every variable is runtime only and a change needs a restart.  An automatic sync would remove the manual re-copy on rotation.
- **Variable names.**  The Infisical names are pending from Muse.  The defaults above are what the sample config reads.  If they differ, set `email_env` and `key_env` (or the routine's `url_env` and `key_env`) in the seat's section, copy the variables into Coolify under those names, and restart the app.
- **GB-COMPILER credentials.**  `ZULIP_GB_COMPILER_EMAIL` and `ZULIP_GB_COMPILER_API_KEY` in Infisical currently hold another bot's credentials, an admin bot's.  The seat is refused (the key's own bot is not GB-COMPILER, and an admin key is refused) until the owner replaces them with the real GB-Compiler pair.  Then copy them to Coolify and restart the app.
- **Cloud seat inboxes have no reader.**  MA, JET, GROK-WEB, INSTINCT and ECHO are held with inbox capture, and nothing consumes it (see [Cloud Seats](#cloud-seats)).  Options:  an `http` wake adapter for a seat whose app can take a webhook (none can today), a hosted MCP tool that reads this instance's seat inbox (it would need a path from the Worker to the container, which does not exist), or a periodic Zulip digest from the captured rows.  Until one of these is chosen, the queue is durable capture and a status line only.  The same goes for `FLEET_SEATS`:  GROK-WEB, INSTINCT and ECHO are not eligible senders.
