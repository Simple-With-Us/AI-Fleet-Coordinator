// PagerDuty source: a v3 webhook delivery becomes one row in the standing
// block of its PagerDuty service, from trigger to resolve.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import { createHmac } from "node:crypto";
import assert from "node:assert/strict";
import { __testing } from "./index.js";
import { fromPagerDuty, normalize } from "./normalize.js";
import { decide } from "./state.js";

const { handler, stateKey, validatePagerDutyPayload } = __testing;

function fakeKV() {
  const store = new Map();
  return {
    store,
    async get(key, type) {
      const value = store.has(key) ? store.get(key) : null;
      if (value === null) return null;
      return type === "json" ? JSON.parse(value) : value;
    },
    async put(key, value) {
      store.set(key, value);
    },
    async delete(key) {
      store.delete(key);
    },
    async list({ prefix = "" } = {}) {
      return {
        keys: [...store.keys()].filter((key) => key.startsWith(prefix)).map((name) => ({ name })),
        listComplete: true,
      };
    },
  };
}

function fakeStorage() {
  const data = new Map();
  return {
    async get(key) {
      return data.get(key);
    },
    async put(key, value) {
      data.set(key, value);
    },
    async list({ prefix }) {
      return new Map([...data].filter(([key]) => key.startsWith(prefix)));
    },
  };
}

function envFor(kv, extra = {}) {
  return {
    ALERT_STATE: kv,
    WRITER_STORAGE: fakeStorage(),
    ZULIP_BOT_EMAIL_PAGERDUTY: "pagerduty-status-bot@example.invalid",
    ZULIP_BOT_API_KEY_PAGERDUTY: "pagerduty-key",
    PAGERDUTY_WEBHOOK_SECRET: "header-fixture",
    INGRESS_SHARED_TOKEN: "shared-fixture",
    ...extra,
  };
}

