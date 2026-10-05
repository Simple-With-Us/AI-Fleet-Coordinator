// List open kody-deferred issues and rank the ones whose file has moved.
//   node scripts/kody-deferred-sweep.mjs [--repos a,b] [--json] [--stale-days 30] [--owner Simple-With-Us]

import { execFileSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { parseArgs } from 'node:util';

export const DEFAULT_REPOS = [
  'Socratic-Trade',
  'Congress.Trade',
  'Usage-Monitor',
  'CodeCaps',
  'BotFleet',
  'HogHunter',
  'AI-Fleet-Coordinator',
  'Clutch',
];

const SEVERITY_RANK = { critical: 0, high: 1, medium: 2, low: 3 };

export class HttpError extends Error {
  constructor(message, status, data) {
    super(message);
    this.name = 'HttpError';
    this.status = status;
    this.data = data;
  }
}

export function qualifyRepo(name, owner = 'Simple-With-Us') {
  const trimmed = String(name || '').trim();
  if (!trimmed) return '';
  if (trimmed.includes('/')) return trimmed;
  return `${owner}/${trimmed}`;
}

export function resolveRepoList(raw, owner = 'Simple-With-Us') {
  return String(raw || '')
    .split(',')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((name) => qualifyRepo(name, owner));
}

/** Path from the hidden marker, else a `**File:**` backtick. */
export function extractPath(body) {
  const text = String(body || '');
  const marker = text.match(/<!--\s*kody-defer\b([\s\S]*?)-->/);
  if (marker) {
    const found = marker[1].match(/(?:^|\s)path=(\S+)/);
    if (found) return found[1];
  }
  const file = text.match(/\*\*File:\*\*\s*`([^`]+)`/);
  return file ? file[1] : null;
}

export function severityFromLabels(labels) {
  const names = (labels || []).map((label) => (typeof label === 'string' ? label : label?.name || ''));
  for (const severity of ['critical', 'high', 'medium', 'low']) {
    if (names.includes(`kody-${severity}`)) return severity;
  }
  return null;
}

export function ageDays(created, now) {
  const start = new Date(created).getTime();
  const end = new Date(now).getTime();
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 0;
  return Math.floor((end - start) / 86400000);
}

/** Stale when the issue is older than `days` (default 30). */
export function isStale(item, now, days = 30) {
  const created = item?.createdAt || item?.created_at || item?.created;
  return ageDays(created, now) > days;
}

/**
 * Touched-since first, then severity (critical > high > medium > low > none),
 * then oldest created, then repo and number.
 */
export function rankIssues(items) {
  return [...(items || [])].sort((a, b) => {
    const touched = (a.touchedSince ? 0 : 1) - (b.touchedSince ? 0 : 1);
    if (touched) return touched;
    const severity = (SEVERITY_RANK[a.severity] ?? 4) - (SEVERITY_RANK[b.severity] ?? 4);
    if (severity) return severity;
    const created = (Date.parse(a.createdAt) || 0) - (Date.parse(b.createdAt) || 0);
    if (created) return created;
    if (a.repo !== b.repo) return a.repo < b.repo ? -1 : 1;
    return (a.number || 0) - (b.number || 0);
  });
}

function sha7(sha) {
  return String(sha || '').slice(0, 7);
}

function touchedCell(item) {
  if (!item.touchedSince) return 'no';
  const count = item.touchCount || 0;
  return `yes (${count} commits, last ${sha7(item.lastSha)})`;
}

function issueLink(item) {
  const url = item.url || `https://github.com/${item.repo}/issues/${item.number}`;
  return `[#${item.number}](${url})`;
}

export function renderMarkdown(items, { now = new Date(), skipped = [], staleDays = 30 } = {}) {
  const list = items || [];
  const touched = list.filter((item) => item.touchedSince).length;
  const stale = list.filter((item) => isStale(item, now, staleDays)).length;
  const lines = [
    '# Kody deferred findings',
    '',
    `Total: ${list.length}.  Touched since defer: ${touched}.  Stale >${staleDays}d: ${stale}.`,
    '',
    '| # | Repo | Issue | Severity | Age (d) | File touched since | Stale | File |',
    '|---|---|---|---|---|---|---|---|',
  ];
  list.forEach((item, index) => {
    const age = ageDays(item.createdAt, now);
    const staleMark = isStale(item, now, staleDays) ? `STALE >${staleDays}d` : '';
    const file = item.path ? `\`${item.path}\`` : '';
    lines.push(
      `| ${index + 1} | ${item.repo} | ${issueLink(item)} | ${item.severity || ''} | ${age} | ${touchedCell(item)} | ${staleMark} | ${file} |`,
    );
  });
  if (skipped && skipped.length) {
    lines.push('', '## Skipped', '', '| Repo | Reason |', '|---|---|');
    for (const row of skipped) {
      lines.push(`| ${row.repo} | ${row.reason} |`);
    }
  }
  lines.push('');
  return lines.join('\n');
}

