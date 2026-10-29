import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {WF_V1_ACTIONS,WF_V1_TOOLS,WF_V1_READ_ACTIONS,validWfV1Response,validWfV1Args,parseWfV1Args} from '../src/gr-workflow-v1.js';
const py=readFileSync(new URL('../../../git-relay/git_relay/workflow_verbs.py',import.meta.url),'utf8');
const specs=[...py.slice(py.indexOf('SPECS = {'),py.indexOf('ACTIONS = tuple')).matchAll(/"(wf_[a-z_]+)":/g)].map(m=>m[1]);
const onlyOf=name=>{
  const body=py.split(/\n    def /).find(b=>b.startsWith(name+'('));
  const m=body&&body.match(/_only\(\s*args,\s*\(([^)]*)\),?\s*\)/);
  return m?[...m[1].matchAll(/"([a-z0-9_]+)"/g)].map(x=>x[1]).sort():null;
};
test('names equal the relay SPECS',()=>{
  const rel=specs.slice().sort();
  assert.deepEqual([...WF_V1_ACTIONS].sort(),rel);
  assert.equal(WF_V1_TOOLS.length,rel.length);
});
test('argument sets equal the relay _only() sets',()=>{
  for(const t of WF_V1_TOOLS){
    const action=t.name.slice(3);
    const want=['wf_run_rerun','wf_run_cancel','wf_run_approve'].includes(action)?['repo','run_id']:onlyOf(action);
    assert.ok(want,action);
    assert.deepEqual(Object.keys(t.inputSchema.properties).sort(),want,action);
    assert.equal(t.inputSchema.additionalProperties,false);
  }
});
test('writes carry a head fence where the relay has one',()=>{
  for(const a of ['wf_update_branch','wf_files_put','wf_merge_guarded']) assert.ok(WF_V1_TOOLS.find(t=>t.name==='gr_'+a).inputSchema.required.includes('expected_head_sha'));
});
const repo='example/repo';
test('valid and invalid args',()=>{
  assert.ok(validWfV1Args('wf_merge_guarded',{repo,number:9,expected_head_sha:'a'.repeat(40),method:'squash'}));
  assert.ok(!validWfV1Args('wf_merge_guarded',{repo,number:9,expected_head_sha:'a'.repeat(40),url:'https://x.invalid'}));
  assert.ok(!validWfV1Args('wf_merge_guarded',{repo,number:9}));
  assert.ok(!validWfV1Args('wf_merge_guarded',{repo,number:9,expected_head_sha:'zz'}));
  assert.ok(!validWfV1Args('mint_token',{repo}));
  assert.ok(!validWfV1Args('wf_run_rerun',{repo:'a b/c',run_id:1}));
  assert.ok(validWfV1Args('wf_files_put',{repo,branch:'b',message:'m',expected_head_sha:'a'.repeat(40),files:[{path:'a.txt',content_b64:'QQ=='}]}));
  assert.ok(!validWfV1Args('wf_files_put',{repo,branch:'b',message:'m',expected_head_sha:'a'.repeat(40),files:[]}));
});

import {HostedTools, HOSTED_READ_TOOLS, HOSTED_WRITE_TOOLS} from '../src/hosted-tools.js';
import {GrRelayClient} from '../src/gr-tools.js';
import {toolListFor} from '../src/delegated-identities.js';
import {loadConfig} from '../src/config.js';
import {testEnv} from './helpers.mjs';
import * as S from '../src/seat-state.js';
function harness(fetchImpl, enabled=true) {
  const store=S.memoryStore(); const gate={store};
  for (const name of ['reserve','noteRateLimited','idemBegin','idemSet','idemDrop','idemPeek','auditCall','roleSet']) gate[name]=options=>S[name](store,options,Date.now());
  gate.roleGet=()=>S.roleGet(store,Date.now()); gate.callTail=n=>S.callTail(store,n);
  const gr=new GrRelayClient({baseUrl:'https://broker.invalid',accessId:'private-access-identifier',accessSecret:'private-access-secret',bearer:'caller-own-bearer',fetch:fetchImpl});
  return new HostedTools({seat:'JET-CHAT',scopes:['zulip:read','zulip:write'],clientId:'test',grantRef:'abc123',paused:false,config:{...loadConfig(testEnv()),grWorkflowEnabled:enabled},zulip:null,key:'',gate,gr,log:()=>{},sleep:async()=>{}});
}
test('default-off discovery, cache separation and direct-call denial',async()=>{
  assert.equal(loadConfig(testEnv()).grWorkflowEnabled,false);
  assert.ok(!toolListFor('JET-CHAT',{}).list.some(t=>t.name.startsWith('gr_wf_')));
  assert.equal(toolListFor('JET-CHAT',{grWorkflowEnabled:true}).list.filter(t=>t.name.startsWith('gr_wf_')).length,WF_V1_ACTIONS.length);
  assert.ok(!toolListFor('JET-CHAT',{}).list.some(t=>t.name.startsWith('gr_wf_')));
  let calls=0;const t=harness(async()=>{calls++;throw Error('must not fetch');},false);
  const r=await t.call('gr_wf_runs',{repo});
  assert.ok(r.isError);assert.equal(calls,0);
});
test('all workflow tools have dispatch and correct read/write scope',()=>{
  for(const action of WF_V1_ACTIONS) {
    assert.equal(typeof HostedTools.prototype['tool_gr_'+action],'function');
    const read=WF_V1_READ_ACTIONS.includes(action);
    assert.equal(HOSTED_READ_TOOLS.has('gr_'+action),read);
    assert.equal(HOSTED_WRITE_TOOLS.has('gr_'+action),!read);
  }
});
test('end-to-end front dispatch pins origin, caller bearer, Access headers and head fence',async()=>{
  const args={repo,number:9,expected_head_sha:'a'.repeat(40),method:'squash'};let count=0;
  const t=harness(async(url,init)=>{
    count++;assert.equal(url,'https://broker.invalid/v1/wf_merge_guarded');
    assert.equal(init.redirect,'manual');assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');
    assert.equal(init.headers['CF-Access-Client-Secret'],'private-access-secret');
    assert.deepEqual(JSON.parse(init.body),args);
    return new Response(JSON.stringify({merged:true,sha:'b'.repeat(40)}));
  });
  const r=await t.call('gr_wf_merge_guarded',args);
  assert.ok(!r.isError,r.content?.[0]?.text);assert.equal(count,1);
});
test('write failures never retry or claim no write; read failures are retryable',async()=>{
  for(const response of [()=>{throw Error('private-access-secret');},()=>new Response('not json'),()=>new Response(JSON.stringify({outcome:'pending',error:'private prose'}),{status:409}),()=>new Response('edge failure',{status:503})]) {
    let count=0;const t=harness(async()=>{count++;return response();});
    const r=await t.call('gr_wf_run_cancel',{repo,run_id:7});
    assert.ok(r.isError);assert.match(r.content[0].text,/unknown/i);assert.equal(count,1);
    assert.ok(!JSON.stringify(r).includes('private-access-secret'));
  }
});
test('read success, malformed receipts and streamed caps',async()=>{
  const good=harness(async()=>new Response(JSON.stringify({runs:[],complete:false})));
  assert.ok(!(await good.call('gr_wf_runs',{repo})).isError);
  for(const body of [{unexpected:'field'}, 'x'.repeat(1048577),{runs:['private-access-secret'],complete:false}]) {
    const t=harness(async()=>new Response(typeof body==='string'?body:JSON.stringify(body)));
    assert.ok((await t.call('gr_wf_runs',{repo})).isError);
  }
});
test('invalid or extra fields fail before network',async()=>{
  let count=0;const t=harness(async()=>{count++;return new Response('{}');});
  assert.ok((await t.call('gr_wf_merge_guarded',{repo,number:1,expected_head_sha:'bad'})).isError);
  assert.ok((await t.call('gr_wf_runs',{repo,url:'https://evil.invalid'})).isError);
  assert.equal(count,0);
});
test('relay requirements: per_page max50, method/ref required, content nonempty, no inherited fields',()=>{
  assert.ok(!validWfV1Args('wf_runs',{repo,per_page:51}));
  assert.ok(!validWfV1Args('wf_merge_guarded',{repo,number:1,expected_head_sha:'a'.repeat(40)}));
  assert.ok(!validWfV1Args('wf_commits',{repo}));
  assert.ok(!validWfV1Args('wf_file_get',{repo,path:'f'}));
  assert.ok(!validWfV1Args('wf_file_put',{repo,path:'f',branch:'b',message:'m',content_b64:''}));
  assert.ok(!validWfV1Args('wf_runs',JSON.parse('{"repo":"a/b","__proto__":"bad"}')));
  assert.ok(!validWfV1Args('wf_runs',{repo,toString:'bad'}));
});

test('path/ref/repo traversal and structural bombs fail closed',async()=>{
 for(const bad of ['../a','/a','a/../b','a//b']) assert.ok(!validWfV1Args('wf_file_get',{repo,path:bad,ref:'main'}),bad);
 assert.ok(!validWfV1Args('wf_tree',{repo,ref:'../main'}));
 assert.ok(!validWfV1Args('wf_runs',{repo:'../repo'}));
 const cycle={repo};cycle.self=cycle;
 const deep={repo};let cur=deep;for(let i=0;i<20;i++){cur.x={};cur=cur.x;}
 for(const args of [cycle,deep,{repo,term:'x'.repeat(70000)},{repo,x:Array(501).fill('x')}]) {
  assert.ok(!validWfV1Args('wf_runs',args));
  const t=harness(async()=>{throw Error('no network');});
  assert.ok((await t.call('gr_wf_runs',args)).isError);
 }
});

test('success completion, file-count receipts and malformed failure metadata',async()=>{
 const complete=harness(async()=>new Response(JSON.stringify({runs:[],complete:true})));
 assert.ok(!(await complete.call('gr_wf_runs',{repo})).isError);
 const bad=harness(async()=>new Response(JSON.stringify({error:'x',retryable:'yes'}),{status:503}));
 const r=await bad.call('gr_wf_run_cancel',{repo,run_id:1});assert.ok(r.isError);assert.match(r.content[0].text,/unknown/i);
 const args={repo,branch:'b',message:'m',expected_head_sha:'a'.repeat(40),files:[{path:'x',content_b64:'QQ=='}]};
 for(const files of [null,{},[],0,21,'1']) {
   const t=harness(async()=>new Response(JSON.stringify({branch:'b',commit:'a'.repeat(40),files})));
   assert.ok((await t.call('gr_wf_files_put',args)).isError);
 }
 assert.ok(!validWfV1Args('wf_file_put',{repo,path:'a',branch:'b',message:'m',content_b64:'A'.repeat(2796205)}));
});

test('valid refusal context is stripped without losing status or retry semantics',async()=>{
 for(const status of [403,429,503]) {
  const gr=new GrRelayClient({baseUrl:'https://broker.invalid',bearer:'own',fetch:async()=>new Response(JSON.stringify({error:'private denial prose',retryable:true,context:{reason:'private-context'}}),{status})});
  assert.deepEqual(await gr.callWorkflow('wf_runs',{repo}),{status,body:{error:'workflow_request_refused',retryable:true}});
 }
});

