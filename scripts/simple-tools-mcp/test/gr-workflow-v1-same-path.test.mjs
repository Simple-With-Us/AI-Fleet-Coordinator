import test from 'node:test';
import assert from 'node:assert/strict';
import {WF_V1_TOOLS,validWfV1Args} from '../src/gr-workflow-v1.js';
const repo='example/repo';
const base_tree='a'.repeat(40);
const source_commit='b'.repeat(40);
const sha='c'.repeat(40);
const entry={path:'dir/file.txt',mode:'100644',sha,source_commit};
const args=files=>({repo,base_tree,files});

test('same-path source proof is an exact immutable commit field, not a source-path or approval selector',()=>{
  const schema=WF_V1_TOOLS.find(t=>t.name==='gr_wf_git_tree_create').inputSchema.properties.files.items;
  assert.equal(schema.additionalProperties,false);
  assert.equal(schema.properties.source_commit.pattern,'^[0-9a-f]{40}$');
  assert.ok(validWfV1Args('wf_git_tree_create',args([entry])));
  for(const change of [{source_commit:'main'},{source_commit:null},{source_path:'different.txt'},{approved:true},{mode:'120000'}]){
    assert.equal(validWfV1Args('wf_git_tree_create',args([{...entry,...change}])),false);
  }
});

test('new inline and ordinary sha semantics remain separate and ambiguous entries fail closed',()=>{
  assert.ok(validWfV1Args('wf_git_tree_create',args([{path:'file',mode:'100644',sha}])));
  assert.ok(validWfV1Args('wf_git_tree_create',args([{path:'file',mode:'100644',text:'safe'}])));
  assert.equal(validWfV1Args('wf_git_tree_create',args([{...entry,text:'safe'}])),false);
  assert.equal(validWfV1Args('wf_git_tree_create',args([{path:'file',mode:'100644',source_commit,text:'safe'}])),false);
});

test('source references do not consume inline transport budget or waive its cap',()=>{
  const file={path:'inline',mode:'100644',text:'x'.repeat(40000)};
  assert.ok(validWfV1Args('wf_git_tree_create',args([entry,file])));
  assert.equal(validWfV1Args('wf_git_tree_create',args([entry,{...file,text:file.text+'x'}])),false);
  assert.ok(validWfV1Args('wf_git_tree_create',{...args([entry,{...file,text:'x'.repeat(100000)}]),allow_large_text:true}));
  assert.equal(validWfV1Args('wf_git_tree_create',args(Array.from({length:21},(_,n)=>({...entry,path:`file-${n}`})))),false);
});
