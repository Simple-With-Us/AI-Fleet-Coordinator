// Zod at the boundary (AFC-331 review):  tool arguments, relay envelopes and Zulip reaction objects are parsed
// by strict zod schemas before any field is read.  `parseArgs` is the one place a failure becomes a ToolError, and
// its message names the rule and the field, never the value (a value may be a body or a key).

import { ToolError } from "./contract.js";

/** The first issue of a ZodError as one line:  "argument 'section': ..." or "unknown argument 'x'". */
export function zodProblem(error) {
  const issue = error.issues[0];
  if (!issue) return "arguments are not valid";
  if (issue.code === "unrecognized_keys") {
    const key = issue.keys?.[0];
    return `unknown argument ${typeof key === "string" && /^[A-Za-z0-9_]{1,40}$/.test(key) ? `'${key}'` : "(name not shown)"}`;
  }
  const field = issue.path.length ? `argument '${issue.path.join(".")}'` : "arguments";
  if (issue.code === "invalid_type") return `${field} must be of type ${issue.expected}`;
  return `${field} ${issue.message.replace(/^Invalid input:?\s*/i, "is not valid").slice(0, 120)}`;
}

/** `schema.safeParse(value)`, or a ToolError invalid_argument. */
export function parseArgs(schema, value) {
  const parsed = schema.safeParse(value === undefined || value === null ? {} : value);
  if (!parsed.success) throw new ToolError("invalid_argument", zodProblem(parsed.error));
  return parsed.data;
}
