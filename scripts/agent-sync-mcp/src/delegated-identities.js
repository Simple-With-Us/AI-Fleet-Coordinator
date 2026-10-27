// Per-call identity for every hosted grant (AFC-331, docs/protocols/per-call-identity.md sections 2-4).
//
// The grant says who may act (its allowed set) and each call says who is acting, in its `identity`
// argument.  One table decides the sets:
//
//   a CURSOR or GROK-WEB grant   CURSOR, GROK-WEB and every enabled Grok Bot persona (GB_IDENTITIES)
//   every other grant            its own seat
//
// A write with no identity is refused with identity_required;  a read with none acts as the grant's
// own seat.  An identity outside the set is refused with identity_not_allowed.  A removed tool name
// (renamed-tools.json, shared with the stdio server) is refused with unknown_tool naming its
// replacement.  The identity's own credentials do the work:  a peer seat's own SEAT_SECRETS key and
// Linear app, a persona's row of the encrypted GB_IDENTITY_CREDENTIALS bundle;  each is checked
// against Zulip users/me (and, for Linear writes, the viewer) before its first call.  The grant's
// scope, pause and budget still govern every call.
//
// GB_IDENTITIES is read from scripts/agent_sync/mcp/gb-identities.json, the one list the stdio
// server reads too.  No other list of personas exists, here or anywhere.  It comes through
// infisical-manifest.js, which checks the table's shape once:  a malformed table enables no
// persona (an empty list and one logged event) instead of failing this module's load, which
// would stop the whole Worker (kody 4367022121).
import { GB_ENABLED_IDENTITIES } from "./infisical-manifest.js";
import RENAMED from "../../agent_sync/mcp/renamed-tools.json" with { type: "json" };
import { HOSTED_TOOL_LIST, HOSTED_TOOLS_BY_NAME, HOSTED_WRITE_TOOLS, UnknownTool } from "./hosted-tools.js";
import { resolveSecret, infisicalMode } from "./infisical.js";
import { ToolError, errorResult, okResult, scanSecret, schemaProblem } from "./contract.js";
import { EVENTS_ONLY_SEATS, SHARED_IDENTITY_SEATS } from "./config.js";
import { IDENTITIES_SPEC, GUIDE_SPEC } from "./zulip-tool-specs.js";
import { guideResult, GuideArgs, GUIDE_URL } from "./guide.js";
import { canonicalSeatName } from "./identity-aliases.js";
import { parseArgs } from "./args.js";
import { z } from "zod";
import { GUIDE_TEXT } from "./guide-text.js";

const GuideCall = GuideArgs.extend({ identity: z.unknown().optional() });

export const GB_IDENTITIES = GB_ENABLED_IDENTITIES;
const GB_SET = new Set(GB_IDENTITIES);
export const GB_CREDENTIAL_BUNDLE = "GB_IDENTITY_CREDENTIALS";
export const GB_CREDENTIAL_BUNDLE_MAX_BYTES = 5 * 1024;
export const RENAMED_TOOLS = Object.freeze({ ...RENAMED.renamed });
export { SHARED_IDENTITY_SEATS };

/** The identities a grant for `seat` may act as:  its own seat first. */
export function allowedIdentities(seat) {
  if (SHARED_IDENTITY_SEATS.includes(seat)) {
    return Object.freeze([seat, ...SHARED_IDENTITY_SEATS.filter((other) => other !== seat), ...GB_IDENTITIES]);
  }
  return Object.freeze([seat]);
}

/**
 * A caller's identity argument as a canonical name (trimmed, upper case, _ as -, published aliases resolved as the
 * stdio server does:  INSTINCT is WISE, MUSE-ASSIST is MA, GROK-BUILD is GROK), or null.  The grant's allowed set is
 * checked against the resolved name.
 */
export function canonicalIdentity(raw) {
  if (typeof raw !== "string") return null;
  const name = canonicalSeatName(raw.trim().replaceAll("_", "-"));
  return /^[A-Z0-9][A-Z0-9-]{0,31}$/.test(name) ? name : null;
}

export function delegatedNames(identity) {
  if (!GB_SET.has(identity)) return null;
  const role = identity.slice(3).replaceAll("-", "_");
  return Object.freeze({
    zulipKey: `ZULIP_GB_${role}_API_KEY`,
    zulipEmail: `ZULIP_GB_${role}_EMAIL`,
    linearClientId: `LINEAR_GB_${role}_CLIENT_ID`,
    linearClientSecret: `LINEAR_GB_${role}_CLIENT_SECRET`,
  });
}

