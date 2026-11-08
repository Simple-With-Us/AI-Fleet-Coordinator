// GitHub I/O for the Kody first-review prompter. All decisions live in lib.mjs.
// Runs inside a per-repo caller workflow with that repo's own GITHUB_TOKEN, so it
// can only read and comment on its own repository: no cross-repo credential exists.
//
// Env:
//   GITHUB_TOKEN  required, provided by the workflow (permissions: issues: write)
//   GITHUB_REPOSITORY  owner/name, set by Actions
//   DRY_RUN  'true' to list decisions without commenting
import { decide, TRIGGER_TEXT } from './lib.mjs';

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
    throw new Error(`${options.method || 'GET'} ${path} -> ${res.status}`);
  }
  return res.status === 204 ? null : res.json();
}

// Every open PR, drafts included (owner asked for drafts too). 100 covers this fleet
// with headroom; pagination here is deliberate, not an afterthought.
async function listOpenPrs() {
  const prs = [];
  for (let page = 1; page <= 5; page += 1) {
    const batch = await api(`/repos/${repo}/pulls?state=open&per_page=100&page=${page}`);
    prs.push(...batch);
    if (batch.length < 100) break;
  }
  return prs;
}

const prs = await listOpenPrs();
let prompted = 0;
for (const pr of prs) {
  const [issueComments, reviews] = await Promise.all([
    api(`/repos/${repo}/issues/${pr.number}/comments?per_page=100`),
    api(`/repos/${repo}/pulls/${pr.number}/reviews?per_page=100`),
  ]);
  const verdict = decide(pr, { issueComments, reviews, now: Date.now() });
  console.log(`#${pr.number} ${pr.draft ? 'draft ' : ''}${verdict}`);
  if (verdict !== 'prompt') continue;
  if (dryRun) continue;
  await api(`/repos/${repo}/issues/${pr.number}/comments`, {
    method: 'POST',
    body: JSON.stringify({ body: TRIGGER_TEXT }),
  });
  prompted += 1;
}
console.log(dryRun ? `dry-run complete, ${prompted} would prompt` : `prompted ${prompted}`);
