# alert-ingress

One door for every inbound signal into Zulip, replacing per-vendor webhooks that each post one message per event.

## Why this exists

`#alerts` became unreadable.  The measured cause on Sat, Oct 18, 2026 was 30 messages in under 7 hours — 20 of them from **one** Datadog monitor (`System load is high for host Mac.Jays.Services`) flapping Triggered/Recovered every 10-60 minutes, each body a full default Datadog template over 1 KB.

That is not really "too many alerts".  Every inbound integration shared one delivery model:

- **one new message per event**, so a state that had not changed still produced chatter;
- **no shared state**, so nothing could know a repeat delivery was not news;
- **a topic pinned in the webhook URL**, so every monitor from a vendor landed in one bucket (`zulip-fleet-guide.md` says topic = service name; the routing plan says `?stream=&topic=<service>`);
- **an incoming-webhook bot**, which Zulip documents as "limited to only sending messages into Zulip" — so no integration could ever keep a message current.

Reducing alert volume treats the symptom.  This changes the model.

## The model

```
inbound event -> normalize() -> state machine -> decide()
                                          |
                     state CHANGED ---------+---------> post one line
                     (open/escalate/resolve)
                     state UNCHANGED -------+---------> rewrite the standing status
                                                            (PATCH, in place)
```

A message is only warranted when **state** changes.  The channel ends up as:

- **one standing status block per topic**, edited in place, always showing current truth; and
- **a sparse timeline** of events that actually mattered.

## Routes

| Route | Purpose |
|---|---|
| `POST /webhook/:source` | vendor event; signature verified, replies immediately |
| `GET  /status` | **machine-readable** status for any consumer that renders it |
| `GET  /status/:channel/:topic` | the rendered standing status, as Markdown |
| `GET  /sweep` | run the promotion sweep by hand |
| `GET  /health` | liveness |

Sources with a bespoke parser: `datadog`, `betterstack`.  Anything else falls back to a conventional-key reader.  The route's source name is authoritative, so a payload cannot claim to be another integration.

## One writer, many readers

The ingress is the **only writer** of alert state.  `GET /status` publishes it, so any surface can render the same truth rather than re-deriving health independently and drifting:

```
GET /status
{
  "generatedAt": "2026-10-18T22:40:00.000Z",
  "channel": "alerts",
  "summary": { "topics": 2, "alerts": 3, "firing": 1 },
  "topics": {
    "Mac.Jays.Services": {
      "firing": 1,
      "total": 2,
      "alerts": [
        {
          "key": "datadog:22024797",
          "service": "Mac.Jays.Services",
          "phase": "alerting",
          "severity": "MODERATE",
          "text": "System load is high",
          "link": "https://us5.datadoghq.com/monitors/22024797",
          "since": 1792358697000,
          "updated": 1792360437000,
          "prodDown": false
        }
      ]
    }
  }
}
```

Contract rules, enforced by `status.test.js`:

- only `phase: "alerting"` may surface a non-`INFO` severity, so a held incident never reads as alarming;
- alerts sort firing → pending → ok;
- a topic whose states have all cleared is **dropped**, so the view self-cleans as incidents resolve;
- the payload carries no credential, ever.

**Live consumer:** the Usage Monitor `/ops` page (`usage.jays.services/ops`) renders this as its Fleet Alerts card, so the dashboard and the Zulip channel cannot disagree.

## Signature verification

No verifier configured means **reject**, never trust:

