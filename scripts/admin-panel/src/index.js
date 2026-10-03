/**
 * Fleet admin panel — https://admin.jays.services
 *
 * A single Cloudflare Worker that fans out to every fleet control plane and
 * returns one status document.  Every integration degrades on its own: a
 * missing secret reports "Not configured", a failing call reports its error
 * text, and neither stops the other cards from rendering.
 *
 * Two fields, two meanings, never conflated:
 *   ok     the API call itself worked.  false means we could not ask.
 *   state  what the answer said: up / warn / down / off / pending.
 * A section is allowed to be ok:true state:"down" — that is a healthy check
 * reporting an unhealthy fleet, which is the normal case for a status page.
 *
 * SUBREQUEST BUDGET.  A Worker invocation on the free plan may make 50
 * subrequests.  Probing everything in one invocation blew through that, so the
 * page asks for one section per request and each section states its own
 * worst case below.  `/api/status` still answers: it picks the sections that
 * fit a 45-subrequest budget, oldest-checked first, and defers the rest to the
 * next call, by which time the sections ahead of them are cached and free.
 * Only a section that will really fetch is charged — an unconfigured one costs
 * nothing, so it can never crowd out a check that would have done work.
 *
 * DEADLINES.  The budget bounds how many calls we make, not how long they
 * take, and Cloudflare cuts a request off near 100 s.  So every section is
 * raced against SECTION_DEADLINE_MS and the whole document against
 * STATUS_DEADLINE_MS; a section that loses its race reports warn and says it
 * timed out.  The selected sections run concurrently, so /api/status answers
 * in about one section's deadline rather than the sum of all of them.
 *
 * Routes:
 *   GET /api/status              every section that fits, run concurrently
 *   GET /api/status?section=…    one section, its own 30 s cache
 *   GET /api/recall/search?q=    proxy for the fleet RAG search
 *   *                            static assets from ./public
 */

const UA = 'admin-jays-services/1.0';
const CACHE_TTL_MS = 30_000;
const PROBE_TIMEOUT_MS = 8_000;
const API_TIMEOUT_MS = 12_000;
// Hybrid search plus rerank is genuinely slow — the Mac measures several
// seconds for a warm query and more for a cold one.  8 s was cutting it off.
const RECALL_SEARCH_TIMEOUT_MS = 25_000;
const RECALL_CANARY_LIMIT = 3;
const RECALL_SEARCH_LIMIT = 8;

// How many subrequests one invocation of /api/status may spend.  Below the
// platform's 50 so the response itself and any retry still have room.
const STATUS_BUDGET = 45;

// How long one section may take before the page gives up on it, and how long
// the whole /api/status document may take.  Both sit well under Cloudflare's
// edge limit, and the second is the first plus enough room to assemble the
// JSON.  The page polls every 60 s and disables its refresh button while a
// request is in flight, so a section that hangs must not outlast the poll.
const SECTION_DEADLINE_MS = 25_000;
const STATUS_DEADLINE_MS = 28_000;

// Public surfaces to probe.  Anything behind Cloudflare Access answers with a
// redirect to cloudflareaccess.com or with 401/403, both of which mean the
// service is up and guarding itself.
//
// admin.jays.services is deliberately absent: a Worker probing its own
// hostname spends a subrequest to learn something it already knows, and on
// some routings talks to itself.
const ENDPOINTS = [
  { name: 'Socratic Trade', url: 'https://socratictrade.com' },
  { name: 'Congress.Trade', url: 'https://congress.trade' },
  { name: 'Usage Monitor', url: 'https://usage.jays.services' },
  { name: 'DealDex', url: 'https://dealdex.net' },
  { name: 'Personal Site', url: 'https://jays.services' },
  { name: 'BotFleet', url: 'https://botfleet.app' },
  // autorotate.codes is NXDOMAIN (retired); live product is on Vercel.
  { name: 'Autorotate', url: 'https://autorotate.vercel.app' },
  { name: 'ContactLogo', url: 'https://contactlogo.com' },
  // The gh-pages CNAME is codecaps.simplewithus.com; the github.io path is
  // case-sensitive and the lower-case /codecaps/ spelling 404s.
  { name: 'CodeCaps', url: 'https://codecaps.simplewithus.com/' },
  { name: 'Fleet Activity', url: 'https://simple-with-us.github.io/AI-Fleet-Coordinator/' },
  { name: 'Start Page', url: 'https://start.jays.services' },
  { name: 'The Board', url: 'https://mac.jays.services/board' },
  { name: 'Coolify', url: 'https://host.jays.services' },
  { name: 'Agents Gateway', url: 'https://agents.jays.services/health' },
  // Scout retired 2026-09-09 — DNS removed; do not probe scout.jays.services.
  { name: 'Xcode Bridge', url: 'https://xcode.jays.services/health' },
  { name: 'Agent Sync', url: 'https://agent-sync.jays.services/health' },
  { name: 'Fleet Recall', url: 'https://recall.jays.services/health' },
];

// Mirrors fleet-apps.json at the repo root (apps[].repo / .displayName / .kind).
// The Worker has no filesystem, so the registry is inlined here.  Keep in sync
// when an app is onboarded — scripts/check-fleet-registry.py will not catch
// drift in this copy.
//
// Hog Hunter is a local-only Mac app and CodeCaps is not registered in
// fleet-apps.json yet.  CodeCaps is probed at github.io/codecaps/ (not the
// retired agent-bar/ path).  Autorotate stays in APPS for GitHub; its public
// probe is autorotate.vercel.app — autorotate.codes and Scout are retired.
const APPS = [
  { repo: 'Socratic-Trade', name: 'Socratic Trade', kind: 'product' },
  { repo: 'Congress.Trade', name: 'Congress.Trade', kind: 'product' },
  { repo: 'Usage-Monitor', name: 'Usage Monitor', kind: 'product' },
  { repo: 'congress-trading-shared', name: 'congress-trading-shared', kind: 'library' },
  { repo: 'DealDex', name: 'DealDex.net', kind: 'product' },
  { repo: 'AI-Fleet-Coordinator', name: 'AI Fleet Coordinator', kind: 'infra' },
  { repo: 'Personal-Site', name: 'Personal Site', kind: 'product' },
  { repo: 'Autorotate', name: 'Autorotate.Codes', kind: 'product' },
  { repo: 'ContactLogo', name: 'ContactLogo', kind: 'product' },
  { repo: 'BotFleet', name: 'BotFleet.app', kind: 'product' },
  { repo: 'HogHunter', name: 'Hog Hunter', kind: 'product' },
  { repo: 'Fleet-OPS', name: 'Fleet Ops', kind: 'infra' },
  { repo: 'Clutch', name: 'Clutch', kind: 'infra' },
];

