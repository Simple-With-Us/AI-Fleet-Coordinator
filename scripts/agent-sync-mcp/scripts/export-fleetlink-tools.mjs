#!/usr/bin/env node
// Export the hosted fleetlink_* tool specs for the stdio server (scripts/agent_sync/mcp/fleetlink-tools.json).
// src/fleetlink.js is the source;  test/fleetlink.test.mjs fails when the committed JSON is stale.
//   node scripts/export-fleetlink-tools.mjs          write the file
//   node scripts/export-fleetlink-tools.mjs --check  exit 1 when it is stale
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { FLEETLINK_TOOLS } from "../src/fleetlink.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const FLEETLINK_TOOLS_JSON = path.resolve(HERE, "../../agent_sync/mcp/fleetlink-tools.json");

export function fleetlinkToolsText() {
  const out = {
    "//": "The fleetlink_* tools, one contract for the stdio server and the hosted Worker.  Generated from src/fleetlink.js FLEETLINK_TOOLS (test/fleetlink.test.mjs fails when this file differs);  regenerate with: cd scripts/agent-sync-mcp && node scripts/export-fleetlink-tools.mjs",
    tools: FLEETLINK_TOOLS,
  };
  return `${JSON.stringify(out, null, 2)}\n`;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const text = fleetlinkToolsText();
  let current = null;
  try { current = readFileSync(FLEETLINK_TOOLS_JSON, "utf8"); } catch { current = null; }
  if (process.argv.includes("--check")) {
    if (current !== text) { console.error("fleetlink-tools.json is stale.  Run:  cd scripts/agent-sync-mcp && node scripts/export-fleetlink-tools.mjs"); process.exit(1); }
  } else if (current !== text) {
    writeFileSync(FLEETLINK_TOOLS_JSON, text);
    console.log("wrote fleetlink-tools.json");
  }
}
