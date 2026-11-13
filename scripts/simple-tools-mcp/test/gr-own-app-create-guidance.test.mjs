import test from 'node:test';
import assert from 'node:assert/strict';
import {GR_TOOLS} from '../src/gr-tools.js';
import {WF_V1_TOOLS} from '../src/gr-workflow-v1.js';
import {readFileSync} from 'node:fs';
test('legacy owner-token creation is not presented as an authorized own-App route',()=>{
 const legacy=GR_TOOLS.find(t=>t.name==='gr_open_pr');
 assert.match(legacy.description,/Do not use/);
 assert.match(legacy.description,/gr_wf_pr_create/);
 assert.match(legacy.description,/does not run as the calling seat's own GitHub App/);
 assert.match(legacy.description,/does not authorize this legacy route or a direct GitHub fallback/);
 const own=WF_V1_TOOLS.find(t=>t.name==='gr_wf_pr_create');
 assert.match(own.description,/caller's own GitHub App/);
});
test('protocol PR creation mapping names the own-App fenced route',()=>{
 const guide=readFileSync(new URL('../../../docs/protocols/simple-tools-guide.md',import.meta.url),'utf8');
 assert.match(guide,/`gh pr create`[^\n]*`gr_wf_pr_create`/);
 assert.doesNotMatch(guide,/PR-write authoring runs under the owner's scoped PR-only token on purpose/);
});
