import test from 'node:test';
import assert from 'node:assert/strict';
import worker from './index.js';
const env={GITHUB_WEBHOOK_SECRET:'secret',GITHUB_TOKEN:'github',ZULIP_SITE:'https://zulip.example.com',ZULIP_BOT_EMAIL:'alerts@example.com',ZULIP_BOT_API_KEY:'key'};
async function signed(payload,event='pull_request') {
 const body=JSON.stringify(payload),key=await crypto.subtle.importKey('raw',new TextEncoder().encode('secret'),{name:'HMAC',hash:'SHA-256'},false,['sign']);
 const digest=Array.from(new Uint8Array(await crypto.subtle.sign('HMAC',key,new TextEncoder().encode(body))),b=>b.toString(16).padStart(2,'0')).join('');
 return new Request('https://test/any-existing-path',{method:'POST',headers:{'x-hub-signature-256':'sha256='+digest,'x-github-event':event},body});
}
async function run({reply={result:'success',id:7},previous=null,state='dirty',payload={repository:{full_name:'a/b'},pull_request:{number:1}},event='pull_request'}={}) {
 const real=globalThis.fetch,calls=[],data=new Map(previous?[['state:a/b#1',previous]]:[]),pending=[];
 globalThis.fetch=async(url,init)=>{assert.equal(init.redirect,'manual');calls.push({url,body:init?.body});return new Response(JSON.stringify(String(url).includes('api.github.com')?{number:1,state:'open',mergeable_state:state,title:'PR',html_url:'https://test/pr',base:{ref:'main'},head:{ref:'wise/fix'},user:{login:'owner'}}:reply));};
 try {const res=await worker.fetch(await signed(payload,event),{...env,PR_STATE:{async get(k){return data.get(k)||null;},async put(k,v){data.set(k,v);},async delete(k){data.delete(k);}}},{waitUntil:p=>pending.push(p)});await Promise.all(pending);return {status:res.status,calls,data};}finally{globalThis.fetch=real;}
}
test('real signed conflict flow posts only to Zulip Alerts and stores existing state key',async()=>{
 const r=await run();assert.equal(r.status,200);assert.equal(r.calls.length,2);
 assert.equal(r.calls[1].url,'https://zulip.example.com/api/v1/messages');
 const body=new URLSearchParams(r.calls[1].body);assert.equal(body.get('to'),'Alerts');assert.equal(body.get('topic'),'pr-conflicts b');assert.match(body.get('content'),/merge conflict/);assert.match(body.get('content'),/#1 PR/);assert.match(body.get('content'),/@\*\*Wise\*\*/i);assert.ok(!body.get('content').includes('undefined'));
 assert.equal(r.data.get('state:a/b#1'),'dirty');assert.ok(!r.calls.some(x=>String(x.url).includes('slack')));
});
test('failed Zulip delivery never poisons dedupe or loses recovery',async()=>{
 for(const previous of [null,'dirty']){const r=await run({previous,state:previous?'clean':'dirty',reply:{result:'error',msg:'fixture'}});assert.equal(r.data.get('state:a/b#1')||null,previous);}
});
test('existing dirty state suppresses duplicate delivery; recovery clears only after success',async()=>{
 assert.equal((await run({previous:'dirty'})).calls.length,1);
 const r=await run({previous:'dirty',state:'clean'});assert.equal(r.calls.length,2);assert.equal(r.data.size,0);
});
test('malformed signature and payload rejected without remote work',async()=>{
 assert.equal((await worker.fetch(new Request('https://test/',{method:'POST'}),env,{})).status,401);
 for(const p of [null,[],{},{repository:{full_name:'a/b'},pull_request:{number:0}}]){const r=await run({payload:p});assert.equal(r.status,400);assert.equal(r.calls.length,0);}
});
test('health remains storage free and existing POST paths stay accepted',async()=>{
 assert.equal((await worker.fetch(new Request('https://test/legacy'),{},{})).status,200);
 assert.equal((await run()).status,200);
});
test('ping remains ignored and candidates deduplicated',async()=>{
 const ping=await run({payload:{zen:'fixture'},event:'ping'});assert.equal(ping.status,200);assert.equal(ping.calls.length,0);
 const r=await run({payload:{repository:{full_name:'a/b'},check_suite:{pull_requests:[{number:1},{number:1}]}},event:'check_suite'});assert.equal(r.calls.length,2);
});
