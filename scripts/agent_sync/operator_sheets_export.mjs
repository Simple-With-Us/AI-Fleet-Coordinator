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
// Short or empty pages do not end the scan, and duplicate target IDs refuse writes.
// inspect() must return { sessions: { rowCount }, events: { rowCount } }, where each
// rowCount is the positive safe-integer physical grid row count, including the header.
// readRows(table, { startRow, limit }) returns an array (or { rows: array }) of
// { rowNumber, cells } records. rowNumber is the 1-based physical row within that page;
// row/values aliases are accepted. Sparse pages do not shorten the physical grid.
// findSessionRowByRecordId and other optional fast lookups are unsupported because
// they do not prove uniqueness across the complete destination grid.
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

// Find one immutable ID across the complete physical grid, rejecting ambiguous matches.
// An optional fast lookup is deliberately not used: it cannot prove uniqueness.
async function findRowById(adapter, table, id) {
  // Physical grid bounds are required: a short Sheets values page can hide later data.
  const bounds = await adapter.inspect();
  const rowCount = bounds?.[table]?.rowCount;
  if (!Number.isSafeInteger(rowCount) || rowCount < 1) {
    throw new Error("adapter.inspect must return { sessions: { rowCount }, events: { rowCount } } with positive safe-integer physical grid row counts");
  }
  let found = null;
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
      if (cells[0] === id) {
        if (found !== null) throw new Error("duplicate Record ID or Event ID in destination");
        found = rowNumber;
      }
    }
  }
  return found;
}

async function upsertSession(adapter, cells) {
  const rowNumber = await findRowById(adapter, "sessions", cells[0]);
  if (rowNumber == null) {
    await adapter.appendSession(cells);
    return "appended";
  }
  await adapter.updateSessionRow(rowNumber, cells);
  return "updated";
}

async function appendEventOnce(adapter, cells) {
  const existing = await findRowById(adapter, "events", cells[0]);
  if (existing != null) return "already-present"; // reconcile by Event ID before append
  await adapter.appendEvent(cells);
  return "appended";
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
export async function drainOutbox({
  outbox,
  adapter,
  limit = 50,
  log = () => {},
}) {
  const rows = await outbox.pending(limit);
  let exported = 0;
  let failed = 0;
  if (!rows.length) return { exported, failed };
  await adapter.validateHeaders(); // header check before any live write
  for (const row of rows) {
    try {
      const payload = typeof row.payload === "string" ? JSON.parse(row.payload) : row.payload;
      const cells = formatCellsForDisplay(row.kind, payload.cells);
      // A credential-shaped cell is a permanent per-row refusal: count and log it, leave the row
      // pending for the Operator, and keep draining - one refused row must not wedge later rows.
      const headers = row.kind === "session_upsert" ? SESSIONS_HEADERS : EVENTS_HEADERS;
      const refusedIndex = cells.findIndex((cell) => scanCellText(cell) !== null);
      if (refusedIndex >= 0) {
        failed += 1;
        log(`outbox ${row.outbox_id}: credential-shaped cell refused (${headers[refusedIndex]}); left pending`);
        continue;
      }
      if (row.kind === "session_upsert") {
        await upsertSession(adapter, cells);
      } else if (row.kind === "event_append") {
        await appendEventOnce(adapter, cells);
      } else {
        throw new Error(`unknown outbox kind ${row.kind}`);
      }
      await outbox.markExported([row.outbox_id]); // only after the write confirms
      exported += 1;
    } catch (err) {
      failed += 1;
      log(`outbox ${row.outbox_id}: ${err.message}`);
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