test('typed and workflow discovery gates have independent cache keys',()=>{
 const flags={grWorkflowEnabled:true,grTypedEnabled:true};
 assert.equal(toolListFor('JET-CHAT',flags).list.filter(t=>t.name.startsWith('gr_wf_')).length,WF_V1_ACTIONS.length);
 assert.equal(toolListFor('JET-CHAT',flags).list.filter(t=>t.name.startsWith('gr_v1_pr_')).length,3);
 assert.equal(toolListFor('JET-CHAT',{grWorkflowEnabled:true}).list.filter(t=>t.name.startsWith('gr_v1_pr_')).length,0);
 assert.equal(toolListFor('JET-CHAT',{grTypedEnabled:true}).list.filter(t=>t.name.startsWith('gr_wf_')).length,0);
 assert.ok(!toolListFor('JET-CHAT',{}).list.some(t=>t.name.startsWith('gr_wf_')||t.name.startsWith('gr_v1_pr_')));
});

test('release assets are metadata reads and bounded upload with fail-closed receipts',async()=>{
 const asset={id:7,name:'proof.txt',state:'uploaded',size:2};
 for(const [action,args,body] of [
  ['wf_release_assets',{repo,release_id:3},{assets:[asset],complete:true}],
  ['wf_release_asset_get',{repo,asset_id:7},{asset}],
  ['wf_release_asset_upload',{repo,release_id:3,name:'proof.txt',content_b64:'aGk='},{asset}]
 ]){
  assert.ok(validWfV1Args(action,args));
  const ok=harness(async()=>new Response(JSON.stringify(body)));
  assert.ok(!(await ok.call('gr_'+action,args)).isError);
  const bad=harness(async()=>new Response(JSON.stringify({asset:{...asset,id:true}})));
  assert.ok((await bad.call('gr_'+action,args)).isError);
 }
 assert.ok(!validWfV1Args('wf_release_asset_upload',{repo,release_id:3,name:'../x',content_b64:'aGk='}));
});

test('asset filename trailing dot is refused locally',()=>{
 assert.ok(!validWfV1Args('wf_release_asset_upload',{repo,release_id:3,name:'proof.',content_b64:'aGk='}));
 assert.ok(validWfV1Args('wf_release_asset_upload',{repo,release_id:3,name:'x',content_b64:'aGk='}));
});

test('review/inline/branch additions keep bounded shapes and uncertain write failures',async()=>{
 const commit_id='a'.repeat(40);
 const requests=[
  ['wf_review_submit',{repo,number:1,commit_id,event:'COMMENT',body:'hello'}],
  ['wf_review_inline',{repo,number:1,commit_id,path:'src/a.js',line:1,side:'RIGHT',body:'hello'}],
  ['wf_branch_create',{repo,branch:'wise/new',sha:commit_id}],
 ];
 for(const [action,args] of requests){
  assert.ok(validWfV1Args(action,args));
  assert.ok(!validWfV1Args(action,{...args,force:true}));
  const t=harness(async()=>new Response(JSON.stringify({unexpected:'malformed write'})));
  const result=await t.call('gr_'+action,args);
  assert.ok(result.isError);assert.match(result.content[0].text,/unknown/i);
 }
 assert.ok(!validWfV1Args('wf_review_inline',{...requests[1][1],line:true}));
 assert.ok(!validWfV1Args('wf_review_submit',{...requests[0][1],body:'x'.repeat(32769)}));
 assert.ok(!validWfV1Args('wf_review_submit',{...requests[0][1],body:'   '}));
 assert.ok(!validWfV1Args('wf_review_submit',{...requests[0][1],body:'é'.repeat(20000)}));
 assert.ok(validWfV1Args('wf_review_submit',{...requests[0][1],body:'héllo'}));
});
import {workflowInputLimit,boundedWorkflowInput} from '../src/gr-workflow-v1.js';
test('binary and content tools: schemas, per-action input limit, response envelopes',()=>{
  const r='example/repo';
  assert.equal(workflowInputLimit('wf_release_asset_upload'),65536);
  assert.ok(workflowInputLimit('wf_release_asset_upload_binary')>245760);
  const big='A'.repeat(245760);
  assert.ok(validWfV1Args('wf_release_asset_upload_binary',{repo:r,release_id:3,name:'a.bin',content_b64:big}));
  assert.ok(!validWfV1Args('wf_release_asset_upload_binary',{repo:r,release_id:3,name:'a.bin',content_b64:big+'AAAA'}));
  assert.ok(!validWfV1Args('wf_release_asset_upload',{repo:r,release_id:3,name:'a.bin',content_b64:big}));
  assert.ok(!boundedWorkflowInput({a:big}));
  assert.ok(validWfV1Args('wf_release_asset_delete',{repo:r,asset_id:7,expected_name:'a.bin'}));
  assert.ok(!validWfV1Args('wf_release_asset_delete',{repo:r,asset_id:7}));
  assert.ok(!validWfV1Args('wf_release_asset_delete',{repo:r,asset_id:7,expected_name:'a/b'}));
  assert.ok(!validWfV1Args('wf_release_asset_update',{repo:r,asset_id:7,expected_name:'a',url:'https://x'}));
  for (const [a,k] of [['wf_release_asset_download',{asset_id:7}],['wf_artifact_download',{artifact_id:5}],['wf_run_logs',{run_id:9}],['wf_job_log',{job_id:4}]]) {
    assert.ok(validWfV1Args(a,{repo:r,...k,offset:0,length:262144}));
    assert.ok(!validWfV1Args(a,{repo:r,...k,length:262145}));
    assert.ok(!validWfV1Args(a,{repo:r,...k,offset:-1}));
    assert.ok(!validWfV1Args(a,{repo:r,...k,url:'https://x'}));
  }
  const meta={offset:0,next_offset:3,total:3,complete:true};
  assert.ok(validWfV1Response('wf_release_asset_download',{...meta,asset_id:7,content_b64:'YWJj'}));
  assert.ok(!validWfV1Response('wf_release_asset_download',{...meta,asset_id:7,content_b64:'not base64!'}));
  assert.ok(!validWfV1Response('wf_release_asset_download',{...meta,asset_id:7,content_b64:'YWJj',extra:1}));
  assert.ok(validWfV1Response('wf_job_log',{...meta,job_id:4,content:'hi'}));
  assert.ok(!validWfV1Response('wf_job_log',{...meta,total:'3',job_id:4,content:'hi'}));
  assert.ok(validWfV1Response('wf_release_asset_delete',{deleted:true,asset_id:7}));
  assert.ok(!validWfV1Response('wf_release_asset_delete',{deleted:false,asset_id:7}));
  assert.ok(validWfV1Response('wf_release_asset_update',{asset:{id:7,name:'a',state:'uploaded',size:1}}));
});

test('review lifecycle closed args and receipt validation',()=>{
 for (const [action,args] of [['wf_review_update',{number:1,review_id:2,body:'edit'}],['wf_review_dismiss',{number:1,review_id:2,message:'obsolete'}],['wf_review_pending_delete',{number:1,review_id:2}],['wf_review_comment_reply',{number:1,comment_id:2,body:'reply'}]]) {
  assert.ok(validWfV1Args(action,{repo,...args}),action);
  assert.ok(!validWfV1Args(action,{repo,...args,force:true}),action);
  assert.ok(!validWfV1Response(action,{}),action);
 }
});
test('branch and tag operations preserve fixed receipts and write UNKNOWN',async()=>{
 const cases=[['wf_branch_update',{branch:'feature/x',sha:'b'.repeat(40),expected_sha:'a'.repeat(40)},{ref:'refs/heads/feature/x',sha:'b'.repeat(40)}],['wf_branch_delete',{branch:'feature/x',expected_sha:'a'.repeat(40)},{deleted:true,ref:'refs/heads/feature/x'}],['wf_tag_create',{tag:'v1',sha:'a'.repeat(40)},{ref:'refs/tags/v1',sha:'a'.repeat(40)}],['wf_tag_get',{tag:'v1'},{ref:'refs/tags/v1',sha:'a'.repeat(40)}],['wf_tag_delete',{tag:'v1',expected_sha:'a'.repeat(40)},{deleted:true,ref:'refs/tags/v1'}]];
 for(const [action,args,reply] of cases){
  const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});
  assert.ok(!(await t.call('gr_'+action,{repo,...args})).isError,action);
  if(action!=='wf_tag_get'){const fail=harness(async()=>{throw Error('no reply');});const r=await fail.call('gr_'+action,{repo,...args});assert.ok(r.isError);assert.match(r.content[0].text,/unknown/i);}
 }
});

test('actual review regression',()=>{assert.ok(!validWfV1Response('wf_branch_update',{ref:'refs/tags/v1',sha:'a'.repeat(40)})); assert.ok(!validWfV1Response('wf_tag_delete',{deleted:true,ref:'refs/heads/main'}));});
test('merge queue closed inputs fixed own-bearer calls and unknown writes',async()=>{
 for(const action of ['wf_merge_queue_entry','wf_merge_queue_enqueue','wf_merge_queue_dequeue']){
  const args={repo,number:1,...(action==='wf_merge_queue_entry'?{}:{expected_sha:'a'.repeat(40)})};
  assert.ok(validWfV1Args(action,args));assert.ok(!validWfV1Args(action,{...args,jump:true}));
  const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({entry:action==='wf_merge_queue_entry'?null:{id:'MQ1',pull_request_id:'PR1'}}));});
  assert.ok(!(await t.call('gr_'+action,args)).isError);
  if(action!=='wf_merge_queue_entry'){const lost=harness(async()=>{throw Error('lost');});const out=await lost.call('gr_'+action,args);assert.ok(out.isError);assert.match(out.content[0].text,/unknown/i);}
 }
});
test('fixed PR controls and thread reads preserve scope and UNKNOWN',async()=>{
 for(const action of ['wf_pr_ready','wf_pr_draft','wf_automerge_disable','wf_automerge_enable','wf_review_threads']){
  const args={repo,number:1,...(action==='wf_automerge_enable'?{method:'SQUASH'}:{})};
  const t=harness(async(url)=>{assert.ok(url.endsWith('/v1/'+action));return new Response(JSON.stringify(action==='wf_review_threads'?{threads:{nodes:[]}}:{pull:{id:'PR_abc'}}));});
  assert.ok(!(await t.call('gr_'+action,args)).isError,action);
  if(action!=='wf_review_threads'){const fail=harness(async()=>{throw Error('lost');});const r=await fail.call('gr_'+action,args);assert.ok(r.isError);assert.match(r.content[0].text,/unknown/i);}
 }
});
test('issue assignment labels and locking preserve own-bearer writes and UNKNOWN',async()=>{
 const cases=[['wf_assignees_add',{number:2,assignees:['swu-wise']},{result:{id:1,number:2,assignees:[{login:'swu-wise'}]}}],['wf_assignees_remove',{number:2,assignees:['swu-wise']},{result:{id:1,number:2,assignees:[]}}],['wf_issue_lock',{number:2,reason:'resolved'},{completed:true}],['wf_issue_unlock',{number:2},{completed:true}],['wf_label_create',{name:'n',color:'aabbcc',description:'d'},{result:{id:1,name:'n',color:'aabbcc'}}],['wf_label_delete',{name:'n'},{completed:true}]];
 for(const [action,args,reply] of cases){
  const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});
  assert.ok(!(await t.call('gr_'+action,{repo,...args})).isError,action);
  const fail=harness(async()=>{throw Error('no response');});const r=await fail.call('gr_'+action,{repo,...args});assert.ok(r.isError);assert.match(r.content[0].text,/unknown/i);
 }
});

