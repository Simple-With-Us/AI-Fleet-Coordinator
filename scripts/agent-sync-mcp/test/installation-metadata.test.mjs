import test from 'node:test';
import assert from 'node:assert/strict';
import {WF_V1_TOOLS,WF_V1_READ_ACTIONS,validWfV1Args,validWfV1Response} from '../src/gr-workflow-v1.js';
const action='wf_installation_metadata';
const good={installation:{app_slug:'swu-wise',installation_id:34,account:{id:56,login:'Simple-With-Us'},accepted_permissions:{contents:'write',workflows:'write'},suspended:false}};
test('reader closed schema forbids selectors and is read-only',()=>{
 assert.ok(WF_V1_READ_ACTIONS.includes(action));assert.ok(WF_V1_TOOLS.some(t=>t.name==='gr_'+action));assert.ok(validWfV1Args(action,{repo:'Simple-With-Us/AI-Fleet-Coordinator'}));
 for(const key of ['seat','app_slug','installation_id','token','url'])assert.equal(validWfV1Args(action,{repo:'Simple-With-Us/AI-Fleet-Coordinator',[key]:'other'}),false);
});
test('reader accepts safe fields only',()=>{
 assert.ok(validWfV1Response(action,good));
 for(const key of ['token','jwt','headers','raw'])assert.equal(validWfV1Response(action,{installation:{...good.installation,[key]:'private'}}),false);
 assert.equal(validWfV1Response(action,{installation:{...good.installation,accepted_permissions:{workflows:'admin'}}}),false);
});
