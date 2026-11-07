// Per-request CPU of the real Worker bundle in real workerd (AFC-617).
//
// Node cannot say what a request costs on Workers:  workerd has no eval, so zod runs without its JIT
// fast path, and the timers and crypto differ.  This bundles src/index.js, runs it in Miniflare's
// workerd, sends sequential /mcp requests as one seat bearer, and reports the workerd process's own
// user+system CPU per request (read from /proc, so Linux only).  Synthetic relay answers stand in for
// git-relay, and an optional fake Infisical lets a run include the folder refresh.  No credential or
// real data is used or written.
//
// usage:  node scripts/bench-cpu.workerd.mjs [--cases a,b,c] [--requests 200] [--refresh]
//   cases:   ping, initialize, tools_list, identities, whoami, gr_wf_runs, gr_wf_run_jobs, gr_wf_checks, gr_wf_pr_get
//   --refresh  fallback-mode Infisical with the cache reset before every request, so each request pays
//              the folder refresh (the cost one isolate pays every INFISICAL_CACHE_TTL_S seconds)
//
// The worker's own console lines share stdout;  the results are the lines that start with RESULT.
// The floor of about 3 ms (the ping row) is Miniflare's own request handling, not the Worker's.
import { build } from "esbuild";
import { Miniflare, NoOpLog, convertV4MiniflareOptions } from "miniflare";
import { createHash, randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { testEnv, wranglerConfig } from "../test/helpers.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const arg = (name, fallback) => {
  const at = process.argv.indexOf(name);
  return at === -1 ? fallback : process.argv[at + 1];
};
const refresh = process.argv.includes("--refresh");
const cases = arg("--cases", "ping,identities,gr_wf_runs,gr_wf_run_jobs,gr_wf_checks,gr_wf_pr_get").split(",");
const requests = Number(arg("--requests", "200"));

// The Worker answers only its own configured host (hostAllowed, and the wrangler.jsonc var that must equal
// it), so a synthetic host would be refused with 404 before any code under test ran.  It is the host
// wrangler.jsonc and test/mcp-guards.workerd.mjs already publish;  nothing here is a credential.
const HOST = "https://agents.simplewithus.com";
const REPO = "Simple-With-Us/AI-Fleet-Coordinator";
const SHA = "a".repeat(40);

// ---- synthetic relay answers, shaped like the real ones (sizes from a live sample, Nov 7, 2026)
const words = (n) => "lorem ipsum dolor sit amet ".repeat(n).trim();
const user = { login: "swu-claude[bot]", id: 1234567, node_id: "BOT_kgDOAbcdef", type: "Bot", site_admin: false, url: "https://api.github.com/users/swu-claude%5Bbot%5D", html_url: "https://github.com/apps/swu-claude" };
const repoObject = { id: 987654321, name: "AI-Fleet-Coordinator", full_name: REPO, private: true, owner: user, default_branch: "main", visibility: "private", topics: [], license: null };
const RELAY = {
  wf_runs: { runs: Array.from({ length: 30 }, (_, i) => ({ id: 1000 + i, head_sha: SHA, status: "completed", conclusion: "success", created_at: "2026-11-07T20:00:00Z", event: "pull_request", head_branch: `claude/branch-${i}`, name: "ci", run_number: i + 1 })), complete: true },
  wf_run_jobs: { jobs: [{ id: 1, run_id: 1000, name: "test", status: "completed", conclusion: "success", steps: Array.from({ length: 8 }, (_, i) => ({ name: `step ${i}`, status: "completed", conclusion: "success", number: i + 1 })) }], complete: true },
  wf_checks: { checks: Array.from({ length: 30 }, (_, i) => ({ id: 5000 + i, name: `check ${i}`, status: "completed", conclusion: "success", completed_at: "2026-11-07T20:00:00Z", head_sha: SHA, app_id: 15368, app_slug: "github-actions" })), complete: true },
  wf_pr_get: { pull: { id: 111, number: 1446, state: "open", title: "Muster: row selection and badge words", body: words(500), user, labels: [], assignees: [], head: { sha: SHA, ref: "claude/x", repo: repoObject, user }, base: { sha: "b".repeat(40), ref: "main", repo: repoObject, user }, ...Object.fromEntries(Array.from({ length: 60 }, (_, i) => [`field_${i}`, `value ${i} ${words(8)}`])) } },
};
const ARGS = {
  gr_wf_runs: { repo: REPO }, gr_wf_run_jobs: { repo: REPO, run_id: 1000 }, gr_wf_checks: { repo: REPO, ref: SHA }, gr_wf_pr_get: { repo: REPO, number: 1446 }, identities: {}, whoami: {},
};