test('issue management rejects empty receipts and invalid logins',()=>{
 assert.equal(validWfV1Response('wf_assignees_add',{result:{}}),false);
 assert.equal(validWfV1Args('wf_assignees_add',{repo,number:1,assignees:['-bad']}),false);
});
test('workflow state and run/artifact deletion preserve own-bearer writes and UNKNOWN',async()=>{
 const cases=[['wf_workflow_enable','workflow_id'],['wf_workflow_disable','workflow_id'],['wf_run_delete','run_id'],['wf_artifact_delete','artifact_id']];
 for(const [action,key] of cases){
  const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({completed:true,[key]:7,...(action==='wf_workflow_enable'?{state:'enabled'}:action==='wf_workflow_disable'?{state:'disabled'}:{})}));});
  assert.ok(!(await t.call('gr_'+action,{repo,[key]:7})).isError,action);
  const fail=harness(async()=>{throw Error('no response');});const r=await fail.call('gr_'+action,{repo,[key]:7});assert.ok(r.isError);assert.match(r.content[0].text,/unknown/i);
 }
});

test('actual review regression',()=>{for(const id of [0,-1,true,'1']) assert.ok(!validWfV1Response('wf_run_delete',{completed:true,run_id:id}));});
test('new read envelopes reject malformed objects and fixed routes use own bearer',async()=>{
  const cases=[['wf_pr_get',{number:1},{pull:{id:1,number:1,state:'open',head:{sha:'a'.repeat(40)},base:{sha:'b'.repeat(40)}}}],['wf_compare',{base:'main',head:'feature/x'},{comparison:{status:'identical',ahead_by:0,behind_by:0,total_commits:0,commits:[]}}],['wf_repo_get',{},{repository:{id:1,name:'Repo',full_name:repo,default_branch:'main',private:true}}],['wf_commit_get',{sha:'a'.repeat(40)},{commit:{sha:'a'.repeat(40),commit:{message:'x'},parents:[]}}],['wf_run_get',{run_id:1},{run:{id:1,head_sha:'a'.repeat(40),status:'completed',conclusion:'success'}}],['wf_run_artifacts',{run_id:1},{artifacts:{total_count:0,artifacts:[]}}]];
  for(const [action,args,result] of cases){
    let calls=0;
    const t=harness(async(url,init)=>{
      calls++;assert.ok(url.endsWith('/v1/'+action));
      assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');
      assert.deepEqual(JSON.parse(init.body),{repo,...args});
      return new Response(JSON.stringify(result));
    });
    const answer=await t.call('gr_'+action,{repo,...args});
    assert.ok(!answer.isError,action+JSON.stringify(answer));assert.equal(calls,1);
  }
});

test('read object checks preserve valid existing file-write string receipts',()=>{
 assert.ok(validWfV1Response('wf_file_put',{path:'a.txt',branch:'feature/x',commit:'a'.repeat(40)}));
 assert.ok(validWfV1Response('wf_files_put',{branch:'feature/x',commit:'a'.repeat(40),files:2}));
 assert.ok(!validWfV1Response('wf_branch_get',{branch:'main'}));
 assert.ok(!validWfV1Response('wf_commit_get',{commit:'a'.repeat(40)}));
});

test('file write receipts require string branch and path',()=>{
 for (const bad of [{path:'x',branch:{},commit:'a'.repeat(40)},{path:2,branch:'b',commit:'a'.repeat(40)},{path:'x',branch:'',commit:'a'.repeat(40)}]) assert.equal(validWfV1Response('wf_file_put',bad),false);
 assert.equal(validWfV1Response('wf_file_put',{path:'x',branch:'b',commit:'a'.repeat(40)}),true);
});
test('issue relation pages closed boundary and own bearer',async()=>{
 const a={repo,number:1,page:2,per_page:2},reply={issues:[{id:7,number:8,title:'Child',state:'open',repo}],page:2,has_more:false};
 assert.ok(validWfV1Args('wf_issue_subissues',a));assert.ok(!validWfV1Args('wf_issue_subissues',{...a,page:1001}));
 assert.ok(validWfV1Response('wf_issue_subissues',reply));assert.ok(!validWfV1Response('wf_issue_subissues',{...reply,issues:[{id:true,number:8,title:'x',state:'open'}]}));
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_issue_subissues'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});assert.ok(!(await t.call('gr_wf_issue_subissues',a)).isError);
});

test('relations title credential rejected and closed nongit Zod before dispatch',()=>{
 assert.equal(validWfV1Response('wf_issue_parent',{issue:{id:1,number:2,title:'ghp_'+'x'.repeat(36),state:'open',repo:null}}),false); // Synthetic scanner-shape fixture only, never a credential.
 assert.equal(validWfV1Args('wf_issue_blocking',{repo,number:1,raw:true}),false);
});

test('repo metadata exact own-bearer reads and bounded closed receipts',async()=>{
 for(const [action,args,reply] of [['wf_repo_tags',{page:2,per_page:1},{tags:[{name:'v1',sha:'a'.repeat(40)}],page:2,has_more:true}],['wf_repo_languages',{},{languages:[{name:'C++',bytes:7}]}]]){
 const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});assert.ok(!(await t.call('gr_'+action,{repo,...args})).isError);assert.ok(!validWfV1Args(action,{repo,...args,url:'x'}));
 }
 assert.equal(validWfV1Response('wf_repo_languages',{languages:[{name:'Python',bytes:true}]}),false);assert.equal(validWfV1Response('wf_repo_tags',{tags:[{name:'x',sha:'main'}],page:1,has_more:false}),false);
});

test('relation provider-pattern scanner and request bounds fail closed',async()=>{
 // Runtime-assembled synthetic scanner fixtures only, never credentials.
 const issue={id:1,number:2,title:'fixture',state:'open',repo:null};
 for(const prefix of ['sk-','sk-proj-','sk-ant-']){
  const fake=prefix+'x'.repeat(30);
  assert.equal(validWfV1Response('wf_issue_parent',{issue:{...issue,title:fake}}),false);
  assert.equal(validWfV1Response('wf_issue_parent',{issue:{...issue,repo:'fixture/'+fake}}),false);
 }
 let calls=0;const h=harness(async()=>{calls++;throw Error('not reached');});
 const cycle={repo};cycle.self=cycle;
 for(const args of [cycle,{repo,term:'x'.repeat(70000)}, {repo,extra:[[[[[[[1]]]]]]]}])assert.ok((await h.call('gr_wf_repo_tags',args)).isError);
 assert.equal(calls,0);
});
test('bounded git object fixed own-bearer calls and unknown blob creates',async()=>{
 for(const [action,args,reply] of [['wf_git_blob_get',{sha:'a'.repeat(40)},{blob:{sha:'a'.repeat(40),size:2,text:'hi'}}],['wf_git_blob_create',{text:'hi'},{blob:{sha:'a'.repeat(40),size:2}}],['wf_git_commit_get',{sha:'a'.repeat(40)},{commit:{sha:'a'.repeat(40),tree_sha:'b'.repeat(40),parents:[],message:'message'}}],['wf_git_tree_get',{sha:'a'.repeat(40)},{tree:{sha:'a'.repeat(40),entries:[],truncated:false}}]]){
  assert.ok(validWfV1Args(action,{repo,...args}));assert.ok(!validWfV1Args(action,{repo,...args,url:'https://evil.invalid'}));
  const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});assert.ok(!(await t.call('gr_'+action,{repo,...args})).isError);
 }
 const lost=harness(async()=>{throw Error('lost');});const out=await lost.call('gr_wf_git_blob_create',{repo,text:'hi'});assert.ok(out.isError);assert.match(out.content[0].text,/unknown/i);
});

test('unattached tree commit writes have closed schemas own bearer and UNKNOWN',async()=>{
 for(const [action,args,reply] of [['wf_git_tree_create',{base_tree:'a'.repeat(40),files:[{path:'x.txt',text:'hi',mode:'100644'}]},{tree:{sha:'b'.repeat(40),files:1}}],['wf_git_commit_create',{tree_sha:'a'.repeat(40),parents:['b'.repeat(40)],message:'edit'},{commit:{sha:'c'.repeat(40),tree_sha:'a'.repeat(40),parents:['b'.repeat(40)]}}]]){
  assert.ok(validWfV1Args(action,{repo,...args}));assert.ok(!validWfV1Args(action,{repo,...args,author:'other'}));
  const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});assert.ok(!(await t.call('gr_'+action,{repo,...args})).isError);
  const lost=harness(async()=>{throw Error('lost');});const out=await lost.call('gr_'+action,{repo,...args});assert.ok(out.isError);assert.match(out.content[0].text,/unknown/i);
 }
});

test('Git create UTF8 budgets and strict receipt parent sets',()=>{
 const args={repo,tree_sha:'a'.repeat(40),parents:['b'.repeat(40)],message:'edit'};
 assert.ok(!validWfV1Args('wf_git_commit_create',{...args,message:'é'.repeat(16385)}));
 assert.ok(!validWfV1Args('wf_git_commit_create',{...args,parents:['b'.repeat(40),'b'.repeat(40)]}));
 assert.ok(!validWfV1Args('wf_git_blob_create',{repo,text:'é'.repeat(20001)}));
 assert.ok(!validWfV1Response('wf_git_commit_create',{commit:{sha:'a'.repeat(40),tree_sha:'b'.repeat(40),parents:[]}}));
 assert.ok(!validWfV1Response('wf_git_commit_create',{commit:{sha:'a'.repeat(40),tree_sha:'b'.repeat(40),parents:['c'.repeat(40),'c'.repeat(40)]}}));
});

test('all actions parse strict Zod before dispatch including nongit',async()=>{
 for(const args of [{repo,number:1,extra:1},{repo,number:true}])assert.equal(validWfV1Args('wf_pr_get',args),false);
 let calls=0;const t=harness(async()=>{calls++;throw Error('must not dispatch');});const r=await t.call('gr_wf_pr_get',{repo,number:1,extra:1});assert.ok(r.isError);assert.equal(calls,0);
 let sent=0;const ok=harness(async(url,init)=>{sent++;assert.ok(url.endsWith('/v1/wf_git_commit_create'));assert.equal(JSON.parse(init.body).message,'edit');return new Response(JSON.stringify({commit:{sha:'a'.repeat(40),tree_sha:'b'.repeat(40),parents:['c'.repeat(40)]}}));});assert.ok(!(await ok.call('gr_wf_git_commit_create',{repo,tree_sha:'b'.repeat(40),parents:['c'.repeat(40)],message:'edit'})).isError);assert.equal(sent,1);
});

test('comment and release lifecycle envelopes and write UNKNOWN',async()=>{
 const cases=[['wf_issue_comment_update',{comment_id:2,body:'updated'},{result:{id:2}}],['wf_issue_comment_delete',{comment_id:2},{deleted:true}],['wf_review_comment_update',{comment_id:2,body:'updated'},{result:{id:2}}],['wf_review_comment_delete',{comment_id:2},{deleted:true}],['wf_release_list',{},{releases:[],complete:false}],['wf_release_get',{release_id:4},{result:{id:4}}],['wf_release_delete',{release_id:4},{deleted:true}]];
 for(const [action,args,reply] of cases){
  const t=harness(async(url)=>{assert.ok(url.endsWith('/v1/'+action));return new Response(JSON.stringify(reply));});
  assert.ok(!(await t.call('gr_'+action,{repo,...args})).isError,action);
  if(!WF_V1_READ_ACTIONS.includes(action)){
   const fail=harness(async()=>{throw Error('no response');});const r=await fail.call('gr_'+action,{repo,...args});assert.ok(r.isError);assert.match(r.content[0].text,/unknown/i);
  }
 }
});

