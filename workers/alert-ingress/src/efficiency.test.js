// Usage guards and efficiency: the Zulip timer, unchanged deliveries, the pending
// index, Zulip 429s, the storm breaker, inventory overflow and the stateless layer.
//
// Run: node --test workers/alert-ingress/src/*.test.js

import test from "node:test";
import assert from "node:assert/strict";
import worker, { AlertWriter, __testing } from "./index.js";
import {
  DELIVERIES_PER_DAY,
  DeliveryBreaker,
  NEW_KEYS_PER_DAY,
  SHED_WRITES_PER_DAY,
  SOURCE_DAY_RESERVE,
  SOURCE_EVENTS_PER_MINUTE,
  SOURCE_NEW_KEY_RESERVE,
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
    async list({ prefix, limit = Infinity, startAfter }) {
      counts.listed.push(prefix);
      const sorted = [...data].sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
      return new Map(
        sorted
          .filter(([key]) => key.startsWith(prefix) && (startAfter === undefined || key > startAfter))
          .slice(0, limit),
      );
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
  for (let i = 0; i < 4; i++) await breaker.admit("a");
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

// GET /status holds without the Cache API, which this suite no longer stubs:
// it is only documented as working on custom domains.

test("GET /status is served from memory for its window, with no auth and the same body", async () => {
  __testing.resetStatusMemo();
  const body = JSON.stringify({ summary: { topics: 1 } });
  const writer = stubWriter(() => new Response(body, { headers: { "Content-Type": "application/json" } }));
  const env = { ...ENV, ALERT_WRITER: writer.binding };
  const first = await worker.fetch(new Request("https://test/status"), env);
  assert.equal(first.status, 200);
  assert.equal(first.headers.get("x-status-cache"), "miss");
  assert.equal(first.headers.get("cache-control"), "public, max-age=30");
  assert.equal(await first.text(), body);
  // A query string cannot be used to miss the copy.
  const second = await worker.fetch(new Request("https://test/status?bust=1"), env);
  assert.equal(second.headers.get("x-status-cache"), "hit");
  assert.equal(await second.text(), body);
  assert.equal(writer.seen.fetches, 1);
  __testing.resetStatusMemo();
});

test("the /status copy expires with its window", async () => {
  __testing.resetStatusMemo();
  const clock = Date.now;
  let now = 1_800_000_000_000;
  Date.now = () => now;
  try {
    const writer = stubWriter(() => new Response("{}"));
    const env = { ...ENV, ALERT_WRITER: writer.binding };
    await worker.fetch(new Request("https://test/status"), env);
    now += 29_000;
    await worker.fetch(new Request("https://test/status"), env);
    assert.equal(writer.seen.fetches, 1);
    now += 2_000;
    await worker.fetch(new Request("https://test/status"), env);
    assert.equal(writer.seen.fetches, 2);
  } finally {
    Date.now = clock;
    __testing.resetStatusMemo();
  }
});

test("no method walks around the /status copy", async () => {
  __testing.resetStatusMemo();
  const methods = [];
  const writer = stubWriter((request) => {
    methods.push(request.method);
    return new Response("{}");
  });
  const env = { ...ENV, ALERT_WRITER: writer.binding };
  for (const method of ["POST", "PUT", "DELETE", "PATCH"]) {
    assert.equal((await worker.fetch(new Request("https://test/status", { method }), env)).status, 405);
  }
  assert.equal(writer.seen.fetches, 0);
  // A HEAD miss reads as a GET, and what it keeps serves the next GET.
  const head = await worker.fetch(new Request("https://test/status", { method: "HEAD" }), env);
  assert.equal(head.status, 200);
  assert.equal(head.body, null);
  assert.deepEqual(methods, ["GET"]);
  const next = await worker.fetch(new Request("https://test/status"), env);
  assert.equal(next.headers.get("x-status-cache"), "hit");
  assert.equal(await next.text(), "{}");
  assert.equal(writer.seen.fetches, 1);
  __testing.resetStatusMemo();
});

test("a failed /status is not kept", async () => {
  __testing.resetStatusMemo();
  const failing = stubWriter(() => new Response("{}", { status: 503 }));
  const env = { ...ENV, ALERT_WRITER: failing.binding };
  assert.equal((await worker.fetch(new Request("https://test/status"), env)).status, 503);
  assert.equal((await worker.fetch(new Request("https://test/status"), env)).status, 503);
  assert.equal(failing.seen.fetches, 2);
  __testing.resetStatusMemo();
});

test("the writer does one full /status read per window, and a delivery ends the window", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const read = () => writer.fetch(new Request("https://test/status"));
  await withFetch(zulipStub().fetchStub, async () => {
    await hook(writer, firing());
    assert.equal((await (await read()).json()).summary.alerts, 1);
    const listed = db.counts.listed.length;
    assert.equal((await (await read()).json()).summary.alerts, 1);
    assert.equal(db.counts.listed.length, listed);
    await hook(writer, firing("two"));
    assert.equal((await (await read()).json()).summary.alerts, 2);
  });
});

// Review fixes:  the breaker's state

test("a breaker read that fails refuses the delivery and keeps the day's count", async () => {
  let clock = 1_800_000_000_000;
  const db = storage();
  const day = Math.floor(clock / 86_400_000);
  db.data.set("breaker:v1", { day, total: 4000, shedWrites: 7, newKeys: 9, sources: { a: 4000 }, newBySource: { a: 9 } });
  const get = db.get;
  db.get = async () => { throw new Error("storage down"); };
  const breaker = new DeliveryBreaker(db, () => clock);
  await assert.rejects(breaker.admit("a"), (error) => error.code === "breaker_unavailable");
  assert.equal(breaker.loaded, false);
  db.get = get;
  await breaker.admit("a");
  assert.equal(breaker.total, 4001);
  assert.equal(breaker.newKeys, 9);
  clock += 60_000;
  for (let i = 0; i < 5; i++) await breaker.admit("a");
  // The save carries the real count, not a count restarted from zero.
  assert.equal(db.data.get("breaker:v1").total, 4005);
});

test("a storage read failure answers the vendor 503, writing nothing", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const get = db.get;
  db.get = async (key) => {
    if (key === "breaker:v1") throw new Error("storage down");
    return get(key);
  };
  const saved = console.error;
  console.error = () => {};
  try {
    await withFetch(zulipStub().fetchStub, async () => {
      assert.equal((await hook(writer, firing())).status, 503);
      assert.equal(db.data.has("kv:state:generic:one"), false);
    });
  } finally {
    console.error = saved;
  }
});