function completeRole(identity, fields) {
  if (!Array.isArray(fields) || fields.length !== 4 || fields.some((value) => typeof value !== "string" || !value.trim())) return null;
  const [email, zulipKey, linearClientId, linearClientSecret] = fields.map((value) => value.trim());
  const expectedEmail = `${identity.slice(3).toLowerCase()}-grok-bot@simplewithus.zulipchat.com`;
  if (email.toLowerCase() !== expectedEmail) return null;
  return Object.freeze({ email: expectedEmail, zulipKey, linearClientId, linearClientSecret });
}

/**
 * Parse once for this authenticated request.  The encrypted Worker secret is
 * {"v":1,"r":{"GB-ROLE":[email,zulipKey,linearClientId,linearClientSecret]}}.
 * A present but invalid bundle never falls back to legacy bindings.  Legacy
 * rows are accepted only when all four fields belong to the same fixed role.
 */
export function loadDelegatedCredentials(env) {
  const roles = new Map();
  const raw = env?.[GB_CREDENTIAL_BUNDLE];
  if (raw !== undefined && raw !== null) {
    try {
      if (typeof raw !== "string" || new TextEncoder().encode(raw).byteLength > GB_CREDENTIAL_BUNDLE_MAX_BYTES) throw new Error("invalid bundle");
      const bundle = JSON.parse(raw);
      if (bundle?.v !== 1 || !bundle.r || typeof bundle.r !== "object" || Array.isArray(bundle.r)
        || Object.keys(bundle.r).length !== GB_IDENTITIES.length) throw new Error("invalid bundle");
      for (const identity of GB_IDENTITIES) {
        const role = completeRole(identity, bundle.r[identity]);
        if (!role) throw new Error("invalid role");
        roles.set(identity, role);
      }
      return { source: "bundle", valid: true, roles };
    } catch {
      // Never include parsing errors or values in diagnostics.
      return { source: "bundle", valid: false, roles: new Map() };
    }
  }
  for (const identity of GB_IDENTITIES) {
    const names = delegatedNames(identity);
    const row = completeRole(identity, [env?.[names.zulipEmail], env?.[names.zulipKey], env?.[names.linearClientId], env?.[names.linearClientSecret]]);
    if (row) roles.set(identity, row);
  }
  return { source: "legacy", valid: true, roles };
}

/**
 * The GB credential set with Infisical resolution (paths /zulip + /linear).
 * Off mode is exactly loadDelegatedCredentials(env).  Every field of every
 * enabled persona is declared in INFISICAL_EXPECT:  in required mode a
 * missing declared field throws visibly instead of silently dropping a
 * configured persona.  In fallback each field's read falls back to its
 * legacy per-field env binding (observable in logs + stats);  when NO field
 * resolves at all the family falls back to the env bundle/legacy rows as a
 * whole, so a bundle-configured deployment is never silently emptied.  A
 * partial resolution in fallback keeps the bundle row of every persona the
 * Infisical path could not assemble completely (kody 4365622415).
 * completeRole validates every assembled row exactly as for the bundle.
 */
export async function resolveDelegatedCredentials(env) {
  const mode = infisicalMode(env);
  if (mode === "off") return loadDelegatedCredentials(env);
  const roles = new Map();
  let resolvedAny = false;
  await Promise.all(GB_IDENTITIES.map(async (identity) => {
    const names = delegatedNames(identity);
    const [zulipEmail, zulipKey, linearClientId, linearClientSecret] = await Promise.all([
      resolveSecret(env, { name: names.zulipEmail, path: "/zulip", envName: names.zulipEmail, optional: true }),
      resolveSecret(env, { name: names.zulipKey, path: "/zulip", envName: names.zulipKey, optional: true }),
      resolveSecret(env, { name: names.linearClientId, path: "/linear", envName: names.linearClientId, optional: true }),
      resolveSecret(env, { name: names.linearClientSecret, path: "/linear", envName: names.linearClientSecret, optional: true }),
    ]);
    if (zulipEmail || zulipKey || linearClientId || linearClientSecret) resolvedAny = true;
    const row = completeRole(identity, [zulipEmail, zulipKey, linearClientId, linearClientSecret]);
    if (row) roles.set(identity, row);
  }));
  if (!resolvedAny) return loadDelegatedCredentials(env);
  if (mode === "fallback" && roles.size < GB_IDENTITIES.length) {
    // E.g. /zulip served while /linear was out of scope:  every row is
    // incomplete although the bundle still holds them all.  Each missing
    // field was already counted as a fallback by resolveSecret.
    const legacy = loadDelegatedCredentials(env);
    if (legacy.valid) for (const [identity, row] of legacy.roles) if (!roles.has(identity)) roles.set(identity, row);
  }
  return { source: "infisical", valid: true, roles };
}

