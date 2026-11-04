import { test } from 'node:test';
import assert from 'node:assert/strict';
import { withServiceTitle } from '../src/tool-titles.js';
import { HOSTED_TOOL_LIST } from '../src/hosted-tools.js';
import { toolListFor } from '../src/delegated-identities.js';

test('service titles cover prefixed, unprefixed and untitled entries without duplicate prefixes', () => {
  for (const [name,title,want] of [
    ['linear_list','Linear List','Linear: List'],
    ['linear_list_issues','List Issues','Linear: List Issues'],
    ['linear_save_issue','Linear Save Issue','Linear: Save Issue'],
    ['gr_open_pr','Open Pull Request','GitHub: Open Pull Request'],
    ['gr_wf_pr_get',undefined,'GitHub: Pull Request Get'],
    ['gr_wf_git_blob_get',undefined,'GitHub: Git Blob Get'],
    ['linear_get_issue','Linear: Get Issue','Linear: Get Issue'],
  ]) assert.equal(withServiceTitle({name,title}).title,want);
});
test('only title changes and unrelated families retain identity',()=>{
  const original={name:'gr_wf_pr_get',description:'read',inputSchema:{type:'object'},annotations:{readOnlyHint:true}};
  const got=withServiceTitle(original);const {title,...rest}=got;assert.deepEqual(rest,original);assert.equal(got.inputSchema,original.inputSchema);
  const other={name:'zulip_search',title:'Zulip Search Messages'};assert.equal(withServiceTitle(other),other);
});
test('complete hosted catalog has exactly one service prefix and stable protocol names',()=>{
  for(const tool of HOSTED_TOOL_LIST){
    const family=tool.name.startsWith('linear_')?'Linear':tool.name.startsWith('gr_')?'GitHub':null;
    if(family){assert.ok(tool.title.startsWith(`${family}: `),tool.name);assert.ok(!tool.title.includes(`${family}: ${family}`),tool.title);assert.equal(withServiceTitle(tool).title,tool.title);}
  }
  assert.equal(new Set(HOSTED_TOOL_LIST.map(t=>t.name)).size,HOSTED_TOOL_LIST.length);
});
test('actual per-connection advertised catalog preserves titles and identity decoration',()=>{
  const {list}=toolListFor('ECHO',{grWorkflowEnabled:true,grTypedEnabled:true});
  for(const name of ['linear_list','linear_list_issues','gr_open_pr','gr_wf_pr_get']){
    const tool=list.find(t=>t.name===name);assert.ok(tool,name);assert.match(tool.title,/^(Linear|GitHub): /);assert.deepEqual(tool.inputSchema.properties.identity.enum,['ECHO']);
  }
});
