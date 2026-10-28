import { WA_EVENT, WA_HEADER, WA_MAX_BODY, binding, verifyRequest } from "./contract.js";
import { subEventName, deliverEvent, eventIdFor } from "../events.js";
import { WAKE_SECRETS } from "../config.js";
import { infisicalMode, prefetchFolder, resolveSecret } from "../infisical.js";
const reply = (body, status = 200) => Response.json(body, { status, headers: { "Cache-Control": "no-store" } });

export async function deliverPending(gate, fetchFn, deadline = Date.now() + 10000, source) {
  const subs = (await gate.eventSubscriptions({})).filter(s => subEventName(s) === WA_EVENT);
  if (!subs.length) return { delivered: 0, pending: true };
  const destination = subs.map(s => `${s.id}:${s.refresh_before}`).join(",");
  const pending = await gate.waClaim({ destination, ...(source ? { source } : {}) });
  if (!pending) return { delivered: 0 };
  let delivered = 0; let permanent = false;
  try {
    for (const sub of subs) {
      let result = { outcome: "outcome_unknown", status: null, attempts: 0 };
      try {
        const event = { name: WA_EVENT, eventId: await eventIdFor(`${pending.wake_id ?? pending.id}:ring:${pending.ring ?? 0}`, sub.id), timestamp: new Date().toISOString(), data: { source: pending.source, owner_authorized: pending.source === "authenticated_owner" }, cursor: null };
        result = await deliverEvent(fetchFn, sub, event, { deadline, isLive: () => gate.eventSubLive({ subId: sub.id }) });
        if (result.outcome === "failed" && (result.status == null || result.status === 408 || result.status >= 500)) result = { ...result, outcome: "outcome_unknown" };
      } catch { /* The stable event id remains pending for reconciliation. */ }
      finally {
        try { await gate.eventDeliveryResult({ subId: sub.id, outcome: result.outcome, status: result.status, attempts: result.attempts, wakeRef: "wa", messageId: null }); }
        catch { console.log(JSON.stringify({ event: "wa_delivery_audit_failed" })); }
      }
      if (result.outcome === "delivered") delivered++;
      if (["rejected", "gone"].includes(result.outcome)) permanent = true;
    }
  } finally {
    await gate.waSettle({ key: pending.key, keys: pending.keys, token: pending.token, delivered: delivered > 0, permanent, noSubscriber: !subs.length });
  }
  return { delivered, pending: pending.source === "authenticated_owner" || delivered === 0 };
}