test('actual review regression',()=>{for(const id of [0,-1,true,'1']) assert.ok(!validWfV1Response('wf_run_delete',{completed:true,run_id:id}));});

test('issue relation pages closed boundary and own bearer',async()=>{
 const a={repo,number:1,page:2,per_page:2},reply={issues:[{id:7,number:8,title:'Child',state:'open',repo}],page:2,has_more:false};
 assert.ok(validWfV1Args('wf_issue_subissues',a));assert.ok(!validWfV1Args('wf_issue_subissues',{...a,page:1001}));
 assert.ok(validWfV1Response('wf_issue_subissues',reply));assert.ok(!validWfV1Response('wf_issue_subissues',{...reply,issues:[{id:true,number:8,title:'x',state:'open'}]}));
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_issue_subissues'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});assert.ok(!(await t.call('gr_wf_issue_subissues',a)).isError);
});

// Runtime-assembled synthetic credential shapes, never credentials.
test('relations title credential rejected and closed nongit Zod before dispatch',()=>{
 assert.equal(validWfV1Response('wf_issue_parent',{issue:{id:1,number:2,title:'ghp_'+'x'.repeat(36),state:'open',repo}}),false);
 assert.equal(validWfV1Args('wf_issue_blocking',{repo,number:1,raw:true}),false);
});

test('history metadata closed own-bearer reads and nullable absent fields',async()=>{
 const event={id:1,event:'commented',created_at:null,commit_id:null};
 for(const action of ['wf_issue_events','wf_issue_timeline']){
 const args={repo,number:1,page:2,per_page:1};assert.ok(validWfV1Args(action,args));assert.ok(!validWfV1Args(action,{...args,page:1001}));assert.ok(!validWfV1Args(action,{...args,url:'https://evil.invalid'}));
 const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');assert.deepEqual(JSON.parse(init.body),args);return new Response(JSON.stringify({events:[event],page:2,has_more:true}));});assert.ok(!(await t.call('gr_'+action,args)).isError);
 assert.equal(validWfV1Response(action,{events:[{...event,body:'not allowed'}],page:1,has_more:false}),false);
 }
});

test('documented hyphenated timeline kinds pass receipt boundary',()=>{
 for(const event of ['cross-referenced','line-commented','commit-commented'])assert.ok(validWfV1Response('wf_issue_timeline',{events:[{event,id:null,created_at:null,commit_id:null}],page:1,has_more:false}));
});
// Synthetic runtime-assembled credential shapes below are never credentials.
test('relations title credential rejected and closed nongit Zod before dispatch',()=>{
 assert.equal(validWfV1Response('wf_issue_parent',{issue:{id:1,number:2,title:'ghp_'+'x'.repeat(36),state:'open',repo}}),false);
 assert.equal(validWfV1Args('wf_issue_blocking',{repo,number:1,raw:true}),false);
 // Synthetic runtime-only shapes, never credentials.
 for(const title of ['sk-'+'x'.repeat(30),'sk-ant-'+'x'.repeat(30)]) {
  assert.equal(validWfV1Args('wf_issue_create',{repo,title,body:'ok body'}),false);
  assert.equal(validWfV1Args('wf_issue_update',{repo,number:1,title}),false);
 }
});

test('dependency writes exact own bearer, closed shapes and unknown no replay',async()=>{
 for(const action of ['wf_issue_dependency_add','wf_issue_dependency_remove']){
 const args={repo,number:1,blocker_number:2};assert.ok(validWfV1Args(action,args));assert.ok(!validWfV1Args(action,{...args,issue_id:8}));
 const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');assert.deepEqual(JSON.parse(init.body),args);return new Response(JSON.stringify({completed:true,number:1,blocker_number:2,blocking:action.endsWith('_add')}));});assert.ok(!(await t.call('gr_'+action,args)).isError);
 const bad=harness(async()=>{throw Error('lost');});assert.match((await bad.call('gr_'+action,args)).content[0].text,/unknown/i);
 assert.equal(validWfV1Response(action,{completed:true,number:1,blocker_number:true,blocking:true}),false);
 }
});
test('management writes use fixed routes, own bearer and UNKNOWN on transport failure',async()=>{
  const cases=[['wf_labels_set',{number:3,labels:['bug']},{labels:[]}],['wf_reviewers_request',{number:3,reviewers:['swu-wise']},{result:{id:1,requested_reviewers:[]}}],['wf_reviewers_remove',{number:3,reviewers:['swu-wise']},{result:{id:1,requested_reviewers:[]}}],['wf_release_create',{tag:'v1',name:'n',body:'b',draft:true,prerelease:false},{result:{id:1}}],['wf_release_update',{release_id:2,name:'n',body:'b',draft:false,prerelease:true},{result:{id:2}}]];
  for(const [action,args,reply] of cases){
    let calls=0;const t=harness(async(url,init)=>{calls++;assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});
    assert.ok(!(await t.call('gr_'+action,{repo,...args})).isError,action);assert.equal(calls,1);
    const fail=harness(async()=>{throw Error('private failure');});
    const r=await fail.call('gr_'+action,{repo,...args});assert.ok(r.isError);assert.match(r.content[0].text,/unknown/i);
  }
});

test('actual review regression',()=>{assert.ok(!validWfV1Args('wf_labels_set',{repo,number:1,labels:['\nattack']})); assert.ok(!validWfV1Args('wf_reviewers_request',{repo,number:1,reviewers:['\nattack']}));});

test('management receipts and UTF8 input fail closed',()=>{
 assert.equal(validWfV1Response('wf_labels_set',{labels:[{}]}),false);
 assert.equal(validWfV1Response('wf_release_create',{result:{}}),false);
 assert.equal(validWfV1Args('wf_release_create',{repo,tag:'v1',name:'n',body:'é'.repeat(20000),draft:true,prerelease:false}),false);
});

test('environment reads exact own bearer and nullable unknown protection summary',async()=>{
 const args={repo,name:'staging'},environment={id:7,name:'staging',created_at:'x',updated_at:'x',protection_rule_types:null,deployment_branch_policy:null};
 assert.ok(validWfV1Args('wf_environment_get',args));assert.ok(!validWfV1Args('wf_environment_get',{...args,approve:true}));
 assert.ok(validWfV1Response('wf_environment_get',{environment}));assert.ok(!validWfV1Response('wf_environment_get',{environment:{...environment,deployment_branch_policy:{protected_branches:'false',custom_branch_policies:false}}}));
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_environment_get'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({environment}));});assert.ok(!(await t.call('gr_wf_environment_get',args)).isError);
});
test('file delete fixed route own bearer and uncertain write',async()=>{
 const args={repo,path:'x.txt',branch:'feature/x',message:'Remove obsolete file',prior_sha:'a'.repeat(40)};
 assert.ok(validWfV1Args('wf_file_delete',args));assert.ok(!validWfV1Args('wf_file_delete',{...args,force:true}));
 const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/wf_file_delete'));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({deleted:true,path:'x.txt',branch:'feature/x',commit:'b'.repeat(40)}));});
 assert.ok(!(await t.call('gr_wf_file_delete',args)).isError);
 const lost=harness(async()=>{throw Error('lost');});const out=await lost.call('gr_wf_file_delete',args);assert.ok(out.isError);assert.match(out.content[0].text,/unknown/i);
});

test('file deletion message byte limit and nonblank parity',()=>{
 const a={repo,path:'x',branch:'feature/x',message:'Remove',prior_sha:'a'.repeat(40)};
 assert.ok(!validWfV1Args('wf_file_delete',{...a,message:' '}));
 assert.ok(!validWfV1Args('wf_file_delete',{...a,message:'é'.repeat(251)}));
 assert.ok(validWfV1Args('wf_file_delete',{...a,message:'é'.repeat(250)}));
});
test('actual review regression',()=>{assert.ok(validWfV1Args('wf_issue_comment_update',{repo,comment_id:1,body:'x'.repeat(501)})); assert.ok(!validWfV1Args('wf_review_comment_update',{repo,comment_id:1,body:'é'.repeat(20000)}));});

test('Actions completion receipt state and request id are bound',()=>{
 assert.ok(!validWfV1Response('wf_run_delete',{completed:true,run_id:2},{run_id:1}));
 assert.ok(!validWfV1Response('wf_workflow_enable',{completed:true,workflow_id:1,state:'disabled'},{workflow_id:1}));
 assert.ok(validWfV1Response('wf_workflow_enable',{completed:true,workflow_id:1,state:'enabled'},{workflow_id:1}));
});

test('all new read endpoint boundaries reject untyped nested collections',()=>{
 for(const [action,body] of [['wf_pr_get',{pull:{}}],['wf_pr_reviews',{reviews:[null],complete:false}],['wf_pr_comments',{comments:[{}],complete:false}],['wf_issue_comments',{comments:[{id:true,body:'x'}],complete:false}],['wf_labels',{labels:[{id:1,name:'x',color:'bad'}],complete:false}],['wf_workflows',{workflows:{arbitrary:true}}],['wf_run_artifacts',{artifacts:{total_count:1,artifacts:[{}]}}]]) assert.equal(validWfV1Response(action,body),false,action);
});
test('destructive Actions descriptions expose unfenced irreversible behavior and boundary validation',()=>{
 for(const a of ['wf_workflow_disable','wf_run_delete','wf_artifact_delete']){
  const t=WF_V1_TOOLS.find(x=>x.name==='gr_'+a);assert.match(t.description,/authorization/);assert.match(t.description,/fence/);
 }
 for(const value of [true,'1',0,-1,null]) assert.ok(!validWfV1Response('wf_run_delete',{completed:true,run_id:value}));
 assert.ok(!validWfV1Response('wf_run_delete',{completed:true,run_id:1,extra:true}));
});

test('response bounds precede Zod recursion and reject cycles getters and oversize',()=>{
 const cycle={};cycle.self=cycle;assert.equal(validWfV1Response('wf_pr_get',cycle),false);
 const getter={};Object.defineProperty(getter,'completed',{enumerable:true,get(){throw Error('must not read');}});assert.equal(validWfV1Response('wf_workflow_enable',getter),false);
 assert.equal(validWfV1Response('wf_pr_get',{pull:{x:'x'.repeat(65537)}}),false);
 assert.equal(validWfV1Response('wf_repo_get',{repository:{}}),false);
});
test('untrusted nested read response Zod boundary rejects malformed fields',()=>{
 for(const [a,b] of [['wf_pr_get',{pull:{}}],['wf_commit_get',{commit:{sha:true}}],['wf_branch_get',{branch:{name:'x',commit:{sha:7}}}],['wf_repo_get',{repository:{id:true}}],['wf_workflows',{workflows:{total_count:1,workflows:[{id:true}]}}],['wf_run_get',{run:{id:1,head_sha:'a'.repeat(40),status:'done',conclusion:7}}],['wf_run_artifacts',{artifacts:{total_count:1,artifacts:[{id:1,name:'x',size_in_bytes:-1,expired:false}]}}]])assert.equal(validWfV1Response(a,b),false,a);
 assert.equal(validWfV1Response('wf_file_put',{branch:'x',path:'a',commit:'a'.repeat(40),unexpected:true}),false);
});

