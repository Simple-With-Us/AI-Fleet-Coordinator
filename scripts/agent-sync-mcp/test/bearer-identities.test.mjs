import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";
import { z } from "zod";
import { BEARER_ONLY_SEATS, BF_BEARER_SEATS, ADDITIONAL_BEARER_SEATS, BEARER_SEATS, resolveBearerSeatCredentials } from "../src/bearer-identities.js";
import { gate } from "../src/gate.js";
import { KNOWN_SEATS } from "../src/config.js";
import { newSeatBearer, parseSbtBearer } from "../src/gb-bearer.js";
import { verifyGrRequest, GR_VERIFY_CONTRACT } from "../src/gr-verify.js";
import { wakeSignature } from "../src/wake.js";
import { _resetInfisicalForTests } from "../src/infisical.js";

globalThis.crypto ??= webcrypto;
const registry = z.object({
  seats: z.array(z.object({
    tag: z.string(),
    zulipBotEmail: z.string().email().nullish(),
    zulipFileCode: z.string().optional(),
    retired: z.boolean().optional(),
  })),
}).parse(JSON.parse(readFileSync(new URL("../../../fleet-apps.json", import.meta.url))));
const config = { hostedSeats: [], botEmails: {} };

test("all active ordinary registry bots have canonical bearer identities without adding OAuth seats", () => {
  const ordinary = registry.seats.filter((r) => !r.retired && r.zulipBotEmail);
  for (const row of ordinary) assert.ok(BEARER_SEATS.includes(row.tag), row.tag);
  for (const [identity, row] of Object.entries(BEARER_ONLY_SEATS)) {
    const source = ordinary.find((r) => r.tag === identity);
    assert.equal(row.email, source.zulipBotEmail);
    assert.equal(row.prefix, source.zulipFileCode.toUpperCase().replaceAll("-", "_"));
    assert.ok(!KNOWN_SEATS.includes(identity), "bearer inclusion must not grant OAuth hosting");
  }
  for (const name of ["GROK-BOT", "MONET", "DSH", "GROK-BUILD", "INSTINCT", "BF-TYPO", "OPERATOR"]) {
    assert.ok(!BEARER_SEATS.includes(name), name);
    assert.equal(parseSbtBearer(`sbt_${name.toLowerCase()}_${"a".repeat(43)}`), null);
  }
});

test("each canonical seat bearer round-trips and is recognized by the signed hash-only verifier", async () => {
  const key = "offline-test-only-" + "k".repeat(32);
  for (const identity of BEARER_SEATS) {
    const bearer = await newSeatBearer(identity);
    assert.equal(parseSbtBearer(bearer.token).identity, identity);
    const bytes = new TextEncoder().encode(JSON.stringify({ contract: GR_VERIFY_CONTRACT, identity, hash: bearer.hash, sent_at: Math.floor(Date.now() / 1000) }));
    const signature = await wakeSignature(key, bytes);
    assert.equal((await verifyGrRequest({ bytes, signature, key })).query.identity, identity);
  }
});

test("new bearer identities cannot activate from off env credentials", async () => {
  for (const mode of ["off"]) {
    const env = { INFISICAL_MODE: mode, AG_ZULIP_KEY: "offline-dummy", AG_ZULIP_EMAIL: BEARER_ONLY_SEATS.AG.email };
    assert.equal(await resolveBearerSeatCredentials(env, config, "AG"), null);
  }
});