// Called only after routing to the private module.  Distinct keys prevent an
// Operator from forging WhatsApp owner provenance, or the reverse.
export async function serveWa(request, env, config, gate, fetchFn = fetch) {
  if (!config.whatsappEnabled || !config.hostedSeats.includes("JET")) return reply({ ok: false, reason: "disabled" }, 404);
  if (config.mcpDisabled) return reply({ ok: false, reason: "mcp_disabled" }, 503);
  // AFC-545: the bridge's outbox claim and the result it posts back ride the
  // same signed channel as the ingest.
  const path = new URL(request.url).pathname;
  const kind = path === "/internal/wa-operator/JET" ?  "operator"
    : path === "/internal/wa-outbox/JET" ?  "outbox"
      : path === "/internal/wa-outbox/JET/result" ?  "outbox_result" : "ingest";
  const owner = binding(env.WA_OWNER_BINDING_JET);
  const keyName = kind === "operator" ? "WA_OPERATOR_HMAC_KEY_JET" : "WA_BRIDGE_HMAC_KEY_JET";
  const otherWaName = kind === "operator" ? "WA_BRIDGE_HMAC_KEY_JET" : "WA_OPERATOR_HMAC_KEY_JET";
  // Both WA keys are declared at Fleet-OPS /wake (src/infisical-manifest.js), so they resolve through Infisical
  // like the wake keys (kody 4365623241);  off mode reads the binding exactly as before, untrimmed.
  const viaInfisical = infisicalMode(env) !== "off";
  if (viaInfisical) await prefetchFolder(env, "/wake");
  const wakeRead = (name) => resolveSecret(env, { name, path: "/wake", envName: name, optional: true });
  const rawKey = viaInfisical ? await wakeRead(keyName) : env[keyName];
  const bindingKeys = Object.entries(env).filter(([k,v]) => /(?:WAKE_HMAC_KEY_|_ROUTINE_KEY$|^SPIKE_HMAC_KEY$|^HEALTH_HMAC_KEY$|^WA_.*HMAC_KEY)/.test(k) && k !== keyName && typeof v === "string").map(([,v]) => v.trim());
  // The reuse guard also sees the keys Infisical serves, which need not be Worker bindings any more.
  const resolvedKeys = viaInfisical ? (await Promise.all([otherWaName, ...Object.values(WAKE_SECRETS)].map(wakeRead))).map((v) => v.trim()).filter(Boolean) : [];
  const otherKeys = [...new Set([...bindingKeys, ...resolvedKeys])];
  if (kind === "ingest" && !owner) return reply({ ok: false, reason: "not_configured", required: "WA_OWNER_BINDING_JET" }, 404);
  if ((kind === "outbox" || kind === "outbox_result") && !owner) return reply({ ok: false, reason: "not_configured", required: "WA_OWNER_BINDING_JET" }, 404);
  if (typeof rawKey !== "string" || rawKey.trim().length < 32) return reply({ ok: false, reason: "not_configured", required: keyName }, 404);
  if (otherKeys.includes(rawKey.trim())) return reply({ ok: false, reason: "key_reused", required: keyName }, 404);
  if (request.method !== "POST") return reply({ ok: false }, 405);
  if (!/^[a-f0-9]{64}$/.test(request.headers.get(WA_HEADER) ?? "")) return reply({ ok: false }, 401);
  if (env.TOKEN_RATE_LIMITER) {
    const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
    const limit = await env.TOKEN_RATE_LIMITER.limit({ key: `wa:${kind}:${ip}` });
    if (!limit.success) return Response.json({ ok: false, reason: "transport_rate_limited" }, { status: 429, headers: { "Retry-After": "60", "Cache-Control": "no-store" } });
  }
  // Bounded streaming even when Content-Length is absent or dishonest.
  const reader = request.body?.getReader();
  if (!reader) return reply({ ok: false }, 400);
  const chunks = []; let size = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > WA_MAX_BODY) { await reader.cancel(); return reply({ ok: false }, 413); }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size); let at = 0;
  for (const c of chunks) { bytes.set(c, at); at += c.length; }
  const checked = await verifyRequest(bytes, request.headers.get(WA_HEADER), rawKey, kind);
  if (checked.status !== 200) return reply({ ok: false }, checked.status);
  let result;
  if (kind === "ingest") result = await gate.waIngest({ body: checked.body, owner, known: [rawKey, ...otherKeys] });
  else if (kind === "operator") result = await gate.waOperator({ requestId: checked.body.request_id });
  else if (kind === "outbox") result = await gate.waOutboxClaim({});
  else result = await gate.waOutboxResult({ body: checked.body });
  if (!result.ok) {
    if (result.reason === "operator_busy") {
      // A new request can recover an older pending wake even if the WhatsApp
      // poller is down.  The new id remains unaccepted; never replay it blindly.
      if (config.whatsappWakeEnabled) {
        try { await deliverPending(gate, fetchFn, Date.now() + 10000, "operator"); }
        catch { console.log(JSON.stringify({ event: "wa_delivery_failed" })); }
      }
      return Response.json(result, { status: 429, headers: { "Cache-Control": "no-store", "Retry-After": String(result.retry_after_s) } });
    }
    return reply(result, 403);
  }
  // The outbox routes carry no wake: they answer the bridge, not Jet.
  if (kind === "outbox" || kind === "outbox_result") return reply(result);
  let delivery = { delivered: 0, disabled: true };
  if (config.whatsappWakeEnabled) {
    try { delivery = await deliverPending(gate, fetchFn, Date.now() + 10000, kind === "operator" ? "operator" : undefined); }
    catch { console.log(JSON.stringify({ event: "wa_delivery_failed" })); delivery = { delivered: 0, pending: true }; }
  }
  return reply({ ...result, ...delivery });
}
