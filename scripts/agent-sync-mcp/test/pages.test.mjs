import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { STYLE, STYLE_HASH, PAGE_CSP, SENTENCE_GAP, REFERRER_POLICY, escapeHtml, ownerTime, consentPage, adminPage, errorPage, gapped, noConnectionPage, withReferrerPolicy } from "../src/pages.js";
import { sameOriginPost } from "../src/policy.js";
import { browserPostOrigin, ISSUER } from "./helpers.mjs";

test("the stylesheet hash in the CSP matches the stylesheet", () => {
  assert.equal(STYLE_HASH, `sha256-${createHash("sha256").update(STYLE, "utf8").digest("base64")}`);
  assert.match(PAGE_CSP, /^default-src 'none'; /);
  assert.match(PAGE_CSP, /frame-ancestors 'none'/);
  assert.doesNotMatch(PAGE_CSP, /script-src|unsafe-inline|form-action/);
});

test("escaping covers markup and attribute breakouts", () => {
  assert.equal(escapeHtml(`<script>"x"&'y'\``), "&#60;script&#62;&#34;x&#34;&#38;&#39;y&#39;&#96;");
});

test("owner clock:  12-hour, am or pm, Central, no zone abbreviation", () => {
  assert.equal(ownerTime(Date.parse("2026-10-09T08:15:00Z")), "Fri, Oct 9, 3:15am");
  assert.equal(ownerTime(Date.parse("2026-10-09T20:05:00Z")), "Fri, Oct 9, 3:05pm");
  assert.equal(ownerTime(NaN), "");
});