test("new seats resolve only their exact own Infisical names and reject swapped or absent emails", async () => {
  const originalFetch = globalThis.fetch;
  const env = { INFISICAL_MODE: "required", INFISICAL_SERVICE_TOKEN: "offline-dummy", INFISICAL_WORKSPACE_ID: "offline-workspace" };
  try {
    for (const [identity, row] of Object.entries(ADDITIONAL_BEARER_SEATS)) {
      for (const email of [row.email, "other-bot@simplewithus.zulipchat.com", ""]) {
        _resetInfisicalForTests();
        const names = [];
        globalThis.fetch = async (url) => {
          const name = decodeURIComponent(new URL(url).pathname.split("/").at(-1));
          names.push(name);
          const value = name === `${row.prefix}_ZULIP_KEY` ? "offline-dummy-key" : email;
          if (!value) return new Response("", { status: 404 });
          return Response.json({ secret: { secretValue: value } });
        };
        const result = await resolveBearerSeatCredentials(env, config, identity);
        assert.deepEqual(names.sort(), [`${row.prefix}_ZULIP_EMAIL`, `${row.prefix}_ZULIP_KEY`].sort());
        assert.deepEqual(result, email === row.email ? { email, key: "offline-dummy-key" } : null);
      }
    }
    assert.equal(await resolveBearerSeatCredentials(env, config, "BF-TYPO"), null);
  } finally {
    globalThis.fetch = originalFetch;
    _resetInfisicalForTests();
  }
});

test("BotFleet roles are explicit, distinct from GB personas, and never wildcard admitted", () => {
  const roles = ["BUILDER", "COMPILER", "DEPLOYER", "DESIGNER", "DIRECTOR", "ENGINEER", "TRADER", "FIXER", "HOUSEKEEPER", "MONITOR", "ORACLE", "PLUMBER", "PUBLISHER"];
  assert.deepEqual(Object.keys(BF_BEARER_SEATS).sort(), roles.map((r) => `BF-${r}`).sort());
  for (const role of roles) {
    const row = BF_BEARER_SEATS[`BF-${role}`];
    assert.equal(row.email, `bf-${role.toLowerCase()}-bot@simplewithus.zulipchat.com`);
    assert.equal(row.prefix, `BF_${role}`);
    assert.ok(!KNOWN_SEATS.includes(`BF-${role}`));
  }
  for (const identity of ["BF-TYPO", "constructor", "toString", "__proto__"]) {
    assert.ok(!BEARER_SEATS.includes(identity));
  }
});

test("every bearer identity opens only its own SeatGate, unknown names fail closed", () => {
  const env = { SEAT_GATE: { idFromName: (name) => name, get: (id) => ({ id }) } };
  for (const identity of BEARER_SEATS) assert.deepEqual(gate(env, identity), { id: identity });
  for (const identity of ["BF-TYPO", "GROK-BOT", "OPERATOR", "__proto__"]) assert.throws(() => gate(env, identity), /not a seat/);
});

test("unhosted CODEX bearer readiness uses only its own native records without OAuth or outer-mode changes", async () => {
  const originalFetch = globalThis.fetch;
  const codex = registry.seats.find((r) => r.tag === "CODEX");
  const canonical = z.string().email().parse(codex?.zulipBotEmail);
  assert.equal(canonical, "codex-bot@simplewithus.zulipchat.com");
  const env = Object.freeze({ INFISICAL_MODE: "fallback", INFISICAL_SERVICE_TOKEN: "offline-dummy", INFISICAL_WORKSPACE_ID: "offline-workspace", ZULIP_KEY_CODEX: "must-not-fallback", ZULIP_EMAIL_CODEX: canonical });
  const unhosted = Object.freeze({ hostedSeats: Object.freeze([]), botEmails: Object.freeze({}) });
  try {
    for (const values of [
      { CODEX_ZULIP_KEY: "offline-own-key", CODEX_ZULIP_EMAIL: canonical },
      { CODEX_ZULIP_KEY: "offline-own-key", CODEX_ZULIP_EMAIL: `  ${canonical.toUpperCase()}  ` },
      { CODEX_ZULIP_KEY: "offline-own-key", CODEX_ZULIP_EMAIL: "echo-bot@simplewithus.zulipchat.com" },
      { CODEX_ZULIP_EMAIL: canonical },
      { CODEX_ZULIP_KEY: "offline-own-key" },
    ]) {
      _resetInfisicalForTests();
      const reads = [];
      globalThis.fetch = async (url) => {
        const name = decodeURIComponent(new URL(url).pathname.split("/").at(-1));
        reads.push(name);
        return values[name] ? Response.json({ secret: { secretValue: values[name] } }) : new Response("not found", { status: 404 });
      };
      const result = await resolveBearerSeatCredentials(env, unhosted, "CODEX");
      assert.deepEqual(reads.sort(), ["CODEX_ZULIP_EMAIL", "CODEX_ZULIP_KEY"]);
      assert.deepEqual(result, values.CODEX_ZULIP_KEY && String(values.CODEX_ZULIP_EMAIL).trim().toLowerCase() === canonical ? { key: "offline-own-key", email: canonical } : null);
      assert.equal(env.INFISICAL_MODE, "fallback");
      assert.deepEqual(unhosted, { hostedSeats: [], botEmails: {} });
    }
    for (const status of [403, 500]) {
      _resetInfisicalForTests();
      globalThis.fetch = async () => new Response("unavailable", { status });
      await assert.rejects(() => resolveBearerSeatCredentials(env, unhosted, "CODEX"), (error) => error.status === status);
    }
    _resetInfisicalForTests();
    globalThis.fetch = async () => { throw new Error("network unavailable"); };
    await assert.rejects(() => resolveBearerSeatCredentials(env, unhosted, "CODEX"), /network unavailable/);
    assert.equal(await resolveBearerSeatCredentials({ ...env, INFISICAL_MODE: "off" }, unhosted, "CODEX"), null);
    assert.equal(await resolveBearerSeatCredentials({ ...env, INFISICAL_MODE: "off" }, unhosted, "AG"), null);
  } finally {
    globalThis.fetch = originalFetch;
    _resetInfisicalForTests();
  }
});


