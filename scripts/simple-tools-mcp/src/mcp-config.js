// Fleet MCP list config (AFC-890 C2).  One admin-edited record on the Worker: `defaults` for every bot,
// `bf` for every BF-* bot, `perSeat[seat]` for one bot.  Names and https URLs only:  a URL with credentials,
// a query or a fragment is refused, so a secret cannot ride in here.  The record is never a credential store.
import { adminApiRequestRefusal } from "./admin-api.js";

export const MCP_CONFIG_KEY = "stmcp:admin:mcp-config:v1";
const NAME_RE = /^[a-z0-9][a-z0-9._-]{0,47}$/;
const MAX_ENTRIES = 40;
const MAX_URL = 300;
const MAX_BODY = 16384;
const BF_RE = /^BF-[A-Z0-9-]{1,32}$/;

export const EMPTY_CONFIG = Object.freeze({ version: 0, defaults: [], bf: [], perSeat: {} });

/** A list of { name, url } as a clean array, or { error }. */
export function parseEntries(value) {
  if (!Array.isArray(value) || value.length > MAX_ENTRIES) return { error: "bad_entries" };
  const seen = new Set();
  const out = [];
  for (const e of value) {
    if (!e || typeof e !== "object" || Array.isArray(e)) return { error: "bad_entries" };
    if (Object.keys(e).some((k) => k !== "name" && k !== "url")) return { error: "bad_entries" };
    if (typeof e.name !== "string" || !NAME_RE.test(e.name) || seen.has(e.name)) return { error: "bad_name" };
    if (typeof e.url !== "string" || e.url.length > MAX_URL) return { error: "bad_url" };
    let u;
    try { u = new URL(e.url); } catch { return { error: "bad_url" }; }
    if (u.protocol !== "https:" || u.username || u.password || u.search || u.hash || !u.hostname) return { error: "bad_url" };
    seen.add(e.name);
    out.push({ name: e.name, url: u.href });
  }
  return { value: out };
}

function parseNames(value) {
  if (!Array.isArray(value) || value.length > MAX_ENTRIES || !value.every((n) => typeof n === "string" && NAME_RE.test(n))) return { error: "bad_names" };
  return { value: [...new Set(value)] };
}

/** A seat override: { replace } alone, or any of { add, remove }.  Empty override means inherit. */
export function parseOverride(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { error: "bad_override" };
  const keys = Object.keys(raw);
  if (keys.some((k) => !["replace", "add", "remove"].includes(k))) return { error: "bad_override" };
  if (raw.replace !== undefined && (raw.add !== undefined || raw.remove !== undefined)) return { error: "replace_excludes_add_remove" };
  const out = {};
  if (raw.replace !== undefined) { const r = parseEntries(raw.replace); if (r.error) return r; out.replace = r.value; }
  if (raw.add !== undefined) { const r = parseEntries(raw.add); if (r.error) return r; out.add = r.value; }
  if (raw.remove !== undefined) { const r = parseNames(raw.remove); if (r.error) return r; out.remove = r.value; }
  return { value: out };
}

/** The stored record, re-validated; anything unreadable is reported, never defaulted silently. */
export function normalizeStored(raw) {
  if (raw === null || raw === undefined) return { value: { ...EMPTY_CONFIG, perSeat: {} } };
  let obj = raw;
  if (typeof raw === "string") { try { obj = JSON.parse(raw); } catch { return { error: "stored_unreadable" }; } }
  if (!obj || typeof obj !== "object" || !Number.isInteger(obj.version) || obj.version < 0) return { error: "stored_unreadable" };
  const d = parseEntries(obj.defaults ?? []);
  const b = parseEntries(obj.bf ?? []);
  if (d.error || b.error || !obj.perSeat || typeof obj.perSeat !== "object") return { error: "stored_unreadable" };
  const perSeat = {};
  for (const [seat, ov] of Object.entries(obj.perSeat)) {
    const p = parseOverride(ov);
    if (p.error) return { error: "stored_unreadable" };
    perSeat[seat] = p.value;
  }
  return { value: { version: obj.version, defaults: d.value, bf: b.value, perSeat } };
}

/** The effective list for one seat: defaults, then bf (BF-* only), then the seat's own override. */
export function effectiveFor(config, seat) {
  let list = [...config.defaults];
  if (BF_RE.test(seat)) {
    const byName = new Map(list.map((e) => [e.name, e]));
    for (const e of config.bf) byName.set(e.name, e);
    list = [...byName.values()];
  }
  const ov = config.perSeat[seat];
  if (!ov) return list;
  if (ov.replace) return [...ov.replace];
  const byName = new Map(list.filter((e) => !(ov.remove ?? []).includes(e.name)).map((e) => [e.name, e]));
  for (const e of ov.add ?? []) byName.set(e.name, e);
  return [...byName.values()];
}

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store, private", Pragma: "no-cache" },
  });
}

