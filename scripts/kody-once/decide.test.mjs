import test from 'node:test';
import assert from 'node:assert/strict';
import { decideOnce, isFullReviewReceipt, KODY_APP_ID, KODY_APP_SLUG, KODY_CHECK_NAME } from './decide.mjs';

const RECEIPT = {
  name: KODY_CHECK_NAME,
  app: { slug: KODY_APP_SLUG, id: KODY_APP_ID },
  status: 'completed',
  conclusion: 'success',
  completed_at: '2026-11-11T01:05:25Z',
  output: {
    title: 'Code Review Complete',
    summary: 'Review finished successfully. Suggestions (if any) were posted as PR/file comments.',
  },
};

function check(overrides) {
  return { ...RECEIPT, output: { ...RECEIPT.output }, ...overrides };
}

test('a full receipt is the kody-ai success with the auto-merge title and summary', () => {
  assert.equal(isFullReviewReceipt(RECEIPT), true);
  assert.equal(isFullReviewReceipt(check({ app: { slug: 'github-actions' } })), false);
  assert.equal(isFullReviewReceipt(check({ conclusion: 'neutral' })), false);
  assert.equal(
    isFullReviewReceipt(check({ output: { title: 'Code Review Complete', summary: 'Review finished.' } })),
    false,
  );
});

test('opened and ready_for_review run once, and a draft open does not', () => {
  assert.deepEqual(decideOnce({ action: 'opened', draft: false }), { action: 'review', reason: 'first-ready' });
  assert.deepEqual(decideOnce({ action: 'ready_for_review', draft: false }), {
    action: 'review',
    reason: 'first-ready',
  });
  assert.deepEqual(decideOnce({ action: 'reopened', draft: false }), { action: 'review', reason: 'first-ready' });
  assert.deepEqual(decideOnce({ action: 'opened', draft: true }), { action: 'skip', reason: 'draft' });
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: true }), { action: 'skip', reason: 'draft' });
});

test('a later synchronize skips once a receipt exists', () => {
  const decision = decideOnce({ action: 'synchronize', draft: false }, { checks: [RECEIPT] });
  assert.deepEqual(decision, { action: 'skip', reason: 'already-reviewed' });
});

test('a no-diff skip after a receipt stays a skip', () => {
  const skipped = check({
    conclusion: 'skipped',
    completed_at: '2026-11-11T06:09:20Z',
    output: { title: 'Code Review Skipped', summary: 'No New Commits (No changes detected since last review)' },
  });
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [RECEIPT, skipped] }), {
    action: 'skip',
    reason: 'already-reviewed',
  });
});

test('the last failed or non-receipt attempt runs again', () => {
  const failure = check({
    conclusion: 'failure',
    completed_at: '2026-11-11T08:00:00Z',
    output: { title: 'Code Review Failed', summary: 'Review failed.' },
  });
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [RECEIPT, failure] }), {
    action: 'review',
    reason: 'failed-attempt',
  });

  const timedOut = check({
    conclusion: 'timed_out',
    completed_at: '2026-11-11T02:00:00Z',
    output: { title: 'Code Review Failed', summary: 'timed out' },
  });
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [timedOut] }), {
    action: 'review',
    reason: 'failed-attempt',
  });

  const bareSuccess = check({
    output: { title: 'Code Review Complete', summary: 'Review finished.' },
  });
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [bareSuccess] }), {
    action: 'review',
    reason: 'failed-attempt',
  });
});

test('a skipped check with no receipt is not a completed review', () => {
  const skipped = check({
    conclusion: 'skipped',
    output: { title: 'Code Review Skipped', summary: 'Manual mode requires @kody start-review.' },
  });
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [skipped] }), {
    action: 'review',
    reason: 'not-a-review',
  });
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [] }), {
    action: 'review',
    reason: 'no-review-yet',
  });
});

test('an explicit comment or the kody-rereview label requests another review', () => {
  const comments = [{ created_at: '2026-11-11T09:00:00Z', body: '@kody start-review' }];
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [RECEIPT], comments }), {
    action: 'review',
    reason: 'comment',
  });
  assert.deepEqual(
    decideOnce({ action: 'labeled', label: 'kody-rereview', draft: false }, { checks: [RECEIPT] }),
    { action: 'review', reason: 'label' },
  );
});

test('a stale comment, a fenced mention, and a leftover label do not request again', () => {
  const older = [{ created_at: '2026-11-11T00:00:00Z', body: '@kody start-review' }];
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [RECEIPT], comments: older }), {
    action: 'skip',
    reason: 'already-reviewed',
  });
  const fenced = [{ created_at: '2026-11-11T09:00:00Z', body: '```\n@kody start-review\n```' }];
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [RECEIPT], comments: fenced }), {
    action: 'skip',
    reason: 'already-reviewed',
  });
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false, label: 'kody-rereview' }, { checks: [RECEIPT] }), {
    action: 'skip',
    reason: 'already-reviewed',
  });
});

test('an in-flight check is not a second request', () => {
  const running = check({ status: 'in_progress', conclusion: null, completed_at: null, output: { title: '', summary: '' } });
  const comments = [{ created_at: '2026-11-11T09:00:00Z', body: '@kody start-review' }];
  assert.deepEqual(decideOnce({ action: 'synchronize', draft: false }, { checks: [RECEIPT, running], comments }), {
    action: 'skip',
    reason: 'in-flight',
  });
});

test('unrelated events skip', () => {
  assert.deepEqual(decideOnce({ action: 'labeled', label: 'bug', draft: false }), {
    action: 'skip',
    reason: 'unsupported-event',
  });
});
