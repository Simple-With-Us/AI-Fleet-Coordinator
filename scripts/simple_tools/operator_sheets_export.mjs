// Async idempotent exporter: Operator registry outbox -> fleet reference workbook (AFC-412 PR2).
//
// The exporter is the sole programmatic writer of the Sessions (2033000102) and Events
// (2033000103) tabs; it never touches Reference (2033000101). It depends on the AFC-428 adapter
// surface - inspect(), validateHeaders(), readRows(table, {startRow, limit}),
// appendSession(cells), updateSessionRow(rowNumber, cells), appendEvent(cells) - injected here so
// the adapter stays Codex's slice. RAW writes, no automatic mutation retries: after any failure
// the row stays pending and the next drain reconciles by id before writing.
//
// Adapter asks folded in (owner-approved plan 2026-10-20):
//   (a) readRows must return sheet row numbers, or findSessionRowByRecordId must exist - the
//       Sessions upsert cannot target updateSessionRow without one of them;
//   (b) Events recovery reconcile is a bounded readRows scan (event ids are ULIDs, so rows are
//       roughly time-ordered; the scan stops at end of data);
//   (c) appendSession/appendEvent returning the written row number helps logging - optional here.

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

// Find a sheet row by its first-column id. Returns the 1-based sheet row number or null.
// Requires ask (a): readRows pages that carry row numbers ({ rowNumber, cells } per row), or a
// dedicated findSessionRowByRecordId on the adapter for the Sessions tab.
async function findRowById(adapter, table, id) {
  if (table === "sessions" && typeof adapter.findSessionRowByRecordId === "function") {
    return adapter.findSessionRowByRecordId(id);
  }
  let startRow = 2; // row 1 is the header
  for (;;) {
    const page = await adapter.readRows(table, { startRow, limit: PAGE_SIZE });
    const rows = page.rows ?? page;
    if (!rows.length) return null;
    for (const r of rows) {
      const rowNumber = r.rowNumber ?? r.row;
      const cells = r.cells ?? r.values ?? r;
      if (rowNumber == null) {
        throw new Error("adapter.readRows did not return row numbers (adapter ask (a))");
      }
      if (cells[0] === id) return rowNumber;
    }
    if (rows.length < PAGE_SIZE) return null;
    startRow += PAGE_SIZE;
  }
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
      if (row.kind === "session_upsert") {
        await upsertSession(adapter, payload.cells);
      } else if (row.kind === "event_append") {
        await appendEventOnce(adapter, payload.cells);
      } else {
        throw new Error(`unknown outbox kind ${row.kind}`);
      }
      await outbox.markExported([row.outbox_id]); // only after the write confirms
      exported += 1;
    } catch (err) {
      failed += 1;
      log(`outbox ${row.outbox_id}: ${err.message}`);
      break; // stop the drain: later snapshots must not overtake an unexported earlier one
    }
  }
  return { exported, failed };
}
