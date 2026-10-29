// General Linear GraphQL tools share the existing seat-bound transport.
import { Kind, print } from "graphql";
import { ToolError, okResult, sha256Hex, stableJson, scanSecret, newNonce } from "./contract.js";
import { LinearError, linearClean, linearEnvelope, Q_ISSUE } from "./linear.js";
import { prepareGraphql } from "./linear-graphql.js";

const PROTECTED_LABEL = /^(?:claimed:|seat:|stale-claim$)/i;
const SECRET_FIELD = /secret|token|password|credential|authorization|cookie|api_?key/i;
const DEFINITIVE_MUTATION_FAILURE_KINDS = new Set(["unauthorized", "rate_limited", "mint"]);
const typeRef = (depth) => `kind name${depth ? ` ofType { ${typeRef(depth - 1)} }` : ""}`;
const TYPE_QUERY = `query AgentSyncSchema($name: String!) {
  __type(name: $name) {
    name kind description
    fields(includeDeprecated: true) {
      name description isDeprecated deprecationReason
      args { name description defaultValue type { ${typeRef(7)} } }
      type { ${typeRef(7)} }
    }
    inputFields { name description defaultValue type { ${typeRef(7)} } }
    enumValues(includeDeprecated: true) { name description isDeprecated deprecationReason }
    possibleTypes { name kind }
    interfaces { name kind }
  }
}`;

function cleanData(tools, data, depth = 0) {
  if (depth > 32) throw new ToolError("invalid_argument", "Linear response nesting exceeds the response limit.  Select fewer nested fields.");
  if (typeof data === "string") {
    const value = tools.scrub(data);
    return scanSecret(value, tools.known) ? "[redacted secret]" : linearClean(value, 64000)[0];
  }
  if (Array.isArray(data)) return data.map((item) => cleanData(tools, item, depth + 1));
  if (data && typeof data === "object") {
    return Object.fromEntries(Object.entries(data).map(([key, value]) => [
      linearClean(key, 200)[0], SECRET_FIELD.test(key) ? "[redacted secret]" : cleanData(tools, value, depth + 1),
    ]));
  }
  return data;
}

function apiResult(tools, data, { max_chars = 24000, operation = "query", duplicate = false } = {}) {
  const safe = cleanData(tools, data);
  if (JSON.stringify(safe).length > max_chars) {
    throw new ToolError("response_too_large", "Linear response exceeds max_chars.  Use pagination or select fewer fields; a mutation may already have succeeded, so retain its idempotency key.");
  }
  // Only the fenced text contains remote data.  No untrusted fields are
  // promoted to structured MCP output as trusted metadata.
  return okResult(linearEnvelope(`simple-tools linear_${operation}`, duplicate ? "Previously completed mutation; not repeated." : "Linear API result (untrusted data).", [safe], newNonce()));
}

export async function apiSchema(tools, args) {
  const client = tools.requireLinear();
  await tools.reserveLinear("read");
  const data = await tools.linearCall(() => client.call(TYPE_QUERY, { name: args.type_name }));
  const type = data.__type;
  if (!type) throw new ToolError("not_found", "Linear has no type with that name.");
  const kind = type.fields ? "fields" : type.inputFields ? "inputFields" : "enumValues";
  const fields = (type[kind] ?? []).filter((field) => !args.field || field.name.toLowerCase().includes(args.field.toLowerCase()));
  return apiResult(tools, {
    name: type.name, kind: type.kind, description: type.description,
    total: fields.length, offset: args.offset, next_offset: args.offset + args.limit < fields.length ? args.offset + args.limit : null,
    [kind]: fields.slice(args.offset, args.offset + args.limit),
    interfaces: type.interfaces, possibleTypes: type.possibleTypes,
  }, { max_chars: 64000, operation: "schema" });
}

export async function apiQuery(tools, args) {
  const client = tools.requireLinear();
  const prepared = prepareGraphql(args.query, args.variables, "query");
  tools.scanLinear({ query: prepared.query, variables: JSON.stringify(prepared.variables) });
  await tools.reserveLinear("read");
  const data = await tools.linearCall(() => client.call(prepared.query, prepared.variables));
  return apiResult(tools, data, args);
}

