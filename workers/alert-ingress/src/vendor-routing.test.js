// Vendor routing: one credential, one state namespace and one standing
// message per source, so two vendors that share a service name can never
// edit each other's message.
//
// Run: node --test workers/alert-ingress/src/

import test from "node:test";
import assert from "node:assert/strict";
import { __testing } from "./index.js";

const {
  handleEvent,
  refreshStatus,
  runSweep,
  collectTopics,
  stateKey,
  srcKey,
  svcKey,
  topicKey,
  indexPrefix,
  statusKey,
  statusOwnerKey,
  deliveryKey,
  handler,
} = __testing;

/** Minimal in-memory stand-in for the Workers KV binding. */
function fakeKV(seed = {}) {
  const store = new Map(Object.entries(seed));
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
    async list({ prefix = "" } = {}) {
      return {
        keys: [...store.keys()]
          .filter((key) => key.startsWith(prefix))
          .map((name) => ({ name })),
        listComplete: true,
      };
    },
  };
}

/** Minimal in-memory stand-in for the Durable Object storage binding. */
function fakeStorage() {
  const data = new Map();
  return {
    data,
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

// One credential pair per source.  The generic source owns the legacy
// un-suffixed pair; the vendor sources own pairs of their own.
const CREDENTIALS = {
  ZULIP_BOT_EMAIL: "generic-bot@example.invalid",
  ZULIP_BOT_API_KEY: "generic-key",
  ZULIP_BOT_EMAIL_DATADOG: "datadog-bot@example.invalid",
  ZULIP_BOT_API_KEY_DATADOG: "datadog-key",
  ZULIP_BOT_EMAIL_BETTERSTACK: "betterstack-bot@example.invalid",
  ZULIP_BOT_API_KEY_BETTERSTACK: "betterstack-key",
};

function envFor(kv, extra = {}) {
  return {
    ALERT_STATE: kv,
    WRITER_STORAGE: fakeStorage(),
    ...CREDENTIALS,
    ...extra,
  };
}

/**
 * Stub the Zulip API.  Every call records the credential it used, so a test
 * can prove which identity wrote.
 */
function stubZulip(behaviour = {}) {
  const calls = [];
  const original = globalThis.fetch;
  let nextId = 100;
  globalThis.fetch = async (url, opts) => {
    const decoded = atob(opts.headers.Authorization.replace(/^Basic /, ""));
    const call = {
      method: opts.method,
      url: String(url),
      email: decoded.slice(0, decoded.indexOf(":")),
      apiKey: decoded.slice(decoded.indexOf(":") + 1),
      form: opts.body ? new URLSearchParams(opts.body) : null,
    };
    calls.push(call);
    if (behaviour.throwOn?.(call)) throw new Error("zulip unreachable");
    if (behaviour.refuseOn?.(call)) return { ok: false, status: 403 };
    if (opts.method === "POST")
      return { ok: true, json: async () => ({ result: "success", id: nextId++ }) };
    return { ok: true, json: async () => ({ result: "success" }) };
  };
  return {
    calls,
    posts: () => calls.filter((call) => call.method === "POST"),
    patches: () => calls.filter((call) => call.method === "PATCH"),
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

const datadogEvent = {
  monitor_id: "22024797",
  alert_type: "triggered",
  alert_transition: "System load is high",
  host: "Shared.Service",
  priority: "MODERATE",
  event_url: "https://us5.datadoghq.com/monitors/22024797",
};

const betterstackEvent = {
  data: {
    attributes: {
      check_name: "Shared.Service",
      status: "triggered",
      url: "https://uptime.betterstack.com/team/1/monitors/42",
    },
  },
};

/** A standing status write, as opposed to a one-line event post. */
const isStatusWrite = (call) =>
  call.method === "POST" && call.form.get("content").startsWith("**Status:");

/** Seed one source's tracked signal in a topic. */
function seedSignal(kv, { source, key, service, topic = service, state }) {
  kv.store.set(stateKey(key), JSON.stringify(state));
  kv.store.set(svcKey(key), JSON.stringify(service));
  kv.store.set(srcKey(key), JSON.stringify(source));
  kv.store.set(indexPrefix(source, "alerts", topic) + key, "1");
  kv.store.set(topicKey(source, "alerts", topic), "1");
}

test("two vendors on one topic keep their own standing message and credential", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    await handleEvent("datadog", datadogEvent, env);
    const datadogCalls = zulip.calls.length;
    await handleEvent("betterstack", betterstackEvent, env);

    // Every write of a vendor used that vendor's own credential.
    for (const call of zulip.calls.slice(0, datadogCalls))
      assert.equal(call.email, "datadog-bot@example.invalid");
    for (const call of zulip.calls.slice(datadogCalls))
      assert.equal(call.email, "betterstack-bot@example.invalid");

    // Each vendor has its own standing message id for the shared topic.
    const datadogStatus = Number(
      kv.store.get(statusKey("datadog", "alerts", "Shared.Service")),
    );
    const betterstackStatus = Number(
      kv.store.get(statusKey("betterstack", "alerts", "Shared.Service")),
    );
    assert.ok(datadogStatus > 0);
    assert.ok(betterstackStatus > 0);
    assert.notEqual(datadogStatus, betterstackStatus);

    // Each standing message lists only its own vendor's signals.
    const datadogContent = zulip.posts()[0].form.get("content");
    const betterstackContent = zulip.posts()[1].form.get("content");
    assert.match(datadogContent, /System load is high/);
    assert.doesNotMatch(datadogContent, /Shared\.Service is triggered/);
    assert.match(betterstackContent, /Shared\.Service is triggered/);
    assert.doesNotMatch(betterstackContent, /System load is high/);
  } finally {
    zulip.restore();
  }
});

test("a message id is never reused across vendors and each vendor edits its own", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    await handleEvent("datadog", datadogEvent, env);
    await handleEvent("betterstack", betterstackEvent, env);
    const datadogStatus = Number(
      kv.store.get(statusKey("datadog", "alerts", "Shared.Service")),
    );
    const betterstackStatus = Number(
      kv.store.get(statusKey("betterstack", "alerts", "Shared.Service")),
    );
    assert.notEqual(datadogStatus, betterstackStatus);

    await refreshStatus(env, "datadog", "alerts", "Shared.Service");
    await refreshStatus(env, "betterstack", "alerts", "Shared.Service");

    // Each refresh patched that vendor's own standing message, and no other.
    const patched = zulip
      .patches()
      .map((call) => call.url.split("/").pop())
      .sort();
    assert.deepEqual(patched, [String(datadogStatus), String(betterstackStatus)].sort());
  } finally {
    zulip.restore();
  }
});

