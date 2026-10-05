import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  ageDays,
  extractPath,
  isStale,
  rankIssues,
  renderJson,
  renderMarkdown,
  sweep,
} from './kody-deferred-sweep.mjs';

const NOW = '2026-10-05T00:00:00Z';

test('rankIssues puts touched files first, then severity, then age, then repo', () => {
  const ranked = rankIssues([
    { repo: 'Simple-With-Us/Clutch', number: 2, severity: 'critical', createdAt: '2026-09-01T00:00:00Z', touchedSince: false },
    { repo: 'Simple-With-Us/BotFleet', number: 9, severity: 'low', createdAt: '2026-08-01T00:00:00Z', touchedSince: true },
    { repo: 'Simple-With-Us/BotFleet', number: 3, severity: 'high', createdAt: '2026-09-15T00:00:00Z', touchedSince: true },
    { repo: 'Simple-With-Us/BotFleet', number: 4, severity: 'high', createdAt: '2026-09-01T00:00:00Z', touchedSince: true },
    { repo: 'Simple-With-Us/AI-Fleet-Coordinator', number: 8, severity: null, createdAt: '2026-07-01T00:00:00Z', touchedSince: false },
    { repo: 'Simple-With-Us/AI-Fleet-Coordinator', number: 1, severity: 'critical', createdAt: '2026-09-01T00:00:00Z', touchedSince: false },
    { repo: 'Simple-With-Us/AI-Fleet-Coordinator', number: 7, severity: 'high', createdAt: '2026-09-01T00:00:00Z', touchedSince: true },
  ]);
  assert.deepEqual(
    ranked.map((item) => `${item.touchedSince ? 'T' : 'U'}:${item.severity || 'none'}:${item.repo.split('/')[1]}#${item.number}`),
    [
      'T:high:AI-Fleet-Coordinator#7',
      'T:high:BotFleet#4',
      'T:high:BotFleet#3',
      'T:low:BotFleet#9',
      'U:critical:AI-Fleet-Coordinator#1',
      'U:critical:Clutch#2',
      'U:none:AI-Fleet-Coordinator#8',
    ],
  );
});

test('ageDays and isStale use a strict greater-than threshold', () => {
  assert.equal(ageDays('2026-09-05T00:00:00Z', NOW), 30);
  assert.equal(ageDays('2026-09-04T00:00:00Z', NOW), 31);
  assert.equal(isStale({ createdAt: '2026-09-05T00:00:00Z' }, NOW, 30), false);
  assert.equal(isStale({ createdAt: '2026-09-04T00:00:00Z' }, NOW, 30), true);
  assert.equal(isStale({ createdAt: '2026-10-04T00:00:00Z' }, NOW, 30), false);
});

test('extractPath prefers the marker and falls back to the File line', () => {
  assert.equal(
    extractPath('<!-- kody-defer thread=PRRT_x root=5 path=scripts/foo.sh -->\n**File:** `other.sh`'),
    'scripts/foo.sh',
  );
  assert.equal(extractPath('## File\n\n**File:** `scripts/pm2-ecosystem.config.cjs`\n'), 'scripts/pm2-ecosystem.config.cjs');
  assert.equal(extractPath('no path here'), null);
  assert.equal(extractPath(''), null);
});

