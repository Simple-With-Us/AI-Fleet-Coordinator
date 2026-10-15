import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const workflow = readFileSync(new URL('./workflows/pr-autofix-reusable.yml', import.meta.url), 'utf8');
const job = workflow.slice(workflow.indexOf('jobs:'));
const steps = job.slice(job.indexOf('    steps:')).split(/\n      - /).slice(1);

const resolve = steps.find((step) => step.includes('id: pr'));
const runFix = steps.find((step) => step.includes('id: fix'));
const validate = steps.find((step) => step.includes('id: validate'));
const diagnostics = steps.find((step) => step.includes('id: diagnostics'));
const upload = steps.find((step) => step.includes('uses: actions/upload-artifact@'));
const push = steps.find((step) => step.includes('name: Commit And Push'));

test('model run fails closed and validation precedes push', () => {
  assert(resolve && runFix && validate && push);
  assert.match(resolve, /headRefOid/);
  assert.match(runFix, /set -euo pipefail/);
  assert(!runFix.includes('|| true'));
  assert.match(runFix, /--output-format stream-json/);
  assert.match(runFix, /claude-execution-output\.json/);
  assert(steps.indexOf(runFix) < steps.indexOf(validate));
  assert(steps.indexOf(validate) < steps.indexOf(push));
  assert.match(validate, /head_sha/);
  assert.match(validate, /verify\.sh/);
  assert.match(push, /START_HEAD_SHA/);
});

test('sanitized diagnostics are retained without raw transcripts', () => {
  assert(diagnostics && upload);
  assert.match(diagnostics, /if: \$\{\{ always\(\)/);
  assert.match(diagnostics, /uses: Simple-With-Us\/congress-trading-shared\/\.github\/actions\/autofix-diagnostics@[a-f0-9]{40}/);
  assert(steps.indexOf(runFix) < steps.indexOf(diagnostics));
  assert(steps.indexOf(diagnostics) < steps.indexOf(upload));
  assert.match(upload, /steps\.diagnostics\.outputs\.summary-path != ''/);
  assert.match(upload, /retention-days: 7\s*$/);
});

test('trusted policy is loaded from the default branch, not the PR checkout', () => {
  assert.match(workflow, /pr-autofix-trusted\.yml/);
  assert.match(workflow, /contents\/\$path\?ref=\$DEFAULT_BRANCH/);
  assert(!workflow.includes('configured=$(awk \'/^max_iterations:/\' .autofix.yml'));
});

test('job concurrency serializes autofix writers per pull request', () => {
  assert.match(job, /concurrency:/);
  assert.match(job, /group: pr-autofix-\$\{\{ github\.repository \}\}-\$\{\{ github\.event\.issue\.number \|\| github\.event\.pull_request\.number \}\}/);
  assert.match(job, /cancel-in-progress: false/);
});

test('push prefers the GitHub App installation token when configured', () => {
  assert.match(workflow, /create-github-app-token@/);
  assert.match(push, /PUSH_TOKEN/);
  assert.match(workflow, /vars\.SWU_AUTOFIX_GITHUB_APP_ID/);
  assert.match(workflow, /id: app_push/);
  assert.match(workflow, /mint_token=true/);
});

test('step if expressions never reference secrets so startup validation passes', () => {
  for (const step of steps) {
    const ifLine = step
      .split('\n')
      .find((line) => line.trimStart().startsWith('if:'));
    if (ifLine) {
      assert(!ifLine.includes('secrets.'), `step if must not reference secrets: ${ifLine.trim()}`);
    }
  }
});
