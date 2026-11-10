// Tests for simple_tools/operator_sheets_export.mjs (AFC-412 PR2).
// Run: node --test scripts/test_operator_sheets_export.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { drainOutbox, SESSIONS_HEADERS, EVENTS_HEADERS } from "./simple_tools/operator_sheets_export.mjs";

function fakeAdapter() {
  const sessions = []; // { rowNumber, cells }
  const events = [];
  return {
    calls: [],
    async inspect() { return { sessions: { rowCount: sessions.length + 1 }, events: { rowCount: events.length + 1 } }; },
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

test('failed acknowledgement reconciles session on next drain without duplicate', async()=>{
 const adapter=fakeAdapter();const outbox=fakeOutbox([{outbox_id:'ack1',kind:'session_upsert',payload:{cells:sessionCells('ack-record')}}]);
 outbox.markExported=async()=>{throw Error('ack uncertain')};
 assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:0,failed:1});
 outbox.markExported=async ids=>outbox.marked.push(...ids);
 assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:1,failed:0});
 assert.equal(adapter.calls.filter(x=>x==='appendSession').length,1);
 assert.equal(adapter.calls.filter(x=>x==='updateSessionRow').length,1);
});
test('failed acknowledgement reconciles event on next drain without duplicate', async()=>{
 const adapter=fakeAdapter();const outbox=fakeOutbox([{outbox_id:'ack2',kind:'event_append',payload:{cells:eventCells('ack-event','r1')}}]);
 outbox.markExported=async()=>{throw Error('ack uncertain')};
 assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:0,failed:1});
 outbox.markExported=async ids=>outbox.marked.push(...ids);
 assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:1,failed:0});
 assert.equal(adapter.calls.filter(x=>x==='appendEvent').length,1);
});
test('header mismatch refuses all writes and acknowledgements',async()=>{
 const adapter=fakeAdapter();adapter.validateHeaders=async()=>{throw Error('header mismatch')};const outbox=fakeOutbox([{outbox_id:'header',kind:'session_upsert',payload:{cells:sessionCells('r1')}}]);
 await assert.rejects(drainOutbox({outbox,adapter}),/header mismatch/);assert.deepEqual(outbox.marked,[]);assert.deepEqual(adapter.calls,[]);
});
test('earlier failure fences later snapshots',async()=>{
 const adapter=fakeAdapter();adapter.appendSession=async()=>{throw Error('write failed')};const outbox=fakeOutbox([{outbox_id:'first',kind:'session_upsert',payload:{cells:sessionCells('r1')}},{outbox_id:'second',kind:'event_append',payload:{cells:eventCells('e1','r1')}}]);
 assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:0,failed:1});assert.deepEqual(outbox.marked,[]);assert.ok(!adapter.calls.includes('appendEvent'));
});
test('reconcile scans beyond first 200 rows',async()=>{
 const adapter=fakeAdapter();for(let i=0;i<205;i++)await adapter.appendSession(sessionCells('r'+i));adapter.calls=[];
 const outbox=fakeOutbox([{outbox_id:'page',kind:'session_upsert',payload:{cells:sessionCells('r204','changed')}}]);
 assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:1,failed:0});assert.deepEqual(adapter.calls,['validateHeaders','updateSessionRow']);
});
test('malformed payload stays pending and blocks later items',async()=>{
 const adapter=fakeAdapter();const outbox=fakeOutbox([{outbox_id:'bad',kind:'session_upsert',payload:'{'},{outbox_id:'later',kind:'event_append',payload:{cells:eventCells('e1','r1')}}]);
 assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:0,failed:1});assert.deepEqual(outbox.marked,[]);assert.deepEqual(adapter.calls,['validateHeaders']);
});