test("a malformed breaker record starts the day over instead of stopping deliveries", async () => {
  const clock = 1_800_000_000_000;
  const db = storage();
  const day = Math.floor(clock / 86_400_000);
  db.data.set("breaker:v1", { day, total: "many", shedWrites: -3, newKeys: 1.5, sources: { a: "x" }, shed: null });
  const breaker = new DeliveryBreaker(db, () => clock);
  assert.equal((await breaker.admit("a")).shed, false);
  assert.equal(breaker.total, 1);
  assert.equal(breaker.shedWrites, 0);
  assert.equal(breaker.newKeys, 0);
  assert.equal(breaker.perSourceDay.get("a"), 1);
  db.data.set("breaker:v1", "garbage");
  const lines = [];
  const saved = console.error;
  console.error = (line) => lines.push(JSON.parse(line).event);
  try {
    assert.equal((await new DeliveryBreaker(db, () => clock).admit("a")).shed, false);
  } finally {
    console.error = saved;
  }
  assert.deepEqual(lines, ["alert.breaker_state_invalid"]);
});

test("an out-of-range saved count is logged and cannot shut the day's caps", async () => {
  const clock = 1_800_000_000_000;
  const db = storage();
  const day = Math.floor(clock / 86_400_000);
  db.data.set("breaker:v1", {
    day,
    total: 1e12,
    shedWrites: 1e12,
    newKeys: 1e12,
    sources: { a: 1e12 },
    newBySource: { a: 1e12 },
    shed: { "day:a": 1e12 },
  });
  const lines = [];
  const saved = console.error;
  console.error = (line) => lines.push(JSON.parse(line).event);
  try {
    const breaker = new DeliveryBreaker(db, () => clock);
    assert.equal((await breaker.admit("a")).shed, false);
    assert.equal(breaker.total, 1);
    assert.equal(await breaker.spendShedWrite(), true);
    assert.equal(await breaker.spendNewKey("a"), true);
    assert.equal(breaker.shed.get("day:a"), 0);
  } finally {
    console.error = saved;
  }
  assert.deepEqual(lines, ["alert.breaker_state_invalid"]);
});

