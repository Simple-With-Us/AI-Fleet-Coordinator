// Pure helpers for the autofix dispatcher.  No I/O here; dispatcher.mjs owns GitHub.
// The tried marker format is owned by pr-autofix-reusable.yml: its runs leave a
// marker comment of the form `swu-autofix-run: bot=<login>` on the PR, where the
// reusable lowercases the login and strips the `[bot]` suffix first (the
// `requester=...` line in pr-autofix-reusable.yml), so swu-echo[bot]'s marker
// reads `swu-autofix-run: bot=swu-echo`.

export const DISPATCHER_BOT = 'swu-echo[bot]';
// The bot login as the reusable actually writes it into a marker comment.
export const DISPATCHER_MARKER_BOT = DISPATCHER_BOT.replace(/\[bot\]$/, '').toLowerCase();
export const DISPATCH_TAG = '@pr-autofix';
export const MARKER_PATTERN = /swu-autofix-run:\s*bot=([\w.-]+(?:\[bot\])?)/;
export const REVIEWER_BOTS = ['kody-ai[bot]', 'chatgpt-codex-connector[bot]', 'sentry[bot]'];

export function markerBot(body) {
  const match = String(body || '').match(MARKER_PATTERN);
  return match ? match[1] : null;
}

// "Autofix has not attempted" means no autofix marker from ANY bot, not only the
// dispatcher's: a PR already fixed once under the one-run-per-bot rule is tried.
export function hasAnyAutofixMarker(issueComments) {
  return (issueComments || []).some((comment) => markerBot(comment?.body) !== null);
}

export function hasReviewFindings(reviewComments, reviewers = REVIEWER_BOTS) {
  const wanted = new Set(reviewers);
  return (reviewComments || []).some(
    (comment) => wanted.has(comment?.user?.login) && String(comment?.body || '').trim() !== '',
  );
}

export function pickOldest(candidates) {
  const sorted = [...(candidates || [])].sort((a, b) => {
    const at = Date.parse(a.created_at) || 0;
    const bt = Date.parse(b.created_at) || 0;
    if (at !== bt) return at - bt;
    const an = `${a.repo}#${a.number}`;
    const bn = `${b.repo}#${b.number}`;
    return an < bn ? -1 : an > bn ? 1 : 0;
  });
  return sorted[0] || null;
}

export function dispatchCommentBody() {
  return `${DISPATCH_TAG} dispatcher trial: this is the oldest open fleet PR with review findings autofix has not attempted.  One autofix run per bot per PR (owner rule 2026-10-20);  this uses ${DISPATCHER_BOT}'s one run here.  cc @jaywedgeworth22`;
}

export function extractCostUsd(logText) {
  const matches = [...String(logText || '').matchAll(/"total_cost_usd"\s*:\s*([0-9.]+)/g)];
  if (!matches.length) return null;
  return Number(matches[matches.length - 1][1]);
}

export function extractRunId(markerCommentBody) {
  const match = String(markerCommentBody || '').match(/actions\/runs\/(\d+)/);
  return match ? match[1] : null;
}

export function tallyTotalUsd(entries) {
  return (entries || []).reduce((sum, entry) => sum + (Number(entry?.cost_usd) || 0), 0);
}

export function parseTally(text) {
  return String(text || '')
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      try {
        return JSON.parse(line);
      } catch {
        return null;
      }
    })
    .filter(Boolean);
}
