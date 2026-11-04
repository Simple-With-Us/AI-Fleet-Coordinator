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
