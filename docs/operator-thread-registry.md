# Operator Thread Registry — Design Note

Owner directive (Tue, Nov 3, 2026):  the Operator should maintain a registry of chat threads
(Zulip stream/topic threads) in a Google Sheet — a NEW sheet, separate from the SWU Fleet
References workbook — with all the thread info it can carry, human-visible first;  if the
sheet is also usable as a registry surface (read back by tools), that is a bonus.

Built in this lane (branch `Muse-Assist/operator-thread-registry`, off `main` at `3abb5a2`).
Nothing here is deployed, no listener config is flipped, and no live Zulip history is
backfilled.  States are kept distinct throughout:  built and tested in the lane only.

## The Sheet

- Title:  **Operator Thread Registry**
- ID:  `1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE`
- URL:  https://docs.google.com/spreadsheets/d/1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE/edit
- Tab:  `Threads` (sheetId 1290118782), header row frozen, header row written and read back
  verified at creation, column widths set for reading (Topic and Participants widest).
- The workbook was created through the owner's Google Sheets connection so the owner can
  see it immediately.  The Operator's own writes do NOT use that connection — see
  "Credential seam" below.  (A duplicate workbook from a retried create call was trashed;
  the ID above is the one of record.)

## How It Works

1. **Observation (listener-side, deterministic).**  `Daemon.route_message` — the one path
   every routed message takes, live or backfill — calls `Daemon._observe_thread`, which
   folds the message into `ThreadRegistryStore`
   (`<state root>/operator/thread-registry.json`, override
   `AGENT_SYNC_OPERATOR_THREAD_REGISTRY`).  Observation is best-effort:  a failure is logged
   (`thread-registry-error`) and never touches routing.  Messages are deduplicated by Zulip
   message id (the store keeps the newest 20,000 processed ids), so a message delivered to
   several seat runners, or replayed through backfill, counts once.  The store holds one
   aggregate per thread, never one row per message, so the sync is bounded by thread count
   and never rescans history.
2. **Projection.**  `ThreadRegistryStore.rows(now)` projects aggregates to sheet rows in
   Thread Key order.  Status and the Registry Updated At stamp are computed from the sync
   run's clock, so one run produces a self-consistent sheet.
3. **Sync (Google write).**  `scripts/agent_sync/thread_registry_export.mjs` is the only
   Google writer.  `python3 -m agent_sync.thread_registry export-rows` emits the desired
   rows as JSON;  the drain validates the live header against the schema, reads the
   existing rows, plans upserts by Thread Key, and writes.  The Python
   `plan_upserts`/`sync_to_sheet` in `thread_registry.py` are the reference implementation
   of the same semantics and are what the tests exercise;  the JS plan is a line-for-line
   mirror, the same arrangement as `operator_sheets_export.mjs`.

Sync guarantees:

- **Idempotent.**  A row whose cells match (ignoring Registry Updated At) is not rewritten;
  a re-run with no new observations performs zero writes.
- **Header first.**  Any header drift aborts the sync before a single write.
- **Failure is partial and honest.**  Writes stop at the first failure;  rows already
  written stay written, the drain reports the counts and the error, and the next run
  reconciles by Thread Key.  No blind retries.
- **Key collisions.**  Topics that differ only by case fold to one key (Zulip groups them
  together), so they share one row and the latest casing is shown.  If the sheet itself
  somehow holds two rows with one key, the first is the row of record;  duplicates are
  reported in the sync result and never deleted or written by the sync.
- **Human edits.**  Purpose is the one column a human may own:  the projection always
  carries blank, and an update preserves a non-blank Purpose already in the sheet.

## Schema — One Row Per Thread, and Where Every Column Comes From