test('comparison malformed and hostile response resource bounds',()=>{
 assert.equal(validWfV1Response('wf_compare',{comparison:{}}),false);
 const cycle={};cycle.x=cycle;assert.equal(validWfV1Response('wf_pr_get',cycle),false);
 const bomb={};Object.defineProperty(bomb,'pull',{enumerable:true,get(){throw Error('must not read');}});assert.equal(validWfV1Response('wf_pr_get',bomb),false);
 assert.equal(validWfV1Response('wf_pr_get',{pull:{x:'x'.repeat(65537)}}),false);
});

test('response budgets admit populated threads and discriminate hostile generic responses',()=>{
 const threads={threads:{pageInfo:{hasNextPage:false,endCursor:null},nodes:[{id:'T1',isResolved:false,isOutdated:false,path:'a.ts',line:1,comments:{nodes:[{id:1,databaseId:2,body:'x',author:{login:'fixture'}}],pageInfo:{hasNextPage:false,endCursor:null}}}]}};

 assert.equal(validWfV1Response('wf_review_threads',threads),true);
 assert.equal(validWfV1Response('wf_file_get',{path:'x',sha:'a'.repeat(40),size:60000,encoding:'base64',content:'eA=='.repeat(20000)}),true);
 const valid={total:1,items:[{path:'a',sha:'b'}],complete:false};assert.equal(validWfV1Response('wf_code_search',valid),true);
 const cycle={total:1,items:[],complete:false};cycle.items.push(cycle);assert.equal(validWfV1Response('wf_code_search',cycle),false);
 const oversized={...valid,items:[{path:'x'.repeat(1048577),sha:'b'}]};assert.equal(validWfV1Response('wf_code_search',oversized),false);
 let nested={};for(let i=0;i<13;i++)nested={x:nested};assert.equal(validWfV1Response('wf_code_search',{...valid,items:[nested]}),false);
});

test('environment receipts work without Node Buffer and refuse hostile nested data',()=>{
 const prior=globalThis.Buffer;
 try {
  globalThis.Buffer=undefined;
  const environment={id:7,name:'staging',created_at:'x',updated_at:'x',protection_rule_types:null,deployment_branch_policy:null};
  assert.equal(validWfV1Response('wf_environment_get',{environment}),true);
  assert.equal(validWfV1Response('wf_environment_get',{environment:{...environment,id:true}}),false);
  const cycle={};cycle.environment=cycle;assert.equal(validWfV1Response('wf_environment_get',cycle),false);
 } finally {globalThis.Buffer=prior;}
});

