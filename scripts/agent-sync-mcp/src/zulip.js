// Zulip egress for the hosted seats (spec 3.8).  Only the compiled realm is
// ever called, under /api/v1/, with `redirect: "manual"` (any 3xx is an error)
// and a 15-second timeout.  The Basic header is built per request from the
// seat's key and never logged.  On 429 the wait comes from the Retry-After
// header or the JSON body's retry-after, within a 20-second budget per call and
// at most 2 attempts (spec 3.7);  past that the caller gets rate_limited.
//
// Pure module:  the caller hands in `fetch` and `sleep`, so node tests run it
// against a fake Zulip.

import { ApiError, DmGuardRefused, MAX_CONTENT_LENGTH, NetworkError } from "./contract.js";
import { fixOutbound } from "./textfmt.js";

export const REALM = "https://simplewithus.zulipchat.com";
export const TIMEOUT_MS = 15_000;
export const RATE_LIMIT_BUDGET_MS = 20_000;
export const MAX_ATTEMPTS = 2;

// Paths whose `content` is message text this process sends (zulip.py outbound_params).
const OUTBOUND_TEXT_PATH = /^(?:messages|scheduled_messages)(?:\/\d+)?$/;
const PATH_RE = /^[A-Za-z0-9_/-]{1,80}$/;

// The only endpoints the hosted tools call, per method (AFC-330).  Anything else is refused before
// any request, so no tool (and no bug in one) can reach an admin endpoint with a seat's key.
export const ALLOWED_PATHS = Object.freeze({
  GET: [/^users\/me$/, /^users$/, /^users\/me\/subscriptions$/, /^users\/me\/\d+\/topics$/, /^messages$/, /^messages\/\d+$/,
    /^messages\/\d+\/history$/, /^streams$/, /^streams\/\d+$/, /^streams\/\d+\/members$/, /^users\/\d+\/presence$/,
    /^user_groups$/, /^realm\/emoji$/, /^scheduled_messages$/, /^drafts$/],
  POST: [/^messages$/, /^messages\/\d+\/reactions$/, /^messages\/flags$/, /^messages\/flags\/narrow$/, /^user_topics$/,
    /^users\/me\/status$/, /^user_uploads$/, /^scheduled_messages$/, /^drafts$/, /^typing$/],
  PATCH: [/^messages\/\d+$/, /^scheduled_messages\/\d+$/, /^drafts\/\d+$/],
  DELETE: [/^messages\/\d+$/, /^messages\/\d+\/reactions$/, /^users\/me\/subscriptions$/, /^scheduled_messages\/\d+$/, /^drafts\/\d+$/],
});
const UPLOAD_NAME_RE = /^[A-Za-z0-9._ -]{1,100}$/;

/** Is `method path` one the hosted tools may call? */
export function pathAllowed(method, path) {
  if (typeof path !== "string" || !PATH_RE.test(path) || path.includes("..")) return false;
  return (ALLOWED_PATHS[method] ?? []).some((re) => re.test(path));
}

/** zulip.py encode_params:  strings as they are, booleans and numbers as text, the rest as JSON. */
export function encodeParams(params) {
  const out = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null) continue;
    if (typeof value === "string") out.append(key, value);
    else if (typeof value === "boolean") out.append(key, value ? "true" : "false");
    else if (typeof value === "number") out.append(key, String(value));
    else out.append(key, JSON.stringify(value));
  }
  return out.toString();
}

// The owner-in-the-conversation guard (AFC-469).  Mirrors scripts/agent_sync/zulip.py:  the POST
// paths whose body can carry a direct message, and the message types that make one.
export const DM_GUARD_PATHS = Object.freeze(["messages", "scheduled_messages", "drafts"]);
const DM_GUARD_TYPES = Object.freeze(["direct", "private"]);

/** The user ids in a `to` value, or an empty list when it names no user id. */
function recipientIds(value) {
  if (Array.isArray(value)) {
    const ids = [];
    for (const item of value) {
      const id = Number(item);
      if (!Number.isSafeInteger(id)) return [];
      ids.push(id);
    }
    return ids;
  }
  if (typeof value === "string") {
    const text = value.trim();
    if (!text.startsWith("[")) return [];
    try {
      const parsed = JSON.parse(text);
      return Array.isArray(parsed) ? recipientIds(parsed) : [];
    } catch {
      return [];
    }
  }
  return [];
}

