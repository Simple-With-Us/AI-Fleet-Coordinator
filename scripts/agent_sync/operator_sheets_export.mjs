// Async idempotent exporter: Operator registry outbox -> fleet reference workbook (AFC-412 PR2).
//
// The exporter is the sole programmatic writer of the Sessions (2033000102) and Events
// (2033000103) tabs; it never touches Reference (2033000101). It depends on the AFC-428 adapter
// surface - inspect(), validateHeaders(), readRows(table, {startRow, limit}),
// appendSession(cells), updateSessionRow(rowNumber, cells), appendEvent(cells) - injected here so
// the adapter stays Codex's slice. RAW writes, no automatic mutation retries: after any failure
// the row stays pending and the next drain reconciles by id before writing.
//
// Reconciliation uses inspect() physical grid bounds and numbered readRows pages.
// At the start of a drain, each tab is scanned once across its complete physical grid
// into an ID index (id -> physical row numbers). Short or empty pages do not end the
// scan, and an ID landing on more than one row is ambiguous: writes to it refuse.
// inspect() must return { sessions: { rowCount }, events: { rowCount } }, where each
// rowCount is the positive safe-integer physical grid row count, including the header.
// readRows(table, { startRow, limit }) returns an array (or { rows: array }) of
// { rowNumber, cells } records. rowNumber is the 1-based physical row within that page;
// row/values aliases are accepted. Sparse pages do not shorten the physical grid.
// findSessionRowByRecordId and other optional fast lookups are unsupported because
// they do not prove uniqueness across the complete destination grid.
// Appends feed the index from their confirmed row numbers; an append that does not
// confirm a row number marks the index stale and the next lookup rescans the grid.
// appendSession/appendEvent may return the written row number for logging.
// The runner must hold one exclusive cross-process writer lease through reconcile,
// write and acknowledgement, and prevent concurrent manual row restructuring.
// Errors leave the earliest row pending and stop the batch, preserving snapshot order.
// After a confirmed append and failed registry acknowledgement, the next drain
// reconciles the immutable ID before acknowledgement, without another event append.
// A truly unknown Sheets transport outcome may commit late: even an exclusive writer
// cannot prove a currently absent ID will stay absent. The runner must not blindly
// resume that drain until the outstanding request is settled or safely reconciled.
// This exporter does not persist a general ok/unknown/refused mutation ledger.

// The exporter owns credential rejection before invoking the adapter (OPERATOR-SHEETS-ADAPTER.md).
import { scanCellText } from "./operator_sheets_google.mjs";

export const SESSIONS_HEADERS = [
  "Record ID", "Label", "Seat", "Platform", "Host", "Native Session ID", "Title", "Project",
  "Thread Link", "Apple Note Link", "State", "Wake Capability", "Registered At", "Last Seen At",
  "Closed At", "Source",
];
export const EVENTS_HEADERS = [
  "Event ID", "Recorded At", "Session Record ID", "Label", "Event", "Actor", "Outcome",
  "Source", "Details",
];

const PAGE_SIZE = 200;

export const PAYLOAD_HEADERS = { sessions: SESSIONS_HEADERS, events: EVENTS_HEADERS };

// Scan one tab's complete physical grid once and index every first-cell ID to its
// physical row numbers. The scan never ends early: physical grid bounds come from
// inspect(), pages are capped to those bounds, and every returned row number is
// validated against its page, exactly as the per-ID scan did before (OPS-1037) -
// the difference is the grid is walked once per drain, not once per outbox row.
async function scanTableIndex(adapter, table, bounds) {
  const rowCount = bounds?.[table]?.rowCount;
  if (!Number.isSafeInteger(rowCount) || rowCount < 1) {
    throw new Error("adapter.inspect must return { sessions: { rowCount }, events: { rowCount } } with positive safe-integer physical grid row counts");
  }
  const index = new Map();
  for (let startRow = 2; startRow <= rowCount; startRow += PAGE_SIZE) {
    const limit = Math.min(PAGE_SIZE, rowCount - startRow + 1);
    const page = await adapter.readRows(table, { startRow, limit });
    const rows = page.rows ?? page;
    if (!Array.isArray(rows) || rows.length > limit) {
      throw new Error("adapter.readRows returned an invalid page");
    }
    for (const r of rows) {
      const rowNumber = r.rowNumber ?? r.row;
      const cells = r.cells ?? r.values ?? r;
      if (!Number.isSafeInteger(rowNumber) || rowNumber < startRow || rowNumber >= startRow + limit) {
        throw new Error("adapter.readRows did not return valid physical row numbers");
      }
      const id = cells[0];
      if (id === "" || id == null) continue; // blank rows carry no identity
      if (!index.has(id)) index.set(id, []);
      index.get(id).push(rowNumber);
    }
  }
  return index;
}

