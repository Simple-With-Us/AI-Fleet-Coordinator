import test from 'node:test';
import assert from 'node:assert/strict';
import {GR_TOOLS} from '../src/gr-tools.js';
import {WF_V1_TOOLS} from '../src/gr-workflow-v1.js';
import {readFileSync} from 'node:fs';
test('normal PR creation preserves the owner-scoped relay route',()=>{
 const owner=GR_TOOLS.find(t=>t.name==='gr_open_pr');
 assert.match(owner.description,/owner's scoped PR-only token/);
 assert.doesNotMatch(owner.description,/Do not use/);
 const own=WF_V1_TOOLS.find(t=>t.name==='gr_wf_pr_create');
 assert.match(own.description,/normal fleet PR creation use gr_open_pr/);
 assert.match(own.description,/live seat policy grants it/);
 assert.doesNotMatch(own.description,/UNGRANTED|no seat policy grants/);
});
test('protocol PR creation mapping names owner-scoped relay creation',()=>{
 const guide=readFileSync(new URL('../../../docs/protocols/simple-tools-guide.md',import.meta.url),'utf8');
 assert.match(guide,/`gh pr create`[^\n]*`gr_open_pr`/);
 assert.match(guide,/Other operations use the calling seat's own GitHub App/);
 assert.doesNotMatch(guide,/`gh` is the logged fallback/);
});
