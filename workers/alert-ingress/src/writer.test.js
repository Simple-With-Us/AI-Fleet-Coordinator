import test from "node:test";
import assert from "node:assert/strict";
import worker, { AlertWriter, __testing } from "./index.js";
import { normalize } from "./normalize.js";
import { writeStatus } from "./zulip.js";
function storage() {
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
const legacy = {
  async get() {
    return null;
  },
  async list() {
    return { keys: [], list_complete: true };
  },
};
// The generic source owns the legacy un-suffixed pair; every vendor source
// owns a pair of its own.
const env = {
  ALERT_STATE: legacy,
  INGRESS_SHARED_TOKEN: "fixture",
  ZULIP_BOT_EMAIL: "generic-bot@example.invalid",
  ZULIP_BOT_API_KEY: "generic-key",
  ZULIP_BOT_EMAIL_BETTERSTACK: "betterstack-bot@example.invalid",
  ZULIP_BOT_API_KEY_BETTERSTACK: "betterstack-key",
};
test("authenticated Better Stack default incident ingests and resolves", async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = async () => ({ ok: true, json: async () => ({ result: "success", id: 8 }) });
  const state = storage();
  const writer = new AlertWriter({ storage: state }, { ...env, BETTERSTACK_WEBHOOK_SECRET: "fixture" });
  try {
    for (const resolved_at of [null, "2026-10-20T00:00:00Z"]) {
      const body = { data: { id: "incident-1", type: "incident", attributes: {
        name: "Synthetic service", url: "https://example.test", http_method: "get",
        cause: "Error 500", started_at: "2026-10-20T00:00:00Z", resolved_at,
        acknowledged_at: null, response_content: null, response_url: null, screenshot_url: null,
      }, relationships: { monitor: { data: { id: "monitor-1", type: "monitor" } } } } };
      const result = await writer.fetch(new Request("https://test/webhook/betterstack", {
        method: "POST", headers: { "x-betterstack-token": "fixture" }, body: JSON.stringify(body),
      }));
      assert.equal(result.status, 200);
      const savedState = JSON.parse(await state.get("kv:state:betterstack:monitor-1"));
      assert.equal(savedState.phase, resolved_at ? "ok" : "pending");
      assert.equal(JSON.parse(await state.get("kv:svc:betterstack:monitor-1")), "Synthetic service");
    }
  } finally { globalThis.fetch = saved; }
});
const request = () =>
  new Request("https://test/status/alerts/svc", { headers: { authorization: "Bearer fixture" } });
