// Kody /defer runner.  Reads the Actions event and files a tracking issue.
// Auto-runs only when invoked as the process entrypoint.  Tests call run().

import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import {
  LABELS,
  buildIssueBody,
  buildIssueTitle,
  cleanFindingText,
  hasDeferredReply,
  isAllowedActor,
  isBlockedSeverity,
  manualResolveReply,
  parseDeferCommand,
  parseSeverity,
  refusalMessage,
  stripDeferCommand,
} from './lib.mjs';

export class GitHubError extends Error {
  constructor(message, status, data) {
    super(message);
    this.name = 'GitHubError';
    this.status = status;
    this.data = data;
  }
}

function graphqlEndpoint(env) {
  if (env.GITHUB_GRAPHQL_URL) return String(env.GITHUB_GRAPHQL_URL).replace(/\/$/, '');
  const api = String(env.GITHUB_API_URL || 'https://api.github.com').replace(/\/$/, '');
  if (api.includes('api.github.com')) return 'https://api.github.com/graphql';
  return api.replace(/\/api\/v3$/, '/api/graphql');
}

function isDryRun(env) {
  const v = String(env.KODY_DEFER_DRY_RUN || '').trim().toLowerCase();
  return v === '1' || v === 'true' || v === 'yes';
}

function isWrite(method, body) {
  if (method === 'GET' || method === 'HEAD') return false;
  if (body && typeof body.query === 'string') return /^\s*mutation\b/.test(body.query);
  return true;
}

/**
 * @param {{env: NodeJS.ProcessEnv, api?: Function|{fetch?: Function}, log: Function}} opts
 */
export function createClient({ env, api, log }) {
  const fetchImpl = typeof api === 'function'
    ? api
    : (api && typeof api.fetch === 'function' ? api.fetch.bind(api) : globalThis.fetch);
  const token = env.GITHUB_TOKEN || env.GH_TOKEN || '';
  const apiBase = String(env.GITHUB_API_URL || 'https://api.github.com').replace(/\/$/, '');
  const gql = graphqlEndpoint(env);
  const dry = isDryRun(env);

  async function raw(method, url, body) {
    if (dry && isWrite(method, body)) {
      log(`dry-run ${method} ${url}`);
      if (method === 'POST' && /\/issues$/.test(url)) {
        return { status: 201, data: { number: 0, html_url: '', dryRun: true }, headers: emptyHeaders() };
      }
      if (body && typeof body.query === 'string' && /^\s*mutation\b/.test(body.query)) {
        return {
          status: 200,
          data: { data: { resolveReviewThread: { thread: { isResolved: true } } } },
          headers: emptyHeaders(),
        };
      }
      return { status: 200, data: { dryRun: true }, headers: emptyHeaders() };
    }
    const headers = {
      Accept: 'application/vnd.github+json',
      'User-Agent': 'kody-defer',
      'X-GitHub-Api-Version': '2022-11-28',
    };
    if (token) headers.Authorization = `Bearer ${token}`;
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    let res;
    try {
      res = await fetchImpl(url, {
        method,
        headers,
        body: body !== undefined ? JSON.stringify(body) : undefined,
      });
    } catch (err) {
      throw new GitHubError(`network error ${method} ${url}: ${err.message}`, 0, null);
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
      throw new GitHubError(`${method} ${url} failed (${res.status}): ${detail}`, res.status, data);
    }
    return { status: res.status, data, headers: res.headers || emptyHeaders() };
  }

  function urlFor(path) {
    if (path.startsWith('http://') || path.startsWith('https://')) return path;
    return apiBase + (path.startsWith('/') ? path : `/${path}`);
  }

  return {
    dry,
    apiBase,
    gql,
    async get(path) {
      const { data } = await raw('GET', urlFor(path));
      return data;
    },
    async getRaw(path) {
      return raw('GET', urlFor(path));
    },
    async post(path, body) {
      const { data } = await raw('POST', urlFor(path), body);
      return data;
    },
    async graphql(query, variables) {
      const { data } = await raw('POST', gql, { query, variables });
      if (data && Array.isArray(data.errors) && data.errors.length) {
        const msg = data.errors.map((e) => e.message).filter(Boolean).join('; ');
        throw new GitHubError(`GraphQL error: ${msg || 'request failed'}`, 200, data);
      }
      return data ? data.data : null;
    },
  };
}

function emptyHeaders() {
  return { get() { return null; } };
}

const THREADS_QUERY = `
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id
          isResolved
          comments(first: 100) {
            nodes { databaseId body url createdAt }
          }
        }
      }
    }
  }
}`.trim();

function nextLink(headers) {
  const link = headers?.get?.('link') || headers?.get?.('Link') || '';
  const m = String(link).match(/<([^>]+)>;\s*rel="next"/);
  return m ? m[1] : null;
}

