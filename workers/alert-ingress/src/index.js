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
//   POST /webhook/:source            receive a vendor event (signature-verified)
//   GET  /status                     machine-readable status for any consumer
//   GET  /status/:source/:channel/:topic  rebuild one source's standing status
//   GET  /status/:channel/:topic     rebuild every source's standing status
//   GET  /sweep                      promote incidents that aged past the window
//
// Vendor routing: every source has its own identity, its own state namespace
// and its own standing message, so two vendors that share a service name can
// never edit each other's message.
//
// KV layout:
//   state:<key>                AlertState per alertable thing
//   src:<key>                  source that owns the state, so the sweep can
//                              resolve the vendor credential
//   svc:<key>                  service name, so a topic stays stable across events
//   svcname:<source>:<service> alert key, so a renamed generic vendor label re-joins its incident
//   topic:<source>:<chan>:<topic>   topic membership for one source
//   idx:<source>:<chan>:<topic>:<key>  membership marker for one source
//   status:<source>:<chan>:<topic> message id of that source's standing status
//   statusowner:<message id>   the source that pinned a standing message

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
// Which source owns a state.  The sweep needs it to pick the right vendor
// credential; a state written before this key existed falls back to the
// source prefix in its alert key.
const srcKey = (k) => `src:${k}`;
const svcKey = (k) => `svc:${k}`;
// Reverse map for the generic path, whose key derives from the service label:
// a vendor rename mints a new key, and this finds the incident it belongs to.
// Source-scoped: two vendors sharing a label must never merge incidents.
const svcNameKey = (source, service) =>
  `svcname:${source}:${String(service || "").toLowerCase().slice(0, 120)}`;
// Zulip refuses topics over 60 characters (scripts/agent-sync-mcp contract).
const MAX_TOPIC_LENGTH = 60;
function boundedTopic(service) {
  if (service.length <= MAX_TOPIC_LENGTH) return service;
  let h = 0;
  for (let i = 0; i < service.length; i++)
    h = (h * 31 + service.charCodeAt(i)) >>> 0;
  // Deterministic prefix + hash: a long vendor label cannot strand an
  // incident as pending on a refused write, and distinct long names still
  // land in distinct topics.
  return `${service.slice(0, MAX_TOPIC_LENGTH - 6)}~${h.toString(36).padStart(5, "0").slice(0, 5)}`;
}
// Topic membership is its own key.  Deriving the topic by splitting the `idx:`
// key on ":" would mis-parse any service name containing a colon, so the topic
// list is tracked explicitly instead of inferred.
// Source-scoped: the standing status of a topic belongs to exactly one
// vendor, so every topic-shaped key carries its source.
const topicKey = (source, c, t) => `topic:${source}:${c}:${t}`;
// The standing status message id of one source in one topic.
const statusKey = (source, c, t) => `status:${source}:${c}:${t}`;
// Membership marker prefix, so a status rebuild knows its rows.
const indexPrefix = (source, c, t) => `idx:${source}:${c}:${t}:`;
// Durable delivery outcome of one source's standing status.  Per source, so a
// held write for one vendor never blocks another vendor's refresh.
const deliveryKey = (source, c, t) =>
  `delivery:${JSON.stringify([source, c, t])}`;
// Which source pinned a standing message id.  A vendor never edits a message
// another vendor pinned.
const statusOwnerKey = (id) => `statusowner:${id}`;
// Source names are route segments, so they are bounded and boring.
const SOURCE_TOKEN = /^[a-z0-9_-]{1,32}$/;

/**
 * The credential names one source may use, most specific first.
 *
 * The generic source owns the legacy un-suffixed pair: `ZULIP_BOT_EMAIL` and
 * `ZULIP_BOT_API_KEY` ARE the generic source's own identity, not a wildcard
 * for any vendor.  Every other source -- datadog, betterstack, or a custom
 * route name -- needs its own dedicated pair and has no fallback at all.
 */
export function credentialPairs(source) {
  const slug = String(source || "generic")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .toUpperCase();
  if (slug === "GENERIC")
    return [
      {
        email: "ZULIP_BOT_EMAIL_GENERIC",
        apiKey: "ZULIP_BOT_API_KEY_GENERIC",
      },
      { email: "ZULIP_BOT_EMAIL", apiKey: "ZULIP_BOT_API_KEY" },
    ];
  return [
    {
      email: `ZULIP_BOT_EMAIL_${slug}`,
      apiKey: `ZULIP_BOT_API_KEY_${slug}`,
    },
  ];
}

