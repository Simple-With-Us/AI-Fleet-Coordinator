// Shared alert ingress — one door for every inbound signal.
//
// The problem this solves is not "too many alerts", it is that every inbound
// integration shared one delivery model: one new message per event, no state,
// no structure.  A monitor that flaps ten times in an hour produced ten
// messages, and the channel could only be read by scrolling.
//
// The model here instead:
//
//   inbound event -> normalize -> state transition -> decide
//   decide() posts a message ONLY on a real state change (open, escalate,
//   resolve).  Everything else just rewrites one standing status message per
//   topic via PATCH.
//
// So the channel becomes: one editable status block per topic that always shows
// current truth, plus a sparse timeline of events that actually mattered.
//
// Routes:
//   POST /webhook/:source     receive a vendor event (signature-verified)
//   GET  /status/:channel/:topic   rebuild the standing status (operator token required)
//   GET  /sweep               promote incidents that aged past the window
//
// KV layout:
//   state:<key>        AlertState per alertable thing
//   status:<chan>:<topic>  message id of that topic's standing status
//   svc:<key>          service name, so a topic stays stable across events

import { normalize } from "./normalize.js";
import {
  decide,
  sweep,
  initialState,
  DEFAULT_MIN_OPEN_MS,
  PROD_DOWN_MIN_OPEN_MS,
} from "./state.js";
import { postEvent, writeStatus, renderStatus } from "./zulip.js";

const DEFAULT_CHANNEL = "alerts";

const stateKey = (k) => `state:${k}`;
const statusKey = (c, t) => `status:${c}:${t}`;
const svcKey = (k) => `svc:${k}`;
// Topic membership is its own key.  Deriving the topic by splitting the `idx:`
// key on ":" would mis-parse any service name containing a colon, so the topic
// list is tracked explicitly instead of inferred.
const topicKey = (c, t) => `topic:${c}:${t}`;

