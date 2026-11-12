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
                     state UNCHANGED -------+---------> nothing, unless the block's content
                                                            changed (then PATCH, in place)
```

A message is only warranted when **state** changes, and the standing block is only rewritten when what it says changes.  The channel ends up as:

- **one standing status block per topic**, edited in place, always showing current truth (a recovered signal is counted in its header but not drawn as a row); and
- **a sparse timeline** of events that actually mattered.

Three channels, one topic per service in each, and every channel requires topics (owner, Oct 29, 2026):

| Channel | Holds |
|---|---|
| `#Alerts` | only the standing status blocks, the distilled view |
| `#Alerts-Log` | the sparse one-line timeline |
| `#Alerts-Raw` | every authenticated delivery as the vendor sent it:  its own title and full text, then the whole payload in a collapsed spoiler |

The raw copy is a record, not a decision.  It is posted for every authenticated, well-formed delivery except a PagerDuty ping and a delivery that arrives during a Zulip pause or a storm shed (see [Usage guards](#usage-guards)), under the topic the event is filed under (its pinned service), and it runs beside the event path, so a failure to post it never changes how the event is handled.  A body the validator refuses gets no raw copy:  the 400 tells the vendor, and input that failed the size, depth and character checks is not posted into Zulip.  Vendor text cannot ping anyone (user, silent and group mention syntax is broken with a zero-width space), and every credential shape is replaced with `[redacted]` in each string value before the payload is encoded, so a token after an encoded newline is caught too.  A private key goes through its END marker, or to the end of the value if that marker is missing, so its body goes with it.  It goes through the normal messages API with the vendor's status bot, because Zulip's own vendor integrations take the key only in the URL.  A copy that fails logs `alert.raw_copy_failed` with Zulip's status and outcome, and a 2xx with no usable id logs `alert.raw_copy_unconfirmed`.

A resource alert tags people (owner, Oct 30, 2026).  When one of Datadog's host monitors for memory, network, disk latency, load or the disk forecast opens or escalates, its #Alerts-Log line carries a second line tagging both Housekeepers and Jay.  CPU monitor 22024794 tags only SEVERE saturation immediately; warnings and recoveries stay quiet.  Datadog already requires a 90% busy average over five minutes, so this monitor skips the ingress's additional ten-minute hold.

## Routes

| Route | Purpose |
|---|---|
| `POST /webhook/:source` | vendor event; signature verified, and it replies once the event is recorded and its Zulip calls have finished |
| `GET  /status` | **machine-readable** status for any consumer that renders it, kept in memory for 30 seconds; a method other than `GET` or `HEAD` gets `405` |
| `GET  /status/:source/:channel/:topic` | rebuild one source's standing status, with that source's credential |
| `GET  /status/:channel/:topic` | rebuild every source's standing status for that topic |
| `GET  /sweep` | run the promotion sweep by hand |
| `GET  /health` | liveness |

Sources with a bespoke parser: `datadog`, `betterstack`, `sentry`, `pagerduty`.  Anything else falls back to a conventional-key reader.  The route's source name is authoritative, so a payload cannot claim to be another integration.

Both refresh routes are operator-only (the shared bearer token) and both refuse any channel other than `alerts`: the bot only ever writes into the alerts channel, so a standing message can never be written anywhere else.  The two-segment route carries no vendor dimension, so it refreshes every source that tracks that topic, each with its own credential; the three-segment route names one source and refreshes only that one.  A delivery that is held is answered with `409` and `{"error": "status held"}` rather than a silent success, and `GET /status/:source/:channel/:topic?reconcile=<message id>` adopts a standing message id an operator has inspected, which is the one way a held topic is released.  The id is read back from Zulip before it is adopted:  a message that is not this source's standing block in that topic answers `409`, and an id Zulip refuses or never answers for answers `503` with `{"error": "message could not be read"}`, never the writer's generic outage.

## Vendor routing

Every source has its own identity, its own state namespace and its own standing message.  Two vendors that share a service name cannot edit each other's message, and one vendor can never post as another.

**Credentials.**  Each source resolves its own pair, and there is no path from one source to another source's bot:

| Source | Credential names |
|---|---|
| `datadog` | `ZULIP_BOT_EMAIL_DATADOG` / `ZULIP_BOT_API_KEY_DATADOG` |
| `betterstack` | `ZULIP_BOT_EMAIL_BETTERSTACK` / `ZULIP_BOT_API_KEY_BETTERSTACK` |
| `sentry` | `ZULIP_BOT_EMAIL_SENTRY` / `ZULIP_BOT_API_KEY_SENTRY` |
| `pagerduty` | `ZULIP_BOT_EMAIL_PAGERDUTY` / `ZULIP_BOT_API_KEY_PAGERDUTY` |
| `generic` | `ZULIP_BOT_EMAIL_GENERIC` / `ZULIP_BOT_API_KEY_GENERIC`, or the legacy `ZULIP_BOT_EMAIL` / `ZULIP_BOT_API_KEY` pair |
| any other route name | `ZULIP_BOT_EMAIL_<ROUTE>` / `ZULIP_BOT_API_KEY_<ROUTE>` |

The legacy un-suffixed pair **is** the generic source's own identity.  It is not a wildcard fallback for any vendor: a source with no pair of its own fails closed, posts nothing, writes no status, and answers with a structured error (`{"error": "credential unavailable", "source": "..."}`).  A route name is its own source, so a custom route needs a custom pair.

**State.**  Every key that can be shared between two vendors carries its source, so the state of one vendor can never be read, refreshed or swept as another's.  The standing status of a topic belongs to exactly one vendor: a vendor never PATCHes a message id another vendor pinned (`statusowner:<id>` records the pinner) and never adopts one.  The first refresh after this change creates a fresh standing message per vendor; the pre-change shared `status:<channel>:<topic>` id is left unread rather than adopted.

A message id that **no** source owns is neither adopted nor posted over.  It may be this source's own standing message from a write whose acknowledgement was lost, and posting a second one would leave two standing messages in the topic, so the refresh is held with `409` until an operator reconciles it.  A message id that **another** source owns is left alone, and the asking source posts its own standing message instead.

**Deployment precondition.**  The per-vendor credentials must exist in Infisical before a vendor source can deliver, because a vendor source with no pair of its own fails closed instead of borrowing the shared bot.  `scripts/alert-ingress/provision-secrets.py` copies them into the Worker's bindings;  Infisical stays the source of truth and the Worker holds a copy.

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
          "source": "datadog",
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
  },
  "sources": {
    "datadog": {
      "summary": { "topics": 1, "alerts": 2, "firing": 1 },
      "topics": { "Mac.Jays.Services": { "firing": 1, "total": 2, "alerts": [] } }
    }
  }
}
```

Contract rules, enforced by `status.test.js`:

- only `phase: "alerting"` may surface a non-`INFO` severity, so a held incident never reads as alarming;
- alerts sort firing → pending → ok;
- a topic whose states have all cleared is **dropped**, so the view self-cleans as incidents resolve;
- the payload carries no credential, ever.

The top-level shape above is unchanged.  A `sources` breakdown is added beside it, so a consumer can read one vendor's view without re-deriving it: `sources.<source>.summary` counts that source's topics, alerts and firing signals, and `sources.<source>.topics` is the same per-topic shape for that source alone.  The flat `topics` map stays the fleet-wide view, and every alert row names the source that owns it.

**Live consumer:** the Usage Monitor `/ops` page (`usage.jays.services/ops`) renders this as its Fleet Alerts card, so the dashboard and the Zulip channel cannot disagree.

## Signature verification

No verifier configured means **reject**, never trust:

- **Datadog** — the custom header `X-Datadog-Token` set to `DATADOG_WEBHOOK_SECRET`, as supported by [the provider's webhook configuration](https://docs.datadoghq.com/integrations/webhooks/).  Datadog cannot sign an outgoing body, so a body HMAC alone could never be satisfied; a real `x-datadog-signature` HMAC-SHA256 is still accepted for senders that can produce one.
- **BetterStack** — configure the custom header `X-BetterStack-Token` to match `BETTERSTACK_WEBHOOK_SECRET`, as supported by [the provider's outgoing-webhook documentation](https://betterstack.com/docs/errors/integrations/outgoing-webhooks/).
- **Sentry** — the `sentry-hook-signature` header, the lowercase hex HMAC-SHA256 digest of the raw request body under `SENTRY_WEBHOOK_SECRET`.  Sentry's integration platform signs the body; it does not send a static secret header, so a `sentry-hook-secret` header is not the contract.  A Sentry request with no Sentry secret configured is **refused**, never served by the shared token: the shared token is the generic path, and letting it stand in for Sentry would let any holder of it post as the Sentry identity.
- **PagerDuty** — a v3 webhook subscription created with the custom header `X-PagerDuty-Token` set to `PAGERDUTY_WEBHOOK_SECRET`.  PagerDuty also signs every body with the secret it generates for the subscription (`X-PagerDuty-Signature: v1=<hex>`, several during a rotation), and that is accepted when `PAGERDUTY_SIGNING_SECRET` holds it.  Like Sentry, the source is refused rather than served by the shared token.
- **anything else** — `Authorization: Bearer <token>` matching `INGRESS_SHARED_TOKEN`.  Operator routes use the same header; query-string secrets are rejected.

Stable alert keys retain their first service/topic assignment when vendor labels change, preserving one membership and one standing status.

## PagerDuty

A PagerDuty incident is a row in the standing block of its PagerDuty service, in `#alerts` like every other source (owner, Oct 29, 2026, which settles AFC-472).  PagerDuty stays the pager:  it still pages phones on its own;  this is the readable record, and `#incident-response` is left to people.  The incident id is the key, so an acknowledgement, a reassignment or a priority change edits the row instead of posting, and a resolve closes it.  The incident's own status decides firing, not the event type, so a reassignment of a resolved incident cannot reopen it.  P1 reads SEVERE and P2 MODERATE;  otherwise high urgency reads SEVERE and low urgency MODERATE.  A closed incident is pruned:  its state, service, source and membership records are deleted and a `closed:<key>` tombstone is left, so the standing block and the inventory hold only open incidents instead of every incident ever opened, which would grow until the 2,000-key inventory bound refused the source.  The tombstone is never listed;  a late retry of the original trigger is answered `ignored`, and only `incident.reopened` revives the incident.  A ping, or an event about something that is not an incident, is answered 200 with `"action": "ignored"` and dropped, because a 4xx would make PagerDuty retry and then disable the subscription.

