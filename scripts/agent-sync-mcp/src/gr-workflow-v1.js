import {z} from 'zod';
// Default-off caller contract. hosted-tools registers these tools, but they are neither listed nor
// callable unless the owner sets GR_WORKFLOW_TOOLS=on (config.grWorkflowEnabled).
// Contract for the git-relay wf_ workflow verbs (git-relay/git_relay/workflow_verbs.py).
// Fixed action names and closed argument sets only: no raw token, URL, query or
// arbitrary path input. The relay stays the policy boundary (per-seat allowlist,
// ceilings, GIT_RELAY_ENABLE_WORKFLOW_VERBS); this module adds no policy of its own.
// Host rollout (relay flag, seat config, Worker env) is separate and not done here.
const REPO = { type: 'string', pattern: '^(?!.*\\.\\.)[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$', description: 'owner/name' };
const SHA = { type: 'string', pattern: '^[0-9a-f]{40}$' };
const INT = { type: 'integer', minimum: 1, maximum: 1e12 };
const PER_PAGE = { type: 'integer', minimum: 1, maximum: 50 };
const CONTENT = { type: 'string', minLength: 1, maxLength: 60000 };
const PATH = {type:'string', maxLength:1024, pattern:'^(?!/)(?!.*//)(?!.*(?:^|/)[.]{1,2}(?:/|$))(?!.*[/]$)[A-Za-z0-9._/+@~-]+$'};
const REF = {type:'string', maxLength:200, pattern:'^(?![/-])(?!.*[.][.])(?!.*(?:/|[.]lock)$)[A-Za-z0-9._/-]+$'};
const STR = { type: 'string', maxLength: 500 };
const obj = (required, properties) => ({ type: 'object', additionalProperties: false, required, properties });