export function delegatedStatus(credentials, identity) {
  if (!GB_SET.has(identity)) return null;
  const configured = Boolean(credentials?.valid && credentials.roles.has(identity));
  return { identity, zulip: configured ? "configured" : "missing", linear: configured ? "configured" : "missing" };
}

// ---------------------------------------------------------------- tools/list per connection

function identityProperty(allowed, seat) {
  return {
    type: "string",
    enum: [...allowed],
    description: `The bot this call acts as.  Writes must name it;  reads without it act as ${seat}.  identities lists the set.`,
  };
}

function decorate(spec, allowed, seat) {
  const write = HOSTED_WRITE_TOOLS.has(spec.name);
  const inputSchema = {
    ...spec.inputSchema,
    properties: { ...(spec.inputSchema?.properties ?? {}), identity: identityProperty(allowed, seat) },
    ...(write ? { required: [...(spec.inputSchema?.required ?? []), "identity"] } : {}),
  };
  return { ...spec, inputSchema };
}

const LIST_CACHE = new Map();

const EMPTY_TOOL_SET = Object.freeze({ list: Object.freeze([]), byName: Object.freeze({}) });

/** The tools/list a grant for `seat` gets:  every hosted tool with its identity enum, then identities after whoami. */
export function toolListFor(seat, config = {}) {
  // An events-only seat advertises no tools;  the call path refuses every
  // name too (IdentityRouter.call), so this is never discovery-only hiding.
  // `eventsOnlySeats` overrides the production set in tests only.
  const eventsOnlySeats = config.eventsOnlySeats ?? EVENTS_ONLY_SEATS;
  if (eventsOnlySeats.includes(seat)) return EMPTY_TOOL_SET;
  const cacheKey = `${seat}:${config.whatsappEnabled === true}:${config.grWorkflowEnabled === true}:${config.eventsOnlySeats ? config.eventsOnlySeats.join(",") : ""}`;
  const cached = LIST_CACHE.get(cacheKey);
  if (cached) return cached;
  const allowed = allowedIdentities(seat);
  const decorated = HOSTED_TOOL_LIST.filter(spec => (!spec.name.startsWith("whatsapp_") || (seat === "JET-CHAT" && config.whatsappEnabled === true)) && (!spec.name.startsWith("gr_wf_") || config.grWorkflowEnabled === true)).map((spec) => decorate(spec, allowed, seat));
  const at = decorated.findIndex((spec) => spec.name === "whoami") + 1;
  const list = Object.freeze([...decorated.slice(0, at), IDENTITIES_SPEC, decorate(GUIDE_SPEC, allowed, seat), ...decorated.slice(at)]);
  const byName = Object.freeze(Object.fromEntries(list.map((spec) => [spec.name, spec])));
  const entry = Object.freeze({ list, byName });
  LIST_CACHE.set(cacheKey, entry);
  return entry;
}

// ---------------------------------------------------------------- the router

/**
 * One router per authenticated request, for every grant.  `callerTools` is the grant's own
 * HostedTools;  `createIdentity(identity)` builds the HostedTools of another allowed identity from
 * that identity's own credentials (server owned, never from arguments);  `identityStatus(identity)`
 * says whether a peer seat's Zulip and Linear credentials are installed (names only).
 */
export class IdentityRouter {
  constructor({ callerTools, credentials = null, createIdentity = null, identityStatus = null, guideText = GUIDE_TEXT, eventsOnlySeats = callerTools.config?.eventsOnlySeats ?? EVENTS_ONLY_SEATS }) {
    this.guideText = guideText;
    this.callerTools = callerTools;
    this.seat = callerTools.seat;
    this.credentials = credentials;
    this.createIdentity = createIdentity;
    this.identityStatus = identityStatus;
    // Test-only override for the events-only mechanism;  production reads the
    // seats.js constant through the default.
    this.eventsOnlySeats = eventsOnlySeats;
    this.eventsOnly = eventsOnlySeats.includes(this.seat);
    this.allowed = allowedIdentities(this.seat);
    const { list, byName } = toolListFor(this.seat, { ...(callerTools.config ?? {}), eventsOnlySeats });
    this.toolList = list;
    this.toolsByName = byName;
    this.built = new Map();
  }

