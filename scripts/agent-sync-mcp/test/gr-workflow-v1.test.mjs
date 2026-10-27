import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {WF_V1_ACTIONS,WF_V1_TOOLS,validWfV1Args} from '../src/gr-workflow-v1.js';
const py=readFileSync(new URL('../../../git-relay/git_relay/workflow_verbs.py',import.meta.url),'utf8');
const specs=[...py.slice(py.indexOf('SPECS = {'),py.indexOf('ACTIONS = tuple')).matchAll(/"(wf_[a-z_]+)":/g)].map(m=>m[1]);
const onlyOf=name=>{
  const body=py.split(/\n    def /).find(b=>b.startsWith(name+'('));
  const m=body&&body.match(/_only\(args, \(([^)]*)\)\)/);
  return m?[...m[1].matchAll(/"([a-z0-9_]+)"/g)].map(x=>x[1]).sort():null;
};
test('names equal the relay SPECS, plus wf_job_log when it lands',()=>{
  const rel=specs.filter(a=>a!=='wf_job_log').sort();
  assert.deepEqual([...WF_V1_ACTIONS].sort(),rel);
  assert.equal(WF_V1_TOOLS.length,rel.length);
});
test('argument sets equal the relay _only() sets',()=>{
  for(const t of WF_V1_TOOLS){
    const action=t.name.slice(3);
    const want=['wf_run_rerun','wf_run_cancel','wf_run_approve'].includes(action)?['repo','run_id']:onlyOf(action);
    assert.ok(want,action);
    assert.deepEqual(Object.keys(t.inputSchema.properties).sort(),want,action);
    assert.equal(t.inputSchema.additionalProperties,false);
  }
});
test('writes carry a head fence where the relay has one',()=>{
  for(const a of ['wf_update_branch','wf_files_put','wf_merge_guarded']) assert.ok(WF_V1_TOOLS.find(t=>t.name==='gr_'+a).inputSchema.required.includes('expected_head_sha'));
});
const repo='Simple-With-Us/Socratic-Trade';
test('valid and invalid args',()=>{
  assert.ok(validWfV1Args('wf_merge_guarded',{repo,number:9,expected_head_sha:'a'.repeat(40),method:'squash'}));
  assert.ok(!validWfV1Args('wf_merge_guarded',{repo,number:9,expected_head_sha:'a'.repeat(40),url:'https://x.invalid'}));
  assert.ok(!validWfV1Args('wf_merge_guarded',{repo,number:9}));
  assert.ok(!validWfV1Args('wf_merge_guarded',{repo,number:9,expected_head_sha:'zz'}));
  assert.ok(!validWfV1Args('mint_token',{repo}));
  assert.ok(!validWfV1Args('wf_run_rerun',{repo:'a b/c',run_id:1}));
  assert.ok(validWfV1Args('wf_files_put',{repo,branch:'b',message:'m',expected_head_sha:'a'.repeat(40),files:[{path:'a.txt',content_b64:'QQ=='}]}));
  assert.ok(!validWfV1Args('wf_files_put',{repo,branch:'b',message:'m',expected_head_sha:'a'.repeat(40),files:[]}));
});
test('module is not registered in hosted tools',()=>{
  const hosted=readFileSync(new URL('../src/hosted-tools.js',import.meta.url),'utf8');
  assert.ok(!hosted.includes('gr-workflow-v1'));
});