test("a saved map of the wrong shape is logged, and a missing one is not", async () => {
  const clock = 1_800_000_000_000;
  const day = Math.floor(clock / 86_400_000);
  const loadLogs = async (record) => {
    const db = storage();
    db.data.set("breaker:v1", { day, total: 3, ...record });
    const lines = [];
    const saved = console.error;
    console.error = (line) => lines.push(JSON.parse(line).event);
    try {
      const breaker = new DeliveryBreaker(db, () => clock);
      assert.equal((await breaker.admit("a")).shed, false);
      assert.equal(breaker.total, 4);
    } finally {
      console.error = saved;
    }
    return lines;
  };
  for (const bad of [{ sources: 5 }, { newBySource: "nope" }, { shed: null }, { sources: [1] }]) {
    assert.deepEqual(await loadLogs(bad), ["alert.breaker_state_invalid"]);
  }
  assert.deepEqual(await loadLogs({}), []);
});

test("a sound saved record, even one past every cap, logs nothing and is believed", async () => {
  const clock = 1_800_000_000_000;
  const db = storage();
  const day = Math.floor(clock / 86_400_000);
  db.data.set("breaker:v1", { day, total: 900_000, shedWrites: 2000, newKeys: 1000, sources: { a: 900_000 }, newBySource: { a: 900 } });
  const lines = [];
  const saved = console.error;
  console.error = (line) => lines.push(line);
  try {
    const breaker = new DeliveryBreaker(db, () => clock);
    assert.equal((await breaker.admit("a")).reason, "day");
    assert.equal(await breaker.spendShedWrite(), false);
    assert.equal(await breaker.spendNewKey("a"), false);
  } finally {
    console.error = saved;
  }
  assert.deepEqual(lines, []);
});

// Review fixes:  the pending index

function pendingRow(db, id, since) {
  db.data.set(`kv:state:generic:${id}`, JSON.stringify({ phase: "pending", severity: "SEVERE", since, updated: since, text: "t", link: "" }));
  db.data.set(`kv:svc:generic:${id}`, JSON.stringify("svc"));
  db.data.set(`pending:generic:${id}`, 1);
}

test("the hourly reconcile writes nothing for an index entry that is already there", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const puts = [];
  const put = db.put;
  db.put = async (key, value) => {
    puts.push(key);
    return put(key, value);
  };
  for (let i = 0; i < 3; i++) pendingRow(db, `k${i}`, Date.now());
  db.data.set("kv:state:generic:missed", JSON.stringify({ phase: "pending", severity: "SEVERE", since: Date.now(), updated: Date.now(), text: "t", link: "" }));
  const { fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    await sweepOnce(writer);
    // The first pass adds only the entry that was missing.
    assert.deepEqual(puts.filter((key) => key.startsWith("pending:")), ["pending:generic:missed"]);
    puts.length = 0;
    db.data.set("sweep:reconciled-at", Date.now() - 61 * 60 * 1000);
    await sweepOnce(writer);
    assert.deepEqual(puts.filter((key) => key.startsWith("pending:")), []);
  });
});

test("every pending incident gets a turn when the index is longer than one read", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const now = Date.now();
  for (let i = 0; i < 700; i++) pendingRow(db, `k${String(i).padStart(4, "0")}`, now);
  // The last one sorts past the first read, and it is the only one that aged out.
  pendingRow(db, "z-aged", now - 60 * 60 * 1000);
  db.data.set("sweep:reconciled-at", now);
  const { fetchStub } = zulipStub();
  await withFetch(fetchStub, async () => {
    const first = await (await sweepOnce(writer)).json();
    assert.equal(first.checked, 500);
    assert.equal(first.promoted, 0);
    assert.equal(db.data.get("sweep:pending-cursor"), "generic:k0499");
    const second = await (await sweepOnce(writer)).json();
    assert.equal(second.checked, 201);
    assert.equal(second.promoted, 1);
    assert.equal(state(db, "generic:z-aged").phase, "alerting");
    // The end of the index puts the next sweep back at the start.
    assert.equal(db.data.has("sweep:pending-cursor"), false);
    assert.equal((await (await sweepOnce(writer)).json()).checked, 500);
  });
});