/**
 * The user ids a request would deliver a direct message to, or null when it is not one.
 *
 * Three shapes carry a direct message:  {type: "direct", to: [ids]}, {type: "private", to: [ids]}
 * and the drafts endpoint's {drafts: [{type: "private", to: [ids]}]}.  A channel send names a
 * stream, not a user, so it is never a direct message.
 */
export function directRecipients(params) {
  if (!params || typeof params !== "object") return null;
  if (Array.isArray(params.drafts)) {
    const found = [];
    let direct = false;
    for (const draft of params.drafts) {
      if (!draft || typeof draft !== "object") continue;
      if (DM_GUARD_TYPES.includes(String(draft.type ?? "").trim().toLowerCase())) {
        direct = true;
        found.push(...recipientIds(draft.to));
      }
    }
    //  A drafts list with no direct draft in it is not a direct message at all.
    return direct ? found : null;
  }
  //  One draft being rewritten:  PATCH drafts/<id> carries {draft: {type, to}}, the same shape a
  //  created draft has, so a draft's recipient set is checked when it is rewritten and not only
  //  when it is created.
  if (params.draft && typeof params.draft === "object" && !Array.isArray(params.draft)) {
    const one = params.draft;
    if (DM_GUARD_TYPES.includes(String(one.type ?? "").trim().toLowerCase())) {
      return recipientIds(one.to);
    }
    return null;
  }
  if (DM_GUARD_TYPES.includes(String(params.type ?? "").trim().toLowerCase())) {
    return recipientIds(params.to);
  }
  return null;
}

/**
 * Throw DmGuardRefused unless a direct send has the owner in its recipient list.
 *
 * The rule (owner, Oct 23 2026):  a bot may send a direct message only when Jay is an actual
 * participant in the conversation.  Participation is the authenticated Zulip user id, never a
 * display name, and a mention of Jay in the message text is not participation.  It fails closed:
 *  with no owner id pinned a direct send is refused.
 */
export function checkDmGuard(path, params, ownerUserId) {
  //  The endpoint is the path's first segment:  drafts/123 is the same endpoint as drafts, and a
  //  draft's recipient set is rewritten by a PATCH.
  const endpoint = String(path ?? "").replace(/^\/+|\/+$/g, "").split("/")[0];
  if (!DM_GUARD_PATHS.includes(endpoint)) return;
  const recipients = directRecipients(params);
  if (recipients === null) return;
  const owner = Number(ownerUserId);
  if (!Number.isSafeInteger(owner) || owner <= 0) {
    throw new DmGuardRefused(
      "the owner's Zulip user id is not pinned, so this direct message cannot be checked against it:  " +
        "set OWNER_USER_ID and try again",
      { code: "dm_owner_not_pinned" },
    );
  }
  if (recipients.includes(owner)) return;
  throw new DmGuardRefused(
    "a direct message may go out only when the owner is in the conversation:  add the owner to the " +
      "recipients, or post in a channel instead",
    { code: "dm_owner_not_participant" },
  );
}

function basicHeader(email, key) {
  const bytes = new TextEncoder().encode(`${email}:${key}`);
  return `Basic ${btoa(String.fromCharCode(...bytes))}`;
}

function retryAfterMs(response, data) {
  const header = response.headers.get("retry-after");
  const fromHeader = header !== null && /^\d+(\.\d+)?$/.test(header.trim()) ? Number(header) : NaN;
  const body = data && typeof data === "object" ? data["retry-after"] : undefined;
  const fromBody = typeof body === "number" ? body : typeof body === "string" ? Number(body) : NaN;
  const seconds = Number.isFinite(fromHeader) ? fromHeader : Number.isFinite(fromBody) ? fromBody : NaN;
  return Number.isFinite(seconds) ? Math.max(0, seconds * 1000) : null;
}

export class ZulipClient {
  /**
   * @param {{email: string, key: string, ownerUserId?: number, fetch?: typeof fetch, sleep?: (ms: number) => Promise<void>, now?: () => number}} options
   */
  constructor({ email, key, ownerUserId = 0, fetch: fetchImpl = globalThis.fetch.bind(globalThis), sleep = (ms) => new Promise((r) => setTimeout(r, ms)), now = Date.now }) {
    this.email = email;
    this.#key = key;
    // The owner's Zulip user id, for the owner-in-the-conversation guard (AFC-469).  0 means "not
    // pinned", and the guard then refuses every direct send:  it fails closed.
    this.ownerUserId = Number(ownerUserId);
    if (!Number.isSafeInteger(this.ownerUserId)) this.ownerUserId = 0;
    this.fetch = fetchImpl;
    this.sleep = sleep;
    this.now = now;
  }