/** Constant-time-ish compare so signature checks do not leak by timing. */
function safeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function hmacHex(secret, body, algo = "SHA-256") {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: algo },
    false,
    ["sign"],
  );
  // sign() takes a BufferSource, not a string: passing the raw request text
  // straight through throws and every signed webhook fails before comparison.
  const bytes =
    typeof body === "string" ? new TextEncoder().encode(body) : body;
  const mac = await crypto.subtle.sign("HMAC", key, bytes);
  return [...new Uint8Array(mac)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

/**
 * Verify a vendor signature.  Each vendor signs differently; a vendor with no
 * verifier configured is REJECTED rather than trusted, so an unsigned POST can
 * never inject a fake alert.
 */
async function verify(request, raw, env, source) {
  if (source === "datadog") {
    const secret = env.DATADOG_WEBHOOK_SECRET;
    if (!secret) return false;
    // Datadog's webhook integration sends static custom headers; it has no
    // facility to compute a per-body HMAC, so an HMAC-only gate could never be
    // satisfied by a real Datadog delivery.  Accept the configured static
    // header, matching how Better Stack is authenticated.
    const token = request.headers.get("x-datadog-token") || "";
    if (token) return safeEqual(token, secret);
    // Still honour a genuine body signature for senders that can produce one.
    const sig = request.headers.get("x-datadog-signature") || "";
    const expected = await hmacHex(secret, raw);
    return safeEqual(sig, expected);
  }
  if (source === "betterstack") {
    const secret = env.BETTERSTACK_WEBHOOK_SECRET;
    if (!secret) return false;
    // Better Stack supports static custom headers, not body signatures.
    return safeEqual(request.headers.get("x-betterstack-token") || "", secret);
  }
  return authorized(request, env);
}

/** Header-only shared-token gate; credentials must never be placed in URLs. */
function authorized(request, env) {
  const token = request.headers.get("authorization")?.match(/^Bearer\s+(\S+)$/i)?.[1] || "";
  return (
    !!env.INGRESS_SHARED_TOKEN && safeEqual(token, env.INGRESS_SHARED_TOKEN)
  );
}

/** All tracked states for one topic, with their service labels resolved. */
async function collectStates(env, channel, topic) {
  const prefix = `idx:${channel}:${topic}:`;
  const keys = { keys: await listAll(env.ALERT_STATE, prefix) };
  // Parallel reads: serial get pairs cost 2N KV round-trips per call.
  const fetched = await mapBounded(keys.keys, async ({ name }) => {
    const key = name.slice(prefix.length);
    const raw = await env.ALERT_STATE.get(stateKey(key), "json");
    if (!raw) return null;
    const svc = (await env.ALERT_STATE.get(svcKey(key), "json")) || key;
    return { key, ...raw, service: svc };
  });
  return fetched.filter(Boolean);
}

/** Every topic that has at least one tracked signal. */
async function collectTopics(env) {
  const prefix = `topic:${DEFAULT_CHANNEL}:`;
  const seen = new Set();
  const keys = { keys: await listAll(env.ALERT_STATE, prefix) };
  for (const { name } of keys.keys) {
    const topic = name.slice(prefix.length);
    if (topic) seen.add(topic);
  }
  const topics = {};
  let firing = 0;
  let total = 0;
  // Parallel across topics: serial collectStates made /status cost ~1 + T*(1+2A) KV calls.
  const perTopic = await mapBounded([...seen].sort(), async (topic) => [
    topic,
    await collectStates(env, DEFAULT_CHANNEL, topic),
  ]);
  for (const [topic, states] of perTopic) {
    // A topic whose states have all been cleared is dropped rather than
    // rendered as an empty card, so the view self-cleans as incidents resolve.
    if (states.length === 0) continue;
    const tf = states.filter((s) => s.phase === "alerting").length;
    firing += tf;
    total += states.length;
    topics[topic] = {
      firing: tf,
      total: states.length,
      alerts: states
        .map((s) => ({
          key: s.key,
          service: s.service,
          phase: s.phase,
          severity: s.phase === "alerting" ? s.severity : "INFO",
          text: s.text,
          link: s.link,
          since: s.since,
          updated: s.updated,
          prodDown: s.prodDown === true,
        }))
        .sort((a, b) => {
          const rank = { alerting: 0, pending: 1, ok: 2 };
          return (
            rank[a.phase] - rank[b.phase] || a.service.localeCompare(b.service)
          );
        }),
    };
  }
  return { topics, firing, total };
}

/**
 * Central Time stamp in the fleet's house style: 12-hour, lowercase meridiem,
 * no space before it (`Oct 18, 5:39pm`).  `toLocaleString` emits `5:39 PM`, so
 * normalize it -- zulip-fleet-guide.md requires `3:15pm`, never `3:15 PM`.
 */
function ctStamp() {
  const s = new Date().toLocaleString("en-US", {
    timeZone: "America/Chicago",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
  return s.replace(/\s*(AM|PM)$/, (_, m) => m.toLowerCase());
}

/** Rewrite the standing status block for a topic from all its states. */
async function refreshStatus(env, channel, topic) {
  const states = await collectStates(env, channel, topic);
  const content = renderStatus({ topic, states, updatedAt: ctStamp() });
  if (!env.WRITER_STORAGE) throw new Error("durable writer required");
  const outcomeKey = `delivery:${JSON.stringify([channel, topic])}`;
  const prior = await env.WRITER_STORAGE.get(outcomeKey);
  if (prior?.outcome === "unknown" || prior?.outcome === "refused")
    return content;
  const statusId =
    prior?.id ||
    Number(await env.ALERT_STATE.get(statusKey(channel, topic), "json")) ||
    null;
  // Commit uncertainty before crossing the network.  A crash or ambiguous reply
  // cannot turn a possibly accepted create into another automatic POST.
  await env.WRITER_STORAGE.put(outcomeKey, {
    outcome: "unknown",
    id: statusId,
  });
  try {
    const id = await writeStatus({
      email: env.ZULIP_BOT_EMAIL,
      apiKey: env.ZULIP_BOT_API_KEY,
      channel,
      topic,
      content,
      statusId,
    });
    if (id) {
      await env.WRITER_STORAGE.put(outcomeKey, { outcome: "ok", id });
      await env.ALERT_STATE.put(statusKey(channel, topic), JSON.stringify(id));
    }
  } catch (error) {
    if (error?.outcome === "refused") {
      await env.WRITER_STORAGE.put(outcomeKey, {
        outcome: "refused",
        id: statusId,
      });
    }
  }
  return content;
}

function advanceRevision(previous, next) {
  const prior = Number.isSafeInteger(previous?.transitionRevision)
    ? previous.transitionRevision
    : 0;
  const changed =
    previous?.phase !== next.phase || previous?.severity !== next.severity;
  return { ...next, transitionRevision: prior + (changed ? 1 : 0) };
}

function transitionIdentity(key, action, previous, next) {
  // Refresh timestamps and descriptive metadata never create another delivery
  // identity for the same incident generation and severity transition.
  return [
    key,
    action,
    previous?.phase,
    previous?.since,
    previous?.transitionRevision || 0,
    next.severity,
  ];
}

async function sendEventOnce(env, identity, args) {
  if (!env.WRITER_STORAGE) throw new Error("durable writer required");
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(JSON.stringify(identity)),
  );
  const key =
    "event:" +
    [...new Uint8Array(digest)]
      .map((value) => value.toString(16).padStart(2, "0"))
      .join("");
  const prior = await env.WRITER_STORAGE.get(key);
  if (prior?.outcome === "ok") return prior.id;
  if (prior?.outcome === "unknown") return null;
  await env.WRITER_STORAGE.put(key, { outcome: "unknown" });
  try {
    const id = await postEvent(args);
    if (id) await env.WRITER_STORAGE.put(key, { outcome: "ok", id });
    return id;
  } catch (error) {
    if (error?.outcome === "refused") {
      await env.WRITER_STORAGE.put(key, { outcome: "refused" });
      throw new Error("event definitively refused");
    }
    return null;
  }
}

async function boundedService(value) {
  const chars = Array.from(value);
  if (chars.length <= 60) return value;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  const suffix = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, "0")).join("").slice(0, 10);
  return chars.slice(0, 49).join("") + "-" + suffix;
}

