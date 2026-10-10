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

  /** One API call.  Returns the parsed JSON on result "success";  throws ApiError or NetworkError. */
  async request(method, path, params) {
    if (!PATH_RE.test(path) || path.includes("..")) throw new ApiError("bad API path", { status: 0, code: "BAD_PATH" });
    let sendParams = params ?? {};
    if (method === "POST" && typeof sendParams.content === "string" && OUTBOUND_TEXT_PATH.test(path)) {
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
            ...(method === "GET" ? {} : { "Content-Type": "application/x-www-form-urlencoded" }),
          },
          ...(method === "GET" ? {} : { body: encoded }),
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
