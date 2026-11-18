import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {adminPage} from '../src/pages.js';
const src=readFileSync(new URL('../src/index.js',import.meta.url),'utf8');
const base={email:'owner',clients:[],refusals:[],audits:[],csrf:'test',generatedAt:Date.now(),seats:[{seat:'CODEX',grants:[],epoch:0,keyInstalled:true,role:null,unavailableFields:[]}]};
test('default inventory is explicitly deferred, not absent or failed',async()=>{
 const html=await adminPage({...base,clientsDeferred:true,seats:[{...base.seats[0],grantsDeferred:true}]}).text();
 assert.match(html,/Not checked \(load on request\)/);assert.match(html,/Grant not checked/);assert.match(html,/inspect_grants=CODEX/);assert.match(html,/inspect_clients=1/);
 assert.ok(!html.includes('Needs setup: grant'));assert.ok(!html.includes('No hand-registered clients.'));
});
test('attempted failure stays unknown and successful inventory has timestamp',async()=>{
 const failed=await adminPage({...base,clientsUnavailable:true,seats:[{...base.seats[0],unavailableFields:['grants']}]}).text();
 assert.match(failed,/Unknown \(diagnostic read failed\)/);
 const good=await adminPage({...base,clientsCheckedAt:Date.now(),seats:[{...base.seats[0],grantsCheckedAt:Date.now()}]}).text();
 assert.match(good,/snapshot only/);assert.match(good,/No hand-registered clients/);
});
test('default admin listing makes zero inventory requests; one seat opt-in cannot fan out',()=>{
 const body=src.slice(src.indexOf('async function serveAdmin'),src.indexOf('const clients =',src.indexOf('async function serveAdmin'))>0?src.indexOf('const clients =',src.indexOf('async function serveAdmin')):src.indexOf('const logGate',src.indexOf('async function serveAdmin')));
 assert.match(body,/seat === inspectGrantSeat \? listAllGrants\(oauth, seat\) : Promise.resolve\(null\)/);
 assert.match(body,/if \(inspectClients\) \{\s+try \{\s+const result = await oauth.listClients\(\)/);
});
test('authorization and revocation checks remain byte-identical to source base',()=>{
 const before=readFileSync(new URL('./fixtures/admin-lazy-index-base.txt',import.meta.url),'utf8');
 for(const [start,end] of [['async function authorizeGet','/** Parse a form'],['      case "revoke": {','      case "create_manual_client": {']]){
  assert.equal(src.slice(src.indexOf(start),src.indexOf(end,src.indexOf(start))),before.slice(before.indexOf(start),before.indexOf(end,before.indexOf(start))));
 }
});

test('deferred grants never hide independent role refusal or recent failure',async()=>{
 const role=await adminPage({...base,seats:[{...base.seats[0],grantsDeferred:true,role:{ok:false,reason:'not_member'}}]}).text();
 assert.match(role,/Role refused; grant not checked/);
 const failed=await adminPage({...base,statusCalls:[{seat:'CODEX',ts:Date.now(),tool:'whoami',outcome:'error',error_code:'unavailable'}],seats:[{...base.seats[0],grantsDeferred:true}]}).text();
 assert.match(failed,/Recent failure; grant not checked/);
});