// ---- bundle with a hook that can reset the Infisical caches before a request
const dir = mkdtempSync(path.join(tmpdir(), "bench-cpu-"));
const entry = path.join(dir, "entry.js");
writeFileSync(entry, `import worker, { SeatGate } from ${JSON.stringify(path.join(root, "src/index.js"))};
import { _resetInfisicalForTests } from ${JSON.stringify(path.join(root, "src/infisical.js"))};
export { SeatGate };
export default { async fetch(request, env, ctx) { if (request.headers.get("x-bench-reset")) _resetInfisicalForTests(); return worker.fetch(request, env, ctx); } };
`);
const out = path.join(dir, "worker.mjs");
process.chdir(root);
await build({ entryPoints: [entry], bundle: true, format: "esm", platform: "browser", conditions: ["workerd", "worker", "browser"], external: ["cloudflare:workers", "node:*"], outfile: out, logLevel: "error" });
const script = readFileSync(out, "utf8");
const w = wranglerConfig();

const infisicalListing = (folder) => JSON.stringify({ secrets: Array.from({ length: 25 }, (_, i) => ({
  id: `id-${i}`, _id: `id-${i}`, workspace: "ws", environment: "prod", version: 3, type: "shared", secretKey: `KEY_${folder.slice(1).toUpperCase().replace(/-/g, "_")}_${i}`, secretValue: "v".repeat(48), secretComment: "",
  secretReminderNote: null, secretReminderRepeatDays: null, skipMultilineEncoding: false, createdAt: "2026-10-01T00:00:00.000Z", updatedAt: "2026-11-01T00:00:00.000Z",
  actor: { actorId: "a", actorType: "user", name: "n", membershipId: "m" }, tags: [{ id: "t", slug: "s", name: "n", color: "#fff" }], secretPath: folder, isRotatedSecret: false, rotationId: null, secretMetadata: [],
})), imports: [] });
const json = (body, status = 200) => new Response(typeof body === "string" ? body : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const options = convertV4MiniflareOptions({
  upstream: HOST,
  workers: [{
    name: "bench",
    modules: true,
    script,
    compatibilityDate: w.compatibility_date,
    compatibilityFlags: w.compatibility_flags,
    kvNamespaces: ["OAUTH_KV"],
    durableObjects: { SEAT_GATE: { className: "SeatGate", useSQLite: true } },
    ratelimits: Object.fromEntries(["MCP_RATE_LIMITER", "AUTH_FAIL_RATE_LIMITER", "TOKEN_RATE_LIMITER"].map((name, i) => [name, { namespace_id: String(i + 1), simple: { limit: 1_000_000, period: 60 } }])),
    bindings: testEnv({
      HOSTED_SEATS: "JET,GROK-WEB,CLAUDE", EVENTS_ONLY_SEATS: "JET", ZULIP_CLAUDE_API_KEY: "z".repeat(32),
      INFISICAL_MODE: refresh ? "fallback" : "off", INFISICAL_SERVICE_TOKEN: "st.fixture", INFISICAL_WORKSPACE_ID: "ws-fixture",
      RECALL_API_TOKEN: `rt_${"Q".repeat(40)}`, RECALL_ACCESS_CLIENT_ID: "0123456789abcdef0123456789abcdef.access", RECALL_ACCESS_CLIENT_SECRET: "f".repeat(64),
      GR_ACCESS_CLIENT_ID: "grid".padEnd(32, "a"), GR_ACCESS_CLIENT_SECRET: "grsecret".padEnd(64, "b"),
    }),
    outboundService: (request) => {
      const url = new URL(request.url);
      if (url.hostname === "app.infisical.com") return url.pathname === "/api/v3/secrets/raw" ? json(infisicalListing(url.searchParams.get("secretPath"))) : json({ message: "not found" }, 404);
      // users/me answers as the bot whose Basic credential asked, so the seat's own email check passes.
      if (url.pathname === "/api/v1/users/me") return json({ result: "success", msg: "", user_id: 4242, email: atob((request.headers.get("authorization") ?? "").replace(/^Basic /, "")).split(":")[0], full_name: "Bench", is_bot: true, is_admin: false, is_owner: false, role: 400 });
      const relay = /^\/v1\/(wf_[a-z_]+)$/.exec(url.pathname);
      if (relay && RELAY[relay[1]]) return json(RELAY[relay[1]]);
      return new Response("absent", { status: 404 });
    },
  }],
});
options.log = new NoOpLog();
const mf = new Miniflare(options);
await mf.ready;

const token = `sbt_claude_${randomBytes(32).toString("base64url")}`;
const namespace = await mf.getDurableObjectNamespace("SEAT_GATE", "bench");
await namespace.get(namespace.idFromName("CLAUDE")).gbBearerMint({ hash: createHash("sha256").update(token).digest("hex"), by: "bench", expiresAt: Date.now() + 3_600_000, mcpAccept: true });

let id = 0;
function frame(name) {
  if (name === "ping") return { method: "ping" };
  if (name === "tools_list") return { method: "tools/list", params: {} };
  if (name === "initialize") return { method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "bench", version: "1" } } };
  return { method: "tools/call", params: { name, arguments: ARGS[name] ?? {} } };
}
async function post(name) {
  const res = await mf.dispatchFetch(`${HOST}/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json, text/event-stream", "CF-Connecting-IP": "203.0.113.9", Authorization: `Bearer ${token}`, ...(refresh ? { "x-bench-reset": "1" } : {}) },
    body: JSON.stringify({ jsonrpc: "2.0", id: ++id, ...frame(name) }),
  });
  const text = await res.text();
  return { status: res.status, bytes: text.length, isError: text.includes('"isError":true') };
}

// workerd's own process CPU, from /proc (USER_HZ is 100 on Linux).
const workerdCpuMs = () => {
  let total = 0;
  for (const entry of readdirSync("/proc")) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const stat = readFileSync(`/proc/${entry}/stat`, "utf8");
      const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
      if (Number(fields[1]) === process.pid && /workerd/.test(stat.slice(stat.indexOf("(") + 1, stat.lastIndexOf(")")))) total += (Number(fields[11]) + Number(fields[12])) * 10;
    } catch { /* the process went away */ }
  }
  return total;
};

try {
  for (const name of cases) {
    const first = await post(name);
    for (let i = 0; i < 40; i++) await post(name);
    await new Promise((resolve) => setTimeout(resolve, 300));
    const before = workerdCpuMs();
    for (let i = 0; i < requests; i++) await post(name);
    await new Promise((resolve) => setTimeout(resolve, 100));
    const perRequest = (workerdCpuMs() - before) / requests;
    console.log(`RESULT ${name.padEnd(16)} ${perRequest.toFixed(2).padStart(6)} ms CPU per request  status ${first.status}  response ${String(first.bytes).padStart(6)} bytes${first.isError ? "  (an error result:  the fixture is wrong)" : ""}${refresh ? "  [Infisical refresh every request]" : ""}`);
  }
} finally {
  await mf.dispose();
  rmSync(dir, { recursive: true, force: true });
}
