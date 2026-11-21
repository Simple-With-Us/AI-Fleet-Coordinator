// Freshness-owned read-only ASC child. Only the scoped parent-provided in-memory trio is accepted.
import { createPrivateKey, sign } from 'node:crypto';
import { pathToFileURL } from 'node:url';
const fail = () => new Error('scoped-asc-read-unavailable');
export async function readScopedAsc(apiPath, { env = process.env, fetchImpl = fetch, now = Date.now } = {}) {
  try {
    if (typeof apiPath !== 'string' || !/^\/v[12]\/[A-Za-z0-9]/.test(apiPath) || /[\\\s#]/.test(apiPath) || /%(?:2e|2f|5c|25)/i.test(apiPath) || apiPath.split(/[/?]/).some(p => p === '.' || p === '..')) throw fail();
    const url = new URL('https://api.appstoreconnect.apple.com' + apiPath);
    if (url.origin !== 'https://api.appstoreconnect.apple.com') throw fail();
    const id = env.ASC_KEY_ID, issuer = env.ASC_ISSUER_ID, pem = env.ASC_KEY_P8;
    if (!id || !issuer || !pem || /[\s\0]/.test(id) || /[\s\0]/.test(issuer)) throw fail();
    const key = createPrivateKey(pem);
    if (key.asymmetricKeyType !== 'ec' || key.asymmetricKeyDetails?.namedCurve !== 'prime256v1') throw fail();
    const t = Math.floor(now() / 1000), enc = x => Buffer.from(JSON.stringify(x)).toString('base64url');
    const unsigned = enc({ alg: 'ES256', kid: id, typ: 'JWT' }) + '.' + enc({ iss: issuer, iat: t - 30, exp: t + 300, aud: 'appstoreconnect-v1' });
    const jwt = unsigned + '.' + sign('sha256', Buffer.from(unsigned), { key, dsaEncoding: 'ieee-p1363' }).toString('base64url');
    const response = await fetchImpl(url.href, { method: 'GET', redirect: 'error', signal: AbortSignal.timeout(30000), headers: { Authorization: `Bearer ${jwt}`, Accept: 'application/json' } });
    if (!response.ok) { const e = fail(); if (Number.isInteger(response.status) && response.status >= 100 && response.status <= 599) e.httpStatus = response.status; throw e; }
    if (!response.body?.getReader) throw fail();
    const reader = response.body.getReader(), chunks = []; let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        size += value.byteLength; if (size > 4 * 1024 * 1024) throw fail(); chunks.push(value);
      }
    } finally { await reader.cancel().catch(() => {}); }
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch (err) { const e = fail(); if (Number.isInteger(err?.httpStatus) && err.httpStatus >= 100 && err.httpStatus <= 599) e.httpStatus = err.httpStatus; throw e; }
}
export async function main(argv = process.argv.slice(2), options = {}) {
  try {
    if (argv.length !== 1) throw fail();
    const value = await readScopedAsc(argv[0], options);
    (options.out || process.stdout).write(JSON.stringify(value) + '\n'); return 0;
  } catch (err) { (options.err || process.stderr).write('scoped-asc-read-unavailable' + (err.httpStatus ? ` HTTP ${err.httpStatus}` : '') + '\n'); return 1; }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) process.exitCode = await main();
