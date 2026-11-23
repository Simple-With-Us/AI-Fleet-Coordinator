import test from "node:test";
import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { WF_V1_ACTIONS, WF_V1_READ_ACTIONS, validWfV1Args, validWfV1Response, workflowInputLimit } from "../src/gr-workflow-v1.js";

const repo = "Fixture-Org/fixture-repo";
test("secret verbs are registered, sealed-only and names-only", () => {
  for (const a of ["wf_secret_put_org_actions", "wf_secrets_repo_dependabot", "wf_org_variable_set", "wf_secret_repos_set_org_codespaces"]) assert.ok(WF_V1_ACTIONS.includes(a), a);
  assert.ok(WF_V1_READ_ACTIONS.includes("wf_secrets_org_actions"));
  assert.ok(!WF_V1_READ_ACTIONS.includes("wf_secret_put_org_actions"));
  const c = randomBytes(120).toString("base64");
  assert.ok(validWfV1Args("wf_secret_put_org_actions", { repo, name: "UM_TOKEN", encrypted_value: c, key_id: "123", visibility: "all" }));
  assert.ok(!validWfV1Args("wf_secret_put_org_actions", { repo, name: "UM_TOKEN", value: "plain", key_id: "123" }));
  assert.ok(workflowInputLimit("wf_secret_put_org_actions") >= 70000);
  assert.ok(validWfV1Response("wf_secrets_org_actions", { secrets: [{ name: "A", created_at: "x", updated_at: "y", visibility: "all" }], total_count: 1, page: 1 }, { repo }));
  assert.ok(!validWfV1Response("wf_secrets_org_actions", { secrets: [{ name: "A", value: "leak" }], total_count: 1, page: 1 }, { repo }));
  assert.ok(validWfV1Response("wf_secret_put_org_actions", { completed: true, name: "UM_TOKEN" }, { repo, name: "UM_TOKEN" }));
});