function stubZulip() {
  const calls = [];
  const original = globalThis.fetch;
  let nextId = 500;
  globalThis.fetch = async (url, opts) => {
    const decoded = atob(opts.headers.Authorization.replace(/^Basic /, ""));
    calls.push({ method: opts.method, email: decoded.slice(0, decoded.indexOf(":")) });
    if (opts.method === "POST")
      return { ok: true, json: async () => ({ result: "success", id: nextId++ }) };
    return { ok: true, json: async () => ({ result: "success" }) };
  };
  return {
    calls,
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

/** A PagerDuty v3 incident delivery, shaped like the documented sample. */
function incident(status, extra = {}) {
  return {
    event: {
      id: "01DEN0B3D9IYXZ6ZHWDA0C5YMC",
      event_type: `incident.${status === "triggered" ? "triggered" : status}`,
      resource_type: "incident",
      occurred_at: "2026-10-29T01:13:24.000Z",
      agent: { id: "P3GDGTC", type: "user_reference", summary: "Owner" },
      data: {
        id: "Q094CK9DAG3RYE",
        type: "incident",
        html_url: "https://example.pagerduty.com/incidents/Q094CK9DAG3RYE",
        number: 578,
        status,
        title: "BotFleet production error spike",
        service: { id: "PA87S7M", summary: "jays.services", type: "service_reference" },
        assignees: [{ id: "P3GDGTC", summary: "Owner", type: "user_reference" }],
        priority: null,
        urgency: "high",
        ...extra,
      },
    },
  };
}

const post = (body, headers) =>
  new Request("https://test/webhook/pagerduty", {
    method: "POST",
    headers,
    body: typeof body === "string" ? body : JSON.stringify(body),
  });

test("a PagerDuty incident is keyed by its id and filed under its service", () => {
  const event = fromPagerDuty(incident("triggered"));
  assert.equal(event.source, "pagerduty");
  assert.equal(event.key, "pagerduty:q094ck9dag3rye");
  assert.equal(event.service, "jays.services");
  assert.equal(event.firing, true);
  assert.equal(event.severity, "SEVERE");
  assert.equal(event.text, "#578 BotFleet production error spike");
  assert.equal(event.link, "https://example.pagerduty.com/incidents/Q094CK9DAG3RYE");
  assert.equal(event.prodDown, true);
  assert.equal(normalize("pagerduty", incident("triggered")).key, event.key);
});

test("acknowledged stays firing and says so;  resolved closes", () => {
  const acked = fromPagerDuty(incident("acknowledged"));
  assert.equal(acked.firing, true);
  assert.match(acked.text, /\(acknowledged\)$/);
  const resolved = fromPagerDuty(incident("resolved"));
  assert.equal(resolved.firing, false);
  assert.equal(resolved.severity, "INFO");
});

test("the incident's status, not the event type, decides firing", () => {
  // A reassignment of a resolved incident must not reopen it.
  const body = incident("resolved");
  body.event.event_type = "incident.reassigned";
  assert.equal(fromPagerDuty(body).firing, false);
  // No status at all stays firing unless the event itself is a resolution.
  const bare = incident("triggered");
  delete bare.event.data.status;
  assert.equal(fromPagerDuty(bare).firing, true);
  bare.event.event_type = "incident.resolved";
  assert.equal(fromPagerDuty(bare).firing, false);
});

test("P1 and P2 outrank the urgency;  low urgency reads MODERATE", () => {
  assert.equal(fromPagerDuty(incident("triggered", { priority: { summary: "P1" }, urgency: "low" })).severity, "SEVERE");
  assert.equal(fromPagerDuty(incident("triggered", { priority: { summary: "P2" } })).severity, "MODERATE");
  assert.equal(fromPagerDuty(incident("triggered", { priority: { summary: "Custom" }, urgency: "low" })).severity, "MODERATE");
  assert.equal(fromPagerDuty(incident("triggered", { urgency: "low" })).severity, "MODERATE");
});

test("an incident with no identity, or one shaped like a credential, is refused", () => {
  const none = incident("triggered");
  delete none.event.data.id;
  assert.throws(() => fromPagerDuty(none), /no identity/);
  assert.throws(() => validatePagerDutyPayload(none), /missing identity/);
  const secret = incident("triggered", { id: "sk-ant-abcdefghijklmnopqrstuvwxyz" });
  assert.throws(() => fromPagerDuty(secret), (error) => error.code === "invalid_identity");
});

test("the validator refuses a body without the v3 envelope or with a mistyped field", () => {
  assert.throws(() => validatePagerDutyPayload({ data: {} }), /invalid payload/);
  assert.throws(() => validatePagerDutyPayload({ event: { data: {} } }), /missing event type/);
  assert.throws(
    () => validatePagerDutyPayload(incident("triggered", { title: { nested: true } })),
    /invalid text field/,
  );
  assert.throws(
    () => validatePagerDutyPayload(incident("triggered", { service: "jays.services" })),
    /invalid payload/,
  );
  assert.doesNotThrow(() => validatePagerDutyPayload(incident("triggered")));
});

test("a fresh firing incident with no vendor severity opens SEVERE, not INFO", () => {
  const result = decide(null, { firing: true, text: "down", link: "" }, 1000);
  assert.equal(result.state.severity, "SEVERE");
  // An open incident keeps its own severity when an update names none.
  const open = { phase: "alerting", severity: "MODERATE", since: 0, updated: 0, text: "down", link: "" };
  assert.equal(decide(open, { firing: true, text: "down", link: "" }, 2000).state.severity, "MODERATE");
});

test("the route accepts the static header and holds the incident", async () => {
  const kv = fakeKV();
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      post(incident("triggered"), { "x-pagerduty-token": "header-fixture" }),
      envFor(kv),
      {},
    );
    assert.equal(response.status, 200);
    const answer = await response.json();
    assert.equal(answer.action, "hold");
    const state = JSON.parse(kv.store.get(stateKey("pagerduty:q094ck9dag3rye")));
    assert.equal(state.phase, "pending");
    assert.equal(state.severity, "SEVERE");
    // Every write is the PagerDuty bot's own.
    assert.ok(zulip.calls.length > 0);
    assert.ok(zulip.calls.every((call) => call.email === "pagerduty-status-bot@example.invalid"));
  } finally {
    zulip.restore();
  }
});

test("the route accepts PagerDuty's v1 body signature, one of several during a rotation", async () => {
  const kv = fakeKV();
  const zulip = stubZulip();
  try {
    const body = JSON.stringify(incident("triggered"));
    const sig = createHmac("sha256", "signing-fixture").update(body).digest("hex");
    const response = await handler.fetch(
      post(body, { "x-pagerduty-signature": `v1=${"0".repeat(64)}, v1=${sig}` }),
      envFor(kv, { PAGERDUTY_WEBHOOK_SECRET: undefined, PAGERDUTY_SIGNING_SECRET: "signing-fixture" }),
      {},
    );
    assert.equal(response.status, 200);
  } finally {
    zulip.restore();
  }
});

