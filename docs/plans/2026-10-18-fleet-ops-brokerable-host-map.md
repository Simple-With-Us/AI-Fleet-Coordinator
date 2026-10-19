# Fleet-OPS credentials mapped to brokerable hosts, and the Agent Vault versus Agent Proxy decision

Linear:  OPS-969 (`[AFC][MM]`), a Phase 2 preparation slice of OPS-818.  Seat:  MM.  Sun, Oct 18.
Continues Antigravity's `~/.secrets & global-api-keys Deprecation & Migration Catalog` (chat `181cffaf-ac9d-45fc-b0b3-ba5e29175f3e`) and reads the CLAUDE plan `2026-10-18-infisical-agent-proxy-adoption.md`.

Names only.  No secret value was read, printed or written by this work.

## Why this slice

Antigravity's catalog answers *which files touch `~/.secrets`*.  A credential broker is configured the other way round: per host, per scheme.  Nobody had produced that other half, so Phase 2 had no input to start from.  This does that mapping, plus two corrections that change what the pilot should be built on.

## Correction 1:  Agent Vault is the recommended vehicle, and it is not sandboxed

Infisical's Agent Proxy page now opens with "For new setups, we recommend using Agent Vault.  Use Agent Proxy only if you need a sandboxed local proxy that runs on your own computer, or if you want the proxy to apply a dynamic secret."  Their blog says Agent Proxy "has evolved into Agent Vault".

That matters because the OPS-818 plan's Phase 2 picked the vehicle partly on a measured Agent Proxy drawback.  Measured on this Mac, Oct 18:  `127.0.0.1` is unreachable inside `secrets agent-proxy run`, even with `--allow-host`, so local MCP servers and hooks break.  Agent Vault does not have that problem:

| | Agent Proxy `connect` | Agent Vault `run` |
|---|---|---|
| Sandbox | no | no |
| Loopback | reachable | reachable; `--no-proxy` always merges `localhost` and `127.0.0.1` |
| Agent credential | universal-auth client id and secret (`--client-id`, `--client-secret`) | a session token minted over a named access bundle |
| Revocation | leases on a poll | session revoked when the agent exits unless `--keep-session`; TTL default `7d` |
| Scoping | proxied services on a folder, one host per folder | access bundles, with per-service method and path rules |
| CA | local CA written on first run, expires in about a week | fetched from the proxy on every run; `--ca-fingerprint` pins it |
| Traffic policy | `--unmatched-host allow\|block` (default allow) | policy set in Infisical, arrives on each poll |

The plan's item 9 already asked for exactly this look before Phase 2 commits to either.  Answer:  commit to Agent Vault unless a reason appears to prefer the sandbox, and there is not one for the fleet's terminal agents, because the one thing the sandbox adds is exactly what breaks them.

**Cost.**  Agent Proxy "is part of any plan".  Agent Vault is a Pro add-on:  5,000 brokered requests a month included, then $2 per 1,000.  A counted request is one where the proxy attaches a real credential.  That is the owner-level decision this slice cannot make.

## Correction 2:  the agent identity must not be able to read what the broker hands it

`secrets agent-proxy connect` carries the guardrail `--allow-readable-brokered-secrets`:  it refuses to start "if the agent can read secrets that proxied services broker to it".  So the agent-side identity cannot also hold read access to the folder the credential lives in, or the pilot cannot start without overriding its own guardrail.

Fleet-OPS `/agent-proxy` already holds both halves as six keys:

```
INFISICAL_AGENT_PROXY_CLIENT_ID, INFISICAL_AGENT_PROXY_CLIENT_SECRET, INFISICAL_AGENT_PROXY_IDENTITY_ID,
INFISICAL_AGENT_CLIENT_ID,       INFISICAL_AGENT_CLIENT_SECRET,       INFISICAL_AGENT_IDENTITY_ID
```

The proxy identity reads secrets and leases;  the agent identity routes only.  OPS-818 records that their scopes are unchecked.  **Verifying those two memberships is the gate on the pilot** and is the next thing to do;  it is a dashboard read, no writes.

## The map:  72 folders, 667 keys, and most of them are not brokerable

