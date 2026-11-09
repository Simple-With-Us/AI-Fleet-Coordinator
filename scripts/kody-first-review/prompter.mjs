// GitHub I/O for the Kody first-review prompter. All decisions live in lib.mjs.
// Runs inside a per-repo caller workflow with that repo's own GITHUB_TOKEN, so it
// can only read and comment on its own repository: no cross-repo credential exists.
//
// Env:
//   GITHUB_TOKEN  required, provided by the workflow (permissions: issues: write)
//   GITHUB_REPOSITORY  owner/name, set by Actions
//   DRY_RUN  'true' to list decisions without commenting
import { decide, describeApiError, TRIGGER_TEXT } from './lib.mjs';

const API = 'https://api.github.com';
const token = process.env.GITHUB_TOKEN;
const repo = process.env.GITHUB_REPOSITORY;
const dryRun = String(process.env.DRY_RUN || '').toLowerCase() === 'true';

if (!token || !repo) {
  console.error('GITHUB_TOKEN and GITHUB_REPOSITORY are required');
  process.exit(1);
}

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      authorization: `Bearer ${token}`,
      accept: 'application/vnd.github+json',
      'x-github-api-version': '2022-11-28',
      ...(options.headers || {}),
    },
  });
  if (!res.ok) {
    const bodyText = await res.text().catch(() => '');
    throw new Error(
      describeApiError({ method: options.method || 'GET', path, status: res.status, headers: res.headers, bodyText }),
    );
  }
  return res.status === 204 ? null : res.json();
}

// Every page of a list endpoint. The cap is a runaway guard, far above any real PR.
async function listAll(path, maxPages = 20) {
  const items = [];
  const sep = path.includes('?') ? '&' : '?';
  for (let page = 1; page <= maxPages; page += 1) {
    const batch = await api(`${path}${sep}per_page=100&page=${page}`);
    items.push(...batch);
    if (batch.length < 100) break;
  }
  return items;
}

// Every open PR, drafts included (owner asked for drafts too).
const listOpenPrs = () => listAll(`/repos/${repo}/pulls?state=open`, 5);

// One PR: fetch every comment and review page, decide, and post if needed. Returns
// true when it prompted (or would have, in a dry run).
async function handle(pr) {
  const [issueComments, reviews] = await Promise.all([
    listAll(`/repos/${repo}/issues/${pr.number}/comments`),
    listAll(`/repos/${repo}/pulls/${pr.number}/reviews`),
  ]);
  const verdict = decide(pr, { issueComments, reviews, now: Date.now() });
  console.log(`#${pr.number} ${pr.draft ? 'draft ' : ''}${verdict}`);
  if (verdict !== 'prompt') return false;
  if (!dryRun) {
    await api(`/repos/${repo}/issues/${pr.number}/comments`, {
      method: 'POST',
      body: JSON.stringify({ body: TRIGGER_TEXT }),
    });
  }
  return true;
}

// A few PRs at a time keeps the run well inside the job timeout without bursting the
// API. One PR's failure is logged and the sweep moves on; the run still exits non-zero
// so a failing tick is visible in Actions.
const CONCURRENCY = 4;
const prs = await listOpenPrs();
let prompted = 0;
let failed = 0;
let next = 0;
async function worker() {
  while (next < prs.length) {
    const pr = prs[next];
    next += 1;
    try {
      if (await handle(pr)) prompted += 1;
    } catch (error) {
      failed += 1;
      console.error(`#${pr.number} error: ${error.message}`);
    }
  }
}
await Promise.all(Array.from({ length: Math.min(CONCURRENCY, prs.length) }, worker));
console.log(dryRun ? `dry-run complete, ${prompted} would prompt` : `prompted ${prompted}`);
if (failed > 0) {
  console.error(`${failed} PR(s) failed`);
  process.exitCode = 1;
}
