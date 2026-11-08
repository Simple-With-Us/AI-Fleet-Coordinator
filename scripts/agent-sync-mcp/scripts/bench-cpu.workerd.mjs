// Per-request CPU of the real Worker bundle in real workerd (AFC-617).
//
// Node cannot say what a request costs on Workers:  workerd has no eval, so zod runs without its JIT
// fast path, and the timers and crypto differ.  This bundles src/index.js, runs it in Miniflare's
// workerd, sends sequential /mcp requests as one seat bearer, and reports the workerd process's own
// user+system CPU per request (read from /proc, so Linux only).  Synthetic relay answers stand in for
// git-relay, and an optional fake Infisical lets a run include the folder refresh.  No credential or
// real data is used or written.
//
// usage:  node scripts/bench-cpu.workerd.mjs [--cases a,b,c] [--requests 200] [--route claude|gb|ma]
//                                            [--infisical off|warm|cold] [--listing 25] [--refresh]
//   cases:   ping, initialize, tools_list, identities, whoami, gr_wf_runs, gr_wf_run_jobs, gr_wf_checks, gr_wf_pr_get,
//            zulip_read_topic, zulip_search, zulip_inbox, operator_inbox_pending
//   --route  who calls:  claude (an sbt_ seat bearer, the default), gb (a Grok Bot's gbb_ bearer) or ma (an
//            unhosted sbt_ seat that reads its own /zulip records)
//   --infisical  off (env bindings only, the default for claude), warm (production's fallback mode with the cache
//            warm, the default for gb and ma) or cold (the cache reset before every request, so each request pays
//            the folder refresh, the cost one isolate pays every INFISICAL_CACHE_TTL_S seconds;  --refresh is the same)
//   --listing  secrets per Infisical folder in the fake listing (at least the names a route reads)
//   --cold-start [--isolates 8]  report the first request of a brand-new isolate (a fresh Miniflare per sample, each
//            thread's CPU read from schedstat in nanoseconds) instead of the warm per-request mean;  this is where
//            the CPU of a rarely used seat goes
//   --minify  bundle with esbuild's minify and kept names, the way `wrangler deploy` can
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
import { GB_ENABLED_IDENTITIES } from "../src/infisical-manifest.js";
import { SEAT_SECRETS } from "../src/config.js";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const arg = (name, fallback) => {
  const at = process.argv.indexOf(name);
  return at === -1 ? fallback : process.argv[at + 1];
};
const route = arg("--route", "claude");
const infisical = arg("--infisical", process.argv.includes("--refresh") ? "cold" : route === "claude" ? "off" : "warm");
const refresh = infisical === "cold";
const listingSize = Number(arg("--listing", "25"));
const coldStart = process.argv.includes("--cold-start");
const coldSamples = Number(arg("--isolates", "8"));
const cases = arg("--cases", "ping,identities,gr_wf_runs,gr_wf_run_jobs,gr_wf_checks,gr_wf_pr_get").split(",");
const requests = Number(arg("--requests", "200"));
const ROUTES = {
  claude: { gate: "CLAUDE", token: () => `sbt_claude_${randomBytes(32).toString("base64url")}` },
  gb: { gate: "GB-DESIGNER", token: () => `gbb_designer_${randomBytes(32).toString("base64url")}` },
  ma: { gate: "MA", token: () => `sbt_ma_${randomBytes(32).toString("base64url")}` },
};
if (!ROUTES[route]) throw new Error(`--route must be one of ${Object.keys(ROUTES).join(", ")}`);

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
const CHANNEL = "work-in-progress";
const ARGS = {
  gr_wf_runs: { repo: REPO }, gr_wf_run_jobs: { repo: REPO, run_id: 1000 }, gr_wf_checks: { repo: REPO, ref: SHA }, gr_wf_pr_get: { repo: REPO, number: 1446 }, identities: {}, whoami: {},
  zulip_read_topic: { channel: CHANNEL, topic: "AFC-617 bench", limit: 30 }, zulip_search: { query: "bench", limit: 30 }, zulip_inbox: { limit: 30 }, operator_inbox_pending: {},
};

