import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHmac } from 'node:crypto';
import worker from './index.js';

const key = 'state:example/project#42';
const ttl = 60 * 60 * 24 * 14;

function harness(t, initialState) {
  const state = new Map(initialState ? [[key, initialState]] : []);
  const calls = [];
  let mergeState = 'dirty';
  let slack = () => Response.json({ ok: true });
  let kvFailure = false;
  const env = {
    GITHUB_WEBHOOK_SECRET: 'synthetic-webhook-secret',
    GITHUB_TOKEN: 'synthetic-github-token',
    SLACK_BOT_TOKEN: 'synthetic-slack-token',
    PR_STATE: {
      async get(k) { return state.get(k) ?? null; },
      async put(k, value, options) {
        calls.push(['put', k, value, options]);
        if (kvFailure) throw new Error('synthetic KV failure');
        state.set(k, value);
      },
      async delete(k) {
        calls.push(['delete', k]);
        if (kvFailure) throw new Error('synthetic KV failure');
        state.delete(k);
      },
    },
  };
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === 'https://api.github.com/repos/example/project/pulls/42') {
      return Response.json({
        state: mergeState === 'closed' ? 'closed' : 'open',
        mergeable_state: mergeState,
        title: 'Synthetic PR', html_url: 'https://github.com/example/project/pull/42',
      });
    }
    assert.equal(url, 'https://slack.com/api/chat.postMessage');
    const message = JSON.parse(options.body);
    assert.equal(message.channel, 'C0BEZDJDNKV');
    calls.push(['slack', message.text]);
    return slack();
  });
  return {
    state, calls,
    setMergeState(value) { mergeState = value; },
    setSlack(value) { slack = value; },
    setKvFailure(value) { kvFailure = value; },
    async deliver(event = 'pull_request', validSignature = true) {
      const body = JSON.stringify({
        repository: { full_name: 'example/project' },
        pull_request: { number: 42 },
        check_suite: { pull_requests: [{ number: 42 }] },
        check_run: { pull_requests: [{ number: 42 }] },
      });
      const signature = createHmac('sha256', env.GITHUB_WEBHOOK_SECRET).update(body).digest('hex');
      const pending = [];
      const response = await worker.fetch(new Request('https://worker.example', {
        method: 'POST', body,
        headers: {
          'x-github-event': event,
          'x-hub-signature-256': validSignature ? `sha256=${signature}` : 'sha256=invalid',
        },
      }), env, { waitUntil(promise) { pending.push(promise); } });
      const results = await Promise.allSettled(pending);
      return { response, results };
    },
  };
}

const failures = {
  'HTTP 429': () => new Response('rate limited', { status: 429 }),
  'HTTP 500 even with ok:true': () => Response.json({ ok: true }, { status: 500 }),
  'HTTP 200 API rejection': () => Response.json({ ok: false, error: 'channel_not_found' }),
  'missing API confirmation': () => Response.json({}),
  'non-boolean API confirmation': () => Response.json({ ok: 'true' }),
  'null JSON response': () => Response.json(null),
  'invalid JSON response': () => new Response('not JSON'),
  'network rejection': () => { throw new Error('synthetic network failure'); },
};

for (const [name, fail] of Object.entries(failures)) {
  for (const scenario of ['first alert', 'changed alert', 'recovery']) {
    test(`${scenario}: ${name} leaves delivery retryable, success then deduplicates`, async (t) => {
      const previous = scenario === 'first alert' ? undefined : 'dirty';
      const next = scenario === 'recovery' ? 'clean' : 'blocked';
      const h = harness(t, previous);
      h.setMergeState(next);
      h.setSlack(fail);
      const failed = await h.deliver();
      assert.equal(failed.response.status, 200); // acknowledgement is still asynchronous
      assert.equal(failed.results[0].status, 'rejected');
      assert.equal(h.state.get(key), previous);
      assert.deepEqual(h.calls.map(([operation]) => operation), ['slack']);

      h.setSlack(() => Response.json({ ok: true }));
      const retried = await h.deliver();
      assert.equal(retried.results[0].status, 'fulfilled');
      assert.equal(h.state.get(key), scenario === 'recovery' ? undefined : next);
      assert.deepEqual(h.calls.map(([operation]) => operation), ['slack', 'slack', scenario === 'recovery' ? 'delete' : 'put']);
      if (scenario !== 'recovery') assert.deepEqual(h.calls[2], ['put', key, next, { expirationTtl: ttl }]);
      if (scenario === 'recovery') assert.match(h.calls[1][1], /recovered -> clean/);
      const count = h.calls.length;
      await h.deliver();
      assert.equal(h.calls.length, count);
    });
  }
}

for (const next of ['dirty', 'blocked', 'unstable']) {
  test(`${next} alerts once, deduplicates all supported events, then recovers`, async (t) => {
    const h = harness(t);
    h.setMergeState(next);
    await h.deliver('check_suite');
    assert.deepEqual(h.calls.map(([op]) => op), ['slack', 'put']);
    assert.match(h.calls[0][1], new RegExp(`-- ${next} \\(`));
    await h.deliver('check_run');
    await h.deliver('pull_request');
    assert.equal(h.calls.length, 2);
    h.setMergeState('clean');
    await h.deliver('check_run');
    assert.deepEqual(h.calls.map(([op]) => op), ['slack', 'put', 'slack', 'delete']);
  });
}

test('KV failure after confirmed Slack delivery can duplicate on retry', async (t) => {
  const h = harness(t);
  h.setKvFailure(true);
  assert.equal((await h.deliver()).results[0].status, 'rejected');
  assert.equal(h.state.has(key), false);
  h.setKvFailure(false);
  await h.deliver();
  assert.deepEqual(h.calls.map(([op]) => op), ['slack', 'put', 'slack', 'put']);
});

test('recovery KV failure retains prior alert and retries recovery', async (t) => {
  const h = harness(t, 'dirty');
  h.setMergeState('clean');
  h.setKvFailure(true);
  assert.equal((await h.deliver()).results[0].status, 'rejected');
  assert.equal(h.state.get(key), 'dirty');
  h.setKvFailure(false);
  await h.deliver();
  assert.equal(h.state.has(key), false);
  assert.deepEqual(h.calls.map(([op]) => op), ['slack', 'delete', 'slack', 'delete']);
});

test('closed PR clears stale marker without a recovery notification', async (t) => {
  const h = harness(t, 'dirty');
  h.setMergeState('closed');
  await h.deliver();
  assert.deepEqual(h.calls, [['delete', key]]);
  assert.equal(h.state.has(key), false);
});

test('healthy untracked PR, ignored event, and invalid signature do not send Slack', async (t) => {
  const h = harness(t);
  h.setMergeState('clean');
  await h.deliver();
  await h.deliver('ping');
  assert.equal((await h.deliver('pull_request', false)).response.status, 401);
  assert.deepEqual(h.calls, []);
});
