// Usage guards and efficiency: the Zulip timer, unchanged deliveries, the pending
// index, Zulip 429s, the storm breaker, inventory overflow and the stateless layer.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import assert from "node:assert/strict";
import worker, { AlertWriter } from "./index.js";
import {
  DELIVERIES_PER_DAY,
  DeliveryBreaker,
  SOURCE_DAY_RESERVE,
  SOURCE_EVENTS_PER_MINUTE,
} from "./breaker.js";
import { postRaw, writeStatus } from "./zulip.js";

function storage() {
  const data = new Map();
  const counts = { put: 0, listed: [] };
  return {
    data,
    counts,
    async get(key) {
      return data.get(key);
    },
    async put(key, value) {
      counts.put++;
      data.set(key, value);
    },
    async delete(key) {
      return data.delete(key);
    },
    async list({ prefix, limit = Infinity }) {
      counts.listed.push(prefix);
      return new Map([...data].filter(([key]) => key.startsWith(prefix)).slice(0, limit));
    },
  };
}

const ENV = {
  INGRESS_SHARED_TOKEN: "fixture",
  ZULIP_BOT_EMAIL: "generic-bot@example.invalid",
  ZULIP_BOT_API_KEY: "generic-key",
};

function zulipStub(respond = () => ({})) {
  const calls = [];
  const fetchStub = async (url, opts) => {
    const form = new URLSearchParams(opts.body || "");
    const call = { method: opts.method, to: form.get("to"), url: String(url), topic: form.get("topic") };
    calls.push(call);
    const custom = respond(call, calls);
    if (custom?.status) {
      return {
        ok: false,
        status: custom.status,
        headers: new Headers(custom.headers || {}),
        body: { cancel: async () => {} },
      };
    }
    return { ok: true, json: async () => ({ result: "success", id: 100 + calls.length }) };
  };
  return { calls, fetchStub };
}

async function withFetch(fetchStub, run) {
  const saved = globalThis.fetch;
  globalThis.fetch = fetchStub;
  try {
    return await run();
  } finally {
    globalThis.fetch = saved;
  }
}

const hook = (writer, body) =>
  writer.fetch(
    new Request("https://test/webhook/generic", {
      method: "POST",
      headers: { authorization: "Bearer fixture" },
      body: JSON.stringify(body),
    }),
  );
const sweepOnce = (writer) =>
  writer.fetch(new Request("https://test/sweep", { headers: { authorization: "Bearer fixture" } }));
const firing = (id = "one", summary = "down") => ({ id, service: "svc", status: "down", summary });
const isPatch = (call) => call.method === "PATCH";
const state = (db, key) => JSON.parse(db.data.get(`kv:state:${key}`));

// The Zulip timer

test("the Zulip timer is cleared when a call ends, on every path", async () => {
  const savedSet = globalThis.setTimeout;
  const savedClear = globalThis.clearTimeout;
  const armed = new Set();
  const delays = [];
  globalThis.setTimeout = (fn, ms) => {
    const id = savedSet(fn, ms);
    armed.add(id);
    delays.push(ms);
    return id;
  };
  globalThis.clearTimeout = (id) => {
    armed.delete(id);
    savedClear(id);
  };
  const args = { email: "b@example.invalid", apiKey: "k", channel: "c", topic: "t", content: "x" };
  try {
    for (const respond of [
      async () => ({ ok: true, json: async () => ({ result: "success", id: 1 }) }),
      async () => ({ ok: true, json: async () => { throw new Error("bad body"); } }),
      async () => ({ ok: false, status: 500 }),
      async () => { throw new Error("network"); },
    ]) {
      await withFetch(respond, () => postRaw(args).catch(() => null));
      assert.equal(armed.size, 0);
    }
    assert.ok(delays.length >= 4);
    assert.ok(delays.every((ms) => ms === 5000));
  } finally {
    globalThis.setTimeout = savedSet;
    globalThis.clearTimeout = savedClear;
  }
});