export function renderJson(items, { now = new Date(), skipped = [], staleDays = 30 } = {}) {
  const list = items || [];
  return JSON.stringify({
    generatedAt: new Date(now).toISOString(),
    total: list.length,
    touched: list.filter((item) => item.touchedSince).length,
    stale: list.filter((item) => isStale(item, now, staleDays)).length,
    staleDays,
    skipped,
    issues: list.map((item) => ({
      repo: item.repo,
      number: item.number,
      title: item.title || '',
      url: item.url || '',
      severity: item.severity || null,
      createdAt: item.createdAt,
      ageDays: ageDays(item.createdAt, now),
      stale: isStale(item, now, staleDays),
      path: item.path || null,
      touchedSince: Boolean(item.touchedSince),
      touchCount: item.touchCount || 0,
      lastSha: item.lastSha || null,
      lastDate: item.lastDate || null,
    })),
  }, null, 2);
}

function nextLink(headers) {
  const link = headers?.get?.('link') || headers?.get?.('Link') || '';
  const match = String(link).match(/<([^>]+)>;\s*rel="next"/);
  return match ? match[1] : null;
}

export function createGitHubApi({ token, fetchImpl = globalThis.fetch, apiBase = 'https://api.github.com' } = {}) {
  const base = String(apiBase || 'https://api.github.com').replace(/\/$/, '');
  async function getRaw(urlOrPath) {
    const url = urlOrPath.startsWith('http') ? urlOrPath : `${base}${urlOrPath.startsWith('/') ? '' : '/'}${urlOrPath}`;
    const headers = {
      Accept: 'application/vnd.github+json',
      'User-Agent': 'kody-deferred-sweep',
      'X-GitHub-Api-Version': '2022-11-28',
    };
    if (token) headers.Authorization = `Bearer ${token}`;
    let res;
    try {
      res = await fetchImpl(url, { method: 'GET', headers });
    } catch (err) {
      throw new HttpError(`network error GET ${url}: ${err.message}`, 0, null);
    }
    const text = await res.text();
    let data = null;
    if (text) {
      try {
        data = JSON.parse(text);
      } catch {
        data = { raw: text.slice(0, 500) };
      }
    }
    if (!res.ok) {
      const detail = data && data.message ? data.message : text.slice(0, 300);
      throw new HttpError(`GET ${url} failed (${res.status}): ${detail}`, res.status, data);
    }
    return { status: res.status, data, headers: res.headers || { get() { return null; } } };
  }
  return {
    apiBase: base,
    getRaw,
    async get(url) { return (await getRaw(url)).data; },
  };
}

async function listPages(api, url) {
  const out = [];
  let next = url;
  while (next) {
    const res = await api.getRaw(next);
    if (Array.isArray(res.data)) out.push(...res.data);
    next = nextLink(res.headers);
  }
  return out;
}

function apiBaseFrom(api) {
  return api.apiBase || 'https://api.github.com';
}

async function commitsSince(api, repo, branch, path, since) {
  const base = apiBaseFrom(api);
  const url = `${base}/repos/${repo}/commits?sha=${encodeURIComponent(branch)}&path=${encodeURIComponent(path)}&since=${encodeURIComponent(since)}&per_page=100`;
  try {
    const data = await api.get(url);
    const commits = Array.isArray(data) ? data : [];
    const latest = commits[0];
    return {
      touchedSince: commits.length > 0,
      touchCount: commits.length,
      lastSha: latest?.sha || null,
      lastDate: latest?.commit?.committer?.date || latest?.commit?.author?.date || null,
    };
  } catch (err) {
    if (err.status === 404) {
      return { touchedSince: false, touchCount: 0, lastSha: null, lastDate: null };
    }
    throw err;
  }
}

