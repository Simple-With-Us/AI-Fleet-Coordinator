// GR-MCP front:  the Worker proxies the git-relay actions for bearer identities
// (owner direction 2026-10-22:  every Grok Bot is on ST-MCP and GR-MCP;  the
// relay itself stays tailnet-local on its host).  The relay sits behind the
// fleet tunnel on the existing Cloudflare Access app, exactly like recall
// (src/recall.js):  this client pins one https origin, sends the Access
// service-token headers, and passes the CALLER'S bearer through unchanged -
// the relay resolves it to the identity through the shared verifier
// (/internal/gr-bearer-verify) and enforces its own per-seat allowlist, so
// the Worker adds no policy of its own and never sees an App token.
//
// mint_token is NOT fronted:  raw App tokens never cross the Worker (the
// relay keeps it HTTP-only for seats on the tailnet, mirroring the stdio
// wrapper's rule).
//
// Failure shape:  fail closed.  No upstream URL, no Access credential, an
// unreachable or mis-answering relay - the tools answer not_configured or the
// relay's own error body;  no fallback, no retry storm, secrets never logged.

import { resolveSecret } from "./infisical.js";

export const GR_TIMEOUT_MS = 15_000;
export const GR_RESPONSE_LIMIT = 262_144; // bytes:  a relay answer is small JSON
export const GR_SECRETS = Object.freeze({
  accessId: "GR_ACCESS_CLIENT_ID",
  accessSecret: "GR_ACCESS_CLIENT_SECRET",
});

// The seven fronted actions, in the order the admin panel and docs name them.
// open_pr is the only one the relay still runs under the owner (PR-only)
// token;  the rest run as the calling seat's own GitHub App.
export const GR_ACTIONS = Object.freeze([
  "open_pr",
  "edit_pr",
  "reply_thread",
  "comment_pr",
  "merge_pr",
  "workflow_dispatch",
  "resolve_thread",
]);

/** The upstream origin when it is a bare https origin (a trailing slash
 * allowed), else null.  Same rule as listenerBaseUrl:  any path, query,
 * credentials or non-https scheme reads as unset. */
export function grUpstreamUrl(raw) {
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

/** The Access service-token pair, resolved through Infisical /tools like the
 * recall pair;  both absent reads as capability-off, never a throw. */
export async function resolveGrSecrets(env) {
  const read = async (name) => (await resolveSecret(env, { name, path: "/tools", envName: name, optional: true })).trim();
  const [accessId, accessSecret] = await Promise.all([read(GR_SECRETS.accessId), read(GR_SECRETS.accessSecret)]);
  return { accessId, accessSecret };
}

export class GrRelayClient {
  #base;
  #headers;
  #secrets;

  /** `bearer` is the caller's own admin-minted bearer, forwarded verbatim on
   * every call;  the relay authenticates it. */
  constructor({ baseUrl, accessId, accessSecret, bearer, fetch: fetchImpl = globalThis.fetch?.bind(globalThis), timeoutMs = GR_TIMEOUT_MS }) {
    this.#base = baseUrl;
    this.#headers = {
      "CF-Access-Client-Id": accessId,
      "CF-Access-Client-Secret": accessSecret,
      "Authorization": `Bearer ${bearer}`,
      "Content-Type": "application/json",
      "User-Agent": "agent-sync-mcp/2.0",
    };
    this.#secrets = [accessId, accessSecret, bearer];
    this.fetch = fetchImpl;
    this.timeoutMs = timeoutMs;
  }

  secrets() {
    return this.#secrets;
  }

  /** POST /v1/<action> with the tool's args;  returns the relay's parsed JSON
   * and HTTP status.  Never throws on a relay refusal - a 4xx body is data
   * the tool caller should see (its own allowlist answer). */
  async call(action, args) {
    let response;
    try {
      response = await this.fetch(`${this.#base}/v1/${action}`, {
        method: "POST",
        headers: this.#headers,
        body: JSON.stringify(args ?? {}),
        redirect: "manual",
        signal: AbortSignal.timeout(this.timeoutMs),
      });
    } catch {
      return { status: 0, body: { error: "upstream_unreachable" } };
    }
    const text = await response.text();
    if (text.length > GR_RESPONSE_LIMIT) return { status: 502, body: { error: "upstream_oversize" } };
    let body;
    try {
      body = JSON.parse(text);
    } catch {
      return { status: 502, body: { error: "upstream_bad_json" } };
    }
    return { status: response.status, body };
  }
}

/** A configured client, or null when the front is not configured (no upstream
 * URL, or the Access pair is absent).  `bearer` is required:  the front never
 * answers for an OAuth grant, only for bearer identities. */
export function grClientFromResolved({ accessId, accessSecret }, env, { bearer } = {}) {
  const baseUrl = grUpstreamUrl(env?.GR_UPSTREAM_URL);
  if (!baseUrl || !accessId || !accessSecret || typeof bearer !== "string" || !bearer) return null;
  return new GrRelayClient({ baseUrl, accessId, accessSecret, bearer });
}
