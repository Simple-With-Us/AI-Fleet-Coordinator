// Dedicated coverage for the nine hardening changes made after the first
// review round of AFC-351, one test per finding, plus the narrowing test that
// two answered review threads promised.
//
// Every test here names the finding it pins, so a failure says which behaviour
// regressed rather than which line moved.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import { createHmac } from "node:crypto";
import assert from "node:assert/strict";
import { __testing } from "./index.js";
import { fromSentry } from "./normalize.js";

const sentrySig = (body, secret = "fixture") =>
  createHmac("sha256", secret).update(body).digest("hex");

const {
  handleEvent,
  refreshStatus,
  runSweep,
  collectStates,
  sourcesForTopic,
  credentialSlug,
  missingCredentialNames,
  statusRevision,
  stateKey,
  srcKey,
  svcKey,
  topicKey,
  indexPrefix,
  statusKey,
  statusOwnerKey,
  deliveryKey,
  deliveryBarrierKey,
  handler,
} = __testing;

/**
 * Minimal in-memory stand-in for the Workers KV binding.
 *
 * `throwOnGet` makes one key fail on read, which is how a single failing
 * incident is simulated without touching the production code.
 */
function fakeKV(seed = {}, options = {}) {
  const store = new Map(Object.entries(seed));
  return {
    store,
    async get(key, type) {
      if (options.throwOnGet?.(key)) throw new Error(`kv read failed for ${key}`);
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

const CREDENTIALS = {
  ZULIP_BOT_EMAIL: "generic-bot@example.invalid",
  ZULIP_BOT_API_KEY: "generic-key",
  ZULIP_BOT_EMAIL_DATADOG: "datadog-bot@example.invalid",
  ZULIP_BOT_API_KEY_DATADOG: "datadog-key",
  ZULIP_BOT_EMAIL_BETTERSTACK: "betterstack-bot@example.invalid",
  ZULIP_BOT_API_KEY_BETTERSTACK: "betterstack-key",
  ZULIP_BOT_EMAIL_SENTRY: "sentry-bot@example.invalid",
  ZULIP_BOT_API_KEY_SENTRY: "sentry-key",
};

function envFor(kv, extra = {}) {
  return {
    ALERT_STATE: kv,
    WRITER_STORAGE: fakeStorage(),
    INGRESS_SHARED_TOKEN: "fixture",
    DATADOG_WEBHOOK_SECRET: "fixture",
    SENTRY_WEBHOOK_SECRET: "fixture",
    ...CREDENTIALS,
    ...extra,
  };
}

/**
 * Stub the Zulip API.
 *
 * Every call records the credential it used, so a test can prove which
 * identity wrote.  A refusal may carry a `retry-after` hint and a status
 * code, which is what the delivery failure has to report.
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
    if (behaviour.refuseOn?.(call)) {
      const headers = behaviour.retryAfter
        ? {
            get: (name) =>
              String(name).toLowerCase() === "retry-after"
                ? String(behaviour.retryAfter)
                : null,
          }
        : undefined;
      return { ok: false, status: behaviour.refuseStatus || 403, headers };
    }
    if (opts.method === "POST") {
      // A POST that answers without an id is the lost-acknowledgement case:
      // the message may have landed and nobody knows its id.
      if (behaviour.noIdOnPost?.(call)) {
        return { ok: true, json: async () => ({ result: "success" }) };
      }
      return { ok: true, json: async () => ({ result: "success", id: nextId++ }) };
    }
    return { ok: true, json: async () => ({ result: "success" }) };
  };
  return {
    calls,
    posts: () => calls.filter((call) => call.method === "POST"),
    patches: () => calls.filter((call) => call.method === "PATCH"),
    statusWrites: () =>
      calls.filter(
        (call) =>
          call.method === "PATCH" ||
          (call.form && call.form.get("content").startsWith("**Status:")),
      ),
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

/**
 * The durable delivery row of one source's standing status.
 *
 * The row is scoped to the revision of the content it carries, so the key is
 * found by its prefix rather than built exactly.
 */
function deliveryRow(storage, source, channel, topic) {
  const prefix = `delivery:${JSON.stringify([source, channel, topic])}`.replace(
    /\]$/,
    "",
  );
  for (const [key, value] of storage.data) {
    if (key.startsWith(prefix)) return value;
  }
  return undefined;
}

/**
 * Every delivery row of one source's standing status, one per revision.
 *
 * A source whose standing block changed twice carries two rows, so a test that
 * looks for a row has to say which revision it wants.
 */
function deliveryRows(storage, source, channel, topic) {
  const prefix = `delivery:${JSON.stringify([source, channel, topic])}`.replace(
    /\]$/,
    "",
  );
  const rows = [];
  for (const [key, value] of storage.data) {
    if (key.startsWith(prefix)) rows.push(value);
  }
  return rows;
}

const pendingState = (overrides = {}) => ({
  phase: "pending",
  severity: "MODERATE",
  since: Date.now() - 60 * 60 * 1000,
  updated: Date.now() - 60 * 60 * 1000,
  text: "load high",
  link: "",
  ...overrides,
});

// ---------------------------------------------------------------------------
// F1: the failure names the variables an operator has to configure.
// ---------------------------------------------------------------------------

test("F1 the missing credential names name every variable the source still needs", async () => {
  const env = {
    ZULIP_BOT_EMAIL: "generic-bot@example.invalid",
    ZULIP_BOT_API_KEY: "generic-key",
  };
  assert.deepEqual(missingCredentialNames(env, "datadog"), [
    "ZULIP_BOT_EMAIL_DATADOG",
    "ZULIP_BOT_API_KEY_DATADOG",
  ]);
  // A half-configured pair names only the half that is absent.
  assert.deepEqual(
    missingCredentialNames(
      { ...env, ZULIP_BOT_EMAIL_DATADOG: "datadog-bot@example.invalid" },
      "datadog",
    ),
    ["ZULIP_BOT_API_KEY_DATADOG"],
  );
  // A blank value counts as absent, not as configured.
  assert.deepEqual(
    missingCredentialNames(
      { ...env, ZULIP_BOT_EMAIL_DATADOG: "   " },
      "datadog",
    ),
    ["ZULIP_BOT_EMAIL_DATADOG", "ZULIP_BOT_API_KEY_DATADOG"],
  );
  // The generic source owns two candidate pairs: the generic one is preferred
  // and the legacy un-suffixed pair is its second.  The list covers every
  // candidate, so the preferred pair is named while it is absent.
  assert.deepEqual(missingCredentialNames(env, "generic"), [
    "ZULIP_BOT_EMAIL_GENERIC",
    "ZULIP_BOT_API_KEY_GENERIC",
  ]);
  // With both candidates present, nothing is missing.
  assert.deepEqual(
    missingCredentialNames(
      {
        ...env,
        ZULIP_BOT_EMAIL_GENERIC: "generic-bot@example.invalid",
        ZULIP_BOT_API_KEY_GENERIC: "generic-key",
      },
      "generic",
    ),
    [],
  );
});

test("F1 the webhook route names the variables that are missing", async () => {
  const kv = fakeKV();
  const env = envFor(kv, {
    ZULIP_BOT_EMAIL_DATADOG: undefined,
    ZULIP_BOT_API_KEY_DATADOG: undefined,
  });
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
    assert.deepEqual(body.missing, [
      "ZULIP_BOT_EMAIL_DATADOG",
      "ZULIP_BOT_API_KEY_DATADOG",
    ]);
    // The names are variable names, not values.
    for (const name of body.missing) {
      assert.equal(/^[A-Z0-9_]+$/.test(name), true);
    }
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

test("F1 the source-aware refresh route names the variables that are missing", async () => {
  const kv = fakeKV();
  const env = envFor(kv, {
    ZULIP_BOT_EMAIL_DATADOG: undefined,
    ZULIP_BOT_API_KEY_DATADOG: undefined,
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
    assert.equal(response.status, 503);
    const body = await response.json();
    assert.equal(body.error, "credential unavailable");
    assert.equal(body.source, "datadog");
    assert.deepEqual(body.missing, [
      "ZULIP_BOT_EMAIL_DATADOG",
      "ZULIP_BOT_API_KEY_DATADOG",
    ]);
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

// ---------------------------------------------------------------------------
// F2: a failed delivery is surfaced, never swallowed.
// ---------------------------------------------------------------------------

test("F2 a failed status delivery throws a sanitized error that carries no upstream body", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  const env = envFor(kv);
  const zulip = stubZulip({ refuseOn: () => true });
  try {
    await assert.rejects(
      () => refreshStatus(env, "datadog", "alerts", "Shared.Service"),
      /status delivery failed/,
    );
    const error = await refreshStatus(
      env,
      "datadog",
      "alerts",
      "Shared.Service",
    ).catch((caught) => caught);
    assert.equal(error.message, "status delivery failed");
    assert.equal(error.code, "status_delivery_failed");
    assert.equal(error.outcome, "refused");
    assert.equal(error.detail.code, 403);
    // The upstream answer is never echoed: the error carries a fixed set of
    // fields and nothing else.
    assert.deepEqual(Object.keys(error), [
      "code",
      "source",
      "status",
      "outcome",
      "detail",
    ]);
    assert.deepEqual(Object.keys(error.detail), ["code", "retryAfter"]);
  } finally {
    zulip.restore();
  }
});

test("F2 a failed status delivery is reported by handleEvent instead of throwing", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip({ refuseOn: (call) => isStatusWrite(call), retryAfter: 30 });
  try {
    const result = await handleEvent("datadog", datadogEvent, env);
    // The event was accepted, so the webhook does not fail over the standing
    // block; the caller is told the standing block is stale instead.
    assert.equal(result.action, "hold");
    assert.equal(result.posted, false);
    assert.equal(result.statusDelivery.outcome, "refused");
    assert.equal(result.statusDelivery.code, 403);
    assert.equal(result.statusDelivery.retryAfter, 30);
  } finally {
    zulip.restore();
  }
});

test("F2 the source-aware refresh route answers 502 when the standing block cannot be written", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip({ refuseOn: () => true });
  try {
    const response = await handler.fetch(
      new Request("https://test/status/datadog/alerts/Shared.Service", {
        headers: { authorization: "Bearer fixture" },
      }),
      env,
      {},
    );
    assert.equal(response.status, 502);
    const body = await response.json();
    assert.equal(body.error, "status delivery failed");
    assert.equal(body.source, "datadog");
    assert.equal(body.outcome, "refused");
  } finally {
    zulip.restore();
  }
});

// ---------------------------------------------------------------------------
// F3: only an unknown outcome blocks a repeat; a refusal is released.
// ---------------------------------------------------------------------------

test("F3 a refused status delivery is released and a later attempt proceeds", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  const storage = fakeStorage();
  const env = envFor(kv, { WRITER_STORAGE: storage });
  const refusing = stubZulip({ refuseOn: () => true });
  try {
    await assert.rejects(
      () => refreshStatus(env, "datadog", "alerts", "Shared.Service"),
      /status delivery failed/,
    );
  } finally {
    refusing.restore();
  }

  const row = deliveryRow(storage, "datadog", "alerts", "Shared.Service");
  assert.equal(row.outcome, "refused");
  assert.equal(row.released, true);
  assert.equal(row.code, 403);

  // The refusal was definitive, so nothing was delivered and a later attempt
  // is allowed to try again.
  const working = stubZulip();
  try {
    const before = working.statusWrites().length;
    await refreshStatus(env, "datadog", "alerts", "Shared.Service");
    assert.equal(working.statusWrites().length, before + 1);
    const after = deliveryRow(storage, "datadog", "alerts", "Shared.Service");
    assert.equal(after.outcome, "ok");
  } finally {
    working.restore();
  }
});

test("F3 an unknown status delivery holds the next attempt, because it may have landed", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  const storage = fakeStorage();
  const env = envFor(kv, { WRITER_STORAGE: storage });
  const losing = stubZulip({ throwOn: () => true });
  try {
    await assert.rejects(
      () => refreshStatus(env, "datadog", "alerts", "Shared.Service"),
      /status delivery failed/,
    );
  } finally {
    losing.restore();
  }

  const held = deliveryRow(storage, "datadog", "alerts", "Shared.Service");
  assert.equal(held.outcome, "unknown");
  assert.equal(held.released, undefined);

  // The write may have landed, so it is not repeated blindly, and the caller
  // is told the standing block is not known to be current instead of
  // reading a successful refresh.
  const working = stubZulip();
  try {
    const before = working.calls.length;
    await assert.rejects(
      () => refreshStatus(env, "datadog", "alerts", "Shared.Service"),
      /status delivery failed/,
    );
    assert.equal(working.calls.length, before);
  } finally {
    working.restore();
  }
});

// ---------------------------------------------------------------------------
// F4: the delivery outcome is scoped to a content revision.
// ---------------------------------------------------------------------------

test("F4 the revision digest is stable for the same rows and changes with them", async () => {
  const rows = [
    { phase: "alerting", severity: "MODERATE", service: "a", text: "x", link: "" },
    { phase: "ok", severity: "INFO", service: "b", text: "y", link: "l" },
  ];
  const first = await statusRevision(rows);
  // The same rows in another order are the same standing block.
  const reordered = await statusRevision([rows[1], rows[0]]);
  assert.equal(first, reordered);
  // A changed row is a new standing block.
  const changed = await statusRevision([
    { ...rows[0], text: "changed" },
    rows[1],
  ]);
  assert.notEqual(first, changed);
  // The delivery key carries the revision, so two revisions never share a row.
  assert.notEqual(
    deliveryKey("datadog", "alerts", "Shared.Service", first),
    deliveryKey("datadog", "alerts", "Shared.Service", changed),
  );
});

test("F4 a held delivery holds the whole topic, across revisions", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  const storage = fakeStorage();
  const env = envFor(kv, { WRITER_STORAGE: storage });
  const losing = stubZulip({ throwOn: (call) => isStatusWrite(call) });
  try {
    await assert.rejects(
      () => refreshStatus(env, "datadog", "alerts", "Shared.Service"),
      /status delivery failed/,
    );
  } finally {
    losing.restore();
  }
  const held = deliveryRow(storage, "datadog", "alerts", "Shared.Service");
  assert.equal(held.outcome, "unknown");

  // The standing block changes, so it is a new revision and a new delivery
  // identity.  The hold is scoped to the source, channel and topic, not to
  // the revision, so the new revision must not post a second standing
  // message on top of the one that may already exist.
  const state = JSON.parse(kv.store.get(stateKey("datadog:1")));
  kv.store.set(
    stateKey("datadog:1"),
    JSON.stringify({ ...state, text: "load high again" }),
  );

  const working = stubZulip();
  try {
    await assert.rejects(
      () => refreshStatus(env, "datadog", "alerts", "Shared.Service"),
      /status delivery failed/,
    );
    assert.equal(working.statusWrites().length, 0);
    // The source still carries one row, the held revision.  A second row
    // would mean a second delivery had been started.
    const rows = deliveryRows(storage, "datadog", "alerts", "Shared.Service");
    assert.equal(rows.length, 1);
    assert.equal(rows[0].outcome, "unknown");
  } finally {
    working.restore();
  }
});

test("F4 an operator reconciliation releases a held topic", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  const storage = fakeStorage();
  const env = envFor(kv, { WRITER_STORAGE: storage });
  const losing = stubZulip({ throwOn: (call) => isStatusWrite(call) });
  try {
    await assert.rejects(
      () => refreshStatus(env, "datadog", "alerts", "Shared.Service"),
      /status delivery failed/,
    );
  } finally {
    losing.restore();
  }

  // The operator names the message the lost write created, and the worker
  // adopts it, claims it for this source and releases the barrier.
  const working = stubZulip();
  try {
    const response = await handler.fetch(
      new Request(
        "https://test/status/datadog/alerts/Shared.Service?reconcile=4242",
        { headers: { authorization: "Bearer fixture" } },
      ),
      env,
      {},
    );
    assert.equal(response.status, 200);
    // The standing message was adopted and edited in place, not reposted.
    assert.equal(working.posts().length, 0);
    assert.equal(working.patches().length, 1);
    const id = Number(kv.store.get(statusKey("datadog", "alerts", "Shared.Service")));
    assert.equal(id, 4242);
    assert.equal(
      kv.store.get(statusOwnerKey(4242)),
      JSON.stringify("datadog"),
    );

    // The topic is live again: a later refresh writes and reports ok.
    const content = await refreshStatus(
      env,
      "datadog",
      "alerts",
      "Shared.Service",
    );
    assert.match(content, /Status: Shared\.Service/);
    const row = deliveryRow(storage, "datadog", "alerts", "Shared.Service");
    assert.equal(row.outcome, "ok");
  } finally {
    working.restore();
  }
});

