#!/usr/bin/env node
// Export the hosted Agent-Sync Linear catalogs for the `board` CLI.
//
// The hosted server's named Linear commands, its proxied official Linear MCP
// tools and its public-input allowlist live in JavaScript (src/).  The board
// CLI (scripts/linear-board/board) is stdlib Python, so it reads the same
// catalogs as JSON from scripts/linear-board/catalog/.  This script is the only
// writer of those files, and test/linear-catalog-parity.test.mjs fails when the
// committed JSON differs from a fresh export, so the two sides change together.
//
// It also writes scripts/linear-board/testdata/agent-sync-fixtures.json:  the
// documents the hosted server builds for every named command, and its verdicts
// on a set of GraphQL guard and public-input cases.  The board tests replay
// them against the Python ports.
//
// Run from scripts/agent-sync-mcp:
//   node scripts/export-linear-catalog.mjs          write the files
//   node scripts/export-linear-catalog.mjs --check  exit 1 when a file is stale
//
// Deterministic:  no timestamps, object keys sorted, arrays in served order,
// two-space indent and a trailing newline.

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { LINEAR_NAMED_COMMANDS, LINEAR_NAMED_TOOLS } from "../src/linear-named-specs.js";
import { buildNamedOperation } from "../src/linear-named.js";
import { LINEAR_MCP_TOOLS, LINEAR_MCP_BY_NAME } from "../src/linear-mcp-specs.js";
import { LINEAR_MCP_URL, LINEAR_MCP_PROTOCOL_VERSION } from "../src/linear-mcp-client.js";
import { PUBLIC_INPUT_FIELDS, INTERNAL_INPUT_FIELDS, assertPublicInputs } from "../src/linear-public-inputs.js";
import { prepareGraphql } from "../src/linear-graphql.js";
import { LINEAR_TOOLS, LINEAR_WRITE_TOOLS } from "../src/linear.js";
import { withDefaults, schemaProblem } from "../src/contract.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const PACKAGE = path.dirname(HERE);
const BOARD_DIR = path.resolve(PACKAGE, "../linear-board");
const GENERATOR = "scripts/agent-sync-mcp/scripts/export-linear-catalog.mjs";
const REGENERATE = "cd scripts/agent-sync-mcp && node scripts/export-linear-catalog.mjs";

/** JSON with every object's keys sorted, so the bytes depend only on the data. */
export function stableStringify(value) {
  const sort = (v) => {
    if (Array.isArray(v)) return v.map(sort);
    if (v && typeof v === "object") {
      return Object.fromEntries(Object.keys(v).sort().filter((k) => v[k] !== undefined).map((k) => [k, sort(v[k])]));
    }
    return v;
  };
  return `${JSON.stringify(sort(value), null, 2)}\n`;
}

/** Args as an ordered list:  the hosted document names variables in this order. */
function argList(args) {
  return Object.entries(args).map(([name, arg]) => ({ name, ...arg }));
}

function commandShape(command) {
  return {
    name: command.name,
    title: command.title,
    description: command.description,
    root: command.root,
    args: argList(command.args),
    selection: command.selection ?? null,
    destructive: Boolean(command.destructive),
  };
}

function namedCatalog() {
  const tools = Object.fromEntries(LINEAR_NAMED_TOOLS.map((tool) => [tool.name, tool]));
  const commands = LINEAR_NAMED_COMMANDS.map((command) => {
    const tool = tools[command.name];
    return {
      ...commandShape(command),
      kind: command.write ? "write" : "read",
      tool_description: tool.description,
      input_schema: tool.inputSchema,
      annotations: tool.annotations,
      ...(command.variants ? { variants: { create: commandShape(command.variants.create), update: commandShape(command.variants.update) } } : {}),
    };
  });
  return {
    generated_by: GENERATOR,
    regenerate: REGENERATE,
    sources: ["src/linear-read-catalog.js", "src/linear-write-catalog.js", "src/linear-named-specs.js"],
    count: commands.length,
    commands,
  };
}

