import test from "node:test";
import assert from "node:assert/strict";
import { serveMcpConfig, parseEntries, parseOverride, effectiveFor, normalizeStored, MCP_CONFIG_KEY } from "../src/mcp-config.js";

const SEATS = ["WISE", "JET", "BF-BUILDER", "BF-TRADER"];
const E = (name, host = "example.com") => ({ name, url: `https://${host}/mcp` });

function deps(scope = "owner", initial = null) {
  const store = new Map(initial ? [[MCP_CONFIG_KEY, JSON.stringify(initial)]] : []);
  const audits = [];
  return { store, audits, d: { issuer: "https://agents.example", actor: "service-token:t", scope, seats: SEATS, kv: { get: async (k) => store.get(k) ?? null, put: async (k, v) => { store.set(k, v); } }, audit: async (e) => { audits.push(e); } } };
}
const post = (body) => new Request("https://agents.example/admin/api/mcp-config", { method: "POST", headers: { "content-type": "application/json", "x-admin-api": "1" }, body: JSON.stringify(body) });
const get = () => new Request("https://agents.example/admin/api/mcp-config");

test("entries: https only, no credentials, query or fragment, unique clean names", () => {
  assert.deepEqual(parseEntries([E("a")]).value, [{ name: "a", url: "https://example.com/mcp" }]);
  for (const url of ["http://x.com/m", "https://u:p@x.com/m", "https://x.com/m?k=v", "https://x.com/m#f", "nope"]) assert.ok(parseEntries([{ name: "a", url }]).error, url);
  assert.ok(parseEntries([E("A")]).error);
  assert.ok(parseEntries([E("a"), E("a")]).error);
  assert.ok(parseEntries([{ name: "a", url: "https://x.com/m", token: "x" }]).error);
});

test("effective list layers defaults, bf, then the seat override", () => {
  const c = { version: 1, defaults: [E("one"), E("two")], bf: [E("two", "bf.example.com"), E("three")], perSeat: { "BF-TRADER": { remove: ["one"], add: [E("four")] }, JET: { replace: [E("only")] } } };
  assert.deepEqual(effectiveFor(c, "WISE").map((e) => e.name), ["one", "two"]);
  assert.deepEqual(effectiveFor(c, "BF-BUILDER").map((e) => e.name), ["one", "two", "three"]);
  assert.equal(effectiveFor(c, "BF-BUILDER").find((e) => e.name === "two").url, "https://bf.example.com/mcp");
  assert.deepEqual(effectiveFor(c, "BF-TRADER").map((e) => e.name), ["two", "three", "four"]);
  assert.deepEqual(effectiveFor(c, "JET").map((e) => e.name), ["only"]);
});

test("override: replace excludes add and remove; unknown keys refused", () => {
  assert.ok(parseOverride({ replace: [], add: [] }).error);
  assert.ok(parseOverride({ extra: 1 }).error);
  assert.deepEqual(parseOverride({}).value, {});
});

test("owner sets defaults, version moves, audit has names only", async () => {
  const { d, audits, store } = deps();
  const r = await serveMcpConfig(post({ action: "set_defaults", entries: [E("a")], expected_version: 0 }), d);
  assert.equal(r.status, 200);
  const body = await r.json();
  assert.equal(body.version, 1);
  assert.deepEqual(body.effective.WISE, [E("a")]);
  assert.equal(JSON.parse(store.get(MCP_CONFIG_KEY)).version, 1);
  assert.deepEqual(audits[0], { event: "mcp_config_changed", by: "service-token:t", scope: "owner", version: 1, action: "set_defaults" });
});

test("stale expected_version is a conflict and writes nothing", async () => {
  const { d, store } = deps("owner", { version: 3, defaults: [], bf: [], perSeat: {} });
  const r = await serveMcpConfig(post({ action: "set_bf", entries: [], expected_version: 2 }), d);
  assert.equal(r.status, 409);
  assert.equal(JSON.parse(store.get(MCP_CONFIG_KEY)).version, 3);
  assert.equal((await serveMcpConfig(post({ action: "set_bf", entries: [] }), d)).status, 400);
});

test("bf scope: BF-* seats and bf list only, never defaults or other seats", async () => {
  const { d } = deps("bf", { version: 1, defaults: [E("secret-default")], bf: [], perSeat: { WISE: { add: [E("w")] } } });
  assert.equal((await serveMcpConfig(post({ action: "set_defaults", entries: [], expected_version: 1 }), d)).status, 403);
  assert.equal((await serveMcpConfig(post({ action: "set_seat", seat: "WISE", override: {}, expected_version: 1 }), d)).status, 403);
  assert.equal((await serveMcpConfig(post({ action: "clear_seat", seat: "JET", expected_version: 1 }), d)).status, 403);
  const ok = await serveMcpConfig(post({ action: "set_seat", seat: "BF-BUILDER", override: { add: [E("x")] }, expected_version: 1 }), d);
  assert.equal(ok.status, 200);
  const body = await ok.json();
  assert.deepEqual(Object.keys(body.effective).sort(), ["BF-BUILDER", "BF-TRADER"]);
  assert.equal(body.defaults, undefined);
  assert.equal(body.perSeat.WISE, undefined);
  const read = await (await serveMcpConfig(get(), d)).json();
  assert.equal(read.perSeat.WISE, undefined);
  assert.equal(read.defaults, undefined);
});

test("POST needs JSON, the custom header and a known seat; GET is read only", async () => {
  const { d } = deps();
  const noHeader = new Request("https://agents.example/admin/api/mcp-config", { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  assert.equal((await serveMcpConfig(noHeader, d)).status, 403);
  assert.equal((await serveMcpConfig(post({ action: "set_seat", seat: "NOPE", override: {}, expected_version: 0 }), d)).status, 400);
  assert.equal((await serveMcpConfig(new Request("https://agents.example/admin/api/mcp-config", { method: "DELETE" }), d)).status, 405);
});

test("an unreadable store or a failing read is refused, never treated as empty", async () => {
  assert.ok(normalizeStored("{not json").error);
  const { d } = deps();
  d.kv.get = async () => { throw new Error("down"); };
  assert.equal((await serveMcpConfig(post({ action: "set_bf", entries: [], expected_version: 0 }), d)).status, 503);
  d.kv.get = async () => "{bad";
  assert.equal((await serveMcpConfig(get(), d)).status, 503);
});
