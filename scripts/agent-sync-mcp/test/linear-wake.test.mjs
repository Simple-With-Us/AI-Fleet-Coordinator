// The Linear wake door (src/linear-wake.js):  the signature, the freshness
// window, the event filter, the delivery, and the fact that nothing secret
// ever reaches a log line.
//
// The tests drive the pure decision function and the route handler directly,
// with the Infisical and the Linear and the delivery endpoints faked through
// one fetch.  Every credential-shaped value here is synthetic.

import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import {
  decideLinearWake,
  serveLinearWake,
  buildSeatMap,
  _resetLinearWakeForTests,
  LINEAR_WAKE_CONTRACT,
  LINEAR_WAKE_SECRET_NAME,
  LINEAR_WAKE_MAX_BODY,
  NO_WAKE_MARKERS,
  ZULIP_RELAY_MARKER,
} from "../src/linear-wake.js";
import { _resetInfisicalForTests } from "../src/infisical.js";

const SECRET = "s".repeat(64);
const NOW = Date.UTC(2026, 9, 27, 12, 0, 0);
const GB = "GB-FIXER";
const BF = "BF-BUILDER";
const GB_KEY = "crsr-abcdefghijklmnopqrstuvwxyz012345";
const BF_SECRET = "bf-secret-abcdefghijklmnopqrstuvwxyz012345";
const GB_ENDPOINT = "https://api2.cursor.sh/gb-fixer-hook";
const BF_ENDPOINT = "https://botfleet.example/hooks/abc123";

/** The seat map two seats, as buildSeatMap would have built it. */
function seatMap() {
  return new Map([
    [GB, { seat: GB, userId: "user-gb-fixer", urlKey: "gb-fixer", name: "GB-Fixer", displayName: "GB-Fixer" }],
    [BF, { seat: BF, userId: "user-bf-builder", urlKey: "bf-builder", name: "BF-Builder", displayName: "BF-Builder" }],
  ]);
}

/** A Linear webhook body:  a comment that mentions a seat by its profile url. */
function comment({ body = null, seat = GB, type = "Comment", action = "create", extra = {} } = {}) {
  const key = seat === GB ? "gb-fixer" : "bf-builder";
  const text = body ?? `hey https://linear.app/simple-with-us/profiles/${key} look at this`;
  return {
    type,
    action,
    createdAt: new Date(NOW).toISOString(),
    webhookTimestamp: NOW,
    actor: { id: "user-jay", name: "Jay Wedgeworth", type: "user" },
    url: `https://linear.app/simple-with-us/issue/AFC-1/comment/1`,
    data: {
      id: "comment-1",
      body: text,
      issue: { id: "issue-1", identifier: "AFC-1", title: "A title", url: "https://linear.app/simple-with-us/issue/AFC-1" },
      ...extra,
    },
  };
}

const hex = (bytes) => createHmac("sha256", Buffer.from(SECRET, "utf8")).update(bytes).digest("hex");

/**
 * One fake fetch:  the Infisical raw-secret reads, the Linear token and
 * GraphQL calls, and the delivery endpoints.  Every call is recorded.
 */