// The ten Linear tools that are neither named commands nor proxied official tools:  the seven the
// server served first (linear_whoami, list, show, file, claim, comment, status) and the three general
// GraphQL tools (linear_schema, query, mutation).  The stdio server (scripts/agent_sync/mcp/linear_tools.py)
// serves them under the same names and schemas, through `board`.
const CORE_NAMES = ["linear_whoami", "linear_list", "linear_show", "linear_file", "linear_claim", "linear_comment",
  "linear_status", "linear_schema", "linear_query", "linear_mutation"];

function coreCatalog() {
  const by = Object.fromEntries(LINEAR_TOOLS.map((tool) => [tool.name, tool]));
  const tools = CORE_NAMES.map((name) => {
    const tool = by[name];
    return {
      name: tool.name,
      title: tool.title,
      description: tool.description,
      kind: LINEAR_WRITE_TOOLS.has(name) ? "write" : "read",
      input_schema: tool.inputSchema,
      ...(tool.outputSchema ? { output_schema: tool.outputSchema } : {}),
      annotations: tool.annotations,
    };
  });
  return {
    generated_by: GENERATOR,
    regenerate: REGENERATE,
    sources: ["src/linear.js", "src/linear-api.js"],
    count: tools.length,
    tools,
  };
}

function officialCatalog() {
  const tools = LINEAR_MCP_TOOLS.map((tool) => {
    const spec = LINEAR_MCP_BY_NAME[tool.name];
    return {
      name: spec.name,
      upstream_name: spec.upstreamName,
      title: spec.title,
      description: spec.description,
      kind: spec.write ? "write" : "read",
      input_schema: spec.inputSchema,
      annotations: spec.annotations,
    };
  });
  return {
    generated_by: GENERATOR,
    regenerate: REGENERATE,
    sources: ["src/linear-mcp-specs.js", "src/linear-mcp-client.js"],
    url: LINEAR_MCP_URL,
    protocol_version: LINEAR_MCP_PROTOCOL_VERSION,
    // A served tool name is the upstream name with this prefix;  the client
    // strips it before tools/call.  The four local fields never go upstream.
    prefix: "linear_",
    local_fields: ["idempotency_key", "max_chars", "session"],
    count: tools.length,
    tools,
  };
}

function publicInputs() {
  const source = readFileSync(path.join(PACKAGE, "src/linear-public-inputs.js"), "utf8");
  const sha = /Official schema SHA-256: ([0-9a-f]{64})/.exec(source)?.[1] ?? null;
  return {
    generated_by: GENERATOR,
    regenerate: REGENERATE,
    sources: ["src/linear-public-inputs.js"],
    official_schema_sha256: sha,
    public: PUBLIC_INPUT_FIELDS,
    internal: INTERNAL_INPUT_FIELDS,
  };
}

// ----------------------------------------------------------------- fixtures

function outcome(fn) {
  try { return { ok: true, value: fn() }; }
  catch (error) { return { ok: false, error: String(error?.message ?? error) }; }
}

function sampleValue(name, arg) {
  const schema = arg.schema ?? {};
  if (schema.type === "object") return {};
  if (schema.type === "boolean") return true;
  if (schema.type === "integer") return Math.min(Math.max(5, schema.minimum ?? 5), schema.maximum ?? 5);
  return name === "id" ? "id-1" : `${name}-value`;
}

