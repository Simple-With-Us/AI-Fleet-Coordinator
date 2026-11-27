// Service-only deployment canary on the real /mcp route.  CI proves the live Worker with a bearer that belongs to
// no seat, no Zulip bot and no OAuth grant:  `scn_` followed by random characters, checked against the SHA-256
// digest in SERVICE_CANARY_BEARER_SHA256 (a Worker var or secret, never the bearer itself).  Unset or malformed
// digest means the canary is off and every `scn_` bearer is refused.
//
// What it serves is the same SDK stack as every seat (createMcpHandler over a low-level Server, same Host check,
// same bounded body) with exactly one tool, `deploy_canary`.  The tool is read-only, takes no arguments, returns a
// fixed answer and reaches no gate, KV, Durable Object, Infisical, upstream or seat credential.  It cannot write,
// delegate, route to another identity or list seats.
import { createMcpHandler } from "agents/mcp/server";
import { Server, ProtocolError, ProtocolErrorCode } from "@modelcontextprotocol/server";
import { PUBLIC_HOST } from "./config.js";
import { readBodyBytes } from "./wake.js";
import { sha256Hex } from "./gb-bearer.js";
import { SERVER_VERSION } from "./mcp.js";

export const CANARY_BEARER_RE = /^scn_[A-Za-z0-9_-]{32,128}$/;
export const CANARY_TOOL = "deploy_canary";
export const CANARY_MAX_BODY_BYTES = 4096;
const DIGEST_RE = /^[0-9a-f]{64}$/;

const TOOL = Object.freeze({
  name: CANARY_TOOL,
  description: "Read-only deployment canary.  Takes no arguments and returns a fixed answer.",
  inputSchema: Object.freeze({ type: "object", properties: Object.freeze({}), additionalProperties: false }),
  annotations: Object.freeze({ readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false }),
});

/** The bearer in an Authorization header when it has the scn_ prefix, else null. */
export function canaryBearerFromRequest(request) {
  const header = request.headers.get("authorization") ?? "";
  const m = /^Bearer\s+(scn_\S+)$/i.exec(header.trim());
  return m ? m[1] : null;
}

function sameDigest(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

/** True only for a well-formed canary bearer whose digest equals the configured one. */
export async function verifyCanaryBearer(env, token) {
  const expected = String(env?.SERVICE_CANARY_BEARER_SHA256 ?? "").trim().toLowerCase();
  if (!DIGEST_RE.test(expected) || typeof token !== "string" || !CANARY_BEARER_RE.test(token)) return false;
  return sameDigest(await sha256Hex(token), expected);
}

export function buildCanaryServer() {
  const server = new Server(
    { name: "ST-MCP-CANARY", version: SERVER_VERSION },
    { capabilities: { tools: {} }, instructions: "Deployment canary.  One read-only tool, no seat." },
  );
  server.setRequestHandler("tools/list", async () => ({ tools: [TOOL] }));
  server.setRequestHandler("tools/call", async (request) => {
    if (request.params?.name !== CANARY_TOOL) throw new ProtocolError(ProtocolErrorCode.InvalidParams, "Unknown tool");
    const args = request.params?.arguments;
    if (args !== undefined && (!args || typeof args !== "object" || Array.isArray(args) || Object.keys(args).length)) {
      throw new ProtocolError(ProtocolErrorCode.InvalidParams, "No arguments accepted");
    }
    return { content: [{ type: "text", text: JSON.stringify({ ok: true, canary: "service" }) }] };
  });
  return server;
}

/** Serve one /mcp request for a canary bearer the caller already verified. */
export async function serveCanaryMcp(request) {
  let bounded = request;
  if (request.method === "POST") {
    const bytes = await readBodyBytes(request, CANARY_MAX_BODY_BYTES);
    if (bytes === null) return new Response("Payload Too Large", { status: 413 });
    bounded = new Request(request, { body: bytes, cf: request.cf });
  }
  const handler = createMcpHandler(() => buildCanaryServer(), {
    route: "/mcp",
    maxRequestBodySize: CANARY_MAX_BODY_BYTES,
    allowedHostnames: [PUBLIC_HOST],
    onerror: (error) => console.log(JSON.stringify({ event: "mcp_error", name: error?.name ?? "Error" })),
  });
  return handler.fetch(bounded, { authInfo: { token: "service-canary", clientId: "service-canary", scopes: [], extra: {} } });
}
