import test from "node:test";
import assert from "node:assert/strict";
import { ToolError } from "../src/contract.js";
import { assertPublicInputs, INTERNAL_INPUT_FIELDS, PUBLIC_INPUT_FIELDS } from "../src/linear-public-inputs.js";
import { LINEAR_READ_COMMANDS } from "../src/linear-read-catalog.js";
import { LINEAR_WRITE_COMMANDS } from "../src/linear-write-catalog.js";

const named = (catalog, name) => {
  const command = catalog.find((item) => item.name === name);
  assert.ok(command, `catalog command ${name}`);
  return command;
};

function rejects(command, args, message) {
  assert.throws(() => assertPublicInputs(command, args), (error) => {
    assert.ok(error instanceof ToolError);
    assert.equal(error.code, "invalid_argument");
    if (message) assert.equal(error.message, message);
    return true;
  });
}

test("static metadata includes public read and write input types and restricted fields", () => {
  assert.equal(PUBLIC_INPUT_FIELDS.IssueCreateInput.description, "String");
  assert.equal(PUBLIC_INPUT_FIELDS.TemplateCreateInput.templateData, "JSON!");
  assert.ok(INTERNAL_INPUT_FIELDS.IssueCreateInput.includes("descriptionData"));
  assert.ok(INTERNAL_INPUT_FIELDS.IssueUpdateInput.includes("trusted"));
  assert.ok(INTERNAL_INPUT_FIELDS.CommentCreateInput.includes("subscriberIds"));
  assert.ok(INTERNAL_INPUT_FIELDS.DocumentUpdateInput.includes("personalUserId"));
  assert.ok(INTERNAL_INPUT_FIELDS.ProjectCreateInput.includes("leadTeamId"));
  assert.ok(PUBLIC_INPUT_FIELDS.IssueFilter);
  assert.ok(INTERNAL_INPUT_FIELDS.IssueFilter.includes("searchableContent"));
});

test("public write inputs identify restricted schema fields without echoing supplied values", () => {
  const secretLike = "private-example-do-not-echo";
  const cases = [
    ["linear_create_issue", { input: { title: "x", descriptionData: secretLike } }],
    ["linear_update_issue", { id: "issue-id", input: { trusted: true } }],
    ["linear_create_comment", { input: { issueId: "issue-id", body: "text", bodyData: secretLike, subscriberIds: [secretLike] } }],
    ["linear_update_comment", { id: "comment-id", input: { bodyData: secretLike } }],
    ["linear_create_document", { input: { title: "x", teamId: secretLike } }],
    ["linear_update_document", { id: "doc-id", input: { personalUserId: secretLike } }],
    ["linear_update_document", { id: "doc-id", input: { teamId: secretLike } }],
    ["linear_create_project", { input: { name: "x", leadTeamId: secretLike } }],
    ["linear_update_project", { id: "project-id", input: { leadTeamId: secretLike } }],
  ];
  for (const [name, args] of cases) {
    let caught;
    try { assertPublicInputs(named(LINEAR_WRITE_COMMANDS, name), args); }
    catch (error) { caught = error; }
    assert.ok(caught instanceof ToolError, name);
    assert.equal(caught.code, "invalid_argument", name);
    assert.ok(!caught.message.includes(secretLike), name);
    assert.match(caught.message, /^Linear input contains a restricted field \([A-Za-z]+\.[A-Za-z]+\)\.$/, name);
  }
});

test("recursive typed input checks reject internal and unknown nested filter fields", () => {
  const view = named(LINEAR_WRITE_COMMANDS, "linear_create_custom_view");
  rejects(view, { input: { name: "view", filterData: { searchableContent: { contains: "private" } } } });
  rejects(view, { input: { name: "view", filterData: { madeUpInternalField: "private" } } });

  const issue = { title: "x" };
  issue.and = [issue];
  rejects(named(LINEAR_READ_COMMANDS, "linear_list_issues"), { filter: issue }, "Linear input is cyclic or exceeds the validation limit.");
});

test("unknown typed fields are rejected while public descriptions, JSON leaves, and scalar userId remain valid", () => {
  rejects(named(LINEAR_WRITE_COMMANDS, "linear_create_issue"), { input: { title: "x", unexpectedField: "value" } });
  assert.equal(assertPublicInputs(named(LINEAR_WRITE_COMMANDS, "linear_create_issue"), {
    input: { title: "x", description: "Public markdown.", labelIds: ["label-id"] },
  }).input.description, "Public markdown.");
  assert.equal(assertPublicInputs(named(LINEAR_WRITE_COMMANDS, "linear_create_template"), {
    input: { name: "Template", templateData: { trusted: true, descriptionData: { arbitrary: [1, 2] } } },
  }).input.templateData.trusted, true);
  assert.equal(assertPublicInputs(named(LINEAR_WRITE_COMMANDS, "linear_share_issue"), {
    id: "issue-id", userId: "user-id",
  }).userId, "user-id");
});

test("typed input traversal is bounded by depth and total node count", () => {
  const command = named(LINEAR_WRITE_COMMANDS, "linear_create_custom_view");
  let nested = {};
  for (let index = 0; index < 18; index++) nested = { and: [nested] };
  rejects(command, { input: { name: "view", filterData: nested } }, "Linear input exceeds the validation limit.");
  const wide = { and: Array.from({ length: 1001 }, () => ({})) };
  rejects(command, { input: { name: "view", filterData: wide } }, "Linear input exceeds the validation limit.");
});


test("GraphQL singleton list coercion remains available for public typed inputs", () => {
  const command = named(LINEAR_READ_COMMANDS, "linear_list_issues");
  const value = { filter: { and: { title: { contains: "Fleet" } }, id: { in: "issue-id" } } };
  assert.doesNotThrow(() => assertPublicInputs(command, value));
});
