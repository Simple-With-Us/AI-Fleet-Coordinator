import test from "node:test";
import assert from "node:assert/strict";
import { loadConfig } from "../src/config.js";
import {
  strictRedirectProblem,
  hostAllowed,
  isUrlShapedClientId,
  classifyPath,
  preGateAuthorize,
  postGateAuthRequest,
  gateTokenForm,
  basicClientId,
  sameOriginPost,
  safeEqual,
  logSafe,
} from "../src/policy.js";
import { testEnv, authorizeParams, CHATGPT_CLIENT, CHATGPT_REDIRECT, RESOURCE, CHALLENGE } from "./helpers.mjs";

const config = loadConfig(testEnv());

test("strict redirect accepts ChatGPT's exact callback", () => {
  assert.equal(strictRedirectProblem(CHATGPT_REDIRECT), "");
});

test("strict redirect refuses every bypass shape", () => {
  const cases = {
    "http://chatgpt.com/connector_platform_oauth_redirect": "not https",
    "https://chatgpt.com@evil.example/connector_platform_oauth_redirect": "userinfo",
    "https://chatgpt.com:8443/connector_platform_oauth_redirect": "port",
    "https://chatgpt.com:443/connector_platform_oauth_redirect": "not canonical",
    "https://chatgpt.com/connector_platform_oauth_redirect?x=1": "query",
    "https://chatgpt.com/connector_platform_oauth_redirect#frag": "fragment",
    "https://chatgpt.com%2F@evil.example/x": "encoded slash, backslash or dot",
    "https://chatgpt.com/a%2Fb": "encoded slash, backslash or dot",
    "https://chatgpt.com\\@evil.example/x": "backslash",
    "https://ChatGPT.com/connector_platform_oauth_redirect": "not canonical",
    "https://chatgpt.com/./connector_platform_oauth_redirect": "not canonical",
    "https://chatgpt.com/connector platform": "control, space or non-ASCII character",
    "": "empty",
  };
  for (const [uri, reason] of Object.entries(cases)) {
    assert.equal(strictRedirectProblem(uri), reason, uri);
  }
});

test("host check:  only agent-sync.jays.services on https and the default port", () => {
  const ok = new Request("https://agent-sync.jays.services/mcp");
  assert.equal(hostAllowed(ok, config.host), true);
  for (const url of [
    "https://agent-sync-mcp.example.workers.dev/mcp",
    "https://evil.example/mcp",
    "https://agent-sync.jays.services:8443/mcp",
    "http://agent-sync.jays.services/mcp",
    "https://agent-sync.jays.services.evil.example/mcp",
  ]) {
    assert.equal(hostAllowed(new Request(url), config.host), false, url);
  }
  const spoofed = new Request("https://agent-sync.jays.services/mcp", { headers: { Host: "evil.example" } });
  // Node's Request drops a forbidden Host header;  the URL is what the runtime routes on.
  assert.equal(hostAllowed(spoofed, config.host), spoofed.headers.get("host") === null || spoofed.headers.get("host") === config.host);
  const fake = { url: "https://agent-sync.jays.services/mcp", headers: new Headers({ host: "evil.example" }) };
  assert.equal(hostAllowed(fake, config.host), false);
});

test("path allowlist", () => {
  assert.equal(classifyPath("/mcp"), "mcp");
  assert.equal(classifyPath("/oauth/token"), "token");
  assert.equal(classifyPath("/authorize"), "authorize");
  assert.equal(classifyPath("/admin"), "admin");
  assert.equal(classifyPath("/admin/action"), "admin");
  assert.equal(classifyPath("/.well-known/oauth-authorization-server"), "metadata");
  assert.equal(classifyPath("/.well-known/oauth-protected-resource/mcp"), "metadata");
  for (const p of ["/", "/oauth/register", "/mcp/", "/post", "/admin2", "/.well-known/openid-configuration"]) {
    assert.equal(classifyPath(p), null, p);
  }
});

