// AFC-331 section 7:  the guide tool on the hosted server, and the bundled text.  No dependencies.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { guideModuleText, GUIDE_MD, GUIDE_MODULE } from "../scripts/export-guide.mjs";
import RENAMED_CONTRACT from "../../agent_sync/mcp/renamed-tools.json" with { type: "json" };
import { GUIDE_TEXT } from "../src/guide-text.js";
import { guideResult, sectionNames, splitSections } from "../src/guide.js";
import { IdentityRouter } from "../src/delegated-identities.js";
import { okResult, ERROR_META_KEY } from "../src/contract.js";

test("src/guide-text.js is the guide markdown, bundled at build time (regenerate: node scripts/export-guide.mjs)", () => {
  assert.equal(readFileSync(GUIDE_MODULE, "utf8"), guideModuleText(), "src/guide-text.js is stale.  Run:  cd scripts/agent-sync-mcp && node scripts/export-guide.mjs");
  assert.equal(GUIDE_TEXT, readFileSync(GUIDE_MD, "utf8"));
});

// The Old Names table is the client-visible half of the rename registry (AFC-331 section 4, and the
// listener_* -> eavesdropper_* cutover, owner 2026-10-22).  The generator only bundles the markdown,
// so nothing else keeps the two in step:  every renamed-tools.json key must appear as a row, or the
// guide tells an agent a name the router refuses with unknown_tool.
test("every renamed-tools.json entry has a row in the guide's Old Names table", () => {
  const renamed = RENAMED_CONTRACT.renamed;
  const section = GUIDE_TEXT.split(/^## Old Names and New Names$/m)[1]?.split(/^## /m)[0] ?? "";
  const rows = new Set(
    [...section.matchAll(/^\|\s*`([^`]+)`\s*\|\s*`([^`]+)`\s*\|$/gm)].map((m) => m[1]),
  );
  assert.ok(rows.size > 0, "the Old Names table has rows to check");
  const missing = Object.keys(renamed).filter((old) => !rows.has(old));
  assert.deepEqual(missing, [], "renamed-tools.json names with no guide row");
});

test("the guide has the sections the design lists, each a ## heading", () => {
  const names = sectionNames(GUIDE_TEXT);
  for (const wanted of ["How to Pass Identity", "Which Connection a Client Gets", "Tools by Family", "Old Names and New Names", "Apple Notes", "CLI Equivalents"]) {
    assert.ok(names.includes(wanted), wanted);
  }
});

test("a fenced ## line is not a section", () => {
  const text = "# T\n\n## One\n\n```\n## not a heading\n```\n\n## Two\nbody\n";
  assert.deepEqual(splitSections(text).map(([n]) => n), ["One", "Two"]);
});

test("guideResult returns the whole guide, one section (matched loosely), or lists the sections on a miss", () => {
  const whole = guideResult(GUIDE_TEXT, {});
  assert.equal(whole.content[0].text, GUIDE_TEXT);
  assert.equal(whole.structuredContent.section, null);
  const one = guideResult(GUIDE_TEXT, { section: "apple  NOTES" });
  assert.equal(one.structuredContent.section, "Apple Notes");
  assert.ok(one.content[0].text.startsWith("## Apple Notes\n"));
  assert.ok(!one.content[0].text.includes("## CLI Equivalents"));
  assert.throws(() => guideResult(GUIDE_TEXT, { section: "nope" }), (error) => error.code === "not_found" && /How to Pass Identity/.test(error.message) && error.extra.sections.length === sectionNames(GUIDE_TEXT).length);
});

class Stub {
  constructor() {
    this.seat = "CURSOR";
    this.paused = false;
    this.grantRef = "g";
    this.clientId = "c";
    this.audits = [];
    this.gate = { auditCall: async (row) => { this.audits.push(row); } };
  }
  async call() { return okResult("{}", {}); }
  requireScope() {}
  async safely(fn) { return fn(); }
}

test("the router answers guide with no identity and ignores one that is supplied", async () => {
  const router = new IdentityRouter({ callerTools: new Stub() });
  const names = router.toolList.map((t) => t.name);
  assert.deepEqual(names.slice(0, 3), ["whoami", "identities", "guide"]);
  const whole = await router.call("guide", undefined);
  assert.equal(whole.content[0].text, GUIDE_TEXT);
  const named = await router.call("guide", { identity: "GB-FIXER", section: "CLI Equivalents" });
  assert.equal(named.structuredContent.section, "CLI Equivalents");
  const miss = await router.call("guide", { section: "nope" });
  assert.equal(miss.isError, true);
  assert.equal(JSON.parse(miss.content[0].text).code, "not_found");
  assert.deepEqual(miss._meta[ERROR_META_KEY], JSON.parse(miss.content[0].text));
  const bad = await router.call("guide", { section: 5 });
  assert.equal(JSON.parse(bad.content[0].text).code, "invalid_argument");
  const guideSpec = router.toolList.find((t) => t.name === "guide");
  assert.ok(!(guideSpec.inputSchema.required ?? []).includes("identity"));
});

test("guide arguments are a strict zod object:  an unknown key or a non-string section is invalid_argument", async () => {
  assert.throws(() => guideResult(GUIDE_TEXT, { section: 5 }), (error) => error.code === "invalid_argument" && /argument 'section' must be of type string/.test(error.message));
  assert.throws(() => guideResult(GUIDE_TEXT, { section: "x", extra: 1 }), (error) => error.code === "invalid_argument" && /unknown argument 'extra'/.test(error.message));
  assert.throws(() => guideResult(GUIDE_TEXT, { section: "x".repeat(101) }), (error) => error.code === "invalid_argument");
  const router = new IdentityRouter({ callerTools: new Stub() });
  for (const args of [{ extra: 1 }, { identity: "CURSOR", extra: 1 }, [], "text"]) {
    const result = await router.call("guide", args);
    assert.equal(JSON.parse(result.content[0].text).code, "invalid_argument", JSON.stringify(args));
  }
});

// Public help must not teach retired product branding.
test("guide opening uses current component names without a compatibility launcher", () => {
  const opening = GUIDE_TEXT.split("## How to Pass Identity")[0];
  assert.ok(opening.includes("**simple-tools**"));
  assert.ok(opening.includes("**ST-MCP**"));
  assert.ok(!opening.includes("agent-sync"));
  assert.ok(!opening.includes("compatibility launcher"));
});

test("read result formatters do not emit retired product prefixes", () => {
  for (const name of ["contract.js", "hosted-tools.js", "linear-api.js", "linear-mcp.js", "notes.js"]) {
    const source = readFileSync(new URL(`../src/${name}`, import.meta.url), "utf8");
    assert.ok(!source.includes("`agent-sync ${"), name);
  }
  for (const name of ["tools.py", "fleetlink_tools.py", "recall_tools.py", "linear_tools.py"]) {
    const source = readFileSync(new URL(`../../agent_sync/mcp/${name}`, import.meta.url), "utf8");
    assert.ok(!/"agent-sync (?:%s|recall_search)/.test(source), name);
  }
});
