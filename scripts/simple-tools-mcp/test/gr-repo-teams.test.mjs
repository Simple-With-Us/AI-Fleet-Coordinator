import test from 'node:test';
import assert from 'node:assert/strict';
import {WF_V1_TOOLS, WF_V1_READ_ACTIONS, validWfV1Args, validWfV1Response} from '../src/gr-workflow-v1.js';
const action='wf_repo_teams',repo='fixture/example';
const result={teams:[{id:1,slug:'fixture-team',name:'Fixture team',permission:'push',privacy:'closed'}],page:1,has_more:false};
test('repository teams is a typed bounded read with no caller scope expansion',()=>{
 assert.ok(WF_V1_READ_ACTIONS.includes(action));
 const tool=WF_V1_TOOLS.find(t=>t.name==='gr_'+action);
 assert.ok(tool);
 assert.ok(validWfV1Args(action,{repo,page:1,per_page:50}));
 for(const args of [{repo,page:0},{repo,page:true},{repo,per_page:51},{repo,url:'https://elsewhere.invalid'},{repo,team:'other'}]) assert.ok(!validWfV1Args(action,args));
});
test('repository teams receipt allows only the projected shape',()=>{
 assert.ok(validWfV1Response(action,result));
 for(const change of [{id:true},{slug:'bad/slug'},{name:'a'.repeat(257)},{name:'bad\nname'},{permission:'owner'},{privacy:'public'},{url:'https://elsewhere.invalid'}]) {
  const bad=structuredClone(result);Object.assign(bad.teams[0],change);assert.ok(!validWfV1Response(action,bad));
 }
 const huge=structuredClone(result);huge.teams=Array(51).fill(result.teams[0]);assert.ok(!validWfV1Response(action,huge));
});
