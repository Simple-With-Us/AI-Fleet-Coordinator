// Unknown keys in a Sentry body.  The shape pick drops unknown keys from the result, but the
// node and depth budgets bind the WHOLE input:  safeParse walks every value first.  These tests
// pin that unknown subtrees are charged and refused, dropped from results, and that the
// declared-shape budgets still bind.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import assert from "node:assert/strict";
import { parseSentryPayload, safeParse, SchemaError, __testing } from "./sentry-schema.js";

const { MAX_DEPTH, MAX_NODES, parseValue, object, optional, text } = __testing;

const issueBody = (extra = {}) => ({
  action: "created",
  data: { issue: { id: "1", title: "Boom", ...extra.issue }, ...extra.data },
  ...extra.top,
});

test("unknown keys at every level are dropped from the parsed result", () => {
  const parsed = parseSentryPayload(
    issueBody({
      top: { actor: { type: "user" }, installation: { uuid: "x" } },
      data: { extra_group: [1, 2, 3] },
      issue: { tags: [{ key: "a", value: "b" }], project: { slug: "p", internal_id: 7 } },
    }),
  );
  assert.deepEqual(Object.keys(parsed).sort(), ["action", "data"]);
  assert.deepEqual(Object.keys(parsed.data), ["issue"]);
  assert.deepEqual(Object.keys(parsed.data.issue).sort(), ["id", "project", "title"]);
  assert.deepEqual(parsed.data.issue.project, { slug: "p" });
});

test("an unknown subtree is counted against the whole-body budget, and past it the body is refused", () => {
  // A wide unknown object: far more nodes than the budget, all under unknown keys.
  const wide = {};
  for (let i = 0; i < 100_000; i += 1) wide["k" + i] = i;
  const start = performance.now();
  assert.throws(
    () => parseSentryPayload(issueBody({ top: { wide } })),
    (error) => error instanceof SchemaError && error.message === "payload too large",
  );
  assert.ok(performance.now() - start < 1000, "the walk stops at the budget");
  // The same count inside the issue, under an unknown key.
  assert.throws(() => parseSentryPayload(issueBody({ issue: { wide } })), SchemaError);
  // Exactly at the budget passes, one over fails.
  const fits = (n) => {
    const extra = {};
    // issueBody itself is: root, action, data, issue, id, title = 6 nodes
    for (let i = 0; i < n; i += 1) extra["u" + i] = 1;
    return () => parseSentryPayload(issueBody({ top: extra }));
  };
  assert.doesNotThrow(fits(MAX_NODES - 6));
  assert.throws(fits(MAX_NODES - 5), (error) => error instanceof SchemaError && error.message === "payload too large");
});

test("a deep unknown subtree is refused past the depth bound, with no stack overflow", () => {
  let deep = {};
  for (let i = 0; i < 50_000; i += 1) deep = { next: deep };
  assert.throws(
    () => parseSentryPayload(issueBody({ top: { deep } })),
    (error) => error instanceof SchemaError && error.message === "payload nested too deep",
  );
  let ok = { v: 1 };
  for (let i = 0; i < MAX_DEPTH - 2; i += 1) ok = { n: ok }; // top.u (depth 1) + chain stays within MAX_DEPTH
  assert.doesNotThrow(() => parseSentryPayload(issueBody({ top: { u: ok } })));
  let arr = [];
  for (let i = 0; i < 50_000; i += 1) arr = [arr];
  assert.throws(() => parseSentryPayload(issueBody({ issue: { u: arr } })), SchemaError);
});

test("the entry point's own validator refuses the same unknown subtrees", async () => {
  const { __testing: entry } = await import("./index.js");
  const valid = (extra) => issueBody({ ...extra, issue: { level: "error", project: { slug: "p" }, ...extra.issue } });
  assert.doesNotThrow(() => entry.validateSentryPayload(valid({})), "the base body is valid, so a refusal below is the budget");
  const wide = {};
  for (let i = 0; i < 3000; i += 1) wide["k" + i] = i;
  assert.throws(() => entry.validateSentryPayload(valid({ top: { wide } })));
  let deep = { v: 1 };
  for (let i = 0; i < MAX_DEPTH + 2; i += 1) deep = { n: deep };
  assert.throws(() => entry.validateSentryPayload(valid({ top: { deep } })));
  assert.doesNotThrow(() => entry.validateSentryPayload(valid({ top: { actor: { type: "user" } } })));
});

test("a credential-shaped value under an unknown key is dropped, so it is never persisted or relayed", () => {
  const sample = "lin_" + "api_" + "Ab12".repeat(8); // built at run time and low-entropy:  gitleaks runs on this repo
  const parsed = parseSentryPayload(issueBody({ issue: { tags: [{ value: sample }], note: sample } }));
  assert.equal(JSON.stringify(parsed).includes(sample), false);
});

test("a credential-shaped value in a declared field is still refused, naming the field only", () => {
  const sample = "lin_" + "api_" + "Ab12".repeat(8);
  assert.throws(
    () => parseSentryPayload(issueBody({ issue: { title: sample } })),
    (error) => {
      assert.ok(error instanceof SchemaError);
      assert.equal(error.code, "secret");
      assert.equal(error.field, "data.issue.title");
      assert.equal(String(error.message).includes(sample), false);
      return true;
    },
  );
});

test("parseValue's node budget counts every declared value visited, and stops the walk past it", () => {
  const shape = object({ a: optional(text()), b: optional(text()), c: optional(text()) });
  const within = { nodes: 0, depth: 0, maxNodes: 4, maxDepth: MAX_DEPTH };
  assert.deepEqual(parseValue(shape, { a: "1", b: "2", c: "3" }, "", within), { a: "1", b: "2", c: "3" });
  assert.equal(within.nodes, 4); // the object and its three declared fields
  const over = { nodes: 0, depth: 0, maxNodes: 3, maxDepth: MAX_DEPTH };
  assert.throws(
    () => parseValue(shape, { a: "1", b: "2", c: "3" }, "", over),
    (error) => error instanceof SchemaError && error.message === "payload too large",
  );
  // parseValue alone charges declared values;  unknown keys are charged by the
  // whole-body walk that safeParse runs first (see the tests above).
});

test("the depth bound still binds declared nesting", () => {
  let shape = object({ leaf: optional(text()) });
  let value = { leaf: "x" };
  for (let i = 0; i < MAX_DEPTH + 1; i += 1) {
    shape = object({ inner: optional(shape) });
    value = { inner: value };
  }
  const result = safeParse(shape, value);
  assert.equal(result.success, false);
  assert.equal(result.error.message, "payload nested too deep");
  assert.ok(MAX_NODES >= 100, "the node budget stays generous for a real alert");
});
