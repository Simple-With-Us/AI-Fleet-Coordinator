// gr_comment_pr must accept the optional intent_id dedupe override end to
// end:  the advertised MCP input schema (additionalProperties:false) names it,
// and the relay transport forwards caller args verbatim (sentry thread
// 4535033892 - the schema used to reject it before the relay could see it).
// No credential-shaped literals here (same write-time scan constraint as
// gr-dispatch-inputs-schema.test.mjs).

import test from "node:test";
import assert from "node:assert/strict";
import { GR_TOOLS, GrRelayClient } from "../src/gr-tools.js";

const INTENT = "pr-1780-kody-refresh";

test("gr_comment_pr advertises optional intent_id with the dispatch pattern", () => {
  const tool = GR_TOOLS.find((t) => t.name === "gr_comment_pr");
  assert.deepEqual(tool.inputSchema.properties.intent_id, {
    type: "string",
    pattern: "^[A-Za-z0-9][A-Za-z0-9_.:-]{7,99}$",
  });
  assert.equal(tool.inputSchema.additionalProperties, false);
  assert.ok(!tool.inputSchema.required.includes("intent_id"));
});

test("intent_id reaches the relay request body unchanged", async () => {
  const args = { repo: "Simple-With-Us/AI-Fleet-Coordinator", number: 1780, body: "review follow-up", intent_id: INTENT };
  let sent = null;
  const client = new GrRelayClient({
    baseUrl: "https://relay.invalid",
    accessId: "test-access-id",
    accessSecret: "test-access-secret",
    bearer: "test-client-bearer",
    fetch: async (url, init) => {
      assert.equal(url, "https://relay.invalid/v1/comment_pr");
      sent = JSON.parse(init.body);
      return new Response(JSON.stringify({ ok: true }));
    },
  });
  const result = await client.call("comment_pr", args);
  assert.equal(result.status, 200);
  assert.equal(sent.intent_id, INTENT);
  assert.equal(sent.number, 1780);
});
