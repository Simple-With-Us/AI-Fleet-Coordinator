import { test } from 'node:test';
import assert from 'node:assert/strict';
import { generateKeyPairSync } from 'node:crypto';
import { makeDeps, ascFailureText } from './ios_testflight_freshness.mjs';
import { readScopedAsc, main } from './testflight_asc_service.mjs';
const { privateKey } = generateKeyPairSync('ec', { namedCurve: 'prime256v1' });
const env = { ASC_KEY_ID: 'SYNTHETIC', ASC_ISSUER_ID: 'synthetic-issuer', ASC_KEY_P8: privateKey.export({ format: 'pem', type: 'pkcs8' }) };
test('reads GET only with scoped in-memory trio, even with unrelated fallback environment present', async () => {
  let calls = 0;
  const value = await readScopedAsc('/v1/apps?limit=1', { env: { ...env, ASC_KEY_PATH: '/must-not-read', FLEET_SECRETS: '/must-not-run' }, fetchImpl: async (url, options) => {
    calls++; assert.equal(url, 'https://api.appstoreconnect.apple.com/v1/apps?limit=1');
    assert.equal(options.method, 'GET'); assert.equal(options.redirect, 'error');
    assert.match(options.headers.Authorization, /^Bearer [^.]+\.[^.]+\.[^.]+$/);
    return new Response(JSON.stringify({ data: [] }));
  } });
  assert.deepEqual(value, { data: [] }); assert.equal(calls, 1);
});
test('missing or invalid injected trio never falls back and never requests', async () => {
  for (const missing of [{}, { ...env, ASC_KEY_P8: '' }, { ...env, ASC_KEY_P8: 'not-a-key' }]) {
    await assert.rejects(readScopedAsc('/v1/apps', { env: missing, fetchImpl: async () => { throw Error('must-not-request'); } }), /^Error: scoped-asc-read-unavailable$/);
  }
});
test('refuses outside host and path escapes before request', async () => {
  for (const path of ['https://example.invalid', '//example.invalid/v1/apps', '/v1/../apps', '/v1/apps%2Fsecret', '/v1/apps#fragment']) {
    await assert.rejects(readScopedAsc(path, { env, fetchImpl: async () => { throw Error('must-not-request'); } }), /scoped-asc-read-unavailable/);
  }
});
test('errors reveal neither credentials nor provider bodies and CLI cannot select a mutation', async () => {
  let out = '', err = '';
  const code = await main(['/v1/apps'], { env, fetchImpl: async () => new Response('sensitive-provider-body', { status: 401 }), out: {write:s=>out+=s}, err:{write:s=>err+=s} });
  assert.equal(code, 1); assert.equal(out, ''); assert.equal(err, 'scoped-asc-read-unavailable HTTP 401\n');
  assert.equal(await main(['POST','/v1/apps'], { err:{write:s=>{} } }), 1);
});
test('legacy flag and environment refuse before credentials or spawn', async () => {
  for (const [en, opts] of [[{IOS_TF_ALLOW_LEGACY_ASC_CREDS:'1'},{}],[{}, {allowLegacyAsc:true}]]) {
    let calls=0; const d=makeDeps(en,{...opts,fetchImpl:async()=>calls++,execFileImpl:()=>calls++,runCmd:async()=>calls++,serviceGitHub:async()=>{}});
    await assert.rejects(d.prepareAsc(), /legacy ASC credentials unsupported/);
    await assert.rejects(d.asc('/v1/apps'), /legacy ASC credentials unsupported/);
    assert.equal(calls,0);
  }
});
test('parent uses dedicated child and one GET path, strips fallback credentials', async () => {
  const project='00000000-0000-4000-8000-000000000001';let args,child;
  const d=makeDeps({IOS_TF_FRESHNESS_INFISICAL_TOKEN:'st.synthetic-scoped-service-reader',INFISICAL_FLEET_OPS_PROJECT_ID:project,ASC_KEY_PATH:'/must-not-read'}, {
    serviceGitHub:async()=>{},fetchImpl:async(url)=>new Response(JSON.stringify(url.endsWith('/v2/service-token')?{workspace:project,permissions:['read'],scopes:[{environment:'prod',secretPath:'/asc'}],expiresAt:'2099-01-01T00:00:00Z'}:{secret:{secretKey:decodeURIComponent(new URL(url).pathname.split('/').at(-1)),secretPath:'/asc',workspace:project,environment:'prod',type:'shared',secretValueHidden:false,secretValue:env[decodeURIComponent(new URL(url).pathname.split('/').at(-1))]}})),
    runCmd:async(cmd,a,o)=>{args=a;child=o.env;return '{"data":[]}';}
  });
  assert.deepEqual(await d.asc('/v1/apps?limit=1'),{data:[]});
  assert.equal(args.length,2);assert.ok(args[0].endsWith('/testflight_asc_service.mjs'));assert.equal(args[1],'/v1/apps?limit=1');assert.equal(child.ASC_KEY_P8,env.ASC_KEY_P8);assert.equal(child.ASC_KEY_PATH,undefined);assert.equal(child.IOS_TF_FRESHNESS_INFISICAL_TOKEN,undefined);
});
test('parent diagnosis emits only numeric HTTP status, not provider body', () => {
  assert.equal(ascFailureText({stderr:'scoped-asc-read-unavailable HTTP 403',stdout:'{"errors":[{"code":"sensitive-value"}]}',code:1},'/v1/apps'), 'App Store Connect /v1/apps: HTTP 403');
});
