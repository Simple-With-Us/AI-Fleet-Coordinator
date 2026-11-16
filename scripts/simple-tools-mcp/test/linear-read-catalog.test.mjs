import test from "node:test";
import assert from "node:assert/strict";
import { parse } from "graphql";
import { LINEAR_READ_CATALOG_NOTES, LINEAR_READ_COMMANDS } from "../src/linear-read-catalog.js";

function queryFor(command) {
  const definitions = Object.entries(command.args).map(([name, arg]) => {
    const defaultValue = arg.default === undefined ? "" : ` = ${JSON.stringify(arg.default)}`;
    return `$${name}:${arg.type}${arg.required ? "!" : ""}${defaultValue}`;
  });
  const args = Object.keys(command.args).map((name) => `${name}:$${name}`);
  return `query${definitions.length ? `(${definitions.join(",")})` : ""}{${command.root}${args.length ? `(${args.join(",")})` : ""}{${command.selection}}}`;
}

test("named Linear read catalog is unique and every generated operation parses", () => {
  const names = LINEAR_READ_COMMANDS.map((command) => command.name);
  assert.equal(new Set(names).size, names.length);
  for (const command of LINEAR_READ_COMMANDS) {
    assert.match(command.name, /^linear_(?:list|get|search)_[a-z0-9_]+$/);
    assert.ok(command.title.length > 0);
    assert.ok(command.description.length > 0);
    assert.ok(command.root.length > 0);
    assert.ok(command.selection.length > 0);
    assert.doesNotThrow(() => parse(queryFor(command)), command.name);
  }
});

test("list commands expose capped pagination and exact GraphQL argument names and types", () => {
  const issues = LINEAR_READ_COMMANDS.find((command) => command.name === "linear_list_issues");
  assert.deepEqual(Object.keys(issues.args), ["first", "after", "filter", "includeArchived"]);
  assert.deepEqual(issues.args.first, {
    type: "Int",
    required: false,
    description: "Maximum number of records to return (1–100; defaults to 20).",
    default: 20,
    schema: { type: "integer", minimum: 1, maximum: 100, default: 20 },
  });
  assert.equal(issues.args.filter.type, "IssueFilter");
  assert.equal(issues.args.after.type, "String");
  assert.equal(issues.args.includeArchived.type, "Boolean");

  for (const name of ["linear_list_projects", "linear_list_project_milestones", "linear_list_agent_skills", "linear_list_release_pipelines"]) {
    const command = LINEAR_READ_COMMANDS.find((entry) => entry.name === name);
    assert.equal(command.args.first.default, 20, name);
    assert.equal(command.args.first.schema.maximum, 100, name);
    assert.match(command.selection, /pageInfo \{ hasNextPage endCursor \}/, name);
  }
});

test("single-resource commands require IDs; get-comment narrows the optional GraphQL ID-or-hash root to ID", () => {
  for (const name of ["linear_get_issue", "linear_get_project", "linear_get_release", "linear_get_agent_skill", "linear_get_workflow_state", "linear_get_triage_responsibility", "linear_get_custom_view"]) {
    const command = LINEAR_READ_COMMANDS.find((entry) => entry.name === name);
    assert.deepEqual(command.args.id, {
      type: "String",
      required: true,
      description: name === "linear_get_issue" ? "Issue ID or identifier."
        : name === "linear_get_project" ? "Project ID."
          : name === "linear_get_release" ? "Release ID or slug."
            : name === "linear_get_agent_skill" ? "Agent skill ID."
              : name === "linear_get_workflow_state" ? "Workflow state ID."
                : name === "linear_get_triage_responsibility" ? "Triage responsibility ID."
                  : "Custom view ID.",
      schema: { type: "string", minLength: 1 },
    });
  }
  const comment = LINEAR_READ_COMMANDS.find((entry) => entry.name === "linear_get_comment");
  assert.deepEqual(Object.keys(comment.args), ["id"]);
  assert.equal(comment.args.id.required, true);
});

test("verified non-connection roots retain their actual argument shape and the catalog documents omissions", () => {
  const workspace = LINEAR_READ_COMMANDS.find((command) => command.name === "linear_get_workspace");
  assert.equal(workspace.root, "organization");
  assert.deepEqual(workspace.args, {});

  const templates = LINEAR_READ_COMMANDS.find((command) => command.name === "linear_list_templates");
  assert.equal(templates.root, "templates");
  assert.deepEqual(templates.args, {});
  assert.doesNotMatch(templates.selection, /nodes \{|pageInfo/);

  const searchTemplates = LINEAR_READ_COMMANDS.find((command) => command.name === "linear_search_templates");
  assert.equal(searchTemplates.root, "templateSearch");
  assert.equal(searchTemplates.args.filter.type, "TemplateFilter");
  assert.ok(LINEAR_READ_CATALOG_NOTES.some((note) => note.includes("customerNeeds/customerNeed")));
  assert.ok(LINEAR_READ_CATALOG_NOTES.some((note) => note.includes("diff query is marked internal")));
});

test("catalog includes supported screenshot-oriented domains without inventing absent roots", () => {
  const roots = new Set(LINEAR_READ_COMMANDS.map((command) => command.root));
  for (const root of ["issues", "comments", "attachments", "cycles", "customViews", "customView", "documents", "workflowStates", "workflowState", "triageResponsibility", "issueLabels", "projectLabels", "projects", "projectMilestones", "teams", "users", "organization", "templates", "notifications", "projectUpdates", "initiatives", "favorites", "roadmaps", "customerNeeds", "releases", "releaseNotes", "releasePipelines", "releaseStages", "agentSkills"]) {
    assert.ok(roots.has(root), `expected public schema root ${root}`);
  }
  assert.equal(roots.has("customerRequests"), false);
  assert.equal(roots.has("diff"), false);
  assert.equal(roots.has("extractImages"), false);
});

test("detail selections include useful document, template, and project content", () => {
  const selectionFor = (name) => LINEAR_READ_COMMANDS.find((entry) => entry.name === name).selection;
  assert.match(selectionFor("linear_get_document"), /\bcontent\b/);
  assert.match(selectionFor("linear_get_template"), /\btemplateData\b/);
  assert.match(selectionFor("linear_get_project"), /\bcontent\b/);
  assert.equal((selectionFor("linear_get_issue").match(/\bupdatedAt\b/g) ?? []).length, 1);
});
