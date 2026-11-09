import test from 'node:test';
import assert from 'node:assert/strict';
import {
  decide,
  describeApiError,
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

// Built at runtime so no token-shaped literal sits in the source.
const FAKE_PREFIX = ['g', 'h', 's', '_'].join('');

test('describeApiError keeps a bounded GitHub reason and drops credentials', () => {
  const headers = new Map([
    ['x-accepted-github-permissions', 'issues=write'],
    ['x-github-request-id', 'ABCD:1234'],
    ['authorization', `Bearer ${FAKE_PREFIX}SHOULDNEVERAPPEAR1234567890`],
  ]);
  const text = describeApiError({
    method: 'POST',
    path: '/repos/o/r/issues/1/comments',
    status: 403,
    headers,
    bodyText: JSON.stringify({
      message: `Resource not accessible by integration ${FAKE_PREFIX}ABCDEFGHIJKLMNOP1234`,
      documentation_url: 'https://docs.github.com/rest/issues/comments',
    }),
  });
  assert.match(text, /^POST \/repos\/o\/r\/issues\/1\/comments -> 403 /);
  assert.match(text, /Resource not accessible by integration \[redacted\]/);
  assert.match(text, /x-accepted-github-permissions=issues=write/);
  assert.match(text, /x-github-request-id=ABCD:1234/);
  assert.doesNotMatch(text, /SHOULDNEVERAPPEAR|ABCDEFGHIJKLMNOP|authorization/i);
});

test('describeApiError ignores non-JSON bodies and caps the message', () => {
  assert.equal(
    describeApiError({ method: 'GET', path: '/x', status: 502, bodyText: '<html>secret page</html>' }),
    'GET /x -> 502',
  );
  const long = describeApiError({ status: 403, bodyText: JSON.stringify({ message: 'a'.repeat(5000) }) });
  assert.ok(long.length < 400);
});