test("a non-OK reply cancels its unread body, and a 429 is its own outcome", async () => {
  let cancelled = 0;
  const reply = {
    ok: false,
    status: 429,
    headers: new Headers({ "retry-after": "1.5" }),
    body: { cancel: async () => { cancelled++; } },
  };
  await withFetch(async () => reply, () =>
    assert.rejects(
      writeStatus({ email: "b@example.invalid", apiKey: "k", channel: "c", topic: "t", content: "x", statusId: 4 }),
      (error) => error.outcome === "rate_limited" && error.status === 429 && error.retryAfter === 2,
    ),
  );
  assert.equal(cancelled, 1);
});

// Unchanged deliveries

test("a repeat delivery writes no rows and sends no PATCH, as the README promises", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const { calls, fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    assert.equal((await hook(writer, firing())).status, 200);
    const before = { puts: db.counts.put, calls: calls.length };
    assert.equal((await hook(writer, firing())).status, 200);
    assert.equal(db.counts.put, before.puts);
    // Only the raw copy, which is a record of every delivery.
    assert.deepEqual(calls.slice(before.calls).map((call) => call.to), ["Alerts-Raw"]);
  });
});

test("a changed block is delivered, including a return to an earlier revision", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const { calls, fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    await hook(writer, firing("one", "A"));
    await hook(writer, firing("one", "B"));
    await hook(writer, firing("one", "A"));
    assert.equal(calls.filter(isPatch).length, 2);
  });
});

test("a refused revision is not treated as delivered on its retry", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  let refuse = false;
  const { calls, fetchStub } = zulipStub((call) => (refuse && isPatch(call) ? { status: 403 } : null));
  await withFetch(fetchStub, async () => {
    await hook(writer, firing("one", "A"));
    refuse = true;
    await hook(writer, firing("one", "B"));
    refuse = false;
    await hook(writer, firing("one", "B"));
    assert.equal(calls.filter(isPatch).length, 2);
  });
});

test("the operator route still rewrites an unchanged block", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const { calls, fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    await hook(writer, firing());
    const route = () =>
      writer.fetch(new Request("https://test/status/generic/alerts/svc", { headers: { authorization: "Bearer fixture" } }));
    assert.equal((await route()).status, 200);
    assert.equal(calls.filter(isPatch).length, 1);
  });
});

test("a repeat refreshes `updated` once the throttle has passed, so readers do not go stale", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const { fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    await hook(writer, firing());
    const old = Date.now() - 11 * 60 * 1000;
    db.data.set("kv:state:generic:one", JSON.stringify({ ...state(db, "generic:one"), updated: old }));
    await hook(writer, firing());
    assert.ok(state(db, "generic:one").updated > old);
  });
});

// The sweep

test("the first sweep builds the pending index, and later sweeps read only it", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const old = Date.now() - 60 * 60 * 1000;
  db.data.set("kv:state:generic:quiet", JSON.stringify({ phase: "ok", severity: "INFO", since: 1, updated: 1, text: "", link: "" }));
  db.data.set("kv:state:generic:held", JSON.stringify({ phase: "pending", severity: "SEVERE", since: old, updated: old, text: "t", link: "" }));
  db.data.set("kv:svc:generic:held", JSON.stringify("svc"));
  db.data.set("kv:src:generic:held", JSON.stringify("generic"));
  const { fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    const first = await (await sweepOnce(writer)).json();
    assert.equal(first.promoted, 1);
    assert.equal(first.checked, 2);
    assert.equal(state(db, "generic:held").phase, "alerting");
    assert.equal(db.data.has("pending:generic:held"), false);
    // The repair ran:  the row now has its membership marker.
    assert.equal(db.data.get("kv:idx:generic:alerts:svc:generic:held"), "1");
    db.counts.listed.length = 0;
    const idle = await (await sweepOnce(writer)).json();
    assert.equal(idle.checked, 0);
    assert.equal(db.counts.listed.includes("kv:state:"), false);
    assert.equal(db.counts.listed.includes("kv:topic:"), false);
  });
});