test("an idle sweep over a short index writes no cursor", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  pendingRow(db, "one", Date.now());
  db.data.set("sweep:reconciled-at", Date.now());
  await withFetch(zulipStub().fetchStub, () => sweepOnce(writer));
  assert.equal(db.data.has("sweep:pending-cursor"), false);
});

test("a sweep that runs out of budget resumes with the incident it did not reach", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const old = Date.now() - 60 * 60 * 1000;
  for (let i = 0; i < 520; i++) pendingRow(db, `k${String(i).padStart(4, "0")}`, old);
  db.data.set("sweep:reconciled-at", Date.now());
  const { fetchStub } = zulipStub();
  const saved = console.error;
  console.error = () => {};
  try {
    await withFetch(fetchStub, async () => {
      assert.equal((await (await sweepOnce(writer)).json()).promoted, 40);
      assert.equal(db.data.get("sweep:pending-cursor"), "generic:k0039");
      await sweepOnce(writer);
      assert.equal(state(db, "generic:k0040").phase, "alerting");
    });
  } finally {
    console.error = saved;
  }
});

test("the reconcile drops stale entries past the first read of the index", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  for (let i = 0; i < 620; i++) db.data.set(`pending:generic:gone${String(i).padStart(4, "0")}`, 1);
  await withFetch(zulipStub().fetchStub, () => sweepOnce(writer));
  assert.equal([...db.data.keys()].some((key) => key.startsWith("pending:")), false);
});

// Review fixes:  failures on the Zulip side

test("a raw copy whose rate-limit bookkeeping fails cannot change the delivery's answer", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const put = db.put;
  db.put = async (key, value) => {
    if (key.startsWith("zulip-pause:")) throw new Error("storage down");
    return put(key, value);
  };
  const limited = zulipStub((call) => (call.to === "Alerts-Raw" ? { status: 429 } : null));
  const saved = console.error;
  console.error = () => {};
  try {
    await withFetch(limited.fetchStub, async () => {
      assert.equal((await hook(writer, firing())).status, 200);
      assert.equal(state(db, "generic:one").phase, "pending");
    });
  } finally {
    console.error = saved;
  }
});

test("a notice Zulip did not confirm is tried again soon, not a whole interval later", async () => {
  const db = storage();
  const env = {
    ...ENV,
    BETTERSTACK_WEBHOOK_SECRET: "fixture",
    ZULIP_BOT_EMAIL_BETTERSTACK: "bs@example.invalid",
    ZULIP_BOT_API_KEY_BETTERSTACK: "bs-key",
  };
  const writer = new AlertWriter({ storage: db }, env);
  const empty = () =>
    new Request("https://test/webhook/betterstack", { method: "POST", headers: { "x-betterstack-token": "fixture" } });
  const saved = console.error;
  console.error = () => {};
  try {
    await withFetch(async () => ({ ok: true, json: async () => ({ result: "error" }) }), () => writer.fetch(empty()));
  } finally {
    console.error = saved;
  }
  assert.ok(db.data.get("notice:empty-body:betterstack") < Date.now() - 23 * 60 * 60 * 1000);
});

async function queuedLine(db, writer) {
  await withFetch(zulipStub().fetchStub, () => hook(writer, firing()));
  db.data.set("kv:state:generic:one", JSON.stringify({ ...state(db, "generic:one"), phase: "alerting", severity: "MODERATE" }));
  db.data.delete("pending:generic:one");
  await withFetch(zulipStub((call) => (call.to === "Alerts-Log" ? { status: 429 } : null)).fetchStub, () =>
    hook(writer, { id: "one", service: "svc", status: "recovered" }),
  );
  db.data.set("zulip-pause:generic", Date.now() - 1);
}
const queued = (db) => [...db.data.keys()].filter((key) => key.startsWith("queued-event:")).length;

test("a queued line survives a storage error, and only a refusal drops it", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const saved = console.error;
  console.error = () => {};
  try {
    await queuedLine(db, writer);
    assert.equal(queued(db), 1);
    const get = db.get;
    db.get = async (key) => {
      if (key.startsWith("event:")) throw new Error("storage down");
      return get(key);
    };
    await withFetch(zulipStub().fetchStub, () => sweepOnce(writer));
    assert.equal(queued(db), 1);
    db.get = get;
    await withFetch(zulipStub((call) => (call.to === "Alerts-Log" ? { status: 403 } : null)).fetchStub, () => sweepOnce(writer));
    assert.equal(queued(db), 0);
  } finally {
    console.error = saved;
  }
});