A broker attaches a credential to an outbound HTTPS request.  That single sentence excludes a large part of this vault.  Sorted by why.

### A.  Brokerable as they stand  (HTTPS, one header or basic credential)

| Folder | Keys | Host | Scheme |
|---|---|---|---|
| `/zulip`, `/zulipadmin` | 143 | `simplewithus.zulipchat.com` | Basic, `email:API key` (admin uses the same host) |
| `/linear`, `/linear-echo-app-token`, `/infisical-echo-linear` | 91 | `api.linear.app` | Bearer |
| `/backblaze-app-keys` | 32 | `api.backblazeb2.com` | Basic, `keyID:applicationKey` (four buckets;  the rest is config, not a secret) |
| `/nylas` | 7 | `api.nylas.com` | Bearer |
| `/comet-chat` | 4 | `api.comet.chat` | Bearer, `COMETCHAT_REST_API_KEY` |
| `/alphavantage-keys` | 6 | `www.alphavantage.co` | query parameter `apikey` |
| `/pagerduty-api-key` | 1 | `api.pagerduty.com` | Bearer |
| `/cloudflare` | 1 | `api.cloudflare.com` | Bearer |
| `/github-runner-pat`, `/github-org` | 4 | `api.github.com`, `sentry.io` | Bearer |
| `/fleetlink-auth`, `/fleetlink-handbook`, `/fleet-share-hmac`, `/jet-wake-hmac` | 7 | `fleetlink.online`, Zulip | Bearer / HMAC header |
| `/botfleet-sentry`, `/sentry-platform-split-2026-09-08` | 7 | `sentry.io` | DSN and auth token |
| `/botfleet-firefighter-webhooks`, `/botfleet-housekeeper-webhook`, `/uptimerobot-botfleet-webhook` | 11 | PagerDuty and Sentry endpoints | inbound webhook secret |
| `/accesskey`, `/agents-jays-services-access-service-token`, `/acp-jays-services-access-service-token`, `/cursor-admin`, `/st-peer-token`, `/meta-muse-code-api-token`, `/coolify-deploy-only-token`, `/coolify-hetzner-api-token`, `/coolify-hetzner-api-token-stats`, `/coolify-ct-github-webhook-secret` | 16 | the service each belongs to | Bearer |
| `/ct-r2-weekly-archive` | 4 | `r2.cloudflarestorage.com` | Cloudflare R2 account token |

Two shapes need splitting before they can be configured:

- **`/zulip` is 140 keys for about 46 seats** (`<SEAT>_ZULIP_EMAIL`, `_KEY`, `_SITE`).  One host can be mapped once per folder, so each seat needs its own folder (`/zulip/mm`, `/zulip/claude`, …) and its own proxied service.  That is the whole reason the pilot's first service is Zulip and not something simpler:  it is the case that proves the per-seat pattern.
- **`/alphavantage-keys` is six keys called `KEY1`..`KEY6`.**  They are brokerable, and six undifferentiated names in one folder is a rotation hazard.  Name them per consumer before configuring.

### B.  Brokerable only after a transform

| Folder | Keys | Why it is not a header | Transform |
|---|---|---|---|
| `/apple`, `/appstore-connect`, `/asc` | 14 | App Store Connect needs a **JWT signed by the p8 private key**, and one name is a *file path* (`ASC_PRIVATE_KEY_PATH`) | A daemon mints the JWT;  the proxy can then broker the short-lived bearer, never the p8 |
| `/google-sheets-sa` | 1 | a service-account JSON document, not a token | mint an OAuth access token at start and broker that |
| `/agents-gmail`, `/usage-monitor-receipt-inbox-addresses`, `/usage-monitor-receipt-inbox-secrets`, `/receipt-inbox-forward-to` | 6 | Google OAuth refresh tokens | same:  mint, then broker the access token |

The p8 private keys, the service-account JSON and the refresh tokens are the real prize and the real risk, and **none of them can be a brokered credential**.  They are Phase 3 (`infisical run` / `fleet-secrets run`), not Phase 2.

### C.  Not brokerable at all

