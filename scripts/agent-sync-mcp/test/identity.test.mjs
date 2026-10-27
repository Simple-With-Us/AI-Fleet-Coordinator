// Per-call identity on the hosted server (AFC-331, docs/protocols/per-call-identity.md sections 2-4
// and the hosted rows of section 8).  Table-driven from GB_IDENTITIES, SHARED_IDENTITY_SEATS and
// KNOWN_SEATS:  no literal list of personas here.  The grant's own tools and every other identity's
// tools are stubs that record what reached them, so the tests see exactly which identity each call
// ran as and with which arguments.

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import {
  IdentityRouter, allowedIdentities, toolListFor, GB_IDENTITIES, RENAMED_TOOLS, SHARED_IDENTITY_SEATS, canonicalIdentity,
} from "../src/delegated-identities.js";
import { HOSTED_TOOL_LIST, HOSTED_READ_TOOLS, HOSTED_WRITE_TOOLS, UnknownTool } from "../src/hosted-tools.js";
import { EVENTS_ONLY_SEATS, KNOWN_SEATS, GATE_LOG_NAME } from "../src/config.js";
import { gate } from "../src/gate.js";
import { okResult, ERROR_META_KEY } from "../src/contract.js";
import { ROOT } from "./helpers.mjs";

const FIXTURES = readFileSync(path.join(ROOT, "../agent_sync/mcp/fixtures.jsonl"), "utf8")
  .split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("//")).map((l) => JSON.parse(l));

/** A stand-in for one identity's HostedTools:  records calls and audit rows. */
class StubTools {
  constructor(seat, { paused = false, scopes = ["zulip:read", "zulip:write"] } = {}) {
    this.seat = seat;
    this.paused = paused;
    this.scopes = new Set(scopes);
    this.calls = [];
    this.audits = [];
    this.grantRef = "abcdef012345";
    this.clientId = "client";
    this.zulip = {};
    this.linear = null;
    this.recall = null;
    this.gate = { auditCall: async (row) => { this.audits.push(row); } };
  }
  async call(name, args) {
    this.calls.push({ name, args });
    return okResult("{}", { seat: this.seat });
  }
  requireScope() {}
  async reserve() {}
  async safely(fn) { return fn(); }
  scrub(text) { return text; }
}

function router(seat) {
  const caller = new StubTools(seat);
  const built = new Map();
  const created = [];
  const r = new IdentityRouter({
    callerTools: caller,
    credentials: { valid: true, roles: new Map() },
    createIdentity: async (identity) => {
      created.push(identity);
      if (!built.has(identity)) built.set(identity, new StubTools(identity));
      return built.get(identity);
    },
  });
  return { r, caller, built, created };
}

function errorOf(result) {
  assert.equal(result.isError, true, JSON.stringify(result));
  const obj = JSON.parse(result.content[0].text);
  assert.deepEqual(result._meta[ERROR_META_KEY], obj);
  return obj;
}

const OTHER_SEATS = KNOWN_SEATS.filter((seat) => !SHARED_IDENTITY_SEATS.includes(seat));
const sorted = (list) => [...list].sort();

// ---------------------------------------------------------------- the sets (rows 1 to 4)

test("row 1:  a CURSOR grant may act as CURSOR, GROK-WEB and every enabled persona, on purpose", () => {
  assert.deepEqual(sorted(allowedIdentities("CURSOR")), sorted(["CURSOR", "GROK-WEB", ...GB_IDENTITIES]));
  assert.equal(allowedIdentities("CURSOR")[0], "CURSOR");
});

test("row 2:  a GROK-WEB grant has the same set, and its default is GROK-WEB, not CURSOR", async () => {
  assert.deepEqual(sorted(allowedIdentities("GROK-WEB")), sorted(allowedIdentities("CURSOR")));
  const { r, caller } = router("GROK-WEB");
  await r.call("zulip_topics", {});
  assert.deepEqual(caller.calls, [{ name: "zulip_topics", args: {} }]);
  const info = (await r.call("identities", {})).structuredContent;
  assert.equal(info.default_identity, "GROK-WEB");
  assert.equal(info.authenticated_seat, "GROK-WEB");
  assert.deepEqual(info.identities.map((row) => row.identity), [...allowedIdentities("GROK-WEB")]);
});

