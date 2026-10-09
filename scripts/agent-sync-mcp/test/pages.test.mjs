import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { STYLE, STYLE_HASH, PAGE_CSP, SENTENCE_GAP, escapeHtml, ownerTime, consentPage, adminPage, errorPage, noConnectionPage } from "../src/pages.js";

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
      grokRedirectsConfigured: false,
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