| # | Column | Source |
|---|--------|--------|
| A | Thread Key | Derived:  `{stream_id}:{topic, stripped and case-folded}` — the upsert key, and the stable handle a tool reads back. |
| B | Stream | The message's `display_recipient` (Zulip's stream name), falling back to the runner's subscription map (`runner.streams[stream_id]`) when the event omits it;  latest seen name wins, so a stream rename converges. |
| C | Stream ID | The message's `stream_id`. |
| D | Topic | The message's `subject` (topic);  latest casing wins under the case-folded key. |
| E | Status | Derived at projection:  `closed` when the topic carries Zulip's resolved check-mark prefix;  else `active` (last activity ≤ 24h), `idle` (≤ 14d), or `dormant` (> 14d), against the sync clock. |
| F | Participants | Sorted set of participant tags for every sender observed in the thread.  A bot sender maps through `identity.seat_tag_for` (bot email → seat tag, including the fleet's email overrides);  a human sender is their first name, upper-cased.  Bot-vs-human comes from the daemon's realm user map when it knows the sender, else from the realm's `-bot` account naming.  Canonical tags follow the owner's seat-tag ruling (Tue, Nov 3, 2026):  Zcode is **ZC**, Kimi is **KIMI**. |
| G | Participant Count | Length of the Participants set. |
| H | Message Count | Count of distinct Zulip message ids observed for the thread (deduplicated across seat runners and backfill replays).  It counts what the listener has seen since the store was created — not the thread's lifetime total in Zulip. |
| I | First Seen (UTC) | Minimum `timestamp` over observed messages, ISO-8601 UTC. |
| J | Last Activity (UTC) | Maximum `timestamp` over observed messages, ISO-8601 UTC. |
| K | Last Message ID | The Zulip message id at Last Activity — the anchor a reader uses to open or read the thread's recent messages. |
| L | Linear Issues | Deterministic regex extraction from message content:  `[A-Z][A-Z0-9]{1,9}-{digits}` (AFC-412 shape), sorted, deduplicated.  A mention, not a verified link — a string shaped like an issue id is recorded whether or not the issue exists. |
| M | PR References | Deterministic regex extraction from message content:  GitHub pull URLs and `owner/repo#N` (both normalised to `owner/repo#N`), plus bare `PR #N`, sorted, deduplicated. |
| N | Purpose | **Blank.**  No deterministic source exists in a message event, and the registry never invents a summary.  A human may type a purpose into the sheet;  the sync preserves it (see above). |
| O | Registry Updated At (UTC) | The sync run's clock, stamped on rows that run actually writes.  Unchanged rows keep their older stamp, so the column reads as "when this row last changed in the sheet". |

DMs are out of scope:  the registry is for stream threads, and `observe()` ignores
non-stream messages.  A renamed topic is a new key and therefore a new row (the old row
stays, aging to dormant);  merging renamed topics is a possible later refinement, not
faked here.

## Credential Seam — The One Blocker for a Live Drain

The drain authenticates as a **dedicated service account for this registry**, never as an
agent's Google identity:

- Env:  `OPERATOR_GOOGLE_SHEETS_SA` (the service-account JSON), or
- Infisical Fleet-OPS prod, path `/operator-thread-registry`, key
  `OPERATOR_GOOGLE_SHEETS_SA`, resolved through `fleet-secrets` at runtime — the same
  resolution shape as the existing `/google-sheets-sa` secret, a separate purpose-scoped
  one.  The key is never written to disk or printed.

**What is needed (owner/admin action):**  create that service account (or approve reusing
an existing one under this dedicated key name), put its JSON in Infisical at the path
above, and share the "Operator Thread Registry" sheet with the service account's
`client_email` as Editor.  Until then the drain refuses with a clear error and writes
nothing;  everything up to that seam — observation, store, projection, plan, sheet
creation — is built and tested.  Reusing the existing fleet Sheets service account
(`/google-sheets-sa`) under the dedicated key name is a one-line owner decision if a
second service account is unwanted;  the code does not silently fall back to it.

## Gated Follow-Ups (Not Done Here)

1. **Deploy.**  Ship the lane (the daemon hook and the new modules) through the normal
   listener deployment, and schedule the drain (e.g. with the listener's periodic jobs).
   No config is flipped by this lane.
2. **Historical backfill.**  The store starts empty at deploy time, so threads appear as
   the listener sees new messages.  A one-time backfill is feasible from the listener's
   own stored state only in part (per-seat inbox files carry channel/topic/sender/ts for
   inbox items, not full stream traffic), so a complete historical backfill would need a
   Zulip read under the Operator's own credential — a separate, owner-gated decision.
   Not attempted here;  no agent-identity scraping.
3. **Read-back surface.**  The sheet is keyed and stable enough for tools to read
   (Thread Key + Last Message ID anchor).  Wiring a reader (e.g. an ST-MCP tool or the
   registry API at operator.simplewithus.com) is a follow-up once the sheet has live data.

## Rebase Note

PR #1258 renames `scripts/agent_sync` → `scripts/simple_tools`.  Everything in this lane
lives under `scripts/agent_sync/` (`thread_registry.py`, `thread_registry_export.mjs`,
`tests/test_thread_registry.py`, and the `daemon.py` hook) and moves with that rename;
no path outside the package is touched.

## Seat-Tag Surfaces

This lane's only seat-tag surface is the Participants column, which goes through
`identity.seat_tag_for` — a `zc-bot@…` sender yields `ZC` and a `kimi-bot@…` sender yields
`KIMI`, pinned by tests.  `fleet-apps.json` still marks `KIMI` retired (2026-08-21) and
`strict_seat()` therefore refuses `KIMI` as a *launch* seat;  that is fleet seat policy,
not a display tag, and this lane does not change it.  Aligning `fleet-apps.json`, the
listener partition, and the Infisical seat surfaces to the owner's ZC/KIMI ruling is a
separate follow-up.