/**
 * Resolve the credential of exactly one source.
 *
 * Returns null when that source has no credential of its own, so the caller
 * can fail closed.  There is deliberately no path from one source to another
 * source's bot: two vendors on one shared identity would edit each other's
 * standing messages.
 */
export function resolveCredential(env, source) {
  for (const pair of credentialPairs(source)) {
    const email = env?.[pair.email];
    const apiKey = env?.[pair.apiKey];
    if (
      typeof email === "string" &&
      email.trim() &&
      typeof apiKey === "string" &&
      apiKey.trim()
    )
      return { email, apiKey };
  }
  return null;
}

/** Structured failure for a source that has no credential of its own. */
function credentialError(source) {
  const error = new Error("credential unavailable");
  error.code = "credential_unavailable";
  error.source = source;
  error.status = 503;
  return error;
}

/** The source that owns a state key, from its recorded source or its prefix. */
function sourceFromKey(key) {
  const head = String(key || "").split(":")[0] || "";
  return SOURCE_TOKEN.test(head) ? head : "generic";
}

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
    // A present-but-wrong static header must not veto a valid body signature:
    // a sender mid-rotation can carry a stale static token with a genuine HMAC.
    if (token && safeEqual(token, secret)) return true;
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

/** All tracked states for one source in one topic, with their service labels. */
async function collectStates(env, source, channel, topic) {
  const prefix = indexPrefix(source, channel, topic);
  const keys = { keys: await listAll(env.ALERT_STATE, prefix) };
  // Parallel reads: serial get pairs cost 2N KV round-trips per call.
  const fetched = await mapBounded(keys.keys, async ({ name }) => {
    const key = name.slice(prefix.length);
    const raw = await env.ALERT_STATE.get(stateKey(key), "json");
    if (!raw) return null;
    const svc = (await env.ALERT_STATE.get(svcKey(key), "json")) || key;
    return { key, ...raw, service: svc, source };
  });
  return fetched.filter(Boolean);
}

/**
 * Read one topic-membership key.
 *
 * The key is `topic:<source>:<channel>:<topic>`.  The channel is checked
 * against the alerts channel because the worker only ever writes there, and
 * that check is also what keeps a pre-routing key (`topic:alerts:<topic>`)
 * from being misread as a source.
 */
function parseTopicKey(name) {
  if (!name.startsWith("topic:")) return null;
  const parts = name.slice("topic:".length).split(":");
  if (parts.length < 3) return null;
  const [source, channel, ...rest] = parts;
  const topic = rest.join(":");
  if (!SOURCE_TOKEN.test(source)) return null;
  if (channel !== DEFAULT_CHANNEL) return null;
  if (!topic) return null;
  return { source, channel, topic };
}

/** Every source that tracks one topic. */
async function sourcesForTopic(env, channel, topic) {
  const keys = { keys: await listAll(env.ALERT_STATE, "topic:") };
  const sources = new Set();
  for (const { name } of keys.keys) {
    const parsed = parseTopicKey(name);
    if (!parsed) continue;
    if (parsed.channel !== channel || parsed.topic !== topic) continue;
    sources.add(parsed.source);
  }
  return [...sources].sort();
}

const PHASE_RANK = { alerting: 0, pending: 1, ok: 2 };

/** One alert row, as the machine-readable status publishes it. */
function alertRow(state) {
  return {
    key: state.key,
    source: state.source,
    service: state.service,
    phase: state.phase,
    severity: state.phase === "alerting" ? state.severity : "INFO",
    text: state.text,
    link: state.link,
    since: state.since,
    updated: state.updated,
    prodDown: state.prodDown === true,
  };
}

const byPhaseThenService = (a, b) =>
  PHASE_RANK[a.phase] - PHASE_RANK[b.phase] ||
  a.service.localeCompare(b.service);

