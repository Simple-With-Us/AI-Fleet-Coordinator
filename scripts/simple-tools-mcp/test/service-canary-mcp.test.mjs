import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { canaryBearerFromRequest, verifyCanaryBearer, buildCanaryServer, CANARY_TOOL } from "../src/service-canary-mcp.js";

const token = "scn_" + "A1b2C3d4".repeat(5);
const digest = createHash("sha256").update(token).digest("hex");
const req = (authorization) => new Request("https://agents.example/mcp", { headers: authorization ? { authorization } : {} });

test("only an scn_ bearer is picked up, and seat prefixes are not", () => {
  assert.equal(canaryBearerFromRequest(req(`Bearer ${token}`)), token);
  assert.equal(canaryBearerFromRequest(req("Bearer sbt_claude_abc")), null);
  assert.equal(canaryBearerFromRequest(req("Bearer gbb_x")), null);
  assert.equal(canaryBearerFromRequest(req()), null);
});

test("verification needs a well-formed token, a well-formed digest and an exact match", async () => {
  assert.equal(await verifyCanaryBearer({ SERVICE_CANARY_BEARER_SHA256: digest }, token), true);
  assert.equal(await verifyCanaryBearer({ SERVICE_CANARY_BEARER_SHA256: digest.toUpperCase() }, token), true);
  assert.equal(await verifyCanaryBearer({ SERVICE_CANARY_BEARER_SHA256: digest }, token + "x"), false);
  assert.equal(await verifyCanaryBearer({ SERVICE_CANARY_BEARER_SHA256: digest }, "scn_short"), false);
  assert.equal(await verifyCanaryBearer({}, token), false);
  assert.equal(await verifyCanaryBearer({ SERVICE_CANARY_BEARER_SHA256: "" }, token), false);
  assert.equal(await verifyCanaryBearer({ SERVICE_CANARY_BEARER_SHA256: digest.slice(1) }, token), false);
  assert.equal(await verifyCanaryBearer(undefined, token), false);
});

test("the server is built with one tool name and no events or other capability", () => {
  assert.equal(CANARY_TOOL, "deploy_canary");
  assert.ok(buildCanaryServer());
});
