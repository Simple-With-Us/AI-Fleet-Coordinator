import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {WF_V1_ACTIONS,WF_V1_TOOLS,validWfV1Args} from '../src/gr-workflow-v1.js';
const py=readFileSync(new URL('../../../git-relay/git_relay/workflow_verbs.py',import.meta.url),'utf8');
const specs=[...py.slice(py.indexOf('SPECS = {'),py.indexOf('ACTIONS = tuple')).matchAll(/"(wf_[a-z_]+)":/g)].map(m=>m[1]);
const onlyOf=name=>{
  const body=py.split(/\n    def /).find(b=>b.startsWith(name+'('));
  const m=body&&body.match(/_only\(args, \(([^)]*)\)\)/);
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
  assert.equal(toolListFor('JET-CHAT',{grWorkflowEnabled:true}).list.filter(t=>t.name.startsWith('gr_wf_')).length,23);
  assert.ok(!toolListFor('JET-CHAT',{}).list.some(t=>t.name.startsWith('gr_wf_')));
  let calls=0;const t=harness(async()=>{calls++;throw Error('must not fetch');},false);
  const r=await t.call('gr_wf_runs',{repo});
  assert.ok(r.isError);assert.equal(calls,0);
});
test('all workflow tools have dispatch and correct read/write scope',()=>{
  for(const action of WF_V1_ACTIONS) {
    assert.equal(typeof HostedTools.prototype['tool_gr_'+action],'function');
    const read=['wf_release_assets','wf_release_asset_get','wf_pr_list','wf_pr_files','wf_file_get','wf_tree','wf_branches','wf_commits','wf_code_search','wf_runs','wf_run_jobs','wf_checks'].includes(action);
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
 assert.equal(toolListFor('JET-CHAT',flags).list.filter(t=>t.name.startsWith('gr_wf_')).length,23);
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