// ---------------------------------------------------------------------------
// F5: one failing item does not abort the sweep.
// ---------------------------------------------------------------------------

test("F5 one failing incident does not abort the rest of the sweep", async () => {
  const kv = fakeKV(
    {},
    { throwOnGet: (key) => key === srcKey("datadog:broken") },
  );
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:broken",
    service: "Shared.Service",
    state: pendingState(),
  });
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:healthy",
    service: "Shared.Service",
    state: pendingState(),
  });
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    const result = await runSweep(env);
    // The healthy incident was promoted; the broken one was skipped.
    assert.equal(result.promoted, 1);
    assert.equal(
      JSON.parse(kv.store.get(stateKey("datadog:healthy"))).phase,
      "alerting",
    );
    assert.equal(
      JSON.parse(kv.store.get(stateKey("datadog:broken"))).phase,
      "pending",
    );
  } finally {
    zulip.restore();
  }
});

// ---------------------------------------------------------------------------
// F8: a pre-routing membership marker still finds its incident.
// ---------------------------------------------------------------------------

test("F8 a pre-routing membership marker puts the incident in its owner's block", async () => {
  const kv = fakeKV();
  const key = "datadog:22024797";
  kv.store.set(stateKey(key), JSON.stringify(pendingState()));
  kv.store.set(svcKey(key), JSON.stringify("Shared.Service"));
  kv.store.set(srcKey(key), JSON.stringify("datadog"));
  // The pre-routing marker names the topic but carries no source.
  kv.store.set("idx:alerts:Shared.Service:" + key, "1");

  const env = envFor(kv);
  const owned = await collectStates(env, "datadog", "alerts", "Shared.Service");
  assert.equal(owned.length, 1);
  assert.equal(owned[0].key, key);
  assert.equal(owned[0].source, "datadog");
  // Another source does not inherit the incident.
  const other = await collectStates(env, "generic", "alerts", "Shared.Service");
  assert.equal(other.length, 0);
});

