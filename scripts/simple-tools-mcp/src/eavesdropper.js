// The server eavesdropper's records for a hosted seat (AFC-330 part B):  eavesdropper_status,
// eavesdropper_inbox, eavesdropper_owner_queue, eavesdropper_wakes and eavesdropper_held_back, plus
// operator_inbox_pending (OPS-1047):  the seat's wake-worthy pending seat-inbox items after a
// caller-held seq, for hosted seats with no wake adapter.  The specs are
// scripts/simple_tools/mcp/eavesdropper-tools.json (shared with the stdio server);  the hosted server
// lists every one of them except eavesdropper_ack, because the eavesdropper's read API is read-only.
//
// The Worker reads them from the server eavesdropper's signed GET API
// (scripts/simple_tools/listener_api.py):  GET <LISTENER_API_URL>/listener/v1/<SEAT>/<record>?<query>
// with X-Simple-Tools-Timestamp and X-Simple-Tools-Signature, the lowercase hex HMAC-SHA256 of
// "simple-tools-read/1\nGET\n<path>\n<query>\n<timestamp>" keyed with the seat's wake key
// (WAKE_HMAC_KEY_<SEAT>, the same secret the eavesdropper signs that seat's wakes with).  The seat
// comes only from the grant, and only listenerSecretName turns it into a key, so a grant reads its own
// seat's records and never another's.  With LISTENER_API_URL unset (or not https), or no wake key
// for the seat, the tools answer not_configured and everything else works as before.
//
// Pure module:  WebCrypto and fetch only, so `node --test` loads it.

import EAVESDROPPER_CONTRACT from "../../simple_tools/mcp/eavesdropper-tools.json" with { type: "json" };
import { z } from "zod";
import { ToolError, clean, envelope, okResult, BODY_LIMIT, NAME_LIMIT } from "./contract.js";
import { LISTENER_READ_SECRETS, WAKE_SECRETS } from "./config.js";

/** The secret name that signs this seat's listener reads, or null:  its wake key when it has a
 * wake route, else its read-only key from LISTENER_READ_SECRETS (a seat there, like MA, gains
 * reads and no wake route:  the wake and spike routes consult WAKE_SECRETS only). */
export function listenerSecretName(seat) {
  if (Object.hasOwn(LISTENER_READ_SECRETS, seat)) return LISTENER_READ_SECRETS[seat];
  return Object.hasOwn(WAKE_SECRETS, seat) ? WAKE_SECRETS[seat] : null;
}

export const LISTENER_CONTRACT_NAME = "simple-tools-read/1";

// The read API's answer, parsed before anything reads it.  Every view field
// is optional and lenient (catch):  a missing or odd field becomes 0, false,
// null or empty on its own, and never hides the rest of the view.
const count0 = z.number().int().nonnegative().catch(0);
const flag = z.boolean().catch(false);
export const ListenerView = z.looseObject({
  items: z.array(z.unknown()).max(1000).catch([]),
  pending: count0, cursor: count0, total: count0, count: count0, lost: count0,
  next_seq: z.number().int().nonnegative().nullable().catch(null),
  after_seq: count0, latest_seq: z.number().int().nonnegative().nullable().catch(null),
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
export const LISTENER_USER_AGENT = "simple-tools-mcp (hosted listener reader)";
export const LISTENER_RECORDS = Object.freeze({
  eavesdropper_status: "status",
  eavesdropper_inbox: "inbox",
  operator_inbox_pending: "pending-inbox",
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

/** The string the eavesdropper verifies (listener_api.py signing_string). */
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
        headers: { "X-Simple-Tools-Timestamp": timestamp, "X-Simple-Tools-Signature": signature, "User-Agent": LISTENER_USER_AGENT, Accept: "application/json" },
      });
    } catch (error) {
      const timeout = error?.name === "TimeoutError" || error?.name === "AbortError";
      throw new ToolError("unavailable", `could not reach the eavesdropper${timeout ? " (timed out)" : ""}`, { retryable: true });
    }
    if (response.status >= 300 && response.status < 400) throw new ToolError("not_authorized", "the eavesdropper answered with a redirect, which this client never follows");
    if (response.status === 401 || response.status === 403) throw new ToolError("not_authorized", "the eavesdropper refused this seat's signature (the wake key differs, or the clocks are more than five minutes apart)");
    if (response.status === 404) throw new ToolError("not_configured", "the eavesdropper has no read API for this seat (no key for it there, or the API is off)");
    if (response.status === 400) throw new ToolError("invalid_argument", "the eavesdropper refused the parameters");
    if (response.status === 409) throw new ToolError("unavailable", "the eavesdropper cannot read this seat's records right now (check their file modes)");
    if (response.status >= 500) throw new ToolError("unavailable", `the eavesdropper answered HTTP ${response.status}`, { retryable: true, extra: { status: response.status } });
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    const parsed = response.ok ? ListenerEnvelope.safeParse(body) : null;
    if (!parsed?.success || parsed.data.seat !== this.seat || parsed.data.record !== record) {
      throw new ToolError("upstream_error", "the eavesdropper's answer was not a read-API view");
    }
    return ListenerView.parse(parsed.data.view);
  }
}

