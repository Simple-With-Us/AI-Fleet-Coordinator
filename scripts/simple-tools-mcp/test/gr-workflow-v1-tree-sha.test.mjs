import test from 'node:test';
import assert from 'node:assert/strict';
import {validWfV1Args} from '../src/gr-workflow-v1.js';

const S40 = 'a'.repeat(40);
const repo = 'owner/repo';
const base = {repo, base_tree: S40};

test('tree_create accepts verified-blob sha entries with closed schemas', () => {
  assert.ok(validWfV1Args('wf_git_tree_create', {...base, files: [{path: 'big.py', sha: S40, mode: '100644'}]}));
  assert.ok(validWfV1Args('wf_git_tree_create', {...base, files: [
    {path: 'a', text: 'hi', mode: '100644'},
    {path: 'b', sha: S40, mode: '100755'},
  ]}));
  assert.ok(!validWfV1Args('wf_git_tree_create', {...base, files: [{path: 'x', sha: S40, text: 'hi', mode: '100644'}]}));
  assert.ok(!validWfV1Args('wf_git_tree_create', {...base, files: [{path: 'x', mode: '100644'}]}));
  assert.ok(!validWfV1Args('wf_git_tree_create', {...base, files: [{path: 'x', sha: 'z'.repeat(40), mode: '100644'}]}));
  // Sha entries carry no inline text and do not consume the cumulative text cap.
  const files = [
    {path: 'f.txt', text: 'y'.repeat(39000), mode: '100644'},
    {path: 'big.py', sha: S40, mode: '100644'},
  ];
  assert.ok(validWfV1Args('wf_git_tree_create', {...base, files}));
  assert.ok(!validWfV1Args('wf_git_tree_create', {...base, files: [
    {path: 'f.txt', text: 'y'.repeat(40001), mode: '100644'},
    {path: 'big.py', sha: S40, mode: '100644'},
  ]}));
});
