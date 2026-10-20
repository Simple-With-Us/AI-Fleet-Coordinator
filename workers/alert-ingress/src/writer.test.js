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
const env = { ALERT_STATE: legacy, INGRESS_SHARED_TOKEN: "fixture" };
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
    assert.equal(db.data.get('delivery:["alerts","svc"]').outcome, "unknown");
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
    };
  const writer = new AlertWriter({ storage: db }, { ALERT_STATE: historical });
  db.data.set("kv:topic:alerts:svc", "1");
  db.data.set("kv:idx:alerts:svc:historic", "1");
  const response = await writer.fetch(new Request("https://test/status"));
  assert.equal(response.status, 200);
  assert.equal((await response.json()).summary.alerts, 1);
  assert.equal(
    await historical.get("state:historic"),
    JSON.stringify({ phase: "ok" }),
  );
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
      { ALERT_STATE: kv },
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