test("a credential in alert text never reaches a state row or a queued line", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const secret = `ghp_${"a".repeat(36)}`;
  const posted = [];
  const fetchStub = async (url, opts) => {
    posted.push(String(opts.body || ""));
    return { ok: true, json: async () => ({ result: "success", id: 100 + posted.length }) };
  };
  await withFetch(fetchStub, () => hook(writer, firing("one", `down ${secret}`)));
  assert.equal(state(db, "generic:one").text.includes(secret), false);
  assert.equal(state(db, "generic:one").text.includes("[redacted]"), true);
  assert.equal(posted.some((body) => body.includes(secret)), false);
});

// Review fixes:  the new-key cap, and nothing shed in silence

function capped(db, env = ENV) {
  const writer = new AlertWriter({ storage: db }, env);
  writer.breaker.loaded = true;
  writer.breaker.newKeys = NEW_KEYS_PER_DAY;
  writer.breaker.newBySource.set("generic", SOURCE_NEW_KEY_RESERVE);
  return writer;
}

async function capturingLogs(run) {
  const lines = [];
  const saved = console.error;
  console.error = (line) => lines.push(JSON.parse(line));
  try {
    await run();
  } finally {
    console.error = saved;
  }
  return lines;
}

test("past the new-key cap, a new incident gets 503, costs no state row, and is logged and counted", async () => {
  const db = storage();
  const writer = capped(db);
  const { calls, fetchStub } = zulipStub();
  let response;
  const lines = await capturingLogs(() =>
    withFetch(fetchStub, async () => {
      response = await hook(writer, firing("brand-new"));
      await hook(writer, firing("brand-newer"));
    }),
  );
  assert.equal(response.status, 503);
  assert.ok(Number(response.headers.get("retry-after")) > 0);
  assert.equal((await response.json()).reason, "new-keys");
  assert.equal(db.data.has("kv:state:generic:brand-new"), false);
  // One log line a minute, one notice an hour, and each delivery counted.
  const refused = lines.filter((line) => line.event === "alert.delivery_refused");
  assert.equal(refused.length, 1);
  assert.equal(refused[0].reason, "new-keys");
  assert.equal(calls.filter((call) => call.topic === "alert-ingress").length, 1);
  assert.equal(writer.breaker.shed.get("new-keys:generic"), 2);
  // The delivery still leaves a record, for whoever reads the raw channel.
  assert.equal(calls.filter((call) => call.to === "Alerts-Raw").length, 2);
});

test("past the new-key cap, a known incident still moves and a recovery of an unknown one is answered", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  await withFetch(zulipStub().fetchStub, () => hook(writer, firing()));
  writer.breaker.newKeys = NEW_KEYS_PER_DAY;
  writer.breaker.newBySource.set("generic", SOURCE_NEW_KEY_RESERVE);
  await withFetch(zulipStub().fetchStub, async () => {
    const known = await hook(writer, { id: "one", service: "svc", status: "recovered" });
    assert.equal(known.status, 200);
    assert.equal(state(db, "generic:one").phase, "ok");
    const before = db.counts.put;
    const unknown = await hook(writer, { id: "never-seen", service: "svc", status: "recovered" });
    assert.equal(unknown.status, 200);
    assert.equal((await unknown.json()).action, "shed");
    assert.equal(db.data.has("kv:state:generic:never-seen"), false);
    assert.equal(db.counts.put, before);
  });
});