async function readCapped(request) {
  if (!request.body) return "";
  const reader = request.body.getReader();
  const decoder = new TextDecoder();
  let total = 0;
  let text = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > MAX_BODY) { try { await reader.cancel(); } catch { /* ignore */ } return null; }
    text += decoder.decode(value, { stream: true });
  }
  return text + decoder.decode();
}

function view(config, seats, scope) {
  const bf = scope === "bf";
  const shown = seats.filter((s) => !bf || BF_RE.test(s));
  const perSeat = {};
  for (const [s, ov] of Object.entries(config.perSeat)) if (!bf || BF_RE.test(s)) perSeat[s] = ov;
  return {
    version: config.version,
    scope,
    ...(bf ? {} : { defaults: config.defaults }),
    bf: config.bf,
    perSeat,
    effective: Object.fromEntries(shown.map((s) => [s, effectiveFor(config, s)])),
  };
}

/**
 * deps: { issuer, actor, scope: "owner" | "bf", seats: string[] (every known seat), kv: { get(key), put(key, value) },
 *         audit(event) }.  Reads are GET; changes are POST { action, ... , expected_version }.
 *   set_defaults { entries }            owner only
 *   set_bf { entries }                  owner or bf
 *   set_seat { seat, override }         bf scope: BF-* seats only
 *   clear_seat { seat }
 */
export async function serveMcpConfig(request, deps) {
  let rawStored;
  try { rawStored = await deps.kv.get(MCP_CONFIG_KEY); } catch { return json(503, { ok: false, error: "store_unavailable" }); }
  const stored = normalizeStored(rawStored);
  if (stored.error) return json(503, { ok: false, error: stored.error });
  const config = stored.value;
  const bfScope = deps.scope === "bf";
  if (request.method === "GET") return json(200, { ok: true, ...view(config, deps.seats, deps.scope) });
  const refusal = adminApiRequestRefusal(request, deps.issuer);
  if (refusal) return json(refusal === "method" ? 405 : 403, { ok: false, error: "request_refused", reason: refusal });
  const raw = await readCapped(request).catch(() => null);
  if (raw === null) return json(413, { ok: false, error: "bad_body" });
  let body;
  try { body = JSON.parse(raw); } catch { return json(400, { ok: false, error: "bad_body" }); }
  if (!body || typeof body !== "object" || Array.isArray(body)) return json(400, { ok: false, error: "bad_body" });
  if (!Number.isInteger(body.expected_version)) return json(400, { ok: false, error: "expected_version_required" });
  if (body.expected_version !== config.version) return json(409, { ok: false, error: "version_conflict", version: config.version });
  const next = { version: config.version + 1, defaults: config.defaults, bf: config.bf, perSeat: { ...config.perSeat } };
  const { action } = body;
  let detail = { action };
  if (action === "set_defaults") {
    if (bfScope) return json(403, { ok: false, error: "scope_refused" });
    const r = parseEntries(body.entries);
    if (r.error) return json(400, { ok: false, error: r.error });
    next.defaults = r.value;
  } else if (action === "set_bf") {
    const r = parseEntries(body.entries);
    if (r.error) return json(400, { ok: false, error: r.error });
    next.bf = r.value;
  } else if (action === "set_seat" || action === "clear_seat") {
    const seat = body.seat;
    if (typeof seat !== "string" || !deps.seats.includes(seat)) return json(400, { ok: false, error: "unknown_seat" });
    if (bfScope && !BF_RE.test(seat)) return json(403, { ok: false, error: "scope_refused" });
    detail = { action, seat };
    if (action === "clear_seat") delete next.perSeat[seat];
    else {
      const r = parseOverride(body.override);
      if (r.error) return json(400, { ok: false, error: r.error });
      next.perSeat[seat] = r.value;
    }
  } else return json(400, { ok: false, error: "unknown_action" });
  await deps.kv.put(MCP_CONFIG_KEY, JSON.stringify(next));
  try { await deps.audit({ event: "mcp_config_changed", by: deps.actor, scope: deps.scope, version: next.version, ...detail }); } catch { /* the change stands; the audit gap is the caller's log */ }
  return json(200, { ok: true, ...view(next, deps.seats, deps.scope) });
}
