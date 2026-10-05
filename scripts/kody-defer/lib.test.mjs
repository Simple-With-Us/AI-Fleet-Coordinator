import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  LABELS,
  MANUAL_RESOLVE_MESSAGE,
  REFUSAL_MESSAGE,
  buildIssueBody,
  buildIssueTitle,
  cleanFindingText,
  hasDeferredReply,
  isAllowedActor,
  isBlockedSeverity,
  parseDeferCommand,
  parseSeverity,
  refusalMessage,
  summarizeFinding,
} from './lib.mjs';
import { run } from './defer.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const fixture = JSON.parse(readFileSync(join(here, 'fixtures/kody-comment.json'), 'utf8'));

test('parseDeferCommand accepts /defer with and without a reason', () => {
  assert.deepEqual(parseDeferCommand('/defer'), { reason: '' });
  assert.deepEqual(parseDeferCommand('  /defer  '), { reason: '' });
  assert.deepEqual(parseDeferCommand('/defer ship the fix next week'), { reason: 'ship the fix next week' });
  assert.deepEqual(parseDeferCommand('  /defer   needs a design  '), { reason: 'needs a design' });
  assert.deepEqual(parseDeferCommand('/defer:'), { reason: '' });
  assert.deepEqual(parseDeferCommand('/defer: because the Mac is mid-cutover'), {
    reason: 'because the Mac is mid-cutover',
  });
  assert.deepEqual(parseDeferCommand('note\n/defer: later\nmore'), { reason: 'later' });
});

test('parseDeferCommand rejects /deferred, mid-line mentions, and fenced commands', () => {
  assert.equal(parseDeferCommand('/deferred until Friday'), null);
  assert.equal(parseDeferCommand('/deferral please'), null);
  assert.equal(parseDeferCommand('please /defer this finding'), null);
  assert.equal(parseDeferCommand('see the /defer command'), null);
  assert.equal(parseDeferCommand('```\n/defer stolen\n```\n'), null);
  assert.equal(parseDeferCommand('~~~\n/defer: nope\n~~~\n'), null);
  assert.deepEqual(
    parseDeferCommand('```\n/defer stolen\n```\n/defer real reason\n'),
    { reason: 'real reason' },
  );
  assert.equal(parseDeferCommand(''), null);
  assert.equal(parseDeferCommand('no command here'), null);
});

test('isAllowedActor rejects bots and read-only collaborators', () => {
  assert.equal(isAllowedActor({ userType: 'Bot', login: 'kody-ai[bot]', permission: 'admin' }), false);
  assert.equal(isAllowedActor({ userType: 'User', login: 'someone[bot]', permission: 'write' }), false);
  assert.equal(isAllowedActor({ userType: 'User', login: 'jay', permission: 'admin' }), true);
  assert.equal(isAllowedActor({ userType: 'User', login: 'jay', permission: 'maintain' }), true);
  assert.equal(isAllowedActor({ userType: 'User', login: 'jay', permission: 'write' }), true);
  assert.equal(isAllowedActor({ userType: 'User', login: 'jay', permission: 'WRITE' }), true);
  assert.equal(isAllowedActor({ userType: 'User', login: 'jay', permission: 'read' }), false);
  assert.equal(isAllowedActor({ userType: 'User', login: 'jay', permission: 'triage' }), false);
  assert.equal(isAllowedActor({ userType: 'User', login: 'jay', permission: '' }), false);
});

test('parseSeverity reads the shields severity_level badge', () => {
  assert.equal(parseSeverity(fixture.body), 'high');
  assert.equal(
    parseSeverity('![c](https://img.shields.io/badge/severity_level-critical-B71C1C)'),
    'critical',
  );
  assert.equal(
    parseSeverity('![m](https://img.shields.io/badge/severity_level-medium-6B6B92)'),
    'medium',
  );
  assert.equal(
    parseSeverity('![l](https://img.shields.io/badge/severity_level-low-6B6B92)'),
    'low',
  );
  assert.equal(parseSeverity('no badge here'), null);
  assert.equal(parseSeverity('severity_level-urgent'), null);
});

