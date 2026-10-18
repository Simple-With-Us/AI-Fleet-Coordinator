// The machine-readable status contract that consumers render themselves
// (today: the Usage Monitor /ops page).  If this shape changes, that page
// changes with it -- one writer, many readers.
//
// Run: node --test workers/alert-ingress/src/

import test from "node:test";
import assert from "node:assert/strict";
import { __testing } from "./index.js";

const { collectTopics, collectStates, stateKey, svcKey, topicKey, indexPrefix, ctStamp } = __testing;

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
    async list({ prefix = "" } = {}) {
      return {
        keys: [...store.keys()]
          .filter((k) => k.startsWith(prefix))
          .map((name) => ({ name })),
        listComplete: true,
      };
    },
  };
}

/** The collectors take the Workers `env`, so wrap the fake binding the same way. */
const asEnv = (kv) => ({ ALERT_STATE: kv });

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