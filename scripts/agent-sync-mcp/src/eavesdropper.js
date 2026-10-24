// The server listener's records for a hosted seat (AFC-330 part B):  eavesdropper_status,
// eavesdropper_inbox, eavesdropper_owner_queue, eavesdropper_wakes and eavesdropper_held_back.  The specs are
// scripts/agent_sync/mcp/eavesdropper-tools.json (shared with the stdio server);  the hosted server
// lists every one of them except eavesdropper_ack, because the listener's read API is read-only.
//
// The Worker reads them from the server listener's signed GET API
// (scripts/agent_sync/listener_api.py):  GET <LISTENER_API_URL>/listener/v1/<SEAT>/<record>?<query>
// with X-Agent-Sync-Timestamp and X-Agent-Sync-Signature, the lowercase hex HMAC-SHA256 of
// "agent-sync-read/1\nGET\n<path>\n<query>\n<timestamp>" keyed with the seat's wake key
// (WAKE_HMAC_KEY_<SEAT>, the same secret the listener signs that seat's wakes with).  The seat
// comes only from the grant, and only WAKE_SECRETS turns it into a key, so a grant reads its own
// seat's records and never another's.  With LISTENER_API_URL unset (or not https), or no wake key
// for the seat, the tools answer not_configured and everything else works as before.
//
// Pure module:  WebCrypto and fetch only, so `node --test` loads it.

import EAVESDROPPER_CONTRACT from "../../agent_sync/mcp/eavesdropper-tools.json" with { type: "json" };
import { z } from "zod";
import { ToolError, clean, envelope, okResult, BODY_LIMIT, NAME_LIMIT } from "./contract.js";
import { WAKE_SECRETS } from "./config.js";

export const LISTENER_CONTRACT_NAME = "agent-sync-read/1";

// The read API's answer, parsed before anything reads it.  Every view field
// is optional and lenient (catch):  a missing or odd field becomes 0, false,
// null or empty on its own, and never hides the rest of the view.
const count0 = z.number().int().nonnegative().catch(0);
const flag = z.boolean().catch(false);
export const ListenerView = z.looseObject({
  items: z.array(z.unknown()).max(1000).catch([]),
  pending: count0, cursor: count0, total: count0, count: count0, lost: count0,
  next_seq: z.number().int().nonnegative().nullable().catch(null),
  inbox_pending: count0, owner_queue_pending: count0, held_back: count0, wakes_24h: count0, leases: count0,
  inbox_cursor: count0, held_back_lost: count0, nudges_pending: count0,
  paused: flag, paused_wakes: flag,
  wakes_by_state: z.record(z.string(), z.unknown()).catch({}),
});
const ListenerEnvelope = z.looseObject({
  contract: z.literal(LISTENER_CONTRACT_NAME),
  seat: z.string(),
  record: z.string(),
  view: z.looseObject({}),
});
export const LISTENER_TIMEOUT_MS = 10_000;
export const LISTENER_MIN_KEY_LEN = 32;
export const LISTENER_USER_AGENT = "agent-sync-mcp (hosted listener reader)";
export const LISTENER_RECORDS = Object.freeze({
  eavesdropper_status: "status",
  eavesdropper_inbox: "inbox",
  eavesdropper_owner_queue: "owner-queue",
  eavesdropper_wakes: "wakes",
  eavesdropper_held_back: "held-back",
});
export const EAVESDROPPER_TOOLS = Object.freeze(EAVESDROPPER_CONTRACT.tools.filter((tool) => Object.hasOwn(LISTENER_RECORDS, tool.name)));
export const EAVESDROPPER_READ_TOOLS = Object.freeze(new Set(EAVESDROPPER_TOOLS.map((tool) => tool.name)));

const isInt = (v) => typeof v === "number" && Number.isSafeInteger(v);
const ints = (list) => (Array.isArray(list) ? list.filter(isInt) : []);

/** The base URL when it is https with no path, credentials, query or fragment, else null. */
export function listenerBaseUrl(raw) {
  if (typeof raw !== "string" || !raw.trim()) return null;
  let url;
  try {
    url = new URL(raw.trim());
  } catch {
    return null;
  }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash) return null;
  if (url.pathname !== "/" && url.pathname !== "") return null;
  return url.origin;
}