// Vercel personal projects come back on the first call; team-scoped ones need
// one call per team.  Capped so the declared cost below stays honest.
const VERCEL_MAX_TEAMS = 4;

/* ------------------------------------------------------------------ utils */

function isTimeout(err) {
  const name = err && err.name;
  return name === 'TimeoutError' || name === 'AbortError';
}

function errText(err) {
  if (isTimeout(err)) return 'Timed out';
  const msg = (err && err.message) || String(err);
  // workerd reports a connection failure as "internal error; reference = …",
  // which is noise on a status page.
  if (/^internal error/i.test(msg)) return 'Unreachable';
  return msg.length > 200 ? `${msg.slice(0, 200)}…` : msg;
}

function bodySnippet(text) {
  if (!text) return '';
  const flat = text.replace(/\s+/g, ' ').trim();
  if (!flat) return '';
  return ` — ${flat.length > 140 ? `${flat.slice(0, 140)}…` : flat}`;
}

// Hostname of a Location header, resolved against the request URL, with the
// path and query thrown away.  An Access redirect carries the original URL in
// its query string, so printing the whole thing would put our own request —
// tokens in query parameters included — on the status page.
function locationHost(location, requestUrl) {
  if (!location) return 'an unnamed location';
  try {
    return new URL(location, requestUrl).host;
  } catch {
    return 'an unparseable location';
  }
}

async function apiJson(url, { headers = {}, method = 'GET', body, timeoutMs = API_TIMEOUT_MS } = {}) {
  const res = await fetch(url, {
    method,
    body,
    headers: { 'User-Agent': UA, Accept: 'application/json', ...headers },
    // Never follow.  fetch() strips Authorization across origins but keeps
    // custom headers, so a followed hop would hand CF-Access-Client-Id and
    // CF-Access-Client-Secret to whoever the redirect names — and an expired
    // service token redirects every Access-fronted host to cloudflareaccess.com.
    // Each hop is also an uncounted subrequest against the platform's 50.
    redirect: 'manual',
    signal: AbortSignal.timeout(timeoutMs),
  });
  if (res.status >= 300 && res.status < 400) {
    const where = locationHost(res.headers.get('location'), url);
    throw new Error(
      `HTTP ${res.status} redirect to ${where}, not followed`
      + (where.endsWith('cloudflareaccess.com') ? ' — the Access service token looks expired' : ''),
    );
  }
  const text = await res.text();
  if (!res.ok) throw new Error(`HTTP ${res.status}${bodySnippet(text)}`);
  try {
    return JSON.parse(text);
  } catch {
    throw new Error(`HTTP ${res.status} but the response was not JSON`);
  }
}

// Wraps a section so one failure can never take the page down.  A throw here
// is the one case where ok is false and state is down at the same time: we
// could not ask, so we do not know.
async function section(fn) {
  const started = Date.now();
  try {
    const out = await fn();
    return { ms: Date.now() - started, ...out };
  } catch (err) {
    return { ok: false, state: 'down', summary: 'Check failed', error: errText(err), items: [], ms: Date.now() - started };
  }
}

function notConfigured(...names) {
  return {
    ok: false,
    state: 'off',
    summary: 'Not configured',
    error: `Set ${names.join(' and ')} with wrangler secret put.`,
    items: [],
  };
}

function missing(env, ...names) {
  return names.filter((n) => !env[n]);
}

function accessHeaders(env) {
  return {
    'CF-Access-Client-Id': env.CF_ACCESS_CLIENT_ID,
    'CF-Access-Client-Secret': env.CF_ACCESS_CLIENT_SECRET,
  };
}

/* -------------------------------------------------------------- endpoints */
/* Subrequests: one per entry in ENDPOINTS.  Worst case 17.                  */

async function probe(ep) {
  const started = Date.now();
  try {
    const res = await fetch(ep.url, {
      redirect: 'manual',
      headers: { 'User-Agent': UA, Accept: 'text/html,application/json;q=0.9,*/*;q=0.8' },
      signal: AbortSignal.timeout(PROBE_TIMEOUT_MS),
    });
    const ms = Date.now() - started;
    const status = res.status;
    const location = res.headers.get('location') || '';
    if (status >= 200 && status < 300) {
      return { name: ep.name, url: ep.url, state: 'up', status, ms, detail: 'Up' };
    }
    if (status >= 300 && status < 400) {
      const behindAccess = location.includes('cloudflareaccess.com');
      return {
        name: ep.name,
        url: ep.url,
        state: 'up',
        status,
        ms,
        detail: behindAccess ? 'Up, behind Access' : 'Up, redirects',
      };
    }
    // A service that answers 401 or 403 is running and refusing us, which is
    // exactly what every Access-protected and bearer-protected host on this
    // list is supposed to do.  Calling that "down" was the panel lying.
    if (status === 401 || status === 403) {
      return { name: ep.name, url: ep.url, state: 'up', status, ms, detail: 'Up, auth required' };
    }
    return { name: ep.name, url: ep.url, state: 'down', status, ms, detail: `HTTP ${status}` };
  } catch (err) {
    // The tunnel-backed hosts (the Mac's board, the Xcode bridge, agent sync)
    // answer in well under a second from the Mac and time out from
    // Cloudflare's edge.  That is a route we cannot see from here, not a dead
    // service, so it is amber and says where the timeout was measured.
    if (isTimeout(err)) {
      return {
        name: ep.name,
        url: ep.url,
        state: 'warn',
        status: 0,
        ms: Date.now() - started,
        detail: 'Timed out from Cloudflare',
      };
    }
    return {
      name: ep.name,
      url: ep.url,
      state: 'down',
      status: 0,
      ms: Date.now() - started,
      detail: errText(err),
    };
  }
}

async function checkEndpoints() {
  const items = await Promise.all(ENDPOINTS.map(probe));
  const down = items.filter((i) => i.state === 'down').length;
  const warn = items.filter((i) => i.state === 'warn').length;
  const up = items.length - down - warn;

  const parts = [`${up} of ${items.length} up`];
  if (warn) parts.push(`${warn} timed out from Cloudflare`);
  if (down) parts.push(`${down} down`);

  return {
    ok: true,
    state: down ? 'down' : (warn ? 'warn' : 'up'),
    summary: parts.join(', '),
    items,
  };
}

/* ------------------------------------------------------------ fleet recall */
/* Subrequests: health + stats + canary search.  Worst case 3, or 1 when the */
/* search secrets are unset and only the public health probe runs.           */

