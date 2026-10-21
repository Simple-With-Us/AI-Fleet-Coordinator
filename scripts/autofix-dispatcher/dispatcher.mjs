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
  DISPATCHER_BOT,
  DISPATCHER_MARKER_BOT,
  DISPATCH_TAG,
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
// Every fleet workflow whose runs spend model tokens, including BotFleet's
// manual-dispatch paid pilot.  Run `path` values are the caller file names,
// never the reusables they call.
const AUTOFIX_WORKFLOW_FILES = new Set([
  '.github/workflows/autofix.yml',
  '.github/workflows/claude-code-autofix.yml',
  '.github/workflows/kody-fleet-autofix.yml',
  '.github/workflows/kody-autofix-pilot.yml',
]);
// At most this many tally entries are reconciled per tick, so a long backlog
// cannot stretch the scheduled job past its window.
const RECONCILE_CAP = 20;
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

// Every failed API call lands here.  main() aborts the tick when the list is
// non-empty before any dispatch decision: a silently empty scan would look
// like "no candidates" or, worse, "no marker yet" and double-spend a PR's one
// autofix run.
const apiErrors = [];

function ghApiList(path, jq = '.[] | @json') {
  let out;
  try {
    out = gh(['api', path, '--paginate', '--jq', jq]);
  } catch (error) {
    apiErrors.push({ path, error: String(error.stderr || error.message || error).slice(0, 200) });
    return [];
  }
  return out
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

function ghApiObject(path, { notFoundOk = false } = {}) {
  try {
    return JSON.parse(gh(['api', path]));
  } catch (error) {
    const text = String(error.stderr || error.message || error);
    if (notFoundOk && /HTTP 404|Not Found/.test(text)) return null;
    apiErrors.push({ path, error: text.slice(0, 200) });
    return null;
  }
}

function autofixRunsActive(repos) {
  const active = [];
  for (const repo of repos) {
    for (const status of ['in_progress', 'queued']) {
      const data = ghApiObject(`repos/${OWNER}/${repo}/actions/runs?status=${status}&per_page=100`);
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
      // A missing or unparseable created_at would sort to epoch 0 and look
      // like the oldest PR, so such candidates are skipped, never coerced.
      if (pr.draft || Number.isNaN(Date.parse(pr.created_at))) continue;
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
  const branch = ghApiObject(`repos/${OWNER}/${STATE_REPO}/git/refs/heads/${STATE_BRANCH}`, { notFoundOk: true });
  if (!branch) return { entries: [], sha: null };
  const file = ghApiObject(`repos/${OWNER}/${STATE_REPO}/contents/${STATE_PATH}?ref=${STATE_BRANCH}`, { notFoundOk: true });
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
      (comment) => markerBot(comment.body) === DISPATCHER_MARKER_BOT && extractRunId(comment.body),
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
  if (!main?.object?.sha) throw new Error('cannot read the main ref of the state repo');
  try {
    gh(['api', '-X', 'POST', `repos/${OWNER}/${STATE_REPO}/git/refs`, '-f', `ref=refs/heads/${STATE_BRANCH}`, '-f', `sha=${main.object.sha}`]);
  } catch {
    // The create usually fails because the branch exists, but the same catch
    // also swallows auth and network failures - verify the ref landed.
    ghApiObject(`repos/${OWNER}/${STATE_REPO}/git/refs/heads/${STATE_BRANCH}`)
      ?? (() => { throw new Error(`state branch ${STATE_BRANCH} does not exist and could not be created`); })();
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
  const result = { busy: [], candidate_count: 0, pick: null, dispatched: false };
  const tally = args['dry-run'] ? { entries: [], sha: null } : loadTally();

  try {
    // Reconcile the oldest unreconciled dispatches on every tick, capped so a
    // backlog cannot stretch the scheduled job.  Before this ran only on
    // dispatch ticks, so a run started on the last dispatch stayed
    // unreconciled until the next dispatch - possibly never.
    let reconciled = 0;
    tally.entries = tally.entries.map((entry) => {
      if (reconciled >= RECONCILE_CAP) return entry;
      if (entry.run_id != null && entry.conclusion != null) return entry;
      reconciled += 1;
      return reconcileEntry(entry);
    });

    const active = autofixRunsActive(repos);
    result.busy = active;

    // Fail closed: any API failure during reconcile or the scans above makes
    // "no active runs" and "no marker yet" untrustworthy, so skip dispatch.
    if (active.length === 0 && apiErrors.length === 0) {
      const candidates = findCandidates(repos);
      result.candidate_count = candidates.length;
      const pick = pickOldest(candidates);
      result.pick = pick;

      if (pick && !args['dry-run'] && apiErrors.length === 0) {
        // Recheck immediately before spending: a run started between the scan
        // and now wins.
        if (autofixRunsActive(repos).length > 0 || apiErrors.length > 0) {
          result.busy = autofixRunsActive(repos);
        } else {
          // Idempotency: a crash between posting the tag and saving the tally
          // would otherwise post a second tag comment on the next tick.
          const prior = ghApiList(
            `repos/${OWNER}/${pick.repo}/issues/${pick.number}/comments?per_page=100`,
            '.[] | {body, html_url, user: {login: .user.login}} | @json',
          ).find(
            (comment) => comment?.user?.login === DISPATCHER_BOT
              && String(comment?.body || '').includes(`${DISPATCH_TAG} dispatcher trial`),
          );
          let commentUrl = prior?.html_url || null;
          if (!prior && apiErrors.length === 0) {
            const posted = JSON.parse(
              gh([
                'api', '-X', 'POST', `repos/${OWNER}/${pick.repo}/issues/${pick.number}/comments`,
                '-f', `body=${dispatchCommentBody()}`,
              ]),
            );
            commentUrl = posted.html_url;
            result.dispatched = true;
            result.comment_url = posted.html_url;
          }
          const known = tally.entries.some(
            (entry) => entry.repo === pick.repo && entry.number === pick.number,
          );
          if (!known) {
            tally.entries.push({
              ts: new Date().toISOString(),
              repo: pick.repo,
              number: pick.number,
              pr_url: pick.url,
              comment_url: commentUrl,
              run_id: prior ? extractRunId(prior.body) : null,
              run_url: null,
              conclusion: null,
              cost_usd: null,
            });
          }
        }
      }
    }
  } finally {
    // Save whatever reconcile learned even when a later step failed: losing it
    // would re-spend lookups every tick and, without the idempotency check
    // above, risk a second tag on the same PR.
    if (!args['dry-run']) saveTally(tally.entries, tally.sha);
  }

  result.tally_total_usd = tallyTotalUsd(tally.entries);
  result.tally_runs = tally.entries.length;
  print(result, args.json);
  if (apiErrors.length > 0) {
    process.stderr.write(`aborted: ${apiErrors.length} GitHub API call(s) failed; refused to act on partial data\n`);
    for (const failure of apiErrors) process.stderr.write(`  ${failure.path}: ${failure.error}\n`);
    process.exit(1);
  }
}

main();
