import test from "node:test";
import assert from "node:assert/strict";
import { WF_V1_ACTIONS, WF_V1_READ_ACTIONS, validWfV1Args, validWfV1Response } from "../src/gr-workflow-v1.js";

const repo = "Fixture-Org/fixture-repo";
const hook = { id: 9, name: "web", active: true, events: ["push"], url: "https://example.invalid/h", url_withheld: false, content_type: "json", has_secret: true };
test("hook, deploy key and environment verbs are registered with the right read set", () => {
  for (const a of ["wf_hook_create", "wf_org_hook_update", "wf_deploy_key_add", "wf_environment_put", "wf_env_variable_set"]) assert.ok(WF_V1_ACTIONS.includes(a), a);
  for (const a of ["wf_hook_get", "wf_org_hooks", "wf_deploy_key_get", "wf_env_branch_policies", "wf_env_variables"]) assert.ok(WF_V1_READ_ACTIONS.includes(a), a);
  for (const a of ["wf_hook_create", "wf_hook_delete", "wf_deploy_key_add", "wf_environment_put", "wf_environment_delete", "wf_env_variable_set"]) assert.ok(!WF_V1_READ_ACTIONS.includes(a), a);
});
test("args are closed and bounded", () => {
  assert.ok(validWfV1Args("wf_hook_create", { repo, url: "https://example.invalid/h", secret: "s", events: ["push"] }));
  assert.ok(!validWfV1Args("wf_hook_create", { repo }));
  assert.ok(!validWfV1Args("wf_hook_create", { repo, url: "https://example.invalid/h", extra: 1 }));
  assert.ok(!validWfV1Args("wf_hook_get", { repo, id: 0 }));
  assert.ok(validWfV1Args("wf_environment_put", { repo, name: "prod", reviewers: [{ type: "User", id: 1 }], deployment_branch_policy: { protected_branches: true, custom_branch_policies: false } }));
  assert.ok(!validWfV1Args("wf_environment_put", { repo, name: "prod", wait_timer: 43201 }));
  assert.ok(!validWfV1Args("wf_env_variable_set", { repo, name: "prod", variable: "GITHUB_X", value: "1" }));
});
test("answers are shape-checked receipts and never carry a secret", () => {
  assert.ok(validWfV1Response("wf_hook_get", { hook }, { repo, id: 9 }));
  assert.ok(!validWfV1Response("wf_hook_get", { hook: { ...hook, secret: "leak" } }, { repo, id: 9 }));
  assert.ok(!validWfV1Response("wf_hook_get", { hook }, { repo, id: 10 }));
  assert.ok(validWfV1Response("wf_hook_delete", { completed: true, id: 9, already_absent: true }, { repo, id: 9 }));
  assert.ok(validWfV1Response("wf_env_variable_set", { completed: true, name: "prod", variable: "FOO" }, { repo, name: "prod", variable: "FOO", value: "1" }));
  assert.ok(!validWfV1Response("wf_env_variable_set", { completed: true, name: "prod", variable: "BAR" }, { repo, name: "prod", variable: "FOO", value: "1" }));
  assert.ok(validWfV1Response("wf_env_variables", { variables: [{ name: "FOO", value: "1", created_at: null, updated_at: null }], total_count: 1, page: 1 }, { repo, name: "prod" }));
  assert.ok(validWfV1Response("wf_org_hooks", { hooks: [hook], page: 1, has_more: false }, { repo }));
});
