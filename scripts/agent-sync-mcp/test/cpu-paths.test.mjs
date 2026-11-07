// AFC-617:  the helpers that cut per-call Worker CPU must answer exactly what the code they replaced did.
// Each reference below is that earlier code, kept here verbatim so the parity cannot drift.
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { z } from "zod";
import { boundedWorkflowInput, isJsonValue, utf8Bytes, validWfV1Response } from "../src/gr-workflow-v1.js";
import { sha256Hex } from "../src/gb-bearer.js";

const encoderLength = (text) => new TextEncoder().encode(text).length;

test("utf8Bytes counts exactly what TextEncoder encodes, including lone surrogates and emoji", () => {
  const samples = [
    "", "a", "plain ascii text with spaces", "é", "ß", "€", "中文字符", "\u0000\u007f\u0080߿ࠀ￿",
    "😀", "a😀b", "\ud83d", "\ude00", "\ud83dx", "x\ude00", "\ud83d😀", "\ude00\ud83d", "😀😀\ud83d",
    "x".repeat(10_000), "é".repeat(5_000), "😀".repeat(2_500),
  ];
  for (const text of samples) assert.equal(utf8Bytes(text), encoderLength(text), JSON.stringify(text).slice(0, 40));
  // Every code unit alone, and each followed by a low surrogate.
  for (let unit = 0; unit <= 0xffff; unit += 7) {
    const one = String.fromCharCode(unit);
    assert.equal(utf8Bytes(one), encoderLength(one), `unit ${unit}`);
    const pair = one + "\udc00";
    assert.equal(utf8Bytes(pair), encoderLength(pair), `unit ${unit} + low`);
  }
});

// The recursive zod union that isJsonValue replaced.
const jsonValueUnion = z.lazy(() => z.union([z.string(), z.number().finite(), z.boolean(), z.null(), z.array(jsonValueUnion), z.record(z.string(), jsonValueUnion)]));

test("isJsonValue accepts and refuses exactly what the zod union did", () => {
  class Box { constructor() { this.a = 1; } }
  const sparse = [1, , 3]; // eslint-disable-line no-sparse-arrays
  const withGetter = Object.defineProperty({}, "x", { enumerable: true, get: () => 1 });
  const cases = [
    null, true, false, 0, -0, 1.5, 1e308, "", "text", [], {}, [1, "a", null, [true, { k: [] }]], { a: { b: { c: [1, 2, { d: null }] } } },
    Number.NaN, Infinity, -Infinity, undefined, () => 1, Symbol("s"), 10n, new Date(0), new Map(), new Set([1]), new Box(), Object.create(null),
    sparse, [undefined], { a: undefined }, { a: () => 1 }, { a: new Date(0) }, [new Box()], { a: [Infinity] }, withGetter,
    JSON.parse('{"__proto__": {"x": 1}, "a": [1e999]}'), JSON.parse('{"__proto__": {"x": 1}, "a": [1]}'), JSON.parse("[1e999]"), JSON.parse('{"a":"b"}'),
  ];
  for (const value of cases) {
    const want = jsonValueUnion.safeParse(value).success;
    assert.equal(isJsonValue(value), want, `${typeof value} ${String(Object.prototype.toString.call(value))} ${(() => { try { return JSON.stringify(value)?.slice(0, 50); } catch { return "?"; } })()}`);
  }
});

test("isJsonValue is bounded:  depth 12 and 50000 nodes by default, and the limits can be set", () => {
  const nested = (levels) => { let value = "leaf"; for (let i = 0; i < levels; i++) value = { k: value }; return value; };
  assert.equal(isJsonValue(nested(12)), true, "12 levels below the root is the limit");
  assert.equal(isJsonValue(nested(13)), false);
  const nestedArray = (levels) => { let value = 1; for (let i = 0; i < levels; i++) value = [value]; return value; };
  assert.equal(isJsonValue(nestedArray(12)), true);
  assert.equal(isJsonValue(nestedArray(13)), false);
  assert.equal(isJsonValue(Array.from({ length: 49_999 }, () => 1)), true, "the array itself is one node");
  assert.equal(isJsonValue(Array.from({ length: 50_000 }, () => 1)), false, "50001 nodes with the array");
  assert.equal(isJsonValue(nested(3), { maxDepth: 2 }), false);
  assert.equal(isJsonValue(nested(2), { maxDepth: 2 }), true);
  assert.equal(isJsonValue([1, 2, 3], { maxNodes: 3 }), false);
  assert.equal(isJsonValue([1, 2], { maxNodes: 3 }), true);
  // validWfV1Response bounds the whole body to depth 12 first, so isJsonValue's limits never cut off a body it accepts:
  // the run's field sits at depth 3, so 9 levels reach depth 12 and 10 levels pass it.
  assert.equal(validWfV1Response("wf_runs", { runs: [{ a: nested(9) }], complete: true }, { repo: "o/r" }), true);
  assert.equal(validWfV1Response("wf_runs", { runs: [{ a: nested(10) }], complete: true }, { repo: "o/r" }), false);
});

