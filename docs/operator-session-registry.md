# Operator Session Registry — Design Note

Owner directive (Tue, Nov 3, 2026, as corrected):  the "chat threads" the Operator
registers in a sheet are first of all the **lettered agent session threads** — the
Operator's own session registry, the same sessions it picks to wake — mirrored to a
Google Sheet with all the info it can carry, human-visible first.  The Zulip
stream/topic registry (docs/operator-thread-registry.md) is the second track.  Both
tracks live in **one workbook on different tabs** (owner, 02:06 CT), and the Sessions
tab carries a **Linear Issues** column (owner, 02:07 CT):  every Linear issue key
mentioned in connection with a session, accumulated and deduplicated.

Built in this lane (branch `Muse-Assist/operator-session-registry`, off `main` at
`3abb5a2`).  Nothing here is deployed, the sync has never run against a live registry
store, and no live data is backfilled.  States are kept distinct throughout:  built and
tested in the lane only, against fixture stores.

## The Workbook

- Title:  **Operator Thread Registry** (unchanged — see the proposal below)
- ID:  `1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE`
- URL:  https://docs.google.com/spreadsheets/d/1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE/edit
- Tab **Sessions** (sheetId 915562327, first tab):  created in this lane through the
  owner's Google Sheets connection, header row written and read back verified, header
  frozen, column widths set for reading (Title and Native Session ID widest).
- Tab **Zulip Threads** (sheetId 1290118782):  the thread track's tab, renamed in this
  lane from "Threads" to the clearer name, per the owner's one-workbook direction.
  Its contents and header are untouched.

**Workbook title proposal (not applied):**  the workbook now holds both registries, so
"Operator Thread Registry" undersells the Sessions tab.  Proposed:  **"Operator
Registries"** — the tabs carry the specifics ("Sessions", "Zulip Threads").  Renaming
is one owner word;  this lane did not rename it silently.

**Coupling flag:**  the thread lane's code names its tab `"Threads"`
(`thread_registry.SHEET_TAB`, `thread_registry_export.mjs`).  The live tab is now
"Zulip Threads", so that lane's drain would fail its header check until the constant
is updated to match when the lane lands.  Nothing is deployed, so nothing live breaks;
the rename is recorded here and in this lane's report so it is not lost.

## How It Works

1. **Source.**  The registry store itself (`operator_registry.py`'s SQLite database:
   `sessions`, `alias_assignments`, `events`).  No observation layer is needed — the
   registry is already the record of the sessions the wake path chooses from.
2. **Read-only projection.**  `session_registry.project_rows` opens the store with
   SQLite `mode=ro` plus `PRAGMA query_only = ON` and makes one pass over each table.
   The sync is never a second writer and never mutates the store;  it works against
   whichever store is canonical (the Mac store is the active allocator today) without
   competing with it.  Deliberate contrast with the wake path's read handle, which
   uses `mode=rw` because it cannot afford a refused read against a hot journal:  a
   sheet sync can fail honestly and let the next run reconcile, so it takes the
   strictly read-only side.  Tests pin this:  the store's bytes and directory are
   unchanged after a sync, a write through the sync's handle is refused by SQLite,
   and a missing store is an error, never a creation.
3. **Sync (Google write).**  `scripts/agent_sync/session_registry_export.mjs` is the
   only Google writer.  `python3 -m agent_sync.session_registry export-rows` emits the
   desired rows as JSON;  the drain validates the live header against the schema,
   reads the existing rows, plans upserts by Record ID, and writes.  The Python
   `plan_upserts`/`sync_to_sheet` in `session_registry.py` are the reference
   implementation of the same semantics and are what the tests exercise;  the JS plan
   is a line-for-line mirror, the same arrangement as the thread registry.

Sync guarantees:

- **Idempotent.**  A row whose cells match (ignoring Registry Updated At) is not
  rewritten;  a re-run with no registry change performs zero writes.  The write side
  is the incremental part;  the read side is one bounded pass per table, never an
  external history.
- **One row per session, keyed by Record ID.**  The immutable ULID surrogate is the
  upsert key, so a relabel, a status change, or a recycled letter updates the
  session's row in place — never a second row.
- **Header first.**  Any header drift aborts the sync before a single write.
- **Failure is partial and honest.**  Writes stop at the first failure;  rows already
  written stay written, the drain reports the counts and the error, and the next run
  reconciles by Record ID.  No blind retries.
- **Key collisions.**  If the sheet somehow holds two rows with one Record ID, the
  first is the row of record;  duplicates are reported in the sync result and never
  deleted or written by the sync.

## Schema — One Row Per Session, and Where Every Column Comes From

| # | Column | Source |
|---|--------|--------|
| A | Record ID | `sessions.record_id` — the immutable ULID surrogate;  the upsert key. |
| B | Letter | `alias_assignments.letter` for the session's live assignment, or the last one it held once retired — `SessionRegistry._last_alias`'s semantics (active first, else latest `assigned_at`).  Alias rows are never deleted, so a closed or relabelled session still shows the letter it ended on.  Blank when the session was never lettered. |
| C | Generation | `alias_assignments.generation` of that same assignment — a recycled letter shows its own generation per session (gen 1 on the session that closed, gen 2 on the one that holds it now). |
| D | Seat | `sessions.seat`. |
| E | Platform | `sessions.platform` (the folded namespace, as stored). |
| F | Verified Host | `sessions.verified_host`. |
| G | Profile Scope | `sessions.profile_scope`. |
| H | Native Session ID | `sessions.native_session_id`.  Together with E–G this is the session's identity tuple, shown in full. |
| I | Title | `sessions.title`. |
| J | Thread Link | `sessions.thread_link`. |
| K | Status | `sessions.state` verbatim (`active`, `closed`, …). |
| L | Wake Capability | `sessions.wake_capability` — the wake pathway the registry carries for the session (`unknown` until the registration records one). |
| M | Registered At (UTC) | `sessions.registered_at`, the registry's stored canonical UTC ISO text, carried verbatim. |
| N | Last Seen At (UTC) | `sessions.last_seen_at`, verbatim — the registry's last-touch stamp for the session. |
| O | Closed At (UTC) | `sessions.closed_at`, verbatim;  blank while the session is open. |
| P | Event Count | `COUNT(events)` for the session (`events.session_record_id = record_id`). |
| Q | Linear Issues | Deterministic extraction (below):  the union of issue keys found in `sessions.title`, `sessions.thread_link`, and the session's `events.details`, sorted, deduplicated, joined with `", "`. |
| R | Registry Updated At (UTC) | The sync run's clock, stamped on rows that run actually writes.  Unchanged rows keep their older stamp, so the column reads as "when this row last changed in the sheet". |

