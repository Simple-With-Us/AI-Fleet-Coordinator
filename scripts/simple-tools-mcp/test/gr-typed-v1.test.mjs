import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {TYPED_V1_ACTIONS,TYPED_V1_TOOLS,validTypedV1Args,callTypedV1} from '../src/gr-typed-v1.js';
const args={repo:'Simple-With-Us/Socratic-Trade',number:9,expected_head:'a'.repeat(40)};
const receipt={repo:args.repo,number:9,head:args.expected_head,pr_id:'PR_abc',state:'OPEN',auto_merge_enabled:false,
  labels:['hold/do-not-merge'],label_events:[],complete:false,hold_decision:'not_evaluated',principal:'swu-wise[bot]',
  outcome:'confirmed_disabled',write_attempted:true,atomic_head_guard:false};
const opts={action:'pr_automerge_disable',args,baseUrl:'https://broker.invalid',bearer:'opaque-client-bearer',
  accessId:'private-access-id',accessSecret:'private-access-secret',expectedPrincipal:'swu-wise[bot]'};
const reply=body=>async()=>new Response(JSON.stringify(body));
test('three bounded versioned tools, no mint or arm',()=>{assert.equal(TYPED_V1_TOOLS.length,3);assert.ok(!TYPED_V1_ACTIONS.includes('mint_token'));assert.ok(!TYPED_V1_ACTIONS.includes('pr_automerge_enable'));});
test('reject proxy args',()=>assert.equal(validTypedV1Args({...args,url:'https://evil.invalid'}),false));
test('valid disarm uses fixed path caller bearer server Access no redirects',async()=>{
  const result=await callTypedV1({...opts,fetchImpl:async(url,init)=>{
    assert.equal(url,'https://broker.invalid/v1/pr_automerge_disable');assert.equal(init.redirect,'manual');
    assert.equal(init.headers.Authorization,'Bearer '+opts.bearer);assert.equal(init.headers['CF-Access-Client-Secret'],opts.accessSecret);
    return new Response(JSON.stringify(receipt));}});
  assert.equal(result.status,200);assert.equal(result.body.hold_decision,'not_evaluated');assert.equal(result.body.atomic_head_guard,false);
});
for (const [name,body] of [['wrongPrincipal',{...receipt,principal:'swu-echo[bot]'}],['headDrift',{...receipt,head:'b'.repeat(40)}],
 ['armed',{...receipt,auto_merge_enabled:true}],['atomicLie',{...receipt,atomic_head_guard:true}],['extraToken',{...receipt,token:'secret'}],
 ['reflectedSecret',{...receipt,labels:[opts.accessSecret]}]]) test(name+' fails closed',async()=>assert.equal((await callTypedV1({...opts,fetchImpl:reply(body)})).status,502));
test('oversize streamed response capped',async()=>assert.equal((await callTypedV1({...opts,fetchImpl:async()=>new Response(' '.repeat(65537))})).status,502));
test('upstream exception sanitized',async()=>{const r=await callTypedV1({...opts,fetchImpl:async()=>{throw Error(opts.accessSecret)}});assert.equal(r.body.outcome,'unknown');assert.ok(!JSON.stringify(r).includes(opts.accessSecret));});
test('existing seven schema preserved and typed dispatch registered',()=>{
 const original=readFileSync(new URL('../src/gr-tools.js',import.meta.url),'utf8');assert.ok(original.includes('outcome: z.literal("unknown")'));
 const hosted=readFileSync(new URL('../src/hosted-tools.js',import.meta.url),'utf8');assert.ok(hosted.includes('gr-typed-v1'));
});


import {HostedTools} from '../src/hosted-tools.js';
import {GrRelayClient} from '../src/gr-tools.js';
import {toolListFor} from '../src/delegated-identities.js';
import {loadConfig} from '../src/config.js';
import {testEnv} from './helpers.mjs';
import * as S from '../src/seat-state.js';
function harness(fetchImpl, enabled=true, principals={'JET-CHAT':'swu-wise[bot]'}) {
  const store=S.memoryStore(); const gate={store};
  for (const name of ['reserve','noteRateLimited','idemBegin','idemSet','idemDrop','idemPeek','auditCall','roleSet']) gate[name]=options=>S[name](store,options,Date.now());
  gate.roleGet=()=>S.roleGet(store,Date.now());
  return new HostedTools({seat:'JET-CHAT',scopes:['zulip:read','zulip:write'],clientId:'test',grantRef:'abc123',paused:false,
    config:{...loadConfig(testEnv()),grTypedEnabled:enabled,grTypedPrincipals:principals},zulip:null,key:'',gate,log:()=>{},sleep:async()=>{},
    gr:new GrRelayClient({baseUrl:opts.baseUrl,accessId:opts.accessId,accessSecret:opts.accessSecret,bearer:opts.bearer,fetch:fetchImpl})});
}
test('typed discovery defaults off, on/off caches isolated, direct gate and missing principal refuse',async()=>{
  assert.equal(loadConfig(testEnv()).grTypedEnabled,false);
  assert.ok(!toolListFor('JET-CHAT',{}).list.some(t=>t.name.startsWith('gr_v1_pr_')));
  assert.equal(toolListFor('JET-CHAT',{grTypedEnabled:true}).list.filter(t=>t.name.startsWith('gr_v1_pr_')).length,3);
  assert.ok(!toolListFor('JET-CHAT',{}).list.some(t=>t.name.startsWith('gr_v1_pr_')));
  for(const [enabled,principals] of [[false,{'JET-CHAT':'swu-wise[bot]'}],[true,{}]]) {
    let count=0; const t=harness(async()=>{count++;throw Error('must not fetch');},enabled,principals);
    assert.ok((await t.call('gr_v1_pr_state',args)).isError);assert.equal(count,0);
  }
});
test('real hosted dispatch uses bounded typed transport and binds principal/head',async()=>{
  let count=0;
  const t=harness(async(url,init)=>{count++;assert.equal(url,'https://broker.invalid/v1/pr_automerge_disable');
    assert.deepEqual(JSON.parse(init.body),args);assert.equal(init.headers.Authorization,'Bearer '+opts.bearer);
    return new Response(JSON.stringify(receipt));});
  const result=await t.call('gr_v1_pr_automerge_disable',args);
  assert.ok(!result.isError,result.content?.[0]?.text);assert.equal(count,1);
  const wrong=harness(reply({...receipt,principal:'swu-echo[bot]'}));
  assert.ok((await wrong.call('gr_v1_pr_automerge_disable',args)).isError);
});
test('closed disarm receipt is rejected, retryable preflight stays safe and no retries occur',async()=>{
  assert.equal((await callTypedV1({...opts,fetchImpl:reply({...receipt,state:'CLOSED'})})).status,502);
  let count=0;const t=harness(async()=>{count++;return new Response(JSON.stringify({error:'preflight unavailable',retryable:true}),{status:503});});
  const r=await t.call('gr_v1_pr_automerge_disable',args);
  assert.ok(r.isError);assert.equal(count,1);assert.match(r.content[0].text,/Nothing was written/);
});

test('typed quota429 stays retryable through hosted dispatch',async()=>{
 const t=harness(async()=>new Response(JSON.stringify({error:'quota',retryable:true}),{status:429}));
 const r=await t.call('gr_v1_pr_state',args);
 assert.ok(r.isError);assert.match(r.content[0].text,/rate_limited/);
});
