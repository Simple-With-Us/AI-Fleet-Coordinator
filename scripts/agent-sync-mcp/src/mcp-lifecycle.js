// initialize, ping, notifications and tools/list answered without the SDK
// (AFC-617).  None of them touches a secret or a tool, yet each one used to
// resolve every secret and build an SDK server and transport, and they are about
// a third of all /mcp requests.
//
// Runs only after the caller's authentication and seat checks.  This answers
// ONLY a plain, valid frame on the SDK's legacy (stateless) path, with the bytes
// and headers the SDK would send (test/mcp-lifecycle.test.mjs compares them with
// the real SDK).  Anything else, including every malformed frame, a browser
// Origin, a batch, a modern-protocol request and an events-only seat, goes on to
// the SDK, so its checks and errors stay the only ones.  A seat that can offer
// MCP Events must not reach this at all (its capabilities depend on a secret).

import { classifyInboundRequest, hostHeaderValidationResponse, isJsonContentType, SUPPORTED_PROTOCOL_VERSIONS } from "@modelcontextprotocol/server";
import { PUBLIC_HOST } from "./config.js";
import { identityInstructions, toolListCacheKey, toolListFor, allowedIdentities } from "./delegated-identities.js";
import { hostedInstructions } from "./hosted-tools.js";
import { readBodyBytes } from "./wake.js";
import { SERVER_VERSION } from "./mcp.js";

// A lifecycle frame is tiny.  A larger body stays with the SDK path unread here.
export const LIFECYCLE_MAX_BYTES = 64 * 1024;

const CORS_HEADERS = Object.freeze({
  "Access-Control-Allow-Headers": "Content-Type, Accept, Authorization, mcp-session-id, MCP-Protocol-Version, Mcp-Method, Mcp-Name",
  "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Expose-Headers": "mcp-session-id",
  "Access-Control-Max-Age": "86400",
});
const SSE_HEADERS = Object.freeze({
  ...CORS_HEADERS,
  "Content-Type": "text/event-stream",
  "Cache-Control": "no-cache, no-transform",
  Connection: "keep-alive",
  "X-Accel-Buffering": "no",
});

const encoder = new TextEncoder();
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const emptyParams = (params) => params === undefined || (isObject(params) && Object.keys(params).length === 0);

function sse(result, id) {
  return new Response(`event: message\ndata: ${JSON.stringify({ result, jsonrpc: "2.0", id })}\n\n`, { status: 200, headers: SSE_HEADERS });
}

// tools/list bytes per isolate:  the list is about 470 KB, so it is serialized once.
const LIST_BYTES = new Map();

function serveToolList(seat, config, id) {
  const key = toolListCacheKey(seat, config);
  let cached = LIST_BYTES.get(key);
  if (!cached) {
    const tools = JSON.stringify(toolListFor(seat, config).list);
    cached = { head: encoder.encode(`event: message\ndata: {"result":{"tools":${tools}},"jsonrpc":"2.0","id":`), tail: encoder.encode("}\n\n") };
    LIST_BYTES.set(key, cached);
  }
  const idBytes = encoder.encode(JSON.stringify(id));
  const body = new Uint8Array(cached.head.length + idBytes.length + cached.tail.length);
  body.set(cached.head, 0);
  body.set(idBytes, cached.head.length);
  body.set(cached.tail, cached.head.length + idBytes.length);
  return new Response(body, { status: 200, headers: SSE_HEADERS });
}

function validInitialize(params) {
  return isObject(params)
    && SUPPORTED_PROTOCOL_VERSIONS.includes(params.protocolVersion)
    && isObject(params.capabilities)
    && isObject(params.clientInfo)
    && typeof params.clientInfo.name === "string"
    && typeof params.clientInfo.version === "string"
    && params._meta === undefined;
}

function answer(body, seat, config) {
  const { id, method, params } = body;
  const hasId = Object.hasOwn(body, "id");
  if (!hasId) {
    if (!method.startsWith("notifications/") || (params !== undefined && (!isObject(params) || params._meta !== undefined))) return null;
    return new Response(null, { status: 202, headers: CORS_HEADERS });
  }
  if (typeof id !== "string" && !Number.isSafeInteger(id)) return null;
  if (method === "ping" && emptyParams(params)) return sse({}, id);
  if (method === "tools/list" && emptyParams(params)) return serveToolList(seat, config, id);
  if (method === "initialize" && validInitialize(params)) {
    return sse({
      protocolVersion: params.protocolVersion,
      capabilities: { tools: {} },
      serverInfo: { name: "ST-MCP", version: SERVER_VERSION },
      instructions: hostedInstructions(seat) + identityInstructions(seat, allowedIdentities(seat)),
    }, id);
  }
  return null;
}

/**
 * { response } when this frame was answered here, else { request }:  the request
 * to carry on with, which may be a rebuilt copy because the body was read.
 */
export async function answerLifecycle(request, { seat, config }) {
  if (request.method !== "POST" || config.eventsOnlySeats.includes(seat)) return { request };
  const headers = request.headers;
  const accept = headers.get("accept");
  const version = headers.get("mcp-protocol-version");
  const declared = Number(headers.get("content-length") ?? "0");
  if (headers.has("origin")
    || !accept?.includes("application/json") || !accept.includes("text/event-stream")
    || !isJsonContentType(headers.get("content-type"))
    || (version !== null && !SUPPORTED_PROTOCOL_VERSIONS.includes(version))
    || !(declared > 0 && declared <= LIFECYCLE_MAX_BYTES)
    || hostHeaderValidationResponse(request, [PUBLIC_HOST])) return { request };
  const bytes = await readBodyBytes(request, LIFECYCLE_MAX_BYTES);
  if (bytes === null) return { request: new Request(request.url, { method: "POST", headers }) };
  const carryOn = () => ({ request: new Request(request, { body: bytes, cf: request.cf }) });
  let body;
  try {
    body = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
  } catch {
    return carryOn();
  }
  if (!isObject(body) || body.jsonrpc !== "2.0" || typeof body.method !== "string"
    || Object.keys(body).some((key) => !["jsonrpc", "id", "method", "params"].includes(key))) return carryOn();
  const outcome = classifyInboundRequest({
    httpMethod: "POST",
    protocolVersionHeader: version ?? undefined,
    mcpMethodHeader: headers.get("mcp-method") ?? undefined,
    mcpNameHeader: headers.get("mcp-name") ?? undefined,
    body,
  });
  if (outcome.kind !== "legacy") return carryOn();
  const response = answer(body, seat, config);
  return response ? { response } : carryOn();
}