test("fallback additional identities and unhosted MA use strict own records without outer-mode mutation", async () => {
  const originalFetch = globalThis.fetch;
  try {
    const rows = {...ADDITIONAL_BEARER_SEATS, MA: {prefix: "MA", email: "muse-assist-bot@simplewithus.zulipchat.com"}};
    for (const [identity,row] of Object.entries(rows)) {
      if (identity === "BF-BUILDER") continue;
      const env = Object.freeze({INFISICAL_MODE: "fallback", INFISICAL_SERVICE_TOKEN: "offline", INFISICAL_WORKSPACE_ID: "offline", [`${row.prefix}_ZULIP_KEY`]: "must-not-fallback"});
      for (const outcome of ["valid", "swapped", "missing", "denied", "failed"]) {
        _resetInfisicalForTests();
        const reads=[];
        globalThis.fetch=async url=>{
          const u=new URL(url); const name=decodeURIComponent(u.pathname.split("/").at(-1)); reads.push(name);
          assert.equal(u.searchParams.get("secretPath"), "/zulip");
          if(outcome==="denied") return new Response("denied",{status:403});
          if(outcome==="failed") throw new Error("offline failure");
          if(outcome==="missing") return new Response("missing",{status:404});
          return Response.json({secret:{secretValue:name.endsWith("_KEY") ? "own-key" : outcome==="swapped" ? "echo-bot@simplewithus.zulipchat.com" : row.email}});
        };
        if(outcome==="denied"||outcome==="failed") await assert.rejects(()=>resolveBearerSeatCredentials(env,config,identity));
        else assert.deepEqual(await resolveBearerSeatCredentials(env,config,identity), outcome==="valid" ? {key:"own-key",email:row.email}:null);
        assert.deepEqual(reads.sort(),[`${row.prefix}_ZULIP_EMAIL`,`${row.prefix}_ZULIP_KEY`].sort());
        assert.equal(env.INFISICAL_MODE,"fallback");
      }
      _resetInfisicalForTests();
      globalThis.fetch=async()=>{throw new Error("off must not fetch")};
      assert.equal(await resolveBearerSeatCredentials({...env,INFISICAL_MODE:"off"},config,identity),null);
    }
  } finally {globalThis.fetch=originalFetch;_resetInfisicalForTests();}
});

test("fallback readiness does not revive retired Builder", async () => {
  const originalFetch = globalThis.fetch;
  try {
    globalThis.fetch = async () => { throw new Error("Builder must not fetch"); };
    for (const mode of ["fallback", "off"]) {
      assert.equal(await resolveBearerSeatCredentials({ INFISICAL_MODE: mode }, config, "BF-BUILDER"), null);
    }
  } finally {
    globalThis.fetch = originalFetch;
    _resetInfisicalForTests();
  }
});