test("row 3:  every other grant acts as exactly its own seat;  a persona or a peer seat is refused", async () => {
  assert.ok(OTHER_SEATS.length > 0);
  for (const seat of OTHER_SEATS) {
    assert.deepEqual([...allowedIdentities(seat)], [seat], seat);
    const { r, caller, created } = router(seat);
    if (EVENTS_ONLY_SEATS.includes(seat)) {
      await assert.rejects(r.call("zulip_post", { topic: "t", text: "x", identity: seat.toLowerCase() }), UnknownTool);
      assert.deepEqual(created, [], "no other identity was ever built");
      assert.deepEqual(caller.calls, []);
      continue;
    }
    for (const claimed of [GB_IDENTITIES[0], ...SHARED_IDENTITY_SEATS]) {
      const error = errorOf(await r.call("zulip_post", { topic: "t", text: "x", identity: claimed }));
      assert.equal(error.code, "identity_not_allowed", `${seat} as ${claimed}`);
      assert.deepEqual(error.allowed, [seat]);
    }
    assert.deepEqual(created, [], "no other identity was ever built");
    assert.deepEqual(caller.calls, []);
    await r.call("zulip_post", { topic: "t", text: "x", identity: seat.toLowerCase() });
    assert.deepEqual(caller.calls, [{ name: "zulip_post", args: { topic: "t", text: "x" } }]);
  }
});

test("row 4:  a persona that GB_IDENTITIES leaves out is in no set", () => {
  assert.ok(GB_IDENTITIES.includes("GB-ENGINEER"));
  assert.ok(!GB_IDENTITIES.includes("GB-RETIRED"));
  for (const seat of KNOWN_SEATS) {
    const expected = SHARED_IDENTITY_SEATS.includes(seat)
      ? [seat, ...SHARED_IDENTITY_SEATS.filter((other) => other !== seat), ...GB_IDENTITIES]
      : [seat];
    assert.deepEqual([...allowedIdentities(seat)], expected, seat);
  }
});

// ---------------------------------------------------------------- the call rule (row 5)

test("row 5:  a write without identity is refused with identity_required;  a read without it uses the default", async () => {
  for (const seat of KNOWN_SEATS) {
    const { r, caller } = router(seat);
    if (EVENTS_ONLY_SEATS.includes(seat)) {
      for (const name of [...HOSTED_WRITE_TOOLS, "whoami"]) {
        await assert.rejects(r.call(name, {}), UnknownTool, `${seat} ${name}`);
      }
      assert.deepEqual(caller.calls, []);
      continue;
    }
    for (const name of HOSTED_WRITE_TOOLS) {
      const error = errorOf(await r.call(name, {}));
      assert.equal(error.code, "identity_required", `${seat} ${name}`);
    }
    assert.deepEqual(caller.calls, []);
    await r.call("whoami", {});
    await r.call("zulip_inbox");
    assert.deepEqual(caller.calls.map((c) => c.name), ["whoami", "zulip_inbox"]);
  }
});

test("a named identity runs on that identity's own tools, with identity removed from the arguments", async () => {
  const { r, caller, built, created } = router("CURSOR");
  for (const identity of allowedIdentities("CURSOR").slice(1)) {
    await r.call("zulip_post", { topic: "t", text: "x", identity });
    assert.deepEqual(built.get(identity).calls, [{ name: "zulip_post", args: { topic: "t", text: "x" } }], identity);
  }
  await r.call("zulip_post", { topic: "t", text: "again", identity: "grok_web" });  // case and _ are forgiven
  assert.equal(built.get("GROK-WEB").calls.length, 2);
  assert.deepEqual(created, allowedIdentities("CURSOR").slice(1), "each identity is built once per request");
  assert.deepEqual(caller.calls, []);
});

test("an identity that is not a name, or is outside the set, is refused without echoing it", async () => {
  const { r } = router("CURSOR");
  for (const claimed of ["SENTRY", "JAY", "GB-RETIRED", "", 7, null, "x".repeat(80), "GB FIXER"]) {
    const error = errorOf(await r.call("zulip_topics", { identity: claimed }));
    assert.equal(error.code, "identity_not_allowed", String(claimed));
    if (typeof claimed === "string" && claimed.length > 3) assert.ok(!error.message.includes(claimed), "the claim is not echoed");
  }
  assert.equal(canonicalIdentity(" gb_fixer "), "GB-FIXER");
});