test("an incident is promoted from the index once its hold has passed", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const { calls, fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    await sweepOnce(writer);
    await hook(writer, firing());
    assert.equal(db.data.get("pending:generic:one"), 1);
    assert.equal((await (await sweepOnce(writer)).json()).promoted, 0);
    const held = state(db, "generic:one");
    db.data.set("kv:state:generic:one", JSON.stringify({ ...held, since: Date.now() - 60 * 60 * 1000 }));
    assert.equal((await (await sweepOnce(writer)).json()).promoted, 1);
    assert.ok(calls.some((call) => call.to === "Alerts-Log"));
    assert.equal(db.data.has("pending:generic:one"), false);
  });
});

test("a recovery inside the hold clears the index, and a stale entry is dropped", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const { fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    await sweepOnce(writer);
    await hook(writer, firing());
    await hook(writer, { id: "one", service: "svc", status: "recovered" });
    assert.equal(db.data.has("pending:generic:one"), false);
    db.data.set("pending:generic:gone", 1);
    await sweepOnce(writer);
    assert.equal(db.data.has("pending:generic:gone"), false);
  });
});

test("a repeat on a closed row writes nothing, however old the row is", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const { fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    await hook(writer, firing());
    await hook(writer, { id: "one", service: "svc", status: "recovered" });
    assert.equal(state(db, "generic:one").phase, "ok");
    const before = db.counts.put;
    await hook(writer, { id: "one", service: "svc", status: "recovered" });
    assert.equal(db.counts.put, before);
  });
});

test("a pending incident on a topic over its bound is still announced", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const old = Date.now() - 60 * 60 * 1000;
  const { calls, fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    await hook(writer, firing());
    db.data.set("kv:state:generic:one", JSON.stringify({ ...state(db, "generic:one"), since: old }));
    for (let i = 0; i < 2001; i++) db.data.set(`kv:idx:generic:alerts:svc:fill${i}`, "1");
    const body = await (await sweepOnce(writer)).json();
    assert.equal(body.promoted, 1);
    assert.equal(state(db, "generic:one").phase, "alerting");
    assert.ok(calls.some((call) => call.to === "Alerts-Log" && call.topic === "svc"));
  });
});

test("the reconcile finds a pending row the index missed within the hour", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const old = Date.now() - 60 * 60 * 1000;
  const { fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    await sweepOnce(writer);
    db.data.set("kv:state:generic:lost", JSON.stringify({ phase: "pending", severity: "SEVERE", since: old, updated: old, text: "t", link: "" }));
    assert.equal((await (await sweepOnce(writer)).json()).promoted, 0);
    db.data.set("sweep:reconciled-at", Date.now() - 61 * 60 * 1000);
    assert.equal((await (await sweepOnce(writer)).json()).promoted, 1);
  });
});

test("a large backlog stays within one sweep's Zulip budget and finishes later", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const old = Date.now() - 60 * 60 * 1000;
  for (let i = 0; i < 45; i++) {
    db.data.set(`kv:state:generic:k${i}`, JSON.stringify({ phase: "pending", severity: "SEVERE", since: old, updated: old, text: "t", link: "" }));
    db.data.set(`kv:svc:generic:k${i}`, JSON.stringify("svc"));
  }
  const { calls, fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    const first = await (await sweepOnce(writer)).json();
    assert.equal(first.promoted, 40);
    assert.ok(first.deferred > 0);
    assert.ok(calls.length <= 40);
    const second = await (await sweepOnce(writer)).json();
    assert.equal(second.promoted, 5);
    assert.equal(state(db, "generic:k44").phase, "alerting");
  });
});

// Zulip 429