// ---- synthetic Zulip answers, shaped like the real ones (field names from the Zulip API;  sizes are a guess,
// a realm of 90 people and bots, 120 subscribed channels, 30 messages of about 900 characters each)
const MEMBERS = Array.from({ length: 90 }, (_, i) => ({ user_id: 1000 + i, email: `member${i}@simplewithus.zulipchat.com`, delivery_email: null, full_name: `Member ${i}`, is_bot: i % 2 === 0, is_admin: i === 0, is_owner: i === 0, is_guest: false, is_active: true, role: i === 0 ? 100 : 400, timezone: "America/Chicago", date_joined: "2026-09-01T00:00:00.000000+00:00", avatar_url: `https://secure.gravatar.com/avatar/${"0".repeat(32)}?d=identicon&version=1`, avatar_version: 1, bot_type: i % 2 === 0 ? 1 : null, bot_owner_id: i % 2 === 0 ? 1000 : null, profile_data: {} }));
const SUBSCRIPTIONS = Array.from({ length: 120 }, (_, i) => ({ stream_id: i === 0 ? 900001 : 800000 + i, name: i === 0 ? CHANNEL : `channel-${i}`, description: words(6), rendered_description: `<p>${words(6)}</p>`, invite_only: false, is_web_public: false, color: "#76ce90", pin_to_top: false, in_home_view: true, audible_notifications: null, desktop_notifications: null, wildcard_mentions_notify: null, is_muted: false, date_created: 1790000000, creator_id: 1000, stream_post_policy: 1, message_retention_days: null, first_message_id: 1, history_public_to_subscribers: true, is_announcement_only: false, can_remove_subscribers_group: 2 }));
const messageRow = (i, extra = {}) => ({ id: 500000 + i, sender_id: 1000 + (i % 90), sender_full_name: `Member ${i % 90}`, sender_email: `member${i % 90}@simplewithus.zulipchat.com`, sender_realm_str: "simplewithus", type: "stream", stream_id: 900001, display_recipient: CHANNEL, subject: "AFC-617 bench", topic: "AFC-617 bench", content: `**Update ${i}**:  ${words(36)}\n\n- ${words(6)}\n- ${words(6)}\n\n\`\`\`\nnpm test\n\`\`\``, content_type: "text/x-markdown", timestamp: 1794000000 + i * 60, client: "website", flags: ["read"], reactions: [], recipient_id: 77, avatar_url: null, is_me_message: false, edit_history: [], ...extra });
const messagesAnswer = (url) => {
  const params = url.searchParams;
  const count = Math.max(Number(params.get("num_before") ?? 0), Number(params.get("num_after") ?? 0), 1);
  const narrow = params.get("narrow") ?? "";
  const direct = narrow.includes('"dm"');
  const rows = Array.from({ length: Math.min(count, 30) }, (_, i) => direct
    ? messageRow(i, { type: "private", stream_id: undefined, display_recipient: [{ id: 1000, email: "member0@simplewithus.zulipchat.com", full_name: "Member 0" }, { id: 4242, email: "bench@simplewithus.zulipchat.com", full_name: "Bench" }] })
    : messageRow(i));
  return { result: "success", msg: "", messages: rows, anchor: rows.at(-1)?.id ?? 0, found_anchor: true, found_newest: true, found_oldest: false, history_limited: false };
};
const listenerAnswer = (seat) => ({ contract: "agent-sync-read/1", seat, record: "pending-inbox", view: { items: Array.from({ length: 5 }, (_, i) => ({ seq: 40 + i, kind: "mention", channel: CHANNEL, topic: "AFC-617 bench", sender: "owner", preview: words(8) })), pending: 5, next_seq: 45, after_seq: 39, latest_seq: 45, inbox_pending: 5 } });

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
// --minify bundles the way `wrangler deploy` does with `minify: true` (names kept, as wrangler keeps them).
const minify = process.argv.includes("--minify");
await build({ entryPoints: [entry], bundle: true, format: "esm", platform: "browser", conditions: ["workerd", "worker", "browser"], external: ["cloudflare:workers", "node:*"], outfile: out, logLevel: "error", ...(minify ? { minify: true, keepNames: true } : {}) });
const script = readFileSync(out, "utf8");
console.log(`RESULT bundle ${String(script.length).padStart(8)} bytes${minify ? " (minified, names kept)" : ""}`);
const w = wranglerConfig();

