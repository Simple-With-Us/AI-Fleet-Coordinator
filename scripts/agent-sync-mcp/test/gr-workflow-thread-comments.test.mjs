import test from 'node:test';
import assert from 'node:assert/strict';
import {validWfV1Args,validWfV1Response} from '../src/gr-workflow-v1.js';
const sha='a'.repeat(40),action='wf_review_thread_comments';
const args={repo:'owner/repo',number:1,expected_head_sha:sha,thread_id:'PRRT_fixture'};
const good={number:1,head_sha:sha,thread_id:'PRRT_fixture',is_resolved:true,is_outdated:false,comments:[{id:4505651651,body:'untrusted',url:'https://github.com/owner/repo/pull/1#discussion_r4505651651',author:'kody-ai',review_id:5817290486,commit_id:sha,reply_to_id:4505627149}],has_more:false,end_cursor:'cursor',snapshot:false};
test('bounded thread reply args and exact receipt binding',()=>{
 assert.ok(validWfV1Args(action,args));
 assert.ok(validWfV1Args(action,{...args,cursor:'cursor'}));
 assert.ok(!validWfV1Args(action,{...args,cursor:'bad cursor'}));
 assert.ok(!validWfV1Args(action,{...args,thread_id:'foreign'}));
 assert.ok(validWfV1Response(action,good,args));
 for(const change of [{number:2},{head_sha:'b'.repeat(40)},{thread_id:'PRRT_other'},{snapshot:true},{has_more:true,end_cursor:null},{comments:[good.comments[0],good.comments[0]]}])assert.ok(!validWfV1Response(action,{...good,...change},args));
 assert.ok(!validWfV1Response(action,{...good,comments:[{...good.comments[0],body:'x'.repeat(65537)}]},args));
 assert.ok(!validWfV1Response(action,{...good,comments:[{...good.comments[0],url:'https://wrong.invalid/'}]},args));
});