async function findThread(client, owner, repo, prNumber, rootId) {
  let cursor = null;
  const want = Number(rootId);
  do {
    const data = await client.graphql(THREADS_QUERY, {
      owner,
      name: repo,
      number: prNumber,
      cursor,
    });
    const conn = data?.repository?.pullRequest?.reviewThreads;
    const nodes = conn?.nodes || [];
    for (const node of nodes) {
      const comments = node?.comments?.nodes || [];
      if (comments.some((c) => Number(c.databaseId) === want)) return node;
    }
    if (!conn?.pageInfo?.hasNextPage) break;
    cursor = conn.pageInfo.endCursor;
  } while (cursor);
  return null;
}

async function findIssueByThread(client, owner, repo, threadId) {
  let url = `/repos/${owner}/${repo}/issues?state=all&labels=${encodeURIComponent('kody-deferred')}&per_page=100`;
  const needle = `thread=${threadId}`;
  while (url) {
    const res = await client.getRaw(url);
    const list = Array.isArray(res.data) ? res.data : [];
    for (const issue of list) {
      if (issue.pull_request) continue;
      if (typeof issue.body === 'string' && issue.body.includes(needle)) {
        return issue.number;
      }
    }
    url = nextLink(res.headers);
  }
  return null;
}

async function ensureLabel(client, owner, repo, label) {
  const path = `/repos/${owner}/${repo}/labels/${encodeURIComponent(label.name)}`;
  try {
    await client.get(path);
  } catch (err) {
    if (err.status !== 404) throw err;
    try {
      await client.post(`/repos/${owner}/${repo}/labels`, {
        name: label.name,
        color: label.color,
        description: label.description || '',
      });
    } catch (createErr) {
      if (createErr.status !== 422) throw createErr;
    }
  }
}

function reply(client, owner, repo, prNumber, rootId, body) {
  return client.post(
    `/repos/${owner}/${repo}/pulls/${prNumber}/comments/${rootId}/replies`,
    { body },
  );
}

async function resolveOrManual(client, owner, repo, prNumber, rootId, threadId, issueNumber, log) {
  try {
    const data = await client.graphql(
      `mutation($id: ID!) {
        resolveReviewThread(input: { threadId: $id }) {
          thread { isResolved }
        }
      }`,
      { id: threadId },
    );
    const resolved = data?.resolveReviewThread?.thread?.isResolved;
    if (!resolved) throw new GitHubError('resolveReviewThread returned an unresolved thread', 200, data);
    log(`resolved thread for #${issueNumber}`);
  } catch (err) {
    const short = String(err.message || err).replace(/\s+/g, ' ').slice(0, 180);
    log(`resolve failed: ${short}`);
    await reply(client, owner, repo, prNumber, rootId, manualResolveReply(issueNumber, short));
  }
}

function eventIsReviewComment(event, env) {
  const name = env.GITHUB_EVENT_NAME;
  if (name && name !== 'pull_request_review_comment') return false;
  if (!event || event.action !== 'created') return false;
  if (!event.comment || !event.pull_request) return false;
  return true;
}

async function loadRoot(client, owner, repo, comment) {
  let root = {
    id: comment.id,
    body: comment.body || '',
    path: comment.path,
    line: comment.line,
    start_line: comment.start_line,
    original_line: comment.original_line,
    html_url: comment.html_url,
    in_reply_to_id: comment.in_reply_to_id,
    user: comment.user,
    created_at: comment.created_at,
  };
  const seen = new Set();
  while (root.in_reply_to_id && !seen.has(root.id)) {
    seen.add(root.id);
    const parent = await client.get(
      `/repos/${owner}/${repo}/pulls/comments/${root.in_reply_to_id}`,
    );
    root = parent;
  }
  return root;
}

/**
 * Run the defer flow.
 * @param {{event?: object, api?: Function|{fetch?: Function}, env?: NodeJS.ProcessEnv, log?: Function}} opts
 * @returns {Promise<{code: number, action: string, issue?: number|null, logs: string[]}>}
 */