// The names a route reads, by folder;  the rest of each listing is filler, so a refresh parses a realistic count.
const GB_ROLES = GB_ENABLED_IDENTITIES.map((identity) => identity.slice(3).replaceAll("-", "_"));
const NAMED = {
  "/zulip": {
    ...Object.fromEntries(GB_ROLES.flatMap((r) => [[`ZULIP_GB_${r}_API_KEY`, "g".repeat(32)], [`ZULIP_GB_${r}_EMAIL`, `${r.toLowerCase()}-grok-bot@simplewithus.zulipchat.com`]])),
    MA_ZULIP_KEY: "m".repeat(32), MA_ZULIP_EMAIL: "muse-assist-bot@simplewithus.zulipchat.com",
    [SEAT_SECRETS.CLAUDE.infisical.name]: "z".repeat(32),
  },
  "/linear": Object.fromEntries(GB_ROLES.flatMap((r) => [[`LINEAR_GB_${r}_CLIENT_ID`, "l".repeat(24)], [`LINEAR_GB_${r}_CLIENT_SECRET`, "s".repeat(40)]])),
  "/wake": { WAKE_HMAC_KEY_MA: "w".repeat(64) },
};
const infisicalListing = (folder) => {
  const named = Object.entries(NAMED[folder] ?? {});
  const filler = Array.from({ length: Math.max(0, listingSize - named.length) }, (_, i) => [`KEY_${folder.slice(1).toUpperCase().replace(/-/g, "_")}_${i}`, "v".repeat(48)]);
  return JSON.stringify({ secrets: [...named, ...filler].map(([secretKey, secretValue], i) => ({
    id: `id-${i}`, _id: `id-${i}`, workspace: "ws", environment: "prod", version: 3, type: "shared", secretKey, secretValue, secretComment: "",
    secretReminderNote: null, secretReminderRepeatDays: null, skipMultilineEncoding: false, createdAt: "2026-10-01T00:00:00.000Z", updatedAt: "2026-11-01T00:00:00.000Z",
    actor: { actorId: "a", actorType: "user", name: "n", membershipId: "m" }, tags: [{ id: "t", slug: "s", name: "n", color: "#fff" }], secretPath: folder, isRotatedSecret: false, rotationId: null, secretMetadata: [],
  })), imports: [] });
};
const json = (body, status = 200) => new Response(typeof body === "string" ? body : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const makeOptions = () => convertV4MiniflareOptions({
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
      INFISICAL_MODE: infisical === "off" ? "off" : "fallback", INFISICAL_SERVICE_TOKEN: "st.fixture", INFISICAL_WORKSPACE_ID: "ws-fixture",
      LISTENER_API_URL: "https://listener.example.test",
      RECALL_API_TOKEN: `rt_${"Q".repeat(40)}`, RECALL_ACCESS_CLIENT_ID: "0123456789abcdef0123456789abcdef.access", RECALL_ACCESS_CLIENT_SECRET: "f".repeat(64),
      GR_ACCESS_CLIENT_ID: "grid".padEnd(32, "a"), GR_ACCESS_CLIENT_SECRET: "grsecret".padEnd(64, "b"),
    }),
    outboundService: (request) => {
      const url = new URL(request.url);
      if (url.hostname === "app.infisical.com") {
        if (url.pathname === "/api/v3/secrets/raw") return json(infisicalListing(url.searchParams.get("secretPath")));
        // One named read (a folder the route does not list first, such as /wake on the seat-bearer route).
        const named = /^\/api\/v3\/secrets\/raw\/([^/]+)$/.exec(url.pathname);
        const found = named ? NAMED[url.searchParams.get("secretPath")]?.[decodeURIComponent(named[1])] : undefined;
        return found ? json({ secret: { secretKey: decodeURIComponent(named[1]), secretValue: found } }) : json({ message: "not found" }, 404);
      }
      if (url.hostname === "listener.example.test") return json(listenerAnswer(/^\/listener\/v1\/([^/]+)\//.exec(url.pathname)?.[1] ?? "MA"));
      if (url.pathname === "/api/v1/users") return json({ result: "success", msg: "", members: MEMBERS });
      if (url.pathname === "/api/v1/users/me/subscriptions") return json({ result: "success", msg: "", subscriptions: SUBSCRIPTIONS });
      if (url.pathname === "/api/v1/messages") return json(messagesAnswer(url));
      // users/me answers as the bot whose Basic credential asked, so the seat's own email check passes.
      if (url.pathname === "/api/v1/users/me") return json({ result: "success", msg: "", user_id: 4242, email: atob((request.headers.get("authorization") ?? "").replace(/^Basic /, "")).split(":")[0], full_name: "Bench", is_bot: true, is_admin: false, is_owner: false, role: 400 });
      const relay = /^\/v1\/(wf_[a-z_]+)$/.exec(url.pathname);
      if (relay && RELAY[relay[1]]) return json(RELAY[relay[1]]);
      return new Response("absent", { status: 404 });
    },
  }],
});

