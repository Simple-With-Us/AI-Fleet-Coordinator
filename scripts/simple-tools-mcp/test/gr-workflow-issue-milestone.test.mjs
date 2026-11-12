import test from 'node:test';
import assert from 'node:assert/strict';
import {validWfV1Response,validWfV1Args} from '../src/gr-workflow-v1.js';
import {HostedTools} from '../src/hosted-tools.js';
import {GrRelayClient} from '../src/gr-tools.js';
import {loadConfig} from '../src/config.js';
import {testEnv} from './helpers.mjs';
import * as S from '../src/seat-state.js';

const repo='example/repo';
function harness(fetchImpl) {
  const store=S.memoryStore(); const gate={store};
  for (const name of ['reserve','noteRateLimited','idemBegin','idemSet','idemDrop','idemPeek','auditCall','roleSet']) gate[name]=options=>S[name](store,options,Date.now());
  gate.roleGet=()=>S.roleGet(store,Date.now()); gate.callTail=n=>S.callTail(store,n);
  const gr=new GrRelayClient({baseUrl:'https://broker.invalid',accessId:'test-id',accessSecret:'test-value',bearer:'own-bearer',fetch:fetchImpl});
  return new HostedTools({seat:'JET-CHAT',scopes:['zulip:read','zulip:write'],clientId:'test',grantRef:'abc123',paused:false,config:{...loadConfig(testEnv()),grWorkflowEnabled:true},zulip:null,key:'',gate,gr,log:()=>{},sleep:async()=>{}});
}

test('issue milestone set/remove strict nullable scalar and exact receipt',async()=>{
 for(const n of [3,0]){
  const args={repo,number:2,milestone_number:n},receipt={issue_number:2,milestone_number:n===0?null:n};
  assert.equal(validWfV1Args('wf_issue_milestone_set',args),true);assert.equal(validWfV1Response('wf_issue_milestone_set',receipt,args),true);
  assert.equal(validWfV1Response('wf_issue_milestone_set',{...receipt,issue_number:4},args),false);
  const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_issue_milestone_set'));assert.equal(i.headers.Authorization,'Bearer own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_issue_milestone_set',args)).isError);
 }
 for(const n of [true,-1,'3',null])assert.equal(validWfV1Args('wf_issue_milestone_set',{repo,number:2,milestone_number:n}),false);
 assert.equal(validWfV1Args('wf_issue_milestone_set',{repo,number:2}),false);
});