function fakeFetch({ secrets = {}, linear = true, endpointStatus = 200, endpointRedirect = null } = {}) {
  const calls = [];
  const infisical = {
    [LINEAR_WAKE_SECRET_NAME]: SECRET,
    ZULIP_ALERT_GB_FIXER_ENDPOINT: GB_ENDPOINT,
    ZULIP_ALERT_GB_FIXER_KEY: GB_KEY,
    BF_LINEAR_WAKE_BUILDER_URL: BF_ENDPOINT,
    BF_LINEAR_WAKE_BUILDER_SECRET: BF_SECRET,
    LINEAR_GB_FIXER_CLIENT_ID: "cid-gb-fixer",
    LINEAR_GB_FIXER_CLIENT_SECRET: "sec-gb-fixer",
    LINEAR_BF_BUILDER_CLIENT_ID: "cid-bf-builder",
    LINEAR_BF_BUILDER_CLIENT_SECRET: "sec-bf-builder",
    ...secrets,
  };
  const impl = async (url, options = {}) => {
    const href = String(url);
    calls.push({ url: href, method: options.method ?? "GET", headers: options.headers ?? {}, body: options.body, redirect: options.redirect ?? null });
    // The folder listing, the way a real one answers:  every name the folder
    // holds, and nothing else.  A name with an empty value is absent.
    if (href.includes("/api/v3/secrets/raw?")) {
      const items = Object.entries(infisical)
        .filter(([, value]) => value !== "" && value !== undefined)
        .map(([secretKey, secretValue]) => ({ secretKey, secretValue }));
      return new Response(JSON.stringify({ secrets: items, totalCount: items.length }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (href.includes("/api/v3/secrets/raw/")) {
      const name = decodeURIComponent(href.split("/api/v3/secrets/raw/")[1].split("?")[0]);
      const value = infisical[name];
      if (!value) return new Response(JSON.stringify({ error: "not found" }), { status: 404 });
      return new Response(JSON.stringify({ secret: { secretValue: value } }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (href.includes("/api/v3/secrets/raw?")) {
      return new Response(JSON.stringify({ secrets: [], totalCount: 0 }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (href.includes("api.linear.app/oauth/token")) {
      return new Response(JSON.stringify({ access_token: "lin-token", token_type: "Bearer", expires_in: 30 * 86400 }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (href.includes("api.linear.app/graphql")) {
      if (!linear) return new Response("{}", { status: 500 });
      return new Response(JSON.stringify({ data: { viewer: { id: "user-gb-fixer", name: "GB-Fixer", displayName: "GB-Fixer", url: "https://linear.app/simple-with-us/profiles/gb-fixer" } } }), { status: 200, headers: { "Content-Type": "application/json" } });
    }
    if (href === GB_ENDPOINT || href === BF_ENDPOINT || href.startsWith("http://insecure")) {
      if (endpointRedirect) return new Response("", { status: endpointRedirect, headers: { Location: "https://elsewhere.example/" } });
      return new Response("{}", { status: endpointStatus, headers: { "Content-Type": "application/json" } });
    }
    return new Response("{}", { status: 404 });
  };
  impl.calls = calls;
  // The Infisical module reads through the global fetch, so the fake has to
  // stand in for it too (the same shape as test/infisical.test.mjs).
  globalThis.fetch = impl;
  return impl;
}

/** The env the route needs:  Infisical only, in required mode. */
function env(overrides = {}) {
  return { INFISICAL_MODE: "required", INFISICAL_SERVICE_TOKEN: "st-test", INFISICAL_WORKSPACE_ID: "ws-test", INFISICAL_ENVIRONMENT: "prod", ...overrides };
}

/** A signed request for one body. */
function request(payload, { delivery = "d-1", signature = hex(Buffer.from(JSON.stringify(payload), "utf8")), method = "POST", raw = null } = {}) {
  const body = raw ?? JSON.stringify(payload);
  const headers = new Headers({ "content-type": "application/json" });
  if (signature !== null) headers.set("linear-signature", signature);
  if (delivery !== null) headers.set("linear-delivery", delivery);
  return new Request("https://agents.simplewithus.com/webhooks/linear", { method, headers, body: method === "GET" ? undefined : body });
}

/** A ctx whose waitUntil work the test can await. */
function ctx() {
  const pending = [];
  return { pending, waitUntil: (p) => pending.push(p) };
}

/** Run the route and return the answer plus the recorded calls. */
async function run(payload, { fetchImpl, ...rest } = {}) {
  const fetcher = fetchImpl ?? fakeFetch();
  const context = ctx();
  const response = await serveLinearWake(request(payload, rest), env(), context, { fetchImpl: fetcher, now: () => NOW, seatMap: seatMap() });
  await Promise.all(context.pending);
  return { response, body: await response.json(), calls: fetcher.calls, pending: context.pending };
}

// ---------------------------------------------------------------- the decision function

test("a comment that mentions a GB seat by its profile url wakes that seat", () => {
  const out = decideLinearWake(comment(), seatMap());
  assert.equal(out.skip, undefined);
  assert.deepEqual(out.seats, [{ seat: GB, via: "mention" }]);
});

test("a comment that mentions a BF seat wakes that seat", () => {
  const out = decideLinearWake(comment({ seat: BF }), seatMap());
  assert.deepEqual(out.seats, [{ seat: BF, via: "mention" }]);
});

test("a plain word that happens to be a seat's name is not a mention", () => {
  const out = decideLinearWake(comment({ body: "the fixer should look at this" }), seatMap());
  assert.equal(out.skip, "no_seat_matched");
});

test("an @-mention of the seat's display name is a mention", () => {
  const out = decideLinearWake(comment({ body: "hey @GB-Fixer look" }), seatMap());
  assert.deepEqual(out.seats, [{ seat: GB, via: "mention" }]);
});

test("a mention inside a code span does not count", () => {
  const out = decideLinearWake(comment({ body: "try `https://linear.app/simple-with-us/profiles/gb-fixer`" }), seatMap());
  assert.equal(out.skip, "no_seat_matched");
});

test("a mention inside a fenced code block does not count", () => {
  const out = decideLinearWake(comment({ body: "```\nhttps://linear.app/simple-with-us/profiles/gb-fixer\n```" }), seatMap());
  assert.equal(out.skip, "no_seat_matched");
});

test("a mention on a quote line does not count", () => {
  const out = decideLinearWake(comment({ body: "> https://linear.app/simple-with-us/profiles/gb-fixer" }), seatMap());
  assert.equal(out.skip, "no_seat_matched");
});

test("the relay's marker comment is a Zulip thread, not a wake", () => {
  const out = decideLinearWake(comment({ body: `${ZULIP_RELAY_MARKER} AFC-1|42\n**Zulip discussion:** #work > topic` }), seatMap());
  assert.equal(out.skip, "zulip_thread");
});

test("a comment with a Zulip narrow link is a Zulip thread", () => {
  const out = decideLinearWake(comment({ body: "see https://simplewithus.zulipchat.com/#narrow/near/4242" }), seatMap());
  assert.equal(out.skip, "zulip_thread");
});

test("a comment from the relay app is a Zulip thread", () => {
  const payload = comment();
  payload.actor = { id: "relay", name: "Relay → Linear", type: "app" };
  assert.equal(decideLinearWake(payload, seatMap()).skip, "zulip_thread");
});

test("a comment with a Zulip attachment is a Zulip thread", () => {
  const payload = comment({ extra: { attachments: [{ url: "https://simplewithus.zulipchat.com/user_uploads/1/x.png" }] } });
  assert.equal(decideLinearWake(payload, seatMap()).skip, "zulip_thread");
});

test("a comment that opens with the eyes marker is not a wake", () => {
  assert.equal(decideLinearWake(comment({ body: ":eyes: https://linear.app/simple-with-us/profiles/gb-fixer" }), seatMap()).skip, "no_wake_marker");
  assert.equal(decideLinearWake(comment({ body: `  ${NO_WAKE_MARKERS[1]} https://linear.app/simple-with-us/profiles/gb-fixer` }), seatMap()).skip, "no_wake_marker");
});

test("a seat never wakes itself", () => {
  const payload = comment();
  payload.actor = { id: "user-gb-fixer", name: "GB-Fixer", type: "app" };
  assert.equal(decideLinearWake(payload, seatMap()).skip, "no_seat_matched");
});

test("an attachment event never wakes anybody", () => {
  const payload = comment();
  payload.type = "Attachment";
  assert.equal(decideLinearWake(payload, seatMap()).skip, "not_a_wake_event");
});

test("an issue created with an assignee that is a seat wakes that seat", () => {
  const payload = {
    type: "Issue",
    action: "create",
    webhookTimestamp: NOW,
    actor: { id: "user-jay", name: "Jay", type: "user" },
    data: { id: "issue-1", identifier: "AFC-1", title: "A title", description: "", assigneeId: "user-bf-builder", url: "https://linear.app/simple-with-us/issue/AFC-1" },
  };
  assert.deepEqual(decideLinearWake(payload, seatMap()).seats, [{ seat: BF, via: "assigned" }]);
});

test("an issue update that assigns a seat wakes that seat", () => {
  const payload = {
    type: "Issue",
    action: "update",
    webhookTimestamp: NOW,
    actor: { id: "user-jay", name: "Jay", type: "user" },
    data: { id: "issue-1", identifier: "AFC-1", title: "A title", description: "", assigneeId: "user-gb-fixer", url: "https://linear.app/simple-with-us/issue/AFC-1" },
    updatedFrom: { assigneeId: null },
  };
  assert.deepEqual(decideLinearWake(payload, seatMap()).seats, [{ seat: GB, via: "assigned" }]);
});

test("an issue update without an assignee change is not a wake", () => {
  const payload = {
    type: "Issue",
    action: "update",
    webhookTimestamp: NOW,
    actor: { id: "user-jay", name: "Jay", type: "user" },
    data: { id: "issue-1", identifier: "AFC-1", title: "A title", description: "", assigneeId: "user-jay", url: "https://linear.app/simple-with-us/issue/AFC-1" },
    updatedFrom: { title: "Old title" },
  };
  assert.equal(decideLinearWake(payload, seatMap()).skip, "not_a_wake_event");
});

test("a comment update that only edits the text is not a wake", () => {
  const payload = comment({ action: "update" });
  payload.updatedFrom = { body: "hey https://linear.app/simple-with-us/profiles/gb-fixer look at this" };
  payload.data.body = "hey https://linear.app/simple-with-us/profiles/gb-fixer look at THAT";
  assert.equal(decideLinearWake(payload, seatMap()).skip, "not_a_wake_event");
});

test("a comment update that adds a new mention wakes the new seat only", () => {
  const payload = comment({ action: "update" });
  payload.updatedFrom = { body: "hey https://linear.app/simple-with-us/profiles/gb-fixer look" };
  payload.data.body = "hey https://linear.app/simple-with-us/profiles/gb-fixer and https://linear.app/simple-with-us/profiles/bf-builder look";
  assert.deepEqual(decideLinearWake(payload, seatMap()).seats, [{ seat: BF, via: "mention" }]);
});

test("a comment update that did not change the body wakes nobody", () => {
  const payload = comment({ action: "update" });
  payload.updatedFrom = { resolvedAt: "2026-10-27T12:00:00.000Z" };
  assert.equal(decideLinearWake(payload, seatMap()).skip, "not_a_wake_event");
});

test("a comment update diffs the id-only mentions against the previous mention ids", () => {
  const payload = comment({ action: "update" });
  payload.updatedFrom = { body: "look at this", mentionedUserIds: ["user-gb-fixer"] };
  payload.data.body = "look at this";
  payload.data.mentionedUserIds = ["user-gb-fixer"];
  assert.equal(decideLinearWake(payload, seatMap()).skip, "not_a_wake_event");
});

test("a comment update that adds a seat by id alone wakes that seat", () => {
  const payload = comment({ action: "update" });
  payload.updatedFrom = { body: "look at this", mentionedUserIds: ["user-gb-fixer"] };
  payload.data.body = "look at this";
  payload.data.mentionedUserIds = ["user-gb-fixer", "user-bf-builder"];
  assert.deepEqual(decideLinearWake(payload, seatMap()).seats, [{ seat: BF, via: "mention" }]);
});

// ---------------------------------------------------------------- the door

test("a good signature is accepted and the seat is woken", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const { response, body, calls } = await run(comment(), { fetchImpl: fetcher });
  assert.equal(response.status, 200);
  assert.deepEqual(body, { ok: true, woke: [GB] });
  const delivery = calls.find((call) => call.url === GB_ENDPOINT);
  assert.ok(delivery, "the GB endpoint was called");
  assert.equal(delivery.method, "POST");
  assert.equal(delivery.headers.Authorization, `Bearer ${GB_KEY}`);
  const sent = JSON.parse(delivery.body);
  assert.equal(sent.contract, LINEAR_WAKE_CONTRACT);
  assert.equal(sent.seat, GB);
  assert.equal(sent.via, "mention");
  assert.equal(sent.issue.identifier, "AFC-1");
  assert.equal(sent.issue.title, "A title");
  assert.equal(sent.comment_url, "https://linear.app/simple-with-us/issue/AFC-1/comment/1");
  assert.deepEqual(sent.actor, { name: "Jay Wedgeworth", type: "user" });
  assert.equal(sent.linear_delivery, "d-1");
  assert.ok(sent.sent_at);
  assert.match(sent.note, /untrusted/i);
});

test("a BF seat is woken through its BotFleet hook", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const { body, calls } = await run(comment({ seat: BF }), { fetchImpl: fetcher });
  assert.deepEqual(body, { ok: true, woke: [BF] });
  const delivery = calls.find((call) => call.url === BF_ENDPOINT);
  assert.ok(delivery, "the BF hook was called");
  assert.equal(delivery.headers.Authorization, `Bearer ${BF_SECRET}`);
});

test("a bad signature is 401 and nothing is delivered", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const { response, calls } = await run(comment(), { fetchImpl: fetcher, signature: "0".repeat(64) });
  assert.equal(response.status, 401);
  assert.ok(!calls.some((call) => call.url === GB_ENDPOINT), "nothing was delivered");
});

test("a missing signature is 401", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const { response } = await run(comment(), { fetchImpl: fetcher, signature: null });
  assert.equal(response.status, 401);
});

test("a stale webhookTimestamp is 401", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const payload = comment();
  payload.webhookTimestamp = NOW - 120_000;
  const { response, body } = await run(payload, { fetchImpl: fetcher });
  assert.equal(response.status, 401);
  assert.deepEqual(body, { error: "stale" });
});

test("a mode that is not required refuses the door with 503", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const response = await serveLinearWake(request(comment()), { INFISICAL_MODE: "fallback" }, ctx(), { fetchImpl: fetcher, now: () => NOW });
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "linear_wake_requires_infisical_required_mode" });
  assert.ok(!fetcher.calls.some((call) => call.url.includes("secrets/raw")), "no secret was read");
});

test("a missing signing secret is 503 and unconfigured", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch({ secrets: { [LINEAR_WAKE_SECRET_NAME]: "" } });
  const response = await serveLinearWake(request(comment()), env(), ctx(), { fetchImpl: fetcher, now: () => NOW });
  assert.equal(response.status, 503);
  assert.deepEqual(await response.json(), { error: "linear_wake_unconfigured" });
  assert.ok(!fetcher.calls.some((call) => call.url === GB_ENDPOINT), "nothing was delivered");
});

test("a repeated delivery id wakes nobody twice", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const first = await run(comment(), { fetchImpl: fetcher, delivery: "dup-1" });
  assert.deepEqual(first.body, { ok: true, woke: [GB] });
  const second = await run(comment(), { fetchImpl: fetcher, delivery: "dup-1" });
  assert.deepEqual(second.body, { ok: true, skipped: "duplicate" });
  assert.equal(fetcher.calls.filter((call) => call.url === GB_ENDPOINT).length, 1, "one delivery only");
});

test("a request without the delivery header is refused with 400", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const { response, body, calls } = await run(comment(), { fetchImpl: fetcher, delivery: null });
  assert.equal(response.status, 400);
  assert.deepEqual(body, { error: "missing_delivery" });
  assert.ok(!calls.some((call) => call.url === GB_ENDPOINT), "nothing is delivered");
});

test("a payload that does not match the schema is refused with 400", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const payload = comment();
  payload.data = "not an object";
  const { response, body, calls } = await run(payload, { fetchImpl: fetcher });
  assert.equal(response.status, 400);
  assert.deepEqual(body, { error: "bad_payload" });
  assert.ok(!calls.some((call) => call.url === GB_ENDPOINT), "nothing is delivered");
});

