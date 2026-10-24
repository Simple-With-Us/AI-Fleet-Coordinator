// Runs dispatcher.mjs end to end against a fake `gh` that serves canned GitHub responses
// from a scenario file and logs every call, so the control flow (what gets posted, what
// gets saved, when a tick aborts) is covered without touching GitHub.

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { chmodSync, existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { dispatchCommentBody } from './lib.mjs';

const DISPATCHER = join(dirname(fileURLToPath(import.meta.url)), 'dispatcher.mjs');
const OWNER = 'Simple-With-Us';
const REPO = 'BotFleet';
const REPO_API = `repos/${OWNER}/${REPO}`;
const STATE_API = `repos/${OWNER}/AI-Fleet-Coordinator`;
const STATE_BRANCH_REF = `${STATE_API}/git/refs/heads/autofix-dispatcher-state`;
const STATE_FILE = `${STATE_API}/contents/autofix-dispatcher/tally.jsonl`;

const NOT_FOUND = { __error: 'HTTP 404: Not Found' };
// The fake gh runs the caller's --jq through the real jq;  ubuntu-latest and macOS ship it.
const JQ_MISSING = spawnSync('jq', ['--version']).status !== 0 && 'jq is not installed';
const check = (name, fn) => test(name, { skip: JQ_MISSING }, fn);
const FINDING = { body: '**Bug** something', user: { login: 'kody-ai[bot]' } };

// The fake gh: `gh api [-X METHOD] <path> [--paginate --jq X] [-f k=v] [--input -]` and
// `gh run view ...`.  A route that the scenario does not name is a loud 500, never a 404,
// so a test cannot pass by accident through a call it did not mean to allow.  A `--jq`
// the caller passes runs through the real jq, as gh runs it, so every response reaches
// the dispatcher through the same projection production uses.
const FAKE_GH = `#!${process.execPath}
const fs = require('node:fs');
const argv = process.argv.slice(2);
fs.appendFileSync(process.env.FAKE_GH_LOG, JSON.stringify(argv) + '\\n');
const fail = (message) => { process.stderr.write(message + '\\n'); process.exit(1); };
// The scenario is a fixture this file writes, not outside input;  the shape check only
// turns a malformed one into a named failure instead of a TypeError inside a route.
const scenario = JSON.parse(fs.readFileSync(process.env.FAKE_GH_SCENARIO, 'utf8'));
if (!scenario || typeof scenario.routes !== 'object' || scenario.routes === null || Array.isArray(scenario.routes)) {
  fail('fake gh scenario: routes must be an object');
}
if (scenario.runLog != null && typeof scenario.runLog !== 'string') fail('fake gh scenario: runLog must be a string');
if (argv[0] === 'run') {
  if (scenario.runLog == null) fail('HTTP 404: Not Found');
  process.stdout.write(scenario.runLog);
  process.exit(0);
}
let method = 'GET';
let path = null;
let jq = null;
for (let i = 1; i < argv.length; i += 1) {
  if (argv[i] === '-X') { method = argv[i + 1]; i += 1; }
  else if (argv[i] === '--jq') { jq = argv[i + 1]; i += 1; }
  else if (['-f', '--input'].includes(argv[i])) { i += 1; }
  else if (!argv[i].startsWith('-') && path === null) { path = argv[i]; }
}
const key = method + ' ' + path;
if (!(key in scenario.routes)) fail('HTTP 500: fake gh has no route for ' + key);
const value = scenario.routes[key];
if (value && value.__error) fail(value.__error);
if (method === 'PUT') fs.writeFileSync(process.env.FAKE_GH_PUT, fs.readFileSync(0, 'utf8'));
if (argv.includes('--paginate') && !Array.isArray(value)) {
  fail('fake gh scenario: route ' + key + ' must be a list for --paginate');
}
if (jq !== null) {
  // gh prints a string result raw and anything else as compact JSON:  jq -r -c.
  const out = require('node:child_process').spawnSync('jq', ['-r', '-c', jq], { input: JSON.stringify(value), encoding: 'utf8' });
  if (out.status !== 0) fail('fake gh: jq failed for ' + key + ': ' + out.stderr);
  process.stdout.write(out.stdout);
} else if (argv.includes('--paginate')) {
  for (const item of value) process.stdout.write(JSON.stringify(item) + '\\n');
} else {
  process.stdout.write(JSON.stringify(value));
}
`;

function pr(number, {
  created = '2026-10-20T00:00:00Z', fullName = `${OWNER}/${REPO}`, baseName = `${OWNER}/${REPO}`, ref = 'fix',
} = {}) {
  return {
    number,
    created_at: created,
    draft: false,
    html_url: `https://github.com/${OWNER}/${REPO}/pull/${number}`,
    head: { ref, repo: fullName ? { full_name: fullName } : null },
    base: { repo: { full_name: baseName } },
  };
}

function dispatchComment(number) {
  return {
    body: dispatchCommentBody({ owner: OWNER, repo: REPO, number }),
    html_url: `https://github.com/${OWNER}/${REPO}/pull/${number}#issuecomment-${number}`,
    user: { login: 'swu-echo[bot]' },
  };
}

// tally: null means the state branch does not exist yet;  otherwise the existing entries.
function scenario(pulls, { issueComments = {}, tally = null, activeRuns = [], extra = {}, withState = true } = {}) {
  const routes = {
    [`GET ${REPO_API}/actions/runs?status=in_progress&per_page=100`]: { workflow_runs: activeRuns },
    [`GET ${REPO_API}/actions/runs?status=queued&per_page=100`]: { workflow_runs: [] },
    [`GET ${REPO_API}/pulls?state=open&per_page=100`]: pulls,
  };
  for (const item of pulls) {
    routes[`GET ${REPO_API}/issues/${item.number}/comments?per_page=100`] = issueComments[item.number] || [];
    routes[`GET ${REPO_API}/pulls/${item.number}/comments?per_page=100`] = [FINDING];
    routes[`POST ${REPO_API}/issues/${item.number}/comments`] = {
      html_url: `https://github.com/${OWNER}/${REPO}/pull/${item.number}#issuecomment-new`,
    };
  }
  if (withState) {
    if (tally === null) {
      routes[`GET ${STATE_BRANCH_REF}`] = NOT_FOUND;
      routes[`POST ${STATE_API}/git/refs`] = {};
    } else {
      routes[`GET ${STATE_BRANCH_REF}`] = { object: { sha: 'branchsha' } };
      routes[`GET ${STATE_FILE}?ref=autofix-dispatcher-state`] = {
        content: Buffer.from(tally.map((entry) => `${JSON.stringify(entry)}\n`).join('')).toString('base64'),
        sha: 'tallysha',
      };
      routes[`POST ${STATE_API}/git/refs`] = { __error: 'HTTP 422: Reference already exists' };
    }
    routes[`GET ${STATE_API}/git/refs/heads/main`] = { object: { sha: 'mainsha' } };
    routes[`PUT ${STATE_FILE}`] = {};
  }
  return { routes: { ...routes, ...extra } };
}

function run(scenarioValue, flags = [], { json = true } = {}) {
  const dir = mkdtempSync(join(tmpdir(), 'fake-gh-'));
  const gh = join(dir, 'gh');
  writeFileSync(gh, FAKE_GH);
  chmodSync(gh, 0o755);
  const files = { scenario: join(dir, 'scenario.json'), log: join(dir, 'log.jsonl'), put: join(dir, 'put.json') };
  writeFileSync(files.scenario, JSON.stringify(scenarioValue));
  writeFileSync(files.log, '');
  const child = spawnSync(process.execPath, [DISPATCHER, ...(json ? ['--json'] : []), '--repos', REPO, ...flags], {
    encoding: 'utf8',
    env: {
      ...process.env,
      PATH: `${dir}${delimiter}${process.env.PATH}`,
      FAKE_GH_SCENARIO: files.scenario,
      FAKE_GH_LOG: files.log,
      FAKE_GH_PUT: files.put,
    },
  });
  const calls = readFileSync(files.log, 'utf8').split('\n').filter(Boolean).map((line) => JSON.parse(line));
  const posts = calls.filter((argv) => argv.includes('POST') && argv.some((arg) => arg.endsWith('/comments')));
  let saved = null;
  if (existsSync(files.put)) {
    const content = JSON.parse(readFileSync(files.put, 'utf8')).content;
    saved = Buffer.from(content, 'base64').toString('utf8').split('\n').filter(Boolean).map((line) => JSON.parse(line));
  }
  let result = null;
  try {
    result = JSON.parse(child.stdout);
  } catch {
    // An aborted tick may print nothing parseable;  the assertions look at stderr.
  }
  return { status: child.status, stdout: child.stdout, stderr: child.stderr, result, calls, posts, saved };
}

check('a PR the dispatcher already tagged is recorded and skipped, so the next PR is dispatched', () => {
  const out = run(scenario(
    [pr(1, { created: '2026-10-19T00:00:00Z' }), pr(2)],
    { issueComments: { 1: [dispatchComment(1)] } },
  ));
  assert.equal(out.status, 0, out.stderr);
  assert.equal(out.result.pick.number, 2);
  assert.equal(out.result.candidate_count, 1);
  assert.equal(out.result.dispatched, true);
  assert.equal(out.posts.length, 1);
  assert.ok(out.posts[0].includes(`${REPO_API}/issues/2/comments`));
  // GitHub text takes a real U+00A0 plus a normal space between sentences (owner ruling
  // 2026-10-23);  only the Claude Code and MiniMax Code chat panes keep the `&nbsp;` entity.
  const posted = out.posts[0].find((arg) => arg.startsWith('body=')).slice('body='.length);
  assert.doesNotMatch(posted, /[.!?] {2}/);
  assert.equal(posted.match(/[.!?]\u00A0 /g).length, 4);
  assert.ok(!posted.includes('&nbsp;'), 'the posted body never carries the entity');
  // PR 1's tag never reached the tally (the tick that posted it died), so it is recorded now.
  assert.deepEqual(out.saved.map((entry) => entry.number).sort(), [1, 2]);
  assert.match(out.saved.find((entry) => entry.number === 1).comment_url, /issuecomment-1$/);
});

check('a same-repo PR is picked from the full pulls payload through the real projection', () => {
  // A realistic listing row:  extra fields, and head and base names that differ in case.
  const row = {
    ...pr(2),
    title: 'fix', user: { login: 'someone' }, base: { ref: 'main', repo: { full_name: `${OWNER.toLowerCase()}/${REPO.toLowerCase()}`, private: false } },
  };
  const out = run(scenario([row]));
  assert.equal(out.status, 0, out.stderr);
  assert.equal(out.result.candidate_count, 1);
  assert.equal(out.result.pick.number, 2);
  const listing = out.calls.find((argv) => argv.some((arg) => arg.endsWith('/pulls?state=open&per_page=100')));
  assert.ok(listing.includes('--jq'), 'the listing went through a jq projection');
});

check('a fork PR is never a candidate', () => {
  const out = run(scenario(
    [pr(1, { created: '2026-10-19T00:00:00Z', fullName: 'someone/BotFleet' }), pr(2), pr(3, { fullName: null })],
  ));
  assert.equal(out.status, 0, out.stderr);
  assert.equal(out.result.pick.number, 2);
  assert.equal(out.result.candidate_count, 1);
});

check('a repo renamed on GitHub keeps its PRs:  same-repo is head against base, not the configured name', () => {
  // The API answers for the configured (old) name with PRs that carry the new one.
  const renamed = `${OWNER}/BotFleet-Renamed`;
  const out = run(scenario([pr(2, { fullName: renamed, baseName: renamed })]));
  assert.equal(out.status, 0, out.stderr);
  assert.equal(out.result.candidate_count, 1);
  assert.equal(out.result.pick.number, 2);
});

check('a reconcile lookup that fails is a warning and does not block the dispatch', () => {
  const stuck = { ts: '2026-10-01T00:00:00Z', repo: REPO, number: 9, run_id: '555', conclusion: null, cost_usd: null };
  const out = run(scenario([pr(2)], {
    tally: [stuck],
    extra: { [`GET ${REPO_API}/actions/runs/555`]: { __error: 'HTTP 404: Not Found' } },
  }));
  assert.equal(out.status, 0, out.stderr);
  assert.equal(out.result.dispatched, true);
  assert.match(out.stderr, /warning: reconcile lookup failed/);
  assert.deepEqual(out.saved.map((entry) => entry.number).sort(), [2, 9]);
  assert.ok(out.saved.find((entry) => entry.number === 9).checked_at, 'the failed entry is stamped so it rotates to the back');
});

check('a tally that could not be read is never written back, and nothing is dispatched', () => {
  const out = run(scenario([pr(2)], {
    tally: [],
    extra: { [`GET ${STATE_FILE}?ref=autofix-dispatcher-state`]: { __error: 'HTTP 502: Bad Gateway' } },
  }));
  assert.equal(out.status, 1);
  assert.match(out.stderr, /aborted: 1 GitHub API call/);
  assert.match(out.stderr, /HTTP 502/);
  assert.equal(out.saved, null, 'no PUT of a stand-in tally');
  assert.equal(out.posts.length, 0);
  // The stand-in is empty, so a total from it would be a false zero.
  assert.equal('tally_total_usd' in out.result, false);
  assert.equal('tally_runs' in out.result, false);
  assert.equal(out.result.tally_unavailable, true);
});

check('the text summary says the tally is unavailable instead of printing a false zero', () => {
  const out = run(scenario([pr(2)], {
    tally: [{ ts: '2026-10-01T00:00:00Z', repo: REPO, number: 9, run_id: '555', conclusion: 'success', cost_usd: 0.42 }],
    extra: { [`GET ${STATE_FILE}?ref=autofix-dispatcher-state`]: { __error: 'HTTP 502: Bad Gateway' } },
  }), [], { json: false });
  assert.equal(out.status, 1);
  assert.match(out.stdout, /tally total: unavailable/);
  assert.doesNotMatch(out.stdout, /\$0\.0000/);
});

check('a readable tally reports its running total', () => {
  const out = run(scenario([], {
    tally: [{ ts: '2026-10-01T00:00:00Z', repo: REPO, number: 9, run_id: '555', conclusion: 'success', cost_usd: 0.42 }],
  }));
  assert.equal(out.status, 0, out.stderr);
  assert.equal(out.result.tally_total_usd, 0.42);
  assert.equal(out.result.tally_runs, 1);
  assert.equal('tally_unavailable' in out.result, false);
});

check('an active autofix run ends the tick without a dispatch', () => {
  const out = run(scenario([pr(2)], {
    activeRuns: [{ id: 77, path: '.github/workflows/autofix.yml' }],
  }));
  assert.equal(out.status, 0, out.stderr);
  assert.equal(out.result.busy.length, 1);
  assert.equal(out.result.dispatched, false);
  assert.equal(out.posts.length, 0);
});

check('a dry run picks without posting, saving or reporting a tally total', () => {
  const out = run(scenario([pr(2)], { withState: false }), ['--dry-run']);
  assert.equal(out.status, 0, out.stderr);
  assert.equal(out.result.pick.number, 2);
  assert.equal(out.result.dispatched, false);
  assert.equal('tally_total_usd' in out.result, false);
  assert.equal(out.posts.length, 0);
  assert.equal(out.saved, null);
});
