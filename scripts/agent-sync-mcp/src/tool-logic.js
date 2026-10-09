// Phase 0 tool behavior, kept apart from the MCP SDK so `node --test` covers it.
//
// The seat comes only from the grant's props (spec 1, "Identity never travels
// in arguments").  Errors follow the spec 1 error model:  `isError: true`, no
// `structuredContent`, the error object in `content[0].text` and in
// `_meta["agent-sync/error"]`.

import { RESOURCE_METADATA_URL } from "./config.js";

export const INSTRUCTIONS =
  "Phase 0 stub of the fleet's agent-sync server.  You act as one bot, SEAT.  " +
  "hello shows which seat this connection is.  hello_write tests the write confirmation and posts nothing.  " +
  "Zulip tools arrive in a later phase.";

export function instructionsFor(seat) {
  return INSTRUCTIONS.replace("SEAT", seat || "an unknown seat");
}

export function toolError(code, message, extraMeta = {}) {
  const error = { code, message, retryable: false };
  return {
    isError: true,
    content: [{ type: "text", text: JSON.stringify(error) }],
    _meta: { "agent-sync/error": error, ...extraMeta },
  };
}

function ok(output) {
  return { content: [{ type: "text", text: JSON.stringify(output) }], structuredContent: output };
}

/** Read the per-request facts the Worker attached to authInfo. */
export function callerFrom(authInfo) {
  const props = authInfo?.extra?.props ?? {};
  return {
    seat: typeof props.seat === "string" ? props.seat : "",
    scopes: Array.isArray(authInfo?.scopes) ? authInfo.scopes.map(String) : [],
    clientId: typeof authInfo?.clientId === "string" ? authInfo.clientId : "",
    paused: authInfo?.extra?.paused === true,
  };
}

export function hello(_args, authInfo) {
  const caller = callerFrom(authInfo);
  if (!caller.seat) return toolError("not_authorized", "This connection has no seat.");
  if (caller.paused) return toolError("paused", "This seat is paused.");
  if (!caller.scopes.includes("zulip:read")) return scopeError("zulip:read");
  return ok({ seat: caller.seat, scopes: caller.scopes, client_id: caller.clientId });
}

export function helloWrite(args, authInfo) {
  const caller = callerFrom(authInfo);
  if (!caller.seat) return toolError("not_authorized", "This connection has no seat.");
  if (caller.paused) return toolError("paused", "This seat is paused.");
  if (!caller.scopes.includes("zulip:write")) return scopeError("zulip:write");
  const note = typeof args?.note === "string" ? args.note : "";
  // The note is counted, never echoed or stored:  Phase 0 posts nothing.
  return ok({ ack: true, seat: caller.seat, posted: false, note_length: note.length });
}

function scopeError(scope) {
  const challenge = `Bearer error="insufficient_scope", scope="${scope}", resource_metadata="${RESOURCE_METADATA_URL}", error_description="This tool needs ${scope}"`;
  return toolError("not_authorized", `This tool needs the ${scope} scope.`, { "mcp/www_authenticate": [challenge] });
}

export const HANDLERS = Object.freeze({ hello, hello_write: helloWrite });
