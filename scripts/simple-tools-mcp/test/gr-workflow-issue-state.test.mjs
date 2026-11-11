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

test('explicit issue close reason and reopen receipts remain exact',async()=>{
 const args={repo,number:2,state:'closed',reason:'not_planned'},receipt={issue_number:2,state:'closed',reason:'not_planned'};
 assert.equal(validWfV1Args('wf_issue_state_set',args),true);assert.equal(validWfV1Args('wf_issue_state_set',{...args,state:'open'}),false);assert.equal(validWfV1Response('wf_issue_state_set',receipt,args),true);assert.equal(validWfV1Response('wf_issue_state_set',{...receipt,reason:'completed'},args),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_issue_state_set'));assert.equal(i.headers.Authorization,'Bearer own-bearer');return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_issue_state_set',args)).isError);
});