// action -> [description, schema]. Writes name their head-fence argument.
const SPECS = {
 wf_repo_tags:['Read bounded tag name/commit SHA metadata, not annotated tag contents or a complete ref history.  Pagination required.',obj(['repo'],{repo:REPO,per_page:PER_PAGE,page:{type:'integer',minimum:1,maximum:1000}})],
 wf_repo_languages:['Read bounded repository language-byte counts; not file counts, contributors, or a live code audit.',obj(['repo'],{repo:REPO})],

 wf_issue_parent:['Read issue parent in exact repo; missing parent, visibility and other upstream refusals are indistinguishable; never a fabricated empty parent.',obj(['repo','number'],{repo:REPO,number:INT})],
 wf_issue_subissues:['Read bounded issue relation page, no writes or cross-repo lookup.\u00A0 has_more is conservative, not completeness proof.',obj(['repo','number'],{repo:REPO,number:INT,per_page:PER_PAGE,page:{type:'integer',minimum:1,maximum:1000}})],
 wf_issue_blocked_by:['Read bounded issue relation page, no writes or cross-repo lookup.\u00A0 has_more is conservative, not completeness proof.',obj(['repo','number'],{repo:REPO,number:INT,per_page:PER_PAGE,page:{type:'integer',minimum:1,maximum:1000}})],
 wf_issue_blocking:['Read bounded issue relation page, no writes or cross-repo lookup.\u00A0 has_more is conservative, not completeness proof.',obj(['repo','number'],{repo:REPO,number:INT,per_page:PER_PAGE,page:{type:'integer',minimum:1,maximum:1000}})],

  wf_pr_get: ['Read pr get.', obj(['repo', 'number'], {repo:REPO,number:INT})],
  wf_pr_reviews: ['Read pr reviews.', obj(['repo', 'number'], {repo:REPO,number:INT,per_page:PER_PAGE})],
  wf_pr_comments: ['Read pr comments.', obj(['repo', 'number'], {repo:REPO,number:INT,per_page:PER_PAGE})],
  wf_issue_comments: ['Read issue comments.', obj(['repo', 'number'], {repo:REPO,number:INT,per_page:PER_PAGE})],
  wf_commit_get: ['Read commit get.', obj(['repo', 'sha'], {repo:REPO,sha:SHA})],
  wf_compare: ['Read compare.', obj(['repo', 'base', 'head'], {repo:REPO,base:REF,head:REF})],
  wf_branch_get: ['Read branch get.', obj(['repo', 'branch'], {repo:REPO,branch:REF})],
  wf_repo_get: ['Read repo get.', obj(['repo'], {repo:REPO})],
  wf_labels: ['Read labels.', obj(['repo'], {repo:REPO,per_page:PER_PAGE})],
  wf_workflows: ['Read workflows.', obj(['repo'], {repo:REPO,per_page:PER_PAGE})],
  wf_run_get: ['Read run get.', obj(['repo', 'run_id'], {repo:REPO,run_id:INT})],
  wf_run_artifacts: ['Read run artifacts.', obj(['repo', 'run_id'], {repo:REPO,run_id:INT,per_page:PER_PAGE})],
  wf_workflow_enable: ['workflow enable.',obj(['repo','workflow_id'],{repo:REPO,workflow_id:INT})],
  wf_workflow_disable: ['workflow disable.',obj(['repo','workflow_id'],{repo:REPO,workflow_id:INT})],
  wf_run_delete: ['run delete.',obj(['repo','run_id'],{repo:REPO,run_id:INT})],
  wf_artifact_delete: ['artifact delete.',obj(['repo','artifact_id'],{repo:REPO,artifact_id:INT})],

  wf_release_assets: ['List release asset metadata, not binary downloads.',obj(['repo','release_id'],{repo:REPO,release_id:INT,per_page:PER_PAGE})],
  wf_release_asset_get: ['Read release asset metadata, not binary downloads.',obj(['repo','asset_id'],{repo:REPO,asset_id:INT})],
  wf_release_asset_upload: ['Upload up to 32 KiB secret-scanned UTF-8 text as a new asset; no overwrite/delete.',obj(['repo','release_id','name','content_b64'],{repo:REPO,release_id:INT,name:{type:'string',pattern:'^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9_-])?$'},content_b64:{type:'string',minLength:1,maxLength:43692}})],
  wf_review_submit: ['Submit a commit-bound PR review (pre-read is not an atomic head lock).', obj(['repo','number','commit_id','event','body'], {repo:REPO,number:INT,commit_id:SHA,event:{type:'string',enum:['APPROVE','REQUEST_CHANGES','COMMENT']},body:{type:"string",minLength:1,maxLength:32768}})],
  wf_review_inline: ['Create a commit-bound inline PR comment.', obj(['repo','number','commit_id','path','line','side','body'], {repo:REPO,number:INT,commit_id:SHA,path:PATH,line:{type:'integer',minimum:1,maximum:10000000},side:{type:'string',enum:['LEFT','RIGHT']},body:{type:"string",minLength:1,maxLength:32768}})],
  wf_branch_create: ['Create a new branch without overwrite.', obj(['repo','branch','sha'],{repo:REPO,branch:REF,sha:SHA})],
  wf_pr_list: ['List pull requests.', obj(['repo'], { repo: REPO, state: STR, head: REF, base: REF, per_page: PER_PAGE })],
  wf_pr_files: ['List files of a pull request.', obj(['repo', 'number'], { repo: REPO, number: INT, per_page: PER_PAGE })],
  wf_file_get: ['Read one file.', obj(['repo', 'path', 'ref'], { repo: REPO, path: PATH, ref: REF })],
  wf_tree: ['Read a git tree.', obj(['repo', 'ref'], { repo: REPO, ref: REF })],
  wf_branches: ['List branches.', obj(['repo'], { repo: REPO, per_page: PER_PAGE })],
  wf_commits: ['List commits.', obj(['repo', 'ref'], { repo: REPO, ref: REF, path: PATH, per_page: PER_PAGE })],
  wf_code_search: ['Search code in the repository.', obj(['repo', 'term'], { repo: REPO, term: STR, per_page: PER_PAGE })],
  wf_runs: ['List workflow runs.', obj(['repo'], { repo: REPO, branch: REF, head_sha: SHA, status: STR, per_page: PER_PAGE })],
  wf_run_jobs: ['List jobs of a workflow run.', obj(['repo', 'run_id'], { repo: REPO, run_id: INT, per_page: PER_PAGE })],
  wf_checks: ['List check runs for a ref (needs checks:read in the seat ceiling).', obj(['repo', 'ref'], { repo: REPO, ref: REF, per_page: PER_PAGE })],
  wf_update_branch: ['Update a PR branch, fenced on its head.', obj(['repo', 'number', 'expected_head_sha'], { repo: REPO, number: INT, expected_head_sha: SHA })],
  wf_run_rerun: ['Re-run a workflow run.', obj(['repo', 'run_id'], { repo: REPO, run_id: INT })],
  wf_run_cancel: ['Cancel a workflow run.', obj(['repo', 'run_id'], { repo: REPO, run_id: INT })],
  wf_run_approve: ['Approve a pending workflow run.', obj(['repo', 'run_id'], { repo: REPO, run_id: INT })],
  wf_file_put: ['Write one file to a branch.', obj(['repo', 'path', 'branch', 'message', 'content_b64'], { repo: REPO, path: PATH, branch: REF, message: STR, content_b64: CONTENT, prior_sha: SHA })],
  wf_files_put: ['Write up to 20 files in one commit, fenced on the branch head.', obj(['repo', 'branch', 'message', 'expected_head_sha', 'files'], {
    repo: REPO, branch: REF, message: STR, expected_head_sha: SHA,
    files: { type: 'array', minItems: 1, maxItems: 20, items: obj(['path'], { path: PATH, content_b64: CONTENT, delete: { type: 'boolean' } }) } })],
  wf_merge_guarded: ['Merge a pull request, fenced on its head.', obj(['repo', 'number', 'method', 'expected_head_sha'], { repo: REPO, number: INT, method: { type: 'string', enum: ['squash', 'merge', 'rebase'] }, expected_head_sha: SHA })],
};
export const WF_V1_ACTIONS = Object.freeze(Object.keys(SPECS));
export const WF_V1_READ_ACTIONS = Object.freeze(['wf_repo_tags','wf_repo_languages','wf_issue_parent','wf_issue_subissues','wf_issue_blocked_by','wf_issue_blocking','wf_pr_get','wf_pr_reviews','wf_pr_comments','wf_issue_comments','wf_commit_get','wf_compare','wf_branch_get','wf_repo_get','wf_labels','wf_workflows','wf_run_get','wf_run_artifacts','wf_release_assets','wf_release_asset_get','wf_pr_list','wf_pr_files','wf_file_get','wf_tree','wf_branches','wf_commits','wf_code_search','wf_runs','wf_run_jobs','wf_checks']);
export const WF_V1_TOOLS = Object.freeze(WF_V1_ACTIONS.map(action => Object.freeze({
  name: `gr_${action}`, description: `${SPECS[action][0]} Inactive candidate; runs as your own GitHub App only when the relay enables workflow verbs.`,
  inputSchema: SPECS[action][1],
})));
const typeOk = (schema, v) => schema.type === 'integer' ? Number.isInteger(v) && v >= schema.minimum && v <= schema.maximum
  : schema.type === 'boolean' ? typeof v === 'boolean'
  : schema.type === 'array' ? Array.isArray(v) && v.length >= schema.minItems && v.length <= schema.maxItems && v.every(x => validWfV1Args2(schema.items, x))
  : typeof v === 'string' && (!schema.pattern || new RegExp(schema.pattern).test(v)) && (!schema.enum || schema.enum.includes(v)) &&
    (!schema.minLength || v.length >= schema.minLength) && (!schema.maxLength || v.length <= schema.maxLength);