test("URL-shaped client ids", () => {
  assert.equal(isUrlShapedClientId(CHATGPT_CLIENT), true);
  assert.equal(isUrlShapedClientId("http://x"), true);
  assert.equal(isUrlShapedClientId("data:,x"), true);
  assert.equal(isUrlShapedClientId("abc123DEF"), false);
});

test("pre-gate admits ChatGPT for JET", () => {
  const got = preGateAuthorize(authorizeParams(), config);
  assert.deepEqual(got, { ok: true, seat: "JET", clientId: CHATGPT_CLIENT, redirectUri: CHATGPT_REDIRECT, scopes: ["zulip:read", "zulip:write"] });
});

test("pre-gate refuses before any fetch", () => {
  const cases = [
    [{ client_id: "https://evil.example/oauth/client.json" }, "cimd_client_not_allowlisted"],
    [{ client_id: "https://chatgpt.com/oauth/other.json" }, "cimd_client_not_allowlisted"],
    [{ redirect_uri: "https://chatgpt.com.evil.example/connector_platform_oauth_redirect" }, "redirect_not_allowlisted"],
    [{ redirect_uri: "https://chatgpt.com@evil.example/connector_platform_oauth_redirect" }, "redirect_not_allowlisted"],
    [{ redirect_uri: "https://chatgpt.com:443/connector_platform_oauth_redirect" }, "redirect_not_allowlisted"],
    [{ redirect_uri: `${CHATGPT_REDIRECT}?a=b` }, "redirect_not_allowlisted"],
    [{ redirect_uri: "https://chatgpt.com/connector/oauth/abc123" }, "redirect_not_allowlisted"],
    [{ code_challenge_method: "plain" }, "pkce_method_not_s256"],
    [{ code_challenge_method: undefined }, "pkce_method_not_s256"],
    [{ code_challenge: undefined }, "pkce_challenge_missing"],
    [{ code_challenge: "short" }, "pkce_challenge_missing"],
    [{ resource: "https://agent-sync.jays.services/" }, "resource_mismatch"],
    [{ resource: `${RESOURCE}?mode=x` }, "resource_mismatch"],
    [{ resource: undefined }, "resource_mismatch"],
    [{ scope: "zulip:read zulip:admin" }, "scope_not_supported"],
    [{ response_type: "token" }, "response_type_not_code"],
    [{ client_id: "" }, "client_id_missing_or_long"],
    [{ client_id: [CHATGPT_CLIENT, "x"] }, "repeated_client_id"],
    [{ redirect_uri: [CHATGPT_REDIRECT, CHATGPT_REDIRECT] }, "repeated_redirect_uri"],
  ];
  for (const [over, reason] of cases) {
    const got = preGateAuthorize(authorizeParams(over), config);
    assert.equal(got.ok, false, JSON.stringify(over));
    assert.equal(got.reason, reason, JSON.stringify(over));
  }
});

test("pre-gate refuses a seat that is not hosted", () => {
  const narrow = loadConfig(testEnv({ HOSTED_SEATS: "GROK-WEB" }));
  assert.equal(preGateAuthorize(authorizeParams(), narrow).reason, "seat_not_hosted");
});

test("a Grok redirect binds only to GROK-WEB, and a hand client may use it", () => {
  const grok = "https://grok.com/oauth/callback";
  const seats = { ...testEnv().SEATS, "GROK-WEB": { redirect_uris: [grok], cimd_client_ids: [] } };
  const cfg = loadConfig(testEnv({ SEATS: seats }));
  const got = preGateAuthorize(authorizeParams({ client_id: "AbCdEf123", redirect_uri: grok }), cfg);
  assert.equal(got.ok, true);
  assert.equal(got.seat, "GROK-WEB");
  // ChatGPT's CIMD id with Grok's redirect is cross-family.
  assert.equal(preGateAuthorize(authorizeParams({ redirect_uri: grok }), cfg).reason, "cimd_client_not_allowlisted");
});

