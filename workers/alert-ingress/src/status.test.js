// The machine-readable status contract that consumers render themselves
// (today: the Usage Monitor /ops page).  If this shape changes, that page
// changes with it -- one writer, many readers.
//
// Run: node --test workers/alert-ingress/src/

import test from "node:test";
import assert from "node:assert/strict";
import { __testing } from "./index.js";

const { collectTopics, collectStates, listAll, runSweep, handleEvent, verify, stateKey, svcKey, topicKey, indexPrefix, ctStamp } = __testing;

/** Minimal in-memory stand-in for the Workers KV binding. */
function fakeKV(seed = {}) {
  const store = new Map(Object.entries(seed));
  return {
    store,
    async get(key, type) {
      const v = store.has(key) ? store.get(key) : null;
      if (v === null) return null;
      return type === "json" ? JSON.parse(v) : v;
    },
    async put(key, value) {
      store.set(key, value);
    },
    async delete(key) {
      store.delete(key);
    },
    async list({ prefix = "", cursor } = {}) {
      return {
        keys: [...store.keys()]
          .filter((k) => k.startsWith(prefix))
          .map((name) => ({ name })),
        listComplete: true,
      };
    },
  };
}

/**
 * The same binding, but paginated like the real one: at most `pageSize` keys
 * per call plus a cursor.  A caller that reads one page and stops silently
 * loses the tail.
 */
function pagedKV(pageSize = 2, seed = {}) {
  const base = fakeKV(seed);
  return {
    ...base,
    async list({ prefix = "", cursor } = {}) {
      const all = [...base.store.keys()].filter((k) => k.startsWith(prefix));
      const start = cursor ? Number(cursor) : 0;
      const slice = all.slice(start, start + pageSize);
      const next = start + pageSize;
      const more = next < all.length;
      return {
        keys: slice.map((name) => ({ name })),
        listComplete: !more,
        cursor: more ? String(next) : undefined,
      };
    },
  };
}

/** Minimal in-memory stand-in for the Durable Object storage binding. */
function fakeStorage() {
  const data = new Map();
  return {
    data,
    async get(k) {
      return data.get(k);
    },
    async put(k, v) {
      data.set(k, v);
    },
    async list({ prefix }) {
      return new Map([...data].filter(([k]) => k.startsWith(prefix)));
    },
  };
}

/** The collectors take the Workers `env`, so wrap the fake binding the same way. */
const asEnv = (kv) => ({ ALERT_STATE: kv, WRITER_STORAGE: fakeStorage() });

function seed(kv, { channel = "alerts", key, service, state }) {
  kv.store.set(stateKey(key), JSON.stringify(state));
  kv.store.set(svcKey(key), JSON.stringify(service));
  kv.store.set(indexPrefix(channel, service) + key, "1");
  kv.store.set(topicKey(channel, service), "1");
}

test("collectTopics groups states by topic and counts firing", async () => {
  const kv = fakeKV();
  seed(kv, { key: "datadog:1", service: "Mac.Jays.Services", state: { phase: "alerting", severity: "SEVERE", since: 1, updated: 2, text: "load high", link: "l1" } });
  seed(kv, { key: "datadog:2", service: "Mac.Jays.Services", state: { phase: "ok", severity: "INFO", since: 1, updated: 2, text: "cpu ok", link: "" } });
  seed(kv, { key: "bt:1", service: "Congress.Trade", state: { phase: "alerting", severity: "MODERATE", since: 1, updated: 2, text: "down", link: "l2" } });

  const { topics, firing, total } = await collectTopics(asEnv(kv));
  assert.deepEqual(Object.keys(topics).sort(), ["Congress.Trade", "Mac.Jays.Services"]);
  assert.equal(firing, 2);
  assert.equal(total, 3);
  assert.equal(topics["Mac.Jays.Services"].firing, 1);
  assert.equal(topics["Mac.Jays.Services"].total, 2);
});

test("a non-alerting state never reports a severity that looks alarming", async () => {
  const kv = fakeKV();
  // A pending incident still holds MODERATE in stored state; the contract says
  // only `alerting` may surface a non-INFO severity.
  seed(kv, { key: "k", service: "svc", state: { phase: "pending", severity: "SEVERE", since: 1, updated: 2, text: "t", link: "" } });
  const { topics } = await collectTopics(asEnv(kv));
  assert.equal(topics.svc.alerts[0].severity, "INFO");
  assert.equal(topics.svc.alerts[0].phase, "pending");
});

test("alerts are ordered firing first, then pending, then ok", async () => {
  const kv = fakeKV();
  seed(kv, { key: "a", service: "svc", state: { phase: "ok", severity: "INFO", since: 1, updated: 2, text: "fine", link: "" } });
  seed(kv, { key: "b", service: "svc", state: { phase: "pending", severity: "MODERATE", since: 1, updated: 2, text: "maybe", link: "" } });
  seed(kv, { key: "c", service: "svc", state: { phase: "alerting", severity: "SEVERE", since: 1, updated: 2, text: "bad", link: "" } });
  const { topics } = await collectTopics(asEnv(kv));
  assert.deepEqual(topics.svc.alerts.map((a) => a.phase), ["alerting", "pending", "ok"]);
});