test('empty first page does not hide an existing ID on a later grid page',async()=>{
 const writes=[];const scans=[];const adapter={validateHeaders:async()=>{},inspect:async()=>({sessions:{rowCount:403},events:{rowCount:1}}),readRows:async(table,{startRow,limit})=>{scans.push([startRow,limit]);return startRow===202?[{rowNumber:202,cells:sessionCells('sparse')}]:[]},updateSessionRow:async n=>writes.push(['update',n]),appendSession:async()=>writes.push(['append'])};
 const outbox=fakeOutbox([{outbox_id:'sparse',kind:'session_upsert',payload:{cells:sessionCells('sparse')}}]);assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:1,failed:0});assert.deepEqual(writes,[['update',202]]);assert.deepEqual(scans,[[2,200],[202,200],[402,2]]);
});
test('duplicate target IDs across pages refuse writes and acknowledgement',async()=>{
 const writes=[];const adapter={validateHeaders:async()=>{},inspect:async()=>({sessions:{rowCount:401},events:{rowCount:1}}),readRows:async(table,{startRow})=>[{rowNumber:startRow,cells:sessionCells('same')}],updateSessionRow:async n=>writes.push(n),appendSession:async()=>writes.push('append')};const outbox=fakeOutbox([{outbox_id:'duplicate',kind:'session_upsert',payload:{cells:sessionCells('same')}}]);assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:0,failed:1});assert.deepEqual(writes,[]);assert.deepEqual(outbox.marked,[]);
});
test('invalid physical grid bounds fail closed',async()=>{
 const adapter=fakeAdapter();adapter.inspect=async()=>({sessions:{rowCount:0}});const outbox=fakeOutbox([{outbox_id:'bounds',kind:'session_upsert',payload:{cells:sessionCells('r1')}}]);assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:0,failed:1});assert.deepEqual(outbox.marked,[]);assert.ok(!adapter.calls.includes('appendSession'));
});

test('event reconciliation scans blank pages and refuses duplicate Event IDs',async()=>{
 let duplicate=false;const writes=[];const adapter={validateHeaders:async()=>{},inspect:async()=>({sessions:{rowCount:1},events:{rowCount:402}}),readRows:async(table,{startRow})=>startRow===202||duplicate&&startRow===402?[{rowNumber:startRow,cells:eventCells('event-sparse','r')}]:[],appendEvent:async()=>writes.push('append')};
 const outbox=fakeOutbox([{outbox_id:'event',kind:'event_append',payload:{cells:eventCells('event-sparse','r')}}]);assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:1,failed:0});assert.deepEqual(writes,[]);duplicate=true;outbox.marked=[];assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:0,failed:1});assert.deepEqual(outbox.marked,[]);
});
test('out-of-page physical row number refuses mutation',async()=>{
 const adapter=fakeAdapter();adapter.inspect=async()=>({sessions:{rowCount:2}});adapter.readRows=async()=>[{rowNumber:300,cells:sessionCells('r')}];const outbox=fakeOutbox([{outbox_id:'badrow',kind:'session_upsert',payload:{cells:sessionCells('r')}}]);assert.deepEqual(await drainOutbox({outbox,adapter}),{exported:0,failed:1});assert.deepEqual(outbox.marked,[]);assert.ok(!adapter.calls.includes('updateSessionRow'));
});

// Display contract tests (owner rule): America/Chicago conversion, 12-hour AM/PM, no timezone
// label, text output; invalid/empty pass through; non-timestamp cells unchanged; UTC stays internal.
import { formatDisplayTimestamp, formatCellsForDisplay } from "./simple_tools/operator_sheets_export.mjs";

test("formatter converts UTC to America/Chicago 12-hour with AM/PM and no zone label", () => {
  const out = formatDisplayTimestamp("2026-11-04T15:05:31.000Z"); // 9:05:31 AM CST
  assert.equal(out, "11/04/2026, 9:05:31 AM");
  assert.ok(!/CT|CST|CDT|Central|UTC|GMT|Z$/i.test(out), "no timezone label: " + out);
  assert.equal(typeof out, "string");
});

test("formatter midnight and noon boundaries", () => {
  assert.match(formatDisplayTimestamp("2026-11-04T06:00:00.000Z"), /12:00:00 AM$/); // midnight Chicago
  assert.match(formatDisplayTimestamp("2026-11-04T18:00:00.000Z"), /12:00:00 PM$/); // noon Chicago
});

test("formatter handles DST: same UTC hour renders differently across CST/CDT", () => {
  const winter = formatDisplayTimestamp("2026-01-15T15:00:00.000Z"); // CST UTC-6 -> 9 AM
  const summer = formatDisplayTimestamp("2026-07-15T15:00:00.000Z"); // CDT UTC-5 -> 10 AM
  assert.match(winter, /9:00:00 AM$/);
  assert.match(summer, /10:00:00 AM$/);
});

test("formatter preserves the calendar date and passes through empty/invalid values", () => {
  assert.match(formatDisplayTimestamp("2026-11-04T04:30:00.000Z"), /^11\/03\/2026/); // still Nov 3 in Chicago
  assert.equal(formatDisplayTimestamp(""), "");
  assert.equal(formatDisplayTimestamp(null), null);
  assert.equal(formatDisplayTimestamp("not-a-date"), "not-a-date");
});

