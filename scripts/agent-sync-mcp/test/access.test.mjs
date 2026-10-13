import test from "node:test";
import assert from "node:assert/strict";
import { verifyAccessJwt, clearCertCache } from "../src/access.js";

const TEAM = "silent-frost-37e0.cloudflareaccess.com";
const AUD = "b".repeat(64);
const access = { configured: true, teamDomain: TEAM, aud: AUD, ownerEmails: ["mail@jays.services", "second-owner@example.com"] };
const NOW = Date.parse("2026-10-09T06:00:00Z");
const nowS = Math.floor(NOW / 1000);

const b64url = (bytes) => Buffer.from(bytes).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const enc = (obj) => b64url(new TextEncoder().encode(JSON.stringify(obj)));

const { privateKey, publicKey } = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true,
  ["sign", "verify"],
);
const other = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true,
  ["sign", "verify"],
);
const jwk = { ...(await crypto.subtle.exportKey("jwk", publicKey)), kid: "k1" };
const certs = { keys: [jwk] };
const fetchCerts = async (domain) => {
  assert.equal(domain, TEAM);
  return certs;
};

async function sign(claims, { kid = "k1", alg = "RS256", key = privateKey } = {}) {
  const head = enc({ alg, kid, typ: "JWT" });
  const body = enc(claims);
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(`${head}.${body}`));
  return `${head}.${body}.${b64url(new Uint8Array(sig))}`;
}

const good = () => ({ iss: `https://${TEAM}`, aud: [AUD], exp: nowS + 600, iat: nowS - 10, nbf: nowS - 10, email: "Mail@Jays.Services", type: "app" });
const verify = (token, acc = access) => verifyAccessJwt(token, acc, { fetchCerts, now: NOW });

test("a valid owner token passes", async () => {
  clearCertCache();
  assert.deepEqual(await verify(await sign(good())), { ok: true, email: "mail@jays.services" });
  assert.deepEqual(await verify(await sign({ ...good(), email: "second-owner@example.com" })), { ok: true, email: "second-owner@example.com" });
});

test("every bad claim fails closed", async () => {
  clearCertCache();
  const cases = [
    [{ ...good(), aud: ["c".repeat(64)] }, "bad_audience"],
    [{ ...good(), iss: "https://evil.cloudflareaccess.com" }, "bad_issuer"],
    [{ ...good(), exp: nowS - 1 }, "expired"],
    [{ ...good(), exp: undefined }, "expired"],
    [{ ...good(), nbf: nowS + 3600 }, "not_yet_valid"],
    [{ ...good(), email: "someone@example.com" }, "not_owner"],
    [{ ...good(), email: undefined, common_name: "service-token-id" }, "not_owner"],
  ];
  for (const [claims, reason] of cases) {
    const got = await verify(await sign(claims));
    assert.deepEqual(got, { ok: false, reason }, JSON.stringify(claims));
  }
});

test("signature, key id and algorithm are enforced", async () => {
  clearCertCache();
  assert.equal((await verify(await sign(good(), { key: other.privateKey }))).reason, "bad_signature");
  assert.equal((await verify(await sign(good(), { kid: "nope" }))).reason, "unknown_kid");
  assert.equal((await verify(await sign(good(), { alg: "HS256" }))).reason, "bad_alg");
  const t = await sign(good());
  const [h, , s] = t.split(".");
  assert.equal((await verify(`${h}.${enc({ ...good(), email: "mail@jays.services", aud: [AUD], admin: true })}.${s}`)).reason, "bad_signature");
  assert.equal((await verify("")).reason, "no_token");
  assert.equal((await verify(null)).reason, "no_token");
  assert.equal((await verify("a.b")).reason, "malformed");
});

test("unconfigured Access or unreachable certs fail closed", async () => {
  clearCertCache();
  const token = await sign(good());
  assert.equal((await verify(token, { ...access, configured: false })).reason, "access_not_configured");
  const broken = await verifyAccessJwt(token, access, { fetchCerts: async () => { throw new Error("down"); }, now: NOW });
  assert.equal(broken.reason, "certs_unavailable");
});
