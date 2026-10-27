// INACTIVE candidate. Not imported by hosted-tools, index or the tool registry.
// Contract for the git-relay wf_ workflow verbs (git-relay/git_relay/workflow_verbs.py).
// Fixed action names and closed argument sets only: no raw token, URL, query or
// arbitrary path input. The relay stays the policy boundary (per-seat allowlist,
// ceilings, GIT_RELAY_ENABLE_WORKFLOW_VERBS); this module adds no policy of its own.
// Wiring it into the Worker needs its own reviewed change plus host qualification.
const REPO = { type: 'string', pattern: '^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$', description: 'owner/name' };
const SHA = { type: 'string', pattern: '^[0-9a-f]{40}$' };
const INT = { type: 'integer', minimum: 1, maximum: 1e12 };
const PER_PAGE = { type: 'integer', minimum: 1, maximum: 100 };
const STR = { type: 'string', maxLength: 500 };
const obj = (required, properties) => ({ type: 'object', additionalProperties: false, required, properties });

// action -> [description, schema]. Writes name their head-fence argument.
const SPECS = {
  wf_pr_list: ['List pull requests.', obj(['repo'], { repo: REPO, state: STR, head: STR, base: STR, per_page: PER_PAGE })],
  wf_pr_files: ['List files of a pull request.', obj(['repo', 'number'], { repo: REPO, number: INT, per_page: PER_PAGE })],
  wf_file_get: ['Read one file.', obj(['repo', 'path'], { repo: REPO, path: STR, ref: STR })],
  wf_tree: ['Read a git tree.', obj(['repo', 'ref'], { repo: REPO, ref: STR })],
  wf_branches: ['List branches.', obj(['repo'], { repo: REPO, per_page: PER_PAGE })],
  wf_commits: ['List commits.', obj(['repo'], { repo: REPO, ref: STR, path: STR, per_page: PER_PAGE })],
  wf_code_search: ['Search code in the repository.', obj(['repo', 'term'], { repo: REPO, term: STR, per_page: PER_PAGE })],
  wf_runs: ['List workflow runs.', obj(['repo'], { repo: REPO, branch: STR, head_sha: SHA, status: STR, per_page: PER_PAGE })],
  wf_run_jobs: ['List jobs of a workflow run.', obj(['repo', 'run_id'], { repo: REPO, run_id: INT, per_page: PER_PAGE })],
  wf_checks: ['List check runs for a ref (needs checks:read in the seat ceiling).', obj(['repo', 'ref'], { repo: REPO, ref: STR, per_page: PER_PAGE })],
  wf_update_branch: ['Update a PR branch, fenced on its head.', obj(['repo', 'number', 'expected_head_sha'], { repo: REPO, number: INT, expected_head_sha: SHA })],
  wf_run_rerun: ['Re-run a workflow run.', obj(['repo', 'run_id'], { repo: REPO, run_id: INT })],
  wf_run_cancel: ['Cancel a workflow run.', obj(['repo', 'run_id'], { repo: REPO, run_id: INT })],
  wf_run_approve: ['Approve a pending workflow run.', obj(['repo', 'run_id'], { repo: REPO, run_id: INT })],
  wf_file_put: ['Write one file to a branch.', obj(['repo', 'path', 'branch', 'message', 'content_b64'], { repo: REPO, path: STR, branch: STR, message: STR, content_b64: { type: 'string' }, prior_sha: SHA })],
  wf_files_put: ['Write up to 20 files in one commit, fenced on the branch head.', obj(['repo', 'branch', 'message', 'expected_head_sha', 'files'], {
    repo: REPO, branch: STR, message: STR, expected_head_sha: SHA,
    files: { type: 'array', minItems: 1, maxItems: 20, items: obj(['path'], { path: STR, content_b64: { type: 'string' }, delete: { type: 'boolean' } }) } })],
  wf_merge_guarded: ['Merge a pull request, fenced on its head.', obj(['repo', 'number', 'expected_head_sha'], { repo: REPO, number: INT, method: { type: 'string', enum: ['squash', 'merge', 'rebase'] }, expected_head_sha: SHA })],
};
export const WF_V1_ACTIONS = Object.freeze(Object.keys(SPECS));
export const WF_V1_TOOLS = Object.freeze(WF_V1_ACTIONS.map(action => Object.freeze({
  name: `gr_${action}`, description: `${SPECS[action][0]} Inactive candidate; runs as your own GitHub App only when the relay enables workflow verbs.`,
  inputSchema: SPECS[action][1],
})));
const typeOk = (schema, v) => schema.type === 'integer' ? Number.isInteger(v) && v >= schema.minimum && v <= schema.maximum
  : schema.type === 'boolean' ? typeof v === 'boolean'
  : schema.type === 'array' ? Array.isArray(v) && v.length >= schema.minItems && v.length <= schema.maxItems && v.every(x => validWfV1Args2(schema.items, x))
  : typeof v === 'string' && (!schema.pattern || new RegExp(schema.pattern).test(v)) && (!schema.enum || schema.enum.includes(v)) &&
    (!schema.maxLength || new TextEncoder().encode(v).length <= schema.maxLength || schema === undefined);
function validWfV1Args2(schema, args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return false;
  if (!schema.required.every(k => k in args)) return false;
  return Object.keys(args).every(k => schema.properties[k] && typeOk(schema.properties[k], args[k]));
}
export const validWfV1Args = (action, args) => WF_V1_ACTIONS.includes(action) && validWfV1Args2(SPECS[action][1], args);