// Project payload cells (canonical registry layout) onto the destination tab's own
// headers, by name. A destination header the payload cannot fill is a hard error:
// writing a silent blank would corrupt the mirror. Tabs whose headers equal the
// canonical layout project to themselves.
export function projectCells(table, payloadCells, sheetHeaders) {
  const canonical = PAYLOAD_HEADERS[table];
  if (!canonical) throw new Error(`unknown table ${table}`);
  if (!Array.isArray(payloadCells) || payloadCells.length !== canonical.length) {
    throw new Error(`${table}: payload carries ${payloadCells?.length} cells, expected ${canonical.length}`);
  }
  const byName = new Map(canonical.map((h, i) => [h, payloadCells[i] ?? ""]));
  return sheetHeaders.map((h) => {
    if (!byName.has(h)) {
      throw new Error(`${table}: destination header "${h}" has no payload field; refusing to write a blank`);
    }
    return byName.get(h);
  });
}


// Display contract (owner rule, SWU Operator workbook Reference > Time Display): timestamps are
// stored canonically as UTC ISO (operator_registry.py) and formatted for display only at write
// time here: America/Chicago conversion, 12-hour clock with AM/PM, NO timezone label in the
// displayed text, written as text values so the workbook timezone cannot reinterpret them.
// Empty, null, or unparseable values pass through unchanged; non-timestamp cells are untouched.
const SESSION_TIME_COLUMNS = [12, 13, 14]; // Registered At, Last Seen At, Closed At
const EVENT_TIME_COLUMNS = [1]; // Recorded At

const DISPLAY_FORMAT = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Chicago",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "numeric",
  minute: "2-digit",
  second: "2-digit",
  hour12: true,
});

export function formatDisplayTimestamp(value) {
  if (value == null || value === "") return value;
  const ms = Date.parse(value);
  if (Number.isNaN(ms)) return value; // preserve invalid semantics
  return DISPLAY_FORMAT.format(new Date(ms)); // e.g. "11/04/2026, 9:05:31 PM" - no zone label
}

export function formatCellsForDisplay(kind, cells) {
  const cols = kind === "session_upsert" ? SESSION_TIME_COLUMNS : EVENT_TIME_COLUMNS;
  const out = cells.slice();
  for (const i of cols) {
    if (i < out.length) out[i] = formatDisplayTimestamp(out[i]);
  }
  return out;
}