  #key;

  get(path, params) {
    return this.request("GET", path, params);
  }

  post(path, params) {
    return this.request("POST", path, params);
  }

  patch(path, params) {
    return this.request("PATCH", path, params);
  }

  delete(path, params) {
    return this.request("DELETE", path, params);
  }

  /** POST user_uploads with one file part.  Returns Zulip's answer ({uri, url, filename}). */
  upload(filename, bytes, contentType = "application/octet-stream") {
    if (!UPLOAD_NAME_RE.test(filename) || !filename.replace(/[ .]/g, "")) throw new ApiError("bad upload name", { status: 0, code: "BAD_PATH" });
    const form = new FormData();
    form.append("filename", new Blob([bytes], { type: /^[A-Za-z0-9.+-]{1,60}\/[A-Za-z0-9.+-]{1,60}$/.test(contentType) ? contentType : "application/octet-stream" }), filename);
    return this.request("POST", "user_uploads", null, { form });
  }

  /** One API call.  Returns the parsed JSON on result "success";  throws ApiError or NetworkError. */
  async request(method, path, params, { form = null } = {}) {
    if (!pathAllowed(method, path)) throw new ApiError("bad API path", { status: 0, code: "BAD_PATH" });
    // A POST or a PATCH that would deliver a direct message is checked before anything is sent:
    // the owner has to be in the recipient list (AFC-469).  A PATCH matters because editing a
    // draft rewrites its recipient set, and a draft is a message nobody has sent yet.  The check
    // throws and sends nothing.
    if (method === "POST" || method === "PATCH") checkDmGuard(path, params, this.ownerUserId);
    let sendParams = params ?? {};
    if ((method === "POST" || method === "PATCH") && typeof sendParams.content === "string" && OUTBOUND_TEXT_PATH.test(path)) {
      sendParams = { ...sendParams, content: fixOutbound(sendParams.content, { limit: MAX_CONTENT_LENGTH })[0] };
    }
    const encoded = encodeParams(sendParams);
    // GET carries its parameters in the query;  DELETE in the body and the query too (zulip.py does the
    // same, so a server variant that reads only one of them still sees them).
    const url = `${REALM}/api/v1/${path}${(method === "GET" || method === "DELETE") && encoded ? `?${encoded}` : ""}`;
    const started = this.now();
    for (let attempt = 1; ; attempt++) {
      let response;
      try {
        response = await this.fetch(url, {
          method,
          redirect: "manual",
          signal: AbortSignal.timeout(TIMEOUT_MS),
          headers: {
            Authorization: basicHeader(this.email, this.#key),
            "User-Agent": "agent-sync-mcp (hosted)",
            // A multipart upload sets its own boundary header.
            ...(method === "GET" || form ? {} : { "Content-Type": "application/x-www-form-urlencoded" }),
          },
          ...(method === "GET" ? {} : { body: form ?? encoded }),
        });
      } catch (error) {
        const timeout = error?.name === "TimeoutError" || error?.name === "AbortError";
        // A write that never got an answer may have landed:  POST is maybe-sent.
        throw new NetworkError(timeout ? "timed out" : "connection failed", { timeout, maybeSent: method !== "GET" });
      }
      if (response.status >= 300 && response.status < 400) {
        throw new ApiError("Zulip answered with a redirect, which this client never follows", { status: response.status, code: "REDIRECT" });
      }
      let data = null;
      try {
        data = await response.json();
      } catch {
        data = null;
      }
      if (response.status === 429) {
        const wait = retryAfterMs(response, data);
        const elapsed = this.now() - started;
        if (attempt < MAX_ATTEMPTS && wait !== null && elapsed + wait <= RATE_LIMIT_BUDGET_MS) {
          await this.sleep(wait);
          continue;
        }
        throw new ApiError("rate limited", { status: 429, code: data?.code ?? "RATE_LIMIT_HIT", data: { "retry-after": wait === null ? null : wait / 1000 } });
      }
      if (response.ok && data && data.result === "success") return data;
      throw new ApiError("Zulip refused the request", { status: response.status, code: typeof data?.code === "string" ? data.code : null, data: {} });
    }
  }
}
