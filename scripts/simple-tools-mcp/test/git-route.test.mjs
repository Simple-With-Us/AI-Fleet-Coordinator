// The hosted read-only git route:  only the two git paths reach the relay, and the Access pair never reaches the caller.
import assert from "node:assert/strict";
import test from "node:test";

import { isGitPath, serveGit } from "../src/git-route.js";

const ENV = { GR_UPSTREAM_URL: "https://relay.example" };
const SECRETS = async () => ({ accessId: "access-id", accessSecret: "access-secret" });
const ADV = "https://agents.example/git/Fixture-Repo/example.git/info/refs?service=git-upload-pack";
const PACK = "https://agents.example/git/Fixture-Repo/example.git/git-upload-pack";
const AUTH = { Authorization: "Bearer seat-bearer" };

function upstream(status = 200, headers = { "Content-Type": "application/x-git-upload-pack-advertisement" }, body = "refs") {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return new Response(body, { status, headers });
  };
  return { calls, fetchImpl };
}

test("the advertisement passes the bearer and adds the Access pair", async () => {
  const { calls, fetchImpl } = upstream();
  const resp = await serveGit(new Request(ADV, { headers: { ...AUTH, "Git-Protocol": "version=2", Cookie: "x=y" } }), ENV, { fetchImpl, secrets: SECRETS });
  assert.equal(resp.status, 200);
  assert.equal(await resp.text(), "refs");
  assert.equal(calls[0].url, "https://relay.example/git/Fixture-Repo/example.git/info/refs?service=git-upload-pack");
  const sent = calls[0].init.headers;
  assert.equal(sent.get("Authorization"), "Bearer seat-bearer");
  assert.equal(sent.get("CF-Access-Client-Id"), "access-id");
  assert.equal(sent.get("Git-Protocol"), "version=2");
  assert.equal(sent.get("Cookie"), null);
  assert.equal(calls[0].init.redirect, "manual");
});

test("upload-pack forwards the body and sends back only git's own headers", async () => {
  const { calls, fetchImpl } = upstream(200, { "Content-Type": "application/x-git-upload-pack-result", "Set-Cookie": "a=b", "CF-Access-Client-Secret": "leak" }, "PACK");
  const resp = await serveGit(new Request(PACK, {
    method: "POST",
    body: "0000",
    headers: { ...AUTH, "Content-Type": "application/x-git-upload-pack-request", "Content-Length": "4" },
  }), ENV, { fetchImpl, secrets: SECRETS });
  assert.equal(await resp.text(), "PACK");
  assert.equal(calls[0].init.method, "POST");
  assert.equal(resp.headers.get("Set-Cookie"), null);
  assert.equal(resp.headers.get("CF-Access-Client-Secret"), null);
});

test("pushing and every other path never reach the relay", async () => {
  const { calls, fetchImpl } = upstream();
  const cases = [
    ["https://agents.example/git/a/b.git/info/refs?service=git-receive-pack", "GET", 403],
    ["https://agents.example/git/a/b.git/git-receive-pack", "POST", 403],
    ["https://agents.example/git/a/b.git/HEAD", "GET", 404],
    ["https://agents.example/git/a/b/c.git/info/refs?service=git-upload-pack", "GET", 404],
    ["https://agents.example/git", "GET", 404],
    ["https://agents.example/git/a/b.git/info/refs", "GET", 400],
    [ADV, "POST", 405],
    [PACK, "GET", 405],
  ];
  for (const [url, method, status] of cases) {
    const init = { method, headers: { ...AUTH, "Content-Length": "4" } };
    if (method === "POST") init.body = "0000";
    const resp = await serveGit(new Request(url, init), ENV, { fetchImpl, secrets: SECRETS });
    assert.equal(resp.status, status, url);
  }
  assert.equal(calls.length, 0);
});

test("no bearer, an oversized body and an unset relay are refused before any upstream call", async () => {
  const { calls, fetchImpl } = upstream();
  assert.equal((await serveGit(new Request(ADV), ENV, { fetchImpl, secrets: SECRETS })).status, 401);
  const big = new Request(PACK, { method: "POST", body: "0000", headers: { ...AUTH, "Content-Length": String(9 * 1024 * 1024) } });
  assert.equal((await serveGit(big, ENV, { fetchImpl, secrets: SECRETS })).status, 411);
  assert.equal((await serveGit(new Request(ADV, { headers: AUTH }), {}, { fetchImpl, secrets: SECRETS })).status, 503);
  const noPair = async () => ({ accessId: "", accessSecret: "" });
  assert.equal((await serveGit(new Request(ADV, { headers: AUTH }), ENV, { fetchImpl, secrets: noPair })).status, 503);
  assert.equal(calls.length, 0);
});

test("a missing setting is named, never valued", async () => {
  const { fetchImpl } = upstream();
  const noUrl = await serveGit(new Request(ADV, { headers: AUTH }), {}, { fetchImpl, secrets: SECRETS });
  assert.equal(noUrl.status, 503);
  const noUrlText = await noUrl.text();
  assert.match(noUrlText, /GR_UPSTREAM_URL/);
  assert.doesNotMatch(noUrlText, /accessId|accessSecret/);
  const noPair = async () => ({ accessId: "", accessSecret: "" });
  const noPairResp = await serveGit(new Request(ADV, { headers: AUTH }), ENV, { fetchImpl, secrets: noPair });
  const noPairText = await noPairResp.text();
  assert.match(noPairText, /accessId/);
  assert.match(noPairText, /accessSecret/);
  const halfPair = async () => ({ accessId: "access-id", accessSecret: "" });
  const halfText = await (await serveGit(new Request(ADV, { headers: AUTH }), ENV, { fetchImpl, secrets: halfPair })).text();
  assert.match(halfText, /accessSecret/);
  assert.doesNotMatch(halfText, /access-id/);
});

test("an upstream redirect is refused, never relayed", async () => {
  for (const status of [301, 302, 307, 308]) {
    const { calls, fetchImpl } = upstream(status, { Location: "https://elsewhere.example/git" });
    const resp = await serveGit(new Request(ADV, { headers: AUTH }), ENV, { fetchImpl, secrets: SECRETS });
    assert.equal(resp.status, 502);
    assert.equal(await resp.json().then((b) => b.error), "upstream_redirect_refused");
    assert.equal(resp.headers.get("Location"), null);
    assert.equal(calls.length, 1);
  }
});

test("an upstream failure says nothing about the Access pair", async () => {
  const fetchImpl = async () => { throw new Error("access-secret in a message"); };
  const resp = await serveGit(new Request(ADV, { headers: AUTH }), ENV, { fetchImpl, secrets: SECRETS });
  assert.equal(resp.status, 502);
  assert.doesNotMatch(await resp.text(), /access-secret/);
});

test("only /git and its children count as git paths", () => {
  assert.equal(isGitPath("/git/a/b.git/info/refs"), true);
  assert.equal(isGitPath("/git"), true);
  assert.equal(isGitPath("/github"), false);
  assert.equal(isGitPath("/mcp"), false);
});