test("F8 a pre-routing topic marker yields the source that owns the incident", async () => {
  const kv = fakeKV();
  const key = "datadog:22024797";
  kv.store.set(stateKey(key), JSON.stringify(pendingState()));
  kv.store.set(svcKey(key), JSON.stringify("Shared.Service"));
  kv.store.set(srcKey(key), JSON.stringify("datadog"));
  kv.store.set("idx:alerts:Shared.Service:" + key, "1");
  kv.store.set("topic:alerts:Shared.Service", "1");

  const env = envFor(kv);
  const sources = await sourcesForTopic(env, "alerts", "Shared.Service");
  assert.deepEqual(sources, ["datadog"]);
});

test("F8 an incident carrying both marker forms appears once", async () => {
  const kv = fakeKV();
  const key = "datadog:22024797";
  kv.store.set(stateKey(key), JSON.stringify(pendingState()));
  kv.store.set(svcKey(key), JSON.stringify("Shared.Service"));
  kv.store.set(srcKey(key), JSON.stringify("datadog"));
  kv.store.set("idx:alerts:Shared.Service:" + key, "1");
  kv.store.set(indexPrefix("datadog", "alerts", "Shared.Service") + key, "1");

  const env = envFor(kv);
  const rows = await collectStates(env, "datadog", "alerts", "Shared.Service");
  assert.equal(rows.length, 1);
});