test('cleanFindingText strips badges, details, and keeps the suggestion', () => {
  const cleaned = cleanFindingText(fixture.body);
  assert.equal(cleaned.includes('img.shields.io'), false);
  assert.equal(cleaned.includes('<details'), false);
  assert.equal(cleaned.includes('</details>'), false);
  assert.equal(cleaned.includes('Prompt for LLM'), false);
  assert.equal(cleaned.includes('kody-codereview'), false);
  assert.equal(cleaned.includes('expect_launchd'), true);
  assert.equal(cleaned.includes('expect_launchd=('), true);
  assert.equal(
    cleaned.includes('app.botfleet.server app.botfleet.server.plist com.jay.botfleet-server'),
    true,
  );
  assert.equal(cleaned.includes('botfleet_update_in_progress'), true);
  assert.equal(/\n{4,}/.test(cleaned), false);
});

test('summarizeFinding truncates at a word boundary with an ellipsis', () => {
  const summary = summarizeFinding(cleanFindingText(fixture.body), 80);
  assert.ok(summary.length <= 80, summary);
  assert.ok(summary.startsWith('expect_launchd is reduced'), summary);
  assert.equal(summary.endsWith('…'), true);
  assert.equal(/\s…$/.test(summary), false);
  const short = summarizeFinding('Short finding.', 80);
  assert.equal(short, 'Short finding.');
  const cut = summarizeFinding('alpha beta gamma delta epsilon', 12);
  assert.ok(cut.endsWith('…'));
  assert.ok(cut.length <= 12);
  assert.equal(cut.includes('gamma'), false);
});

test('buildIssueTitle prefixes Deferred review', () => {
  const title = buildIssueTitle('The watcher reports a false DOWN.');
  assert.equal(title.startsWith('Deferred review: '), true);
  assert.equal(title, 'Deferred review: The watcher reports a false DOWN.');
  const long = buildIssueTitle(cleanFindingText(fixture.body));
  assert.equal(long.startsWith('Deferred review: '), true);
  assert.ok(long.length < 120);
});

