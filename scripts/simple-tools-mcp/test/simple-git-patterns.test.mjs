// simple-git (scripts/git-tools/git) fails closed on any schema pattern outside its reviewed set (_KNOWN_PATTERN_DIGESTS):
// it refuses the whole tools/list answer ("advertised <tool> with a missing or unreadable schema"), so ONE advertised gr_
// tool with a pattern nobody added there stops every simple-git call, lane retire and lane prune included.  That happened
// when gr_wf_review_thread_comments shipped with a thread_id pattern that was not in the set.  This pins that every
// pattern a caller is actually listed, under simple-git's own tool-name rule, is a member, so the next tool fails here,
// in the pull request that adds it, instead of on the server.
//
// Listed means what tools/list gives a seat, and the check runs on both lists a seat can get:  the production one (the
// workflow verbs on, the typed gr_v1_pr_* tools off) and the one with GR_TYPED_TOOLS on.  The typed tools' repo pattern
// has unbounded repeats, so its digest was reviewed and added to the set (with the reasons beside it in git-tools/git)
// before that flag could be turned on without breaking every simple-git call.

import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { toolListFor } from "../src/delegated-identities.js";

const here = path.dirname(fileURLToPath(import.meta.url));
const SIMPLE_GIT = path.join(here, "..", "..", "git-tools", "git");
const GIT_TOOL_NAME = /^gr_[a-z0-9_]{1,80}$/; // TOOL_NAME_RE in git-tools/git
const PRODUCTION = { grWorkflowEnabled: true, grTypedEnabled: false };

/** The sha256 digests simple-git accepts, read from its source. */
function reviewedDigests() {
  const text = readFileSync(SIMPLE_GIT, "utf8");
  const start = text.indexOf("_KNOWN_PATTERN_DIGESTS = frozenset(");
  const end = text.indexOf("def _pattern_known", start);
  assert.ok(start > 0 && end > start, "the reviewed pattern set is not where this test expects it in git-tools/git");
  return new Set((text.slice(start, end).match(/"[0-9a-f]{64}"/g) ?? []).map((quoted) => quoted.slice(1, -1)));
}

/** Every `pattern` in a schema, with where it sits (properties, items and anyOf, as simple-git walks them). */
function patternsOf(schema, where, out = []) {
  if (!schema || typeof schema !== "object") return out;
  if (typeof schema.pattern === "string") out.push({ where, pattern: schema.pattern });
  for (const [key, child] of Object.entries(schema.properties ?? {})) patternsOf(child, `${where}.${key}`, out);
  if (schema.items) patternsOf(schema.items, `${where}[]`, out);
  for (const [index, variant] of (schema.anyOf ?? []).entries()) patternsOf(variant, `${where}|${index}`, out);
  return out;
}

const sha256 = (text) => createHash("sha256").update(text, "utf8").digest("hex");

const plain = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isNumber = (value) => typeof value === "number" && Number.isFinite(value);
const nonNegativeInteger = (value) => Number.isInteger(value) && value >= 0;

/**
 * What `_schema_constraints_ok` in git-tools/git requires of the structure around the patterns (everything but the digest
 * check), because any one failure makes simple-git refuse the whole tools/list answer, not only an unreviewed pattern:
 * an object at every node, an object `items` (the tuple form, an array, is refused), a list of objects for `anyOf`, string
 * `type` and `required` entries, numeric bounds, non-negative integer length and item bounds, a list `enum`, at most 200
 * properties, and no deeper than 8 levels.  A missing or null key counts as absent, as it does there.
 */
function shapeProblems(schema, where, depth = 0, out = []) {
  if (!plain(schema)) return out.push(`${where}: not an object`), out;
  if (depth > 8) return out.push(`${where}: nested deeper than 8 levels`), out;
  if (schema.type != null && typeof schema.type !== "string") out.push(`${where}: type is not a string`);
  if (schema.required != null && !(Array.isArray(schema.required) && schema.required.every((entry) => typeof entry === "string"))) {
    out.push(`${where}: required is not a list of strings`);
  }
  for (const key of ["minimum", "maximum"]) if (key in schema && !isNumber(schema[key])) out.push(`${where}: ${key} is not a number`);
  for (const key of ["minLength", "maxLength", "minItems", "maxItems"]) {
    if (key in schema && !nonNegativeInteger(schema[key])) out.push(`${where}: ${key} is not a non-negative integer`);
  }
  if (schema.pattern != null && typeof schema.pattern !== "string") out.push(`${where}: pattern is not a string`);
  if (schema.enum != null && !Array.isArray(schema.enum)) out.push(`${where}: enum is not a list`);
  if (schema.items != null) shapeProblems(schema.items, `${where}[]`, depth + 1, out);
  if (schema.properties != null) {
    if (!plain(schema.properties) || Object.keys(schema.properties).length > 200) {
      out.push(`${where}: properties is not a map of at most 200 entries`);
    } else {
      for (const [key, child] of Object.entries(schema.properties)) shapeProblems(child, `${where}.${key}`, depth + 1, out);
    }
  }
  if (schema.anyOf != null) {
    if (!Array.isArray(schema.anyOf)) out.push(`${where}: anyOf is not a list`);
    else schema.anyOf.forEach((variant, index) => shapeProblems(variant, `${where}|${index}`, depth + 1, out));
  }
  return out;
}