test("a vendor never patches a message id another vendor pinned, and never adopts it", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: {
      phase: "alerting",
      severity: "MODERATE",
      since: 1,
      updated: 2,
      text: "load high",
      link: "",
    },
  });
  // The standing message of this topic is pinned by another vendor.
  kv.store.set(statusKey("datadog", "alerts", "Shared.Service"), JSON.stringify(500));
  kv.store.set(statusOwnerKey(500), JSON.stringify("betterstack"));

  const zulip = stubZulip();
  try {
    await refreshStatus(env, "datadog", "alerts", "Shared.Service");
    // The foreign message was never edited.
    assert.equal(
      zulip.calls.filter((call) => call.url.endsWith("/messages/500")).length,
      0,
    );
    // The vendor built its own standing message instead.
    const created = zulip.posts();
    assert.equal(created.length, 1);
    const own = Number(
      kv.store.get(statusKey("datadog", "alerts", "Shared.Service")),
    );
    assert.notEqual(own, 500);
    assert.equal(kv.store.get(statusOwnerKey(own)), JSON.stringify("datadog"));
  } finally {
    zulip.restore();
  }
});

test("the sweep promotes an aged incident with that vendor's credential", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const old = Date.now() - 60 * 60 * 1000;
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Datadog.Service",
    state: {
      phase: "pending",
      severity: "MODERATE",
      since: old,
      updated: old,
      text: "load high",
      link: "",
    },
  });
  seedSignal(kv, {
    source: "betterstack",
    key: "betterstack:2",
    service: "Better.Service",
    state: {
      phase: "pending",
      severity: "INFO",
      since: old,
      updated: old,
      text: "check down",
      link: "",
    },
  });

  const zulip = stubZulip();
  try {
    const result = await runSweep(env);
    assert.equal(result.promoted, 2);
    // Each promotion posted with its own vendor credential.
    const events = zulip.posts().filter((call) => !isStatusWrite(call));
    assert.equal(events.length, 2);
    assert.deepEqual(
      events.map((call) => call.email).sort(),
      ["betterstack-bot@example.invalid", "datadog-bot@example.invalid"],
    );
    // The datadog promotion landed in the datadog topic.
    const datadogPost = events.find(
      (call) => call.email === "datadog-bot@example.invalid",
    );
    assert.equal(datadogPost.form.get("to"), "alerts");
    assert.equal(datadogPost.form.get("topic"), "Datadog.Service");
    // Both incidents are announced now.
    assert.equal(
      JSON.parse(kv.store.get(stateKey("datadog:1"))).phase,
      "alerting",
    );
    assert.equal(
      JSON.parse(kv.store.get(stateKey("betterstack:2"))).phase,
      "alerting",
    );
  } finally {
    zulip.restore();
  }
});

