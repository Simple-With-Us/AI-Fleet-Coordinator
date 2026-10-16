// AFC-331 section 7:  the guide tool on the hosted server, and the bundled text.  No dependencies.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { guideModuleText, GUIDE_MD, GUIDE_MODULE } from "../scripts/export-guide.mjs";
import { GUIDE_TEXT } from "../src/guide-text.js";
import { guideResult, sectionNames, splitSections } from "../src/guide.js";
import { IdentityRouter } from "../src/delegated-identities.js";
import { okResult, ERROR_META_KEY } from "../src/contract.js";

test("src/guide-text.js is the guide markdown, bundled at build time (regenerate: node scripts/export-guide.mjs)", () => {
  assert.equal(readFileSync(GUIDE_MODULE, "utf8"), guideModuleText(), "src/guide-text.js is stale.  Run:  cd scripts/agent-sync-mcp && node scripts/export-guide.mjs");
  assert.equal(GUIDE_TEXT, readFileSync(GUIDE_MD, "utf8"));
});

test("the guide has the sections the design lists, each a ## heading", () => {
  const names = sectionNames(GUIDE_TEXT);
  for (const wanted of ["How to Pass Identity", "Which Connection a Client Gets", "Tools by Family", "Old Names and New Names", "The Notes Relay", "CLI Equivalents"]) {
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
  const one = guideResult(GUIDE_TEXT, { section: "the notes  RELAY" });
  assert.equal(one.structuredContent.section, "The Notes Relay");
  assert.ok(one.content[0].text.startsWith("## The Notes Relay\n"));
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
