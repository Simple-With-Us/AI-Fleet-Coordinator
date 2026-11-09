import { assertPublicInputs } from "./linear-public-inputs.js";
import { parse, Kind } from 'graphql';
import { ToolError } from './contract.js';
import { apiQuery, apiMutation } from './linear-api.js';
import { LINEAR_NAMED_BY_NAME } from './linear-named-specs.js';

function selectedFields(value) {
  // A field selection cannot escape its wrapper and append operations/roots.
  let document;
  try { document = parse(`fragment NamedSelection on NamedResource { ${value} }`, { maxTokens: 4000 }); }
  catch { throw new ToolError('invalid_argument', 'selection must contain only GraphQL response fields.'); }
  if (document.definitions.length !== 1 || document.definitions[0].kind !== Kind.FRAGMENT_DEFINITION || document.definitions[0].name.value !== 'NamedSelection') {
    throw new ToolError('invalid_argument', 'selection must contain only GraphQL response fields.');
  }
  return value;
}

export function buildNamedOperation(name, args) {
  const entry = LINEAR_NAMED_BY_NAME[name];
  if (!entry) throw new ToolError('invalid_argument', 'Unknown named Linear command.');
  const command = entry.variants ? (args.id !== undefined ? entry.variants.update : entry.variants.create) : entry;
  if (entry.variants) {
    for (const key of Object.keys(entry.args)) {
      if (args[key] !== undefined && !Object.hasOwn(command.args, key)) {
        throw new ToolError('invalid_argument', `${key} is not supported for this save operation.  Use the matching create or update command.`);
      }
    }
  }
  const variables = {};
  const definitions = [];
  const parameters = [];
  for (const [key, arg] of Object.entries(command.args)) {
    if (args[key] === undefined) continue;
    variables[key] = args[key];
    definitions.push(`$${key}: ${arg.required && !arg.type.endsWith("!") ? `${arg.type}!` : arg.type}`);
    parameters.push(`${key}: $${key}`);
  }
  assertPublicInputs(command, variables);
  const fields = args.selection ?? command.selection;
  const operation = entry.write ? 'mutation' : 'query';
  const document = `${operation} AgentSyncNamed${definitions.length ? `(${definitions.join(', ')})` : ''} { ${command.root}${parameters.length ? `(${parameters.join(', ')})` : ''}${fields ? ` { ${selectedFields(fields)} }` : ''} }`;
  return { entry, document, variables };
}

// The response budget applied when the caller omits max_chars, per tool.
// Only document reads differ:  one whole document routinely passes the
// shared 24,000 default (the 26.7K-char deficiency log did), and the
// document ceiling is 1,000,000 — raised from 64,000 on 2026-11-08, when
// 64,000 was both the default and the hard maximum, so a larger document
// could not be read in full at any setting.
export const NAMED_DEFAULT_MAX_CHARS = Object.freeze({ linear_get_document: 1000000 });

// Slice a document read's content before the response budgets see it, so a
// document past the 1,000,000 ceiling still reads in full across calls:
// the caller walks content_offset by the returned slice length until it
// reaches content_total.  Only the linearCall result is rewritten;  every
// other tools method binds through to the real object unchanged.
function sliceDocumentContent(tools, offset, length) {
  return new Proxy(tools, {
    get(target, prop) {
      if (prop === "linearCall") {
        return async (fn, opts) => {
          const data = await target.linearCall(fn, opts);
          const doc = data?.document;
          if (doc && typeof doc.content === "string") {
            const total = doc.content.length;
            const slice = length === undefined ? doc.content.slice(offset) : doc.content.slice(offset, offset + length);
            return { ...data, document: { ...doc, content: slice, content_total: total, content_offset: offset } };
          }
          return data;
        };
      }
      const value = Reflect.get(target, prop, target);
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

export function callNamedLinear(tools, name, args, audit) {
  const { entry, document, variables } = buildNamedOperation(name, args);
  const shared = { variables, max_chars: args.max_chars ?? NAMED_DEFAULT_MAX_CHARS[name] };
  if (entry.write) {
    return apiMutation(tools, { ...shared, mutation: document, idempotency_key: args.idempotency_key, session: args.session }, audit);
  }
  if (name === "linear_get_document" && (args.content_offset !== undefined || args.content_length !== undefined)) {
    const offset = args.content_offset ?? 0;
    if (!Number.isInteger(offset) || offset < 0) {
      throw new ToolError("invalid_argument", "content_offset must be a non-negative integer.");
    }
    if (args.content_length !== undefined && (!Number.isInteger(args.content_length) || args.content_length < 1 || args.content_length > 1000000)) {
      throw new ToolError("invalid_argument", "content_length must be an integer between 1 and 1,000,000.");
    }
    return apiQuery(sliceDocumentContent(tools, offset, args.content_length), { ...shared, query: document });
  }
  return apiQuery(tools, { ...shared, query: document });
}
