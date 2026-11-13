import test from "node:test";
import assert from "node:assert/strict";
import { PEER_ACTIONS_METHODS, PEER_ACTIONS_TOOLS, PEER_ACTIONS_WRITE_TOOLS, SHEETS_SA_TOKEN_BINDING, buildPeerActionsRow } from "../src/peer_actions.js";
import { _resetInfisicalForTests } from "../src/infisical.js";

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

const LOG_ARGS = { requesting_seat: "TEST-SEAT", request: "r", action_taken: "a", result: "x" };
const sheetsEnv = (extra = {}) => ({ INFISICAL_MODE: "fallback", INFISICAL_WORKSPACE_ID: "ws-test", ...extra });

async function withFetch(handler, body) {
  const realFetch = globalThis.fetch;
  globalThis.fetch = handler;
  _resetInfisicalForTests();
  try {
    return await body();
  } finally {
    globalThis.fetch = realFetch;
    _resetInfisicalForTests();
  }
}

const logWith = (workerEnv) => PEER_ACTIONS_METHODS.tool_peer_actions_log.call({ seat: "TEST-SEAT", workerEnv, reserve: async () => {} }, LOG_ARGS, {});

test("an unbound INFISICAL_SHEETS_SA_TOKEN answers not_configured, and a GOOGLE_SHEETS_SA Worker binding is never read", async () => {
  let calls = 0;
  await withFetch(async () => { calls++; return new Response("{}", { status: 500 }); }, async () => {
    const valid = JSON.stringify({ client_email: "sa@example.test", private_key: "k" });
    await assert.rejects(logWith(sheetsEnv({ GOOGLE_SHEETS_SA: valid, INFISICAL_SERVICE_TOKEN: "st.broad" })), (error) => {
      assert.equal(error.code, "not_configured");
      assert.match(error.message, new RegExp(SHEETS_SA_TOKEN_BINDING));
      return true;
    });
  });
  assert.equal(calls, 0, "no Infisical call without the dedicated token, and the broad token is not used");
});

test("a scope refusal (403) on /google-sheets-sa answers not_configured in fallback and required modes", async () => {
  for (const mode of ["fallback", "required"]) {
    const seen = [];
    await withFetch(async (url, init) => { seen.push([String(url), init.headers.Authorization]); return new Response("{}", { status: 403 }); }, async () => {
      await assert.rejects(logWith(sheetsEnv({ INFISICAL_MODE: mode, [SHEETS_SA_TOKEN_BINDING]: "st.sheets", INFISICAL_SERVICE_TOKEN: "st.broad" })), (error) => error.code === "not_configured", mode);
    });
    assert.equal(seen.length, 1, mode);
    assert.match(seen[0][0], /secretPath=%2Fgoogle-sheets-sa/);
    assert.equal(seen[0][1], "Bearer st.sheets", "read with the dedicated token, never the broad one");
  }
});

test("a GOOGLE_SHEETS_SA that is not a service-account object answers invalid_secret", async () => {
  for (const value of ["'{\"type\":1}'", "null", "[]", "{}", '{"client_email":"sa@example.test"}', '{"client_email":"","private_key":"k"}']) {
    await withFetch(async () => Response.json({ secret: { secretValue: value } }), async () => {
      await assert.rejects(logWith(sheetsEnv({ [SHEETS_SA_TOKEN_BINDING]: "st.sheets" })), (error) => error.code === "invalid_secret", value);
    });
  }
});
