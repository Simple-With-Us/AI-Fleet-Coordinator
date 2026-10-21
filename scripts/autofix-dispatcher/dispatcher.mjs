#!/usr/bin/env node
// Autofix dispatcher (trial, owner 2026-10-21):  pick the oldest open fleet PR that has
// review-bot findings and no autofix marker from any bot, then start one run by posting
// the @pr-autofix tag as swu-echo[bot].  The run itself is the repo's existing
// pr-autofix lane;  the reusable owns the per-PR marker comment, so "tried" state stays
// in exactly one place.  One dispatch per tick, and no dispatch while any fleet autofix
// run is queued or in progress.  A JSONL tally on the autofix-dispatcher-state branch
// records every dispatch and is reconciled with run outcomes and cost on later ticks.
//
//   node scripts/autofix-dispatcher/dispatcher.mjs [--dry-run] [--json] [--repos a,b]
//
// Requires GH_TOKEN with cross-repo access (the swu-echo installation token).

import { execFileSync } from 'node:child_process';
import { parseArgs } from 'node:util';
import {
  dispatchCommentBody,
  extractCostUsd,
  extractRunId,
  hasAnyAutofixMarker,
  hasReviewFindings,
  markerBot,
  parseTally,
  pickOldest,
  tallyTotalUsd,
} from './lib.mjs';

const OWNER = 'Simple-With-Us';
const STATE_REPO = 'AI-Fleet-Coordinator';
const STATE_BRANCH = 'autofix-dispatcher-state';
const STATE_PATH = 'autofix-dispatcher/tally.jsonl';
const AUTOFIX_WORKFLOW_FILES = new Set([
  '.github/workflows/autofix.yml',
  '.github/workflows/claude-code-autofix.yml',
]);
// Every repo with a pr-autofix caller whose effective allowed_bots includes
// swu-echo[bot].  homebrew-tap's caller names an explicit list without it, so the
// tag would never fire there;  FleetLink has no pr-autofix caller.
const DEFAULT_REPOS = [
  'AI-Fleet-Coordinator',
  'BotFleet',
  'Clutch',
  'CodeCaps',
  'Congress.Trade',
  'ContactLogo',
  'DealDex',
  'HogHunter',
  'Kodus-Config',
  'Personal-Site',
  'Simple-With-Us',
  'Socratic-Trade',
  'Usage-Monitor',
];

const { values: args } = parseArgs({
  options: {
    'dry-run': { type: 'boolean', default: false },
    json: { type: 'boolean', default: false },
    repos: { type: 'string', default: '' },
  },
});

function gh(argv, input = null) {
  return execFileSync('gh', argv, {
    encoding: 'utf8',
    maxBuffer: 64 * 1024 * 1024,
    input: input == null ? undefined : input,
  });
}