async function protectLabels(tools, prepared) {
  const name = prepared.roots[0].name.value;
  const issueMutation = /^issue/i.test(name);
  const args = prepared.args;
  if (/^issueLabel/i.test(name) && !["issueLabelCreate", "issueLabelUpdate", "issueLabelDelete", "issueLabelArchive", "issueLabelUnarchive", "issueLabelRetire", "issueLabelRestore"].includes(name)) {
    throw new ToolError("invalid_argument", "This label administration operation requires a dedicated fleet-aware tool.");
  }
  if (/^issueLabel/i.test(name) && args.replaceTeamLabels === true) {
    throw new ToolError("invalid_argument", "Replacing team labels can alter protected fleet ownership labels.  Update ordinary labels individually.");
  }
  const labelIds = new Set();
  let replacesLabels = false;
  const walk = (value) => {
    if (!value || typeof value !== "object") return;
    for (const [key, child] of Object.entries(value)) {
      if (/^(?:creatorId|authorId|actorId|createdById|createAsUser)$/i.test(key)) {
        throw new ToolError("invalid_argument", "Linear author identity is fixed by this seat's app credential.");
      }
      if (issueMutation && /^delegateId$/i.test(key)) {
        throw new ToolError("invalid_argument", "The delegate is a fleet claim marker.  Claim with linear_claim; release with linear_status.");
      }
      if (issueMutation && /^(?:added|removed)?labelIds?$/i.test(key)) {
        if (key === "labelIds") replacesLabels = true;
        for (const id of Array.isArray(child) ? child : [child]) {
          if (typeof id !== "string") throw new ToolError("invalid_argument", "Label identifiers must be strings.");
          labelIds.add(id);
        }
      } else if (child && typeof child === "object") walk(child);
    }
  };
  walk(args);
  if (/^issueLabel/i.test(name)) {
    if (typeof args.input?.name === "string" && PROTECTED_LABEL.test(args.input.name.trim())) {
      throw new ToolError("invalid_argument", "Fleet claim and seat labels are managed by linear_claim.");
    }
    if (typeof args.id === "string") labelIds.add(args.id);
  }
  if (labelIds.size > 50) throw new ToolError("invalid_argument", "At most 50 label identifiers may be checked per mutation.");
  for (const id of labelIds) {
    await tools.reserveLinear("read");
    const result = await tools.linearCall(() => tools.linear.call(
      "query AgentSyncLabel($id: String!) { issueLabel(id: $id) { id name } }", { id },
    ));
    if (!result.issueLabel) throw new ToolError("not_found", "A supplied label does not exist.");
    if (PROTECTED_LABEL.test(result.issueLabel.name)) {
      throw new ToolError("invalid_argument", "Fleet claim and seat-label edits must use linear_claim.");
    }
  }
  if (name === "issueUpdate" && replacesLabels) {
    if (typeof args.id !== "string") throw new ToolError("invalid_argument", "issueUpdate requires an issue id.");
    await tools.reserveLinear("read");
    const result = await tools.linearCall(() => tools.linear.call(Q_ISSUE, { id: args.id }));
    if ((result.issue?.labels?.nodes ?? []).some((label) => PROTECTED_LABEL.test(label.name))) {
      throw new ToolError("invalid_argument", "Replacing labels would alter a fleet claim.  Use issueAddLabel/issueRemoveLabel for ordinary labels, or linear_claim for ownership.");
    }
  }
}