// Contract lives in scripts/fleet-recall-service/server.py and
// scripts/fleet_rag/recall_api.py:
//   GET  /health         public, no auth
//   GET  /recall/stats   bearer
//   POST /recall/search  bearer, body { query, limit, ... }
// The whole host also sits behind Cloudflare Access, so service-token headers
// ride along on every call including /health.

function recallBase(env) {
  return env.RECALL_BASE || 'https://recall.jays.services';
}

function recallHeaders(env) {
  return {
    ...accessHeaders(env),
    Authorization: `Bearer ${env.RECALL_API_TOKEN}`,
    'Content-Type': 'application/json',
  };
}

// /health is exempt from the bearer check but still behind Access, so send the
// service-token headers when we have them.
function recallPublicHeaders(env) {
  return env.CF_ACCESS_CLIENT_ID && env.CF_ACCESS_CLIENT_SECRET ? accessHeaders(env) : {};
}

// POST, JSON body, and the field really is called `query` — recall_api.py
// reads body["query"], not "q" and not "text".
//
// Deliberately not exported.  A named export on the Worker module is callable
// over RPC by anything given a service binding to us, which would reach the
// corpus without passing the route's configuration check.  Nothing imports it.
async function recallSearch(env, query, limit = RECALL_SEARCH_LIMIT) {
  const data = await apiJson(`${recallBase(env)}/recall/search`, {
    method: 'POST',
    headers: recallHeaders(env),
    body: JSON.stringify({ query, limit }),
    timeoutMs: RECALL_SEARCH_TIMEOUT_MS,
  });
  return { hits: (data.hits || []).map(normaliseHit), mode: data.mode || '' };
}

// Hit fields come from _hit() in scripts/fleet_rag/recall_api.py: score, text,
// source, app, category, seat, doc_id, chunk_index, heading, title, url, path,
// created_at (epoch ms).
function normaliseHit(h) {
  const created = typeof h.created_at === 'number' && h.created_at > 0 ? h.created_at : '';
  return {
    title: h.title || h.heading || h.doc_id || 'Untitled',
    score: typeof h.score === 'number' ? h.score : null,
    seat: h.seat || '',
    date: created,
    category: h.category || '',
    app: h.app || '',
    source: h.source || '',
    url: h.url || '',
    path: h.path || '',
    text: h.text || '',
  };
}

async function checkRecall(env) {
  const checkHealth = () => section(async () => {
    const data = await apiJson(`${recallBase(env)}/health`, {
      headers: recallPublicHeaders(env),
      timeoutMs: PROBE_TIMEOUT_MS,
    });
    const up = data.ok === true && data.backend_ok !== false;
    return {
      ok: true,
      state: up ? 'up' : 'warn',
      summary: up ? 'Healthy' : (data.error ? String(data.error) : 'Backend is not answering'),
      healthy: up,
      points: typeof data.points === 'number' ? data.points : null,
      collection: data.collection || '',
      version: data.version || '',
    };
  });

  const gaps = missing(env, 'RECALL_API_TOKEN', 'CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET');
  if (gaps.length) {
    const health = await checkHealth();
    return {
      ...notConfigured(...gaps),
      health,
      points: health.points ?? null,
      collection: health.collection || '',
      state: health.healthy ? 'warn' : 'off',
      summary: health.healthy ? 'Healthy, but search is not configured' : 'Not configured',
    };
  }

  // All three at once.  Awaiting health first and only then starting the
  // 25 s canary made this section cost health + canary end to end, which is
  // what pushed a cold /api/status past Cloudflare's edge limit.
  const [health, stats, canary] = await Promise.all([
    checkHealth(),
    section(async () => {
      const data = await apiJson(`${recallBase(env)}/recall/stats`, { headers: recallHeaders(env) });
      return {
        ok: true,
        state: data.embedder_healthy === false ? 'warn' : 'up',
        summary: data.embedder_healthy === false ? 'Embedder is unhealthy' : 'Stats read',
        points: typeof data.points === 'number' ? data.points : null,
        collection: data.collection || '',
        collectionStatus: data.status || '',
        embedder: data.embedder_healthy,
        reranker: data.rerank_healthy,
        bySource: data.by_source || {},
        byApp: data.by_app || {},
      };
    }),
    section(async () => {
      const { hits, mode } = await recallSearch(env, 'fleet mode', RECALL_CANARY_LIMIT);
      const found = hits.length > 0;
      return {
        ok: true,
        state: found ? 'up' : 'warn',
        summary: found ? `${hits.length} hits, mode ${mode || 'unknown'}` : 'No hits for the canary query',
        found,
        items: hits,
      };
    }),
  ]);

  const points = stats.points ?? health.points ?? null;
  const parts = [];
  if (points !== null) parts.push(`${points.toLocaleString('en-US')} points`);
  parts.push(health.healthy ? 'healthy' : 'health check failed');
  parts.push(canary.found ? 'canary passed' : 'canary failed');
  if (stats.reranker === false) parts.push('reranker unhealthy');

  const state = health.healthy && canary.found && stats.ok
    ? (stats.state === 'warn' ? 'warn' : 'up')
    : (health.healthy ? 'warn' : 'down');

  // Top sources by point count, so the card says something about the corpus.
  const breakdown = Object.entries(stats.bySource || {})
    .filter(([, n]) => Number(n) > 0)
    .sort((a, b) => b[1] - a[1])
    .map(([name, count]) => ({ name, count, state: 'up' }));

  return {
    ok: health.ok && stats.ok && canary.ok,
    state,
    summary: parts.join(', '),
    health,
    stats,
    canary,
    points,
    collection: stats.collection || health.collection || '',
    collectionStatus: stats.collectionStatus || '',
    breakdown,
    items: canary.items || [],
    error: [health.error, stats.error, canary.error].filter(Boolean).join(' · ') || undefined,
  };
}

/* -------------------------------------------------------------- the board */
/* Subrequests: stats + findings list.  Worst case 2.                       */

