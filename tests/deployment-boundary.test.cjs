const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const os = require('node:os');
const root = path.resolve(__dirname, '..');
const config = JSON.parse(fs.readFileSync(path.join(root, 'vercel.json')));

// public/logos is an explicit allowlist, not a wildcard: this guard exists to
// catch a private document or an unreviewed asset reaching the deployed site, so
// a new file must be added here deliberately.  Two sets, both copied verbatim
// from AI-Fleet-Coordinator/agent-logos/ (the fleet's canonical marks):
//   seat marks  — the quota panel's CSS-masked cursor, and the Fleet RAG card's
//                 seat chips / Top Seats bars
//   app icons   — the Fleet RAG card's Components panel, beside each app name
// See docs/provider-marks.md.
const SEAT_MARKS = [
  'claude.svg', 'codex.svg', 'cursor.png', 'grok.svg', 'grok-bot.svg',
  'monet.svg', 'ag.svg', 'gemini.svg', 'minimax.png', 'deepseek.svg',
  'kimi.svg', 'muse.svg', 'muse-assist.png', 'renoir.svg',
];
const APP_ICONS = [
  'app-st.svg', 'app-ct.png', 'app-um.png', 'app-bf.png', 'app-dd.png',
  'app-cl.png', 'app-ar.png', 'app-ps.png',
];
const SEAT_BADGES = [
  'badges/badge-claude.png',
  'badges/badge-codex.png',
  'badges/badge-deepseek.png',
  'badges/badge-grok.png',
  'badges/badge-grokbot.png',
  'badges/badge-mcode.png',
  'badges/badge-muse-code.png',
];
const LOGO_FILES = [...SEAT_MARKS, ...APP_ICONS, ...SEAT_BADGES].map((f) => `logos/${f}`).sort();

test('static output contains only intended web assets and no repository data', () => {
  assert.equal(config.outputDirectory, 'public');
  const files = fs.readdirSync(path.join(root, 'public'), { recursive: true }).sort();
  const expectedLogos = ['logos/badges', ...LOGO_FILES].sort();
  assert.deepEqual(files, ['index.html', 'logos', ...expectedLogos, 'robots.txt']);
  // Every declared logo must actually exist, so deleting one fails here rather
  // than shipping a 404 the page silently falls back from.
  for (const rel of LOGO_FILES) {
    assert.ok(fs.existsSync(path.join(root, 'public', rel)), `${rel} is declared but missing`);
  }
  assert.match(fs.readFileSync(path.join(root, 'public/index.html'), 'utf8'), /\/api\/quota/);
  assert.ok(config.functions['api/quota.js']);
  assert.equal(config.functions['api/rag-snapshot.js'].includeFiles, 'site-snapshot.json');
});

test('deployment input allowlist rejects private documents and future unknown files', () => {
  // Git and .vercelignore use ignore-style patterns; test with tracked-file checks disabled.
  const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-deploy-boundary-'));
  try {
    execFileSync('git', ['init', '-q', temp]);
    fs.copyFileSync(path.join(root, '.vercelignore'), path.join(temp, '.gitignore'));
    const allowed = [
      'public/index.html', 'public/robots.txt',
      ...LOGO_FILES.map((f) => `public/${f}`),
      'api/quota.js', 'api/rag-snapshot.js', 'site-snapshot.json', 'vercel.json', 'vercel-ignore.sh',
    ];
    // scripts/agent-sync-mcp is a separate Cloudflare Worker (docs/protocols/agent-sync-mcp.md)
    // that must never ship to Vercel.
    const denied = ['ATTACK-MAP.md', 'docs/DOMAINS-AND-ROUTING.md', 'docs/domains.json', 'docs/new-private-note.md', 'scripts/cloud-setup.sh', 'api/new-private-helper.js', '.env', 'new-inventory.json', 'scripts/agent-sync-mcp/src/index.js', 'scripts/agent-sync-mcp/wrangler.jsonc', 'scripts/agent-sync-mcp/infra_phase0.py'];
    for (const name of [...allowed, ...denied]) {
      fs.mkdirSync(path.dirname(path.join(temp, name)), { recursive: true });
      fs.writeFileSync(path.join(temp, name), 'fixture');
    }
    const ignored = execFileSync('git', ['-C', temp, 'check-ignore', '--no-index', ...allowed, ...denied], { encoding: 'utf8' }).trim().split('\n').sort();
    assert.deepEqual(ignored, denied.sort());
  } finally {
    fs.rmSync(temp, { recursive: true, force: true });
  }
});

test('snapshot API can read explicit bundled data without serving it statically', async () => {
  const handler = require('../api/rag-snapshot.js');
  const previous = process.cwd();
  const res = { headers: {}, setHeader(k, v) { this.headers[k] = v; }, status(n) { this.code = n; return this; }, send(s) { this.body = s; }, json(s) { this.body = JSON.stringify(s); } };
  try {
    process.chdir(root);
    await handler({}, res);
  } finally { process.chdir(previous); }
  assert.equal(res.code, 200);
  assert.equal(res.headers["Cache-Control"], "private, no-store");
  assert.deepEqual(JSON.parse(res.body), JSON.parse(fs.readFileSync(path.join(root, 'site-snapshot.json'))));
  assert.equal(fs.existsSync(path.join(root, 'public/site-snapshot.json')), false);
});
