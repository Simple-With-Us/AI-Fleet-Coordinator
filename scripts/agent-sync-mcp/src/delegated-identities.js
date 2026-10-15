// A CURSOR OAuth connection may act as one of these owner-authorized Grok Bot
// personas for a single tool call.  They are not OAuth seats or wake targets.
import { HOSTED_TOOL_LIST, HOSTED_TOOLS_BY_NAME } from "./hosted-tools.js";
import { ToolError, errorResult, okResult, schemaProblem } from "./contract.js";

export const GB_IDENTITIES = Object.freeze([
  "GB-DIRECTOR", "GB-FIXER", "GB-DESIGNER", "GB-COMPILER",
  "GB-HOUSEKEEPER", "GB-PUBLISHER", "GB-DEPLOYER", "GB-MONITOR",
  "GB-PLUMBER", "GB-ORACLE", "GB-TRADER",
]);
export const CURSOR_IDENTITIES = Object.freeze(["CURSOR", ...GB_IDENTITIES]);
const GB_SET = new Set(GB_IDENTITIES);
export const GB_CREDENTIAL_BUNDLE = "GB_IDENTITY_CREDENTIALS";
export const GB_CREDENTIAL_BUNDLE_MAX_BYTES = 5 * 1024;

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

export function delegatedStatus(credentials, identity) {
  if (!GB_SET.has(identity)) return null;
  const configured = credentials.valid && credentials.roles.has(identity);
  return { identity, zulip: configured ? "configured" : "missing", linear: configured ? "configured" : "missing" };
}

const DISCOVERY = Object.freeze({
  name: "agent_sync_identities",
  title: "Agent Sync Identities",
  description: "List identities this CURSOR connection may select and whether each service is configured.  No credentials are returned.",
  inputSchema: { type: "object", additionalProperties: false, properties: {} },
  outputSchema: {
    type: "object", required: ["authenticated_seat", "identities"],
    properties: {
      authenticated_seat: { type: "string" },
      identities: { type: "array", items: {
        type: "object", required: ["identity", "zulip", "linear", "recall"],
        properties: { identity: { type: "string" }, zulip: { type: "string" }, linear: { type: "string" }, recall: { type: "string" } },
      } },
    },
  },
  annotations: { readOnlyHint: true, openWorldHint: false },
});

const routedTool = (spec) => ({
  ...spec,
  inputSchema: {
    ...spec.inputSchema,
    properties: {
      ...(spec.inputSchema?.properties ?? {}),
      identity: { type: "string", enum: [...CURSOR_IDENTITIES], default: "CURSOR", description: "Act as this identity for this call.  The CURSOR OAuth grant remains the caller." },
    },
  },
  ...(["whoami", "linear_whoami"].includes(spec.name) && spec.outputSchema ? {
    outputSchema: {
      ...spec.outputSchema,
      properties: { ...spec.outputSchema.properties, authenticated_seat: { type: "string" } },
    },
  } : {}),
});

export const CURSOR_TOOL_LIST = Object.freeze([...HOSTED_TOOL_LIST.map(routedTool), DISCOVERY]);
export const CURSOR_TOOLS_BY_NAME = Object.freeze(Object.fromEntries(CURSOR_TOOL_LIST.map((tool) => [tool.name, tool])));

/** All routing state belongs to this request.  `createRole` is server owned. */
export class IdentityRouter {
  constructor({ callerTools, credentials, createRole }) {
    this.callerTools = callerTools;
    this.seat = callerTools.seat;
    this.credentials = credentials;
    this.createRole = createRole;
  }

