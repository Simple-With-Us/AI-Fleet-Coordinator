import test from "node:test";
import assert from "node:assert/strict";
import { loadConfig } from "../src/config.js";
import {
  strictRedirectProblem,
  hostAllowed,
  isUrlShapedClientId,
  clientIdVerdict,
  cimdHostname,
  isFormContentType,
  readLimited,
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
  assert.equal(classifyPath("/health"), "health");
  for (const p of ["/health/", "/Health", "/health/x", "/healthz"]) assert.equal(classifyPath(p), null, p);
  assert.equal(classifyPath("/.well-known/oauth-authorization-server"), "metadata");
  assert.equal(classifyPath("/.well-known/oauth-protected-resource/mcp"), "metadata");
  for (const p of ["/", "/oauth/register", "/mcp/", "/post", "/admin2", "/.well-known/openid-configuration"]) {
    assert.equal(classifyPath(p), null, p);
  }
});

test("URL-shaped client ids, including the shapes URL parsing normalizes into a URL", () => {
  assert.equal(isUrlShapedClientId(CHATGPT_CLIENT), true);
  assert.equal(isUrlShapedClientId("http://x"), true);
  assert.equal(isUrlShapedClientId("data:,x"), true);
  assert.equal(isUrlShapedClientId("abc123DEF"), false);
  assert.equal(isUrlShapedClientId(undefined), false);
  // WHATWG URL drops leading and trailing spaces and removes tabs and newlines.
  assert.equal(isUrlShapedClientId(" https://evil.example/c.json"), true);
  assert.equal(isUrlShapedClientId("https://evil.example/c.json "), true);
  assert.equal(isUrlShapedClientId("ht\ttps://evil.example/c.json"), true);
  assert.equal(isUrlShapedClientId("h\nttps://evil.example/c.json"), true);
  assert.equal(isUrlShapedClientId("\u0001https://evil.example/c.json"), true);
  assert.equal(cimdHostname(CHATGPT_CLIENT), "chatgpt.com");
  assert.equal(cimdHostname("http://chatgpt.com/x"), "");
  assert.equal(cimdHostname("abc123DEF"), "");
});

test("client id verdict:  exact allowlisted CIMD id or the library's hand-client shape, nothing else", () => {
  assert.deepEqual(clientIdVerdict(CHATGPT_CLIENT, config), { ok: true, seat: "JET" });
  assert.deepEqual(clientIdVerdict("AbCdEf123_-xYz09", config), { ok: true, seat: "" });
  assert.deepEqual(clientIdVerdict("", config), { ok: true, seat: "" });
  assert.deepEqual(clientIdVerdict(undefined, config), { ok: true, seat: "" });
  const refusedUrl = [
    "https://evil.example/c.json",
    "https://chatgpt.com/oauth/client.json?x=1",
    "https://chatgpt.com/oauth/client.json ",
    " https://chatgpt.com/oauth/client.json",
    "ht\ttps://evil.example/c.json",
    "HTTPS://CHATGPT.COM/oauth/client.json",
    "http://chatgpt.com/oauth/client.json",
    "data:,x",
  ];
  for (const id of refusedUrl) assert.deepEqual(clientIdVerdict(id, config), { ok: false, reason: "cimd_client_not_allowlisted" }, JSON.stringify(id));
  for (const id of ["a b", "short", "x".repeat(65), "has.dot.in.it", "slash/in/id1", "unicode\u00e9xxx", "\u00a0AbCdEf123456"]) {
    assert.equal(clientIdVerdict(id, config).ok, false, JSON.stringify(id));
  }
});

test("pre-gate admits ChatGPT for JET", () => {
  const got = preGateAuthorize(authorizeParams(), config);
  assert.deepEqual(got, { ok: true, seat: "JET", clientId: CHATGPT_CLIENT, redirectUri: CHATGPT_REDIRECT, scopes: ["zulip:read", "zulip:write"] });
});