function ghApiList(path, jq = '.[] | @json') {
  let out;
  try {
    out = gh(['api', path, '--paginate', '--jq', jq]);
  } catch {
    return [];
  }
  return out
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function ghApiObject(path) {
  try {
    return JSON.parse(gh(['api', path]));
  } catch {
    return null;
  }
}

function autofixRunsActive(repos) {
  const active = [];
  for (const repo of repos) {
    for (const status of ['in_progress', 'queued']) {
      const data = ghApiObject(`repos/${OWNER}/${repo}/actions/runs?status=${status}&per_page=20`);
      for (const run of data?.workflow_runs || []) {
        if (AUTOFIX_WORKFLOW_FILES.has(run.path)) {
          active.push({ repo, run_id: run.id, status, path: run.path });
        }
      }
    }
  }
  return active;
}

function findCandidates(repos) {
  const candidates = [];
  for (const repo of repos) {
    const pulls = ghApiList(`repos/${OWNER}/${repo}/pulls?state=open&per_page=100`);
    for (const pr of pulls) {
      if (pr.draft) continue;
      const issueComments = ghApiList(
        `repos/${OWNER}/${repo}/issues/${pr.number}/comments?per_page=100`,
        '.[] | {body} | @json',
      );
      if (hasAnyAutofixMarker(issueComments)) continue;
      const reviewComments = ghApiList(
        `repos/${OWNER}/${repo}/pulls/${pr.number}/comments?per_page=100`,
        '.[] | {body, user: {login: .user.login}} | @json',
      );
      if (!hasReviewFindings(reviewComments)) continue;
      candidates.push({ repo, number: pr.number, created_at: pr.created_at, url: pr.html_url });
    }
  }
  return candidates;
}

function loadTally() {
  const branch = ghApiObject(`repos/${OWNER}/${STATE_REPO}/git/refs/heads/${STATE_BRANCH}`);
  if (!branch) return { entries: [], sha: null };
  const file = ghApiObject(`repos/${OWNER}/${STATE_REPO}/contents/${STATE_PATH}?ref=${STATE_BRANCH}`);
  if (!file?.content) return { entries: [], sha: null };
  return { entries: parseTally(Buffer.from(file.content, 'base64').toString('utf8')), sha: file.sha };
}

function reconcileEntry(entry) {
  if (entry.run_id == null) {
    const comments = ghApiList(
      `repos/${OWNER}/${entry.repo}/issues/${entry.number}/comments?per_page=100`,
      '.[] | {body} | @json',
    );
    const marker = comments.find(
      (comment) => markerBot(comment.body) === 'swu-echo[bot]' && extractRunId(comment.body),
    );
    if (marker) entry.run_id = extractRunId(marker.body);
  }
  if (entry.run_id != null && entry.conclusion == null) {
    const run = ghApiObject(`repos/${OWNER}/${entry.repo}/actions/runs/${entry.run_id}`);
    if (run?.status === 'completed') {
      entry.conclusion = run.conclusion;
      entry.run_url = run.html_url;
      try {
        const logs = gh(['run', 'view', String(entry.run_id), '--repo', `${OWNER}/${entry.repo}`, '--log']);
        entry.cost_usd = extractCostUsd(logs);
      } catch {
        entry.cost_usd = null;
      }
    }
  }
  return entry;
}

function saveTally(entries, existingSha) {
  const content = Buffer.from(entries.map((entry) => JSON.stringify(entry)).join('\n') + '\n').toString('base64');
  const main = ghApiObject(`repos/${OWNER}/${STATE_REPO}/git/refs/heads/main`);
  try {
    gh(['api', '-X', 'POST', `repos/${OWNER}/${STATE_REPO}/git/refs`, '-f', `ref=refs/heads/${STATE_BRANCH}`, '-f', `sha=${main.object.sha}`]);
  } catch {
    // The state branch already exists.
  }
  const body = { message: 'autofix dispatcher tally', content, branch: STATE_BRANCH };
  if (existingSha) body.sha = existingSha;
  gh(['api', '-X', 'PUT', `repos/${OWNER}/${STATE_REPO}/contents/${STATE_PATH}`, '--input', '-'], JSON.stringify(body));
}

function print(result, asJson) {
  if (asJson) {
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
    return;
  }
  process.stdout.write(`busy: ${result.busy.length} active autofix run(s)\n`);
  for (const run of result.busy) process.stdout.write(`  ${run.repo} run ${run.run_id} ${run.status}\n`);
  process.stdout.write(`candidates: ${result.candidate_count}\n`);
  process.stdout.write(`pick: ${result.pick ? `${result.pick.repo}#${result.pick.number} (${result.pick.created_at})` : 'none'}\n`);
  process.stdout.write(`dispatched: ${result.dispatched}\n`);
  if (result.tally_total_usd != null) process.stdout.write(`tally total: $${result.tally_total_usd.toFixed(4)} across ${result.tally_runs} dispatch(es)\n`);
}

function main() {
  const repos = args.repos
    ? args.repos.split(',').map((name) => name.trim()).filter(Boolean)
    : DEFAULT_REPOS;
  const active = autofixRunsActive(repos);
  const result = { busy: active, candidate_count: 0, pick: null, dispatched: false };

  if (active.length > 0) {
    print(result, args.json);
    return;
  }

  const candidates = findCandidates(repos);
  const pick = pickOldest(candidates);
  result.candidate_count = candidates.length;
  result.pick = pick;

  if (!pick || args['dry-run']) {
    print(result, args.json);
    return;
  }

  // Recheck immediately before spending: a run started between the scan and now wins.
  if (autofixRunsActive(repos).length > 0) {
    print(result, args.json);
    return;
  }

  const posted = JSON.parse(
    gh([
      'api', '-X', 'POST', `repos/${OWNER}/${pick.repo}/issues/${pick.number}/comments`,
      '-f', `body=${dispatchCommentBody()}`,
    ]),
  );
  result.dispatched = true;
  result.comment_url = posted.html_url;

  const tally = loadTally();
  tally.entries = tally.entries.map(reconcileEntry);
  tally.entries.push({
    ts: new Date().toISOString(),
    repo: pick.repo,
    number: pick.number,
    pr_url: pick.url,
    comment_url: posted.html_url,
    run_id: null,
    run_url: null,
    conclusion: null,
    cost_usd: null,
  });
  saveTally(tally.entries, tally.sha);
  result.tally_total_usd = tallyTotalUsd(tally.entries);
  result.tally_runs = tally.entries.length;

  print(result, args.json);
}

main();