test("a 429 on a timeline line answers 200, honors Retry-After, and the sweep catches up", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  await withFetch(zulipStub().fetchStub, () => hook(writer, firing()));
  const held = state(db, "generic:one");
  db.data.set("kv:state:generic:one", JSON.stringify({ ...held, phase: "alerting", severity: "MODERATE" }));
  db.data.delete("pending:generic:one");

  // The resolve's line is refused with a 429.
  const limited = zulipStub((call) => (call.to === "Alerts-Log" ? { status: 429, headers: { "retry-after": "120" } } : null));
  await withFetch(limited.fetchStub, async () => {
    const response = await hook(writer, { id: "one", service: "svc", status: "recovered" });
    assert.equal(response.status, 200);
    assert.equal(state(db, "generic:one").phase, "ok");
    const until = db.data.get("zulip-pause:generic");
    assert.ok(until > Date.now() + 100 * 1000 && until <= Date.now() + 120 * 1000);
  });

  // During the pause nothing calls Zulip, and a transition is still recorded.
  const quiet = zulipStub();
  await withFetch(quiet.fetchStub, async () => {
    const response = await hook(writer, firing("two"));
    assert.equal(response.status, 200);
    assert.equal(state(db, "generic:two").phase, "pending");
    assert.equal((await response.json()).statusDelivery.outcome, "paused");
    await sweepOnce(writer);
    assert.equal(quiet.calls.length, 0);
  });

  // After the pause, one line and one refresh per topic.
  db.data.set("zulip-pause:generic", Date.now() - 1);
  const after = zulipStub();
  await withFetch(after.fetchStub, async () => {
    await sweepOnce(writer);
    assert.equal(after.calls.filter((call) => call.to === "Alerts-Log").length, 1);
    assert.equal(after.calls.filter(isPatch).length, 1);
    assert.equal([...db.data.keys()].some((key) => key.startsWith("queued-event:")), false);
    assert.equal([...db.data.keys()].some((key) => key.startsWith("dirty-topic:")), false);
  });
});

test("a 429 on the standing block pauses the bot and leaves the topic for the sweep", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  await withFetch(zulipStub().fetchStub, () => hook(writer, firing("one", "A")));
  const limited = zulipStub((call) => (isPatch(call) ? { status: 429 } : null));
  await withFetch(limited.fetchStub, async () => {
    const response = await hook(writer, firing("one", "B"));
    assert.equal(response.status, 200);
    assert.ok(db.data.get("zulip-pause:generic") > Date.now());
    assert.equal([...db.data.keys()].some((key) => key.startsWith("dirty-topic:")), true);
  });
});

test("a held block whose rewrite fails ambiguously stays on the sweep's list until it lands", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const dirty = () => [...db.data.keys()].some((key) => key.startsWith("dirty-topic:"));
  await withFetch(zulipStub().fetchStub, () => hook(writer, firing("one", "A")));
  await withFetch(zulipStub((call) => (isPatch(call) ? { status: 429 } : null)).fetchStub, () =>
    hook(writer, firing("one", "B")),
  );
  db.data.set("zulip-pause:generic", Date.now() - 1);
  await withFetch(zulipStub((call) => (isPatch(call) ? { status: 500 } : null)).fetchStub, () => sweepOnce(writer));
  assert.equal(dirty(), true);
  const after = zulipStub();
  await withFetch(after.fetchStub, () => sweepOnce(writer));
  assert.equal(after.calls.filter(isPatch).length, 1);
  assert.equal(dirty(), false);
});

test("a held block whose rewrite is refused leaves the sweep's list", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  await withFetch(zulipStub().fetchStub, () => hook(writer, firing("one", "A")));
  await withFetch(zulipStub((call) => (isPatch(call) ? { status: 429 } : null)).fetchStub, () =>
    hook(writer, firing("one", "B")),
  );
  db.data.set("zulip-pause:generic", Date.now() - 1);
  await withFetch(zulipStub((call) => (isPatch(call) ? { status: 403 } : null)).fetchStub, () => sweepOnce(writer));
  assert.equal([...db.data.keys()].some((key) => key.startsWith("dirty-topic:")), false);
});

test("a Retry-After far in the future is bounded", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  await withFetch(zulipStub().fetchStub, () => hook(writer, firing("one", "A")));
  const limited = zulipStub((call) => (isPatch(call) ? { status: 429, headers: { "retry-after": "999999" } } : null));
  await withFetch(limited.fetchStub, () => hook(writer, firing("one", "B")));
  assert.ok(db.data.get("zulip-pause:generic") <= Date.now() + 3600 * 1000);
});