function tagInputs(tools, prepared, session) {
  const taggedVariables = new Set();
  const tagObject = (value) => {
    if (Array.isArray(value)) return value.map(tagObject);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(Object.entries(value).map(([key, child]) => [key,
      ["body", "description"].includes(key) && typeof child === "string"
        ? tools.linearBody(child, session) : tagObject(child),
    ]));
  };
  const transform = (node) => {
    if (node.kind === Kind.VARIABLE) {
      const name = node.name.value;
      if (Object.hasOwn(prepared.variables, name) && !taggedVariables.has(name)) {
        prepared.variables[name] = tagObject(prepared.variables[name]);
        taggedVariables.add(name);
      }
    } else if (node.kind === Kind.OBJECT) {
      for (const field of node.fields) {
        if (["body", "description"].includes(field.name.value)) {
          if (field.value.kind === Kind.STRING) field.value.value = tools.linearBody(field.value.value, session);
          else if (field.value.kind === Kind.VARIABLE) {
            const name = field.value.name.value;
            if (typeof prepared.variables[name] === "string" && !taggedVariables.has(name)) {
              prepared.variables[name] = tools.linearBody(prepared.variables[name], session);
              taggedVariables.add(name);
            }
          }
        } else transform(field.value);
      }
    } else if (node.kind === Kind.LIST) node.values.forEach(transform);
  };
  for (const argument of prepared.roots[0].arguments ?? []) {
    if (argument.name.value === "input") transform(argument.value);
  }
  prepared.query = print(prepared.document);
}

export async function apiMutation(tools, args, audit) {
  const client = tools.requireLinearWrites();
  const prepared = prepareGraphql(args.mutation, args.variables, "mutation");
  tools.scanLinear({ mutation: prepared.query, variables: JSON.stringify(prepared.variables) });
  await tools.linearCall(() => client.confirmViewer());
  await protectLabels(tools, prepared);
  tagInputs(tools, prepared, args.session);
  const digest = await sha256Hex(stableJson({ query: prepared.query, variables: prepared.variables }));
  const key = await sha256Hex(`linear-api:${tools.seat}:${args.idempotency_key}`);
  const operation = prepared.roots[0].name.value;
  audit.idem_ref = (await sha256Hex(key)).slice(0, 12);
  const begun = await tools.gate.linearApiBegin({ key, request_sha: digest, operation });
  if (begun.verdict === "conflict") {
    throw new ToolError("invalid_argument", "This idempotency key belongs to a different mutation.  Choose a new key for a new intended change.");
  }
  if (begun.verdict === "duplicate") return apiResult(tools, begun.row.data, { ...args, operation: "mutation", duplicate: true });
  if (begun.verdict === "busy") throw new ToolError("rate_limited", "This mutation is already in flight.  Retry with the same idempotency key.", { retryable: true, retryAfterS: 30 });
  if (begun.verdict !== "send") {
    throw new ToolError("outcome_unknown", "The previous mutation outcome is uncertain.  Check Linear before making another change; this key will not be replayed automatically for 24 hours.");
  }
  try { await tools.reserveLinear("write"); }
  catch (error) { await tools.gate.linearApiDrop({ storageKey: begun.storageKey }); throw error; }
  let data;
  let definitiveFailure = false;
  try {
    data = cleanData(tools, await tools.linearCall(async () => {
      try {
        return await client.call(prepared.query, prepared.variables, { retryOn401: false });
      } catch (error) {
        if (error instanceof LinearError && ([401, 429].includes(error.status) || DEFINITIVE_MUTATION_FAILURE_KINDS.has(error.kind))) {
          await tools.gate.linearApiDrop({ storageKey: begun.storageKey });
          definitiveFailure = true;
        }
        throw error;
      }
    }, { write: true }));
    if (JSON.stringify(data).length > 64000) {
      // Keep a completed marker even when the requested response is too large.
      data = { mutation_completed: true, operation, response_omitted: "Select fewer fields to inspect this resource with linear_query." };
    }
    const cached = new TextEncoder().encode(JSON.stringify(data)).length <= 8192
      ? data : { mutation_completed: true, operation, response_omitted: "Use linear_query to inspect this completed mutation's resource." };
    await tools.gate.linearApiSet({ storageKey: begun.storageKey, changes: { state: "sent", data: cached } });
  } catch (error) {
    if (definitiveFailure) throw error;
    await tools.safely(() => tools.gate.linearApiSet({ storageKey: begun.storageKey, changes: { state: "unknown" } }));
    throw new ToolError("outcome_unknown", `The mutation may have reached Linear.  Check its result before using another idempotency key.  ${tools.scrub(error.message ?? "Upstream failure.")}`);
  }
  return apiResult(tools, data, { ...args, operation: "mutation" });
}