async function handleEvent(source, payload, env) {
  const event = normalize(source, payload);
  const channel = DEFAULT_CHANNEL;
  // Keep the original topic for a stable key when vendor labels change.
  const priorService = await env.ALERT_STATE.get(svcKey(event.key), "json");
  if (typeof priorService === "string" && priorService) event.service = priorService;
  event.service = await boundedService(event.service);
  const topic = event.service;

  const prev = await env.ALERT_STATE.get(stateKey(event.key), "json");
  const now = Date.now();
  const result = decide(prev, event, now, {
    minOpenMs: Number(env.MIN_OPEN_MS) || DEFAULT_MIN_OPEN_MS,
  });

  result.state = advanceRevision(prev, result.state);
  const inventory = await collectStates(env, channel, topic);
  if (
    !inventory.some((row) => row.key === event.key) &&
    inventory.length >= MAX_KEYS
  )
    throw new Error("inventory bound exceeded");
  let posted = null;
  const sev =
    result.state.phase === "alerting" ? result.state.severity : "INFO";
  if (result.action === "announce" || result.action === "update") {
    posted = await sendEventOnce(
      env,
      transitionIdentity(event.key, result.action, prev, result.state),
      {
        email: env.ZULIP_BOT_EMAIL,
        apiKey: env.ZULIP_BOT_API_KEY,
        channel,
        topic,
        severity: sev,
        service: event.service,
        what: result.state.text,
        link: result.state.link,
      },
    );
  } else if (result.action === "resolve") {
    posted = await sendEventOnce(
      env,
      transitionIdentity(event.key, result.action, prev, result.state),
      {
        email: env.ZULIP_BOT_EMAIL,
        apiKey: env.ZULIP_BOT_API_KEY,
        channel,
        topic,
        severity: "INFO",
        service: event.service,
        what: "recovered",
        link: event.link,
      },
    );
  }

  await env.ALERT_STATE.put(stateKey(event.key), JSON.stringify(result.state));
  await env.ALERT_STATE.put(svcKey(event.key), JSON.stringify(event.service));
  await env.ALERT_STATE.put(`idx:${channel}:${topic}:${event.key}`, "1");
  await env.ALERT_STATE.put(topicKey(channel, topic), "1");

  // Every outcome refreshes the standing status, which is what makes the
  // channel readable at a glance instead of by scrolling.
  const status = await refreshStatus(env, channel, topic);

  return { action: result.action, posted: !!posted, status };
}

