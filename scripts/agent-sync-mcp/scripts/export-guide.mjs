#!/usr/bin/env node
// Bundle docs/protocols/simple-tools-guide.md into src/guide-text.js, so the Worker (and the node tests)
// import the guide as a plain JavaScript string.  test/guide.test.mjs fails when the committed module differs.
//   node scripts/export-guide.mjs          write src/guide-text.js
//   node scripts/export-guide.mjs --check  exit 1 when it is stale
import { readFileSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const GUIDE_MD = path.resolve(HERE, "../../../docs/protocols/simple-tools-guide.md");
export const GUIDE_MODULE = path.resolve(HERE, "../src/guide-text.js");

export function guideModuleText() {
  const md = readFileSync(GUIDE_MD, "utf8");
  return `// GENERATED from docs/protocols/simple-tools-guide.md by scripts/export-guide.mjs;  do not edit.\n// Regenerate with:  cd scripts/agent-sync-mcp && node scripts/export-guide.mjs\nexport const GUIDE_TEXT = ${JSON.stringify(md)};\n`;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const text = guideModuleText();
  let current = null;
  try { current = readFileSync(GUIDE_MODULE, "utf8"); } catch { current = null; }
  if (process.argv.includes("--check")) {
    if (current !== text) { console.error("src/guide-text.js is stale.  Run:  cd scripts/agent-sync-mcp && node scripts/export-guide.mjs"); process.exit(1); }
  } else if (current !== text) {
    writeFileSync(GUIDE_MODULE, text);
    console.log("wrote src/guide-text.js");
  }
}