test("post-gate re-checks the library's parsed request", () => {
  const good = { clientId: CHATGPT_CLIENT, redirectUri: CHATGPT_REDIRECT, codeChallenge: CHALLENGE, codeChallengeMethod: "S256", resource: RESOURCE, scope: ["zulip:read"] };
  assert.equal(postGateAuthRequest(good, config), "JET");
  assert.equal(postGateAuthRequest({ ...good, codeChallengeMethod: "plain" }, config), null);
  assert.equal(postGateAuthRequest({ ...good, codeChallenge: undefined }, config), null);
  assert.equal(postGateAuthRequest({ ...good, resource: "https://x.example/mcp" }, config), null);
  assert.equal(postGateAuthRequest({ ...good, redirectUri: "https://evil.example/cb" }, config), null);
  assert.equal(postGateAuthRequest({ ...good, clientId: "https://evil.example/c.json" }, config), null);
  assert.equal(postGateAuthRequest({ ...good, scope: ["admin"] }, config), null);
});

test("token gate:  unlisted CIMD refused, blank secret dropped", () => {
  const refused = gateTokenForm(new URLSearchParams({ grant_type: "authorization_code", client_id: "https://evil.example/c.json" }), undefined, config);
  assert.equal(refused.ok, false);
  assert.equal(refused.reason, "cimd_client_not_allowlisted");
  const viaBasic = gateTokenForm(new URLSearchParams({ grant_type: "refresh_token" }), "https://evil.example/c.json", config);
  assert.equal(viaBasic.ok, false);

  const chatgpt = gateTokenForm(new URLSearchParams({ client_id: CHATGPT_CLIENT, code: "x" }), undefined, config);
  assert.deepEqual(chatgpt, { ok: true });

  const blank = gateTokenForm(new URLSearchParams("client_id=abc&client_secret=&code=x"), undefined, config);
  assert.equal(blank.ok, true);
  assert.equal(blank.form.has("client_secret"), false);
  assert.equal(blank.form.get("code"), "x");

  const real = gateTokenForm(new URLSearchParams("client_id=abc&client_secret=s3cret"), undefined, config);
  assert.deepEqual(real, { ok: true });

  assert.equal(gateTokenForm(new URLSearchParams("client_id=a&client_id=b"), undefined, config).reason, "repeated_client_id");
});

test("Basic header client id", () => {
  assert.equal(basicClientId(`Basic ${btoa("client%3Aid:secret")}`), "client:id");
  assert.equal(basicClientId("Bearer x"), undefined);
  assert.equal(basicClientId(undefined), undefined);
});

test("same-origin POST and CSRF compare", () => {
  const mk = (h, method = "POST") => new Request("https://agent-sync.jays.services/admin/action", { method, headers: h });
  const issuer = "https://agent-sync.jays.services";
  assert.equal(sameOriginPost(mk({ Origin: issuer, "Sec-Fetch-Site": "same-origin" }), issuer, { requireFetchSite: true }), true);
  assert.equal(sameOriginPost(mk({ Origin: issuer }), issuer, { requireFetchSite: true }), false);
  assert.equal(sameOriginPost(mk({ Origin: issuer }), issuer), true);
  assert.equal(sameOriginPost(mk({ Origin: "https://evil.example", "Sec-Fetch-Site": "cross-site" }), issuer), false);
  assert.equal(sameOriginPost(mk({ Origin: issuer, "Sec-Fetch-Site": "cross-site" }), issuer), false);
  assert.equal(sameOriginPost(mk({ Origin: issuer }, "GET"), issuer), false);
  assert.equal(safeEqual("abc", "abc"), true);
  assert.equal(safeEqual("abc", "abd"), false);
  assert.equal(safeEqual("", ""), false);
  assert.equal(safeEqual("abc", undefined), false);
});

test("refusal log values are truncated and de-controlled", () => {
  assert.equal(logSafe("a\nb\u2028c"), "a?b?c");
  assert.equal(logSafe("x".repeat(500)).length, 300);
  assert.equal(logSafe(undefined), "");
});