**No Last Wake Outcome column — deliberately.**  The brief asked for it *if recorded*.
It is not recorded:  the registry's event vocabulary is lifecycle and provenance only
(register, resume, relabel, close, reopen, update, touch;  notes_request,
notes_result, token_mint, token_revoke, token_scope_check), and no code path writes a
wake outcome into the registry — wake outcomes live in the listener's own wake
ledger/logs, a different store.  Rather than invent the data or reach into a second
store, the column stays out;  Wake Capability (L) is the wake field the registry
actually carries.  If wake outcomes are ever recorded into the registry, the column
joins the schema then.

## Linear Issues — Extraction Rules

- The pattern is the identical one the Zulip Threads tab uses —
  `\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b` (AFC-412 shape) — so an issue key means the same
  thing on both tabs.  Matching is case-sensitive:  lowercase lookalikes are not keys.
- Sources are only fields the registry itself carries for the session:  its **title**,
  its **thread link**, and the **details of its events**.  Nothing is inferred from
  the seat, the platform, or any other session;  a key is listed when its text
  appears in one of those fields, and never otherwise.  (A mention is not a verified
  link:  a string shaped like an issue id is recorded whether or not the issue
  exists — the same honesty rule as the thread tab.)
- **Accumulation** falls out of the store's shape:  events are append-only history,
  and each projection takes the union over all of the session's stored events plus
  its current title/link, so the list only grows as more mentions land — a key
  mentioned once stays listed, and duplicates across sources land once.  Pinned by
  tests, including a key that arrives in a later event joining the list on the next
  sync without disturbing the row's identity.

## Credential Seam — The Same One Blocker as the Thread Track

The drain authenticates as a **dedicated service account for the Operator
registries**, never as an agent's Google identity:

- Env:  `OPERATOR_GOOGLE_SHEETS_SA` (the service-account JSON), or
- Infisical Fleet-OPS prod, path `/operator-thread-registry`, key
  `OPERATOR_GOOGLE_SHEETS_SA`, resolved through `fleet-secrets` at runtime — the
  same seam, the same key, as the thread registry's drain;  one seam serves both
  tabs.  The key is never written to disk or printed.  There is no fallback to any
  other identity.

**What is needed (owner/admin action):**  create that service account (or approve
reusing an existing one under this dedicated key name), put its JSON in Infisical at
the path above, and share the workbook with the service account's `client_email` as
Editor.  Until then the drain refuses with a clear error and writes nothing;
everything up to that seam — the tab, the projection, the plan — is built and tested.

## Gated Follow-Ups (Not Done Here)

1. **First live sync.**  Runs only once the service account exists **and** the two
   diverged registry stores are reconciled — syncing from a store that is about to
   be reconciled would mirror a state that is known not to be canonical.  The sync
   reads whichever store it is pointed at (`AGENT_SYNC_OPERATOR_REGISTRY` or
   `--registry`);  choosing the canonical store at deploy time is the cutover's
   call, not this lane's.
2. **Deploy.**  Ship the lane through the normal listener deployment and schedule
   the drain (e.g. with the listener's periodic jobs).  No config is flipped here.
3. **Thread lane constant.**  Update `thread_registry.SHEET_TAB` and
   `thread_registry_export.mjs` from "Threads" to "Zulip Threads" when that lane
   lands (see the coupling flag above).
4. **Workbook title.**  Rename to "Operator Registries" on the owner's word (see
   the proposal above).
5. **Read-back surface.**  The tab is keyed by Record ID with the full identity
   tuple, so tools can read it back as a registry surface;  wiring a reader (e.g.
   the registry API at operator.simplewithus.com) is a follow-up once the tab has
   live data.

## Rebase Note

PR #1258 renames `scripts/agent_sync` → `scripts/simple_tools`.  Everything in this
lane lives under `scripts/agent_sync/` (`session_registry.py`,
`session_registry_export.mjs`, `tests/test_session_registry.py`) and moves with that
rename;  no path outside the package is touched.

## Tests

`cd scripts && python3 -m unittest agent_sync.tests.test_session_registry` — 19
tests, all passing:  projection sources per column (including last-held letter after
close, recycled-letter generations, unlettered sessions), Linear extraction and its
accumulation/dedup over time, read-only proofs (store bytes and directory unchanged,
write refused, missing store not created), sync behaviour (first-run appends,
idempotent re-runs, close and relabel updating in place, new sessions appending,
duplicate Record ID rows reported and left alone, header mismatch writing nothing,
a failed write stopping the sync partial and the next run reconciling), and the CLI
export-rows/stats payloads.  The exporter's header list is verified byte-equal to
the Python HEADERS.  Neighbouring suites re-run clean (112 tests:
test_operator_service, test_operator_registration, test_operator_registration_ask).