// scripts/mac-collab/mac-collab-server.py:
//   GET /health         public
//   GET /findings/stats bearer -> { total, open, p0p1_open, done, by_kind, apps }
//   GET /findings       bearer -> { findings: [...], count, total_matching }
// The stats route has no per-severity or per-status breakdown, so the open
// rows are listed once and counted here.
async function checkBoard(env) {
  if (!env.MAC_COLLAB_TOKEN) return notConfigured('MAC_COLLAB_TOKEN');
  const base = env.BOARD_BASE || 'https://mac.jays.services';
  const headers = { Authorization: `Bearer ${env.MAC_COLLAB_TOKEN}` };

  const [stats, rows] = await Promise.all([
    apiJson(`${base}/findings/stats`, { headers }),
    apiJson(`${base}/findings?status=open,in_progress&limit=2000`, { headers })
      .catch((err) => ({ __error: errText(err) })),
  ]);

  const findings = (rows && rows.findings) || [];
  const bySeverity = { P0: 0, P1: 0, P2: 0, P3: 0, P4: 0 };
  let open = 0;
  let inProgress = 0;
  for (const f of findings) {
    if (bySeverity[f.severity] !== undefined) bySeverity[f.severity] += 1;
    if (f.status === 'open') open += 1;
    else if (f.status === 'in_progress') inProgress += 1;
  }

  const items = Object.entries(bySeverity)
    .filter(([key, n]) => n > 0 || key === 'P0' || key === 'P1')
    .map(([name, count]) => ({
      name,
      count,
      state: count === 0 ? 'up' : (name === 'P0' ? 'down' : (name === 'P1' ? 'warn' : 'up')),
    }));

  const urgent = findings
    .filter((f) => f.severity === 'P0' || f.severity === 'P1')
    .slice(0, 6)
    .map((f) => ({
      name: f.title || `Finding ${f.id}`,
      sub: [f.app, f.addressed_by || 'unclaimed'].filter(Boolean).join(' · '),
      status: `${f.severity} ${String(f.status || '').replace('_', ' ')}`,
      state: f.severity === 'P0' ? 'down' : 'warn',
      url: `${base}/board`,
    }));

  const openTotal = Number(stats.open ?? (open + inProgress));
  const p0p1 = Number(stats.p0p1_open ?? (bySeverity.P0 + bySeverity.P1));
  const state = bySeverity.P0 > 0 ? 'down' : (p0p1 > 0 ? 'warn' : 'up');

  return {
    ok: !(rows && rows.__error),
    state,
    summary: `${open} open, ${inProgress} in progress, ${p0p1} at P0 or P1`,
    items,
    urgent,
    counts: { total: Number(stats.total || 0), open: openTotal, done: Number(stats.done || 0), p0p1 },
    byKind: stats.by_kind || {},
    apps: stats.apps || [],
    error: rows && rows.__error ? `Findings list: ${rows.__error}` : undefined,
  };
}

/* ---------------------------------------------------------------- coolify */
/* Subrequests: applications + servers.  Worst case 2.                      */

async function checkCoolify(env) {
  if (!env.COOLIFY_TOKEN) return notConfigured('COOLIFY_TOKEN');
  const base = env.COOLIFY_BASE || 'https://host.jays.services';
  const headers = { Authorization: `Bearer ${env.COOLIFY_TOKEN}` };

  const [apps, servers] = await Promise.all([
    apiJson(`${base}/api/v1/applications`, { headers }),
    apiJson(`${base}/api/v1/servers`, { headers }).catch((err) => ({ __error: errText(err) })),
  ]);

  const list = Array.isArray(apps) ? apps : (apps.data || []);
  const items = list.map((a) => {
    const status = String(a.status || 'unknown');
    return {
      name: a.name || a.uuid || 'unnamed',
      status,
      state: coolifyState(status),
      url: a.fqdn ? String(a.fqdn).split(',')[0].trim() : '',
      uuid: a.uuid || '',
    };
  }).sort((x, y) => x.name.localeCompare(y.name));

  const serverList = Array.isArray(servers) ? servers : (servers.data || []);
  const serverItems = serverList.map((s) => ({
    name: s.name || 'server',
    state: s.is_reachable && s.is_usable ? 'up' : (s.is_reachable ? 'warn' : 'down'),
    status: s.is_reachable ? (s.is_usable ? 'reachable and usable' : 'reachable, not usable') : 'unreachable',
  }));

  const bad = items.filter((i) => i.state === 'down').length;
  const warn = items.filter((i) => i.state === 'warn').length;
  return {
    ok: !(servers && servers.__error),
    state: bad > 0 ? 'down' : (warn > 0 ? 'warn' : 'up'),
    summary: `${items.length - bad - warn} of ${items.length} running`,
    items,
    servers: serverItems,
    error: servers && servers.__error ? `Servers: ${servers.__error}` : undefined,
  };
}

function coolifyState(status) {
  const s = status.toLowerCase();
  if (s.startsWith('running')) return s.includes('unhealthy') ? 'warn' : 'up';
  if (s.startsWith('exited') || s.startsWith('stopped') || s.startsWith('degraded')) return 'down';
  if (s.startsWith('restarting') || s.startsWith('starting')) return 'warn';
  return 'warn';
}

/* ----------------------------------------------------------------- github */
/* Subrequests: one org-wide PR search + one actions/runs call per repo.     */
/* Worst case 1 + APPS.length = 13.  It used to be two per repo — 24 — and   */
/* that alone was half the platform's budget.                                */