function validWfV1Args2(schema, args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return false;
  if (!schema.required.every(k => Object.hasOwn(args,k))) return false;
  return Object.keys(args).every(k => Object.hasOwn(schema.properties,k) && typeOk(schema.properties[k], args[k]));
}
// Iterative guard runs before recursive schema code or JSON serialization.
export function boundedWorkflowInput(args) {
  const stack=[[args,0]], seen=new Set(); let nodes=0, bytes=0;
  while(stack.length) {
    const [value,depth]=stack.pop();
    if(++nodes>500 || depth>6) return false;
    if(typeof value==='string') {
      if(value.length>65536) return false;
      bytes+=new TextEncoder().encode(value).length;
    } else if(value && typeof value==='object') {
      if(seen.has(value)) return false; seen.add(value);
      if(Object.getPrototypeOf(value)!==Object.prototype && !Array.isArray(value) && Object.getPrototypeOf(value)!==null) return false;
      const keys=Object.keys(value); if(keys.length>100) return false;
      for(const key of keys) {
        const d=Object.getOwnPropertyDescriptor(value,key);
        if(!d || !Object.hasOwn(d,'value')) return false;
        bytes+=key.length; stack.push([d.value,depth+1]);
      }
    } else if(!['number','boolean'].includes(typeof value) && value!==null) return false;
    if(bytes>65536) return false;
  }
  return true;
}
// The relay measures review bodies in UTF-8 bytes and refuses blank ones;
// mirror that here so bad input fails before network instead of as a relay 400.
const reviewBodyOk = args => typeof args?.body === 'string' && args.body.trim() !== '' &&
  new TextEncoder().encode(args.body).length <= 32768;