test("the sweep resolves the source of a state that carries no source record", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const old = Date.now() - 60 * 60 * 1000;
  // A state written before the source was recorded: the key itself names the
  // source, and the sweep must still use that vendor's credential.
  kv.store.set(
    stateKey("datadog:9"),
    JSON.stringify({
      phase: "pending",
      severity: "MODERATE",
      since: old,
      updated: old,
      text: "load high",
      link: "",
    }),
  );
  kv.store.set(svcKey("datadog:9"), JSON.stringify("Legacy.Service"));
  kv.store.set(indexPrefix("datadog", "alerts", "Legacy.Service") + "datadog:9", "1");
  kv.store.set(topicKey("datadog", "alerts", "Legacy.Service"), "1");

  const zulip = stubZulip();
  try {
    const result = await runSweep(env);
    assert.equal(result.promoted, 1);
    const events = zulip.posts().filter((call) => !isStatusWrite(call));
    assert.equal(events.length, 1);
    assert.equal(events[0].email, "datadog-bot@example.invalid");
  } finally {
    zulip.restore();
  }
});

test("a source with no credential of its own fails closed", async () => {
  const kv = fakeKV();
  // Only the generic source has a credential here.
  const env = {
    ALERT_STATE: kv,
    WRITER_STORAGE: fakeStorage(),
    INGRESS_SHARED_TOKEN: "fixture",
    DATADOG_WEBHOOK_SECRET: "fixture",
    ZULIP_BOT_EMAIL: "generic-bot@example.invalid",
    ZULIP_BOT_API_KEY: "generic-key",
  };
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      new Request("https://test/webhook/datadog", {
        method: "POST",
        headers: { "x-datadog-token": "fixture" },
        body: JSON.stringify(datadogEvent),
      }),
      env,
      {},
    );
    assert.equal(response.status, 503);
    const body = await response.json();
    assert.equal(body.error, "credential unavailable");
    assert.equal(body.source, "datadog");
    // Nothing was posted, and no state was written.
    assert.equal(zulip.calls.length, 0);
    assert.equal(kv.store.has(stateKey("datadog:22024797")), false);
    assert.equal(kv.store.has(statusKey("datadog", "alerts", "Shared.Service")), false);
  } finally {
    zulip.restore();
  }
});