test("a published alias names its seat:  INSTINCT on a WISE grant and MUSE-ASSIST on an MA grant", async () => {
  for (const [seat, alias] of [["WISE", "INSTINCT"], ["MA", "MUSE-ASSIST"], ["MA", "rob"]]) {
    const { r, caller, created } = router(seat);
    await r.call("zulip_post", { topic: "t", text: "x", identity: alias });
    assert.deepEqual(caller.calls, [{ name: "zulip_post", args: { topic: "t", text: "x" } }], `${alias} on ${seat}`);
    assert.deepEqual(created, [], "it is the grant's own seat:  nothing else was built");
  }
  const { r } = router("WISE");
  assert.equal(errorOf(await r.call("zulip_post", { topic: "t", text: "x", identity: "MUSE-ASSIST" })).code, "identity_not_allowed");
});

test("the grant's scope and pause govern a call made as another identity", async () => {
  const { r, caller, created } = router("GROK-WEB");
  caller.paused = true;
  assert.equal(errorOf(await r.call("zulip_post", { topic: "t", text: "x", identity: "CURSOR" })).code, "paused");
  assert.deepEqual(created, []);
});

// ---------------------------------------------------------------- the advertised schema (row 6)

test("row 6:  each grant's tools/list offers exactly its own set, required on writes, and identities after whoami", () => {
  for (const seat of KNOWN_SEATS) {
    const { list } = toolListFor(seat, { whatsappEnabled: true, grTypedEnabled: true, grWorkflowEnabled: true });
    if (EVENTS_ONLY_SEATS.includes(seat)) {
      assert.deepEqual(list, [], `${seat} is wake-only:  it advertises no tools`);
      continue;
    }
    assert.deepEqual(list.slice(0, 3).map((t) => t.name), ["whoami", "identities", "guide"]);
    // The whatsapp tools ride with the full Jet login (JET-CHAT);  every other seat
    // has them withheld, so only JET-CHAT advertises the complete catalog.
    assert.equal(list.length, HOSTED_TOOL_LIST.length + 2 - (seat === "JET-CHAT" ? 0 : 1));
    for (const tool of list) {
      const properties = tool.inputSchema.properties ?? {};
      if (tool.name === "identities") {
        assert.ok(!("identity" in properties));
        continue;
      }
      assert.deepEqual(properties.identity.enum, [...allowedIdentities(seat)], `${seat} ${tool.name}`);
      assert.equal((tool.inputSchema.required ?? []).includes("identity"), HOSTED_WRITE_TOOLS.has(tool.name), `${seat} ${tool.name}`);
    }
  }
});

test("every hosted tool is classified as exactly one of read or write", () => {
  // JET is wake-only;  JET-CHAT serves the full set (including the whatsapp
  // tools on the full Jet login) and the classification covers the whole catalog.
  const names = toolListFor("JET-CHAT", { whatsappEnabled: true }).list.map((t) => t.name);
  for (const name of names) assert.notEqual(HOSTED_READ_TOOLS.has(name), HOSTED_WRITE_TOOLS.has(name), name);
  assert.deepEqual(sorted([...HOSTED_READ_TOOLS, ...HOSTED_WRITE_TOOLS]), sorted([...HOSTED_TOOL_LIST.map((t) => t.name), "identities", "guide"]));
});

// ---------------------------------------------------------------- audit (row 7)

test("row 7:  refusal rows carry the grant's own seat and the claimed identity, never a hard-coded CURSOR", async () => {
  for (const seat of KNOWN_SEATS) {
    const { r, caller } = router(seat);
    if (EVENTS_ONLY_SEATS.includes(seat)) {
      await assert.rejects(r.call("zulip_post", { topic: "t", text: "x" }), UnknownTool);
      assert.deepEqual(caller.audits.map((row) => [row.seat, row.authenticated_seat, row.error_code]), [[seat, seat, "unknown_tool"]]);
      continue;
    }
    await r.call("zulip_post", { topic: "t", text: "x" });
    await r.call("zulip_post", { topic: "t", text: "x", identity: "SENTRY" });
    await r.call("post", {});
    for (const row of caller.audits) {
      assert.equal(row.authenticated_seat, seat);
      assert.equal(row.seat, seat);
    }
    assert.deepEqual(caller.audits.map((row) => [row.acting_identity, row.error_code]), [
      ["none", "identity_required"], ["rejected", "identity_not_allowed"], ["none", "unknown_tool"],
    ]);
  }
});

