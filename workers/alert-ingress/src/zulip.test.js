// Regression tests for writeStatus:  Zulip's edit endpoint answers
// {result:"success"} with no message id, so the edit path must be judged on
// the result field.  Checking `patched.id` instead fell through and posted a
// duplicate message on every refresh.
//
// Run: node --test workers/alert-ingress/src/

import test from "node:test";
import assert from "node:assert/strict";
import { writeStatus } from "./zulip.js";

const ARGS = {
  email: "bot@example.com",
  apiKey: "k",
  channel: "alerts",
  topic: "svc",
  content: "status",
};

test("writeStatus keeps the standing message when the edit succeeds", async () => {
  const calls = [];
  const orig = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, method: opts.method });
    return { ok: true, json: async () => ({ result: "success", msg: "" }) };
  };
  try {
    const id = await writeStatus({ ...ARGS, statusId: 42 });
    assert.equal(id, 42);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "PATCH");
    assert.ok(calls[0].url.endsWith("/messages/42"));
  } finally {
    globalThis.fetch = orig;
  }
});

test("writeStatus never creates after a definitive rejected edit", async () => {
  const orig = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, opts) => {
    calls.push(opts.method);
    return { ok: false, status: 403 };
  };
  try {
    await assert.rejects(
      writeStatus({ ...ARGS, statusId: 42 }),
      (error) => error.outcome === "refused",
    );
    assert.deepEqual(calls, ["PATCH"]);
  } finally {
    globalThis.fetch = orig;
  }
});

test("writeStatus posts a fresh message when there is no standing id", async () => {
  const calls = [];
  const orig = globalThis.fetch;
  globalThis.fetch = async (url, opts) => {
    calls.push({ url, method: opts.method });
    return { ok: true, json: async () => ({ result: "success", id: 7 }) };
  };
  try {
    const id = await writeStatus({ ...ARGS, statusId: null });
    assert.equal(id, 7);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].method, "POST");
  } finally {
    globalThis.fetch = orig;
  }
});
