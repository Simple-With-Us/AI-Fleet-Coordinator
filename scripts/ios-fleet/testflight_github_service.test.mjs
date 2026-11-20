import test from 'node:test';
import assert from 'node:assert/strict';
import {generateKeyPairSync} from 'node:crypto';
import {makeServiceGitHub,APP_ID,INSTALLATION_ID} from './testflight_github_service.mjs';
import {makeDeps} from './ios_testflight_freshness.mjs';
const key=generateKeyPairSync('rsa',{modulusLength:2048}).privateKey;
function world(change={}) {
 const calls=[];const github=makeServiceGitHub({key:()=>key,fetchImpl:async(url,opts)=>{
  const route=new URL(url).pathname;calls.push({route,...opts});let result={ok:true},status=200;
  if(route==='/app')result={id:APP_ID,...change.app};
  else if(route===`/app/installations/${INSTALLATION_ID}`)result={app_id:APP_ID,account:{login:'Simple-With-Us'},repository_selection:'selected',suspended_at:null,...change.install};
  else if(route.endsWith('/access_tokens')){let b=JSON.parse(opts.body);result={token:'fixture',expires_at:new Date(Date.now()+60000).toISOString(),permissions:b.permissions,repositories:[{full_name:'Simple-With-Us/Example'}],...change.mint}}
  else if(route==='/installation/token')status=change.revokeFail?500:204;
  else if(change.dataFail)status=change.dataFail;
  else if(change.dispatchResponse)result=change.dispatchResponse;
  return {ok:status<400,status,json:async()=>result};
 }});return {github,calls};
}
test('missing key fails before network, never consults gh or environment',async()=>{
 let n=0;const f=makeServiceGitHub({key:()=>{throw Error('sensitive failure')},fetchImpl:()=>n++});
 await assert.rejects(()=>f('repos/Simple-With-Us/Example/commits/main'),/dedicated-testflight-service-auth-unavailable/);assert.equal(n,0);
});
test('authenticated read scoped to one repo, minimal read grant, revoked',async()=>{
 const w=world();await w.github('repos/Simple-With-Us/Example/compare/aaa...bbb');
 const m=w.calls.find(c=>c.route.endsWith('access_tokens'));assert.deepEqual(JSON.parse(m.body),{repositories:['Example'],permissions:{contents:'read',metadata:'read',checks:'read',actions_variables:'read',actions:'read'}});assert.equal(w.calls.at(-1).route,'/installation/token');assert.equal(w.calls.at(-1).method,'DELETE');
});
test('dispatch requests only actions write, revokes after execution',async()=>{
 const w=world();await w.github('repos/Simple-With-Us/Example/actions/workflows/ios-ship.yml/dispatches',{method:'POST',body:{ref:'main'}});
 assert.equal(JSON.parse(w.calls[2].body).permissions.actions,'write');assert.equal(w.calls.at(-1).method,'DELETE');
});
for(const [name,change] of Object.entries({wrongApp:{app:{id:1}},wrongInstallationApp:{install:{app_id:1}},wrongOwner:{install:{account:{login:'other'}}},allRepos:{install:{repository_selection:'all'}},suspended:{install:{suspended_at:'now'}},wrongRepo:{mint:{repositories:[{full_name:'Simple-With-Us/Other'}]}},broadPermission:{mint:{permissions:{contents:'write'}}},expired:{mint:{expires_at:'2000-01-01T00:00:00Z'}}}))test(name+' refuses data',async()=>{const w=world(change);await assert.rejects(()=>w.github('repos/Simple-With-Us/Example/commits/main'));assert.equal(w.calls.some(c=>c.route.startsWith('/repos/')),false)});
test('data failure still revokes without exposing upstream body',async()=>{const w=world({dataFail:503});await assert.rejects(()=>w.github('repos/Simple-With-Us/Example/commits/main'),/dedicated-testflight-service-auth-unavailable/);assert.equal(w.calls.at(-1).route,'/installation/token')});
test('revoke failure refuses success',async()=>{const w=world({revokeFail:true});await assert.rejects(()=>w.github('repos/Simple-With-Us/Example/commits/main'))});
test('foreign owner, traversal and unrelated writes refused before mint',async()=>{for(const route of ['repos/Other/Example/commits/main','repos/Simple-With-Us/Example/../Other']){const w=world();await assert.rejects(()=>w.github(route));assert.equal(w.calls.length,0)}const w=world();await assert.rejects(()=>w.github('repos/Simple-With-Us/Example/issues',{method:'POST'}));assert.equal(w.calls.length,0)});
test('consumer read and dispatch use dedicated adapter only',async()=>{const calls=[];const deps=makeDeps({GH_TOKEN:'synthetic-forbidden-agent-token'},{serviceGitHub:async(...a)=>{calls.push(a);return {sha:'test'}}});assert.deepEqual(await deps.gh('repos/Simple-With-Us/Example/commits/main'),{sha:'test'});await deps.ghDispatch('Simple-With-Us/Example','ios-ship.yml','main',{});assert.equal(calls.length,2);await assert.rejects(()=>deps.ghDispatch('Simple-With-Us/Example','ios-ship.yml','other',{}));assert.equal(calls.length,2)});