// The breaker

async function trip(writer) {
  for (let i = 0; i < SOURCE_EVENTS_PER_MINUTE; i++) await hook(writer, firing());
}

test("past the cap, a known unchanged event sheds with 200 and costs nothing", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const clock = Date.now;
  Date.now = () => 1_800_000_000_000 + 1000;
  const { calls, fetchStub } = zulipStub();
  try {
    await withFetch(fetchStub, async () => {
      await trip(writer);
      await hook(writer, firing());
      const notices = calls.filter((call) => call.topic === "alert-ingress").length;
      assert.equal(notices, 1);
      const before = { puts: db.counts.put, calls: calls.length };
      const response = await hook(writer, firing());
      assert.equal(response.status, 200);
      assert.equal((await response.json()).action, "shed");
      assert.equal(db.counts.put, before.puts);
      assert.equal(calls.length, before.calls);
    });
  } finally {
    Date.now = clock;
  }
});

test("past the cap, a transition on a known key is recorded, so a resolve cannot be lost", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const clock = Date.now;
  Date.now = () => 1_800_000_000_000 + 1000;
  const { calls, fetchStub } = zulipStub();
  try {
    await withFetch(fetchStub, async () => {
      await trip(writer);
      await hook(writer, firing());
      const sent = calls.length;
      const response = await hook(writer, { id: "one", service: "svc", status: "recovered" });
      assert.equal(response.status, 200);
      assert.equal(state(db, "generic:one").phase, "ok");
      assert.equal(calls.length, sent);
      assert.equal([...db.data.keys()].some((key) => key.startsWith("dirty-topic:")), true);
    });
  } finally {
    Date.now = clock;
  }
});

test("past the cap, a new incident gets 503 with Retry-After and writes nothing", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const clock = Date.now;
  Date.now = () => 1_800_000_000_000 + 1000;
  const { fetchStub } = zulipStub();
  try {
    await withFetch(fetchStub, async () => {
      await trip(writer);
      await hook(writer, firing());
      const before = db.counts.put;
      const response = await hook(writer, firing("brand-new"));
      assert.equal(response.status, 503);
      assert.equal(response.headers.get("retry-after"), "60");
      assert.equal(db.counts.put, before);
      assert.equal(db.data.has("kv:state:generic:brand-new"), false);
    });
  } finally {
    Date.now = clock;
  }
});

test("the breaker saves its count at most once a minute, and not for quiet traffic", async () => {
  let clock = 1_800_000_000_000;
  const db = storage();
  const breaker = new DeliveryBreaker(db, () => clock);
  for (let i = 0; i < 24; i++) await breaker.admit("a");
  assert.equal(db.counts.put, 0);
  for (let i = 0; i < 100; i++) {
    await breaker.admit("b");
    clock += 100;
  }
  assert.equal(db.counts.put, 1);
  clock += 60_000;
  await breaker.admit("b");
  assert.equal(db.counts.put, 2);
  // A restart resumes the day's saved count.
  const restarted = new DeliveryBreaker(db, () => clock);
  await restarted.load();
  assert.equal(restarted.total, db.data.get("breaker:v1").total);
});

test("the per-minute cap is per source and clears on the next minute", async () => {
  let clock = 1_800_000_000_000;
  const breaker = new DeliveryBreaker(storage(), () => clock);
  for (let i = 0; i < SOURCE_EVENTS_PER_MINUTE; i++) {
    assert.equal((await breaker.admit("a")).shed, false);
  }
  const tripped = await breaker.admit("a");
  assert.equal(tripped.shed, true);
  assert.equal(tripped.trippedNow, true);
  assert.equal((await breaker.admit("a")).trippedNow, false);
  assert.equal((await breaker.admit("b")).shed, false);
  clock += 60_000;
  assert.equal((await breaker.admit("a")).shed, false);
});

