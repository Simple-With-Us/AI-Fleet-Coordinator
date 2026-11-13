import test from "node:test";
import assert from "node:assert/strict";
import { PEER_ACTIONS_METHODS, PEER_ACTIONS_TOOLS, PEER_ACTIONS_WRITE_TOOLS, buildPeerActionsRow } from "../src/peer_actions.js";

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

test("an unreadable or unbound GOOGLE_SHEETS_SA answers not_configured, not invalid JSON", async () => {
  const ctx = { seat: "GB-PLUMBER", workerEnv: { INFISICAL_MODE: "off" }, reserve: async () => {} };
  const args = { requesting_seat: "GB-PLUMBER", request: "r", action_taken: "a", result: "x" };
  await assert.rejects(PEER_ACTIONS_METHODS.tool_peer_actions_log.call(ctx, args, {}), (error) => {
    assert.equal(error.code, "not_configured");
    assert.match(error.message, /no Worker binding/);
    return true;
  });
});

test("a malformed GOOGLE_SHEETS_SA binding still answers invalid_secret", async () => {
  const ctx = { seat: "GB-PLUMBER", workerEnv: { INFISICAL_MODE: "off", GOOGLE_SHEETS_SA: "'{\"type\":1}'" }, reserve: async () => {} };
  const args = { requesting_seat: "GB-PLUMBER", request: "r", action_taken: "a", result: "x" };
  await assert.rejects(PEER_ACTIONS_METHODS.tool_peer_actions_log.call(ctx, args, {}), (error) => error.code === "invalid_secret");
});