/** Promote aged incidents.  Wire as a cron trigger. */
async function runSweep(env) {
  const now = Date.now();
  const minOpenMs = Number(env.MIN_OPEN_MS) || DEFAULT_MIN_OPEN_MS;
  const all = { keys: await listAll(env.ALERT_STATE, "state:") };
  await collectTopics(env); // Complete every dependent inventory before the first send.
  let promoted = 0;
  const dirtyTopics = new Set();
  for (const { name } of all.keys) {
    const key = name.slice("state:".length);
    const prev = await env.ALERT_STATE.get(name, "json");
    const s = sweep(prev, now, { minOpenMs });
    if (!s) continue;
    s.state = advanceRevision(prev, s.state);
    try {
      const service = (await env.ALERT_STATE.get(svcKey(key), "json")) || key;
      await sendEventOnce(
        env,
        transitionIdentity(key, "announce", prev, s.state),
        {
          email: env.ZULIP_BOT_EMAIL,
          apiKey: env.ZULIP_BOT_API_KEY,
          channel: DEFAULT_CHANNEL,
          topic: service,
          severity: s.state.severity,
          service,
          what: s.state.text,
          link: s.state.link,
        },
      );
      await env.ALERT_STATE.put(name, JSON.stringify(s.state));
      promoted++;
      dirtyTopics.add(service);
    } catch {
      console.error(JSON.stringify({ event: "alert.promotion_failed" }));
    }
  }
  // One status refresh per topic, however many incidents promoted in it, and
  // all in parallel instead of one rebuild per incident.
  await mapBounded([...dirtyTopics], async (topic) => {
    try {
      await refreshStatus(env, DEFAULT_CHANNEL, topic);
    } catch {
      console.error(JSON.stringify({ event: "alert.status_refresh_failed" }));
    }
  });
  return { promoted, checked: all.keys.length };
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const handler = {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // Liveness, so a monitor can watch the ingress itself.
    if (url.pathname === "/health") {
      return json({ ok: true, sources: ["datadog", "betterstack", "generic"] });
    }

    if (url.pathname === "/sweep") {
      // Allow an operator to trigger the sweep by hand.
      if (!authorized(request, env)) {
        return json({ error: "unauthorized" }, 401);
      }
      return json(await runSweep(env));
    }

    // Machine-readable status for consumers that render it themselves --
    // today the Usage Monitor /ops page.  One writer, many readers, so the
    // dashboard and the Zulip channel cannot drift apart.
    if (url.pathname === "/status") {
      const collected = await collectTopics(env);
      return json({
        generatedAt: new Date().toISOString(),
        channel: DEFAULT_CHANNEL,
        summary: {
          topics: Object.keys(collected.topics).length,
          alerts: collected.total,
          firing: collected.firing,
        },
        ...collected,
      });
    }

    const statusMatch = url.pathname.match(/^\/status\/([^/]+)\/([^/]+)$/);
    if (statusMatch) {
      // This route rewrites the standing Zulip message (refreshStatus calls
      // writeStatus), so it is operator-only: an unauthenticated hit would let
      // anyone make the bot post into any channel it can reach.
      if (!authorized(request, env)) {
        return json({ error: "unauthorized" }, 401);
      }
      const [, channel, topic] = statusMatch;
      const content = await refreshStatus(
        env,
        decodeURIComponent(channel),
        decodeURIComponent(topic),
      );
      return json({ channel, topic, content });
    }

    const hookMatch = url.pathname.match(/^\/webhook\/([a-z0-9_-]{1,32})$/i);
    if (!hookMatch) return json({ error: "not found" }, 404);
    if (request.method !== "POST") return json({ error: "POST only" }, 405);

    const source = hookMatch[1].toLowerCase();
    const raw = await request.text();
    if (!(await verify(request, raw, env, source))) {
      return json({ error: "bad signature" }, 401);
    }

    if (new TextEncoder().encode(raw).byteLength > 65536) return json({ error: "payload too large" }, 413);
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      return json({ error: "invalid json" }, 400);
    }

    try {
      validatePayload(payload);
    } catch {
      return json({ error: "invalid payload" }, 400);
    }
    return json(await handleEvent(source, payload, env));
  },

  // The sweep that promotes aged incidents to real messages.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runSweep(env));
  },
};

export const __testing = {
  handleEvent,
  verify,
  listAll,
  refreshStatus,
  runSweep,
  collectStates,
  collectTopics,
  safeEqual,
  stateKey,
  statusKey,
  svcKey,
  topicKey,
  ctStamp,
  indexPrefix: (c, t) => `idx:${c}:${t}:`,
};

