// Per-call native metadata is a client assertion, never another principal or permission.
// CODEX uses exact thread_id; ZC uses its namespaced ToolExecutor trace session_id.
// Never substitute shared roots, environment, workspace inference or tool arguments.
const SESSION = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ZC_SESSION = /^sess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const result = (state, session = null) => Object.freeze({ state, session });

export function nativeOriginContext(meta) {
  if (meta === undefined) return result("missing");
  if (!object(meta)) return result("invalid");
  if (Object.hasOwn(meta, "com.zcode/request-context")) {
    if (Object.hasOwn(meta, "threadId") || Object.hasOwn(meta, "x-codex-turn-metadata")) return result("conflict");
    const ctx = meta["com.zcode/request-context"];
    if (!object(ctx) || !ZC_SESSION.test(ctx.session_id ?? "")
      || typeof ctx.turn_id !== "string" || !ctx.turn_id
      || typeof ctx.runtime_scope !== "string" || !ctx.runtime_scope) return result("invalid");
    for (const name of ["session_id", "turn_id", "runtime_scope", "workspace_path", "workspace_identity", "workspace_key", "remote_session_id"]) {
      if (Object.hasOwn(meta, name) && meta[name] !== ctx[name]) return result("conflict");
    }
    return Object.freeze({state: "exact", session: ctx.session_id, platform: "zcode"});
  }
  const candidates = [];
  if (Object.hasOwn(meta, "threadId")) candidates.push(meta.threadId);
  if (Object.hasOwn(meta, "x-codex-turn-metadata")) {
    const turn = meta["x-codex-turn-metadata"];
    if (!object(turn)) return result("invalid");
    if (Object.hasOwn(turn, "thread_id")) candidates.push(turn.thread_id);
  }
  if (!candidates.length) return result("missing");
  if (candidates.some((value) => typeof value !== "string" || !SESSION.test(value))) return result("invalid");
  if (candidates.some((value) => value !== candidates[0])) return result("conflict");
  return result("exact", candidates[0]);
}

// Preparation seam only: a service-owned, creation-time resolver must supply the
// existing three-claim envelope. This module does not select a registry or credential.
// Callers must still record the claims in the protected creation receipt and run
// the existing independent Operator binding checks before any review alert effect.
export async function resolveNativeOriginClaims(seat, context, resolver = null) {
  const held = (reason) => Object.freeze({ state: "held", reason, claims: null });
  if (!["CODEX", "ZC"].includes(seat)) return held("unsupported-seat");
  const exact = seat === "ZC" ? context?.platform === "zcode" && ZC_SESSION.test(context.session ?? "")
    : context?.platform !== "zcode" && SESSION.test(context?.session ?? "");
  if (context?.state !== "exact" || !exact) return held("no-exact-thread");
  if (typeof resolver !== "function") return held("lookup-unavailable");
  let claims;
  try {
    claims = await resolver(Object.freeze({ seat, session: context.session }));
  } catch {
    return held("lookup-unavailable");
  }
  if (!object(claims) || claims.origin_session_claim !== context.session
    || typeof claims.origin_letter_claim !== "string" || !/^[A-Z]{2}$/.test(claims.origin_letter_claim)
    || !Number.isSafeInteger(claims.origin_generation_claim) || claims.origin_generation_claim < 1
    || Object.keys(claims).some((key) => !["origin_session_claim", "origin_letter_claim", "origin_generation_claim"].includes(key))) {
    return held("lookup-unverified");
  }
  return Object.freeze({ state: "resolved", reason: null, claims: Object.freeze({ ...claims }) });
}
