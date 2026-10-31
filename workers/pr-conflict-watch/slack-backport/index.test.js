import test from 'node:test';
import assert from 'node:assert/strict';
import worker from './index.js';
import original from './original-fixture.js';
async function signed(payload,event='pull_request') {
 const body=JSON.stringify(payload),key=await crypto.subtle.importKey('raw',new TextEncoder().encode('secret'),{name:'HMAC',hash:'SHA-256'},false,['sign']);
 const digest=Array.from(new Uint8Array(await crypto.subtle.sign('HMAC',key,new TextEncoder().encode(body))),b=>b.toString(16).padStart(2,'0')).join('');
 return new Request('https://test/any-existing-path',{method:'POST',headers:{'x-hub-signature-256':'sha256='+digest,'x-github-event':event},body});
}
async function run(target,payload,event='pull_request',state='dirty',previous=null) {
 const real=globalThis.fetch, calls=[],saved=new Map(previous?[['state:a/b#1',previous]]:[]),pending=[];
 const env={GITHUB_WEBHOOK_SECRET:'secret',GITHUB_TOKEN:'github',SLACK_BOT_TOKEN:'slack',PR_STATE:{async get(k){return saved.get(k)||null;},async put(k,v){saved.set(k,v);},async delete(k){saved.delete(k);}}};
 globalThis.fetch=async(url,init)=>{calls.push({url,body:init?.body});return new Response(JSON.stringify(String(url).includes('api.github.com')?{state:state==='closed'?'closed':'open',mergeable_state:state,title:'PR',html_url:'https://test/pr'}:{ok:true}));};
 try {const res=await target.fetch(await signed(payload,event),env,{waitUntil:p=>pending.push(p)});await Promise.all(pending);return {status:res.status,body:await res.text(),calls,saved:[...saved]};}finally{globalThis.fetch=real;}
}
test('matches deployed Slack outputs and state changes for valid signed events',async()=>{
 for (const [state,previous] of [['dirty',null],['dirty','dirty'],['clean','dirty'],['closed','dirty']]) {
  const payload={repository:{full_name:'a/b'},pull_request:{number:1}};
  assert.deepEqual(await run(worker,payload,'pull_request',state,previous),await run(original,payload,'pull_request',state,previous));
 }
});
test('malformed authentication is rejected without any external work',async()=>{
 for (const headers of [{},{'x-hub-signature-256':'nope'}]) {
  assert.equal((await worker.fetch(new Request('https://test/',{method:'POST',headers}),{},{})).status,401);
 }
});
test('signed malformed payloads return 400 without downstream calls',async()=>{
 for (const p of [null,[],{},{repository:{full_name:'a/b'},pull_request:{number:0}}]) {
  const r=await run(worker,p);assert.equal(r.status,400);assert.equal(r.calls.length,0);
 }
});
test('paths and health stay compatible',async()=>{
 assert.equal((await worker.fetch(new Request('https://test/legacy-path'),{},{})).status,200);
 assert.equal((await run(worker,{repository:{full_name:'a/b'},pull_request:{number:1}})).status,200);
});

test('duplicate candidates produce one lookup per PR',async()=>{
 const r=await run(worker,{repository:{full_name:'a/b'},check_suite:{pull_requests:[{number:1},{number:1}]}},'check_suite');
 assert.equal(r.status,200);assert.equal(r.calls.filter(c=>String(c.url).includes('api.github.com')).length,1);
});

test('malformed GitHub replies cannot change state or notify Slack',async()=>{
 const real=globalThis.fetch;let writes=0;
 globalThis.fetch=async(url,init)=>{assert.equal(init.redirect,'manual');return new Response(JSON.stringify({state:'unexpected'}));};
 try {
  const pending=[],env={GITHUB_WEBHOOK_SECRET:'secret',GITHUB_TOKEN:'g',PR_STATE:{async get(){throw Error('must not read state');},async put(){writes++;}}};
  await worker.fetch(await signed({repository:{full_name:'a/b'},pull_request:{number:1}}),env,{waitUntil:p=>pending.push(p)});
  await Promise.all(pending);assert.equal(writes,0);
 }finally{globalThis.fetch=real;}
});