test("a body over the limit is 413", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const huge = "x".repeat(LINEAR_WAKE_MAX_BODY + 10);
  const response = await serveLinearWake(request(null, { raw: huge }), env(), ctx(), { fetchImpl: fetcher, now: () => NOW });
  assert.equal(response.status, 413);
});

test("a relay comment is skipped and nothing is delivered", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const { response, body, calls } = await run(comment({ body: `${ZULIP_RELAY_MARKER} AFC-1|42\nhello` }), { fetchImpl: fetcher });
  assert.equal(response.status, 200);
  assert.deepEqual(body, { ok: true, skipped: "zulip_thread" });
  assert.ok(!calls.some((call) => call.url === GB_ENDPOINT));
});

test("an eyes-prefixed comment is skipped", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  const { body } = await run(comment({ body: ":eyes: https://linear.app/simple-with-us/profiles/gb-fixer" }), { fetchImpl: fetcher });
  assert.deepEqual(body, { ok: true, skipped: "no_wake_marker" });
});

test("a seat is woken at most six times an hour", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch();
  for (let i = 0; i < 6; i++) {
    const out = await run(comment(), { fetchImpl: fetcher, delivery: `rate-${i}` });
    assert.deepEqual(out.body, { ok: true, woke: [GB] }, `wake ${i + 1} is allowed`);
  }
  const seventh = await run(comment(), { fetchImpl: fetcher, delivery: "rate-6" });
  assert.deepEqual(seventh.body, { ok: true, skipped: "rate_limited" });
  assert.equal(fetcher.calls.filter((call) => call.url === GB_ENDPOINT).length, 6, "six deliveries only");
});