test("a storm from one source cannot lock another out for the day", async () => {
  let clock = 1_800_000_000_000;
  const db = storage();
  const breaker = new DeliveryBreaker(db, () => clock);
  for (let i = 0; i <= DELIVERIES_PER_DAY; i++) {
    await breaker.admit("noisy");
    clock += 1100;
  }
  assert.equal((await breaker.admit("noisy")).reason, "day");
  assert.equal((await breaker.admit("quiet")).shed, false);
  // The reserve survives a restart, and it is a bound, not an exemption.
  clock += 60_000;
  await breaker.admit("quiet");
  const restarted = new DeliveryBreaker(db, () => clock);
  await restarted.load();
  assert.equal(restarted.perSourceDay.get("noisy"), DELIVERIES_PER_DAY + 2);
  for (let i = 0; i < SOURCE_DAY_RESERVE; i++) {
    await restarted.admit("quiet");
    clock += 1100;
  }
  assert.equal((await restarted.admit("quiet")).reason, "day");
});

// Inventory overflow

test("over the bound, a new incident is refused with one notice and a known one still moves", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  await withFetch(zulipStub().fetchStub, () => hook(writer, firing()));
  for (let i = 0; i < 2001; i++) db.data.set(`kv:idx:generic:alerts:svc:fill${i}`, "1");
  const { calls, fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    const refused = await hook(writer, firing("brand-new"));
    assert.equal(refused.status, 503);
    assert.equal(db.data.has("kv:state:generic:brand-new"), false);
    await hook(writer, firing("brand-newer"));
    assert.equal(calls.filter((call) => call.topic === "alert-ingress").length, 1);

    const known = await hook(writer, { id: "one", service: "svc", status: "recovered" });
    assert.equal(known.status, 200);
    assert.equal((await known.json()).statusDelivery.outcome, "overflow");
    assert.equal(state(db, "generic:one").phase, "ok");
  });
});

test("an overflowing state inventory does not stop promotions", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const old = Date.now() - 60 * 60 * 1000;
  for (let i = 0; i < 2001; i++) db.data.set(`kv:state:generic:fill${i}`, "{}");
  db.data.set("kv:state:generic:held", JSON.stringify({ phase: "pending", severity: "SEVERE", since: old, updated: old, text: "t", link: "" }));
  db.data.set("kv:svc:generic:held", JSON.stringify("svc"));
  db.data.set("pending:generic:held", 1);
  const { calls, fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    const response = await sweepOnce(writer);
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.overflow, true);
    assert.equal(body.promoted, 1);
    assert.ok(calls.some((call) => call.topic === "alert-ingress"));
  });
});

// The stateless layer

function stubWriter(answer) {
  const seen = { fetches: 0 };
  return {
    seen,
    binding: {
      idFromName: (name) => name,
      get: () => ({
        fetch: async (request) => {
          seen.fetches++;
          return answer(request);
        },
      }),
    },
  };
}

test("GUARD_MODE off stops fetch, scheduled and the writer before any work", async () => {
  const writer = stubWriter(() => new Response("{}"));
  const env = { ...ENV, GUARD_MODE: "off", ALERT_WRITER: writer.binding };
  for (const path of ["/health", "/status", "/webhook/generic"]) {
    const response = await worker.fetch(new Request(`https://test${path}`), env);
    assert.equal(response.status, 503);
    assert.equal(response.headers.get("retry-after"), "3600");
  }
  const pending = [];
  await worker.scheduled({}, env, { waitUntil: (p) => pending.push(p) });
  assert.equal(pending.length, 0);
  assert.equal(writer.seen.fetches, 0);
  const db = storage();
  const direct = await new AlertWriter({ storage: db }, env).fetch(new Request("https://test/status"));
  assert.equal(direct.status, 503);
  assert.equal(db.counts.put, 0);
});