function namedFixtures() {
  const out = [];
  const tools = Object.fromEntries(LINEAR_NAMED_TOOLS.map((tool) => [tool.name, tool]));
  for (const command of LINEAR_NAMED_COMMANDS) {
    const all = Object.fromEntries(Object.entries(command.args).map(([name, arg]) => [name, sampleValue(name, arg)]));
    const required = Object.fromEntries(Object.entries(command.args).filter(([, arg]) => arg.required).map(([name, arg]) => [name, sampleValue(name, arg)]));
    const cases = [["required", required], ["all", all]];
    if (command.variants) {
      const { id, ...create } = all;
      cases.push(["create", create]);
    }
    if (command.selection) cases.push(["selection", { ...required, selection: "id" }]);
    for (const [label, args] of cases) {
      const input = { ...args, ...(command.write ? { idempotency_key: "fixture-key" } : {}) };
      const problem = schemaProblem(tools[command.name].inputSchema, input);
      const filled = withDefaults(tools[command.name].inputSchema, input);
      const built = problem ? { ok: false, error: problem } : outcome(() => buildNamedOperation(command.name, filled));
      const row = { name: command.name, case: label, args: input, ok: built.ok };
      if (!built.ok) row.error = built.error;
      else {
        row.document = built.value.document;
        row.variables = built.value.variables;
        const guard = outcome(() => prepareGraphql(built.value.document, built.value.variables, command.write ? "mutation" : "query"));
        row.guard_ok = guard.ok;
        if (!guard.ok) row.guard_error = guard.error;
      }
      out.push(row);
    }
  }
  return out;
}

const deep = (n) => `query { ${"a { ".repeat(n)}id${" }".repeat(n)} }`;
const many = (n) => `query { ${Array.from({ length: n }, (_, i) => `f${i}`).join(" ")} }`;
const GUARD_CASES = [
  ["plain query", "query { viewer { id name } }", {}, "query"],
  ["shorthand query", "{ viewer { id } }", {}, "query"],
  ["named query with variables", "query Q($id: String!, $n: Int = 3) { issue(id: $id) { id comments(first: $n) { nodes { id } } } }", { id: "AFC-1" }, "query"],
  ["two operations", "query A { viewer { id } } query B { viewer { id } }", {}, "query"],
  ["query given as mutation", "query { viewer { id } }", {}, "mutation"],
  ["mutation given as query", "mutation { issueCreate(input: { title: \"x\" }) { success } }", {}, "query"],
  ["type definition", "type Foo { a: Int }", {}, "query"],
  ["syntax error", "query { viewer { id }", {}, "query"],
  ["empty document", "", {}, "query"],
  ["fragment cycle", "query { viewer { ...A } } fragment A on User { ...B } fragment B on User { ...A }", {}, "query"],
  ["unknown fragment", "query { viewer { ...Missing } }", {}, "query"],
  ["duplicate fragment names", "query { viewer { ...A } } fragment A on User { id } fragment A on User { name }", {}, "query"],
  ["unused fragment cycle", "query { viewer { id } } fragment A on User { ...A }", {}, "query"],
  ["depth sixteen", deep(15), {}, "query"],
  ["depth seventeen", deep(16), {}, "query"],
  ["five hundred fields", many(500), {}, "query"],
  ["five hundred one fields", many(501), {}, "query"],
  ["fragment expansion over the field limit", `query { ${"a: viewer { ...F } ".replace("a:", "x0:")}${Array.from({ length: 30 }, (_, i) => `x${i + 1}: viewer { ...F }`).join(" ")} } fragment F on User { ${Array.from({ length: 20 }, (_, i) => `g${i}`).join(" ")} }`, {}, "query"],
  ["comments and directives", "# a comment\nquery Q($skip: Boolean!) { viewer { id name @skip(if: $skip) } }", { skip: false }, "query"],
  ["block string and escapes", "mutation { commentCreate(input: { body: \"\"\"line one\n  \"quoted\" \\\"\"\"\n\"\"\", issueId: \"a\\u00e9\\n\" }) { success } }", {}, "mutation"],
  ["variables array", "query { viewer { id } }", [1], "query"],
  ["variables too deep", "query { viewer { id } }", { a: { b: { c: { d: { e: { f: { g: { h: { i: { j: { k: { l: { m: { n: { o: { p: { q: { r: 1 } } } } } } } } } } } } } } } } } }, "query"],
  ["document over the length limit", `query { viewer { id } }${" ".repeat(24000)}`, {}, "query"],
  ["too many tokens", `query { ${Array.from({ length: 4100 }, () => "a: b").join(" ")} }`, {}, "query"],
  ["mutation issueCreate", "mutation M($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id } } }", { input: { title: "t", teamId: "team" } }, "mutation"],
  ["mutation aliased root", "mutation { made: issueCreate(input: { title: \"t\", labelIds: [\"l1\", \"l2\"], priority: 2, estimate: 1.5, flag: true, none: null, kind: BUG }) { success } }", {}, "mutation"],
  ["mutation default variable", "mutation M($id: String = \"AFC-1\") { issueArchive(id: $id) { success } }", {}, "mutation"],
  ["mutation two roots", "mutation { a: issueArchive(id: \"1\") { success } b: issueArchive(id: \"2\") { success } }", {}, "mutation"],
  ["mutation roots through an inline fragment", "mutation { ... on Mutation { issueArchive(id: \"1\") { success } } }", {}, "mutation"],
  ["mutation roots through a fragment spread", "mutation { ...R } fragment R on Mutation { issueArchive(id: \"1\") { success } }", {}, "mutation"],
  ["blocked apiKey root", "mutation { apiKeyCreate(input: { label: \"x\" }) { success } }", {}, "mutation"],
  ["blocked organization root", "mutation { organizationUpdate(input: { name: \"x\" }) { success } }", {}, "mutation"],
  ["blocked user root", "mutation { userUpdate(id: \"me\", input: { name: \"x\" }) { success } }", {}, "mutation"],
  ["blocked webhook root", "mutation { webhookCreate(input: { url: \"https://x\" }) { success } }", {}, "mutation"],
  ["blocked integration root", "mutation { airbyteIntegrationConnect(input: {}) { success } }", {}, "mutation"],
  ["blocked teamMembership root", "mutation { teamMembershipCreate(input: {}) { success } }", {}, "mutation"],
  ["batch root", "mutation { issueBatchUpdate(ids: [\"1\"], input: {}) { success } }", {}, "mutation"],
  ["bulk root", "mutation { issueBulkMove(input: {}) { success } }", {}, "mutation"],
  ["subscription", "subscription { issueUpdated { id } }", {}, "query"],
  ["variables resolved into root arguments", "mutation M($input: IssueUpdateInput!, $id: String!) { issueUpdate(id: $id, input: $input) { success } }", { id: "AFC-2", input: { addedLabelIds: ["x"], description: "d" } }, "mutation"],
];

