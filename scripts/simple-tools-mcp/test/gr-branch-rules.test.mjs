import test from "node:test";
import assert from "node:assert/strict";
import { validWfV1Args, validWfV1Response, WF_V1_READ_ACTIONS } from "../src/gr-workflow-v1.js";

const args = { repo: "a/b", branch: "main" };

test("wf_branch_rules accepts a projected pull_request rule and refuses extras", () => {
  assert.equal(WF_V1_READ_ACTIONS.includes("wf_branch_rules"), true);
  assert.equal(validWfV1Args("wf_branch_rules", args), true);
  const body = {
    rules: [{ type: "pull_request", parameters: { required_review_thread_resolution: true } }],
  };
  assert.equal(validWfV1Response("wf_branch_rules", body, args), true);
  assert.equal(validWfV1Response("wf_branch_rules", { ...body, ruleset_id: 9 }, args), false);
  assert.equal(validWfV1Response("wf_branch_rules", {
    rules: [{ type: "pull_request", parameters: { surprise: true } }],
  }, args), false);
  assert.equal(validWfV1Response("wf_branch_rules", {
    rules: [{ type: "no_such_rule", parameters: {} }],
  }, args), false);
});
