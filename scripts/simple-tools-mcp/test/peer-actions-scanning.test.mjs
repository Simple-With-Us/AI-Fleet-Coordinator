import assert from "node:assert/strict";
import test from "node:test";

import { buildPeerActionsRow } from "../src/peer_actions.js";

test("every hosted registry text field is scanned before a row exists", () => {
  const unsafe = "gh" + "p_" + "A".repeat(40);
  const base = { requesting_seat: "CLAUDE", request: "message 123", action_taken: "reviewed code", result: "complete" };
  for (const field of ["request", "action_taken", "result"]) {
    assert.throws(() => buildPeerActionsRow({ ...base, [field]: unsafe }, "CODEX"), error => {
      assert.equal(error.code, "refused_secret");
      assert.ok(error.message.includes(field));
      assert.ok(!error.message.includes(unsafe));
      assert.ok(!error.message.includes("gh" + "p_"));
      return true;
    });
  }
});

test("trusted identifiers and authored facts remain loggable", () => {
  const { row } = buildPeerActionsRow({ requesting_seat: "CLAUDE", request: "message 123", action_taken: "reviewed code", result: "complete" }, "CODEX");
  assert.deepEqual(row.slice(1), ["CODEX", "CLAUDE", "message 123", "reviewed code", "complete"]);
});