function guardFixtures() {
  return GUARD_CASES.map(([name, document, variables, expected]) => {
    const result = outcome(() => prepareGraphql(document, variables, expected));
    const row = { name, document, variables, expected, ok: result.ok };
    if (!result.ok) row.error = result.error;
    else {
      row.roots = result.value.roots.map((root) => root.name.value);
      row.operation = result.value.operation;
      if (expected === "mutation") row.args = result.value.args;
    }
    return row;
  });
}

function publicInputFixtures() {
  const byName = Object.fromEntries(LINEAR_NAMED_COMMANDS.map((command) => [command.name, command]));
  const hiddenCreate = INTERNAL_INPUT_FIELDS.IssueCreateInput?.[0];
  const hiddenFilter = INTERNAL_INPUT_FIELDS.IssueFilter?.[0];
  const cases = [
    ["create issue", "linear_create_issue", { input: { title: "t", teamId: "team", labelIds: ["a", "b"] } }],
    ["restricted create field", "linear_create_issue", { input: { title: "t", [hiddenCreate]: "x" } }],
    ["unknown create field", "linear_create_issue", { input: { title: "t", notAField: 1 } }],
    ["input not an object", "linear_create_issue", { input: "text" }],
    ["input array", "linear_create_issue", { input: [{ title: "t" }] }],
    ["nested filter", "linear_list_issues", { filter: { and: [{ title: { eq: "x" } }, { team: { key: { eq: "AFC" } } }] } }],
    ["restricted nested filter field", "linear_list_issues", { filter: { or: [{ [hiddenFilter]: { eq: 1 } }] } }],
    ["unknown nested filter field", "linear_list_issues", { filter: { team: { nope: true } } }],
    ["non-list coerced to a list", "linear_list_issues", { filter: { and: { title: { eq: "x" } } } }],
    ["null input value", "linear_list_issues", { filter: null }],
    ["scalar arguments only", "linear_get_issue", { id: "AFC-1" }],
  ];
  return cases.map(([name, command, values]) => {
    const result = outcome(() => assertPublicInputs(byName[command], values));
    return { name, command, values, ok: result.ok, ...(result.ok ? {} : { error: result.error }) };
  });
}