- **Datadog** — the custom header `X-Datadog-Token` set to `DATADOG_WEBHOOK_SECRET`, as supported by [the provider's webhook configuration](https://docs.datadoghq.com/integrations/webhooks/).  Datadog cannot sign an outgoing body, so a body HMAC alone could never be satisfied; a real `x-datadog-signature` HMAC-SHA256 is still accepted for senders that can produce one.
- **BetterStack** — configure the custom header `X-BetterStack-Token` to match `BETTERSTACK_WEBHOOK_SECRET`, as supported by [the provider's outgoing-webhook documentation](https://betterstack.com/docs/errors/integrations/outgoing-webhooks/).
- **anything else** — `Authorization: Bearer <token>` matching `INGRESS_SHARED_TOKEN`.  Operator routes use the same header; query-string secrets are rejected.

Stable alert keys retain their first service/topic assignment when vendor labels change, preserving one membership and one standing status.

## The wait window

An incident is held, not posted, until it is still open after `MIN_OPEN_MS` (default 10 minutes; 5 for `prodDown`, matching `zulip-fleet-guide.md`).  A flap that resolves inside the window is **discarded entirely** — nobody saw it open, so nobody needs to see it close.  The `*/5` cron promotes anything that aged out.  That single rule is what turns a flapping monitor from twenty messages into one.

## KV layout (`ALERT_STATE`)

| Key | Value |
|---|---|
| `state:<key>` | `AlertState` — phase (`ok`/`pending`/`alerting`), severity, since, text, link |
| `svc:<key>` | service name, so a topic stays stable |
| `topic:<channel>:<topic>` | topic membership; tracked explicitly rather than parsed out of `idx:` |
| `idx:<channel>:<topic>:<key>` | membership marker, so a status rebuild knows its rows |
| `status:<channel>:<topic>` | message id of the standing status, for the PATCH |

## Setup (owner)

1. Create a KV namespace and put its id in `wrangler.toml`.
2. Create a Zulip bot with an API key — **not** an incoming webhook, which cannot edit.  The bot posts to `#alerts` only.
3. `wrangler secret put ZULIP_BOT_EMAIL`, `ZULIP_BOT_API_KEY`, `INGRESS_SHARED_TOKEN`, then `DATADOG_WEBHOOK_SECRET` and/or `BETTERSTACK_WEBHOOK_SECRET`.
4. Deploy, then re-point each vendor's webhook at `https://alert-ingress.<account>.workers.dev/webhook/<source>` and set the matching secret on the vendor side.
5. Stop the old Zulip incoming-webhook bots for that source, or both will post.

## Tests

```bash
cd workers/alert-ingress && node --test src/
```

Pure functions, no Workers runtime needed.  The suite includes the real Oct 18 flap replayed: ten events, zero messages until it genuinely stays open.

## Deliberate non-goals

- Not a pager.  `zulip-fleet-guide.md` keeps PagerDuty as the pager; this is a readable record.
- Not a host-metrics collector.  It routes and coalesces what vendors already send.
- No vendor credential ever enters a request; all signing material is a Worker secret.

## Durable Writer Deployment Gate

Source review does not activate this ingress.  The shipped configuration intentionally has no live KV namespace and no `ALERT_WRITER` binding; every nonhealth route fails closed with HTTP 503 until an approved deployment supplies both.  No resource, credential, webhook or cron was activated by this change.

`AlertWriter` is a single named Durable Object (`alerts-v1`) that serializes complete state reads, transitions, inventory rendering and Zulip writes.  Its durable storage overlays the existing KV records; historical KV records remain readable and are never deleted or migrated destructively.  All status readers route through the same object, avoiding stale KV reads after mutation.  A KV get/put lock is not used.

An approved deployment must bind `ALERT_WRITER` to exported class `AlertWriter` and declare its SQLite class migration in its private Wrangler deployment configuration, in addition to the existing `ALERT_STATE` binding.  This repository does not provision those resources.  The existing deployment-boundary decision remains explicit in the PR; a merge does not establish a production runtime.

Before any standing-message write the object persists an `unknown` delivery outcome keyed by channel/topic.  Only a validated successful response records `ok` with a positive integer message ID.  Timeout, malformed success, crash and ambiguous network failure retain `unknown`, and subsequent refreshes do not replay it.  Explicit provider refusal records `refused` and also requires operator reconciliation; no automatic create follows a failed edit.  An operator must inspect the actual Zulip message before repairing a held outcome.

Inventory walks drain at most 20 pages and 2,000 keys, reject missing/cyclic cursors and never publish a partial inventory as complete.  The writer returns HTTP 503 with `stale: true` when inventory fails.  Reads use bounded batches of four; independent sweep status-refresh failures emit only a fixed event name and leave other topics eligible for processing.

Webhook JSON must be an object with bounded scalar fields, bounded string tags and object-shaped `alert`/`data` envelopes.  Better Stack's default incident body is JSON:API shaped, so `attributes` and `relationships` are object envelopes too and envelope nesting is depth-bounded; a correctly authenticated default delivery is accepted rather than rejected as an oversized text field.  Invalid field shapes, control bytes and bodies over 64 KiB fail before normalization or side effects.  These dependency-free boundary validators match the worker's existing stdlib-only packaging; no unchecked object is passed into normalization.  Zulip response fields are also validated before an ID can be accepted.

The concrete manifest is `wrangler.durable-writer.example.toml`.  Before deployment, copy it to an owner-controlled configuration beside the template, set its KV ID to the verified existing historical namespace, confirm the Cloudflare account and Worker name, and review the `alert-writer-v1` migration that creates only `AlertWriter`.  Inject the existing named secrets through the approved secret workflow; never put values in this manifest.  Deploy using `wrangler deploy --config <private-config-path>` only after those scope checks.  Verify health, authenticated status, existing historical records and a synthetic no-send fixture before moving webhook routes; no existing listener or model session needs restarting.  Keep the old Worker version and KV namespace available for rollback, and preserve the new durable outcome records when rolling back so unknown sends cannot replay.

Timeline transitions use a separate durable outcome keyed by the exact normalized transition and previous state.  Destination inventory is completed before mutation; a definitive pre-send failure leaves the previous phase unchanged.  Timeline `unknown` is never resent, while a definitive provider refusal can retry the same transition safely.  Sweep failures are isolated per incident, so one refusal cannot abort independent promotions.

Better Stack Uptime incident-change payloads support the default data.attributes resolved_at field as well as explicit status; optional monitor relationship IDs retain stable keys.
