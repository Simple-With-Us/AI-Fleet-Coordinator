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
  wf_issue_comment_update: ['issue comment update.',obj(['repo', 'comment_id', 'body'],{repo:REPO,comment_id:INT,body:{type:"string",minLength:1,maxLength:32768}})],
  wf_issue_comment_delete: ['issue comment delete.',obj(['repo', 'comment_id'],{repo:REPO,comment_id:INT})],
  wf_review_comment_update: ['review comment update.',obj(['repo', 'comment_id', 'body'],{repo:REPO,comment_id:INT,body:{type:"string",minLength:1,maxLength:32768}})],
  wf_review_comment_delete: ['review comment delete.',obj(['repo', 'comment_id'],{repo:REPO,comment_id:INT})],
  wf_release_list: ['release list.',obj(['repo'],{repo:REPO})],
  wf_release_get: ['release get.',obj(['repo', 'release_id'],{repo:REPO,release_id:INT})],
  wf_release_delete: ['release delete.',obj(['repo', 'release_id'],{repo:REPO,release_id:INT})],
  wf_pr_draft: ['pr draft.',obj(['repo','number'],{repo:REPO,number:INT})],
  wf_pr_ready: ['pr ready.',obj(['repo','number'],{repo:REPO,number:INT})],
  wf_automerge_enable: ['automerge enable.',obj(['repo','number','method'],{repo:REPO,number:INT,method:{type:'string',enum:['SQUASH','MERGE','REBASE']}})],
  wf_automerge_disable: ['automerge disable.',obj(['repo','number'],{repo:REPO,number:INT})],
  wf_review_threads: ['review threads.',obj(['repo','number'],{repo:REPO,number:INT})],
  wf_assignees_add: ['assignees add.',obj(['repo', 'number', 'assignees'],{repo:REPO,number:INT,assignees:{type:'array',minItems:1,maxItems:10,items:{type:'string',pattern:'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$'}}})],
  wf_assignees_remove: ['assignees remove.',obj(['repo', 'number', 'assignees'],{repo:REPO,number:INT,assignees:{type:'array',minItems:1,maxItems:10,items:{type:'string',pattern:'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$'}}})],
  wf_issue_lock: ['issue lock.',obj(['repo', 'number', 'reason'],{repo:REPO,number:INT,reason:{type:'string',enum:['off-topic','too heated','resolved','spam']}})],
  wf_issue_unlock: ['issue unlock.',obj(['repo', 'number'],{repo:REPO,number:INT})],
  wf_label_create: ['label create.',obj(['repo', 'name', 'color', 'description'],{repo:REPO,name:STR,color:{type:'string',pattern:'^[0-9a-fA-F]{6}$'},description:STR})],
  wf_label_delete: ['label delete.',obj(['repo', 'name'],{repo:REPO,name:STR})],
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
  wf_workflow_disable: ['Disable a workflow, stopping future CI triggers; can break required checks. No concurrency fence, explicit action/repo and owner execution authority required.',obj(['repo','workflow_id'],{repo:REPO,workflow_id:INT})],
  wf_run_delete: ['Irreversibly delete run and logs. No concurrency fence, explicit action/repo and owner execution authority required.',obj(['repo','run_id'],{repo:REPO,run_id:INT})],
  wf_artifact_delete: ['Irreversibly remove artifact data. No concurrency fence, explicit action/repo and owner execution authority required.',obj(['repo','artifact_id'],{repo:REPO,artifact_id:INT})],

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
export const WF_V1_READ_ACTIONS = Object.freeze(['wf_review_threads','wf_release_list','wf_release_get','wf_pr_get','wf_pr_reviews','wf_pr_comments','wf_issue_comments','wf_commit_get','wf_compare','wf_branch_get','wf_repo_get','wf_labels','wf_workflows','wf_run_get','wf_run_artifacts','wf_release_assets','wf_release_asset_get','wf_pr_list','wf_pr_files','wf_file_get','wf_tree','wf_branches','wf_commits','wf_code_search','wf_runs','wf_run_jobs','wf_checks']);
export const WF_V1_TOOLS = Object.freeze(WF_V1_ACTIONS.map(action => Object.freeze({
  name: `gr_${action}`, description: `${SPECS[action][0]} Inactive candidate; runs as your own GitHub App only when the relay enables workflow verbs.`,
  inputSchema: SPECS[action][1],
})));
const typeOk = (schema, v) => schema.type === 'integer' ? Number.isInteger(v) && v >= schema.minimum && v <= schema.maximum
  : schema.type === 'boolean' ? typeof v === 'boolean'
  : schema.type === 'array' ? Array.isArray(v) && v.length >= schema.minItems && v.length <= schema.maxItems && v.every(x => schema.items.type === "object" ? validWfV1Args2(schema.items, x) : typeOk(schema.items, x))
  : typeof v === 'string' && (!schema.pattern || new RegExp(schema.pattern).test(v)) && (!schema.enum || schema.enum.includes(v)) &&
    (!schema.minLength || v.length >= schema.minLength) && (!schema.maxLength || v.length <= schema.maxLength);
