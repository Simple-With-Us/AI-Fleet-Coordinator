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
| `GET  /sweep?token=…` | run the promotion sweep by hand |
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

- **Datadog** — HMAC-SHA256 of the raw body in `x-datadog-signature`, compared in constant time.
- **BetterStack** — HMAC-SHA256 in `x-betterstack-signature`.
- **anything else** — `?token=` matching `INGRESS_SHARED_TOKEN`.

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