import test from "node:test";
import assert from "node:assert/strict";
import { WF_V1_ACTIONS, WF_V1_READ_ACTIONS, validWfV1Args, validWfV1Response } from "../src/gr-workflow-v1.js";

const repo = "Fixture-Org/fixture-repo";
const settings = { full_name: repo, description: "d", homepage: null, default_branch: "main", visibility: "public", archived: false, private: false,
  has_issues: false, has_wiki: false, has_projects: false, has_discussions: false, allow_squash_merge: true, allow_merge_commit: false,
  allow_rebase_merge: false, allow_auto_merge: true, delete_branch_on_merge: true, allow_update_branch: true, web_commit_signoff_required: false,
  squash_merge_commit_title: null, squash_merge_commit_message: null, merge_commit_title: null, merge_commit_message: null,
  topics: ["a"], security_and_analysis: { secret_scanning: "enabled" } };
test("repo settings verbs are registered; only the read is a read", () => {
  for (const a of ["wf_repo_settings", "wf_repo_update", "wf_repo_topics_set"]) assert.ok(WF_V1_ACTIONS.includes(a), a);
  assert.ok(WF_V1_READ_ACTIONS.includes("wf_repo_settings"));
  assert.ok(!WF_V1_READ_ACTIONS.includes("wf_repo_update"));
  assert.ok(!WF_V1_READ_ACTIONS.includes("wf_repo_topics_set"));
});
test("args are closed: no rename, privacy or archive", () => {
  assert.ok(validWfV1Args("wf_repo_update", { repo, has_wiki: false, security_and_analysis: { secret_scanning: "enabled" } }));
  assert.ok(!validWfV1Args("wf_repo_update", { repo, private: true }));
  assert.ok(!validWfV1Args("wf_repo_update", { repo, archived: true }));
  assert.ok(!validWfV1Args("wf_repo_update", { repo, name: "x" }));
  assert.ok(validWfV1Args("wf_repo_topics_set", { repo, topics: ["a", "b"] }));
  assert.ok(!validWfV1Args("wf_repo_topics_set", { repo }));
});
test("answers are shape-checked", () => {
  assert.ok(validWfV1Response("wf_repo_settings", { settings }, { repo }));
  assert.ok(!validWfV1Response("wf_repo_settings", { settings: { ...settings, token: "x" } }, { repo }));
  assert.ok(validWfV1Response("wf_repo_topics_set", { topics: ["b", "a"] }, { repo, topics: ["a", "b"] }));
  assert.ok(!validWfV1Response("wf_repo_topics_set", { topics: ["a"] }, { repo, topics: ["a", "b"] }));
});