test("a refused credential is recorded and never retried as another bot", async () => {
  const kv = fakeKV();
  const storage = fakeStorage();
  const env = envFor(kv, { WRITER_STORAGE: storage });
  const zulip = stubZulip({ refuseOn: () => true });
  try {
    await handleEvent("datadog", datadogEvent, env);
    await handleEvent("datadog", datadogEvent, env);
    // The refusal is held for reconciliation, not retried.
    const outcome = storage.data.get(deliveryKey("datadog", "alerts", "Shared.Service"));
    assert.equal(outcome.outcome, "refused");
    assert.equal(zulip.calls.length, 1);
    // And it was never sent as another source's bot.
    assert.equal(zulip.calls[0].email, "datadog-bot@example.invalid");
  } finally {
    zulip.restore();
  }
});

test("the sweep leaves a source with no credential alone", async () => {
  const kv = fakeKV();
  const env = {
    ALERT_STATE: kv,
    WRITER_STORAGE: fakeStorage(),
    ZULIP_BOT_EMAIL: "generic-bot@example.invalid",
    ZULIP_BOT_API_KEY: "generic-key",
  };
  const old = Date.now() - 60 * 60 * 1000;
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Datadog.Service",
    state: {
      phase: "pending",
      severity: "MODERATE",
      since: old,
      updated: old,
      text: "load high",
      link: "",
    },
  });
  const zulip = stubZulip();
  try {
    const result = await runSweep(env);
    assert.equal(result.promoted, 0);
    assert.equal(zulip.calls.length, 0);
    // The incident stays pending until its source has a credential.
    assert.equal(JSON.parse(kv.store.get(stateKey("datadog:1"))).phase, "pending");
  } finally {
    zulip.restore();
  }
});

test("an ambiguous write for one vendor never creates a message and never blocks another", async () => {
  const kv = fakeKV();
  const storage = fakeStorage();
  const env = envFor(kv, { WRITER_STORAGE: storage });
  // The datadog edit is lost on the wire; the betterstack edit still lands.
  const zulip = stubZulip({
    throwOn: (call) =>
      call.email === "datadog-bot@example.invalid" && call.method === "PATCH",
  });
  try {
    await handleEvent("datadog", datadogEvent, env);
    await handleEvent("betterstack", betterstackEvent, env);
    const postsBefore = zulip.posts().length;

    await refreshStatus(env, "datadog", "alerts", "Shared.Service");
    const held = storage.data.get(deliveryKey("datadog", "alerts", "Shared.Service"));
    assert.equal(held.outcome, "unknown");

    // A second refresh of that vendor creates no new message and sends nothing.
    const before = zulip.calls.length;
    await refreshStatus(env, "datadog", "alerts", "Shared.Service");
    assert.equal(zulip.calls.length, before);

    // The other vendor is untouched by the held write.
    await refreshStatus(env, "betterstack", "alerts", "Shared.Service");
    const other = storage.data.get(
      deliveryKey("betterstack", "alerts", "Shared.Service"),
    );
    assert.equal(other.outcome, "ok");
    // No new standing message was created for either vendor.
    assert.equal(zulip.posts().length, postsBefore);
  } finally {
    zulip.restore();
  }
});

