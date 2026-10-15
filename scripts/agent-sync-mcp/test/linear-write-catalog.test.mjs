import test from "node:test";
import assert from "node:assert/strict";
import { LINEAR_WRITE_COMMANDS } from "../src/linear-write-catalog.js";
import { prepareGraphql } from "../src/linear-graphql.js";

test("named Linear write catalog has unique, single-root GraphQL commands", () => {
  assert.ok(LINEAR_WRITE_COMMANDS.length >= 80);
  assert.equal(new Set(LINEAR_WRITE_COMMANDS.map((entry) => entry.name)).size, LINEAR_WRITE_COMMANDS.length);
  for (const entry of LINEAR_WRITE_COMMANDS) {
    assert.match(entry.name, /^linear_[a-z0-9_]+$/);
    assert.ok(entry.title && entry.description && entry.root && entry.selection);
    assert.equal(typeof entry.destructive, "boolean");
    assert.doesNotMatch(entry.root, /^(?:apiKey|oauth|application|integration|sso|scim|organization|user|teamMembership|authentication)/i);
    assert.doesNotMatch(entry.root, /batch|bulk/i);

    const definitions = Object.entries(entry.args).map(([name, spec]) => `$${name}: ${spec.type}`).join(", ");
    const argumentsText = Object.keys(entry.args).map((name) => `${name}: $${name}`).join(", ");
    const document = `mutation ${entry.name.replaceAll("_", "")}${definitions ? `(${definitions})` : ""} { ${entry.root}${argumentsText ? `(${argumentsText})` : ""} { ${entry.selection} } }`;
    const prepared = prepareGraphql(document, {}, "mutation");
    assert.equal(prepared.roots.length, 1, entry.name);
    assert.equal(prepared.roots[0].name.value, entry.root, entry.name);
    assert.deepEqual(prepared.roots[0].arguments.map((arg) => arg.name.value), Object.keys(entry.args), entry.name);
    for (const [name, spec] of Object.entries(entry.args)) {
      assert.equal(spec.required, spec.type.endsWith("!"), `${entry.name}.${name}`);
      assert.ok(spec.schema && typeof spec.schema === "object", `${entry.name}.${name} schema`);
    }
  }
});

test("named wrappers cover common Linear entities and keep destructive hints explicit", () => {
  const byName = new Map(LINEAR_WRITE_COMMANDS.map((entry) => [entry.name, entry]));
  for (const name of [
    "linear_create_issue", "linear_create_comment", "linear_create_document", "linear_create_project",
    "linear_create_issue_label", "linear_create_project_label", "linear_create_project_milestone",
    "linear_create_attachment", "linear_create_project_update", "linear_create_release",
    "linear_create_release_note", "linear_create_agent_skill",
    "linear_create_custom_view", "linear_create_template", "linear_update_notification",
    "linear_resolve_comment", "linear_create_issue_relation", "linear_create_initiative",
    "linear_create_initiative_update", "linear_create_workflow_state", "linear_complete_release",
    "linear_share_issue", "linear_unshare_issue", "linear_link_attachment_url", "linear_prepare_attachment_upload",
  ]) assert.ok(byName.has(name), name);
  for (const name of [
    "linear_delete_issue", "linear_archive_issue", "linear_delete_project", "linear_archive_project",
    "linear_delete_comment", "linear_delete_attachment", "linear_delete_project_update", "linear_delete_release",
    "linear_delete_release_note", "linear_delete_agent_skill",
  ]) assert.equal(byName.get(name)?.destructive, true, name);
  for (const name of ["linear_unarchive_issue", "linear_unarchive_project", "linear_restore_issue_label", "linear_restore_project_label", "linear_unarchive_release"])
    assert.equal(byName.get(name)?.destructive, false, name);
});

test("named command arguments preserve the official GraphQL root signatures", () => {
  const byRoot = new Map(LINEAR_WRITE_COMMANDS.map((entry) => [entry.root, entry]));
  assert.deepEqual(Object.fromEntries(Object.entries(byRoot.get("projectCreate").args).map(([key, value]) => [key, value.type])), {
    input: "ProjectCreateInput!", slackChannelName: "String",
  });
  assert.deepEqual(Object.fromEntries(Object.entries(byRoot.get("issueDelete").args).map(([key, value]) => [key, value.type])), {
    id: "String!",
  });
  assert.deepEqual(Object.fromEntries(Object.entries(byRoot.get("commentUpdate").args).map(([key, value]) => [key, value.type])), {
    id: "String!", input: "CommentUpdateInput!",
  });
  assert.equal(byRoot.get("projectUpdateCreate").args.input.type, "ProjectUpdateCreateInput!");
  assert.equal(byRoot.get("releaseCreate").args.input.type, "ReleaseCreateInput!");
  assert.equal(byRoot.get("releaseNoteCreate").args.input.type, "ReleaseNoteCreateInput!");
  assert.equal(byRoot.get("agentSkillUpdate").args.input.type, "AgentSkillUpdateInput!");
  assert.deepEqual(Object.fromEntries(Object.entries(byRoot.get("issueRelationCreate").args).map(([key, value]) => [key, value.type])), {
    input: "IssueRelationCreateInput!",
  });
  assert.deepEqual(Object.fromEntries(Object.entries(byRoot.get("fileUpload").args).map(([key, value]) => [key, value.type])), {
    contentType: "String!", filename: "String!", size: "Int!",
  });
  assert.equal(byRoot.get("fileUpload").args.size.schema.minimum, 1);
  assert.equal(byRoot.get("fileUpload").args.size.schema.maximum, 2147483647);
  assert.deepEqual(Object.fromEntries(Object.entries(byRoot.get("attachmentLinkURL").args).map(([key, value]) => [key, value.type])), {
    issueId: "String!", url: "String!", title: "String", id: "String",
  });
});

test("catalog excludes privileged, batch, and external-import mutation families", () => {
  const roots = new Set(LINEAR_WRITE_COMMANDS.map((entry) => entry.root));
  for (const excluded of ["cycleCreate", "projectMilestoneMove", "issueBatchCreate", "issueBatchUpdate", "issueImportCreateJira", "agentSessionCreate", "oauthClientCreate", "organizationUpdate"])
    assert.equal(roots.has(excluded), false, excluded);
});

test("catalog omits internal and admin-only arguments", () => {
  const byRoot = new Map(LINEAR_WRITE_COMMANDS.map((entry) => [entry.root, entry]));
  assert.equal(Object.hasOwn(byRoot.get("projectCreate").args, "aiConversationId"), false);
  assert.equal(Object.hasOwn(byRoot.get("projectCreate").args, "projectDraftId"), false);
  assert.equal(Object.hasOwn(byRoot.get("issueDelete").args, "permanentlyDelete"), false);
  assert.equal(Object.hasOwn(byRoot.get("commentUpdate").args, "skipEditedAt"), false);
  assert.equal(Object.hasOwn(byRoot.get("issueRelationCreate").args, "overrideCreatedAt"), false);
});
