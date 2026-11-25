import test from 'node:test';
import assert from 'node:assert/strict';
import {WF_V1_TOOLS,WF_V1_READ_ACTIONS,validWfV1Args,validWfV1Response} from '../src/gr-workflow-v1.js';
const action='wf_repo_collaborators',repo='fixture/example';
const row={id:1,login:'fixture-user',type:'User',role_name:'custom reviewer',permissions:{pull:true,triage:false,push:false,maintain:false,admin:false}};
const receipt={collaborators:[row],page:1,has_more:false,affiliation:'all',permission:null};
test('collaborator schema is bounded, read-only and no arbitrary route',()=>{
 assert.ok(WF_V1_TOOLS.some(t=>t.name==='gr_'+action));assert.ok(WF_V1_READ_ACTIONS.includes(action));
 assert.ok(validWfV1Args(action,{repo,affiliation:'outside',permission:'moderate',per_page:50}));
 for(const bad of [{page:true},{per_page:51},{permission:'owner'},{affiliation:[]},{url:'https://elsewhere.invalid'}])assert.ok(!validWfV1Args(action,{repo,...bad}));
});
test('receipt strict projection and request correlation',()=>{
 assert.ok(validWfV1Response(action,receipt,{repo}));
 for(const change of [{id:true},{id:Number.MAX_SAFE_INTEGER+1},{login:'bad/name'},{role_name:' '},{email:'not-returned'},{permissions:{pull:true}}]){
  const x=structuredClone(receipt);Object.assign(x.collaborators[0],change);assert.ok(!validWfV1Response(action,x,{repo}));
 }
 for(const change of [{page:2},{has_more:true},{affiliation:'outside'},{permission:'pull'},{collaborators:[row,row]}])assert.ok(!validWfV1Response(action,{...receipt,...change},{repo}));
 assert.ok(validWfV1Response(action,{...receipt,has_more:true},{repo,per_page:1}));
 assert.ok(validWfV1Response(action,{...receipt,permission:'pull'},{repo,permission:'pull'}));
});

test('permission read binds one exact username and projects only effective role',()=>{
 const a='wf_collaborator_permission',args={repo,username:'fixture-user'};
 const body={user:{id:1,login:'FIXTURE-USER',type:'User'},permission:'read',role_name:'custom'};
 assert.ok(WF_V1_READ_ACTIONS.includes(a));assert.ok(validWfV1Args(a,args));
 assert.ok(validWfV1Args(a,{repo,username:'helper[bot]'}));assert.ok(validWfV1Response(a,body,args));
 for(const username of ['../other','bad/name','',true])assert.ok(!validWfV1Args(a,{repo,username}));
 assert.ok(!validWfV1Response(a,{...body,user:{...body.user,login:'other'}},args));
 assert.ok(!validWfV1Response(a,body,{repo}));
 assert.ok(!validWfV1Response(a,body,{repo,username:1}));
 assert.ok(!validWfV1Response(a,{...body,permission:'maintain'},args));
});