// ---------------------------------------------------------------------------
// F9: the credential slug is injective in the source name.
// ---------------------------------------------------------------------------

test("F9 the credential slug separates source names that used to collide", async () => {
  assert.notEqual(credentialSlug("acme-eu"), credentialSlug("acme_eu"));
  assert.notEqual(credentialSlug("acme.eu"), credentialSlug("acme_eu"));
  assert.notEqual(credentialSlug("acme eu"), credentialSlug("acme_eu"));
  // The three built-in sources keep the slugs they already had.
  assert.equal(credentialSlug("datadog"), "DATADOG");
  assert.equal(credentialSlug("betterstack"), "BETTERSTACK");
  assert.equal(credentialSlug("generic"), "GENERIC");
  // The encoding is fixed width, so it is uniquely decodable.
  assert.equal(credentialSlug("acme-eu"), "ACME_2DEU");
  assert.equal(credentialSlug("acme_eu"), "ACME_5FEU");
});

test("F9 two sources that used to share a slug no longer share a credential", async () => {
  const env = {
    ZULIP_BOT_EMAIL: "generic-bot@example.invalid",
    ZULIP_BOT_API_KEY: "generic-key",
    ZULIP_BOT_EMAIL_ACME_2DEU: "dash-bot@example.invalid",
    ZULIP_BOT_API_KEY_ACME_2DEU: "dash-key",
    ZULIP_BOT_EMAIL_ACME_5FEU: "underscore-bot@example.invalid",
    ZULIP_BOT_API_KEY_ACME_5FEU: "underscore-key",
  };
  const kv = fakeKV();
  const resolved = envFor(kv, env);
  const { resolveCredential } = __testing;
  assert.equal(resolveCredential(resolved, "acme-eu").email, "dash-bot@example.invalid");
  assert.equal(
    resolveCredential(resolved, "acme_eu").email,
    "underscore-bot@example.invalid",
  );
});

