// The board CLI (scripts/linear-board/board) reads the hosted server's Linear
// catalogs as JSON from scripts/linear-board/catalog/.  This pins those files to
// a fresh export of the JavaScript they come from, so a change to a named
// command, an official MCP tool or the public-input allowlist fails CI until the
// JSON is regenerated and committed with it:
//
//   cd scripts/agent-sync-mcp && node scripts/export-linear-catalog.mjs
//
// The board side replays testdata/agent-sync-fixtures.json (also pinned here)
// against its Python ports in scripts/linear-board/test_board_parity.py.

import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";

import { buildCatalogFiles, BOARD_DIR, stableStringify } from "../scripts/export-linear-catalog.mjs";
import { LINEAR_NAMED_COMMANDS } from "../src/linear-named-specs.js";
import { LINEAR_MCP_TOOLS } from "../src/linear-mcp-specs.js";

const files = buildCatalogFiles();

for (const [relative, text] of Object.entries(files)) {
  test(`scripts/linear-board/${relative} equals a fresh export`, () => {
    let committed;
    try { committed = readFileSync(path.join(BOARD_DIR, relative), "utf8"); }
    catch { assert.fail(`${relative} is missing.  Run:  cd scripts/agent-sync-mcp && node scripts/export-linear-catalog.mjs`); }
    // One equality, not a deep diff:  a stale file names the regenerate command.
    assert.ok(committed === text, `${relative} is stale.  Run:  cd scripts/agent-sync-mcp && node scripts/export-linear-catalog.mjs`);
  });
}

test("the export is deterministic and ends with a newline", () => {
  const again = buildCatalogFiles();
  for (const [relative, text] of Object.entries(files)) {
    assert.equal(again[relative], text, relative);
    assert.ok(text.endsWith("}\n"), relative);
  }
  assert.equal(stableStringify({ b: 1, a: [{ d: 1, c: 2 }] }), '{\n  "a": [\n    {\n      "c": 2,\n      "d": 1\n    }\n  ],\n  "b": 1\n}\n');
});

test("the exported catalogs list exactly what the server serves, in served order", () => {
  const named = JSON.parse(files["catalog/named-commands.json"]);
  assert.deepEqual(named.commands.map((c) => c.name), LINEAR_NAMED_COMMANDS.map((c) => c.name));
  assert.equal(named.count, LINEAR_NAMED_COMMANDS.length);
  for (const [i, command] of named.commands.entries()) {
    const served = LINEAR_NAMED_COMMANDS[i];
    assert.equal(command.kind, served.write ? "write" : "read", command.name);
    // Argument order decides the variable order of the built document.
    assert.deepEqual(command.args.map((a) => a.name), Object.keys(served.args), command.name);
    assert.equal(Boolean(command.variants), Boolean(served.variants), command.name);
  }
  const official = JSON.parse(files["catalog/official-mcp-tools.json"]);
  assert.deepEqual(official.tools.map((t) => t.name), LINEAR_MCP_TOOLS.map((t) => t.name));
  for (const tool of official.tools) assert.equal(tool.name, `${official.prefix}${tool.upstream_name}`);
  const inputs = JSON.parse(files["catalog/public-inputs.json"]);
  assert.match(inputs.official_schema_sha256, /^[0-9a-f]{64}$/);
  assert.ok(Object.keys(inputs.public).length > 100);
});
