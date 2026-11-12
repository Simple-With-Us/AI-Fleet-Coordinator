// The hosted Zulip client:  realm-only egress, no redirects, the 429 budget,
// and the sentence gap on outgoing message content only.  No dependencies.

import test from "node:test";
import assert from "node:assert/strict";
import { ZulipClient, encodeParams } from "../src/zulip.js";
import { ApiError, NetworkError } from "../src/contract.js";

function client(handler) {
  const seen = [];
  let now = 0;
  const c = new ZulipClient({
    email: "grok-web-bot@simplewithus.zulipchat.com",
    key: "k".repeat(32),
    fetch: async (url, init) => {
      seen.push({ url, init });
      return handler(url, init, seen.length);
    },
    sleep: async (ms) => {
      now += ms;
    },
    now: () => now,
  });
  return { c, seen };
}
const ok = (body = {}) => new Response(JSON.stringify({ result: "success", ...body }), { status: 200 });

test("only the realm's /api/v1/, never following a redirect, with a timeout", async () => {
  const { c, seen } = client(() => ok({ x: 1 }));
  await c.get("users/me");
  assert.equal(seen[0].url, "https://simplewithus.zulipchat.com/api/v1/users/me");
  assert.equal(seen[0].init.redirect, "manual");
  assert.ok(seen[0].init.signal instanceof AbortSignal);
  for (const bad of ["../admin", "users/me?x=1", "https://evil.example/x", "users/me#f", ""]) {
    await assert.rejects(c.get(bad), ApiError, bad);
  }
  assert.equal(seen.length, 1);
  const r = client(() => new Response(null, { status: 302, headers: { Location: "https://evil.example/" } }));
  await assert.rejects(r.c.get("users/me"), (e) => e instanceof ApiError && e.code === "REDIRECT");
});

test("429:  Retry-After header or body, two attempts within 20 seconds, then ApiError 429", async () => {
  const header = client((u, i, n) => (n === 1 ? new Response("{}", { status: 429, headers: { "Retry-After": "3" } }) : ok({ id: 1 })));
  assert.equal((await header.c.get("messages")).id, 1);
  assert.equal(header.seen.length, 2);
  const body = client(() => new Response(JSON.stringify({ result: "error", code: "RATE_LIMIT_HIT", "retry-after": 30 }), { status: 429 }));
  await assert.rejects(body.c.get("messages"), (e) => e.status === 429 && e.data["retry-after"] === 30);
  assert.equal(body.seen.length, 1, "a wait past the 20-second budget is not slept");
});

test("a network failure on a write may have landed;  on a read it did not", async () => {
  const boom = client(() => {
    throw Object.assign(new Error("x"), { name: "TimeoutError" });
  });
  await assert.rejects(boom.c.post("messages", { content: "x" }), (e) => e instanceof NetworkError && e.maybeSent && e.timeout);
  await assert.rejects(boom.c.get("messages"), (e) => e instanceof NetworkError && !e.maybeSent);
});

test("the sentence gap goes on outgoing message content, never on topics or reads", async () => {
  const { c, seen } = client(() => ok({ id: 9 }));
  await c.post("messages", { type: "stream", to: 1, topic: "A.  B", content: "One.  Two." });
  const sent = new URLSearchParams(seen[0].init.body);
  assert.equal(sent.get("content"), "One.  Two.");
  assert.equal(sent.get("topic"), "A.  B");
  await c.post("messages/9/reactions", { emoji_name: "eyes" });
  assert.equal(encodeParams({ a: true, b: 2, c: [{ operator: "channel", operand: 5 }], d: "x", e: null }), "a=true&b=2&c=%5B%7B%22operator%22%3A%22channel%22%2C%22operand%22%3A5%7D%5D&d=x");
});
