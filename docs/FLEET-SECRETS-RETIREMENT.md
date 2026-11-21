# Retire `fleet-secrets`: one contract for every consumer

Owner directive, Tue, Nov 17, 2026: delete `fleet-secrets` off the Mac and the server, and replace every past,
present and future use with Infisical service tokens.

The current build of the shared reader is already service-token only (`fleet_infisical.machine_token` raises
`service-token-required`), so the CLI is not a credential of its own any more.  What it still is: one broad
reader, used by many consumers, each of which should instead hold its own narrow grant.

## The contract

1. **One token per consumer.**  Each consumer holds its own scoped, read-only service token, named
   `<consumer>-ro`, scoped to exactly one project, environment `prod` and one folder.  A consumer that reads two
   folders gets two tokens, or one token per folder; it never gets the shared broad reader.

2. **The store is the consumer's own.**  Nothing is stored in a shared place.

   | Host | Store | Read |
   | --- | --- | --- |
   | Mac | the owner login keychain, one generic-password item: service `infisical-service-token`, account `<consumer>-ro` | `/usr/bin/security find-generic-password -s infisical-service-token -a <consumer>-ro -w` |
   | Server | a file owned by the consumer account, mode 0600, inside a parent directory of mode 0700: `~/.config/infisical/tokens/<consumer>-ro.token` | read the file |

   A token is never in another consumer store, never in the retired reader, never in a repository and never on a
   command line.

3. **The read is direct and scoped.**  The consumer calls the Infisical raw-secret route with its own token:

   ```
   GET {INFISICAL_API}/api/v3/secrets/raw/{name}
       ?projectId=<project id>&environment=prod&secretPath=<folder>
   Authorization: Bearer <token>
   ```

   The shared Python implementation is `scripts/fleet_infisical.py`: `fetch_verified(name, folder, env, token)`
   performs the read and also checks the answer own provenance (workspace, environment, secretPath, secretKey),
   so a value from the wrong place fails closed.

   Where a consumer lives outside this repository (a Mac-local script, another repo tool), it calls the same
   route itself, with the same checks its own code needs.  `scripts/mac-resource-watch.py` is the reviewed
   precedent for that shape.

4. **The consumer's own store is the authority for the token.**  The scoped service token is read from the
   consumer's own store, and it is the credential the read runs under.  The environment may carry a copy of
   that same token, which is how a host transports a value it already synced from Infisical, and a copy is
   used only when the scoped read populated it.  A hand-set environment value never replaces the reviewed,
   scope-limited grant.  The read's own scope check still decides whether any token may read a folder at
   all:  `validate_service_metadata` requires the token's granted folders to equal the declared folders
   exactly.

   This is about the token.  A consumer whose own contract says a host binds a secret *value* in the
   environment (a realm credential for a given realm, for example) keeps that behaviour:  the value is the
   host's own binding, and the token that fetches it is still the consumer's own scoped service token.

5. **No fallback, no cached copy.**  There is no route to the shared reader, to a machine identity, to the System
   Keychain old identity item or to a cached value.  A missing token, a locked keychain, a 403 or a 404 yields
   nothing, and the consumer says which of those it was.

6. **The CLI is gone.**  `scripts/fleet_secrets/`, `scripts/install-fleet-secrets.sh`,
   `scripts/fleet_secrets/seed-system-keychain.sh`, `scripts/fleet_secrets/switch-pm2-jobs.sh`, the release
   bundle `fleet_secrets/` entries, the doctor `fleet-secrets` rows, the compliance check that runs
   `fleet-secrets names`, and the copy on both hosts.

## The tokens, one per consumer

Every account below is a read-only service token, 90-day expiry, scoped to one project, `prod`, and one folder.
The Mac copy lives in the owner's login keychain; the server copy is a 0600 file in `~/.config/infisical/tokens`.
The project ids are infrastructure identifiers and stay out of this public repository.