async function checkGitHub(env) {
  if (!env.GITHUB_TOKEN) return notConfigured('GITHUB_TOKEN');
  // Owner moved from the personal account to the Simple-With-Us org.  The
  // search qualifier is `org:` because GitHub 422s the whole search on
  // `user:` for an org (verified 2026-10-03: user:jaywedgeworth22 -> HTTP 422,
  // user:Simple-With-Us -> 16 open PRs), which silently killed the PR half of
  // this card.  `org:` is the correct qualifier for an organisation and works
  // for the org only, so keep this an org check rather than a generic owner.
  const owner = env.GITHUB_OWNER || 'Simple-With-Us';
  const qualifier = env.GITHUB_OWNER_IS_ORG === 'false' ? 'user' : 'org';
  const headers = {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    Accept: 'application/vnd.github+json',
    'X-GitHub-Api-Version': '2022-11-28',
  };

  // One search for every open PR the owner has, grouped by repo afterwards.
  const search = await apiJson(
    `https://api.github.com/search/issues?q=${encodeURIComponent(`is:pr is:open ${qualifier}:${owner}`)}&per_page=100`,
    { headers },
  ).catch((err) => ({ __error: errText(err) }));

  const prCounts = new Map();
  let openPrs = null;
  let prError = '';
  let prTruncated = false;
  if (search && search.__error) {
    prError = search.__error;
  } else {
    const found = search.items || [];
    for (const item of found) {
      // repository_url is https://api.github.com/repos/<owner>/<repo>
      const repo = String(item.repository_url || '').split('/').pop();
      if (repo) prCounts.set(repo, (prCounts.get(repo) || 0) + 1);
    }
    openPrs = Number(search.total_count || 0);
    // per_page caps at 100, so beyond that the per-repo split is partial even
    // though the total is exact.  Say so rather than quietly under-reporting.
    prTruncated = openPrs > found.length;
  }

  const items = await Promise.all(APPS.map(async (app) => {
    const row = {
      name: app.name,
      repo: app.repo,
      // The Worker owns the link so the owner never drifts from GITHUB_OWNER
      // again.  The front-end used to hardcode jaywedgeworth22/ here.
      repoUrl: `https://github.com/${owner}/${app.repo}`,
      kind: app.kind,
      prs: prError ? null : (prCounts.get(app.repo) || 0),
      run: null,
      state: 'warn',
    };
    if (prError) row.prError = prError;

    const runs = await apiJson(
      `https://api.github.com/repos/${owner}/${app.repo}/actions/runs?branch=main&per_page=1`,
      { headers },
    ).catch((err) => ({ __error: errText(err) }));

    if (runs && runs.__error) {
      row.runError = runs.__error;
    } else {
      const run = (runs.workflow_runs || [])[0];
      if (run) {
        row.run = {
          status: run.status,
          conclusion: run.conclusion,
          url: run.html_url,
          updated: run.updated_at,
          name: run.name,
        };
      }
    }

    row.state = runState(row.run);
    return row;
  }));

  const failing = items.filter((i) => i.state === 'down');
  const runErrors = items.filter((i) => i.runError).length;
  const noRun = items.filter((i) => !i.run && !i.runError).length;
  const total = openPrs === null
    ? 'PR count failed'
    : `${openPrs} open PR${openPrs === 1 ? '' : 's'}${prTruncated ? ' (per-repo split capped at 100)' : ''}`;

  // "No repo is failing" is only green when we actually heard back about every
  // repo.  If all thirteen actions/runs calls fail, failing.length is 0 and the
  // old code called that up — the panel reporting main green everywhere on the
  // strength of having learned nothing.
  const parts = [total];
  if (failing.length) {
    parts.push(`${failing.length} repo${failing.length === 1 ? '' : 's'} failing on main`);
  } else if (!runErrors && !noRun) {
    parts.push('main green everywhere');
  }
  if (runErrors) parts.push(`${runErrors} run check${runErrors === 1 ? '' : 's'} failed`);
  if (noRun) parts.push(`${noRun} with no run on main`);

  return {
    ok: !prError && runErrors === 0,
    state: failing.length ? 'down' : ((runErrors || noRun) ? 'warn' : 'up'),
    summary: parts.join(', '),
    items,
    error: prError ? `PR search: ${prError}` : undefined,
  };
}

function runState(run) {
  if (!run) return 'warn';
  if (run.status !== 'completed') return 'warn';
  if (run.conclusion === 'success') return 'up';
  if (run.conclusion === 'skipped' || run.conclusion === 'neutral' || run.conclusion === 'cancelled') return 'warn';
  return 'down';
}

/* ----------------------------------------------------------------- vercel */
/* Subrequests: personal projects, then teams + one call per team.           */
/* Worst case 2 + VERCEL_MAX_TEAMS = 6.                                     */

async function checkVercel(env) {
  if (!env.VERCEL_TOKEN) return notConfigured('VERCEL_TOKEN');
  const headers = { Authorization: `Bearer ${env.VERCEL_TOKEN}` };

  let projects = await fetchVercelProjects(headers, '');
  const scopes = [];
  const scopeErrors = [];
  if (!projects.length) {
    const teams = await apiJson('https://api.vercel.com/v2/teams', { headers }).catch(() => ({ teams: [] }));
    // All the teams at once.  Awaiting them one by one made this section cost
    // up to four API timeouts back to back on top of the first two calls, and
    // it is the same number of subrequests either way.  A team that fails is
    // named rather than thrown, so one bad scope cannot blank the card.
    const scoped = await Promise.all(
      (teams.teams || []).slice(0, VERCEL_MAX_TEAMS).map(async (team) => {
        const label = team.slug || team.name || team.id;
        try {
          return { label, list: await fetchVercelProjects(headers, `&teamId=${encodeURIComponent(team.id)}`) };
        } catch (err) {
          return { label, list: [], error: errText(err) };
        }
      }),
    );
    for (const { label, list, error } of scoped) {
      if (error) scopeErrors.push(`${label}: ${error}`);
      else if (list.length) scopes.push(label);
      projects = projects.concat(list);
    }
  }

  const items = projects.map((p) => {
    // A project whose recent production deploys are ALL canceled has no READY
    // entry left in `latestDeployments` — Vercel only carries a short window —
    // so `pickVercelDeployment` falls through to the newest canceled one.  That
    // says a build attempt was abandoned, NOT that the site is unreachable, and
    // treating it as a failure contradicted the Endpoints card (botfleet.app and
    // contactlogo.com both answer 200).  `vercelState` now owns that judgement.
    const dep = pickVercelDeployment(p.latestDeployments);
    const readyState = dep.readyState || dep.state || 'UNKNOWN';
    return {
      name: p.name,
      status: String(readyState).toLowerCase(),
      state: vercelState(readyState),
      url: dep.url ? `https://${dep.url}` : '',
      updated: dep.createdAt ? new Date(dep.createdAt).toISOString() : '',
    };
  }).sort((a, b) => a.name.localeCompare(b.name));

  const bad = items.filter((i) => i.state === 'down').length;
  const soft = items.filter((i) => i.state === 'warn').length;
  return {
    ok: scopeErrors.length === 0,
    state: bad > 0 ? 'down' : (scopeErrors.length || soft ? 'warn' : 'up'),
    summary: items.length
      ? `${items.length} projects, ${bad} failed${soft ? `, ${soft} without a ready deployment` : ''}${scopes.length ? ` (teams: ${scopes.join(', ')})` : ''}`
      : 'No projects visible to this token',
    items,
    error: scopeErrors.length ? `Teams: ${scopeErrors.join(' · ')}` : undefined,
  };
}

async function fetchVercelProjects(headers, suffix) {
  const data = await apiJson(`https://api.vercel.com/v9/projects?limit=50${suffix}`, { headers });
  return data.projects || [];
}

// Prefer the newest READY deployment when the absolute latest is CANCELED
// (or otherwise non-READY).  Falls back to [0] when nothing is READY yet.
function pickVercelDeployment(deployments) {
  const list = Array.isArray(deployments) ? deployments : [];
  if (!list.length) return {};
  const ready = list.find((d) => String(d.readyState || d.state || '').toUpperCase() === 'READY');
  return ready || list[0] || {};
}

