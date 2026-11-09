// GitHub I/O for the Kody first-review prompter. All decisions live in lib.mjs.
// Runs inside a per-repo caller workflow with that repo's own GITHUB_TOKEN, so it
// can only read and comment on its own repository: no cross-repo credential exists.
//
// Env:
//   GITHUB_TOKEN  required, provided by the workflow (permissions: issues: write)
//   GITHUB_REPOSITORY  owner/name, set by Actions
//   DRY_RUN  'true' to list decisions without commenting
import { classifyHttpError, decide, safeErrorDetails, SAFE_ERROR_HEADERS, sweepPrompts, TRIGGER_TEXT } from './lib.mjs';

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
    // Status plus allowlisted response headers only; the body is never read.
    const headers = {};
    for (const name of SAFE_ERROR_HEADERS) {
      const value = res.headers.get(name);
      if (value != null) headers[name] = value;
    }
    const error = new Error(`${options.method || 'GET'} ${path} -> ${safeErrorDetails(res.status, headers)}`);
    error.status = res.status;
    error.classification = classifyHttpError(res.status, headers);
    throw error;
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

// One PR: fetch every comment and review page and return the decision (logged).
async function verdictFor(pr) {
  const [issueComments, reviews] = await Promise.all([
    listAll(`/repos/${repo}/issues/${pr.number}/comments`),
    listAll(`/repos/${repo}/pulls/${pr.number}/reviews`),
  ]);
  const verdict = decide(pr, { issueComments, reviews, now: Date.now() });
  console.log(`#${pr.number} ${pr.draft ? 'draft ' : ''}${verdict}`);
  return verdict;
}

async function postTrigger(pr) {
  if (!dryRun) {
    await api(`/repos/${repo}/issues/${pr.number}/comments`, {
      method: 'POST',
      body: JSON.stringify({ body: TRIGGER_TEXT }),
    });
  }
}

// A few PRs at a time keeps the run well inside the job timeout without bursting the
// API. One PR's failure is logged and the sweep moves on; a classified terminal
// failure blocks the remaining prompt POSTs instead of repeating identical refused
// writes. Blocked prompts keep the run non-zero with the count explicit, so
// uncompleted obligations stay visible in Actions.
const CONCURRENCY = 4;
const prs = await listOpenPrs();
const { prompted, failed, blocked, unprocessed, terminalClassification } = await sweepPrompts(prs, {
  concurrency: CONCURRENCY,
  log: (...args) => console.log(...args),
  logError: (...args) => console.error(...args),
  verdictFor,
  postTrigger,
});
console.log(dryRun ? `dry-run complete, ${prompted} would prompt` : `prompted ${prompted}`);
if (blocked > 0 || unprocessed > 0) {
  console.error(`${blocked} prompt(s) blocked and ${unprocessed} PR(s) unprocessed after terminal ${terminalClassification} failure; obligations NOT completed this run`);
}
if (failed > 0) {
  console.error(`${failed} PR(s) failed`);
}
if (failed > 0 || blocked > 0 || unprocessed > 0) {
  process.exitCode = 1;
}
