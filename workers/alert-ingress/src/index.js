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
//   GET  /status/:channel/:topic   read the current standing status
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

/** Rewrite the standing status block for a topic from all its states. */
async function refreshStatus(env, channel, topic) {
  const now = new Date();
  const stamp = now.toLocaleString("en-US", {
    timeZone: "America/Chicago",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
  const keys = (await env.ALERT_STATE.list({ prefix: `idx:${channel}:${topic}:` })).keys;
  const states = [];
  for (const { name } of keys) {
    const key = name.slice(`idx:${channel}:${topic}:`.length);
    const raw = await env.ALERT_STATE.get(stateKey(key), "json");
    if (!raw) continue;
    states.push({ ...raw, service: (await env.ALERT_STATE.get(svcKey(key), "json")) || key });
  }
  const content = renderStatus({ topic, states, updatedAt: stamp });
  const id = await writeStatus({
    email: env.ZULIP_BOT_EMAIL,
    apiKey: env.ZULIP_BOT_API_KEY,
    channel,
    topic,
    content,
    statusId: Number(await env.ALERT_STATE.get(statusKey(channel, topic), "json")) || null,
  });
  if (id) await env.ALERT_STATE.put(statusKey(channel, topic), JSON.stringify(id));
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
    await refreshStatus(env, DEFAULT_CHANNEL, service);
  }
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
      const token = url.searchParams.get("token") || request.headers.get("authorization")?.replace(/^Bearer\s+/i, "") || "";
      if (!env.INGRESS_SHARED_TOKEN || !safeEqual(token, env.INGRESS_SHARED_TOKEN)) {
        return json({ error: "unauthorized" }, 401);
      }
      return json(await runSweep(env));
    }

    const statusMatch = url.pathname.match(/^\/status\/([^/]+)\/([^/]+)$/);
    if (statusMatch) {
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

export const __testing = { refreshStatus, runSweep, safeEqual, stateKey, statusKey, svcKey, indexPrefix: (c, t) => `idx:${c}:${t}:` };