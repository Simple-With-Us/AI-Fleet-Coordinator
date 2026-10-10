import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { STYLE, STYLE_HASH, PAGE_CSP, SENTENCE_GAP, REFERRER_POLICY, escapeHtml, ownerTime, consentPage, adminPage, errorPage, noConnectionPage, withReferrerPolicy } from "../src/pages.js";
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

const consentFacts = { clientName: "ChatGPT", clientDomain: "chatgpt.com", clientId: "https://chatgpt.com/oauth/client.json", redirectHost: "chatgpt.com", scopes: ["zulip:read"], seat: "JET", handle: "h", replacing: [] };
const adminFacts = { email: "mail@jays.services", seats: [], clients: [], refusals: [], audits: [], csrf: "c".repeat(64), notice: "", grokRedirectsConfigured: true };

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
  const [authorizePart, tokenPart] = html.split("Refused Token Requests");
  assert.ok(authorizePart.includes("Refused Authorize Requests"));
  assert.ok(authorizePart.includes("mail@jays.services") && authorizePart.includes("https://grok.com/cb"));
  assert.ok(authorizePart.includes("<th>Resource</th>") && authorizePart.includes("https://x.example/&#60;mcp&#62;"), "the resource the client sent, escaped");
  assert.ok(!authorizePart.includes("&#60;evil&#62;"), "token rows are not in the authorize table");
  assert.ok(tokenPart.includes("&#60;evil&#62;") && tokenPart.includes(">41<"));
});