function validWfV1Args2(schema, args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return false;
  if (!schema.required.every(k => Object.hasOwn(args,k))) return false;
  return Object.keys(args).every(k => Object.hasOwn(schema.properties,k) && typeOk(schema.properties[k], args[k]));
}
// Iterative guard runs before recursive schema code or JSON serialization.
export function boundedWorkflowInput(args, {maxDepth=6,maxNodes=500,maxBytes=65536,maxString=65536,maxKeys=100}={}) {
  const stack=[[args,0]], seen=new Set(); let nodes=0, bytes=0;
  while(stack.length) {
    const [value,depth]=stack.pop();
    if(++nodes>maxNodes || depth>maxDepth) return false;
    if(typeof value==='string') {
      if(value.length>maxString) return false;
      bytes+=new TextEncoder().encode(value).length;
    } else if(value && typeof value==='object') {
      if(seen.has(value)) return false; seen.add(value);
      if(Object.getPrototypeOf(value)!==Object.prototype && !Array.isArray(value) && Object.getPrototypeOf(value)!==null) return false;
      const keys=Object.keys(value); if(keys.length>maxKeys) return false;
      for(const key of keys) {
        const d=Object.getOwnPropertyDescriptor(value,key);
        if(!d || !Object.hasOwn(d,'value')) return false;
        bytes+=key.length; stack.push([d.value,depth+1]);
      }
    } else if(!['number','boolean'].includes(typeof value) && value!==null) return false;
    if(bytes>maxBytes) return false;
  }
  return true;
}
// The relay measures review bodies in UTF-8 bytes and refuses blank ones;
// mirror that here so bad input fails before network instead of as a relay 400.
const reviewBodyOk = args => typeof args?.body === 'string' && args.body.trim() !== '' &&
  new TextEncoder().encode(args.body).length <= 32768;
export const validWfV1Args = (action, args) => boundedWorkflowInput(args) && WF_V1_ACTIONS.includes(action) && validWfV1Args2(SPECS[action][1], args) &&
  (action !== 'wf_review_submit' && action !== 'wf_review_inline' && action !== 'wf_issue_comment_update' && action !== 'wf_review_comment_update' || reviewBodyOk(args));