## The wait window

A fresh incident whose vendor names no severity opens SEVERE:  Better Stack never sends one and Datadog often does not, so a site that went down used to read INFO.  An open incident keeps its own severity when an update names none.

An incident is held, not posted, until it is still open after `MIN_OPEN_MS` (default 10 minutes; 5 for `prodDown`, matching `zulip-fleet-guide.md`).  A flap that resolves inside the window is **discarded entirely** — nobody saw it open, so nobody needs to see it close.  The `*/5` cron promotes anything that aged out, reading only the pending index.  That single rule is what turns a flapping monitor from twenty messages into one.

## Usage guards

A code bug or a vendor storm must not run up a bill or use up a cap the fleet shares.  All the caps below are constants in `src/breaker.js` and `src/index.js`.

**Off switch.**  `GUARD_MODE=off` answers 503 with `Retry-After` on every route, including `/health`, in the entry point and in the writer, and the cron does nothing.  The account watchdog sets it to stop this Worker.  Vendors retry and then give up, so events sent while it is off can be lost.

**Storm breaker.**  Past 60 deliveries a minute from one source, or 5,000 a day in all (each source keeps 500 a day past that, so one vendor's storm cannot lock the others out), a delivery makes no Zulip call and answers by what is safe, because a vendor's retry is its only redelivery:

| During a shed, a delivery for | Answer | Writes |
|---|---|---|
| A known incident, unchanged | `200` with `"action": "shed"` | none |
| A known incident that changed, such as a resolve | `200` with `"action": "shed"`, once its state is recorded | its state, up to 2,000 a day, then `503` |
| A new incident, or a reopen | `503` with `Retry-After` | none |

Every delivery shed or refused here is counted, and the count is saved with the breaker's.  One line a minute per reason and source goes to the logs, `alert.delivery_shed` or `alert.delivery_refused`, carrying the day's count, and `guards.shed` in `GET /status` shows the same numbers.  A breaker that cannot read its saved count answers `503` rather than start from zero, which would reopen every cap and then overwrite the day's real count.  A saved record that reads back corrupt does not refuse anything:  what is sound is kept, what is not reads as zero, the caps go on counting from there, one `alert.breaker_state_invalid` line is logged, and the next delivery overwrites the bad row.

The count lives in memory and is saved at most once a minute, and not at all when a wake carries fewer than five deliveries.  Each trip posts one notice to `#Alerts-Log`, topic `alert-ingress`, at most once an hour.  The next sweep rewrites each shed topic's block once.  A shed transition's timeline line is not posted later, because a storm's timeline is noise.  A long storm answering 503 can count against PagerDuty's rule that disables a subscription after repeated failures.

**New incidents.**  A delivery for an incident with no row is the dearest kind:  about 20 rows written with its promotion, against about 6 for a transition on a known incident.  So new incidents have a cap of their own, 1,000 a day (each source keeps 100 past that).  Past it a new incident answers `503` with `Retry-After`, writes no state, and is logged and counted under `new-keys`, with one notice an hour to `#Alerts-Log`.  Its raw copy still goes to `#Alerts-Raw`, so the delivery leaves a record even if the vendor stops retrying.  A known incident still moves.  A recovery of an incident never recorded is answered `200`, spends no allowance and writes no row, because there is nothing to record;  PagerDuty keeps its one tombstone row, which stops a late trigger from opening what the vendor already closed.  The allowance is spent only once the delivery is sure to be recorded, so a vendor retrying an inventory refusal or a Zulip refusal costs nothing.  The count is saved on every new incident, and a save that fails logs `alert.breaker_save_failed`, with the day's `total` and `newKeys` and the count still `unsaved`, at most once a minute.

With every cap hit at once, a day's worst case is about 85,000 rows written, against the free plan's 100,000.  That allowance is for the whole account, so other Workers' Durable Objects spend from it too.  `efficiency.test.js` pins this sum to the caps.

**Zulip 429.**  A 429 is not a refusal to retry at once.  It pauses that source's bot for `Retry-After` seconds, 60 if it gives none, and at most an hour.  During the pause the worker records state, makes no Zulip call and answers `200`, so the vendor does not retry into the limit.  The sweep then posts the held timeline lines, 10 per sweep, and rewrites each held block once.  A held line Zulip does not confirm stays queued and is sent again, 3 tries in all, one per sweep, so a line can repeat rather than go missing;  after the last try it logs `alert.queued_event_abandoned` and posts a notice.  A line sent straight away or at promotion is still sent once, and one Zulip does not confirm logs `alert.event_unconfirmed`, with the incident recorded and shown in its standing block.  Raw copies from the pause are not posted later.  A severe CPU alert that arrives during a pause waits out the ordinary hold.

**Writer unreachable.**  The entry point answers `503` with `Retry-After` and logs one `alert.writer_unreachable` line.  It never answers `200` here, since nothing was recorded.  A cron sweep that fails logs `alert.sweep_failed` or `alert.writer_unreachable`.

**Inventory overflow.**  Above the 2,000-key bound a new incident gets `503` and a notice, while a known incident still moves and reports `"statusDelivery": {"outcome": "overflow"}`.  An overflow of the state list stops only the reconcile, and promotions continue from the index.

**Sweep budget.**  One sweep makes at most 40 Zulip calls, below the free plan's 50 subrequests, and the rest wait for the next sweep.

**The sweep's cost.**  The sweep reads the `pending:` index, not every state, so an idle sweep costs a few rows instead of about eight per state.  The full scan that rebuilds the index and repairs legacy markers runs on the first sweep after deploy and then hourly, so a pending row the index missed, such as one written by older code after a rollback, waits an hour at most.  The scan writes only the entries that are missing.  A failed read of the index does not stop it:  it logs `alert.pending_index_failed`, promotes from the state rows it holds, adds each pending entry and removes none.  A failed write does not stop it either:  a delete that fails leaves the stale entry for a later pass, a stamp that fails means the next sweep runs the pass again, each logs `alert.pending_reconcile_write_failed`, and the promotions still run.  One sweep reads at most 500 index entries, and when there are more it saves where it stopped, so the next sweep takes the next 500 and every entry gets its turn.  The cron stays on `*/5` rather than moving to a Durable Object alarm:  it is 0.3% of the daily request cap, and a cron recovers by itself from a capped outage, where an alarm can use up its retries and lose the alert.

**The Zulip timer.**  Each call has a 5-second timeout on a timer cleared when the call ends.  `AbortSignal.timeout` cannot be cleared, and its pending timer held the object active for the full timeout after every call.

**`GET /status` does not rely on the Cache API**, which is only documented as working on custom domains, while this Worker runs on workers.dev.  Each isolate keeps the answer in memory for 30 seconds, so a burst wakes the writer at most once per window per isolate.  The writer keeps its own copy for the same window, until a delivery or a sweep ends it, so however many isolates ask, the full read runs about once per window.  An empty webhook body works the same way:  each isolate lets one through a day.  `x-status-cache: hit` or `miss` says which answer a reader got.

**`GET /status` stays unauthenticated**, because Usage Monitor's `/ops` reads it with no token.  It returns hostnames and links, so a bearer is still due, as a coordinated change with the Usage Monitor deploy landing first.

## KV layout (`ALERT_STATE`)

| Key | Value |
|---|---|
| `state:<key>` | `AlertState` — phase (`ok`/`pending`/`alerting`), severity, since, text, link |
| `src:<key>` | the source that owns the state, so the sweep resolves that vendor's credential |
| `svc:<key>` | service name, so a topic stays stable |
| `svcname:<source>:<service>` | alert key for service-derived generic keys, so a renamed vendor label re-joins its incident |
| `topic:<source>:<channel>:<topic>` | topic membership for one source |
| `idx:<source>:<channel>:<topic>:<key>` | membership marker, so a status rebuild knows its rows |
| `status:<source>:<channel>:<topic>` | message id of that source's standing status, for the PATCH |
| `statusowner:<message id>` | the source that pinned a standing message |
| `closed:<key>` | when a per-incident alert (PagerDuty) closed and was pruned;  never listed |

The durable delivery outcome of a standing status is keyed per source as well: `delivery:["<source>","<channel>","<topic>"]`, so a held write for one vendor never blocks another vendor's refresh.

Two scopes, two keys.  The **outcome** is scoped to a content revision, `delivery:["<source>","<channel>","<topic>","<revision>"]`, because an unchanged standing block must not count as a new delivery and re-PATCH forever.  The **uncertainty barrier** is scoped to the source, channel and topic alone, `delivery-barrier:<source>:<channel>:<topic>`, because a create whose acknowledgement was lost may already have landed, and a changed standing block must not post a second standing message on top of it.  The barrier carries the message id: an id means the write edits a message this source already owns and can be retried, and no id means a create may have landed and no further POST may happen for that topic until an operator reconciles it.

The writer's own storage also holds, outside this layout, `pending:<key>` (incidents in their hold), `zulip-pause:<source>`, `queued-event:<hash>`, `dirty-topic:["<source>","<topic>"]`, `notice:<kind>`, `breaker:v1`, `sweep:reconciled-at` and `sweep:pending-cursor`.

## Setup (owner)

1. Create a KV namespace and put its id in `wrangler.toml`.
2. Create a Zulip bot with an API key — **not** an incoming webhook, which cannot edit.  The bot posts to `#alerts` only.  Create one bot per vendor that needs its own standing message, and give each one its own pair of names.
3. Register `INGRESS_SHARED_TOKEN`, `DATADOG_WEBHOOK_SECRET`, `BETTERSTACK_WEBHOOK_SECRET` and/or `SENTRY_WEBHOOK_SECRET`, the `MIN_OPEN_MS` tunable, and one `ZULIP_BOT_EMAIL_*` / `ZULIP_BOT_API_KEY_*` pair per source (see [Vendor routing](#vendor-routing)) in the Infisical inventory (`INFISICAL.md`) -- Infisical is the sole source of truth for secrets and settings knobs -- then provision the Worker bindings from Infisical through the secure owner handoff documented there.  A bare `wrangler secret put` creates Worker-only copies that Infisical rotations will not govern.
4. Deploy, then re-point each vendor's webhook at `https://alert-ingress.<account>.workers.dev/webhook/<source>` and set the matching secret on the vendor side.
5. Stop the old Zulip incoming-webhook bots for that source, or both will post.


## Tests

```bash
cd workers/alert-ingress && node --test src/*.test.js
```

Pure functions, no Workers runtime needed.  The suite includes the real Oct 18 flap replayed: ten events, zero messages until it genuinely stays open.

The test files are named explicitly on purpose.  On Node 24 the directory form (`node --test src/`) resolves `src/index.js` as a single module and reports one passing test without running any test file, so a gate written that way proves nothing.  The CI step uses the form above, and it runs 281 tests.  `resource.test.js` checks who a resource alert tags:  each resource monitor with or without a scope, CPU severity gating and immediate severe notification, other vendors or a look-alike id, the mentions on their own line under a promoted alert, and none on recovery.  `raw.test.js` covers the raw copy:  each vendor's title and text, the payload spoiler, mentions made harmless, credentials redacted, a private key's body with them, fences a payload cannot close or inflate, and the 10,000-character Zulip limit.  `writer.test.js` checks that the legacy KV is listed once and never again, and that a failed copy reads through and waits ten minutes to retry.  `efficiency.test.js` covers the usage guards:  the cleared Zulip timer, unchanged deliveries, the pending index, Zulip 429s, the breaker, inventory overflow, and the entry point's off switch, empty-body refusal, writer failures and `/status` copy, the new-incident cap, the breaker's state and the worst-case row sum.  `budget-guard-run.test.js` pulls in the shared guard's `.mjs` tests, which the `*.test.js` glob would otherwise miss.  `pagerduty.test.js` covers the PagerDuty source:  the incident key and its service topic, acknowledged and resolved, the status deciding over the event type, the priority and urgency severities, both ways in (static header and v1 signature), the refusals, a ping dropped without a write, and a closed incident pruned to a tombstone that only a reopen lifts.

`vendor-routing.test.js` covers the vendor routing: same-topic isolation between two vendors, message ids that are never reused across vendors, the sweep and the resolve path using the right vendor credential, a missing or refused credential failing closed, an ambiguous write that never creates a second message, and a refresh that is refused outside the alerts channel.

`vendor-routing-hardening.test.js` covers the hardening that answered the review: the missing credential names the operator has to set, a failed status delivery is surfaced instead of swallowed, only an unknown outcome holds a repeat delivery, the delivery outcome is scoped to a content revision while the uncertainty barrier is scoped to the source, channel and topic, a standing message id whose owner is unknown is held rather than adopted or posted over, a standing message another source owns is left alone, a pre-routing membership marker names an incident rather than a source, the credential slug is injective in the source name, the sweep survives one bad incident, the legacy refresh route reports the sources it could not refresh, a malformed percent-escape is a bad request, and an unknown webhook field never reaches persistence, the standing block or the timeline (only the raw copy keeps it).

It also covers the Sentry source, which arrives as a native webhook body rather than through the generic shape: the issue shape and the metric-alert shape are both parsed, the webhook is accepted only under the Sentry identity and only with the Sentry secret, a request with no Sentry secret is refused rather than served by the shared token, and the generic route refuses a native Sentry body instead of flattening it into a generic alert.

## Deliberate non-goals

- Not a pager.  `zulip-fleet-guide.md` keeps PagerDuty as the pager; this is a readable record.
- Not a host-metrics collector.  It routes and coalesces what vendors already send.
- No vendor credential ever enters a request; all signing material is a Worker secret.

## Deployment

The ingress is deployed and live at `https://alert-ingress.jays-services.workers.dev`.  The committed `wrangler.toml` is the deployment manifest:  it binds the `ALERT_STATE` KV namespace the Worker has used since its first deployment, the `ALERT_WRITER` Durable Object, the `alert-writer-v1` SQLite class migration that creates only `AlertWriter`, and the `*/5 * * * *` sweep cron that promotes incidents which stayed open past the wait window.  A deploy keeps the bound secrets (`--keep-vars`), so the credential set below survives it.

`AlertWriter` is a single named Durable Object (`alerts-v1`) that serializes complete state reads, transitions, inventory rendering and Zulip writes.  Its durable storage overlays the existing KV records; historical KV records remain readable and are never deleted or migrated destructively.  Nothing writes the KV namespace once the writer owns the records, so the writer copies it into durable storage once (a record it already holds wins) and marks the copy `legacy:imported`.  From then on it never lists or reads KV.  Every KV list counts against the account's free-plan cap of 1,000 a day, and listing KV on each sweep and webhook had spent about 3,300 a day.  Until a copy succeeds, the writer keeps reading through to KV and retries the copy at most every ten minutes.  All status readers route through the same object, avoiding stale KV reads after mutation.  A KV get/put lock is not used.

Rollback keeps the previous Worker version and the same KV namespace available, and it preserves the durable outcome records, so an unknown send cannot replay.  No existing eavesdropper or model session needs restarting.

Before any standing-message write the object persists an `unknown` delivery outcome, on the revision-scoped outcome key and on the topic-scoped uncertainty barrier alike.  Only a validated successful response records `ok` with a positive integer message ID.  Timeout, malformed success, crash and ambiguous network failure retain `unknown`, and subsequent refreshes do not replay it.  Explicit provider refusal records `refused` and releases the barrier, because a refusal is definitive and nothing landed, so a later attempt may try again.  A write that returns no message ID is not a success either: the acknowledgement was lost, so it is recorded as `unknown` and the topic is held until an operator reconciles it.  A barrier that carries a message ID is retryable, because the retry edits the message this source already owns; a barrier without one is not, because a create may have landed that nobody knows about.  An operator must inspect the actual Zulip message before repairing a held outcome, and `?reconcile=<message id>` is the lever that adopts it.

Inventory walks drain at most 20 pages and 2,000 keys, reject missing/cyclic cursors and never publish a partial inventory as complete.  `GET /status` returns HTTP 503 with `stale: true` when inventory fails;  webhooks and the sweep handle an overflow as [Usage guards](#usage-guards) describes.  Reads use bounded batches of four; independent sweep status-refresh failures emit only a fixed event name and leave other topics eligible for processing.

Webhook JSON must be an object with bounded scalar fields, bounded string tags and object-shaped `alert`/`data` envelopes.  Better Stack's default incident body is JSON:API shaped, so `attributes` and `relationships` are object envelopes too and envelope nesting is depth-bounded; a correctly authenticated default delivery is accepted rather than rejected as an oversized text field.  Invalid field shapes, control bytes and bodies over 64 KiB fail before normalization or side effects.  One size limit rules:  every body is measured in encoded UTF-8 bytes and refused above 64 KiB, the check runs before the signature is verified and before the body is parsed, and a body of multi-byte characters is measured the same way as one of ASCII.  These dependency-free boundary validators match the worker's existing stdlib-only packaging; no unchecked object is passed into normalization.  Zulip response fields are also validated before an ID can be accepted.

A Sentry body carries the vendor's native shape, so it has a rule of its own:  the body is bounded in depth, breadth and text length, control characters are refused, and the variant is checked against the fields the parser actually reads.  An issue must carry an identity (`shortId` or `id`), a title, a level and a project; a metric alert must carry an identity (`id` or its rule's `id`), a title (its own or the rule's `name`) and its `alert_rule`.  Every consumed field is type-checked against the type the parser reads it as, a null sibling variant counts as absent rather than as a contradiction, and a body with no identity is refused with a 400 instead of normalizing onto a constant key — two malformed bodies on one constant key would share an incident, and one alert's resolution would close the other's incident and standing message.  A key with a credential shape, on any source, is not refused:  it becomes a digest of itself, `<source>:redacted-<digest>`, so the alert still files, one label stays one incident and two stay two, and the delivery logs `alert.identity_redacted`.  A refused alert is the worst outcome this worker has, and the digest keeps a token out of every row name just as a refusal would.  A Datadog scope is scanned whole before it is cut to 128 characters, so a token the cut would shorten leaves no prefix;  such a key keeps its monitor id, `datadog:<monitor>:redacted-<digest>`.

### Credentials

Infisical is the source of truth;  the Worker holds a copy.  `scripts/alert-ingress/provision-secrets.py` reads the per-vendor Zulip bot pairs from AFC prod `/zulip`, creates the four generated secrets there when they are absent (`INGRESS_SHARED_TOKEN`, `DATADOG_WEBHOOK_SECRET`, `BETTERSTACK_WEBHOOK_SECRET`, `PAGERDUTY_WEBHOOK_SECRET`), and PUTs them to the Worker under the names the table above asks for.  The PagerDuty pair is optional until its bot exists:  a missing pair is skipped and that source stays closed.  `SENTRY_WEBHOOK_SECRET` is never generated, because Sentry signs with its internal integration's own client secret, which Sentry issues;  a made-up value can never verify a real delivery, so the operator copies Sentry's value into Infisical and the provisioner copies it on.  It is a dry run unless `--apply` is passed, it prints names and lengths only, and it is idempotent:  an existing value is reused, so a rotation in Infisical propagates on the next run.

```
python3 scripts/alert-ingress/provision-secrets.py            # dry run
python3 scripts/alert-ingress/provision-secrets.py --apply    # write
```

### Proving it

`scripts/alert-ingress/selftest.py` is the end-to-end proof against the live Worker.  It posts synthetic Datadog and Better Stack events, reads the topic back through the vendor bots' own credentials, and asserts that a flapping monitor produces exactly one standing message, that two vendors sharing a service name own separate standing messages, that a second alert joins the same standing block as a second row, and that every delivery left its raw copy in `#Alerts-Raw` while `#Alerts` got no alert line.  `--cleanup` resolves the test incidents and deletes the test messages in all three channels, keeping the standing blocks the Worker still points at.

```
python3 scripts/alert-ingress/selftest.py              # prove
python3 scripts/alert-ingress/selftest.py --cleanup    # remove the test messages
```

The first firing event is **held** for `MIN_OPEN_MS` before it is announced as an alert line, so the alert line that proves the announcement path is posted by the cron sweep minutes later, not by the script.  Check the topic again once the sweep has run:  one line per announced incident, never one per event.

The cleanup removes only the one-line alert rows.  The standing status message the ingress still points at is left in place and its id is printed, because deleting it would leave the Worker pinned to a message that no longer exists.  When the cleanup prints "No standing status message was left behind", the topic has no standing block at all:  either the ingress has not fired for it yet, or the block was deleted -- by an earlier version of this script, or by hand -- while the Worker still holds its id.  A stale id is refused by Zulip, so no fresh standing block is created and the topic stays without one.  The pinned keys -- `status:<source>:<channel>:<topic>` and `statusowner:<message id>` -- live in the writer's durable storage (prefixed `kv:`), not in the `ALERT_STATE` KV namespace, so deleting them from KV changes nothing.  No operator route removes them yet.  Once they are gone, the next standing block whose content differs from the stale one is created fresh;  a refresh that renders the same content still finds the stale id in the writer's record of the last delivery, and is refused again.  The reconcile route is the lever for the other case, where a standing block exists but the Worker does not know its id:  `GET /status/:source/:channel/:topic?reconcile=<message id>`.

### Deployment gate

The CI deployment gate for this Worker is `scripts/worker-targets.json` (OPS-W01).  The entry names `.github/workflows/alert-workers-deploy.yml` as this Worker's one pipeline and carries the functional probe, `https://alert-ingress.jays-services.workers.dev/health`, which answers `{"ok":true}`, so a release that breaks liveness is refused instead of shipped.  That workflow releases only once a reviewed credential qualification receipt is recorded, and then a merge that changes this Worker's own files replaces every isolate and restarts the `ALERT_WRITER` Durable Object.  `docs/worker-deploy-automation.md` has the receipt steps.  The `worker-deploy` workflow no longer releases this Worker, so `vars.WORKER_DEPLOY_ENABLED` has no effect on it and the Oct 29 note below no longer describes the gate.

As of Oct 29, 2026 `vars.WORKER_DEPLOY_ENABLED` was not set in the repository, the organization or any environment, so `worker-deploy` runs ended `skipped`.  That workflow no longer releases this Worker, so the variable no longer matters here.  `alert-workers-deploy.yml` is the one pipeline, gated by the credential qualification receipt in `scripts/deploy-alert-workers.py`, so whether a merge redeploys this Worker depends on that receipt.

### Vendor cutover

State on Thu, Oct 29, 2026, 3:00 AM (AFC-566):

| Vendor | Route into Zulip | How it was changed |
|---|---|---|
| Datadog | **ingress** | The one webhook every monitor names, `zulip-alerts` (Integrations > Webhooks), now posts to `https://alert-ingress.jays-services.workers.dev/webhook/datadog` with the custom header `X-Datadog-Token` set to `DATADOG_WEBHOOK_SECRET` and the JSON payload below.  Because the name is unchanged, every monitor that says `@webhook-zulip-alerts` follows without an edit. |
| Better Stack | **ingress** | The outgoing webhook, renamed `alert-ingress`, now posts the default incident body to `https://alert-ingress.jays-services.workers.dev/webhook/betterstack` with `X-BetterStack-Token` set to `BETTERSTACK_WEBHOOK_SECRET`.  The API returns the header in the webhook's `included` custom-template record, not on the webhook itself. |
| PagerDuty | **ingress** | The generic bot `pagerduty-status-bot` posts the standing block;  its pair is `ZULIP_PAGERDUTY_BOT_EMAIL` / `ZULIP_PAGERDUTY_BOT_API_KEY` in AFC prod `/zulip`.  v3 subscription PZU1X4I, filtered to the `jays.services` service, posts eight incident events to `/webhook/pagerduty` with `X-PagerDuty-Token` set to the generated `PAGERDUTY_WEBHOOK_SECRET`.  The `Zulip #incident-response` subscription (PJCDNVU) was deleted.  PagerDuty's BotFleet subscription is unchanged. |
| Sentry | through PagerDuty | Sentry's alerts reach Zulip only through PagerDuty's `jays.services` service, so they move with PagerDuty.  A direct route needs an internal integration whose client secret is stored as `SENTRY_WEBHOOK_SECRET`. |

Every change was made through the vendor's API from the server, with the vendor key and the header secret read from Infisical into the process's memory, so no secret passed through a transcript or a URL.  The vendor keys are Fleet-OPS `DD_API_KEY` / `DD_APP_KEY` / `DD_SITE` and `BETTERSTACK_API_KEY` in `/global-api-keys`, and `PAGERDUTY_API_KEY` in `/pagerduty-api-key`.  The previous configurations were saved for rollback first.

The Datadog payload template, which is what `fromDatadog()` reads, plus `event_msg` (`$EVENT_MSG`, the full Markdown body with the runbook) for the raw copy.  That one field may run to 32 KiB;  every other text field stays under 4 KiB:

```json
{"monitor_id": "$ALERT_ID", "alert_id": "$ALERT_ID", "alert_type": "$ALERT_TYPE",
 "alert_transition_type": "$ALERT_TRANSITION", "alert_title": "$ALERT_TITLE",
 "alert_transition": "$ALERT_TITLE", "priority": "$ALERT_PRIORITY", "host": "$HOSTNAME",
 "scope": "$ALERT_SCOPE", "event_url": "$LINK", "event_msg": "$EVENT_MSG"}
```

Re-pointing a vendor is enough to stop its old messages:  the legacy incoming-webhook URL receives nothing once the vendor posts to the ingress.  The incoming-webhook bots `[Datadog]`, `[BetterStack]`, `[PagerDuty]` and `[Sentry]` are now unused, and their keys sat in the old vendor URLs, so deactivating them is the clean end of the cutover.  No secret ever travels in a URL;  each vendor carries its credential in a header, and the shared bearer token `INGRESS_SHARED_TOKEN` gates the operator routes only.

Timeline transitions use a separate durable outcome keyed by the exact normalized transition and previous state.  Destination inventory is completed before mutation; a definitive pre-send failure leaves the previous phase unchanged.  Timeline `unknown` is never resent, while a definitive provider refusal can retry the same transition safely.  Sweep failures are isolated per incident, so one refusal cannot abort independent promotions.

**Better Stack delivers empty bodies (Nov 4, 2026).**  The webhook's requests carry the right token, `content-type: application/json` and `content-length: 0`, so none of them files an incident (every delivery in the 7 days measured was refused).  They come in rapid bursts at irregular times, which looks like event deliveries with retries rather than a scheduled probe.  The worker answers `400` and reports it once a day to `#Alerts-Log`, topic `alert-ingress`.  The fix is at the vendor:  the webhook has to send a JSON body.

Better Stack Uptime incident-change payloads support the default data.attributes resolved_at field as well as explicit status; optional monitor relationship IDs retain stable keys.

### Request bounds

The entry point rejects unknown routes, wrong webhook methods, missing/malformed vendor authentication and oversized declared webhook bodies before Durable Object dispatch. Operator routes keep their existing methods and bearer checks. An empty webhook body is refused here too, apart from about one a day that reaches the writer so it can report it.  Webhook bodies are streamed up to the existing 64 KiB byte limit before verification/parsing; missing or inaccurate Content-Length cannot bypass the limit. `/health` stays storage-free.