test("pre-gate admits Jay's ChatGPT connector as an exact pair, and no sibling connector", () => {
  const redirect = "https://chatgpt.com/connector/oauth/Aa3WqJNIVGqM";
  const client = "https://chatgpt.com/oauth/Aa3WqJNIVGqM/client.json";
  const got = preGateAuthorize(authorizeParams({ client_id: client, redirect_uri: redirect }), config);
  assert.deepEqual(got, { ok: true, seat: "JET", clientId: client, redirectUri: redirect, scopes: ["zulip:read", "zulip:write"] });
  const siblingClient = "https://chatgpt.com/oauth/Zz9XyWvUtSrQ/client.json";
  const siblingRedirect = "https://chatgpt.com/connector/oauth/Zz9XyWvUtSrQ";
  assert.equal(preGateAuthorize(authorizeParams({ client_id: siblingClient, redirect_uri: siblingRedirect }), config).reason, "redirect_not_allowlisted");
  assert.equal(preGateAuthorize(authorizeParams({ client_id: client, redirect_uri: siblingRedirect }), config).reason, "redirect_not_allowlisted");
  assert.equal(preGateAuthorize(authorizeParams({ client_id: siblingClient, redirect_uri: redirect }), config).reason, "cimd_client_not_allowlisted");
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
    [{ client_id: " https://chatgpt.com/oauth/client.json" }, "cimd_client_not_allowlisted"],
    [{ client_id: "ht\ttps://evil.example/c.json" }, "cimd_client_not_allowlisted"],
    [{ client_id: "not a shape" }, "client_id_shape"],
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

test("Grok's published client metadata document binds only to GROK-WEB", () => {
  const grokClient = "https://grok.com/oauth/mcp-client.json";
  const grokRedirect = "https://grok.com/connectors-oauth-exchange-code/";
  const got = preGateAuthorize(authorizeParams({ client_id: grokClient, redirect_uri: grokRedirect }), config);
  assert.deepEqual(got, { ok: true, seat: "GROK-WEB", clientId: grokClient, redirectUri: grokRedirect, scopes: ["zulip:read", "zulip:write"] });
  // Cross-family in both directions.
  assert.equal(preGateAuthorize(authorizeParams({ client_id: CHATGPT_CLIENT, redirect_uri: grokRedirect }), config).reason, "cimd_client_not_allowlisted");
  assert.equal(preGateAuthorize(authorizeParams({ client_id: grokClient, redirect_uri: CHATGPT_REDIRECT }), config).reason, "cimd_client_not_allowlisted");
  // console.x.ai is not allowlisted until the xAI account is Business or Enterprise.
  assert.equal(preGateAuthorize(authorizeParams({ client_id: grokClient, redirect_uri: "https://console.x.ai/connectors-oauth-exchange-code/" }), config).reason, "redirect_not_allowlisted");
});

test("a hand-registered client may use a seat's configured redirect", () => {
  const grok = "https://grok.com/oauth/callback";
  const seats = { ...testEnv().SEATS, "GROK-WEB": { redirect_uris: [grok], cimd_client_ids: [] } };
  const cfg = loadConfig(testEnv({ SEATS: seats }));
  const got = preGateAuthorize(authorizeParams({ client_id: "AbCdEf123", redirect_uri: grok }), cfg);
  assert.equal(got.ok, true);
  assert.equal(got.seat, "GROK-WEB");
  // ChatGPT's CIMD id with Grok's redirect is cross-family.
  assert.equal(preGateAuthorize(authorizeParams({ redirect_uri: grok }), cfg).reason, "cimd_client_not_allowlisted");
});

test("a resource_mismatch refusal carries the resource the client sent, capped for the log", () => {
  const sent = "https://agent-sync.jays.services/";
  const got = preGateAuthorize(authorizeParams({ resource: sent }), config);
  assert.equal(got.reason, "resource_mismatch");
  assert.equal(got.resource, sent);
  assert.equal(preGateAuthorize(authorizeParams({ resource: undefined }), config).resource, "");
  assert.equal(preGateAuthorize(authorizeParams({ resource: `https://x.example/${"a".repeat(600)}` }), config).resource.length, 200);
  assert.equal(preGateAuthorize(authorizeParams(), config).ok, true);
});

test("post-gate re-checks the library's parsed request", () => {
  const good = { clientId: CHATGPT_CLIENT, redirectUri: CHATGPT_REDIRECT, codeChallenge: CHALLENGE, codeChallengeMethod: "S256", resource: RESOURCE, scope: ["zulip:read"] };
  assert.equal(postGateAuthRequest(good, config), "JET");
  assert.equal(postGateAuthRequest({ ...good, codeChallengeMethod: "plain" }, config), null);
  assert.equal(postGateAuthRequest({ ...good, codeChallenge: undefined }, config), null);
  assert.equal(postGateAuthRequest({ ...good, resource: "https://x.example/mcp" }, config), null);
  assert.equal(postGateAuthRequest({ ...good, redirectUri: "https://evil.example/cb" }, config), null);
  assert.equal(postGateAuthRequest({ ...good, clientId: "https://evil.example/c.json" }, config), null);
  assert.equal(postGateAuthRequest({ ...good, clientId: " https://chatgpt.com/oauth/client.json" }, config), null);
  assert.equal(postGateAuthRequest({ ...good, clientId: "AbCdEf1234567890" }, config), "JET", "a hand client id passes the shape check");
  assert.equal(postGateAuthRequest({ ...good, clientId: "https://grok.com/oauth/mcp-client.json" }, config), null, "Grok's id with ChatGPT's redirect is cross-family");
  assert.equal(postGateAuthRequest({ ...good, scope: ["admin"] }, config), null);
});

const basic = (id, secret = "") => `Basic ${btoa(`${encodeURIComponent(id)}:${secret}`)}`;

test("token gate:  unlisted CIMD refused, blank secret dropped, the validated form is what is forwarded", () => {
  const refused = gateTokenForm(new URLSearchParams({ grant_type: "authorization_code", client_id: "https://evil.example/c.json" }), null, config);
  assert.equal(refused.ok, false);
  assert.equal(refused.reason, "cimd_client_not_allowlisted");
  const viaBasic = gateTokenForm(new URLSearchParams({ grant_type: "refresh_token" }), basic("https://evil.example/c.json"), config);
  assert.equal(viaBasic.ok, false);
  assert.equal(viaBasic.reason, "cimd_client_not_allowlisted");

  const chatgpt = gateTokenForm(new URLSearchParams({ client_id: CHATGPT_CLIENT, code: "x" }), null, config);
  assert.equal(chatgpt.ok, true);
  assert.equal(chatgpt.form.get("client_id"), CHATGPT_CLIENT);

  const blank = gateTokenForm(new URLSearchParams("client_id=AbCdEf1234567890&client_secret=&code=x"), undefined, config);
  assert.equal(blank.ok, true);
  assert.equal(blank.form.has("client_secret"), false);
  assert.equal(blank.form.get("code"), "x");

  const real = gateTokenForm(new URLSearchParams("client_id=AbCdEf1234567890&client_secret=s3cret"), "", config);
  assert.equal(real.ok, true);
  assert.equal(real.form.get("client_secret"), "s3cret");

  assert.equal(gateTokenForm(new URLSearchParams("client_id=a&client_id=b"), null, config).reason, "repeated_client_id");
});

test("token gate:  a Basic header the library would not read as Basic cannot hide a form client_id", () => {
  // The library splits the scheme on a space or tab only.  Each header below is
  // Basic to a JS \\s regex but not Basic to the library, which then reads the
  // form client_id and fetches it.  The gate refuses the header outright.
  const evilForm = new URLSearchParams({ grant_type: "authorization_code", client_id: "https://evil.example/c.json", code: "x" });
  const creds = btoa("x:");
  for (const sep of ["\u00a0", "\u3000", "\ufeff", "\u000b", "\u000c", "\u2003", "\t"]) {
    const got = gateTokenForm(evilForm, `Basic${sep}${creds}`, config);
    assert.equal(got.ok, false, JSON.stringify(sep));
    assert.equal(got.reason, "authorization_not_basic", JSON.stringify(sep));
  }
  // Even a perfectly normal Basic header cannot skip the form id check.
  const withBasic = gateTokenForm(evilForm, basic("AbCdEf1234567890"), config);
  assert.equal(withBasic.ok, false);
  assert.equal(withBasic.reason, "cimd_client_not_allowlisted");
  for (const header of ["Bearer abc", "Basic", "Basic ", "Basic  YTo=", "Basic YTo= ", "Basic !!!!", "Basic Zm9v", "Digest x=1"]) {
    assert.equal(gateTokenForm(new URLSearchParams("code=x"), header, config).reason, "authorization_not_basic", JSON.stringify(header));
  }
  assert.equal(gateTokenForm(new URLSearchParams("code=x"), basic("AbCdEf1234567890", "s"), config).ok, true);
  assert.equal(gateTokenForm(new URLSearchParams("code=x"), `basic ${btoa("AbCdEf1234567890:")}`, config).ok, true, "the scheme is case-insensitive");
});

test("token gate:  leading-space and tab-split ids are refused whichever field carries them", () => {
  for (const id of [" https://evil.example/c.json", "ht\ttps://evil.example/c.json", "https://evil.example/c.json\n"]) {
    const viaForm = gateTokenForm(new URLSearchParams({ client_id: id }), null, config);
    assert.equal(viaForm.ok, false, JSON.stringify(id));
    assert.equal(viaForm.reason, "cimd_client_not_allowlisted", JSON.stringify(id));
    assert.equal(gateTokenForm(new URLSearchParams(), basic(id), config).ok, false, JSON.stringify(id));
  }
});

test("token gate:  Content-Type is read the way the library reads it", () => {
  assert.equal(isFormContentType("application/x-www-form-urlencoded"), true);
  assert.equal(isFormContentType("Application/X-WWW-Form-Urlencoded; charset=UTF-8"), true);
  // The library trims with JS trim(), which drops all of these;  a startsWith test would skip the gate.
  for (const lead of ["\u00a0", "\ufeff", "\u3000", " ", "\t", "\u000b", "\u2003"]) {
    assert.equal(isFormContentType(`${lead}application/x-www-form-urlencoded`), true, JSON.stringify(lead));
  }
  for (const other of ["application/json", "text/plain; x=application/x-www-form-urlencoded", "multipart/form-data", "", null, undefined, "application/x-www-form-urlencoded2"]) {
    assert.equal(isFormContentType(other), false, JSON.stringify(other));
  }
});

test("Basic header client id", () => {
  assert.equal(basicClientId(`Basic ${btoa("client%3Aid:secret")}`), "client:id");
  assert.equal(basicClientId(undefined), "");
  assert.equal(basicClientId(""), "");
  assert.equal(basicClientId("Bearer x"), null);
  assert.equal(basicClientId(`Basic${"\u00a0"}${btoa("x:")}`), null);
  assert.equal(basicClientId(`Basic ${btoa("no-colon")}`), null);
});

test("bounded body read stops a large chunked upload", async () => {
  const mk = (body) => new Request("https://agent-sync.jays.services/oauth/token", { method: "POST", body });
  assert.equal(await readLimited(mk("a=b&c=d"), 100), "a=b&c=d");
  assert.equal(await readLimited(new Request("https://agent-sync.jays.services/oauth/token"), 100), "");
  assert.equal(await readLimited(mk("x".repeat(101)), 100), null);
  const stream = new ReadableStream({
    pull(controller) {
      controller.enqueue(new TextEncoder().encode("y".repeat(1000)));
    },
  });
  const chunked = new Request("https://agent-sync.jays.services/oauth/token", { method: "POST", body: stream, duplex: "half" });
  assert.equal(await readLimited(chunked, 4096), null, "an endless stream is cut off");
  // A BOM is kept (the form parser keeps it), so the key the gate sees is the key the library sees.
  const bom = await readLimited(mk(new Uint8Array([0xef, 0xbb, 0xbf, 0x61, 0x3d, 0x62])), 100);
  assert.equal(bom, "\ufeffa=b");
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
