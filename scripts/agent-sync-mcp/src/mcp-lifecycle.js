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
//
// A plain tools/call frame is recognised here too, and answered by serveRoutedMcp below once the
// route has built the seat's tools:  the first call an isolate serves spent about 20 ms of CPU
// building the SDK's server, handler and protocol schemas, for a request the router answers itself.

import { classifyInboundRequest, hostHeaderValidationResponse, isJsonContentType, ProtocolError, ProtocolErrorCode, SUPPORTED_PROTOCOL_VERSIONS } from "@modelcontextprotocol/server";
import { PUBLIC_HOST } from "./config.js";
import { IdentityRouter, identityInstructions, toolListCacheKey, toolListFor, allowedIdentities, pageOf, TOOLS_LIST_PAGE } from "./delegated-identities.js";
import { UnknownTool, hostedInstructions } from "./hosted-tools.js";
import { readBodyBytes } from "./wake.js";
import { SERVER_VERSION, serveMcpRequest } from "./mcp.js";
import { ToolError } from "./contract.js";

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

function serveToolList(seat, config, id, cursor) {
  // A cursor opts into one bounded page (AFC-641);  without one the full list is
  // served, byte-identical to before paging existed.
  if (cursor !== undefined) {
    let page;
    try {
      page = pageOf(toolListFor(seat, config).list, cursor, TOOLS_LIST_PAGE);
    } catch (error) {
      if (error instanceof ToolError) return sseError(id, -32602, error.message);
      throw error;
    }
    return sse(page, id);
  }
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

function sseError(id, code, message) {
  return new Response(`event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id: id ?? null, error: { code, message } })}\n\n`, { status: 200, headers: SSE_HEADERS });
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
  if (method === "tools/list" && (emptyParams(params) || (isObject(params) && Object.keys(params).length === 1 && typeof params.cursor === "string")))
    return serveToolList(seat, config, id, params?.cursor);
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
  // Longer than its Content-Length claimed, and the body is gone:  never forward it empty.
  if (bytes === null) return { response: new Response("Payload Too Large", { status: 413 }) };
  const carryOn = (call = null) => {
    const next = new Request(request, { body: bytes, cf: request.cf });
    if (call) CALLS.set(next, call);
    return { request: next };
  };
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
  return response ? { response } : carryOn(toolCallFrame(body));
}

// ---------------------------------------------------------------- tools/call without the SDK

// A plain tools/call frame this module recognised, by the rebuilt request that carries its bytes.
const CALLS = new WeakMap();

/**
 * The frame's call when it is exactly the shape the SDK would accept without adding or changing
 * anything:  an id, a tool name, and arguments that are a plain object or absent.  A `_meta`, a task,
 * any other key, or an `__proto__` key in the arguments (the SDK copies the arguments, and a copy
 * treats that key differently from the parsed original) stays with the SDK.
 */
function toolCallFrame(body) {
  const { id, method, params } = body;
  if (method !== "tools/call" || !Object.hasOwn(body, "id") || (typeof id !== "string" && !Number.isSafeInteger(id))) return null;
  if (!isObject(params) || Object.keys(params).some((key) => key !== "name" && key !== "arguments")) return null;
  if (typeof params.name !== "string" || params.name === "") return null;
  const args = params.arguments;
  if (args !== undefined && (!isObject(args) || Object.hasOwn(args, "__proto__"))) return null;
  return { id, name: params.name, args };
}

/** The call answerLifecycle recognised in this exact request, or null. */
export function takeToolCall(request) {
  return CALLS.get(request) ?? null;
}

const isPlainStructured = (value) => !(typeof value !== "object" || value === null || Array.isArray(value));

/**
 * What the SDK does to a tool result before it is sent, on the legacy wire era (its projectCallToolResult):
 * a structured result that is not an object gets a text copy when it has no text, and is wrapped as
 * { result } when it is not an object or when the tool advertises an output schema whose root is not an
 * object.  test/mcp-call.test.mjs compares this with the SDK's own method.
 */
export function projectToolResult(result, outputSchema) {
  let withText = result;
  const sc = result.structuredContent;
  if (sc !== undefined && !isPlainStructured(sc) && !(result.content?.some((part) => part.type === "text") ?? false)) {
    withText = { ...result, content: [...(result.content ?? []), { type: "text", text: JSON.stringify(sc) }] };
  }
  const structured = withText.structuredContent;
  if (structured === undefined) return withText;
  const schemaWrapped = outputSchema !== undefined && outputSchema.type !== "object";
  if (isPlainStructured(structured) && !schemaWrapped) return withText;
  return { ...withText, structuredContent: { result: structured } };
}

const RESULT_KEYS = new Set(["_meta", "content", "structuredContent", "isError"]);

/** A text block as the SDK's content schema leaves it:  its known keys in order, and nothing else. */
function textBlock(block) {
  if (block === null || typeof block !== "object" || block.type !== "text" || typeof block.text !== "string") return block;
  return { type: "text", text: block.text, ...(block.annotations !== undefined ? { annotations: block.annotations } : {}), ...(block._meta !== undefined ? { _meta: block._meta } : {}) };
}

/**
 * The SDK passes every tool result through its CallToolResult schema before it is sent:  `_meta`, `content`
 * (an empty list when absent), `structuredContent` and `isError`, in that order, then any other key, with a
 * text block cut down to its own keys.  The tools build every result with okResult or errorResult
 * (src/contract.js), which have only those keys and plain text blocks.
 */
function normalizedResult(result) {
  const out = {};
  if (result._meta !== undefined) out._meta = result._meta;
  out.content = Array.isArray(result.content) ? result.content.map(textBlock) : result.content ?? [];
  if (result.structuredContent !== undefined) out.structuredContent = result.structuredContent;
  if (result.isError !== undefined) out.isError = result.isError;
  for (const key of Object.keys(result)) if (!RESULT_KEYS.has(key)) out[key] = result[key];
  return out;
}

/** The JSON-RPC error body the SDK sends for a handler that threw. */
function thrownErrorBody(error) {
  const code = Number.isSafeInteger(error?.code) ? error.code : ProtocolErrorCode.InternalError;
  return { code: code === -32002 ? -32602 : code, message: error?.message ?? "Internal error", ...(error?.data !== undefined ? { data: error.data } : {}) };
}

/** The one SSE event the SDK writes for this call, as text. */
export async function toolCallEvent({ id, name, args }, router) {
  let payload;
  try {
    let result;
    try {
      result = await router.call(name, args);
    } catch (error) {
      if (error instanceof UnknownTool) throw new ProtocolError(ProtocolErrorCode.InvalidParams, "Unknown tool");
      throw error;
    }
    payload = { result: normalizedResult(projectToolResult(result, result.isError ? undefined : router.toolsByName[name]?.outputSchema)), jsonrpc: "2.0", id };
  } catch (error) {
    payload = { jsonrpc: "2.0", id, error: thrownErrorBody(error) };
  }
  try {
    return `event: message\ndata: ${JSON.stringify(payload)}\n\n`;
  } catch {
    return `event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id, error: thrownErrorBody(null) })}\n\n`;
  }
}

/**
 * The answer to a recognised tool call:  200 with the SSE headers at once, and the event when the
 * tool is done, which is what the SDK's transport does (a long tool never holds the headers back).
 */
function serveToolCall(call, router) {
  const body = new ReadableStream({
    start(controller) {
      toolCallEvent(call, router).then((text) => {
        controller.enqueue(encoder.encode(text));
        controller.close();
      }, (error) => controller.error(error));
    },
  });
  return new Response(body, { status: 200, headers: SSE_HEADERS });
}

/**
 * serveMcpRequest, except that a tool call answerLifecycle recognised and the router lists is answered
 * here, without building the SDK's server.  Everything else (a seat with events, a name the seat's list
 * does not hold, a frame with anything unusual in it) goes to serveMcpRequest unchanged.
 */
export async function serveRoutedMcp(request, tools, authInfo, events = null) {
  const call = events === null && tools instanceof IdentityRouter ? takeToolCall(request) : null;
  if (!call || tools.eventsOnly || !Object.hasOwn(tools.toolsByName, call.name)) return serveMcpRequest(request, tools, authInfo, events);
  return serveToolCall(call, tools);
}
