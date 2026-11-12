import test from "node:test";
import assert from "node:assert/strict";
import {
  COMPACT_BODY_LIMIT,
  approximateTokens,
  compactLinearGraphql,
  effectiveLimit,
  splitFullArg,
  truncateText,
} from "../src/list-presentation.js";

test("splitFullArg defaults to compact", () => {
  const { args, full } = splitFullArg({ limit: 3 });
  assert.equal(full, false);
  assert.equal(args.limit, 3);
});

test("effectiveLimit honors explicit limit and full mode", () => {
  assert.equal(effectiveLimit({}, { schemaDefault: 20, full: false }), 10);
  assert.equal(effectiveLimit({ limit: 40 }, { schemaDefault: 20, full: false }), 40);
  assert.equal(effectiveLimit({}, { schemaDefault: 20, full: true }), 20);
});

test("truncateText shortens long strings", () => {
  const [text, cut] = truncateText("a".repeat(500), COMPACT_BODY_LIMIT);
  assert.ok(cut);
  assert.ok(text.endsWith("..."));
});

test("compactLinearGraphql reduces token estimate on fat nodes", () => {
  const fat = {
    comments: {
      nodes: Array.from({ length: 30 }, (_, i) => ({
        id: `c${i}`,
        body: "word ".repeat(200),
      })),
    },
  };
  const fullTokens = approximateTokens(fat);
  const { data, meta } = compactLinearGraphql(fat, 5);
  const compactTokens = approximateTokens(data);
  assert.ok(meta.truncated);
  assert.ok(compactTokens < fullTokens * 0.2, `expected big savings:  ${fullTokens} -> ${compactTokens}`);
});