// Both tool lists a seat can be given:  the production one, and the one with the typed gr_v1_pr_* tools switched on.
const TOOL_LISTS = {
  "the production flags": PRODUCTION,
  "GR_TYPED_TOOLS on": { ...PRODUCTION, grTypedEnabled: true },
};

for (const [label, config] of Object.entries(TOOL_LISTS)) test(`every pattern a listed gr_ tool carries is in simple-git's reviewed set (${label})`, () => {
  const known = reviewedDigests();
  const tools = toolListFor("CLAUDE", config).list.filter((tool) => GIT_TOOL_NAME.test(tool.name));
  const advertised = tools.flatMap((tool) => patternsOf(tool.inputSchema, tool.name));
  // Not vacuous: the set was found, and there are many git tools carrying patterns.
  assert.ok(known.size >= 19, `read only ${known.size} reviewed digests from git-tools/git`);
  assert.ok(tools.length > 100, `only ${tools.length} gr_ tools matched simple-git's name rule`);
  assert.ok(advertised.length > 100, `only ${advertised.length} patterns found in their schemas`);
  assert.ok(tools.some((tool) => tool.name === "gr_wf_review_thread_comments"), "the tool that broke simple-git is not listed");
  // The flag decides whether the typed tools are listed at all, so each list really is the one it says it is.
  const typed = tools.filter((tool) => tool.name.startsWith("gr_v1_pr_")).map((tool) => tool.name).sort();
  assert.deepEqual(
    typed,
    config.grTypedEnabled ? ["gr_v1_pr_automerge_disable", "gr_v1_pr_hold_state", "gr_v1_pr_state"] : [],
    `the typed tools are not listed as ${label} says`,
  );
  // The rest of what simple-git needs of a schema (_schema_ok), since one refused schema also refuses the whole list.
  const problems = tools.flatMap((tool) => [
    ...(tool.inputSchema?.type != null && tool.inputSchema.type !== "object" ? [`${tool.name}: the root type is not object`] : []),
    ...shapeProblems(tool.inputSchema, tool.name),
  ]);
  assert.deepEqual(problems, [], "simple-git would refuse these schemas, and then every call");
  const missing = advertised
    .filter(({ pattern }) => !known.has(sha256(pattern)))
    .map(({ where, pattern }) => `${where}: ${pattern}  (sha256 ${sha256(pattern)})`);
  assert.deepEqual(
    [...new Set(missing)],
    [],
    "simple-git would refuse these patterns and then every call.  Add each digest to _KNOWN_PATTERN_DIGESTS in " +
      "scripts/git-tools/git (a reviewed change: confirm the pattern is linear and bounded), or change the schema.",
  );
});

test("the check finds patterns in nested schemas and tells a reviewed digest from an unreviewed one", () => {
  const known = reviewedDigests();
  assert.equal(known.has(sha256("^unreviewed-[a-z]{1,10}$")), false);
  const nested = patternsOf(
    { type: "object", properties: { id: { type: "string", pattern: "^x{1,9}$" }, rows: { type: "array", items: { type: "string", pattern: "^y$" } } } },
    "t",
  );
  assert.deepEqual(nested, [{ where: "t.id", pattern: "^x{1,9}$" }, { where: "t.rows[]", pattern: "^y$" }]);
});

test("the shape check catches what simple-git refuses besides an unreviewed pattern", () => {
  const ok = { type: "object", required: ["a"], properties: { a: { type: "string", maxLength: 9, pattern: "^a$" }, n: { type: "integer", minimum: 1, maximum: 5 } } };
  assert.deepEqual(shapeProblems(ok, "t"), []);
  const refused = {
    "tuple-form items": { type: "object", properties: { rows: { type: "array", items: [{ type: "string", pattern: "^y$" }] } } },
    "a non-object anyOf entry": { type: "object", properties: { v: { anyOf: ["string"] } } },
    "anyOf that is not a list": { type: "object", properties: { v: { anyOf: { type: "string" } } } },
    "required that is not strings": { type: "object", required: [1] },
    "a string bound": { type: "object", properties: { n: { type: "integer", maximum: "5" } } },
    "a negative length": { type: "object", properties: { s: { type: "string", maxLength: -1 } } },
    "an enum that is not a list": { type: "object", properties: { s: { type: "string", enum: "a" } } },
    "a properties value that is not a map": { type: "object", properties: ["a"] },
    "a non-object property": { type: "object", properties: { s: "string" } },
    "too deep": { type: "object", properties: { a: { properties: { b: { properties: { c: { properties: { d: { properties: { e: { properties: { f: { properties: { g: { properties: { h: { properties: { i: { type: "string" } } } } } } } } } } } } } } } } } } },
  };
  for (const [name, schema] of Object.entries(refused)) {
    assert.notDeepEqual(shapeProblems(schema, "t"), [], name);
  }
  assert.notDeepEqual(shapeProblems([], "t"), []);
});
