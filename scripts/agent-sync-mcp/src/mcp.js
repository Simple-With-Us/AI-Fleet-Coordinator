// The MCP endpoint:  `createMcpHandler` (agents) over an SDK v2 `McpServer`
// whose tools are registered from checked-in JSON Schemas (the Phase 0 spike
// in spec section 6:  JSON schemas through `fromJsonSchema`, validated with
// the cf-worker validator, since Ajv's code generation is blocked in Workers).

import { createMcpHandler } from "agents/mcp/server";
import { McpServer, fromJsonSchema } from "@modelcontextprotocol/server";
import { CfWorkerJsonSchemaValidator } from "@modelcontextprotocol/server/validators/cf-worker";
import TOOLS from "./tools.phase0.json" with { type: "json" };
import { PUBLIC_HOST } from "./config.js";
import { HANDLERS, instructionsFor, callerFrom } from "./tool-logic.js";

const validator = new CfWorkerJsonSchemaValidator();

export const TOOL_SCHEMAS = TOOLS;

function buildServer({ authInfo }) {
  const caller = callerFrom(authInfo);
  const server = new McpServer(
    { name: "agent-sync", version: "0.0.0-phase0" },
    { instructions: instructionsFor(caller.seat), jsonSchemaValidator: validator },
  );
  for (const tool of TOOLS) {
    const handler = HANDLERS[tool.name];
    server.registerTool(
      tool.name,
      {
        title: tool.title,
        description: tool.description,
        annotations: tool.annotations,
        inputSchema: fromJsonSchema(tool.inputSchema, validator),
        outputSchema: fromJsonSchema(tool.outputSchema, validator),
      },
      // The seat comes from the verified grant passed in by the Worker, never
      // from the tool arguments (which the schema already limits).
      async (args) => handler(args, authInfo),
    );
  }
  return server;
}

export const mcpHandler = createMcpHandler(buildServer, {
  route: "/mcp",
  allowedHostnames: [PUBLIC_HOST],
  onerror: (error) => console.log(JSON.stringify({ event: "mcp_error", name: error?.name ?? "Error" })),
});
