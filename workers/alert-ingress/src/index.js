import { readBoundedBody, BodyLimitError } from "./body.js";
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
// Three channels, one topic per service in each (owner, Oct 29, 2026):
//   #Alerts      only the standing status blocks
//   #Alerts-Log  the sparse one-line timeline
//   #Alerts-Raw  every authenticated delivery as the vendor sent it
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
import {
  postEvent,
  postRaw,
  writeStatus,
  renderStatus,
  fetchMessage,
  STANDING_BLOCK_HEADER,
} from "./zulip.js";
import { renderRaw } from "./raw.js";
import { isSevereCpu, resourceMentions } from "./resource.js";
import { BudgetExceeded, createBudget, guardOff, guardOffResponse } from "./budget-guard.mjs";
import { DeliveryBreaker, NEW_KEYS_PER_DAY, secondsToNextDay } from "./breaker.js";

const DEFAULT_CHANNEL = "alerts";
// The standing blocks keep DEFAULT_CHANNEL, which also names their records;
// the timeline and the raw copies go to channels of their own.
const LOG_CHANNEL = "Alerts-Log",
  RAW_CHANNEL = "Alerts-Raw";

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
// Zulip refuses topics over 60 characters (scripts/simple-tools-mcp contract).
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
// Membership keys written before vendor routing carried no source.  They
// belong to the generic source, which owns the legacy un-suffixed identity,
// and they are still read so an incident tracked before the routing change
// does not vanish from the dashboard.
const legacyTopicKey = (c, t) => `topic:${c}:${t}`;
const legacyIndexPrefix = (c, t) => `idx:${c}:${t}:`;
// Durable delivery outcome of one source's standing status.  Per source, so a
// held write for one vendor never blocks another vendor's refresh, and scoped
// to the revision of the content it carries, so a changed standing block is a
// new delivery and an unchanged one is not.
const deliveryKey = (source, c, t, revision = "base") =>
  `delivery:${JSON.stringify([source, c, t, revision])}`;
// The uncertainty barrier of one source in one topic.
//
// The delivery outcome above is scoped to the revision of the content, so an
// unchanged standing block needs no new delivery and a changed one is a new
// delivery.  That is not enough on its own: a POST whose acknowledgement was
// lost may have landed, and a changed revision would then post a second
// standing message for the same topic.  The barrier is scoped to the source,
// channel and topic instead, so it holds across revisions until the write is
// reconciled.
const deliveryBarrierKey = (source, c, t) =>
  `delivery-barrier:${source}:${c}:${t}`;
// Which source pinned a standing message id.  A vendor never edits a message
// another vendor pinned.
const statusOwnerKey = (id) => `statusowner:${id}`;
// Sources whose alert key names one incident rather than one monitor.  A
// monitor comes back, so its records are kept;  an incident does not, so a
// closed one is pruned, or every incident ever opened would stay a row of the
// standing block and the inventory would grow until it hit MAX_KEYS.
const PER_INCIDENT_SOURCES = new Set(["pagerduty"]);
// What is left of a pruned incident:  the time it closed.  It is never
// listed, so it costs nothing in the inventory, and it is what stops a late
// retry of the original trigger from opening the incident a second time.
const closedKey = (k) => `closed:${k}`;
// Source names are route segments, so they are bounded and boring.
const SOURCE_TOKEN = /^[a-z0-9_-]{1,32}$/;
// The incidents waiting out their hold.  The sweep reads only these, so an idle
// sweep costs a few rows instead of about eight per tracked state.  Writer
// storage, not the overlay:  nothing older than this index can be in it.
const pendingKey = (k) => `pending:${k}`;
const PENDING_PREFIX = "pending:";
const PENDING_SCAN_LIMIT = 500;
// Where the next sweep resumes when the index is longer than one read, so every
// entry takes its turn.  Written only while the index is over that length.
const PENDING_CURSOR = "sweep:pending-cursor";
// When the full state scan last rebuilt the index and repaired legacy markers.
const RECONCILED_AT = "sweep:reconciled-at";
// Hourly, because an incident missing from the index is an alert nobody hears
// about until this runs.  Code without the index, such as a rollback that is later
// rolled forward, writes exactly that.  An hour costs about five rows per state.
const RECONCILE_EVERY_MS = 60 * 60 * 1000;
// A source's bot is rate limited until this time, from Zulip's Retry-After.
const zulipPauseKey = (source) => `zulip-pause:${source}`;
const ZULIP_PAUSE_DEFAULT_S = 60;
const ZULIP_PAUSE_MAX_S = 3600;
// Work held back during a pause or a shed, which the next sweep catches up on.
const QUEUED_EVENT_PREFIX = "queued-event:";
const DIRTY_TOPIC_PREFIX = "dirty-topic:";
const dirtyTopicKey = (source, topic) =>
  `${DIRTY_TOPIC_PREFIX}${JSON.stringify([source, topic])}`;
const CATCH_UP_BATCH = 10;
// A held line Zulip did not confirm gets this many tries, one per sweep.
const QUEUED_LINE_ATTEMPTS = 3;
// A repeat delivery refreshes `updated` at most this often.
const STATE_TOUCH_MS = 10 * 60 * 1000;
const NOTICE_EVERY_MS = 60 * 60 * 1000;
const NOTICE_DAILY_MS = 24 * 60 * 60 * 1000;
// A notice that failed to send is tried again this soon, not a whole interval on.
const NOTICE_RETRY_MS = 5 * 60 * 1000;
const NOTICE_SOURCES = ["datadog", "betterstack", "pagerduty", "sentry", "generic"];
// Below the free plan's 50 subrequests, so a large backlog finishes on later
// sweeps instead of being killed part way through one.
const sweepBudget = createBudget({
  worker: "alert-ingress",
  perInvocation: { zulipCall: 40 },
});

/**
 * The credential names one source may use, most specific first.
 *
 * The generic source owns the legacy un-suffixed pair: `ZULIP_BOT_EMAIL` and
 * `ZULIP_BOT_API_KEY` ARE the generic source's own identity, not a wildcard
 * for any vendor.  Every other source -- datadog, betterstack, or a custom
 * route name -- needs its own dedicated pair and has no fallback at all.
 */