// outbox: { pending(limit) -> [{outbox_id, kind, payload}], markExported(ids) -> void }
// The Python bridge (operator_registry.py pending-outbox / mark-exported) is one implementation.
//
// Optional OPS-1037 wiring:
//   layout:   { sessions: [...headers], events: [...headers] } - the destination tabs'
//             own headers, when they differ from the canonical payload layout. Cells
//             are projected by header name before writing; the ID column must stay
//             first, because reconciliation keys on the first cell.
//   attempts: { record({outboxId, destination, tab, kind, outcome, detail}) } - the
//             store-side export fence (operator_registry.py record-export-attempt).
//             `tab` carries the table key ("sessions" / "events"); the attempts
//             implementation names the destination's actual tab title from its
//             workbook profile. After a write confirms, the attempt is recorded
//             first and the outbox row marked exported second; a failure in either
//             leaves the row pending and the next drain reconciles by ID.
//   destination: the fence's name for the target (the workbook id).
//
// Session snapshots of one record inside a single batch collapse to the latest: the
// sheet is a current-state mirror, so an intermediate snapshot has no sheet-visible
// effect. A collapsed row is fenced 'exported' with the superseding outbox id in the
// detail and marked exported; if the winning snapshot then fails, the drain stops
// with it still pending, so no state is lost.
export async function drainOutbox({
  outbox,
  adapter,
  attempts = null,
  destination = "",
  layout = null,
  limit = 50,
  log = () => {},
}) {
  const rows = await outbox.pending(limit);
  let exported = 0;
  let failed = 0;
  if (!rows.length) return { exported, failed };
  await adapter.validateHeaders(); // header check before any live write

  const fence = async (row, table, outcome, detail = "") => {
    if (!attempts) return;
    await attempts.record({
      outboxId: row.outbox_id, destination, tab: table, kind: row.kind, outcome, detail,
    });
  };
  const fenceQuiet = async (row, table, outcome, detail = "") => {
    try {
      await fence(row, table, outcome, detail);
    } catch (err) {
      log(`outbox ${row.outbox_id}: fence record failed: ${err.message}`);
    }
  };

  // One full-grid index per tab for the whole drain (see scanTableIndex). A scan
  // failure is a drain failure before any write: report it as one failed batch.
  const indexes = {};
  const stale = { sessions: false, events: false };
  try {
    const bounds = await adapter.inspect();
    for (const table of ["sessions", "events"]) {
      indexes[table] = await scanTableIndex(adapter, table, bounds);
    }
  } catch (err) {
    log(`reconcile scan failed: ${err.message}`);
    return { exported: 0, failed: 1 };
  }
  const indexFor = async (table) => {
    if (stale[table]) {
      const bounds = await adapter.inspect();
      indexes[table] = await scanTableIndex(adapter, table, bounds);
      stale[table] = false;
    }
    return indexes[table];
  };

  if (layout) {
    for (const table of ["sessions", "events"]) {
      if (!Array.isArray(layout[table]) || layout[table][0] !== PAYLOAD_HEADERS[table][0]) {
        throw new Error(`layout for ${table} must keep "${PAYLOAD_HEADERS[table][0]}" as its first header`);
      }
    }
  }

  // Latest session snapshot per record ID within this batch.
  const latestSessionRow = new Map();
  for (const row of rows) {
    if (row.kind !== "session_upsert") continue;
    try {
      const payload = typeof row.payload === "string" ? JSON.parse(row.payload) : row.payload;
      if (Array.isArray(payload?.cells)) latestSessionRow.set(payload.cells[0], row.outbox_id);
    } catch {
      // A malformed payload fails in the row loop below, in order; it wins nothing here.
    }
  }

  for (const row of rows) {
    const table = row.kind === "session_upsert" ? "sessions" : row.kind === "event_append" ? "events" : null;
    try {
      if (table === null) throw new Error(`unknown outbox kind ${row.kind}`);
      const payload = typeof row.payload === "string" ? JSON.parse(row.payload) : row.payload;
      const cells = formatCellsForDisplay(row.kind, payload.cells);
      // A credential-shaped cell is a permanent per-row refusal: count and log it, leave the row
      // pending for the Operator, and keep draining - one refused row must not wedge later rows.
      const headers = row.kind === "session_upsert" ? SESSIONS_HEADERS : EVENTS_HEADERS;
      const refusedIndex = cells.findIndex((cell) => scanCellText(cell) !== null);
      if (refusedIndex >= 0) {
        failed += 1;
        await fenceQuiet(row, table, "failed", `credential-shaped cell refused (${headers[refusedIndex]})`);
        log(`outbox ${row.outbox_id}: credential-shaped cell refused (${headers[refusedIndex]}); left pending`);
        continue;
      }

      if (row.kind === "session_upsert" && latestSessionRow.get(payload.cells[0]) !== row.outbox_id) {
        const winner = latestSessionRow.get(payload.cells[0]);
        await fence(row, table, "exported", `superseded in batch by ${winner}; latest snapshot written once`);
        await outbox.markExported([row.outbox_id]);
        exported += 1;
        continue;
      }

      const writeCells = layout ? projectCells(table, cells, layout[table]) : cells;
      const index = await indexFor(table);
      const matches = index.get(payload.cells[0]) ?? [];
      if (matches.length > 1) {
        failed += 1;
        await fenceQuiet(row, table, "ambiguous",
          `id ${payload.cells[0]} appears on ${matches.length} rows (${matches.join(", ")}); refusing to write`);
        log(`outbox ${row.outbox_id}: duplicate Record ID or Event ID in destination (${matches.join(", ")}); left pending`);
        break; // ambiguous destination state needs a human; later rows must not pile onto it
      }
      const existing = matches.length === 1 ? matches[0] : null;

      let outcome = "exported";
      if (row.kind === "event_append" && existing != null) {
        outcome = "already_present"; // reconcile by Event ID before append
      } else if (row.kind === "session_upsert" && existing != null) {
        await adapter.updateSessionRow(existing, writeCells, { validate: false });
      } else if (row.kind === "session_upsert") {
        const res = await adapter.appendSession(writeCells, { validate: false });
        const rn = res?.rowNumber;
        if (Number.isSafeInteger(rn)) index.set(payload.cells[0], [rn]);
        else stale[table] = true; // append landed but its row is unconfirmed: rescan before reuse
      } else {
        const res = await adapter.appendEvent(writeCells, { validate: false });
        const rn = res?.rowNumber;
        if (Number.isSafeInteger(rn)) index.set(payload.cells[0], [rn]);
        else stale[table] = true;
      }
      await fence(row, table, outcome);
      await outbox.markExported([row.outbox_id]); // only after the write + fence confirm
      exported += 1;
    } catch (err) {
      failed += 1;
      log(`outbox ${row.outbox_id}: ${err.message}`);
      if (table !== null) await fenceQuiet(row, table, "failed", err.message);
      // A deterministic local validation refusal (invalid_row, credential_in_cell) means nothing
      // was sent and a retry can never succeed: skip past the poison row instead of wedging the
      // drain behind it. Anything else may be uncertain or transient, so stop the drain: later
      // snapshots must not overtake an unexported earlier one.
      if (err && (err.code === "invalid_row" || err.code === "credential_in_cell")) {
        continue;
      }
      break; // stop the drain: later snapshots must not overtake an unexported earlier one
    }
  }
  return { exported, failed };
}
