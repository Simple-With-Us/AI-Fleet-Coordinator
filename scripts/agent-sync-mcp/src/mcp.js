// The MCP endpoint:  `createMcpHandler` (agents) over a low-level SDK v2
// `Server`.  `tools/list` serves the stdio server's own tools.json, and
// `tools/call` runs the hosted tools, which validate arguments themselves:
// `McpServer` would answer a schema failure with JSON-RPC -32602, and the
// contract makes it a tool error (`invalid_argument`) the model can fix
// (spec 1, error model).  An unknown tool name stays a protocol error.

import { createMcpHandler } from "agents/mcp/server";
import { Server, ProtocolError, ProtocolErrorCode } from "@modelcontextprotocol/server";
import { TOOLS, TOOLS_BY_NAME, instructionsFor } from "./contract.js";
import { UnknownTool } from "./hosted-tools.js";
import { PUBLIC_HOST } from "./config.js";

export const SERVER_VERSION = "2.0.0-phase2";

/** One Server per request, bound to that request's seat and tools. */
export function buildServer(tools) {
  const server = new Server(
    { name: "agent-sync", version: SERVER_VERSION },
    { capabilities: { tools: {} }, instructions: instructionsFor(tools.seat) },
  );
  server.setRequestHandler("tools/list", async () => ({ tools: TOOLS.tools }));
  server.setRequestHandler("tools/call", async (request) => {
    const name = request.params?.name;
    let result;
    try {
      result = await tools.call(name, request.params?.arguments);
    } catch (error) {
      if (error instanceof UnknownTool) throw new ProtocolError(ProtocolErrorCode.InvalidParams, "Unknown tool");
      throw error;
    }
    return server.projectCallToolResult(result, result.isError ? undefined : TOOLS_BY_NAME[name]?.outputSchema);
  });
  return server;
}

/** Serve one /mcp request for `tools` (the Worker checked the grant first). */
export function serveMcpRequest(request, tools, authInfo) {
  const handler = createMcpHandler(() => buildServer(tools), {
    route: "/mcp",
    allowedHostnames: [PUBLIC_HOST],
    onerror: (error) => console.log(JSON.stringify({ event: "mcp_error", name: error?.name ?? "Error" })),
  });
  return handler.fetch(request, { authInfo });
}