export function credentialPairs(source) {
  const slug = credentialSlug(source);
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
 * The credential slug of one source, injective in the source name.
 *
 * The slug used to collapse every run of non-alphanumeric characters into a
 * single underscore, so `acme-eu` and `acme_eu` produced the same slug and the
 * two routes shared one bot identity and could edit each other's standing
 * messages.  Now every character outside `[a-z0-9]` encodes as an underscore
 * followed by two uppercase hex digits of its UTF-8 byte, so distinct source
 * names never share a slug.  The escape width is fixed, which keeps the
 * encoding uniquely decodable and therefore injective.
 *
 * The three built-in sources contain only `[a-z]`, so their slugs are
 * unchanged: `datadog` is `DATADOG`, `betterstack` is `BETTERSTACK` and
 * `generic` is `GENERIC`.
 */
export function credentialSlug(source) {
  const name = String(source || "generic").toLowerCase();
  let slug = "";
  for (const byte of new TextEncoder().encode(name)) {
    const char = String.fromCharCode(byte);
    if (char >= "a" && char <= "z") slug += char;
    else if (char >= "0" && char <= "9") slug += char;
    else slug += `_${byte.toString(16).toUpperCase().padStart(2, "0")}`;
  }
  return slug.toUpperCase();
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

/**
 * The secret variable names that are still missing for one source.
 *
 * The names are not secret; the values are.  An operator who cannot tell
 * which variable to configure cannot configure it, so the failure names every
 * variable that is absent or blank for this source, in the order the pairs
 * are tried.  The list covers every candidate pair, so a source with more than
 * one candidate names what is absent across all of them; it is only read when
 * no pair resolved, so those are the names an operator has to set.
 */
export function missingCredentialNames(env, source) {
  const missing = [];
  for (const pair of credentialPairs(source)) {
    if (!isConfigured(env, pair.email)) missing.push(pair.email);
    if (!isConfigured(env, pair.apiKey)) missing.push(pair.apiKey);
  }
  return missing;
}

/** A variable counts as configured when it is a non-blank string. */
function isConfigured(env, name) {
  const value = env?.[name];
  return typeof value === "string" && value.trim() !== "";
}

/** Structured failure for a source that has no credential of its own. */
function credentialError(source, missing = []) {
  const error = new Error("credential unavailable");
  error.code = "credential_unavailable";
  error.source = source;
  error.status = 503;
  error.missing = missing;
  return error;
}

/**
 * A sanitized delivery failure.
 *
 * The upstream error is never echoed, because it can carry a credential or a
 * response body from the Zulip API.  The caller sees a fixed message, the
 * source, and a status code.  `outcome` keeps the distinction that matters
 * for a retry: `refused` is definitive and may be retried, `unknown` means
 * the write may have landed and must not be repeated blindly.
 */
function deliveryError(source, outcome, detail = {}) {
  const error = new Error("status delivery failed");
  error.code = "status_delivery_failed";
  error.source = source;
  error.status = 502;
  error.outcome = outcome;
  error.detail = {
    code: Number.isInteger(detail.code) ? detail.code : null,
    retryAfter: Number.isInteger(detail.retryAfter) ? detail.retryAfter : null,
  };
  return error;
}

/**
 * A standing status that cannot be written safely yet.
 *
 * The standing message for this topic may already exist but be unclaimed, so
 * neither adopting it nor posting a second one is safe.  The topic is held
 * until an operator reconciles it, and the caller is told the standing block
 * is not known to be current rather than reading a success.
 */
function statusHeldError(source, reason) {
  const error = new Error("standing status held");
  error.code = "status_held";
  error.source = source;
  error.status = 409;
  error.reason = reason;
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
  if (source === "sentry") {
    const secret = env.SENTRY_WEBHOOK_SECRET;
    if (!secret) return false;
    // Sentry's integration platform signs the request body with the app's
    // client secret and sends the lowercase hex HMAC-SHA256 digest in the
    // Sentry-Hook-Signature header (docs.sentry.io, integration platform
    // webhooks).  A Sentry route with no verifier of its own would fall
    // through to the shared bearer token, which means any holder of that
    // token could inject a Sentry alert, so the source is refused instead.
    const sig = request.headers.get("sentry-hook-signature") || "";
    if (!sig) return false;
    return safeEqual(sig, await hmacHex(secret, raw));
  }
  if (source === "pagerduty") {
    // A v3 webhook subscription carries static custom headers, so the
    // subscription is created with `X-PagerDuty-Token` set to
    // PAGERDUTY_WEBHOOK_SECRET.  PagerDuty also signs every body with the
    // secret it generates for the subscription, sent as
    // `X-PagerDuty-Signature: v1=<hex>[,v1=<hex>]` (more than one during a
    // rotation), and that is honoured when PAGERDUTY_SIGNING_SECRET is set.
    // Like Sentry, the source never falls through to the shared bearer token.
    const token = env.PAGERDUTY_WEBHOOK_SECRET;
    const header = request.headers.get("x-pagerduty-token") || "";
    if (token && header && safeEqual(header, token)) return true;
    const signing = env.PAGERDUTY_SIGNING_SECRET;
    if (!signing) return false;
    const expected = `v1=${await hmacHex(signing, raw)}`;
    return (request.headers.get("x-pagerduty-signature") || "")
      .split(",")
      .some((part) => safeEqual(part.trim(), expected));
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

/**
 * The sources that own the incidents of a pre-routing topic.
 *
 * A pre-routing topic marker names a topic but not a source, so the sources
 * are read from the incidents its legacy markers list.
 */
async function legacyTopicSources(env, channel, topic) {
  const prefix = legacyIndexPrefix(channel, topic);
  const legacy = await listAll(env.ALERT_STATE, prefix);
  const sources = new Set();
  for (const { name } of legacy) {
    const key = name.slice(prefix.length);
    sources.add(await resolveStateSource(env, key));
  }
  return [...sources].sort();
}

/** All tracked states for one source in one topic, with their service labels. */
async function collectStates(env, source, channel, topic) {
  const prefix = indexPrefix(source, channel, topic);
  const scoped = await listAll(env.ALERT_STATE, prefix);
  const entries = scoped.map((entry) => ({ ...entry, prefix }));
  // Records written before vendor routing carry an unscoped marker that names
  // the incident but not the source, so the incident's own source decides
  // which standing block it belongs in.  Reading both marker forms costs one
  // extra list per topic, and the dedupe below is what keeps an incident
  // that carries both from appearing twice.
  const legacyPrefix = legacyIndexPrefix(channel, topic);
  const legacy = await listAll(env.ALERT_STATE, legacyPrefix);
  for (const entry of legacy) {
    entries.push({ ...entry, prefix: legacyPrefix, legacy: true });
  }
  const fetched = await mapBounded(entries, async (entry) => {
    const key = entry.name.slice(entry.prefix.length);
    const raw = await env.ALERT_STATE.get(stateKey(key), "json");
    if (!raw) return null;
    if (entry.legacy) {
      const owner = await resolveStateSource(env, key);
      if (owner !== source) return null;
    }
    const svc = (await env.ALERT_STATE.get(svcKey(key), "json")) || key;
    return { key, ...raw, service: svc, source };
  });
  // The same incident can carry both markers, so rows are keyed by the alert
  // key: without that the incident would appear twice in the standing block.
  const byKey = new Map();
  for (const row of fetched) if (row) byKey.set(row.key, row);
  return [...byKey.values()];
}

/**
 * Read one topic-membership key.
 *
 * The current key is `topic:<source>:<channel>:<topic>`.  The channel is
 * checked against the alerts channel because the worker only ever writes
 * there, and that check is also what keeps a pre-routing key
 * (`topic:alerts:<topic>`) from being misread as a source.
 *
 * A pre-routing key is `topic:<channel>:<topic>` and carries no source.  It
 * is read as a legacy membership of the generic source.  The current format
 * is tried first, so a key whose source happens to be `alerts` is never
 * mistaken for the legacy form.
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

/**
 * Read a pre-routing topic-membership key.
 *
 * `topic:<channel>:<topic>` was written before vendor routing and carries no
 * source, so it names a topic and nothing more.  The channel must be the
 * alerts channel, the only channel this worker ever writes.  The sources of
 * the topic are read from the incidents its legacy markers list.
 */
function parseLegacyTopicKey(name) {
  if (!name.startsWith("topic:")) return null;
  const parts = name.slice("topic:".length).split(":");
  if (parts.length < 2) return null;
  const [channel, ...rest] = parts;
  const topic = rest.join(":");
  if (channel !== DEFAULT_CHANNEL) return null;
  if (!topic) return null;
  return { channel, topic };
}

/** Every source that tracks one topic. */
async function sourcesForTopic(env, channel, topic) {
  const keys = { keys: await listAll(env.ALERT_STATE, "topic:") };
  const sources = new Set();
  for (const { name } of keys.keys) {
    const parsed = parseTopicKey(name);
    if (parsed) {
      if (parsed.channel !== channel || parsed.topic !== topic) continue;
      sources.add(parsed.source);
      continue;
    }
    // A pre-routing topic marker names a topic but not a source, so the
    // sources are the owners of the incidents its legacy markers list.
    const legacy = parseLegacyTopicKey(name);
    if (!legacy) continue;
    if (legacy.channel !== channel || legacy.topic !== topic) continue;
    for (const owner of await legacyTopicSources(env, channel, topic)) {
      sources.add(owner);
    }
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
    if (parsed) {
      pairs.set(`${parsed.source}\u0000${parsed.topic}`, parsed);
      continue;
    }
    // A pre-routing topic marker names a topic but not a source, so it
    // contributes one pair per source that owns an incident in that topic.
    const legacy = parseLegacyTopicKey(name);
    if (!legacy) continue;
    const owners = await legacyTopicSources(env, legacy.channel, legacy.topic);
    for (const owner of owners) {
      pairs.set(`${owner}\u0000${legacy.topic}`, {
        source: owner,
        channel: legacy.channel,
        topic: legacy.topic,
      });
    }
  }
  // Null-prototype maps: topic names come from vendor service labels, so a
  // topic literally named "constructor" or "__proto__" must not resolve to an
  // inherited Object member (a truthy read here used to crash /status with a
  // TypeError, and a "__proto__" write silently reparented the bucket).
  const topics = Object.create(null);
  const sources = Object.create(null);
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
        topics: Object.create(null),
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
 * A digest of what the standing block says, independent of the timestamp.
 *
 * The rendered content carries an "updated at" stamp, so it differs on every
 * refresh.  Scoping the delivery outcome to that stamp would make every
 * refresh a new delivery identity and defeat the idempotency guard, so the
 * revision is computed from the rows themselves: the same rows need no new
 * delivery, and changed rows are a new revision that may be delivered.
 */
async function statusRevision(states) {
  const rows = states
    .map((s) => [s.phase, s.severity, s.service, s.text, s.link])
    .sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(JSON.stringify(rows)),
  );
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("")
    .slice(0, 16);
}

/**
 * Rewrite the standing status block for one source in one topic.
 *
 * The source decides both the credential and the message: a vendor never
 * touches another vendor's standing message, and a vendor with no credential
 * of its own writes nothing at all.
 *
 * The delivery outcome is scoped to the revision of the content, so a changed
 * standing block is a new delivery and an unchanged one is not.  Only an
 * `unknown` outcome blocks: it means the write may have landed, so repeating
 * it blindly could post twice.  A `refused` outcome is definitive, so the row
 * is released and a later attempt may try again, and the refusal code and
 * retry hint are preserved for the operator.
 *
 * A failed delivery is never swallowed.  The outcome is recorded and a
 * sanitized error is thrown, so the caller learns the standing block is not
 * current instead of reading a successful return as a delivered one.
 */
async function refreshStatus(env, source, channel, topic, options = {}) {
  const credential = options.credential || resolveCredential(env, source);
  if (!credential) {
    throw credentialError(source, missingCredentialNames(env, source));
  }
  // The whole read-decide-write section of one topic runs under that topic's
  // lock, so two refreshes cannot both read "no standing message yet" and
  // both post one.
  return withTopicLock(`${source}\u0000${channel}\u0000${topic}`, () =>
    refreshStatusLocked(env, source, channel, topic, { ...options, credential }),
  );
}

/** One lock per source, channel and topic; a second refresh waits for the first. */
const topicLocks = new Map();

function withTopicLock(key, fn) {
  const previous = topicLocks.get(key) || Promise.resolve();
  const run = previous.then(fn, fn);
  const settled = run.then(
    () => {},
    () => {},
  );
  topicLocks.set(key, settled);
  settled.then(() => {
    if (topicLocks.get(key) === settled) topicLocks.delete(key);
  });
  return run;
}

async function refreshStatusLocked(env, source, channel, topic, options = {}) {
  const credential = options.credential;
  const states = await collectStates(env, source, channel, topic);
  const content = renderStatus({
    topic,
    states,
    updatedAt: ctStamp(),
    source,
  });
  if (!env.WRITER_STORAGE) throw new Error("durable writer required");
  const revision = await statusRevision(states);
  const outcomeKey = deliveryKey(source, channel, topic, revision);
  const barrierKey = deliveryBarrierKey(source, channel, topic);
  const prior = await env.WRITER_STORAGE.get(outcomeKey);
  const barrier = await env.WRITER_STORAGE.get(barrierKey);
  // The uncertainty barrier is scoped to the source, channel and topic, not
  // to the revision of the content.  A POST whose acknowledgement was lost
  // may have landed, so a changed standing block must not post a second
  // standing message on top of it.  A write whose message id is known is
  // safe to retry, because it edits the message it already owns instead of
  // creating another one.
  if (barrier?.outcome === "unknown" && !barrier.id) {
    throw deliveryError(source, "unknown", { code: null, retryAfter: null });
  }
  // A message id the operator just adopted wins over anything else, so a reconcile
  // edits the adopted message and not a stale one.
  //
  // After that, the CURRENT pin wins over an id left on an older outcome row.  The
  // outcome row is history:  it records what a past delivery believed, and a reconcile
  // that moved the standing message does not rewrite it.  Preferring the history would
  // let a returning source edit a message it no longer owns and re-pin it, which is how
  // a source that had been reconciled away came back and took the topic's pin again.
  let statusId =
    options.adoptId ||
    Number(await env.ALERT_STATE.get(statusKey(source, channel, topic), "json")) ||
    prior?.id ||
    null;
  // Pinned ownership: a standing message belongs to exactly one source.  A
  // message id another vendor pinned is never edited here, and never adopted
  // as this source's own standing message.  A message id no source has
  // claimed is not adopted either: it may be this source's own standing
  // message from a write whose acknowledgement was lost, and posting a
  // second one would leave two standing messages in the topic.  The topic is
  // held until an operator reconciles it.
  if (statusId) {
    const owner = await env.ALERT_STATE.get(statusOwnerKey(statusId), "json");
    if (owner === source) {
      // This source's own standing message: edit it in place.
    } else if (owner) {
      // Another source owns that message, so it is not ours to edit.  Each
      // source has its own standing message per topic, so a new one of ours
      // is correct.
      statusId = null;
    } else {
      throw statusHeldError(source, "standing message owner unknown");
    }
  }
  // A barrier that is still `unknown` means an earlier POST may have landed.
  // Only a retry that edits the standing message we already own is safe,
  // because it cannot create a second one.  If the id did not resolve to a
  // message this source owns - the pin was cleared, or the outcome row that
  // carried it is gone - the retry below would POST a fresh message on top of
  // one that may already exist, so the topic stays held instead.
  if (barrier?.outcome === "unknown" && !statusId) {
    throw deliveryError(source, "unknown", { code: null, retryAfter: null });
  }
  // An unchanged block is not a new delivery, so it costs no PATCH and no
  // delivery rows.  All of these must hold:  the outcome row alone can be from
  // an older pass through this revision (A, B, A), and a refusal leaves the
  // barrier `ok` at a revision that never landed.  The operator routes do not
  // pass this option, so a manual rebuild always writes.
  if (
    options.skipUnchanged &&
    !options.adoptId &&
    statusId &&
    prior?.outcome === "ok" &&
    barrier?.outcome === "ok" &&
    barrier.revision === revision &&
    barrier.id === statusId
  ) {
    return content;
  }
  // Commit uncertainty before crossing the network.  A crash or ambiguous reply
  // cannot turn a possibly accepted create into another automatic POST.  The
  // barrier carries the message id as well: an id means the write can be
  // retried, and no id means the write may have created a message nobody
  // knows about, so no further POST may happen for this topic.
  await claimDeliveryBarrier(env, {
    source,
    outcomeKey,
    barrierKey,
    revision,
    statusId,
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
    if (!id) {
      // The write was not confirmed.  Returning the intended content here
      // would answer the caller as if the channel were current, so an
      // unconfirmed write is reported as one.
      throw new Error("status write was not confirmed");
    }
    await env.WRITER_STORAGE.put(outcomeKey, { outcome: "ok", id, revision });
    await env.WRITER_STORAGE.put(barrierKey, { outcome: "ok", id, revision });
    await putIfChanged(
      env,
      statusKey(source, channel, topic),
      JSON.stringify(id),
    );
    await putIfChanged(env, statusOwnerKey(id), JSON.stringify(source));
  } catch (error) {
    const rateLimited = error?.outcome === "rate_limited";
    const outcome =
      error?.outcome === "refused" || rateLimited ? "refused" : "unknown";
    const detail = {
      code: Number.isInteger(error?.status) ? error.status : null,
      retryAfter: Number.isInteger(error?.retryAfter) ? error.retryAfter : null,
    };
    if (rateLimited) {
      await noteRateLimit(env, source, detail.retryAfter);
      await markTopicDirty(env, source, topic);
    }
    if (outcome === "refused") {
      // A refusal is definitive: nothing was delivered, so the row is
      // released and a later attempt may try again.  The code and the retry
      // hint are kept for the operator.
      await env.WRITER_STORAGE.put(outcomeKey, {
        outcome: "refused",
        released: true,
        id: statusId,
        revision,
        ...detail,
      });
      // The refusal was definitive, so nothing landed and the barrier is
      // released with it.
      await env.WRITER_STORAGE.put(barrierKey, {
        outcome: "ok",
        id: statusId,
        revision,
      });
    }
    throw deliveryError(source, outcome, detail);
  }
  return content;
}

/**
 * Claim the uncertainty barrier of one source, channel and topic.
 *
 * The check and the claim are one transaction, so two refreshes cannot both
 * see an open barrier and both write.  A barrier that is still `unknown` with
 * no message id refuses the claim outright: the write it covers may have
 * created a message nobody knows about.
 */
async function claimDeliveryBarrier(env, { source, outcomeKey, barrierKey, revision, statusId }) {
  const storage = env.WRITER_STORAGE;
  const claim = async (txn) => {
    const barrier = await txn.get(barrierKey);
    if (barrier?.outcome === "unknown" && !(barrier.id && statusId)) {
      throw deliveryError(source, "unknown", { code: null, retryAfter: null });
    }
    await txn.put(outcomeKey, { outcome: "unknown", id: statusId, revision });
    await txn.put(barrierKey, { outcome: "unknown", id: statusId, revision });
  };
  if (typeof storage.transaction === "function") {
    await storage.transaction(claim);
    return;
  }
  await claim(storage);
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

/** Write a record only when its value differs:  a read is far cheaper than a write. */
async function putIfChanged(env, key, value) {
  if ((await env.ALERT_STATE.get(key)) === value) return;
  await env.ALERT_STATE.put(key, value);
}

/** True when a state differs from the stored one in more than its timestamp. */
function stateChanged(previous, next) {
  if (!previous) return true;
  const { updated: _a, ...before } = previous;
  const { updated: _b, ...after } = next;
  return JSON.stringify(before) !== JSON.stringify(after);
}

/** The time this source's bot may call Zulip again, or 0. */
async function zulipPausedUntil(env, source) {
  return Number(await env.WRITER_STORAGE?.get(zulipPauseKey(source))) || 0;
}

/** Honor a 429's Retry-After, bounded so a bad header cannot silence alerts for long. */
async function noteRateLimit(env, source, retryAfter) {
  const seconds = Math.min(
    Number.isInteger(retryAfter) && retryAfter > 0 ? retryAfter : ZULIP_PAUSE_DEFAULT_S,
    ZULIP_PAUSE_MAX_S,
  );
  await env.WRITER_STORAGE?.put(zulipPauseKey(source), Date.now() + seconds * 1000);
  console.error(
    JSON.stringify({ event: "alert.zulip_rate_limited", source, retryAfter: seconds }),
  );
}

/** Ask the next sweep for one refresh of this standing block. */
async function markTopicDirty(env, source, topic) {
  const key = dirtyTopicKey(source, topic);
  if ((await env.WRITER_STORAGE?.get(key)) === undefined) {
    await env.WRITER_STORAGE?.put(key, 1);
  }
}

/** Hold a timeline line for the next sweep, under its own transition identity. */
async function queueEvent(env, source, identity, args) {
  const { email: _email, apiKey: _apiKey, ...line } = args;
  const key = QUEUED_EVENT_PREFIX + (await eventDigest(identity));
  await env.WRITER_STORAGE?.put(key, { source, identity, line });
}

/**
 * Tell the operators about a guard that tripped, at most once per interval.
 * It never throws, because it runs on paths that are already failing.
 */
async function notifyOperator(env, kind, what, source, everyMs = NOTICE_EVERY_MS) {
  console.error(JSON.stringify({ event: "alert.operator_notice", kind }));
  const storage = env.WRITER_STORAGE;
  if (!storage) return;
  const key = `notice:${kind}`;
  const now = Date.now();
  const sender = [source, ...NOTICE_SOURCES].find(
    (name) => name && resolveCredential(env, name),
  );
  try {
    if (now - (Number(await storage.get(key)) || 0) < everyMs) return;
    if (!sender || (await zulipPausedUntil(env, sender)) > now) return;
    const credential = resolveCredential(env, sender);
    await storage.put(key, now);
    const posted = await postEvent({
      email: credential.email,
      apiKey: credential.apiKey,
      channel: LOG_CHANNEL,
      topic: "alert-ingress",
      severity: "SEVERE",
      service: "alert-ingress",
      what,
    });
    // An answer with no message id is not a delivered notice.  At worst the retry
    // repeats a notice that did land, which beats a guard nobody hears about.
    if (!posted) {
      // Zulip answered 2xx with no usable id.  The label is for the log alone, so it
      // can be told from a network failure or a storage failure, which log null.
      const error = new Error("operator notice was not confirmed");
      error.outcome = "unconfirmed";
      throw error;
    }
  } catch (error) {
    console.error(
      JSON.stringify({
        event: "alert.operator_notice_failed",
        kind,
        status: error?.status ?? null,
        outcome: error?.outcome ?? null,
      }),
    );
    try {
      await storage.put(key, now - everyMs + NOTICE_RETRY_MS);
      if (error?.outcome === "rate_limited") {
        await noteRateLimit(env, sender, error.retryAfter);
      }
    } catch {}
  }
}

/** A delivery that was not recorded, so the vendor must keep its retry. */
function shedRefused(retryAfter, reason) {
  const error = new Error("delivery shed");
  error.code = "shed_refused";
  error.retryAfter = retryAfter;
  error.reason = reason;
  return error;
}

/** Log and count a delivery that was shed or refused, so none is lost silently. */
function recordShed(env, source, reason, refused) {
  const noted = env.ALERT_BREAKER?.noteShed(source, reason) ?? { today: null, log: true };
  if (!noted.log) return;
  console.error(
    JSON.stringify({
      event: refused ? "alert.delivery_refused" : "alert.delivery_shed",
      source,
      reason,
      today: noted.today,
    }),
  );
}

function inventoryBoundError() {
  const error = new Error("inventory bound exceeded");
  error.code = "inventory_bound";
  return error;
}

async function eventDigest(identity) {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(JSON.stringify(identity)),
  );
  return [...new Uint8Array(digest)]
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

// Returns the message id, or null when Zulip did not confirm the line.  The row
// stays `unknown`, so a later call does not post again unless `replayUnknown` says
// that a repeated line is the better risk.
async function sendEventOnce(env, identity, args, { replayUnknown = false } = {}) {
  if (!env.WRITER_STORAGE) throw new Error("durable writer required");
  const key = "event:" + (await eventDigest(identity));
  const prior = await env.WRITER_STORAGE.get(key);
  if (prior?.outcome === "ok") return prior.id;
  if (prior?.outcome === "unknown" && !replayUnknown) return null;
  await env.WRITER_STORAGE.put(key, { outcome: "unknown" });
  const unconfirmed = () => {
    console.error(JSON.stringify({ event: "alert.event_unconfirmed" }));
    return null;
  };
  try {
    const id = await postEvent(args);
    if (!id) return unconfirmed();
    await env.WRITER_STORAGE.put(key, { outcome: "ok", id });
    return id;
  } catch (error) {
    if (error?.outcome === "rate_limited") {
      // Nothing landed, so the same transition may be sent again after the pause.
      await env.WRITER_STORAGE.put(key, { outcome: "refused" });
      const limited = new Error("event rate limited");
      limited.code = "zulip_rate_limited";
      limited.retryAfter = error.retryAfter;
      throw limited;
    }
    if (error?.outcome === "refused") {
      await env.WRITER_STORAGE.put(key, { outcome: "refused" });
      const refused = new Error("event definitively refused");
      refused.code = "event_refused";
      throw refused;
    }
    return unconfirmed();
  }
}

// The raw copy is a record, not a decision:  it is posted for every
// authenticated delivery, under the service's topic, and a failure to post it
// never changes how the event itself is handled.  A vendor retry posts it again,
// as the old per-vendor integrations did.
async function postRawCopy(env, source, payload) {
  const credential = resolveCredential(env, source);
  if (!credential) return;
  let topic = source;
  try {
    // The topic the event path files this alert under:  its pinned service
    // when it has one, so a renamed vendor label does not split the record.
    const event = normalize(source, payload);
    const pinned = await env.ALERT_STATE.get(svcKey(event.key), "json");
    topic = boundedTopic(typeof pinned === "string" && pinned ? pinned : event.service) || source;
  } catch {
    // A body the event path refuses still leaves a raw record, by source.
  }
  try {
    const id = await postRaw({
      email: credential.email,
      apiKey: credential.apiKey,
      channel: RAW_CHANNEL,
      topic,
      content: renderRaw(source, payload),
    });
    // A 2xx with no usable id may or may not have landed.  It changes nothing, as
    // with any failure of this copy, but it leaves a trace.
    if (!id) console.error(JSON.stringify({ event: "alert.raw_copy_unconfirmed", source }));
  } catch (error) {
    // Its own guard:  this promise is awaited in the route's `finally`, where a
    // rejection would turn an event already recorded into a 503.
    if (error?.outcome === "rate_limited") {
      try {
        await noteRateLimit(env, source, error.retryAfter);
      } catch {}
    }
    console.error(
      JSON.stringify({
        event: "alert.raw_copy_failed",
        source,
        status: error?.status ?? null,
        outcome: error?.outcome ?? null,
      }),
    );
  }
}

// `options.shed` and `options.paused` both skip every Zulip call.  A shed comes
// from the breaker and a pause from a Zulip 429.
async function handleEvent(source, payload, env, options = {}) {
  const { shed = false, paused = false, breaker = null } = options;
  // The route is authoritative: it names the source, and the source names the
  // credential, the state namespace and the standing message.
  const sourceName = String(source || "generic").toLowerCase();
  const credential = resolveCredential(env, sourceName);
  // A source with no credential of its own posts nothing and writes nothing.
  if (!credential) {
    throw credentialError(sourceName, missingCredentialNames(env, sourceName));
  }
  const event = normalize(sourceName, payload);
  const channel = DEFAULT_CHANNEL;
  const perIncident = PER_INCIDENT_SOURCES.has(sourceName);
  let reopening = false;
  // Named before the rename below can change the key, so a reopen clears the
  // tombstone it found rather than another incident's.
  const tombstone = closedKey(event.key);
  if (perIncident && (await env.ALERT_STATE.get(tombstone))) {
    // The incident closed and was pruned.  Only a reopen revives it;
    // anything else is a late or repeated delivery about a closed incident.
    if (!event.reopened) {
      return { action: "ignored", posted: false, status: null, statusDelivery: null };
    }
    // A reopen mints the incident afresh, which a shed does not record.
    if (shed) throw shedRefused(breaker?.retryAfter ?? 60, breaker?.reason ?? "shed");
    reopening = true;
  }
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
  let result = decide(prev, event, now, {
    minOpenMs: Number(env.MIN_OPEN_MS) || DEFAULT_MIN_OPEN_MS,
  });
  // Datadog's CPU monitor already requires sustained saturation.  Do not
  // make a severe CPU event wait through the ingress hold a second time.
  // While Zulip cannot be called it stays pending, and the sweep promotes it.
  if (
    !shed &&
    !paused &&
    result.state.phase === "pending" &&
    isSevereCpu(event.key, result.state.severity)
  ) {
    result = sweep(result.state, now, { minOpenMs: 0 }) || result;
  }

  result.state = advanceRevision(prev, result.state);
  if (shed) {
    // A vendor's retry is its only redelivery, so 200 is answered only when the
    // state is safe:  nothing new to record, or recorded below.  An unknown
    // incident is not recorded in a storm, because a unique-key storm is what
    // would fill the inventory, so it keeps its retry.
    if (!prev && result.state.phase !== "ok") {
      throw shedRefused(breaker?.retryAfter ?? 60, breaker?.reason ?? "shed");
    }
    if (!prev || !stateChanged(prev, result.state)) {
      return { action: "shed", posted: false, status: null, statusDelivery: { outcome: "shed" } };
    }
    if (!(await breaker?.spendShedWrite())) {
      throw shedRefused(breaker?.retryAfter ?? 60, "shed-writes");
    }
  } else {
    // A delivery with no row is the dearest kind, so it has a daily cap of its
    // own.  A recovery of something never recorded has nothing to lose.  A new
    // incident keeps its retry, its raw copy is already on its way to
    // #Alerts-Raw, and the route logs and counts the refusal.
    if (!prev && breaker?.newKeyAllowed && !breaker.newKeyAllowed()) {
      if (result.state.phase === "ok") {
        return {
          action: "shed",
          reason: "new-keys",
          posted: false,
          status: null,
          statusDelivery: { outcome: "shed" },
        };
      }
      await notifyOperator(
        env,
        `new-keys:${sourceName}`,
        `${sourceName} is past the daily cap of ${NEW_KEYS_PER_DAY} new incidents, so new ones are refused for the vendor to retry.  Known incidents still move.  /status counts what was refused.`,
        sourceName,
      );
      throw shedRefused(secondsToNextDay(now), "new-keys");
    }
    // A recovery of something never recorded has nothing to record, and a closed
    // row would cost as much as a transition and take a place in the inventory.
    // A per-incident source keeps its tombstone, because that one row stops a late
    // trigger from opening what the vendor already closed.
    if (!prev && result.state.phase === "ok") {
      if (perIncident) await env.ALERT_STATE.put(closedKey(event.key), String(now));
      return { action: result.action, posted: false, status: null, statusDelivery: null };
    }
    // Over the bound, an incident already recorded still moves, so a recovery
    // cannot be lost, and a new one is refused with a notice instead of
    // failing the same way forever with nobody told.
    let inventory = null;
    try {
      inventory = await collectStates(env, sourceName, channel, topic);
    } catch (error) {
      if (error?.code !== "inventory_bound") throw error;
    }
    const known = inventory
      ? inventory.some((row) => row.key === event.key)
      : !!prev;
    if (!known && (!inventory || inventory.length >= MAX_KEYS)) {
      await notifyOperator(
        env,
        `inventory:${sourceName}`,
        `The ${sourceName} inventory is over its ${MAX_KEYS}-key bound, so new incidents are being refused.  Closed rows need pruning.`,
        sourceName,
      );
      throw shedRefused(3600, "inventory");
    }
  }
  if (event.identityRedacted) {
    console.error(JSON.stringify({ event: "alert.identity_redacted", source: sourceName }));
  }
  if (reopening) await env.ALERT_STATE.delete(tombstone);
  let posted = null;
  let rateLimited = false;
  const sev =
    result.state.phase === "alerting" ? result.state.severity : "INFO";
  const line =
    result.action === "announce" || result.action === "update"
      ? {
          severity: sev,
          what: result.state.text,
          link: result.state.link,
          mentions: resourceMentions(event.key, result.state.severity),
        }
      : result.action === "resolve"
        ? { severity: "INFO", what: "recovered", link: event.link }
        : null;
  // A storm's timeline is noise, so a shed queues no line.  A pause is short and
  // its lines are real, so they wait for the sweep.
  if (line && !shed) {
    const identity = transitionIdentity(event.key, result.action, prev, result.state);
    const args = {
      email: credential.email,
      apiKey: credential.apiKey,
      channel: LOG_CHANNEL,
      topic,
      service: event.service,
      ...line,
    };
    if (paused) {
      await queueEvent(env, sourceName, identity, args);
    } else {
      try {
        posted = await sendEventOnce(env, identity, args);
      } catch (error) {
        if (error?.code !== "zulip_rate_limited") throw error;
        rateLimited = true;
        await noteRateLimit(env, sourceName, error.retryAfter);
        await queueEvent(env, sourceName, identity, args);
      }
    }
  }
  // Spent only now, once the delivery is sure to be recorded:  a vendor retrying
  // an inventory or a Zulip refusal costs no allowance, and a recovery of an
  // incident never recorded has already returned, so it cannot use up what a real
  // new incident needs.  A shed never reaches here with no row.
  if (!prev) await breaker?.spendNewKey?.();

  const wasPending = prev?.phase === "pending";
  const isPending = result.state.phase === "pending";
  if (perIncident && result.state.phase === "ok") {
    // A closed incident is pruned:  its row leaves the standing block and its
    // records leave the inventory, and the tombstone keeps it closed.
    await env.ALERT_STATE.put(closedKey(event.key), String(now));
    for (const key of [
      indexPrefix(sourceName, channel, topic) + event.key,
      stateKey(event.key),
      svcKey(event.key),
      srcKey(event.key),
    ]) {
      await env.ALERT_STATE.delete(key);
    }
  } else {
    // The index goes first:  a crash then leaves an entry the sweep discards,
    // never a pending incident the sweep cannot see.
    if (isPending && !wasPending) {
      await env.WRITER_STORAGE?.put(pendingKey(event.key), 1);
    }
    // A repeat on a closed row leaves `updated` where it was, so it is not a touch.
    const touched =
      result.state.updated !== prev?.updated &&
      now - (prev?.updated || 0) >= STATE_TOUCH_MS;
    if (stateChanged(prev, result.state) || touched) {
      await env.ALERT_STATE.put(stateKey(event.key), JSON.stringify(result.state));
    }
    await putIfChanged(env, svcKey(event.key), JSON.stringify(event.service));
    // The source is recorded with the state so the sweep can resolve the right
    // vendor credential for this incident.
    await putIfChanged(env, srcKey(event.key), JSON.stringify(sourceName));
    if (event.key === `${sourceName}:${event.service.toLowerCase()}`) {
      await putIfChanged(env, svcNameKey(sourceName, event.service), JSON.stringify(event.key));
    }
    await putIfChanged(env, indexPrefix(sourceName, channel, topic) + event.key, "1");
  }
  if (wasPending && !isPending) {
    await env.WRITER_STORAGE?.delete?.(pendingKey(event.key));
  }
  await putIfChanged(env, topicKey(sourceName, channel, topic), "1");

  // No Zulip call now:  the state is recorded, so the vendor gets its 200 and
  // the next sweep rewrites the block once, however many events arrived.
  if (shed || paused || rateLimited) {
    await markTopicDirty(env, sourceName, topic);
    return {
      action: shed ? "shed" : result.action,
      posted: false,
      status: null,
      statusDelivery: { outcome: shed ? "shed" : "paused" },
    };
  }

  // Every outcome refreshes the standing status, which is what makes the
  // channel readable at a glance instead of by scrolling.
  //
  // A delivery failure is reported, not thrown.  The incident is recorded and
  // the vendor's event was accepted, so the webhook must not fail over a
  // standing block that could not be written; the caller is told the status
  // is stale instead.
  let status = null;
  let statusDelivery = null;
  try {
    status = await refreshStatus(env, sourceName, channel, topic, {
      credential,
      skipUnchanged: true,
    });
  } catch (error) {
    if (error?.code === "inventory_bound") {
      statusDelivery = { outcome: "overflow" };
    } else if (error?.code === "status_held") {
      // The standing block for this topic is held: a standing message exists
      // that no source has claimed, so it is neither edited nor duplicated.
      // The event was accepted and recorded, so the webhook must not fail
      // over it; the caller is told the status is held instead.
      statusDelivery = { outcome: "held", reason: error.reason };
      console.error(
        JSON.stringify({
          event: "alert.status_held",
          source: sourceName,
          reason: error.reason,
        }),
      );
    } else {
      if (error?.code !== "status_delivery_failed") throw error;
      statusDelivery = {
        outcome: error.outcome,
        code: error.detail?.code ?? null,
        retryAfter: error.detail?.retryAfter ?? null,
      };
      console.error(
        JSON.stringify({
          event: "alert.status_delivery_failed",
          source: sourceName,
          outcome: error.outcome,
        }),
      );
    }
  }

  return { action: result.action, posted: !!posted, status, statusDelivery };
}

/** Promote aged incidents.  Wire as a cron trigger. */
async function runSweep(env) {
  const now = Date.now();
  const minOpenMs = Number(env.MIN_OPEN_MS) || DEFAULT_MIN_OPEN_MS;
  const storage = env.WRITER_STORAGE;
  const calls = sweepBudget.invocation();
  const { keys, checked, overflow, page } = await pendingForSweep(env, now);
  let examined = null;
  let promoted = 0;
  let deferred = 0;
  // Topics are per source: two vendors can share a service name, and each
  // one owns its own standing message.
  const dirtyTopics = new Map();
  // The inventory a promotion depends on, read before its first send.  A topic
  // that cannot be read fails here, with nothing posted.  One that is only over
  // its bound still promotes, because an announced alert beats a current block.
  const preflighted = new Set();
  for (const key of keys) {
    const name = stateKey(key);
    // Promotion is the only step that can stop the loop, so anything that returns
    // to the top of it has been looked at.
    const lookedAt = examined;
    examined = key;
    // One incident can fail for many reasons, and none of them may abort the
    // batch: a source lookup that throws, a credential that cannot be
    // resolved, a state that cannot be read.  Every item is handled on its
    // own, and the sweep continues with the next one.
    try {
      const prev = await env.ALERT_STATE.get(name, "json");
      if (prev?.phase !== "pending") {
        await storage?.delete?.(pendingKey(key));
        continue;
      }
      const s = sweep(prev, now, { minOpenMs });
      if (!s) continue;
      s.state = advanceRevision(prev, s.state);
      const source = await resolveStateSource(env, key);
      const service = (await env.ALERT_STATE.get(svcKey(key), "json")) || key;
      const topic = boundedTopic(service);
      const credential = resolveCredential(env, source);
      // No credential of its own means no post at all: the incident stays
      // pending until the source has one, and it is never sent as another bot.
      if (!credential) {
        console.error(JSON.stringify({ event: "alert.credential_unavailable" }));
        continue;
      }
      if ((await zulipPausedUntil(env, source)) > now) {
        deferred++;
        continue;
      }
      const preflightKey = `${source}\u0000${topic}`;
      if (!preflighted.has(preflightKey)) {
        try {
          await collectStates(env, source, DEFAULT_CHANNEL, topic);
        } catch (error) {
          if (error?.code !== "inventory_bound") throw error;
        }
        preflighted.add(preflightKey);
      }
      calls.spend("zulipCall");
      await sendEventOnce(
        env,
        transitionIdentity(key, "announce", prev, s.state),
        {
          email: credential.email,
          apiKey: credential.apiKey,
          channel: LOG_CHANNEL,
          topic,
          severity: s.state.severity,
          service,
          what: s.state.text,
          link: s.state.link,
          mentions: resourceMentions(key, s.state.severity),
        },
      );
      await env.ALERT_STATE.put(name, JSON.stringify(s.state));
      await putIfChanged(env, srcKey(key), JSON.stringify(source));
      await storage?.delete?.(pendingKey(key));
      promoted++;
      dirtyTopics.set(`${source}\u0000${topic}`, { source, topic });
    } catch (error) {
      // The budget is spent, so the rest wait for the next sweep.
      if (error instanceof BudgetExceeded) {
        // This one did not get its turn, so the next sweep starts with it.
        examined = lookedAt;
        deferred++;
        break;
      }
      if (error?.code === "zulip_rate_limited") {
        const source = await resolveStateSource(env, key).catch(() => null);
        if (source) await noteRateLimit(env, source, error.retryAfter);
        deferred++;
        continue;
      }
      console.error(
        JSON.stringify({ event: "alert.promotion_failed", code: error?.code ?? null }),
      );
    }
  }
  await movePendingCursor(storage, page, examined);
  deferred += await flushQueuedEvents(env, calls, now);
  for (const { source, topic } of await listDirtyTopics(env)) {
    dirtyTopics.set(`${source}\u0000${topic}`, { source, topic });
  }
  // One status refresh per source and topic, however many incidents promoted
  // in it, and all in parallel instead of one rebuild per incident.
  await mapBounded([...dirtyTopics.values()], async ({ source, topic }) => {
    try {
      if ((await zulipPausedUntil(env, source)) > Date.now()) {
        await markTopicDirty(env, source, topic);
        deferred++;
        return;
      }
      calls.spend("zulipCall");
      await storage?.delete?.(dirtyTopicKey(source, topic));
      await refreshStatus(env, source, DEFAULT_CHANNEL, topic, { skipUnchanged: true });
    } catch (error) {
      if (error instanceof BudgetExceeded) {
        await markTopicDirty(env, source, topic);
        deferred++;
        return;
      }
      // An ambiguous write to a message this source owns is safe to repeat, so it
      // stays on the list.  A refusal or a held topic will not improve by retrying.
      if (error?.code === "status_delivery_failed" && error.outcome === "unknown") {
        const barrier = await storage?.get(deliveryBarrierKey(source, DEFAULT_CHANNEL, topic));
        if (barrier?.id) await markTopicDirty(env, source, topic);
      }
      console.error(JSON.stringify({ event: "alert.status_refresh_failed" }));
    }
  });
  return { promoted, checked, deferred, overflow };
}

/**
 * The incidents to look at:  the pending index, rebuilt from every state row on
 * the first sweep after a deploy that adds it, then hourly as a self-heal.
 *
 * That full pass also does the legacy marker repair.  Records written before
 * vendor routing have no per-source indices, so the new readers cannot see them.
 * It stays rather than being removed, because the writer's storage cannot be read
 * from outside to show that no such record is left.
 */
async function pendingForSweep(env, now) {
  const storage = env.WRITER_STORAGE;
  const usable = typeof storage?.list === "function";
  if (usable && now - (Number(await storage.get(RECONCILED_AT)) || 0) < RECONCILE_EVERY_MS) {
    const page = await pendingPage(storage);
    return { keys: page.keys, checked: page.keys.length, overflow: false, page };
  }
  let all;
  try {
    all = await listAll(env.ALERT_STATE, "state:");
  } catch (error) {
    if (error?.code !== "inventory_bound" || !usable) throw error;
    // Promotions keep running from the index, so one overflow cannot stop them.
    await notifyOperator(
      env,
      "inventory:state",
      `The state inventory is over its ${MAX_KEYS}-key bound, so the reconcile cannot run.  Closed rows need pruning.`,
    );
    await stampReconciled(storage, now);
    const page = await pendingPage(storage);
    return { keys: page.keys, checked: page.keys.length, overflow: true, page };
  }
  const keys = [];
  const keep = new Set();
  // What the index already holds.  An unchanged entry costs no write:  rewriting
  // each one hourly is 24 rows a day per pending incident, to store the same 1.
  // A failed read must not cost the sweep:  this pass already holds every state
  // row, so it promotes from those.  With the index unknown it adds each pending
  // entry and removes none, and the next reconcile puts the rest right.
  let present = null;
  if (usable) {
    try {
      present = await allPending(storage);
    } catch {
      present = null;
      console.error(JSON.stringify({ event: "alert.pending_index_failed" }));
    }
  }
  for (const { name } of all) {
    const key = name.slice("state:".length);
    try {
      const state = await env.ALERT_STATE.get(name, "json");
      if (!state) continue;
      const source = await resolveStateSource(env, key);
      const service = (await env.ALERT_STATE.get(svcKey(key), "json")) || key;
      const topic = boundedTopic(service);
      const marker = indexPrefix(source, DEFAULT_CHANNEL, topic) + key;
      if ((await env.ALERT_STATE.get(marker)) === null) {
        await env.ALERT_STATE.put(marker, "1");
        await env.ALERT_STATE.put(topicKey(source, DEFAULT_CHANNEL, topic), "1");
      }
      if (state.phase !== "pending") continue;
      keys.push(key);
      keep.add(key);
      if (!present?.has(key)) await storage?.put(pendingKey(key), 1);
    } catch {
      // A row that cannot be read keeps any index entry it has.
      keep.add(key);
      console.error(JSON.stringify({ event: "alert.promotion_failed", code: "reconcile" }));
    }
  }
  if (usable) {
    // Tidying the index must not cost the promotions, which have not run yet.  A
    // stale entry that stays is dropped when a sweep reads it or by the next pass.
    try {
      for (const key of present ?? []) {
        if (!keep.has(key)) await storage.delete?.(pendingKey(key));
      }
    } catch {
      console.error(JSON.stringify({ event: "alert.pending_reconcile_write_failed", step: "delete" }));
    }
    await stampReconciled(storage, now);
  }
  return { keys, checked: all.length, overflow: false, page: null };
}

/** Record the pass.  If that fails, the next sweep repeats it and nothing else is lost. */
async function stampReconciled(storage, now) {
  try {
    await storage.put(RECONCILED_AT, now);
  } catch {
    console.error(JSON.stringify({ event: "alert.pending_reconcile_write_failed", step: "stamp" }));
  }
}

/** One sweep's turn at the pending index, resuming where the last one stopped. */
async function pendingPage(storage) {
  const cursor = (await storage.get(PENDING_CURSOR)) || null;
  const listed = await storage.list({
    prefix: PENDING_PREFIX,
    limit: PENDING_SCAN_LIMIT,
    ...(cursor ? { startAfter: pendingKey(cursor) } : {}),
  });
  const keys = [...listed.keys()].map((name) => name.slice(PENDING_PREFIX.length));
  return { keys, cursor, full: keys.length >= PENDING_SCAN_LIMIT };
}

/** Every entry of the pending index, for the hourly reconcile. */
async function allPending(storage) {
  const present = new Set();
  let after;
  for (let turn = 0; turn < 20; turn++) {
    const listed = await storage.list({
      prefix: PENDING_PREFIX,
      limit: PENDING_SCAN_LIMIT,
      ...(after ? { startAfter: after } : {}),
    });
    for (const name of listed.keys()) {
      present.add(name.slice(PENDING_PREFIX.length));
      after = name;
    }
    if (listed.size < PENDING_SCAN_LIMIT) break;
  }
  return present;
}

/** Save where the next sweep resumes.  It costs a row only while the index is long. */
async function movePendingCursor(storage, page, examined) {
  if (!page) return;
  try {
    if (page.full) {
      if (examined && examined !== page.cursor) await storage.put(PENDING_CURSOR, examined);
    } else if (page.cursor) {
      await storage.delete?.(PENDING_CURSOR);
    }
  } catch {
    // A cursor that did not move repeats a turn, and the next sweep tries again.
    console.error(JSON.stringify({ event: "alert.pending_cursor_failed" }));
  }
}

/** Post the timeline lines a pause held back.  Returns how many still wait. */
async function flushQueuedEvents(env, calls, now) {
  const storage = env.WRITER_STORAGE;
  if (typeof storage?.list !== "function") return 0;
  const queued = await storage.list({ prefix: QUEUED_EVENT_PREFIX, limit: CATCH_UP_BATCH });
  let waiting = 0;
  for (const [key, item] of queued) {
    try {
      if ((await zulipPausedUntil(env, item.source)) > now) {
        waiting++;
        continue;
      }
      const credential = resolveCredential(env, item.source);
      if (!credential) {
        await storage.delete?.(key);
        continue;
      }
      calls.spend("zulipCall");
      const id = await sendEventOnce(
        env,
        item.identity,
        { ...item.line, email: credential.email, apiKey: credential.apiKey },
        { replayUnknown: true },
      );
      if (id) {
        await storage.delete?.(key);
        continue;
      }
      // Zulip did not confirm it, and this line may carry the only ping for an
      // alert, so it is sent again on the next sweeps:  a repeated line beats a
      // missing one.  It is bounded, because a row kept for good would hold a place
      // in the batch, and giving up is logged and told to the operators.
      const attempts = (Number(item.attempts) || 0) + 1;
      if (attempts < QUEUED_LINE_ATTEMPTS) {
        await storage.put(key, { ...item, attempts });
        waiting++;
        continue;
      }
      await storage.delete?.(key);
      console.error(JSON.stringify({ event: "alert.queued_event_abandoned", source: item.source }));
      await notifyOperator(
        env,
        `queued-line:${item.source}`,
        `A held ${item.source} timeline line was not confirmed by Zulip after ${QUEUED_LINE_ATTEMPTS} tries, so it may be missing from ${LOG_CHANNEL}.  The incident itself is recorded and shows in its standing block.`,
        item.source,
      );
    } catch (error) {
      if (error instanceof BudgetExceeded) return waiting + 1;
      if (error?.code === "zulip_rate_limited") {
        await noteRateLimit(env, item.source, error.retryAfter);
        waiting++;
        continue;
      }
      console.error(JSON.stringify({ event: "alert.queued_event_failed", source: item.source }));
      // A definitive refusal will not improve with retries every five minutes.
      // Anything else, such as a storage error, may not have sent the line at all.
      if (error?.code === "event_refused") await storage.delete?.(key);
      else waiting++;
    }
  }
  return waiting;
}

/** The standing blocks waiting for a refresh, at most one batch per sweep. */
async function listDirtyTopics(env) {
  const storage = env.WRITER_STORAGE;
  if (typeof storage?.list !== "function") return [];
  const listed = await storage.list({ prefix: DIRTY_TOPIC_PREFIX, limit: CATCH_UP_BATCH * 2 });
  return [...listed.keys()].map((key) => {
    const [source, topic] = JSON.parse(key.slice(DIRTY_TOPIC_PREFIX.length));
    return { source, topic };
  });
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
      return json({ ok: true, sources: ["datadog", "betterstack", "sentry", "pagerduty", "generic"] });
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
      // What the guards shed today, where an operator can read it.  A breaker that
      // cannot read its count must not take the read down with it.
      const guards = await env.ALERT_BREAKER?.snapshot().catch(() => null);
      return json({
        generatedAt: new Date().toISOString(),
        guards: guards ?? null,
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
      // A malformed percent-escape is a bad request, not a server error, so it
      // is classified here rather than escaping as an unhandled URIError.
      let channel;
      let topic;
      let source = null;
      try {
        channel = decodeURIComponent(segments ? segments[1] : statusMatch[1]);
        topic = decodeURIComponent(segments ? segments[2] : statusMatch[2]);
        if (segments) source = decodeURIComponent(segments[0]).toLowerCase();
      } catch {
        return json({ error: "invalid path" }, 400);
      }
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
        if (!SOURCE_TOKEN.test(source)) {
          return json({ error: "unknown source", source }, 400);
        }
        // The source's own credential, bound because an adopted id is proved with it:
        // the message has to be shown to have been sent by this source's bot.
        const credential = resolveCredential(env, source);
        if (!credential) {
          return json(
            {
              error: "credential unavailable",
              source,
              missing: missingCredentialNames(env, source),
            },
            503,
          );
        }
        // Reconciliation.  A write whose acknowledgement was lost may have
        // created a standing message nobody knows about, and the topic is
        // held until it is named.  The operator supplies the message id from
        // the Zulip channel; the worker adopts it, claims it for this source
        // and releases the barrier, so the topic resumes on its own.
        const reconcile = url.searchParams.get("reconcile");
        let adoptId = null;
        if (reconcile !== null) {
          const id = Number(reconcile);
          if (!Number.isSafeInteger(id) || id <= 0) {
            return json({ error: "invalid reconcile id", reconcile }, 400);
          }
          if (!env.WRITER_STORAGE) {
            return json({ error: "durable writer required" }, 503);
          }
          // A message another source pinned is never adopted: that would let
          // this source edit, or duplicate, another vendor's standing message.
          const pinned = await env.ALERT_STATE.get(statusOwnerKey(id), "json");
          if (pinned && pinned !== source) {
            return json(
              { error: "message owned by another source", source, id },
              409,
            );
          }
          // An unpinned id is proved before it is adopted.  A bare id names any
          // message in the realm, and a PATCH on it edits whatever it points at, so
          // the id has to be shown to be this source's standing message in THIS
          // topic before anything is adopted or mutated.  A message that cannot be
          // read is not adopted either:  refusing costs one round trip, while a
          // wrong adoption edits somebody else's message.
          const message = await fetchMessage({
            email: credential.email,
            apiKey: credential.apiKey,
            messageId: id,
          });
          if (!message) {
            return json(
              { error: "message could not be read", source, id },
              503,
            );
          }
          const channelName =
            typeof message.display_recipient === "string"
              ? message.display_recipient
              : String(message.display_recipient?.name || "");
          const sameTopic =
            message.type === "stream" &&
            channelName === String(DEFAULT_CHANNEL) &&
            String(message.subject || "") === String(topic);
          const sameSender = String(message.sender_email || "") === credential.email;
          // Channel, topic and sender do not prove the message is the standing
          // block.  This source's one-line alert messages are posted to the
          // same channel and topic by the same bot, so an alert line satisfies
          // every one of those checks.  Adopting one would point the pin at an
          // event message that refreshStatus then PATCHes with the rendered
          // status block, silently destroying that alert's content.  Only the
          // standing block carries the rendered status header, so that header is
          // required before anything is adopted or mutated.
          const isStandingBlock = String(message.content || "").startsWith(
            STANDING_BLOCK_HEADER,
          );
          if (!sameTopic || !sameSender || !isStandingBlock) {
            return json(
              { error: "message is not this source's standing message", source, id },
              409,
            );
          }
          await env.ALERT_STATE.put(
            statusKey(source, DEFAULT_CHANNEL, topic),
            JSON.stringify(id),
          );
          await env.ALERT_STATE.put(statusOwnerKey(id), JSON.stringify(source));
          adoptId = id;
          await env.WRITER_STORAGE.put(
            deliveryBarrierKey(source, DEFAULT_CHANNEL, topic),
            { outcome: "ok", id, reconciled: true },
          );
        }
        try {
          const content = await refreshStatus(
            env,
            source,
            DEFAULT_CHANNEL,
            topic,
            { adoptId },
          );
          return json({ source, channel: DEFAULT_CHANNEL, topic, content });
        } catch (error) {
          // The standing block could not be written.  That is a bad gateway,
          // not a silent success: the caller must not read the route as
          // confirmation that the channel is current.
          if (error?.code === "status_held") {
            // The standing message for this topic exists but no source has
            // claimed it, so it is neither edited nor duplicated.  The topic
            // stays held until an operator reconciles it.
            return json(
              { error: "status held", source, reason: error.reason },
              409,
            );
          }
          if (error?.code !== "status_delivery_failed") throw error;
          return json(
            { error: "status delivery failed", source, outcome: error.outcome },
            502,
          );
        }
      }
      // The legacy route carries no vendor dimension, so it refreshes every
      // source that tracks the topic, each with its own credential.  A topic
      // nobody tracks still gets the generic source's standing status, which
      // is what this route always did.
      const sources = await sourcesForTopic(env, DEFAULT_CHANNEL, topic);
      const tracked = sources.length > 0;
      // Null-prototype map, like the inventory in collectTopics: a source
      // name comes from the vendor route name, so a route literally named
      // "__proto__" must not set this object's prototype and erase the
      // response that carries it.
      const rendered = Object.create(null);
      const unavailable = [];
      const failed = [];
      const held = [];
      // The sources are refreshed in parallel.  A serial loop made the
      // response wait for every source's Zulip timeout in turn, so the route
      // could take N times the timeout of a single source.
      const results = await mapBounded(
        tracked ? sources : ["generic"],
        async (source) => {
          if (!resolveCredential(env, source)) {
            return { source, unavailable: true };
          }
          try {
            return {
              source,
              content: await refreshStatus(
                env,
                source,
                DEFAULT_CHANNEL,
                topic,
              ),
            };
          } catch (error) {
            if (error?.code === "status_held") {
              return { source, held: true, reason: error.reason };
            }
            if (error?.code !== "status_delivery_failed") throw error;
            return { source, failed: true, outcome: error.outcome };
          }
        },
      );
      for (const result of results) {
        if (result.unavailable) {
          unavailable.push({
            source: result.source,
            error: "credential unavailable",
          });
          continue;
        }
        if (result.failed) {
          failed.push({
            source: result.source,
            error: "status delivery failed",
            outcome: result.outcome,
          });
          continue;
        }
        if (result.held) {
          held.push({
            source: result.source,
            error: "status held",
            reason: result.reason,
          });
          continue;
        }
        rendered[result.source] = result.content;
      }
      return json({
        channel: DEFAULT_CHANNEL,
        topic,
        sources: rendered,
        unavailable,
        failed,
        held,
      });
    }

    const hookMatch = url.pathname.match(/^\/webhook\/([a-z0-9_-]{1,32})$/i);
    if (!hookMatch) return json({ error: "not found" }, 404);
    if (request.method !== "POST") return json({ error: "POST only" }, 405);

    const source = hookMatch[1].toLowerCase();
    let raw;
    try { raw = new TextDecoder().decode(await readBoundedBody(request, MAX_BODY_BYTES)); }
    catch (error) {
      if (error instanceof BodyLimitError) return json({ error: "payload too large" }, 413);
      return json({ error: "invalid body" }, 400);
    }
    if (!(await verify(request, raw, env, source))) {
      return json({ error: "bad signature" }, 401);
    }

    // The authenticated route names the source, and the source names the
    // credential.  A source with no credential of its own fails closed: it
    // posts nothing, writes no status, and never borrows another source's
    // identity.
    if (!resolveCredential(env, source)) {
      return json(
        {
          error: "credential unavailable",
          source,
          missing: missingCredentialNames(env, source),
        },
        503,
      );
    }

    // An authenticated sender posting nothing means real events are being lost
    // at the vendor.  The entry point lets one such delivery through per isolate
    // per day so that it is reported here, with the throttle in durable storage.
    if (raw === "") {
      await notifyOperator(
        env,
        `empty-body:${source}`,
        `${source} is delivering webhooks with an empty body, so its incidents are not reaching #Alerts.  Its webhook needs a body at the vendor.`,
        source,
        NOTICE_DAILY_MS,
      );
      return json({ error: "empty body" }, 400);
    }

    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      return json({ error: "invalid json" }, 400);
    }

    try {
      // A Sentry body carries the vendor's native shape, which the generic
      // boundary rule refuses, so it has a rule of its own.  Every other
      // source keeps the generic rule unchanged.
      if (source === "sentry") validateSentryPayload(payload);
      else if (source === "pagerduty") validatePagerDutyPayload(payload);
      else validatePayload(payload);
    } catch {
      return json({ error: "invalid payload" }, 400);
    }
    // A PagerDuty subscription also delivers its own ping and events about
    // things that are not incidents.  They are authenticated and well formed,
    // and they say nothing about an alert, so they are acknowledged and
    // dropped:  answering 4xx would make PagerDuty retry, then disable the
    // subscription.
    if (source === "pagerduty" && !pagerDutyIncidentEvent(payload)) {
      return json({ action: "ignored", posted: false });
    }
    const breaker = env.ALERT_BREAKER ? await env.ALERT_BREAKER.admit(source) : null;
    const shed = breaker?.shed === true;
    if (breaker?.trippedNow) {
      await notifyOperator(
        env,
        `breaker:${breaker.reason}:${source}`,
        `Alert storm from ${source}:  past the ${breaker.reason === "day" ? "daily" : "per-minute"} delivery cap, so its deliveries are being recorded without Zulip calls, and new incidents are refused for the vendor to retry.`,
        source,
      );
    }
    const paused = !shed && (await zulipPausedUntil(env, source)) > Date.now();
    // The raw copy runs beside the event path rather than before it, so a
    // delivery waits for one Zulip round trip, not two.  It is awaited before
    // the response either way, because the writer's request is what keeps it
    // alive, and postRawCopy never throws.  A shed or a pause skips it, because
    // these copies are what would keep Zulip rate limiting the bot.
    const rawCopy = shed || paused ? null : postRawCopy(env, source, payload);
    try {
      return json(
        await handleEvent(source, payload, env, {
          shed,
          paused,
          breaker: breaker && {
            ...breaker,
            spendShedWrite: () => env.ALERT_BREAKER.spendShedWrite(),
            newKeyAllowed: () => env.ALERT_BREAKER.newKeyAllowed(source),
            spendNewKey: () => env.ALERT_BREAKER.spendNewKey(source),
          },
        }).then((result) => {
          if (result.action === "shed") {
            recordShed(env, source, result.reason ?? breaker?.reason ?? "shed", false);
          }
          return result;
        }),
      );
    } catch (error) {
      // Nothing was recorded, so the vendor keeps its retry.
      if (error?.code === "shed_refused") {
        recordShed(env, source, error.reason, true);
        return new Response(
          JSON.stringify({ error: "delivery shed", reason: error.reason }),
          {
            status: 503,
            headers: {
              "Content-Type": "application/json",
              "Retry-After": String(error.retryAfter),
            },
          },
        );
      }
      if (error?.code === "credential_unavailable") {
        return json(
          {
            error: "credential unavailable",
            source: error.source,
            missing: error.missing || [],
          },
          503,
        );
      }
      throw error;
    } finally {
      await rawCopy;
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
  pendingKey,
  sourcesForTopic,
  parseTopicKey,
  parseLegacyTopicKey,
  resolveStateSource,
  resolveCredential,
  credentialPairs,
  credentialSlug,
  missingCredentialNames,
  statusRevision,
  safeEqual,
  stateKey,
  srcKey,
  statusKey,
  statusOwnerKey,
  svcKey,
  topicKey,
  deliveryKey,
  deliveryBarrierKey,
  statusHeldError,
  ctStamp,
  indexPrefix,
};

const MAX_KEYS = 2000;
const STATUS_MEMO_MS = 30_000;
// Object-envelope nesting bound.  A JSON:API vendor body reaches four levels
// (data.relationships.monitor.data); anything deeper is rejected, not walked.
const MAX_PAYLOAD_DEPTH = 6;

/**
 * The one body-size limit, in encoded UTF-8 bytes.
 *
 * Bytes, not UTF-16 code units:  a non-ASCII body can be far larger in UTF-8
 * than its .length suggests, and the documented limit is 64 KiB.  One limit
 * rules for every source, and it is checked before the body is verified or
 * parsed, so an oversized body costs no signature work and no parse.
 */
const MAX_BODY_BYTES = 64 * 1024;
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
    if (keys.length > MAX_KEYS) throw inventoryBoundError();
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
    // Datadog's full Markdown body (`event_msg`, for the raw copy) carries the
    // monitor's runbook, so it alone may run past 4 KiB;  the 64 KiB body
    // limit still bounds it.
    const longest = key === "event_msg" ? 32768 : 4096;
    if (
      typeof value === "string" &&
      (value.length > longest || /[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(value))
    )
      throw new Error("invalid text");
  }
}

/**
 * Validate a Sentry payload.
 *
 * A native Sentry payload nests objects -- the issue, its project, its
 * metadata, the metric alert and its rule -- and the generic boundary rule
 * rejects every object it does not know, so a Sentry body failed validation
 * as an invalid text field and the route could never accept one.
 *
 * This rule accepts the native shape and keeps the guarantee that matters:
 * the body is bounded in depth, in breadth and in text length, and control
 * characters are refused.  The parser reads only text, so a nested blob can
 * never become a topic name, a label or a link.
 *
 * On top of the bound, every field the parser consumes is checked for the
 * type the parser reads it as, and the field that names the alert is
 * required.  Without that, a body carrying no identity was normalized under
 * a constant key, so two different malformed alerts shared one incident and
 * one alert's resolution closed the other's incident and standing message.
 */
const MAX_SENTRY_NODES = 2000;

/**
 * A consumed Sentry field, read the way the normalizer reads it.
 *
 * The normalizer reads a field as text only when it is a string or a finite
 * number, so the boundary uses the same rule.  A field of any other type is
 * refused rather than ignored, because the parser would then see a different
 * payload from the one that was signed.
 */
function sentryText(value) {
  if (typeof value === "string") return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

/** The text of the first of these fields that carries one. */
function sentryFirstText(source, fields) {
  for (const field of fields) {
    const text = sentryText(source?.[field]);
    if (text !== null && text.trim() !== "") return text.trim();
  }
  return "";
}

/**
 * Check the consumed fields of an issue.
 *
 * The identity names the incident, the title names the alert, the level
 * decides the severity and the project names the service, so all four are
 * required:  a body missing any of them is not an alert this worker can file,
 * and a body missing the identity has no key to file it under.  Every other
 * consumed field is checked for the type the parser reads it as, and may be
 * absent, because the parser has a documented fallback for each.
 */
function validateSentryIssue(issue) {
  if (!sentryFirstText(issue, ["shortId", "id"])) throw new Error("missing identity");
  if (!sentryFirstText(issue, ["title"])) throw new Error("missing title");
  if (!sentryFirstText(issue, ["level"])) throw new Error("missing level");
  for (const field of ["level", "status", "url", "permalink", "culprit"]) {
    const value = issue[field];
    if (value === undefined || value === null) continue;
    if (sentryText(value) === null) throw new Error("invalid text field");
  }
  const project = issue.project;
  if (!project || typeof project !== "object" || Array.isArray(project)) {
    throw new Error("invalid payload");
  }
  for (const field of ["slug", "name"]) {
    const value = project[field];
    if (value === undefined || value === null) continue;
    if (sentryText(value) === null) throw new Error("invalid text field");
  }
  const metadata = issue.metadata;
  if (metadata !== undefined && metadata !== null) {
    if (typeof metadata !== "object" || Array.isArray(metadata)) {
      throw new Error("invalid payload");
    }
    for (const field of ["type", "value"]) {
      const value = metadata[field];
      if (value === undefined || value === null) continue;
      if (sentryText(value) === null) throw new Error("invalid text field");
    }
  }
}

/**
 * Check the consumed fields of a metric alert.
 *
 * The identity names the incident and the title names the alert, so both are
 * required:  the title may come from the rule, which is where Sentry puts it
 * when the alert itself carries no title.  The rule is required, because the
 * name, the status and the link the parser reads come from it.  Every other
 * consumed field is checked for the type the parser reads it as.
 */
function validateSentryMetric(metric) {
  const rule = metric.alert_rule;
  if (!rule || typeof rule !== "object" || Array.isArray(rule)) {
    throw new Error("invalid payload");
  }
  for (const field of ["id", "name", "status", "url"]) {
    const value = rule[field];
    if (value === undefined || value === null) continue;
    if (sentryText(value) === null) throw new Error("invalid text field");
  }
  if (!sentryFirstText(metric, ["id"]) && !sentryFirstText(rule, ["id"])) {
    throw new Error("missing identity");
  }
  for (const field of ["title", "status", "url"]) {
    const value = metric[field];
    if (value === undefined || value === null) continue;
    if (sentryText(value) === null) throw new Error("invalid text field");
  }
  const identifier = metric.metric_identifier;
  if (identifier !== undefined && identifier !== null) {
    if (typeof identifier !== "object" || Array.isArray(identifier)) {
      throw new Error("invalid payload");
    }
    for (const field of ["name"]) {
      const value = identifier[field];
      if (value === undefined || value === null) continue;
      if (sentryText(value) === null) throw new Error("invalid text field");
    }
  }
  if (!sentryFirstText(metric, ["title"]) && !sentryFirstText(rule, ["name"])) {
    throw new Error("missing title");
  }
}

function validateSentryPayload(payload) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("invalid payload");
  }
  if (Object.keys(payload).length > 100) throw new Error("too many fields");
  if (payload.action !== undefined && payload.action !== null) {
    if (typeof payload.action !== "string") throw new Error("invalid text field");
    if (payload.action.length > 4096) throw new Error("invalid text");
  }
  const data = payload.data;
  if (!data || typeof data !== "object" || Array.isArray(data)) {
    throw new Error("invalid payload");
  }
  if (Object.keys(data).length > 20) throw new Error("too many fields");
  const issue = data.issue;
  const metric = data.metric_alert;
  // Sentry sends exactly one of the two shapes.  A body carrying both, or
  // neither, is not a Sentry alert and is refused.  A null sibling counts as
  // absent, because null is how a JSON encoder writes a field that is not
  // there, so a body carrying one shape and an explicit null sibling is the
  // one shape and not a contradiction.
  const hasIssue = issue !== undefined && issue !== null;
  const hasMetric = metric !== undefined && metric !== null;
  if (hasIssue === hasMetric) {
    throw new Error("invalid payload");
  }
  const body = hasIssue ? issue : metric;
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    throw new Error("invalid payload");
  }
  if (hasIssue) validateSentryIssue(body);
  else validateSentryMetric(body);
  walkBounded(payload);
}

/**
 * Bound a native vendor body:  depth, breadth, node count and text length, with
 * control characters refused.  Sentry and PagerDuty both send nested native
 * shapes that the generic rule cannot accept, and both get this same bound.
 */
function walkBounded(payload) {
  let nodes = 0;
  const walk = (value, depth) => {
    if (++nodes > MAX_SENTRY_NODES) throw new Error("payload too large");
    if (depth > MAX_PAYLOAD_DEPTH) throw new Error("payload nested too deep");
    if (value === null || value === undefined) return;
    if (typeof value === "string") {
      if (value.length > 4096) throw new Error("invalid text");
      if (/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(value)) {
        throw new Error("invalid text");
      }
      return;
    }
    if (typeof value === "number" || typeof value === "boolean") return;
    if (Array.isArray(value)) {
      if (value.length > 100) throw new Error("too many fields");
      for (const item of value) walk(item, depth + 1);
      return;
    }
    if (typeof value === "object") {
      const entries = Object.entries(value);
      if (entries.length > 100) throw new Error("too many fields");
      for (const [, item] of entries) walk(item, depth + 1);
      return;
    }
    throw new Error("invalid text field");
  };
  walk(payload, 0);
}

/** True when a PagerDuty v3 delivery is about an incident, whose `data` is the incident. */
function pagerDutyIncidentEvent(payload) {
  const event = payload?.event;
  return (
    event?.resource_type === "incident" &&
    typeof event.event_type === "string" &&
    event.event_type.startsWith("incident.") &&
    event.data?.type === "incident"
  );
}

/**
 * Validate a PagerDuty v3 webhook body.
 *
 * Every delivery is `{ event: { event_type, resource_type, data } }`.  The
 * envelope is required for every delivery, a ping included.  An incident
 * event must also carry the incident's identity, and every incident field the
 * parser reads is checked for the type it reads it as, the same rule as
 * Sentry:  a body with no identity would otherwise file under a constant key.
 */
function validatePagerDutyPayload(payload) {
  const object = (value) =>
    value !== null && typeof value === "object" && !Array.isArray(value);
  if (!object(payload)) throw new Error("invalid payload");
  if (Object.keys(payload).length > 20) throw new Error("too many fields");
  const event = payload.event;
  if (!object(event)) throw new Error("invalid payload");
  if (typeof event.event_type !== "string" || !event.event_type.trim()) {
    throw new Error("missing event type");
  }
  if (!object(event.data)) throw new Error("invalid payload");
  if (pagerDutyIncidentEvent(payload)) {
    const data = event.data;
    if (!sentryFirstText(data, ["id"])) throw new Error("missing identity");
    for (const field of ["id", "status", "title", "number", "html_url", "urgency"]) {
      const value = data[field];
      if (value === undefined || value === null) continue;
      if (sentryText(value) === null) throw new Error("invalid text field");
    }
    for (const field of ["service", "priority"]) {
      const value = data[field];
      if (value === undefined || value === null) continue;
      if (!object(value)) throw new Error("invalid payload");
      for (const name of ["summary", "name"]) {
        const text = value[name];
        if (text === undefined || text === null) continue;
        if (sentryText(text) === null) throw new Error("invalid text field");
      }
    }
  }
  walkBounded(payload);
}

// One named object owns every read/render/write transaction.  Durable storage
// overlays legacy KV without deleting historical records or trusting KV locks.
const LEGACY_IMPORTED = "legacy:imported",
  LEGACY_RETRY_AT = "legacy:retryAt";
export class AlertWriter {
  constructor(ctx, env) {
    this.ctx = ctx;
    this.env = env;
    this.tail = Promise.resolve();
    this.breaker = new DeliveryBreaker(ctx.storage);
    this.statusBody = null;
  }
  fetch(request) {
    if (guardOff(this.env)) return guardOffResponse();
    const task = this.tail.then(() => this.dispatch(request));
    this.tail = task.catch(() => {});
    return task;
  }
  // The legacy KV is frozen (nothing writes it once the writer owns the
  // records), so it is copied into durable storage once and never read again.
  // Every KV list counts against the account's free-plan cap of 1,000 a day,
  // and listing it on each sweep and webhook spent about 3,300 a day.  Until a
  // copy succeeds the overlay keeps reading through to KV, retried at most
  // every ten minutes;  the retry time is stored, so an evicted writer waits
  // too.  The read-through itself is not skipped, because an inventory
  // without the frozen records could post a second standing block.
  async legacyImported() {
    const storage = this.ctx.storage,
      legacy = this.env.ALERT_STATE;
    if (this.imported) return true;
    try {
      if (!legacy || (await storage.get(LEGACY_IMPORTED)) !== undefined)
        return (this.imported = true);
      this.importRetryAt ??= (await storage.get(LEGACY_RETRY_AT)) || 0;
      if (Date.now() < this.importRetryAt) return false;
      for (const { name } of await listAll(legacy, "")) {
        if ((await storage.get(`kv:${name}`)) !== undefined) continue;
        const value = await legacy.get(name);
        if (value !== null) await storage.put(`kv:${name}`, value);
      }
      await storage.put(LEGACY_IMPORTED, Date.now());
      return (this.imported = true);
    } catch {
      console.error(JSON.stringify({ event: "alert.legacy_import_failed" }));
      this.importRetryAt = Date.now() + 10 * 60 * 1000;
      try {
        await storage.put(LEGACY_RETRY_AT, this.importRetryAt);
      } catch {}
      return false;
    }
  }
  async dispatch(request) {
    // The writer is one object, so this bounds a full /status read to one per
    // window however many isolates ask.  Anything else may change what it says.
    const isStatus = new URL(request.url).pathname === "/status";
    if (!isStatus) this.statusBody = null;
    const storage = this.ctx.storage,
      legacy = this.env.ALERT_STATE,
      imported = await this.legacyImported();
    if (isStatus && this.statusBody && this.statusBody.expires > Date.now()) {
      return new Response(this.statusBody.text, { headers: { "Content-Type": "application/json" } });
    }
    const overlay = {
      async get(key, type) {
        let value = await storage.get(`kv:${key}`);
        if (value === undefined && !imported) value = await legacy.get(key);
        return value == null
          ? null
          : type === "json"
            ? JSON.parse(value)
            : value;
      },
      async put(key, value) {
        await storage.put(`kv:${key}`, value);
      },
      // Only the writer's own records are deleted.  The one caller prunes the
      // records of a closed per-incident alert, and every such record was
      // written through this overlay, after the legacy KV was frozen, so there
      // is no legacy copy to show through.
      async delete(key) {
        await storage.delete(`kv:${key}`);
      },
      async list({ prefix }) {
        const previous = imported ? [] : await listAll(legacy, prefix);
        const local = await storage.list({
          prefix: `kv:${prefix}`,
          limit: MAX_KEYS + 1,
        });
        const names = new Set(previous.map((k) => k.name));
        for (const key of local.keys()) names.add(key.slice(3));
        if (names.size > MAX_KEYS) throw inventoryBoundError();
        return {
          keys: [...names].map((name) => ({ name })),
          list_complete: true,
        };
      },
    };
    try {
      const response = await handler.fetch(
        request,
        { ...this.env, ALERT_STATE: overlay, WRITER_STORAGE: storage, ALERT_BREAKER: this.breaker },
        {},
      );
      if (isStatus && response.status === 200) {
        const text = await response.text();
        this.statusBody = { text, expires: Date.now() + STATUS_MEMO_MS };
        return new Response(text, { headers: { "Content-Type": "application/json" } });
      }
      return response;
    } catch (error) {
      console.error(
        JSON.stringify({
          event: "alert.writer_failed",
          path: new URL(request.url).pathname,
          code: error?.code ?? null,
        }),
      );
      return json({ error: "writer unavailable", stale: true }, 503);
    }
  }
}
// The one name the writer is reached by.  The webhook path and the cron sweep
// must resolve to the same object, because the delivery barrier lives in that
// object's storage:  two names would be two writers, each blind to the other's
// barrier, and both could post a standing message for one topic.  So the name
// is written once, here, and both call sites read it.
const WRITER_OBJECT_NAME = "alerts-v1";
const writerFor = (env) =>
  env.ALERT_WRITER.get(env.ALERT_WRITER.idFromName(WRITER_OBJECT_NAME));

/** One line for a writer that could not be reached.  Nothing from the error's text. */
function logWriterUnreachable(where, error) {
  console.error(
    JSON.stringify({
      event: "alert.writer_unreachable",
      where,
      name: error?.name ?? null,
      capped: /Exceeded allowed volume/.test(String(error?.message)),
    }),
  );
}

let statusMemo = null;
/**
 * GET /status from this isolate's memory, so a burst of readers wakes the writer
 * at most once per 30 seconds per isolate, and the writer's own copy bounds the
 * full read.  Memory rather than the Cache API, because the Cache API is only
 * documented as working on custom domains and this Worker runs on workers.dev.
 * The query string is not part of the key, so it cannot be used to miss.  Still
 * unauthenticated:  Usage Monitor's /ops reads it with no token, so a bearer has
 * to land there first.  A HEAD reads the same copy, and reaches the writer as a
 * GET, so a bodyless reply can never be kept.
 */
async function cachedStatus(incoming, env) {
  let served = "hit";
  if (!statusMemo || statusMemo.expires <= Date.now()) {
    const response = await writerFor(env).fetch(new Request(new URL("/status", incoming.url).toString()));
    if (response.status !== 200) return response;
    statusMemo = { text: await response.text(), expires: Date.now() + STATUS_MEMO_MS };
    served = "miss";
  }
  return new Response(incoming.method === "HEAD" ? null : statusMemo.text, {
    headers: {
      "Content-Type": "application/json",
      "cache-control": `public, max-age=${STATUS_MEMO_MS / 1000}`,
      "x-status-cache": served,
    },
  });
}

const EMPTY_BODY_REPORT_MS = 24 * 60 * 60 * 1000;
const emptyBodyReported = new Map();
/**
 * Whether this empty delivery should reach the writer, which reports it once a
 * day.  Only a sender that proved itself may wake the writer this way, and at
 * most once a day per isolate, so nearly every empty delivery is answered here
 * without waking it.  The writer's durable throttle bounds the notice itself.
 */
async function emptyBodyReportDue(request, env, source) {
  if ((emptyBodyReported.get(source) || 0) > Date.now()) return false;
  if (!(await verify(request, "", env, source))) return false;
  emptyBodyReported.set(source, Date.now() + EMPTY_BODY_REPORT_MS);
  return true;
}

export default {
  async fetch(request, env, ctx) {
    if (guardOff(env)) return guardOffResponse();
    const path = new URL(request.url).pathname;
    if (path === "/health") return handler.fetch(request, env, {});
    const hook = path.match(/^\/webhook\/([a-z0-9_-]{1,32})$/i);
    const operator = path === "/sweep" || /^\/status\/[^/]+\/[^/]+(?:\/[^/]+)?$/.test(path);
    if (!hook && !operator && path !== "/status") return json({ error: "not found" }, 404);
    if (hook) {
      if (request.method !== "POST") return json({ error: "POST only" }, 405);
      const source = hook[1].toLowerCase();
      // Static-token sources and the generic bearer can be rejected without
      // waking the writer. Signed sources still verify the body inside it.
      if ((source === "betterstack" || !["datadog", "sentry", "pagerduty"].includes(source)) &&
          !(await verify(request, "", env, source))) return json({ error: "bad signature" }, 401);
      if (source === "datadog" &&
          !(env.DATADOG_WEBHOOK_SECRET && safeEqual(request.headers.get("x-datadog-token") || "", env.DATADOG_WEBHOOK_SECRET)) &&
          !/^[a-f0-9]{64}$/.test(request.headers.get("x-datadog-signature") || ""))
        return json({ error: "bad signature" }, 401);
      if (source === "sentry" && !/^[a-f0-9]{64}$/.test(request.headers.get("sentry-hook-signature") || ""))
        return json({ error: "bad signature" }, 401);
      if (source === "pagerduty" &&
          !(env.PAGERDUTY_WEBHOOK_SECRET && safeEqual(request.headers.get("x-pagerduty-token") || "", env.PAGERDUTY_WEBHOOK_SECRET)) &&
          !(request.headers.get("x-pagerduty-signature") || "").split(",").some(part => /^v1=[a-f0-9]{64}$/.test(part.trim())))
        return json({ error: "bad signature" }, 401);
      const length = request.headers.get("content-length");
      if (length && /^\d+$/.test(length) && Number(length) > MAX_BODY_BYTES)
        return json({ error: "payload too large" }, 413);
    }
    if (operator && !authorized(request, env)) return json({ error: "unauthorized" }, 401);
    if (!env.ALERT_WRITER)
      return json({ error: "durable writer unavailable", stale: true }, 503);
    // An empty body cannot be an alert, so it is refused without waking the writer.
    if (
      hook &&
      (request.headers.get("content-length") === "0" || request.body === null) &&
      !(await emptyBodyReportDue(request, env, hook[1].toLowerCase()))
    ) {
      return json({ error: "empty body" }, 400);
    }
    try {
      if (path === "/status") {
        // Any other method would walk around the cache and wake the writer,
        // with no credential, on every request.
        if (request.method !== "GET" && request.method !== "HEAD") {
          return json({ error: "GET only" }, 405);
        }
        return await cachedStatus(request, env);
      }
      return await writerFor(env).fetch(request);
    } catch (error) {
      // Still a 5xx:  the vendor's retry is the only redelivery, and nothing was
      // recorded here, so answering 200 would lose the event for good.
      logWriterUnreachable(hook ? hook[1].toLowerCase() : path, error);
      return new Response(
        JSON.stringify({ error: "writer unavailable", stale: true }),
        {
          status: 503,
          headers: { "Content-Type": "application/json", "Retry-After": "60" },
        },
      );
    }
  },
  async scheduled(event, env, ctx) {
    if (guardOff(env)) return;
    if (!env.ALERT_WRITER || !env.INGRESS_SHARED_TOKEN) return;
    ctx.waitUntil(
      (async () => {
        try {
          const response = await writerFor(env).fetch(
            new Request("https://internal/sweep", {
              headers: { authorization: `Bearer ${env.INGRESS_SHARED_TOKEN}` },
            }),
          );
          if (!response.ok) {
            console.error(
              JSON.stringify({ event: "alert.sweep_failed", status: response.status }),
            );
          }
          await response.body?.cancel?.();
        } catch (error) {
          logWriterUnreachable("sweep", error);
        }
      })(),
    );
  },
};
Object.assign(__testing, {
  listAll,
  mapBounded,
  validatePayload,
  validatePagerDutyPayload,
  verify,
  handler,
  handleEvent,
  sendEventOnce,
  transitionIdentity,
  advanceRevision,
  resetStatusMemo() {
    statusMemo = null;
  },
});