test("an empty body is refused without waking the writer, after one daily report", async () => {
  const writer = stubWriter(() => new Response("{}", { status: 400 }));
  const env = {
    ...ENV,
    BETTERSTACK_WEBHOOK_SECRET: "fixture",
    ALERT_WRITER: writer.binding,
  };
  const empty = (token) =>
    new Request("https://test/webhook/betterstack", {
      method: "POST",
      headers: { "x-betterstack-token": token, "content-length": "0" },
    });
  assert.equal((await worker.fetch(empty("wrong"), env)).status, 401);
  assert.equal(writer.seen.fetches, 0);
  await worker.fetch(empty("fixture"), env);
  assert.equal(writer.seen.fetches, 1);
  for (let i = 0; i < 4; i++) {
    const response = await worker.fetch(empty("fixture"), env);
    assert.equal(response.status, 400);
    assert.equal((await response.json()).error, "empty body");
  }
  assert.equal(writer.seen.fetches, 1);
});

test("an empty delivery is reported to Zulip at most once a day", async () => {
  const db = storage();
  const env = {
    ...ENV,
    BETTERSTACK_WEBHOOK_SECRET: "fixture",
    ZULIP_BOT_EMAIL_BETTERSTACK: "bs@example.invalid",
    ZULIP_BOT_API_KEY_BETTERSTACK: "bs-key",
  };
  const writer = new AlertWriter({ storage: db }, env);
  const empty = () =>
    new Request("https://test/webhook/betterstack", {
      method: "POST",
      headers: { "x-betterstack-token": "fixture" },
    });
  const { calls, fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    for (let i = 0; i < 3; i++) assert.equal((await writer.fetch(empty())).status, 400);
    assert.equal(calls.filter((call) => call.topic === "alert-ingress").length, 1);
    assert.equal([...db.data.keys()].some((key) => key.startsWith("kv:state:")), false);
  });
});

test("a notice refused with a 429 pauses the bot and is tried again soon", async () => {
  const db = storage();
  const env = {
    ...ENV,
    BETTERSTACK_WEBHOOK_SECRET: "fixture",
    ZULIP_BOT_EMAIL_BETTERSTACK: "bs@example.invalid",
    ZULIP_BOT_API_KEY_BETTERSTACK: "bs-key",
  };
  const writer = new AlertWriter({ storage: db }, env);
  const empty = () =>
    new Request("https://test/webhook/betterstack", {
      method: "POST",
      headers: { "x-betterstack-token": "fixture" },
    });
  const limited = zulipStub(() => ({ status: 429, headers: { "retry-after": "120" } }));
  await withFetch(limited.fetchStub, () => writer.fetch(empty()));
  assert.ok(db.data.get("zulip-pause:betterstack") > Date.now());
  assert.ok(db.data.get("notice:empty-body:betterstack") < Date.now() - 23 * 60 * 60 * 1000);
});

test("a writer that cannot be reached keeps a 5xx and logs one line, with no error text", async () => {
  const env = {
    ...ENV,
    ALERT_WRITER: {
      idFromName: (name) => name,
      get: () => ({
        fetch: async () => {
          throw new Error("Exceeded allowed volume of requests in Durable Objects free tier private-detail");
        },
      }),
    },
  };
  const lines = [];
  const saved = console.error;
  console.error = (line) => lines.push(line);
  try {
    const response = await worker.fetch(
      new Request("https://test/webhook/generic", {
        method: "POST",
        headers: { authorization: "Bearer fixture" },
        body: "{}",
      }),
      env,
    );
    assert.equal(response.status, 503);
    assert.equal(response.headers.get("retry-after"), "60");
  } finally {
    console.error = saved;
  }
  assert.equal(lines.length, 1);
  assert.equal(JSON.parse(lines[0]).capped, true);
  assert.equal(lines[0].includes("private-detail"), false);
});

