// simple-git (scripts/git-tools/git) fails closed on any schema pattern outside its reviewed set (_KNOWN_PATTERN_DIGESTS):
// it refuses the whole tools/list answer ("advertised <tool> with a missing or unreadable schema"), so ONE advertised gr_
// tool with a pattern nobody added there stops every simple-git call, lane retire and lane prune included.  That happened
// when gr_wf_review_thread_comments shipped with a thread_id pattern that was not in the set.  This pins that every
// pattern a caller is actually listed, under simple-git's own tool-name rule, is a member, so the next tool fails here,
// in the pull request that adds it, instead of on the server.
//
// Listed means what tools/list gives a seat with the production flags:  the workflow verbs on and the typed gr_v1_pr_*
// tools off (GR_TYPED_TOOLS).  The typed tools carry one pattern simple-git has not reviewed (the repo pattern with
// unbounded repeats), so turning that flag on needs its digest added to the set first.

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

test("every pattern a listed gr_ tool carries is in simple-git's reviewed set", () => {
  const known = reviewedDigests();
  const tools = toolListFor("CLAUDE", PRODUCTION).list.filter((tool) => GIT_TOOL_NAME.test(tool.name));
  const advertised = tools.flatMap((tool) => patternsOf(tool.inputSchema, tool.name));
  // Not vacuous: the set was found, and there are many git tools carrying patterns.
  assert.ok(known.size >= 19, `read only ${known.size} reviewed digests from git-tools/git`);
  assert.ok(tools.length > 100, `only ${tools.length} gr_ tools matched simple-git's name rule`);
  assert.ok(advertised.length > 100, `only ${advertised.length} patterns found in their schemas`);
  assert.ok(tools.some((tool) => tool.name === "gr_wf_review_thread_comments"), "the tool that broke simple-git is not listed");
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