/** A ListenerClient for `seat`, or null when the read API is not configured for it.  Never throws. */
export function listenerClientFromEnv(env, seat, { fetch: fetchImpl = globalThis.fetch?.bind(globalThis), now = Date.now, wakeKey } = {}) {
  const baseUrl = listenerBaseUrl(env?.LISTENER_API_URL);
  const name = listenerSecretName(seat);
  // wakeKey: an already-resolved key (src/infisical.js resolveSecret), so the
  // eavesdropper honors required mode; without it the legacy env binding is read.
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
      "The eavesdropper's records are not reachable from this server for this seat.  The owner sets LISTENER_API_URL and the seat's WAKE_HMAC_KEY_<SEAT>, and enables [listener_api] on the server eavesdropper (scripts/simple-tools-mcp/DEPLOY.md).  The Zulip tools still work.",
    );
  },

  async listenerView(tool, args) {
    const client = this.requireListener();
    await this.reserve("read");
    const params = {
      eavesdropper_status: {},
      eavesdropper_inbox: { since_seq: args.since_seq, limit: args.limit },
      // check is a count-only read:  limit 0 makes the listener count without returning bodies.
      operator_inbox_pending: { after_seq: args.after_seq ?? 0, limit: args.mode === "pull" ? (args.limit ?? 20) : 0 },
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

  async tool_operator_inbox_pending(args, audit) {
    // No seat argument exists:  this.seat comes from the grant, and listenerClientFromEnv turned
    // exactly that seat into its signing key, so the read can only ever be the caller's own inbox.
    const mode = args.mode === "pull" ? "pull" : "check";
    const view = await this.listenerView("operator_inbox_pending", { ...args, mode });
    const latestSeq = isInt(view.latest_seq) ? view.latest_seq : null;
    const structured = { mode, count: count(view.count), latest_seq: latestSeq, after_seq: count(view.after_seq) };
    if (mode === "check") {
      return okResult(JSON.stringify({ seat: this.seat, ...structured }), structured);
    }
    const raw = Array.isArray(view.items) ? view.items : [];
    const items = raw.map((item) => {
      const out = listenerItem(item);
      // listenerItem keeps integer lists only;  the router's stored classes are strings, so they
      // are re-admitted here under the same shape rule, with the row's wake verdict beside them.
      const classes = item && typeof item === "object" && Array.isArray(item.classes) ? item.classes.filter((c) => typeof c === "string" && /^[a-z_]{1,32}$/.test(c)) : [];
      out.classes = classes;
      out.wake_worthy = item?.wake_worthy === true;
      out.wake_owner = item?.wake_owner === true;
      return out;
    });
    structured.seqs = items.map((i) => i.seq).filter(isInt);
    structured.ids = items.map((i) => i.id).filter(isInt);
    const summary = `${structured.count} pending item${structured.count === 1 ? "" : "s"} after seq ${structured.after_seq}, latest_seq ${latestSeq ?? "None"}`;
    return okResult(envelope("operator_inbox_pending", [], summary, items), structured);
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
