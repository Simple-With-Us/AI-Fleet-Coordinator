import test from 'node:test';
import assert from 'node:assert/strict';
import {
  decide,
  hasPendingTrigger,
  isTriggerComment,
  kodyHasAnswered,
  oldEnough,
  KODY_LOGIN,
  MIN_PR_AGE_MINUTES,
} from './lib.mjs';

const NOW = Date.parse('2026-11-07T21:00:00-06:00');
const oldPr = {
  state: 'open',
  created_at: '2026-11-06T10:00:00-06:00',
  head: { repo: { full_name: 'Simple-With-Us/CodeCaps' } },
  base: { repo: { full_name: 'Simple-With-Us/CodeCaps' } },
};

test('trigger text must be its own line', () => {
  assert.equal(isTriggerComment('@kody start-review'), true);
  assert.equal(isTriggerComment('please run @kody start-review now'), false);
  assert.equal(isTriggerComment('> @kody start-review\nquoted earlier'), false);
  assert.equal(isTriggerComment('```\n@kody start-review\n```'), false);
  assert.equal(isTriggerComment('~~~\n@kody start-review\n~~~'), false);
  assert.equal(isTriggerComment('~~~\nquoted\n~~~\n@kody start-review'), true);
  assert.equal(isTriggerComment('@kody start-review\r'), true);
  assert.equal(isTriggerComment('@KODY START-REVIEW'), true);
  assert.equal(isTriggerComment('@kody start-review please'), false);
});

test('kody answered on any comment or review, including failures', () => {
  assert.equal(kodyHasAnswered({ issueComments: [{ user: { login: KODY_LOGIN }, body: 'Code Review Could Not Complete' }] }), true);
  assert.equal(kodyHasAnswered({ reviews: [{ user: { login: KODY_LOGIN } }] }), true);
  assert.equal(kodyHasAnswered({ issueComments: [{ user: { login: 'someone' } }] }), false);
});

test('age gate', () => {
  const fresh = { ...oldPr, created_at: new Date(NOW - 5 * 60 * 1000).toISOString() };
  assert.equal(oldEnough(fresh, NOW), false);
  const old = { ...oldPr, created_at: new Date(NOW - (MIN_PR_AGE_MINUTES + 1) * 60 * 1000).toISOString() };
  assert.equal(oldEnough(old, NOW), true);
  assert.equal(oldEnough({ created_at: 'garbage' }, NOW), false);
});

test('decide prompts only a quiet old same-repo open PR', () => {
  assert.equal(decide(oldPr, { now: NOW }), 'prompt');
  assert.equal(decide({ ...oldPr, state: 'closed' }, { now: NOW }), 'not-open');
  assert.equal(
    decide({ ...oldPr, head: { repo: { full_name: 'fork/CodeCaps' } } }, { now: NOW }),
    'fork-pr',
  );
  assert.equal(
    decide(oldPr, { now: NOW, issueComments: [{ user: { login: KODY_LOGIN }, body: 'x' }] }),
    'kody-answered',
  );
  assert.equal(
    decide(oldPr, { now: NOW, issueComments: [{ user: { login: 'someone' }, body: '@kody start-review' }] }),
    'trigger-pending',
  );
  assert.equal(hasPendingTrigger([{ body: 'unrelated' }]), false);
});