// ---------------------------------------------------------------------------
// F10: the legacy refresh route reports every source it could not refresh.
// ---------------------------------------------------------------------------

test("F10 the legacy refresh route refreshes every source that tracks the topic", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  seedSignal(kv, {
    source: "betterstack",
    key: "betterstack:2",
    service: "Shared.Service",
    state: pendingState(),
  });
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      new Request("https://test/status/alerts/Shared.Service", {
        headers: { authorization: "Bearer fixture" },
      }),
      env,
      {},
    );
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.deepEqual(Object.keys(body.sources).sort(), ["betterstack", "datadog"]);
    assert.deepEqual(body.unavailable, []);
    assert.deepEqual(body.failed, []);
    // Each vendor wrote its own standing block, with its own identity.
    const identities = new Set(zulip.statusWrites().map((call) => call.email));
    assert.deepEqual(
      [...identities].sort(),
      ["betterstack-bot@example.invalid", "datadog-bot@example.invalid"],
    );
  } finally {
    zulip.restore();
  }
});

test("F10 the legacy refresh route names the source it could not refresh", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  seedSignal(kv, {
    source: "betterstack",
    key: "betterstack:2",
    service: "Shared.Service",
    state: pendingState(),
  });
  const env = envFor(kv, {
    ZULIP_BOT_EMAIL_BETTERSTACK: undefined,
    ZULIP_BOT_API_KEY_BETTERSTACK: undefined,
  });
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      new Request("https://test/status/alerts/Shared.Service", {
        headers: { authorization: "Bearer fixture" },
      }),
      env,
      {},
    );
    assert.equal(response.status, 200);
    const body = await response.json();
    // The vendor with no credential is reported, and the other one still ran.
    assert.deepEqual(body.unavailable, [
      { source: "betterstack", error: "credential unavailable" },
    ]);
    assert.deepEqual(Object.keys(body.sources), ["datadog"]);
  } finally {
    zulip.restore();
  }
});

