import test from "node:test";
import assert from "node:assert/strict";
import { prepareGraphql } from "../src/linear-graphql.js";

function rejected(document, variables, expected, code = "invalid_argument") {
  assert.throws(
    () => prepareGraphql(document, variables, expected),
    (error) => error.code === code && typeof error.message === "string",
  );
}

test("prepares query documents with aliases and expanded fragments", () => {
  const result = prepareGraphql(`
    query ProjectList($limit: Int!) {
      projects(first: $limit) { nodes { ...ProjectFields } }
    }
    fragment ProjectFields on Project { projectName: name id }
  `, { limit: 4 });

  assert.equal(result.operation, "ProjectList");
  assert.match(result.query, /fragment ProjectFields/);
  assert.deepEqual(result.variables, { limit: 4 });
  assert.equal(result.roots.length, 1);
  assert.equal(result.roots[0].name.value, "projects");
});

test("expands fragment root fields and resolves literal, enum and variable mutation arguments", () => {
  const result = prepareGraphql(`
    mutation Update($id: String!) {
      update: projectUpdate(id: $id, input: { name: "Changed", state: planned }) { success }
    }
  `, { id: "project-id" }, "mutation");

  assert.equal(result.operation, "Update");
  assert.equal(result.roots.length, 1);
  assert.equal(result.roots[0].alias.value, "update");
  assert.deepEqual(result.args, { id: "project-id", input: { name: "Changed", state: "planned" } });
});

test("allows root fields supplied by fragments and retains their AST nodes", () => {
  const result = prepareGraphql(`mutation M { ...Root } fragment Root on Mutation { projectUpdate(id: "p", input: { name: "n" }) { success } }`, {}, "mutation");
  assert.equal(result.roots.length, 1);
  assert.equal(result.roots[0].name.value, "projectUpdate");
  assert.deepEqual(result.args, { id: "p", input: { name: "n" } });
});

test("applies mutation variable defaults before resolving root args, while explicit variables win", () => {
  const document = `mutation M($input: ProjectUpdateInput! = { labelIds: ["hidden"] }) { projectUpdate(id: "p", input: $input) { success } }`;
  const withDefault = prepareGraphql(document, {}, "mutation");
  assert.deepEqual(withDefault.variables, { input: { labelIds: ["hidden"] } });
  assert.deepEqual(withDefault.args, { id: "p", input: { labelIds: ["hidden"] } });

  const overridden = prepareGraphql(document, { input: { labelIds: [] } }, "mutation");
  assert.deepEqual(overridden.variables, { input: { labelIds: [] } });
  assert.deepEqual(overridden.args, { id: "p", input: { labelIds: [] } });
});

test("applies a __proto__ variable default as an own data property", () => {
  const result = prepareGraphql(`mutation M($__proto__: String = "safe") { projectUpdate(id: $__proto__, input: {}) { success } }`, {}, "mutation");
  assert.equal(Object.hasOwn(result.variables, "__proto__"), true);
  assert.equal(result.variables.__proto__, "safe");
  assert.equal(Object.getPrototypeOf(result.variables), Object.prototype);
  assert.equal(result.args.id, "safe");
});

test("rejects unknown and cyclic fragment spreads and duplicate definitions", () => {
  rejected("query Q { projects { ...Missing } }");
  rejected("query Q { projects { ...A } } fragment A on Project { ...B } fragment B on Project { ...A }");
  rejected("query Q { projects { id } } fragment F on Project { id } fragment F on Project { name }");
  rejected("query Q { projects { id } } query R { teams { id } }");
});

test("enforces expanded selection depth and field-count bounds", () => {
  let nested = "id";
  for (let i = 0; i < 16; i++) nested = `child { ${nested} }`;
  rejected(`query Q { root { ${nested} } }`);

  const spread = Array.from({ length: 251 }, () => "...F").join(" ");
  rejected(`query Q { root { ${spread} } } fragment F on Root { a b }`);
});

test("enforces document and JSON variable size and nesting limits", () => {
  rejected(`query Q { field(arg: "${"x".repeat(24000)}") }`);
  rejected("query Q { field }", { value: "x".repeat(24000) });
  let nested = "leaf";
  for (let i = 0; i < 17; i++) nested = { child: nested };
  rejected("query Q { field }", { value: nested });
  rejected(`query Q { ${Array.from({ length: 4001 }, () => "field").join(" ")} }`);
});

test("rejects operation-kind bypasses and requires the requested single operation", () => {
  rejected("mutation M { projectUpdate(id: \"p\", input: {}) { success } }", {}, "query");
  rejected("subscription S { issue(id: \"i\") { id } }", {}, "query");
  rejected("query Q { projects { id } }", {}, "mutation");
  rejected("# mutation M { secret }\nquery Q { projects { id } }", {}, "mutation");
  rejected("query Q { field }", {}, "subscription");
});

test("requires exactly one mutation root and blocks protected and batch roots", () => {
  rejected("mutation M { projectUpdate(id: \"p\", input: {}) { success } otherUpdate { success } }", {}, "mutation");
  for (const root of ["apiKeyCreate", "OAuthClientUpdate", "applicationCreate", "integrationUpdate", "ssoCreate", "scimUpdate", "organizationUpdate", "userUpdate", "teamMembershipUpdate", "authenticationUpdate"]) {
    rejected(`mutation M { ${root} { success } }`, {}, "mutation");
  }
  rejected("mutation M { issueBatchUpdate { success } }", {}, "mutation");
  rejected("mutation M { projectBulkUpdate { success } }", {}, "mutation");
});
