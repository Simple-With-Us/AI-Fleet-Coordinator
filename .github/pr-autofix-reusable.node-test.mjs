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

test('the caller, not the reusable job, holds the per-PR concurrency group', () => {
  // A job-level copy of the caller's group deadlocks:  GitHub cancels the job before any step
  // ("Canceling since a deadlock was detected ... between a top level workflow and
  // 'autofix / autofix'", BotFleet run 39054950194).  Serialization lives in the caller.
  assert.doesNotMatch(job, /^\s+concurrency:/m);
  const caller = readFileSync(new URL('./workflows/autofix.yml', import.meta.url), 'utf8');
  const top = caller.slice(0, caller.indexOf('jobs:'));
  assert.match(top, /^concurrency:\n  group: pr-autofix-\$\{\{ github\.repository \}\}-\$\{\{ github\.event\.issue\.number \|\| github\.event\.pull_request\.number \}\}\n  cancel-in-progress: false$/m);
});

test('push prefers the GitHub App installation token when configured', () => {
  assert.match(workflow, /create-github-app-token@/);
  assert.match(push, /PUSH_TOKEN/);
  assert.match(workflow, /vars\.SWU_AUTOFIX_GITHUB_APP_ID/);
  assert.match(workflow, /id: app_push/);
  assert.match(workflow, /mint_token=true/);
});

test('bot guard reads review.user only on pull_request_review events', () => {
  assert.match(job, /github\.event_name == 'pull_request_review'/);
  assert.match(job, /github\.event\.review\.user\.type/);
  assert.match(job, /github\.event_name != 'pull_request_review'/);
  assert.match(job, /github\.event\.comment\.user\.type/);
  assert.doesNotMatch(job, /github\.event\.comment\.user\.type \|\| github\.event\.review\.user\.type/);
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
