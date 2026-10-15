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

const installed = (env, name) => typeof env?.[name] === "string" && env[name].trim().length > 0;

export function delegatedStatus(env, identity) {
  const names = delegatedNames(identity);
  if (!names) return null;
  const email = String(env?.[names.zulipEmail] ?? "").trim().toLowerCase();
  const expectedEmail = `${identity.slice(3).toLowerCase()}-grok-bot@simplewithus.zulipchat.com`;
  return {
    identity,
    zulip: installed(env, names.zulipKey) && email === expectedEmail ? "configured" : "missing",
    linear: installed(env, names.linearClientId) && installed(env, names.linearClientSecret) ? "configured" : "missing",
  };
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
  constructor({ callerTools, env, createRole }) {
    this.callerTools = callerTools;
    this.seat = callerTools.seat;
    this.env = env;
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
        ...GB_IDENTITIES.map((identity) => ({ ...delegatedStatus(this.env, identity), recall: this.callerTools.recall ? "configured" : "missing" })),
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
