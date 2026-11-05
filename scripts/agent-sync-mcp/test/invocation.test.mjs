// The per-invocation budget (src/invocation.js) and the guard bindings it stands
// beside.  The workerd half is test/mcp-guards.workerd.mjs.

import test from "node:test";
import assert from "node:assert/strict";
import { BudgetExceeded } from "../src/budget-guard.mjs";
import { INVOCATION_CAPS, MINUTE_CAPS, countedFetch, countedStub, diagnosticRowAllowed, runInvocation, spend } from "../src/invocation.js";
import { wranglerConfig } from "./helpers.mjs";

const quietly = async (fn) => {
  const real = console.warn;
  console.warn = () => {};
  try {
    return await fn();
  } finally {
    console.warn = real;
  }
};

test("outside an invocation nothing is counted and a stub stays itself", () => {
  const stub = { rpc() {} };
  assert.equal(countedStub(stub), stub);
  assert.doesNotThrow(() => spend("doRpc"));
});

test("inside an invocation a stub's method calls spend, and the cap refuses before the call", async () => {
  await quietly(() => runInvocation(async () => {
    let calls = 0;
    const stub = countedStub({ rpc(value) { calls += 1; return value; }, name: "gate" });
    assert.equal(stub.name, "gate", "a plain property passes through");
    assert.equal(stub.then, undefined, "awaiting a stub stays safe");
    for (let i = 0; i < INVOCATION_CAPS.doRpc; i++) assert.equal(stub.rpc(i), i);
    assert.throws(() => stub.rpc(0), BudgetExceeded);
    assert.equal(calls, INVOCATION_CAPS.doRpc);
  }));
});

test("each invocation has its own budget", async () => {
  await quietly(async () => {
    const use = () => runInvocation(async () => { for (let i = 0; i < INVOCATION_CAPS.fetch; i++) spend("fetch"); });
    await use();
    await use();
  });
});

test("the fetch budget refuses the 51st fetch as a rejected promise, and sends nothing", async () => {
  await quietly(() => runInvocation(async () => {
    let sent = 0;
    const guarded = countedFetch(async () => { sent += 1; return new Response("ok"); });
    for (let i = 0; i < INVOCATION_CAPS.fetch; i++) await guarded("https://x.test/");
    await assert.rejects(() => guarded("https://x.test/"), BudgetExceeded);
    assert.equal(sent, INVOCATION_CAPS.fetch);
  }));
});

test("diagnostic rows are capped per isolate minute, across invocations", async () => {
  await quietly(async () => {
    // Three caps' worth inside at most two minute windows:  some must be refused.
    let refused = 0;
    for (let i = 0; i < MINUTE_CAPS.diagnosticRow * 3; i++) {
      await runInvocation(async () => { if (!diagnosticRowAllowed()) refused += 1; });
    }
    assert.ok(refused >= MINUTE_CAPS.diagnosticRow);
  });
});

test("wrangler.jsonc declares both guards with documented periods and account-unique namespaces", () => {
  const limits = Object.fromEntries(wranglerConfig().ratelimits.map((r) => [r.name, r]));
  assert.deepEqual(limits.MCP_RATE_LIMITER.simple, { limit: 300, period: 60 });
  assert.deepEqual(limits.AUTH_FAIL_RATE_LIMITER.simple, { limit: 60, period: 60 });
  const ids = Object.values(limits).map((r) => r.namespace_id);
  assert.equal(new Set(ids).size, ids.length);
  for (const r of Object.values(limits)) assert.ok([10, 60].includes(r.simple.period), r.name);
});
