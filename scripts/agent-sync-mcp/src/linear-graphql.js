import { parse, print, Kind, valueFromASTUntyped } from "graphql";
import { ToolError } from "./contract.js";

const DOCUMENT_LIMIT = 24000;
const VARIABLES_LIMIT = 24000;
const MAX_DEPTH = 16;
const MAX_FIELDS = 500;
const BLOCKED_MUTATION_ROOT = /^(?:apiKey|oauth|application|integration|sso|scim|organization|user|teamMembership|authentication|webhook|workspace|role|permission|session|accessToken)/i;

function invalid(message) {
  throw new ToolError("invalid_argument", message);
}

function copyVariables(variables) {
  let encoded;
  try {
    encoded = JSON.stringify(variables);
  } catch {
    invalid("variables must be JSON data.");
  }
  if (typeof encoded !== "string") invalid("variables must be a JSON object.");
  if (encoded.length > VARIABLES_LIMIT) invalid("variables exceed the 24000-character limit.");

  let copied;
  try {
    copied = JSON.parse(encoded);
  } catch {
    invalid("variables must be JSON data.");
  }
  if (copied === null || Array.isArray(copied) || typeof copied !== "object") {
    invalid("variables must be a JSON object.");
  }

  const checkDepth = (value, depth) => {
    if (depth > MAX_DEPTH) invalid("variables exceed the maximum nesting depth of 16.");
    if (value && typeof value === "object") {
      for (const child of Object.values(value)) checkDepth(child, depth + 1);
    }
  };
  checkDepth(copied, 0);
  return copied;
}

function definitionsOf(ast) {
  const operations = [];
  const fragments = new Map();
  for (const definition of ast.definitions) {
    if (definition.kind === Kind.OPERATION_DEFINITION) {
      operations.push(definition);
      continue;
    }
    if (definition.kind === Kind.FRAGMENT_DEFINITION) {
      const name = definition.name.value;
      if (fragments.has(name)) invalid("GraphQL fragment names must be unique.");
      fragments.set(name, definition);
      continue;
    }
    invalid("GraphQL documents may contain only an operation and fragment definitions.");
  }
  if (operations.length !== 1) invalid("GraphQL document must contain exactly one operation.");
  return { operation: operations[0], fragments };
}

function validateFragmentGraph(fragments, operation) {
  const state = new Map();
  const visit = (name) => {
    const mark = state.get(name);
    if (mark === "visiting") invalid("GraphQL fragment cycles are not allowed.");
    if (mark === "done") return;
    const fragment = fragments.get(name);
    if (!fragment) invalid("GraphQL document contains an unknown fragment spread.");
    state.set(name, "visiting");
    const scan = (selectionSet) => {
      for (const selection of selectionSet.selections) {
        if (selection.kind === Kind.FRAGMENT_SPREAD) visit(selection.name.value);
        else if (selection.selectionSet) scan(selection.selectionSet);
      }
    };
    scan(fragment.selectionSet);
    state.set(name, "done");
  };
  for (const name of fragments.keys()) visit(name);
  const checkOperationReferences = (selectionSet) => {
    for (const selection of selectionSet.selections) {
      if (selection.kind === Kind.FRAGMENT_SPREAD) {
        if (!fragments.has(selection.name.value)) invalid("GraphQL document contains an unknown fragment spread.");
      } else if (selection.selectionSet) checkOperationReferences(selection.selectionSet);
    }
  };
  checkOperationReferences(operation.selectionSet);
}

function expandAndCount(operation, fragments) {
  let fieldCount = 0;
  const roots = [];
  const walk = (selectionSet, depth, atRoot = false) => {
    if (depth > MAX_DEPTH) invalid("GraphQL selection depth exceeds 16.");
    for (const selection of selectionSet.selections) {
      if (selection.kind === Kind.FIELD) {
        fieldCount++;
        if (fieldCount > MAX_FIELDS) invalid("GraphQL document exceeds 500 expanded fields.");
        if (atRoot) roots.push(selection);
        if (selection.selectionSet) walk(selection.selectionSet, depth + 1);
      } else if (selection.kind === Kind.INLINE_FRAGMENT) {
        walk(selection.selectionSet, depth, atRoot);
      } else if (selection.kind === Kind.FRAGMENT_SPREAD) {
        walk(fragments.get(selection.name.value).selectionSet, depth, atRoot);
      }
    }
  };
  walk(operation.selectionSet, 1, true);
  return roots;
}

function plainJSON(value) {
  if (Array.isArray(value)) return value.map(plainJSON);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, plainJSON(child)]));
  }
  return value;
}

function applyVariableDefaults(operation, variables) {
  for (const definition of operation.variableDefinitions ?? []) {
    const name = definition.variable.name.value;
    if (!Object.hasOwn(variables, name) && definition.defaultValue) {
      Object.defineProperty(variables, name, {
        value: plainJSON(valueFromASTUntyped(definition.defaultValue, variables)),
        enumerable: true,
        configurable: true,
        writable: true,
      });
    }
  }
}

function resolvedArguments(field, variables) {
  return Object.fromEntries((field.arguments ?? []).map((argument) => [
    argument.name.value,
    plainJSON(valueFromASTUntyped(argument.value, variables)),
  ]));
}

/** Parse and bound one Linear GraphQL operation before it reaches the API. */
export function prepareGraphql(document, variables = {}, expected = "query") {
  if (typeof document !== "string") invalid("GraphQL document must be a string.");
  if (document.length > DOCUMENT_LIMIT) invalid("GraphQL document exceeds the 24000-character limit.");
  if (expected !== "query" && expected !== "mutation") invalid("operation must be query or mutation.");

  const copiedVariables = copyVariables(variables);
  let ast;
  try {
    ast = parse(document, { maxTokens: 8000 });
  } catch {
    invalid("GraphQL document is invalid.");
  }

  const { operation, fragments } = definitionsOf(ast);
  if (operation.operation !== expected) invalid(`GraphQL document must be a ${expected}.`);
  applyVariableDefaults(operation, copiedVariables);
  validateFragmentGraph(fragments, operation);
  const roots = expandAndCount(operation, fragments);
  if (expected === "mutation") {
    if (roots.length !== 1) invalid("GraphQL mutations must contain exactly one root field.");
    const rootName = roots[0].name.value;
    if (BLOCKED_MUTATION_ROOT.test(rootName)) invalid("This Linear mutation root is protected.");
    if (/(?:Batch|Bulk)/i.test(rootName)) invalid("Batch and bulk mutation roots are not allowed.");
  }

  return {
    document: ast,
    query: print(ast),
    variables: copiedVariables,
    operation: operation.name?.value ?? null,
    roots,
    ...(expected === "mutation" ? { args: resolvedArguments(roots[0], copiedVariables) } : {}),
  };
}