// Only a build that actually FAILED is a service failure.  CANCELED is an
// abandoned or superseded attempt: the production alias keeps serving the last
// READY build, so the project is not down and must not paint the card red.
// Reporting it as `down` made a healthy project contradict its own Endpoints
// row (botfleet 2026-09-29).
function vercelState(readyState) {
  const s = String(readyState).toUpperCase();
  if (s === 'READY') return 'up';
  if (s === 'ERROR') return 'down';
  if (s === 'CANCELED' || s === 'BUILDING' || s === 'QUEUED' || s === 'INITIALIZING') return 'warn';
  return 'warn';
}

/* ----------------------------------------------------------------- sentry */
/* Subrequests: one issues query.  Worst case 1.                            */

async function checkSentry(env) {
  if (!env.SENTRY_AUTH_TOKEN) return notConfigured('SENTRY_AUTH_TOKEN');
  const org = env.SENTRY_ORG || 'simple-with-us';
  const issues = await apiJson(
    `https://sentry.io/api/0/organizations/${org}/issues/?query=${encodeURIComponent('is:unresolved')}&statsPeriod=24h&limit=100`,
    { headers: { Authorization: `Bearer ${env.SENTRY_AUTH_TOKEN}` } },
  );

  const byProject = new Map();
  for (const issue of issues) {
    const slug = (issue.project && issue.project.slug) || 'unknown';
    const row = byProject.get(slug) || { name: slug, count: 0, top: '' };
    row.count += 1;
    if (!row.top) row.top = issue.title || issue.culprit || '';
    byProject.set(slug, row);
  }

  const items = [...byProject.values()]
    .map((r) => ({ ...r, state: r.count === 0 ? 'up' : (r.count > 10 ? 'down' : 'warn') }))
    .sort((a, b) => b.count - a.count);

  return {
    ok: true,
    state: issues.length === 0 ? 'up' : (issues.length > 25 ? 'down' : 'warn'),
    summary: issues.length === 0
      ? 'No unresolved issues in the last 24 hours'
      : `${issues.length} unresolved issues across ${items.length} projects`,
    items,
    link: `https://${org}.sentry.io`,
  };
}

/* -------------------------------------------------------------- pagerduty */
/* Subrequests: one incidents query.  Worst case 1.                         */

async function checkPagerDuty(env) {
  if (!env.PAGERDUTY_API_KEY) return notConfigured('PAGERDUTY_API_KEY');
  // limit is the page size, not the count.  Asking for total=true makes
  // PagerDuty return the real number, so a full page stops reading as
  // "exactly 25 incidents".
  const data = await apiJson(
    'https://api.pagerduty.com/incidents?statuses[]=triggered&statuses[]=acknowledged&limit=25&total=true',
    {
      headers: {
        Authorization: `Token token=${env.PAGERDUTY_API_KEY}`,
        Accept: 'application/vnd.pagerduty+json;version=2',
      },
    },
  );

  const items = (data.incidents || []).map((i) => ({
    name: i.title || i.summary || 'Incident',
    status: i.status,
    state: i.status === 'triggered' ? 'down' : 'warn',
    url: i.html_url || '',
    service: (i.service && i.service.summary) || '',
    urgency: i.urgency || '',
  }));

  // total is authoritative when present.  Without it, a full page plus more
  // pages is at least this many, so say so rather than guessing a number.
  const total = typeof data.total === 'number' ? data.total : null;
  const count = total !== null ? String(total) : (data.more ? `${items.length}+` : String(items.length));
  const none = total !== null ? total === 0 : items.length === 0;

  return {
    ok: true,
    state: items.some((i) => i.state === 'down') ? 'down' : (none ? 'up' : 'warn'),
    summary: none ? 'No open incidents' : `${count} open incidents`,
    items,
    total,
  };
}

/* ---------------------------------------------------------------- datadog */
/* Subrequests: one monitor listing.  Worst case 1.                         */

async function checkDatadog(env) {
  const gaps = missing(env, 'DD_API_KEY', 'DD_APP_KEY');
  if (gaps.length) return notConfigured(...gaps);
  const site = env.DD_SITE || 'us5.datadoghq.com';
  const monitors = await apiJson(`https://api.${site}/api/v1/monitor?page_size=100`, {
    headers: { 'DD-API-KEY': env.DD_API_KEY, 'DD-APPLICATION-KEY': env.DD_APP_KEY },
  });

  const counts = { Alert: 0, Warn: 0, 'No Data': 0, OK: 0, Other: 0 };
  const alerting = [];
  for (const m of monitors) {
    const state = m.overall_state || 'Other';
    if (counts[state] === undefined) counts.Other += 1;
    else counts[state] += 1;
    if (state === 'Alert' || state === 'Warn') {
      alerting.push({
        name: m.name || `Monitor ${m.id}`,
        status: state.toLowerCase(),
        state: state === 'Alert' ? 'down' : 'warn',
        url: `https://app.${site.replace('api.', '')}/monitors/${m.id}`,
      });
    }
  }

  const items = Object.entries(counts)
    .filter(([, n]) => n > 0)
    .map(([name, count]) => ({
      name,
      count,
      state: name === 'Alert' ? 'down' : (name === 'Warn' || name === 'No Data' ? 'warn' : 'up'),
    }));

  return {
    ok: true,
    state: counts.Alert > 0 ? 'down' : (counts.Warn > 0 ? 'warn' : 'up'),
    summary: `${monitors.length} monitors, ${counts.Alert} alerting, ${counts.Warn} warning`,
    items,
    alerting,
  };
}

/* ------------------------------------------------------------ orchestration */

// Worst-case subrequests per section, as a function of env.  These are the
// numbers the comment above each check states; keep them together or the
// budget stops meaning anything.
//
// cost() must return 0 for exactly the cases where run() returns notConfigured
// without fetching.  Charging a check that never calls out was letting an
// unset GITHUB_TOKEN and VERCEL_TOKEN spend 21 of the 45 on nothing, which
// deferred the sections at the end of the list and overstated `subrequests`.
// ---------------------------------------------------------------------------
// Ops reference sheet (Fleet Ops Inventory tabs inside the ASC workbook).
//
// This card is the "what did I forget" surface: which host runs what, what
// each hz* shortcut does, and what a reboot actually takes down.  It is NOT a
// health signal — the sheet is reference data regenerated by a Mac-side sync,
// so a stale sync is worth surfacing but a stale sheet is never "down".
//
// Cost: 1 token exchange + 1 metadata call + 1 values.get, but the token is
// cached per isolate so steady-state is 2.  Charged as 2 to stay honest.
const OPS_SHEET_DEFAULT = '1fyp76U-GnlRbm5GeevnPY5WxPekxualMiZW5M-5VlSY';