test("a sweep that fails or cannot reach the writer is reported", async () => {
  const lines = [];
  const saved = console.error;
  console.error = (line) => lines.push(JSON.parse(line));
  const run = async (binding) => {
    const pending = [];
    await worker.scheduled({}, { ...ENV, ALERT_WRITER: binding }, { waitUntil: (p) => pending.push(p) });
    await Promise.all(pending);
  };
  try {
    await run(stubWriter(() => new Response("{}", { status: 503 })).binding);
    await run(stubWriter(() => { throw new Error("gone"); }).binding);
    await run(stubWriter(() => new Response("{}")).binding);
  } finally {
    console.error = saved;
  }
  assert.deepEqual(lines.map((line) => line.event), ["alert.sweep_failed", "alert.writer_unreachable"]);
  assert.equal(lines[0].status, 503);
});

function fakeCaches() {
  const entries = new Map();
  return {
    entries,
    default: {
      async match(request) {
        return entries.get(request.url)?.clone();
      },
      async put(request, response) {
        entries.set(request.url, response.clone());
      },
    },
  };
}

test("GET /status is served from the cache for its window, with no auth and the same body", async () => {
  const saved = globalThis.caches;
  const caches = fakeCaches();
  globalThis.caches = caches;
  const body = JSON.stringify({ summary: { topics: 1 } });
  const writer = stubWriter(() => new Response(body, { headers: { "Content-Type": "application/json" } }));
  const env = { ...ENV, ALERT_WRITER: writer.binding };
  const pending = [];
  const ctx = { waitUntil: (p) => pending.push(p) };
  try {
    const first = await worker.fetch(new Request("https://test/status"), env, ctx);
    await Promise.all(pending);
    assert.equal(first.status, 200);
    assert.equal(first.headers.get("x-status-cache"), "miss");
    assert.equal(first.headers.get("cache-control"), "public, max-age=30");
    assert.equal(await first.text(), body);
    // A query string cannot be used to miss the cache.
    const second = await worker.fetch(new Request("https://test/status?bust=1"), env, ctx);
    assert.equal(second.headers.get("x-status-cache"), "hit");
    assert.equal(await second.text(), body);
    assert.equal(writer.seen.fetches, 1);
  } finally {
    globalThis.caches = saved;
  }
});

test("no method walks around the /status cache", async () => {
  const saved = globalThis.caches;
  const caches = fakeCaches();
  globalThis.caches = caches;
  const methods = [];
  const writer = stubWriter((request) => {
    methods.push(request.method);
    return new Response("{}");
  });
  const env = { ...ENV, ALERT_WRITER: writer.binding };
  const pending = [];
  const ctx = { waitUntil: (p) => pending.push(p) };
  try {
    for (const method of ["POST", "PUT", "DELETE", "PATCH"]) {
      const response = await worker.fetch(new Request("https://test/status", { method }), env, ctx);
      assert.equal(response.status, 405);
    }
    assert.equal(writer.seen.fetches, 0);
    // A HEAD miss reads as a GET, and what it stores serves the next GET.
    assert.equal((await worker.fetch(new Request("https://test/status", { method: "HEAD" }), env, ctx)).status, 200);
    await Promise.all(pending);
    assert.deepEqual(methods, ["GET"]);
    const next = await worker.fetch(new Request("https://test/status"), env, ctx);
    assert.equal(next.headers.get("x-status-cache"), "hit");
    assert.equal(await next.text(), "{}");
    assert.equal(writer.seen.fetches, 1);
  } finally {
    globalThis.caches = saved;
  }
});

test("a failed /status is not cached, and a broken cache does not fail the read", async () => {
  const saved = globalThis.caches;
  const caches = fakeCaches();
  globalThis.caches = caches;
  const failing = stubWriter(() => new Response("{}", { status: 503 }));
  try {
    assert.equal((await worker.fetch(new Request("https://test/status"), { ...ENV, ALERT_WRITER: failing.binding })).status, 503);
    assert.equal(caches.entries.size, 0);
    globalThis.caches = {
      default: {
        match: async () => { throw new Error("cache down"); },
        put: async () => { throw new Error("cache down"); },
      },
    };
    const working = stubWriter(() => new Response("{}"));
    assert.equal((await worker.fetch(new Request("https://test/status"), { ...ENV, ALERT_WRITER: working.binding })).status, 200);
  } finally {
    globalThis.caches = saved;
  }
});
