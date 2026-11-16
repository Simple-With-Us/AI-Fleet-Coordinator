import test from 'node:test';
import assert from 'node:assert/strict';
import {WF_V1_READ_ACTIONS,validWfV1Response,validWfV1Args} from '../src/gr-workflow-v1.js';
import {HostedTools, HOSTED_READ_TOOLS, HOSTED_WRITE_TOOLS} from '../src/hosted-tools.js';
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

test('generated release-note preview is strict scanned nonmutating despite required write permission',async()=>{
 const args={repo,tag:'v2',target_sha:'a'.repeat(40),previous_tag:'v1'},receipt={preview:{name:'Release2',body:'Untrusted changes'},saved:false};
 assert.equal(WF_V1_READ_ACTIONS.includes('wf_release_notes_preview'),false);assert.equal(HOSTED_WRITE_TOOLS.has('gr_wf_release_notes_preview'),true);assert.equal(HOSTED_READ_TOOLS.has('gr_wf_release_notes_preview'),false);assert.equal(validWfV1Response('wf_release_notes_preview',receipt),true);
 // Synthetic scanner fixture assembled at runtime, never a credential.
 for(const bad of [{...receipt,saved:true},{preview:{...receipt.preview,body:'sk-proj-'+'x'.repeat(24)},saved:false},{preview:{...receipt.preview,extra:true},saved:false}])assert.equal(validWfV1Response('wf_release_notes_preview',bad),false);
 assert.equal(validWfV1Args('wf_release_notes_preview',{...args,tag:'v1'}),false);
 const t=harness(async(u,i)=>{assert.ok(u.endsWith('/v1/wf_release_notes_preview'));assert.equal(i.headers.Authorization,'Bearer own-bearer');assert.deepEqual(JSON.parse(i.body),args);return new Response(JSON.stringify(receipt));});assert.ok(!(await t.call('gr_wf_release_notes_preview',args)).isError);
 const down=harness(async()=>{throw Error('lost preview');});const result=await down.call('gr_wf_release_notes_preview',args);assert.ok(result.isError);assert.match(JSON.stringify(result),/unknown/i);
});
