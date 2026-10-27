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
test('existing seven schema untouched and typed module unregistered',()=>{
 const original=readFileSync(new URL('../src/gr-tools.js',import.meta.url),'utf8');assert.ok(original.includes('outcome: z.literal("unknown")'));
 const hosted=readFileSync(new URL('../src/hosted-tools.js',import.meta.url),'utf8');assert.ok(!hosted.includes('gr-typed-v1'));
});