// ---------------------------------------------------------------- old names

test("a removed name is refused with unknown_tool naming its replacement;  an unknown name stays a protocol error", async () => {
  const { r, caller } = router("JET-CHAT");
  const names = new Set(toolListFor("JET-CHAT", { whatsappEnabled: true }).list.map((t) => t.name));
  // The replacement must name a real tool.  The hosted server serves only the read-only
  // eavesdropper_* tools, so eavesdropper_ack (stdio-only, like eavesdropper_ack before the rename)
  // is a real name that this transport never served;  the old name is still refused here.
  const contract = new Set(
    JSON.parse(readFileSync(path.join(ROOT, "../agent_sync/mcp/eavesdropper-tools.json"), "utf8"))
      .tools.map((t) => t.name),
  );
  for (const [old, replacement] of Object.entries(RENAMED_TOOLS)) {
    assert.ok(!names.has(old), old);
    assert.ok(names.has(replacement) || contract.has(replacement), replacement);
    const error = errorOf(await r.call(old, { topic: "t" }));
    assert.equal(error.code, "unknown_tool");
    assert.equal(error.replacement, replacement);
  }
  assert.deepEqual(caller.calls, []);
  await r.call("no_such_tool", {});  // handed to the grant's tools, which throw UnknownTool for the protocol error
  assert.deepEqual(caller.calls, [{ name: "no_such_tool", args: {} }]);
});

test("shared fixtures:  renamed and identity cases", async () => {
  const cases = FIXTURES.filter((c) => ["renamed", "identity"].includes(c.kind));
  assert.ok(cases.length >= 4);
  for (const c of cases) {
    const { r } = router("JET-CHAT");
    const result = await r.call(c.tool, c.arguments ?? {});
    if (c.expect === "ok") {
      assert.ok(!result.isError, c.case);
      continue;
    }
    const error = errorOf(result);
    assert.equal(error.code, c.expect, c.case);
    if (c.replacement) assert.equal(error.replacement, c.replacement, c.case);
  }
});

test("the instructions state the rule, the set and the families", () => {
  const { r } = router("GROK-WEB");
  const text = r.instructions();
  assert.match(text, /writes must name it;  reads without it act as GROK-WEB/);
  for (const identity of allowedIdentities("GROK-WEB")) assert.ok(text.includes(identity), identity);
  assert.match(text, /zulip_\*, linear_\*, recall_\*, notes_\*, fleetlink_\* and listener_\*/);
  assert.match(text, /Call guide for the full tools guide/);
  assert.match(text, /fleetlink\.online\/simple-tools-guide\//);
});

// ---------------------------------------------------------------- the per-identity gate

test("gate() opens a gate for every seat, every enabled persona and the refusal log, and nothing else", () => {
  const opened = [];
  const env = { SEAT_GATE: { idFromName: (name) => name, get: (id) => { opened.push(id); return { id }; } } };
  for (const name of [...KNOWN_SEATS, ...GB_IDENTITIES, GATE_LOG_NAME]) assert.deepEqual(gate(env, name), { id: name }, name);
  assert.deepEqual(opened, [...KNOWN_SEATS, ...GB_IDENTITIES, GATE_LOG_NAME]);
  for (const name of ["SENTRY", "JAY", "GB-RETIRED", "gb-fixer", "GB-", "", "__proto__", undefined, null, 5]) {
    assert.throws(() => gate(env, name), /not a seat/, String(name));
  }
  assert.ok(!GB_IDENTITIES.includes("GB-RETIRED"), "a persona that is not enabled never gets a gate");
  assert.equal(opened.length, KNOWN_SEATS.length + GB_IDENTITIES.length + 1, "the refused names opened nothing");
});
