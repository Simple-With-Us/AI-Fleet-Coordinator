// Dedicated TestFlight App only. No gh account, environment token or seat fallback.
import { sign } from 'node:crypto';
import { resolveIsolatedTestflightKey } from './testflight_infisical_key.mjs';
export const APP_ID = 5840817;
export const INSTALLATION_ID = 183854416;
const API = 'https://api.github.com';
const refusal = () => new Error('dedicated-testflight-service-auth-unavailable');
export function makeServiceGitHub({ key = resolveIsolatedTestflightKey, fetchImpl = fetch, now = Date.now, budgetMs = 8 * 60 * 1000 } = {}) {
  const deadline = now() + budgetMs;
  // One resolver promise per run-scoped adapter, including a failed resolution.
  // Private key stays in memory, never on disk; every GitHub call still verifies
  // App/installation and mints/revokes its own repository-scoped token.
  let keyPromise;
  const runKey = () => keyPromise ??= Promise.resolve().then(() => key());
  async function request(route, auth, method = 'GET', body, onSend) {
    try {
      const remaining = route === '/installation/token' ? 10000 : Math.min(30000, deadline - now());
      if (remaining <= 0) throw refusal();
      const options = { method, redirect: 'error', signal: AbortSignal.timeout(Math.max(1, Math.floor(remaining))),
        headers: { Authorization: `Bearer ${auth}`, Accept: 'application/vnd.github+json', 'X-GitHub-Api-Version': '2026-03-10', 'User-Agent': 'swu-testflight-automation', 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) };
      onSend?.();
      const response = await fetchImpl(API + route, options);
      if (!response.ok) { const e = refusal(); e.notFound = response.status === 404; e.status = response.status; throw e; }
      return response.status === 204 ? null : await response.json();
    } catch (e) { const safe = refusal(); safe.notFound = e.notFound === true; safe.status = e.status; throw safe; }
  }
  return async function github(route, { method = 'GET', body } = {}) {
    let token, result, failure, revokeFailed = false, dispatchAttempted = false, dispatchAccepted = false;
    try {
      const m = /^repos\/Simple-With-Us\/([A-Za-z0-9_.-]+)(?:\/|$)/.exec(route);
      if (!m || !new URL(API + '/' + route).pathname.startsWith('/repos/Simple-With-Us/' + m[1] + '/') || route.split(/[/?]/).some(s => s === '..' || s === '.') || /[\\\s#]/.test(route)) throw refusal();
      const write = method !== 'GET';
      if (write && (method !== 'POST' || !/^repos\/Simple-With-Us\/[A-Za-z0-9_.-]+\/actions\/workflows\/[A-Za-z0-9_.-]+\/dispatches$/.test(route))) throw refusal();
      const t = Math.floor(now() / 1000), enc = v => Buffer.from(JSON.stringify(v)).toString('base64url');
      const unsigned = enc({ alg: 'RS256', typ: 'JWT' }) + '.' + enc({ iat: t - 30, exp: t + 300, iss: APP_ID });
      if (deadline <= now()) throw refusal();
      const jwt = unsigned + '.' + sign('RSA-SHA256', Buffer.from(unsigned), await runKey()).toString('base64url');
      const app = await request('/app', jwt);
      if (app.id !== APP_ID) throw refusal();
      const install = await request(`/app/installations/${INSTALLATION_ID}`, jwt);
      if (install.app_id !== APP_ID || install.account?.login !== 'Simple-With-Us' || install.suspended_at || install.repository_selection !== 'selected') throw refusal();
      const permissions = { contents: 'read', metadata: 'read', checks: 'read', actions_variables: 'read', actions: write ? 'write' : 'read' };
      const minted = await request(`/app/installations/${INSTALLATION_ID}/access_tokens`, jwt, 'POST', { repositories: [m[1]], permissions });
      token = minted.token;
      if (typeof token !== 'string' || !token || !Number.isFinite(Date.parse(minted.expires_at)) || Date.parse(minted.expires_at) <= now() || !Array.isArray(minted.repositories) || minted.repositories.length !== 1 || minted.repositories[0].full_name !== `Simple-With-Us/${m[1]}` || Object.entries(minted.permissions || {}).some(([k,v]) => permissions[k] !== v) || Object.entries(permissions).some(([k,v]) => minted.permissions?.[k] !== v)) throw refusal();
      result = await request('/' + route, token, method, body, () => { dispatchAttempted = write; });
      dispatchAccepted = write;
    } catch (e) { failure = refusal(); failure.notFound = e.notFound === true; failure.status = e.status; }
    finally { if (token) { try { await request('/installation/token', token, 'DELETE'); } catch { revokeFailed = true; } } }
    if (method !== 'GET') {
      if (failure) { failure.dispatchOutcome = !dispatchAttempted ? 'not-attempted' : [400,401,403,404,422].includes(failure.status) ? 'rejected' : 'unknown'; failure.revocationFailed = revokeFailed; throw failure; }
      return { dispatchOutcome: dispatchAccepted ? 'accepted' : 'not-attempted', revocationFailed: revokeFailed, ...(Number.isSafeInteger(result?.workflow_run_id) && result.workflow_run_id > 0 ? {runId: result.workflow_run_id} : {}) };
    }
    if (failure) throw failure;
    if (revokeFailed) throw refusal();
    return result;
  };
}