  /** The identity rule for the MCP `instructions`. */
  instructions() {
    return `  Each call names the bot it acts as in its identity argument:  writes must name it;  reads without it act as ${this.seat}.  ` +
      `This connection may act as ${this.allowed.join(", ")};  identities lists them with their status.  Tools are named by family:  ` +
      "zulip_*, linear_*, recall_*, notes_*, fleetlink_* and listener_*;  whoami, identities and guide stand alone.  " +
      `Call guide for the full tools guide (or one section), also at ${GUIDE_URL}.`;
  }

  async audit(row) {
    await this.callerTools.safely(() => this.callerTools.gate.auditCall({
      seat: this.seat, authenticated_seat: this.seat, grant_ref: this.callerTools.grantRef, client_id: this.callerTools.clientId,
      outcome: "error", retryable: false, ...row,
    }));
  }

  async refuse(name, actingIdentity, error) {
    await this.audit({ acting_identity: actingIdentity, tool: name, error_code: error.code, error_message: error.message.slice(0, 120) });
    return errorResult(error);
  }

  async call(name, rawArgs) {
    // An events-only seat serves notifications only:  every name is unknown
    // to the connection, the same protocol error as any unknown name
    // (buildServer maps UnknownTool), audited like one.
    if (this.eventsOnly) {
      // The requested name is client-controlled:  audit it secret-scanned and
      // length-capped, the same hygiene seat-state applies to error messages.
      // Bound before scanning (kody 4365544427):  an unbounded client string
      // must not reach scanSecret.  The cap is a plain string slice
      // (kody 4365848075):  spreading first would materialize one array
      // element per code point of the unbounded input.
      // Same bound as the seat-state path:  64 code units caps the encoded
      // size at 192 UTF-8 bytes (kody 4366019266), and a trailing lone high
      // surrogate is trimmed (kody 4366019481).
      const requested = (typeof name === "string" ? name : "").slice(0, 64).replace(/[\uD800-\uDBFF]$/, "");
      const audited = scanSecret(requested) ? "<omitted after secret scan>" : requested;
      await this.audit({ acting_identity: "none", tool: audited, error_code: "unknown_tool", error_message: "This connection delivers event notifications only;  it has no tools." });
      // The thrown error carries the AUDITED name too:  a secret-shaped tool
      // name must never echo back to the caller (kody 4363581984).
      throw new UnknownTool(audited);
    }
    if (typeof name === "string" && Object.hasOwn(RENAMED_TOOLS, name)) {
      const replacement = RENAMED_TOOLS[name];
      return this.refuse(name, "none", new ToolError("unknown_tool", `${name} was renamed ${replacement} (AFC-331):  call ${replacement} instead`, { extra: { replacement } }));
    }
    if (name === "identities") return this.identities(rawArgs);
    if (name === "guide") return this.guide(rawArgs);
    // Unknown names stay protocol errors, audited by the grant's own tools.
    if (!Object.hasOwn(HOSTED_TOOLS_BY_NAME, name)) return this.callerTools.call(name, rawArgs);
    // The grant's scope comes first:  a read-only grant learns it cannot write before it learns how to
    // name an identity, and the challenge is the same whichever identity a call names.
    try {
      this.callerTools.requireScope(name);
    } catch (error) {
      const mapped = error instanceof ToolError ? error : new ToolError("unavailable", `The ${this.seat} authorization check failed.`);
      await this.audit({ acting_identity: "none", tool: name, error_code: mapped.code, error_message: `${this.seat} authorization refused.` });
      return errorResult(mapped);
    }
    const args = rawArgs ?? {};
    const isObject = args && typeof args === "object" && !Array.isArray(args);
    const named = isObject && Object.hasOwn(args, "identity");
    const write = HOSTED_WRITE_TOOLS.has(name);
    let identity;
    if (!named) {
      if (write) {
        return this.refuse(name, "none", new ToolError("identity_required", `${name} is a write:  name the bot it acts as in identity (one of ${this.allowed.join(", ")};  identities lists them)`, { extra: { allowed: [...this.allowed] } }));
      }
      identity = this.seat;
    } else {
      identity = canonicalIdentity(args.identity);
      if (!identity || !this.allowed.includes(identity)) {
        return this.refuse(name, "rejected", new ToolError("identity_not_allowed", `That identity is not in this connection's allowed set:  ${this.allowed.join(", ")}`, { extra: { allowed: [...this.allowed] } }));
      }
    }
    // Only the identity property is removed;  inner schemas never see it.  Non-object arguments reach the
    // inner validator unchanged.
    const innerArgs = named ? Object.fromEntries(Object.entries(args).filter(([key]) => key !== "identity")) : rawArgs;
    if (identity === this.seat) return this.callerTools.call(name, innerArgs);
    // The grant's pause applies before another identity is built (its scope was checked above).
    try {
      if (this.callerTools.paused) throw new ToolError("paused", `The ${this.seat} seat is paused.`);
    } catch (error) {
      const mapped = error instanceof ToolError ? error : new ToolError("unavailable", `The ${this.seat} authorization check failed.`);
      await this.audit({ seat: identity, acting_identity: identity, tool: name, error_code: mapped.code, error_message: `${this.seat} authorization refused.` });
      return errorResult(mapped);
    }
    let tools = this.built.get(identity);
    if (!tools) {
      try {
        if (!this.createIdentity) throw new ToolError("not_configured", `This server cannot act as ${identity}.`);
        tools = await this.createIdentity(identity);
        this.built.set(identity, tools);
      } catch (error) {
        const mapped = error instanceof ToolError ? error : new ToolError("unavailable", "Could not load the selected identity.");
        await this.audit({ seat: identity, acting_identity: identity, tool: name, error_code: mapped.code, error_message: "Selected identity unavailable." });
        return errorResult(mapped, (message) => this.callerTools.scrub(message));
      }
    }
    return tools.call(name, innerArgs);
  }