test('deployment deletion uses strict real id envelope and own bearer',async()=>{
 assert.equal(validWfV1Response('wf_deployment_delete',{deleted:true,deployment_id:2}),true);
 for(const body of [{deleted:true},{deleted:true,deployment_id:true},{deleted:true,deployment_id:2,extra:1}])assert.equal(validWfV1Response('wf_deployment_delete',body),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_deployment_delete'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({deleted:true,deployment_id:2}));});assert.ok(!(await t.call('gr_wf_deployment_delete',{repo,deployment_id:2})).isError);
});
test('milestone delete receipt and writes use strict envelopes and own-bearer route',async()=>{
 const a={repo,milestone:2};
 assert.equal(validWfV1Response('wf_milestone_delete',{deleted:true,number:2}),true);
 for(const bad of [{deleted:true},{deleted:true,number:true},{deleted:true,number:2,extra:1}])assert.equal(validWfV1Response('wf_milestone_delete',bad),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_milestone_delete'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({deleted:true,number:2}));});assert.ok(!(await t.call('gr_wf_milestone_delete',a)).isError);
});

test('commit status own report namespace strict receipts and own bearer',async()=>{
 const args={repo,sha:'a'.repeat(40),context:'swu-wise/report',state:'pending',description:'queued'};
 const status={id:2,state:'pending',context:'swu-wise/report',description:'queued',created_at:null};
 assert.equal(validWfV1Args('wf_commit_status_create',args),true);
 for(const context of ['test','gitleaks'])assert.equal(validWfV1Args('wf_commit_status_create',{...args,context}),false);
 for(const bad of [{...status,id:true},{...status,context:' '},{...status,description:3},{...status,created_at:false},{...status,secret:1}])assert.equal(validWfV1Response('wf_commit_status_create',{status:bad}),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_commit_status_create'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({status}));});assert.ok(!(await t.call('gr_wf_commit_status_create',args)).isError);
});
test('thread resolve receipts are strict and fixed routes preserve own bearer',async()=>{
 const args={repo,number:1,thread_id:'PRRT_fixture'},thread={id:'PRRT_fixture',isResolved:true};
 assert.equal(validWfV1Response('wf_review_thread_resolve',{thread}),true);
 for(const bad of [{...thread,isResolved:false},{...thread,id:3},{...thread,private:'x'}])assert.equal(validWfV1Response('wf_review_thread_resolve',{thread:bad}),false);
 assert.equal(validWfV1Response('wf_review_thread_unresolve',{thread}),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_review_thread_resolve'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({thread}));});assert.ok(!(await t.call('gr_wf_review_thread_resolve',args)).isError);
});
test('binary chunk strict receipt and own bearer upload path',async()=>{
 const good={offset:0,next_offset:3,total:3,complete:true,asset_id:7,content_b64:'YWJj'};
 for(const bad of [{...good,asset_id:true},{...good,next_offset:-1},{...good,complete:false,next_offset:0},{...good,total:'3'}])assert.equal(validWfV1Response('wf_release_asset_download',bad),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_release_asset_upload_binary'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({asset:{id:7,name:'a.bin',state:'uploaded',size:184320}}));});
 assert.ok(!(await t.call('gr_wf_release_asset_upload_binary',{repo,release_id:3,name:'a.bin',content_b64:'YWFh'.repeat(61440)})).isError);
});
test('review lifecycle projected receipts use cached strict schemas and own bearer',async()=>{
 const review={id:2,state:'PENDING',commit_id:'a'.repeat(40),html_url:null};
 assert.equal(validWfV1Response('wf_review_pending_delete',{review}),true);
 assert.equal(validWfV1Response('wf_review_dismiss',{review}),false);
 assert.equal(validWfV1Response('wf_review_pending_delete',{review:{...review,id:true}}),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_review_pending_delete'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({review}));});assert.ok(!(await t.call('gr_wf_review_pending_delete',{repo,number:1,review_id:2})).isError);
});
test('history endpoint-specific ID contract and preparse structural limits',async()=>{
 const noid={id:null,event:'cross-referenced',created_at:null,commit_id:null};
 assert.equal(validWfV1Response('wf_issue_events',{events:[noid],page:1,has_more:false}),false);
 assert.equal(validWfV1Response('wf_issue_timeline',{events:[noid],page:1,has_more:false}),true);
 let calls=0;const t=harness(async()=>{calls++;throw Error('must not dispatch');});
 const args={repo,number:1};args.cycle=args;
 assert.ok((await t.call('gr_wf_issue_events',args)).isError);assert.equal(calls,0);
 // Runtime-only synthetic provider shapes, never credentials.
 for(const title of ['sk-'+'x'.repeat(30),'sk-ant-'+'x'.repeat(30),'sk-proj-'+'x'.repeat(30),'sk_live_'+'x'.repeat(30)])assert.equal(validWfV1Response('wf_issue_parent',{issue:{id:1,number:2,title,state:'open',repo}},{repo}),false);
 const prior=globalThis.Buffer;try{globalThis.Buffer=undefined;assert.equal(validWfV1Response('wf_issue_parent',{issue:{id:1,number:2,title:'ok',state:'open',repo}},{repo}),true);}finally{globalThis.Buffer=prior;}
});

test('relation provider-shaped title and foreign metadata refuse without content leaks',()=>{
 // Synthetic runtime-only shapes, never credentials.
 for(const title of ['sk-'+'x'.repeat(30),'sk-ant-'+'x'.repeat(30),'sk-proj-'+'x'.repeat(30),'sk_live_'+'x'.repeat(30)])assert.equal(validWfV1Response('wf_issue_parent',{issue:{id:1,number:2,title,state:'open',repo}}, {repo}),false);
 assert.equal(validWfV1Response('wf_issue_parent',{issue:{id:1,number:2,title:'private',state:'open',repo:'Other/Repo'}},{repo}),false);
 const prior=globalThis.Buffer;try{globalThis.Buffer=undefined;assert.equal(validWfV1Response('wf_issue_parent',{issue:{id:1,number:2,title:'ok',state:'open',repo}},{repo}),true);}finally{globalThis.Buffer=prior;}
});

test('relation receipts preserve page availability without credentials and use browser-safe cached schemas',async()=>{
 const item={id:1,number:2,title:'<withheld: credential-shaped title>',state:'open',repo};
 const orig=globalThis.Buffer;globalThis.Buffer=undefined;
 try{assert.equal(validWfV1Response('wf_issue_parent',{issue:item}),true);}finally{globalThis.Buffer=orig;}
 // Synthetic runtime-assembled fixture, never a credential. Only exercises receipt rejection.
 for(const title of ['ghp_'+'x'.repeat(36),'sk-proj-'+'x'.repeat(24)])assert.equal(validWfV1Response('wf_issue_parent',{issue:{...item,title}}),false);
 assert.equal(validWfV1Response('wf_issue_subissues',{issues:[item,{...item,title:'normal'}],page:1,has_more:false}),true);
 let calls=0;const t=harness(async(u,i)=>{calls++;assert.ok(u.endsWith('/v1/wf_issue_parent'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify({issue:item}));});
 assert.ok(!(await t.call('gr_wf_issue_parent',{repo,number:1})).isError);assert.equal(calls,1);
 assert.ok((await t.call('gr_wf_issue_parent',{repo,number:1,extra:'x'.repeat(65537)})).isError);assert.equal(calls,1);
 const gr=new GrRelayClient({baseUrl:'https://broker.invalid',bearer:'own',fetch:async()=>{calls++;return new Response('{}');}});
 // Iterative shape/byte guard refuses oversized input before serialization.
 assert.deepEqual(await gr.callWorkflow('wf_issue_parent',{repo,number:1,payload:'x'.repeat(70000)}),{status:400,body:{error:'invalid_workflow_args'}});
 assert.equal(calls,1);
});

test('rename strict receipts correlate paths branch and prior blob and use caller bearer',async()=>{
 const args={repo,source_path:'old.txt',destination_path:'new.txt',branch:'feature/x',message:'Rename file',expected_head_sha:'a'.repeat(40),prior_sha:'b'.repeat(40)};
 const receipt={source_path:'old.txt',destination_path:'new.txt',branch:'feature/x',commit:'c'.repeat(40),blob_sha:'b'.repeat(40)};
 assert.equal(validWfV1Args('wf_file_rename',args),true);
 assert.equal(validWfV1Response('wf_file_rename',receipt,args),true);
 for(const bad of [{...receipt,source_path:'wrong'},{...receipt,blob_sha:'d'.repeat(40)},{...receipt,commit:7},{...receipt,extra:true}])assert.equal(validWfV1Response('wf_file_rename',bad,args),false);
 let calls=0;const t=harness(async(u,i)=>{calls++;assert.ok(u.endsWith('/v1/wf_file_rename'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');assert.deepEqual(JSON.parse(i.body),args);return new Response(JSON.stringify(receipt));});
 assert.ok(!(await t.call('gr_wf_file_rename',args)).isError);assert.equal(calls,1);
 for(const bad of [{...args,force:true},{...args,message:' '},{...args,message:'é'.repeat(251)},{...args,source_path:'../x'}])assert.ok((await t.call('gr_wf_file_rename',bad)).isError);
 assert.equal(calls,1);
});

test('bounded git object fixed own-bearer calls and unknown blob creates',async()=>{
 for(const [action,args,reply] of [['wf_git_blob_get',{sha:'a'.repeat(40)},{blob:{sha:'a'.repeat(40),size:2,text:'hi'}}],['wf_git_blob_create',{text:'hi'},{blob:{sha:'a'.repeat(40),size:2}}],['wf_git_commit_get',{sha:'a'.repeat(40)},{commit:{sha:'a'.repeat(40),tree_sha:'b'.repeat(40),parents:[],message:'message'}}],['wf_git_tree_get',{sha:'a'.repeat(40)},{tree:{sha:'a'.repeat(40),entries:[],truncated:false}}]]){
  assert.ok(validWfV1Args(action,{repo,...args}));assert.ok(!validWfV1Args(action,{repo,...args,url:'https://evil.invalid'}));
  const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});assert.ok(!(await t.call('gr_'+action,{repo,...args})).isError);
 }
 const lost=harness(async()=>{throw Error('lost');});const out=await lost.call('gr_wf_git_blob_create',{repo,text:'hi'});assert.ok(out.isError);assert.match(out.content[0].text,/unknown/i);
});

test('git read contracts accept actual whitespace filenames and refuse absent write actions',()=>{
 const tree={tree:{sha:'a'.repeat(40),truncated:false,entries:[{sha:'b'.repeat(40),type:'blob',mode:'100644',path:' '}]}};
 assert.ok(validWfV1Response('wf_git_tree_get',tree));tree.tree.entries[0].path='';assert.equal(validWfV1Response('wf_git_tree_get',tree),false);
 assert.equal(validWfV1Args('wf_git_commit_create',{}),false);assert.equal(validWfV1Response('wf_git_commit_create',{}),false);
});
test('untrusted nested read response Zod boundary rejects malformed fields',()=>{
 for(const [a,b] of [['wf_pr_get',{pull:{}}],['wf_commit_get',{commit:{sha:true}}],['wf_branch_get',{branch:{name:'x',commit:{sha:7}}}],['wf_repo_get',{repository:{id:true}}],['wf_workflows',{workflows:{total_count:1,workflows:[{id:true}]}}],['wf_run_get',{run:{id:1,head_sha:'a'.repeat(40),status:'done',conclusion:7}}],['wf_run_artifacts',{artifacts:{total_count:1,artifacts:[{id:1,name:'x',size_in_bytes:-1,expired:false}]}}]])assert.equal(validWfV1Response(a,b),false,a);
 assert.equal(validWfV1Response('wf_file_put',{branch:'x',path:'a',commit:'a'.repeat(40),unexpected:true}),false);
});

test('comparison malformed and hostile response resource bounds',()=>{
 assert.equal(validWfV1Response('wf_compare',{comparison:{}}),false);
 const cycle={};cycle.x=cycle;assert.equal(validWfV1Response('wf_pr_get',cycle),false);
 const bomb={};Object.defineProperty(bomb,'pull',{enumerable:true,get(){throw Error('must not read');}});assert.equal(validWfV1Response('wf_pr_get',bomb),false);
 assert.equal(validWfV1Response('wf_pr_get',{pull:{x:'x'.repeat(65537)}}),false);
});

test('response budgets admit populated threads and discriminate hostile generic responses',()=>{
 const threads={threads:{pageInfo:{hasNextPage:false,endCursor:null},nodes:[{id:'T1',isResolved:false,isOutdated:false,path:'a.ts',line:1,comments:{nodes:[{id:1,databaseId:2,body:'x',author:{login:'fixture'}}],pageInfo:{hasNextPage:false,endCursor:null}}}]}};
 assert.equal(validWfV1Response('wf_review_threads',threads),true);
 assert.equal(validWfV1Response('wf_file_get',{path:'x',sha:'a'.repeat(40),size:60000,encoding:'base64',content:'eA=='.repeat(20000)}),true);
 const valid={total:1,items:[{path:'a',sha:'b'}],complete:false};assert.equal(validWfV1Response('wf_code_search',valid),true);
 const cycle={total:1,items:[],complete:false};cycle.items.push(cycle);assert.equal(validWfV1Response('wf_code_search',cycle),false);
 const oversized={...valid,items:[{path:'x'.repeat(1048577),sha:'b'}]};assert.equal(validWfV1Response('wf_code_search',oversized),false);
 let nested={};for(let i=0;i<13;i++)nested={x:nested};assert.equal(validWfV1Response('wf_code_search',{...valid,items:[nested]}),false);
});

test('PR response text scan refuses credential-shaped title and body without blocking plain or null text',()=>{
 // Synthetic-only credential shapes, assembled at runtime; none is a real token.
 const base={id:1,number:1,state:'open',head:{sha:'a'.repeat(40)},base:{sha:'b'.repeat(40)}};
 assert.equal(validWfV1Response('wf_pr_get',{pull:{...base,title:'plain',body:null}}),true);
 for(const key of ['title','body'])for(const value of ['ghp_'+'x'.repeat(30),'github_pat_'+'x'.repeat(30)])assert.equal(validWfV1Response('wf_pr_get',{pull:{...base,[key]:value}}),false);
});


test('Git create receipts bind counts tree and ordered parents to request',async()=>{
 const treeArgs={repo,base_tree:'a'.repeat(40),files:[{path:'x',text:'hi',mode:'100644'}]};
 const commitArgs={repo,tree_sha:'a'.repeat(40),parents:['b'.repeat(40),'c'.repeat(40)],message:'edit'};
 const tree={tree:{sha:'d'.repeat(40),files:1}};
 const commit={commit:{sha:'d'.repeat(40),tree_sha:commitArgs.tree_sha,parents:commitArgs.parents}};
 for(const [action,args,good,bad] of [
  ['wf_git_tree_create',treeArgs,tree,{tree:{...tree.tree,files:2}}],
  ['wf_git_commit_create',commitArgs,commit,{commit:{...commit.commit,tree_sha:'e'.repeat(40)}}],
  ['wf_git_commit_create',commitArgs,commit,{commit:{...commit.commit,parents:[...commitArgs.parents].reverse()}}],
  ['wf_git_commit_create',commitArgs,commit,{commit:{...commit.commit,parents:[commitArgs.parents[0]]}}],
  ['wf_git_commit_create',commitArgs,commit,{commit:{...commit.commit,parents:['e'.repeat(40),'f'.repeat(40)]}}],
 ]){
  assert.equal(validWfV1Response(action,good,args),true);
  assert.equal(validWfV1Response(action,bad,args),false);
  let calls=0;const t=harness(async()=>{calls++;return new Response(JSON.stringify(bad));});
  const result=await t.call('gr_'+action,args);assert.ok(result.isError);assert.match(result.content[0].text,/unknown/i);assert.equal(calls,1);
 }
});
test('persisted status, rename, dismiss and deployment text refuses credential shapes before network',async()=>{
 // Synthetic-only credential shape, assembled at runtime; not a real token.
 const fake='gh'+'p_'+'A'.repeat(36);
 const cases=[
  ['wf_commit_status_create',{repo,sha:'a'.repeat(40),context:'swu-wise/report',state:'pending',description:'queued'},['description']],
  ['wf_file_rename',{repo,source_path:'old.txt',destination_path:'new.txt',branch:'feature/x',message:'Rename file',expected_head_sha:'a'.repeat(40),prior_sha:'b'.repeat(40)},['message']],
  ['wf_review_dismiss',{repo,number:1,review_id:2,message:'obsolete'},['message']],
 ];
 for(const [action,args,fields] of cases){
  assert.ok(parseWfV1Args(action,args),action);
  for(const k of fields){
   const bad={...args,[k]:`see ${fake}`};
   assert.equal(validWfV1Args(action,bad),false,`${action}.${k}`);
   assert.equal(parseWfV1Args(action,bad),null,`${action}.${k}`);
   let calls=0;const t=harness(async()=>{calls++;return new Response('{}');});
   const out=await t.call(`gr_${action}`,bad);
   assert.ok(out.isError);assert.equal(calls,0);assert.ok(!out.content[0].text.includes(fake));
  }
 }
});

test('per-action checks read the Zod-parsed copy, not the raw caller object',()=>{
 // validWfV1Args is the parse itself, so a credential check never sees the raw object.
 const raw={repo,sha:'a'.repeat(40),context:'swu-wise/report',state:'pending',description:'queued'};
 assert.equal(validWfV1Args('wf_commit_status_create',{...raw,extra:1}),false);
 assert.equal(validWfV1Args('wf_commit_status_create',{...raw,description:7}),false);
 const args={repo,sha:'a'.repeat(40),context:'swu-wise/report',state:'pending',description:'queued'};
 const parsed=parseWfV1Args('wf_commit_status_create',args);
 assert.deepEqual(parsed,args);assert.notEqual(parsed,args);
 const trap=Object.defineProperty({...args},'description',{enumerable:true,get(){throw Error('read before validation');}});
 assert.equal(parseWfV1Args('wf_commit_status_create',trap),null);
 assert.equal(parseWfV1Args('wf_not_an_action',args),null);
});

test('full-file request budgets are action-local, encoded and bounded',()=>{
 const one=n=>({repo,path:'source.swift',branch:'review',message:'update',content_b64:'A'.repeat(n-1)+'='});
 for(const n of [60001,191012,2796204]) assert.ok(validWfV1Args('wf_file_put',one(n)));
 assert.ok(!validWfV1Args('wf_file_put',one(2796205)));
 const multi={repo,branch:'review',message:'update',expected_head_sha:'a'.repeat(40),files:[{path:'a',content_b64:'A'.repeat(191012)},{path:'b',content_b64:'A'.repeat(61816)}]};
 assert.ok(validWfV1Args('wf_files_put',multi));
 multi.files.push({path:'c',content_b64:'A'.repeat(6291456)});
 assert.ok(!validWfV1Args('wf_files_put',multi));
 assert.ok(!validWfV1Args('wf_runs',{repo,term:'x'.repeat(70000)}));
});

test('file aggregate counts actual JSON escapes, not just string bytes',()=>{
 const make=()=>({repo,branch:'review',message:'m',expected_head_sha:'a'.repeat(40),files:[{path:'a',content_b64:'A'.repeat(2796204)},{path:'b',content_b64:'A'},{path:'c',content_b64:'A'.repeat(1000000)}]});
 const args=make();
 args.files[1].content_b64='A'.repeat(6291456-new TextEncoder().encode(JSON.stringify(args)).length+1);
 assert.equal(new TextEncoder().encode(JSON.stringify(args)).length,6291456);
 assert.ok(!validWfV1Args('wf_files_put',args));
 args.files[1].content_b64+='A';assert.ok(!validWfV1Args('wf_files_put',args));
 const escaped=make();escaped.message='"'.repeat(500);
 escaped.files[1].content_b64='A'.repeat(6291456-new TextEncoder().encode(JSON.stringify(escaped)).length+2);
 assert.ok(!validWfV1Args('wf_files_put',escaped));
});


test('only file content advertises the larger content limit',()=>{
 const schema=name=>WF_V1_TOOLS.find(t=>t.name==='gr_'+name).inputSchema.properties;
 assert.equal(schema('wf_file_put').content_b64.maxLength,2796204);
 assert.equal(schema('wf_files_put').files.items.properties.content_b64.maxLength,2796204);
 for(const action of ['wf_issue_create','wf_issue_update','wf_issue_comment']){
   const props=schema(action);
   assert.equal(props.body.maxLength,60000);
   if(props.title) assert.equal(props.title.maxLength,60000);
 }
});

test('decoded per-file and aggregate boundaries match relay budgets',()=>{
 const file=n=>({path:'a',content_b64:Buffer.alloc(n,97).toString('base64')});
 const one={repo,branch:'review',message:'update',...file(2*1024*1024)};
 assert.ok(validWfV1Args('wf_file_put',one));
 one.content_b64=Buffer.alloc(2*1024*1024+1,97).toString('base64');
 assert.ok(!validWfV1Args('wf_file_put',one));
 const multi={repo,branch:'review',message:'update',expected_head_sha:'a'.repeat(40),files:[file(2*1024*1024),{...file(2*1024*1024),path:'b'}]};
 assert.ok(validWfV1Args('wf_files_put',multi));
 multi.files.push({...file(1),path:'c'});assert.ok(!validWfV1Args('wf_files_put',multi));
});

test('exact-name check pages and run get correlate head ID and provenance without output',async()=>{
 const row={id:7,head_sha:'a'.repeat(40),name:'Kody Code Review',status:'completed',conclusion:'success',app_id:8},args={repo,head_sha:row.head_sha,check_name:row.name,page:2,per_page:1},receipt={checks:[row],head_sha:row.head_sha,check_name:row.name,page:2,has_more:true};
 assert.equal(validWfV1Response('wf_check_runs_page',receipt,args),true);assert.equal(validWfV1Response('wf_check_runs_page',{...receipt,has_more:false},args),false);assert.equal(validWfV1Response('wf_check_run_get',{check:row},{repo,check_run_id:8,head_sha:row.head_sha}),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_check_runs_page'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_check_runs_page',args)).isError);
});

test('check names fail closed before network and get binds head',async()=>{
 for(const check_name of [' ', 'é'.repeat(128), 'x\0y', '\ud800', 'sk-proj-'+'x'.repeat(24)]){
  assert.equal(validWfV1Args('wf_check_runs_page',{repo,head_sha:'a'.repeat(40),check_name}),false);
  let calls=0;const t=harness(async()=>{calls++;return new Response('{}');});
  assert.ok((await t.call('gr_wf_check_runs_page',{repo,head_sha:'a'.repeat(40),check_name})).isError);assert.equal(calls,0);
 }
 const row={id:7,head_sha:'a'.repeat(40),name:'test',status:'completed',conclusion:'success',app_id:8};
 const args={repo,check_run_id:7,head_sha:row.head_sha};
 assert.equal(validWfV1Response('wf_check_run_get',{check:row},args),true);
 assert.equal(validWfV1Response('wf_check_run_get',{check:{...row,head_sha:'b'.repeat(40)}},args),false);
 let calls=0;const t=harness(async(url)=>{calls++;assert.ok(url.endsWith('/v1/wf_check_run_get'));return new Response(JSON.stringify({check:row}));});
 assert.ok(!(await t.call('gr_wf_check_run_get',args)).isError);assert.equal(calls,1);
});

test('file history immutable range and path correlated strict metadata receipt',async()=>{
 const args={repo,path:'docs/a.txt',head_sha:'a'.repeat(40),page:2,per_page:1},receipt={commits:[{sha:'b'.repeat(40),parents:[]}],path:'docs/a.txt',head_sha:'a'.repeat(40),page:2,has_more:true};
 assert.equal(validWfV1Response('wf_file_history',receipt,args),true);assert.ok(WF_V1_READ_ACTIONS.includes('wf_file_history'));
 for(const bad of [{...receipt,path:'other'},{...receipt,has_more:false},{...receipt,commits:[{sha:'b'.repeat(40),parents:[],message:'private'}]}])assert.equal(validWfV1Response('wf_file_history',bad,args),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_file_history'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_file_history',args)).isError);
});

test('file history closed arguments enforce bounded path head page and no credential-shaped input',()=>{
 const args={repo,path:'docs/a.txt',head_sha:'a'.repeat(40)};
 assert.equal(validWfV1Args('wf_file_history',args),true);
 for(const bad of [{...args,follow:true},{...args,page:0},{...args,page:1001},{...args,per_page:51},{...args,path:'../x'},{...args,path:'a/'.repeat(17)+'x'},{...args,head_sha:true},{...args,path:'ghp_'+'x'.repeat(36)}]) assert.equal(validWfV1Args('wf_file_history',bad),false);
 const receipt={commits:[],path:args.path,head_sha:args.head_sha,page:1,has_more:false};
 for(const bad of [{...receipt,head_sha:'b'.repeat(40)},{...receipt,page:2},{...receipt,commits:[{sha:'b'.repeat(40),parents:Array(17).fill('c'.repeat(40))}]},{...receipt,extra:true}])assert.equal(validWfV1Response('wf_file_history',bad,args),false);
});

test('run pages expose search cap, mutable state and bounded PR-number observations',async()=>{
 const args={repo,status:'waiting',per_page:1},row={id:7,run_attempt:1,workflow_id:8,head_branch:'wise/a',head_sha:'a'.repeat(40),status:'waiting',conclusion:null,pull_numbers:[2]},receipt={runs:[row],total:1,page:1,has_more:true,search_cap:1000,snapshot:false};
 assert.equal(validWfV1Response('wf_runs_page',receipt,args),true);assert.equal(validWfV1Response('wf_runs_page',{...receipt,search_cap:null},args),false);assert.equal(validWfV1Response('wf_runs_page',{...receipt,runs:[{...row,logs_url:'no'}]},args),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_runs_page'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_runs_page',args)).isError);
});
test('workflow pages map numeric IDs to bounded paths not display-name authority',async()=>{
 const args={repo,per_page:1},row={id:7,path:'.github/workflows/ci.yml',state:'active'},receipt={workflows:[row],total:1,page:1,has_more:true,snapshot:false};
 assert.equal(validWfV1Response('wf_workflows_page',receipt,args),true);assert.equal(validWfV1Response('wf_workflows_page',{...receipt,workflows:[{...row,name:'claimed authority'}]},args),false);assert.equal(validWfV1Response('wf_workflows_page',{...receipt,workflows:[{...row,path:'.github/workflows/../ci.yml'}]},args),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_workflows_page'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_workflows_page',args)).isError);
});

test('actions pages refuse closed args and filtered search windows before transport',()=>{
 for(const a of ['wf_runs_page','wf_workflows_page'])for(const bad of [{repo,page:0},{repo,page:1001},{repo,per_page:51},{repo,raw_query:'x'}])assert.equal(validWfV1Args(a,bad),false);
 assert.equal(validWfV1Args('wf_runs_page',{repo,status:'queued',page:21,per_page:50}),false);
 assert.equal(validWfV1Args('wf_runs_page',{repo,page:1000,per_page:50}),true);
});

test('PR file pages are strict metadata only and correlate expected head',async()=>{
 const args={repo,number:2,expected_head_sha:'a'.repeat(40),per_page:1},file={sha:'b'.repeat(40),filename:'a.txt',previous_filename:null,status:'modified',additions:1,deletions:0,changes:1},receipt={files:[file],number:2,head_sha:args.expected_head_sha,page:1,has_more:true,history_complete:false};
 assert.equal(validWfV1Response('wf_pr_files_page',receipt,args),true);assert.equal(validWfV1Response('wf_pr_files_page',{...receipt,history_complete:true},args),false);assert.equal(validWfV1Response('wf_pr_files_page',{...receipt,files:[{...file,patch:'private'}]},args),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_pr_files_page'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_pr_files_page',args)).isError);
});
test('PR file cached receipt refuses oversized escaped page without truncation',()=>{
 const args={repo,number:2,expected_head_sha:'a'.repeat(40)},row={sha:'b'.repeat(40),filename:''.repeat(1000),previous_filename:''.repeat(1000),status:'renamed',additions:1,deletions:1,changes:2};
 assert.equal(validWfV1Response('wf_pr_files_page',{files:Array(30).fill(row),number:2,head_sha:args.expected_head_sha,page:1,has_more:true,history_complete:false},args),false);
});