test("formatCellsForDisplay formats only the time columns, UTC remains in the source cells", async () => {
  const cells = sessionCells("r1");
  cells[12] = "2026-11-04T15:05:31.000Z";
  cells[13] = "";
  cells[14] = "2026-07-15T15:00:00.000Z";
  const out = formatCellsForDisplay("session_upsert", cells);
  assert.equal(out[12], "11/04/2026, 9:05:31 AM");
  assert.equal(out[13], "");
  assert.match(out[14], /10:00:00 AM$/);
  assert.equal(out[0], "r1");
  assert.equal(cells[12], "2026-11-04T15:05:31.000Z"); // internal UTC untouched
  const ev = eventCells("e1", "r1");
  ev[1] = "2026-11-04T15:05:31.000Z";
  const evOut = formatCellsForDisplay("event_append", ev);
  assert.equal(evOut[1], "11/04/2026, 9:05:31 AM");
  assert.equal(evOut[4], "register");
});

test("drainOutbox writes display-formatted timestamps to the adapter", async () => {
  const adapter = fakeAdapter();
  const cells = sessionCells("r1");
  cells[12] = "2026-11-04T15:05:31.000Z";
  const outbox = fakeOutbox([{ outbox_id: "o1", kind: "session_upsert",
    payload: JSON.stringify({ cells }) }]);
  const res = await drainOutbox({ outbox, adapter });
  assert.deepEqual(res, { exported: 1, failed: 0 });
  const written = adapter.sessions?.[0] ?? null;
  // adapter fake stores via appendSession into closure; read through readRows
  const page = await adapter.readRows("sessions", { startRow: 2, limit: 10 });
  const row = page.rows[0].cells ?? page.rows[0];
  assert.equal(row[12], "11/04/2026, 9:05:31 AM");
});

test("a credential-shaped cell fails its row without wedging the drain", async () => {
  const adapter = fakeAdapter();
  const poison = sessionCells("r-poison");
  poison[6] = "see gh" + "p_" + "x".repeat(36); // built in parts: no credential literal in the fixture
  const outbox = fakeOutbox([
    { outbox_id: "o1", kind: "session_upsert", payload: JSON.stringify({ cells: poison }) },
    { outbox_id: "o2", kind: "session_upsert", payload: JSON.stringify({ cells: sessionCells("r2") }) },
  ]);
  const logs = [];
  const res = await drainOutbox({ outbox, adapter, log: (m) => logs.push(m) });
  assert.deepEqual(res, { exported: 1, failed: 1 });
  assert.deepEqual(outbox.marked, ["o2"]); // the refused row stays pending, the healthy row exports
  assert.ok(logs.some((m) => m.includes("o1") && m.includes("Title")));
});

test("drainOutbox skips a deterministically invalid row instead of wedging the drain", async () => {
  const adapter = fakeAdapter();
  const realAppend = adapter.appendEvent.bind(adapter);
  adapter.appendEvent = async (cells) => {
    if (cells[0] === "poison") {
      const err = new Error("row must be an array of strings");
      err.code = "invalid_row";
      throw err;
    }
    return realAppend(cells);
  };
  const rows = [
    { outbox_id: "o-a", kind: "event_append", payload: { cells: eventCells("a-evt", "r") } },
    { outbox_id: "o-b", kind: "event_append", payload: { cells: eventCells("poison", "r") } },
    { outbox_id: "o-c", kind: "event_append", payload: { cells: eventCells("c-evt", "r") } },
  ];
  const marked = [];
  const outbox = {
    marked,
    async pending() { return rows.filter((r) => !marked.includes(r.outbox_id)); },
    async markExported(ids) { marked.push(...ids); },
  };
  assert.deepEqual(await drainOutbox({ outbox, adapter }), { exported: 2, failed: 1 });
  assert.deepEqual(marked, ["o-a", "o-c"]);
  // poison row stays pending for the Operator; a later drain retries only it and fails it again
  assert.deepEqual(await drainOutbox({ outbox, adapter }), { exported: 0, failed: 1 });
  assert.deepEqual(marked, ["o-a", "o-c"]);
});

// ---- OPS-1037: fence recording, layout projection, batch collapse, stale index ----