test("a wrong header, a wrong signature or the shared bearer token is refused", async () => {
  const zulip = stubZulip();
  try {
    for (const headers of [
      { "x-pagerduty-token": "wrong" },
      { "x-pagerduty-signature": `v1=${"0".repeat(64)}` },
      { authorization: "Bearer shared-fixture" },
    ]) {
      const response = await handler.fetch(
        post(incident("triggered"), headers),
        envFor(fakeKV(), { PAGERDUTY_SIGNING_SECRET: "signing-fixture" }),
        {},
      );
      assert.equal(response.status, 401, JSON.stringify(headers));
    }
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

test("a ping or a non-incident event is acknowledged and dropped", async () => {
  const kv = fakeKV();
  const zulip = stubZulip();
  try {
    const ping = {
      event: {
        id: "01PING",
        event_type: "pagey.ping",
        resource_type: "pagey",
        data: { message: "Hello from your friend Pagey!", type: "ping" },
      },
    };
    const response = await handler.fetch(
      post(ping, { "x-pagerduty-token": "header-fixture" }),
      envFor(kv),
      {},
    );
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { action: "ignored", posted: false });
    assert.equal(zulip.calls.length, 0);
    assert.equal(kv.store.size, 0);
  } finally {
    zulip.restore();
  }
});

test("a trigger and a resolve inside the wait window never reach the channel as lines", async () => {
  const kv = fakeKV();
  const zulip = stubZulip();
  try {
    const env = envFor(kv);
    const headers = { "x-pagerduty-token": "header-fixture" };
    await handler.fetch(post(incident("triggered"), headers), env, {});
    const response = await handler.fetch(post(incident("resolved"), headers), env, {});
    assert.equal((await response.json()).action, "discard");
    // Only the standing block was written:  one POST creating it, no alert line.
    const posts = zulip.calls.filter((call) => call.method === "POST");
    assert.equal(posts.length, 1);
  } finally {
    zulip.restore();
  }
});

test("a closed incident is pruned:  no state, no row, only a tombstone", async () => {
  const kv = fakeKV();
  const zulip = stubZulip();
  try {
    const env = envFor(kv);
    const headers = { "x-pagerduty-token": "header-fixture" };
    await handler.fetch(post(incident("triggered"), headers), env, {});
    // The incident aged past the wait window and was announced by the sweep.
    const key = "pagerduty:q094ck9dag3rye";
    const open = JSON.parse(kv.store.get(stateKey(key)));
    kv.store.set(stateKey(key), JSON.stringify({ ...open, phase: "alerting", since: 0 }));
    const response = await handler.fetch(post(incident("resolved"), headers), env, {});
    const answer = await response.json();
    assert.equal(answer.action, "resolve");
    const left = [...kv.store.keys()].filter((name) => name.includes(key));
    assert.deepEqual(left, [`closed:${key}`]);
    // The standing block no longer lists it.
    assert.doesNotMatch(answer.status, /BotFleet production error spike/);
  } finally {
    zulip.restore();
  }
});

test("after a close, a late trigger is ignored and a reopen revives the incident", async () => {
  const kv = fakeKV();
  const zulip = stubZulip();
  try {
    const env = envFor(kv);
    const headers = { "x-pagerduty-token": "header-fixture" };
    const key = "pagerduty:q094ck9dag3rye";
    await handler.fetch(post(incident("triggered"), headers), env, {});
    await handler.fetch(post(incident("resolved"), headers), env, {});
    assert.ok(kv.store.has(`closed:${key}`));
    const late = await handler.fetch(post(incident("triggered"), headers), env, {});
    assert.equal((await late.json()).action, "ignored");
    assert.equal(kv.store.has(stateKey(key)), false);
    const reopen = incident("triggered");
    reopen.event.event_type = "incident.reopened";
    const revived = await handler.fetch(post(reopen, headers), env, {});
    assert.equal((await revived.json()).action, "hold");
    assert.equal(kv.store.has(`closed:${key}`), false);
    assert.equal(JSON.parse(kv.store.get(stateKey(key))).phase, "pending");
  } finally {
    zulip.restore();
  }
});
