// The Sentry webhook schemas.
//
// A Sentry body is not the generic shape:  it nests the issue, its project,
// its metadata, the metric alert and its alert rule, so the generic boundary
// rule refused every real delivery as an invalid text field.
//
// Two schemas describe the two native shapes, and the envelope that carries
// them.  The contract is the one a schema library gives:  a parse answers
// success or failure, a failure names the field that failed, and a success
// carries only the declared fields, so an unknown or wrongly shaped field
// can never reach the renderer, the writer or the channel.
//
// The Worker ships without a bundler and without packages (the README says
// so), so the schemas are declared here rather than pulled from a schema
// library.  The shapes are plain data and the parse is explicit, so the
// guarantee does not depend on the library:  a declared field must have the
// declared type, every string is bounded and free of control bytes, every
// string is scanned for a credential before it can be persisted or relayed,
// and the whole body is bounded in depth and in total nodes.
//
// Unknown vendor fields are dropped rather than refused.  A Sentry body
// carries fields the fleet never reads (actor, installation, tags), and
// refusing a genuine alert over a field nobody reads would lose the alert;
// dropping it keeps it out of the parsed result all the same.

import { scanForCredential } from "./secret-scan.js";

// The same bound the generic payload rule uses.
const MAX_DEPTH = 6;
// A global node budget.  Per-container limits alone still allow a broadly
// branching body to carry exponentially many values, so every visited value
// is counted.
const MAX_NODES = 2000;
// The same text bound the generic payload rule uses.
const TEXT_LIMIT = 4096;
const CONTROL_BYTES = /[\x00-\x08\x0b\x0c\x0e-\x1f]/;

/**
 * A parse failure.
 *
 * `code` is "invalid" for a shape or bound failure and "secret" for a
 * credential-like value.  `field` is the path of the field that failed, which
 * names the field and never its value:  a secret must not reach the caller,
 * the logs or the response body.
 */
export class SchemaError extends Error {
  constructor(message, field, code = "invalid") {
    super(message);
    this.name = "SchemaError";
    this.code = code;
    this.field = field;
  }
}

/** A bounded string. */
const text = (limit = TEXT_LIMIT) => ({ kind: "text", limit });
/** A bounded identifier:  Sentry sends ids as strings or as numbers. */
const identifier = (limit = TEXT_LIMIT) => ({ kind: "identifier", limit });
/** An optional field. */
const optional = (shape) => ({ ...shape, optional: true });
/** An object with a known shape;  unknown keys are dropped. */
const object = (shape) => ({ kind: "object", shape });

const PROJECT_SCHEMA = object({
  slug: optional(text()),
  name: optional(text()),
});

const METADATA_SCHEMA = object({
  type: optional(text()),
  value: optional(text()),
});

const ALERT_RULE_SCHEMA = object({
  id: optional(identifier()),
  name: optional(text()),
  status: optional(text()),
  url: optional(text()),
});

/** The issue alert shape. */
export const SENTRY_ISSUE_SCHEMA = object({
  id: optional(identifier()),
  shortId: optional(identifier()),
  title: optional(text()),
  level: optional(text()),
  url: optional(text()),
  permalink: optional(text()),
  metadata: optional(METADATA_SCHEMA),
  project: optional(PROJECT_SCHEMA),
});

/** The metric alert shape. */
export const SENTRY_METRIC_ALERT_SCHEMA = object({
  id: optional(identifier()),
  title: optional(text()),
  status: optional(text()),
  url: optional(text()),
  alert_rule: optional(ALERT_RULE_SCHEMA),
  metric_identifier: optional(object({ name: optional(text()) })),
});

/** The envelope that carries exactly one of the two shapes. */
const SENTRY_ENVELOPE_SCHEMA = object({
  action: optional(text()),
  level: optional(text()),
  data: object({
    issue: optional(SENTRY_ISSUE_SCHEMA),
    metric_alert: optional(SENTRY_METRIC_ALERT_SCHEMA),
  }),
});

function parseValue(shape, value, path, budget) {
  budget.nodes += 1;
  if (budget.nodes > budget.maxNodes) {
    throw new SchemaError("payload too large", path || null);
  }
  if (value === undefined || value === null) {
    if (shape.optional) return undefined;
    throw new SchemaError("missing field", path || null);
  }
  if (shape.kind === "text" || shape.kind === "identifier") {
    let text_ = value;
    if (shape.kind === "identifier" && typeof value === "number") {
      if (!Number.isFinite(value)) throw new SchemaError("invalid number", path);
      text_ = String(value);
    }
    if (typeof text_ !== "string") throw new SchemaError("invalid text field", path);
    if (text_.length > shape.limit) throw new SchemaError("invalid text", path);
    if (CONTROL_BYTES.test(text_)) throw new SchemaError("invalid text", path);
    // Every string that survives the parse is one that can be persisted or
    // relayed, so every string is scanned.  The kind is never echoed with
    // the value.
    if (scanForCredential(text_)) {
      throw new SchemaError("credential-like value", path, "secret");
    }
    return text_;
  }
  if (shape.kind === "object") {
    if (typeof value !== "object" || Array.isArray(value)) {
      throw new SchemaError("invalid payload", path);
    }
    budget.depth += 1;
    if (budget.depth > budget.maxDepth) {
      throw new SchemaError("payload nested too deep", path);
    }
    const out = {};
    for (const [key, sub] of Object.entries(shape.shape)) {
      const parsed = parseValue(
        sub,
        value[key],
        path ? `${path}.${key}` : key,
        budget,
      );
      if (parsed !== undefined) out[key] = parsed;
    }
    budget.depth -= 1;
    return out;
  }
  throw new SchemaError("invalid schema", path);
}

/**
 * Parse a value against a schema.
 *
 * The answer is either `{ success: true, data }` or
 * `{ success: false, error }`, and the data of a success carries only the
 * declared fields.
 */
export function safeParse(shape, value) {
  try {
    return {
      success: true,
      data: parseValue(shape, value, "", {
        nodes: 0,
        depth: 0,
        maxNodes: MAX_NODES,
        maxDepth: MAX_DEPTH,
      }),
    };
  } catch (error) {
    if (error instanceof SchemaError) return { success: false, error };
    throw error;
  }
}

/**
 * Parse a Sentry webhook body.
 *
 * Sentry sends exactly one of the two shapes, so a body carrying both, or
 * neither, is not a Sentry alert and is refused.  Each shape must also name
 * itself:  an issue without an id or a short id, and a metric alert without
 * an id or a title, are incomplete and never reach the renderer.
 */
export function parseSentryPayload(payload) {
  const parsed = safeParse(SENTRY_ENVELOPE_SCHEMA, payload);
  if (!parsed.success) throw parsed.error;
  const data = parsed.data.data || {};
  const hasIssue = data.issue !== undefined;
  const hasMetric = data.metric_alert !== undefined;
  if (hasIssue === hasMetric) {
    throw new SchemaError("invalid payload", "data");
  }
  if (hasIssue && !data.issue.id && !data.issue.shortId) {
    throw new SchemaError("invalid payload", "data.issue");
  }
  if (hasMetric && !data.metric_alert.id && !data.metric_alert.title) {
    throw new SchemaError("invalid payload", "data.metric_alert");
  }
  return parsed.data;
}

export const __testing = {
  MAX_DEPTH,
  MAX_NODES,
  SENTRY_ENVELOPE_SCHEMA,
  parseValue,
  text,
  optional,
  object,
  identifier,
};