async function hmacHex(key, text) {
  const cryptoKey = await crypto.subtle.importKey("raw", new TextEncoder().encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const signature = await crypto.subtle.sign("HMAC", cryptoKey, new TextEncoder().encode(text));
  return [...new Uint8Array(signature)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** The string the listener verifies (listener_api.py signing_string). */
export function listenerSigningString(path, query, timestamp) {
  return `${LISTENER_CONTRACT_NAME}\nGET\n${path}\n${query}\n${timestamp}`;
}

export class ListenerClient {
  constructor({ baseUrl, seat, key, fetch: fetchImpl, now = Date.now, timeoutMs = LISTENER_TIMEOUT_MS }) {
    this.baseUrl = baseUrl;
    this.seat = seat;
    this.fetch = fetchImpl;
    this.now = now;
    this.timeoutMs = timeoutMs;
    this.#key = key;
  }

  #key;

  /** The loaded key, for the caller's scrub list. */
  secrets() {
    return [this.#key];
  }

  /** One record's view.  Throws ToolError;  never echoes a response body. */
  async read(record, params = {}) {
    const path = `/listener/v1/${this.seat}/${record}`;
    const query = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== null).map(([k, v]) => [k, String(v)])).toString();
    const timestamp = String(Math.floor(this.now() / 1000));
    const signature = await hmacHex(this.#key, listenerSigningString(path, query, timestamp));
    let response;
    try {
      response = await this.fetch(`${this.baseUrl}${path}${query ? `?${query}` : ""}`, {
        method: "GET",
        redirect: "manual",
        signal: AbortSignal.timeout(this.timeoutMs),
        headers: { "X-Agent-Sync-Timestamp": timestamp, "X-Agent-Sync-Signature": signature, "User-Agent": LISTENER_USER_AGENT, Accept: "application/json" },
      });
    } catch (error) {
      const timeout = error?.name === "TimeoutError" || error?.name === "AbortError";
      throw new ToolError("unavailable", `could not reach the listener${timeout ? " (timed out)" : ""}`, { retryable: true });
    }
    if (response.status >= 300 && response.status < 400) throw new ToolError("not_authorized", "the listener answered with a redirect, which this client never follows");
    if (response.status === 401 || response.status === 403) throw new ToolError("not_authorized", "the listener refused this seat's signature (the wake key differs, or the clocks are more than five minutes apart)");
    if (response.status === 404) throw new ToolError("not_configured", "the listener has no read API for this seat (no key for it there, or the API is off)");
    if (response.status === 400) throw new ToolError("invalid_argument", "the listener refused the parameters");
    if (response.status === 409) throw new ToolError("unavailable", "the listener cannot read this seat's records right now (check their file modes)");
    if (response.status >= 500) throw new ToolError("unavailable", `the listener answered HTTP ${response.status}`, { retryable: true, extra: { status: response.status } });
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const parsed = response.ok ? ListenerEnvelope.safeParse(body) : null;
    if (!parsed?.success || parsed.data.seat !== this.seat || parsed.data.record !== record) {
      throw new ToolError("upstream_error", "the listener's answer was not a read-API view");
    }
    return ListenerView.parse(parsed.data.view);
  }
}

/** A ListenerClient for `seat`, or null when the read API is not configured for it.  Never throws. */
export function listenerClientFromEnv(env, seat, { fetch: fetchImpl = globalThis.fetch?.bind(globalThis), now = Date.now, wakeKey } = {}) {
  const baseUrl = listenerBaseUrl(env?.LISTENER_API_URL);
  const name = Object.hasOwn(WAKE_SECRETS, seat) ? WAKE_SECRETS[seat] : null;
  // wakeKey: an already-resolved key (src/infisical.js resolveSecret), so the
  // listener honors required mode; without it the legacy env binding is read.
  const key = wakeKey !== undefined ? String(wakeKey).trim() : name && typeof env?.[name] === "string" ? env[name].trim() : "";
  if (!baseUrl || key.length < LISTENER_MIN_KEY_LEN || typeof fetchImpl !== "function") return null;
  return new ListenerClient({ baseUrl, seat, key, fetch: fetchImpl, now });
}

/** One view item made safe for the fence:  strings cleaned, integer lists kept, nested objects dropped. */
export function listenerItem(item) {
  const out = {};
  for (const [key, value] of Object.entries(item && typeof item === "object" ? item : {})) {
    if (!/^[a-z_]{1,32}$/.test(key)) continue;
    if (typeof value === "string") out[key] = clean(value, key === "body" ? BODY_LIMIT : NAME_LIMIT)[0];
    else if (typeof value === "number" || typeof value === "boolean" || value === null) out[key] = value;
    else if (Array.isArray(value)) out[key] = ints(value);
  }
  return out;
}

const count = (value) => (isInt(value) && value >= 0 ? value : 0);

export const EAVESDROPPER_METHODS = {
  requireListener() {
    if (this.listener) return this.listener;
    throw new ToolError(
      "not_configured",
      "The listener's records are not reachable from this server for this seat.  The owner sets LISTENER_API_URL and the seat's WAKE_HMAC_KEY_<SEAT>, and enables [listener_api] on the server listener (scripts/agent-sync-mcp/DEPLOY.md).  The Zulip tools still work.",
    );
  },

  async listenerView(tool, args) {
    const client = this.requireListener();
    await this.reserve("read");
    const params = {
      eavesdropper_status: {},
      eavesdropper_inbox: { since_seq: args.since_seq, limit: args.limit },
      eavesdropper_owner_queue: { all: args.all ? "true" : undefined, limit: args.limit },
      eavesdropper_wakes: { since_hours: args.since_hours, state: args.state, limit: args.limit },
      eavesdropper_held_back: { limit: args.limit },
    }[tool];
    return client.read(LISTENER_RECORDS[tool], params);
  },

  async tool_eavesdropper_status(args, audit) {
    const view = await this.listenerView("eavesdropper_status", args);
    const structured = {
      inbox_pending: count(view.inbox_pending),
      owner_queue_pending: count(view.owner_queue_pending),
      held_back: count(view.held_back),
      wakes_24h: count(view.wakes_24h),
      leases: count(view.leases),
      paused: view.paused === true,
    };
    const byState = Object.fromEntries(Object.entries(view.wakes_by_state ?? {}).filter(([k, v]) => /^[a-z_]{1,32}$/.test(k) && isInt(v)));
    const text = { seat: this.seat, ...structured, inbox_cursor: count(view.inbox_cursor), held_back_lost: count(view.held_back_lost), wakes_by_state: byState, nudges_pending: count(view.nudges_pending), paused_wakes: view.paused_wakes === true };
    return okResult(JSON.stringify(text), structured);
  },

  async tool_eavesdropper_inbox(args, audit) {
    const view = await this.listenerView("eavesdropper_inbox", args);
    const items = (Array.isArray(view.items) ? view.items : []).map(listenerItem);
    const nextSeq = isInt(view.next_seq) ? view.next_seq : null;
    const summary = `${items.length} item${items.length === 1 ? "" : "s"}, ${count(view.pending)} pending after cursor ${count(view.cursor)}, next_seq ${nextSeq ?? "None"}`;
    return okResult(envelope("eavesdropper_inbox", [], summary, items), {
      count: items.length,
      ids: items.map((i) => i.id).filter(isInt),
      seqs: items.map((i) => i.seq).filter(isInt),
      next_seq: nextSeq,
    });
  },

  async tool_eavesdropper_owner_queue(args, audit) {
    const view = await this.listenerView("eavesdropper_owner_queue", args);
    const items = (Array.isArray(view.items) ? view.items : []).map(listenerItem);
    return okResult(envelope("eavesdropper_owner_queue", [], `${items.length} note${items.length === 1 ? "" : "s"}, ${count(view.pending)} not surfaced`, items), {
      count: items.length,
      seqs: items.map((i) => i.seq).filter(isInt),
    });
  },

  async tool_eavesdropper_wakes(args, audit) {
    const view = await this.listenerView("eavesdropper_wakes", args);
    const items = (Array.isArray(view.items) ? view.items : []).map(listenerItem);
    return okResult(envelope("eavesdropper_wakes", [], `${items.length} wake${items.length === 1 ? "" : "s"} of ${count(view.total)}`, items), {
      count: items.length,
      ids: items.flatMap((i) => ints(i.trigger_ids)),
    });
  },

  async tool_eavesdropper_held_back(args, audit) {
    const view = await this.listenerView("eavesdropper_held_back", args);
    const items = (Array.isArray(view.items) ? view.items : []).map(listenerItem);
    return okResult(envelope("eavesdropper_held_back", [], `${count(view.count)} held back, ${count(view.lost)} lost`, items), {
      count: items.length,
      ids: items.map((i) => i.message_id).filter(isInt),
      lost: count(view.lost),
    });
  },
};
