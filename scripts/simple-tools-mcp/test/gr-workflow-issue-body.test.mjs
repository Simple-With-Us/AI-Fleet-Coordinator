import test from 'node:test';
import assert from 'node:assert/strict';
import {validWfV1Args, validWfV1Response} from '../src/gr-workflow-v1.js';

const repo = 'example/repo';
const issue = {
  number: 2,
  state: 'open',
  title: 'x',
  updated_at: null,
  html_url: 'https://example.invalid/2',
  body: 'details',
  body_withheld: false,
};

test('wf_issue_list accepts a bounded label filter and refuses a bad one', () => {
  assert.equal(validWfV1Args('wf_issue_list', {repo, state: 'open', per_page: 20, labels: 'needs-mac'}), true);
  assert.equal(validWfV1Args('wf_issue_list', {repo, labels: 'needs-mac, mac-seat-claimed'}), true);
  assert.equal(validWfV1Args('wf_issue_list', {repo, labels: Array.from({length: 10}, () => 'a'.repeat(100)).join(',')}), true);
  for (const labels of ['', 'ok,,x', 'bad\nlabel', 'a'.repeat(101), Array.from({length: 11}, () => 'ab').join(','), 3]) {
    assert.equal(validWfV1Args('wf_issue_list', {repo, labels}), false, String(labels));
  }
});

test('wf_issue_get accepts a bounded body and refuses an unfenced one', () => {
  const args = {repo, number: 2};
  assert.equal(validWfV1Response('wf_issue_get', {issue}, args), true);
  assert.equal(validWfV1Response('wf_issue_get', {issue: {...issue, body: 'x'.repeat(65536)}}, args), true);
  assert.equal(validWfV1Response('wf_issue_get', {issue: {...issue, body: 'x'.repeat(65537)}}, args), false);
  assert.equal(validWfV1Response('wf_issue_get', {issue: {...issue, number: 9}}, args), false);
  assert.equal(validWfV1Response('wf_issue_get', {issue: {number: 2, state: 'open'}}, args), false);
  const secret = 'AKIA' + '0'.repeat(16);
  assert.equal(validWfV1Response('wf_issue_get', {issue: {...issue, body: 'key ' + secret}}, args), false);
  assert.equal(validWfV1Response('wf_issue_get', {issue: {...issue, body: '<withheld: credential-shaped text>', body_withheld: true}}, args), true);
  assert.equal(validWfV1Response('wf_issue_get', {issue: {...issue, body: 'key ' + secret, body_withheld: true}}, args), false);
  assert.equal(validWfV1Response('wf_issue_get', {issue: {...issue, body: 'still here', body_withheld: true}}, args), false);
});

test('issue writes still return a summary without a body', () => {
  assert.equal(validWfV1Response('wf_issue_create', {issue: {number: 2, state: 'open'}}, {repo, title: 't', body: 'b'}), true);
  assert.equal(validWfV1Response('wf_issue_list', {issues: [{number: 3, state: 'open'}], complete: false}, {repo, labels: 'needs-mac'}), true);
});
