import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto, randomBytes} from 'node:crypto';
import {nativeOriginContext,resolveNativeOriginClaims} from '../src/native-origin-context.js';
import {nativeOriginResolver} from '../src/native-origin-client.js';
globalThis.crypto ??= webcrypto;
const sid='sess_7e170443-7fad-4343-acfe-95a4c34377be';
const child='sess_019a1234-abcd-7123-8123-0123456789ab';
const envelope=(session=sid)=>({'com.zcode/request-context':{session_id:session,turn_id:'turn-fixture',runtime_scope:'main'}});
const claims={origin_session_claim:sid,origin_letter_claim:'II',origin_generation_claim:2};
test('ZC uses actual per-call session, not root or process workspace',async()=>{
 const meta={...envelope(child),root_session_id:sid,session_id:child};
 const ctx=nativeOriginContext(meta); assert.equal(ctx.session,child); assert.equal(ctx.platform,'zcode');assert.ok(Object.isFrozen(ctx));
 let requested;
 const result=await resolveNativeOriginClaims('ZC',ctx,async req=>{requested=req;return {...claims,origin_session_claim:child}});
 assert.equal(result.state,'resolved');assert.deepEqual(requested,{seat:'ZC',session:child});
 assert.equal((await resolveNativeOriginClaims('CODEX',ctx,()=>{throw Error('cross-seat')})).reason,'no-exact-thread');
});
test('missing, malformed, mixed and conflicting ZC metadata cannot become exact',()=>{
 assert.equal(nativeOriginContext({session_id:sid}).state,'missing');
 for(const c of [null,[],{}, {session_id:sid}, {session_id:sid,turn_id:'turn',runtime_scope:''}, {session_id:123,turn_id:'turn',runtime_scope:'main'}]) assert.equal(nativeOriginContext({'com.zcode/request-context':c}).state,'invalid');
 for(const over of [{session_id:child},{turn_id:'other'},{threadId:sid},{'x-codex-turn-metadata':{}}]) assert.equal(nativeOriginContext({...envelope(),...over}).state,'conflict');
});
test('ZC resolver uses separate fixed endpoint and credential, exact session response only',async()=>{
 const env={REVIEW_ORIGIN_ZC_READ_URL:'https://operator.example/registry/v1/review-origin-zcode',REVIEW_ORIGIN_ZC_READ_KEY:randomBytes(32).toString('hex')};
 let count=0;
 const resolver=nativeOriginResolver(env,{seat:'ZC',fetcher:async(url,init)=>{count++;assert.equal(new URL(url).pathname,'/registry/v1/review-origin-zcode');assert.equal(new URL(url).searchParams.get('native_session_id'),sid);assert.equal(init.redirect,'manual');return Response.json(claims)}});
 assert.deepEqual(await resolver({seat:'ZC',session:sid}),claims);
 await assert.rejects(resolver({seat:'CODEX',session:sid}));assert.equal(count,1);
 assert.equal(nativeOriginResolver(env),null);
 assert.equal(nativeOriginResolver({...env,REVIEW_ORIGIN_ZC_READ_URL:env.REVIEW_ORIGIN_ZC_READ_URL.replace('-zcode','')},{seat:'ZC'}),null);
});