/** Every topic that has at least one tracked signal, plus a per-source view. */
async function collectTopics(env) {
  const keys = { keys: await listAll(env.ALERT_STATE, "topic:") };
  const pairs = new Map();
  for (const { name } of keys.keys) {
    const parsed = parseTopicKey(name);
    if (!parsed) continue;
    pairs.set(`${parsed.source}\u0000${parsed.topic}`, parsed);
  }
  const topics = {};
  const sources = {};
  let firing = 0;
  let total = 0;
  // Parallel across topics: serial collectStates made /status cost ~1 + T*(1+2A) KV calls.
  const perTopic = await mapBounded([...pairs.values()], async (pair) => [
    pair,
    await collectStates(env, pair.source, pair.channel, pair.topic),
  ]);
  for (const [pair, states] of perTopic) {
    // A topic whose states have all been cleared is dropped rather than
    // rendered as an empty card, so the view self-cleans as incidents resolve.
    if (states.length === 0) continue;
    const tf = states.filter((s) => s.phase === "alerting").length;
    firing += tf;
    total += states.length;
    const alerts = states.map(alertRow).sort(byPhaseThenService);
    const flat = topics[pair.topic] || (topics[pair.topic] = {
      firing: 0,
      total: 0,
      alerts: [],
    });
    flat.firing += tf;
    flat.total += states.length;
    flat.alerts.push(...alerts);
    const bucket =
      sources[pair.source] ||
      (sources[pair.source] = {
        summary: { topics: 0, alerts: 0, firing: 0 },
        topics: {},
      });
    bucket.topics[pair.topic] = { firing: tf, total: states.length, alerts };
    bucket.summary.topics++;
    bucket.summary.alerts += states.length;
    bucket.summary.firing += tf;
  }
  for (const topic of Object.values(topics)) topic.alerts.sort(byPhaseThenService);
  return { topics, sources, firing, total };
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

/**
 * Rewrite the standing status block for one source in one topic.
 *
 * The source decides both the credential and the message: a vendor never
 * touches another vendor's standing message, and a vendor with no credential
 * of its own writes nothing at all.
 */
async function refreshStatus(env, source, channel, topic, options = {}) {
  const credential = options.credential || resolveCredential(env, source);
  if (!credential) throw credentialError(source);
  const states = await collectStates(env, source, channel, topic);
  const content = renderStatus({
    topic,
    states,
    updatedAt: ctStamp(),
    source,
  });
  if (!env.WRITER_STORAGE) throw new Error("durable writer required");
  const outcomeKey = deliveryKey(source, channel, topic);
  const prior = await env.WRITER_STORAGE.get(outcomeKey);
  if (prior?.outcome === "unknown" || prior?.outcome === "refused")
    return content;
  let statusId =
    prior?.id ||
    Number(await env.ALERT_STATE.get(statusKey(source, channel, topic), "json")) ||
    null;
  // Pinned ownership: a standing message belongs to exactly one source.  A
  // message id another vendor pinned is never edited here, and never adopted
  // as this source's own standing message.
  if (statusId) {
    const owner = await env.ALERT_STATE.get(statusOwnerKey(statusId), "json");
    if (owner && owner !== source) statusId = null;
  }
  // Commit uncertainty before crossing the network.  A crash or ambiguous reply
  // cannot turn a possibly accepted create into another automatic POST.
  await env.WRITER_STORAGE.put(outcomeKey, {
    outcome: "unknown",
    id: statusId,
  });
  try {
    const id = await writeStatus({
      email: credential.email,
      apiKey: credential.apiKey,
      channel,
      topic,
      content,
      statusId,
    });
    if (id) {
      await env.WRITER_STORAGE.put(outcomeKey, { outcome: "ok", id });
      await env.ALERT_STATE.put(
        statusKey(source, channel, topic),
        JSON.stringify(id),
      );
      await env.ALERT_STATE.put(statusOwnerKey(id), JSON.stringify(source));
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

async function handleEvent(source, payload, env) {
  // The route is authoritative: it names the source, and the source names the
  // credential, the state namespace and the standing message.
  const sourceName = String(source || "generic").toLowerCase();
  const credential = resolveCredential(env, sourceName);
  // A source with no credential of its own posts nothing and writes nothing.
  if (!credential) throw credentialError(sourceName);
  const event = normalize(sourceName, payload);
  const channel = DEFAULT_CHANNEL;
  // Keep the original topic for a stable key when vendor labels change.  The
  // svc: pin and the state read are independent, so they run in parallel.
  let [priorService, prev] = await Promise.all([
    env.ALERT_STATE.get(svcKey(event.key), "json"),
    env.ALERT_STATE.get(stateKey(event.key), "json"),
  ]);
  if (typeof priorService === "string" && priorService) {
    event.service = priorService;
  } else if (event.key === `${sourceName}:${event.service.toLowerCase()}`) {
    // Only a key derived from the service label itself (the generic path with
    // no id/fingerprint) mints a NEW key on a vendor rename, before the pin
    // can engage.  The reverse service -> key map catches exactly that rename
    // and re-joins the existing incident; identifier-keyed incidents keep
    // their svc:<key> pin and never touch this map.
    const priorKey = await env.ALERT_STATE.get(svcNameKey(sourceName, event.service), "json");
    if (typeof priorKey === "string" && priorKey && priorKey !== event.key) {
      event.key = priorKey;
      [priorService, prev] = await Promise.all([
        env.ALERT_STATE.get(svcKey(event.key), "json"),
        env.ALERT_STATE.get(stateKey(event.key), "json"),
      ]);
      if (typeof priorService === "string" && priorService) event.service = priorService;
    }
  }
  const topic = boundedTopic(event.service);
  const now = Date.now();
  const result = decide(prev, event, now, {
    minOpenMs: Number(env.MIN_OPEN_MS) || DEFAULT_MIN_OPEN_MS,
  });

  result.state = advanceRevision(prev, result.state);
  const inventory = await collectStates(env, sourceName, channel, topic);
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
        email: credential.email,
        apiKey: credential.apiKey,
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
        email: credential.email,
        apiKey: credential.apiKey,
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
  // The source is recorded with the state so the sweep can resolve the right
  // vendor credential for this incident.
  await env.ALERT_STATE.put(srcKey(event.key), JSON.stringify(sourceName));
  if (event.key === `${sourceName}:${event.service.toLowerCase()}`) {
    await env.ALERT_STATE.put(svcNameKey(sourceName, event.service), JSON.stringify(event.key));
  }
  await env.ALERT_STATE.put(indexPrefix(sourceName, channel, topic) + event.key, "1");
  await env.ALERT_STATE.put(topicKey(sourceName, channel, topic), "1");

  // Every outcome refreshes the standing status, which is what makes the
  // channel readable at a glance instead of by scrolling.
  const status = await refreshStatus(env, sourceName, channel, topic, {
    credential,
  });

  return { action: result.action, posted: !!posted, status };
}

/** Promote aged incidents.  Wire as a cron trigger. */
async function runSweep(env) {
  const now = Date.now();
  const minOpenMs = Number(env.MIN_OPEN_MS) || DEFAULT_MIN_OPEN_MS;
  const all = { keys: await listAll(env.ALERT_STATE, "state:") };
  await collectTopics(env); // Complete every dependent inventory before the first send.
  let promoted = 0;
  // Topics are per source: two vendors can share a service name, and each
  // one owns its own standing message.
  const dirtyTopics = new Map();
  for (const { name } of all.keys) {
    const key = name.slice("state:".length);
    const prev = await env.ALERT_STATE.get(name, "json");
    const s = sweep(prev, now, { minOpenMs });
    if (!s) continue;
    const source = await resolveStateSource(env, key);
    const credential = resolveCredential(env, source);
    // No credential of its own means no post at all: the incident stays
    // pending until the source has one, and it is never sent as another bot.
    if (!credential) {
      console.error(JSON.stringify({ event: "alert.credential_unavailable" }));
      continue;
    }
    s.state = advanceRevision(prev, s.state);
    try {
      const service = (await env.ALERT_STATE.get(svcKey(key), "json")) || key;
      // The same bounded topic the ingest path uses, so a long vendor label
      // does not strand the promotion in a different topic.
      const topic = boundedTopic(service);
      await sendEventOnce(
        env,
        transitionIdentity(key, "announce", prev, s.state),
        {
          email: credential.email,
          apiKey: credential.apiKey,
          channel: DEFAULT_CHANNEL,
          topic,
          severity: s.state.severity,
          service,
          what: s.state.text,
          link: s.state.link,
        },
      );
      await env.ALERT_STATE.put(name, JSON.stringify(s.state));
      await env.ALERT_STATE.put(srcKey(key), JSON.stringify(source));
      promoted++;
      dirtyTopics.set(`${source}\u0000${topic}`, { source, topic });
    } catch {
      console.error(JSON.stringify({ event: "alert.promotion_failed" }));
    }
  }
  // One status refresh per source and topic, however many incidents promoted
  // in it, and all in parallel instead of one rebuild per incident.
  await mapBounded([...dirtyTopics.values()], async ({ source, topic }) => {
    try {
      await refreshStatus(env, source, DEFAULT_CHANNEL, topic);
    } catch {
      console.error(JSON.stringify({ event: "alert.status_refresh_failed" }));
    }
  });
  return { promoted, checked: all.keys.length };
}

/** The source that owns a state: its recorded source, else its key prefix. */
async function resolveStateSource(env, key) {
  const recorded = await env.ALERT_STATE.get(srcKey(key), "json");
  if (typeof recorded === "string" && recorded) return recorded.toLowerCase();
  return sourceFromKey(key);
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
    // dashboard and the Zulip channel cannot drift apart.  The top-level
    // shape is unchanged; the per-source breakdown is added beside it.
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

    // The refresh routes rewrite a standing Zulip message (refreshStatus calls
    // writeStatus), so they are operator-only: an unauthenticated hit would let
    // anyone make the bot post into any channel it can reach.
    const sourceStatusMatch = url.pathname.match(
      /^\/status\/([^/]+)\/([^/]+)\/([^/]+)$/,
    );
    const statusMatch = url.pathname.match(/^\/status\/([^/]+)\/([^/]+)$/);
    if (sourceStatusMatch || statusMatch) {
      if (!authorized(request, env)) {
        return json({ error: "unauthorized" }, 401);
      }
      const segments = sourceStatusMatch ? sourceStatusMatch.slice(1) : null;
      const channel = decodeURIComponent(
        segments ? segments[1] : statusMatch[1],
      );
      const topic = decodeURIComponent(
        segments ? segments[2] : statusMatch[2],
      );
      // The bot only ever writes into the alerts channel.  Any other channel
      // is refused rather than written, whatever the operator token says.
      if (channel !== DEFAULT_CHANNEL) {
        return json(
          {
            error: "channel not permitted",
            channel,
            allowed: [DEFAULT_CHANNEL],
          },
          403,
        );
      }
      if (segments) {
        // Source-aware refresh: one vendor's standing status, with that
        // vendor's own credential.
        const source = decodeURIComponent(segments[0]).toLowerCase();
        if (!SOURCE_TOKEN.test(source)) {
          return json({ error: "unknown source", source }, 400);
        }
        if (!resolveCredential(env, source)) {
          return json(
            { error: "credential unavailable", source },
            503,
          );
        }
        const content = await refreshStatus(env, source, DEFAULT_CHANNEL, topic);
        return json({ source, channel: DEFAULT_CHANNEL, topic, content });
      }
      // The legacy route carries no vendor dimension, so it refreshes every
      // source that tracks the topic, each with its own credential.  A topic
      // nobody tracks still gets the generic source's standing status, which
      // is what this route always did.
      const sources = await sourcesForTopic(env, DEFAULT_CHANNEL, topic);
      const tracked = sources.length > 0;
      const rendered = {};
      const unavailable = [];
      for (const source of tracked ? sources : ["generic"]) {
        if (!resolveCredential(env, source)) {
          unavailable.push({ source, error: "credential unavailable" });
          continue;
        }
        rendered[source] = await refreshStatus(
          env,
          source,
          DEFAULT_CHANNEL,
          topic,
        );
      }
      return json({
        channel: DEFAULT_CHANNEL,
        topic,
        sources: rendered,
        unavailable,
      });
    }

    const hookMatch = url.pathname.match(/^\/webhook\/([a-z0-9_-]{1,32})$/i);
    if (!hookMatch) return json({ error: "not found" }, 404);
    if (request.method !== "POST") return json({ error: "POST only" }, 405);

    const source = hookMatch[1].toLowerCase();
    const raw = await request.text();
    if (!(await verify(request, raw, env, source))) {
      return json({ error: "bad signature" }, 401);
    }

    // The authenticated route names the source, and the source names the
    // credential.  A source with no credential of its own fails closed: it
    // posts nothing, writes no status, and never borrows another source's
    // identity.
    if (!resolveCredential(env, source)) {
      return json({ error: "credential unavailable", source }, 503);
    }

    // Bytes, not UTF-16 code units: a non-ASCII body can be far larger in
    // UTF-8 than its .length suggests, and the documented limit is 64 KiB.
    if (new TextEncoder().encode(raw).length > 65536) return json({ error: "payload too large" }, 413);
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
    try {
      return json(await handleEvent(source, payload, env));
    } catch (error) {
      if (error?.code === "credential_unavailable") {
        return json(
          { error: "credential unavailable", source: error.source },
          503,
        );
      }
      throw error;
    }
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
  sourcesForTopic,
  parseTopicKey,
  resolveStateSource,
  resolveCredential,
  credentialPairs,
  safeEqual,
  stateKey,
  srcKey,
  statusKey,
  statusOwnerKey,
  svcKey,
  topicKey,
  deliveryKey,
  ctStamp,
  indexPrefix,
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
  // envelopes too -- not oversized text fields.  `monitor` is both that
  // envelope (object) and a conventional service label for generic vendors
  // (string), so it is handled separately in the loop below.
  const envelopes = new Set([
    "alert",
    "data",
    "attributes",
    "relationships",
  ]);
  for (const [key, value] of Object.entries(payload)) {
    if (booleans.has(key) && typeof value !== "boolean")
      throw new Error("invalid boolean");
    if (
      !booleans.has(key) &&
      !identifiers.has(key) &&
      !envelopes.has(key) &&
      !(key === "monitor" && object(value)) &&
      key !== "monitor_tags" &&
      value !== null &&
      typeof value !== "string"
    )
      throw new Error("invalid text field");
    if (envelopes.has(key) || (key === "monitor" && object(value))) {
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
});