test('renderMarkdown has the summary, table, links, and stale marker', () => {
  const items = rankIssues([
    {
      repo: 'Simple-With-Us/AI-Fleet-Coordinator',
      number: 12,
      url: 'https://github.com/Simple-With-Us/AI-Fleet-Coordinator/issues/12',
      severity: 'high',
      createdAt: '2026-08-26T00:00:00Z',
      touchedSince: true,
      touchCount: 2,
      lastSha: 'abc1234deadbeef',
      path: 'scripts/foo.sh',
    },
    {
      repo: 'Simple-With-Us/Clutch',
      number: 4,
      url: 'https://github.com/Simple-With-Us/Clutch/issues/4',
      severity: 'low',
      createdAt: '2026-10-01T00:00:00Z',
      touchedSince: false,
      touchCount: 0,
      path: 'web/app.js',
    },
  ]);
  const md = renderMarkdown(items, { now: NOW, staleDays: 30, skipped: [{ repo: 'Simple-With-Us/CodeCaps', reason: 'label kody-deferred not found' }] });
  assert.match(md, /^# Kody deferred findings/);
  assert.match(md, /Total: 2\. {2}Touched since defer: 1\. {2}Stale >30d: 1\./);
  assert.match(md, /\| # \| Repo \| Issue \| Severity \| Age \(d\) \| File touched since \| Stale \| File \|/);
  assert.match(md, /\[#12\]\(https:\/\/github\.com\/Simple-With-Us\/AI-Fleet-Coordinator\/issues\/12\)/);
  assert.match(md, /yes \(2 commits, last abc1234\)/);
  assert.match(md, /STALE >30d/);
  assert.match(md, /`scripts\/foo\.sh`/);
  assert.match(md, /\| no \| {2}\| `web\/app\.js` \|/);
  assert.match(md, /\| Simple-With-Us\/CodeCaps \| label kody-deferred not found \|/);
  const json = JSON.parse(renderJson(items, { now: NOW, staleDays: 30, skipped: [] }));
  assert.equal(json.total, 2);
  assert.equal(json.touched, 1);
  assert.equal(json.stale, 1);
  assert.equal(json.issues[0].stale, true);
  assert.equal(json.issues[0].ageDays, 40);
  assert.equal(Date.parse(json.generatedAt), Date.parse(NOW));
});

function jsonResponse(status, data, headers = {}) {
  const bag = new Map(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => bag.get(String(name).toLowerCase()) ?? null },
    async text() { return JSON.stringify(data); },
  };
}

test('sweep ranks a fake API and skips missing repos and labels', async () => {
  const calls = [];
  const afc = 'Simple-With-Us/AI-Fleet-Coordinator';
  const oldBody = [
    '## File',
    '',
    '**File:** `scripts/a.sh`',
    '',
    '<!-- kody-defer thread=PRRT_old root=1 path=scripts/a.sh -->',
  ].join('\n');
  const freshBody = '<!-- kody-defer thread=PRRT_new root=2 path=scripts/b.sh -->';
  const pages = {
    [`https://api.github.com/repos/${afc}/issues?state=open&labels=kody-deferred&per_page=100`]: {
      status: 200,
      data: [
        {
          number: 10,
          title: 'old high',
          html_url: `https://github.com/${afc}/issues/10`,
          created_at: '2026-08-26T00:00:00Z',
          body: oldBody,
          labels: [{ name: 'kody-deferred' }, { name: 'kody-high' }],
        },
        {
          number: 99,
          title: 'a pull request',
          pull_request: { url: 'https://api.github.com/repos/x/pulls/99' },
          created_at: '2026-08-01T00:00:00Z',
          body: oldBody,
          labels: [{ name: 'kody-deferred' }, { name: 'kody-critical' }],
        },
      ],
      headers: {
        link: `<https://api.github.com/repos/${afc}/issues?state=open&labels=kody-deferred&per_page=100&page=2>; rel="next"`,
      },
    },
    [`https://api.github.com/repos/${afc}/issues?state=open&labels=kody-deferred&per_page=100&page=2`]: {
      status: 200,
      data: [
        {
          number: 11,
          title: 'fresh low',
          html_url: `https://github.com/${afc}/issues/11`,
          created_at: '2026-10-03T00:00:00Z',
          body: freshBody,
          labels: [{ name: 'kody-low' }, { name: 'kody-deferred' }],
        },
      ],
    },
  };

  const fetchImpl = async (url) => {
    calls.push(url);
    const u = new URL(url);
    const key = u.origin + u.pathname + u.search;
    if (u.pathname === '/repos/Simple-With-Us/Missing') {
      return jsonResponse(404, { message: 'Not Found' });
    }
    if (u.pathname === '/repos/Simple-With-Us/CodeCaps') {
      return jsonResponse(200, { default_branch: 'main' });
    }
    if (u.pathname === '/repos/Simple-With-Us/CodeCaps/labels/kody-deferred') {
      return jsonResponse(404, { message: 'Not Found' });
    }
    if (u.pathname === `/repos/${afc}` && !u.pathname.includes('/issues') && !u.pathname.includes('/commits') && !u.pathname.includes('/labels')) {
      return jsonResponse(200, { default_branch: 'main', full_name: afc });
    }
    if (u.pathname === `/repos/${afc}/labels/kody-deferred`) {
      return jsonResponse(200, { name: 'kody-deferred' });
    }
    if (pages[key]) return jsonResponse(pages[key].status, pages[key].data, pages[key].headers || {});
    if (u.pathname === `/repos/${afc}/commits`) {
      const path = u.searchParams.get('path');
      if (path === 'scripts/a.sh') {
        return jsonResponse(200, [
          { sha: 'abcdef1234567890', commit: { committer: { date: '2026-09-01T00:00:00Z' } } },
          { sha: '1111111111111111', commit: { committer: { date: '2026-08-27T00:00:00Z' } } },
        ]);
      }
      if (path === 'scripts/b.sh') return jsonResponse(200, []);
    }
    return jsonResponse(500, { message: `unhandled ${url}` });
  };

  const api = {
    apiBase: 'https://api.github.com',
    async getRaw(url) {
      const res = await fetchImpl(url);
      const text = await res.text();
      const data = JSON.parse(text);
      if (!res.ok) {
        const err = new Error(data.message || 'failed');
        err.status = res.status;
        throw err;
      }
      return { data, headers: res.headers, status: res.status };
    },
    async get(url) {
      return (await this.getRaw(url)).data;
    },
  };

  const result = await sweep({
    repos: [afc, 'Simple-With-Us/Missing', 'Simple-With-Us/CodeCaps'],
    api,
    now: NOW,
    staleDays: 30,
    log() {},
  });

  assert.deepEqual(result.items.map((item) => item.number), [10, 11]);
  assert.equal(result.items[0].touchedSince, true);
  assert.equal(result.items[0].touchCount, 2);
  assert.equal(result.items[0].lastSha, 'abcdef1234567890');
  assert.equal(result.items[0].severity, 'high');
  assert.equal(result.items[0].path, 'scripts/a.sh');
  assert.equal(result.items[1].touchedSince, false);
  assert.equal(result.items[1].severity, 'low');
  assert.deepEqual(result.skipped.map((row) => row.reason), [
    'repository not found',
    'label kody-deferred not found',
  ]);
  const md = renderMarkdown(result.items, { now: NOW, skipped: result.skipped, staleDays: 30 });
  assert.match(md, /yes \(2 commits, last abcdef1\)/);
  assert.match(md, /STALE >30d/);
  assert.equal(calls.some((url) => url.includes('pulls/99')), false);
});