| Account | Project | Folder | Consumer | Host |
| --- | --- | --- | --- | --- |
| `operator-github-apps-ro` | Fleet-OPS | `/github-apps` | the GitHub relay's App credentials | server |
| `operator-github-callers-ro` | Fleet-OPS | `/operator-github/callers` | the GitHub relay's caller HMACs | server |
| `github-pat-ro` | Fleet-OPS | `/github` | the fleet's write-capable GitHub credential:  the shell's `GH_TOKEN` and the iOS publish's fallback | Mac |
| `zulip-ro` | Fleet-OPS | `/zulip` | `simple-chat`, `simple-board` | both |
| `zulip-operator-ro` | Fleet-OPS | `/zulip_operator` | the Operator's Zulip admin credential | both |
| `wake-ro` | Fleet-OPS | `/wake` | the wake router | both |
| `fleet-doctor-zulip-ro` | Fleet-OPS | `/zulip` | the fleet doctor's Zulip check | both |
| `fleet-doctor-linear-ro` | Fleet-OPS | `/linear` | the fleet doctor's Linear check | both |
| `fleet-doctor-fleetlink-ro` | AI Fleet Coordinator | `/fleetlink` | the fleet doctor's FleetLink check | both |
| `fleet-config-check-ro` | Fleet-OPS | `/zulip` | the compliance checker's Zulip key | Mac |
| `outbox-bridge-ro` | Fleet-OPS | `/zulip` | the GitHub outbox bridge | Mac |
| `grok-acp-ro` | Fleet-OPS | `/grok-acp` | the Grok ACP secret | Mac |
| `cursor-surfaces-ro` | Fleet-OPS | `/global-api-keys` | the Cursor chat surfaces | Mac |
| `seat-mcp-ro` | Fleet-OPS | `/seat-mcp` | the seat MCP token | Mac |
| `worker-health-ro` | Fleet-OPS | `/` | the fleet worker health key (the name is in neither project) | Mac |
| `zulip-provision-ro` | Fleet-OPS | `/zulip_operator` | the Zulip bot provisioner | Mac |
| `fleet-work-backup-ro` | Fleet-OPS | `/global-api-keys` | the backup job's credentials | server |
| `fleet-mode-digest-ro` | Fleet-OPS | `/global-api-keys` | the weekly fleet-mode digest | Mac |
| `infra-phase0-ro` | Fleet-OPS | `/global-api-keys` | the ST-MCP infra phase 0 | Mac |
| `install-seat-key-ro` | Fleet-OPS | `/global-api-keys` | the seat-key handoff's Cloudflare credentials | Mac |
| `install-seat-key-zulip-ro` | Fleet-OPS | `/zulip` | the seat-key handoff's Zulip credentials | Mac |
| `ios-testflight-freshness-ro` | Fleet-OPS | `/asc` | the TestFlight freshness check | Mac |
| `sheet-read-ro` | Fleet-OPS | `/google-sheets-sa` | the sheet reader | both |
| `zulip-linkifiers-operator-ro` | Fleet-OPS | `/zulip_operator` | the linkifier's Operator credential | server |
| `zulip-linkifiers-zulip-ro` | Fleet-OPS | `/zulip` | the linkifier's seat credential | server |
| `muster-fleetops-ro` | Fleet-OPS | `/` | Muster's Fleet-OPS reads | Mac |
| `muster-afc-ro` | AI Fleet Coordinator | `/` | Muster's knob registry root | Mac |
| `muster-afc-code-mirrors-ro` | AI Fleet Coordinator | `/code-mirrors` | Muster's knob registry | Mac |
| `muster-afc-wake-router-ro` | AI Fleet Coordinator | `/wake-router` | Muster's knob registry | Mac |
| `cf-usage-watchdog-ro` | AI Fleet Coordinator | `/cf-usage-watchdog` | the Cloudflare usage watchdog | server |
| `alert-ingress-afc-ro` | AI Fleet Coordinator | `/zulip` | the alert-ingress provisioner, AFC side | server |
| `alert-ingress-fleetops-ro` | Fleet-OPS | `/global-api-keys` | the alert-ingress provisioner, Fleet-OPS side | Mac + server |
| `alert-ingress-selftest-ro` | AI Fleet Coordinator | `/zulip` | the alert-ingress selftest | server |
| `notes-client-ro` | AI Fleet Coordinator | `/` | the notes client | both |
| `notes-operator-ro` | AI Fleet Coordinator | `/operator` | the notes client, Operator key | both |
| `afc-wake-router-ro` | AI Fleet Coordinator | `/wake-router` | the wake router | server |
| `scout-runtime-ro` | Fleet-OPS | `/senate-relay` | the Scout runner | Mac |
| `scout-runtime-global-ro` | Fleet-OPS | `/global-api-keys` | the Scout runner | Mac |
| `sentry-dashboards-ro` | Fleet-OPS | `/global-api-keys` | the Sentry dashboards | Mac |
| `autorotate-publish-ro` | Fleet-OPS | `/asc` | the autorotate publisher | Mac |
| `b2-hetzner-size-ro` | Fleet-OPS | `/global-api-keys` | the Backblaze size helper | Mac |
| `mm-board-ro` | Fleet-OPS | `/linear` | the Linear board helper | Mac |
| `apple-notes-ro` | Fleet-OPS | `/global-api-keys` | the Apple Notes helper's Pushover credentials | Mac |
| `fleetlink-email-ro` | Fleet-OPS | `/global-api-keys` | the FleetLink email helper | Mac |
| `hetzner-availability-ro` | Fleet-OPS | `/global-api-keys` | the Hetzner availability watcher | Mac |
| `hetzner-cx43-watch-ro` | Fleet-OPS | `/global-api-keys` | the Hetzner price watch | Mac |
| `pushover-notify-ro` | Fleet-OPS | `/global-api-keys` | the Pushover notification path | Mac |
| `r2-usage-monitor-ro` | Fleet-OPS | `/global-api-keys` | the R2 usage monitor | Mac |
| `fleet-ops-sheet-ro` | Fleet-OPS | `/google-sheets-sa` | the operator sheet helper | Mac |
| `fleet-apps-sheet-sync-ro` | Fleet-OPS | `/google-sheets-sa` | the fleet apps sheet sync | Mac |
| `fleet-tools-mm-sheets-ro` | Fleet-OPS | `/google-sheets-sa` | the fleet reference sheet helper | Mac |
| `fleet-tools-mm-asc-ro` | Fleet-OPS | `/asc` | the App Store Connect helper | Mac |
| `operator-thread-registry-ro` | Fleet-OPS | `/operator-thread-registry` | the session registry drain | Mac |
| `icloud-mail-ro` | Fleet-OPS | `/icloud-mail` | the iCloud mail client | Mac |
| `botfleet-server-ro` | Fleet-OPS | `/` | the BotFleet harness start script's identity names | Mac |
| `usage-monitor-collectors-ro` | Fleet-OPS | `/global-api-keys` | the Usage Monitor collectors | Mac |
| `usage-monitor-collectors-global-ro` | Fleet-OPS | `/` | the Usage Monitor collectors' root-level names | Mac |
| `fleet-rag-fleetops-ro` | Fleet-OPS | `/global-api-keys` | the recall reader's Fleet-OPS reader | Mac + server |

