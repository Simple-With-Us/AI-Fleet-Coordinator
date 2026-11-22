import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { nativeOriginResolver } from "../src/native-origin-client.js";
import { resolveNativeOriginClaims } from "../src/native-origin-context.js";
globalThis.crypto ??= webcrypto;

const session = "019a1234-abcd-7123-8123-0123456789ab";
const valid = { origin_session_claim: session, origin_letter_claim: "AA", origin_generation_claim: 7 };
const env = { REVIEW_ORIGIN_READ_URL: "https://origin.example/registry/v1/review-origin", REVIEW_ORIGIN_CODEX_READ_KEY: "fixture-only-".repeat(4) };
const resolver = (value) => nativeOriginResolver(env, { fetcher: async () => Response.json(value) });

test("network boundary returns only exact typed three-claim envelope", async () => {
  assert.deepEqual(await resolver(valid)({ seat: "CODEX", session }), valid);
  assert.deepEqual(await resolver({ ...valid, origin_generation_claim: Number.MAX_SAFE_INTEGER })({ seat: "CODEX", session }), { ...valid, origin_generation_claim: Number.MAX_SAFE_INTEGER });
  for (const value of [null, [], {}, { session, letter: "AA", generation: "7" },
    { ...valid, extra: true }, { ...valid, origin_session_claim: session.toUpperCase() },
    { ...valid, origin_session_claim: "019a1234-abcd-7123-8123-0123456789ac" },
    { ...valid, origin_letter_claim: "A" }, { ...valid, origin_letter_claim: "aa" },
    { ...valid, origin_generation_claim: "7" }, { ...valid, origin_generation_claim: true },
    { ...valid, origin_generation_claim: 0 }, { ...valid, origin_generation_claim: -1 },
    { ...valid, origin_generation_claim: 1.5 }, { ...valid, origin_generation_claim: Number.MAX_SAFE_INTEGER + 1 },
  ]) {
    await assert.rejects(resolver(value)({ seat: "CODEX", session }));
  }
});

test("independent resolver still validates and freezes nonnetwork callback claims", async () => {
  const context = { state: "exact", session };
  const result = await resolveNativeOriginClaims("CODEX", context, async () => valid);
  assert.equal(result.state, "resolved");
  assert.ok(Object.isFrozen(result.claims));
  for (const value of [{ ...valid, extra: true }, { ...valid, origin_generation_claim: "7" },
    { ...valid, origin_session_claim: "019a1234-abcd-7123-8123-0123456789ac" }]) {
    assert.equal((await resolveNativeOriginClaims("CODEX", context, async () => value)).state, "held");
  }
});