test("missing durable writer fails closed", async () => {
  for (const path of ["/status", "/sweep", "/webhook/generic"])
    assert.equal(
      (await worker.fetch(new Request(`https://test${path}`), {})).status,
      503,
    );
});
test("page chain drains empty pages and refuses cycles", async () => {
  let calls = 0;
  const kv = {
    async list({ cursor }) {
      calls++;
      return cursor
        ? { keys: [{ name: "state:b" }], list_complete: true }
        : { keys: [], cursor: "next", list_complete: false };
    },
  };
  assert.equal((await __testing.listAll(kv, "state:")).length, 1);
  assert.equal(calls, 2);
  await assert.rejects(
    __testing.listAll(
      {
        async list() {
          return { keys: [], cursor: "loop", list_complete: false };
        },
      },
      "state:",
    ),
  );
});
test("parallel reads bounded to four", async () => {
  let active = 0,
    peak = 0;
  await __testing.mapBounded(Array.from({ length: 30 }), async () => {
    active++;
    peak = Math.max(peak, active);
    await new Promise((r) => setImmediate(r));
    active--;
  });
  assert.equal(peak, 4);
});
test("payload shapes fail closed", () => {
  for (const value of [
    null,
    [],
    { alert: [] },
    { summary: {} },
    { summary: "bad\u0000text" },
  ])
    assert.throws(() => __testing.validatePayload(value));
  __testing.validatePayload({
    alert: { id: "one", status: "down", firing: true },
  });
});
test("exact recovery and route namespace", () => {
  for (const status of ["okay", "recovers", "clears", "recovery"])
    assert.equal(normalize("custom", { status }).firing, false);
  for (const status of ["broken", "disrupted", "not okay"])
    assert.equal(normalize("custom", { status }).firing, true);
  assert.equal(
    normalize("custom", { source: "datadog", id: "same", status: "down" }).key,
    "custom:same",
  );
  assert.equal(
    normalize("datadog", {
      alert_type: "error",
      event_type: "alert",
      alert_transition_type: "Recovered",
    }).firing,
    false,
  );
});
test("concurrent status creates once then edits", async () => {
  const db = storage(),
    writer = new AlertWriter({ storage: db }, env),
    original = globalThis.fetch,
    methods = [];
  globalThis.fetch = async (url, opts) => {
    methods.push(opts.method);
    await new Promise((r) => setImmediate(r));
    return {
      ok: true,
      json: async () =>
        opts.method === "POST"
          ? { result: "success", id: 8 }
          : { result: "success" },
    };
  };
  try {
    assert.deepEqual(
      (
        await Promise.all([writer.fetch(request()), writer.fetch(request())])
      ).map((r) => r.status),
      [200, 200],
    );
    assert.deepEqual(methods, ["POST", "PATCH"]);
  } finally {
    globalThis.fetch = original;
  }
});
test("lost reply remains unknown after restart without replay", async () => {
  const db = storage(),
    original = globalThis.fetch;
  let sends = 0;
  globalThis.fetch = async () => {
    sends++;
    assert.equal(db.data.get('delivery:["generic","alerts","svc"]').outcome, "unknown");
    throw new Error("private response");
  };
  try {
    await new AlertWriter({ storage: db }, env).fetch(request());
    await new AlertWriter({ storage: db }, env).fetch(request());
    assert.equal(sends, 1);
  } finally {
    globalThis.fetch = original;
  }
});
test("failed durable write prevents network", async () => {
  const db = storage(),
    original = globalThis.fetch;
  db.put = async () => {
    throw new Error("unavailable");
  };
  let calls = 0;
  globalThis.fetch = async () => {
    calls++;
  };
  try {
    assert.equal(
      (await new AlertWriter({ storage: db }, env).fetch(request())).status,
      503,
    );
    assert.equal(calls, 0);
  } finally {
    globalThis.fetch = original;
  }
});
test("malformed acknowledgement never accepts an ID", async () => {
  const original = globalThis.fetch;
  try {
    for (const body of [{ id: 3 }, { result: "success", id: "3" }, [], null]) {
      globalThis.fetch = async () => ({ ok: true, json: async () => body });
      assert.equal(
        await writeStatus({
          email: "test@example.invalid",
          apiKey: "fixture",
          channel: "alerts",
          topic: "svc",
          content: "text",
        }),
        null,
      );
    }
  } finally {
    globalThis.fetch = original;
  }
});
test("incomplete inventory forbids sweep writes and sends", async () => {
  const original = globalThis.fetch;
  let sends = 0,
    puts = 0;
  globalThis.fetch = async () => {
    sends++;
  };
  try {
    const kv = {
      async list() {
        return { keys: [{ name: "state:one" }], list_complete: false };
      },
      async put() {
        puts++;
      },
    };
    await assert.rejects(__testing.runSweep({ ALERT_STATE: kv }));
    assert.equal(puts, 0);
    assert.equal(sends, 0);
  } finally {
    globalThis.fetch = original;
  }
});
test("inventory bound does not expose partial records", async () => {
  await assert.rejects(
    __testing.listAll(
      {
        async list() {
          return {
            keys: Array.from({ length: 2001 }, (_, i) => ({
              name: `state:${i}`,
            })),
            list_complete: true,
          };
        },
      },
      "state:",
    ),
  );
});
test("durable overlay preserves historical KV and reads its own writes", async () => {
  const db = storage(),
    historical = {
      ...legacy,
      async get(key) {
        return key === "state:historic"
          ? JSON.stringify({ phase: "ok" })
          : null;
      },
      async list({ prefix }) {
        return {
          keys: "state:historic".startsWith(prefix) ? [{ name: "state:historic" }] : [],
          list_complete: true,
        };
      },
    };
  const writer = new AlertWriter({ storage: db }, { ALERT_STATE: historical });
  db.data.set("kv:topic:generic:alerts:svc", "1");
  db.data.set("kv:idx:generic:alerts:svc:historic", "1");
  const response = await writer.fetch(new Request("https://test/status"));
  assert.equal(response.status, 200);
  assert.equal((await response.json()).summary.alerts, 1);
  assert.equal(
    await historical.get("state:historic"),
    JSON.stringify({ phase: "ok" }),
  );
});
// Every KV list counts against the free plan's 1,000 a day, so the frozen
// legacy namespace is copied once and the writer never reads it again.
function countingLegacy(records) {
  const calls = { list: 0, get: 0 };
  return {
    calls,
    async get(key) {
      calls.get++;
      return records[key] ?? null;
    },
    async list({ prefix }) {
      calls.list++;
      return {
        keys: Object.keys(records).filter((k) => k.startsWith(prefix)).map((name) => ({ name })),
        list_complete: true,
      };
    },
  };
}
test("legacy KV is copied once, then never listed or read again", async () => {
  const db = storage(),
    kv = countingLegacy({
      "topic:generic:alerts:svc": "1",
      "idx:generic:alerts:svc:historic": "1",
      "state:historic": JSON.stringify({ phase: "ok" }),
    });
  db.data.set("kv:state:historic", JSON.stringify({ phase: "alerting" }));
  for (let i = 0; i < 3; i++) {
    const writer = new AlertWriter({ storage: db }, { ALERT_STATE: kv });
    for (let j = 0; j < 2; j++) {
      const response = await writer.fetch(new Request("https://test/status"));
      assert.equal(response.status, 200);
      assert.equal((await response.json()).summary.alerts, 1);
    }
  }
  assert.deepEqual(kv.calls, { list: 1, get: 2 });
  assert.equal(db.data.get("kv:topic:generic:alerts:svc"), "1");
  // The writer's own newer record wins over the frozen copy.
  assert.equal(db.data.get("kv:state:historic"), JSON.stringify({ phase: "alerting" }));
  assert.notEqual(db.data.get("legacy:imported"), undefined);
});
test("a failed copy reads through to KV and retries only after ten minutes", async () => {
  const db = storage(),
    kv = countingLegacy({
      "topic:generic:alerts:svc": "1",
      "idx:generic:alerts:svc:a": "1",
      "state:a": JSON.stringify({ phase: "ok" }),
    });
  const list = kv.list;
  let quota = true;
  kv.list = async (opts) => {
    if (quota && opts.prefix === "") {
      kv.calls.list++;
      throw new Error("KV list() limit exceeded for the day");
    }
    return list(opts);
  };
  const writer = new AlertWriter({ storage: db }, { ALERT_STATE: kv }),
    status = async () => (await (await writer.fetch(new Request("https://test/status"))).json()).summary.alerts;
  assert.equal(await status(), 1);
  assert.equal(await status(), 1);
  assert.equal(db.data.get("legacy:imported"), undefined);
  const before = kv.calls.list;
  quota = false;
  writer.importRetryAt = 0;
  assert.equal(await status(), 1);
  assert.notEqual(db.data.get("legacy:imported"), undefined);
  assert.equal(kv.calls.list, before + 1);
  assert.equal(await status(), 1);
  assert.equal(kv.calls.list, before + 1);
});
test("definite pre-send inventory failure cannot advance event state", async () => {
  let writes = 0;
  const kv = {
    async get() {
      return { phase: "alerting", severity: "SEVERE", since: 1 };
    },
    async list() {
      return { keys: [], list_complete: false };
    },
    async put() {
      writes++;
    },
  };
  await assert.rejects(
    __testing.handleEvent(
      "custom",
      { id: "one", service: "svc", status: "recovered" },
      {
        ALERT_STATE: kv,
        ZULIP_BOT_EMAIL_CUSTOM: "custom-bot@example.invalid",
        ZULIP_BOT_API_KEY_CUSTOM: "custom-key",
      },
    ),
  );
  assert.equal(writes, 0);
});
test("definitive timeline refusal retries safely but unknown never retries", async () => {
  const db = storage(),
    original = globalThis.fetch;
  let attempts = 0;
  const args = {
    email: "test@example.invalid",
    apiKey: "fixture",
    channel: "alerts",
    topic: "svc",
    severity: "INFO",
    service: "svc",
    what: "recovered",
  };
  try {
    globalThis.fetch = async () => {
      attempts++;
      return { ok: false, status: 403 };
    };
    await assert.rejects(
      __testing.sendEventOnce(
        { WRITER_STORAGE: db },
        ["stable-transition"],
        args,
      ),
    );
    globalThis.fetch = async () => {
      attempts++;
      return { ok: true, json: async () => ({ result: "success", id: 19 }) };
    };
    assert.equal(
      await __testing.sendEventOnce(
        { WRITER_STORAGE: db },
        ["stable-transition"],
        args,
      ),
      19,
    );
    assert.equal(
      await __testing.sendEventOnce(
        { WRITER_STORAGE: db },
        ["stable-transition"],
        args,
      ),
      19,
    );
    assert.equal(attempts, 2);
    globalThis.fetch = async () => {
      attempts++;
      throw new Error("lost response");
    };
    assert.equal(
      await __testing.sendEventOnce(
        { WRITER_STORAGE: db },
        ["uncertain-transition"],
        args,
      ),
      null,
    );
    assert.equal(
      await __testing.sendEventOnce(
        { WRITER_STORAGE: db },
        ["uncertain-transition"],
        args,
      ),
      null,
    );
    assert.equal(attempts, 3);
  } finally {
    globalThis.fetch = original;
  }
});
test("incident refresh metadata cannot replay an unknown transition after crash", async () => {
  const db = storage(),
    original = globalThis.fetch;
  const previous = {
    phase: "pending",
    since: 100,
    updated: 101,
    text: "initial",
    severity: "SEVERE",
  };
  const next = { ...previous, phase: "alerting" };
  const identity = (value) =>
    __testing.transitionIdentity("monitor", "announce", value, next);
  let sends = 0;
  globalThis.fetch = async () => {
    sends++;
    throw new Error("accepted then lost");
  };
  try {
    await __testing.sendEventOnce({ WRITER_STORAGE: db }, identity(previous), {
      email: "test@example.invalid",
      apiKey: "fixture",
      severity: "SEVERE",
      service: "svc",
      what: "down",
    });
    // Crash before phase commit, then an ordinary refresh changes only metadata.
    const refreshed = { ...previous, updated: 999, text: "new description" };
    assert.deepEqual(identity(previous), identity(refreshed));
    await __testing.sendEventOnce({ WRITER_STORAGE: db }, identity(refreshed), {
      email: "test@example.invalid",
      apiKey: "fixture",
      severity: "SEVERE",
      service: "svc",
      what: "down",
    });
    assert.equal(sends, 1);
  } finally {
    globalThis.fetch = original;
  }
});
test("real severity cycles advance delivery revision but ordinary refreshes do not", async () => {
  const { advanceRevision, transitionIdentity } = __testing;
  const initial = {
    phase: "alerting",
    since: 100,
    severity: "MODERATE",
    updated: 101,
  };
  const severe = advanceRevision(initial, { ...initial, severity: "SEVERE" });
  const moderate = advanceRevision(severe, { ...severe, severity: "MODERATE" });
  const second = advanceRevision(moderate, { ...moderate, severity: "SEVERE" });
  assert.equal(severe.transitionRevision, 1);
  assert.equal(second.transitionRevision, 3);
  assert.notDeepEqual(
    transitionIdentity("monitor", "update", initial, severe),
    transitionIdentity("monitor", "update", moderate, second),
  );
  assert.equal(
    advanceRevision(second, { ...second, updated: 999, text: "refresh" })
      .transitionRevision,
    3,
  );
});
test("the webhook path and the cron sweep reach the same writer object", async () => {
  // The delivery barrier lives in the writer object's storage, so the two
  // entry points must resolve to one object.  Two names would be two writers,
  // each blind to the other's barrier.
  const names = [];
  const env = {
    INGRESS_SHARED_TOKEN: "fixture",
    ALERT_WRITER: {
      idFromName: (name) => {
        names.push(name);
        return { name };
      },
      get: () => ({ fetch: async () => new Response("{}") }),
    },
  };
  await worker.fetch(new Request("https://test/status/alerts/x"), env);
  const pending = [];
  await worker.scheduled({}, env, { waitUntil: (p) => pending.push(p) });
  await Promise.all(pending);
  assert.equal(names.length, 2);
  assert.equal(names[0], names[1]);
});