function gitSchema(schema){
 if(schema.type==='object')return z.object(Object.fromEntries(Object.entries(schema.properties).map(([k,v])=>[k,schema.required.includes(k)?gitSchema(v):gitSchema(v).optional()]))).strict();
 if(schema.type==='array')return z.array(gitSchema(schema.items)).min(schema.minItems).max(schema.maxItems);
 if(schema.type==='integer')return z.number().int().min(schema.minimum).max(schema.maximum);
 if(schema.type==='boolean')return z.boolean();
 let out=z.string();if(schema.minLength)out=out.min(schema.minLength);if(schema.maxLength)out=out.max(schema.maxLength);if(schema.pattern)out=out.regex(new RegExp(schema.pattern));if(schema.enum)out=out.refine(v=>schema.enum.includes(v));return out;
}
const ARG_SCHEMAS=Object.fromEntries(Object.entries(SPECS).map(([a,[,s]])=>[a,gitSchema(s)]));
export function parseWfV1Args(action,args){
 if(!boundedWorkflowInput(args)||!WF_V1_ACTIONS.includes(action))return null;
 const parsed=ARG_SCHEMAS[action].safeParse(args);
 if(!parsed.success)return null;
 if(['wf_review_submit','wf_review_inline'].includes(action)&&!reviewBodyOk(parsed.data))return null;
 return parsed.data;
}
export const validWfV1Args = (action, args) => parseWfV1Args(action,args)!==null &&
  (action !== 'wf_review_submit' && action !== 'wf_review_inline' || reviewBodyOk(args));