test("a refresh for a channel other than alerts is refused", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    const headers = { authorization: "Bearer fixture" };
    for (const path of [
      "/status/other/svc",
      "/status/datadog/other/svc",
      "/status/generic/other/svc",
    ]) {
      const response = await handler.fetch(
        new Request(`https://test${path}`, { headers }),
        { ...env, INGRESS_SHARED_TOKEN: "fixture" },
        {},
      );
      assert.equal(response.status, 403, `${path} must be refused`);
      const body = await response.json();
      assert.equal(body.error, "channel not permitted");
      assert.deepEqual(body.allowed, ["alerts"]);
    }
    // Nothing was written anywhere.
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

test("the source-aware refresh writes only into the alerts channel", async () => {
  const kv = fakeKV();
  const env = envFor(kv, { INGRESS_SHARED_TOKEN: "fixture" });
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: {
      phase: "alerting",
      severity: "MODERATE",
      since: 1,
      updated: 2,
      text: "load high",
      link: "",
    },
  });
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      new Request("https://test/status/datadog/alerts/Shared.Service", {
        headers: { authorization: "Bearer fixture" },
      }),
      env,
      {},
    );
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.source, "datadog");
    assert.equal(body.channel, "alerts");
    assert.equal(zulip.calls.length, 1);
    assert.equal(zulip.calls[0].email, "datadog-bot@example.invalid");
    assert.equal(zulip.calls[0].form.get("to"), "alerts");
    assert.equal(zulip.calls[0].form.get("topic"), "Shared.Service");
  } finally {
    zulip.restore();
  }
});

test("the two-segment and three-segment refresh routes do not match each other", async () => {
  const kv = fakeKV();
  const env = envFor(kv, { INGRESS_SHARED_TOKEN: "fixture" });
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: {
      phase: "alerting",
      severity: "MODERATE",
      since: 1,
      updated: 2,
      text: "load high",
      link: "",
    },
  });
  const zulip = stubZulip();
  try {
    const two = await handler.fetch(
      new Request("https://test/status/alerts/Shared.Service", {
        headers: { authorization: "Bearer fixture" },
      }),
      env,
      {},
    );
    assert.equal(two.status, 200);
    const twoBody = await two.json();
    assert.equal(twoBody.source, undefined);
    assert.ok(twoBody.sources.datadog);

    const three = await handler.fetch(
      new Request("https://test/status/datadog/alerts/Shared.Service", {
        headers: { authorization: "Bearer fixture" },
      }),
      env,
      {},
    );
    assert.equal(three.status, 200);
    const threeBody = await three.json();
    assert.equal(threeBody.source, "datadog");
    assert.equal(threeBody.sources, undefined);
  } finally {
    zulip.restore();
  }
});

test("an unauthenticated refresh is refused before anything is written", async () => {
  const kv = fakeKV();
  const env = envFor(kv, { INGRESS_SHARED_TOKEN: "fixture" });
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      new Request("https://test/status/datadog/alerts/Shared.Service"),
      env,
      {},
    );
    assert.equal(response.status, 401);
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

test("the machine-readable status keeps its shape and adds a per-source breakdown", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: {
      phase: "alerting",
      severity: "MODERATE",
      since: 1,
      updated: 2,
      text: "load high",
      link: "",
    },
  });
  seedSignal(kv, {
    source: "betterstack",
    key: "betterstack:2",
    service: "Shared.Service",
    state: {
      phase: "ok",
      severity: "INFO",
      since: 1,
      updated: 2,
      text: "check up",
      link: "",
    },
  });
  const collected = await collectTopics(env);
  // The existing top-level shape is unchanged.
  assert.deepEqual(Object.keys(collected.topics).sort(), ["Shared.Service"]);
  assert.equal(collected.topics["Shared.Service"].total, 2);
  assert.equal(collected.firing, 1);
  assert.equal(collected.total, 2);
  // The per-source breakdown is added beside it.
  assert.deepEqual(Object.keys(collected.sources).sort(), [
    "betterstack",
    "datadog",
  ]);
  assert.equal(collected.sources.datadog.summary.alerts, 1);
  assert.equal(collected.sources.betterstack.summary.firing, 0);
  assert.equal(
    collected.sources.datadog.topics["Shared.Service"].alerts[0].text,
    "load high",
  );
  // Every row names its source.
  for (const row of collected.topics["Shared.Service"].alerts)
    assert.ok(["datadog", "betterstack"].includes(row.source));
});
