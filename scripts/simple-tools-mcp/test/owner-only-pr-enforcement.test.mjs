import test from 'node:test';
import assert from 'node:assert/strict';
import {GrRelayClient} from '../src/gr-tools.js';
import {HOSTED_TOOL_LIST, HostedTools} from '../src/hosted-tools.js';
test('legacy PR create absent from catalog and blocked in direct hosted handler',async()=>{
 assert.ok(!HOSTED_TOOL_LIST.some(t=>t.name==='gr_wf_pr_create'));
 assert.ok(HOSTED_TOOL_LIST.some(t=>t.name==='gr_open_pr'));
 let sent=0;
 await assert.rejects(HostedTools.prototype.tool_gr_wf_pr_create.call({grCall:()=>sent++},{}),/gr_open_pr/);
 assert.equal(sent,0);
});
test('legacy transport refuses before credential fetch or relay network',async()=>{
 let sent=0;
 const client=new GrRelayClient({baseUrl:'https://broker.invalid',bearer:'opaque',fetch:async()=>{sent++;throw Error('unexpected');}});
 const result=await client.callWorkflow('wf_pr_create',{});
 assert.equal(result.status,403);
 assert.equal(result.body.error,'owner_only_pr_creation');
 assert.equal(sent,0);
});