function fakeAttempts() {
  return { records: [], async record(r) { this.records.push(r); } };
}

test("fence records exported and already_present attempts with destination, tab and kind", async () => {
  const adapter = fakeAdapter();
  await adapter.appendEvent(eventCells("e1", "r1")); // event already in the sheet
  adapter.calls = [];
  const attempts = fakeAttempts();
  const outbox = fakeOutbox([
    { outbox_id: "f1", kind: "session_upsert", payload: JSON.stringify({ cells: sessionCells("r1") }) },
    { outbox_id: "f2", kind: "event_append", payload: JSON.stringify({ cells: eventCells("e1", "r1") }) },
  ]);
  const res = await drainOutbox({ outbox, adapter, attempts, destination: "wb-1" });
  assert.deepEqual(res, { exported: 2, failed: 0 });
  assert.deepEqual(attempts.records, [
    { outboxId: "f1", destination: "wb-1", tab: "sessions", kind: "session_upsert", outcome: "exported", detail: "" },
    { outboxId: "f2", destination: "wb-1", tab: "events", kind: "event_append", outcome: "already_present", detail: "" },
  ]);
  assert.deepEqual(outbox.marked, ["f1", "f2"]);
});

test("fence records a failed attempt and the row stays pending", async () => {
  const adapter = fakeAdapter();
  adapter.appendSession = async () => { throw new Error("HTTP 503"); };
  const attempts = fakeAttempts();
  const outbox = fakeOutbox([{ outbox_id: "f3", kind: "session_upsert",
    payload: JSON.stringify({ cells: sessionCells("r9") }) }]);
  const res = await drainOutbox({ outbox, adapter, attempts, destination: "wb-1" });
  assert.deepEqual(res, { exported: 0, failed: 1 });
  assert.equal(attempts.records.length, 1);
  assert.equal(attempts.records[0].outcome, "failed");
  assert.match(attempts.records[0].detail, /503/);
  assert.deepEqual(outbox.marked, []);
});

test("duplicate IDs in the destination fence as ambiguous and stop the drain", async () => {
  const writes = [];
  const adapter = {
    validateHeaders: async () => {},
    inspect: async () => ({ sessions: { rowCount: 401 }, events: { rowCount: 1 } }),
    readRows: async (table, { startRow }) => [{ rowNumber: startRow, cells: sessionCells("same") }],
    appendSession: async () => writes.push("append"),
    updateSessionRow: async () => writes.push("update"),
    appendEvent: async () => writes.push("event"),
  };
  const attempts = fakeAttempts();
  const outbox = fakeOutbox([
    { outbox_id: "a1", kind: "session_upsert", payload: JSON.stringify({ cells: sessionCells("same") }) },
    { outbox_id: "a2", kind: "event_append", payload: JSON.stringify({ cells: eventCells("e9", "same") }) },
  ]);
  const res = await drainOutbox({ outbox, adapter, attempts, destination: "wb-1" });
  assert.deepEqual(res, { exported: 0, failed: 1 });
  assert.deepEqual(writes, []);
  assert.deepEqual(outbox.marked, []); // the later row never runs: the drain stopped
  assert.equal(attempts.records.length, 1);
  assert.equal(attempts.records[0].outcome, "ambiguous");
  assert.match(attempts.records[0].detail, /appears on 2 rows/);
});

test("layout projection writes the workbook's own columns by header name", async () => {
  const WB_SESSIONS = ["Record ID", "Label", "Seat", "Platform", "Host", "Native Session ID",
    "Title", "Project", "Thread Link", "Apple Note Link"];
  const adapter = fakeAdapter();
  const cells = sessionCells("r1", "A");
  cells[5] = "native-1"; cells[6] = "A Title"; cells[10] = "active"; cells[15] = "test source";
  const outbox = fakeOutbox([{ outbox_id: "p1", kind: "session_upsert",
    payload: JSON.stringify({ cells }) }]);
  const res = await drainOutbox({ outbox, adapter, layout: { sessions: WB_SESSIONS, events: EVENTS_HEADERS } });
  assert.deepEqual(res, { exported: 1, failed: 0 });
  const page = await adapter.readRows("sessions", { startRow: 2, limit: 10 });
  const written = page.rows[0].cells ?? page.rows[0];
  assert.equal(written.length, 10);
  assert.deepEqual(written.slice(0, 7), ["r1", "A", "MM", "", "", "native-1", "A Title"]);
});

