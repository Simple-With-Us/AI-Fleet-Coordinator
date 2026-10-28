import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {WF_V1_ACTIONS,WF_V1_TOOLS,WF_V1_READ_ACTIONS,validWfV1Response,validWfV1Args} from '../src/gr-workflow-v1.js';
const py=readFileSync(new URL('../../../git-relay/git_relay/workflow_verbs.py',import.meta.url),'utf8');
const specs=[...py.slice(py.indexOf('SPECS = {'),py.indexOf('ACTIONS = tuple')).matchAll(/"(wf_[a-z_]+)":/g)].map(m=>m[1]);
const onlyOf=name=>{
  const body=py.split(/\n    def /).find(b=>b.startsWith(name+'('));
  const m=body&&body.match(/_only\(\s*args,\s*\(([^)]*)\),?\s*\)/);
  return m?[...m[1].matchAll(/"([a-z0-9_]+)"/g)].map(x=>x[1]).sort():null;
};
test('names equal the relay SPECS, plus wf_job_log when it lands',()=>{
  const rel=specs.filter(a=>a!=='wf_job_log').sort();
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
  assert.equal(toolListFor('JET-CHAT',{grWorkflowEnabled:true}).list.filter(t=>t.name.startsWith('gr_wf_')).length,62);
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
 assert.ok(!validWfV1Args('wf_file_put',{repo,path:'a',branch:'b',message:'m',content_b64:'A'.repeat(60001)}));
});

test('valid refusal context is stripped without losing status or retry semantics',async()=>{
 for(const status of [403,429,503]) {
  const gr=new GrRelayClient({baseUrl:'https://broker.invalid',bearer:'own',fetch:async()=>new Response(JSON.stringify({error:'private denial prose',retryable:true,context:{reason:'private-context'}}),{status})});
  assert.deepEqual(await gr.callWorkflow('wf_runs',{repo}),{status,body:{error:'workflow_request_refused',retryable:true}});
 }
});

test('typed and workflow discovery gates have independent cache keys',()=>{
 const flags={grWorkflowEnabled:true,grTypedEnabled:true};
 assert.equal(toolListFor('JET-CHAT',flags).list.filter(t=>t.name.startsWith('gr_wf_')).length,62);
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

test('branch and tag operations preserve fixed receipts and write UNKNOWN',async()=>{
 const cases=[['wf_branch_update',{branch:'feature/x',sha:'b'.repeat(40),expected_sha:'a'.repeat(40)},{ref:'refs/heads/feature/x',sha:'b'.repeat(40)}],['wf_branch_delete',{branch:'feature/x',expected_sha:'a'.repeat(40)},{deleted:true,ref:'refs/heads/feature/x'}],['wf_tag_create',{tag:'v1',sha:'a'.repeat(40)},{ref:'refs/tags/v1',sha:'a'.repeat(40)}],['wf_tag_get',{tag:'v1'},{ref:'refs/tags/v1',sha:'a'.repeat(40)}],['wf_tag_delete',{tag:'v1',expected_sha:'a'.repeat(40)},{deleted:true,ref:'refs/tags/v1'}]];
 for(const [action,args,reply] of cases){
  const t=harness(async(url,init)=>{assert.ok(url.endsWith('/v1/'+action));assert.equal(init.headers.Authorization,'Bearer caller-own-bearer');return new Response(JSON.stringify(reply));});
  assert.ok(!(await t.call('gr_'+action,{repo,...args})).isError,action);
  if(action!=='wf_tag_get'){const fail=harness(async()=>{throw Error('no reply');});const r=await fail.call('gr_'+action,{repo,...args});assert.ok(r.isError);assert.match(r.content[0].text,/unknown/i);}
 }
});

test('actual review regression',()=>{assert.ok(!validWfV1Response('wf_branch_update',{ref:'refs/tags/v1',sha:'a'.repeat(40)})); assert.ok(!validWfV1Response('wf_tag_delete',{deleted:true,ref:'refs/heads/main'}));});
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
