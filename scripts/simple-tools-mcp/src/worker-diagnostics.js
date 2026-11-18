// worker_diagnostics:  the Worker's own wake and runtime diagnostics, read-only, for a seat's own bearer.
//
// The Worker records one aggregated row per (kind, reason, seat) in the refusal-log gate's storage
// (src/seat-state.js recordDiagnostic:  kind, reason, seat, count, ts, status).  Until now only the owner's
// /admin page could show them, so a seat could not tell why a wake route answered 404.  This tool reads
// exactly that store through the same diagnosticTail the admin page uses, adds nothing to it and writes
// nothing.
//
// Safety:  a row is re-admitted field by field.  Only kind, reason, seat, count, status and ts come out, each
// under the same shape the writer enforces, so a row that somehow carried anything else (key material, a
// header, a body) never reaches the caller.  The tool takes no secret and returns none.
//
// Arguments:  the handler bounds the raw arguments structurally first (depth, nodes, bytes, keys), then parses
// them with a strict zod schema and reads only the parsed data, whoever calls it.  HostedTools.call runs the same
// bound before its JSON-Schema check, so an oversized argument object never reaches either validator.
//
// Pure module apart from the HostedTools method at the bottom:  no `cloudflare:` imports.

import { z } from "zod";
import { ToolError, okResult, formatTime } from "./contract.js";
import { boundedWorkflowInput } from "./gr-workflow-v1.js";
import { GAP } from "./textfmt.js";

export const WORKER_DIAGNOSTICS_NAME = "worker_diagnostics";
/** The most rows the store keeps (seat-state.js DIAGNOSTIC_CAP) and the most one call returns. */
export const WORKER_DIAGNOSTICS_MAX = 100;
export const WORKER_DIAGNOSTICS_DEFAULT = 50;
/** How many of the newest rows of every kind ride along as `recent`. */
export const WORKER_DIAGNOSTICS_RECENT = 50;

const KIND_RE = /^[a-z][a-z0-9_-]{0,39}$/i;
const REASON_RE = /^[a-z0-9_:-]{1,80}$/i;
const SEAT_RE = /^[A-Z][A-Z0-9_-]{0,39}$/;

// The whole argument object is at most two short scalars:  `seat` (40 characters) and `limit`.  Anything deeper,
// wider or longer is refused before any schema code walks it.
const ARGUMENT_BOUNDS = Object.freeze({ maxDepth: 1, maxNodes: 3, maxBytes: 64, maxString: 40, maxKeys: 2 });

/** True when `args` fits the structural bounds above (it says nothing about the values;  the zod parse does). */
export function boundedDiagnosticsInput(args) {
  return boundedWorkflowInput(args, ARGUMENT_BOUNDS);
}

/** The one accepted argument shape.  Strict:  an unknown key is refused.  Only an omitted limit takes the default;
 * every other limit that is not an integer from 1 to WORKER_DIAGNOSTICS_MAX is refused, never clamped. */
export const WorkerDiagnosticsArgs = z.strictObject({
  seat: z.string().regex(SEAT_RE).optional(),
  limit: z.number().int().min(1).max(WORKER_DIAGNOSTICS_MAX).default(WORKER_DIAGNOSTICS_DEFAULT),
});

/** A refusal message built from the first zod issue's field only, never from the value the caller sent. */
function argumentProblem(error) {
  const issue = error.issues[0];
  const field = issue?.path?.[0];
  if (field === "limit") return `limit must be an integer from 1 to ${WORKER_DIAGNOSTICS_MAX}`;
  if (field === "seat") return "seat must be an upper-case seat tag (A-Z, 0-9, _ or -, at most 40 characters)";
  if (issue?.code === "unrecognized_keys") return "unknown argument:  worker_diagnostics takes only seat and limit";
  return "arguments must be an object with only seat and limit";
}

const storeUnavailable = () => new ToolError("unavailable", "The Worker's diagnostics store is temporarily unavailable.  Try again shortly.", { retryable: true });

const rowSchema = {
  type: "object",
  required: ["kind", "reason", "seat", "count", "status", "ts", "time"],
  properties: {
    kind: { type: "string" },
    reason: { type: "string" },
    seat: { type: ["string", "null"] },
    count: { type: "integer" },
    status: { type: ["integer", "null"] },
    ts: { type: "integer" },
    time: { type: "string" },
  },
};

