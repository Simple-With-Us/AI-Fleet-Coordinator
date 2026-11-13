import assert from "node:assert/strict";
import test from "node:test";

import { buildPeerActionsRow, PEER_ACTIONS_METHODS } from "../src/peer_actions.js";

test("every hosted registry text field is scanned before a row exists", () => {
  // Synthetic scanner fixture assembled at runtime; never a credential.
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

test("hosted loaded credentials refuse before dry-run output or network writes", async () => {
  const known = "synthetic loaded credential";
  for (const dry_run of [true, false]) {
    for (const field of ["request", "action_taken", "result"]) {
      let reservations = 0;
      const context = { seat: "CODEX", known: [known], workerEnv: {}, reserve: async () => { reservations++; } };
      const args = { requesting_seat: "CLAUDE", request: "message 123", action_taken: "reviewed code", result: "complete", dry_run, [field]: known };
      await assert.rejects(PEER_ACTIONS_METHODS.tool_peer_actions_log.call(context, args, {}), error => {
        assert.equal(error.code, "refused_secret");
        assert.ok(error.message.includes(field));
        assert.ok(!error.message.includes(known));
        return true;
      });
      assert.equal(reservations, 0);
    }
  }
});