export async function run({ event, api, env = process.env, log = console.log } = {}) {
  const logs = [];
  const write = (...parts) => {
    const line = parts.join(' ');
    logs.push(line);
    log(line);
  };

  if (!eventIsReviewComment(event, env)) {
    return { code: 0, action: 'ignored', logs };
  }

  const parsed = parseDeferCommand(event.comment.body || '');
  if (!parsed) return { code: 0, action: 'ignored', logs };

  const login = event.comment.user?.login || '';
  const userType = event.comment.user?.type || '';
  if (userType === 'Bot' || (login && login.endsWith('[bot]'))) {
    write(`skip: bot actor ${login || userType}`);
    return { code: 0, action: 'bot', logs };
  }

  const repoFull = env.GITHUB_REPOSITORY || event.repository?.full_name || '';
  if (!repoFull.includes('/')) {
    throw new GitHubError('GITHUB_REPOSITORY must be owner/repo', 0, null);
  }
  const [owner, repo] = repoFull.split('/');
  const prNumber = event.pull_request.number;
  const headSha = event.pull_request.head?.sha || '';
  const prUrl = event.pull_request.html_url
    || `https://github.com/${owner}/${repo}/pull/${prNumber}`;
  const client = createClient({ env, api, log: write });

  let permission = '';
  try {
    const data = await client.get(
      `/repos/${owner}/${repo}/collaborators/${encodeURIComponent(login)}/permission`,
    );
    permission = data?.permission || '';
  } catch (err) {
    if (err.status === 404) {
      write(`skip: ${login} is not a collaborator`);
      return { code: 0, action: 'not-collaborator', logs };
    }
    throw err;
  }
  if (!isAllowedActor({ userType, login, permission })) {
    write(`skip: ${login} has permission ${permission || 'none'}; need admin, maintain, or write`);
    return { code: 0, action: 'not-collaborator', logs };
  }

  const triggerId = event.comment.id;
  const root = await loadRoot(client, owner, repo, event.comment);
  const rootIsTrigger = Number(root.id) === Number(triggerId);
  const rawFinding = rootIsTrigger ? stripDeferCommand(root.body || '') : (root.body || '');
  const finding = cleanFindingText(rawFinding);
  const severity = parseSeverity(root.body || '');

  const thread = await findThread(client, owner, repo, prNumber, root.id);
  if (!thread) {
    throw new GitHubError(
      `review thread containing comment ${root.id} was not found on PR #${prNumber}`,
      0,
      null,
    );
  }

  const comments = (thread.comments?.nodes || []).map((c) => ({
    id: c.databaseId,
    body: c.body || '',
    url: c.url,
    createdAt: c.createdAt,
  }));

  const already = hasDeferredReply(comments);
  if (already) {
    write(`skip: thread already deferred to #${already}`);
    return { code: 0, action: 'already-deferred', issue: already, logs };
  }

  const prior = await findIssueByThread(client, owner, repo, thread.id);
  if (prior) {
    write(`found existing issue #${prior} for this thread; linking instead of creating`);
    await reply(client, owner, repo, prNumber, root.id, `Deferred to #${prior}.`);
    await resolveOrManual(client, owner, repo, prNumber, root.id, thread.id, prior, write);
    return { code: 0, action: 'relinked', issue: prior, logs };
  }

  const blockedCsv = env.KODY_DEFER_BLOCKED_SEVERITIES ?? 'critical';
  if (isBlockedSeverity(severity, blockedCsv)) {
    const msg = refusalMessage(severity || 'critical');
    const deferAt = event.comment.created_at || event.comment.createdAt || '';
    const duplicate = comments.some((c) => {
      if (String(c.body).trim() !== msg) return false;
      if (!deferAt || !c.createdAt) return true;
      return c.createdAt > deferAt;
    });
    if (duplicate) {
      write('skip: refusal already posted');
      return { code: 0, action: 'refused-duplicate', logs };
    }
    await reply(client, owner, repo, prNumber, root.id, msg);
    write(`refused: severity ${severity} is blocked (${blockedCsv})`);
    return { code: 0, action: 'refused', logs };
  }

  const labels = [LABELS.deferred];
  if (severity && LABELS[severity]) labels.push(LABELS[severity]);
  for (const label of labels) {
    await ensureLabel(client, owner, repo, label);
  }

  const path = root.path || event.comment.path || '';
  const line = root.line ?? root.original_line ?? event.comment.line ?? event.comment.original_line ?? null;
  const startLine = root.start_line ?? root.original_start_line ?? event.comment.start_line ?? null;
  const title = buildIssueTitle(finding || rawFinding || 'Kody finding');
  const body = buildIssueBody({
    finding,
    path,
    line,
    startLine,
    prUrl,
    prNumber,
    commentUrl: root.html_url || event.comment.html_url || '',
    deferCommentUrl: event.comment.html_url || '',
    reason: parsed.reason,
    headSha,
    severity,
    deferredBy: login,
    threadId: thread.id,
    rootCommentId: root.id,
    repo: `${owner}/${repo}`,
  });

  const issue = await client.post(`/repos/${owner}/${repo}/issues`, {
    title,
    body,
    labels: labels.map((l) => l.name),
  });
  const number = issue.number;
  write(`opened issue #${number}`);
  await reply(client, owner, repo, prNumber, root.id, `Deferred to #${number}.`);
  await resolveOrManual(client, owner, repo, prNumber, root.id, thread.id, number, write);
  return { code: 0, action: 'deferred', issue: number, logs };
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

if (invokedAsMain()) {
  try {
    const eventPath = process.env.GITHUB_EVENT_PATH;
    if (!eventPath) {
      console.error('GITHUB_EVENT_PATH is not set');
      process.exit(1);
    }
    const event = JSON.parse(readFileSync(eventPath, 'utf8'));
    const result = await run({ event, env: process.env });
    process.exit(result.code ?? 0);
  } catch (err) {
    console.error(err && err.stack ? err.stack : String(err));
    process.exit(1);
  }
}
