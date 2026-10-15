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
  const fields = args.selection ?? command.selection;
  const operation = entry.write ? 'mutation' : 'query';
  const document = `${operation} AgentSyncNamed${definitions.length ? `(${definitions.join(', ')})` : ''} { ${command.root}${parameters.length ? `(${parameters.join(', ')})` : ''}${fields ? ` { ${selectedFields(fields)} }` : ''} }`;
  return { entry, document, variables };
}

export function callNamedLinear(tools, name, args, audit) {
  const { entry, document, variables } = buildNamedOperation(name, args);
  const shared = { variables, max_chars: args.max_chars };
  return entry.write
    ? apiMutation(tools, { ...shared, mutation: document, idempotency_key: args.idempotency_key, session: args.session }, audit)
    : apiQuery(tools, { ...shared, query: document });
}
