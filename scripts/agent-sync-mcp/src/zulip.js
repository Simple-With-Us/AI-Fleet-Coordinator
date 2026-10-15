// Zulip egress for the hosted seats (spec 3.8).  Only the compiled realm is
// ever called, under /api/v1/, with `redirect: "manual"` (any 3xx is an error)
// and a 15-second timeout.  The Basic header is built per request from the
// seat's key and never logged.  On 429 the wait comes from the Retry-After
// header or the JSON body's retry-after, within a 20-second budget per call and
// at most 2 attempts (spec 3.7);  past that the caller gets rate_limited.
//
// Pure module:  the caller hands in `fetch` and `sleep`, so node tests run it
// against a fake Zulip.

import { ApiError, NetworkError } from "./contract.js";
import { sentenceGap } from "./textfmt.js";

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
   * @param {{email: string, key: string, fetch?: typeof fetch, sleep?: (ms: number) => Promise<void>, now?: () => number}} options
   */
  constructor({ email, key, fetch: fetchImpl = globalThis.fetch.bind(globalThis), sleep = (ms) => new Promise((r) => setTimeout(r, ms)), now = Date.now }) {
    this.email = email;
    this.#key = key;
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
    let sendParams = params ?? {};
    if ((method === "POST" || method === "PATCH") && typeof sendParams.content === "string" && OUTBOUND_TEXT_PATH.test(path)) {
      sendParams = { ...sendParams, content: sentenceGap(sendParams.content) };
    }
    const encoded = encodeParams(sendParams);
    const url = `${REALM}/api/v1/${path}${method === "GET" && encoded ? `?${encoded}` : ""}`;
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