// Check the fixed success envelope before returning service data to a caller.
const RESPONSE_KEYS = {
  wf_pr_get:['pull'],
  wf_pr_reviews:['reviews','complete'],
  wf_pr_comments:['comments','complete'],
  wf_issue_comments:['comments','complete'],
  wf_commit_get:['commit'],
  wf_compare:['comparison'],
  wf_branch_get:['branch'],
  wf_repo_get:['repository'],
  wf_labels:['labels','complete'],
  wf_workflows:['workflows'],
  wf_run_get:['run'],
  wf_run_artifacts:['artifacts'],
  wf_workflow_enable:['completed','workflow_id'],
  wf_workflow_disable:['completed','workflow_id'],
  wf_run_delete:['completed','run_id'],
  wf_artifact_delete:['completed','artifact_id'],

  wf_release_assets:['assets','complete'],wf_release_asset_get:['asset'],wf_release_asset_upload:['asset'],
  wf_review_submit:['review'],wf_review_inline:['comment'],wf_branch_create:['ref','sha'],
  wf_pr_list: ['pulls','complete'], wf_pr_files: ['files','complete'],
  wf_file_get: ['path','sha','size','encoding','content'], wf_tree: ['sha','truncated','entries'],
  wf_branches: ['branches','complete'], wf_commits: ['commits','complete'],
  wf_code_search: ['total','items','complete'], wf_runs: ['runs','complete'],
  wf_run_jobs: ['jobs','complete'], wf_checks: ['checks','complete'],
  wf_update_branch: ['accepted','message'], wf_run_rerun: ['requested','run_id'],
  wf_run_cancel: ['requested','run_id'], wf_run_approve: ['requested','run_id'],
  wf_file_put: ['path','branch','commit'], wf_files_put: ['branch','commit','files'],
  wf_merge_guarded: ['merged','sha'],
};
const credentialPattern=/(?:github_pat_[A-Za-z0-9_]{20,}|gh[opusr]_[A-Za-z0-9]{20,}|\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|\bst\.[A-Za-z0-9]{20,})/;
export function validWfV1Response(action, body) {
 if(action==='wf_repo_tags'||action==='wf_repo_languages'){
  if(!boundedWorkflowInput(body))return false;
  const text=max=>z.string().min(1).refine(v=>Buffer.byteLength(v,'utf8')<=max).refine(v=>!/(?:github_pat_[A-Za-z0-9_]{20,}|gh[opusr]_[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|\bst\.[A-Za-z0-9]{20,})/.test(v));
  return (action==='wf_repo_tags'?z.object({tags:z.array(z.object({name:text(1024),sha:z.string().regex(/^[0-9a-f]{40}$/)}).strict()).max(50),page:z.number().int().min(1).max(1000),has_more:z.boolean()}).strict():z.object({languages:z.array(z.object({name:text(100),bytes:z.number().int().min(0).max(Number.MAX_SAFE_INTEGER)}).strict()).max(100)}).strict()).safeParse(body).success;
 }

 if(['wf_issue_parent','wf_issue_subissues','wf_issue_blocked_by','wf_issue_blocking'].includes(action)){
  const issue=z.object({id:z.number().int().positive(),number:z.number().int().positive(),title:z.string().refine(v=>Buffer.byteLength(v,'utf8')<=4096).refine(v=>!credentialPattern.test(v)).refine(v=>!/(?:github_pat_[A-Za-z0-9_]{20,}|gh[opusr]_[A-Za-z0-9]{20,}|-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----|\bst\.[A-Za-z0-9]{20,})/.test(v)),state:z.enum(['open','closed']),repo:z.string().regex(/^[A-Za-z0-9._-]{1,100}\/[A-Za-z0-9._-]{1,100}$/).refine(v=>!credentialPattern.test(v)).nullable()}).strict();
  return (action==='wf_issue_parent'?z.object({issue}).strict():z.object({issues:z.array(issue).max(50),page:z.number().int().min(1).max(1000),has_more:z.boolean()}).strict()).safeParse(body).success;
 }

  const keys = RESPONSE_KEYS[action];
  if (!keys || !body || typeof body !== 'object' || Array.isArray(body) ||
      Object.keys(body).sort().join(',') !== [...keys].sort().join(',')) return false;
  for (const k of ['pulls','files','entries','branches','commits','items','runs','jobs','checks','reviews','comments','labels']) {
    if (k in body && !(action === 'wf_files_put' && k === 'files') && !Array.isArray(body[k])) return false;
  }
  if (action.startsWith('wf_release_asset')) {
    const assetOk = x => x && !Array.isArray(x) && Number.isInteger(x.id) && x.id > 0 && typeof x.name === 'string' && x.name.length > 0 && x.name.length <= 255 && ['uploaded','open','starter'].includes(x.state) && Number.isInteger(x.size) && x.size >= 0;
    if (action === 'wf_release_assets' ? !Array.isArray(body.assets) || !body.assets.every(assetOk) : !assetOk(body.asset)) return false;
  }
  for (const k of ['pull','commit','comparison','branch','repository','workflows','run','artifacts']) {
    if ((action === 'wf_file_put' || action === 'wf_files_put') && (k === 'branch' || k === 'commit')) { if (typeof body[k] !== 'string' || body[k].length === 0) return false; continue; }
    if (k in body && (!body[k] || typeof body[k] !== 'object' || Array.isArray(body[k]))) return false;
  }
  if ('completed' in body && body.completed !== true) return false;
  const completedId = {wf_workflow_enable:'workflow_id',wf_workflow_disable:'workflow_id',wf_run_delete:'run_id',wf_artifact_delete:'artifact_id'}[action];
  if (completedId && (!Number.isInteger(body[completedId]) || body[completedId] < 1)) return false;
  if (action === 'wf_review_submit' && (!body.review || !Number.isInteger(body.review.id) || body.review.id < 1 || !["APPROVED","CHANGES_REQUESTED","COMMENTED"].includes(body.review.state) || !/^[0-9a-f]{40}$/.test(body.review.commit_id))) return false;
  if (action === 'wf_review_inline' && (!body.comment || !Number.isInteger(body.comment.id) || body.comment.id < 1 || !/^[0-9a-f]{40}$/.test(body.comment.commit_id))) return false;
  if (action === 'wf_branch_create' && (typeof body.ref !== 'string' || !body.ref.startsWith('refs/heads/') || !/^[0-9a-f]{40}$/.test(body.sha))) return false;
  if ('complete' in body && typeof body.complete !== 'boolean') return false;
  if (action === 'wf_files_put' && (!Number.isInteger(body.files) || body.files < 1 || body.files > 20 || typeof body.branch !== 'string')) return false;
  if ('truncated' in body && typeof body.truncated !== 'boolean') return false;
  if (action === 'wf_merge_guarded') return body.merged === true && /^[0-9a-f]{40}$/.test(body.sha);
  if (action === 'wf_update_branch') return body.accepted === true && typeof body.message === 'string';
  if (['wf_run_rerun','wf_run_cancel','wf_run_approve'].includes(action)) {
    const suffix = {wf_run_rerun:'rerun-failed-jobs',wf_run_cancel:'cancel',wf_run_approve:'approve'};
    return body.requested === suffix[action] && Number.isInteger(body.run_id);
  }
  if (action === 'wf_file_put' && (typeof body.path !== 'string' || body.path.length === 0)) return false;
  if (action !== 'wf_commit_get' && 'commit' in body && !/^[0-9a-f]{40}$/.test(body.commit)) return false;
  return true;
}