test("the new-key cap is saved at once, has a per-source reserve, and clears with the day", async () => {
  let clock = 1_800_000_000_000;
  const db = storage();
  const breaker = new DeliveryBreaker(db, () => clock);
  await breaker.load();
  assert.equal(await breaker.spendNewKey("noisy"), true);
  assert.equal(db.data.get("breaker:v1").newKeys, 1);
  for (let i = 1; i < NEW_KEYS_PER_DAY; i++) await breaker.spendNewKey("noisy");
  assert.equal(await breaker.spendNewKey("noisy"), false);
  // Another vendor keeps its own allowance, and it is a bound, not an exemption.
  for (let i = 0; i < SOURCE_NEW_KEY_RESERVE; i++) assert.equal(await breaker.spendNewKey("quiet"), true);
  assert.equal(await breaker.spendNewKey("quiet"), false);
  // An evicted writer resumes the day's count.
  const restarted = new DeliveryBreaker(db, () => clock);
  await restarted.load();
  assert.equal(await restarted.spendNewKey("noisy"), false);
  clock += 86_400_000;
  assert.equal(await restarted.spendNewKey("noisy"), true);
});

test("a refused reopen keeps its tombstone until the retry is admitted", async () => {
  const db = storage();
  const env = {
    ...ENV,
    PAGERDUTY_WEBHOOK_SECRET: "fixture",
    ZULIP_BOT_EMAIL_PAGERDUTY: "pd@example.invalid",
    ZULIP_BOT_API_KEY_PAGERDUTY: "pd-key",
  };
  const writer = capped(db, env);
  writer.breaker.newBySource.set("pagerduty", SOURCE_NEW_KEY_RESERVE);
  db.data.set("kv:closed:pagerduty:p1", "1");
  const reopen = () =>
    writer.fetch(
      new Request("https://test/webhook/pagerduty", {
        method: "POST",
        headers: { "x-pagerduty-token": "fixture" },
        body: JSON.stringify({
          event: {
            event_type: "incident.reopened",
            resource_type: "incident",
            data: { type: "incident", id: "P1", status: "triggered", title: "t", service: { summary: "svc" } },
          },
        }),
      }),
    );
  await capturingLogs(() =>
    withFetch(zulipStub().fetchStub, async () => {
      assert.equal((await reopen()).status, 503);
      assert.equal(db.data.has("kv:closed:pagerduty:p1"), true);
      writer.breaker.newKeys = 0;
      assert.equal((await reopen()).status, 200);
      assert.equal(db.data.has("kv:closed:pagerduty:p1"), false);
    }),
  );
});

test("a storm's refused and answered deliveries are all logged and counted, and /status shows them", async () => {
  const db = storage();
  const writer = new AlertWriter({ storage: db }, ENV);
  const clock = Date.now;
  Date.now = () => 1_800_000_000_000 + 1000;
  try {
    const lines = await capturingLogs(() =>
      withFetch(zulipStub().fetchStub, async () => {
        await trip(writer);
        await hook(writer, firing());
        await hook(writer, firing());
        await hook(writer, firing("brand-new"));
      }),
    );
    assert.deepEqual(
      lines.filter((line) => line.event?.startsWith("alert.delivery")).map((line) => [line.event, line.reason]),
      [["alert.delivery_shed", "minute"]],
    );
    // The refusal shares the minute's reason, so its count carries it.
    assert.equal(writer.breaker.shed.get("minute:generic"), 3);
    const status = await (await writer.fetch(new Request("https://test/status"))).json();
    assert.equal(status.guards.shed["minute:generic"], 3);
    assert.equal(status.guards.newKeys, 1);
  } finally {
    Date.now = clock;
  }
});

test("the caps together keep a day's worst case safely under the free plan's 100,000 rows", () => {
  // Measured against the real writer with a stubbed Zulip:
  // scratch rows-by-class.mjs gives these per delivery, promotion included.
  const ROWS_PER_NEW_KEY = 20;
  const ROWS_PER_TRANSITION = 7;
  const ROWS_PER_SHED_WRITE = 8;
  const SOURCES = 5;
  const deliveries = DELIVERIES_PER_DAY + SOURCE_DAY_RESERVE * (SOURCES - 1);
  const newKeys = NEW_KEYS_PER_DAY + SOURCE_NEW_KEY_RESERVE * (SOURCES - 1);
  const breakerSaves = 24 * 60;
  const sweeps = 288 + 24 + 288; // cursor, reconcile and catch-up bookkeeping
  const worst =
    newKeys * ROWS_PER_NEW_KEY +
    (deliveries - newKeys) * ROWS_PER_TRANSITION +
    SHED_WRITES_PER_DAY * ROWS_PER_SHED_WRITE +
    breakerSaves +
    sweeps;
  assert.ok(worst < 90_000, `worst case is ${worst} rows`);
});