test('accepted dispatch survives revoke failure without throwing', async()=>{ const w=world({revokeFail:true});assert.deepEqual(await w.github('repos/Simple-With-Us/Example/actions/workflows/ios-ship.yml/dispatches',{method:'POST',body:{ref:'main'}}),{dispatchOutcome:'accepted',revocationFailed:true}); });
test('failed dispatch response is unknown and retains cleanup warning', async()=>{ const w=world({dataFail:503,revokeFail:true});await assert.rejects(()=>w.github('repos/Simple-With-Us/Example/actions/workflows/ios-ship.yml/dispatches',{method:'POST',body:{ref:'main'}}),e=>e.dispatchOutcome==='unknown'&&e.revocationFailed); });
test('auth refusal records not-attempted dispatch', async()=>{ const w=world({app:{id:0}});await assert.rejects(()=>w.github('repos/Simple-With-Us/Example/actions/workflows/ios-ship.yml/dispatches',{method:'POST',body:{ref:'main'}}),e=>e.dispatchOutcome==='not-attempted'); });
test('workflow primitive input values stringify like gh raw-field', async()=>{let got;const d=makeDeps({}, {serviceGitHub:async(...a)=>{got=a;return {dispatchOutcome:'accepted'}}});await d.ghDispatch('Simple-With-Us/Example','ios-ship.yml','main',{upload:true,count:3,label:'test'});assert.deepEqual(got[1].body.inputs,{upload:'true',count:'3',label:'test'});await assert.rejects(()=>d.ghDispatch('Simple-With-Us/Example','ios-ship.yml','main',{bad:{x:1}}));});

for(const status of [400,401,403,404,422])test(`dispatch HTTP ${status} is rejected not unknown`,async()=>{const w=world({dataFail:status});await assert.rejects(()=>w.github('repos/Simple-With-Us/Example/actions/workflows/ios-ship.yml/dispatches',{method:'POST',body:{ref:'main'}}),e=>e.dispatchOutcome==='rejected');});
test('current dispatch response retains exact run ID',async()=>{const w=world({dispatchResponse:{workflow_run_id:987}});const out=await w.github('repos/Simple-With-Us/Example/actions/workflows/ios-ship.yml/dispatches',{method:'POST',body:{ref:'main'}});assert.equal(out.runId,987);assert.equal(w.calls[0].headers['X-GitHub-Api-Version'],'2026-03-10');});
test('service deadline halts new API traffic',async()=>{let t=0,n=0;const g=makeServiceGitHub({key:()=>key,now:()=>t,budgetMs:100,fetchImpl:async()=>{n++;throw Error('unexpected')}});t=101;await assert.rejects(()=>g('repos/Simple-With-Us/Example/commits/main'));assert.equal(n,0);});
test('consumer deadline prevents dispatch network before POST',async()=>{let t=0,n=0;const d=makeDeps({}, {clock:()=>t,runtimeBudgetMs:100,serviceGitHub:async()=>n++});t=101;await assert.rejects(()=>d.ghDispatch('Simple-With-Us/Example','ios-ship.yml','main',{}),/deadline/);assert.equal(n,0);});