  /** The guide tool:  no identity, no credential, nothing written.  A supplied `identity` is ignored. */
  async guide(rawArgs) {
    const name = "guide";
    try {
      // `identity` is accepted (every tool takes it) and ignored;  anything else must be the guide's own argument.
      const { identity: _ignored, ...inner } = parseArgs(GuideCall, rawArgs);
      this.callerTools.requireScope(name);
      if (this.callerTools.paused) throw new ToolError("paused", `The ${this.seat} seat is paused.`);
      return guideResult(this.guideText, inner);
    } catch (error) {
      const mapped = error instanceof ToolError ? error : new ToolError("unavailable", "The guide is unavailable.");
      await this.audit({ acting_identity: this.seat, tool: name, error_code: mapped.code, error_message: mapped.message.slice(0, 120) });
      return errorResult(mapped);
    }
  }

  status(identity) {
    const recall = this.callerTools.recall ? "configured" : "missing";
    if (identity === this.seat) {
      return { identity, zulip: this.callerTools.zulip ? "configured" : "missing", linear: this.callerTools.linear ? "configured" : "missing", recall };
    }
    if (GB_SET.has(identity)) return { ...delegatedStatus(this.credentials, identity), recall };
    const peer = this.identityStatus ? this.identityStatus(identity) : null;
    return { identity, zulip: peer?.zulip ?? "missing", linear: peer?.linear ?? "missing", recall };
  }

  async identities(rawArgs) {
    const name = "identities";
    try {
      const problem = schemaProblem(IDENTITIES_SPEC.inputSchema, rawArgs ?? {});
      if (problem) throw new ToolError("invalid_argument", problem);
      this.callerTools.requireScope(name);
      if (this.callerTools.paused) throw new ToolError("paused", `The ${this.seat} seat is paused.`);
      await this.callerTools.reserve("read");
    } catch (error) {
      const mapped = error instanceof ToolError ? error : new ToolError("unavailable", "Identity discovery is unavailable.");
      await this.audit({ acting_identity: this.seat, tool: name, error_code: mapped.code, error_message: "Identity discovery unavailable." });
      return errorResult(mapped);
    }
    const info = {
      default_identity: this.seat,
      authenticated_seat: this.seat,
      transport: "hosted",
      identities: this.allowed.map((identity) => this.status(identity)),
    };
    await this.callerTools.safely(() => this.callerTools.gate.auditCall({
      seat: this.seat, authenticated_seat: this.seat, acting_identity: this.seat,
      grant_ref: this.callerTools.grantRef, client_id: this.callerTools.clientId, tool: name, outcome: "ok",
    }));
    return okResult(JSON.stringify(info), info);
  }
}
