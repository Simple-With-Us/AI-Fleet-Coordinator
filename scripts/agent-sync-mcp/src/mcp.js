// The MCP endpoint:  `createMcpHandler` (agents) over a low-level SDK v2
// `Server`.  `tools/list` serves the stdio server's own tools.json plus the
// hosted-only fleet recall tools (src/recall.js), and
// `tools/call` runs the hosted tools, which validate arguments themselves:
// `McpServer` would answer a schema failure with JSON-RPC -32602, and the
// contract makes it a tool error (`invalid_argument`) the model can fix
// (spec 1, error model).  An unknown tool name stays a protocol error.
//
// MCP Events (src/events.js):  the server advertises `events` next to `tools`
// on `server/discover` (protocol revision 2026-07-28, which the SDK serves
// already) and answers events/list, events/subscribe and events/unsubscribe on
// this same authenticated endpoint.  They are not spec request methods in the
// SDK, so each is registered with a zod params schema.

import { createMcpHandler } from "agents/mcp/server";
import { Server, ProtocolError, ProtocolErrorCode } from "@modelcontextprotocol/server";
import { UnknownTool, HOSTED_TOOL_LIST, HOSTED_TOOLS_BY_NAME, hostedInstructions } from "./hosted-tools.js";
import { PUBLIC_HOST } from "./config.js";
import { EventError } from "./events.js";
import { z } from "zod";

export const SERVER_VERSION = "2.0.0-phase2";

const EventParams = Object.freeze({
  list: z.object({ cursor: z.string().max(200).optional() }).passthrough(),
  subscribe: z
    .object({
      name: z.string().max(100),
      arguments: z.record(z.string(), z.unknown()).nullable().optional(),
      delivery: z.object({ mode: z.string().max(20), url: z.string().max(2048), secret: z.string().max(200) }).passthrough(),
      cursor: z.unknown().optional(),
      ttlMs: z.number().nullable().optional(),
    })
    .passthrough(),
  unsubscribe: z
    .object({
      name: z.string().max(100),
      arguments: z.record(z.string(), z.unknown()).nullable().optional(),
      delivery: z.object({ mode: z.string().max(20).optional(), url: z.string().max(2048) }).passthrough(),
    })
    .passthrough(),
});

async function asProtocolError(fn) {
  try {
    return await fn();
  } catch (error) {
    if (error instanceof EventError) throw new ProtocolError(error.code, error.message, error.data);
    throw error;
  }
}

/** One Server per request, bound to that request's seat, tools and (optionally) events. */
export function buildServer(tools, events = null) {
  const server = new Server(
    { name: "agent-sync", version: SERVER_VERSION },
    { capabilities: events ? { tools: {}, events: {} } : { tools: {} }, instructions: hostedInstructions(tools.seat) },
  );
  if (events) {
    server.setRequestHandler("events/list", { params: EventParams.list }, async () => events.list());
    server.setRequestHandler("events/subscribe", { params: EventParams.subscribe }, (params) => asProtocolError(() => events.subscribe(params)));
    server.setRequestHandler("events/unsubscribe", { params: EventParams.unsubscribe }, (params) => asProtocolError(() => events.unsubscribe(params)));
  }
  server.setRequestHandler("tools/list", async () => ({ tools: [...HOSTED_TOOL_LIST] }));
  server.setRequestHandler("tools/call", async (request) => {
    const name = request.params?.name;
    let result;
    try {
      result = await tools.call(name, request.params?.arguments);
    } catch (error) {
      if (error instanceof UnknownTool) throw new ProtocolError(ProtocolErrorCode.InvalidParams, "Unknown tool");
      throw error;
    }
    return server.projectCallToolResult(result, result.isError ? undefined : HOSTED_TOOLS_BY_NAME[name]?.outputSchema);
  });
  return server;
}

/** Serve one /mcp request for `tools` (the Worker checked the grant first). */
export function serveMcpRequest(request, tools, authInfo, events = null) {
  const handler = createMcpHandler(() => buildServer(tools, events), {
    route: "/mcp",
    allowedHostnames: [PUBLIC_HOST],
    onerror: (error) => console.log(JSON.stringify({ event: "mcp_error", name: error?.name ?? "Error" })),
  });
  return handler.fetch(request, { authInfo });
}
