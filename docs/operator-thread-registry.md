# Operator Thread Registry — Design Note

Owner directive (Tue, Nov 3, 2026):  the Operator should maintain a registry of chat threads
(Zulip stream/topic threads) in a Google Sheet — a NEW sheet, separate from the SWU Fleet
References workbook — with all the thread info it can carry, human-visible first;  if the
sheet is also usable as a registry surface (read back by tools), that is a bonus.

First built in the lane `Muse-Assist/operator-thread-registry` (off `main` at `3abb5a2`).
The observation hook and the store ship with the listener image, and AFC-823 runs the
sheet sync on the server inside the Operator Sheets drain (step 4 below), once that
drain's installer has run from a checkout that carries it.  No live Zulip history is
backfilled.

## The Sheet

- Title:  **Operator Thread Registry**
- ID:  `1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE`
- URL:  https://docs.google.com/spreadsheets/d/1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE/edit
- Tab:  `Zulip Threads` (sheetId 1290118782), header row frozen, header row written and read back
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
3. **Sync (Google write).**  `scripts/simple_tools/thread_registry_export.mjs` is the only
   Google writer.  `python3 -m simple_tools.thread_registry export-rows` emits the desired
   rows as JSON;  the drain validates the live header against the schema, reads the
   existing rows, plans upserts by Thread Key, and writes.  The Python
   `plan_upserts`/`sync_to_sheet` in `thread_registry.py` are the reference implementation
   of the same semantics and are what the tests exercise;  the JS plan is a line-for-line
   mirror, the same arrangement as `operator_sheets_export.mjs`.  The JS drain adds the
   guards a live, human-edited tab needs (below).  Run by hand,
   `node thread_registry_export.mjs rows.json` only reads the tab and prints the plan;
   `--write` writes, and first takes the Operator Sheets drain's writer lease
   (`OPERATOR_SHEETS_LOCK`), so it refuses while a scheduled run holds it.