test("F10 the legacy refresh route reports a source whose delivery failed and keeps the others", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  seedSignal(kv, {
    source: "betterstack",
    key: "betterstack:2",
    service: "Shared.Service",
    state: pendingState(),
  });
  const env = envFor(kv);
  const zulip = stubZulip({
    refuseOn: (call) => call.email === "datadog-bot@example.invalid",
  });
  try {
    const response = await handler.fetch(
      new Request("https://test/status/alerts/Shared.Service", {
        headers: { authorization: "Bearer fixture" },
      }),
      env,
      {},
    );
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.deepEqual(body.failed, [
      {
        source: "datadog",
        error: "status delivery failed",
        outcome: "refused",
      },
    ]);
    assert.deepEqual(Object.keys(body.sources), ["betterstack"]);
  } finally {
    zulip.restore();
  }
});

// ---------------------------------------------------------------------------
// F11: a malformed percent-escape is a bad request, not a server error.
// ---------------------------------------------------------------------------

test("F11 a malformed percent-escape is a 400 on both refresh routes", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    for (const path of [
      "/status/alerts/%E0%A4%A",
      "/status/datadog/alerts/%E0%A4%A",
    ]) {
      const response = await handler.fetch(
        new Request(`https://test${path}`, {
          headers: { authorization: "Bearer fixture" },
        }),
        env,
        {},
      );
      assert.equal(response.status, 400);
      const body = await response.json();
      assert.deepEqual(body, { error: "invalid path" });
    }
    // Nothing was written.
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

// ---------------------------------------------------------------------------
// The narrowing test two answered review threads promised.
// ---------------------------------------------------------------------------

test("an unknown webhook field never reaches persistence or the channel", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  const marker = "DO-NOT-PERSIST-9f3a";
  try {
    const payload = {
      ...datadogEvent,
      secret_note: marker,
      unknown_label: marker,
    };
    const response = await handler.fetch(
      new Request("https://test/webhook/datadog", {
        method: "POST",
        headers: { "x-datadog-token": "fixture" },
        body: JSON.stringify(payload),
      }),
      env,
      {},
    );
    assert.equal(response.status, 200);

    // The persisted state record holds only the known fields.
    const stored = JSON.parse(kv.store.get(stateKey("datadog:22024797")));
    assert.deepEqual(Object.keys(stored).sort(), [
      "link",
      "phase",
      "prodDown",
      "severity",
      "since",
      "text",
      "transitionRevision",
      "updated",
    ]);

    // The unknown field is nowhere in the store.
    for (const [key, value] of kv.store) {
      assert.equal(String(value).includes(marker), false, `leaked into ${key}`);
    }
    // And nowhere in what was sent to the channel.
    for (const call of zulip.calls) {
      const body = call.form ? call.form.toString() : "";
      assert.equal(body.includes(marker), false);
    }
  } finally {
    zulip.restore();
  }
});

