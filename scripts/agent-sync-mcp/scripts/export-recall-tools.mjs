#!/usr/bin/env node
// Export the hosted recall_* tool specs for the stdio server (scripts/agent_sync/mcp/recall-tools.json).
// src/recall.js is the source;  test/recall.test.mjs fails when the committed JSON is stale.
//   node scripts/export-recall-tools.mjs          write the file
//   node scripts/export-recall-tools.mjs --check  exit 1 when it is stale
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { RECALL_TOOLS } from "../src/recall.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const RECALL_TOOLS_JSON = path.resolve(HERE, "../../agent_sync/mcp/recall-tools.json");

export function recallToolsText() {
  const out = {
    "//": "AFC-331:  the recall_* tools, one contract for the stdio server and the hosted Worker.  Generated from src/recall.js RECALL_TOOLS (test/recall.test.mjs fails when this file differs);  regenerate with: cd scripts/agent-sync-mcp && node scripts/export-recall-tools.mjs",
    tools: RECALL_TOOLS,
  };
  return `${JSON.stringify(out, null, 2)}\n`;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const text = recallToolsText();
  let current = null;
  try { current = readFileSync(RECALL_TOOLS_JSON, "utf8"); } catch { current = null; }
  if (process.argv.includes("--check")) {
    if (current !== text) { console.error("recall-tools.json is stale.  Run:  cd scripts/agent-sync-mcp && node scripts/export-recall-tools.mjs"); process.exit(1); }
  } else if (current !== text) {
    writeFileSync(RECALL_TOOLS_JSON, text);
    console.log("wrote recall-tools.json");
  }
}