// The earlier boundedWorkflowInput, verbatim.
function referenceBounded(args, { maxDepth = 6, maxNodes = 500, maxBytes = 65536, maxString = 65536, maxKeys = 100 } = {}) {
  const stack = [[args, 0]], seen = new Set(); let nodes = 0, bytes = 0;
  while (stack.length) {
    const [value, depth] = stack.pop();
    if (++nodes > maxNodes || depth > maxDepth) return false;
    if (typeof value === "string") {
      if (value.length > maxString) return false;
      bytes += new TextEncoder().encode(value).length;
    } else if (value && typeof value === "object") {
      if (seen.has(value)) return false; seen.add(value);
      if (Object.getPrototypeOf(value) !== Object.prototype && !Array.isArray(value) && Object.getPrototypeOf(value) !== null) return false;
      const keys = Object.keys(value); if (keys.length > maxKeys) return false;
      for (const key of keys) {
        const d = Object.getOwnPropertyDescriptor(value, key);
        if (!d || !Object.hasOwn(d, "value")) return false;
        bytes += key.length; stack.push([d.value, depth + 1]);
      }
    } else if (!["number", "boolean"].includes(typeof value) && value !== null) return false;
    if (bytes > maxBytes) return false;
  }
  return true;
}

test("boundedWorkflowInput answers exactly what the earlier tuple-and-encoder version did", () => {
  const cyclic = { a: 1 }; cyclic.self = cyclic;
  const shared = { s: 1 };
  const deep = (n) => (n === 0 ? "leaf" : { k: deep(n - 1) });
  class Box { constructor() { this.a = 1; } }
  const withGetter = Object.defineProperty({}, "x", { enumerable: true, get: () => 1 });
  const wide = Object.fromEntries(Array.from({ length: 101 }, (_, i) => [`k${i}`, i]));
  const exactlyWide = Object.fromEntries(Array.from({ length: 100 }, (_, i) => [`k${i}`, i]));
  const many = Array.from({ length: 600 }, (_, i) => i);
  const cases = [
    [{}], [[]], [{ a: 1, b: "two", c: [true, null, { d: 3 }] }], [null], [undefined], [1], ["x"], [() => 1], [Symbol("s")], [10n], [new Date(0)], [new Box()], [withGetter], [cyclic],
    [{ a: shared, b: shared }], [deep(6)], [deep(7)], [deep(12), { maxDepth: 12 }], [deep(13), { maxDepth: 12 }], [wide], [exactlyWide], [many], [many, { maxNodes: 50000 }],
    ["x".repeat(65_536)], ["x".repeat(65_537)], [{ a: "x".repeat(40_000), b: "y".repeat(40_000) }], [{ a: "é".repeat(40_000) }], [{ a: "😀".repeat(20_000) }], [{ a: "😀".repeat(20_000) }, { maxBytes: 70_000, maxString: 70_000 }],
    [{ ["k".repeat(70_000)]: 1 }], [Object.create(null)], [{ a: [undefined] }], [{ a: Infinity, b: Number.NaN }],
    [{ a: "x".repeat(1000), b: ["y".repeat(1000)] }, { maxBytes: 1500, maxString: 1500 }], [{ a: "x".repeat(1000), b: ["y".repeat(1000)] }, { maxBytes: 2100, maxString: 1500 }],
    [{ a: "\ud83d" }], [{ a: "\ude00".repeat(30_000) }], [{ a: "\ude00".repeat(30_000) }, { maxBytes: 90_000, maxString: 90_000 }],
  ];
  for (const [value, options] of cases) {
    assert.equal(boundedWorkflowInput(value, options), referenceBounded(value, options), `${Object.prototype.toString.call(value)} ${JSON.stringify(options)}`);
  }
});

test("sha256Hex equals the platform SHA-256 hex for ASCII, empty and non-ASCII input", async () => {
  for (const text of ["", "a", "sbt_claude_" + "x".repeat(43), "gbb_fixer_" + "Z".repeat(43), "é中😀", "\u0000\u0001ÿ", "x".repeat(100_000)]) {
    assert.equal(await sha256Hex(text), createHash("sha256").update(text).digest("hex"));
  }
});

test("response validation of the fall-through actions is unchanged:  the same bodies pass and fail", () => {
  const runs = { runs: [{ id: 1, head_sha: "a".repeat(40), status: "completed", conclusion: "success", name: "ci", extra: { nested: [1, "x", null] } }], complete: true };
  assert.equal(validWfV1Response("wf_runs", runs, { repo: "o/r" }), true);
  assert.equal(validWfV1Response("wf_runs", { ...runs, unexpected: 1 }, { repo: "o/r" }), false, "strict:  an extra key is refused");
  assert.equal(validWfV1Response("wf_runs", { runs: runs.runs }, { repo: "o/r" }), false, "a missing key is refused");
  assert.equal(validWfV1Response("wf_runs", { ...runs, complete: undefined }, { repo: "o/r" }), false);
  assert.equal(validWfV1Response("wf_runs", { runs: [{ x: Infinity }], complete: true }, { repo: "o/r" }), false, "a non-finite number is not JSON");
  assert.equal(validWfV1Response("wf_runs", { runs: [{ x: new Date(0) }], complete: true }, { repo: "o/r" }), false, "a non-plain object is not JSON");
  assert.equal(validWfV1Response("wf_runs", { runs: "not-an-array", complete: true }, { repo: "o/r" }), false);
  // Repeated calls (the memoized schema) answer the same.
  for (let i = 0; i < 3; i++) assert.equal(validWfV1Response("wf_runs", runs, { repo: "o/r" }), true);
});
