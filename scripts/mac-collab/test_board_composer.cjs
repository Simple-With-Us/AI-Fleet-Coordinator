// Exercise the actual embedded board JavaScript, without touching the live board.
const assert = require('node:assert/strict');
const { execFileSync } = require('node:child_process');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const html = execFileSync('python3', ['-c', `
import ast, sys
module = ast.parse(open(sys.argv[1]).read())
for node in module.body:
    if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'BOARD_HTML' for t in node.targets):
        print(ast.literal_eval(node.value))
`, path.join(__dirname, 'mac-collab-server.py')], { encoding: 'utf8' });
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1]
  .replace('__AGENT_LOGOS_JSON__', '{}')
  .replace('__AGENT_SEATS_JSON__', '{}')
  .replace('__AGENT_ENVS_JSON__', '["cloud"]');

function board() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value: '', hidden: id === 'composer', style: {}, textContent: '',
      options: [], addEventListener() {}, focus() {},
      set innerHTML(value) {
        this.html = value;
        this.options = [...value.matchAll(/<option value="([^"]*)">([^<]*)<\/option>/g)]
          .map((m) => ({ value: m[1], label: m[2] }));
      },
      get innerHTML() { return this.html || ''; },
    });
    return elements.get(id);
  }
  const saved = new Map();
  const storage = { getItem: (key) => saved.get(key) || null, setItem: (key, value) => saved.set(key, value) };
  const requests = [];
  const context = vm.createContext({
    document: { getElementById: element }, localStorage: storage, sessionStorage: storage,
    location: { origin: 'http://board.test', search: '' }, URLSearchParams,
    setTimeout() {}, clearTimeout() {},
    fetch: async (url, opts) => {
      requests.push({ url, ...opts });
      return { ok: true, status: 200, json: async () => url.endsWith('/stats')
        ? { apps: ['fleetlink'], by_kind: {}, total: 0, open: 0, p0p1_open: 0, done: 0 }
        : { findings: [], count: 0, total_matching: 0 } };
    },
  });
  vm.runInContext(script, context);
  return { context, element, requests };
}

test('FleetLink is selectable before any finding exists, with its canonical slug', () => {
  const { context, element } = board();
  context.toggleComposer();
  const options = element('nApp').options;
  assert.deepEqual(options.filter((o) => o.value === 'fleetlink'), [{ value: 'fleetlink', label: 'FleetLink' }]);
  assert.equal(new Set(options.map((o) => o.value)).size, options.length);
  assert.ok(options.some((o) => o.value === 'socratic-trade'));
  assert.ok(options.some((o) => o.value === 'fleet-infra'));
});

test('Cancel and reopen preserve the FleetLink selection without duplicate options', () => {
  const { context, element } = board();
  context.toggleComposer();
  element('nApp').value = 'fleetlink';
  const before = element('nApp').innerHTML;
  context.toggleComposer();
  assert.equal(element('composer').hidden, true);
  context.toggleComposer();
  assert.equal(element('composer').hidden, false);
  assert.equal(element('nApp').value, 'fleetlink');
  assert.equal(element('nApp').innerHTML, before);
});

test('File it submits FleetLink with canonical app identity', async () => {
  const { context, element, requests } = board();
  context.toggleComposer();
  element('nApp').value = 'fleetlink';
  element('nTitle').value = 'FleetLink test finding';
  await context.createItem();
  const posts = requests.filter((r) => r.method === 'POST');
  assert.equal(posts.length, 1);
  assert.equal(JSON.parse(posts[0].body).app, 'fleetlink');
  assert.equal(element('nMsg').textContent, 'Filed.');
});

test('Missing title still blocks submission', async () => {
  const { context, element, requests } = board();
  context.toggleComposer();
  element('nApp').value = 'fleetlink';
  await context.createItem();
  assert.equal(requests.filter((r) => r.method === 'POST').length, 0);
  assert.equal(element('nMsg').textContent, 'Title is required.');
});