test('run page observes startup_failure without treating it as successful review',()=>{
 const args={repo,status:'completed'},row={id:7,run_attempt:1,workflow_id:8,head_branch:'wise/a',head_sha:'a'.repeat(40),status:'completed',conclusion:'startup_failure',pull_numbers:[]};
 const receipt={runs:[row],total:1,page:1,has_more:false,search_cap:1000,snapshot:false};
 assert.equal(validWfV1Response('wf_runs_page',receipt,args),true);
 assert.equal(validWfV1Response('wf_runs_page',{...receipt,runs:[{...row,status:'queued'}]},args),false);
});

test('label update metadata uses closed bounded args and exact correlated receipt',async()=>{
 const args={repo,name:'old/label',expected_label_id:7,new_name:'New label',color:'AaBbee',description:''};
 const receipt={label:{id:7,name:'New label',color:'aabbee',description:''}};
 assert.equal(validWfV1Args('wf_label_update',args),true);assert.equal(validWfV1Response('wf_label_update',receipt,args),true);
 for(const bad of [{label:{...receipt.label,id:8}},{label:{...receipt.label,name:'wrong'}},{label:{...receipt.label,extra:true}}])assert.equal(validWfV1Response('wf_label_update',bad,args),false);
 // Synthetic assembled fixture, never a credential.
 for(const bad of [{...args,archived:true},{...args,description:'é'.repeat(51)},{...args,new_name:'ghp_'+'x'.repeat(36)},{...args,new_name:' '}])assert.equal(validWfV1Args('wf_label_update',bad),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_label_update'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');assert.deepEqual(JSON.parse(i.body),args);return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_label_update',args)).isError);
});

test('additive label contract never uses replace-all semantics or leaks unrelated labels',async()=>{
 const args={repo,number:2,labels:['hold']},receipt={number:2,added:[{id:7,name:'hold',color:'000000'}]};assert.equal(validWfV1Response('wf_labels_add',receipt,args),true);assert.equal(validWfV1Args('wf_labels_add',{...args,labels:['hold','HOLD']}),false);assert.equal(validWfV1Response('wf_labels_add',{number:2,added:[...receipt.added,{id:8,name:'other',color:'000000'}]},args),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_labels_add'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');assert.deepEqual(JSON.parse(i.body),args);return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_labels_add',args)).isError);
});

test('review/comment pages preserve provenance and do not turn text into authority',async()=>{
 const args={repo,number:2,expected_head_sha:'a'.repeat(40),per_page:1},row={id:7,author:{id:8,login:'kody-ai[bot]',type:'Bot'},body:'Untrusted text',body_withheld:false,state:'CHANGES_REQUESTED',commit_id:'b'.repeat(40)},receipt={reviews:[row],number:2,head_sha:args.expected_head_sha,page:1,has_more:true};
 assert.equal(validWfV1Response('wf_pr_reviews_page',receipt,args),true);assert.equal(validWfV1Response('wf_pr_reviews_page',{...receipt,reviews:[{...row,body_withheld:true}]},args),false);
 // Synthetic scanner fixture, never a credential.
 assert.equal(validWfV1Response('wf_pr_reviews_page',{...receipt,reviews:[{...row,body:'ghp_'+'x'.repeat(36)}]},args),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_pr_reviews_page'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_pr_reviews_page',args)).isError);
});

