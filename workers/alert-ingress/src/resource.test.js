// Resource alerts tag both Housekeepers and Jay; CPU only tags classified warnings/saturation, and recovery tags nobody.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import assert from "node:assert/strict";
import { __testing } from "./index.js";
import { RESOURCE_MENTIONS, resourceMentions } from "./resource.js";

const { handleEvent, runSweep, stateKey, srcKey, svcKey, topicKey, indexPrefix } = __testing;

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

function envFor(kv) {
  return {
    ALERT_STATE: kv,
    WRITER_STORAGE: fakeStorage(),
    ZULIP_BOT_EMAIL_DATADOG: "datadog-bot@example.invalid",
    ZULIP_BOT_API_KEY_DATADOG: "datadog-key",
  };
}

function stubZulip() {
  const calls = [];
  const original = globalThis.fetch;
  let nextId = 100;
  globalThis.fetch = async (url, opts) => {
    calls.push({ method: opts.method, form: opts.body ? new URLSearchParams(opts.body) : null });
    if (opts.method === "POST") return { ok: true, json: async () => ({ result: "success", id: nextId++ }) };
    return { ok: true, json: async () => ({ result: "success" }) };
  };
  return {
    /** The one-line event posts, as opposed to standing status writes. */
    events: () =>
      calls.filter((call) => call.method === "POST" && !call.form.get("content").startsWith("**Status:")),
    restore: () => {
      globalThis.fetch = original;
    },
  };
}

function seed(kv, key, service, phase, severity = "MODERATE") {
  const old = Date.now() - 60 * 60 * 1000;
  kv.store.set(
    stateKey(key),
    JSON.stringify({ phase, severity, since: old, updated: old, text: "it is high", link: "" }),
  );
  kv.store.set(svcKey(key), JSON.stringify(service));
  kv.store.set(srcKey(key), JSON.stringify("datadog"));
  kv.store.set(indexPrefix("datadog", "alerts", service) + key, "1");
  kv.store.set(topicKey("datadog", "alerts", service), "1");
}

test("each Datadog resource monitor tags both Housekeepers and Jay, with or without a scope", () => {
  for (const id of ["22024800", "22024799", "22024795", "22024797", "22024796"]) {
    assert.equal(resourceMentions(`datadog:${id}`), RESOURCE_MENTIONS);
    assert.equal(resourceMentions(`datadog:${id}:host:fleet-hetzner-nbg1`), RESOURCE_MENTIONS);
  }
  assert.match(RESOURCE_MENTIONS, /@\*\*BF-Housekeeper\|1216054\*\*/);
  assert.match(RESOURCE_MENTIONS, /@\*\*GB-Housekeeper\|1215533\*\*/);
  assert.match(RESOURCE_MENTIONS, /@\*\*Jay Wedgeworth\|1211974\*\*/);
});

test("the CPU monitor tags only SEVERE saturation events", () => {
  const key = "datadog:22024794:host:fleet-hetzner-nbg1";
  assert.equal(resourceMentions(key, "SEVERE"), RESOURCE_MENTIONS);
  assert.equal(resourceMentions(key, "MODERATE"), "");
  assert.equal(resourceMentions(key, "INFO"), "");
  assert.equal(resourceMentions(key), "");
});

test("CPU, other monitors, other vendors and look-alike ids tag nobody", () => {
  for (const key of [
    "datadog:12345",
    "datadog:220247970",
    "datadog:system load is high",
    "betterstack:22024797",
    "pagerduty:22024797",
    "",
    undefined,
  ])
    assert.equal(resourceMentions(key), "", String(key));
});

test("promoted resource and CPU alerts put mentions on their own line", async () => {
  const kv = fakeKV();
  seed(kv, "datadog:22024797", "load.service", "pending");
  seed(kv, "datadog:22024794", "cpu.service", "pending", "SEVERE");
  const zulip = stubZulip();
  try {
    const result = await runSweep(envFor(kv));
    assert.equal(result.promoted, 2);
    const byTopic = Object.fromEntries(zulip.events().map((call) => [call.form.get("topic"), call.form]));
    assert.equal(byTopic["load.service"].get("to"), "Alerts-Log");
    const [line, mentions, ...rest] = byTopic["load.service"].get("content").split("\n");
    assert.match(line, /^MODERATE  •  load\.service  •  it is high$/);
    assert.equal(mentions, RESOURCE_MENTIONS);
    assert.deepEqual(rest, []);
    const [cpuLine, cpuMentions, ...cpuRest] = byTopic["cpu.service"].get("content").split("\n");
    assert.match(cpuLine, /^SEVERE  •  cpu\.service  •  it is high$/);
    assert.equal(cpuMentions, RESOURCE_MENTIONS);
    assert.deepEqual(cpuRest, []);
  } finally {
    zulip.restore();
  }
});

test("a resource alert's recovery tags nobody", async () => {
  const kv = fakeKV();
  seed(kv, "datadog:22024800", "memory.service", "alerting");
  const zulip = stubZulip();
  try {
    await handleEvent(
      "datadog",
      { monitor_id: "22024800", alert_type: "recovered", alert_transition: "Recovered", host: "memory.service" },
      envFor(kv),
    );
    const events = zulip.events();
    assert.equal(events.length, 1);
    assert.match(events[0].form.get("content"), /recovered/);
    assert.doesNotMatch(events[0].form.get("content"), /@\*\*/);
  } finally {
    zulip.restore();
  }
});

test("a CPU monitor recovery stays quiet", async () => {
  const kv = fakeKV();
  seed(kv, "datadog:22024794", "cpu.service", "alerting");
  const zulip = stubZulip();
  try {
    await handleEvent(
      "datadog",
      { monitor_id: "22024794", alert_type: "Recovered", alert_transition_type: "Recovered", host: "cpu.service" },
      envFor(kv),
    );
    const events = zulip.events();
    assert.equal(events.length, 1);
    assert.match(events[0].form.get("content"), /recovered/);
    assert.doesNotMatch(events[0].form.get("content"), /@\*\*/);
  } finally {
    zulip.restore();
  }
});
