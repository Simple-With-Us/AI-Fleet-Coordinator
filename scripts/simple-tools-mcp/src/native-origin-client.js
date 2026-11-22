import { z } from "zod";

const OriginClaims = z.object({
  origin_session_claim: z.string().regex(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/),
  origin_letter_claim: z.string().regex(/^[A-Z]{2}$/),
  origin_generation_claim: z.number().int().min(1).max(Number.MAX_SAFE_INTEGER),
}).strict();

// A service-owned dedicated read credential. Never accept endpoint/key from tool args.
export function nativeOriginResolver(env, { fetcher = fetch, now = Date.now } = {}) {
  const endpoint = env?.REVIEW_ORIGIN_READ_URL;
  const key = env?.REVIEW_ORIGIN_CODEX_READ_KEY;
  if (!endpoint || typeof key !== "string" || key.length < 32) return null;
  let base;
  try {
    base = new URL(endpoint);
    if (base.protocol !== "https:" || base.username || base.password || base.search || base.hash
      || base.pathname !== "/registry/v1/review-origin") return null;
  } catch { return null; }
  return async ({ seat, session }) => {
    if (seat !== "CODEX") throw new Error("unsupported-seat");
    const url = new URL(base);
    url.searchParams.set("native_session_id", session);
    const stamp = String(Math.floor(now() / 1000));
    const message = `simple-tools-review-origin/1\nGET\n${url.pathname}\n${url.search.slice(1)}\n${stamp}`;
    const signingKey = await crypto.subtle.importKey("raw", new TextEncoder().encode(key), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const bytes = new Uint8Array(await crypto.subtle.sign("HMAC", signingKey, new TextEncoder().encode(message)));
    const signature = [...bytes].map((n) => n.toString(16).padStart(2, "0")).join("");
    const response = await fetcher(url.href, { method: "GET", redirect: "manual", cache: "no-store",
      signal: AbortSignal.timeout(3000), headers: {
        "X-Simple-Tools-Timestamp": stamp, "X-Simple-Tools-Signature": signature,
      } });
    if (response.status !== 200) throw new Error("lookup-held");
    const reader = response.body?.getReader();
    if (!reader) throw new Error("lookup-empty");
    let text = "";
    const decoder = new TextDecoder("utf-8", { fatal: true });
    let count = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        count += value.byteLength;
        if (count > 1024) throw new Error("lookup-large");
        text += decoder.decode(value, { stream: true });
      }
      text += decoder.decode();
    } finally { await reader.cancel(); }
    const claims = OriginClaims.parse(JSON.parse(text));
    if (claims.origin_session_claim !== session) throw new Error("lookup-session-mismatch");
    return claims;
  };
}