test('consumer preflight refusal marks definitely not-attempted',async()=>{let n=0;const d=makeDeps({}, {serviceGitHub:async()=>n++});await assert.rejects(()=>d.ghDispatch('Simple-With-Us/Example','ios-ship.yml','other',{}),e=>e.dispatchOutcome==='not-attempted');assert.equal(n,0);});

test('consumer explicitly requests dispatch run details',async()=>{let got;const d=makeDeps({}, {serviceGitHub:async(...a)=>{got=a}});await d.ghDispatch('Simple-With-Us/Example','ios-ship.yml','main',{});assert.equal(got[1].body.return_run_details,true);});
test('deadline before POST fetch is definitely not-attempted',async()=>{let t=0,posts=0;const g=makeServiceGitHub({key:()=>key,now:()=>t,budgetMs:100,fetchImpl:async(url,opts)=>{const r=new URL(url).pathname;let j={};if(r==='/app')j={id:APP_ID};else if(r.includes('/access_tokens')){j={token:'synthetic',expires_at:new Date(100000).toISOString(),permissions:JSON.parse(opts.body).permissions,repositories:[{full_name:'Simple-With-Us/Example'}]};t=101}else if(r.includes('/app/installations/'))j={app_id:APP_ID,account:{login:'Simple-With-Us'},repository_selection:'selected',suspended_at:null};if(r.endsWith('/dispatches'))posts++;return {ok:true,status:r==='/installation/token'?204:200,json:async()=>j}}});await assert.rejects(()=>g('repos/Simple-With-Us/Example/actions/workflows/ios-ship.yml/dispatches',{method:'POST',body:{ref:'main',return_run_details:true}}),e=>e.dispatchOutcome==='not-attempted');assert.equal(posts,0);});

test('run-scoped key resolution is once even across concurrent calls',async()=>{
 let keys=0,mints=0,revokes=0;
 const g=makeServiceGitHub({key:async()=>{keys++;return key},fetchImpl:async(url,opts)=>{
  const r=new URL(url).pathname;let j={ok:true};
  if(r==='/app')j={id:APP_ID};
  else if(r.endsWith('/access_tokens')){mints++;j={token:'synthetic',expires_at:new Date(Date.now()+60000).toISOString(),permissions:JSON.parse(opts.body).permissions,repositories:[{full_name:'Simple-With-Us/Example'}]}}
  else if(r.startsWith('/app/installations/'))j={app_id:APP_ID,account:{login:'Simple-With-Us'},repository_selection:'selected',suspended_at:null};
  if(r==='/installation/token')revokes++;
  return {ok:true,status:r==='/installation/token'?204:200,json:async()=>j};
 }});
 await Promise.all([g('repos/Simple-With-Us/Example/commits/main'),g('repos/Simple-With-Us/Example/commits/main')]);
 await g('repos/Simple-With-Us/Example/commits/main');assert.equal(keys,1);assert.equal(mints,3);assert.equal(revokes,3);
});
test('failed resolver promise is not retried in the same run and never emits upstream',async()=>{
 let keys=0,network=0;const g=makeServiceGitHub({key:async()=>{keys++;throw Error('synthetic-upstream-sensitive')},fetchImpl:()=>network++});
 for(let i=0;i<3;i++)await assert.rejects(()=>g('repos/Simple-With-Us/Example/commits/main'),e=>e.message==='dedicated-testflight-service-auth-unavailable');
 assert.equal(keys,1);assert.equal(network,0);
});
test('new run-scoped adapter resolves anew, not a process-global private key cache',async()=>{
 let keys=0;for(let i=0;i<2;i++){const g=makeServiceGitHub({key:()=>{keys++;throw Error('synthetic-refusal')}});await assert.rejects(()=>g('repos/Simple-With-Us/Example/commits/main'))}assert.equal(keys,2);
});