test("the contract carries everything a consumer needs and nothing secret", async () => {
  const kv = fakeKV();
  seed(kv, { key: "datadog:9", service: "Mac.Jays.Services", state: { phase: "alerting", severity: "MODERATE", since: 111, updated: 222, text: "System load is high", link: "https://dd/x", prodDown: false } });
  const { topics } = await collectTopics(asEnv(kv));
  const alert = topics["Mac.Jays.Services"].alerts[0];

  for (const field of ["key", "service", "phase", "severity", "text", "link", "since", "updated", "prodDown"]) {
    assert.ok(field in alert, `missing ${field}`);
  }
  assert.equal(alert.severity, "MODERATE");
  assert.equal(alert.link, "https://dd/x");

  // No credential may ever reach a public status payload.
  const serialized = JSON.stringify({ topics });
  assert.ok(!/api[_-]?key|secret|token|password|authorization/i.test(serialized));
});

test("an empty ingress returns a well-formed empty payload, not an error", async () => {
  const { topics, firing, total } = await collectTopics(asEnv(fakeKV()));
  assert.deepEqual(topics, {});
  assert.equal(firing, 0);
  assert.equal(total, 0);
});

test("a topic with only healthy signals still reports, with zero firing", async () => {
  const kv = fakeKV();
  seed(kv, { key: "k", service: "Congress.Trade", state: { phase: "ok", severity: "INFO", since: 1, updated: 2, text: "up", link: "" } });
  const { topics, firing, total } = await collectTopics(asEnv(kv));
  assert.equal(topics["Congress.Trade"].firing, 0);
  assert.equal(topics["Congress.Trade"].total, 1);
  assert.equal(firing, 0);
  assert.equal(total, 1);
});

test("a dangling index entry with no stored state is skipped, not crashed on", async () => {
  const kv = fakeKV();
  kv.store.set(indexPrefix("alerts", "Ghost.Service") + "gone", "1"); // index but no state
  kv.store.set(topicKey("alerts", "Ghost.Service"), "1");
  seed(kv, { key: "real", service: "Real.Service", state: { phase: "ok", severity: "INFO", since: 1, updated: 2, text: "ok", link: "" } });
  const { topics, total } = await collectTopics(asEnv(kv));
  assert.equal(total, 1);
  assert.equal(topics["Ghost.Service"], undefined);
});

test("collectStates resolves the service label and falls back to the key", async () => {
  const kv = fakeKV();
  kv.store.set(stateKey("orphan:1"), JSON.stringify({ phase: "ok", severity: "INFO", since: 1, updated: 2, text: "t", link: "" }));
  kv.store.set(indexPrefix("alerts", "Fallback.Service") + "orphan:1", "1");
  kv.store.set(topicKey("alerts", "Fallback.Service"), "1");
  const states = await collectStates(asEnv(kv), "alerts", "Fallback.Service");
  assert.equal(states.length, 1);
  assert.equal(states[0].service, "orphan:1", "falls back to the key when no label is stored");
});

test("ctStamp renders a Central Time string a human can read", () => {
  const s = ctStamp();
  assert.match(s, /^[A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2}(am|pm)$/);
});

test("listAll drains the whole cursor chain, not just the first page", async () => {
  const kv = pagedKV(2);
  for (let i = 0; i < 7; i++) kv.store.set(`state:k${i}`, "1");
  const names = (await listAll(kv, "state:")).map(({ name }) => name);
  assert.equal(names.length, 7, "every key past page one must be read");
  assert.deepEqual(names, [...kv.store.keys()].filter((k) => k.startsWith("state:")));
});

test("a paginated inventory still reports every topic and every alert", async () => {
  const kv = pagedKV(1);
  seed(kv, { key: "datadog:1", service: "A.svc", state: { phase: "ok", severity: "INFO", since: 1, updated: 2, text: "a", link: "" } });
  seed(kv, { key: "datadog:2", service: "B.svc", state: { phase: "alerting", severity: "SEVERE", since: 1, updated: 2, text: "b", link: "" } });
  seed(kv, { key: "datadog:3", service: "C.svc", state: { phase: "alerting", severity: "SEVERE", since: 1, updated: 2, text: "c", link: "" } });
  const { topics, total } = await collectTopics(asEnv(kv));
  assert.deepEqual(Object.keys(topics).sort(), ["A.svc", "B.svc", "C.svc"]);
  assert.equal(total, 3);
});