let opsToken = { at: 0, value: '' };

async function opsAccessToken(saKey) {
  const now = Date.now();
  if (opsToken.value && now - opsToken.at < 50 * 60 * 1000) return opsToken.value;

  const b64 = (o) => btoa(String.fromCharCode(...new TextEncoder().encode(JSON.stringify(o))))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const header = b64({ alg: 'RS256', typ: 'JWT' });
  const claim = b64({
    iss: saKey.client_email,
    scope: 'https://www.googleapis.com/auth/spreadsheets.readonly',
    aud: 'https://oauth2.googleapis.com/token',
    iat: Math.floor(now / 1000),
    exp: Math.floor(now / 1000) + 3600,
  });
  const key = await crypto.subtle.importKey(
    'pkcs8',
    Uint8Array.from(atob(saKey.private_key.replace(/\\n/g, '\n')), (c) => c.charCodeAt(0)),
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' },
    false,
    ['sign'],
  );
  const sig = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', key, new TextEncoder().encode(`${header}.${claim}`));
  const jwt = `${header}.${claim}.${btoa(String.fromCharCode(...new Uint8Array(sig)))
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')}`;

  const res = await fetch('https://oauth2.googleapis.com/token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: `grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer&assertion=${jwt}`,
    signal: AbortSignal.timeout(API_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`Google token exchange returned HTTP ${res.status}`);
  const json = await res.json();
  opsToken = { at: now, value: json.access_token || '' };
  return opsToken.value;
}

async function checkOpsSheet(env) {
  let saKey;
  try {
    saKey = JSON.parse(env.GOOGLE_SA_JSON || '');
  } catch {
    return notConfigured('GOOGLE_SA_JSON');
  }
  if (!saKey.client_email) return notConfigured('GOOGLE_SA_JSON');

  const id = env.OPS_SHEET_ID || OPS_SHEET_DEFAULT;
  const token = await opsAccessToken(saKey);
  const auth = { Authorization: `Bearer ${token}` };

  const wanted = ['Mac Scripts', 'Mac Background Jobs', 'Server Scripts', 'Server Schedules', 'Shortcuts', 'Cleanup Queue', 'Host Map'];
  const meta = await apiJson(`https://sheets.googleapis.com/v4/spreadsheets/${id}?fields=sheets.properties`, { headers: auth });
  const present = (meta.sheets || []).map((s) => s.properties.title);
  const have = wanted.filter((t) => present.includes(t));
  const missing = wanted.filter((t) => !present.includes(t));
  if (!have.length) throw new Error(`none of the ops tabs exist in ${id}`);

  // batchGet is the ONLY values read that takes several ranges in one call —
  // a comma-joined path on values.get returns HTTP 400 "Unable to parse range".
  // The parameter name must be REPEATED per range: `ranges=A&ranges=B`.  Joining
  // with a bare `&` (ranges=A&B) binds only the first and 400s the rest, the
  // same shape as PagerDuty needing `statuses=triggered&statuses=acknowledged`.
  const ranges = wanted.map((t) => `${t}!A1:Z400`);
  const vals = await apiJson(`https://sheets.googleapis.com/v4/spreadsheets/${id}/values:batchGet?` +
    ranges.map((r) => `ranges=${encodeURIComponent(r)}`).join('&'), { headers: auth });

  const got = vals.valueRanges || [];
  const counts = {};
  got.forEach((r, i) => {
    const title = (r.range || '').split('!')[0].replace(/'/g, '');
    counts[title] = (r.values || []).length;
  });
  const rowTotal = got.reduce((a, r) => a + Math.max(0, (r.values || []).length - 1), 0);
  const unreadable = have.filter((t) => !counts[t]);

  const items = [
    ...have.map((t) => ({ name: t, count: Math.max(0, (counts[t] || 0) - 1), state: 'up' })),
    ...(missing.length
      ? [{ name: `Missing tabs: ${missing.join(', ')}`, count: missing.length, state: 'warn' }]
      : []),
  ];

  return {
    ok: true,
    // Reference data is never "down".  Only a tab that vanished is a warning.
    state: (missing.length || unreadable.length) ? 'warn' : 'up',
    summary: `${rowTotal} rows across ${have.length} ops tabs` +
      (missing.length ? `; ${missing.length} tab(s) missing — run ~/apps/fleet-ops-sheet/fleet-ops-sheet-sync.mjs` : ''),
    items,
    link: `https://docs.google.com/spreadsheets/d/${id}/edit`,
  };
}

const CHECKS = {
  endpoints: { cost: () => ENDPOINTS.length, run: () => checkEndpoints() },
  opsSheet: { cost: (env) => (env.GOOGLE_SA_JSON ? 2 : 0), run: (env) => checkOpsSheet(env) },
  // The health probe runs even when search is unconfigured, so recall is never
  // free — it is 1 without the secrets and 3 with them.
  recall: {
    cost: (env) => (missing(env, 'RECALL_API_TOKEN', 'CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET').length ? 1 : 3),
    run: (env) => checkRecall(env),
  },
  board: { cost: (env) => (env.MAC_COLLAB_TOKEN ? 2 : 0), run: (env) => checkBoard(env) },
  coolify: { cost: (env) => (env.COOLIFY_TOKEN ? 2 : 0), run: (env) => checkCoolify(env) },
  github: { cost: (env) => (env.GITHUB_TOKEN ? 1 + APPS.length : 0), run: (env) => checkGitHub(env) },
  vercel: { cost: (env) => (env.VERCEL_TOKEN ? 2 + VERCEL_MAX_TEAMS : 0), run: (env) => checkVercel(env) },
  sentry: { cost: (env) => (env.SENTRY_AUTH_TOKEN ? 1 : 0), run: (env) => checkSentry(env) },
  pagerduty: { cost: (env) => (env.PAGERDUTY_API_KEY ? 1 : 0), run: (env) => checkPagerDuty(env) },
  datadog: {
    cost: (env) => (missing(env, 'DD_API_KEY', 'DD_APP_KEY').length ? 0 : 1),
    run: (env) => checkDatadog(env),
  },
};

const SECTION_NAMES = Object.keys(CHECKS);

// One cache entry per section, so a page that asks for nine sections in
// parallel and then refreshes does not re-probe everything.
const sectionCache = new Map();

function cached(name) {
  const hit = sectionCache.get(name);
  if (!hit) return null;
  return { ...hit, fresh: Date.now() - hit.at < CACHE_TTL_MS };
}

function deadlineResult(what, ms) {
  // The document-wide deadline hands out whatever is left of its window, which
  // can be under a second, and "timed out after 0 s" reads like a bug.
  const took = ms >= 1000 ? `${Math.round(ms / 1000)} s` : `${ms} ms`;
  return {
    ok: false,
    state: 'warn',
    summary: `Timed out after ${took}`,
    error: `${what} did not answer within the ${took} deadline, so the page stopped waiting on it.`,
    items: [],
    timedOut: true,
  };
}

// Promise.race against a wall clock.  The loser is not cancelled — every fetch
// underneath carries its own AbortSignal — but the response stops waiting on
// it, and a section that finishes late still populates the cache for the next
// call.  Without this the budget bounded how many calls we made and nothing
// bounded how long they took, so a cold fan-out could run past Cloudflare's
// edge limit and the page's refresh button stayed disabled through it.
function withDeadline(promise, ms, what) {
  let timer;
  const alarm = new Promise((resolve) => {
    timer = setTimeout(() => resolve(deadlineResult(what, ms)), ms);
  });
  return Promise.race([promise, alarm]).finally(() => clearTimeout(timer));
}

async function runSection(name, env) {
  const hit = cached(name);
  if (hit && hit.fresh) return { ...hit.data, cached: true, checkedAt: hit.checkedAt };

  const run = section(() => CHECKS[name].run(env)).then((data) => {
    const checkedAt = new Date().toISOString();
    sectionCache.set(name, { at: Date.now(), data, checkedAt });
    return { ...data, cached: false, checkedAt };
  });

  const out = await withDeadline(run, SECTION_DEADLINE_MS, `The ${name} check`);
  return out.timedOut ? { ...out, cached: false, checkedAt: new Date().toISOString() } : out;
}

// Oldest first, never-checked ahead of everything.  SECTION_NAMES is a fixed
// order and `spent` resets every invocation, so running it in declaration
// order meant a 60 s poller re-ran the same first sections and deferred the
// same last ones forever — pagerduty and datadog never got checked at all.
// Sorting by when each section last ran makes whatever was deferred lead the
// next call.  Array.prototype.sort is stable, so ties keep declaration order.
function staleFirst() {
  return [...SECTION_NAMES].sort((a, b) => {
    const at = (n) => (sectionCache.get(n) || { at: 0 }).at;
    return at(a) - at(b);
  });
}

// Picks what fits the budget, then runs those concurrently rather than in
// order: the sections do not contend for anything, and sequencing them made
// the document cost the sum of every section's worst case.  A section that
// does not fit is served stale if we have it and marked pending if we do not;
// either way the next call leads with it.
async function buildStatus(env) {
  const sections = {};
  const pending = [];
  let spent = 0;

  for (const name of staleFirst()) {
    const hit = cached(name);
    if (hit && hit.fresh) {
      sections[name] = { ...hit.data, cached: true, checkedAt: hit.checkedAt };
      continue;
    }
    const cost = CHECKS[name].cost(env);
    if (spent + cost > STATUS_BUDGET) {
      sections[name] = hit
        ? { ...hit.data, cached: true, stale: true, checkedAt: hit.checkedAt }
        : {
          ok: true,
          state: 'pending',
          summary: 'Deferred to the next call to stay under the subrequest limit',
          items: [],
        };
      continue;
    }
    spent += cost;
    pending.push(name);
  }

  // One deadline over the whole fan-out as well as one per section, so a
  // section that hangs just short of its own deadline cannot push the document
  // past the edge limit.  Anything still running when this fires is reported
  // the way a deferred section is, and its result lands in the cache for the
  // next call.
  const started = Date.now();
  await Promise.all(pending.map(async (name) => {
    const left = Math.max(0, STATUS_DEADLINE_MS - (Date.now() - started));
    sections[name] = await withDeadline(runSection(name, env), left, `The ${name} check`);
  }));

  // Assembled in declaration order, not completion order, so the document
  // reads the same way every time however the concurrency lands.
  const ordered = {};
  for (const name of SECTION_NAMES) ordered[name] = sections[name];

  // A section we ran but did not wait for spent its subrequests all the same,
  // so `spent` stays honest about what this invocation charged.
  const states = Object.values(sections)
    .map((s) => s.state)
    .filter((s) => s !== 'off' && s !== 'pending');
  const overall = states.includes('down') ? 'down' : (states.includes('warn') ? 'warn' : 'up');

  return { checkedAt: new Date().toISOString(), overall, subrequests: spent, sections: ordered };
}

function jsonResponse(payload, status = 200) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      'Content-Type': 'application/json; charset=utf-8',
      'Cache-Control': 'no-store',
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname === '/api/status') {
      const name = (url.searchParams.get('section') || '').trim();
      if (name) {
        if (!CHECKS[name]) {
          return jsonResponse({ ok: false, error: `No such section. Try one of: ${SECTION_NAMES.join(', ')}.` }, 404);
        }
        const data = await runSection(name, env);
        return jsonResponse({ checkedAt: data.checkedAt, section: name, data });
      }
      return jsonResponse(await buildStatus(env));
    }

    if (url.pathname === '/api/sections') {
      return jsonResponse({ sections: SECTION_NAMES });
    }

    if (url.pathname === '/api/recall/search') {
      const q = (url.searchParams.get('q') || '').trim();
      if (!q) return jsonResponse({ ok: false, error: 'Add a query.', hits: [] }, 400);
      const gaps = missing(env, 'RECALL_API_TOKEN', 'CF_ACCESS_CLIENT_ID', 'CF_ACCESS_CLIENT_SECRET');
      if (gaps.length) {
        return jsonResponse({ ok: false, error: 'Not configured', hits: [] }, 503);
      }
      const asked = Number(url.searchParams.get('limit'));
      const limit = Math.min(Number.isFinite(asked) && asked > 0 ? asked : RECALL_SEARCH_LIMIT, 25);
      try {
        // recallSearch returns { hits, mode } — hand the array to the page, not
        // the wrapper, or every search renders as "no hits".
        const { hits, mode } = await recallSearch(env, q, limit);
        return jsonResponse({ ok: true, query: q, mode, hits });
      } catch (err) {
        return jsonResponse({ ok: false, error: errText(err), hits: [] }, 502);
      }
    }

    if (url.pathname.startsWith('/api/')) {
      return jsonResponse({ ok: false, error: 'No such route.' }, 404);
    }

    return env.ASSETS.fetch(request);
  },
};