test("an unknown webhook field the validator cannot type is refused at the route", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    // A field the parser does not know and the validator cannot type is
    // refused before it reaches anything, which is a stronger guarantee than
    // dropping it.
    for (const extra of [{ injected: { nested: "value" } }, { unknown_numeric: 4242 }]) {
      const response = await handler.fetch(
        new Request("https://test/webhook/datadog", {
          method: "POST",
          headers: { "x-datadog-token": "fixture" },
          body: JSON.stringify({ ...datadogEvent, ...extra }),
        }),
        env,
        {},
      );
      assert.equal(response.status, 400);
      const body = await response.json();
      assert.equal(body.error, "invalid payload");
    }
    // Nothing was written and nothing was sent.
    assert.equal(kv.store.size, 0);
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

test("a generic route's payload cannot claim another source's namespace", async () => {
  const kv = fakeKV();
  const env = envFor(kv, {
    ZULIP_BOT_EMAIL_OTHER: "other-bot@example.invalid",
    ZULIP_BOT_API_KEY_OTHER: "other-key",
  });
  const zulip = stubZulip();
  try {
    const payload = {
      source: "datadog",
      id: "22024797",
      service: "Shared.Service",
      status: "triggered",
      severity: "MODERATE",
      summary: "load high",
    };
    const response = await handler.fetch(
      new Request("https://test/webhook/other", {
        method: "POST",
        headers: { authorization: "Bearer fixture" },
        body: JSON.stringify(payload),
      }),
      env,
      {},
    );
    assert.equal(response.status, 200);
    // The route name is the only authority for the state namespace.
    assert.equal(kv.store.has(stateKey("datadog:22024797")), false);
    assert.equal(kv.store.has(stateKey("other:22024797")), true);
  } finally {
    zulip.restore();
  }
});

// ---------------------------------------------------------------------------
// AFC-471: the Sentry source, the topic-scoped uncertainty barrier, the
// positive owner match, and an unconfirmed write that is reported as one.
// ---------------------------------------------------------------------------

const SENTRY_ISSUE_PAYLOAD = {
  action: "created",
  data: {
    issue: {
      id: "1234567",
      shortId: "PROJ-1",
      title: "TypeError: cannot read properties of undefined",
      level: "error",
      url: "https://sentry.io/organizations/example/issues/1234567/",
      culprit: "app/orders.js",
      project: { id: 42, slug: "orders", name: "Orders" },
      metadata: { type: "TypeError", value: "cannot read properties" },
      tags: [{ key: "environment", value: "production" }],
    },
  },
  actor: { id: 1, name: "someone", type: "user" },
  installation: { id: 9, uuid: "abc" },
};

const SENTRY_METRIC_PAYLOAD = {
  action: "created",
  data: {
    metric_alert: {
      id: "5678",
      title: "Error rate above five percent",
      status: "critical",
      url: "https://sentry.io/organizations/example/alerts/5678/",
      alert_rule: { id: "12", name: "Error rate", status: "critical" },
      metric_identifier: { name: "error_rate", namespace: "custom" },
    },
  },
};

test("S1 a Sentry issue payload becomes one incident with the fleet severities", () => {
  const event = fromSentry(SENTRY_ISSUE_PAYLOAD);
  assert.equal(event.source, "sentry");
  assert.equal(event.key, "sentry:proj-1");
  assert.equal(event.service, "orders");
  assert.equal(event.severity, "SEVERE");
  assert.equal(event.firing, true);
  assert.match(event.link, /sentry\.io/);
});

test("S1 a Sentry metric alert has its own incident identity", () => {
  const event = fromSentry(SENTRY_METRIC_PAYLOAD);
  assert.equal(event.key, "sentry:metric:5678");
  assert.equal(event.service, "Error rate");
  assert.equal(event.severity, "SEVERE");
  assert.equal(event.firing, true);
});

test("S1 a Sentry resolution closes its incident", () => {
  const event = fromSentry({ ...SENTRY_ISSUE_PAYLOAD, action: "resolved" });
  assert.equal(event.firing, false);
  assert.equal(event.severity, "INFO");
});

test("S1 a Sentry issue webhook is accepted and posts under the Sentry identity", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      new Request("https://test/webhook/sentry", {
        method: "POST",
        headers: {
          "sentry-hook-signature": sentrySig(JSON.stringify(SENTRY_ISSUE_PAYLOAD)),
        },
        body: JSON.stringify(SENTRY_ISSUE_PAYLOAD),
      }),
      env,
      {},
    );
    assert.equal(response.status, 200);
    // The incident lives in the Sentry namespace, under the project slug.
    const state = JSON.parse(kv.store.get(stateKey("sentry:proj-1")));
    assert.equal(state.phase, "pending");
    assert.equal(state.severity, "SEVERE");
    assert.equal(
      JSON.parse(kv.store.get(svcKey("sentry:proj-1"))),
      "orders",
    );
    // The standing status was written with the Sentry identity and no other.
    const standing = zulip.statusWrites();
    assert.equal(standing.length, 1);
    assert.equal(standing[0].email, "sentry-bot@example.invalid");
  } finally {
    zulip.restore();
  }
});

test("S1 a Sentry webhook without its own secret is refused", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      new Request("https://test/webhook/sentry", {
        method: "POST",
        headers: {
          "sentry-hook-signature": sentrySig(JSON.stringify(SENTRY_ISSUE_PAYLOAD), "wrong"),
        },
        body: JSON.stringify(SENTRY_ISSUE_PAYLOAD),
      }),
      env,
      {},
    );
    assert.equal(response.status, 401);
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

test("S1 a Sentry route with no secret of its own does not fall back to the shared token", async () => {
  const kv = fakeKV();
  const env = envFor(kv, { SENTRY_WEBHOOK_SECRET: undefined });
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      new Request("https://test/webhook/sentry", {
        method: "POST",
        headers: { authorization: "Bearer fixture" },
        body: JSON.stringify(SENTRY_ISSUE_PAYLOAD),
      }),
      env,
      {},
    );
    // The shared bearer token is not a Sentry credential.
    assert.equal(response.status, 401);
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

test("S1 a generic route still refuses a native Sentry body", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    const response = await handler.fetch(
      new Request("https://test/webhook/generic", {
        method: "POST",
        headers: { authorization: "Bearer fixture" },
        body: JSON.stringify(SENTRY_ISSUE_PAYLOAD),
      }),
      env,
      {},
    );
    // The generic boundary rule is unchanged for every other source.
    assert.equal(response.status, 400);
  } finally {
    zulip.restore();
  }
});