  async call(name, rawArgs) {
    if (name === DISCOVERY.name) {
      try {
        const problem = schemaProblem(DISCOVERY.inputSchema, rawArgs ?? {});
        if (problem) throw new ToolError("invalid_argument", problem);
        this.callerTools.requireScope("whoami");
        if (this.callerTools.paused) throw new ToolError("paused", "The CURSOR seat is paused.");
        await this.callerTools.reserve("read");
      } catch (error) {
        const mapped = error instanceof ToolError ? error : new ToolError("unavailable", "Identity discovery is unavailable.");
        await this.callerTools.safely(() => this.callerTools.gate.auditCall({
          seat: "CURSOR", authenticated_seat: "CURSOR", acting_identity: "CURSOR",
          grant_ref: this.callerTools.grantRef, client_id: this.callerTools.clientId,
          tool: name, outcome: "error", error_code: mapped.code, error_message: "Identity discovery unavailable.", retryable: false,
        }));
        return errorResult(mapped);
      }
      const identities = [
        { identity: "CURSOR", zulip: this.callerTools.zulip ? "configured" : "missing", linear: this.callerTools.linear ? "configured" : "missing", recall: this.callerTools.recall ? "configured" : "missing" },
        ...GB_IDENTITIES.map((identity) => ({ ...delegatedStatus(this.credentials, identity), recall: this.callerTools.recall ? "configured" : "missing" })),
      ];
      const info = { authenticated_seat: "CURSOR", identities };
      await this.callerTools.safely(() => this.callerTools.gate.auditCall({
        seat: "CURSOR", authenticated_seat: "CURSOR", acting_identity: "CURSOR",
        grant_ref: this.callerTools.grantRef, client_id: this.callerTools.clientId,
        tool: name, outcome: "ok",
      }));
      return okResult(JSON.stringify(info), info);
    }
    // Keep unknown names as protocol errors, exactly as the ordinary server.
    if (!Object.hasOwn(HOSTED_TOOLS_BY_NAME, name)) return this.callerTools.call(name, rawArgs);
    const args = rawArgs ?? {};
    // Selection is read from this call only.  A preceding call cannot change
    // the default for another call, including parallel Cursor agents.
    const identity = args && typeof args === "object" && !Array.isArray(args) && Object.hasOwn(args, "identity") ? args.identity : "CURSOR";
    if (!CURSOR_IDENTITIES.includes(identity)) {
      await this.callerTools.safely(() => this.callerTools.gate.auditCall({
        seat: "CURSOR", authenticated_seat: "CURSOR", acting_identity: "rejected",
        grant_ref: this.callerTools.grantRef, client_id: this.callerTools.clientId,
        tool: name, outcome: "error", error_code: "invalid_argument", error_message: "Invalid identity.", retryable: false,
      }));
      return errorResult(new ToolError("invalid_argument", "identity must be one of the advertised identities"));
    }
    // Only the wrapper property is removed; inner schemas and dispatch never
    // see it.  Non-object arguments reach the original validator unchanged.
    const innerArgs = args && typeof args === "object" && !Array.isArray(args)
      ? Object.fromEntries(Object.entries(args).filter(([key]) => key !== "identity")) : args;
    if (identity === "CURSOR") return this.callerTools.call(name, innerArgs);
    // Scope and pause on the CURSOR grant apply before a GB role is built.
    // The role's own pause and CURSOR's live reserve gate are checked later.
    let callerError;
    try {
      this.callerTools.requireScope(name);
      if (this.callerTools.paused) throw new ToolError("paused", "The CURSOR seat is paused.");
    } catch (error) {
      callerError = error instanceof ToolError ? error : new ToolError("unavailable", "The CURSOR authorization check failed.");
    }
    if (callerError) {
      await this.callerTools.safely(() => this.callerTools.gate.auditCall({
        seat: identity, authenticated_seat: "CURSOR", acting_identity: identity,
        grant_ref: this.callerTools.grantRef, client_id: this.callerTools.clientId,
        tool: name, outcome: "error", error_code: callerError.code, error_message: "CURSOR authorization refused.", retryable: false,
      }));
      return errorResult(callerError);
    }
    let roleTools;
    try {
      roleTools = await this.createRole(identity);
    } catch (error) {
      const mapped = error instanceof ToolError ? error : new ToolError("unavailable", "Could not load the selected identity.");
      await this.callerTools.safely(() => this.callerTools.gate.auditCall({
        seat: identity, authenticated_seat: "CURSOR", acting_identity: identity,
        grant_ref: this.callerTools.grantRef, client_id: this.callerTools.clientId,
        tool: name, outcome: "error", error_code: mapped.code, error_message: "Selected identity unavailable.", retryable: false,
      }));
      return errorResult(mapped, (message) => this.callerTools.scrub(message));
    }
    return roleTools.call(name, innerArgs);
  }
}
