// The per-invocation budget (src/invocation.js) and the guard bindings it stands
// beside.  The workerd half is test/mcp-guards.workerd.mjs.

import test from "node:test";
import assert from "node:assert/strict";
import { BudgetExceeded } from "../src/budget-guard.mjs";
import { INVOCATION_CAPS, MINUTE_CAPS, countedFetch, countedStub, diagnosticRowAllowed, logLimited, runInvocation, spend, withinLimit } from "../src/invocation.js";
import { BEARER_SEATS } from "../src/bearer-identities.js";
import { GB_IDENTITIES } from "../src/delegated-identities.js";
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

test("a missing or failing limiter allows the request, and the log names which guard is not running", async () => {
  const real = console.log;
  const lines = [];
  console.log = (line) => lines.push(JSON.parse(line));
  try {
    assert.equal(await withinLimit({}, "MCP_RATE_LIMITER", "k"), true);
    assert.equal(await withinLimit({}, "AUTH_FAIL_RATE_LIMITER", "k"), true);
    // A second line for the same binding in one minute is suppressed;  another binding's is not.
    const minute = Math.floor(Date.now() / 60_000);
    assert.equal(await withinLimit({}, "MCP_RATE_LIMITER", "k"), true);
    if (Math.floor(Date.now() / 60_000) === minute) assert.equal(lines.length, 2);
    assert.deepEqual(lines.slice(0, 2), [
      { event: "rate_limiter_missing", binding: "MCP_RATE_LIMITER" },
      { event: "rate_limiter_missing", binding: "AUTH_FAIL_RATE_LIMITER" },
    ]);
    lines.length = 0;
    const failing = { limit: async () => { throw new Error("down"); } };
    assert.equal(await withinLimit({ CUSTOM_LIMITER: failing }, "CUSTOM_LIMITER", "k"), true);
    assert.deepEqual(lines, [{ event: "rate_limiter_unavailable", binding: "CUSTOM_LIMITER" }]);
    // A working limiter's refusal is its own answer, and an event with no binding logs only the event.
    const over = { limit: async () => ({ success: false }) };
    assert.equal(await withinLimit({ OVER_LIMITER: over }, "OVER_LIMITER", "k"), false);
    lines.length = 0;
    logLimited("guard_test_event");
    assert.deepEqual(lines, [{ event: "guard_test_event" }]);
  } finally {
    console.log = real;
  }
});

test("wrangler.jsonc declares both guards with documented periods and account-unique namespaces", () => {
  const limits = Object.fromEntries(wranglerConfig().ratelimits.map((r) => [r.name, r]));
  assert.deepEqual(limits.MCP_RATE_LIMITER.simple, { limit: 300, period: 60 });
  assert.deepEqual(limits.AUTH_FAIL_RATE_LIMITER.simple, { limit: 120, period: 60 });
  const ids = Object.values(limits).map((r) => r.namespace_id);
  assert.equal(new Set(ids).size, ids.length);
  for (const r of Object.values(limits)) assert.ok([10, 60].includes(r.simple.period), r.name);
});

// Every identity the code can name, each with one credential.
const IDENTITIES = BEARER_SEATS.length + GB_IDENTITIES.length;

test("the failed-auth limit leaves room for every identity failing in two isolates at once", () => {
  // One credential counts once a minute per isolate, and the limiter is per location.
  // A new seat that breaks this needs the limit looked at again, not this test relaxed.
  const limit = wranglerConfig().ratelimits.find((r) => r.name === "AUTH_FAIL_RATE_LIMITER").simple.limit;
  assert.ok(limit >= IDENTITIES * 2, `${limit} is under ${IDENTITIES} identities in two isolates`);
});

test("the gate budget holds GET /admin and fires before the platform's subrequest limit", () => {
  const config = wranglerConfig();
  // src/index.js, /admin:  5 reads per hosted seat, 3 global log reads, 2 per bearer row.
  const hostedSeats = config.vars.HOSTED_SEATS.split(",").length;
  const adminRpcs = hostedSeats * 5 + 3 + IDENTITIES * 2;
  assert.ok(adminRpcs <= INVOCATION_CAPS.doRpc, `/admin makes ${adminRpcs} gate calls, over the ${INVOCATION_CAPS.doRpc} budget`);
  // Whether a gate call counts as a subrequest is not verified, so assume that it does.
  assert.ok(INVOCATION_CAPS.doRpc + INVOCATION_CAPS.fetch <= config.limits.subrequests);
});