test("B1 a held topic does not block another source in the same topic", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  seedSignal(kv, {
    source: "betterstack",
    key: "betterstack:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  const storage = fakeStorage();
  const env = envFor(kv, { WRITER_STORAGE: storage });
  const losing = stubZulip({
    throwOn: (call) => call.email === "datadog-bot@example.invalid" && isStatusWrite(call),
  });
  try {
    await assert.rejects(
      () => refreshStatus(env, "datadog", "alerts", "Shared.Service"),
      /status delivery failed/,
    );
  } finally {
    losing.restore();
  }

  // The barrier belongs to the source that lost the write, so the other
  // source writes its own standing message in the same topic.
  const working = stubZulip();
  try {
    await refreshStatus(env, "betterstack", "alerts", "Shared.Service");
    const other = deliveryRow(storage, "betterstack", "alerts", "Shared.Service");
    assert.equal(other.outcome, "ok");
  } finally {
    working.restore();
  }
});

test("B2 a standing message no source has claimed is neither adopted nor duplicated", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  // The standing message id is recorded, but no source has claimed it, which
  // is what a write that landed and lost its acknowledgement leaves behind.
  kv.store.set(
    statusKey("datadog", "alerts", "Shared.Service"),
    JSON.stringify(999),
  );
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    await assert.rejects(
      () => refreshStatus(env, "datadog", "alerts", "Shared.Service"),
      /standing status held/,
    );
    // Neither edited nor posted over it.
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

test("B3 a standing message another source owns is not edited", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  kv.store.set(
    statusKey("datadog", "alerts", "Shared.Service"),
    JSON.stringify(999),
  );
  kv.store.set(statusOwnerKey(999), JSON.stringify("betterstack"));
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    await refreshStatus(env, "datadog", "alerts", "Shared.Service");
    // It posted a standing message of its own rather than editing the other
    // source's, and it edited nothing.
    assert.equal(zulip.patches().length, 0);
    const standing = zulip.statusWrites();
    assert.equal(standing.length, 1);
    assert.equal(standing[0].method, "POST");
    assert.equal(standing[0].email, "datadog-bot@example.invalid");
  } finally {
    zulip.restore();
  }
});

test("O1 a write that cannot be confirmed answers as a failure, not a refresh", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  const env = envFor(kv);
  // The POST answers without an id, so the message may have landed and
  // nobody knows which one.
  const zulip = stubZulip({ noIdOnPost: () => true });
  try {
    const response = await handler.fetch(
      new Request("https://test/status/datadog/alerts/Shared.Service", {
        headers: { authorization: "Bearer fixture" },
      }),
      env,
      {},
    );
    assert.equal(response.status, 502);
    const body = await response.json();
    assert.equal(body.outcome, "unknown");
  } finally {
    zulip.restore();
  }
});

test("S1 a vendor-controlled level cannot read the severity table's prototype", () => {
  const event = fromSentry({
    action: "created",
    data: { issue: { shortId: "PROJ-9", level: "constructor", title: "t" } },
  });
  assert.equal(event.severity, "SEVERE");
});

test("S1 housekeeping actions do not reopen a resolved incident", () => {
  for (const action of ["assigned", "archived", "ignored"]) {
    const event = fromSentry({
      action,
      data: { issue: { shortId: "PROJ-9", level: "resolved", title: "t" } },
    });
    assert.equal(event.firing, false, action);
  }
  const live = fromSentry({
    action: "created",
    data: { issue: { shortId: "PROJ-9", level: "error", title: "t" } },
  });
  assert.equal(live.firing, true);
});

test("S1 an oversized or too-wide Sentry body is refused before it is parsed", async () => {
  const kv = fakeKV();
  const env = envFor(kv);
  const zulip = stubZulip();
  try {
    const body = JSON.stringify({ pad: "x".repeat(300 * 1024) });
    const response = await handler.fetch(
      new Request("https://test/webhook/sentry", {
        method: "POST",
        headers: { "sentry-hook-signature": sentrySig(body) },
        body,
      }),
      env,
      {},
    );
    assert.equal(response.status, 413);
    assert.equal(zulip.calls.length, 0);
  } finally {
    zulip.restore();
  }
});

test("F4 a reconciliation cannot adopt a message another source owns", async () => {
  const kv = fakeKV();
  seedSignal(kv, {
    source: "datadog",
    key: "datadog:1",
    service: "Shared.Service",
    state: pendingState(),
  });
  kv.store.set(statusOwnerKey(4242), JSON.stringify("betterstack"));
  const storage = fakeStorage();
  const env = envFor(kv, { WRITER_STORAGE: storage });
  const working = stubZulip();
  try {
    const response = await handler.fetch(
      new Request(
        "https://test/status/datadog/alerts/Shared.Service?reconcile=4242",
        { headers: { authorization: "Bearer fixture" } },
      ),
      env,
      {},
    );
    assert.equal(response.status, 409);
    assert.equal(working.patches().length, 0);
    assert.equal(working.posts().length, 0);
    assert.equal(kv.store.get(statusOwnerKey(4242)), JSON.stringify("betterstack"));
  } finally {
    working.restore();
  }
});