const MAX_KEYS = 2000;
// Object-envelope nesting bound.  A JSON:API vendor body reaches four levels
// (data.relationships.monitor.data); anything deeper is rejected, not walked.
const MAX_PAYLOAD_DEPTH = 6;
async function listAll(kv, prefix) {
  const keys = [],
    cursors = new Set();
  let cursor;
  for (let page = 0; page < 20; page++) {
    const result = await kv.list({
      prefix,
      limit: 100,
      ...(cursor ? { cursor } : {}),
    });
    if (!Array.isArray(result.keys)) throw new Error("invalid inventory");
    keys.push(...result.keys);
    if (keys.length > MAX_KEYS) throw new Error("inventory bound exceeded");
    if (result.list_complete === true || result.listComplete === true)
      return keys;
    if (
      typeof result.cursor !== "string" ||
      !result.cursor ||
      cursors.has(result.cursor)
    )
      throw new Error("incomplete inventory");
    cursor = result.cursor;
    cursors.add(cursor);
  }
  throw new Error("inventory page bound exceeded");
}
async function mapBounded(items, fn) {
  const result = new Array(items.length);
  let next = 0;
  await Promise.all(
    Array.from({ length: Math.min(4, items.length) }, async () => {
      while (next < items.length) {
        const i = next++;
        result[i] = await fn(items[i]);
      }
    }),
  );
  return result;
}
function validatePayload(payload, depth = 0) {
  const object = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value);
  if (!object(payload)) throw new Error("invalid payload");
  // Bounded depth: object envelopes nest (JSON:API reaches four levels), but a
  // hostile body must not be able to recurse without end.
  if (depth > MAX_PAYLOAD_DEPTH) throw new Error("payload nested too deep");
  if (Object.keys(payload).length > 100) throw new Error("too many fields");
  const booleans = new Set(["firing", "prod_down"]);
  const identifiers = new Set(["id", "monitor_id", "alert_id", "check_id"]);
  // JSON:API vendors nest the fields under data.attributes and the monitor
  // identity under data.relationships.monitor.data, so those are object
  // envelopes too -- not oversized text fields.
  const envelopes = new Set([
    "alert",
    "data",
    "attributes",
    "relationships",
    "monitor",
  ]);
  for (const [key, value] of Object.entries(payload)) {
    if (booleans.has(key) && typeof value !== "boolean")
      throw new Error("invalid boolean");
    if (
      !booleans.has(key) &&
      !identifiers.has(key) &&
      !envelopes.has(key) &&
      key !== "monitor_tags" &&
      value !== null &&
      typeof value !== "string"
    )
      throw new Error("invalid text field");
    if (envelopes.has(key)) {
      if (!object(value)) throw new Error("invalid nested payload");
      validatePayload(value, depth + 1);
    } else if (key === "monitor_tags") {
      if (
        !Array.isArray(value) ||
        value.length > 100 ||
        value.some((v) => typeof v !== "string")
      )
        throw new Error("invalid tags");
    } else if (
      value !== null &&
      !["string", "number", "boolean"].includes(typeof value)
    ) {
      throw new Error("invalid field");
    }
    if (
      typeof value === "string" &&
      (value.length > 4096 || /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(value))
    )
      throw new Error("invalid text");
  }
}

// One named object owns every read/render/write transaction.  Durable storage
// overlays legacy KV without deleting historical records or trusting KV locks.
export class AlertWriter {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.tail = Promise.resolve();
  }
  fetch(request) {
    const task = this.tail.then(() => this.dispatch(request));
    this.tail = task.catch(() => {});
    return task;
  }
  async dispatch(request) {
    const storage = this.ctx.storage,
      legacy = this.env.ALERT_STATE;
    const overlay = {
      async get(key, type) {
        let value = await storage.get(`kv:${key}`);
        if (value === undefined) value = await legacy.get(key);
        return value == null
          ? null
          : type === "json"
            ? JSON.parse(value)
            : value;
      },
      async put(key, value) {
        await storage.put(`kv:${key}`, value);
      },
      async list({ prefix }) {
        const previous = await listAll(legacy, prefix);
        const local = await storage.list({
          prefix: `kv:${prefix}`,
          limit: MAX_KEYS + 1,
        });
        const names = new Set(previous.map((k) => k.name));
        for (const key of local.keys()) names.add(key.slice(3));
        if (names.size > MAX_KEYS) throw new Error("inventory bound exceeded");
        return {
          keys: [...names].map((name) => ({ name })),
          list_complete: true,
        };
      },
    };
    try {
      return await handler.fetch(
        request,
        { ...this.env, ALERT_STATE: overlay, WRITER_STORAGE: storage },
        {},
      );
    } catch {
      return json({ error: "writer unavailable", stale: true }, 503);
    }
  }
}
export default {
  async fetch(request, env) {
    if (new URL(request.url).pathname === "/health")
      return handler.fetch(request, env, {});
    if (!env.ALERT_WRITER)
      return json({ error: "durable writer unavailable", stale: true }, 503);
    return env.ALERT_WRITER.get(env.ALERT_WRITER.idFromName("alerts-v1")).fetch(
      request,
    );
  },
  async scheduled(event, env, ctx) {
    if (!env.ALERT_WRITER || !env.INGRESS_SHARED_TOKEN) return;
    ctx.waitUntil(
      env.ALERT_WRITER.get(env.ALERT_WRITER.idFromName("alerts-v1")).fetch(
        new Request("https://internal/sweep", {
          headers: { authorization: `Bearer ${env.INGRESS_SHARED_TOKEN}` },
        }),
      ),
    );
  },
};
Object.assign(__testing, {
  listAll,
  mapBounded,
  validatePayload,
  verify,
  handler,
  handleEvent,
  sendEventOnce,
  transitionIdentity,
  advanceRevision,
  boundedService,
});