test("a redirect from the GB endpoint is a failure and is not followed", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch({ endpointRedirect: 302 });
  const { calls } = await run(comment(), { fetchImpl: fetcher });
  const delivery = calls.find((call) => call.url === GB_ENDPOINT);
  assert.ok(delivery, "the endpoint was called");
  assert.equal(delivery.redirect, "manual", "the redirect is never followed");
  assert.ok(!calls.some((call) => call.url === "https://elsewhere.example/"), "the redirect was not followed");
});

test("an http BF endpoint is refused", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const fetcher = fakeFetch({ secrets: { BF_LINEAR_WAKE_BUILDER_URL: "http://botfleet.example/hooks/abc123" } });
  const { calls } = await run(comment({ seat: BF }), { fetchImpl: fetcher });
  assert.ok(!calls.some((call) => call.url.includes("botfleet.example")), "the insecure url was not called");
});

test("no secret ever reaches a log line", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const lines = [];
  const real = console.log;
  console.log = (...args) => lines.push(args.join(" "));
  try {
    const fetcher = fakeFetch();
    await run(comment(), { fetchImpl: fetcher });
  } finally {
    console.log = real;
  }
  const text = lines.join("\n");
  assert.ok(lines.length, "something was logged");
  for (const secret of [SECRET, GB_KEY, BF_SECRET, "lin-token", "cid-gb-fixer", "sec-gb-fixer"]) {
    assert.ok(!text.includes(secret), `the log must not carry ${secret.slice(0, 6)}`);
  }
  assert.ok(!text.includes("hey https://linear.app"), "the log must not carry the body");
});

test("the seat map leaves an unmapped seat out and logs it once", async () => {
  _resetInfisicalForTests();
  _resetLinearWakeForTests();
  const lines = [];
  const real = console.log;
  console.log = (...args) => lines.push(args.join(" "));
  let map;
  try {
    map = await buildSeatMap(env(), { fetchImpl: fakeFetch({ linear: false }), now: () => NOW, gateFor: () => ({ linearTokenGet: async () => null, linearTokenSet: async () => {}, idemBegin: async () => ({ verdict: "go" }), idemDrop: async () => {} }) });
  } finally {
    console.log = real;
  }
  assert.ok(map.size < 23, "not every seat mapped");
  assert.ok(lines.some((line) => line.includes("linear_wake_seat_unmapped")), "the unmapped seat was logged");
  assert.ok(!lines.join("\n").includes("sec-gb-fixer"), "no secret in the log");
});
