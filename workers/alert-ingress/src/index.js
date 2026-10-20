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
import { decide, sweep, initialState, DEFAULT_MIN_OPEN_MS, PROD_DOWN_MIN_OPEN_MS } from "./state.js";
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
  const mac = await crypto.subtle.sign("HMAC", key, body);
  return [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
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
    const sig = request.headers.get("x-datadog-signature") || "";
    const expected = await hmacHex(secret, raw);
    return safeEqual(sig, expected);
  }
  if (source === "betterstack") {
    const secret = env.BETTERSTACK_WEBHOOK_SECRET;
    if (!secret) return false;
    const sig = request.headers.get("x-betterstack-signature") || "";
    const expected = await hmacHex(secret, raw);
    return safeEqual(sig, expected);
  }
  // Unknown source: require a shared bearer secret in the path/query.
  const token = new URL(request.url).searchParams.get("token") || "";
  return !!env.INGRESS_SHARED_TOKEN && safeEqual(token, env.INGRESS_SHARED_TOKEN);
}

/** Shared-token gate for operator routes.  `token` query param or Bearer <redacted> */
function authorized(request, env) {
  const token =
    new URL(request.url).searchParams.get("token") ||
    request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") ||
    "";
  return !!env.INGRESS_SHARED_TOKEN && safeEqual(token, env.INGRESS_SHARED_TOKEN);
}

/** All tracked states for one topic, with their service labels resolved. */
async function collectStates(env, channel, topic) {
  const prefix = `idx:${channel}:${topic}:`;
  const keys = await env.ALERT_STATE.list({ prefix });
  // Parallel reads: serial get pairs cost 2N KV round-trips per call.
  const fetched = await Promise.all(
    keys.keys.map(async ({ name }) => {
      const key = name.slice(prefix.length);
      const raw = await env.ALERT_STATE.get(stateKey(key), "json");
      if (!raw) return null;
      const svc = (await env.ALERT_STATE.get(svcKey(key), "json")) || key;
      return { key, ...raw, service: svc };
    }),
  );
  return fetched.filter(Boolean);
}

/** Every topic that has at least one tracked signal. */
async function collectTopics(env) {
  const prefix = `topic:${DEFAULT_CHANNEL}:`;
  const seen = new Set();
  const keys = await env.ALERT_STATE.list({ prefix });
  for (const { name } of keys.keys) {
    const topic = name.slice(prefix.length);
    if (topic) seen.add(topic);
  }
  const topics = {};
  let firing = 0;
  let total = 0;
  // Parallel across topics: serial collectStates made /status cost ~1 + T*(1+2A) KV calls.
  const perTopic = await Promise.all(
    [...seen].sort().map(async (topic) => [topic, await collectStates(env, DEFAULT_CHANNEL, topic)]),
  );
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
          return rank[a.phase] - rank[b.phase] || a.service.localeCompare(b.service);
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
  // KV gives no cross-request atomicity for the read-then-write inside
  // writeStatus, so two concurrent webhooks for a fresh topic could both POST
  // a new standing message and orphan the first.  A short-lived per-topic lock
  // serializes the create-or-patch decision; a skipped refresh self-heals on
  // the next event or sweep.
  const lockKey = `lock:${channel}:${topic}`;
  if (await env.ALERT_STATE.get(lockKey)) return content;
  await env.ALERT_STATE.put(lockKey, "1", { expirationTtl: 60 });
  try {
    const id = await writeStatus({
      email: env.ZULIP_BOT_EMAIL,
      apiKey: env.ZULIP_BOT_API_KEY,
      channel,
      topic,
      content,
      statusId: Number(await env.ALERT_STATE.get(statusKey(channel, topic), "json")) || null,
    });
    if (id) await env.ALERT_STATE.put(statusKey(channel, topic), JSON.stringify(id));
  } finally {
    await env.ALERT_STATE.delete(lockKey);
  }
  return content;
}

async function handleEvent(source, payload, env) {
  const event = normalize(source, payload);
  const channel = DEFAULT_CHANNEL;
  const topic = event.service;

  const prev = await env.ALERT_STATE.get(stateKey(event.key), "json");
  const now = Date.now();
  const result = decide(prev, event, now, { minOpenMs: Number(env.MIN_OPEN_MS) || DEFAULT_MIN_OPEN_MS });

  await env.ALERT_STATE.put(stateKey(event.key), JSON.stringify(result.state));
  await env.ALERT_STATE.put(svcKey(event.key), JSON.stringify(event.service));
  await env.ALERT_STATE.put(`idx:${channel}:${topic}:${event.key}`, "1");
  await env.ALERT_STATE.put(topicKey(channel, topic), "1");

  let posted = null;
  const sev = result.state.phase === "alerting" ? result.state.severity : "INFO";
  if (result.action === "announce" || result.action === "update") {
    posted = await postEvent({
      email: env.ZULIP_BOT_EMAIL,
      apiKey: env.ZULIP_BOT_API_KEY,
      channel,
      topic,
      severity: sev,
      service: event.service,
      what: result.state.text,
      link: result.state.link,
    });
  } else if (result.action === "resolve") {
    posted = await postEvent({
      email: env.ZULIP_BOT_EMAIL,
      apiKey: env.ZULIP_BOT_API_KEY,
      channel,
      topic,
      severity: "INFO",
      service: event.service,
      what: "recovered",
      link: event.link,
    });
  }

  // Every outcome refreshes the standing status, which is what makes the
  // channel readable at a glance instead of by scrolling.
  const status = await refreshStatus(env, channel, topic);
  return { action: result.action, posted: !!posted, status };
}

/** Promote aged incidents.  Wire as a cron trigger. */
async function runSweep(env) {
  const now = Date.now();
  const minOpenMs = Number(env.MIN_OPEN_MS) || DEFAULT_MIN_OPEN_MS;
  const all = await env.ALERT_STATE.list({ prefix: "state:" });
  let promoted = 0;
  const dirtyTopics = new Set();
  for (const { name } of all.keys) {
    const key = name.slice("state:".length);
    const prev = await env.ALERT_STATE.get(name, "json");
    const s = sweep(prev, now, { minOpenMs });
    if (!s) continue;
    await env.ALERT_STATE.put(name, JSON.stringify(s.state));
    promoted++;
    const service = (await env.ALERT_STATE.get(svcKey(key), "json")) || key;
    await postEvent({
      email: env.ZULIP_BOT_EMAIL,
      apiKey: env.ZULIP_BOT_API_KEY,
      channel: DEFAULT_CHANNEL,
      topic: service,
      severity: s.state.severity,
      service,
      what: s.state.text,
      link: s.state.link,
    });
    dirtyTopics.add(service);
  }
  // One status refresh per topic, however many incidents promoted in it, and
  // all in parallel instead of one rebuild per incident.
  await Promise.all([...dirtyTopics].map((topic) => refreshStatus(env, DEFAULT_CHANNEL, topic)));
  return { promoted, checked: all.keys.length };
}

const json = (body, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

export default {
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
      const content = await refreshStatus(env, decodeURIComponent(channel), decodeURIComponent(topic));
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

    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      return json({ error: "invalid json" }, 400);
    }

    ctx.waitUntil(handleEvent(source, payload, env).catch(() => {}));
    // Acknowledge fast; webhooks retry on slow responses.
    return json({ accepted: true });
  },

  // The sweep that promotes aged incidents to real messages.
  async scheduled(event, env, ctx) {
    ctx.waitUntil(runSweep(env));
  },
};

export const __testing = {
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