`github-pat-ro` is the one account two callers share:  the shell's `GH_TOKEN` resolution and the iOS publish's
fallback read the same credential from the same folder on the same host,  so they are rotated together.

A grant covers exactly its folder and no other:  a token scoped to `/` does not reach a subfolder, so a consumer that
reads two folders holds two tokens.  The Fleet-OPS project root is a cross-folder alias layer, so a name at the root
is a reference to a name in a subfolder and the raw route returns the reference rather than the value;  the shared
client refuses that, so every consumer here is held to the folder its own names live in.  The root-wide grants that
remain are the ones whose names really are literals at the root.

## What stays

`scripts/fleet_infisical.py` is the shared client and it stays: it is service-token only, it verifies the grant
and the answer provenance, and it ships in the fleet-cli release bundle.  It is the library the consumers call,
wherever they live.

## The retired machine identity

The Mac System Keychain held `INFISICAL_ST_CLIENT_ID` and `INFISICAL_ST_CLIENT_SECRET` under the item
`com.jays.agent-bar.mac.infisical-identity`, and the stale installed reader authenticated with it
(`auth_mode: system-keychain`).  That is the universal-auth identity AFC-752 retired.  The item is gone from the
Mac's login keychain, and the reader that used it is gone from both hosts.

## Done means

- No file in this repository calls the `fleet-secrets` command, and no live file on either host does either.
- Every consumer that needs a secret holds its own scoped token and reads through `fleet_infisical` or the same
  REST route.
- Both hosts have no `fleet-secrets` binary and no reader package.
- The Mac retired identity item is gone.
- The doctor reports no `fleet-secrets` row, and the fleet compliance checks no longer start the command.
- Every grant above names the folder the consumer's own names live in, and every consumer's read is proved against
  that folder.
