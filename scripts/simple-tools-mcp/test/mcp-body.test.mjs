import test from 'node:test';
import assert from 'node:assert/strict';
import {boundedMcpRequest,MCP_FILE_BYTES,MCP_DEFAULT_BYTES,FILE_ARGS_BYTES} from '../src/mcp-body.js';
const req=body=>new Request('https://example.test/mcp',{method:'POST',body});
const call=(name='gr_wf_file_put',n=300000)=>({jsonrpc:'2.0',id:1,method:'tools/call',params:{name,arguments:{content_b64:'A'.repeat(n)}}});
test('large MCP allowance only for single existing file calls',async()=>{
 for(const name of ['gr_wf_file_put','gr_wf_files_put']) assert.ok(await boundedMcpRequest(req(JSON.stringify(call(name)))) instanceof Request);
 for(const name of ['gr_wf_runs','zulip_post','gr_wf_release_asset_upload_binary']) assert.equal((await boundedMcpRequest(req(JSON.stringify(call(name))))).status,413);
 for(const body of [JSON.stringify([call()]),JSON.stringify({...call(),extra:'x'}),'x'.repeat(300000)]) assert.equal((await boundedMcpRequest(req(body))).status,413);
});
test('raw, args, envelope and legacy exact boundaries',async()=>{
 assert.ok(await boundedMcpRequest(req('x'.repeat(MCP_DEFAULT_BYTES))) instanceof Request);
 assert.equal((await boundedMcpRequest(req('x'.repeat(MCP_DEFAULT_BYTES+1)))).status,413);
 assert.equal((await boundedMcpRequest(req('x'.repeat(MCP_FILE_BYTES+1)))).status,413);
 const body=call(); body.params.arguments.content_b64='A'.repeat(FILE_ARGS_BYTES-JSON.stringify({content_b64:''}).length);
 assert.ok(await boundedMcpRequest(req(JSON.stringify(body))) instanceof Request);
 body.params.arguments.content_b64+='A'; assert.equal((await boundedMcpRequest(req(JSON.stringify(body)))).status,413);
 const padded=JSON.stringify(call())+' '.repeat(65536); assert.equal((await boundedMcpRequest(req(padded))).status,413);
});
test('escaped duplicate name cannot admit a different tool',async()=>{
 const body=JSON.stringify(call()).replace('"name":"gr_wf_file_put"','"name":"gr_wf_file_put","na\\u006de":"zulip_post"');
 assert.equal((await boundedMcpRequest(req(body))).status,413);
});

test('large deeply nested arguments fail closed rather than throwing',async()=>{
 const prefix='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"gr_wf_file_put","arguments":';
 const body=prefix+'{"a":'.repeat(20000)+'"'+ 'A'.repeat(300000)+'"'+'}'.repeat(20000)+'}}';
 assert.equal((await boundedMcpRequest(req(body))).status,413);
});

test('reconstructed request preserves original request context',async()=>{
 const controller=new AbortController();
 const original=new Request('https://example.test/mcp',{method:'POST',body:'{}',signal:controller.signal,credentials:'include'});
 const rebuilt=await boundedMcpRequest(original);
 assert.equal(rebuilt.credentials,'include');
 controller.abort();
 assert.equal(rebuilt.signal.aborted,true);
 assert.equal(await rebuilt.text(),'{}');
});
