import { test } from 'node:test';
import assert from 'node:assert/strict';
import worker from './worker.js';

const env = { ASSETS: { fetch: () => new Response('public asset') } };
test('page aliases reach protected home before static assets', async () => {
  for (const path of ['/', '/index.html', '/start', '/start/']) {
    const r = await worker.fetch(new Request('https://start.jays.services' + path), env);
    assert.equal(r.status, 302);
    assert.equal(r.headers.get('location'), 'https://home.jays.services/');
    assert.equal(r.headers.get('cache-control'), 'no-store');
  }
});
test('only explicit safe assets are served', async () => {
  assert.equal((await worker.fetch(new Request('https://start.jays.services/robots.txt'), env)).status, 200);
  assert.equal((await worker.fetch(new Request('https://start.jays.services/apple-touch-icon.png'), env)).status, 200);
  for (const path of ['/private.html', '/public/index.html', '/docs/inventory.json']) {
    assert.equal((await worker.fetch(new Request('https://start.jays.services' + path), env)).status, 404);
  }
});
test('old local copies keep the suggestion endpoint and preflight', async () => {
  const r = await worker.fetch(new Request('https://start.jays.services/suggest?q='), env);
  assert.deepEqual(await r.json(), { q: '', suggestions: [] });
  assert.equal(r.headers.get('access-control-allow-origin'), '*');
  const preflight = await worker.fetch(new Request('https://start.jays.services/suggest', { method: 'OPTIONS' }), env);
  assert.equal(preflight.headers.get('access-control-allow-methods'), 'GET, OPTIONS');
});
