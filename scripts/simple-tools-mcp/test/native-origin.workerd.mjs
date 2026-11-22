// Exercise the projection client's real fetch options on the pinned edge runtime.
import { build } from "esbuild";
import { Miniflare, convertV4MiniflareOptions } from "miniflare";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { wranglerConfig } from "./helpers.mjs";

const session = "019a1234-abcd-7123-8123-0123456789ab";
const claims = { origin_session_claim: session, origin_letter_claim: "AA", origin_generation_claim: 7 };
const dir = mkdtempSync(path.join(tmpdir(), "native-origin-workerd-"));
const out = path.join(dir, "worker.mjs");
await build({
  stdin: {
    contents: `import { nativeOriginResolver } from './src/native-origin-client.js';
      export default { async fetch(request, env) {
        try {
          const result = await nativeOriginResolver(env, { now: () => 1795309200000 })({ seat: 'CODEX', session: '${session}' });
          return Response.json({ result });
        } catch (error) { return Response.json({ error: error.message }, { status: 500 }); }
      }};`,
    resolveDir: process.cwd(),
  },
  bundle: true, format: "esm", platform: "browser",
  conditions: ["workerd", "worker", "browser"], outfile: out, logLevel: "error",
});
const w = wranglerConfig();
try {
  for (const status of [200, 302, 401]) {
    let calls = 0;
    const mf = new Miniflare(convertV4MiniflareOptions({ workers: [{
      name: "origin-probe", modules: true, script: readFileSync(out, "utf8"),
      compatibilityDate: w.compatibility_date, compatibilityFlags: w.compatibility_flags,
      bindings: {
        REVIEW_ORIGIN_READ_URL: "https://origin.example/registry/v1/review-origin",
        REVIEW_ORIGIN_CODEX_READ_KEY: "q".repeat(48),
      },
      outboundService(request) {
        calls += 1;
        assert.equal(request.method, "GET");
        const url = new URL(request.url);
        assert.equal(url.host, "origin.example", "the signed request must never follow a redirect");
        assert.equal(url.pathname, "/registry/v1/review-origin");
        assert.deepEqual([...url.searchParams], [["native_session_id", session]]);
        assert.equal(url.search, `?native_session_id=${session}`);
        const timestamp = request.headers.get("X-Simple-Tools-Timestamp");
        assert.equal(timestamp, "1795309200");
        assert.equal(Number(timestamp), 1795309200);
        const canonical = `simple-tools-review-origin/1\nGET\n/registry/v1/review-origin\nnative_session_id=${session}\n${timestamp}`;
        const expected = createHmac("sha256", "q".repeat(48)).update(canonical).digest("hex");
        assert.equal(request.headers.get("X-Simple-Tools-Signature"), expected);
        return status === 200 ? Response.json(claims)
          : new Response("held", { status, headers: status === 302 ? { Location: "https://unexpected.example/" } : {} });
      },
    }] }));
    try {
      const response = await mf.dispatchFetch("https://probe.example/");
      const result = await response.json();
      assert.equal(calls, 1, "each lookup makes one signed request without following redirects");
      if (status === 200) {
        assert.equal(response.status, 200);
        assert.deepEqual(result.result, claims);
      } else {
        assert.equal(response.status, 500);
        assert.equal(result.error, "lookup-held");
      }
      console.log(`ok - native projection actual workerd fetch, upstream ${status}`);
    } finally { await mf.dispose(); }
  }
} finally { rmSync(dir, { recursive: true, force: true }); }