// Check the fixed success envelope before returning service data to a caller.
const RESPONSE_KEYS = {
  wf_issue_comment_update:['result'],
  wf_issue_comment_delete:['deleted'],
  wf_review_comment_update:['result'],
  wf_review_comment_delete:['deleted'],
  wf_release_list:['releases', 'complete'],
  wf_release_get:['result'],
  wf_release_delete:['deleted'],
  wf_workflow_enable:['completed','workflow_id','state'],
  wf_workflow_disable:['completed','workflow_id','state'],
  wf_pr_draft:['pull'],
  wf_pr_ready:['pull'],
  wf_automerge_enable:['pull'],
  wf_automerge_disable:['pull'],
  wf_review_threads:['threads'],
  wf_assignees_add:['result'],
  wf_assignees_remove:['result'],
  wf_issue_lock:['completed'],
  wf_issue_unlock:['completed'],
  wf_label_create:['result'],
  wf_label_delete:['completed'],
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
const shaValue=z.string().regex(/^[0-9a-f]{40}$/),positive=z.number().int().positive();
let jsonValueUnion=null;
const jsonValue=z.lazy(()=>jsonValueUnion??=z.union([z.string(),z.number().finite(),z.boolean(),z.null(),z.array(jsonValue),z.record(z.string(),jsonValue)]));
const objectWith=shape=>z.object(shape).catchall(jsonValue);
const commitObject=objectWith({sha:shaValue,commit:objectWith({message:z.string()}),parents:z.array(objectWith({sha:shaValue}))});
const branchObject=objectWith({name:z.string().min(1),commit:objectWith({sha:shaValue})});
const runObject=objectWith({id:positive,head_sha:shaValue,status:z.string(),conclusion:z.string().nullable()});
const artifactObject=objectWith({id:positive,name:z.string(),size_in_bytes:z.number().int().nonnegative(),expired:z.boolean()});
const workflowObject=objectWith({id:positive,name:z.string(),path:z.string(),state:z.string()});
const pullObject=objectWith({id:positive,number:positive,state:z.enum(['open','closed']),head:objectWith({sha:shaValue}),base:objectWith({sha:shaValue})});
const reviewObject=objectWith({id:positive,state:z.string().max(100),commit_id:shaValue});
const commentObject=objectWith({id:positive,body:z.string().max(32768)});
const labelObject=objectWith({id:positive,name:z.string().max(100),color:z.string().regex(/^[0-9a-fA-F]{6}$/)});
const readResponseSchemas={
 wf_pr_reviews:z.object({reviews:z.array(reviewObject).max(50),complete:z.boolean()}).strict(),
 wf_pr_comments:z.object({comments:z.array(commentObject).max(50),complete:z.boolean()}).strict(),
 wf_issue_comments:z.object({comments:z.array(commentObject).max(50),complete:z.boolean()}).strict(),
 wf_labels:z.object({labels:z.array(labelObject).max(50),complete:z.boolean()}).strict(),
 wf_pr_get:z.object({pull:pullObject}).strict(),
 wf_commit_get:z.object({commit:commitObject}).strict(),
 wf_compare:z.object({comparison:objectWith({status:z.string(),ahead_by:z.number().int().nonnegative(),behind_by:z.number().int().nonnegative(),total_commits:z.number().int().nonnegative(),commits:z.array(commitObject)})}).strict(),
 wf_branch_get:z.object({branch:branchObject}).strict(),
 wf_repo_get:z.object({repository:objectWith({id:positive,name:z.string().min(1),full_name:z.string().min(3),default_branch:z.string().min(1),private:z.boolean()})}).strict(),
 wf_workflows:z.object({workflows:objectWith({total_count:z.number().int().nonnegative(),workflows:z.array(workflowObject).max(50)})}).strict(),
 wf_run_get:z.object({run:runObject}).strict(),
 wf_run_artifacts:z.object({artifacts:objectWith({total_count:z.number().int().nonnegative(),artifacts:z.array(artifactObject).max(50)})}).strict(),
};
export function validWfV1Response(action, body, args) {
 if(!boundedWorkflowInput(body,{maxDepth:12,maxNodes:50000,maxBytes:1048576,maxString:1048576,maxKeys:1000}))return false;
 if(readResponseSchemas[action])return readResponseSchemas[action].safeParse(body).success;

  if (action === 'wf_release_list' && (!Array.isArray(body?.releases) || body.releases.length > 50 || !body.releases.every(x => x && Number.isInteger(x.id) && x.id > 0 && typeof x.draft === 'boolean' && typeof x.prerelease === 'boolean' && Object.keys(x).every(k => ['id','tag_name','name','html_url','created_at','published_at','draft','prerelease'].includes(k)) && ['tag_name','name','html_url','created_at','published_at'].every(k => x[k] === null || typeof x[k] === 'string' && new TextEncoder().encode(x[k]).length <= 2048)))) return false;
 const keysBeforeParse=RESPONSE_KEYS[action];
 if(!keysBeforeParse)return false;
 const shape=Object.fromEntries(keysBeforeParse.map(k=>[k,jsonValue]));
 if(action==='wf_file_put'||action==='wf_files_put'){shape.branch=z.string().min(1);shape.commit=shaValue;if(action==='wf_file_put')shape.path=z.string().min(1);else shape.files=z.number().int().min(1).max(20);}
 const parsed=z.object(shape).strict().safeParse(body);
 if(!parsed.success)return false;
 body=parsed.data;
 const key={wf_workflow_enable:'workflow_id',wf_workflow_disable:'workflow_id',wf_run_delete:'run_id',wf_artifact_delete:'artifact_id'}[action];
 if(key){const shape={completed:z.literal(true),[key]:z.number().int().positive()};if(action==='wf_workflow_enable'||action==='wf_workflow_disable')shape.state=z.literal(action==='wf_workflow_enable'?'enabled':'disabled');const parsed=z.object(shape).strict().safeParse(body);if(!parsed.success)return false;body=parsed.data;}

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
  if (['wf_issue_comment_delete','wf_review_comment_delete','wf_release_delete'].includes(action) && body.deleted !== true) return false;
  if (action === 'wf_release_list' && !Array.isArray(body.releases)) return false;
  if ('result' in body && (!body.result || typeof body.result !== 'object' || Array.isArray(body.result))) return false;
  for (const key of ['pull','threads']) if (key in body && (!body[key] || typeof body[key] !== 'object' || Array.isArray(body[key]))) return false;
  if ('result' in body && (!Number.isInteger(body.result?.id) || body.result.id < 1)) return false;
  if (action.startsWith('wf_assignees_') && (!Number.isInteger(body.result?.number) || !Array.isArray(body.result?.assignees) || !body.result.assignees.every(x => typeof x?.login === 'string'))) return false;
  if (action === 'wf_label_create' && (typeof body.result?.name !== 'string' || !/^[0-9a-fA-F]{6}$/.test(body.result?.color))) return false;
  for (const k of ['pull','commit','comparison','branch','repository','workflows','run','artifacts']) {
    if ((action === 'wf_file_put' || action === 'wf_files_put') && (k === 'branch' || k === 'commit')) { if (typeof body[k] !== 'string' || body[k].length === 0) return false; continue; }
    if (k in body && (!body[k] || typeof body[k] !== 'object' || Array.isArray(body[k]))) return false;
  }
  if ('completed' in body && body.completed !== true) return false;
  if ('result' in body && (!body.result || typeof body.result !== 'object' || Array.isArray(body.result))) return false;
  const completedId = {wf_workflow_enable:'workflow_id',wf_workflow_disable:'workflow_id',wf_run_delete:'run_id',wf_artifact_delete:'artifact_id'}[action];
  if (completedId && (!Number.isInteger(body[completedId]) || body[completedId] < 1)) return false;
  if (completedId && args && body[completedId] !== args[completedId]) return false;
  if (action === 'wf_workflow_enable' && body.state !== 'enabled' || action === 'wf_workflow_disable' && body.state !== 'disabled') return false;
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