| Folder | Keys | Reason |
|---|---|---|
| `/icloud-mail` | 3 | an app-specific password over **IMAP and SMTP**;  the broker only intercepts HTTPS |
| `/mac-certs`, `/apple-certs`, `/codecaps-sparkle-ed25519-key`, `/ios-dist-p12-pass`, `/botfleet-developer-id-export`, `/authkey-*` (4), `/ct-authkey-*`, `/st-authkey-*` | 18 | certificates, private keys and their passphrases:  signing and verification material, never a bearer |
| `/github` | 1 | `GITHUB_MA_TOTP_SECRET`, a TOTP shared secret |
| `/congress-sqlite-web-password`, `/coolify-hetzner-admin-password`, `/coolify-login-hetzner` | 3 | human or admin passwords |
| `/seat-mcp`, `/mac-collab`, `/grok-acp`, `/provider-knob-sync`, `/senate-relay`, `/agent-sync` | 28 | loopback or fleet-internal services;  the proxy bypasses `localhost` and `127.0.0.1` by design |
| `/oracle-cloud-api-key`, `/oracle-usage-monitor` | 2 | cloud-console and billing credentials, not an agent API call |
| `/usage-monitor*`, `/backblaze-monitor`, `/cloudflare-jay-email`, `/receipt-lifecycle-audit-token` | 20 | telemetry and audit, read by daemons that never leave the Mac |

### D.  Not a broker unit

`/global-api-keys` (231) and `/agent-proxy` (6) are not services.  The first is the legacy grab-bag every folder above was carved out of;  it has to be decomposed by host before any of it means anything to a broker.  The second is the broker's own identity material.

## What this bounds

Every key in Fleet-OPS `prod` was classified, and the arithmetic closes at 667:

| Bucket | Keys | Share |
|---|---|---|
| A,  brokerable as they stand | 334 | 50% |
| B,  brokerable after a mint | 21 | 3% |
| C,  not brokerable at all | 75 | 11% |
| Not a broker unit:  `/global-api-keys` (231) and `/agent-proxy` (6) | 237 | 36% |

And even inside bucket A most of the 32 Backblaze keys are bucket names, endpoints and download URLs rather than credentials, so the true count of individual brokered secrets is well under 334.

**So the broker cannot be the thing that retires `~/.secrets`.**  It covers roughly the half that matters most for a prompt injection, `fleet-secrets run` covers the rest, and the p8 private keys, service-account JSON and OAuth refresh tokens — the ones worth stealing most — cannot be brokered at all.  That is worth stating plainly now, because Phase 5 of the OPS-818 plan gates the cutover on retiring the directory, and the directory is not retiring through the proxy.

## The first three services

Configuration, not yet created.  Each needs a folder holding only its own credential and a service bound to one host.

1. **`/zulip/mm` → `simplewithus.zulipchat.com`, Basic.**  Proves the per-seat pattern.  Needs `MM_ZULIP_EMAIL` and `MM_ZULIP_KEY` copied out of `/zulip`, and `/zulip` itself left readable only by the proxy identity.
2. **`/github-mm` → `api.github.com`, Bearer.**  Proves a plain API token.  Needs a **scoped** PAT for the MM seat, not Jay's, or the broker hides a credential that can still do everything Jay can.
3. **`/fleetlink-auth` → `fleetlink.online`, Bearer.**  Already folder-scoped, so it is the cheapest of the three and can go first while the other two are set up.

Proof for each:  run the seat through the proxy and confirm the API returns 200 while the agent's environment and context contain no real credential.  `--ca-fingerprint` on the Agent Vault side, pinned from the Proxies page.

## What I did not do

No Fleet-OPS writes, no folder or service created, no identity changed, no key rotated, no LaunchAgent touched, no `~/.secrets` file read.  Reading was names-only through `fleet-secrets names`, which is the same access any migrated job now has.

## Next

1. Check the two `/agent-proxy` identity memberships against the guardrail above.  Dashboard read, no writes.
2. Jay decides the plan:  Agent Vault on Pro, or Agent Proxy free with the sandbox drawback that breaks local MCP and hooks.
3. Then the three services above, one at a time.