test("layout that moves the ID column is refused before any write", async () => {
  const adapter = fakeAdapter();
  const outbox = fakeOutbox([{ outbox_id: "p2", kind: "session_upsert",
    payload: JSON.stringify({ cells: sessionCells("r1") }) }]);
  await assert.rejects(
    drainOutbox({ outbox, adapter, layout: { sessions: ["Label", "Record ID"], events: EVENTS_HEADERS } }),
    /must keep "Record ID" as its first header/);
  assert.deepEqual(outbox.marked, []);
});

test("session snapshots collapse to the latest within one batch", async () => {
  const adapter = fakeAdapter();
  const attempts = fakeAttempts();
  const outbox = fakeOutbox([
    { outbox_id: "s1", kind: "session_upsert", payload: JSON.stringify({ cells: sessionCells("r1", "A") }) },
    { outbox_id: "s2", kind: "session_upsert", payload: JSON.stringify({ cells: sessionCells("r1", "B") }) },
  ]);
  const res = await drainOutbox({ outbox, adapter, attempts, destination: "wb-1" });
  assert.deepEqual(res, { exported: 2, failed: 0 });
  assert.deepEqual(outbox.marked, ["s1", "s2"]);
  assert.equal(adapter.calls.filter((c) => c === "appendSession").length, 1);
  const page = await adapter.readRows("sessions", { startRow: 2, limit: 10 });
  const rows = page.rows ?? page;
  assert.equal(rows.length, 1);
  assert.equal((rows[0].cells ?? rows[0])[1], "B"); // the latest snapshot's label
  assert.equal(attempts.records[0].outcome, "exported");
  assert.match(attempts.records[0].detail, /superseded in batch by s2/);
  assert.equal(attempts.records[1].outcome, "exported");
});

test("an append without a confirmed row number rescans before the next lookup", async () => {
  const adapter = fakeAdapter(); // fake appends return no rowNumber
  const outbox = fakeOutbox([
    { outbox_id: "d1", kind: "event_append", payload: JSON.stringify({ cells: eventCells("e1", "r1") }) },
    { outbox_id: "d2", kind: "event_append", payload: JSON.stringify({ cells: eventCells("e1", "r1") }) },
  ]);
  const res = await drainOutbox({ outbox, adapter });
  assert.deepEqual(res, { exported: 2, failed: 0 });
  assert.equal(adapter.calls.filter((c) => c === "appendEvent").length, 1); // second reconciled, not duplicated
});

test("superseded snapshots stay pending when the winning snapshot is refused", async () => {
  const adapter = fakeAdapter();
  const winner = sessionCells("r1", "B");
  winner[6] = "see gh" + "p_" + "x".repeat(36); // Title is scanned: the winner can never export
  const attempts = fakeAttempts();
  const outbox = fakeOutbox([
    { outbox_id: "w1", kind: "session_upsert", payload: JSON.stringify({ cells: sessionCells("r1", "A") }) },
    { outbox_id: "w2", kind: "session_upsert", payload: JSON.stringify({ cells: winner }) },
  ]);
  const res = await drainOutbox({ outbox, adapter, attempts, destination: "wb-1" });
  assert.deepEqual(res, { exported: 0, failed: 1 });
  assert.deepEqual(outbox.marked, []); // the loser is NOT marked exported over a refused winner
  assert.ok(!attempts.records.some((r) => r.outboxId === "w1" && r.outcome === "exported"));
});

test("recordBatch attempts receive one batched call per flush", async () => {
  const adapter = fakeAdapter();
  const batches = [];
  const attempts = { recordBatch: async (records) => { batches.push(records); } };
  const outbox = fakeOutbox([
    { outbox_id: "b1", kind: "session_upsert", payload: JSON.stringify({ cells: sessionCells("r1") }) },
    { outbox_id: "b2", kind: "event_append", payload: JSON.stringify({ cells: eventCells("e1", "r1") }) },
    { outbox_id: "b3", kind: "event_append", payload: JSON.stringify({ cells: eventCells("e2", "r1") }) },
  ]);
  const res = await drainOutbox({ outbox, adapter, attempts, destination: "wb-1" });
  assert.deepEqual(res, { exported: 3, failed: 0 });
  assert.equal(batches.length, 1);
  assert.deepEqual(batches[0].map((r) => r.outboxId), ["b1", "b2", "b3"]);
  assert.deepEqual(outbox.marked, ["b1", "b2", "b3"]);
});