export const WORKER_DIAGNOSTICS_TOOLS = Object.freeze([
  {
    name: WORKER_DIAGNOSTICS_NAME,
    title: "Worker Diagnostics",
    description:
      "The hosted Worker's own diagnostics, newest first, read-only:  `wake` is the wake route's rows (reason such as wake_key_missing, method_not_allowed, payload_too_large or a failed signature check, the seat, how many times, the last HTTP status and when), " +
      `optionally for one seat, and \`recent\` is the newest rows of every kind (wake, token, mcp, provider).${GAP}Rows are aggregated by kind, reason and seat and kept 90 days.${GAP}` +
      "Use it to find out why a wake or an MCP call was refused.  It returns names, reasons, seats, counts, timestamps and status codes only, never keys, headers or bodies.",
    inputSchema: {
      type: "object",
      additionalProperties: false,
      properties: {
        seat: { type: "string", pattern: "^[A-Z][A-Z0-9_-]{0,39}$", description: "Only wake rows for this seat tag (for example the seat a wake route answered 404 for)." },
        limit: { type: "integer", minimum: 1, maximum: WORKER_DIAGNOSTICS_MAX, default: WORKER_DIAGNOSTICS_DEFAULT, description: "How many wake rows to return." },
      },
    },
    outputSchema: {
      type: "object",
      required: ["count", "total", "wake", "recent"],
      properties: {
        count: { type: "integer" },
        total: { type: "integer" },
        wake: { type: "array", items: rowSchema },
        recent: { type: "array", items: rowSchema },
      },
    },
    annotations: { readOnlyHint: true, openWorldHint: false },
  },
]);
export const WORKER_DIAGNOSTICS_READ_TOOLS = Object.freeze(new Set(WORKER_DIAGNOSTICS_TOOLS.map((tool) => tool.name)));

/** One stored row made safe:  null unless it has a usable kind, reason and time;  every other key is dropped. */
export function diagnosticRow(row) {
  if (!row || typeof row !== "object" || Array.isArray(row)) return null;
  if (typeof row.kind !== "string" || !KIND_RE.test(row.kind)) return null;
  if (typeof row.reason !== "string" || !REASON_RE.test(row.reason)) return null;
  if (!Number.isSafeInteger(row.ts) || row.ts <= 0) return null;
  return {
    kind: row.kind,
    reason: row.reason,
    seat: typeof row.seat === "string" && SEAT_RE.test(row.seat) ? row.seat : null,
    count: Number.isSafeInteger(row.count) && row.count > 0 ? row.count : 1,
    status: Number.isInteger(row.status) && row.status >= 100 && row.status <= 599 ? row.status : null,
    ts: row.ts,
    time: formatTime(Math.floor(row.ts / 1000)),
  };
}

export const WORKER_DIAGNOSTICS_METHODS = {
  async tool_worker_diagnostics(args, audit) {
    const input = args === undefined ? {} : args;
    if (!boundedDiagnosticsInput(input)) throw new ToolError("invalid_argument", "worker_diagnostics arguments exceed structural limits:  at most seat and limit, each a short scalar.");
    const parsed = WorkerDiagnosticsArgs.safeParse(input);
    if (!parsed.success) throw new ToolError("invalid_argument", argumentProblem(parsed.error));
    const seat = parsed.data.seat ?? null;
    const limit = parsed.data.limit;
    // Null when the HostedTools was built without the refusal-log gate (every Worker construction site in
    // index.js passes gate(env, GATE_LOG_NAME), whose SEAT_GATE binding the seat's own gate needs first, so
    // there a store failure shows up below, as unavailable, at call time).
    const source = this.diagnosticsGate;
    if (!source || typeof source.diagnosticTail !== "function") {
      throw new ToolError("not_configured", "The Worker's diagnostics store is not reachable from this connection.  Every other tool still works.");
    }
    await this.reserve("read");
    // The store keeps at most WORKER_DIAGNOSTICS_MAX rows, so one read is the whole of it, newest first.  A
    // rejected or malformed read is a transient store failure:  retryable, and nothing from the error is echoed.
    let stored;
    try {
      stored = await source.diagnosticTail(WORKER_DIAGNOSTICS_MAX);
    } catch {
      this.log?.({ event: "worker_diagnostics_unavailable" });
      throw storeUnavailable();
    }
    if (!Array.isArray(stored)) {
      this.log?.({ event: "worker_diagnostics_unavailable" });
      throw storeUnavailable();
    }
    const rows = stored.map(diagnosticRow).filter(Boolean);
    const wakeAll = rows.filter((row) => row.kind === "wake" && (seat === null || row.seat === seat));
    const wake = wakeAll.slice(0, limit);
    const out = { count: wake.length, total: wakeAll.length, wake, recent: rows.slice(0, WORKER_DIAGNOSTICS_RECENT) };
    return okResult(JSON.stringify(out), out);
  },
};