test("one topic's failed status refresh does not abort the rest of the sweep", async () => {
  const kv = fakeKV();
  const longAgo = Date.now() - 60 * 60 * 1000;
  for (const key of ["datadog:1", "datadog:2"]) {
    seed(kv, { key, service: key, state: { phase: "pending", severity: "SEVERE", since: longAgo, updated: longAgo, text: "t", link: "" } });
  }
  const orig = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => {
    // The two promotion posts land (they are sequential); every status
    // rewrite after them fails, so the refresh batch rejects.
    if (++calls > 2) throw new Error("zulip unreachable");
    return { ok: true, json: async () => ({ result: "success", id: calls }) };
  };
  try {
    // Both incidents must still be promoted and reported even though no
    // standing-status rewrite can land.
    const out = await runSweep(asEnv(kv));
    assert.equal(out.promoted, 2, "one failing topic must not cancel the others");
    assert.equal(out.checked, 2);
  } finally {
    globalThis.fetch = orig;
  }
});

test("Better Stack accepts only its configured static header", async () => {
  const env = { BETTERSTACK_WEBHOOK_SECRET: "synthetic" };
  for (const [headers, expected] of [[{ "x-betterstack-token": "synthetic" }, true],
    [{ "x-betterstack-token": "wrong" }, false], [{ "x-betterstack-signature": "synthetic" }, false], [{}, false]]) {
    assert.equal(await verify(new Request("https://test/webhook/betterstack", { headers }), "{}", env, "betterstack"), expected);
  }
});

test("generic webhook authentication requires a bearer header, not a URL secret", async () => {
  const env = { INGRESS_SHARED_TOKEN: "synthetic" };
  assert.equal(await verify(new Request("https://test/webhook/custom", { headers: { authorization: "Bearer synthetic" } }), "{}", env, "custom"), true);
  assert.equal(await verify(new Request("https://test/webhook/custom?token=synthetic"), "{}", env, "custom"), false);
  assert.equal(await verify(new Request("https://test/webhook/custom", { headers: { authorization: "Bearer wrong" } }), "{}", env, "custom"), false);
});

test("a signature over the raw body is verified, not thrown on", async () => {
  // SubtleCrypto.sign() only accepts a BufferSource; signing the request text
  // as a string threw and every Datadog webhook 401'd before comparison.
  const raw = JSON.stringify({ monitor_id: "1", alert_type: "triggered" });
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode("fixture"),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = [...new Uint8Array(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(raw)))]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
  const { verify } = __testing;
  const env = { DATADOG_WEBHOOK_SECRET: "fixture" };
  const headers = { "x-datadog-signature": sig };
  assert.equal(await verify({ headers: new Headers(headers), url: "https://test/webhook/datadog" }, raw, env, "datadog"), true);
  assert.equal(await verify({ headers: new Headers({ "x-datadog-signature": "00" }), url: "https://test/webhook/datadog" }, raw, env, "datadog"), false);
});

test("a stable key retains its original service without duplicate membership", async () => {
  const kv = fakeKV();
  seed(kv, { key: "custom:one", service: "Old.svc", state: { phase: "ok", severity: "INFO", since: 1, updated: 2, text: "fine", link: "" } });
  const orig = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ result: "success", id: 5 }) });
  try {
    await handleEvent(
      "custom",
      { id: "one", service: "New.svc", status: "down" },
      asEnv(kv),
    );
  } finally {
    globalThis.fetch = orig;
  }
  // The state must be counted once, under the topic it now belongs to.
  assert.equal(kv.store.has(indexPrefix("alerts", "Old.svc") + "custom:one"), true);
  assert.equal(kv.store.has(topicKey("alerts", "Old.svc")), true);
  assert.equal(kv.store.has(indexPrefix("alerts", "New.svc") + "custom:one"), false);
  const { topics, total } = await collectTopics(asEnv(kv));
  assert.deepEqual(Object.keys(topics), ["Old.svc"]);
  assert.equal(total, 1);
});

test("Datadog accepts the static custom header Datadog can actually send", async () => {
  // Datadog's webhook integration cannot compute a per-body HMAC, so an
  // HMAC-only gate rejected every real Datadog delivery with 401.
  const env = { DATADOG_WEBHOOK_SECRET: "synthetic" };
  const raw = JSON.stringify({ monitor_id: "1", alert_type: "triggered" });
  const headers = (h) =>
    verify(new Request("https://test/webhook/datadog", { headers: h }), raw, env, "datadog");
  assert.equal(await headers({ "x-datadog-token": "synthetic" }), true, "the configured static header authenticates");
  assert.equal(await headers({ "x-datadog-token": "wrong" }), false, "a wrong static header does not");
  assert.equal(await headers({}), false, "an unconfigured source is rejected, never trusted");
});

test("the JSON:API payload shape passes the boundary validator", async () => {
  const { validatePayload } = __testing;
  const incident = {
    data: {
      type: "incident",
      id: "4242",
      attributes: { id: 4242, status: "triggered", url: "https://uptime.betterstack.com/team/1/monitors/99887766" },
      relationships: { monitor: { data: { type: "monitor", id: "99887766" } } },
    },
  };
  validatePayload(incident);
  // Envelope nesting stays bounded rather than recursing without end.
  assert.throws(() => validatePayload({ data: { attributes: { relationships: { monitor: { data: { attributes: { relationships: { monitor: { data: { attributes: {} } } } } } } } } } }));
});
