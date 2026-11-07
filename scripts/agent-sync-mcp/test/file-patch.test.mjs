import test from 'node:test';
import assert from 'node:assert/strict';
import { validWfV1Args, validWfV1Response, WF_V1_TOOLS } from '../src/gr-workflow-v1.js';
import { scanSecret } from '../src/contract.js';
const args={repo:'example/repo',branch:'feature',path:'server/index.ts',message:'fix',expected_head_sha:'a'.repeat(40),base_blob_sha:'b'.repeat(40),edits:[{start:0,end:0,text:'// safe\n'}]};
test('patch schema is closed, bounded, pinned, and registered',()=>{
 assert.ok(WF_V1_TOOLS.some(x=>x.name==='gr_wf_file_patch'));
 assert.ok(validWfV1Args('wf_file_patch',args));
 for(const override of [{base_blob_sha:undefined},{expected_head_sha:'bad'},{extra:true},{edits:[{start:-1,end:1,text:'x'}]},{edits:[{start:0,end:1,text:'x',baseline:true}]},{edits:Array(33).fill(args.edits[0])}])assert.equal(validWfV1Args('wf_file_patch',{...args,...override}),false);
});
test('patch response is bound to requested branch and path',()=>{
 const response={branch:args.branch,path:args.path,commit:'c'.repeat(40)};
 assert.ok(validWfV1Response('wf_file_patch',response,args));
 for(const change of [{path:'elsewhere'},{branch:'other'},{commit:'bad'},{secret:'x'}])assert.equal(validWfV1Response('wf_file_patch',{...response,...change},args),false);
});
test('inserted assignment and dedicated tokens still hit normal Worker scanner',()=>{
 for(const text of ['token = '+'a'.repeat(24),'gh'+'p_'+'a'.repeat(40)])assert.ok(scanSecret(text));
});
