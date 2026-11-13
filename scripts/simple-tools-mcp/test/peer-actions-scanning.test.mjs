import assert from "node:assert/strict";
import test from "node:test";
import { generateKeyPairSync } from "node:crypto";

import { buildPeerActionsRow, PEER_ACTIONS_METHODS } from "../src/peer_actions.js";
import { _resetInfisicalForTests } from "../src/infisical.js";

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

test("a missing transport credential list fails before dry-run output", async () => {
  const args = { requesting_seat: "CLAUDE", request: "message 123", action_taken: "reviewed code", result: "complete", dry_run: true };
  await assert.rejects(PEER_ACTIONS_METHODS.tool_peer_actions_log.call({ seat: "CODEX", workerEnv: {} }, args, {}), error => error.code === "not_configured" && error.message.includes("this.known"));
});

test("compact fetched PEM body refuses before OAuth or row publication", async () => {
  // Fresh test-only PEM is generated in memory and never authenticates.
  const { privateKey: key } = generateKeyPairSync("ed25519", { privateKeyEncoding: { type: "pkcs8", format: "pem" }, publicKeyEncoding: { type: "spki", format: "pem" } });
  const compact = key.split(/\r?\n/).filter(line => !line.startsWith("-----")).join("");
  const args = { requesting_seat: "CLAUDE", request: compact, action_taken: "reviewed code", result: "complete" };
  const context = { seat: "CODEX", known: [], workerEnv: { INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "project-test", INFISICAL_SHEETS_SA_TOKEN: "synthetic own reader" }, reserve: async () => {} };
  const original = globalThis.fetch;
  const calls = [];
  _resetInfisicalForTests();
  globalThis.fetch = async url => {
    calls.push(String(url));
    return Response.json({ secret: { secretValue: JSON.stringify({ client_email: "example@example.test", private_key: key }) } });
  };
  try {
    await assert.rejects(PEER_ACTIONS_METHODS.tool_peer_actions_log.call(context, args, {}), error => {
      assert.equal(error.code, "refused_secret");
      assert.ok(error.message.includes("request"));
      assert.ok(!error.message.includes(compact));
      return true;
    });
    assert.equal(calls.length, 1);
    assert.ok(!calls.some(url => url.includes("googleapis.com")));
  } finally {
    globalThis.fetch = original;
    _resetInfisicalForTests();
  }
});

test("offline dry-run does not fetch an unavailable remote service account", async () => {
  const args = { requesting_seat: "CLAUDE", request: "message 123", action_taken: "reviewed code", result: "complete", dry_run: true };
  const original = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls++; throw new Error("offline"); };
  try {
    const result = await PEER_ACTIONS_METHODS.tool_peer_actions_log.call({ seat: "CODEX", known: [], workerEnv: {}, reserve: async () => { throw new Error("no reservation"); } }, args, {});
    assert.equal(calls, 0);
    assert.equal(result.structuredContent.dry_run, true);
  } finally {
    globalThis.fetch = original;
  }
});

test("fetched service-account credentials refuse before OAuth or row publication", async () => {
  const known = "synthetic fetched private value";
  const args = { requesting_seat: "CLAUDE", request: known, action_taken: "reviewed code", result: "complete" };
  const context = { seat: "CODEX", known: [], workerEnv: { INFISICAL_MODE: "required", INFISICAL_WORKSPACE_ID: "project-test", INFISICAL_SHEETS_SA_TOKEN: "synthetic bootstrap" }, reserve: async () => {} };
  const original = globalThis.fetch;
  const calls = [];
  _resetInfisicalForTests();
  globalThis.fetch = async url => {
    calls.push(String(url));
    return Response.json({ secret: { secretValue: JSON.stringify({ client_email: "example@example.test", private_key: known }) } });
  };
  try {
    await assert.rejects(PEER_ACTIONS_METHODS.tool_peer_actions_log.call(context, args, {}), error => {
      assert.equal(error.code, "refused_secret");
      assert.ok(!error.message.includes(known));
      return true;
    });
    assert.equal(calls.length, 1);
    assert.ok(!calls.some(url => url.includes("googleapis.com")));
  } finally {
    globalThis.fetch = original;
    _resetInfisicalForTests();
  }
});
