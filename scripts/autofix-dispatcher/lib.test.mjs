import { test } from 'node:test';
import assert from 'node:assert/strict';
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
  DISPATCHER_MARKER_BOT,
} from './lib.mjs';

test('markerBot reads the reusable marker format', () => {
  // The reusable strips the [bot] suffix and lowercases before writing.
  assert.equal(markerBot('<!-- swu-autofix-run: bot=swu-echo --> Autofix run ...'), 'swu-echo');
  assert.equal(DISPATCHER_MARKER_BOT, 'swu-echo');
  // The parser also tolerates the unstripped spelling.
  assert.equal(markerBot('swu-autofix-run: bot=kody-ai[bot]'), 'kody-ai[bot]');
  assert.equal(markerBot('no marker here'), null);
});

test('hasAnyAutofixMarker flags any bot marker, not only the dispatcher', () => {
  assert.equal(hasAnyAutofixMarker([{ body: 'swu-autofix-run: bot=swu-minimax[bot]' }]), true);
  assert.equal(hasAnyAutofixMarker([{ body: 'ordinary comment' }]), false);
  assert.equal(hasAnyAutofixMarker([]), false);
});

test('hasReviewFindings needs a nonempty body from a reviewer bot', () => {
  const kody = { user: { login: 'kody-ai[bot]' }, body: '**Bug** something' };
  const human = { user: { login: 'jaywedgeworth22' }, body: 'please fix' };
  const emptyBot = { user: { login: 'kody-ai[bot]' }, body: '   ' };
  assert.equal(hasReviewFindings([human, kody]), true);
  assert.equal(hasReviewFindings([human]), false);
  assert.equal(hasReviewFindings([emptyBot]), false);
});

test('pickOldest sorts by created_at with a stable tiebreak', () => {
  const pick = pickOldest([
    { repo: 'BotFleet', number: 2, created_at: '2026-10-21T01:00:00Z' },
    { repo: 'BotFleet', number: 1, created_at: '2026-10-21T01:00:00Z' },
    { repo: 'Clutch', number: 9, created_at: '2026-10-20T23:00:00Z' },
  ]);
  assert.equal(`${pick.repo}#${pick.number}`, 'Clutch#9');
  assert.equal(pickOldest([]), null);
});

test('dispatchCommentBody carries the tag and the owner cc', () => {
  const body = dispatchCommentBody();
  assert.match(body, /^@pr-autofix\b/);
  assert.match(body, /@jaywedgeworth22/);
});

test('extractCostUsd reads the last total_cost_usd in a log', () => {
  assert.equal(extractCostUsd('{"total_cost_usd": 0} ... {"total_cost_usd": 0.42}'), 0.42);
  assert.equal(extractCostUsd('no cost'), null);
});

test('extractRunId reads the run id from the marker comment', () => {
  const body = '<!-- swu-autofix-run: bot=swu-echo --> Autofix run https://example.com/org/repo/actions/runs/123456789 started';
  assert.equal(extractRunId(body), '123456789');
  assert.equal(extractRunId('no link'), null);
});

test('tally parses jsonl and sums cost', () => {
  const entries = parseTally('{"cost_usd": 0.4}\nnot json\n{"cost_usd": 0.02}\n');
  assert.equal(entries.length, 2);
  assert.ok(Math.abs(tallyTotalUsd(entries) - 0.42) < 1e-9);
});