test("consent page escapes client-controlled strings and has no script", async () => {
  const res = consentPage({
    clientName: `<img src=x onerror=alert(1)>`,
    clientDomain: "chatgpt.com",
    clientId: "https://chatgpt.com/oauth/client.json",
    redirectHost: "chatgpt.com",
    scopes: ["zulip:read", "zulip:write"],
    seat: "JET",
    handle: `h"><b>`,
    replacing: [{ client: "Old <Client>", createdAt: Date.parse("2026-10-08T20:00:00Z") }],
    eventsOnly: false,
  }, new Headers({ "Set-Cookie": "__Host-oauth-consent-x=1; Path=/; Secure; HttpOnly" }));
  const html = await res.text();
  assert.ok(!html.includes("<img"));
  assert.ok(!html.includes("<script"));
  assert.ok(!html.includes(`h"><b>`));
  assert.match(html, /This replaces the grant from Old &#60;Client&#62;, created Thu, Oct 8, 3:00pm\./);
  assert.match(html, /name="seat" value="JET"/);
  assert.ok(html.includes(SENTENCE_GAP), "sentences keep the U+00A0 gap in HTML");
  assert.equal(res.headers.get("Content-Security-Policy"), PAGE_CSP);
  assert.equal(res.headers.get("X-Frame-Options"), "DENY");
  assert.match(res.headers.get("Set-Cookie"), /__Host-oauth-consent-x/);
});

test("arming notice and error pages", async () => {
  const nc = noConnectionPage();
  assert.equal(nc.status, 403);
  assert.match(await nc.text(), /No Connection Expected/);
  const err = errorPage("This Page Expired", "One.  Two.", 400);
  assert.ok((await err.text()).includes(`One.${SENTENCE_GAP}Two.`));
});

test("a quoted sentence opener keeps its gap after escaping", () => {
  // `escapeHtml` rewrites `"` and the backtick to `&#34;`/`&#96;`, so a lookahead
  // that runs after escaping never sees either admitted opener (Codex P2, fixed
  // 2026-10-20).  gapped() gaps the raw text first, as every peer surface does.
  assert.equal(gapped('Done. "Next"'), `Done.${SENTENCE_GAP}&#34;Next&#34;`);
  assert.equal(gapped("Done. `Next`"), `Done.${SENTENCE_GAP}&#96;Next&#96;`);
  // Still escaped exactly once, and still left alone where the lookahead says so:
  // `'` is not an admitted opener, and `e.g.` is not a sentence boundary.
  assert.equal(gapped("Done. 'Next'"), "Done. &#39;Next&#39;");
  // A sentence that ends right before a markdown link is a sentence end (AFC-539).
  assert.equal(gapped("Merged. [PR #314](https://example.com/pull/314)"), `Merged.${SENTENCE_GAP}[PR #314](https://example.com/pull/314)`);
  assert.equal(gapped("e.g. tools"), "e.g. tools");
});

test("admin page renders state and escapes refusal-log values", async () => {
  const res = adminPage(
    {
      email: "mail@jays.services",
      seats: [{ seat: "JET", epoch: 2, paused: false, armed: true, armedUntil: Date.parse("2026-10-09T08:25:00Z"), grants: [] }],
      clients: [],
      refusals: [{ ts: 0, where: "authorize", reason: "redirect_not_allowlisted", client_id: "<x>", redirect_uri: "https://grok.com/cb" }],
      audits: [],
      csrf: "c".repeat(64),
      notice: "Armed for 10 minutes.  Start the connection from the app now.",
      manualSeats: [{ seat: "ECHO", label: "Echo", redirectsConfigured: true, shared: true }, { seat: "GROK-WEB", label: "Grok", redirectsConfigured: false, shared: false }],
    },
    { "Set-Cookie": "__Host-agent-sync-csrf=x; Path=/; Secure; HttpOnly; SameSite=Strict" },
  );
  const html = await res.text();
  assert.match(html, /Armed until Fri, Oct 9, 3:25am/);
  assert.ok(html.includes("&#60;x&#62;"));
  assert.ok(html.includes("https://grok.com/cb"));
  assert.ok(!html.includes("<script"));
  assert.equal((html.match(/name="csrf"/g) ?? []).length >= 4, true);
});

const consentFacts = { clientName: "ChatGPT", clientDomain: "chatgpt.com", clientId: "https://chatgpt.com/oauth/client.json", redirectHost: "chatgpt.com", scopes: ["zulip:read"], seat: "JET", handle: "h", replacing: [], eventsOnly: false };
const adminFacts = { email: "mail@jays.services", seats: [], clients: [], refusals: [], audits: [], csrf: "c".repeat(64), notice: "", manualSeats: [] };

test("P1:  every page that posts a form keeps Origin, so Approve and Arm work in a real browser", async () => {
  // Before the fix every page sent Referrer-Policy: no-referrer, browsers sent
  // Origin: null on the form POST, and sameOriginPost refused it.
  const pages = {
    consent: consentPage(consentFacts),
    admin: adminPage(adminFacts),
    error: errorPage("This Page Expired", "One.  Two.", 400),
    noConnection: noConnectionPage(),
  };
  for (const [name, res] of Object.entries(pages)) {
    const policy = res.headers.get("Referrer-Policy");
    assert.equal(policy, REFERRER_POLICY, name);
    assert.notEqual(policy, "no-referrer", name);
    const origin = browserPostOrigin({ policy });
    assert.equal(origin, ISSUER, `${name}:  the browser sends the issuer as Origin`);
    const post = new Request(`${ISSUER}/admin/action`, { method: "POST", headers: { Origin: origin, "Sec-Fetch-Site": "same-origin" } });
    assert.equal(sameOriginPost(post, ISSUER, { requireFetchSite: true }), true, name);
  }
  // The failure mode this guards against, spelled out.
  assert.equal(browserPostOrigin({ policy: "no-referrer" }), "null");
  const broken = new Request(`${ISSUER}/admin/action`, { method: "POST", headers: { Origin: "null", "Sec-Fetch-Site": "same-origin" } });
  assert.equal(sameOriginPost(broken, ISSUER, { requireFetchSite: true }), false, "Origin: null must stay refused");
  // same-origin keeps Origin for our own forms only;  a cross-origin target gets none, and no Referer.
  assert.equal(browserPostOrigin({ policy: REFERRER_POLICY, target: "https://chatgpt.com/cb" }), "null");
});

test("redirects carry the page Referrer-Policy too, and keep the headers they had", () => {
  const out = withReferrerPolicy({ Location: "https://chatgpt.com/cb", "Set-Cookie": "a=b" });
  assert.equal(out.get("Referrer-Policy"), REFERRER_POLICY);
  assert.equal(out.get("Location"), "https://chatgpt.com/cb");
  assert.equal(withReferrerPolicy(new Headers({ "Referrer-Policy": "no-referrer" })).get("Referrer-Policy"), REFERRER_POLICY);
});

test("admin page keeps authorize and token refusals apart and shows the Access email", async () => {
  const res = adminPage({
    ...adminFacts,
    refusals: [{ ts: 0, where: "authorize", by: "mail@jays.services", reason: "redirect_not_allowlisted", client_id: "https://grok.com/oauth/mcp-client.json", redirect_uri: "https://grok.com/cb", resource: "https://x.example/<mcp>" }],
    tokenRefusals: [{ ts: 0, where: "token", reason: "cimd_client_not_allowlisted", client_id: "https://<evil>/c.json", count: 41 }],
  });
  const html = await res.text();
  const [beforeAuthorize, afterAuthorize] = html.split("<h2>Refused Authorize Requests</h2>");
  const [authorizePart, tokenPart] = afterAuthorize.split("<h2>Refused Token Requests (Unauthenticated, Counted)</h2>");
  assert.ok(beforeAuthorize.includes("Status Overview"));
  assert.ok(authorizePart.includes("mail@jays.services") && authorizePart.includes("https://grok.com/cb"));
  assert.ok(authorizePart.includes("<th>Resource</th>") && authorizePart.includes("https://x.example/&#60;mcp&#62;"), "the resource the client sent, escaped");
  assert.ok(!authorizePart.includes("&#60;evil&#62;"), "token rows are not in the authorize table");
  assert.ok(tokenPart.includes("&#60;evil&#62;") && tokenPart.includes(">41<"));
});

test("consent for a loopback callback says the tokens go to a program on this computer", async () => {
  const html = await consentPage({ ...consentFacts, clientName: "Echo (manual form, ECHO)", clientDomain: "", clientId: "AbCdEf1234567890", redirectHost: "127.0.0.1:8737", loopback: true, seat: "ECHO", eventsOnly: false }).text();
  assert.match(html, /A program on the computer running this browser \(127\.0\.0\.1:8737\)/);
  assert.match(html, /Approve only if you opened this page on the app/);
  assert.match(html, /Connect Echo \(manual form, ECHO\) as ECHO\?/);
  assert.match(html, /name="seat" value="ECHO"/);
  const https = await consentPage(consentFacts).text();
  assert.doesNotMatch(https, /A program on the computer/);
  assert.match(https, /<strong>chatgpt\.com<\/strong>/);
});

test("admin page shows the Infisical mode and counters it is passed, and nothing without them (kody 4365623014)", async () => {
  const shown = await adminPage({ ...adminFacts, infisical: { mode: "required", fetched: 5, cacheHits: 40, fallbacks: 0, absent: 2 } }).text();
  assert.ok(shown.includes(`Secrets mode:  required.${SENTENCE_GAP}Since this isolate started:  5 Infisical reads, 40 cache hits and 0 fallbacks to Worker bindings.${SENTENCE_GAP}2 reads found an optional secret set nowhere, which every mode answers the same way, and are not fallbacks.`));
  assert.doesNotMatch(shown, /<p class="warn">Secrets mode/, "zero fallbacks is not a warning");
  const warned = await adminPage({ ...adminFacts, infisical: { mode: "fallback", fetched: 1, cacheHits: 0, fallbacks: 3 } }).text();
  assert.match(warned, /<p class="warn">Secrets mode:  fallback\./);
  const absent = await adminPage(adminFacts).text();
  assert.doesNotMatch(absent, /Secrets mode/);
});

test("admin page offers a manual client per hosted seat and shows each client's seat", async () => {
  const html = await adminPage({
    ...adminFacts,
    manualSeats: [{ seat: "ECHO", label: "Echo", redirectsConfigured: true, shared: true }, { seat: "WISE", label: "Wise", redirectsConfigured: true, shared: true }],
    clients: [
      { clientId: "AbCdEf1234567890", clientName: "Echo (manual form, ECHO)", seat: "ECHO", redirectUris: ["http://127.0.0.1:8737/callback"] },
      { clientId: "QqQqQqQq12345678", clientName: "Old", seat: "", redirectUris: [] },
    ],
  }).text();
  assert.match(html, /Create Manual Client For ECHO/);
  assert.match(html, /Create Manual Client For WISE/);
  assert.match(html, /name="action" value="create_manual_client"><input type="hidden" name="seat" value="ECHO">/);
  assert.match(html, /shared with another seat of the same app/);
  assert.equal((html.match(/Sync Redirects/g) ?? []).length, 1, "an untagged client gets no sync button");
});

test("admin status distinguishes no traffic, mixed outcomes, readiness, and retained error history", async () => {
  const noTraffic = await adminPage({
    ...adminFacts,
    seats: [{ seat: "JET", paused: false, keyInstalled: true, role: { ok: true }, grants: [], linearEnabled: true, linearConfigured: true, linearWrites: false, recallConfigured: false }],
    statusCalls: [], statusErrors: [], generatedAt: Date.parse("2026-10-09T20:05:00Z"),
  }).text();
  assert.match(noTraffic, /No Recent Traffic/);
  assert.match(noTraffic, /quiet seat is not verified healthy/);
  assert.match(noTraffic, /Needs setup: grant/);
  assert.match(noTraffic, /credential configured/);
  const configuredQuiet = await adminPage({
    ...adminFacts,
    seats: [{ seat: "JET", paused: false, keyInstalled: true, role: { ok: true }, grants: [{ client: "Client", scope: [], createdAt: 0 }] }],
    statusCalls: [], generatedAt: Date.parse("2026-10-09T20:05:00Z"),
  }).text();
  assert.match(configuredQuiet, /Configured; no recent traffic/);
  assert.doesNotMatch(configuredQuiet, /class="status-state state-ready"/);

  const mixed = await adminPage({
    ...adminFacts,
    seats: [{ seat: "JET", paused: false, keyInstalled: true, role: { ok: true }, grants: [{ client: "Client", scope: ["zulip:read"], createdAt: 0 }], linearEnabled: false, linearConfigured: false, linearWrites: false, recallConfigured: true }],
    statusCalls: [
      { ts: Date.parse("2026-10-09T20:00:00Z"), seat: "JET", tool: "zulip_send", error_code: "ok" },
      { ts: Date.parse("2026-10-09T20:05:00Z"), seat: "JET", tool: "linear_query", error_code: "budget_exhausted", error_message: "Budget spent", retryable: true, reset_at: Date.parse("2026-10-09T21:00:00Z") },
    ],
    statusErrors: [{ ts: Date.parse("2026-10-09T20:05:00Z"), seat: "JET", tool: "linear_query", error_code: "budget_exhausted", error_message: "Budget spent", retryable: true, reset_at: Date.parse("2026-10-09T21:00:00Z"), check: { name: "query-budget", params: { limit: 3 } }, upstream_status: 429 }],
    refusals: [{ ts: Date.parse("2026-10-09T20:04:00Z"), seat: "JET", reason: "redirect_not_allowlisted", client_id: "manual-client" }],
    audits: [{ ts: Date.parse("2026-10-09T20:06:00Z"), event: "event_delivery", seat: "JET", outcome: "failed", reason: "callback_error", retryable: true, retry_after_s: 20 }],
    runtimeErrors: [{ ts: Date.parse("2026-10-09T20:06:30Z"), kind: "wake_callback", reason: "callback_timeout", count: 2 }],
    generatedAt: Date.parse("2026-10-09T20:07:00Z"),
  }).text();
  assert.match(mixed, /Recorded Failures/);
  assert.match(mixed, /Last Success/);
  assert.match(mixed, /Fri, Oct 9, 3:00pm/);
  assert.match(mixed, /Wait until the displayed reset time/);
  assert.match(mixed, /Retryable/);
  assert.match(mixed, /Reset Fri, Oct 9, 4:00pm/);
  assert.match(mixed, /Upstream HTTP 429/);
  assert.ok(mixed.includes('Check {&#34;name&#34;:&#34;query-budget&#34;,&#34;params&#34;:{&#34;limit&#34;:3}}'));
  assert.match(mixed, /Authorize Refusal/);
  assert.match(mixed, /Wake Delivery/);
  assert.match(mixed, /wake_callback/);
  assert.match(mixed, /2 occurrences/);
  assert.match(mixed, /History Coverage/);
  assert.equal((mixed.match(/Budget spent/g) ?? []).length, 1, "separate error tail is deduplicated against the latest call sample");
});

test("status diagnostics escape untrusted fields and disclose failed sample collection", async () => {
  const html = await adminPage({
    ...adminFacts,
    seats: [{ seat: `<img src=x onerror=alert(1)>`, paused: true, keyInstalled: false, role: null, grants: [] }],
    statusCalls: [{ ts: 0, seat: "<script>", tool: "<img>", error_code: "custom", error_message: `<svg onload=alert(1)>`, client_id: `" onmouseover="x` }],
    diagnosticsUnavailable: true,
  }).text();
  assert.match(html, /Diagnostics Unavailable/);
  assert.match(html, /diagnostic sample could not be loaded/);
  assert.match(html, /Configuration fields do not verify service health/);
  assert.ok(!html.includes("<img src=x"));
  assert.ok(!html.includes("<svg onload"));
  assert.ok(!html.includes("<script>"));
  assert.doesNotMatch(html, /<script\b/i);
});

test("later seat failures and unavailable diagnostics override a successful call", async () => {
  const seat = { seat: "JET", paused: false, keyInstalled: true, role: { ok: true }, grants: [{ client: "Client", scope: [], createdAt: 0 }] };
  const successfulCall = { ts: Date.parse("2026-10-09T20:05:00Z"), seat: "JET", tool: "zulip_read", error_code: "ok" };
  const afterRefusal = await adminPage({
    ...adminFacts,
    seats: [seat],
    statusCalls: [successfulCall],
    statusAudits: [{ ts: Date.parse("2026-10-09T20:06:00Z"), seat: "JET", event: "mcp_refused", detail: { reason: "paused" } }],
    generatedAt: Date.parse("2026-10-09T20:07:00Z"),
  }).text();
  assert.match(afterRefusal, /Recent failure/);
  assert.match(afterRefusal, /MCP Refusal/);

  const unavailable = await adminPage({
    ...adminFacts,
    seats: [{ ...seat, diagnosticsUnavailable: true }],
    statusCalls: [successfulCall],
    generatedAt: Date.parse("2026-10-09T20:07:00Z"),
  }).text();
  assert.match(unavailable, /Diagnostics unavailable/);
  assert.doesNotMatch(unavailable, /<p class="status-state state-ready">Recent success/);
});

test("wake release retries are visible with actionable callback guidance", async () => {
  const html = await adminPage({
    ...adminFacts,
    statusAudits: [{ ts: Date.parse("2026-10-09T20:06:00Z"), seat: "JET", event: "event_wake_released", detail: {} }],
  }).text();
  assert.match(html, /Wake Delivery/);
  assert.match(html, /delivery attempt failed and the wake was released for retry/);
  assert.match(html, /Check the listener&#39;s callback and the Zulip channel delivery/);
});

test("wake failures drive the same overview status as the unified error list", async () => {
  const html = await adminPage({
    ...adminFacts,
    seats: [{ seat: "JET", paused: false, keyInstalled: true, role: { ok: true }, grants: [{ client: "Client", scope: [], createdAt: 0 }] }],
    statusCalls: [{ ts: Date.parse("2026-10-09T20:05:00Z"), seat: "JET", tool: "zulip_read", error_code: "ok" }],
    statusAudits: [{ ts: Date.parse("2026-10-09T20:06:00Z"), seat: "JET", event: "event_wake_released", detail: {} }],
    generatedAt: Date.parse("2026-10-09T20:07:00Z"),
  }).text();
  assert.match(html, /Recorded Failures/);
  assert.match(html, /event_wake_released/);
});

test("failed state, role, and grant reads render unknown instead of missing or unpaused", async () => {
  const html = await adminPage({
    ...adminFacts,
    seats: [{ seat: "JET", paused: false, keyInstalled: true, role: null, grants: [], diagnosticsUnavailable: true, unavailableFields: ["state", "role", "grants"] }],
    statusCalls: [{ ts: Date.parse("2026-10-09T20:05:00Z"), seat: "JET", tool: "zulip_read", error_code: "ok" }],
    generatedAt: Date.parse("2026-10-09T20:07:00Z"),
  }).text();
  assert.match(html, /<dt>Seat<\/dt><dd>Unknown<\/dd>/);
  assert.match(html, /<dt>Member Role<\/dt><dd>Unknown<\/dd>/);
  assert.match(html, /<dt>Grant<\/dt><dd>Unknown<\/dd>/);
  assert.doesNotMatch(html, /Unpaused|Needs setup: role|Needs setup: grant/);
  assert.match(html, /Diagnostics unavailable/);
});


test("legacy seat controls also show unknown when diagnostic state reads fail", async () => {
  const html = await adminPage({ ...adminFacts, seats: [{ seat: "JET", grants: [], keyInstalled: true, diagnosticsUnavailable: true, unavailableFields: ["state", "role", "grants"] }] }).text();
  const table = html.split('<h2>Seats</h2>')[1].split('<h2>Hand-Registered')[0];
  assert.doesNotMatch(table, />Live<|name="action" value="pause"|name="action" value="unpause"/);
  assert.match(table, /Unknown/);
  assert.match(table, /diagnostic read failed/);
});

test("a role never checked reads not checked yet, not a setup gap (AFC-347)", async () => {
  const html = await adminPage({
    ...adminFacts,
    seats: [{ seat: "WISE", paused: false, keyInstalled: true, role: null, grants: [{ client: "Client", scope: [], createdAt: 0 }] }],
    statusCalls: [], generatedAt: Date.parse("2026-10-18T12:00:00Z"),
  }).text();
  assert.doesNotMatch(html, /Needs setup: role/);
  assert.match(html, /Configured; role not checked yet/);
  assert.match(html, /<dt>Member Role<\/dt><dd>Not checked yet/);
  const refused = await adminPage({
    ...adminFacts,
    seats: [{ seat: "WISE", paused: false, keyInstalled: true, role: { ok: false, reason: "the key belongs to another bot" }, grants: [{ client: "Client", scope: [], createdAt: 0 }] }],
    statusCalls: [], generatedAt: Date.parse("2026-10-18T12:00:00Z"),
  }).text();
  assert.match(refused, /Needs setup: role/);
  assert.match(refused, /<dt>Member Role<\/dt><dd>Refused: the key belongs to another bot/);
});

test("admin page puts the owner's controls first and folds the long sections (AFC-347)", async () => {
  const calls = Array.from({ length: 40 }, (_, i) => ({ ts: Date.parse("2026-10-18T12:00:00Z") - i * 1000, seat: "JET", tool: "zulip_read", error_code: "ok" }));
  const html = await adminPage({ ...adminFacts, calls, statusCalls: calls, generatedAt: Date.parse("2026-10-18T12:00:00Z") }).text();
  // The renamed unified title is pinned:  a rename regression fails here
  // (kody 4372747419 - the visual harness covers the static site only).
  assert.match(html, /<title>Fleet MCP Admin<\/title>/);
  const at = (s) => html.indexOf(s);
  assert.ok(at("<h2>Seats</h2>") > 0 && at("<h2>Seats</h2>") < at("Hand-Registered Clients"));
  assert.ok(at("Hand-Registered Clients") < at('id="status-overview"'));
  assert.ok(at('id="status-overview"') < at("<h2>Tool Calls</h2>"));
  assert.match(html, /<details id="status-overview" class="status-overview fold"><summary><h2>Status Overview<\/h2>/);
  assert.match(html, /<details class="fold"><summary><h2>Tool Calls<\/h2> <span class="count">40 calls<\/span><\/summary>/);
  for (const title of ["Refused Authorize Requests", "Audit"]) assert.match(html, new RegExp(`<details class="fold"><summary><h2>${title}`));
  assert.doesNotMatch(html, /<details[^>]* open/, "folded by default");
});

test("the bearer table shows mismatch counts when present, never hashes", async () => {
  const { adminPage } = await import("../src/pages.js");
  const base = {
    email: "admin@example.test",
    seats: [],
    clients: [],
    refusals: [],
    audits: [],
    generatedAt: 0,
    csrf: "c",
  };
  const withCountRes = adminPage({
    ...base,
    gbBearers: [{ identity: "JET", kind: "seat", credentials: true, status: null, mismatches: { count: 3, last_at: 1730000000000 } }],
  });
  const withCount = await withCountRes.text();
  assert.ok(withCount.includes("3 wrong-secret attempts"), "count line renders");
  const line = withCount.split("3 wrong-secret attempts")[1].slice(0, 120);
  assert.ok(!/[0-9a-f]{64}/.test(line), "the count line carries no token hash");

  const noneRes = adminPage({
    ...base,
    gbBearers: [{ identity: "JET", kind: "seat", credentials: true, status: null, mismatches: { count: 0, last_at: 0 } }],
  });
  const none = await noneRes.text();
  assert.ok(!none.includes("wrong-secret attempt"), "zero renders nothing");

  const missingRes = adminPage({
    ...base,
    gbBearers: [{ identity: "JET", kind: "seat", credentials: true, status: null }],
  });
  const missing = await missingRes.text();
  assert.ok(!missing.includes("wrong-secret attempt"), "absent renders nothing");
});

test("a failed counter read says unavailable, never a silent zero", async () => {
  const { adminPage } = await import("../src/pages.js");
  const base = { email: "admin@example.test", seats: [], clients: [], refusals: [], audits: [], generatedAt: 0, csrf: "c" };
  const html = await adminPage({
    ...base,
    gbBearers: [{ identity: "JET", kind: "seat", credentials: true, status: null, mismatchesUnavailable: true }],
  }).text();
  assert.ok(html.includes("wrong-secret counter read unavailable"), "the failed read is named");
  assert.ok(!html.includes("wrong-secret attempt"), "unavailable is not dressed as a count");
});

test("the mismatch count line labels its lifetime retention", async () => {
  const { adminPage } = await import("../src/pages.js");
  const base = { email: "admin@example.test", seats: [], clients: [], refusals: [], audits: [], generatedAt: 0, csrf: "c" };
  const html = await adminPage({
    ...base,
    gbBearers: [{ identity: "JET", kind: "seat", credentials: true, status: null, mismatches: { count: 3, last_at: 1730000000000 } }],
  }).text();
  assert.ok(html.includes("3 wrong-secret attempts"), "count renders");
  assert.ok(html.includes("(approximate; updates at most once a minute)"), "the write-throttled, lower-bound nature is labelled");
});