test('discussion cached receipt refuses escaped aggregate overflow without dropping records',()=>{
 const args={repo,number:2,expected_head_sha:'a'.repeat(40)},row={id:1,author:{id:2,login:'reviewer',type:'User'},body:'\x01'.repeat(30000),body_withheld:false,state:'COMMENTED',commit_id:'b'.repeat(40)};
 const receipt={reviews:[row,{...row,id:3}],number:2,head_sha:args.expected_head_sha,page:1,has_more:false};
 assert.equal(validWfV1Response('wf_pr_reviews_page',receipt,args),false);
});
test('compare graph page correlates immutable SHA inputs and excludes patches/files',async()=>{
 const args={repo,base_sha:'a'.repeat(40),head_sha:'b'.repeat(40),per_page:1},receipt={...args,merge_base_sha:args.base_sha,status:'ahead',ahead_by:1,behind_by:0,total_commits:1,commits:[{sha:args.head_sha,parents:[args.base_sha]}],page:1,has_more:true};delete receipt.repo;delete receipt.per_page;
 assert.equal(validWfV1Response('wf_compare_commits_page',receipt,args),true);assert.equal(validWfV1Response('wf_compare_commits_page',{...receipt,files:[]},args),false);assert.equal(validWfV1Response('wf_compare_commits_page',{...receipt,base_sha:args.head_sha},args),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_compare_commits_page'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_compare_commits_page',args)).isError);
});
test('PR list pages remain non-snapshot metadata with bounded correlated receipts',async()=>{
 const args={repo,head:'release/a',base:'main',page:2,per_page:1},row={number:2,state:'open',draft:false,head_ref:'release/a',head_sha:'a'.repeat(40),base_ref:'main',base_sha:'b'.repeat(40)},receipt={pulls:[row],page:2,has_more:true,snapshot:false};
 assert.equal(validWfV1Response('wf_pr_list_page',receipt,args),true);assert.equal(validWfV1Response('wf_pr_list_page',{...receipt,pulls:[{...row,state:'closed'}]},args),false);assert.equal(validWfV1Response('wf_pr_list_page',{...receipt,snapshot:true},args),false);assert.equal(validWfV1Response('wf_pr_list_page',{...receipt,pulls:[{...row,title:'omitted'}]},args),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_pr_list_page'));assert.equal(i.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_pr_list_page',args)).isError);
});

test('new label and discussion boundaries reject embedded provider shapes and malformed Unicode',async()=>{
 const args={repo,name:'hold',expected_label_id:7,new_name:'hold',color:'000000',description:'safe'};
 for(const new_name of ['prefixsk-proj-'+'X'.repeat(32),'prefixsk_live_'+'X'.repeat(32),'\ud800']){
  assert.equal(parseWfV1Args('wf_label_update',{...args,new_name}),null);
  let calls=0;const t=harness(async()=>{calls++;throw Error('must not call');});assert.equal((await t.call('gr_wf_label_update',{...args,new_name})).isError,true);assert.equal(calls,0);
 }
 const row={id:7,author:{id:8,login:'robot[bot]',type:'Bot'},body:'prefixsk-proj-'+'X'.repeat(32),body_withheld:false,state:'APPROVED',commit_id:'a'.repeat(40)};
 assert.equal(validWfV1Response('wf_pr_reviews_page',{reviews:[row],number:2,head_sha:'a'.repeat(40),page:1,has_more:false}),false);
 assert.equal(validWfV1Response('wf_pr_reviews_page',{reviews:[{...row,body:'\ud800'}],number:2,head_sha:'a'.repeat(40),page:1,has_more:false}),false);
});

test('PR list retains provider branch metadata without widening write refs',()=>{
 const args={repo:'Simple-With-Us/AI-Fleet-Coordinator'},row={number:2,state:'open',draft:false,head_ref:'deps/pkg@1.2+build#2',head_sha:'a'.repeat(40),base_ref:'main',base_sha:'b'.repeat(40)},body={pulls:[row],page:1,has_more:false,snapshot:false};
 assert.equal(validWfV1Response('wf_pr_list_page',body,args),true);
 assert.equal(validWfV1Args('wf_pr_list_page',{...args,head:row.head_ref}),false);
 for(const head_ref of ['a..b','a@{b','a\n','a/.bad','a.lock'])assert.equal(validWfV1Response('wf_pr_list_page',{...body,pulls:[{...row,head_ref}]},args),false);
});

test('label lookup dot segments and percent escapes never dispatch',()=>{
 const repo='Simple-With-Us/AI-Fleet-Coordinator',base={repo,name:'safe',expected_label_id:7,new_name:'safe',color:'000000',description:''};
 for(const name of ['.','..','a/../b','%2e%2e','a/%2f/b'])for(const k of ['name','new_name'])assert.equal(validWfV1Args('wf_label_update',{...base,[k]:name}),false);
 for(const name of ['.','..','a/../b'])assert.equal(validWfV1Args('wf_labels_add',{repo,number:2,labels:[name]}),false);
});


test('metadata receipts correlate tag pages and reject malformed or credential names',()=>{
 const args={repo,page:2,per_page:1},good={tags:[{name:'v1',sha:'a'.repeat(40)}],page:2,has_more:true};
 assert.ok(validWfV1Response('wf_repo_tags',good,args));
 for(const bad of [{...good,page:1},{...good,has_more:false},{...good,tags:[...good.tags,...good.tags]}])assert.equal(validWfV1Response('wf_repo_tags',bad,args),false);
 // Synthetic scanner-shape fixtures only, assembled at runtime; never credentials.
 for(const name of ['\ud800','bad\nname','bad\0name','sk-'+'x'.repeat(30),'sk_test_'+'x'.repeat(20)]){
  assert.equal(validWfV1Response('wf_repo_tags',{...good,tags:[{name,sha:'a'.repeat(40)}]},args),false);
  assert.equal(validWfV1Response('wf_repo_languages',{languages:[{name,bytes:1}]}),false);
 }
 assert.ok(validWfV1Response('wf_repo_languages',{languages:[{name:'日本語',bytes:1},{name:'C++',bytes:2}]}));
});

test('history receipts bind requested page size and conservative has_more',()=>{
 const row={id:1,event:'closed',created_at:null,commit_id:null};
 for(const action of ['wf_issue_events','wf_issue_timeline']){
  const args={repo:'o/r',number:1,page:2,per_page:1};
  const body={events:[row],page:2,has_more:true};
  assert.equal(validWfV1Response(action,body,args),true);
  for(const extra of [{page:1},{events:[row,row]},{has_more:false}])assert.equal(validWfV1Response(action,{...body,...extra},args),false);
 }
});
test('pending user/team request capability receipts correlate and do not dismiss reviews',()=>{
 const sha='a'.repeat(40);
 for(const op of ['add','remove','rerequest']){
  const a='wf_review_requests_'+op,args={repo,number:3,expected_head_sha:sha,reviewers:['bot'],team_reviewers:['devs']},r={number:3,head_sha:sha,operation:op,reviewers:['bot'],team_reviewers:['devs'],atomic:false};
  assert.ok(validWfV1Args(a,args));assert.ok(validWfV1Response(a,r,args));
  for(const bad of [{...r,number:4},{...r,head_sha:'b'.repeat(40)},{...r,operation:'dismiss'},{...r,atomic:true},{...r,reviewers:['other']},{...r,team_reviewers:['devs','DEVS']},{...r,result:{private:'unbounded'}}])assert.equal(validWfV1Response(a,bad,args),false);
  assert.ok(!WF_V1_READ_ACTIONS.includes(a));
 }
});

test('Actions new read/write requests bind job/run/head/attempt and expose acceptance not completion',()=>{
 for(const [a,op] of [['wf_job_get',null],['wf_job_rerun','job_rerun'],['wf_run_rerun_all','rerun_all'],['wf_run_force_cancel','force_cancel'],['wf_run_logs_delete','logs_delete']]){
  const job=a.startsWith('wf_job_'),args={repo,run_id:3,head_sha:'a'.repeat(40),run_attempt:4,...(job?{job_id:2}:{})};
  const v={job_id:job?2:null,run_id:3,head_sha:'a'.repeat(40),run_attempt:4,status:'completed',conclusion:'success'};
  const r=op?{...v,requested:op,completed:false,atomic:false}:{job:v};
  assert.ok(validWfV1Args(a,args));assert.ok(validWfV1Response(a,r,args));
  for(const change of [{run_id:4},{head_sha:'b'.repeat(40)},{run_attempt:5},{job_id:job?3:2},{status:'completed',conclusion:null}]){
   const changed={...v,...change},bad=op?{...r,...change}:{job:changed};assert.equal(validWfV1Response(a,bad,args),false);
  }
  assert.equal(WF_V1_READ_ACTIONS.includes(a),a==='wf_job_get');
 }
});

test('review request caller shape rejects empty or duplicate principals before network',()=>{
 for(const op of ['add','remove','rerequest'])for(const v of [{},{reviewers:[]},{reviewers:['bot','BOT']},{team_reviewers:['devs','DEVS']},{reviewers:['sk-'+('x'.repeat(25))]}])assert.equal(validWfV1Args('wf_review_requests_'+op,{repo,number:3,expected_head_sha:'a'.repeat(40),...v}),false);
});
test('review context exact inputs, receipts and correlation',()=>{
 const sha='a'.repeat(40), author={id:3,login:'bot',type:'Bot'},comment={id:7,commit_id:sha,author,body:'untrusted',body_withheld:false};
 const args={repo,sha,comment_id:7};
 assert.ok(validWfV1Args('wf_commit_comment_get',args));
 assert.ok(!validWfV1Args('wf_commit_comment_get',{...args,url:'https://invalid'}));
 assert.ok(validWfV1Response('wf_commit_comment_get',{comment},args));
 assert.ok(!validWfV1Response('wf_commit_comment_get',{comment:{...comment,id:8}},args));
 assert.ok(!validWfV1Response('wf_commit_comment_get',{comment:{...comment,commit_id:'b'.repeat(40)}},args));
 const page={sha,comments:[comment],page:2,has_more:true,snapshot:false},pa={repo,sha,page:2,per_page:1};
 assert.ok(validWfV1Response('wf_commit_comments_page',page,pa));
 for(const bad of [{...page,page:1},{...page,has_more:false},{...page,comments:[comment,comment]},{...page,snapshot:true}])assert.ok(!validWfV1Response('wf_commit_comments_page',bad,pa));
 const ra={repo,number:9,expected_head_sha:sha},rr={number:9,head_sha:sha,users:[author],teams:[{id:4,slug:'devs'}],snapshot:false};
 assert.ok(validWfV1Response('wf_pr_requested_reviewers',rr,ra));
 for(const bad of [{...rr,number:8},{...rr,head_sha:'b'.repeat(40)},{...rr,users:[author,author]},{...rr,teams:[{id:4,slug:'devs',name:'extra'}]}])assert.ok(!validWfV1Response('wf_pr_requested_reviewers',bad,ra));
 // Runtime-assembled synthetic scanner shapes only, never credentials.
 assert.ok(!validWfV1Response('wf_commit_comment_get',{comment:{...comment,body:'sk-'+('x'.repeat(25))}},args));
 assert.ok(validWfV1Response('wf_commit_comment_get',{comment:{...comment,body:'<withheld: credential-shaped text>',body_withheld:true}},args));
 assert.ok(WF_V1_READ_ACTIONS.includes('wf_pr_requested_reviewers'));
});