function schemaFixtures() {
  // The JSON Schema subset the hosted server checks arguments with.
  const tools = Object.fromEntries([...LINEAR_NAMED_TOOLS, ...LINEAR_MCP_TOOLS].map((tool) => [tool.name, tool]));
  const cases = [
    ["linear_list_issues", {}],
    ["linear_list_issues", { first: 0 }],
    ["linear_list_issues", { first: 101 }],
    ["linear_list_issues", { first: 2.5 }],
    ["linear_list_issues", { first: "5" }],
    ["linear_list_issues", { bogus: 1 }],
    ["linear_get_issue", {}],
    ["linear_get_issue", { id: "" }],
    ["linear_create_issue", { input: {} }],
    ["linear_create_issue", { input: {}, idempotency_key: "bad key!" }],
    ["linear_create_issue", { input: {}, idempotency_key: "ok-key.1:2_3" }],
    ["linear_create_issue", { input: {}, idempotency_key: "k", max_chars: 999 }],
    ["linear_get_diff", { urlOrId: "" }],
    ["linear_get_diff", { urlOrId: "AFC-1" }],
    ["linear_list_diffs", { orderBy: "sideways" }],
    ["linear_list_diffs", { limit: 251 }],
    ["linear_search_documentation", { query: "cycles", page: 1 }],
  ];
  return cases.map(([name, args]) => ({ name, args, problem: schemaProblem(tools[name].inputSchema, args), defaults: withDefaults(tools[name].inputSchema, args) }));
}

function fixtures() {
  return {
    generated_by: GENERATOR,
    regenerate: REGENERATE,
    named: namedFixtures(),
    guards: guardFixtures(),
    public_inputs: publicInputFixtures(),
    schema: schemaFixtures(),
  };
}

/** Every exported file:  path relative to scripts/linear-board -> exact text. */
export function buildCatalogFiles() {
  return {
    "catalog/named-commands.json": stableStringify(namedCatalog()),
    "catalog/official-mcp-tools.json": stableStringify(officialCatalog()),
    "catalog/core-tools.json": stableStringify(coreCatalog()),
    "catalog/public-inputs.json": stableStringify(publicInputs()),
    "testdata/agent-sync-fixtures.json": stableStringify(fixtures()),
  };
}

export { BOARD_DIR };

function main(argv) {
  const check = argv.includes("--check");
  const files = buildCatalogFiles();
  const stale = [];
  for (const [relative, text] of Object.entries(files)) {
    const target = path.join(BOARD_DIR, relative);
    let current = null;
    try { current = readFileSync(target, "utf8"); } catch { current = null; }
    if (current === text) continue;
    stale.push(relative);
    if (!check) {
      mkdirSync(path.dirname(target), { recursive: true });
      writeFileSync(target, text);
    }
  }
  if (check && stale.length) {
    console.error(`stale: ${stale.join(", ")}.  Regenerate with:  ${REGENERATE}`);
    return 1;
  }
  console.log(stale.length ? `${check ? "stale" : "wrote"}: ${stale.join(", ")}` : "catalog up to date");
  return 0;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
