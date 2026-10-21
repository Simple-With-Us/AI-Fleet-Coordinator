// Tests for agent_sync/operator_sheets_export.mjs (AFC-412 PR2).
// Run: node --test scripts/test_operator_sheets_export.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { drainOutbox, SESSIONS_HEADERS, EVENTS_HEADERS } from "./agent_sync/operator_sheets_export.mjs";

function fakeAdapter() {
  const sessions = []; // { rowNumber, cells }
  const events = [];
  return {
    calls: [],
    async validateHeaders() { this.calls.push("validateHeaders"); },
    async readRows(table, { startRow, limit }) {
      const src = table === "sessions" ? sessions : events;
      return { rows: src.slice(startRow - 2, startRow - 2 + limit) };
    },
    async appendSession(cells) {
      this.calls.push("appendSession");
      sessions.push({ rowNumber: sessions.length + 2, cells });
    },
    async updateSessionRow(rowNumber, cells) {
      this.calls.push("updateSessionRow");
      const i = sessions.findIndex((r) => r.rowNumber === rowNumber);
      sessions[i] = { rowNumber, cells };
    },
    async appendEvent(cells) {
      this.calls.push("appendEvent");
      events.push({ rowNumber: events.length + 2, cells });
    },
  };
}

function fakeOutbox(rows) {
  return {
    marked: [],
    async pending() { return rows; },
    async markExported(ids) { this.marked.push(...ids); },
  };
}

const sessionCells = (id, label = "A") => {
  const c = new Array(SESSIONS_HEADERS.length).fill("");
  c[0] = id; c[1] = label; c[2] = "MM";
  return c;
};
const eventCells = (id, recordId) => {
  const c = new Array(EVENTS_HEADERS.length).fill("");
  c[0] = id; c[2] = recordId; c[4] = "register";
  return c;
};

test("new session appends and is marked exported", async () => {
  const adapter = fakeAdapter();
  const outbox = fakeOutbox([{ outbox_id: "o1", kind: "session_upsert",
    payload: JSON.stringify({ cells: sessionCells("r1") }) }]);
  const res = await drainOutbox({ outbox, adapter });
  assert.deepEqual(res, { exported: 1, failed: 0 });
  assert.deepEqual(outbox.marked, ["o1"]);
  assert.ok(adapter.calls.includes("validateHeaders"));
  assert.ok(adapter.calls.includes("appendSession"));
  assert.ok(!adapter.calls.includes("updateSessionRow"));
});

test("existing Record ID updates instead of appending", async () => {
  const adapter = fakeAdapter();
  await adapter.appendSession(sessionCells("r1"));
  adapter.calls = [];
  const outbox = fakeOutbox([{ outbox_id: "o2", kind: "session_upsert",
    payload: JSON.stringify({ cells: sessionCells("r1", "B") }) }]);
  const res = await drainOutbox({ outbox, adapter });
  assert.deepEqual(res, { exported: 1, failed: 0 });
  assert.deepEqual(adapter.calls, ["validateHeaders", "updateSessionRow"]);
});

test("event already present is not appended twice", async () => {
  const adapter = fakeAdapter();
  await adapter.appendEvent(eventCells("e1", "r1"));
  adapter.calls = [];
  const outbox = fakeOutbox([{ outbox_id: "o3", kind: "event_append",
    payload: JSON.stringify({ cells: eventCells("e1", "r1") }) }]);
  const res = await drainOutbox({ outbox, adapter });
  assert.deepEqual(res, { exported: 1, failed: 0 });
  assert.ok(!adapter.calls.includes("appendEvent"));
});

test("adapter failure leaves the row pending", async () => {
  const adapter = fakeAdapter();
  adapter.appendSession = async () => { throw new Error("HTTP 503"); };
  const outbox = fakeOutbox([{ outbox_id: "o4", kind: "session_upsert",
    payload: JSON.stringify({ cells: sessionCells("r9") }) }]);
  const res = await drainOutbox({ outbox, adapter });
  assert.deepEqual(res, { exported: 0, failed: 1 });
  assert.deepEqual(outbox.marked, []);
});

test("unknown outbox kind fails without a write", async () => {
  const adapter = fakeAdapter();
  const outbox = fakeOutbox([{ outbox_id: "o5", kind: "delete_everything",
    payload: "{}" }]);
  const res = await drainOutbox({ outbox, adapter });
  assert.deepEqual(res, { exported: 0, failed: 1 });
  assert.deepEqual(adapter.calls, ["validateHeaders"]);
});