test('buildIssueBody contains the finding, links, and the sweep marker', () => {
  const body = buildIssueBody({
    finding: 'The watcher reports a false DOWN.',
    path: 'scripts/mac-process-watch.sh',
    line: 107,
    startLine: 102,
    prUrl: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/332',
    prNumber: 332,
    commentUrl: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/332#discussion_r1',
    deferCommentUrl: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/332#discussion_r2',
    reason: '',
    headSha: 'abc123def',
    severity: 'high',
    deferredBy: 'jay',
    threadId: 'PRRT_thread1',
    rootCommentId: 100,
    repo: 'Simple-With-Us/AI-Fleet-Coordinator',
  });
  assert.match(body, /The watcher reports a false DOWN\./);
  assert.match(body, /`scripts\/mac-process-watch\.sh:107`/);
  assert.match(
    body,
    /https:\/\/github\.com\/Simple-With-Us\/AI-Fleet-Coordinator\/blob\/abc123def\/scripts\/mac-process-watch\.sh#L107/,
  );
  assert.match(body, /\*\*File:\*\* `scripts\/mac-process-watch\.sh`/);
  assert.match(body, /\[#332\]\(https:\/\/github\.com\/Simple-With-Us\/AI-Fleet-Coordinator\/pull\/332\)/);
  assert.match(body, /discussion_r1/);
  assert.match(body, /discussion_r2/);
  assert.match(body, /@jay/);
  assert.match(body, /\(none given\)/);
  assert.match(body, /`abc123def`/);
  assert.match(body, /^high$/m);
  assert.match(
    body,
    /<!-- kody-defer thread=PRRT_thread1 root=100 path=scripts\/mac-process-watch\.sh -->/,
  );
});

test('hasDeferredReply returns the issue number or null', () => {
  assert.equal(hasDeferredReply([{ body: 'hello' }, { body: 'Deferred to #12. done' }]), 12);
  assert.equal(hasDeferredReply([{ body: 'not deferred' }]), null);
  assert.equal(hasDeferredReply([{ body: 'see Deferred to #3 later' }]), null);
  assert.equal(hasDeferredReply(['Deferred to #7.']), 7);
  assert.equal(hasDeferredReply([]), null);
});

test('isBlockedSeverity defaults to critical and honors a csv list', () => {
  assert.equal(isBlockedSeverity('critical'), true);
  assert.equal(isBlockedSeverity('high'), false);
  assert.equal(isBlockedSeverity('CRITICAL', 'critical'), true);
  assert.equal(isBlockedSeverity('critical', 'critical,high'), true);
  assert.equal(isBlockedSeverity('high', 'critical, high'), true);
  assert.equal(isBlockedSeverity('medium', 'critical,high'), false);
  assert.equal(isBlockedSeverity('HIGH', ' high '), true);
  assert.equal(isBlockedSeverity(null, 'critical'), false);
  assert.equal(isBlockedSeverity('', 'critical,high'), false);
});

test('refusal message is the exact critical string and capitalizes other severities', () => {
  assert.equal(REFUSAL_MESSAGE, "Critical findings can't be deferred.  Fix this in the PR.");
  assert.equal(REFUSAL_MESSAGE.includes('.  Fix'), true);
  assert.equal(refusalMessage('high'), "High findings can't be deferred.  Fix this in the PR.");
  assert.equal(refusalMessage('medium'), "Medium findings can't be deferred.  Fix this in the PR.");
  assert.equal(MANUAL_RESOLVE_MESSAGE.includes('.  This thread'), true);
  assert.equal(LABELS.deferred.name, 'kody-deferred');
  assert.equal(LABELS.deferred.color, 'D4A72C');
  assert.equal(LABELS.high.name, 'kody-high');
  assert.equal(LABELS.critical.color, 'B60205');
  assert.ok(LABELS.low.description);
});

test('workflow run lines do not interpolate comment text', () => {
  const root = join(here, '..', '..');
  const files = [
    join(root, '.github/workflows/kody-defer.yml'),
    join(root, '.github/workflows/kody-defer-caller.yml'),
    join(root, 'github-workflows-template/workflows/kody-defer.yml'),
  ];
  for (const file of files) {
    const text = readFileSync(file, 'utf8');
    const runBlocks = text.split('\n').filter((line) => /^\s*run:/.test(line));
    for (const line of runBlocks) {
      assert.equal(/github\.event\.comment\.(body|title)/.test(line), false, file);
      assert.equal(/\$\{\{[^}]*comment\.body/.test(line), false, file);
    }
  }
});

function jsonResponse(status, data, headers = {}) {
  const bag = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => bag.get(String(name).toLowerCase()) ?? null },
    async text() {
      return JSON.stringify(data);
    },
  };
}

function baseEnv(extra = {}) {
  return {
    GITHUB_TOKEN: 'test-token',
    GITHUB_REPOSITORY: 'Simple-With-Us/AI-Fleet-Coordinator',
    GITHUB_API_URL: 'https://api.github.com',
    GITHUB_GRAPHQL_URL: 'https://api.github.com/graphql',
    KODY_DEFER_BLOCKED_SEVERITIES: 'critical',
    ...extra,
  };
}

function reviewEvent({
  body = '/defer next week',
  userType = 'User',
  login = 'alice',
  inReplyTo = 100,
  commentId = 200,
  createdAt = '2026-10-05T00:00:00Z',
} = {}) {
  return {
    action: 'created',
    comment: {
      id: commentId,
      body,
      user: { login, type: userType },
      in_reply_to_id: inReplyTo,
      path: 'scripts/mac-process-watch.sh',
      line: 107,
      html_url: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/332#discussion_r200',
      created_at: createdAt,
    },
    pull_request: {
      number: 332,
      html_url: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/332',
      head: { sha: 'abc123def456' },
    },
    repository: { full_name: 'Simple-With-Us/AI-Fleet-Coordinator' },
  };
}

function threadNode(comments, { id = 'PRRT_thread1', isResolved = false } = {}) {
  return {
    id,
    isResolved,
    comments: { nodes: comments },
  };
}

/**
 * @param {{
 *   permission?: string,
 *   rootBody?: string,
 *   threadComments?: object[],
 *   issues?: object[],
 *   resolve?: 'ok'|'fail',
 *   permissionStatus?: number,
 * }} opts
 */
function installFake(opts = {}) {
  const calls = [];
  const permission = opts.permission ?? 'write';
  const permissionStatus = opts.permissionStatus ?? 200;
  const rootBody = opts.rootBody ?? fixture.body;
  const threadComments = opts.threadComments ?? [
    {
      databaseId: 100,
      body: rootBody,
      url: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/332#discussion_r100',
      createdAt: '2026-10-01T00:00:00Z',
    },
    {
      databaseId: 200,
      body: '/defer next week',
      url: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/332#discussion_r200',
      createdAt: '2026-10-05T00:00:00Z',
    },
  ];
  const issues = opts.issues ?? [];
  const resolve = opts.resolve ?? 'ok';

  const fetchImpl = async (url, init = {}) => {
    const method = init.method || 'GET';
    const reqBody = init.body ? JSON.parse(init.body) : null;
    calls.push({ method, url, body: reqBody });
    const u = new URL(url);

    if (method === 'GET' && u.pathname.endsWith('/collaborators/alice/permission')) {
      if (permissionStatus !== 200) return jsonResponse(permissionStatus, { message: 'Not Found' });
      return jsonResponse(200, { permission });
    }
    if (method === 'GET' && /\/pulls\/comments\/\d+$/.test(u.pathname)) {
      return jsonResponse(200, {
        id: 100,
        body: rootBody,
        path: 'scripts/mac-process-watch.sh',
        line: 107,
        start_line: null,
        html_url: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/pull/332#discussion_r100',
        in_reply_to_id: null,
        user: { login: 'kody-ai[bot]', type: 'Bot' },
      });
    }
    if (method === 'POST' && u.pathname === '/graphql') {
      const query = reqBody.query || '';
      if (/mutation/.test(query) && /resolveReviewThread/.test(query)) {
        if (resolve === 'fail') {
          return jsonResponse(200, {
            data: { resolveReviewThread: null },
            errors: [{ message: 'Resource not accessible by integration' }],
          });
        }
        return jsonResponse(200, {
          data: { resolveReviewThread: { thread: { isResolved: true } } },
        });
      }
      return jsonResponse(200, {
        data: {
          repository: {
            pullRequest: {
              reviewThreads: {
                pageInfo: { hasNextPage: false, endCursor: null },
                nodes: [threadNode(threadComments)],
              },
            },
          },
        },
      });
    }
    if (method === 'GET' && u.pathname.endsWith('/issues') && u.searchParams.get('labels') === 'kody-deferred') {
      return jsonResponse(200, issues);
    }
    if (method === 'GET' && /\/labels\//.test(u.pathname)) {
      return jsonResponse(404, { message: 'Not Found' });
    }
    if (method === 'POST' && u.pathname.endsWith('/labels')) {
      return jsonResponse(201, reqBody);
    }
    if (method === 'POST' && u.pathname.endsWith('/issues')) {
      return jsonResponse(201, { number: 42, html_url: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/issues/42' });
    }
    if (method === 'POST' && /\/replies$/.test(u.pathname)) {
      return jsonResponse(201, { id: 300, body: reqBody.body });
    }
    return jsonResponse(500, { message: `unhandled ${method} ${u.pathname}` });
  };

  return { fetchImpl, calls };
}

function writes(calls) {
  return calls.filter((c) => {
    if (c.method !== 'POST') return false;
    if (c.body && typeof c.body.query === 'string' && !/mutation/.test(c.body.query)) return false;
    return true;
  });
}

test('critical severity only posts the refusal', async () => {
  const fake = installFake({
    rootBody: '![c](https://img.shields.io/badge/severity_level-critical-B71C1C)\n\nDrops traffic.\n',
  });
  const result = await run({
    event: reviewEvent(),
    api: fake.fetchImpl,
    env: baseEnv(),
    log() {},
  });
  assert.equal(result.code, 0);
  assert.equal(result.action, 'refused');
  const refusalWrites = writes(fake.calls);
  assert.equal(refusalWrites.length, 1);
  assert.match(refusalWrites[0].url, /\/pulls\/332\/comments\/100\/replies$/);
  assert.equal(refusalWrites[0].body.body, REFUSAL_MESSAGE);
  assert.equal(refusalWrites.some((c) => c.url.endsWith('/issues')), false);
  assert.equal(refusalWrites.some((c) => /resolveReviewThread/.test(c.body?.query || '')), false);
});

test('high severity ensures labels, opens an issue, replies, and resolves', async () => {
  const fake = installFake();
  const result = await run({
    event: reviewEvent({ body: '/defer after the cutover' }),
    api: fake.fetchImpl,
    env: baseEnv(),
    log() {},
  });
  assert.equal(result.code, 0);
  assert.equal(result.action, 'deferred');
  assert.equal(result.issue, 42);
  const created = writes(fake.calls);
  const labelPosts = created.filter((c) => c.url.endsWith('/labels'));
  const names = labelPosts.map((c) => c.body.name).sort();
  assert.deepEqual(names, ['kody-deferred', 'kody-high']);
  const issue = created.find((c) => c.method === 'POST' && c.url.endsWith('/issues'));
  assert.ok(issue);
  assert.deepEqual(issue.body.labels.sort(), ['kody-deferred', 'kody-high']);
  assert.equal(issue.body.title.startsWith('Deferred review: '), true);
  assert.match(issue.body.body, /thread=PRRT_thread1/);
  assert.match(issue.body.body, /path=scripts\/mac-process-watch\.sh/);
  assert.equal(issue.body.body.includes('img.shields.io'), false);
  assert.equal(issue.body.body.includes('Prompt for LLM'), false);
  assert.match(issue.body.body, /after the cutover/);
  const replies = created.filter((c) => c.url.endsWith('/replies'));
  assert.equal(replies.length, 1);
  assert.equal(replies[0].body.body, 'Deferred to #42.');
  assert.ok(created.some((c) => /resolveReviewThread/.test(c.body?.query || '')));
});

test('resolve failure posts a manual-resolve reply and exits ok', async () => {
  const fake = installFake({ resolve: 'fail' });
  const result = await run({
    event: reviewEvent(),
    api: fake.fetchImpl,
    env: baseEnv(),
    log() {},
  });
  assert.equal(result.code, 0);
  assert.equal(result.action, 'deferred');
  const replies = writes(fake.calls).filter((c) => c.url.endsWith('/replies'));
  assert.equal(replies.length, 2);
  assert.equal(replies[0].body.body, 'Deferred to #42.');
  assert.match(replies[1].body.body, /manual resolve/);
  assert.match(replies[1].body.body, /could not resolve it/);
  assert.match(replies[1].body.body, /Resource not accessible by integration/);
  assert.match(replies[1].body.body, /\. {2}The workflow/);
});

test('an existing Deferred to reply performs no writes', async () => {
  const fake = installFake({
    threadComments: [
      {
        databaseId: 100,
        body: fixture.body,
        url: 'https://example.test/c100',
        createdAt: '2026-10-01T00:00:00Z',
      },
      {
        databaseId: 200,
        body: '/defer next week',
        url: 'https://example.test/c200',
        createdAt: '2026-10-05T00:00:00Z',
      },
      {
        databaseId: 201,
        body: 'Deferred to #7.',
        url: 'https://example.test/c201',
        createdAt: '2026-10-05T00:01:00Z',
      },
    ],
  });
  const result = await run({
    event: reviewEvent(),
    api: fake.fetchImpl,
    env: baseEnv(),
    log() {},
  });
  assert.equal(result.code, 0);
  assert.equal(result.action, 'already-deferred');
  assert.equal(result.issue, 7);
  assert.equal(writes(fake.calls).length, 0);
});

test('a read-only collaborator performs no writes', async () => {
  const fake = installFake({ permission: 'read' });
  const result = await run({
    event: reviewEvent(),
    api: fake.fetchImpl,
    env: baseEnv(),
    log() {},
  });
  assert.equal(result.code, 0);
  assert.equal(result.action, 'not-collaborator');
  assert.equal(writes(fake.calls).length, 0);
});

test('a bot actor performs no writes', async () => {
  const fake = installFake();
  const result = await run({
    event: reviewEvent({ userType: 'Bot', login: 'kody-ai[bot]' }),
    api: fake.fetchImpl,
    env: baseEnv(),
    log() {},
  });
  assert.equal(result.code, 0);
  assert.equal(result.action, 'bot');
  assert.equal(fake.calls.length, 0);
});

test('an existing marker issue is reused instead of creating another', async () => {
  const fake = installFake({
    issues: [
      {
        number: 9,
        body: 'old\n<!-- kody-defer thread=PRRT_thread1 root=100 path=scripts/mac-process-watch.sh -->\n',
      },
      { number: 8, pull_request: { url: 'https://example.test/pull/8' }, body: 'thread=PRRT_thread1' },
    ],
  });
  const result = await run({
    event: reviewEvent(),
    api: fake.fetchImpl,
    env: baseEnv(),
    log() {},
  });
  assert.equal(result.code, 0);
  assert.equal(result.action, 'relinked');
  assert.equal(result.issue, 9);
  const linked = writes(fake.calls);
  assert.equal(linked.some((c) => c.url.endsWith('/issues')), false);
  const replies = linked.filter((c) => c.url.endsWith('/replies'));
  assert.equal(replies[0].body.body, 'Deferred to #9.');
  assert.ok(linked.some((c) => /resolveReviewThread/.test(c.body?.query || '')));
});

test('a comment that is not /defer exits quietly', async () => {
  const fake = installFake();
  const result = await run({
    event: reviewEvent({ body: 'looks good' }),
    api: fake.fetchImpl,
    env: baseEnv(),
    log() {},
  });
  assert.equal(result.code, 0);
  assert.equal(result.action, 'ignored');
  assert.equal(fake.calls.length, 0);
});
