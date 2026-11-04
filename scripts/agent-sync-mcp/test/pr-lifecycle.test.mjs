import test from 'node:test';
import assert from 'node:assert/strict';
import {GrRelayClient, GR_TOOLS} from '../src/gr-tools.js';
const args = {repo:'example/repo',number:3,state:'closed',expected_head_sha:'a'.repeat(40),expected_updated_at:'2026-11-04T06:00:00Z'};
const result = {number:3,url:'https://github.com/example/repo/pull/3',state:'closed',draft:false,head_sha:'a'.repeat(40),updated_at:'2026-11-04T06:00:01Z',noop:false,verified:true};
const client = body => new GrRelayClient({baseUrl:'https://relay.test',accessId:'id-fixture',accessSecret:'secret-fixture',bearer:'bearer-fixture',fetch:async()=>new Response(JSON.stringify(body),{status:200})});
test('state and atomic draft have strict discoverable input fields',()=>{
 const edit=GR_TOOLS.find(t=>t.name==='gr_edit_pr');
 assert.equal(edit.inputSchema.additionalProperties,false);
 assert.deepEqual(edit.inputSchema.properties.state.enum,['open','closed']);
 assert.ok(edit.inputSchema.anyOf.some(x=>x.required.includes('expected_head_sha')));
 assert.equal(GR_TOOLS.find(t=>t.name==='gr_open_pr').inputSchema.properties.draft.type,'boolean');
});
test('verified state result validated against request',async()=>{
 assert.equal((await client(result).call('edit_pr',args)).status,200);
 for(const patch of [{number:4},{draft:null},{verified:false},{state:'open'},{head_sha:'b'.repeat(40)},{url:'https://evil.test/x'},{extra:'private'}]) {
  const out=await client({...result,...patch}).call('edit_pr',args);
  assert.equal(out.status,502);assert.equal(out.body.outcome,'unknown');
 }
});
test('create draft mismatched/missing receipt never becomes success',async()=>{
 const a={repo:'example/repo',head:'f',base:'main',title:'x',body:'y',draft:true};
 assert.equal((await client({...result,state:'open',draft:true}).call('open_pr',a)).status,200);
 for(const body of [{number:3,url:result.url},{...result,state:'open',draft:false}])assert.equal((await client(body).call('open_pr',a)).status,502);
});
