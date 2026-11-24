import test from "node:test";
import assert from "node:assert/strict";
import { seatToolStates, listenerNameFor, settle, isUnavailable, SEAT_TOOLS } from "../src/seat-tools-status.js";

const good = {
  zulipKey: "k", zulipEmail: "e@x", linearEnabled: true, linearPair: { clientId: "a", clientSecret: "b" },
  wakeKey: "w".repeat(32), fleetlinkSecret: "s", fleetlinkSeatToken: false, fleetlinkDenied: false,
  recallConfigured: true, relaySecrets: { accessId: "i", accessSecret: "s" }, relayUpstream: true,
  eventsOnly: false, notesKey: "n".repeat(32), listenerUrlOk: true,
};
const get = (row, tool) => row.tools.find((t) => t.tool === tool).state;

test("a fully configured seat is ok for every tool and lists every tool", () => {
  const row = seatToolStates("WISE", good);
  assert.equal(row.problem, "ok");
  assert.deepEqual(row.tools.map((t) => t.tool), [...SEAT_TOOLS]);
});

test("each missing credential names its tool", () => {
  assert.equal(get(seatToolStates("WISE", { ...good, zulipKey: "" }), "zulip"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, zulipEmail: "" }), "zulip"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, linearPair: { clientId: "a", clientSecret: "" } }), "linear"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, wakeKey: "short" }), "wake"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, fleetlinkSecret: "" }), "fleetlink"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, fleetlinkSecret: "", fleetlinkSeatToken: true }), "fleetlink"), "ok");
  assert.equal(get(seatToolStates("WISE", { ...good, fleetlinkDenied: true }), "fleetlink"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, recallConfigured: false }), "recall"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, relaySecrets: { accessId: "", accessSecret: "s" } }), "relay"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, relayUpstream: false }), "relay"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, notesKey: "x" }), "notes"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, notesKey: ` ${"n".repeat(31)} ` }), "notes"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, notesKey: `${"n".repeat(32)}\n` }), "notes"), "ok");
  assert.equal(get(seatToolStates("WISE", { ...good, listenerUrlOk: false }), "notes"), "missing");
  assert.equal(get(seatToolStates("WISE", { ...good, listenerUrlOk: false }), "wake"), "missing");
  assert.match(seatToolStates("WISE", { ...good, zulipKey: "" }).problem, /zulip:missing/);
});

test("a seat without the capability is not_applicable, not a fault", () => {
  const row = seatToolStates("MA", { ...good, linearEnabled: false, eventsOnly: true });
  assert.equal(get(row, "linear"), "not_applicable");
  assert.equal(get(row, "relay"), "not_applicable");
  assert.equal(row.problem, "ok");
  assert.equal(listenerNameFor("NOPE"), null);
  assert.equal(get(seatToolStates("NOPE", good), "wake"), "not_applicable");
});

test("a failed read is unavailable, never missing", async () => {
  const bad = await settle(async () => { throw new Error("x"); });
  assert.ok(isUnavailable(bad));
  assert.equal(await settle(async () => 5), 5);
  const row = seatToolStates("WISE", { ...good, zulipKey: bad, wakeKey: bad, recallConfigured: bad });
  assert.equal(get(row, "zulip"), "unavailable");
  assert.equal(get(row, "wake"), "unavailable");
  assert.equal(get(row, "recall"), "unavailable");
  assert.match(row.problem, /unavailable/);
});