async function sweepRepo(repo, api) {
  const base = apiBaseFrom(api);
  let meta;
  try {
    meta = await api.get(`${base}/repos/${repo}`);
  } catch (err) {
    if (err.status === 404) return { skipped: true, reason: 'repository not found' };
    throw err;
  }
  try {
    await api.get(`${base}/repos/${repo}/labels/${encodeURIComponent('kody-deferred')}`);
  } catch (err) {
    if (err.status === 404) return { skipped: true, reason: 'label kody-deferred not found' };
    throw err;
  }
  const branch = meta.default_branch || 'main';
  const issues = await listPages(
    api,
    `${base}/repos/${repo}/issues?state=open&labels=${encodeURIComponent('kody-deferred')}&per_page=100`,
  );
  const items = [];
  for (const issue of issues) {
    if (issue.pull_request) continue;
    const path = extractPath(issue.body || '');
    const severity = severityFromLabels(issue.labels);
    let touch = { touchedSince: false, touchCount: 0, lastSha: null, lastDate: null };
    if (path) {
      touch = await commitsSince(api, repo, branch, path, issue.created_at);
    }
    items.push({
      repo,
      number: issue.number,
      title: issue.title || '',
      url: issue.html_url || `https://github.com/${repo}/issues/${issue.number}`,
      severity,
      createdAt: issue.created_at,
      path,
      ...touch,
    });
  }
  return { skipped: false, issues: items };
}

/**
 * @param {{repos: string[], api: {get: Function, getRaw: Function, apiBase?: string}, now?: Date|string|number, staleDays?: number, log?: Function}} opts
 */
export async function sweep({ repos, api, now = new Date(), staleDays = 30, log = () => {} } = {}) {
  const skipped = [];
  const items = [];
  for (const repo of repos || []) {
    try {
      const result = await sweepRepo(repo, api);
      if (result.skipped) {
        log(`skip ${repo}: ${result.reason}`);
        skipped.push({ repo, reason: result.reason });
      } else {
        items.push(...result.issues);
      }
    } catch (err) {
      if (err.status === 404) {
        skipped.push({ repo, reason: 'repository not found' });
        continue;
      }
      throw err;
    }
  }
  return {
    items: rankIssues(items),
    skipped,
    now,
    staleDays,
  };
}

export function tokenFromEnv(env = process.env) {
  if (env.GITHUB_TOKEN) return env.GITHUB_TOKEN;
  if (env.GH_TOKEN) return env.GH_TOKEN;
  try {
    const out = execFileSync('gh', ['auth', 'token'], {
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'pipe'],
    }).trim();
    if (out) return out;
  } catch {
    // gh missing or logged out
  }
  throw new Error('No GitHub token.  Set GITHUB_TOKEN or GH_TOKEN, or authenticate with `gh auth login`.');
}

function invokedAsMain() {
  const entry = process.argv[1];
  if (!entry) return false;
  try {
    return import.meta.url === pathToFileURL(entry).href;
  } catch {
    return false;
  }
}

export async function main(argv = process.argv.slice(2), env = process.env, stdout = console.log, stderr = console.error) {
  const { values } = parseArgs({
    args: argv,
    options: {
      repos: { type: 'string' },
      json: { type: 'boolean', default: false },
      'stale-days': { type: 'string', default: '30' },
      owner: { type: 'string', default: 'Simple-With-Us' },
    },
    strict: true,
  });
  const owner = values.owner || 'Simple-With-Us';
  const raw = values.repos || env.KODY_SWEEP_REPOS || DEFAULT_REPOS.join(',');
  const repos = resolveRepoList(raw, owner);
  const staleDays = Number(values['stale-days'] || '30');
  const token = tokenFromEnv(env);
  const api = createGitHubApi({
    token,
    apiBase: env.GITHUB_API_URL || 'https://api.github.com',
  });
  api.apiBase = String(env.GITHUB_API_URL || 'https://api.github.com').replace(/\/$/, '');
  const now = new Date();
  const result = await sweep({ repos, api, now, staleDays, log: stderr });
  if (values.json) stdout(renderJson(result.items, { now, skipped: result.skipped, staleDays }));
  else stdout(renderMarkdown(result.items, { now, skipped: result.skipped, staleDays }));
  return result;
}

if (invokedAsMain()) {
  main().catch((err) => {
    console.error(err && err.message ? err.message : String(err));
    process.exit(1);
  });
}
