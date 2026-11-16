import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { consentPage } from '../src/pages.js';
import { loadConfig } from '../src/config.js';
import { CURSOR_CIMD_CLIENT as CLIENT, CURSOR_CLOUD_REDIRECT as REDIRECT, isCursorExactPair,
  redirectUriProblem, isLoopbackRedirect, preGateAuthorize, postGateAuthRequest,
  clientBoundToSeat, gateTokenForm, resolveArmedSeat } from '../src/policy.js';
import { testEnv, authorizeParams, CHALLENGE, RESOURCE, CHATGPT_CLIENT, CHATGPT_REDIRECT } from './helpers.mjs';
const cfg = loadConfig(testEnv({ HOSTED_SEATS: 'JET,GROK-WEB,CURSOR,CLAUDE' }));
const request = { clientId: CLIENT, redirectUri: REDIRECT, codeChallengeMethod: 'S256',
  codeChallenge: CHALLENGE, resource: RESOURCE, scope: ['zulip:read', 'zulip:write'] };
function pre(over = {}) { return preGateAuthorize(authorizeParams({ client_id: CLIENT, redirect_uri: REDIRECT, ...over }), cfg); }
function token(over = {}, auth = null) {
  return gateTokenForm(new URLSearchParams({ grant_type: 'authorization_code', client_id: CLIENT,
    redirect_uri: REDIRECT, code: 'synthetic-unused-code', code_verifier: 'synthetic-unused-verifier', ...over }), auth, cfg);
}
test('exact Cursor pair passes every client/consent gate only as CURSOR, and still requires arming', () => {
  assert.equal(pre().ok, true);
  assert.deepEqual(pre().seats, ['CURSOR']);
  assert.equal(pre().cimdSeat, 'CURSOR');
  assert.equal(postGateAuthRequest(request, cfg, 'CURSOR'), true);
  assert.equal(clientBoundToSeat({ clientId: CLIENT }, 'CURSOR', REDIRECT, cfg), true);
  for (const seat of ['JET', 'GROK-WEB', 'ECHO', 'WISE', 'CLAUDE']) {
    assert.equal(postGateAuthRequest(request, cfg, seat), false);
    assert.equal(clientBoundToSeat({ clientId: CLIENT }, seat, REDIRECT, cfg), false);
  }
  assert.equal(resolveArmedSeat(pre().seats, new Set()).reason, 'not_armed');
  assert.equal(resolveArmedSeat(pre().seats, new Set(['JET'])).reason, 'not_armed');
  assert.equal(resolveArmedSeat(pre().seats, new Set(['CURSOR'])).seat, 'CURSOR');
});
test('Cursor CIMD uses the exact HTTPS Cloud callback and localhost remains invalid', () => {
  assert.equal(redirectUriProblem(REDIRECT), '');
  assert.equal(isLoopbackRedirect(REDIRECT), false);
  const env = testEnv();
  const bad = (entry, seat = 'CURSOR') => loadConfig({ ...env, SEATS: {
    ...env.SEATS, CURSOR: { redirect_uris: [], cimd_client_ids: [] }, [seat]: entry } });
  assert.throws(() => bad({ redirect_uris: ['http://localhost:8787/callback'], cimd_client_ids: [] }), /not strict/);
  assert.throws(() => bad({ redirect_uris: [REDIRECT], cimd_client_ids: [CLIENT] }, 'JET'), /Cursor CIMD belongs/);
  assert.throws(() => bad({ redirect_uris: [CHATGPT_REDIRECT], cimd_client_ids: [CLIENT] }, 'JET'), /Cursor CIMD belongs/);
});
test('host, port, path, query, fragment and client variants cannot use the exception', () => {
  const variants = ['https://cursor.com/agents/mcp/oauth/callback', 'https://www.cursor.com/agents/mcp/oauth/callback/',
    'https://www.cursor.com/agents/mcp/other', 'https://www.cursor.com:443/agents/mcp/oauth/callback',
    'https://user@www.cursor.com/agents/mcp/oauth/callback', 'https://www.cursor.com/agents/mcp/oauth/callback?x=1',
    'https://www.cursor.com/agents/mcp/oauth/callback#x', ' https://www.cursor.com/agents/mcp/oauth/callback',
    'http://localhost:8787/callback', 'http://127.0.0.1:8787/callback', 'http://[::1]:8787/callback'];
  for (const uri of variants) {
    assert.equal(pre({ redirect_uri: uri }).ok, false, uri);
    assert.equal(postGateAuthRequest({ ...request, redirectUri: uri }, cfg, 'CURSOR'), false, uri);
    assert.equal(token({ redirect_uri: uri }).ok, false, uri);
  }
  for (const id of [CHATGPT_CLIENT, 'https://cursor.com/oauth/other.json', CLIENT + '?x=1', ' ' + CLIENT]) {
    assert.equal(pre({ client_id: id }).ok, false, id);
    assert.equal(postGateAuthRequest({ ...request, clientId: id }, cfg, 'CURSOR'), false, id);
    assert.equal(clientBoundToSeat({ clientId: id }, 'CURSOR', REDIRECT, cfg), false, id);
    assert.equal(token({ client_id: id }).ok, false, id);
  }
});
test('S256, resource and scopes remain enforced before and after consent', () => {
  for (const over of [{ code_challenge_method: 'plain' }, { code_challenge: 'short' }, { response_type: 'token' },
    { scope: 'admin' }, { resource: 'https://evil.test/mcp' }]) assert.equal(pre(over).ok, false);
  for (const over of [{ codeChallengeMethod: 'plain' }, { codeChallenge: '' }, { scope: ['admin'] },
    { resource: 'https://evil.test/mcp' }]) assert.equal(postGateAuthRequest({ ...request, ...over }, cfg, 'CURSOR'), false);
});
test('code-token gate requires exact pair; refresh permits absent redirect but no substitutions', () => {
  assert.equal(token().ok, true);
  assert.equal(token({ client_secret: '' }).form.has('client_secret'), false);
  assert.equal(token({ redirect_uri: 'https://cursor.com/agents/mcp/oauth/callback' }).ok, false);
  assert.equal(gateTokenForm(new URLSearchParams({ grant_type: 'authorization_code', client_id: CLIENT }), null, cfg).ok, false);
  const repeated = new URLSearchParams({ grant_type: 'authorization_code', client_id: CLIENT, redirect_uri: REDIRECT });
  repeated.append('redirect_uri', REDIRECT);
  assert.equal(gateTokenForm(repeated, null, cfg).ok, false);
  assert.equal(token({}, 'Basic ' + btoa('ManualCursor12345:')).ok, false);
  assert.equal(token({}, 'Basic ' + btoa(encodeURIComponent(CLIENT) + ':')).ok, true);
  const refresh = new URLSearchParams({ grant_type: 'refresh_token', client_id: CLIENT, refresh_token: 'synthetic-unused-token' });
  assert.equal(gateTokenForm(refresh, null, cfg).ok, true);
  refresh.set('redirect_uri', 'https://www.cursor.com/agents/mcp/oauth/callback/');
  assert.equal(gateTokenForm(refresh, null, cfg).ok, false);
});
test('static HTTPS, JET, 127.0.0.1 and exact-pair native consent wording remain intact', () => {
  const manual = { clientId: 'ManualCursor12345', clientName: 'Cursor Cloud (manual form, CURSOR)' };
  const https = 'https://www.cursor.com/agents/mcp/oauth/callback';
  assert.equal(pre({ client_id: manual.clientId, redirect_uri: https }).ok, true);
  assert.equal(clientBoundToSeat(manual, 'CURSOR', https, cfg), true);
  assert.equal(pre({ redirect_uri: https }).ok, true, 'published CIMD is allowed only as the exact CURSOR pair');
  assert.equal(pre({ client_id: CHATGPT_CLIENT, redirect_uri: CHATGPT_REDIRECT }).ok, true);
  assert.equal(redirectUriProblem('http://127.0.0.1:8737/callback'), '');
  assert.equal(isLoopbackRedirect('http://127.0.0.1:8737/callback'), true);
  assert.equal(isCursorExactPair(CLIENT, REDIRECT, 'CURSOR'), true);
  const source = readFileSync(new URL('../src/index.js', import.meta.url), 'utf8');
  assert.match(source, /loopback: isLoopbackRedirect\(authRequest.redirectUri\)/);
  assert.match(source, /allowPlainPKCE: false/);
  assert.match(source, /disallowPublicClientRegistration: true/);
});

test('Cursor Cloud consent displays its HTTPS callback without a localhost warning', async () => {
  const response = consentPage({ clientName: 'Cursor', clientDomain: 'cursor.com', clientId: CLIENT,
    redirectHost: 'www.cursor.com', loopback: false, scopes: ['zulip:read'], seat: 'CURSOR',
    handle: 'synthetic-unused-consent', replacing: [], eventsOnly: false }, new Headers());
  const html = await response.text();
  assert.match(html, /www\.cursor\.com/);
  assert.doesNotMatch(html, /localhost must resolve/);
  assert.doesNotMatch(html, /127\.0\.0\.1 always/);
});
