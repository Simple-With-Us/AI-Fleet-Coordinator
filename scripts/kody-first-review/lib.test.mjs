import test from 'node:test';
import assert from 'node:assert/strict';
import {
  classifyHttpError,
  decide,
  hasPendingTrigger,
  isTriggerComment,
  kodyHasAnswered,
  oldEnough,
  safeErrorDetails,
  sweepPrompts,
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

test('classifyHttpError: rate-limit evidence vs policy vs unknown', () => {
  assert.equal(classifyHttpError(429, {}), 'rate-limited');
  assert.equal(classifyHttpError(403, { 'retry-after': '60' }), 'rate-limited');
  assert.equal(classifyHttpError(403, { 'x-ratelimit-remaining': '0' }), 'rate-limited');
  assert.equal(classifyHttpError(403, { 'x-ratelimit-remaining': '4999' }), 'forbidden');
  assert.equal(classifyHttpError(403, {}), 'forbidden');
  assert.equal(classifyHttpError(401, {}), 'forbidden');
  assert.equal(classifyHttpError(500, {}), 'unknown');
  assert.equal(classifyHttpError(404, {}), 'unknown');
});

test('safeErrorDetails: status, class, allowlisted headers only', () => {
  const out = safeErrorDetails(403, {
    'x-github-request-id': 'ABCD:1234:5678',
    'x-ratelimit-limit': '5000',
    'x-ratelimit-remaining': '4999',
    'x-ratelimit-reset': '1762723200',
    'x-evil-header': 'must-not-appear',
    authorization: 'Bearer must-not-appear',
  });
  assert.match(out, /status=403/);
  assert.match(out, /class=forbidden/);
  assert.match(out, /x-github-request-id=ABCD:1234:5678/);
  assert.match(out, /x-ratelimit-remaining=4999/);
  assert.ok(!out.includes('retry-after'));
  assert.ok(!out.includes('x-evil-header'));
  assert.ok(!out.includes('must-not-appear'));
  assert.ok(!out.includes('authorization'));
  const rated = safeErrorDetails(429, { 'retry-after': '60' });
  assert.match(rated, /class=rate-limited/);
  assert.match(rated, /retry-after=60/);
});

const numberedPr = (n) => ({ number: n });

test('sweepPrompts: serialized poster fires at most one terminal-classified POST', async () => {
  let posts = 0;
  const logs = [];
  const res = await sweepPrompts([numberedPr(1), numberedPr(2), numberedPr(3), numberedPr(4)], {
    concurrency: 4,
    verdictFor: async () => 'prompt',
    postTrigger: async () => {
      posts += 1;
      const error = new Error('POST -> status=403 class=forbidden');
      error.classification = 'forbidden';
      throw error;
    },
    log: (m) => logs.push(m),
    logError: (m) => logs.push(m),
  });
  assert.equal(posts, 1);
  assert.equal(res.blocked, 3);
  assert.equal(res.unprocessed, 0);
  assert.equal(res.failed, 1);
  assert.equal(res.terminalClassification, 'forbidden');
  assert.ok(logs.some((m) => m.includes('prompt blocked')));
});

test('sweepPrompts: a worker stops scheduling new PRs after terminal failure', async () => {
  let posts = 0;
  let verdicts = 0;
  const res = await sweepPrompts([numberedPr(1), numberedPr(2), numberedPr(3), numberedPr(4)], {
    concurrency: 1,
    verdictFor: async () => {
      verdicts += 1;
      return 'prompt';
    },
    postTrigger: async () => {
      posts += 1;
      const error = new Error('rate');
      error.classification = 'rate-limited';
      throw error;
    },
  });
  assert.equal(posts, 1);
  assert.equal(verdicts, 1);
  assert.equal(res.blocked, 0);
  assert.equal(res.unprocessed, 3);
  assert.equal(res.terminalClassification, 'rate-limited');
});

test('sweepPrompts: unknown failures are not terminal', async () => {
  let posts = 0;
  const res = await sweepPrompts([numberedPr(1), numberedPr(2), numberedPr(3)], {
    concurrency: 1,
    verdictFor: async () => 'prompt',
    postTrigger: async () => {
      posts += 1;
      const error = new Error('other');
      error.classification = 'unknown';
      throw error;
    },
  });
  assert.equal(posts, 3);
  assert.equal(res.failed, 3);
  assert.equal(res.blocked, 0);
  assert.equal(res.unprocessed, 0);
  assert.equal(res.terminalClassification, null);
});

test('sweepPrompts: non-prompt verdicts never POST; successes count', async () => {
  let posts = 0;
  const verdicts = ['prompt', 'trigger-pending', 'kody-answered'];
  const res = await sweepPrompts([numberedPr(1), numberedPr(2), numberedPr(3)], {
    concurrency: 2,
    verdictFor: async (p) => verdicts[p.number - 1],
    postTrigger: async () => {
      posts += 1;
    },
  });
  assert.equal(posts, 1);
  assert.deepEqual(
    { prompted: res.prompted, failed: res.failed, blocked: res.blocked, unprocessed: res.unprocessed },
    { prompted: 1, failed: 0, blocked: 0, unprocessed: 0 },
  );
});

test('sweepPrompts: terminal GET failure stops further reads and counts unprocessed', async () => {
  let reads = 0;
  let posts = 0;
  const res = await sweepPrompts([numberedPr(1), numberedPr(2), numberedPr(3), numberedPr(4)], {
    concurrency: 1,
    verdictFor: async () => {
      reads += 1;
      const error = new Error('GET -> status=403 class=rate-limited');
      error.classification = 'rate-limited';
      throw error;
    },
    postTrigger: async () => {
      posts += 1;
    },
  });
  assert.equal(reads, 1);
  assert.equal(posts, 0);
  assert.equal(res.failed, 1);
  assert.equal(res.unprocessed, 3);
  assert.equal(res.terminalClassification, 'rate-limited');
});

test('sweepPrompts: concurrent GET terminal failure bounds in-flight reads and blocks later POSTs', async () => {
  let reads = 0;
  let posts = 0;
  const res = await sweepPrompts([numberedPr(1), numberedPr(2), numberedPr(3), numberedPr(4), numberedPr(5), numberedPr(6)], {
    concurrency: 2,
    verdictFor: async (p) => {
      reads += 1;
      if (p.number === 1) {
        const error = new Error('GET -> status=403 class=forbidden');
        error.classification = 'forbidden';
        throw error;
      }
      await new Promise((resolve) => setTimeout(resolve, 5));
      return 'prompt';
    },
    postTrigger: async () => {
      posts += 1;
    },
  });
  assert.equal(reads, 2);
  assert.equal(res.terminalClassification, 'forbidden');
  assert.equal(res.unprocessed, 4);
  assert.equal(res.blocked + res.prompted, 1);
  assert.equal(posts, res.prompted);
});

test('sweepPrompts: unknown GET failure degrades gracefully and the sweep continues', async () => {
  let reads = 0;
  const res = await sweepPrompts([numberedPr(1), numberedPr(2), numberedPr(3)], {
    concurrency: 1,
    verdictFor: async (p) => {
      reads += 1;
      if (p.number === 2) {
        const error = new Error('GET -> status=500 class=unknown');
        error.classification = 'unknown';
        throw error;
      }
      return 'kody-answered';
    },
    postTrigger: async () => {},
  });
  assert.equal(reads, 3);
  assert.equal(res.failed, 1);
  assert.equal(res.unprocessed, 0);
  assert.equal(res.terminalClassification, null);
});
