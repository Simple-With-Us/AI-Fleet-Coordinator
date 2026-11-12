// Ruleset read verbs and wf_repo_get merge settings (AFC-664).  Kept apart from
// gr-workflow-v1.test.mjs, whose exact bytes are an attested relay fixture.
import test from 'node:test';
import assert from 'node:assert/strict';
import {WF_V1_READ_ACTIONS,validWfV1Response,validWfV1Args} from '../src/gr-workflow-v1.js';
import {HostedTools, HOSTED_READ_TOOLS} from '../src/hosted-tools.js';
import {GrRelayClient} from '../src/gr-tools.js';
import {loadConfig} from '../src/config.js';
import {testEnv} from './helpers.mjs';
import * as S from '../src/seat-state.js';

const repo='example/repo';
const ownBearer='caller-own-bearer';
function harness(fetchImpl) {
  const store=S.memoryStore(); const gate={store};
  for (const name of ['reserve','noteRateLimited','idemBegin','idemSet','idemDrop','idemPeek','auditCall','roleSet']) gate[name]=options=>S[name](store,options,Date.now());
  gate.roleGet=()=>S.roleGet(store,Date.now()); gate.callTail=n=>S.callTail(store,n);
  const gr=new GrRelayClient({baseUrl:'https://broker.invalid',accessId:'private-access-identifier',accessSecret:'private-access-secret',bearer:ownBearer,fetch:fetchImpl});
  return new HostedTools({seat:'JET-CHAT',scopes:['zulip:read','zulip:write'],clientId:'test',grantRef:'abc123',paused:false,config:{...loadConfig(testEnv()),grWorkflowEnabled:true},zulip:null,key:'',gate,gr,log:()=>{},sleep:async()=>{}});
}

const rule={type:'pull_request',ruleset_id:42,ruleset_source_type:'Repository',ruleset_source:repo,parameters:{allowed_merge_methods:['squash'],required_approving_review_count:0}};
const ruleset={id:42,name:'main protection',target:'branch',source_type:'Repository',source:repo,enforcement:'active'};

test('ruleset reads are hosted reads that reach the relay with the own bearer',async()=>{
  for(const [action,args,reply] of [
    ['wf_branch_rules',{branch:'main'},{branch:'main',rules:[rule,{...rule,type:'deletion',parameters:null}],page:1,has_more:false}],
    ['wf_branch_rules',{branch:'claude/x',per_page:1,page:2},{branch:'claude/x',rules:[rule],page:2,has_more:true}],
    ['wf_rulesets',{},{rulesets:[ruleset,{...ruleset,id:43,target:null}],page:1,has_more:false}],
    ['wf_rulesets',{includes_parents:false,per_page:1},{rulesets:[ruleset],page:1,has_more:true}],
  ]){
    assert.ok(WF_V1_READ_ACTIONS.includes(action),action);
    assert.ok(HOSTED_READ_TOOLS.has('gr_'+action),action);
    const t=harness(async(url,init)=>{
      assert.ok(url.endsWith('/v1/'+action));
      assert.equal(init.headers.Authorization,['Bearer',ownBearer].join(' '));
      return new Response(JSON.stringify(reply));
    });
    const result=await t.call('gr_'+action,{repo,...args});
    assert.ok(!result.isError,action+' '+JSON.stringify(args));
  }
});

test('ruleset read arguments are closed',()=>{
  assert.ok(validWfV1Args('wf_branch_rules',{repo,branch:'main',page:2,per_page:5}));
  assert.ok(validWfV1Args('wf_rulesets',{repo,includes_parents:true}));
  for(const [action,args] of [
    ['wf_branch_rules',{repo}],
    ['wf_branch_rules',{repo,branch:'../x'}],
    ['wf_branch_rules',{repo,branch:'main',url:'x'}],
    ['wf_branch_rules',{repo,branch:'main',per_page:51}],
    ['wf_rulesets',{repo,includes_parents:'true'}],
    ['wf_rulesets',{repo,per_page:0}],
    ['wf_rulesets',{repo,branch:'main'}],
  ]) assert.equal(validWfV1Args(action,args),false,JSON.stringify(args));
});

test('wf_branch_rules receipts are closed and match the request',()=>{
  const good={branch:'main',rules:[rule],page:1,has_more:false};
  assert.ok(validWfV1Response('wf_branch_rules',good,{repo,branch:'main'}));
  assert.ok(validWfV1Response('wf_branch_rules',good));
  assert.equal(validWfV1Response('wf_branch_rules',good,{repo,branch:'other'}),false);
  assert.equal(validWfV1Response('wf_branch_rules',{...good,page:2},{repo,branch:'main'}),false);
  assert.equal(validWfV1Response('wf_branch_rules',{...good,has_more:true},{repo,branch:'main'}),false);
  for(const bad of [
    {...good,extra:1},
    {...good,rules:[{...rule,ruleset_id:0}]},
    {...good,rules:[{...rule,type:'Bad Type'}]},
    {...good,rules:[{...rule,parameters:['x']}]},
    {...good,rules:[{...rule,unexpected:1}]},
    {...good,branch:''},
  ]) assert.equal(validWfV1Response('wf_branch_rules',bad),false,JSON.stringify(bad));
});

test('wf_rulesets receipts are closed and match the request',()=>{
  const good={rulesets:[ruleset],page:1,has_more:false};
  assert.ok(validWfV1Response('wf_rulesets',good,{repo}));
  assert.equal(validWfV1Response('wf_rulesets',{...good,page:3},{repo,page:2}),false);
  for(const bad of [
    {...good,rulesets:[{...ruleset,enforcement:'on'}]},
    {...good,rulesets:[{...ruleset,bypass_actors:[]}]},
    {...good,rulesets:[{...ruleset,name:''}]},
    {...good,rulesets:[{...ruleset,target:'Branch Target'}]},
  ]) assert.equal(validWfV1Response('wf_rulesets',bad),false,JSON.stringify(bad));
});

test('wf_repo_get carries the merge settings when the relay returns them',()=>{
  const repository={id:9,name:'repo',full_name:repo,default_branch:'main',private:false,allow_merge_commit:false,allow_squash_merge:true,allow_rebase_merge:false,allow_auto_merge:true,delete_branch_on_merge:true};
  assert.ok(validWfV1Response('wf_repo_get',{repository}));
  const bare={id:9,name:'repo',full_name:repo,default_branch:'main',private:false};
  assert.ok(validWfV1Response('wf_repo_get',{repository:bare}));
});
