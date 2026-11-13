import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { composeBody, MAX_CONTENT_LENGTH, normalizeHeader, tagPrefix, UsageError } from "../src/contract.js";

const fixtures = JSON.parse(readFileSync(new URL("../../simple_tools/tests/header-spacing.json", import.meta.url), "utf8"));

test("OPS-1086 golden headers survive JSON and UTF-8 without changing body gaps", () => {
  for (const entry of fixtures) {
    const options = entry.seat ? { seat: entry.seat } : {};
    const actual = normalizeHeader(entry.input, options);
    assert.equal(actual, entry.expected, entry.name);
    assert.equal(normalizeHeader(actual, options), actual, entry.name);
    const wire = Buffer.from(JSON.stringify({ content: actual }), "utf8");
    assert.equal(JSON.parse(wire.toString("utf8")).content, entry.expected, entry.name);
  }
});

test("hosted post serializes 3/2/1 hair spaces and distinct repo/recipient commas", () => {
  const actual = composeBody(tagPrefix("CODEX", "deadbeef"),
    "[CODEX: EE.1.S 📁AFC, OPS → CODEX EE] Body.\u00a0 Next.", { labels: ["AG"] });
  assert.equal(actual, fixtures[0].expected);
  assert.equal(/:(\u200a+)EE/u.exec(actual)[1], "\u200a".repeat(3));
  assert.equal(/S(\u200a+)📁/u.exec(actual)[1], "\u200a".repeat(2));
  assert.equal(/📁(\u200a+)AFC/u.exec(actual)[1], "\u200a");
  assert.equal(/AFC,(\u200a+)OPS/u.exec(actual)[1], "\u200a");
  assert.equal(/CODEX EE,(\u200a+)AG/u.exec(actual)[1], "\u200a".repeat(2));
  assert.ok(!actual.includes("deadbeef"));
  assert.ok(actual.includes(" → CODEX EE,"));
  assert.ok(actual.endsWith("Body.\u00a0 Next."));
  assert.equal(composeBody(tagPrefix("CODEX", "deadbeef"),
    "[CODEX: EE.1.S 📁AFC, OPS -> CODEX EE] Body.\u00a0 Next.", { labels: ["AG"] }), fixtures[0].expected);
});

test("the hosted content cap applies after header serialization", () => {
  const prefix = "[CODEX: EE.1.S 📁AFC] ";
  const input = prefix + "x".repeat(MAX_CONTENT_LENGTH - Array.from(prefix).length);
  assert.throws(() => composeBody(tagPrefix("CODEX", null), input), UsageError);
});
