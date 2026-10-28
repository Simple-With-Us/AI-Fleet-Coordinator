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

import { z } from "zod";
import { callTypedV1 } from "./gr-typed-v1.js";
import { WF_V1_READ_ACTIONS, validWfV1Args, boundedWorkflowInput, parseWfV1Args, validWfV1Response } from "./gr-workflow-v1.js";
import { resolveSecret } from "./infisical.js";

/** The relay's response envelope (kody discussion_r4372745180):  every body
 * is decoded as UNKNOWN and validated with RelayResponseSchema.parse before
 * any field is read, so malformed upstream data can never reach a caller
 * dressed as a successful response.  The envelope is a JSON object whose
 * error fields, when present, have these types;  success bodies carry
 * action-specific payloads alongside. */
export const RelayResponseSchema = z.looseObject({
  error: z.string().max(1000).optional(),
  outcome: z.literal("unknown").optional(),
  retryable: z.boolean().optional(),
});

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

/** The Access service-token pair, resolved from the Worker-only Fleet-OPS
 * folder /gr-front (never /tools:  the relay host reads /tools for its own
 * keys and must never gain this pair);  both absent reads as capability-off,
 * never a throw. */
export async function resolveGrSecrets(env) {
  const read = async (name) => (await resolveSecret(env, { name, path: "/gr-front", envName: name, optional: true })).trim();
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

  async callTyped(action, args, expectedPrincipal) {
    return callTypedV1({action,args,baseUrl:this.#base,bearer:this.#secrets[2],
      accessId:this.#secrets[0],accessSecret:this.#secrets[1],fetchImpl:this.fetch,expectedPrincipal});
  }

  // Workflow verbs get a bounded stream, longer deadline and conservative write
  // uncertainty.  No automatic retries.  Existing seven-action transport is unchanged.
  async callWorkflow(action, args) {
    // Bound depth/nodes/cycles before serialization, then cap raw JSON bytes
    // before recursive Zod parsing. Keep the normalized payload check below.
    if(!boundedWorkflowInput(args))return {status:400,body:{error:"invalid_workflow_args"}};
    const rawSerialized=JSON.stringify(args);
    if(new TextEncoder().encode(rawSerialized).length>65536)return {status:400,body:{error:"workflow_request_too_large"}};
    const parsedArgs=parseWfV1Args(action,args);
    if(parsedArgs===null)return {status:400,body:{error:"invalid_workflow_args"}};
    args=parsedArgs;
    const serialized = JSON.stringify(args);
    if (new TextEncoder().encode(serialized).length > 65536) return {status:400,body:{error:"workflow_request_too_large"}};
    const write = !WF_V1_READ_ACTIONS.includes(action);
    const unavailable = () => ({status:502,body:{error:"upstream_unavailable", ...(write ? {outcome:"unknown",retryable:false} : {retryable:true})}});
    try {
      const response = await this.fetch(`${this.#base}/v1/${action}`, {
        method:"POST", headers:this.#headers, body:serialized,
        redirect:"manual", signal:AbortSignal.timeout(110000),
      });
      if (!response.body) return unavailable();
      const reader = response.body.getReader(); const chunks=[]; let size=0;
      for (;;) {
        const {done,value}=await reader.read(); if(done) break;
        size+=value.byteLength;
        if(size>1048576) { await reader.cancel(); return unavailable(); }
        chunks.push(value);
      }
      const bytes=new Uint8Array(size); let at=0;
      for(const chunk of chunks){bytes.set(chunk,at);at+=chunk.byteLength;}
      const body=JSON.parse(new TextDecoder("utf-8",{fatal:true}).decode(bytes));
      const encoded=JSON.stringify(body);
      if(this.#secrets.filter(Boolean).some(secret=>encoded.includes(secret))) return unavailable();
      if(response.status===200) {
        if(!validWfV1Response(action,body)) return unavailable();
        return {status:200,body};
      }
      if (response.status < 400 || response.status > 599 || !body || typeof body!=="object" || Array.isArray(body)) return unavailable();
      const errorShape = z.object({error:z.string().max(1000),retryable:z.boolean().optional(),outcome:z.enum(['pending','unknown','refused']).optional()}).safeParse(body);
      if(!errorShape.success) return unavailable();
      // Never reflect upstream error prose; a duplicate can be pending/unknown.
      if(response.status===409 && ['pending','unknown'].includes(body.outcome))
        return {status:502,body:{error:"workflow_outcome_unknown",outcome:"unknown",retryable:false}};
      if(response.status>=500 && body.retryable!==true) return unavailable();
      return {status:response.status,body:{error:"workflow_request_refused", ...(body.retryable===true ? {retryable:true} : {})}};
    } catch {return unavailable();}
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
    let text;
    try {
      text = await response.text();
    } catch {
      return { status: 0, body: { error: "upstream_unreachable" } };
    }
    // A REAL 5xx (relay crash, edge error) can follow an accepted write, so
    // an unreadable 5xx body stays outcome-unknown rather than "unreadable
    // answer" (kody discussion_r4372747157);  the caller's own synthetic 502s
    // for sub-5xx statuses keep the plain unreadable mapping.
    const realServerError = response.status >= 500;
    if (text.length > GR_RESPONSE_LIMIT) {
      return { status: 502, body: realServerError ? { error: "upstream_oversize", outcome: "unknown" } : { error: "upstream_oversize" } };
    }
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch {
      return { status: 502, body: realServerError ? { error: "upstream_bad_json", outcome: "unknown" } : { error: "upstream_bad_json" } };
    }
    // The relay always answers the RelayResponseSchema envelope;  anything
    // else is malformed upstream data and must not reach the caller as a
    // successful shape.
    const result = RelayResponseSchema.safeParse(parsed);
    if (!result.success) {
      return { status: 502, body: realServerError ? { error: "upstream_bad_shape", outcome: "unknown" } : { error: "upstream_bad_shape" } };
    }
    return { status: response.status, body: result.data };
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

const REPO = { type: "string", description: "owner/name" };
const NUMBER = { type: "integer", description: "pull request number" };
const obj = (required, properties) => ({ type: "object", additionalProperties: false, required, properties });

/** The hosted GR tool specs (names gr_<action>).  Descriptions name the
 * identity the action runs as;  the relay's per-seat allowlist is the real
 * policy boundary, this server only fronts it. */
export const GR_TOOLS = Object.freeze([
  {
    name: "gr_open_pr",
    title: "Open Pull Request",
    description: "Open a pull request through git-relay.  Runs under the owner's scoped PR-only token (the only action that token performs);  the title and body are scanned for credential-shaped text before anything is sent.",
    inputSchema: obj(["repo", "base", "head", "title", "body"], { repo: REPO, base: { type: "string" }, head: { type: "string" }, title: { type: "string" }, body: { type: "string" } }),
  },
  {
    name: "gr_edit_pr",
    title: "Edit Pull Request",
    description: "Edit a pull request's title and/or body through git-relay, as your own GitHub App.  At least one of title or body is required.",
    inputSchema: {
      ...obj(["repo", "number"], { repo: REPO, number: NUMBER, title: { type: "string" }, body: { type: "string" } }),
      // An edit with neither field can only fail;  refuse it at the schema
      // boundary (the relay's _validate_args refuses it too, as a 400).
      anyOf: [{ required: ["title"] }, { required: ["body"] }],
    },
  },
  {
    name: "gr_reply_thread",
    title: "Reply to Review Comment",
    description: "Reply to a pull request review comment through git-relay, as your own GitHub App.",
    inputSchema: obj(["repo", "number", "comment_id", "body"], { repo: REPO, number: NUMBER, comment_id: { type: "integer" }, body: { type: "string" } }),
  },
  {
    name: "gr_comment_pr",
    title: "Comment on Pull Request",
    description: "Add a conversation comment to a pull request through git-relay, as your own GitHub App.",
    inputSchema: obj(["repo", "number", "body"], { repo: REPO, number: NUMBER, body: { type: "string" } }),
  },
  {
    name: "gr_merge_pr",
    title: "Merge Pull Request",
    description: "Merge a pull request through git-relay, as your own GitHub App, when your seat's allowlist covers it.  method is squash (default), merge or rebase.",
    inputSchema: obj(["repo", "number"], { repo: REPO, number: NUMBER, method: { type: "string", enum: ["squash", "merge", "rebase"] } }),
  },
  {
    name: "gr_workflow_dispatch",
    title: "Dispatch Workflow",
    description: "Trigger a workflow_dispatch run through git-relay, as your own GitHub App.  inputs is a flat object of scalar values.",
    inputSchema: obj(["repo", "workflow", "ref"], { repo: REPO, workflow: { type: "string" }, ref: { type: "string" }, inputs: { type: "object" } }),
  },
  {
    name: "gr_resolve_thread",
    title: "Resolve Review Thread",
    description: "Resolve a pull request review thread through git-relay, as your own GitHub App.  thread_id is the PRRT_ review-thread id.",
    inputSchema: obj(["repo", "thread_id"], { repo: REPO, thread_id: { type: "string" } }),
  },
]);
export const GR_TOOL_NAMES = Object.freeze(GR_TOOLS.map((tool) => tool.name));
