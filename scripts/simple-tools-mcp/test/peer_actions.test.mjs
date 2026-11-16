import test from "node:test";
import assert from "node:assert/strict";
import { PEER_ACTIONS_TOOLS, PEER_ACTIONS_WRITE_TOOLS, buildPeerActionsRow } from "../src/peer_actions.js";

test("peer_actions tool contract", () => {
  assert.equal(PEER_ACTIONS_TOOLS.length, 1);
  assert.equal(PEER_ACTIONS_TOOLS[0].name, "peer_actions_log");
  assert.equal(PEER_ACTIONS_WRITE_TOOLS.has("peer_actions_log"), true);
});

test("buildPeerActionsRow uses identity seat and central CT label", () => {
  const { row } = buildPeerActionsRow(
    {
      requesting_seat: "CURSOR",
      request: "help with sheet",
      action_taken: "logged manually",
      result: "done",
    },
    "GB-ORACLE",
  );
  assert.equal(row[1], "GB-ORACLE");
  assert.equal(row[2], "CURSOR");
  assert.match(row[0], / CT$/);
});