// A running Worker with one minted bearer for the chosen route.
async function start() {
  const options = makeOptions();
  options.log = new NoOpLog();
  const mf = new Miniflare(options);
  await mf.ready;
  const token = ROUTES[route].token();
  const namespace = await mf.getDurableObjectNamespace("SEAT_GATE", "bench");
  await namespace.get(namespace.idFromName(ROUTES[route].gate)).gbBearerMint({ hash: createHash("sha256").update(token).digest("hex"), by: "bench", expiresAt: Date.now() + 3_600_000, mcpAccept: true });
  return { mf, token };
}

let id = 0;
function frame(name) {
  if (name === "ping") return { method: "ping" };
  if (name === "tools_list") return { method: "tools/list", params: {} };
  if (name === "initialize") return { method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "bench", version: "1" } } };
  return { method: "tools/call", params: { name, arguments: ARGS[name] ?? {} } };
}
async function post({ mf, token }, name) {
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

// The same from each thread's schedstat, in nanoseconds:  the tick count above is only good to 10 ms, which is
// fine averaged over hundreds of requests and useless for one.
const workerdCpuNs = () => {
  let total = 0;
  for (const entry of readdirSync("/proc")) {
    if (!/^\d+$/.test(entry)) continue;
    try {
      const stat = readFileSync(`/proc/${entry}/stat`, "utf8");
      const fields = stat.slice(stat.lastIndexOf(")") + 2).split(" ");
      if (Number(fields[1]) !== process.pid || !/workerd/.test(stat.slice(stat.indexOf("(") + 1, stat.lastIndexOf(")")))) continue;
      for (const task of readdirSync(`/proc/${entry}/task`)) total += Number(readFileSync(`/proc/${entry}/task/${task}/schedstat`, "utf8").split(" ")[0]);
    } catch { /* the process went away */ }
  }
  return total;
};
const settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const median = (xs) => [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)];
const tag = `[Infisical ${infisical}${infisical === "off" ? "" : `, ${listingSize} per folder`}]`;

try {
  if (coldStart) {
    // The first request a brand-new isolate serves:  module evaluation, lazy schema builds, every cache empty.
    for (const name of cases) {
      const samples = [];
      let first;
      for (let i = 0; i < coldSamples; i++) {
        const running = await start();
        await settle(400);
        const before = workerdCpuNs();
        const result = await post(running, name);
        await settle(150);
        samples.push((workerdCpuNs() - before) / 1e6);
        first ??= result;
        await running.mf.dispose();
      }
      console.log(`RESULT ${route.padEnd(6)} ${name.padEnd(24)} first request of a new isolate: median ${median(samples).toFixed(1)} ms, min ${Math.min(...samples).toFixed(1)}, max ${Math.max(...samples).toFixed(1)} (${coldSamples} isolates)  status ${first.status}${first.isError ? "  (an error result:  the fixture is wrong)" : ""}  ${tag}`);
    }
  } else {
    const running = await start();
    try {
      for (const name of cases) {
        const first = await post(running, name);
        for (let i = 0; i < 40; i++) await post(running, name);
        await settle(300);
        const before = workerdCpuMs();
        for (let i = 0; i < requests; i++) await post(running, name);
        await settle(100);
        const perRequest = (workerdCpuMs() - before) / requests;
        console.log(`RESULT ${route.padEnd(6)} ${name.padEnd(24)} ${perRequest.toFixed(2).padStart(6)} ms CPU per request  status ${first.status}  response ${String(first.bytes).padStart(6)} bytes${first.isError ? "  (an error result:  the fixture is wrong)" : ""}  ${tag}`);
      }
    } finally {
      await running.mf.dispose();
    }
  }
} finally {
  rmSync(dir, { recursive: true, force: true });
}