4. **Schedule (the server, AFC-823).**  The Operator Sheets drain
   (`scripts/simple_tools/operator_sheets_drain.mjs`, `operator-sheets-drain.service` and its
   timer) runs this sync on every run, before its Sessions and Events export, under the same
   lease and with the same service-account key.  It reads the rows with
   `docker exec -w /app/scripts eavesdropper python3 -m simple_tools.thread_registry export-rows`
   (the store is in the listener container's volume, and no listener route serves it), writes
   them with `drainThreadRows` behind its own write pacing, stops after 4 minutes or 200 writes
   a run, and logs one line of counts.  A failure there never stops the Sessions and Events
   export.  See "Zulip Threads Sync" in `docs/protocols/operator-sheets-export.md`.

Sync guarantees:

- **Idempotent.**  The same aggregates at the same age give the same sheet:  a row whose
  cells match (ignoring Registry Updated At) is not rewritten.  Status is computed from
  the run's clock, so a thread whose last activity crosses 24 hours or 14 days is
  rewritten once with no new messages;  a re-run with no new observations and no Status
  change performs zero writes.
- **Header first.**  Any header drift aborts the sync before a single write.
- **The whole tab is read.**  The drain pages to the tab's grid row count, so a blank
  row a human leaves (Google omits trailing blank rows from a page) never hides the rows
  below it, which would otherwise be planned as appends and written twice.
- **Failure is partial and honest.**  Writes stop at the first failure;  rows already
  written stay written, the drain reports the counts and the error, and the next run
  reconciles by Thread Key.  No blind retries.
- **Key collisions.**  Topics that differ only by case fold to one key (Zulip groups them
  together), so they share one row and the latest casing is shown.  If the sheet itself
  somehow holds two rows with one key, the first is the row of record;  duplicates are
  reported in the sync result and never deleted or written by the sync.
- **Human edits.**  Purpose is the one column a human may own:  the projection always
  carries blank, and an update never writes the Purpose column (nor the Thread Key), so a
  Purpose typed at any time, during a run included, survives.  Just before each run of
  10 updates the drain re-reads those rows' Thread Keys and skips any row a human moved
  (sorted, inserted or deleted rows);  the next run reads the tab again and updates it at
  its new row.
- **Oversized rows.**  A row with a cell over Google's 50,000-character limit (a
  long-lived topic's growing PR list, say) is skipped and counted rather than sent:
  Google would refuse it on every run, and that refusal would stop every write after it.

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
| L | Linear Issues | Deterministic regex extraction from BOTH the thread's topic name and message content:  `[A-Z][A-Z0-9]{1,9}-{digits}` (AFC-412 shape).  The cell is the thread's accumulated set rendered comma-separated, sorted and deduplicated.  The set only ever grows:  a key mentioned in any observed message — or carried by the topic name itself — is added on that sync and never removed afterwards, even if later messages never repeat it.  A mention, not a verified link — a string shaped like an issue id is recorded whether or not the issue exists. |
| M | PR References | Deterministic regex extraction from message content:  GitHub pull URLs and `owner/repo#N` (both normalised to `owner/repo#N`), plus bare `PR #N`, sorted, deduplicated. |
| N | Purpose | **Blank.**  No deterministic source exists in a message event, and the registry never invents a summary.  A human may type a purpose into the sheet;  the sync preserves it (see above). |
| O | Registry Updated At (UTC) | The sync run's clock, stamped on rows that run actually writes.  Unchanged rows keep their older stamp, so the column reads as "when this row last changed in the sheet". |

DMs are out of scope:  the registry is for stream threads, and `observe()` ignores
non-stream messages.  A renamed topic is a new key and therefore a new row (the old row
stays, aging to dormant);  merging renamed topics is a possible later refinement, not
faked here.

## Credential Seam

The drain authenticates as a **service account**, never as an agent's Google identity.
Owner ruling (Nov 3, 2026):  the **existing fleet Sheets service account is reused** for
the Operator registries;  no dedicated SA is created for this registry.

- Env:  `OPERATOR_GOOGLE_SHEETS_SA` (the service-account JSON), or
- Infisical Fleet-OPS prod, path `/google-sheets-sa`, key
  `OPERATOR_GOOGLE_SHEETS_SA`, resolved through `fleet-secrets` at runtime — the approved
  fleet Sheets route, no purpose-scoped copy.  The CLI never writes the key to disk
  or prints it.
- On the server, the scheduled drain reads the same key from
  `/etc/operator-sheets/sa.json` (`OPERATOR_SHEETS_SA_FILE`), which
  `install-operator-sheets-drain.sh` writes (0600, root) from that Infisical path with its
  scoped read-only service token.  The two routes above are the CLI's, for manual runs.

**What is needed (owner/admin action):**  share the "Operator Thread Registry" sheet with
that service account's `client_email` as Editor.  Without the share, the CLI exits with an
error, and the scheduled drain logs the fixed code `sheet_http_403` on every run and
writes no thread rows while the Sessions and Events export carries on.  The code reads
only the approved key above and never falls back to another credential silently.

## Follow-Ups

1. **Deploy.**  The daemon hook and the store ship with the listener image.  The drain is
   scheduled in code by AFC-823 (step 4 above): it runs once the Operator Sheets drain's
   installer has run from a checkout that carries it.
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

The rename this note expected, `scripts/agent_sync` → `scripts/simple_tools` (PR #1258),
is in the tree: `thread_registry.py`, `thread_registry_export.mjs`,
`tests/test_thread_registry.py` and the `daemon.py` hook now live under
`scripts/simple_tools/`, and `scripts/agent_sync/` holds only the session registry.

## Seat-Tag Surfaces

This lane's only seat-tag surface is the Participants column, which goes through
`identity.seat_tag_for` — a `zc-bot@…` sender yields `ZC` and a `kimi-bot@…` sender yields
`KIMI`, pinned by tests.  `fleet-apps.json` still marks `KIMI` retired (2026-08-21) and
`strict_seat()` therefore refuses `KIMI` as a *launch* seat;  that is fleet seat policy,
not a display tag, and this lane does not change it.  Aligning `fleet-apps.json`, the
listener partition, and the Infisical seat surfaces to the owner's ZC/KIMI ruling is a
separate follow-up.
