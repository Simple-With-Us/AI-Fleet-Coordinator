// Re-verify the Cloudflare Access JWT on every request to /authorize and /admin
// (spec 3.3):  team-domain certificates, `aud` tag, `exp`, issuer and an owner
// email.  A misconfigured or missing Access app therefore fails closed.
//
// Pure module apart from WebCrypto and an injectable `fetchCerts`, so it runs
// under `node --test` (Node 22+ has `crypto.subtle`).

const CERT_TTL_MS = 10 * 60 * 1000;
const CLOCK_SKEW_S = 60;
const certCache = new Map();

function b64urlToBytes(text) {
  const pad = text.length % 4 === 0 ? "" : "=".repeat(4 - (text.length % 4));
  const bin = atob(text.replace(/-/g, "+").replace(/_/g, "/") + pad);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function b64urlJson(text) {
  return JSON.parse(new TextDecoder().decode(b64urlToBytes(text)));
}

export async function defaultFetchCerts(teamDomain) {
  const res = await fetch(`https://${teamDomain}/cdn-cgi/access/certs`, {
    redirect: "manual",
    signal: AbortSignal.timeout(10_000),
  });
  if (!res.ok) throw new Error(`access certs: HTTP ${res.status}`);
  return res.json();
}

async function getKeys(teamDomain, fetchCerts, now) {
  const hit = certCache.get(teamDomain);
  if (hit && hit.expires > now) return hit.keys;
  const body = await fetchCerts(teamDomain);
  const keys = Array.isArray(body?.keys) ? body.keys : [];
  certCache.set(teamDomain, { keys, expires: now + CERT_TTL_MS });
  return keys;
}

/** For tests:  forget cached certificates. */
export function clearCertCache() {
  certCache.clear();
}

/**
 * Verify `token` (the `Cf-Access-Jwt-Assertion` header).  Returns
 * `{ ok: true, email }` or `{ ok: false, reason }`.  Never throws for a bad
 * token;  a certificate fetch failure is `{ ok: false, reason: "certs_unavailable" }`.
 */
export async function verifyAccessJwt(token, access, { fetchCerts = defaultFetchCerts, now = Date.now() } = {}) {
  if (!access?.configured) return { ok: false, reason: "access_not_configured" };
  if (typeof token !== "string" || token.length === 0 || token.length > 8192) return { ok: false, reason: "no_token" };
  const parts = token.split(".");
  if (parts.length !== 3) return { ok: false, reason: "malformed" };
  let header, claims;
  try {
    header = b64urlJson(parts[0]);
    claims = b64urlJson(parts[1]);
  } catch {
    return { ok: false, reason: "malformed" };
  }
  if (header?.alg !== "RS256" || typeof header.kid !== "string") return { ok: false, reason: "bad_alg" };

  let keys;
  try {
    keys = await getKeys(access.teamDomain, fetchCerts, now);
  } catch {
    return { ok: false, reason: "certs_unavailable" };
  }
  const jwk = keys.find((k) => k?.kid === header.kid && k?.kty === "RSA");
  if (!jwk) return { ok: false, reason: "unknown_kid" };

  let valid = false;
  try {
    const key = await crypto.subtle.importKey(
      "jwk",
      { kty: "RSA", n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    valid = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      b64urlToBytes(parts[2]),
      new TextEncoder().encode(`${parts[0]}.${parts[1]}`),
    );
  } catch {
    return { ok: false, reason: "bad_signature" };
  }
  if (!valid) return { ok: false, reason: "bad_signature" };

  const nowS = Math.floor(now / 1000);
  if (claims?.iss !== `https://${access.teamDomain}`) return { ok: false, reason: "bad_issuer" };
  const aud = Array.isArray(claims.aud) ? claims.aud : [claims.aud];
  if (!aud.includes(access.aud)) return { ok: false, reason: "bad_audience" };
  if (!Number.isFinite(claims.exp) || claims.exp <= nowS) return { ok: false, reason: "expired" };
  if (Number.isFinite(claims.nbf) && claims.nbf > nowS + CLOCK_SKEW_S) return { ok: false, reason: "not_yet_valid" };
  if (Number.isFinite(claims.iat) && claims.iat > nowS + CLOCK_SKEW_S) return { ok: false, reason: "issued_in_future" };
  // A service token carries no email:  agents never pass this gate.
  const email = typeof claims.email === "string" ? claims.email.toLowerCase() : "";
  if (!email || !access.ownerEmails.includes(email)) return { ok: false, reason: "not_owner" };
  return { ok: true, email };
}
